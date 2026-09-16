"""La identidad por teléfono y su bitácora de consentimiento, contra Postgres de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Existe porque lo que esta tarea promete son garantías de la BASE, no de Python: que la
bitácora no se pueda borrar lo sostiene una FK, que la lista de eventos esté cerrada lo
sostiene un CHECK, y que ese CHECK exista en un esquema que no sea `public` es exactamente
lo que la migración 013 demostró que no se puede dar por hecho.

Escribe en el esquema `pruebas_contacto`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import os

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_contacto"

TEL = "573001112201"
TEL_VECINO = "573001112202"


def _url_de_pruebas() -> str:
    """Sin el pooler --rechaza `options` como parámetro de arranque-- y con el `search_path`
    fijado, para que ninguna consulta de aquí pueda tocar `public` por descuido."""
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("falta MAXICARE_DATABASE_URL")
    directa = base.replace("-pooler.", ".")
    separador = "&" if "?" in directa else "?"
    return f"{directa}{separador}options=-csearch_path%3D{ESQUEMA}"


@pytest.fixture(scope="module")
def url() -> str:
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    return _url_de_pruebas()


@pytest.fixture(scope="module")
def esquema(url: str):
    """Crea el esquema, aplica las migraciones, y lo borra al terminar pase lo que pase."""
    base_sin_esquema = url.split("&options=")[0].split("?options=")[0]

    with persistencia.conectar(base_sin_esquema) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()

    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)

    yield url

    with persistencia.conectar(base_sin_esquema) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()


@pytest.fixture(autouse=True)
def _limpio(esquema):
    """Cada prueba arranca sin filas. La bitácora primero: la FK no deja al revés."""
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM consentimientos")
            cur.execute("DELETE FROM contactos")
        conn.commit()
    yield


@pytest.fixture
def conexion_pruebas(esquema):
    """Una conexión abierta contra el esquema de pruebas, cerrada al terminar."""
    with persistencia.conectar(esquema) as conn:
        yield conn


# ==========================================================================================
# La fila nace, y nace vacía
# ==========================================================================================


def test_asegurar_contacto_crea_la_fila_en_blanco(conexion_pruebas):
    fila = persistencia.asegurar_contacto(conexion_pruebas, TEL)

    assert fila["telefono"] == TEL
    assert fila["aviso_mostrado_en"] is None
    assert fila["politica_version"] is None
    # Nace contactable. La baja es algo que el paciente pide, nunca un default.
    assert fila["no_contactar"] is False


def test_asegurar_contacto_es_idempotente(conexion_pruebas):
    """Dos mensajes seguidos del mismo número no crean dos filas ni revientan. El candado de
    `atencion` es de proceso: no protege entre réplicas."""
    primera = persistencia.asegurar_contacto(conexion_pruebas, TEL)
    segunda = persistencia.asegurar_contacto(conexion_pruebas, TEL)

    assert primera["creado_en"] == segunda["creado_en"]

    with conexion_pruebas.cursor() as cur:
        cur.execute("SELECT count(*) FROM contactos WHERE telefono = %s", (TEL,))
        assert cur.fetchone()[0] == 1


def test_leer_contacto_de_un_numero_que_no_existe(conexion_pruebas):
    """`leer_contacto` NO crea. Es lo que usan el despachador y las tools, que no tienen por
    qué inventar una fila solo por consultar."""
    assert persistencia.leer_contacto(conexion_pruebas, TEL) is None


# ==========================================================================================
# El aviso
# ==========================================================================================


def test_marcar_aviso_mostrado_deja_fecha_version_y_bitacora(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TEL)

    persistencia.marcar_aviso_mostrado(conexion_pruebas, TEL, version="politica-2026-09")

    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila["aviso_mostrado_en"] is not None
    assert fila["politica_version"] == "politica-2026-09"

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT evento, origen, politica_version FROM consentimientos WHERE telefono = %s",
            (TEL,),
        )
        assert cur.fetchall() == [("aviso_mostrado", "codigo", "politica-2026-09")]


# ==========================================================================================
# La baja y su revocación
# ==========================================================================================


def test_pedir_baja_apaga_y_deja_rastro(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TEL)

    cambio = persistencia.pedir_baja(conexion_pruebas, TEL, detalle="no me escriban mas")

    assert cambio is True
    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila["no_contactar"] is True
    assert fila["no_contactar_en"] is not None
    assert fila["no_contactar_origen"] == "paciente"


def test_pedir_baja_dos_veces_no_cambia_el_estado_pero_si_deja_las_dos_peticiones(
    conexion_pruebas,
):
    """El estado es uno; las peticiones son dos hechos distintos y los dos ocurrieron."""
    persistencia.asegurar_contacto(conexion_pruebas, TEL)

    assert persistencia.pedir_baja(conexion_pruebas, TEL) is True
    assert persistencia.pedir_baja(conexion_pruebas, TEL) is False

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM consentimientos WHERE telefono = %s AND evento = %s",
            (TEL, "baja_solicitada"),
        )
        assert cur.fetchone()[0] == 2


def test_revocar_baja_la_levanta_y_conserva_las_dos_decisiones(conexion_pruebas):
    """Es su derecho, y el historial de las dos es lo que lo hace acreditable."""
    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.pedir_baja(conexion_pruebas, TEL)

    assert persistencia.revocar_baja(conexion_pruebas, TEL) is True

    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila["no_contactar"] is False
    assert fila["no_contactar_origen"] is None

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT evento FROM consentimientos WHERE telefono = %s ORDER BY id", (TEL,)
        )
        assert [f[0] for f in cur.fetchall()] == ["baja_solicitada", "baja_revocada"]


def test_la_baja_de_un_numero_no_toca_a_su_vecino(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.asegurar_contacto(conexion_pruebas, TEL_VECINO)

    persistencia.pedir_baja(conexion_pruebas, TEL)

    assert persistencia.leer_contacto(conexion_pruebas, TEL_VECINO)["no_contactar"] is False


# ==========================================================================================
# Un teléfono que nunca pasó por `asegurar_contacto`
#
# Las tres funciones de abajo llaman a `anotar_consentimiento`, que inserta en
# `consentimientos` -- y esa tabla tiene una FK hacia `contactos`. Sin fila padre, ese
# INSERT viola la FK y tumba la transacción ENTERA de la conexión, no solo la escritura del
# consentimiento: un paciente que nunca escribió antes y pide la baja en el mismo aliento
# ("hola, no me escriban más") se quedaría sin baja, sin bitácora y con el turno de WhatsApp
# reventado. Las tres funciones se protegen llamando a `asegurar_contacto` de entrada.
# ==========================================================================================


def test_pedir_baja_sobre_un_numero_que_nunca_escribio_no_revienta(conexion_pruebas):
    assert persistencia.leer_contacto(conexion_pruebas, TEL) is None

    cambio = persistencia.pedir_baja(conexion_pruebas, TEL)

    assert cambio is True
    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila is not None
    assert fila["no_contactar"] is True

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT evento FROM consentimientos WHERE telefono = %s ORDER BY id", (TEL,)
        )
        assert [f[0] for f in cur.fetchall()] == ["baja_solicitada"]


def test_revocar_baja_sobre_un_numero_que_nunca_escribio_no_revienta(conexion_pruebas):
    assert persistencia.leer_contacto(conexion_pruebas, TEL) is None

    cambio = persistencia.revocar_baja(conexion_pruebas, TEL)

    assert cambio is False, "no había baja que levantar, pero la fila se aseguró igual"
    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila is not None
    assert fila["no_contactar"] is False

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT evento FROM consentimientos WHERE telefono = %s ORDER BY id", (TEL,)
        )
        assert [f[0] for f in cur.fetchall()] == ["baja_revocada"]


def test_marcar_aviso_mostrado_sobre_un_numero_que_nunca_escribio_no_revienta(
    conexion_pruebas,
):
    assert persistencia.leer_contacto(conexion_pruebas, TEL) is None

    persistencia.marcar_aviso_mostrado(conexion_pruebas, TEL, version="politica-2026-09")

    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila is not None
    assert fila["aviso_mostrado_en"] is not None
    assert fila["politica_version"] == "politica-2026-09"

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT evento FROM consentimientos WHERE telefono = %s ORDER BY id", (TEL,)
        )
        assert [f[0] for f in cur.fetchall()] == ["aviso_mostrado"]


# ==========================================================================================
# Lo que la BASE garantiza, y no Python
# ==========================================================================================


def test_un_evento_inventado_lo_rechaza_la_base(conexion_pruebas):
    """El CHECK tiene que existir EN ESTE esquema, no solo en `public`. Es lo que la
    migración 013 demostró que no se puede dar por hecho."""
    import psycopg

    persistencia.asegurar_contacto(conexion_pruebas, TEL)

    with pytest.raises(psycopg.errors.CheckViolation):
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "INSERT INTO consentimientos (telefono, evento, origen) VALUES (%s, %s, %s)",
                (TEL, "porque_si", "codigo"),
            )
    conexion_pruebas.rollback()


def test_un_origen_inventado_lo_rechaza_la_base(conexion_pruebas):
    import psycopg

    persistencia.asegurar_contacto(conexion_pruebas, TEL)

    with pytest.raises(psycopg.errors.CheckViolation):
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "INSERT INTO consentimientos (telefono, evento, origen) VALUES (%s, %s, %s)",
                (TEL, "aviso_mostrado", "el_vecino"),
            )
    conexion_pruebas.rollback()


def test_no_se_puede_borrar_un_contacto_con_bitacora(conexion_pruebas):
    """La bitácora es el registro legal. Que no se pueda borrar no es una convención: es una
    FK con ON DELETE RESTRICT.

    Postgres distingue el RESTRICT explícito del NO ACTION por defecto: el primero devuelve
    SQLSTATE 23001 (`RestrictViolation`), no 23503 (`ForeignKeyViolation`). Comprobado contra
    Neon real -- el brief original pedía `ForeignKeyViolation` aquí, y esa clase nunca se
    lanza para un ON DELETE RESTRICT.
    """
    import psycopg

    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.pedir_baja(conexion_pruebas, TEL)

    with pytest.raises(psycopg.errors.RestrictViolation):
        with conexion_pruebas.cursor() as cur:
            cur.execute("DELETE FROM contactos WHERE telefono = %s", (TEL,))
    conexion_pruebas.rollback()


# ==========================================================================================
# /clearstate — la excepción que es el punto de toda la tarea
# ==========================================================================================


def test_clearstate_borra_el_aviso_pero_NUNCA_la_baja(conexion_pruebas):
    """Resetear a alguien lo devuelve a cero, pero nunca lo devuelve a la lista de
    contactables. Ese «no» es del paciente, no del sistema.

    Mismo precedente que los ejemplos de `casos_sin_resolver`, que se borran sin bajar el
    contador (no negociable 22).
    """
    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.marcar_aviso_mostrado(conexion_pruebas, TEL, version="politica-2026-09")
    persistencia.pedir_baja(conexion_pruebas, TEL)

    persistencia.borrar_rastro(conexion_pruebas, TEL)

    fila = persistencia.leer_contacto(conexion_pruebas, TEL)
    assert fila is not None, "la fila de contacto no se borra nunca"
    assert fila["aviso_mostrado_en"] is None, "el aviso sí se resetea: lo volverá a ver"
    assert fila["politica_version"] is None
    assert fila["no_contactar"] is True, "ESTO es lo que no puede pasar nunca"


def test_clearstate_no_toca_la_bitacora_y_anota_el_borrado(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.pedir_baja(conexion_pruebas, TEL)

    persistencia.borrar_rastro(conexion_pruebas, TEL)

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT evento FROM consentimientos WHERE telefono = %s ORDER BY id", (TEL,)
        )
        eventos = [f[0] for f in cur.fetchall()]

    assert eventos == ["baja_solicitada", "rastro_borrado"]


def test_clearstate_sobre_un_numero_sin_contacto_no_revienta(conexion_pruebas):
    """`borrar_rastro` corre sobre números que nunca han escrito. La bitácora tiene una FK:
    insertar `rastro_borrado` sin contacto padre la violaría y tumbaría la transacción
    entera, dejando el reseteo a medias."""
    persistencia.borrar_rastro(conexion_pruebas, TEL_VECINO)

    assert persistencia.leer_contacto(conexion_pruebas, TEL_VECINO) is None

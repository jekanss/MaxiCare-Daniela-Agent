"""Las dos consultas que decidieron el silencio del General, contra Postgres de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Existe porque `tests/test_ingesta.py` dobla `_tema_existente` y `_primer_archivo_de_la_tanda`
con `monkeypatch` --tiene que hacerlo: es una suite offline-- y un doble no ejecuta una línea
de SQL. El `make_interval(hours => ...)` y el `tipo = ANY(%s)` de la ventana son sintaxis que
solo el motor valida, y un error ahí no se ve en rojo: las dos funciones atrapan la excepción
y degradan a propósito. El texto se archivaría sin tema y el General volvería a sonar por cada
archivo, con los 578 tests en verde y nadie enterándose.

También cierra el otro extremo: que `_marcar_reenviado` acepte un `telegram_message_id` NULL
no es una promesa de Python, es una columna que tiene que ser nullable.

Escribe en el esquema `pruebas_ingesta`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from maxicare_daniela import ingesta, persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_ingesta"

TEL = "573001110101"
TEL_VECINO = "573001110102"


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
def esquema():
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    url = _url_de_pruebas()
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
    """Cada prueba arranca sin filas. `mensajes_entrantes` es la única tabla que tocan."""
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mensajes_entrantes")
        conn.commit()
    yield


def _insertar(url: str, *, wamid: str, telefono: str, tipo: str, hace_horas: float = 0.0,
              reenviado: bool = True) -> None:
    """Una fila como la que deja `_registrar`, con la antigüedad que haga falta."""
    recibido = datetime.now(timezone.utc) - timedelta(hours=hace_horas)
    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mensajes_entrantes
                    (wamid, telefono, nombre_perfil, tipo, recibido_en,
                     telegram_message_id, reenviado_en)
                VALUES (%s, %s, 'Ana', %s, %s, %s, %s)
                """,
                (
                    wamid,
                    telefono,
                    tipo,
                    recibido,
                    77 if reenviado else None,
                    recibido if reenviado else None,
                ),
            )
        conn.commit()


# ==========================================================================================
# `_primer_archivo_de_la_tanda` — quién hace sonar el General
# ==========================================================================================


def test_sin_archivos_previos_el_general_suena(esquema):
    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-nuevo") is True


def test_con_un_archivo_reciente_el_general_se_calla(esquema):
    """La radiografía y, dos minutos después, la foto de la encía son UNA cosa."""
    _insertar(esquema, wamid="w-1", telefono=TEL, tipo="image", hace_horas=0.03)

    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-2") is False


def test_pasada_la_ventana_el_general_vuelve_a_sonar(esquema):
    """A las 24 h la conversación murió; lo que llegue después es un caso nuevo."""
    viejo = ingesta.VENTANA_AVISO_ARCHIVO_HORAS + 1
    _insertar(esquema, wamid="w-1", telefono=TEL, tipo="image", hace_horas=viejo)

    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-2") is True


def test_un_texto_previo_no_calla_el_aviso_del_archivo(esquema):
    """La ventana cuenta ARCHIVOS. Si contara mensajes, el «hola» de las 2:00 dejaría mudo
    al archivo de las 2:03, que es justo lo que el doctor tiene que ver."""
    _insertar(esquema, wamid="w-texto", telefono=TEL, tipo="text", hace_horas=0.05)

    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-foto") is True


def test_un_archivo_que_no_se_entrego_no_calla_al_siguiente(esquema):
    """Si el anterior se quedó por el camino --Telegram caído, un 429-- el doctor nunca lo
    vio. Este no puede heredar un aviso que no llegó a existir."""
    _insertar(
        esquema, wamid="w-1", telefono=TEL, tipo="image", hace_horas=0.05, reenviado=False
    )

    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-2") is True


def test_el_archivo_de_otro_numero_no_calla_el_mio(esquema):
    _insertar(esquema, wamid="w-vecino", telefono=TEL_VECINO, tipo="image", hace_horas=0.05)

    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-mio") is True


def test_la_fila_del_propio_mensaje_no_se_cuenta(esquema):
    """`_registrar` inserta ESTE mensaje antes de que nadie pregunte nada. Si la consulta se
    contara a sí misma, ningún archivo sonaría jamás."""
    _insertar(esquema, wamid="w-yo", telefono=TEL, tipo="image", reenviado=False)

    assert ingesta._primer_archivo_de_la_tanda(esquema, TEL, "w-yo") is True


# ==========================================================================================
# `_tema_existente` — dónde se archiva un texto
# ==========================================================================================


def test_un_numero_sin_ficha_no_tiene_tema(esquema):
    """Y por tanto su texto no se manda a ninguna parte. No es un fallo: es el diseño."""
    assert ingesta._tema_existente(esquema, TEL) is None


def test_el_tema_del_paciente_se_encuentra(esquema):
    with persistencia.conectar(esquema) as conn:
        paciente_id = persistencia.asegurar_paciente(
            conn, nombre_completo="Ana Perez", telefono=TEL
        )
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=901)

    assert ingesta._tema_existente(esquema, TEL) == 901


# ==========================================================================================
# La columna que sostiene «no se reenvió, y no es un fallo»
# ==========================================================================================


def test_se_puede_marcar_procesado_sin_telegram_message_id(esquema):
    """El estado nuevo que la migración 004 no contemplaba: `reenviado_en` puesta y
    `telegram_message_id` NULL significa «se decidió no reenviarlo». Si esa columna no fuera
    nullable, cada texto de un número sin tema reventaría el turno."""
    _insertar(esquema, wamid="w-1", telefono=TEL, tipo="text", reenviado=False)

    ingesta._marcar_reenviado(esquema, "w-1", None, None)

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_message_id, reenviado_en, fallo FROM mensajes_entrantes "
            "WHERE wamid = %s",
            ("w-1",),
        )
        telegram_id, reenviado_en, fallo = cur.fetchone()

    assert telegram_id is None
    assert fallo is None
    # Lo que distingue «no hacía falta» de «entró y nadie lo procesó», que es el estado que
    # el índice `ix_mensajes_sin_reenviar` vigila.
    assert reenviado_en is not None

"""Las consultas que deciden el silencio del General, contra Postgres de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Existe porque `tests/test_ingesta.py` dobla `_tema_existente` y `lectura._textos_sin_archivar`
con `monkeypatch` --tiene que hacerlo: es una suite offline-- y un doble no ejecuta una línea
de SQL. Un error ahí no se ve en rojo: las funciones atrapan la excepción y degradan a
propósito, así que el texto se archivaría sin tema y el volcado del escalamiento saldría vacío
con la suite entera en verde.

Desde el 22/09/2026 la consulta que más falta hace vigilar es la del volcado
(`_textos_sin_archivar`), no la del aviso de tanda: aquella se borró con el aviso, y esta
ganó el `OR media_id IS NOT NULL` que es lo único que impide que una radiografía llegada sin
hilo desaparezca sin dejar rastro.

También cierra el otro extremo: que `_marcar_reenviado` acepte un `telegram_message_id` NULL
no es una promesa de Python, es una columna que tiene que ser nullable.

Escribe en el esquema `pruebas_ingesta`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from maxicare_daniela import ingesta, lectura, persistencia
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
# `lectura._textos_sin_archivar` — lo que el escalamiento rescata
#
# Aqui vivian las siete pruebas de `_primer_archivo_de_la_tanda`, la consulta que decidia si
# el General sonaba una vez por tanda de archivos. El 22/09/2026 MaxiCare pidio que al General
# solo lleguen las alertas de escalamiento, y esa funcion se borro entera.
#
# Lo que la sustituye en importancia es esta otra consulta, y por la misma razon por la que
# aquella estaba aqui: offline va doblada con un `monkeypatch`, `rescatar_hilo` se traga sus
# propias excepciones, y un error de SQL dejaria al doctor abriendo un expediente vacio con
# la suite entera en verde. Ademas es MAS fragil que antes: el `OR media_id IS NOT NULL` es
# nuevo y es justo lo que hace que una radiografia que llego sin hilo deje constancia.
# ==========================================================================================


def _mensaje(url: str, *, wamid: str, telefono: str = TEL, tipo: str = "text",
             texto: str | None = None, transcripcion: str | None = None,
             media_id: str | None = None, archivado: bool = False,
             fallo: str | None = None, hace_horas: float = 0.0) -> None:
    """Una fila de `mensajes_entrantes` con el estado exacto que deja `ingesta`.

    `archivado` es `telegram_message_id` puesto, o sea «ya cayo en un hilo»: lo que hace
    idempotente al rescate.
    """
    recibido = datetime.now(timezone.utc) - timedelta(hours=hace_horas)
    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mensajes_entrantes
                    (wamid, telefono, nombre_perfil, tipo, texto, transcripcion, media_id,
                     recibido_en, telegram_message_id, reenviado_en, fallo)
                VALUES (%s, %s, 'Ana', %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (wamid, telefono, tipo, texto, transcripcion, media_id, recibido,
                 55 if archivado else None, recibido, fallo),
            )
        conn.commit()


def test_el_rescate_trae_el_texto_la_transcripcion_y_el_archivo(esquema):
    """Las tres formas, y el `OR media_id IS NOT NULL` que las junta.

    Antes del 22/09/2026 la consulta exigia `texto IS NOT NULL`, asi que una radiografia que
    llegaba sin hilo desaparecia del volcado. Daba igual mientras el General fuera el destino
    de reserva --el doctor la veia ahi-- y dejo de darlo el dia que ese reenvio se corto.
    """
    _mensaje(esquema, wamid="w-1", texto="Buenas, tengo una duda")
    _mensaje(esquema, wamid="w-2", tipo="image", media_id="media-1")
    _mensaje(esquema, wamid="w-3", tipo="audio", media_id="media-2",
             transcripcion="me duele al masticar")

    filas = lectura._textos_sin_archivar(esquema, TEL)

    assert [f[0] for f in filas] == ["w-1", "w-2", "w-3"], "tienen que salir las tres, en orden"
    lineas = lectura.pendientes_legibles(filas)
    assert "Buenas, tengo una duda" in lineas[0]
    assert "una imagen" in lineas[1], f"el archivo no dejo constancia: {lineas[1]!r}"
    assert "me duele al masticar" in lineas[2]


def test_lo_que_YA_cayo_en_un_hilo_no_se_vuelve_a_volcar(esquema):
    """Lo que hace idempotente al rescate. Sin esto, el segundo escalamiento de la misma
    conversacion repetiria el volcado entero."""
    _mensaje(esquema, wamid="w-1", texto="esto ya esta en su hilo", archivado=True)
    _mensaje(esquema, wamid="w-2", texto="esto no")

    assert [f[0] for f in lectura._textos_sin_archivar(esquema, TEL)] == ["w-2"]


def test_un_mensaje_con_FALLO_no_entra_en_el_volcado(esquema):
    """Ese si se intento entregar y se registro como perdido. Volcarlo aqui lo borraria del
    indice por el que se vigila lo que de verdad fallo."""
    _mensaje(esquema, wamid="w-1", texto="no llego", fallo="Telegram rechazo el mensaje")

    assert lectura._textos_sin_archivar(esquema, TEL) == []


def test_el_volcado_no_es_arqueologia_de_otra_conversacion(esquema):
    """La conversacion caduca a las 24 h. Mas alla no es «lo que este paciente venia
    diciendo», es otro asunto."""
    _mensaje(esquema, wamid="w-viejo", texto="de la semana pasada",
             hace_horas=lectura.HORAS_DE_RESCATE + 1)
    _mensaje(esquema, wamid="w-hoy", texto="de ahora")

    assert [f[0] for f in lectura._textos_sin_archivar(esquema, TEL)] == ["w-hoy"]


def test_el_volcado_no_se_lleva_lo_del_vecino(esquema):
    _mensaje(esquema, wamid="w-vecino", telefono=TEL_VECINO, texto="soy otro")
    _mensaje(esquema, wamid="w-mio", texto="soy yo")

    assert [f[0] for f in lectura._textos_sin_archivar(esquema, TEL)] == ["w-mio"]


def test_un_sticker_sin_nada_dentro_tampoco_se_pierde(esquema):
    """Tiene `media_id` y ni texto ni transcripcion: entra por la tercera rama del `OR`, que
    es la unica que lo ve. Una linea de mas en el expediente es barata; una radiografia que
    nadie sabe que llego, no."""
    _mensaje(esquema, wamid="w-1", tipo="sticker", media_id="media-9")

    (linea,) = lectura.pendientes_legibles(lectura._textos_sin_archivar(esquema, TEL))
    assert "sticker" in linea


def test_la_hora_del_volcado_sale_en_BOGOTA_y_no_en_UTC(esquema):
    """Cinco horas de diferencia. Un doctor leyendo «mando una imagen a las 00:13» sobre algo
    que llego a las 19:13 no reconoce su propia tarde."""
    from zoneinfo import ZoneInfo

    ahora_bogota = datetime.now(ZoneInfo("America/Bogota"))
    _mensaje(esquema, wamid="w-1", tipo="image", media_id="media-1")

    (linea,) = lectura.pendientes_legibles(lectura._textos_sin_archivar(esquema, TEL))
    assert ahora_bogota.strftime("%H:%M") in linea, (
        f"la hora no es la de Bogota: {linea!r}"
    )


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

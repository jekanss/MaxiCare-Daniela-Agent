"""El contador contra la base de verdad. El SQL es lo que se prueba aquí.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Copia las cuatro fixtures de `tests/test_contacto_neon.py` --esquema propio, migrado y
borrado al terminar-- porque lo que estas pruebas garantizan es de la BASE, no de Python: que
el `WHERE` de `sumar_seguimiento_fallido` vaya por teléfono y no alcance a la cartera entera
es justo lo que una prueba doblada no puede ver.

Escribe en el esquema `pruebas_reactivacion`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_reactivacion"

AHORA = datetime(2026, 9, 16, 11, 0, tzinfo=ZONA_BOGOTA)
TELEFONO = "573001112233"


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
    """Cada prueba arranca sin filas. `seguimientos` y `consentimientos` primero: las FK no
    dejan al revés (cuelgan de `conversaciones` y de `contactos`, respectivamente)."""
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seguimientos")
            cur.execute("DELETE FROM consentimientos")
            cur.execute("DELETE FROM contactos")
            cur.execute("DELETE FROM conversaciones")
        conn.commit()
    yield


@pytest.fixture
def conexion_pruebas(esquema):
    """Una conexión abierta contra el esquema de pruebas, cerrada al terminar."""
    with persistencia.conectar(esquema) as conn:
        yield conn


# ==========================================================================================
# El contador (tarea 5)
# ==========================================================================================


def test_el_contador_sube_y_apaga(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    assert persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO) == 1
    assert persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO) == 2


def test_sumar_seguimiento_fallido_asegura_la_fila_como_sus_hermanas(conexion_pruebas):
    """I3 (ronda de revision sobre la parada D): sin `asegurar_contacto` adentro, esta funcion
    es la unica de sus hermanas (`pedir_baja`, `revocar_baja`) que se pierde en silencio
    cuando la fila todavia no existe -- el `UPDATE ... WHERE telefono = %s` no encuentra nada
    que tocar y el `RETURNING` no devuelve fila.

    En WhatsApp no muerde porque `atencion._leer_estado` ya asegura el contacto antes de
    llamar al modelo. Pero el chat web del panel (`runtime.py`) NO pasa por ahi: sin este
    arreglo, la primera vez que alguien prueba "ya no, gracias" desde el panel, Daniela
    confirma el cierre y el contador se queda en cero -- un freno que se da por ejercitado sin
    haberlo sido. Aqui NO se llama a `asegurar_contacto` primero, a proposito: es justo el
    escenario que el hallazgo describe.
    """
    assert persistencia.leer_contacto(conexion_pruebas, TELEFONO) is None
    assert persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO) == 1
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto is not None, "la fila nunca se creo: la llamada se perdio en silencio"
    assert contacto["seguimientos_fallidos"] == 1


def test_agendar_lo_devuelve_a_cero(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.reiniciar_seguimientos_fallidos(conexion_pruebas, TELEFONO)
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto["seguimientos_fallidos"] == 0


def test_el_contador_no_alcanza_a_otro_telefono(conexion_pruebas):
    """Mismo riesgo que la redacción de `detalle`: un WHERE flojo apaga a toda la cartera."""
    otro = "573009998877"
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.asegurar_contacto(conexion_pruebas, otro)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    assert persistencia.leer_contacto(conexion_pruebas, otro)["seguimientos_fallidos"] == 0


def test_clearstate_resetea_el_contador_pero_no_la_baja(conexion_pruebas):
    """El contador es del SISTEMA y se va; `no_contactar` es de la persona y se queda.

    Si la baja se fuera, resetear a alguien la devolvería a la lista de contactables sin que
    nadie se entere (no negociable 25).
    """
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.pedir_baja(conexion_pruebas, TELEFONO, origen="paciente")
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.borrar_rastro(conexion_pruebas, TELEFONO)
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto["seguimientos_fallidos"] == 0
    assert contacto["no_contactar"] is True


# ==========================================================================================
# La anulación por teléfono (tarea 6)
# ==========================================================================================


def test_cerrar_anula_el_segundo_intento_que_vive_en_otra_conversacion(conexion_pruebas):
    """La razón entera de que vaya por teléfono.

    La serie empieza en la conversación A; a los 7 días esa conversación ya caducó y el
    segundo intento sigue colgando de ella. Si la anulación fuera por `id_conversacion` de la
    conversación VIVA, no lo alcanzaría -- y el paciente que acaba de decir que no recibiría
    el mensaje siete días después.
    """
    conv_a = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv_a, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(days=7), clave_idempotencia="k-2",
    )
    conv_b = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)  # la viva
    # 3 pequeña: la prueba entera depende de que `asegurar_conversacion` NUNCA sea un
    # get-or-create (no negociable 5, el nombre miente). Si alguna vez lo fuera, `conv_b`
    # sería igual a `conv_a` y la prueba degeneraría en silencio a "una sola conversación",
    # perdiendo justo lo que dice medir: que la anulación alcanza una conversación DISTINTA.
    assert conv_b != conv_a, "asegurar_conversacion dejó de insertar siempre una fila nueva"
    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no"
    )
    assert anulados == 1, "el segundo intento de la serie sobrevivió al «no»"


def test_cerrar_no_se_lleva_por_delante_el_recordatorio_de_una_cita(conexion_pruebas):
    """I2 (ronda de revisión sobre la parada D): la única línea de esta parada con
    consecuencia clínica.

    Sin el `AND s.tipo <> 'recordatorio_cita'` del WHERE, un paciente con cita mañana que dice
    «ya no me interesa» sobre una promoción se lleva por delante el recordatorio de SU PROPIA
    cita -- exactamente el intercambio que el no negociable 25 prohíbe («la baja es comercial:
    no apaga el recordatorio de una cita»). `anular_seguimientos_de_cita` no entra aquí: ese
    recordatorio cuelga de una reactivación cualquiera, no de una cita, así que la única
    guarda que lo protege es esta.
    """
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="recordatorio_cita",
        fecha_objetivo=AHORA + timedelta(hours=3), clave_idempotencia="k-recordatorio",
    )
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(days=7), clave_idempotencia="k-reactivacion",
    )

    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no"
    )

    assert anulados == 1, "tenía que anular SOLO la reactivación, no las dos filas"
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT anulado_en FROM seguimientos WHERE tipo = 'recordatorio_cita' "
            "AND clave_idempotencia = 'k-recordatorio'"
        )
        (anulado_en,) = cur.fetchone()
    assert anulado_en is None, "el recordatorio de la cita quedó anulado por un «no» ajeno"

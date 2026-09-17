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
    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)  # la viva, otra
    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no"
    )
    assert anulados == 1, "el segundo intento de la serie sobrevivió al «no»"

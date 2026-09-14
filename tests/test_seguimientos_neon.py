"""La cola de recordatorios contra Neon. Solo con `-m neon`."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_seguimientos"


def _url_de_pruebas() -> str:
    """La URL de Neon, sin pooler y apuntada al esquema de pruebas de este archivo.

    Copiado de `tests/test_tools_neon.py::_url_de_pruebas`: el pooler de Neon rechaza
    `options` como parámetro de arranque, y cada archivo de la suite de Neon usa su propio
    esquema para que un `DROP SCHEMA` de una prueba no le borre las tablas a otra.
    """
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


@pytest.fixture
def conexion_pruebas(esquema):
    """Una conexión abierta contra el esquema de pruebas, cerrada al terminar."""
    with persistencia.conectar(esquema) as conn:
        yield conn


@pytest.fixture
def cita_de_prueba(esquema):
    """Una conversación y una cita ya creadas en el esquema de pruebas.

    Copiado del montaje que ya usa `tests/test_tools_neon.py` para crear citas
    (`asegurar_paciente` + `asegurar_conversacion` + `registrar_cita`, directos y sin pasar
    por las tools) -- aquí no hace falta jornada ni disponibilidad, solo una fila de verdad
    de la que colgar un seguimiento.
    """
    telefono = "573000000099"
    with persistencia.conectar(esquema) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo="Paciente De Seguimiento", telefono=telefono
        )
        id_conversacion = persistencia.asegurar_conversacion(
            conn, telefono=telefono, paciente_id=id_paciente
        )
        id_cita = persistencia.registrar_cita(
            conn,
            reserva_id=None,
            conversacion_id=id_conversacion,
            paciente_id=id_paciente,
            nombre_completo="Paciente De Seguimiento",
            telefono=telefono,
            tratamiento="limpieza",
            inicio=datetime(2026, 9, 16, 10, 0, tzinfo=ZONA_BOGOTA),
            duracion_minutos=60,
            evento_calendar_id=None,
        )
    return id_cita, id_conversacion


def test_la_017_deja_las_columnas_y_las_perillas(conexion_pruebas):
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'seguimientos' AND table_schema = current_schema()
            """
        )
        columnas = {fila[0] for fila in cur.fetchall()}

    assert {"cita_id", "anulado_en", "motivo_anulacion", "intentos", "fallo"} <= columnas

    operativa = persistencia.leer_configuracion(conexion_pruebas)
    assert operativa["hora_recordatorio_vispera"] == 18
    assert operativa["horas_minimas_para_recordar"] == 4


def test_la_cascada_anula_el_recordatorio_de_una_cita_que_se_movio(conexion_pruebas, cita_de_prueba):
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)

    assert persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="recordatorio_cita",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:recordatorio:1",
        cita_id=id_cita,
    )

    anulados = persistencia.anular_seguimientos_de_cita(
        conexion_pruebas, id_cita, motivo="cita_reprogramada"
    )
    assert anulados == 1

    pendientes = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )
    assert [p for p in pendientes if p["cita_id"] == id_cita] == []


def test_un_seguimiento_anulado_no_vuelve_a_la_cola(conexion_pruebas, cita_de_prueba):
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="recordatorio_cita",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:recordatorio:2",
        cita_id=id_cita,
    )
    pendiente = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )[0]

    persistencia.anular_seguimiento(conexion_pruebas, pendiente["id"], motivo="contacto_reciente")

    restantes = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )
    assert pendiente["id"] not in {r["id"] for r in restantes}


def test_la_cola_trae_lo_que_el_despachador_necesita_para_decidir(conexion_pruebas, cita_de_prueba):
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="recordatorio_cita",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:recordatorio:3",
        cita_id=id_cita,
    )

    fila = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )[0]

    # Sin estas claves el despachador tendría que hacer una consulta por guarda.
    assert set(fila) >= {
        "id", "conversacion_id", "cita_id", "tipo", "fecha_objetivo", "intentos",
        "telefono", "nombre_completo", "tratamiento", "cita_inicio", "cita_estado",
        "tomada_por",
    }

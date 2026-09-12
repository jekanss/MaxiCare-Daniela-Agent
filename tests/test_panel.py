"""Las pruebas del panel: el vocabulario de tratamientos y la edición de fichas.

Las que tocan Neon llevan `@pytest.mark.neon` y escriben en el esquema `pruebas`.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv


def _url_de_pruebas() -> str:
    """La conexión DIRECTA con `search_path=pruebas`. El pooler de Neon rechaza `options`
    como parámetro de arranque, así que hay que quitarle el `-pooler.` al host."""
    url = os.environ.get("MAXICARE_DATABASE_URL", "")
    directa = url.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return f"{directa}{sep}options=-csearch_path%3Dpruebas"


@pytest.fixture
def conn():
    # `@pytest.mark.neon` por sí solo no salta nada: en este proyecto el corte real de
    # `uv run pytest -q` lo hace cada fixture, revisando la variable a mano (igual que
    # `test_tools_neon.py::url`). Sin este chequeo, la suite offline intentaría conectarse
    # a Neon de verdad en cada corrida.
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    # Tampoco hay conftest.py en la raíz que cargue el .env todavía (ver `test_tools_neon.py`,
    # que hace lo mismo dentro de su propia fixture): se carga aquí, no en el cuerpo del
    # módulo, para no fijarle el entorno a las demás pruebas antes de que corran.
    cargar_dotenv()
    with psycopg.connect(_url_de_pruebas()) as c:
        with c.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS pruebas")
        c.commit()
        persistencia.aplicar_esquema(c)
        yield c


@pytest.mark.neon
def test_las_migraciones_nuevas_se_aplican(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tratamientos")
        assert cur.fetchone()[0] >= 14
        cur.execute("SELECT to_regclass('pruebas.cambios_configuracion')")
        assert cur.fetchone()[0] is not None


@pytest.mark.neon
def test_postgres_rechaza_una_clave_que_no_es_una_clave(conn):
    """El CHECK, no solo el validador de Python. Una frase clínica no entra ni por SQL."""
    malas = [
        "Carillas",                       # mayúscula
        "carillas esteticas",             # espacio
        "absceso-periapical",             # guion
        "ca",                             # muy corta
        "reconstruccion_de_hemiarcada_superior_izquierda",  # muy larga
    ]
    for clave in malas:
        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO tratamientos (clave, etiqueta) VALUES (%s, %s)", (clave, "x")
            )
        conn.rollback()

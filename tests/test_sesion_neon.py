"""La sesión persistida contra Neon de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Escribe en el esquema `pruebas_sesion`, que se crea y se borra aquí. NUNCA en `public`:
allí hay pacientes reales de una clínica.

El aislamiento NO va por `search_path` --el pooler de Neon rechaza `options` como parámetro
de arranque-- sino por `schema_translate_map`, que cualifica las sentencias al compilarlas.
Que eso valga también para el DDL es parte de lo que estas pruebas comprueban.
"""

from __future__ import annotations

import os

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_sesion"


def _url_directa() -> str:
    """La URL de Neon sin el pooler. Hace falta para crear y borrar el esquema con
    `search_path`, que es lo único de aquí que sigue necesitando la conexión directa."""
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("falta MAXICARE_DATABASE_URL")
    return base.replace("-pooler.", ".")


@pytest.fixture(scope="module")
def url() -> str:
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    return _url_directa()


@pytest.fixture(scope="module")
def esquema(url: str):
    """Crea el esquema, aplica TODAS las migraciones dentro, y lo borra pase lo que pase."""
    separador = "&" if "?" in url else "?"
    con_esquema = f"{url}{separador}options=-csearch_path%3D{ESQUEMA}"

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()

    with persistencia.conectar(con_esquema) as conn:
        persistencia.aplicar_esquema(conn)

    yield con_esquema

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()


def _columnas(conn, tabla: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
            """,
            (ESQUEMA, tabla),
        )
        return {fila[0]: fila[1] for fila in cur.fetchall()}


def test_la_migracion_crea_las_dos_tablas_del_sdk(esquema):
    with persistencia.conectar(esquema) as conn:
        sesiones = _columnas(conn, "agent_sessions")
        mensajes = _columnas(conn, "agent_messages")

    assert set(sesiones) == {"session_id", "created_at", "updated_at"}
    assert set(mensajes) == {"id", "session_id", "message_data", "created_at"}


def test_las_marcas_de_tiempo_son_sin_zona_como_las_escribe_el_sdk(esquema):
    """`TIMESTAMP WITHOUT TIME ZONE`, no `TIMESTAMPTZ` como el resto de este esquema.

    Se respeta lo que el SDK espera en vez de mejorarlo: una columna «mejor» que la que el
    SDK escribe es una incompatibilidad esperando a que alguien active `create_tables=True`
    en una base nueva y se encuentre dos esquemas distintos con el mismo nombre.
    """
    with persistencia.conectar(esquema) as conn:
        sesiones = _columnas(conn, "agent_sessions")
        mensajes = _columnas(conn, "agent_messages")

    assert sesiones["created_at"] == "timestamp without time zone"
    assert sesiones["updated_at"] == "timestamp without time zone"
    assert mensajes["created_at"] == "timestamp without time zone"


def test_borrar_la_sesion_arrastra_sus_mensajes(esquema):
    """El `ON DELETE CASCADE` es lo que hace que `/clearstate` pueda borrar el historial
    con una sola sentencia. Sin él quedarían mensajes huérfanos de una sesión que ya no
    existe, invisibles y sin forma de llegar a ellos."""
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO agent_sessions (session_id) VALUES ('conv-cascada')")
            cur.execute(
                "INSERT INTO agent_messages (session_id, message_data) VALUES (%s, %s)",
                ("conv-cascada", '{"role":"user","content":"hola"}'),
            )
            cur.execute("DELETE FROM agent_sessions WHERE session_id = 'conv-cascada'")
            cur.execute(
                "SELECT count(*) FROM agent_messages WHERE session_id = 'conv-cascada'"
            )
            quedan = cur.fetchone()[0]
        conn.commit()

    assert quedan == 0


def test_el_indice_por_sesion_y_tiempo_existe(esquema):
    """El SDK ordena por `(session_id, created_at)` en cada `get_items`, que es una vez por
    turno. Sin índice eso es un scan de la tabla entera de historiales."""
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (ESQUEMA, "agent_messages"),
        )
        indices = {fila[0] for fila in cur.fetchall()}

    assert "idx_agent_messages_session_time" in indices

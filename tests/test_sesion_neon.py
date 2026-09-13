"""La sesión persistida contra Neon de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Escribe en el esquema `pruebas_sesion`, que se crea y se borra aquí. NUNCA en `public`:
allí hay pacientes reales de una clínica.

Hoy el aislamiento va por `search_path`: este archivo habla `psycopg` crudo, sin SQLAlchemy
de por medio, así que el DDL (crear y borrar el esquema) y las comprobaciones contra
`information_schema` viajan por la conexión directa --sin `-pooler.`-- con
`options=-csearch_path={ESQUEMA}`. La conexión directa no es un capricho: el pooler de Neon
rechaza `options` como parámetro de arranque, así que sin quitarle el `-pooler.` al host no
hay forma de fijar el `search_path` de la sesión.

Cuando las Tareas 3, 7 y 13 amplíen este archivo con `SQLAlchemySession`, ese segundo
mecanismo va a convivir con este: las sesiones del SDK se aislarán por
`schema_translate_map`, que cualifica las sentencias al compilarlas porque ahí sí hay
SQLAlchemy por debajo. Los dos conviven, cada uno con lo suyo: `search_path` para el
`psycopg` crudo de este archivo, `schema_translate_map` para lo que el SDK escriba encima.

------------------------------------------------------------------------------------------
`_correr` -- por qué las pruebas de la Tarea 3 no usan `asyncio.run` a secas
------------------------------------------------------------------------------------------
En Windows, `asyncio.run` arranca un `ProactorEventLoop`, y el modo async de psycopg lo
rechaza en el propio `connect()`: `InterfaceError: Psycopg cannot use the 'ProactorEvent
Loop' to run in async mode`. En Linux -- donde corre el VPS -- este problema no existe:
`SelectorEventLoop` ya es el loop por defecto, así que en producción `asyncio.run` a secas
funciona sin este envoltorio. Se aísla en una función de este módulo, y no se toca la
política global de `asyncio` para toda la sesión de pytest, para no arriesgar otros módulos
que sí puedan depender del loop por defecto de la plataforma.
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from typing import Any, Coroutine, TypeVar

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_sesion"

_T = TypeVar("_T")


def _correr(corutina: Coroutine[Any, Any, _T]) -> _T:
    """`asyncio.run`, salvo en Windows, donde fuerza un `SelectorEventLoop` -- ver el
    docstring del módulo."""
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        try:
            return loop.run_until_complete(corutina)
        finally:
            loop.close()
    return asyncio.run(corutina)


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


# ==========================================================================================
# `sesion_de_agente` contra las tablas reales de la migración (Tarea 3)
# ==========================================================================================


def test_ida_y_vuelta_contra_las_tablas_de_la_migracion(esquema):
    """LA PRUEBA QUE CAZA QUE EL SDK HAYA CAMBIADO SU ESQUEMA.

    Con `create_tables=False`, el SDK escribe contra las tablas que creó la 010. Si una
    versión futura del SDK añade o renombra una columna, esta prueba cae con un error de
    Postgres -- que es exactamente lo que se quiere, porque la alternativa es que la
    migración se quede vieja en silencio y nadie se entere hasta producción.
    """
    sesion = persistencia.sesion_de_agente(
        "conv-ida-vuelta", database_url=esquema.split("?options=")[0], esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "quiero agendar una valoración"},
                {"role": "assistant", "content": "Claro, ¿qué tratamiento te interesa?"},
            ]
        )
        return await sesion.get_items()

    items = _correr(correr())
    _correr(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == [
        "quiero agendar una valoración",
        "Claro, ¿qué tratamiento te interesa?",
    ]


def test_las_tablas_de_sesion_no_se_crean_en_public(esquema):
    """El aislamiento por `schema_translate_map` es lo único que separa una prueba de la
    base real de la clínica. Se comprueba consultando el catálogo, no asumiendo."""
    sesion = persistencia.sesion_de_agente(
        "conv-aislada", database_url=esquema.split("?options=")[0], esquema=ESQUEMA
    )
    _correr(sesion.add_items([{"role": "user", "content": "hola"}]))
    _correr(persistencia.cerrar_engines())

    with persistencia.conectar(_url_directa()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            " WHERE table_schema = 'public' AND table_name = 'agent_messages'"
        )
        en_public = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM %s.agent_messages WHERE session_id = 'conv-aislada'"
            % ESQUEMA
        )
        en_pruebas = cur.fetchone()[0]

    # `public` SÍ tiene la tabla, porque `inicializar_base.py` aplica la 010 allí. Lo que
    # esta prueba exige es que la FILA no haya caído ahí.
    assert en_public == 1, "la 010 debería estar aplicada en public"
    assert en_pruebas == 1, "la fila tenía que caer en el esquema de pruebas"

    with persistencia.conectar(_url_directa()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.agent_messages WHERE session_id = 'conv-aislada'"
        )
        assert cur.fetchone()[0] == 0, "la fila de una prueba acabó en la base de la clínica"


def test_el_historial_sobrevive_a_tirar_la_sesion(esquema):
    """El reinicio simulado: una sesión escribe, se tira, otra con el mismo `session_id`
    lee, y está todo. Es la mitad del entregable de la fase que se puede comprobar sin
    levantar dos procesos; la otra mitad la hace `scripts/probar_persistencia.py`."""
    base = esquema.split("?options=")[0]

    primera = persistencia.sesion_de_agente(
        "conv-reinicio", database_url=base, esquema=ESQUEMA
    )
    _correr(primera.add_items([{"role": "user", "content": "me llamo Ana"}]))
    _correr(persistencia.cerrar_engines())
    del primera

    segunda = persistencia.sesion_de_agente(
        "conv-reinicio", database_url=base, esquema=ESQUEMA
    )
    items = _correr(segunda.get_items())
    _correr(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == ["me llamo Ana"]

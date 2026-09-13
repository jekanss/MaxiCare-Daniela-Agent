# Fase 7 — Persistencia del historial y trazas agrupadas · Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que una conversación sobreviva a reiniciar el proceso y Daniela la retome con su contexto, y que su trace aparezca agrupado por `group_id` sin una sola línea del texto de la conversación.

**Architecture:** El historial deja de vivir en un diccionario de módulo y pasa a `SQLAlchemySession` del SDK sobre la Neon que ya existe, con `session_id = id_conversacion`. Las dos tablas del SDK las crea una migración nuestra (`010_`), no `create_tables=True`. El aislamiento por esquema se consigue con `schema_translate_map` en los `engine_kwargs`, nunca con `search_path` (el pooler de Neon lo rechaza). En paralelo, `config_de_corrida()` pasa a recibir argumentos para que los tres consumidores de modelo del proyecto caigan bajo el mismo `group_id`.

**Tech Stack:** Python 3.12 · openai-agents 0.22.2 (`agents.extensions.memory.SQLAlchemySession`) · SQLAlchemy 2.0.52 (`create_async_engine`) · psycopg 3.3.5 (dialecto `postgresql+psycopg`) · Neon Postgres · pytest 8

**Spec:** `docs/superpowers/specs/2026-09-13-fase-7-persistencia-design.md` (rama `fase-7-persistencia`, commit `3d84c00`)

---

## Global Constraints

Estas valen para **todas** las tareas. Cada tarea las hereda sin repetirlas.

- **La rama de trabajo es `fase-7-persistencia`.** Ya existe y ya tiene el spec. Antes de la Tarea 1, `git checkout fase-7-persistencia && git merge main --no-ff` para traer los arreglos de agendamiento que ya están en producción.
- **Driver asíncrono obligatorio: `postgresql+psycopg`.** No `asyncpg` (la URL de Neon lleva `sslmode=require` y `channel_binding=require`, vocabulario de psycopg) y nunca `postgresql://` a secas (`create_async_engine` revienta en el constructor pidiendo `psycopg2`, un paquete que este proyecto no usa).
- **`create_tables=False` siempre.** Las tablas las crea `migraciones/010_sesiones_agente.sql`.
- **`ensure_ascii=False` siempre** al construir la sesión. El default del SDK es `True` y deja los acentos escapados en la base.
- **`SessionSettings` sin `limit` hasta la Tarea 13.** El orden del spec §4.1 es obligado: persistir sin límite → medir items/turno → fijar el límite con el dato al lado.
- **Las pruebas nunca escriben en `public`.** Esquema `pruebas` para `-m neon`, `pruebas_web` para el chat del panel, `pruebas_atencion` para `probar_atencion.py`. El aislamiento se consigue con `schema_translate_map`, **no** con `options=-csearch_path=` (el pooler lo rechaza; para `search_path` hay que quitarle el `-pooler.` al host).
- **`.env` tiene credenciales reales. Nunca se imprime.** Al mostrar una cadena de conexión, `***@host`.
- **Lo que no se sabe se marca con el literal `"PENDIENTE"`.** Nunca con un valor plausible.
- **No se registran cédulas ni documentos de identidad.** La ausencia de la columna es la política.
- **TDD estricto**: prueba primero, verla fallar, mínimo para que pase, verla pasar, commit. Y cada prueba que importe se valida **rompiendo el código a propósito**: si no cae con el fallo puesto, la prueba no vale y se rehace.
- **La consola de Windows es cp1252.** Todo script bajo `scripts/` empieza con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y usa marcadores ASCII (`OK` / `FALLA` / `->`).
- **Los heredoc de Bash fallan en este entorno.** Para escribir un archivo, la herramienta Write.
- Comandos: `uv run pytest -q` (offline) · `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` (contra Neon).

---

## Estructura de archivos

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `migraciones/010_sesiones_agente.sql` | **nuevo.** `agent_sessions` y `agent_messages` con las columnas exactas del SDK | 2 |
| `src/maxicare_daniela/persistencia.py` | **modificar.** `url_asincrona()`, `sesion_de_agente()`, caché de engines, `cerrar_engines()`, y el borrado del historial dentro de `borrar_rastro` | 1, 3, 4, 8 |
| `src/maxicare_daniela/atencion.py` | **modificar.** `_sesion_de` devuelve la sesión persistida; muere `_sesiones` y `_podar_sesiones`; `atender` gana `sesion_de` inyectable | 5 |
| `src/maxicare_daniela/conversacion.py` | **modificar.** `SesionEnMemoria` pasa a ser explícitamente el doble de pruebas; `_config_de_corrida` recibe el contexto | 5, 11 |
| `src/maxicare_daniela/contratos.py` | **modificar.** `ContextoDaniela.canal` | 10 |
| `src/maxicare_daniela/config.py` | **modificar.** `version_de_prompt()` y `config_de_corrida()` con argumentos | 9, 10 |
| `src/maxicare_daniela/agentes.py` | **modificar.** `VERSION_PROMPT` y `VERSION_PROMPT_LECTOR` | 9 |
| `src/maxicare_daniela/guardrails.py` | **modificar.** `_preguntar` propaga el `group_id` del contexto | 11 |
| `src/maxicare_daniela/lectura.py` | **modificar.** `leer_archivo`/`leer_y_repartir` reciben `group_id` | 11 |
| `src/maxicare_daniela/ingesta.py` | **modificar.** resuelve la conversación viva antes de lanzar el lector | 11 |
| `src/maxicare_daniela/runtime.py` | **modificar.** el chat web usa la sesión persistida en `pruebas_web` | 6 |
| `src/maxicare_daniela/reseteo.py` | **modificar.** solo el docstring: deja de ser cierto que el historial no está en Postgres | 8 |
| `tests/test_persistencia_sesion.py` | **nuevo.** offline: URL, engine, caché | 1, 3, 4 |
| `tests/test_sesion_neon.py` | **nuevo.** contra Neon: migración, ida y vuelta, reinicio, esquema, recorte | 2, 3, 7, 12, 13 |
| `tests/test_trazas.py` | **nuevo.** offline: hash del prompt y `config_de_corrida` con argumentos | 9, 10 |
| `tests/test_atencion.py` | **modificar.** muere `test_las_sesiones_viejas_se_podan`; nace la prueba de que `atender` usa la persistida | 5 |
| `tests/test_reseteo.py` · `test_reseteo_cable.py` · `test_reseteo_neon.py` | **modificar.** el historial ahora sí hay que borrarlo | 8 |
| `tests/test_conversacion.py` · `test_lectura.py` · `test_guardrails.py` | **modificar.** las cuatro pruebas del tracing ganan el `group_id`; no se relajan | 11 |
| `scripts/probar_atencion.py` · `probar_lectura.py` | **modificar.** `atencion._sesiones.clear()` deja de existir | 5 |
| `scripts/probar_persistencia.py` | **nuevo.** el entregable de la fase: reinicio de proceso de verdad | 14 |
| `scripts/medir_historial.py` | **nuevo.** cuenta items por turno sobre `agent_messages` | 13 |

---

## Tarea 0: Preparar la rama

- [ ] **Paso 1: Traer main a la rama de la fase**

```bash
git checkout fase-7-persistencia
git merge main --no-ff -m "Merge: los arreglos de agendamiento, antes de empezar la fase 7"
```

- [ ] **Paso 2: Comprobar que la suite arranca verde**

Run: `uv run pytest -q`
Expected: PASS (450 passed, 41 skipped en el último estado conocido de `main`)

Si algo cae aquí, **para**: es una regresión del merge, no de esta fase, y hay que arreglarla antes de escribir una línea nueva.

---

## Tarea 1: La URL asíncrona

El fallo que esta tarea evita es feo porque miente: `create_async_engine("postgresql://...")` revienta **en el constructor**, antes de tocar la red, con `ModuleNotFoundError: No module named 'psycopg2'` — un paquete que este proyecto no usa y que mandaría a quien depure al sitio equivocado.

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py` (sección «Acceso a Neon», junto a `conectar`)
- Test: `tests/test_persistencia_sesion.py` (nuevo)

**Interfaces:**
- Consumes: nada.
- Produces: `persistencia.url_asincrona(url: str) -> str`

- [ ] **Paso 1: Escribir las pruebas que fallan**

Crear `tests/test_persistencia_sesion.py`:

```python
"""La fábrica de sesiones persistidas, sin tocar la red.

Todo lo de aquí corre offline: construir un `AsyncEngine` no abre ninguna conexión --el
pool es perezoso-- así que se puede comprobar el dialecto, el esquema y el pool sin Neon.
Lo que sí necesita base vive en `tests/test_sesion_neon.py`.
"""

from __future__ import annotations

import pytest

from maxicare_daniela import persistencia

URL_NEON = (
    "postgresql://usuario:clave@ep-algo-pooler.c-5.us-east-2.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def test_una_url_sincrona_se_reescribe_al_dialecto_asincrono():
    assert persistencia.url_asincrona("postgresql://u:c@host/db").startswith(
        "postgresql+psycopg://"
    )


def test_los_parametros_de_consulta_sobreviven_intactos():
    """`sslmode` y `channel_binding` son de psycopg y son obligatorios en Neon. Perderlos
    no daría un error de sintaxis: daría una conexión rechazada en producción."""
    reescrita = persistencia.url_asincrona(URL_NEON)

    assert "sslmode=require" in reescrita
    assert "channel_binding=require" in reescrita
    assert "ep-algo-pooler.c-5.us-east-2.aws.neon.tech/neondb" in reescrita


def test_una_url_ya_reescrita_no_se_reescribe_dos_veces():
    """Idempotente a propósito: esta función la puede llamar más de un sitio, y
    `postgresql+psycopg+psycopg://` no es un dialecto."""
    una_vez = persistencia.url_asincrona(URL_NEON)

    assert persistencia.url_asincrona(una_vez) == una_vez


def test_otro_dialecto_asincrono_se_respeta():
    """Si alguien fija `asyncpg` a propósito en el entorno, esto no se lo pisa."""
    url = "postgresql+asyncpg://u:c@host/db"

    assert persistencia.url_asincrona(url) == url


def test_una_url_vacia_se_rechaza_con_un_mensaje_util():
    with pytest.raises(ValueError, match="MAXICARE_DATABASE_URL"):
        persistencia.url_asincrona("")
```

- [ ] **Paso 2: Verlas fallar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q`
Expected: FAIL con `AttributeError: module 'maxicare_daniela.persistencia' has no attribute 'url_asincrona'`

- [ ] **Paso 3: Implementar el mínimo**

En `persistencia.py`, justo después de `conectar`:

```python
#: El dialecto asíncrono de este proyecto. `SQLAlchemySession.from_url` llama a
#: `create_async_engine`, que exige un driver async: `postgresql://` a secas falla EN EL
#: CONSTRUCTOR, antes de tocar la red, con `ModuleNotFoundError: No module named 'psycopg2'`
#: -- un paquete que este proyecto no usa, así que el rastro apunta al sitio equivocado.
#:
#: `psycopg` y no `asyncpg`, aunque los dos están instalados y los dos funcionan: la URL de
#: Neon lleva `sslmode=require` y `channel_binding=require` como parámetros de consulta.
#: psycopg 3 los entiende porque son suyos; asyncpg usa otro vocabulario (`ssl=`) y habría
#: que traducirlos a mano. Además psycopg 3 ya es el driver del proyecto: entra un dialecto
#: nuevo, no una librería nueva.
DIALECTO_ASINCRONO = "postgresql+psycopg"


def url_asincrona(url: str) -> str:
    """La misma URL, con el dialecto que `create_async_engine` acepta.

    Idempotente: una URL que ya trae `+driver` se devuelve tal cual, incluso si el driver
    es otro. Quien fije `asyncpg` a propósito en el entorno no se lo encuentra pisado.
    """
    if not url.strip():
        raise ValueError(
            "MAXICARE_DATABASE_URL está vacía: no hay base a la que persistir el historial"
        )
    esquema, separador, resto = url.partition("://")
    if not separador:
        raise ValueError(f"no parece una URL de base de datos: {url[:20]}...")
    if "+" in esquema:
        return url
    return f"{DIALECTO_ASINCRONO}{separador}{resto}"
```

- [ ] **Paso 4: Verlas pasar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q`
Expected: PASS (5 passed)

- [ ] **Paso 5: Romper el código a propósito**

Cambiar el `return` final por `return url` (es decir, no reescribir nada) y correr las pruebas.
Expected: FALLA `test_una_url_sincrona_se_reescribe_al_dialecto_asincrono`.
Deshacer el cambio.

- [ ] **Paso 6: Commit**

```bash
git add src/maxicare_daniela/persistencia.py tests/test_persistencia_sesion.py
git commit -m "feat: la URL de Neon reescrita al dialecto asincrono que el SDK exige"
```

---

## Tarea 2: La migración 010

**Files:**
- Create: `migraciones/010_sesiones_agente.sql`
- Test: `tests/test_sesion_neon.py` (nuevo)

**Interfaces:**
- Consumes: `persistencia.aplicar_esquema(conn)`, que ya aplica todo `migraciones/*.sql` en orden de nombre.
- Produces: las tablas `agent_sessions` y `agent_messages` en el esquema donde apunte la conexión.

Las columnas no son de nuestro gusto: son las que el SDK escribe. Verificadas por introspección de `agents.extensions.memory.sqlalchemy_session` 0.22.2:

```
agent_sessions   session_id  String  primary_key
                 created_at  TIMESTAMP(timezone=False)  NOT NULL  server_default CURRENT_TIMESTAMP
                 updated_at  TIMESTAMP(timezone=False)  NOT NULL  server_default CURRENT_TIMESTAMP

agent_messages   id           Integer primary_key autoincrement
                 session_id   String  NOT NULL  ForeignKey(agent_sessions.session_id, ondelete=CASCADE)
                 message_data Text    NOT NULL
                 created_at   TIMESTAMP(timezone=False)  NOT NULL  server_default CURRENT_TIMESTAMP
                 Index idx_agent_messages_session_time (session_id, created_at)
```

- [ ] **Paso 1: Escribir la prueba que falla**

Crear `tests/test_sesion_neon.py`:

```python
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
```

- [ ] **Paso 2: Verla fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_sesion_neon.py -q`
Expected: FAIL — las cuatro caen. `test_la_migracion_crea_las_dos_tablas_del_sdk` con `assert set() == {...}` porque las tablas no existen.

- [ ] **Paso 3: Escribir la migración**

Crear `migraciones/010_sesiones_agente.sql`:

```sql
-- =========================================================================================
-- El historial de la conversación, que hasta ahora moría con el proceso
--
-- Estas DOS tablas no son nuestras: son las que `agents.extensions.memory.SQLAlchemySession`
-- espera encontrar. Las columnas, los tipos y el nombre del índice están copiados de la
-- definición del SDK 0.22.2, introspeccionada, no adivinada.
--
-- POR QUÉ UNA MIGRACIÓN Y NO `create_tables=True`, que el SDK ofrece gratis:
-- el esquema completo de este proyecto se lee en `migraciones/`, `inicializar_base.py` es la
-- puerta única, y el proceso web no ejecuta DDL al arrancar. El riesgo del camino elegido
-- --que una versión futura del SDK cambie su esquema y esta migración quede vieja, en
-- silencio-- lo caza `tests/test_sesion_neon.py`, que hace un ida y vuelta REAL contra estas
-- tablas con `create_tables=False`: si el SDK cambia, esa prueba cae.
--
-- `TIMESTAMP` SIN ZONA, a diferencia del resto de este esquema, que usa `TIMESTAMPTZ`.
-- No es un descuido y no se «mejora»: el SDK declara `TIMESTAMP(timezone=False)` y compara
-- contra `CURRENT_TIMESTAMP`. Una columna mejor que la que el SDK escribe es una
-- incompatibilidad esperando.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS agent_sessions (
    -- El `id_conversacion` de `conversaciones`. NO es una clave foránea a propósito: el SDK
    -- declara esta columna como `String` suelta y `pruebas_web` tiene las dos tablas sin
    -- que sus conversaciones vivan en el mismo sitio. La integridad la sostiene quien
    -- escribe, que es siempre `persistencia.sesion_de_agente`.
    session_id  VARCHAR   PRIMARY KEY,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id           SERIAL    PRIMARY KEY,
    session_id   VARCHAR   NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,

    -- Un item del historial, serializado a JSON por el SDK. Un item NO es un mensaje: una
    -- llamada a tool y su resultado son dos filas. Es la diferencia que hace falsa la
    -- equivalencia «40 items = 5 conversaciones» y por la que el límite se mide en vez de
    -- estimarse (ver `config.LIMITE_HISTORIAL_SESION`).
    message_data TEXT      NOT NULL,

    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- El nombre lo fija el SDK: `idx_{messages_table}_session_time`. Con `messages_table` en su
-- valor por defecto, este. Cambiarlo no rompe nada hoy, pero deja de coincidir con lo que
-- crearía `create_tables=True` en una base nueva, y entonces habría dos índices haciendo lo
-- mismo con nombres distintos.
CREATE INDEX IF NOT EXISTS idx_agent_messages_session_time
    ON agent_messages (session_id, created_at);
```

- [ ] **Paso 4: Verlas pasar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_sesion_neon.py -q`
Expected: PASS (4 passed)

- [ ] **Paso 5: Romper la migración a propósito**

Cambiar `ON DELETE CASCADE` por nada y volver a correr.
Expected: FALLA `test_borrar_la_sesion_arrastra_sus_mensajes` — con un error de integridad, que es aún mejor señal.
Deshacer.

- [ ] **Paso 6: Aplicarla a la base real y verificar**

Run: `uv run python scripts/inicializar_base.py`
Expected: la lista de migraciones aplicadas termina en `010_sesiones_agente.sql`, y la verificación del entregable de la fase 1 sigue en OK.

- [ ] **Paso 7: Commit**

```bash
git add migraciones/010_sesiones_agente.sql tests/test_sesion_neon.py
git commit -m "feat: la migracion 010 crea las tablas de sesion que el SDK espera"
```

---

## Tarea 3: La fábrica de sesiones

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py`
- Test: `tests/test_persistencia_sesion.py`, `tests/test_sesion_neon.py`

**Interfaces:**
- Consumes: `persistencia.url_asincrona()` (Tarea 1), las tablas de la Tarea 2.
- Produces: `persistencia.sesion_de_agente(id_conversacion: str, *, database_url: str, esquema: str | None = None, limite: int | None = None) -> SQLAlchemySession`

- [ ] **Paso 1: Escribir las pruebas offline que fallan**

Añadir a `tests/test_persistencia_sesion.py`:

```python
def test_la_sesion_no_crea_sus_tablas():
    """`create_tables=False` es un no negociable de la fase: las tablas las crea la
    migración 010. Con `True`, el proceso web ejecutaría DDL al arrancar y el esquema del
    proyecto dejaría de leerse entero en `migraciones/`."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._create_tables is False


def test_la_sesion_guarda_los_acentos_sin_escapar():
    """El default del SDK es `ensure_ascii=True` y deja «informacio\\u00f3n» en la base. El
    ida y vuelta sería correcto igual, pero el texto no se lee a ojo, y eso son horas
    perdidas depurando el día que haya que mirar un historial a mano."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._ensure_ascii is False


def test_la_sesion_lleva_el_id_de_la_conversacion():
    sesion = persistencia.sesion_de_agente("conv-abc", database_url=URL_NEON)

    assert sesion.session_id == "conv-abc"


def test_sin_esquema_no_se_traduce_nada():
    """En producción las tablas están en `public`, que es donde apunta la conexión."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    opciones = sesion._engine.sync_engine.get_execution_options()

    assert "schema_translate_map" not in opciones


def test_con_esquema_las_sentencias_se_cualifican_al_compilarlas():
    """`schema_translate_map` y NO `options=-csearch_path=`: el pooler de Neon rechaza
    `options` como parámetro de arranque («unsupported startup parameter in options:
    search_path»). SQLAlchemy cualifica al compilar, no al abrir la conexión, así que el
    pooler no tiene nada que rechazar."""
    sesion = persistencia.sesion_de_agente(
        "conv-1", database_url=URL_NEON, esquema="pruebas_web"
    )
    opciones = sesion._engine.sync_engine.get_execution_options()

    assert opciones["schema_translate_map"] == {None: "pruebas_web"}


def test_el_engine_usa_el_dialecto_asincrono():
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._engine.dialect.name == "postgresql"
    assert sesion._engine.dialect.is_async is True


def test_sin_limite_el_historial_va_entero():
    """Paso 1 de los tres del spec: persistir SIN límite. Medir viene después, y fijar el
    número viene después de medir. Un límite puesto ahora sería otra suposición."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion.session_settings.limit is None
```

- [ ] **Paso 2: Verlas fallar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q`
Expected: FAIL con `AttributeError: module 'maxicare_daniela.persistencia' has no attribute 'sesion_de_agente'`

- [ ] **Paso 3: Implementar el mínimo**

En `persistencia.py`, debajo de `url_asincrona`:

```python
def sesion_de_agente(
    id_conversacion: str,
    *,
    database_url: str,
    esquema: str | None = None,
    limite: int | None = None,
):
    """El historial de una conversación, guardado en Neon y no en la memoria del proceso.

    `session_id = id_conversacion` a propósito: es la unidad que sobrevive a un reinicio y
    la que `/clearstate` borra. Un `session_id` por teléfono ataría para siempre a una
    persona con todo lo que dijo alguna vez, y resetear a primer contacto dejaría de ser
    posible sin perder el historial entero.

    `esquema` existe para los carriles de prueba (`pruebas`, `pruebas_web`,
    `pruebas_sesion`). Va por `schema_translate_map` y NO por `search_path`: el pooler de
    Neon rechaza `options` como parámetro de arranque, y eso ya costó una tarde en la fase 3.
    SQLAlchemy cualifica las sentencias al COMPILARLAS, así que el pooler no ve nada raro.

    `limite` recorta el historial que se le manda al modelo, contando ITEMS y no mensajes
    -- una llamada a tool y su resultado son dos. Mientras valga `None` el historial va
    entero: es el paso 1 de los tres de la fase (persistir, medir, fijar).
    """
    from agents.extensions.memory import SQLAlchemySession
    from agents.memory.session_settings import SessionSettings

    return SQLAlchemySession(
        id_conversacion,
        engine=_engine_de(database_url, esquema),
        create_tables=False,
        session_settings=SessionSettings(limit=limite),
        # Sin esto los acentos quedan escapados (`ó`) en `message_data`. El ida y vuelta
        # es correcto igual; lo que se pierde es poder leer un historial a ojo el día que
        # haga falta mirarlo, y ese día no se avisa con antelación.
        ensure_ascii=False,
    )
```

`_engine_de` todavía no existe; escribirla provisionalmente sin caché para que estas pruebas pasen. La caché es la Tarea 4:

```python
def _engine_de(database_url: str, esquema: str | None):
    from sqlalchemy.ext.asyncio import create_async_engine

    opciones = {"schema_translate_map": {None: esquema}} if esquema else {}
    return create_async_engine(url_asincrona(database_url), execution_options=opciones)
```

- [ ] **Paso 4: Verlas pasar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q`
Expected: PASS (12 passed)

- [ ] **Paso 5: Escribir la prueba de ida y vuelta contra Neon**

Añadir a `tests/test_sesion_neon.py`:

```python
def test_ida_y_vuelta_contra_las_tablas_de_la_migracion(esquema):
    """LA PRUEBA QUE CAZA QUE EL SDK HAYA CAMBIADO SU ESQUEMA.

    Con `create_tables=False`, el SDK escribe contra las tablas que creó la 010. Si una
    versión futura del SDK añade o renombra una columna, esta prueba cae con un error de
    Postgres -- que es exactamente lo que se quiere, porque la alternativa es que la
    migración se quede vieja en silencio y nadie se entere hasta producción.
    """
    import asyncio

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

    items = asyncio.run(correr())
    asyncio.run(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == [
        "quiero agendar una valoración",
        "Claro, ¿qué tratamiento te interesa?",
    ]


def test_las_tablas_de_sesion_no_se_crean_en_public(esquema):
    """El aislamiento por `schema_translate_map` es lo único que separa una prueba de la
    base real de la clínica. Se comprueba consultando el catálogo, no asumiendo."""
    import asyncio

    sesion = persistencia.sesion_de_agente(
        "conv-aislada", database_url=esquema.split("?options=")[0], esquema=ESQUEMA
    )
    asyncio.run(sesion.add_items([{"role": "user", "content": "hola"}]))
    asyncio.run(persistencia.cerrar_engines())

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
    import asyncio

    base = esquema.split("?options=")[0]

    primera = persistencia.sesion_de_agente(
        "conv-reinicio", database_url=base, esquema=ESQUEMA
    )
    asyncio.run(primera.add_items([{"role": "user", "content": "me llamo Ana"}]))
    asyncio.run(persistencia.cerrar_engines())
    del primera

    segunda = persistencia.sesion_de_agente(
        "conv-reinicio", database_url=base, esquema=ESQUEMA
    )
    items = asyncio.run(segunda.get_items())
    asyncio.run(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == ["me llamo Ana"]
```

- [ ] **Paso 6: Verlas fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_sesion_neon.py -q`
Expected: FAIL con `AttributeError: ... has no attribute 'cerrar_engines'` (llega en la Tarea 4).

Si se quiere verlas fallar por la razón correcta antes de la Tarea 4, sustituir cada `asyncio.run(persistencia.cerrar_engines())` por `asyncio.run(sesion._engine.dispose())` temporalmente. **Deshacerlo** al llegar al Paso 4 de la Tarea 4.

- [ ] **Paso 7: Commit**

```bash
git add src/maxicare_daniela/persistencia.py tests/test_persistencia_sesion.py tests/test_sesion_neon.py
git commit -m "feat: la fabrica de sesiones persistidas, con el esquema por schema_translate_map"
```

---

## Tarea 4: Un engine por base, no uno por turno

Cada `AsyncEngine` trae su propio pool de conexiones. Construir uno por turno abre un pool nuevo cada vez que un paciente escribe, y Neon tiene un techo de conexiones.

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py`
- Test: `tests/test_persistencia_sesion.py`

**Interfaces:**
- Produces: `persistencia.cerrar_engines() -> None` (corrutina), `persistencia.TAMANO_POOL_SESIONES`

- [ ] **Paso 1: Medir antes de elegir el número**

Esto no es una prueba: es el dato que la constante necesita al lado. Correr:

```bash
uv run python -c "from maxicare_daniela import config, persistencia; config.cargar_dotenv(); import os; url=os.environ['MAXICARE_DATABASE_URL'].replace('-pooler.','.'); conn=persistencia.conectar(url); cur=conn.cursor(); cur.execute('SHOW max_connections'); print('max_connections:', cur.fetchone()[0]); cur.execute(\"SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()\"); print('en uso ahora:', cur.fetchone()[0]); conn.close()"
```

Anotar los dos números. El segundo pool tiene que caber debajo del techo **contando que un despliegue solapa brevemente el contenedor viejo y el nuevo**, o sea contando el doble.

Nota sobre el otro pool: `persistencia.conectar` **no tiene pool** — abre una conexión por llamada y la cierra. Su pico es el número de turnos concurrentes, que con un solo worker y el volumen de la clínica es un dígito. El spec §4.5 hablaba de «el pool de psycopg»; no existe, y esto lo deja escrito para que nadie lo busque.

- [ ] **Paso 2: Escribir las pruebas que fallan**

Añadir a `tests/test_persistencia_sesion.py`:

```python
def test_dos_sesiones_de_la_misma_base_comparten_el_engine():
    """Un `AsyncEngine` por turno es un pool de conexiones por turno, y Neon tiene techo.
    El engine se comparte; lo que es barato de crear es la `SQLAlchemySession`."""
    a = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    b = persistencia.sesion_de_agente("conv-2", database_url=URL_NEON)

    assert a._engine is b._engine


def test_esquemas_distintos_no_comparten_engine():
    """`schema_translate_map` va en el engine, así que `public` y `pruebas_web` no pueden
    compartir uno: compartirlo mandaría las filas del chat de pruebas a la base real."""
    produccion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    pruebas = persistencia.sesion_de_agente(
        "conv-1", database_url=URL_NEON, esquema="pruebas_web"
    )

    assert produccion._engine is not pruebas._engine


def test_el_pool_esta_dimensionado_a_mano():
    """No el default de SQLAlchemy, que nadie eligió para este proyecto ni para el techo de
    conexiones de este plan de Neon."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    pool = sesion._engine.pool

    assert pool.size() == persistencia.TAMANO_POOL_SESIONES


def test_cerrar_engines_vacia_la_cache():
    """Sin esto, una prueba deja un engine atado a su bucle de eventos y la siguiente se
    encuentra un `got Future attached to a different loop`."""
    import asyncio

    persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    assert persistencia._engines

    asyncio.run(persistencia.cerrar_engines())

    assert not persistencia._engines
```

- [ ] **Paso 3: Verlas fallar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q`
Expected: FAIL — `test_dos_sesiones_de_la_misma_base_comparten_el_engine` con `assert <AsyncEngine> is <AsyncEngine>`, y las otras tres con `AttributeError`.

- [ ] **Paso 4: Implementar**

Sustituir el `_engine_de` provisional de la Tarea 3 por:

```python
#: Conexiones que el pool de sesiones mantiene abiertas contra Neon, más las de desbordo.
#:
#: Dimensionado a mano y no heredado del default de SQLAlchemy (5 + 10), que nadie eligió
#: para este proyecto. Los dos datos que lo justifican, medidos el 13/09/2026 contra la Neon
#: de la clínica: PENDIENTE (`SHOW max_connections`) y PENDIENTE en uso. El otro consumidor,
#: `persistencia.conectar`, NO tiene pool: abre una conexión por llamada y la cierra, así
#: que su pico es el número de turnos concurrentes -- un dígito con un solo worker.
#:
#: La suma tiene que caber debajo del techo CONTANDO EL DOBLE, porque un despliegue solapa
#: brevemente el contenedor viejo y el nuevo.
TAMANO_POOL_SESIONES = 3
DESBORDO_POOL_SESIONES = 2

#: Un engine por (base, esquema). Se comparte entre todas las conversaciones: lo caro es el
#: pool, no la `SQLAlchemySession`, que es un objeto con dos tablas y un factory.
_engines: dict[tuple[str, str | None], Any] = {}


def _engine_de(database_url: str, esquema: str | None):
    clave = (database_url, esquema)
    engine = _engines.get(clave)
    if engine is not None:
        return engine

    from sqlalchemy.ext.asyncio import create_async_engine

    opciones = {"schema_translate_map": {None: esquema}} if esquema else {}
    engine = create_async_engine(
        url_asincrona(database_url),
        execution_options=opciones,
        pool_size=TAMANO_POOL_SESIONES,
        max_overflow=DESBORDO_POOL_SESIONES,
        # Neon cierra las conexiones ociosas por su cuenta. Sin `pre_ping`, la primera
        # consulta después de un rato tranquilo revienta con una conexión muerta -- y sería
        # el primer paciente de la mañana quien se lo encontrara.
        pool_pre_ping=True,
        pool_recycle=300,
    )
    _engines[clave] = engine
    return engine


async def cerrar_engines() -> None:
    """Cierra y olvida todos los pools. Lo llaman el apagado del servidor y las pruebas.

    En las pruebas hace falta de verdad: un `AsyncEngine` queda atado al bucle de eventos
    en el que se usó por primera vez, y reusarlo desde otro `asyncio.run` da un
    `got Future attached to a different loop` que no se lee como lo que es.
    """
    for engine in list(_engines.values()):
        await engine.dispose()
    _engines.clear()
```

Y añadir la llamada al apagado del servidor en `runtime.py`, junto a lo que ya se cierre en el `lifespan`.

- [ ] **Paso 5: Verlas pasar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q`
Expected: PASS (16 passed)

- [ ] **Paso 6: Poner los números medidos**

Sustituir los dos `PENDIENTE` del docstring de `TAMANO_POOL_SESIONES` por lo que devolvió el Paso 1, y ajustar los valores si el techo no da margen. **No se dejan en `PENDIENTE`**: el literal es para lo que no se sabe, y esto se mide en treinta segundos.

- [ ] **Paso 7: Verificar la suite entera y las de Neon, en serie**

Run: `uv run pytest -q`
Expected: PASS

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: PASS

**En serie, nunca a la vez.** Las dos suites crean y borran esquemas de prueba en la misma base de Neon: corridas en paralelo se pisan y dan un `UndefinedTable: relation "reservas" does not exist` que parece una regresión y no lo es.

- [ ] **Paso 8: Commit**

```bash
git add src/maxicare_daniela/persistencia.py src/maxicare_daniela/runtime.py tests/test_persistencia_sesion.py
git commit -m "feat: un pool de sesiones por base, dimensionado a mano contra el techo de Neon"
```

---

## Tarea 5: WhatsApp usa la sesión persistida

Aquí es donde la fase cumple su promesa. `_sesiones` —el diccionario de módulo que muere con el proceso— deja de ser el almacén.

**Files:**
- Modify: `src/maxicare_daniela/atencion.py` (`_sesiones` línea 130, `_podar_sesiones` línea 368, `olvidar` línea 427, `_sesion_de` línea 448, `atender` línea 681, `atender` cuerpo línea 903, docstring del módulo línea 50)
- Modify: `src/maxicare_daniela/conversacion.py` (docstring de `SesionEnMemoria`, línea 107)
- Modify: `tests/test_atencion.py` (líneas 342-347, 993-1013)
- Modify: `scripts/probar_atencion.py` (línea 331), `scripts/probar_lectura.py` (línea 287)

**Interfaces:**
- Consumes: `persistencia.sesion_de_agente(id, database_url=..., esquema=...)` (Tarea 3).
- Produces: `atender(..., sesion_de: Callable[[str], Any] | None = None)`; `atencion._sesion_de(id_conversacion: str, database_url: str) -> Any`.

- [ ] **Paso 1: Escribir la prueba que falla**

En `tests/test_atencion.py`, **sustituir** `test_las_sesiones_viejas_se_podan` (líneas 993-1013) por:

```python
def test_el_turno_usa_la_sesion_persistida_y_no_la_de_memoria(monkeypatch):
    """La promesa de la fase 7, comprobada por donde se cumple: `atender` tiene que pedirle
    la sesión a `persistencia`, con el id de la conversación y la base de producción.

    Con `SesionEnMemoria` --lo que había-- esta prueba pasa igual si el historial muere al
    reiniciar, porque en un solo proceso no se nota. Por eso lo que se comprueba aquí es la
    FÁBRICA y no el comportamiento: quién construye la sesión es lo que decide si sobrevive.
    """
    pedidas: list[tuple[str, str]] = []

    def fabrica(id_conversacion, *, database_url, esquema=None, limite=None):
        pedidas.append((id_conversacion, database_url))
        return conversacion.SesionEnMemoria(id_conversacion)

    monkeypatch.setattr(atencion.persistencia, "sesion_de_agente", fabrica)

    sesion = atencion._sesion_de("conv-7", "postgresql://u:c@host/db")

    assert pedidas == [("conv-7", "postgresql://u:c@host/db")]
    assert sesion.session_id == "conv-7"


def test_una_sesion_inyectada_gana_a_la_de_produccion():
    """`sesion_de` existe SOLO para que la suite offline pueda correr sin Neon, igual que
    `calendario` y `dormir`. En producción vale `None` y manda la persistida."""
    import inspect

    firma = inspect.signature(atencion.atender)

    assert "sesion_de" in firma.parameters
    assert firma.parameters["sesion_de"].default is None
```

Y en el fixture de las líneas 342-347, quitar `atencion._sesiones.clear()` (el atributo deja de existir).

- [ ] **Paso 2: Verla fallar**

Run: `uv run pytest tests/test_atencion.py -q -k "sesion_persistida or inyectada"`
Expected: FAIL — la primera con `TypeError: _sesion_de() takes ... positional arguments` (hoy pide `ahora`, no `database_url`); la segunda con `assert 'sesion_de' in ...`.

- [ ] **Paso 3: Implementar**

En `atencion.py`:

1. Borrar la línea `_sesiones: dict[str, tuple[conversacion.SesionEnMemoria, float]] = {}` (130) y la función `_podar_sesiones` entera (368-378). Ajustar el comentario de `_candados` (126-129), que explica por qué la poda «ya no puede colgarse de `_sesiones`» y deja de tener sentido.

2. Sustituir `_sesion_de`:

```python
def _sesion_de(id_conversacion: str, database_url: str):
    """El historial de esta conversación, que ahora vive en Neon y no en este proceso.

    Ya no hay diccionario que podar: el estado se fue a la base. Lo que se construye aquí es
    barato --dos definiciones de tabla y un factory-- porque el pool de conexiones lo tiene
    el engine, que `persistencia` comparte entre todas las conversaciones.
    """
    return persistencia.sesion_de_agente(id_conversacion, database_url=database_url)
```

3. En la firma de `atender`, después de `lectura`:

```python
    sesion_de: Callable[[str], Any] | None = None,
```

y en su docstring, junto a `calendario` y `dormir`:

> `sesion_de` existe **solo para que esto se pueda probar** sin Neon. Por omisión es
> `_sesion_de`, la persistida, que es lo que corre en producción: la suite offline le pasa
> una `SesionEnMemoria` y así sigue corriendo en dos segundos y sin señal.

4. En el cuerpo (línea 903), sustituir `sesion = _sesion_de(estado.id_conversacion, momento_inicio)` por:

```python
        fabricar = sesion_de or (lambda id_: _sesion_de(id_, config.database_url))
        sesion = fabricar(estado.id_conversacion)
```

5. Reescribir el docstring de `olvidar` (427-441): la frase «Las sesiones NO hace falta borrarlas y por eso no se tocan» deja de ser cierta. Pasa a:

```python
    """Saca de la memoria del proceso todo lo de este número. Parte de `/clearstate`.

    Lo que borra de verdad es el búfer: uno que sobreviviera al reseteo metería en el turno
    siguiente --el primero de la conversación «nueva»-- los mensajes de la anterior, y la
    prueba de que Daniela no recuerda nada fallaría por el único sitio que no es la base.

    El historial del diálogo ya NO está aquí: desde la fase 7 vive en `agent_messages`, y lo
    borra `persistencia.borrar_rastro` dentro de la misma transacción que el resto del
    rastro. Esta función no lo toca, y no es un olvido: borrar por dos caminos distintos es
    cómo se acaba con uno de los dos desactualizado.

    Se llama con el candado del teléfono cogido; el candado en sí se deja donde está, porque
    quien llama lo tiene tomado en ese momento.
    """
```

6. En el docstring del módulo (línea 50), donde dice que la fase 7 llegará «cambiando `SesionEnMemoria` por `SQLAlchemySession`», ponerlo en pasado: ya está hecho.

7. En `conversacion.py`, el docstring de `SesionEnMemoria` (107-122): quitar «**No es la sesión del plan**... esa llega en la fase 7» y dejar qué es ahora:

```python
    """Historial de conversación que vive en el proceso y se pierde al reiniciarlo.

    Implementa el *protocolo* `Session` del SDK -- `get_items`, `add_items`, `pop_item`,
    `clear_session` -- sin heredar de `SessionABC`, que la documentación del propio SDK
    reserva para sus implementaciones internas.

    **Es el doble de las pruebas offline, y solo eso.** Lo que corre en producción --y en el
    chat web del panel-- es `persistencia.sesion_de_agente`, sobre Neon, desde la fase 7.
    Esta existe para que `uv run pytest -q` siga corriendo en dos segundos y sin señal: una
    suite que exige internet es una suite que alguien acaba saltándose.

    Un doble SIN historial no serviría: Daniela no recordaría el mensaje anterior y cada
    turno empezaría de cero, que es justo lo que no se quiere probar.
    """
```

- [ ] **Paso 4: Verla pasar**

Run: `uv run pytest tests/test_atencion.py -q`
Expected: FAIL todavía — el resto de la suite de `atencion` llama a `atender` sin `sesion_de` y ahora intentaría hablar con Neon.

- [ ] **Paso 5: Inyectar el doble en la suite offline**

En `tests/test_atencion.py`, `tests/test_muro.py` y `tests/test_webhook_responde.py`, toda llamada a `atencion.atender(...)` gana:

```python
        sesion_de=lambda id_: conversacion.SesionEnMemoria(id_),
```

Localizarlas con: `grep -rn "atender(" tests/`

- [ ] **Paso 6: Verlas pasar**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Paso 7: Arreglar los scripts de fase**

En `scripts/probar_atencion.py` (línea 331) y `scripts/probar_lectura.py` (línea 287), quitar `atencion._sesiones.clear()` y ajustar el docstring de la función que lo hacía. Estos dos scripts corren contra Neon en sus propios esquemas (`pruebas_atencion`, `pruebas_lectura`), así que **usan la sesión persistida de verdad** — que es lo que se quiere: lo que se prueba tiene que ser lo que corre.

Run: `uv run python scripts/probar_atencion.py`
Expected: todo OK, sin `--chat` no gasta tokens.

Run: `uv run python scripts/probar_lectura.py`
Expected: todo OK.

- [ ] **Paso 8: Romper el código a propósito**

En `_sesion_de`, devolver `conversacion.SesionEnMemoria(id_conversacion)` en vez de la persistida.
Expected: FALLA `test_el_turno_usa_la_sesion_persistida_y_no_la_de_memoria`.
Deshacer.

- [ ] **Paso 9: Commit**

```bash
git add src/maxicare_daniela/atencion.py src/maxicare_daniela/conversacion.py tests/ scripts/probar_atencion.py scripts/probar_lectura.py
git commit -m "feat: el historial de WhatsApp sobrevive a reiniciar el proceso"
```

---

## Tarea 6: El chat web usa el mismo mecanismo

Podría quedarse con su diccionario, que es más simple. No se hace por la misma razón por la que Daniela no se parte por canal: **lo que se prueba tiene que ser lo que corre.** Un chat de pruebas con otra persistencia deja de probar la persistencia.

**Files:**
- Modify: `src/maxicare_daniela/runtime.py` (líneas 738, 905-945, 1035-1038)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `persistencia.sesion_de_agente(id, database_url=..., esquema="pruebas_web")`.

- [ ] **Paso 1: Escribir la prueba que falla**

En `tests/test_web.py`:

```python
def test_el_chat_de_pruebas_persiste_en_su_propio_esquema(monkeypatch):
    """El chat del panel usa la MISMA sesión persistida que WhatsApp, apuntada a
    `pruebas_web`. Con un diccionario en memoria dejaría de probar lo que existe para
    probar, y el esquema de pruebas es lo único que impide que una cita de mentira ocupe un
    cupo real de la clínica."""
    pedidas: list[dict] = []

    def fabrica(id_conversacion, *, database_url, esquema=None, limite=None):
        pedidas.append({"id": id_conversacion, "esquema": esquema})
        return conversacion.SesionEnMemoria(id_conversacion)

    monkeypatch.setattr(runtime.persistencia, "sesion_de_agente", fabrica)
    monkeypatch.setattr(runtime, "_preparar_esquema_de_pruebas", lambda: "postgresql://x/y")
    monkeypatch.setattr(
        runtime.persistencia, "asegurar_conversacion", lambda *a, **k: "conv-web-1"
    )
    monkeypatch.setattr(runtime.persistencia, "conectar", _conexion_de_mentira)
    monkeypatch.setattr(runtime.persistencia, "leer_configuracion", lambda conn: {
        "capacidad_por_hora": 2, "duracion_cita_minutos": 60, "cierre_relevo_minutos": 180
    })

    runtime._contexto_de_prueba({"usuario": "ana"}, None)

    assert pedidas == [{"id": "conv-web-1", "esquema": runtime.ESQUEMA_PRUEBAS_WEB}]
```

(`_conexion_de_mentira` es el doble de conexión que ya usa `tests/test_web.py`; reutilizar el que haya en `tests/dobles.py`.)

- [ ] **Paso 2: Verla fallar**

Run: `uv run pytest tests/test_web.py -q -k persiste`
Expected: FAIL con `assert [] == [{'id': 'conv-web-1', ...}]` — nadie llamó a la fábrica.

- [ ] **Paso 3: Implementar**

En `runtime.py`:

1. Línea 738, cambiar el tipo del diccionario. Ya no guarda la sesión, solo el contexto:

```python
#: El contexto vivo de cada conversación del chat de pruebas. El HISTORIAL ya no está aquí:
#: desde la fase 7 vive en `agent_messages` del esquema `pruebas_web`, igual que el de
#: WhatsApp vive en el de `public`. Lo que queda en memoria es el contexto --calendario
#: doble, credenciales vacías de Telegram, configuración operativa-- que se reconstruye solo
#: si el proceso se reinicia y no vale la pena persistir.
_conversaciones_de_prueba: dict[str, ContextoDaniela] = {}
```

2. `_contexto_de_prueba` devuelve `tuple[ContextoDaniela, Any]`, y en vez de `par = (ctx, conversacion.SesionEnMemoria(nuevo))`:

```python
    _conversaciones_de_prueba[nuevo] = ctx
    sesion = persistencia.sesion_de_agente(
        nuevo, database_url=config.database_url, esquema=ESQUEMA_PRUEBAS_WEB
    )
    return ctx, sesion
```

Ojo con la URL: aquí se pasa `config.database_url` **sin** el `options=-csearch_path=`, porque el aislamiento lo pone `esquema=`. La URL con `options` que devuelve `_preparar_esquema_de_pruebas` sigue siendo la que usa `persistencia.conectar` para el resto.

3. Al recuperar una conversación viva (línea 907), reconstruir la sesión en vez de sacarla del diccionario:

```python
    if id_conversacion and id_conversacion in _conversaciones_de_prueba:
        ctx = _conversaciones_de_prueba[id_conversacion]
        return ctx, persistencia.sesion_de_agente(
            id_conversacion, database_url=config.database_url, esquema=ESQUEMA_PRUEBAS_WEB
        )
```

4. Ajustar los dos sitios que iteran el diccionario esperando pares (líneas 970-972 y 1038).

- [ ] **Paso 4: Verla pasar**

Run: `uv run pytest tests/test_web.py -q`
Expected: PASS

- [ ] **Paso 5: Verificar de punta a punta contra Neon**

Run: `uv run python scripts/probar_web.py`
Expected: todo OK (sin `--chat` no gasta tokens).

- [ ] **Paso 6: Commit**

```bash
git add src/maxicare_daniela/runtime.py tests/test_web.py
git commit -m "feat: el chat web persiste su historial en pruebas_web, igual que WhatsApp en public"
```

---

## Tarea 7: El corte no puede caer entre una tool y su salida

```
   … item 38: function_call         consultar_disponibilidad   <-- el corte cae AQUÍ
   ─────────────────────────────────────────────────────────── limit
       item 39: function_call_output [tres horarios]
```

Si la ventana corta ahí, lo que se manda al modelo tiene una llamada huérfana, y esa es una petición que la API rechaza. **No se supone que el SDK lo proteja**: esta prueba lo averigua. Si resulta que lo maneja, la prueba lo documenta; si no, el recorte se hace nuestro y esta prueba es la que lo exige.

Va **antes** de fijar el límite (Tarea 13), a propósito: fijar un número sin saber si el corte es seguro es fijar el número de un fallo.

**Files:**
- Test: `tests/test_sesion_neon.py`
- Possibly modify: `src/maxicare_daniela/persistencia.py`

- [ ] **Paso 1: Escribir la prueba**

Añadir a `tests/test_sesion_neon.py`:

```python
#: Un turno con una tool, en items del SDK. La llamada y su salida son DOS items: es la
#: diferencia que hace falsa la equivalencia «40 items = 5 conversaciones».
_LLAMADA = {
    "type": "function_call",
    "call_id": "call_abc",
    "name": "consultar_disponibilidad",
    "arguments": '{"inicio":"2026-09-16T10:00:00-05:00"}',
}
_SALIDA = {
    "type": "function_call_output",
    "call_id": "call_abc",
    "output": "10:00; 11:00; 14:00",
}


def test_el_recorte_no_deja_una_llamada_a_tool_sin_su_salida(esquema):
    """El borde que más va a doler, y que no aparecería en desarrollo: aparecería en la
    conversación número siete de un paciente real.

    Se construye un historial cuyo `limit` cae EXACTAMENTE entre `function_call` y su
    `function_call_output`, y se exige que lo que vuelve no tenga una llamada huérfana. Una
    petición con una llamada sin salida la rechaza la API de OpenAI.
    """
    import asyncio

    base = esquema.split("?options=")[0]
    sesion = persistencia.sesion_de_agente(
        "conv-borde", database_url=base, esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "hola"},
                {"role": "assistant", "content": "hola, ¿en qué te ayudo?"},
                _LLAMADA,
                _SALIDA,
            ]
        )
        # limit=2 deja fuera los dos primeros y corta justo por el borde de la tool.
        return await sesion.get_items(limit=2)

    items = asyncio.run(correr())
    asyncio.run(persistencia.cerrar_engines())

    llamadas = {i.get("call_id") for i in items if i.get("type") == "function_call"}
    salidas = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}

    assert llamadas <= salidas, (
        f"el recorte dejó una llamada a tool sin su salida: {llamadas - salidas}. "
        "La API de OpenAI rechaza esa petición."
    )


def test_un_corte_impar_tampoco_deja_la_llamada_huerfana(esquema):
    """El caso de verdad: `limit=3` corta entre la llamada y su salida, no entre turnos."""
    import asyncio

    base = esquema.split("?options=")[0]
    sesion = persistencia.sesion_de_agente(
        "conv-borde-impar", database_url=base, esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "hola"},
                _LLAMADA,
                _SALIDA,
                {"role": "assistant", "content": "tengo las 10, 11 y 2"},
            ]
        )
        return await sesion.get_items(limit=3)

    items = asyncio.run(correr())
    asyncio.run(persistencia.cerrar_engines())

    llamadas = {i.get("call_id") for i in items if i.get("type") == "function_call"}
    salidas = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}

    assert llamadas <= salidas, (
        f"el recorte dejó una llamada a tool sin su salida: {llamadas - salidas}"
    )
```

- [ ] **Paso 2: Correrlas y ANOTAR el resultado**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_sesion_neon.py -q -k borde`

Aquí hay dos desenlaces y **los dos son válidos**:

- **PASAN** → el SDK 0.22.2 ya protege el borde. Añadir a cada docstring una línea: «Comprobado contra la 0.22.2: el SDK lo maneja. Esta prueba es la que avisará si deja de hacerlo.» Saltar al Paso 4.
- **FALLAN** → el recorte se hace nuestro. Ir al Paso 3.

- [ ] **Paso 3: Solo si fallan — el recorte propio**

Envolver la sesión con una subclase que, después de recortar, tire hacia atrás los `function_call` cuya salida quedó fuera:

```python
class _SesionConCorteSeguro(SQLAlchemySession):
    """`SQLAlchemySession` que nunca devuelve un `function_call` sin su `function_call_output`.

    El recorte por cantidad de items no sabe de pares: puede cortar entre una llamada a tool
    y su resultado, y la API de OpenAI rechaza esa petición. Aquí se descarta la llamada
    huérfana, que es más barato que traerse la salida (que ya no cabe en la ventana).
    """

    async def get_items(self, limit=None):
        items = await super().get_items(limit)
        salidas = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
        return [
            i
            for i in items
            if i.get("type") != "function_call" or i.get("call_id") in salidas
        ]
```

y devolverla desde `sesion_de_agente`.

- [ ] **Paso 4: Commit**

```bash
git add src/maxicare_daniela/persistencia.py tests/test_sesion_neon.py
git commit -m "test: el recorte del historial no parte una llamada a tool de su salida"
```

---

## Tarea 8: `/clearstate` borra el historial

Hoy resetear un número funciona. Con la sesión persistida dejaría de funcionar **sin que nadie lo note**: el paciente volvería a «primer contacto» con Daniela recordando la conversación anterior.

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py` (`borrar_rastro`, línea 1061)
- Modify: `src/maxicare_daniela/reseteo.py` (docstring del módulo, líneas 17-21)
- Modify: `tests/test_reseteo.py` (líneas 125-137), `tests/test_reseteo_neon.py`

**Interfaces:**
- Consumes: `_CONVERSACIONES_DEL_TELEFONO` (ya existe en `persistencia.py`, línea 1007).
- Produces: `borrar_rastro` devuelve una clave más en su dict: `"agent_sessions"`.

- [ ] **Paso 1: Escribir la prueba que falla**

En `tests/test_reseteo_neon.py`:

```python
def test_clearstate_borra_el_historial_del_agente(esquema):
    """Sin esto, `/clearstate` deja de cumplir lo que promete y nadie se entera: el paciente
    vuelve a primer contacto con Daniela recordando lo de antes.

    Es la clase de fallo que solo se ve probándolo, porque la garantía del módulo --que
    `_leer_estado` devuelva los mismos ocho campos-- se sigue cumpliendo igual.
    """
    telefono = "573001112233"

    with persistencia.conectar(esquema) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo="Ana Prueba", telefono=telefono
        )
        id_conversacion = persistencia.asegurar_conversacion(
            conn, telefono=telefono, paciente_id=id_paciente
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_sessions (session_id) VALUES (%s)", (id_conversacion,)
            )
            cur.execute(
                "INSERT INTO agent_messages (session_id, message_data) VALUES (%s, %s)",
                (id_conversacion, '{"role":"user","content":"me llamo Ana"}'),
            )
        conn.commit()

    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, telefono)

    assert borradas["agent_sessions"] == 1

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agent_messages WHERE session_id = %s", (id_conversacion,)
        )
        assert cur.fetchone()[0] == 0, "quedó historial de un número que se reseteó"
```

- [ ] **Paso 2: Verla fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_reseteo_neon.py -q -k historial`
Expected: FAIL con `KeyError: 'agent_sessions'`

- [ ] **Paso 3: Implementar**

En `borrar_rastro`, **antes** del `DELETE FROM conversaciones` (que es lo único que lo ordena: después de borrar las conversaciones ya no hay forma de saber cuáles eran sus ids):

```python
            # El historial del diálogo, desde la fase 7. Va ANTES de borrar `conversaciones`
            # porque los `session_id` SON los ids de esas conversaciones: después del DELETE
            # no habría forma de saber cuáles eran, y el historial quedaría huérfano y vivo.
            #
            # `agent_messages` no se borra a mano: se va sola por el `ON DELETE CASCADE` de
            # la migración 010. Un segundo DELETE aquí sería un sitio más que mantener.
            cur.execute(
                f"""
                DELETE FROM agent_sessions
                 WHERE session_id IN (
                        SELECT id::text FROM conversaciones
                         WHERE id IN ({_CONVERSACIONES_DEL_TELEFONO})
                 )
                """,
                parametros,
            )
            borradas["agent_sessions"] = cur.rowcount
```

Y en el docstring de `borrar_rastro`, añadir a la explicación del orden que `agent_sessions` va antes que `conversaciones` y por qué.

- [ ] **Paso 4: Verla pasar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_reseteo_neon.py -q`
Expected: PASS

- [ ] **Paso 5: Corregir el docstring de `reseteo.py`, que ahora miente**

Las líneas 17-21 dicen:

> El historial del dialogo NO esta en Postgres: vive en `conversacion.SesionEnMemoria`,
> indexada por `id_conversacion`. Borrada la conversacion, la siguiente nace con un id nuevo
> y una sesion vacia, asi que la memoria no hay que limpiarla: deja de ser alcanzable.

Sustituir por:

```
Por que se puede garantizar: todo lo que Daniela sabe de alguien al empezar un turno sale de
`_leer_estado`, que lee `pacientes`, `conversaciones` y `configuracion` --esta ultima es
global, no del paciente--. El historial del dialogo SI esta en Postgres desde la fase 7:
vive en `agent_sessions` / `agent_messages`, con `session_id = id_conversacion`, y
`persistencia.borrar_rastro` lo borra dentro de la MISMA transaccion que el resto del
rastro. Va antes del `DELETE FROM conversaciones` porque los `session_id` son esos ids: al
reves quedaria historial vivo de una conversacion que ya no existe. Y el campo que manda,
`identidad_verificada`, se calcula como `bool(paciente) or bool(verificada)`: sin fila en
`pacientes` vuelve a `False`.
```

- [ ] **Paso 6: Arreglar la prueba que codificaba lo viejo**

`tests/test_reseteo.py` líneas 125-137 (`de_antes = atencion._sesion_de("conversacion-vieja", time.monotonic())`) usa la firma vieja. Reescribirla contra la nueva realidad: lo que hay que comprobar ya no es que la sesión sea inalcanzable, sino que el borrado la incluye. Dejar que la de Neon (Paso 1) sea la que lo demuestra y sustituir esta por una que compruebe el **orden** de las sentencias con una conexión de mentira que registre el SQL:

```python
def test_el_historial_se_borra_antes_que_las_conversaciones():
    """El orden no es estilo: los `session_id` SON los ids de las conversaciones. Al revés,
    el DELETE de `agent_sessions` no encontraría a qué apuntar y el historial se quedaría
    vivo, invisible, ligado a un número que el sistema dice no conocer."""
    conn = _ConexionQueRegistra()

    persistencia.borrar_rastro(conn, "573001112233")

    sentencias = [s.lower() for s in conn.ejecutadas]
    posicion_historial = next(i for i, s in enumerate(sentencias) if "agent_sessions" in s)
    posicion_conversaciones = next(
        i for i, s in enumerate(sentencias) if "delete from conversaciones" in s
    )

    assert posicion_historial < posicion_conversaciones
```

- [ ] **Paso 7: Verificar las dos suites, en serie**

Run: `uv run pytest -q`
Expected: PASS

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: PASS

- [ ] **Paso 8: Romper el código a propósito**

Mover el bloque de `agent_sessions` **después** del `DELETE FROM conversaciones`.
Expected: FALLAN `test_el_historial_se_borra_antes_que_las_conversaciones` y `test_clearstate_borra_el_historial_del_agente` (esta segunda con `0 != 1`).
Deshacer.

- [ ] **Paso 9: Commit**

```bash
git add src/maxicare_daniela/persistencia.py src/maxicare_daniela/reseteo.py tests/
git commit -m "fix: /clearstate borra tambien el historial, que desde la fase 7 esta en la base"
```

---

## Tarea 9: La versión del prompt

`trace_metadata` tiene que llevar qué prompt corrió. No existe hoy ningún número de versión, así que hay que inventar el mecanismo — y uno que nadie tenga que acordarse de subir.

**Files:**
- Modify: `src/maxicare_daniela/config.py`
- Modify: `src/maxicare_daniela/agentes.py`
- Test: `tests/test_trazas.py` (nuevo)

**Interfaces:**
- Produces: `config.version_de_prompt(texto: str) -> str`, `agentes.VERSION_PROMPT: str`, `agentes.VERSION_PROMPT_LECTOR: str`

- [ ] **Paso 1: Escribir las pruebas que fallan**

Crear `tests/test_trazas.py`:

```python
"""El `group_id` y el `trace_metadata` de las tres puertas que hablan con OpenAI.

Nada de aquí toca la red: se construyen `RunConfig` y se miran sus campos.
"""

from __future__ import annotations

from maxicare_daniela import agentes, config


def test_la_version_del_prompt_es_corta_y_estable():
    """Corta porque va en cada trace y nadie lee un sha256 entero; estable porque el mismo
    texto tiene que dar el mismo hash entre procesos --`hash()` de Python no vale: está
    aleatorizado por `PYTHONHASHSEED` y cambiaría en cada arranque."""
    una = config.version_de_prompt("hola")
    otra = config.version_de_prompt("hola")

    assert una == otra
    assert len(una) == 12
    assert una == "b221d9dbb083"  # sha256("hola")[:12], fijo entre procesos


def test_un_prompt_distinto_da_una_version_distinta():
    assert config.version_de_prompt("hola") != config.version_de_prompt("hola ")


def test_la_version_de_daniela_sale_del_texto_estatico():
    assert agentes.VERSION_PROMPT == config.version_de_prompt(agentes.INSTRUCCIONES_DANIELA)


def test_el_vocabulario_de_tratamientos_no_mueve_la_version(monkeypatch):
    """A propósito. El vocabulario se pega AL FINAL del prompt y cambia sin desplegar --la
    clínica añade un tratamiento desde el panel--. Metido en el hash, cada tratamiento nuevo
    parecería un prompt nuevo y la versión dejaría de significar «el prompt cambió».

    Es la misma razón por la que el vocabulario va al final y no al principio: el caché de
    prompt de OpenAI funciona por prefijo idéntico.
    """
    antes = agentes.VERSION_PROMPT
    monkeypatch.setattr(
        agentes.contratos, "vocabulario", lambda: ("ortodoncia", "implantes", "carillas")
    )

    assert agentes.VERSION_PROMPT == antes


def test_la_version_del_lector_es_la_suya():
    """Dos prompts distintos, dos versiones distintas. Compartirlas haría que un cambio en
    el lector pareciera un cambio en Daniela."""
    assert agentes.VERSION_PROMPT_LECTOR != agentes.VERSION_PROMPT
    assert agentes.VERSION_PROMPT_LECTOR == config.version_de_prompt(
        agentes.INSTRUCCIONES_LECTOR
    )
```

- [ ] **Paso 2: Verlas fallar**

Run: `uv run pytest tests/test_trazas.py -q`
Expected: FAIL con `AttributeError: module 'maxicare_daniela.config' has no attribute 'version_de_prompt'`

- [ ] **Paso 3: Implementar**

En `config.py`, junto a `TRACE_INCLUDE_SENSITIVE_DATA`:

```python
def version_de_prompt(texto: str) -> str:
    """Un identificador corto y estable del texto de un prompt, para el `trace_metadata`.

    Un hash y no un número que alguien suba a mano: se mueve solo cuando el prompt cambia de
    verdad y nadie tiene que acordarse. Doce caracteres porque va en cada trace y nadie lee
    un sha256 entero; es un identificador, no una defensa criptográfica.

    `hashlib` y no el `hash()` de Python, que está aleatorizado por `PYTHONHASHSEED`: daría
    una versión distinta en cada arranque del contenedor y el campo dejaría de servir para
    lo único que sirve, que es agrupar trazas del mismo prompt.
    """
    import hashlib

    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12]
```

En `agentes.py`, después de `INSTRUCCIONES_LECTOR`:

```python
#: Qué prompt corrió, para el `trace_metadata`. Se calcula al importar sobre el texto
#: ESTÁTICO, sin el vocabulario de tratamientos ni la fecha: esos dos se pegan al final en
#: `instrucciones_daniela` y cambian sin desplegar --la clínica añade un tratamiento desde el
#: panel, y el día pasa solo--. Incluirlos haría que cada tratamiento nuevo y cada amanecer
#: parecieran un prompt nuevo.
VERSION_PROMPT = version_de_prompt(INSTRUCCIONES_DANIELA)
VERSION_PROMPT_LECTOR = version_de_prompt(INSTRUCCIONES_LECTOR)
```

con `from .config import ..., version_de_prompt` y añadiendo las dos a `__all__`.

- [ ] **Paso 4: Verlas pasar**

Run: `uv run pytest tests/test_trazas.py -q`
Expected: PASS (5 passed)

El literal `b221d9dbb083` está comprobado, no escrito de memoria: `python -c "import hashlib; print(hashlib.sha256('hola'.encode('utf-8')).hexdigest()[:12])"`.

- [ ] **Paso 5: Commit**

```bash
git add src/maxicare_daniela/config.py src/maxicare_daniela/agentes.py tests/test_trazas.py
git commit -m "feat: la version del prompt sale de su texto estatico, no de un numero a mano"
```

---

## Tarea 10: `config_de_corrida()` con argumentos

**Files:**
- Modify: `src/maxicare_daniela/config.py` (líneas 55-85)
- Modify: `src/maxicare_daniela/contratos.py` (`ContextoDaniela`, alrededor de la línea 567)
- Test: `tests/test_trazas.py`

**Interfaces:**
- Produces: `config_de_corrida(*, group_id: str | None = None, canal: str = "whatsapp", version_prompt: str | None = None) -> RunConfig`; `ContextoDaniela.canal: str`

- [ ] **Paso 1: Escribir las pruebas que fallan**

Añadir a `tests/test_trazas.py`:

```python
def test_el_group_id_es_la_conversacion_y_no_el_telefono():
    """La distinción importa y está decidida: un teléfono agrupa a una persona para siempre;
    una conversación agrupa un EPISODIO, que es la unidad que alguien va a querer leer
    cuando llegue un reclamo."""
    corrida = config.config_de_corrida(group_id="b3f1c2d4-0000-4000-8000-000000000001")

    assert corrida.group_id == "b3f1c2d4-0000-4000-8000-000000000001"


def test_el_metadata_lleva_canal_y_version_de_prompt():
    corrida = config.config_de_corrida(
        group_id="conv-1", canal="web", version_prompt="abc123def456"
    )

    assert corrida.trace_metadata == {"canal": "web", "version_prompt": "abc123def456"}


def test_sin_version_de_prompt_el_metadata_no_la_inventa():
    """Los evaluadores de guardrail tienen su propio prompt, que no es el de Daniela. Poner
    el de Daniela ahí sería un dato plausible y falso, que es peor que no tenerlo."""
    corrida = config.config_de_corrida(group_id="conv-1", canal="whatsapp")

    assert corrida.trace_metadata == {"canal": "whatsapp"}


def test_sigue_sin_subir_el_contenido_de_la_conversacion():
    """LA PRUEBA QUE NO SE PUEDE RELAJAR. `RunConfig()` nace en la 0.22.2 con
    `trace_include_sensitive_data=True`, y desde la fase 6A hasta el 13/09/2026 cada
    conversación de WhatsApp subió íntegra al dashboard de OpenAI. Añadirle argumentos a
    esta función no puede reabrir esa puerta."""
    corrida = config.config_de_corrida(group_id="conv-1")

    assert corrida.trace_include_sensitive_data is False
    assert corrida.workflow_name == config.WORKFLOW_NAME


def test_sin_argumentos_sigue_funcionando_como_antes():
    """Los tres llamadores se cablean uno a uno; mientras tanto, ninguno se rompe."""
    corrida = config.config_de_corrida()

    assert corrida.group_id is None
    assert corrida.trace_include_sensitive_data is False


def test_el_contexto_sabe_por_que_canal_habla():
    from maxicare_daniela.contratos import ContextoDaniela
    from maxicare_daniela.calendario import CalendarioDoble

    ctx = ContextoDaniela(
        id_conversacion="conv-1",
        telefono_completo="573001112233",
        database_url="postgresql://x/y",
        calendario=CalendarioDoble(),
    )

    assert ctx.canal == "whatsapp", "el default es el canal de producción"
```

- [ ] **Paso 2: Verlas fallar**

Run: `uv run pytest tests/test_trazas.py -q`
Expected: FAIL con `TypeError: config_de_corrida() got an unexpected keyword argument 'group_id'`

- [ ] **Paso 3: Implementar**

En `config.py`, sustituir la firma y el `return` de `config_de_corrida` (conservando **entero** el docstring que ya tiene, que explica la fuga, y añadiéndole los tres párrafos nuevos):

```python
def config_de_corrida(
    *,
    group_id: str | None = None,
    canal: str = "whatsapp",
    version_prompt: str | None = None,
):
    """... (el docstring actual, íntegro) ...

    `group_id` agrupa las trazas de un mismo EPISODIO. Es el UUID de la conversación, no el
    teléfono: un teléfono agrupa a una persona para siempre; una conversación agrupa lo que
    alguien va a querer leer entero cuando llegue un reclamo. Los TRES consumidores de modelo
    tienen que pasar el mismo, o el trace agrupado tendrá un agujero justo en los turnos con
    archivo, que son los más interesantes de leer.

    `version_prompt` se omite a propósito cuando quien llama no es Daniela ni el lector: los
    evaluadores de guardrail tienen su propio prompt, y poner el de Daniela ahí sería un dato
    plausible y falso.
    """
    from agents import RunConfig

    metadata: dict[str, str] = {"canal": canal}
    if version_prompt is not None:
        metadata["version_prompt"] = version_prompt

    return RunConfig(
        workflow_name=WORKFLOW_NAME,
        trace_include_sensitive_data=TRACE_INCLUDE_SENSITIVE_DATA,
        group_id=group_id,
        trace_metadata=metadata,
    )
```

En `contratos.py`, dentro de `ContextoDaniela`, junto a `tema_general`:

```python
    #: Por dónde llegó este mensaje: `whatsapp` (producción) o `web` (el chat del panel).
    #: Va al `trace_metadata` para poder separar en el dashboard las conversaciones reales de
    #: las pruebas de la clínica, que corren contra el MISMO agente a propósito.
    canal: str = "whatsapp"
```

Y en `runtime._contexto_de_prueba`, añadir `canal="web"` al construir el `ContextoDaniela`.

- [ ] **Paso 4: Verlas pasar**

Run: `uv run pytest tests/test_trazas.py -q`
Expected: PASS (11 passed)

- [ ] **Paso 5: Commit**

```bash
git add src/maxicare_daniela/config.py src/maxicare_daniela/contratos.py src/maxicare_daniela/runtime.py tests/test_trazas.py
git commit -m "feat: config_de_corrida recibe group_id, canal y version de prompt"
```

---

## Tarea 11: Las tres puertas bajo el mismo `group_id`

Los tres consumidores de modelo del proyecto son Daniela (`conversacion.py`), el lector (`lectura.py`) y los evaluadores de guardrail (`guardrails.py`). Si uno queda fuera, el trace agrupado tiene un agujero.

**Files:**
- Modify: `src/maxicare_daniela/conversacion.py` (líneas 200-224, 249)
- Modify: `src/maxicare_daniela/guardrails.py` (líneas 266-285, 337-363)
- Modify: `src/maxicare_daniela/lectura.py` (líneas 236-267, 276-283)
- Modify: `src/maxicare_daniela/ingesta.py` (líneas 276-320)
- Modify: `tests/test_conversacion.py` (285-340), `tests/test_lectura.py` (440-490), `tests/test_guardrails.py` (318-350)

**Interfaces:**
- Consumes: `config.config_de_corrida(group_id=, canal=, version_prompt=)` (Tarea 10); `agentes.VERSION_PROMPT`, `agentes.VERSION_PROMPT_LECTOR` (Tarea 9); `ContextoDaniela.canal` (Tarea 10).
- Produces: `conversacion._config_de_corrida(ctx: ContextoDaniela) -> RunConfig`; `lectura.leer_archivo(..., group_id: str | None = None)`; `lectura.leer_y_repartir(..., group_id: str | None = None)`; `guardrails._preguntar(evaluador, texto, *, ctx=None)`.

- [ ] **Paso 1: Escribir las pruebas que fallan**

En `tests/test_conversacion.py`, **añadir** a las cuatro que ya protegen el tracing (no tocarlas, solo sumarles compañía):

```python
def test_el_turno_de_daniela_va_bajo_el_group_id_de_su_conversacion():
    ctx = _contexto_minimo(id_conversacion="conv-abc")

    corrida = conversacion._config_de_corrida(ctx)

    assert corrida.group_id == "conv-abc"
    assert corrida.trace_metadata["version_prompt"] == agentes.VERSION_PROMPT
    assert corrida.trace_metadata["canal"] == "whatsapp"
    # Y lo de siempre, que ninguna de estas líneas puede reabrir:
    assert corrida.trace_include_sensitive_data is False
```

En `tests/test_guardrails.py`, junto a la que ya existe:

```python
async def test_el_evaluador_corre_bajo_el_group_id_de_la_conversacion(monkeypatch):
    """La puerta más fácil de olvidar: nadie piensa en un freno como en algo que habla con
    OpenAI. Si el evaluador queda fuera del grupo, el trace de un mensaje bloqueado aparece
    suelto, sin la conversación que lo explica -- que es justo el trace que alguien va a
    querer leer."""
    capturado = {}

    async def falso_run(agente, texto, **kwargs):
        capturado.update(kwargs)
        return _resultado_de_mentira(dispara=False)

    monkeypatch.setattr(guardrails.Runner, "run", falso_run)
    ctx = _wrapper_con_contexto(id_conversacion="conv-xyz")

    await guardrails.uso_indebido(ctx, None, "hola")

    assert capturado["run_config"].group_id == "conv-xyz"
    assert capturado["run_config"].trace_include_sensitive_data is False
```

En `tests/test_lectura.py`:

```python
def test_el_lector_corre_bajo_el_group_id_que_le_dan():
    """Sin esto el trace agrupado tiene un agujero justo en los turnos con archivo, que son
    los más interesantes de leer."""
    corrida = lectura._config_de_corrida("conv-con-radiografia")

    assert corrida.group_id == "conv-con-radiografia"
    assert corrida.trace_metadata["version_prompt"] == agentes.VERSION_PROMPT_LECTOR
    assert corrida.trace_include_sensitive_data is False


def test_sin_conversacion_viva_el_lector_no_se_inventa_un_grupo():
    """El primer mensaje de un paciente nuevo PUEDE traer un archivo, y en ese instante la
    conversación todavía no existe: la crea `atencion._leer_estado`, después de que el lector
    ya arrancó. Ese trace queda fuera del grupo, y queda fuera A PROPÓSITO: inventarle un
    `group_id` que no corresponde a ninguna conversación es peor que no tenerlo.
    """
    corrida = lectura._config_de_corrida(None)

    assert corrida.group_id is None
    assert corrida.trace_include_sensitive_data is False
```

- [ ] **Paso 2: Verlas fallar**

Run: `uv run pytest tests/test_conversacion.py tests/test_guardrails.py tests/test_lectura.py -q -k "group_id"`
Expected: FAIL con `TypeError: _config_de_corrida() takes 0 positional arguments but 1 was given`

- [ ] **Paso 3: Cablear `conversacion.py`**

```python
def _config_de_corrida(ctx: ContextoDaniela) -> RunConfig:
    """... (el docstring actual, íntegro) ...

    Desde la fase 7 lleva además el `group_id` de la conversación, que es lo que agrupa las
    trazas de un episodio -- las de Daniela, las del lector y las de los evaluadores.
    """
    return config_de_corrida(
        group_id=ctx.id_conversacion,
        canal=ctx.canal,
        version_prompt=VERSION_PROMPT,
    )
```

y en `responder`, línea 249: `run_config = run_config or _config_de_corrida(ctx)`.

- [ ] **Paso 4: Cablear `guardrails.py`**

```python
async def _preguntar(evaluador: Agent, texto: str, *, ctx=None) -> Veredicto:
    """... (el docstring actual, íntegro) ...

    `ctx` es el `ContextoDaniela` del turno, y solo se usa para el `group_id`: sin él, el
    trace de un mensaje bloqueado aparece suelto, sin la conversación que lo explica.
    """
    try:
        resultado = await Runner.run(
            evaluador,
            texto,
            max_turns=1,
            run_config=config_de_corrida(
                group_id=getattr(ctx, "id_conversacion", None),
                canal=getattr(ctx, "canal", "whatsapp"),
                # Sin `version_prompt`: el prompt de un evaluador no es el de Daniela, y
                # poner el de Daniela aquí sería un dato plausible y falso.
            ),
        )
```

y en los dos llamadores: `await _preguntar(_evaluador_clinico, salida.mensaje_al_paciente, ctx=wrapper.context)` y `await _preguntar(_evaluador_uso, texto, ctx=wrapper.context)`.

- [ ] **Paso 5: Cablear `lectura.py`**

```python
def _config_de_corrida(group_id: str | None) -> RunConfig:
    """... (el docstring actual, íntegro) ..."""
    return config_de_corrida(
        group_id=group_id, canal="whatsapp", version_prompt=VERSION_PROMPT_LECTOR
    )
```

`leer_archivo` y `leer_y_repartir` ganan `group_id: str | None = None` y lo pasan:

```python
    ejecutar = correr or (
        lambda entrada: Runner.run(
            lector_archivos, entrada, run_config=_config_de_corrida(group_id)
        )
    )
```

- [ ] **Paso 6: Cablear `ingesta.py`**

`procesar_mensaje` resuelve la conversación viva antes de lanzar el lector. Va después de la descarga y del envío a Telegram, así que no le quita latencia al camino que no se puede retrasar:

```python
            if lectura_mod.vale_la_pena_leer(m.tipo, archivo.tamano):
                # El `group_id` del lector es la conversación viva, si la hay. Si es el
                # PRIMER mensaje de un paciente nuevo, todavía no existe --la crea
                # `atencion._leer_estado`, después de que esto arranque-- y ese trace queda
                # fuera del grupo. Se acepta: inventarle un id que no corresponde a ninguna
                # conversación sería peor que no tenerlo.
                grupo = await asyncio.to_thread(_conversacion_viva, database_url, m.telefono)
                tarea = asyncio.create_task(
                    lectura_mod.leer_y_repartir(
                        archivo,
                        tipo=m.tipo,
                        telegram=telegram,
                        tema_id=destino,
                        group_id=grupo,
                    )
                )
```

con el ayudante, junto a `_registrar`:

```python
def _conversacion_viva(database_url: str, telefono: str) -> str | None:
    """El id de la conversación abierta de este número, o `None`. Solo para el `group_id`.

    Se traga cualquier fallo: una traza mal agrupada no puede costarle a un doctor la
    lectura de una radiografía.
    """
    try:
        with persistencia.conectar(database_url) as conn:
            viva = persistencia.conversacion_viva(conn, telefono)
    except Exception:  # noqa: BLE001 -- ver el docstring
        log.warning("no se pudo resolver la conversación de %s para el trace", telefono)
        return None
    return viva[0] if viva else None
```

- [ ] **Paso 7: Verlas pasar**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Paso 8: Romper el código a propósito, una vez por puerta**

Por cada una de las tres, sustituir su `config_de_corrida(...)` por `config_de_corrida()` a secas y correr.
Expected: cae la prueba de esa puerta y **solo** la de esa puerta. Si alguna no cae, esa prueba no vale y hay que rehacerla.
Deshacer las tres.

- [ ] **Paso 9: Commit**

```bash
git add src/maxicare_daniela/ tests/
git commit -m "feat: las tres puertas que hablan con OpenAI caen bajo el mismo group_id"
```

---

## Tarea 12: El entregable ejecutable

> «Una conversación sobrevive a reiniciar el proceso y Daniela la retoma con su contexto.»

Una sesión reconstruida a mano no demuestra eso. El reinicio real, sí.

**Files:**
- Create: `scripts/probar_persistencia.py`

- [ ] **Paso 1: Escribir el script**

Sigue el patrón de los demás: sin `--chat` no gasta un token; con `--chat` levanta dos procesos de verdad.

```python
"""El entregable de la fase 7: una conversacion sobrevive a reiniciar el proceso.

    uv run python scripts/probar_persistencia.py            # el cableado. No gasta.
    uv run python scripts/probar_persistencia.py --chat     # el reinicio de verdad. GASTA.

Sin `--chat` comprueba lo que se puede comprobar sin modelo: que las tablas de la 010 estan
aplicadas, que la fabrica devuelve una sesion persistida y no una de memoria, que el
esquema se traduce, y que un ida y vuelta contra Neon guarda y recupera lo que se le puso.

Con `--chat` hace lo que la frase del plan pide de verdad:

    uvicorn arranca
    paciente habla  ----->  <<hola, soy Ana>>
    +--------------------------------------------+
    |  SIGTERM al proceso  ·  arranca otro       |   <-- el reinicio, de verdad
    +--------------------------------------------+
    paciente: <<como me llamo?>>  ->  Daniela lo sabe

Escribe en el esquema `pruebas_persistencia`, que crea y borra comprobando el borrado.
NUNCA en `public`.
"""
```

El cuerpo, por partes:

1. `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y `sys.path.insert(0, .../src)` como los demás.
2. `revisar(etiqueta, condicion, detalle)` con marcadores ASCII `OK` / `FALLA`.
3. **MITAD A (sin `--chat`)**: crear el esquema, `aplicar_esquema`, comprobar que `agent_sessions` y `agent_messages` existen, hacer un `add_items` / `get_items` y comprobar que vuelve lo mismo, y comprobar que **no** hay filas nuevas en `public.agent_messages`.
4. **MITAD B (`--chat`)**: `subprocess.Popen` de `uvicorn maxicare_daniela.runtime:app --port 8099` con `MAXICARE_DATABASE_URL` apuntada al esquema de pruebas; POST al chat con «hola, soy Ana»; `proceso.terminate()` y `proceso.wait()`; arrancar un **segundo** proceso; POST con «¿cómo me llamo?»; comprobar que la respuesta contiene «Ana».
5. `finally` que borra el esquema y **comprueba el borrado con una aserción**, igual que `probar_atencion.py`.

- [ ] **Paso 2: Correr la mitad que no gasta**

Run: `uv run python scripts/probar_persistencia.py`
Expected: todas las líneas en `OK`, salida 0, y el esquema borrado.

- [ ] **Paso 3: Correr el entregable de verdad**

Run: `uv run python scripts/probar_persistencia.py --chat`
Expected: la segunda respuesta contiene «Ana». **Esto gasta tokens** — un turno completo contra el modelo, dos veces.

Si falla aquí y la mitad A pasó, el problema está en el cableado de `atencion`/`runtime`, no en la persistencia: volver a la Tarea 5 o 6.

- [ ] **Paso 4: Registrarlo en la tabla de entregables**

En `CLAUDE.md`, añadir la fila:

```
| `scripts/probar_persistencia.py` | que una conversación sobrevive a reiniciar (fase 7) | solo con `--chat` |
```

- [ ] **Paso 5: Commit**

```bash
git add scripts/probar_persistencia.py CLAUDE.md
git commit -m "feat: el entregable de la fase 7, con un reinicio de proceso de verdad"
```

---

## Tarea 13: El límite, medido

El plan original dice `SessionSettings(limit=40)` y lo justifica como «≈5 conversaciones completas». Esa equivalencia es falsa: **el SDK no cuenta mensajes, cuenta items.** Un turno en que Daniela consulte el conocimiento, mire la agenda y registre el estado gasta seis o siete items él solo.

**Esta tarea va la última porque no hay otra forma.** Hoy no hay nada que medir: el historial no se guarda, así que no hay items históricos que contar. La medición solo es posible *después* de persistir.

```
   1. persistir SIN límite  ──►  2. medir items/turno      ──►  3. fijar el límite
      (Tareas 1-12)              sobre agent_messages          con el dato al lado
```

**Files:**
- Create: `scripts/medir_historial.py`
- Modify: `src/maxicare_daniela/config.py` (línea 88), `src/maxicare_daniela/persistencia.py` (`sesion_de_agente`)
- Test: `tests/test_persistencia_sesion.py`

- [ ] **Paso 1: Dejar correr conversaciones reales**

Desplegar (Tarea 14) y esperar a que haya **al menos veinte turnos reales** en `public.agent_messages`. Con menos, el percentil no significa nada y estaríamos sustituyendo una suposición por otra más cara.

- [ ] **Paso 2: Escribir el medidor**

Crear `scripts/medir_historial.py`, con la cabecera de encoding de siempre. La consulta:

```sql
WITH por_sesion AS (
    SELECT session_id, count(*) AS items
      FROM agent_messages
     GROUP BY session_id
),
turnos AS (
    SELECT c.id::text AS session_id, c.turno_actual
      FROM conversaciones c
     WHERE c.turno_actual > 0
)
SELECT
    count(*)                                                      AS conversaciones,
    round(avg(p.items::numeric / t.turno_actual), 1)               AS items_por_turno_medio,
    max(p.items::numeric / t.turno_actual)                         AS items_por_turno_peor,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY p.items)          AS items_p95,
    max(p.items)                                                   AS items_max,
    max(t.turno_actual)                                            AS turnos_max
  FROM por_sesion p
  JOIN turnos t USING (session_id);
```

Imprime los seis números y, debajo, la recomendación con su aritmética explícita:

```
  items/turno (peor caso medido): N
  turnos que hay que recordar:    6   <- una conversacion de agendamiento completa
  ------------------------------------
  LIMITE_HISTORIAL_SESION =      N*6
```

- [ ] **Paso 3: Correrlo**

Run: `uv run python scripts/medir_historial.py`
Expected: los seis números. **Anotarlos**: van al docstring de la constante.

- [ ] **Paso 4: Escribir la prueba que falla**

Añadir a `tests/test_persistencia_sesion.py`:

```python
def test_el_historial_va_recortado_al_limite_medido():
    """El paso 3 de los tres. Hasta aquí el historial iba entero, y eso era correcto: no
    había items que contar hasta que hubo items guardados.

    El número NO es una estimación: sale de `scripts/medir_historial.py` sobre las
    conversaciones reales, y su aritmética está escrita junto a la constante. Sustituir una
    suposición por otra más grande no es un arreglo.
    """
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion.session_settings.limit == config.LIMITE_HISTORIAL_SESION


def test_un_limite_explicito_gana_al_de_config():
    """Para poder probar el borde del recorte sin depender del número de producción."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON, limite=4)

    assert sesion.session_settings.limit == 4
```

- [ ] **Paso 5: Verlas fallar**

Run: `uv run pytest tests/test_persistencia_sesion.py -q -k limite`
Expected: FAIL con `assert None == 40`

Además, **`test_sin_limite_el_historial_va_entero`** (Tarea 3) va a fallar ahora, y está bien: esa prueba documentaba el paso 1 y el paso 1 terminó. Sustituirla por `test_un_limite_explicito_gana_al_de_config`.

- [ ] **Paso 6: Implementar**

En `config.py`, sustituir la constante y su docstring:

```python
#: `persistencia.compactacion`: recorte del historial por cantidad, cero llamadas de modelo.
#: La memoria larga no vive aquí: vive en el estado estructurado de Neon, que no se degrada.
#:
#: EL SDK CUENTA ITEMS, NO MENSAJES. Una llamada a tool y su resultado son dos items, y un
#: turno en que Daniela consulte el conocimiento, mire la agenda y registre el estado gasta
#: seis o siete él solo. Por eso el 40 del plan --justificado como «5 conversaciones
#: completas»-- era falso: podían ser cinco o seis TURNOS.
#:
#: Medido con `scripts/medir_historial.py` el DD/MM/2026 sobre N conversaciones reales:
#:   items por turno, media: PENDIENTE   peor caso: PENDIENTE
#:   items por conversación, p95: PENDIENTE   máximo: PENDIENTE
#: El número es <peor caso> x 6 turnos, que es una conversación de agendamiento completa
#: --saludo, tratamiento, fecha, disponibilidad, nombre y consentimiento, confirmación--.
LIMITE_HISTORIAL_SESION = PENDIENTE
```

y en `persistencia.sesion_de_agente`, el default del parámetro deja de ser `None`:

```python
def sesion_de_agente(
    id_conversacion: str,
    *,
    database_url: str,
    esquema: str | None = None,
    limite: int | None = -1,
):
    ...
    from .config import LIMITE_HISTORIAL_SESION

    efectivo = LIMITE_HISTORIAL_SESION if limite == -1 else limite
```

(El centinela `-1` y no `None`, porque `None` es un valor legítimo: «sin límite», que es lo que las pruebas del borde necesitan poder pedir.)

- [ ] **Paso 7: Sustituir los `PENDIENTE` por los números del Paso 3**

**Los cuatro.** El literal `PENDIENTE` es para lo que no se sabe; esto se acaba de medir.

- [ ] **Paso 8: Verlas pasar**

Run: `uv run pytest -q`
Expected: PASS

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: PASS

- [ ] **Paso 9: Commit**

```bash
git add src/maxicare_daniela/config.py src/maxicare_daniela/persistencia.py scripts/medir_historial.py tests/test_persistencia_sesion.py
git commit -m "feat: el limite del historial, medido sobre conversaciones reales y no estimado"
```

---

## Tarea 14: Cerrar la fase

- [ ] **Paso 1: Actualizar la documentación que dejó de ser cierta**

- `README.md`: la fase en curso pasa a 7 y su entregable a «una conversación sobrevive a reiniciar el proceso».
- `CLAUDE.md`: el no negociable nuevo, en una línea que sobreviva a una compactación:

```
9. **El historial del diálogo YA está en Postgres** (`agent_sessions` / `agent_messages`,
   `session_id = id_conversacion`). Quien toque `/clearstate` tiene que borrarlo, y va
   ANTES del `DELETE FROM conversaciones`: los `session_id` SON esos ids.
```

- `.claude/rules/migraciones.md`: mencionar que la 010 crea tablas cuyo esquema lo fija el SDK, y que `tests/test_sesion_neon.py` es lo que caza que se queden viejas.

- [ ] **Paso 2: La suite entera, las dos, en serie**

Run: `uv run pytest -q`
Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: las dos en PASS. **Nunca a la vez**: se pisan los esquemas de prueba.

- [ ] **Paso 3: Los scripts de fase que no gastan**

```bash
uv run python scripts/probar_tools.py
uv run python scripts/probar_atencion.py
uv run python scripts/probar_lectura.py
uv run python scripts/probar_web.py
uv run python scripts/probar_persistencia.py
```

Expected: los cinco en OK, salida 0.

- [ ] **Paso 4: Merge a main**

`scripts/desplegar.sh` empaqueta el **árbol de trabajo**, no una rama de git. Desplegar desde la rama dejaría producción corriendo código que no está en `main`.

```bash
git checkout main
git merge fase-7-persistencia --no-ff -m "Merge: fase 7, el historial sobrevive al reinicio"
uv run pytest -q
```

Expected: PASS sobre el merge.

- [ ] **Paso 5: Desplegar**

Run: `bash scripts/desplegar.sh`
Expected: `010_sesiones_agente.sql` en la lista de migraciones aplicadas, contenedor `healthy`, y `/salud` a través de Traefik con `"calendario":"CalendarioGoogle"` y `"base_de_datos":"ok"`.

- [ ] **Paso 6: Comprobarlo a mano, que es lo único que lo demuestra**

Desde un número de `MAXICARE_TELEFONOS_PRUEBA`:

1. `/clearstate`
2. «hola, soy Ana»
3. Reiniciar el contenedor: `ssh <vps> "docker compose -f /opt/maxicare-daniela/docker-compose.yml restart"`
4. «¿cómo me llamo?»

Expected: Daniela responde «Ana». Con el diccionario en memoria respondía que no lo sabía.

---

## Autorrevisión del plan

**1. Cobertura del spec.** Las nueve secciones del spec, con la tarea que las implementa:

| Spec | Tarea |
|---|---|
| §3.2 la URL (`postgresql+psycopg`, no `asyncpg`, no a secas) | 1 |
| §3.3 el esquema por `schema_translate_map`, no `search_path` | 3 |
| §3.4 las tablas las crea la migración, `create_tables=False` | 2 |
| §3.5 `persistencia.py`, `atencion.py`, `conversacion.py`, `config.py`, `lectura.py`, `guardrails.py`, `reseteo.py`, `runtime.py` | 1, 3, 4, 5, 6, 8, 9, 10, 11 |
| §3.6 el `group_id` es la conversación; `trace_metadata` con canal y versión; las TRES puertas | 9, 10, 11 |
| §4.1 el límite se mide antes de fijarse, en ese orden | 13 |
| §4.2 el corte no puede caer entre una tool y su salida | 7 |
| §4.3 `/clearstate` borra el historial, en los dos esquemas | 8 |
| §4.4 el chat web usa el mismo mecanismo | 6 |
| §4.5 un segundo pool, dimensionado a mano | 4 |
| §5 verificación offline, contra Neon, y el entregable ejecutable | 1, 3, 7, 9, 10, 11, 12 |
| §6.5 `ensure_ascii=False` | 3 |

**Fuera de alcance, como dice el spec:** el relevo (§7), la revisión legal y el cifrado (§6.1), la compactación con modelo.

**2. Tres cosas que el spec dio por sentadas y no eran así.** Se dejan escritas porque quien ejecute el plan se las va a encontrar:

- **`persistencia.conectar` no tiene pool.** Abre una conexión por llamada y la cierra. El spec §4.5 hablaba de «cuántas consume ya el pool de psycopg»; no hay tal pool, y el dato que sustituye a ese es el pico de turnos concurrentes. Tarea 4, Paso 1.
- **El lector puede arrancar antes de que exista la conversación.** `ingesta.procesar_mensaje` lanza el lector antes de que `atencion._leer_estado` cree la conversación, así que el primer mensaje de un paciente nuevo que traiga un archivo tiene su trace fuera del grupo. El spec pedía que el lector no quedara fuera; queda fuera en ese único caso, y se documenta en vez de inventarle un id. Tarea 11, Paso 6.
- **`atender` necesita inyección de sesión.** La suite offline corre en dos segundos y sin señal, y eso es parte de por qué se corre. Sin un `sesion_de` inyectable, `test_atencion.py`, `test_muro.py` y `test_webhook_responde.py` pasarían a exigir Neon. `SesionEnMemoria` sobrevive exactamente para eso, como dice §3.1. Tarea 5.

**3. Consistencia de tipos.** `sesion_de_agente(id, *, database_url, esquema=None, limite=...)` se usa con esa firma en las Tareas 3, 4, 5, 6, 7, 12 y 13; el default de `limite` cambia de `None` a `-1` en la Tarea 13 y eso está dicho allí. `config_de_corrida(*, group_id, canal, version_prompt)` es la misma en 10 y 11. `_config_de_corrida` es `(ctx)` en `conversacion` y `(group_id)` en `lectura` — distintas a propósito, porque el lector no tiene contexto.

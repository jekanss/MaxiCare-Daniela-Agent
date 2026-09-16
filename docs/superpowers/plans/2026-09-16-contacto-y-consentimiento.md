# Contacto y consentimiento · plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar al sistema una identidad por teléfono que sobreviva a las 24 h, y sobre ella el
consentimiento: el aviso de la política registrado, la baja comercial y su revocación, y la
garantía de que una baja nunca apaga el recordatorio de una cita.

**Architecture:** Dos tablas nuevas colgadas del teléfono (`contactos`, estado actual, y
`consentimientos`, bitácora inmutable). El turno de WhatsApp crea la fila al entrar el primer
mensaje y lleva dos señales nuevas al contexto. El aviso lo emite el código, no el modelo. El
despachador comprueba la baja antes de sus siete guardas, distinguiendo lo comercial de lo
transaccional con una lista blanca.

**Tech Stack:** Python 3.11+, psycopg (sincrónico, envuelto en `asyncio.to_thread`), Postgres
en Neon, OpenAI Agents SDK 0.22.2, pytest con marca `neon`.

**Spec:** `docs/superpowers/specs/2026-09-16-contacto-y-consentimiento-design.md`

## Global Constraints

Copiados literalmente de la spec y del CLAUDE.md. Aplican a TODAS las tareas.

- **Rama:** `contacto-y-consentimiento`, ya creada. No se empuja a `origin` salvo que el
  usuario lo pida.
- **No hay tabla de control de migraciones.** `persistencia.aplicar_esquema` aplica TODAS las
  `migraciones/*.sql` en orden de nombre, **cada vez**. Todo SQL nuevo debe ser idempotente:
  `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, y los CHECK dentro de un bloque
  `DO $$ ... IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = ... AND conrelid =
  'tabla'::regclass) ... END $$`.
- **`conrelid = 'tabla'::regclass` es obligatorio en cada CHECK.** `pg_constraint.conname` no
  es único en toda la base, solo por tabla: sin el ancla, el `IF NOT EXISTS` encuentra el
  constraint de `public` desde cualquier otro esquema y se salta la creación. Es la lección de
  la migración 013, y costó que `pruebas` aceptara valores inventados en silencio.
- **Estilo de `persistencia.py`:** toda función recibe `conn` como primer parámetro posicional
  y sin anotación de tipo; lo identificador va posicional, todo lo demás keyword-only tras
  `*`; cada escritura termina en `conn.commit()`. Ninguna función del módulo abre su propia
  conexión.
- **Ninguna clave, fecha, origen ni versión la escribe el modelo.** Las tools solo levantan la
  mano; la huella la arma el código desde `ctx`.
- **Lo que no se sabe se marca con el literal `"PENDIENTE"`**, nunca con un valor plausible.
  Precedente en el repo: `relevo.NOMBRE_PENDIENTE = "PENDIENTE"`.
- **No se registran cédulas ni documentos de identidad de ningún tipo.** Ninguna tabla ni
  campo de este plan puede acercarse a eso.
- **Consola de Windows en cp1252:** ningún script bajo `scripts/` imprime `→`, `✅` ni
  emojis. Marcadores ASCII (`OK` / `FALLA` / `->`). Este plan no crea scripts, pero si algún
  paso imprime, sigue la regla.
- **Los heredoc de Bash fallan en este entorno.** Para crear archivos, herramienta Write.
  Para modificar archivos existentes, herramienta Edit — nunca PowerShell con
  `Get-Content`/`Set-Content`, que destroza el encoding y deja mojibake en todos los acentos.
- **Pruebas:** offline `uv run pytest -q`; contra Neon `MAXICARE_PRUEBAS_NEON=1 uv run pytest
  -q -m neon`. Cada archivo de pruebas de Neon usa **su propio esquema**, que crea y borra.
  Nunca `public`.
- **Tras escribir una migración:** `uv run python scripts/inicializar_base.py`.
- **Texto del aviso, literal y único** (no se reescribe en cada sitio):
  `Al continuar aceptas nuestra política de tratamiento de datos: {url}`
- **Versión de política vigente:** `politica-2026-09`.

---

## File Structure

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `migraciones/019_contacto_y_consentimiento.sql` | **nuevo** · las dos tablas, sus CHECK anclados y el índice | 1 |
| `src/maxicare_daniela/persistencia.py` | seis funciones nuevas de acceso a datos + el reseteo dentro de `borrar_rastro` + la columna nueva en `seguimientos_por_despachar` | 1, 5 |
| `tests/test_contacto_neon.py` | **nuevo** · toda la capa de datos contra Postgres real, esquema `pruebas_contacto` | 1 |
| `src/maxicare_daniela/atencion.py` | crear el contacto al leer el estado; las dos señales en `_Estado`; pegar el aviso y marcarlo tras el envío | 2, 3 |
| `src/maxicare_daniela/contratos.py` | el campo nuevo de `ContextoDaniela` | 2 |
| `tests/test_atencion.py` | **existente** · las pruebas offline de las dos tareas de atención | 2, 3 |
| `src/maxicare_daniela/config.py` | URL y versión de la política | 3 |
| `tests/test_config.py` | **existente** · que el default sea `PENDIENTE` | 3 |
| `src/maxicare_daniela/herramientas.py` | dos tools nuevas + su registro en la lista y en `__all__` | 4 |
| `src/maxicare_daniela/agentes.py` | las instrucciones de la baja y del silencio | 4 |
| `tests/test_herramientas.py` | **existente** · las dos tools offline | 4 |
| `src/maxicare_daniela/seguimientos.py` | la lista blanca y la guarda G0 | 5 |
| `tests/test_seguimientos.py` | **existente** · G0 offline, sin base | 5 |
| `.env.ejemplo` | las dos variables nuevas | 3 |

**Cinco tareas.** Cada una termina en algo que se puede probar y revisar por separado. La 1
es toda la capa de datos (incluido `/clearstate`, que es SQL y vive en `persistencia.py`); la
2 no cambia ni un mensaje que vea un paciente; la 3 es la primera que sí.

---

## Task 1: La capa de datos

Migración 019, las seis funciones de acceso, el reseteo de `/clearstate` y sus pruebas contra
Neon. Al terminar esta tarea el sistema **no se comporta distinto**: nada la usa todavía.

**Files:**
- Create: `migraciones/019_contacto_y_consentimiento.sql`
- Create: `tests/test_contacto_neon.py`
- Modify: `src/maxicare_daniela/persistencia.py` (funciones nuevas + `borrar_rastro:2313-2443`)

**Interfaces:**
- Consumes: `persistencia.conectar`, `persistencia.aplicar_esquema`.
- Produces, y de aquí en adelante todas las tareas dependen de estas firmas exactas:
  ```python
  def asegurar_contacto(conn, telefono: str) -> dict[str, Any]
  def leer_contacto(conn, telefono: str) -> dict[str, Any] | None
  def marcar_aviso_mostrado(conn, telefono: str, *, version: str) -> None
  def pedir_baja(conn, telefono: str, *, origen: str = "paciente",
                 detalle: str | None = None) -> bool
  def revocar_baja(conn, telefono: str, *, origen: str = "paciente",
                   detalle: str | None = None) -> bool
  def anotar_consentimiento(conn, telefono: str, *, evento: str, origen: str,
                            version: str | None = None, detalle: str | None = None) -> None
  ```
  Las claves del `dict` que devuelven `asegurar_contacto` y `leer_contacto` son exactamente
  los nombres de columna: `telefono`, `creado_en`, `actualizado_en`, `aviso_mostrado_en`,
  `politica_version`, `no_contactar`, `no_contactar_en`, `no_contactar_origen`.

- [ ] **Step 1: Escribir la migración**

Crear `migraciones/019_contacto_y_consentimiento.sql` con este contenido exacto:

```sql
-- =========================================================================================
-- La persona del teléfono, y lo que ha decidido sobre sus datos
--
-- Hasta aquí el sistema solo sabía de dos cosas: `conversaciones`, que caduca a las 24 h, y
-- `pacientes`, cuya fila solo nace cuando alguien AGENDA (no negociable 12). Quien preguntó
-- y no agendó no era ninguna de las dos: a las 24 horas dejaba de existir. Es justo el grupo
-- que la reactivación quiere volver a buscar.
--
-- `contactos` es esa tercera cosa. Nace con el primer mensaje que entra, no dice nada sobre
-- identidad --tener fila aquí NO es estar verificado, igual que `temas_telegram` (no
-- negociable 16)-- y de ella cuelga el consentimiento.
--
-- Por qué NO se le abre ficha en `pacientes` a todo el que escribe: en este sistema la
-- existencia de esa fila ES la identidad verificada, y de ahí sale `ctx.telefono_sin_paciente`,
-- el permiso que impide que un desconocido mueva o cancele citas ajenas. Si todo el que
-- saluda tuviera ficha, esa distinción desaparecería.
--
-- Por qué NO se guarda en `conversaciones` leyéndolo por teléfono, que era el candidato
-- natural porque ese patrón ya existe (`ultimo_recordatorio_tipo`): `/clearstate` borra las
-- conversaciones, y se llevaría el `no_contactar` con ellas. Un número dado de baja volvería
-- a ser contactable y nadie se enteraría.
--
-- `consentimientos` es la bitácora, y es lo que se enseña si alguien reclama. Nunca se
-- modifica ni se borra: la FK con ON DELETE RESTRICT lo hace imposible aunque alguien lo
-- intente. Una sola tabla con el estado actual perdería justo lo que la ley pide poder
-- acreditar -- si alguien autoriza, se da de baja y vuelve a autorizar, el estado actual solo
-- recuerda lo último.
--
-- Lo que deliberadamente NO está aquí:
--
--   * `canales_autorizados`. Hoy solo existe WhatsApp. Una columna que siempre dice lo mismo
--     es una columna que miente el día que aparezca un segundo canal y nadie la llene.
--   * un campo de «autorizó marketing». El permiso de reactivación es un aviso con derecho a
--     oponerse (decisión D2 de la spec): lo que se captura es la OPOSICIÓN, no el sí. Dos
--     columnas para un solo hecho son dos columnas que acaban contradiciéndose.
--   * cédula o documento de identidad. Prohibición expresa del cliente; la ausencia ES la
--     política, igual que en `pacientes`.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS contactos (
    telefono            TEXT PRIMARY KEY,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = este número nunca ha visto el aviso de la política.
    aviso_mostrado_en   TIMESTAMPTZ,
    politica_version    TEXT,
    -- La baja COMERCIAL. Nunca apaga el recordatorio de una cita: ver la lista blanca de
    -- `seguimientos.TIPOS_NO_COMERCIALES`.
    no_contactar        BOOLEAN NOT NULL DEFAULT FALSE,
    no_contactar_en     TIMESTAMPTZ,
    no_contactar_origen TEXT
);

CREATE TABLE IF NOT EXISTS consentimientos (
    id               BIGSERIAL PRIMARY KEY,
    -- ON DELETE RESTRICT y no CASCADE: borrar un contacto con bitácora tiene que ser
    -- imposible. Es el punto entero de que esta tabla exista.
    telefono         TEXT        NOT NULL REFERENCES contactos(telefono) ON DELETE RESTRICT,
    evento           TEXT        NOT NULL,
    ocurrido_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Congelada en el momento del evento: si la política cambia, hay que poder demostrar
    -- cuál vio cada persona.
    politica_version TEXT,
    origen           TEXT        NOT NULL,
    detalle          TEXT
);

-- `'consentimientos'::regclass` resuelve por el `search_path`, así que apunta a la tabla del
-- esquema en el que se está aplicando la migración y a ninguna otra. Sin eso, aplicarla en
-- `pruebas` iría a comprobar la restricción de `public` y la lista quedaría abierta ahí. Ver
-- la migración 013.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_consentimientos_evento'
           AND conrelid = 'consentimientos'::regclass
    ) THEN
        ALTER TABLE consentimientos
            ADD CONSTRAINT ck_consentimientos_evento CHECK (
                evento IN (
                    'aviso_mostrado',   -- se le enseñó la política, con su versión
                    'baja_solicitada',  -- pidió que no le escribieran más
                    'baja_revocada',    -- pidió volver a recibir
                    'rastro_borrado'    -- /clearstate sobre este número
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_consentimientos_origen'
           AND conrelid = 'consentimientos'::regclass
    ) THEN
        ALTER TABLE consentimientos
            ADD CONSTRAINT ck_consentimientos_origen CHECK (
                origen IN (
                    'codigo',    -- lo hizo el sistema (el aviso)
                    'paciente',  -- lo pidió la persona
                    'clinica'    -- lo hizo alguien de MaxiCare
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_contactos_no_contactar_origen'
           AND conrelid = 'contactos'::regclass
    ) THEN
        ALTER TABLE contactos
            ADD CONSTRAINT ck_contactos_no_contactar_origen CHECK (
                no_contactar_origen IS NULL
                OR no_contactar_origen IN ('paciente', 'clinica')
            );
    END IF;
END $$;

-- La bitácora se lee siempre por teléfono y en orden inverso: «qué ha decidido esta persona».
CREATE INDEX IF NOT EXISTS ix_consentimientos_telefono
    ON consentimientos (telefono, ocurrido_en DESC);
```

- [ ] **Step 2: Aplicar la migración y verificar que no rompe nada**

```bash
uv run python scripts/inicializar_base.py
```

Esperado: termina sin error y reporta la 019 entre las aplicadas. Correrlo **dos veces
seguidas** — la segunda tiene que ser igual de limpia, que es lo que demuestra la
idempotencia.

- [ ] **Step 3: Escribir las pruebas de la capa de datos (fallan)**

Crear `tests/test_contacto_neon.py`:

```python
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
    FK con ON DELETE RESTRICT."""
    import psycopg

    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.pedir_baja(conexion_pruebas, TEL)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
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
```

- [ ] **Step 4: Correrlas y verificar que fallan**

```bash
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_contacto_neon.py
```

Esperado: FAIL con `AttributeError: module 'maxicare_daniela.persistencia' has no attribute
'asegurar_contacto'`.

- [ ] **Step 5: Escribir las seis funciones**

En `persistencia.py`, en un bloque propio con su cabecera de sección (sigue el estilo de
`# ===...===` del módulo). Colócalo justo **antes** de `borrar_rastro`:

```python
# ==========================================================================================
# La persona del teléfono, y lo que ha decidido sobre sus datos (migración 019)
# ==========================================================================================


#: Las columnas de `contactos`, en un solo sitio. Las dos lecturas devuelven un `dict` con
#: exactamente estas claves, así que quien las consuma no depende del orden del SELECT.
_COLUMNAS_CONTACTO = (
    "telefono, creado_en, actualizado_en, aviso_mostrado_en, politica_version, "
    "no_contactar, no_contactar_en, no_contactar_origen"
)


def _fila_contacto(cur) -> dict[str, Any] | None:
    fila = cur.fetchone()
    if fila is None:
        return None
    return dict(zip([d[0] for d in cur.description], fila))


def asegurar_contacto(conn, telefono: str) -> dict[str, Any]:
    """La fila de ese número, creándola en blanco si no estaba. Nunca falla por existir.

    Nace sin aviso y sin baja: contactable, porque la baja es algo que el paciente pide y
    nunca un default. Tener fila aquí NO significa estar verificado -- eso lo sigue diciendo
    la existencia de la fila en `pacientes` (no negociable 12).

    `ON CONFLICT DO NOTHING` y no un `UPDATE`: dos mensajes del mismo número pueden entrar a
    la vez y el candado de `atencion` es de proceso, no protege entre réplicas.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO contactos (telefono) VALUES (%s) ON CONFLICT (telefono) DO NOTHING",
            (telefono,),
        )
        cur.execute(
            f"SELECT {_COLUMNAS_CONTACTO} FROM contactos WHERE telefono = %s", (telefono,)
        )
        fila = _fila_contacto(cur)
    conn.commit()
    if fila is None:
        # Imposible salvo bug: o la insertó esta llamada, o ya estaba. Un `assert` no vale
        # --`python -O` los borra-- y devolver `None` dejaría a `_leer_estado` reventando
        # más tarde con un `TypeError` sin relación aparente con la causa.
        raise RuntimeError(f"no se pudo asegurar el contacto de {telefono}")
    return fila


def leer_contacto(conn, telefono: str) -> dict[str, Any] | None:
    """Lo que ese número ha decidido, o `None` si nunca ha escrito.

    NO crea la fila: la usan el despachador y las tools, que no tienen por qué inventar un
    contacto solo por consultar.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS_CONTACTO} FROM contactos WHERE telefono = %s", (telefono,)
        )
        return _fila_contacto(cur)


def anotar_consentimiento(
    conn,
    telefono: str,
    *,
    evento: str,
    origen: str,
    version: str | None = None,
    detalle: str | None = None,
) -> None:
    """Una línea en la bitácora. Solo se añade: nada la modifica ni la borra.

    No hace `commit()` a propósito -- las tres funciones de abajo la llaman dentro de su
    propia transacción, para que el estado y su rastro entren o no entren juntos. Un estado
    cambiado sin rastro es exactamente lo que esta tabla existe para impedir.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO consentimientos (telefono, evento, origen, politica_version, detalle)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (telefono, evento, origen, version, (detalle or None) and detalle[:500]),
        )


def marcar_aviso_mostrado(conn, telefono: str, *, version: str) -> None:
    """Deja constancia de que a este número se le enseñó la política, y cuál.

    Se llama DESPUÉS de que el envío haya salido bien, nunca antes: ver el comentario de
    `atencion` donde se usa. Es al revés que un recordatorio (no negociable 21) y es
    deliberado.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET aviso_mostrado_en = now(), politica_version = %s, actualizado_en = now()
             WHERE telefono = %s
            """,
            (version, telefono),
        )
    anotar_consentimiento(
        conn, telefono, evento="aviso_mostrado", origen="codigo", version=version
    )
    conn.commit()


def pedir_baja(
    conn, telefono: str, *, origen: str = "paciente", detalle: str | None = None
) -> bool:
    """Apaga las comunicaciones COMERCIALES de ese número. Devuelve si el estado cambió.

    Nunca apaga el recordatorio de una cita: eso lo garantiza la lista blanca de
    `seguimientos.TIPOS_NO_COMERCIALES`, no esta función. Pedir que no te manden publicidad
    no es renunciar a que te avisen de tu propia cita, y confundir las dos cosas deja a un
    paciente sin llegar a la clínica.

    La bitácora se escribe SIEMPRE, aunque el estado ya estuviera puesto: pedirlo dos veces
    son dos hechos distintos, y los dos ocurrieron.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET no_contactar = TRUE, no_contactar_en = now(),
                   no_contactar_origen = %s, actualizado_en = now()
             WHERE telefono = %s AND no_contactar = FALSE
            """,
            (origen, telefono),
        )
        cambio = cur.rowcount > 0
    anotar_consentimiento(
        conn, telefono, evento="baja_solicitada", origen=origen, detalle=detalle
    )
    conn.commit()
    return cambio


def revocar_baja(
    conn, telefono: str, *, origen: str = "paciente", detalle: str | None = None
) -> bool:
    """Vuelve a permitir lo comercial. Devuelve si el estado cambió.

    Que el paciente escriba de nuevo NO llama a esto: la baja solo se levanta si la persona
    lo pide. La bitácora conserva las dos decisiones con sus fechas, que es lo que hace el
    historial acreditable.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET no_contactar = FALSE, no_contactar_en = NULL,
                   no_contactar_origen = NULL, actualizado_en = now()
             WHERE telefono = %s AND no_contactar = TRUE
            """,
            (telefono,),
        )
        cambio = cur.rowcount > 0
    anotar_consentimiento(
        conn, telefono, evento="baja_revocada", origen=origen, detalle=detalle
    )
    conn.commit()
    return cambio
```

- [ ] **Step 6: Meter el reseteo dentro de `borrar_rastro`**

En `persistencia.py`, dentro del `with conn.cursor() as cur:` de `borrar_rastro`, **después
del `DELETE FROM temas_telegram`** y antes del `conn.commit()`:

```python
            # La excepción de `/clearstate`, y el punto entero de la migración 019. Se resetea
            # el aviso --lo volverá a ver, que es lo correcto en un reseteo-- pero el
            # `no_contactar` NO se toca, y la fila NO se borra: si se fueran, resetear a
            # alguien lo devolvería a la lista de contactables sin que nadie se enterara.
            # Mismo criterio que los ejemplos de `casos_sin_resolver`, que se borran sin bajar
            # el contador (no negociable 22).
            cur.execute(
                """
                UPDATE contactos
                   SET aviso_mostrado_en = NULL, politica_version = NULL,
                       actualizado_en = now()
                 WHERE telefono = %(tel)s
                """,
                parametros,
            )
            borradas["contactos_reseteados"] = cur.rowcount

            # El `WHERE EXISTS` es obligatorio: `consentimientos.telefono` tiene una FK, y un
            # `/clearstate` sobre un número que nunca escribió la violaría y tumbaría la
            # transacción entera -- dejando el reseteo a medias justo por la mitad que nadie
            # mira.
            cur.execute(
                """
                INSERT INTO consentimientos (telefono, evento, origen)
                SELECT %(tel)s, 'rastro_borrado', 'clinica'
                 WHERE EXISTS (SELECT 1 FROM contactos WHERE telefono = %(tel)s)
                """,
                parametros,
            )
```

Y en el docstring de `borrar_rastro`, añadir al final un párrafo:

```
    `contactos` es la única tabla que este borrado NO borra. Se le resetea el aviso y nada
    más: el `no_contactar` sobrevive, porque es una decisión del paciente y no un estado del
    sistema. La bitácora `consentimientos` no se toca en ningún caso -- la FK con ON DELETE
    RESTRICT lo hace imposible aunque alguien lo intente.
```

- [ ] **Step 7: Correr las pruebas y verificar que pasan**

```bash
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_contacto_neon.py
```

Esperado: PASS, las 14.

Y la suite entera, para confirmar que no se rompió nada:

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
```

Esperado: todo verde. Si `test_reseteo_neon.py` falla por el conteo nuevo
`contactos_reseteados` en el `dict` que devuelve `borrar_rastro`, ajusta esa prueba: el
conteo nuevo es correcto y la prueba vieja es la que está desactualizada.

- [ ] **Step 8: Commit**

```bash
git add migraciones/019_contacto_y_consentimiento.sql tests/test_contacto_neon.py src/maxicare_daniela/persistencia.py
git commit -m "feat: la persona del telefono, y lo que ha decidido sobre sus datos

Dos tablas: contactos (estado actual, una fila por telefono, nace con el
primer mensaje) y consentimientos (bitacora inmutable, protegida por una
FK con ON DELETE RESTRICT).

La baja sobrevive a /clearstate. Guardarla en conversaciones -- que era el
patron que ya existia -- la habria borrado con ellas, y un numero dado de
baja habria vuelto a ser contactable sin que nadie se enterara.

Los tres CHECK van anclados con conrelid ::regclass: sin eso el IF NOT
EXISTS los ve desde public y la lista queda abierta en pruebas. Leccion de
la migracion 013.

Nada usa esto todavia.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: El contacto nace en el turno, y la señal de la baja llega a Daniela

Al terminar esta tarea **no cambia ni un mensaje que vea un paciente**. Solo se crea la fila y
viajan dos señales nuevas.

**Files:**
- Modify: `src/maxicare_daniela/atencion.py` (`_Estado:226-250`, `_leer_estado:258-341`,
  `ContextoDaniela(...)` en `1030-1044`)
- Modify: `src/maxicare_daniela/contratos.py` (`ContextoDaniela`, junto a
  `telefono_sin_paciente:604`)
- Test: `tests/test_atencion.py`

**Interfaces:**
- Consumes: `persistencia.asegurar_contacto(conn, telefono) -> dict[str, Any]` (Tarea 1).
- Produces:
  - `atencion._Estado.pidio_no_contacto: bool` y `atencion._Estado.aviso_visto: bool`
  - `contratos.ContextoDaniela.pidio_no_contacto: bool`

  **`aviso_visto` se lee aquí y no se usa hasta la Tarea 3.** Es deliberado: sale de la misma
  fila que ya se está leyendo, y tocar la dataclass dos veces en dos tareas es peor que un
  campo que espera una tarea.

- [ ] **Step 1: Escribir las pruebas (fallan)**

En `tests/test_atencion.py`, al final. Sigue el estilo de dobles del archivo — mira cómo las
pruebas existentes construyen un `_Estado` o doblan `_leer_estado` antes de escribir estas, y
reutiliza sus ayudantes en vez de inventar otros:

```python
def test_el_estado_lleva_la_senal_de_la_baja():
    """Sale de la base y nunca del modelo, igual que `telefono_sin_paciente`. Si el modelo
    pudiera ponerla, bastaría con que dijera «no me escriban» para desactivar la
    reactivación de otro."""
    estado = atencion._Estado(
        id_conversacion="c-1",
        turno_actual=0,
        identidad_verificada=False,
        intentos_identificacion=0,
        id_paciente=None,
        nombre_paciente=None,
        telefono_sin_paciente=True,
        tomada_por=None,
        pidio_no_contacto=True,
    )

    assert estado.pidio_no_contacto is True
    # El default es contactable: la baja es algo que el paciente pide.
    assert estado.aviso_visto is False


def test_el_contexto_recibe_la_senal_de_la_baja():
    """El fallo que esto evita: doce días después de darse de baja, María escribe por una
    muela rota. Para el sistema es una conversación nueva y en blanco, así que sin esta
    señal Daniela cierra como cierra siempre --«¿te escribo en unos días?»-- y le pide
    permiso para algo que ella ya negó expresamente."""
    ctx = contratos.ContextoDaniela(
        id_conversacion="c-1",
        telefono_completo="573001112201",
        database_url="postgres://nada",
        calendario=None,
        pidio_no_contacto=True,
    )

    assert ctx.pidio_no_contacto is True


def test_el_contexto_por_defecto_no_tiene_baja():
    ctx = contratos.ContextoDaniela(
        id_conversacion="c-1",
        telefono_completo="573001112201",
        database_url="postgres://nada",
        calendario=None,
    )

    assert ctx.pidio_no_contacto is False
```

- [ ] **Step 2: Correrlas y verificar que fallan**

```bash
uv run pytest -q tests/test_atencion.py -k "baja"
```

Esperado: FAIL con `TypeError: __init__() got an unexpected keyword argument
'pidio_no_contacto'`.

- [ ] **Step 3: Añadir los dos campos a `_Estado`**

En `atencion.py`, dentro de `_Estado`, **después de `operativa`** (los campos con default van
detrás de los que no lo tienen):

```python
    #: Este número pidió que no le escribieran más. Sale de `contactos` --tabla propia por
    #: teléfono, migración 019-- y nunca del modelo: si lo pusiera él, bastaría con que un
    #: paciente dijera «no me escriban» en una frase ambigua para apagarle la reactivación a
    #: otro. Con esto puesto, Daniela no ofrece el seguimiento ni lo menciona.
    pidio_no_contacto: bool = field(default=False)
    #: Este número ya vio el aviso de la política. Se lee aquí, en la misma pasada, y lo
    #: consume el pegado del aviso antes del envío.
    aviso_visto: bool = field(default=False)
```

- [ ] **Step 4: Crear el contacto y leer las señales en `_leer_estado`**

En `atencion.py`, dentro del `with persistencia.conectar(database_url) as conn:` de
`_leer_estado`, **justo después de `paciente = persistencia.buscar_paciente_por_telefono(...)`**:

```python
        # La fila nace aquí, con el primer mensaje que entra, y no cuando alguien agenda: los
        # que preguntan y no agendan son justo los que hay que poder recordar. `asegurar_`
        # porque puede existir desde hace meses; es idempotente.
        contacto = persistencia.asegurar_contacto(conn, telefono)
```

Y en el `return _Estado(...)`, después de `operativa=operativa,`:

```python
        pidio_no_contacto=bool(contacto["no_contactar"]),
        aviso_visto=contacto["aviso_mostrado_en"] is not None,
```

- [ ] **Step 5: Añadir el campo a `ContextoDaniela`**

En `contratos.py`, dentro de `ContextoDaniela`, **justo después del bloque de
`telefono_sin_paciente`** (para que las dos señales que salen de la base queden juntas):

```python
    #: Este número pidió que no le escribieran más. Sale de `contactos` y **nunca del
    #: modelo**, igual que `telefono_sin_paciente`. Con esto puesto, Daniela no ofrece el
    #: seguimiento, no lo insinúa y no lo menciona: el tema no existe en esa conversación.
    #:
    #: Es la baja COMERCIAL. No apaga el recordatorio de una cita ni impide atenderla si ella
    #: escribe: pedir que no te manden publicidad no es darse de baja de la clínica.
    pidio_no_contacto: bool = False
```

- [ ] **Step 6: Pasarlo al contexto**

En `atencion.py`, dentro del `ctx = ContextoDaniela(...)`, después de
`telefono_sin_paciente=estado.telefono_sin_paciente,`:

```python
            pidio_no_contacto=estado.pidio_no_contacto,
```

- [ ] **Step 7: Correr y verificar**

```bash
uv run pytest -q
```

Esperado: todo verde, incluidas las tres nuevas.

```bash
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_atencion.py
```

Esperado: verde. `probar_atencion.py` dobla funciones de `src/` con firmas escritas a mano y
`pytest -q` no lo corre: es la única forma de cazar que `_leer_estado` haya dejado de encajar.
No gasta tokens sin `--chat`.

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/atencion.py src/maxicare_daniela/contratos.py tests/test_atencion.py
git commit -m "feat: la fila de contacto nace con el primer mensaje, y la baja llega al turno

El sistema solo sabia de conversaciones (24 h) y de pacientes (solo si
agendas). Quien pregunta y no agenda no era ninguna de las dos: a las 24
horas dejaba de existir.

ctx.pidio_no_contacto sale de la base y nunca del modelo. Evita el fallo de
volver a ofrecerle el seguimiento a quien ya lo nego: doce dias despues es
una conversacion nueva y en blanco, y Daniela cerraria como cierra siempre.

No cambia ni un mensaje que vea un paciente.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: El aviso de la política

La primera tarea que cambia lo que ve un paciente. Con la URL en `PENDIENTE` —su default— **no
cambia nada**: el aviso no se emite.

**Files:**
- Modify: `src/maxicare_daniela/config.py` (campos junto a `plantilla_recordatorio:401`, y
  `desde_entorno:412-433`)
- Modify: `src/maxicare_daniela/atencion.py` (constante nueva; el tramo del envío en
  `1121-1153`)
- Modify: `.env.ejemplo`
- Test: `tests/test_atencion.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `persistencia.marcar_aviso_mostrado(conn, telefono, *, version)` (Tarea 1);
  `atencion._Estado.aviso_visto` (Tarea 2).
- Produces:
  ```python
  atencion.AVISO_POLITICA: str          # la plantilla del texto, con {url}
  def atencion._con_aviso(respuesta: str, *, url: str) -> str
  def atencion._toca_avisar(estado: _Estado, *, url: str) -> bool
  def atencion._marcar_aviso(database_url: str, telefono: str, version: str) -> None
  config.Config.politica_datos_url: str       # default "PENDIENTE"
  config.Config.politica_datos_version: str   # default "politica-2026-09"
  ```

**Un camino que queda FUERA a propósito:** el envío de emergencia de `atencion.py:946`
(`enviar_texto(..., conversacion.MENSAJE_SEGURO)`) no lleva aviso. Es otro punto de salida,
anterior a que exista el `_Estado`, y meterle el aviso obligaría a cargar el contacto en un
camino cuyo único trabajo es no dejar al paciente en silencio cuando algo ya se rompió. La
consecuencia es inocua: ese número verá el aviso en el mensaje siguiente. Queda dicho aquí
para que no parezca un olvido.

- [ ] **Step 1: Escribir las pruebas (fallan)**

En `tests/test_config.py`:

```python
def test_la_url_de_la_politica_arranca_en_pendiente(monkeypatch):
    """Regla dura 3: lo que no se sabe se marca con el literal, nunca con un valor plausible.
    Aquí además apaga el aviso, que es lo que se quiere: es preferible no enseñar un enlace
    que enseñar uno que no vamos a poder sostener."""
    monkeypatch.setenv("MAXICARE_DATABASE_URL", "postgres://nada")
    monkeypatch.delenv("MAXICARE_POLITICA_DATOS_URL", raising=False)

    c = config.Config.desde_entorno()

    assert c.politica_datos_url == "PENDIENTE"
    assert c.politica_datos_version == "politica-2026-09"
```

En `tests/test_atencion.py`:

```python
def test_sin_url_no_se_aniade_el_aviso():
    """El default es PENDIENTE, y con él el mensaje sale limpio. Mandarle el marcador a un
    paciente sería leerse la regla dura 3 al revés."""
    assert atencion._con_aviso("Hola, con gusto te cuento.", url="PENDIENTE") == (
        "Hola, con gusto te cuento."
    )
    assert atencion._con_aviso("Hola.", url="") == "Hola."


def test_con_url_el_aviso_va_al_final_y_separado():
    """Es un pie, no una interrupción: la respuesta del paciente va primero."""
    salida = atencion._con_aviso("Hola.", url="https://maxicarecol.com/politica-datos")

    assert salida.startswith("Hola.")
    assert salida.endswith(
        "Al continuar aceptas nuestra política de tratamiento de datos: "
        "https://maxicarecol.com/politica-datos"
    )


def test_el_texto_del_aviso_no_se_reescribe_en_cada_sitio():
    """Una sola constante. Si cada sitio lo redactara, dos pacientes tendrían dos avisos
    distintos y ninguno sería el que dice la bitácora que vieron."""
    assert "{url}" in atencion.AVISO_POLITICA


def test_a_quien_ya_lo_vio_no_se_le_repite():
    """Sale UNA vez en la vida de ese número, no una por conversación. El dato viene de
    `contactos`, que no caduca a las 24 h: si viniera de la conversación, el paciente vería
    el aviso legal cada día que escribiera."""
    url = "https://maxicarecol.com/politica-datos"

    assert atencion._toca_avisar(_estado(aviso_visto=False), url=url) is True
    assert atencion._toca_avisar(_estado(aviso_visto=True), url=url) is False


def test_con_la_url_pendiente_no_toca_avisar_a_nadie():
    assert atencion._toca_avisar(_estado(aviso_visto=False), url="PENDIENTE") is False
    assert atencion._toca_avisar(_estado(aviso_visto=False), url="") is False
```

> `_estado(**cambios)` es un ayudante local: construye un `atencion._Estado` con los mismos
> valores mínimos que ya usaste en las pruebas de la Tarea 2. Si el archivo ya tiene uno
> equivalente, reutilízalo en vez de añadir otro.

Y la prueba del camino que da sentido a todo el mecanismo de marcado. Usa los dobles con los
que este archivo ya prueba el turno completo, incluido el del envío que falla:

```python
@pytest.mark.asyncio
async def test_si_el_envio_falla_el_aviso_NO_queda_marcado(...):
    """Al revés que un recordatorio (no negociable 21), y deliberadamente. Marcar antes
    significaría que un timeout de red deja constancia de un aviso que el paciente nunca
    vio -- y esa constancia es precisamente la prueba. Repetir un aviso es inocuo;
    falsificar una prueba, no.

    Doblar `whatsapp.enviar_texto` para que lance, y comprobar que `_marcar_aviso` no se
    llamó ni una vez.
    """
```

- [ ] **Step 2: Correrlas y verificar que fallan**

```bash
uv run pytest -q tests/test_atencion.py tests/test_config.py -k "aviso or politica"
```

Esperado: FAIL con `AttributeError: module 'maxicare_daniela.atencion' has no attribute
'_con_aviso'`.

- [ ] **Step 3: Añadir la configuración**

En `config.py`, junto a `plantilla_recordatorio`:

```python
    #: La dirección donde vive la política de tratamiento de datos que el paciente ve en su
    #: primer mensaje. `PENDIENTE` --su default-- APAGA el aviso: sale el mensaje limpio y no
    #: se registra nada.
    #:
    #: PENDIENTE: la URL real. El PDF existe y es público en Drive, pero ese no es el destino
    #: final por tres razones: Drive deja subir una versión nueva sobre el mismo archivo sin
    #: que el enlace cambie --y entonces quien ya aceptó apunta a un texto que no es el que
    #: vio, que es justo lo que hay que poder acreditar--; un enlace opaco dentro de un
    #: mensaje que pide confianza sobre datos personales trabaja en contra de sí mismo; y si
    #: alguien mueve el archivo, el enlace muere en silencio y el sistema lo sigue mandando.
    #: Tiene que vivir en el dominio de MaxiCare, con una copia congelada por versión.
    politica_datos_url: str = "PENDIENTE"

    #: El identificador de la versión vigente de la política. Se congela en cada fila de
    #: `consentimientos`: si la política cambia, hay que poder demostrar cuál vio cada
    #: persona. Cambiarlo NO reenvía el aviso a quien ya lo vio -- eso es una decisión
    #: aparte, y hoy no está construida.
    politica_datos_version: str = "politica-2026-09"
```

Y en `desde_entorno`, junto a las de plantilla:

```python
            politica_datos_url=_opcional("MAXICARE_POLITICA_DATOS_URL", "PENDIENTE"),
            politica_datos_version=_opcional(
                "MAXICARE_POLITICA_DATOS_VERSION", "politica-2026-09"
            ),
```

En `.env.ejemplo`, junto a las de plantilla:

```
# La politica de tratamiento de datos que ve el paciente en su primer mensaje.
# PENDIENTE apaga el aviso: mientras no este servida desde el dominio de MaxiCare,
# es preferible no enseñar un enlace que no vamos a poder sostener.
MAXICARE_POLITICA_DATOS_URL=
MAXICARE_POLITICA_DATOS_VERSION=
```

- [ ] **Step 4: Escribir la constante y el ayudante**

En `atencion.py`, junto a las otras constantes del módulo (cerca de
`VENTANA_CONVERSACION_HORAS:97`):

```python
#: El aviso de la política, en un solo sitio y con un solo texto. Lo pega el CÓDIGO al primer
#: mensaje saliente de un número que nunca lo ha visto, y no Daniela: una frase en el prompt
#: sirve para que lo diga, pero no sirve como prueba -- nadie sabría si lo dijo, con qué
#: palabras, ni si un día el modelo decidió resumirlo. Mismo principio que las claves de
#: idempotencia (no negociable 2) y la huella de los casos sin resolver (no negociable 22):
#: lo que tiene que ser demostrable lo escribe el código.
AVISO_POLITICA = "Al continuar aceptas nuestra política de tratamiento de datos: {url}"


def _con_aviso(respuesta: str, *, url: str) -> str:
    """La respuesta con el aviso pegado al final, o tal cual si no hay URL que enseñar.

    Va como línea aparte y al final: es un pie, no una interrupción. Lo que el paciente
    preguntó se responde primero.

    Con `url` vacía o en `PENDIENTE` devuelve la respuesta intacta. La regla dura 3 es para
    el código: nunca fue permiso para mandarle el marcador a un paciente.
    """
    if not url or url == "PENDIENTE":
        return respuesta
    return f"{respuesta}\n\n{AVISO_POLITICA.format(url=url)}"


def _toca_avisar(estado: _Estado, *, url: str) -> bool:
    """Si a este número hay que enseñarle el aviso en este mensaje.

    Es una función y no un `if` suelto porque las dos condiciones son la política entera:
    sale UNA vez en la vida del número --el dato viene de `contactos`, que no caduca a las
    24 h, así que un paciente que escribe cada día no ve el aviso legal cada día-- y no sale
    en absoluto mientras no haya una URL que enseñar.
    """
    if not url or url == "PENDIENTE":
        return False
    return not estado.aviso_visto
```

- [ ] **Step 5: Pegarlo antes del envío y marcarlo después**

En `atencion.py`, **justo antes del `try:` que llama a `whatsapp.enviar_texto`** (el de la
línea 1128 aproximadamente, después del `await (dormir or asyncio.sleep)(espera)`):

```python
        # El aviso va pegado al primer saliente de un número que nunca lo ha visto. Se decide
        # aquí, con el texto ya cerrado, para que valga igual si la respuesta salió del modelo
        # o si es el mensaje seguro: a alguien que entra por primera vez y se encuentra un
        # fallo también se le está atendiendo.
        toca_avisar = _toca_avisar(estado, url=config.politica_datos_url)
        if toca_avisar:
            respuesta = _con_aviso(respuesta, url=config.politica_datos_url)
```

Y **dentro del `try`, inmediatamente después de la línea que hace
`wamid_respuesta = await whatsapp.enviar_texto(...)`**, en el camino de éxito:

```python
            if toca_avisar:
                # DESPUÉS del envío, nunca antes. Es al revés que un recordatorio (no
                # negociable 21) y es deliberado: allí el riesgo es mandarlo dos veces, así
                # que se marca antes; aquí el riesgo es dar por mostrado un aviso que no
                # salió, y esa constancia es precisamente la prueba. Repetir un aviso es
                # inocuo; falsificar una prueba, no.
                #
                # Si esto falla, el turno sigue: el paciente ya tiene su respuesta y el aviso
                # se le volverá a enseñar en el siguiente mensaje.
                try:
                    await asyncio.to_thread(
                        _marcar_aviso,
                        config.database_url,
                        mensaje.telefono,
                        config.politica_datos_version,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("no se pudo registrar el aviso de %s", mensaje.telefono)
```

Y el ayudante, junto a las otras funciones que tocan la base en `atencion.py`:

```python
def _marcar_aviso(database_url: str, telefono: str, version: str) -> None:
    """Sincrónica, y siempre dentro de `asyncio.to_thread`, como el resto del módulo."""
    with persistencia.conectar(database_url) as conn:
        persistencia.marcar_aviso_mostrado(conn, telefono, version=version)
```

- [ ] **Step 6: Correr y verificar**

```bash
uv run pytest -q
```

Esperado: todo verde.

```bash
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_atencion.py
```

Esperado: verde.

- [ ] **Step 7: Commit**

```bash
git add src/maxicare_daniela/config.py src/maxicare_daniela/atencion.py .env.ejemplo tests/test_atencion.py tests/test_config.py
git commit -m "feat: el aviso de la politica lo emite el codigo, y se marca DESPUES del envio

Hasta ahora el aviso era una frase del prompt. Sirve para que Daniela lo
diga; no sirve como prueba: nadie sabia si lo dijo, con que palabras, ni si
el modelo decidio resumirlo.

Se marca despues del envio, al reves que un recordatorio (no negociable 21).
Alli el riesgo es mandarlo dos veces; aqui el riesgo es dar por mostrado uno
que no salio, y esa constancia ES la prueba. Repetir un aviso es inocuo.

Con la URL en PENDIENTE -- su default -- no cambia nada: el aviso no se
emite y el mensaje sale limpio.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Las dos tools y lo que Daniela sabe hacer con ellas

**Files:**
- Modify: `src/maxicare_daniela/herramientas.py` (tools nuevas; registro en la lista de tools
  en `1923` y en `__all__` en `1942`)
- Modify: `src/maxicare_daniela/agentes.py` (prompt, tras la línea 84)
- Test: `tests/test_herramientas.py`, `tests/test_agentes.py`

**Interfaces:**
- Consumes: `persistencia.pedir_baja`, `persistencia.revocar_baja` (Tarea 1);
  `ContextoDaniela.telefono_completo`, `ContextoDaniela.pidio_no_contacto` (Tarea 2).
- Produces: las tools `registrar_no_contactar` y `revocar_no_contactar`.

- [ ] **Step 1: Escribir las pruebas (fallan)**

En `tests/test_herramientas.py`. El archivo ya tiene lo que hace falta: `contexto(**cambios)`
(línea 39) construye el `ContextoDaniela`, `BaseFalsa` (línea 50) sustituye al `conn`, y cada
prueba hace `monkeypatch.setattr(h, "_con_base", base_falsa)`. El módulo se importa como `h`.

```python
@pytest.mark.asyncio
async def test_registrar_no_contactar_apaga_lo_comercial(monkeypatch):
    """El modelo solo levanta la mano. La fecha, el origen y la versión las arma el código
    desde `ctx`: si el modelo pudiera escribirlas, la bitácora dejaría de ser una prueba."""
    anotado: list[tuple] = []

    async def base_falsa(ctx, trabajo):
        trabajo(BaseFalsa())

    def pedir_baja(conn, telefono, *, origen="paciente", detalle=None):
        anotado.append((telefono, origen, detalle))
        return True

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "pedir_baja", pedir_baja)

    salida = await h._registrar_no_contactar(contexto(), "dijo que no le escriban")

    assert anotado == [("573001112233", "paciente", "dijo que no le escriban")]
    # El texto que vuelve al modelo tiene que decirle las tres cosas: que quedó anotado, que
    # no insista, y que la cita no se toca.
    assert "no intentes retenerlo" in salida or "no le ofrezcas alternativas" in salida


@pytest.mark.asyncio
async def test_revocar_no_contactar_la_levanta(monkeypatch):
    anotado: list[tuple] = []

    async def base_falsa(ctx, trabajo):
        trabajo(BaseFalsa())

    def revocar_baja(conn, telefono, *, origen="paciente", detalle=None):
        anotado.append((telefono, origen, detalle))
        return True

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "revocar_baja", revocar_baja)

    salida = await h._revocar_no_contactar(contexto(), "pidio que le avisen")

    assert anotado == [("573001112233", "paciente", "pidio que le avisen")]
    assert salida


def test_las_dos_tools_estan_registradas():
    """Una tool que existe y no está en la lista es una tool que el modelo no puede llamar,
    sin un solo error en ningún log."""
    nombres = {t.name for t in h.TODAS}

    assert "registrar_no_contactar" in nombres
    assert "revocar_no_contactar" in nombres
```

En `tests/test_agentes.py`:

```python
def test_el_prompt_prohibe_persuadir_a_quien_pide_la_baja():
    """Marketing lo pidió expresamente: confirmar y no retener."""
    assert "no intentas retenerlo" in agentes.INSTRUCCIONES_DANIELA


def test_el_prompt_manda_callar_el_seguimiento_a_quien_lo_nego():
    assert "no lo ofreces, no lo insinúas y no lo mencionas" in agentes.INSTRUCCIONES_DANIELA
```

- [ ] **Step 2: Correrlas y verificar que fallan**

```bash
uv run pytest -q tests/test_herramientas.py tests/test_agentes.py -k "contactar or baja or persuadir or seguimiento"
```

Esperado: FAIL con `AttributeError: ... has no attribute '_registrar_no_contactar'`.

- [ ] **Step 3: Escribir las dos tools**

En `herramientas.py`, en una sección nueva al final de las tools. Copia el patrón exacto de
`registrar_estado_oportunidad` (`herramientas.py:1317-1377`): helper `_nombre` privado que
recibe el `ctx`, función decorada que recibe el `RunContextWrapper`, y `_con_base(ctx,
trabajo)` para el acceso a Neon.

Antes, crea el manejador de fallo hermano de `_fallo_estado` (`herramientas.py:401`) — cópialo
y cámbiale el texto; **no reutilices `_fallo_estado`**, que habla del estado de la
conversación y aquí diría algo que no es:

```python
def _fallo_privacidad(ctx: RunContextWrapper[ContextoDaniela], error: Exception) -> str:
    log.exception("no se pudo registrar la decisión de privacidad")
    return (
        "No se pudo registrar ahora mismo. Dile al paciente que su solicitud queda anotada "
        "y escala: esto NO se deja pasar en silencio."
    )
```

```python
# ==========================================================================================
# 11. La baja comercial y su revocación
# ==========================================================================================


async def _registrar_no_contactar(ctx: ContextoDaniela, nota: str | None) -> str:
    def trabajo(conn) -> None:
        persistencia.pedir_baja(
            conn, ctx.telefono_completo, origen="paciente", detalle=nota
        )

    await _con_base(ctx, trabajo)
    return (
        "Anotado: a este número no le vuelve a salir nada comercial, ni ahora ni nunca. "
        "Confírmaselo en una línea y sigue con lo que necesite. No le preguntes por qué, no "
        "le ofrezcas alternativas y no intentes retenerlo. Su cita, si tiene una, le sigue "
        "llegando igual: esto no la toca."
    )


@function_tool(failure_error_function=_fallo_privacidad)
async def registrar_no_contactar(
    wrapper: RunContextWrapper[ContextoDaniela],
    nota: str,
) -> str:
    """Anota que el paciente NO quiere recibir más mensajes nuestros.

    Llámala en cuanto lo pida, aunque lo diga de pasada. Si dudas entre si lo pidió o no,
    llámala igual: dejar de escribirle a quien no lo pidió es una molestia, escribirle a
    quien sí lo pidió es faltarle al respeto.

    NO la llames porque el paciente esté molesto, tenga prisa o no conteste. Solo cuando
    pida que no le escriban.

    Args:
        nota: la frase con la que lo pidió, tal cual. Sin interpretarla.
    """
    return await _registrar_no_contactar(wrapper.context, nota or None)


async def _revocar_no_contactar(ctx: ContextoDaniela, nota: str | None) -> str:
    def trabajo(conn) -> None:
        persistencia.revocar_baja(
            conn, ctx.telefono_completo, origen="paciente", detalle=nota
        )

    await _con_base(ctx, trabajo)
    return "Anotado: vuelve a recibir mensajes nuestros. Confírmaselo en una línea."


@function_tool(failure_error_function=_fallo_privacidad)
async def revocar_no_contactar(
    wrapper: RunContextWrapper[ContextoDaniela],
    nota: str,
) -> str:
    """Vuelve a activar los mensajes a un paciente que los había desactivado.

    SOLO si lo pide él. Que vuelva a escribirte no es pedirlo: alguien que se dio de baja y
    meses después pregunta por una muela rota sigue sin querer publicidad.

    Args:
        nota: la frase con la que lo pidió, tal cual.
    """
    return await _revocar_no_contactar(wrapper.context, nota or None)
```

Registra las dos en la tupla `TODAS` (`herramientas.py:1918-1927`) y en `__all__`
(`herramientas.py:1929-1944`), que está **ordenado alfabéticamente**: `registrar_no_contactar`
va justo después de `registrar_estado_oportunidad`, y `revocar_no_contactar` después de
`reprogramar_cita`.

- [ ] **Step 4: Escribir las instrucciones de Daniela**

En `agentes.py`, dentro de `INSTRUCCIONES_DANIELA`, **sustituye** el párrafo de la política de
datos (líneas 83-84) por este bloque. La primera frase se va porque ese aviso ya no lo dices
tú: lo pega el código:

```
Si el paciente pide que no le escribas más, llamas `registrar_no_contactar` en ese mismo \
turno y se lo confirmas en una línea. No le preguntas por qué, no le ofreces alternativas y \
no intentas retenerlo. Si además pide borrar sus datos, revocar una autorización o poner una \
queja sobre ellos, lo mandas a maxicarecol@gmail.com o al +57 321 981 2422, que es donde eso \
se atiende. Nunca pides cédula ni documentos de identidad.

Cuando el contexto dice que este paciente pidió no ser contactado, el seguimiento deja de \
existir para ti: no lo ofreces, no lo insinúas y no lo mencionas. Le atiendes igual de bien \
en todo lo demás.
```

> Comprueba antes si el prompt ya lleva la lista de tools o una sección de reglas de
> privacidad, y coloca el bloque donde encaje con esa estructura en vez de donde diga este
> plan.

- [ ] **Step 5: Correr y verificar**

```bash
uv run pytest -q
```

Esperado: todo verde.

```bash
uv run python scripts/probar_tools.py
```

Esperado: OK en las diez tools de la fase 3 (no gasta tokens). Si el script lleva una cuenta
fija de tools, súbela a doce.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/herramientas.py src/maxicare_daniela/agentes.py tests/test_herramientas.py tests/test_agentes.py
git commit -m "feat: Daniela sabe apagar y volver a encender lo comercial de un numero

Dos tools. El modelo solo levanta la mano: la fecha, el origen y la version
las arma el codigo desde ctx, porque una bitacora que escriba el modelo deja
de ser una prueba.

Ante la duda se marca la baja. Dejar de escribirle a quien no lo pidio es
una molestia; escribirle a quien si lo pidio es el problema que todo esto
existe para evitar.

Y con la baja puesta, el seguimiento deja de existir para Daniela: no lo
ofrece, no lo insinua y no lo menciona.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: El despachador respeta la baja, y nunca la cita

La tarea que cierra la promesa central: una baja comercial **no** puede quitarle a nadie el
recordatorio de su cita.

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py` (`seguimientos_por_despachar:1812-1861`)
- Modify: `src/maxicare_daniela/seguimientos.py` (constante nueva; `decidir:160-193`)
- Test: `tests/test_seguimientos.py`, `tests/test_seguimientos_neon.py`

**Interfaces:**
- Consumes: la columna `no_contactar` de `contactos` (Tarea 1).
- Produces: `seguimientos.TIPOS_NO_COMERCIALES: frozenset[str]`, y la clave `no_contactar` en
  cada fila que devuelve `seguimientos_por_despachar`.

- [ ] **Step 1: Escribir las pruebas offline (fallan)**

En `tests/test_seguimientos.py`. El archivo ya tiene `fila(**cambios)` (línea 115),
`JORNADA` (línea 21) y `momento(dia, hora, minuto=0)` (línea 49) — úsalos tal cual.

**Primero, añade la clave nueva al helper `fila`**, dentro de su `base`, después de
`tomada_por=None`:

```python
        no_contactar=False,
```

Luego, las cuatro pruebas, junto a las de las otras guardas:

```python
def test_g0_anula_una_reactivacion_a_quien_pidio_la_baja():
    decision = seguimientos.decidir(
        fila(tipo="reactivacion", cita_id=None, cita_inicio=None, no_contactar=True),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "anular"
    assert decision.motivo == "baja_solicitada"


def test_g0_NO_toca_el_recordatorio_de_una_cita():
    """La prueba que más importa de todo el trabajo. Pedir que no te manden publicidad no es
    renunciar a que te avisen de tu propia cita: si se mezclan, el paciente no llega, la
    clínica pierde el cupo, y el sistema habría hecho exactamente lo que se le pidió."""
    decision = seguimientos.decidir(
        fila(tipo="recordatorio_cita", no_contactar=True),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "enviar"


def test_un_tipo_inventado_se_trata_como_comercial():
    """`seguimientos.tipo` es texto libre que escribe el modelo. La lista blanca falla hacia
    el lado seguro: lo que no está en ella se comprueba contra la baja. Una lista negra
    dejaría pasar cualquier invento directo al envío."""
    decision = seguimientos.decidir(
        fila(tipo="promo_de_diciembre", cita_id=None, cita_inicio=None, no_contactar=True),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "anular"
    assert decision.motivo == "baja_solicitada"


def test_sin_baja_g0_no_hace_nada():
    decision = seguimientos.decidir(
        fila(tipo="reactivacion", cita_id=None, cita_inicio=None, no_contactar=False),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "enviar"
```

> Si `momento(16, 18)` no es el `ahora` con el que la fila por defecto decide `enviar`, copia
> el que usa `test_un_recordatorio_limpio_sale` (línea 266) — esa prueba ya tiene la
> combinación que atraviesa las siete guardas limpia.

En `tests/test_seguimientos_neon.py`, que la columna llegue de verdad. El archivo ya tiene el
fixture `conexion_pruebas`; copia de las pruebas existentes la forma exacta de crear una
conversación y un seguimiento, y añade estas dos:

```python
def test_la_consulta_trae_la_baja_del_contacto(conexion_pruebas):
    """La comprobación entra como una columna del SELECT que ya hace LEFT JOIN para sacar el
    teléfono, y no como una consulta por fila: una tanda son hasta 50."""
    id_conv = persistencia.asegurar_conversacion(
        conexion_pruebas, telefono=TEL, paciente_id=None, canal="whatsapp"
    )
    ayer = datetime.now(ZONA_BOGOTA) - timedelta(days=1)
    persistencia.programar_seguimiento(
        conexion_pruebas,
        conversacion_id=id_conv,
        tipo="reactivacion",
        fecha_objetivo=ayer,
        clave_idempotencia="baja-1",
    )
    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.pedir_baja(conexion_pruebas, TEL)

    filas = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=datetime.now(ZONA_BOGOTA)
    )

    assert filas[0]["no_contactar"] is True


def test_un_telefono_sin_fila_de_contacto_no_cuenta_como_baja(conexion_pruebas):
    """El LEFT JOIN devuelve NULL, y NULL no es TRUE. El COALESCE lo hace explícito para que
    nadie tenga que acordarse de esto al leer la guarda."""
    id_conv = persistencia.asegurar_conversacion(
        conexion_pruebas, telefono=TEL, paciente_id=None, canal="whatsapp"
    )
    ayer = datetime.now(ZONA_BOGOTA) - timedelta(days=1)
    persistencia.programar_seguimiento(
        conexion_pruebas,
        conversacion_id=id_conv,
        tipo="reactivacion",
        fecha_objetivo=ayer,
        clave_idempotencia="sin-contacto-1",
    )

    filas = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=datetime.now(ZONA_BOGOTA)
    )

    assert filas[0]["no_contactar"] is False
```

> Comprueba la firma real de `persistencia.programar_seguimiento` antes de copiarla: si no
> coincide, usa la que el propio archivo ya emplea en sus otras pruebas.

- [ ] **Step 2: Correrlas y verificar que fallan**

```bash
uv run pytest -q tests/test_seguimientos.py -k "g0 or inventado"
```

Esperado: FAIL — `decidir` devuelve `enviar` donde se espera `anular`.

- [ ] **Step 3: Traer la columna en la consulta**

En `persistencia.py`, en `seguimientos_por_despachar`, añade al SELECT y al FROM:

```sql
            SELECT s.id, s.conversacion_id, s.cita_id, s.tipo, s.fecha_objetivo, s.intentos,
                   COALESCE(c.telefono, cv.telefono)      AS telefono,
                   c.nombre_completo, c.tratamiento, c.inicio AS cita_inicio,
                   c.estado AS cita_estado, cv.tomada_por,
                   -- La baja comercial. Entra como columna de este SELECT --que ya hace el
                   -- LEFT JOIN para sacar el teléfono-- y no como una consulta por fila: una
                   -- tanda son hasta 50. El COALESCE hace explícito que un número sin fila
                   -- de contacto NO está de baja: el LEFT JOIN devuelve NULL, y NULL no es
                   -- FALSE para un `if`.
                   COALESCE(co.no_contactar, FALSE)       AS no_contactar
              FROM seguimientos s
              LEFT JOIN citas c           ON c.id  = s.cita_id
              LEFT JOIN conversaciones cv ON cv.id = s.conversacion_id
              LEFT JOIN contactos co      ON co.telefono = COALESCE(c.telefono, cv.telefono)
             WHERE s.enviado_en IS NULL
               AND s.anulado_en IS NULL
               AND s.fecha_objetivo <= %s
             ORDER BY s.fecha_objetivo
             LIMIT %s
               FOR UPDATE OF s SKIP LOCKED
```

> `FOR UPDATE OF s` ya nombra la tabla explícitamente, así que el LEFT JOIN nuevo no cambia
> qué se bloquea. Verifícalo al correr las pruebas de Neon.

- [ ] **Step 4: La lista blanca y la guarda G0**

En `seguimientos.py`, junto a las constantes (cerca de las líneas 126-148):

```python
#: Los tipos de seguimiento que NO son comerciales, y que por tanto una baja NO apaga.
#:
#: Es una lista BLANCA a propósito. `seguimientos.tipo` es texto libre que escribe el modelo
#: (`programar_seguimiento`), así que con una lista negra de tipos comerciales, cualquier tipo
#: inventado se colaría directo al envío. Con esta, lo que no está aquí se comprueba contra la
#: baja: falla hacia el lado seguro.
#:
#: Acotar `tipo` con un `Literal` y un CHECK es trabajo del sub-proyecto D, y entonces esto se
#: podrá derivar de esa lista en vez de mantenerse a mano.
TIPOS_NO_COMERCIALES = frozenset({"recordatorio_cita"})
```

Y en `decidir`, **lo primero del cuerpo**, antes de leer `cita_estado`:

```python
    # G0. La baja comercial, antes que las siete. Es el orden que pidió MaxiCare por escrito:
    # privacidad -> canal -> criterio -> contacto.
    #
    # Va DENTRO de la bifurcación por tipo, no fuera: un recordatorio de cita atraviesa esta
    # guarda sin mirarla. Pedir que no te manden publicidad no es renunciar a que te avisen de
    # tu propia cita, y si se mezclan, el que pierde es el paciente que SÍ iba a ir.
    if fila.get("tipo") not in TIPOS_NO_COMERCIALES and fila.get("no_contactar"):
        return Decision("anular", "baja_solicitada")
```

Y en el docstring de `decidir`, añade una línea: que G0 va antes de las siete y por qué.

- [ ] **Step 5: Correr y verificar**

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
```

Esperado: todo verde.

```bash
uv run python scripts/probar_recordatorios.py
```

Esperado: OK. Este script dobla el despachador con firmas escritas a mano y `pytest -q` no lo
corre: la fila que alimenta a `decidir` gana una columna, así que es el que caza que el doble
se haya quedado atrás. No gasta tokens.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/persistencia.py src/maxicare_daniela/seguimientos.py tests/test_seguimientos.py tests/test_seguimientos_neon.py
git commit -m "feat: la baja apaga lo comercial y NUNCA el recordatorio de una cita

G0, antes de las siete guardas. Es el orden que MaxiCare pidio por escrito:
privacidad -> canal -> criterio -> contacto.

La lista es BLANCA a proposito. seguimientos.tipo es texto libre que escribe
el modelo, asi que una lista negra dejaria pasar cualquier tipo inventado
directo al envio. Con la blanca, lo que no esta en ella se comprueba contra
la baja.

Y el recordatorio de cita atraviesa la guarda sin mirarla: si se mezclaran,
alguien perderia el aviso de su cita por haber pedido que no le manden
publicidad, no llegaria, y el sistema habria hecho exactamente lo que se le
pidio.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Cierre: lo que hay que hacer a mano al terminar

No son tareas de código, pero el trabajo no está entregado sin ellas.

- [ ] **Correr la suite entera, las dos**, y `scripts/probar_atencion.py`,
      `scripts/probar_recordatorios.py`, `scripts/probar_tools.py` — los tres son gratis.
- [ ] **Actualizar el CLAUDE.md**: la baja que sobrevive a `/clearstate` y el aviso que se
      marca DESPUÉS del envío son exactamente el tipo de trampa que la lista de no negociables
      recoge. Sin eso, la próxima compactación se las lleva.
- [ ] **Mandarle a marketing el párrafo de la §11 de la spec.** El permiso quedó como aviso
      con derecho a oponerse, y eso se aparta de dos frases que ellos pusieron por escrito.
- [ ] **Pedir la URL definitiva de la política** en el dominio de MaxiCare, con copia
      congelada por versión, y poner `MAXICARE_POLITICA_DATOS_URL` en el `.env` del VPS. Hasta
      entonces el aviso no sale.
- [ ] **No desplegar sin eso.** El sistema funciona sin la URL, pero entonces no está
      registrando ningún aviso, que es la mitad del valor de este trabajo.

## Lo que este plan NO hace

Está en la spec, se repite aquí para que nadie lo busque en vano:

- Menores de edad. Fuera a propósito y por escrito.
- Botones interactivos de WhatsApp. **El canal no se toca en ninguna tarea.**
- La memoria del reencuentro (sub-proyecto C). Daniela sigue tratando como desconocida a quien
  vuelve pasadas 24 h.
- El barrido, la cadencia de 20 h / 7 días y las tres plantillas (sub-proyecto D). Incluido
  acotar `seguimientos.tipo` con `Literal` y CHECK, que aquí se esquiva con la lista blanca.

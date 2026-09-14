# Recordatorios de citas · plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir el proceso que la migración 001 llama «otro proceso» y que nunca existió: el que lee la cola de `seguimientos` y manda los recordatorios de las citas.

**Architecture:** Un despachador nuevo (`seguimientos.py`) colgado del bucle de barrido de `runtime.py`, que cada 60 s lee las filas vencidas con `FOR UPDATE SKIP LOCKED`, las pasa por siete guardas y las manda por WhatsApp. La programación deja de depender del modelo: la emiten `crear_cita`, `reprogramar_cita` y `cancelar_cita` desde el código, y una cascada las anula cuando la cita cambia.

**Tech Stack:** Python 3.12 · psycopg (síncrono, envuelto en `asyncio.to_thread`) · FastAPI · openai-agents 0.22.2 · Neon Postgres · WhatsApp Cloud API (Graph)

**Spec:** `docs/superpowers/specs/2026-09-14-recordatorios-de-citas-design.md`

## Global Constraints

- **Ninguna clave de idempotencia la escribe el modelo.** Se arman con `ctx.clave(...)`. Vale también para las claves que arme el despachador.
- **El reloj de una prueba nunca es el de la máquina.** Todo momento sale de `ctx.ahora` o de un parámetro `ahora: datetime`. Nunca `datetime.now()` dentro de la lógica.
- **`from agents import tool` está prohibido.** Si hiciera falta una tool nueva: `from agents.decorators import tool` o `from agents import function_tool`. Este plan no añade ninguna.
- **Lo que no se sabe se marca con el literal `"PENDIENTE"`.** Nunca con un valor plausible.
- **No se registran cédulas ni documentos de identidad.** Ninguna tabla de este plan lleva esa columna.
- **Las migraciones son idempotentes.** `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `ON CONFLICT DO NOTHING`.
- **Los scripts de `scripts/` empiezan con** `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` **y usan marcadores ASCII** (`OK` / `FALLA` / `->`). La consola de Windows es cp1252.
- **Para escribir un archivo nuevo, la herramienta Write; para modificar uno existente, Edit.** Los heredoc de Bash y el ciclo `Get-Content`/`Set-Content` de PowerShell rompen este repositorio.
- **Pruebas:** `uv run pytest -q` para todo; `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` para las que tocan la base. Las de Neon escriben en el esquema `pruebas` y lo borran al terminar.
- **La prueba de concurrencia exige la conexión directa a Neon** — quitarle el `-pooler.` al host. El pooler rechaza `options` como parámetro de arranque y no da dos sesiones de verdad.

---

## Cómo está cortado este plan

Cinco tareas, no nueve. El corte va donde un revisor podría rechazar una y aprobar la de al
lado; donde no podría, no hay corte.

Cuatro de las cinco tienen **dos mitades**, cada una con su propio ciclo de pruebas y su
propio commit. Eso es deliberado: la granularidad de los pasos no cambia —son los mismos 55,
con las mismas pruebas— pero un revisor no gana nada juzgando unas columnas de base de datos
sin las funciones que las escriben, ni una función de horarios sin las guardas que la usan.

Puedes parar y revisar al final de cada mitad; la puerta que importa está al final de cada
tarea. Si ejecutas por subagentes, despacha uno por tarea y deja que haga las dos mitades.

| Tarea | Qué demuestra al terminar | ¿Toca producción? |
|---|---|---|
| 1. La cola | la cola se puede leer, aplazar, anular y marcar | no |
| 2. Las reglas de decisión | decide bien las tres bandas y las siete guardas | no |
| 3. El despachador y su canal | manda una plantilla, y marca antes de mandarla | no |
| 4. Programar y la cascada | crear, mover y cancelar llevan su recordatorio detrás | **sí** — tres tools vivas |
| 5. A correr y demostrado | corre en el servidor y hay un script que lo enseña | **sí** — el arranque |

Las tres primeras se pueden hacer y fusionar sin que cambie nada de lo que hoy funciona. El
riesgo entra en la 4.

## File Structure

| Archivo | Responsabilidad |
|---|---|
| `migraciones/017_cola_de_recordatorios.sql` | **Crear.** Las cinco columnas de `seguimientos`, las dos de `conversaciones`, el índice parcial y las dos perillas. |
| `src/maxicare_daniela/seguimientos.py` | **Crear.** El despachador: cuándo toca un recordatorio (`momento_del_recordatorio`), si sale o no (`decidir`) y el bucle (`despachar`). Sin red propia: recibe los canales. |
| `src/maxicare_daniela/persistencia.py` | **Modificar.** Seis funciones nuevas de cola y cascada; `commit=False` en `registrar_cita` e `insertar_seguimiento`. |
| `src/maxicare_daniela/herramientas.py` | **Modificar.** Programar al crear, mover al reprogramar, anular al cancelar. |
| `src/maxicare_daniela/canales.py` | **Modificar.** `WhatsApp.enviar_plantilla`. |
| `src/maxicare_daniela/contratos.py` | **Modificar.** Dos campos de contexto para que Daniela sepa a qué dice «sí» el paciente. |
| `src/maxicare_daniela/atencion.py` | **Modificar.** Llenar esos dos campos desde la base. |
| `src/maxicare_daniela/config.py` | **Modificar.** El nombre de la plantilla de Meta. |
| `src/maxicare_daniela/runtime.py` | **Modificar.** La tarea de despacho, separada de la de relevos. |
| `tests/test_seguimientos.py` | **Crear.** Las bandas horarias y las siete guardas, sin base ni red. |
| `tests/test_seguimientos_neon.py` | **Crear.** Cola, cascada y concurrencia contra Neon (`-m neon`). |
| `scripts/probar_recordatorios.py` | **Crear.** El entregable de la fase, contra Neon, sin gastar tokens. |

---

## Task 1: La cola de recordatorios


**Files:**
- Create: `migraciones/017_cola_de_recordatorios.sql`
- Test: `tests/test_seguimientos_neon.py`

**Interfaces:**
- Consumes: nada.
- Produces: columnas `seguimientos.cita_id`, `.anulado_en`, `.motivo_anulacion`, `.intentos`, `.fallo`; columnas `conversaciones.ultimo_recordatorio_tipo`, `.ultimo_recordatorio_en`; claves de configuración `hora_recordatorio_vispera` (18) y `horas_minimas_para_recordar` (4).

- [ ] **Step 1: Escribir la prueba que falla**

Crear `tests/test_seguimientos_neon.py`:

```python
"""La cola de recordatorios contra Neon. Solo con `-m neon`."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA

pytestmark = pytest.mark.neon


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
```

El *fixture* `conexion_pruebas` ya existe en la suite de Neon. Si el nombre no coincide, cópialo de `tests/test_tools_neon.py` — **no inventes uno nuevo**, la suite comparte el montaje del esquema `pruebas` y su borrado al terminar.

- [ ] **Step 2: Correr la prueba para verificar que falla**

```
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_seguimientos_neon.py -v
```

Esperado: FALLA. `KeyError: 'hora_recordatorio_vispera'`, o el `assert` de columnas.

- [ ] **Step 3: Escribir la migración**

Crear `migraciones/017_cola_de_recordatorios.sql`:

```sql
-- =========================================================================================
-- La cola de recordatorios deja de ser una lista y pasa a ser una cola de verdad
--
-- `seguimientos` existe desde la 001, que ya la llamaba «la cola que lee otro proceso». Ese
-- proceso no existía: `enviado_en` no se escribía en ninguna línea del repositorio. Lo que
-- faltaba para poder escribirlo no era solo el proceso.
--
-- `cita_id` es lo que le faltaba a la tabla para poder decidir. Colgada solo de
-- `conversacion_id`, una fila no sabe de qué cita habla: el paciente reprograma, el
-- recordatorio sale igual, y le recuerda una cita que ya no existe. Es NULLABLE a propósito
-- --`programar_seguimiento` sigue pudiendo encolar «llámenme el lunes», que no cuelga de
-- ninguna cita-- y un seguimiento sin cita se salta las tres primeras guardas.
--
-- `anulado_en` + `motivo_anulacion` en vez de un DELETE: borrar deja al sistema sin poder
-- responder «¿por qué este paciente no recibió recordatorio?», que es la primera pregunta
-- que hace la clínica cuando alguien no llega.
--
-- `intentos` + `fallo`: la fila se marca ANTES de enviar (no hay transacción que cubra una
-- llamada a Meta), así que un fallo no la devuelve a la cola. Se reintenta dentro del mismo
-- ciclo y, si no sale, queda `enviado_en` puesto Y `fallo` con contenido -- que es la señal
-- a vigilar, igual que `reenviado_en` NULL en `mensajes_entrantes`.
--
-- Las dos columnas de `conversaciones` son para que Daniela sepa a qué dice «sí» un paciente
-- que responde a un recordatorio: el mensaje lo mandó un proceso, no una conversación, así
-- que el historial del agente no lo contiene.
--
-- Las perillas son enteros porque `leer_configuracion` hace `int(valor)` sobre todo lo que
-- encuentra. Un valor no entero se descarta en silencio y manda el default del código.
-- =========================================================================================

ALTER TABLE seguimientos
    ADD COLUMN IF NOT EXISTS cita_id          UUID REFERENCES citas(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS anulado_en       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS motivo_anulacion TEXT,
    ADD COLUMN IF NOT EXISTS intentos         SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS fallo            TEXT;

ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS ultimo_recordatorio_tipo TEXT,
    ADD COLUMN IF NOT EXISTS ultimo_recordatorio_en   TIMESTAMPTZ;

-- El de la 001 no conoce `anulado_en`: un seguimiento anulado seguiría entrando en el
-- barrido para que las guardas lo descartaran otra vez, cada sesenta segundos, para siempre.
CREATE INDEX IF NOT EXISTS ix_seguimientos_por_despachar ON seguimientos (fecha_objetivo)
    WHERE enviado_en IS NULL AND anulado_en IS NULL;

INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('hora_recordatorio_vispera', '18',
     'Hora a la que salen los recordatorios de las citas del dia siguiente. Entero, hora de Bogota.'),
    ('horas_minimas_para_recordar', '4',
     'Antelacion minima para que una cita reciba recordatorio. Por debajo, el paciente acaba de hablar con Daniela.')
ON CONFLICT (clave) DO NOTHING;
```

- [ ] **Step 4: Añadir las perillas a los defaults del código**

En `src/maxicare_daniela/persistencia.py`, dentro de `CONFIGURACION_POR_DEFECTO` (línea 64), después de `"atiende_domingo": 0,`:

```python
    # La cola de recordatorios (migración 017). Los defaults viven aquí por lo mismo que los
    # de la jornada: `leer_configuracion` los usa cuando la tabla todavía no existe.
    "hora_recordatorio_vispera": 18,
    "horas_minimas_para_recordar": 4,
```

- [ ] **Step 5: Correr la migración y la prueba**

```
uv run python scripts/inicializar_base.py
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_seguimientos_neon.py -v
```

Esperado: PASA. Si `inicializar_base.py` no recoge la 017 sola, comprueba que el nombre del archivo siga el patrón `NNN_nombre.sql` de las dieciséis anteriores — las descubre por orden alfabético.

- [ ] **Step 6: Verificar que no se rompió nada**

```
uv run pytest -q
```

Esperado: PASA. La 017 no cambia ninguna firma; si algo cae aquí, es por el cambio en `CONFIGURACION_POR_DEFECTO`.

- [ ] **Step 7: Commit**

```bash
git add migraciones/017_cola_de_recordatorios.sql src/maxicare_daniela/persistencia.py tests/test_seguimientos_neon.py
git commit -m "feat(017): la cola de seguimientos aprende de que cita habla

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

### Segunda mitad: las funciones que leen y escriben la cola


**Files:**
- Modify: `src/maxicare_daniela/persistencia.py:1514-1543` (`registrar_cita`), `:1706-1728` (`insertar_seguimiento`)
- Modify: `tests/test_seguimientos_neon.py`

**Interfaces:**
- Consumes: la migración 017 (Task 1).
- Produces, todas en `persistencia`:
  ```python
  def insertar_seguimiento(conn, *, id_conversacion: str, tipo: str, fecha_objetivo: datetime,
                           clave_idempotencia: str, cita_id: str | None = None,
                           commit: bool = True) -> bool
  def registrar_cita(conn, *, ..., commit: bool = True) -> str          # firma existente + commit
  def anular_seguimientos_de_cita(conn, cita_id: str, *, motivo: str,
                                  commit: bool = True) -> int
  def seguimientos_por_despachar(conn, *, ahora: datetime, limite: int = 50) -> list[dict[str, Any]]
  def marcar_seguimiento_enviado(conn, id_seguimiento: int) -> None
  def aplazar_seguimiento(conn, id_seguimiento: int, *, hasta: datetime) -> None
  def anular_seguimiento(conn, id_seguimiento: int, *, motivo: str) -> None
  def anotar_fallo_de_seguimiento(conn, id_seguimiento: int, *, fallo: str) -> None
  def ultimo_mensaje_del_paciente(conn, telefono: str) -> datetime | None
  def anotar_recordatorio_en_conversacion(conn, id_conversacion: str, *, tipo: str,
                                          cuando: datetime) -> None
  ```
  Las claves de cada `dict` de `seguimientos_por_despachar`: `id`, `conversacion_id`, `cita_id`, `tipo`, `fecha_objetivo`, `intentos`, `telefono`, `nombre_completo`, `tratamiento`, `cita_inicio`, `cita_estado`, `tomada_por`. **`tratamiento` está porque es uno de los cuatro huecos de la plantilla** (Task 6): sin él, `_parametros_del_recordatorio` manda «su cita» donde el paciente espera leer qué le van a hacer.

- [ ] **Step 8: Escribir las pruebas que fallan**

Añadir a `tests/test_seguimientos_neon.py`:

```python
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
```

Añade también el *fixture* `cita_de_prueba` al mismo archivo, que crea una conversación y una cita en el esquema `pruebas` y devuelve `(id_cita, id_conversacion)`. Cópialo del montaje que ya usa `tests/test_tools_neon.py` para crear citas — **no escribas SQL nuevo si allí ya hay un ayudante que lo hace**.

- [ ] **Step 9: Correr para verificar que falla**

```
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_seguimientos_neon.py -v
```

Esperado: FALLA con `AttributeError: module 'maxicare_daniela.persistencia' has no attribute 'anular_seguimientos_de_cita'`.

- [ ] **Step 10: Añadir `commit=False` a las dos funciones que ya existen**

En `persistencia.py`, `insertar_seguimiento` (línea 1706) pasa a:

```python
def insertar_seguimiento(
    conn,
    *,
    id_conversacion: str,
    tipo: str,
    fecha_objetivo: datetime,
    clave_idempotencia: str,
    cita_id: str | None = None,
    commit: bool = True,
) -> bool:
    """`True` si quedó programado ahora, `False` si ya existía. Nunca duplica.

    `cita_id` es NULLABLE a propósito: `programar_seguimiento` sigue pudiendo encolar algo que
    no cuelga de ninguna cita («llámenme el lunes»). Un seguimiento sin cita se salta las tres
    primeras guardas del despachador.

    `commit=False` es lo que permite que la cita y su recordatorio nazcan en UNA transacción.
    Quien lo use se queda a cargo del `commit`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seguimientos
                (conversacion_id, tipo, fecha_objetivo, clave_idempotencia, cita_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (clave_idempotencia) DO NOTHING
            RETURNING id
            """,
            (id_conversacion, tipo, fecha_objetivo, clave_idempotencia, cita_id),
        )
        nuevo = cur.fetchone() is not None
    if commit:
        conn.commit()
    return nuevo
```

En `registrar_cita` (línea 1514), añade `commit: bool = True` al final de los parámetros y cambia el `conn.commit()` de la línea 1542 por:

```python
    if commit:
        conn.commit()
    return id_cita
```

- [ ] **Step 11: Añadir las funciones nuevas**

Al final de la sección «Estado, seguimientos y escalamientos» de `persistencia.py`, después de `insertar_seguimiento`:

```python
def anular_seguimientos_de_cita(
    conn, cita_id: str, *, motivo: str, commit: bool = True
) -> int:
    """Anula los seguimientos vivos de esa cita y devuelve cuántos. Idempotente.

    No borra: un seguimiento anulado con su motivo es lo que permite responder «¿por qué este
    paciente no recibió recordatorio?», que es la primera pregunta que hace la clínica cuando
    alguien no llega.

    `commit=False` para que la anulación viaje en la MISMA transacción que el cambio de la
    cita. Separadas, una caída entre las dos deja un recordatorio vivo apuntando a una cita
    muerta -- y el despachador lo mandaría, porque sus guardas solo miran lo que hay en la base.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE seguimientos SET anulado_en = now(), motivo_anulacion = %s
             WHERE cita_id = %s AND enviado_en IS NULL AND anulado_en IS NULL
            """,
            (motivo, cita_id),
        )
        anulados = cur.rowcount
    if commit:
        conn.commit()
    return anulados


def seguimientos_por_despachar(
    conn, *, ahora: datetime, limite: int = 50
) -> list[dict[str, Any]]:
    """Las filas vencidas, con TODO lo que el despachador necesita para decidir.

    El `LEFT JOIN` a `citas` y a `conversaciones` no es una optimización: sin él, cada guarda
    sería una consulta más por fila, y el barrido de las 6 p. m. --que es cuando salen todos
    los recordatorios del día a la vez-- haría cientos de viajes a Neon.

    `FOR UPDATE ... SKIP LOCKED` es lo que permite que dos instancias no manden el mismo
    recordatorio dos veces. `OF s` porque el bloqueo va sobre la cola, no sobre las citas.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.conversacion_id, s.cita_id, s.tipo, s.fecha_objetivo, s.intentos,
                   COALESCE(c.telefono, cv.telefono)      AS telefono,
                   c.nombre_completo, c.tratamiento, c.inicio AS cita_inicio,
                   c.estado AS cita_estado, cv.tomada_por
              FROM seguimientos s
              LEFT JOIN citas c          ON c.id  = s.cita_id
              LEFT JOIN conversaciones cv ON cv.id = s.conversacion_id
             WHERE s.enviado_en IS NULL
               AND s.anulado_en IS NULL
               AND s.fecha_objetivo <= %s
             ORDER BY s.fecha_objetivo
             LIMIT %s
               FOR UPDATE OF s SKIP LOCKED
            """,
            (ahora, limite),
        )
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def marcar_seguimiento_enviado(conn, id_seguimiento: int) -> None:
    """Va ANTES del envío y con su propio commit, y el orden es deliberado.

    No hay transacción que cubra una llamada HTTP a Meta. Si se enviara primero y el proceso
    muriera antes del commit, la fila seguiría pendiente y el barrido de sesenta segundos
    después mandaría el mismo recordatorio otra vez -- sin que ninguna de las siete guardas lo
    detectara, porque todas seguirían diciendo que sí.

    Antes marcar y no mandar, que mandar y no marcar.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = now() WHERE id = %s", (id_seguimiento,)
        )
    conn.commit()


def aplazar_seguimiento(conn, id_seguimiento: int, *, hasta: datetime) -> None:
    """Lo mueve en el tiempo sin gastarlo. Es lo que hacen las guardas del relevo y del horario:
    el motivo por el que no sale ahora deja de ser cierto más tarde."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET fecha_objetivo = %s WHERE id = %s",
            (hasta, id_seguimiento),
        )
    conn.commit()


def anular_seguimiento(conn, id_seguimiento: int, *, motivo: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET anulado_en = now(), motivo_anulacion = %s WHERE id = %s",
            (motivo[:200], id_seguimiento),
        )
    conn.commit()


def anotar_fallo_de_seguimiento(conn, id_seguimiento: int, *, fallo: str) -> None:
    """`enviado_en` puesto Y `fallo` con contenido es la señal a vigilar: la fila dice «lo
    intenté» y no «salió». Es el mismo par que `reenviado_en` NULL en `mensajes_entrantes`.

    El motivo se trunca a 2000, igual que `marcar_fallo_respuesta`: un traceback entero no
    tiene por qué ocupar la base de la clínica.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET fallo = %s, intentos = intentos + 1 WHERE id = %s",
            (fallo[:2000], id_seguimiento),
        )
    conn.commit()


def ultimo_mensaje_del_paciente(conn, telefono: str) -> datetime | None:
    """Cuándo escribió ese número por última vez, o `None` si nunca.

    Es la fuente exacta de dos cosas distintas: la guarda de contacto reciente --si está
    hablando con Daniela ahora, recordarle la cita la hace ver desmemoriada-- y la ventana de
    24 h de Meta, que se cuenta desde aquí y no desde `conversaciones.actualizada_en`, que
    también la toca Daniela al responder.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(recibido_en) FROM mensajes_entrantes WHERE telefono = %s",
            (telefono,),
        )
        fila = cur.fetchone()
    return fila[0] if fila else None


def anotar_recordatorio_en_conversacion(
    conn, id_conversacion: str, *, tipo: str, cuando: datetime
) -> None:
    """Para que Daniela sepa a qué dice «sí» el paciente que responde a un recordatorio.

    El mensaje lo mandó un proceso, no una conversación: el historial del agente no lo
    contiene. Se anota aquí y `atencion._leer_estado` lo carga en el contexto. NO se inyecta
    un mensaje en `agent_messages`: esas tablas las fija el SDK.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET ultimo_recordatorio_tipo = %s, ultimo_recordatorio_en = %s
             WHERE id = %s
            """,
            (tipo, cuando, id_conversacion),
        )
    conn.commit()
```

- [ ] **Step 12: Correr las pruebas de Neon**

```
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_seguimientos_neon.py -v
```

Esperado: PASA.

- [ ] **Step 13: Correr la suite completa y los scripts que doblan firmas**

`registrar_cita` e `insertar_seguimiento` cambiaron de firma, y `scripts/probar_tools.py` las dobla a mano. **La suite en verde no lo caza.**

```
uv run pytest -q
uv run python scripts/probar_tools.py
uv run python scripts/probar_relevo.py
uv run python scripts/probar_calendario.py
```

Esperado: los cuatro en verde. Los parámetros nuevos tienen default, así que no debería romperse nada; si algo cae, es un llamador posicional.

- [ ] **Step 14: Commit**

```bash
git add src/maxicare_daniela/persistencia.py tests/test_seguimientos_neon.py
git commit -m "feat: la cola de recordatorios se puede leer, aplazar, anular y marcar

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Las reglas de decisión


La función pura de la sección 2 de la spec. Sin base, sin red, sin reloj de la máquina.

**Files:**
- Create: `src/maxicare_daniela/seguimientos.py`
- Create: `tests/test_seguimientos.py`

**Interfaces:**
- Consumes: `calendario.Jornada` (ya existe: `apertura`, `cierre`, `cierre_sabado`, `atiende_domingo`, `cierre_de(momento) -> int | None`, `cabe(inicio, duracion) -> bool`).
- Produces:
  ```python
  def momento_del_recordatorio(
      *,
      inicio_cita: datetime,
      ahora: datetime,
      jornada: Jornada,
      hora_vispera: int = 18,
      horas_minimas: int = 4,
  ) -> datetime | None
  ```
  `None` significa «esta cita no lleva recordatorio». Lo usan las Tasks 4 y 5.

- [ ] **Step 1: Escribir las pruebas que fallan**

Crear `tests/test_seguimientos.py`:

```python
"""El despachador de recordatorios, sin base y sin red.

Las siete guardas y las bandas horarias viven aquí porque son decisiones del código, no de
la base: se prueban en milisegundos y corren siempre. Lo que toca Neon está en
`test_seguimientos_neon.py`, marcado `neon`.

Ningún momento sale del reloj de la máquina: todos entran como parámetro.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada

ZONA = datetime(2026, 9, 15, 9, 0).tzinfo  # se reemplaza abajo
from maxicare_daniela.herramientas import ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sábado, domingo cerrado


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Septiembre de 2026: el 15 es martes, el 19 sábado, el 20 domingo."""
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA_BOGOTA)


def test_una_cita_a_menos_de_cuatro_horas_no_lleva_recordatorio():
    # Escribe a las 9:00 y agenda para las 11:00 del mismo día: acaba de hablar con Daniela.
    assert (
        s.momento_del_recordatorio(
            inicio_cita=momento(15, 11),
            ahora=momento(15, 9),
            jornada=JORNADA,
        )
        is None
    )


def test_entre_cuatro_y_veinticuatro_horas_sale_dos_horas_antes():
    assert s.momento_del_recordatorio(
        inicio_cita=momento(15, 15),
        ahora=momento(15, 9),
        jornada=JORNADA,
    ) == momento(15, 13)


def test_a_mas_de_un_dia_sale_la_vispera_a_las_seis():
    assert s.momento_del_recordatorio(
        inicio_cita=momento(17, 9),
        ahora=momento(15, 9),
        jornada=JORNADA,
    ) == momento(16, 18)


def test_las_dos_horas_antes_que_caen_de_madrugada_se_adelantan_a_la_vispera():
    # Cita a las 8:00 del miércoles, agendada el martes a las 10:00: 22 h de antelación, cae
    # en la banda de las 2 h, y «2 h antes» son las 6:00 a. m. con la clínica cerrada.
    # Aplazarlo a la apertura lo dejaría llegando a las 8:00, la hora de la cita.
    assert s.momento_del_recordatorio(
        inicio_cita=momento(16, 8),
        ahora=momento(15, 10),
        jornada=JORNADA,
    ) == momento(15, 17)  # la víspera, a la hora de cierre


def test_la_vispera_de_un_lunes_es_el_domingo_y_la_clinica_cierra():
    # Cita el lunes 21 a las 9:00. La víspera es domingo: la clínica no abre, así que el
    # recordatorio se adelanta al sábado a la hora de cierre.
    assert s.momento_del_recordatorio(
        inicio_cita=momento(21, 9),
        ahora=momento(17, 9),
        jornada=JORNADA,
    ) == momento(19, 15)  # sábado, cierre de sábado


def test_un_momento_que_ya_paso_no_se_programa():
    # Si el cálculo cae antes de `ahora`, no hay recordatorio que valga.
    assert (
        s.momento_del_recordatorio(
            inicio_cita=momento(15, 10),
            ahora=momento(15, 9, 30),
            jornada=JORNADA,
        )
        is None
    )
```

Borra la línea `ZONA = datetime(...)` al escribir el archivo: está solo para que veas que el import de `ZONA_BOGOTA` viene de `herramientas`. Deja únicamente `from maxicare_daniela.herramientas import ZONA_BOGOTA`.

- [ ] **Step 2: Correr para verificar que falla**

```
uv run pytest -q tests/test_seguimientos.py -v
```

Esperado: FALLA con `ModuleNotFoundError: No module named 'maxicare_daniela.seguimientos'`.

- [ ] **Step 3: Escribir el módulo con la función**

Crear `src/maxicare_daniela/seguimientos.py`:

```python
"""El despachador de recordatorios: el «otro proceso» de la migración 001.

`seguimientos` es una cola desde la fase 3 y hasta hoy nadie la leía: `enviado_en` no se
escribía en ninguna línea del repositorio. Este módulo es quien la lee.

Deliberadamente NO abre conexiones ni habla con Meta por su cuenta: recibe la conexión y los
canales. Es lo que permite probar las siete guardas en milisegundos y sin señal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from .calendario import Jornada

log = logging.getLogger(__name__)

#: Una cita a menos de esto no lleva recordatorio: el paciente acaba de hablar con Daniela.
#: El valor vivo sale de `configuracion`; este es el respaldo.
HORAS_MINIMAS_POR_DEFECTO = 4

#: La hora a la que salen los recordatorios de las citas del día siguiente.
HORA_VISPERA_POR_DEFECTO = 18

#: Cuánta antelación da la banda corta.
HORAS_ANTES_EN_EL_MISMO_DIA = 2


def _vispera_util(dia: datetime, jornada: Jornada, hora_preferida: int) -> datetime | None:
    """El último momento útil en o antes de `dia` en que la clínica está abierta.

    Retrocede día a día mientras la clínica esté cerrada --el domingo es el caso normal, y la
    víspera de un lunes SIEMPRE lo es-- y nunca más de una semana: si en siete días no hay un
    día abierto, la configuración está rota y devolver algo sería inventarse un horario.
    """
    candidato = dia
    for _ in range(7):
        cierre = jornada.cierre_de(candidato)
        if cierre is not None:
            hora = min(hora_preferida, cierre)
            if hora >= jornada.apertura:
                return candidato.replace(hour=hora, minute=0, second=0, microsecond=0)
        candidato = (candidato - timedelta(days=1)).replace(hour=12)
    return None


def momento_del_recordatorio(
    *,
    inicio_cita: datetime,
    ahora: datetime,
    jornada: Jornada,
    hora_vispera: int = HORA_VISPERA_POR_DEFECTO,
    horas_minimas: int = HORAS_MINIMAS_POR_DEFECTO,
) -> datetime | None:
    """Cuándo debe salir el recordatorio de esa cita, o `None` si no lleva.

    Tres bandas, por antelación con que se agendó:

    - menos de `horas_minimas`: ninguno. El paciente acaba de hablar con Daniela y recordarle
      la cita que acaba de agendar la hace ver desmemoriada.
    - hasta 24 h: dos horas antes, que es tiempo de salir de casa.
    - más de 24 h: la víspera a `hora_vispera`. Hora FIJA y no «24 h antes»: con 24 h exactas,
      la cita de las 7 a. m. dispara su recordatorio a las 7 a. m. del día anterior, hora a la
      que mucha gente no mira el teléfono. Con hora fija salen todos juntos y quien quiera
      cambiar la cita alcanza a avisar esa noche, para que la clínica libere el cupo.

    La excepción de las madrugadas: si «dos horas antes» cae antes de la apertura, el
    recordatorio se ADELANTA a la víspera en vez de retrasarse a la apertura. Retrasarlo lo
    dejaría llegando a la hora de la cita, con el paciente ya en la puerta o ya perdido. Es el
    único caso en que un recordatorio se mueve hacia atrás en el tiempo.
    """
    antelacion = inicio_cita - ahora
    if antelacion < timedelta(hours=horas_minimas):
        return None

    if antelacion <= timedelta(hours=24):
        candidato = inicio_cita - timedelta(hours=HORAS_ANTES_EN_EL_MISMO_DIA)
        if candidato.hour < jornada.apertura:
            candidato = _vispera_util(
                inicio_cita - timedelta(days=1), jornada, hora_vispera
            )
    else:
        candidato = _vispera_util(inicio_cita - timedelta(days=1), jornada, hora_vispera)

    if candidato is None or candidato <= ahora:
        return None
    return candidato
```

- [ ] **Step 4: Correr para verificar que pasa**

```
uv run pytest -q tests/test_seguimientos.py -v
```

Esperado: PASA, las seis.

Si `test_las_dos_horas_antes_que_caen_de_madrugada_se_adelantan_a_la_vispera` espera `momento(15, 17)` y obtiene otra hora, mira `_vispera_util`: `min(hora_preferida, cierre)` es lo que hace que las 18:00 pedidas se conviertan en las 17:00 reales de cierre entre semana.

- [ ] **Step 5: Commit**

```bash
git add src/maxicare_daniela/seguimientos.py tests/test_seguimientos.py
git commit -m "feat: cuando toca un recordatorio -- tres bandas y la excepcion de la madrugada

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

### Segunda mitad: las siete guardas


El corazón del despachador, y todo sin base ni red: `decidir` es una función pura que recibe una fila y devuelve qué hacer con ella.

**Files:**
- Modify: `src/maxicare_daniela/seguimientos.py`
- Modify: `tests/test_seguimientos.py`

**Interfaces:**
- Consumes: `momento_del_recordatorio` (Task 2), las claves del dict de `seguimientos_por_despachar` (Task 3).
- Produces:
  ```python
  @dataclass(frozen=True)
  class Decision:
      accion: Literal["enviar", "anular", "aplazar"]
      motivo: str
      hasta: datetime | None = None

  def decidir(fila: dict[str, Any], *, ahora: datetime, jornada: Jornada,
              ultimo_mensaje: datetime | None,
              ya_salio_a_ese_numero: bool = False) -> Decision
  ```

- [ ] **Step 6: Escribir las pruebas que fallan**

Añadir a `tests/test_seguimientos.py`:

```python
def fila(**cambios) -> dict:
    base = dict(
        id=1,
        conversacion_id="conv-1",
        cita_id="cita-1",
        tipo="recordatorio_cita",
        fecha_objetivo=momento(16, 18),
        intentos=0,
        telefono="573001112233",
        nombre_completo="Ana Gómez",
        cita_inicio=momento(17, 9),
        cita_estado="confirmada",
        tomada_por=None,
    )
    base.update(cambios)
    return base


def test_g1_una_cita_cancelada_no_recibe_recordatorio():
    d = s.decidir(
        fila(cita_estado="cancelada"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_cambio"


def test_g2_una_cita_que_ya_paso_no_recibe_recordatorio():
    d = s.decidir(
        fila(cita_inicio=momento(16, 9)),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_pasada"


def test_g3_un_recordatorio_con_mas_de_dos_horas_de_retraso_se_descarta():
    # El proceso estuvo caído: le tocaba a las 18:00 y son las 21:00.
    d = s.decidir(
        fila(),
        ahora=momento(16, 21),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "llego_tarde"


def test_g4_con_el_relevo_puesto_se_aplaza_y_no_se_anula():
    # El doctor puede devolver la conversación en diez minutos: el recordatorio sigue siendo
    # válido. Anularlo aquí lo perdería para siempre.
    d = s.decidir(
        fila(tomada_por="Dr. Pérez"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "aplazar"
    assert d.hasta == momento(16, 18, 30)


def test_g5_fuera_de_la_jornada_se_aplaza_a_la_apertura():
    d = s.decidir(
        fila(fecha_objetivo=momento(16, 6), cita_inicio=momento(17, 9)),
        ahora=momento(16, 6),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "aplazar"
    assert d.hasta == momento(16, 8)


def test_g6_si_el_paciente_acaba_de_escribir_no_se_le_recuerda_nada():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(16, 17, 40),
    )
    assert d.accion == "anular"
    assert d.motivo == "contacto_reciente"


def test_g7_no_se_manda_un_segundo_mensaje_al_mismo_numero():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
        ya_salio_a_ese_numero=True,
    )
    assert d.accion == "aplazar"
    assert d.motivo == "agrupado"


def test_un_recordatorio_limpio_sale():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(14, 9),
    )
    assert d.accion == "enviar"


def test_un_seguimiento_sin_cita_se_salta_las_tres_primeras_guardas():
    # «Llámenme el lunes»: no cuelga de ninguna cita, así que G1, G2 y G3 no aplican.
    d = s.decidir(
        fila(cita_id=None, cita_inicio=None, cita_estado=None, tipo="reactivacion"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "enviar"
```

- [ ] **Step 7: Correr para verificar que falla**

```
uv run pytest -q tests/test_seguimientos.py -v
```

Esperado: FALLA con `AttributeError: module 'maxicare_daniela.seguimientos' has no attribute 'decidir'`.

- [ ] **Step 8: Implementar `decidir`**

Añadir a `src/maxicare_daniela/seguimientos.py`:

```python
#: Cuánto se aplaza un recordatorio que pilló al doctor hablando con el paciente.
MINUTOS_DE_ESPERA_POR_RELEVO = 30

#: A partir de cuánto retraso un recordatorio deja de servir y pasa a estorbar.
HORAS_DE_RETRASO_QUE_LO_INVALIDAN = 2

#: Si el paciente escribió hace menos de esto, ya está hablando con Daniela.
MINUTOS_DE_CONTACTO_RECIENTE = 60


@dataclass(frozen=True)
class Decision:
    """Qué hacer con una fila de la cola. `hasta` solo tiene valor si la acción es aplazar."""

    accion: Literal["enviar", "anular", "aplazar"]
    motivo: str
    hasta: datetime | None = None


def decidir(
    fila: dict[str, Any],
    *,
    ahora: datetime,
    jornada: Jornada,
    ultimo_mensaje: datetime | None,
    ya_salio_a_ese_numero: bool = False,
) -> Decision:
    """Las siete guardas, en orden. Es lo que separa un recordatorio de un buzón de spam.

    El orden importa: las tres primeras son sobre la cita y se saltan si el seguimiento no
    cuelga de ninguna; las dos siguientes aplazan en vez de anular, porque su motivo deja de
    ser cierto más tarde; las dos últimas anulan o agrupan.
    """
    cita_estado = fila.get("cita_estado")
    cita_inicio = fila.get("cita_inicio")

    if fila.get("cita_id") is not None:
        # G1. Cancelada o movida: el recordatorio habla de algo que ya no existe. `reprogramada`
        # no basta para anular --la cascada ya anuló el viejo y creó otro-- pero `cancelada` sí,
        # y una cita que desapareció de la fila también.
        if cita_estado is None or cita_estado == "cancelada":
            return Decision("anular", "cita_cambio")

        # G2. Recordar una cita que ya pasó no es tarde: es decirle al paciente que el sistema
        # no sabe lo que pasó.
        if cita_inicio is not None and cita_inicio <= ahora:
            return Decision("anular", "cita_pasada")

        # G3. El proceso estuvo caído. Sin esto, arrancarlo tras un fin de semana manda de golpe
        # todos los recordatorios atrasados.
        if ahora - fila["fecha_objetivo"] > timedelta(hours=HORAS_DE_RETRASO_QUE_LO_INVALIDAN):
            return Decision("anular", "llego_tarde")

    # G4. Mientras un doctor tiene el relevo, el sistema no se le atraviesa: podría estar
    # acordando otra fecha en ese mismo momento. Aplaza, NO anula.
    if fila.get("tomada_por"):
        return Decision(
            "aplazar",
            "relevo_activo",
            ahora + timedelta(minutes=MINUTOS_DE_ESPERA_POR_RELEVO),
        )

    # G5. Nada a las tres de la mañana. La jornada sale de `configuracion`, no de una constante
    # nueva: duplicarla deja dos horarios que se contradicen.
    cierre = jornada.cierre_de(ahora)
    if cierre is None or ahora.hour < jornada.apertura or ahora.hour >= cierre:
        return Decision("aplazar", "fuera_de_jornada", _proxima_apertura(ahora, jornada))

    # G6. Si está hablando con Daniela ahora mismo, recordarle la cita que acaba de agendar la
    # hace ver desmemoriada.
    if (
        ultimo_mensaje is not None
        and ahora - ultimo_mensaje < timedelta(minutes=MINUTOS_DE_CONTACTO_RECIENTE)
    ):
        return Decision("anular", "contacto_reciente")

    # G7. Un paciente con dos citas la misma semana recibe UN mensaje, no dos. Se aplaza al
    # siguiente ciclo, donde el agrupador lo recogerá junto al otro.
    if ya_salio_a_ese_numero:
        return Decision("aplazar", "agrupado", ahora + timedelta(minutes=1))

    return Decision("enviar", "ok")


def _proxima_apertura(ahora: datetime, jornada: Jornada) -> datetime:
    """El próximo momento en que la clínica está abierta, desde `ahora`.

    Avanza día a día como mucho una semana: si en siete días no abre, la configuración está
    rota y devolver un momento cualquiera sería inventarse un horario. En ese caso devuelve
    mañana a la hora de apertura, y la guarda volverá a aplazarlo -- lo que deja el problema
    visible en la tabla en vez de escondido en un bucle.
    """
    cierre_de_hoy = jornada.cierre_de(ahora)
    if cierre_de_hoy is not None and ahora.hour < jornada.apertura:
        return ahora.replace(hour=jornada.apertura, minute=0, second=0, microsecond=0)

    candidato = ahora
    for _ in range(7):
        candidato = (candidato + timedelta(days=1)).replace(
            hour=jornada.apertura, minute=0, second=0, microsecond=0
        )
        if jornada.cierre_de(candidato) is not None:
            return candidato
    return (ahora + timedelta(days=1)).replace(
        hour=jornada.apertura, minute=0, second=0, microsecond=0
    )
```

- [ ] **Step 9: Correr para verificar que pasa**

```
uv run pytest -q tests/test_seguimientos.py -v
```

Esperado: PASA, las quince (seis de la Task 2 más nueve de esta).

- [ ] **Step 10: Commit**

```bash
git add src/maxicare_daniela/seguimientos.py tests/test_seguimientos.py
git commit -m "feat: las siete guardas -- dos aplazan, cuatro anulan, una agrupa

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: El despachador y su canal


**Files:**
- Modify: `src/maxicare_daniela/canales.py:147-167` (junto a `enviar_texto`)
- Modify: `src/maxicare_daniela/config.py:400` (junto a `telegram_chat_doctores`)
- Modify: `tests/test_seguimientos.py`

**Interfaces:**
- Consumes: `canales.WhatsApp.__init__(token, phone_number_id)`, `canales.ErrorDeCanal`.
- Produces:
  ```python
  async def WhatsApp.enviar_plantilla(self, telefono: str, *, plantilla: str,
                                      parametros: list[str], idioma: str = "es") -> str
  config.plantilla_recordatorio: str   # "" si no está configurada
  ```

- [ ] **Step 1: Escribir la prueba que falla**

Añadir a `tests/test_seguimientos.py`:

```python
class _RespuestaFalsa:
    status_code = 200

    def json(self):
        return {"messages": [{"id": "wamid.PRUEBA"}]}


def test_la_plantilla_viaja_con_el_tipo_y_los_parametros_que_meta_espera(monkeypatch):
    """Meta rechaza el envío entero si el cuerpo no lleva `type: template` con su `language`.

    Se comprueba la FORMA del cuerpo y no solo que no lance: un cuerpo mal armado devuelve 200
    en algunos casos y el mensaje no llega nunca.
    """
    import asyncio

    from maxicare_daniela import canales

    enviados = {}

    class _ClienteFalso:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            enviados["url"] = url
            enviados["cuerpo"] = json
            return _RespuestaFalsa()

    monkeypatch.setattr(canales.httpx, "AsyncClient", _ClienteFalso)

    wa = canales.WhatsApp("token-falso", "phone-id-falso")
    wamid = asyncio.run(
        wa.enviar_plantilla(
            "573001112233",
            plantilla="recordatorio_cita",
            parametros=["Ana", "miércoles 17/9", "09:00", "Limpieza"],
        )
    )

    assert wamid == "wamid.PRUEBA"
    cuerpo = enviados["cuerpo"]
    assert cuerpo["type"] == "template"
    assert cuerpo["template"]["name"] == "recordatorio_cita"
    assert cuerpo["template"]["language"] == {"code": "es"}
    valores = [p["text"] for p in cuerpo["template"]["components"][0]["parameters"]]
    assert valores == ["Ana", "miércoles 17/9", "09:00", "Limpieza"]
```

- [ ] **Step 2: Correr para verificar que falla**

```
uv run pytest -q tests/test_seguimientos.py::test_la_plantilla_viaja_con_el_tipo_y_los_parametros_que_meta_espera -v
```

Esperado: FALLA con `AttributeError: 'WhatsApp' object has no attribute 'enviar_plantilla'`.

- [ ] **Step 3: Implementar el método**

En `src/maxicare_daniela/canales.py`, justo después de `enviar_texto` (línea 167):

```python
    async def enviar_plantilla(
        self,
        telefono: str,
        *,
        plantilla: str,
        parametros: list[str],
        idioma: str = "es",
    ) -> str:
        """Manda una plantilla aprobada y devuelve el wamid.

        Es la única forma de escribirle a alguien FUERA de la ventana de 24 h, que se cuenta
        desde el último mensaje del paciente. Un recordatorio la víspera cae fuera de esa
        ventana casi siempre, así que `enviar_texto` no sirve aquí: Meta lo rechaza.

        Los parámetros van posicionales, en el orden en que aparecen los `{{1}}`, `{{2}}`... del
        texto que Meta aprobó. Cambiar el orden aquí no cambia la plantilla: manda otro dato en
        otro hueco, y el paciente lee una hora donde esperaba un nombre.
        """
        cuerpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "template",
            "template": {
                "name": plantilla,
                "language": {"code": idioma},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": v} for v in parametros],
                    }
                ],
            },
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(
                f"{BASE_GRAPH}/{self._phone_number_id}/messages",
                headers=self._cabeceras,
                json=cuerpo,
            )
        if r.status_code != 200:
            raise ErrorDeCanal(
                f"WhatsApp rechazó la plantilla '{plantilla}': {r.status_code} {r.text[:300]}"
            )
        return r.json()["messages"][0]["id"]
```

- [ ] **Step 4: Añadir la clave de configuración**

En `src/maxicare_daniela/config.py`, junto a `telegram_chat_doctores` (línea 400), añade al mismo constructor:

```python
            plantilla_recordatorio=_opcional("MAXICARE_PLANTILLA_RECORDATORIO"),
```

y declara el campo en la dataclass de configuración, con el resto de los opcionales:

```python
    #: El nombre EXACTO de la plantilla aprobada en el Business Manager de Meta. Vacía
    #: --su default-- apaga el envío: el despachador decide igual, anota lo que habría hecho y
    #: no manda nada. Es lo que permite comprobar en producción que decide bien antes de que
    #: mande un solo mensaje. PENDIENTE: el nombre real, que sale de la aprobación de Meta.
    plantilla_recordatorio: str = ""
```

Añade también la línea a `.env.ejemplo`:

```
MAXICARE_PLANTILLA_RECORDATORIO=
```

- [ ] **Step 5: Correr las pruebas**

```
uv run pytest -q
```

Esperado: PASA. `tests/test_config.py` comprueba que `.env.ejemplo` y la configuración no divergen; si cae ahí, falta la línea del `.env.ejemplo`.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/canales.py src/maxicare_daniela/config.py .env.ejemplo tests/test_seguimientos.py
git commit -m "feat: enviar_plantilla -- la unica puerta fuera de la ventana de 24h

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

### Segunda mitad: el bucle que las usa


Une la cola (Task 3), las guardas (Task 4) y el envío (Task 5).

**Files:**
- Modify: `src/maxicare_daniela/seguimientos.py`
- Modify: `tests/test_seguimientos.py`

**Interfaces:**
- Consumes: `decidir`, `persistencia.seguimientos_por_despachar`, `.marcar_seguimiento_enviado`, `.aplazar_seguimiento`, `.anular_seguimiento`, `.anotar_fallo_de_seguimiento`, `.ultimo_mensaje_del_paciente`, `.anotar_recordatorio_en_conversacion`, `WhatsApp.enviar_plantilla`.
- Produces:
  ```python
  async def despachar(*, database_url: str, whatsapp: Any | None, jornada: Jornada,
                      plantilla: str, ahora: datetime | None = None,
                      limite: int = 50) -> dict[str, int]
  ```
  Devuelve el recuento por acción: `{"enviados": n, "anulados": n, "aplazados": n, "fallidos": n}`.

- [ ] **Step 7: Escribir la prueba que falla**

Añadir a `tests/test_seguimientos.py`:

```python
def test_el_despachador_marca_antes_de_enviar(monkeypatch):
    """El orden es la defensa contra el duplicado, y es lo contrario del instinto.

    No hay transacción que cubra una llamada a Meta. Si se envía primero y el proceso muere
    antes del commit, la fila sigue pendiente y el barrido de sesenta segundos después manda el
    mismo recordatorio otra vez, sin que ninguna guarda lo detecte.
    """
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    orden: list[str] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia,
        "marcar_seguimiento_enviado",
        lambda conn, id_seguimiento: orden.append("marcar"),
    )
    monkeypatch.setattr(
        persistencia,
        "anotar_recordatorio_en_conversacion",
        lambda conn, id_conversacion, **k: None,
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            orden.append("enviar")
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 18),
        )
    )

    assert orden == ["marcar", "enviar"]
    assert recuento["enviados"] == 1


def test_sin_plantilla_configurada_decide_pero_no_manda(monkeypatch):
    """Es lo que permite colgar el despachador en producción y comprobar que decide bien antes
    de que mande un solo mensaje."""
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    mandados: list[str] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia, "marcar_seguimiento_enviado", lambda conn, id_seguimiento: None
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            mandados.append(telefono)
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="",
            ahora=momento(16, 18),
        )
    )

    assert mandados == []
    assert recuento["enviados"] == 0
```

Añade también, arriba del archivo, la conexión falsa:

```python
class _ConexionFalsa:
    """`persistencia.conectar` se usa como context manager. Esto es lo mínimo para doblarlo."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass
```

- [ ] **Step 8: Correr para verificar que falla**

```
uv run pytest -q tests/test_seguimientos.py -k despachador -v
```

Esperado: FALLA con `AttributeError: module 'maxicare_daniela.seguimientos' has no attribute 'despachar'`.

- [ ] **Step 9: Implementar `despachar`**

Añadir a `src/maxicare_daniela/seguimientos.py` (y `import asyncio`, `from . import persistencia` arriba):

```python
#: Cuántas veces se reintenta un envío DENTRO del mismo ciclo. No vuelve a la cola: la fila ya
#: está marcada.
INTENTOS_DE_ENVIO = 3

#: Entre un intento y el siguiente. Corto a propósito: el ciclo entero tiene sesenta segundos.
SEGUNDOS_ENTRE_INTENTOS = 2.0


def _parametros_del_recordatorio(fila: dict[str, Any]) -> list[str]:
    """Los cuatro huecos de la plantilla, en el orden en que Meta los aprobó.

    Cambiar este orden no cambia la plantilla: manda otro dato en otro hueco, y el paciente lee
    una hora donde esperaba su nombre.
    """
    from .herramientas import _formatear_hora

    inicio = fila["cita_inicio"]
    nombre = (fila.get("nombre_completo") or "").split(" ")[0] or "paciente"
    return [
        nombre,
        _formatear_hora(inicio) if inicio else "PENDIENTE",
        f"{inicio:%H:%M}" if inicio else "PENDIENTE",
        fila.get("tratamiento") or "su cita",
    ]


async def despachar(
    *,
    database_url: str,
    whatsapp: Any | None,
    jornada: Jornada,
    plantilla: str,
    ahora: datetime | None = None,
    limite: int = 50,
) -> dict[str, int]:
    """Un ciclo del despachador. Devuelve el recuento por acción.

    `ahora` entra como parámetro para que una prueba pueda fijarlo: es la misma regla que
    `ctx.ahora` en las tools, y la razón por la que esto se puede probar sin esperar a las seis
    de la tarde.

    `plantilla` vacía apaga el ENVÍO sin apagar la decisión: las guardas corren, las anulaciones
    y los aplazamientos se escriben, y no sale un solo mensaje. Es lo que permite comprobar en
    producción que decide bien antes de arriesgar un WhatsApp.
    """
    momento_actual = ahora or datetime.now(jornada_zona())
    recuento = {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 0}
    numeros_de_esta_tanda: set[str] = set()

    def _leer(conn) -> list[dict[str, Any]]:
        return persistencia.seguimientos_por_despachar(
            conn, ahora=momento_actual, limite=limite
        )

    def _con_conexion(trabajo):
        with persistencia.conectar(database_url) as conn:
            return trabajo(conn)

    filas = await asyncio.to_thread(_con_conexion, _leer)

    for fila in filas:
        telefono = fila.get("telefono") or ""

        def _ultimo(conn, telefono=telefono):
            return persistencia.ultimo_mensaje_del_paciente(conn, telefono)

        ultimo = await asyncio.to_thread(_con_conexion, _ultimo)

        decision = decidir(
            fila,
            ahora=momento_actual,
            jornada=jornada,
            ultimo_mensaje=ultimo,
            ya_salio_a_ese_numero=telefono in numeros_de_esta_tanda,
        )

        if decision.accion == "anular":
            await asyncio.to_thread(
                _con_conexion,
                lambda conn, f=fila, d=decision: persistencia.anular_seguimiento(
                    conn, f["id"], motivo=d.motivo
                ),
            )
            recuento["anulados"] += 1
            continue

        if decision.accion == "aplazar":
            await asyncio.to_thread(
                _con_conexion,
                lambda conn, f=fila, d=decision: persistencia.aplazar_seguimiento(
                    conn, f["id"], hasta=d.hasta or momento_actual
                ),
            )
            recuento["aplazados"] += 1
            continue

        # MARCAR PRIMERO. Ver `persistencia.marcar_seguimiento_enviado`: no hay transacción que
        # cubra una llamada a Meta, y mandar dos veces es peor que perder uno.
        await asyncio.to_thread(
            _con_conexion,
            lambda conn, f=fila: persistencia.marcar_seguimiento_enviado(conn, f["id"]),
        )

        if not plantilla or whatsapp is None or not telefono:
            log.info(
                "seguimiento %s: decidido ENVIAR y no se manda (plantilla o canal sin "
                "configurar). El despachador decide, el canal está apagado.",
                fila["id"],
            )
            continue

        fallo: str | None = None
        for intento in range(INTENTOS_DE_ENVIO):
            try:
                await whatsapp.enviar_plantilla(
                    telefono,
                    plantilla=plantilla,
                    parametros=_parametros_del_recordatorio(fila),
                )
                fallo = None
                break
            except Exception as e:  # noqa: BLE001 -- el ciclo tiene que seguir con los demás
                fallo = f"{type(e).__name__}: {e}"
                if intento + 1 < INTENTOS_DE_ENVIO:
                    await asyncio.sleep(SEGUNDOS_ENTRE_INTENTOS)

        if fallo is not None:
            await asyncio.to_thread(
                _con_conexion,
                lambda conn, f=fila, m=fallo: persistencia.anotar_fallo_de_seguimiento(
                    conn, f["id"], fallo=m
                ),
            )
            recuento["fallidos"] += 1
            log.error("seguimiento %s no salió tras %d intentos: %s", fila["id"], INTENTOS_DE_ENVIO, fallo)
            continue

        await asyncio.to_thread(
            _con_conexion,
            lambda conn, f=fila: persistencia.anotar_recordatorio_en_conversacion(
                conn, f["conversacion_id"], tipo=f["tipo"], cuando=momento_actual
            ),
        )
        numeros_de_esta_tanda.add(telefono)
        recuento["enviados"] += 1

    return recuento


def jornada_zona():
    """La zona de Bogotá, importada tarde para no crear un ciclo con `herramientas`."""
    from .herramientas import ZONA_BOGOTA

    return ZONA_BOGOTA
```

- [ ] **Step 10: Correr para verificar que pasa**

```
uv run pytest -q tests/test_seguimientos.py -v
```

Esperado: PASA, las diecisiete.

Si `test_el_despachador_marca_antes_de_enviar` falla con `orden == ["enviar", "marcar"]`, el orden de las dos llamadas está invertido en el código — es exactamente el error que la prueba existe para cazar.

- [ ] **Step 11: Commit**

```bash
git add src/maxicare_daniela/seguimientos.py tests/test_seguimientos.py
git commit -m "feat: el bucle de despacho -- marca antes de enviar, y sin plantilla decide igual

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Programar desde el código, y la cascada


Aquí el recordatorio deja de depender de que el modelo se acuerde.

**Files:**
- Modify: `src/maxicare_daniela/herramientas.py:820-860` (`_crear_cita`, la función `guardar`), `:924-1050` (`_reprogramar_cita`), `:1057-1190` (`_cancelar_cita`)
- Modify: `tests/test_herramientas.py`

**Interfaces:**
- Consumes: `seguimientos.momento_del_recordatorio` (Task 2), `persistencia.insertar_seguimiento(commit=False)`, `persistencia.registrar_cita(commit=False)`, `persistencia.anular_seguimientos_de_cita(commit=False)` (Task 3), `ctx.clave(...)`, `ctx.jornada`, `ctx.ahora`.
- Produces: nada que otra tarea consuma.

- [ ] **Step 1: Escribir las pruebas que fallan**

Añadir a `tests/test_herramientas.py`:

El montaje sale de lo que ya hay en ese archivo: el ayudante `contexto(**cambios)` (línea 38), la clase `BaseFalsa` (línea 50) y el patrón de `base_falsa` con `monkeypatch.setattr(h, "_con_base", ...)` que usan todas las pruebas de `_crear_cita`. No escribas un montaje nuevo.

```python
def test_crear_cita_programa_el_recordatorio_sin_que_el_modelo_lo_pida(monkeypatch):
    """El no-negociable 2 aplicado a otro caso: lo que tiene que ocurrir siempre no lo decide
    el modelo.

    Hasta hoy el recordatorio dependía de que el modelo llamara a `programar_seguimiento`, y ni
    el prompt de `agentes.py` ni `crear_cita` la mencionaban. En la práctica la cola estaba
    vacía: la tool existía desde la fase 3 y nadie la llamaba.
    """
    programados: list[dict] = []
    ctx = contexto(ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (55, 1))
    monkeypatch.setattr(persistencia, "cita_viva_de_reserva", lambda conn, reserva_id: None)
    monkeypatch.setattr(persistencia, "asegurar_paciente", lambda conn, **kw: 7)
    monkeypatch.setattr(persistencia, "registrar_cita", lambda conn, **kw: "cita-nueva")

    def _insertar(conn, **kwargs):
        programados.append(kwargs)
        return True

    monkeypatch.setattr(persistencia, "insertar_seguimiento", _insertar)

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                # Tres días vista: cae en la banda de la víspera.
                inicio=datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )

    assert "Cita confirmada" in texto
    assert len(programados) == 1, "la cita se creó sin recordatorio"
    assert programados[0]["tipo"] == "recordatorio_cita"
    assert programados[0]["cita_id"] == "cita-nueva"
    # La víspera a las 18:00, no «24 horas antes».
    assert programados[0]["fecha_objetivo"] == datetime(
        2026, 9, 16, 18, 0, tzinfo=h.ZONA_BOGOTA
    )
    # La clave la arma `ctx.clave`, nunca el modelo: lleva el id de la conversación delante.
    assert programados[0]["clave_idempotencia"].startswith("conv-1:")
    assert "da-igual" not in programados[0]["clave_idempotencia"]


def test_una_cita_a_dos_horas_no_deja_recordatorio(monkeypatch):
    """El piso de las 4 horas, desde la tool: el paciente acaba de hablar con Daniela."""
    programados: list[dict] = []
    ctx = contexto(ahora=datetime(2026, 9, 15, 9, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (55, 1))
    monkeypatch.setattr(persistencia, "cita_viva_de_reserva", lambda conn, reserva_id: None)
    monkeypatch.setattr(persistencia, "asegurar_paciente", lambda conn, **kw: 7)
    monkeypatch.setattr(persistencia, "registrar_cita", lambda conn, **kw: "cita-nueva")
    monkeypatch.setattr(
        persistencia,
        "insertar_seguimiento",
        lambda conn, **kw: programados.append(kw) or True,
    )

    asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=datetime(2026, 9, 15, 11, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )

    assert programados == []


def test_cancelar_cita_anula_su_recordatorio(monkeypatch):
    """Sin esto, el paciente que canceló recibe la víspera un recordatorio de la cita que
    canceló. Es el fallo que `cita_id` existe para hacer detectable."""
    anulados: list[tuple[str, str]] = []
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA),
        id_paciente=7,
        identidad_verificada=True,
    )

    cita = {
        "id": "cita-1",
        "reserva_id": 55,
        "conversacion_id": "conv-1",
        "paciente_id": 7,
        "nombre_completo": "Ana Gómez",
        "telefono": "573001112233",
        "tratamiento": "limpieza",
        "inicio": datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA),
        "duracion_minutos": 60,
        "evento_calendar_id": None,
        "estado": "confirmada",
    }

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "leer_cita", lambda conn, id_cita: cita)
    monkeypatch.setattr(persistencia, "marcar_cita_cancelada", lambda conn, id_cita, **k: None)
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    monkeypatch.setattr(
        persistencia,
        "anular_seguimientos_de_cita",
        lambda conn, cita_id, **k: (anulados.append((cita_id, k["motivo"])), 1)[1],
    )

    asyncio.run(
        h._cancelar_cita(
            ctx, SolicitudCancelacion(id_cita="cita-1", motivo="no puedo ir")
        )
    )

    assert anulados == [("cita-1", "cita_cancelada")]
```

Si alguno de los `monkeypatch.setattr` apunta a una función que `_cancelar_cita` no llama —`liberar_cupo` puede tener otro nombre—, míralo en `herramientas.py` antes de correr: un doble sobre una función que nadie llama no falla, simplemente no prueba nada.

- [ ] **Step 2: Correr para verificar que falla**

```
uv run pytest -q tests/test_herramientas.py -k recordatorio -v
```

Esperado: FALLA — `assert len(programados) == 1` recibe `0`.

- [ ] **Step 3: Programar en `_crear_cita`**

En `herramientas.py`, dentro de la función `guardar(conn)` de `_crear_cita` (sobre la línea 820), cambia el `return` para que la cita y su recordatorio nazcan en UNA transacción:

```python
    def guardar(conn) -> tuple[str, int]:
        paciente_id = ctx.id_paciente or persistencia.asegurar_paciente(
            conn,
            nombre_completo=solicitud.nombre_completo,
            telefono=ctx.telefono_completo,
        )
        id_cita = persistencia.registrar_cita(
            conn,
            reserva_id=reserva_id,
            conversacion_id=ctx.id_conversacion,
            paciente_id=paciente_id,
            nombre_completo=solicitud.nombre_completo,
            telefono=ctx.telefono_completo,
            tratamiento=solicitud.tratamiento,
            inicio=inicio,
            duracion_minutos=ctx.duracion_cita_minutos,
            evento_calendar_id=evento_id,
            commit=False,
        )
        # El recordatorio lo emite el CÓDIGO, no el modelo. `programar_seguimiento` sigue
        # existiendo para lo que sí es criterio suyo --«llámenme el lunes»--, pero un
        # recordatorio que depende de que se acuerde es un recordatorio que a veces no existe,
        # y nadie se entera de cuál faltó.
        #
        # Va en la MISMA transacción que la cita: sin commit de por medio, una caída no puede
        # dejar una cita sin su recordatorio.
        cuando = seguimientos.momento_del_recordatorio(
            inicio_cita=inicio,
            ahora=ctx.ahora,
            jornada=ctx.jornada,
            hora_vispera=ctx.hora_recordatorio_vispera,
            horas_minimas=ctx.horas_minimas_para_recordar,
        )
        if cuando is not None:
            persistencia.insertar_seguimiento(
                conn,
                id_conversacion=ctx.id_conversacion,
                tipo="recordatorio_cita",
                fecha_objetivo=cuando,
                # La clave la arma `ctx.clave`, como las otras cuatro. Nunca el modelo.
                clave_idempotencia=ctx.clave("recordatorio", id_cita),
                cita_id=id_cita,
                commit=False,
            )
        conn.commit()
        return (id_cita, paciente_id)
```

Añade `from . import seguimientos` a los imports de `herramientas.py`, y los dos campos al contexto — ver el Step 5.

- [ ] **Step 4: La cascada en reprogramar y cancelar**

En `_reprogramar_cita`, dentro de la función que llama a `persistencia.mover_cita`, **antes** del commit:

```python
        persistencia.anular_seguimientos_de_cita(
            conn, id_cita, motivo="cita_reprogramada", commit=False
        )
        cuando = seguimientos.momento_del_recordatorio(
            inicio_cita=destino,
            ahora=ctx.ahora,
            jornada=ctx.jornada,
            hora_vispera=ctx.hora_recordatorio_vispera,
            horas_minimas=ctx.horas_minimas_para_recordar,
        )
        if cuando is not None:
            persistencia.insertar_seguimiento(
                conn,
                id_conversacion=ctx.id_conversacion,
                tipo="recordatorio_cita",
                fecha_objetivo=cuando,
                # El `destino` dentro de la clave es lo que permite que la MISMA cita tenga un
                # recordatorio nuevo por cada hora a la que se mueva, sin chocar con el viejo.
                clave_idempotencia=ctx.clave("recordatorio", id_cita, destino.isoformat()),
                cita_id=id_cita,
                commit=False,
            )
```

En `_cancelar_cita`, junto a `persistencia.marcar_cita_cancelada`, en la misma función y antes del commit:

```python
        persistencia.anular_seguimientos_de_cita(
            conn, solicitud.id_cita, motivo="cita_cancelada", commit=False
        )
```

`mover_cita` y `marcar_cita_cancelada` hacen `conn.commit()` dentro, así que tendrás que darles el mismo tratamiento que a `registrar_cita`: `commit: bool = True` y `if commit: conn.commit()`. Hazlo, y llama a las dos con `commit=False` desde estas dos tools.

- [ ] **Step 5: Los dos campos del contexto**

En `contratos.py`, `ContextoDaniela`, junto a `cierre_relevo_minutos` (línea 596):

```python
    #: Las dos perillas de los recordatorios (migración 017), leídas de `configuracion`.
    hora_recordatorio_vispera: int = 18
    horas_minimas_para_recordar: int = 4

    #: El último recordatorio que le salió a este paciente, si lo hubo. Lo mandó el
    #: despachador y NO la conversación, así que el historial del agente no lo contiene: sin
    #: esto, un paciente que responde «sí, confirmo» le está diciendo que sí a algo que Daniela
    #: no sabe que se dijo. Sale de la base y nunca del modelo, igual que
    #: `telefono_sin_paciente`.
    ultimo_recordatorio_tipo: str | None = None
    ultimo_recordatorio_en: datetime | None = None
```

Y en `atencion.py`, dentro del constructor del contexto (línea ~951), junto a `cierre_relevo_minutos`:

```python
            hora_recordatorio_vispera=operativa.get("hora_recordatorio_vispera", 18),
            horas_minimas_para_recordar=operativa.get("horas_minimas_para_recordar", 4),
            ultimo_recordatorio_tipo=estado.ultimo_recordatorio_tipo,
            ultimo_recordatorio_en=estado.ultimo_recordatorio_en,
```

Las dos últimas exigen que `_leer_estado` las traiga: añade `ultimo_recordatorio_tipo` y
`ultimo_recordatorio_en` al `SELECT` sobre `conversaciones` que ya hace y a la dataclass de
estado que devuelve. Es el mismo camino por el que ya viaja `tomada_por`.

Haz lo mismo en `runtime.py:1418`, donde se construye el otro contexto (el del chat web); allí
los dos campos van a `None`: el chat web no recibe recordatorios.

Y en el prompt de `agentes.py`, donde se describen los datos del contexto que Daniela puede
usar, añade una línea que diga qué significa: si `ultimo_recordatorio_tipo` tiene valor, al
paciente le salió ese recordatorio y un «sí» suyo se refiere a esa cita. Sin esa línea el campo
llega y el modelo no sabe qué hacer con él.

- [ ] **Step 6: Correr todo**

```
uv run pytest -q
uv run pytest -q tests/test_herramientas.py -k recordatorio -v
```

Esperado: PASA. Si cae algo en `test_tools_neon.py` sobre `registrar_cita`, es el `commit=False`: comprueba que el `conn.commit()` final está dentro de `guardar`.

- [ ] **Step 7: Los scripts que doblan firmas**

`_crear_cita`, `_reprogramar_cita` y `_cancelar_cita` cambiaron por dentro, y `probar_tools.py` las dobla.

```
uv run python scripts/probar_tools.py
uv run python scripts/probar_relevo.py
uv run python scripts/probar_calendario.py
```

Esperado: los tres en verde.

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/herramientas.py src/maxicare_daniela/persistencia.py src/maxicare_daniela/contratos.py src/maxicare_daniela/atencion.py src/maxicare_daniela/runtime.py tests/test_herramientas.py
git commit -m "feat: el recordatorio lo emite el codigo, y la cascada lo sigue cuando la cita cambia

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Ponerlo a correr y demostrarlo


**Files:**
- Modify: `src/maxicare_daniela/runtime.py:1755-1820`
- Modify: `tests/test_seguimientos.py`

**Interfaces:**
- Consumes: `seguimientos.despachar` (Task 6), `config.plantilla_recordatorio` (Task 5).
- Produces: la tarea `_tarea_de_recordatorios`, viva mientras viva el servidor.

- [ ] **Step 1: Escribir la prueba que falla**

Añadir a `tests/test_seguimientos.py`:

```python
def test_el_despachador_arranca_aunque_no_haya_telegram():
    """El barrido de relevos NO arranca sin Telegram, y es correcto: sin Telegram no hay
    relevos que cerrar. Los recordatorios no dependen de Telegram, así que colgarlos de esa
    misma tarea los dejaría apagados en cualquier despliegue sin grupo de doctores -- sin un
    solo error en el log, que es exactamente como el barrido de relevos estuvo días caído."""
    import inspect

    from maxicare_daniela import runtime

    fuente = inspect.getsource(runtime._arrancar_despacho_de_recordatorios)
    assert "telegram_bot_token" not in fuente
```

- [ ] **Step 2: Correr para verificar que falla**

```
uv run pytest -q tests/test_seguimientos.py -k arranca -v
```

Esperado: FALLA con `AttributeError: module 'maxicare_daniela.runtime' has no attribute '_arrancar_despacho_de_recordatorios'`.

- [ ] **Step 3: Implementar la tarea**

En `runtime.py`, después de `_parar_barrido_de_relevos`:

```python
#: La referencia viva de la tarea de recordatorios. Igual que `_tarea_de_barrido`: sin
#: guardarla, el recolector de basura se puede llevar una tarea que nadie mira y los
#: recordatorios dejarían de salir sin un solo error en el log.
_tarea_de_recordatorios: asyncio.Task | None = None


async def _despachar_recordatorios_sin_parar() -> None:
    """El reloj de la cola de recordatorios.

    Va en una tarea PROPIA y no dentro de `_barrer_relevos_sin_parar`, aunque el intervalo sea
    el mismo: aquella no arranca sin Telegram --correcto, sin Telegram no hay relevos que
    cerrar-- y los recordatorios no dependen de Telegram para nada. Compartirlas dejaría los
    recordatorios apagados en cualquier despliegue sin grupo de doctores.

    **Asume un solo worker**, igual que el barrido de relevos y que `_candados` de `atencion`.
    Con varias réplicas el daño está acotado por `FOR UPDATE SKIP LOCKED`: cada fila la toma
    una sola.
    """
    while True:
        await asyncio.sleep(SEGUNDOS_ENTRE_BARRIDOS)
        try:
            operativa = await asyncio.to_thread(_leer_configuracion_operativa)
            recuento = await seguimientos.despachar(
                database_url=config.database_url,
                whatsapp=_whatsapp,
                jornada=Jornada(
                    apertura=operativa.get("hora_apertura", 8),
                    cierre=operativa.get("hora_cierre", 17),
                    cierre_sabado=operativa.get("hora_cierre_sabado", 15),
                    atiende_domingo=bool(operativa.get("atiende_domingo", 0)),
                ),
                plantilla=config.plantilla_recordatorio,
            )
            if any(recuento.values()):
                log.info("recordatorios: %s", recuento)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- tiene que seguir vivo mañana
            log.exception("el despacho de recordatorios falló; se reintenta en el ciclo siguiente")


@app.on_event("startup")
async def _arrancar_despacho_de_recordatorios() -> None:
    global _tarea_de_recordatorios
    if not config.database_url:
        log.info("sin base configurada: no arranca el despacho de recordatorios")
        return
    if not config.plantilla_recordatorio:
        log.warning(
            "MAXICARE_PLANTILLA_RECORDATORIO vacía: el despachador decidirá y NO enviará. "
            "Es el modo de comprobación; para enviar de verdad hace falta la plantilla de Meta."
        )
    _tarea_de_recordatorios = asyncio.create_task(_despachar_recordatorios_sin_parar())


@app.on_event("shutdown")
async def _parar_despacho_de_recordatorios() -> None:
    if _tarea_de_recordatorios is None:
        return
    _tarea_de_recordatorios.cancel()
    try:
        await _tarea_de_recordatorios
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
```

Añade `from . import seguimientos` a los imports de `runtime.py`. Si no existe un ayudante `_leer_configuracion_operativa`, escríbelo al lado:

```python
def _leer_configuracion_operativa() -> dict[str, int]:
    with persistencia.conectar(config.database_url) as conn:
        return persistencia.leer_configuracion(conn)
```

- [ ] **Step 4: Correr las pruebas**

```
uv run pytest -q
```

Esperado: PASA. `tests/test_estructura.py` puede comprobar los `on_event` registrados; si cae, añade los dos nuevos a su lista.

- [ ] **Step 5: Commit**

```bash
git add src/maxicare_daniela/runtime.py tests/test_seguimientos.py
git commit -m "feat: el despacho de recordatorios vive en su propia tarea, no en la de relevos

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

### Segunda mitad: el entregable y la documentación


**Files:**
- Create: `scripts/probar_recordatorios.py`
- Modify: `CLAUDE.md`
- Modify: `.claude/rules/scripts-entregables.md`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: el entregable verificable de la fase.

- [ ] **Step 6: Escribir el script**

Crear `scripts/probar_recordatorios.py` con esta estructura. Los ayudantes `marca`, `url_de_pruebas`, `montar_esquema` y `limpiar` **ya existen en `scripts/probar_tools.py`**: cópialos tal cual, no los reinventes — el aislamiento del esquema `pruebas` y el `search_path` sobre la conexión directa son la mitad del valor de ese molde.

```python
"""Corre la cola de recordatorios contra Neon y lo cuenta en claro.

    uv run python scripts/probar_recordatorios.py

Es el entregable de los recordatorios en forma legible. No gasta un solo token: el modelo no
interviene en ningún punto de este camino, que es justamente lo que se quiere demostrar --el
recordatorio lo emite el código.

Escribe en el esquema `pruebas` y lo borra al terminar. Va contra la conexión DIRECTA de Neon,
sin el `-pooler.` del host: la comprobación 6 necesita dos sesiones de verdad, y PgBouncer las
reparte entre sesiones compartidas. Con el pooler, esa comprobación pasa en verde sin probar
nada -- que es peor que no tenerla.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import herramientas as h  # noqa: E402
from maxicare_daniela import persistencia, seguimientos  # noqa: E402
from maxicare_daniela.calendario import CalendarioDoble, Jornada  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402
from maxicare_daniela.contratos import (  # noqa: E402
    ContextoDaniela,
    SolicitudCancelacion,
    SolicitudCita,
)

ESQUEMA = "pruebas"

fallos = 0


def marca(ok: bool) -> str:
    global fallos
    if not ok:
        fallos += 1
    return "OK  " if ok else "FALLA"


def uno_las_columnas_y_las_perillas(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'seguimientos' AND table_schema = current_schema()
            """
        )
        columnas = {f[0] for f in cur.fetchall()}
    esperadas = {"cita_id", "anulado_en", "motivo_anulacion", "intentos", "fallo"}
    operativa = persistencia.leer_configuracion(conn)
    ok = esperadas <= columnas and operativa.get("hora_recordatorio_vispera") == 18
    print(f"{marca(ok)} 1. la 017 dejo las columnas y las perillas")


def dos_crear_cita_deja_su_recordatorio(conn, ctx) -> str:
    inicio = ctx.ahora + timedelta(days=3)
    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Paciente De Prueba",
                inicio=inicio,
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, cita_id, fecha_objetivo FROM seguimientos WHERE cita_id IS NOT NULL"
        )
        filas = cur.fetchall()
    ok = "Cita confirmada" in texto and len(filas) == 1
    print(f"{marca(ok)} 2. crear_cita dejo su recordatorio, sin que el modelo lo pidiera")
    return filas[0][1] if filas else ""


def tres_reprogramar_mueve_el_recordatorio(conn, ctx, id_cita: str) -> None:
    destino = (ctx.ahora + timedelta(days=4)).replace(hour=10, minute=0)
    asyncio.run(h._reprogramar_cita(ctx, id_cita, destino.isoformat()))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE anulado_en IS NOT NULL),
                   count(*) FILTER (WHERE anulado_en IS NULL)
              FROM seguimientos WHERE cita_id = %s
            """,
            (id_cita,),
        )
        anulados, vivos = cur.fetchone()
    ok = anulados == 1 and vivos == 1
    print(f"{marca(ok)} 3. reprogramar anulo el viejo ({anulados}) y creo uno nuevo ({vivos})")


def cuatro_cancelar_anula_el_recordatorio(conn, ctx, id_cita: str) -> None:
    asyncio.run(
        h._cancelar_cita(ctx, SolicitudCancelacion(id_cita=id_cita, motivo="prueba"))
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM seguimientos WHERE cita_id = %s AND anulado_en IS NULL",
            (id_cita,),
        )
        vivos = cur.fetchone()[0]
    ok = vivos == 0
    print(f"{marca(ok)} 4. cancelar dejo {vivos} recordatorios vivos (esperado 0)")


def cinco_el_despachador_decide_sin_enviar(url: str, ctx) -> None:
    """Con `plantilla=""` el despachador corre entero y no manda nada. Es el modo con el que
    se cuelga en produccion para ver que decide bien antes de arriesgar un WhatsApp."""
    recuento = asyncio.run(
        seguimientos.despachar(
            database_url=url,
            whatsapp=None,
            jornada=Jornada(),
            plantilla="",
            ahora=ctx.ahora + timedelta(days=30),
        )
    )
    ok = recuento["enviados"] == 0 and sum(recuento.values()) >= 0
    print(f"{marca(ok)} 5. el despachador decidio sin enviar: {recuento}")


def seis_dos_despachadores_no_toman_la_misma_fila(url: str) -> None:
    """Lo que NINGUNA prueba offline caza, y la razon de que este script exista.

    Dos sesiones piden la cola a la vez. `FOR UPDATE ... SKIP LOCKED` tiene que darle la fila
    a una sola. Con el pooler esto pasa en verde sin probar nada: hace falta la conexion
    directa.
    """
    tomadas: list[list[int]] = []
    barrera = threading.Barrier(2)

    def pedir() -> None:
        with persistencia.conectar(url) as conn:
            conn.autocommit = False
            barrera.wait()
            filas = persistencia.seguimientos_por_despachar(
                conn, ahora=datetime.now().astimezone() + timedelta(days=365)
            )
            tomadas.append([f["id"] for f in filas])
            # Se mantiene la transaccion abierta un instante: sin esto el bloqueo se suelta
            # antes de que la otra sesion llegue a pedir, y la prueba no prueba nada.
            import time

            time.sleep(0.5)
            conn.rollback()

    hilos = [threading.Thread(target=pedir) for _ in range(2)]
    for t in hilos:
        t.start()
    for t in hilos:
        t.join()

    compartidas = set(tomadas[0]) & set(tomadas[1]) if len(tomadas) == 2 else {"sin datos"}
    ok = not compartidas
    print(f"{marca(ok)} 6. dos despachadores tomaron filas distintas (comunes: {compartidas})")


def main() -> int:
    url, _ = url_de_pruebas()          # copiar de probar_tools.py
    montar_esquema(url)                # copiar de probar_tools.py
    try:
        with persistencia.conectar(url) as conn:
            ctx = ContextoDaniela(
                id_conversacion=crear_conversacion(conn),   # copiar de probar_tools.py
                telefono_completo="573000000000",
                database_url=url,
                calendario=CalendarioDoble(),
                ahora=datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0),
                jornada=Jornada(),
            )
            uno_las_columnas_y_las_perillas(conn)
            id_cita = dos_crear_cita_deja_su_recordatorio(conn, ctx)
            if id_cita:
                tres_reprogramar_mueve_el_recordatorio(conn, ctx, id_cita)
                cuatro_cancelar_anula_el_recordatorio(conn, ctx, id_cita)
        cinco_el_despachador_decide_sin_enviar(url, ctx)
        seis_dos_despachadores_no_toman_la_misma_fila(url)
    finally:
        limpiar(url)                   # copiar de probar_tools.py
    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLA(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Los cuatro nombres marcados con «copiar de probar_tools.py» son ayudantes que existen ahí: `url_de_pruebas`, `montar_esquema`, `limpiar` y el montaje que crea una conversación. Ábrelo y cópialos; si alguno tiene otro nombre, usa el que tenga — **no escribas SQL de montaje nuevo**, el aislamiento del esquema depende de ese molde.

- [ ] **Step 7: Correrlo**

```
uv run python scripts/probar_recordatorios.py
```

Esperado: seis `OK`, ningún `FALLA`.

- [ ] **Step 8: Añadirlo a la tabla de entregables del CLAUDE.md**

En la tabla de «Entregables por fase», después de la fila de `probar_panel.py`:

```
| `scripts/probar_recordatorios.py` | la cola de recordatorios y su despachador | no |
```

Y en la frase que sigue a la tabla, donde dice «quien toque una de esas firmas corre los cinco que no gastan», cambia **cinco** por **seis**.

- [ ] **Step 9: Añadir el no-negociable**

En la sección «No negociables» del `CLAUDE.md`, como punto 21:

```markdown
21. **Un recordatorio se MARCA antes de enviarse, y lo emite el código, no el modelo.** No hay
   transacción que cubra una llamada a Meta: enviar primero y marcar después manda el mismo
   recordatorio otra vez sesenta segundos más tarde, y **ninguna de las siete guardas lo
   detecta** —todas siguen diciendo que sí—. La cola cuelga de `cita_id` y no solo de la
   conversación: sin eso, reprogramar deja vivo un recordatorio de una cita que ya no existe.
   El despacho vive en su **propia** tarea de `runtime.py`, no en la de relevos, que no arranca
   sin Telegram.
```

- [ ] **Step 10: Documentar el script en su regla**

En `.claude/rules/scripts-entregables.md`, añade la entrada de `probar_recordatorios.py`: en qué esquema escribe (`pruebas`), qué dobla a mano (las firmas de `_crear_cita`, `_reprogramar_cita` y `persistencia.seguimientos_por_despachar`) y su trampa (el punto 6 exige la conexión directa; con el pooler pasa en verde sin probar nada).

- [ ] **Step 11: Correr todo, por última vez**

```
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_tools.py
uv run python scripts/probar_recordatorios.py
uv run python scripts/probar_relevo.py
uv run python scripts/probar_calendario.py
```

Esperado: todo en verde.

- [ ] **Step 12: Commit**

```bash
git add scripts/probar_recordatorios.py CLAUDE.md .claude/rules/scripts-entregables.md
git commit -m "feat: probar_recordatorios.py, el entregable de la cola

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Lo que este plan NO construye

Dicho aquí para que nadie lo dé por hecho al terminar:

- **El parte diario a los doctores por WhatsApp** (sección 9 de la spec). Necesita una plantilla propia y los números de los doctores, que hoy no están en ninguna parte del proyecto. Decisión del cliente: esperar, y no hacerlo por Telegram.
- **`reactivacion_inasistencia` y `reactivacion_cancelacion`.** La infraestructura queda lista —`insertar_seguimiento` acepta cualquier `tipo`— pero sus disparadores no se cablean aquí: `asistio` no lo escribe nadie en `src/`, y la reactivación por cancelación necesita la comprobación de «si no reagendó», que es una consulta que aún no existe. Van en su propio plan.
- **El envío real.** Hasta que `MAXICARE_PLANTILLA_RECORDATORIO` tenga el nombre de una plantilla aprobada, el despachador decide y no manda. Es deliberado: permite ver en producción que decide bien antes de arriesgar un WhatsApp.

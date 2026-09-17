# Reactivación de leads · plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que un lead que preguntó y no agendó reciba como mucho dos mensajes de seguimiento, y que ninguna de las once reglas anti-reporte se pueda saltar.

**Architecture:** el barrido encola en la tabla `seguimientos` que ya existe; el despachador que ya corre cada 60 s decide y manda. Lo nuevo es (a) cerrar el vocabulario de `tipo`, que hoy es texto libre y se vuelve peligroso al añadir plantillas, (b) cuatro guardas que hoy solo se evalúan cuando el seguimiento tiene cita, (c) despacho multiplantilla y (d) un contador de rechazos por persona.

**Tech Stack:** Python 3.12, psycopg 3, FastAPI, OpenAI Agents SDK 0.22.2, Postgres (Neon), WhatsApp Cloud API.

**Spec:** `docs/superpowers/specs/2026-09-17-reactivacion-de-leads-design.md`
**Plantillas:** `docs/plantillas-meta-reactivacion.md`

## Global Constraints

Copiadas literalmente de la spec y del CLAUDE.md. **Los requisitos de cada tarea las incluyen implícitamente.**

- **Ante la duda, no mandar.** Un número de WhatsApp reportado tumba también los recordatorios de cita, los escalamientos y la atención normal. Un lead perdido cuesta una limpieza; un número quemado cuesta el sistema.
- **Ningún momento sale del reloj de la máquina.** Todos entran como parámetro (`ahora`, `ctx.ahora`). En la suite offline **el presente se clava** — es la tercera vez que este proyecto tropieza con una fecha que envejece.
- **Un seguimiento se MARCA antes de enviarse** (no negociable 21). No hay transacción que cubra una llamada a Meta.
- **Ninguna clave de idempotencia la escribe el modelo.** Las arma el código con `ctx.clave(...)`.
- **Toda migración es idempotente** y no se edita una ya aplicada: se añade la siguiente. Los `DO $$` de constraint filtran por `conrelid`, nunca solo por `conname`.
- **`pytestmark = pytest.mark.neon`** a nivel de módulo en todo lo que toque la base. No se le quita el marcador para que corra siempre.
- **Una tool se prueba por su función interna** `_nombre(...)`, nunca por el `FunctionTool` decorado.
- **Nada de `os.environ[...] = ...`** en el cuerpo de un módulo de prueba: se usa `monkeypatch`.
- **Los scripts de `scripts/` doblan firmas a mano y `pytest -q` no los corre.** Quien cambie una firma que un script dobla corre los seis que no gastan.
- Los mensajes de commit terminan con `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Structure

| Archivo | Responsabilidad | Acción |
|---|---|---|
| `migraciones/021_reactivacion.sql` | vocabulario cerrado de `tipo`, contador en `contactos`, columna de contabilización, perillas | **crear** |
| `src/maxicare_daniela/seguimientos.py` | vocabulario, guardas nuevas, despacho multiplantilla | modificar |
| `src/maxicare_daniela/barrido.py` | quién califica para reactivación y encolar | **crear** |
| `src/maxicare_daniela/persistencia.py` | consultas del barrido, contador, columna nueva en el SELECT | modificar |
| `src/maxicare_daniela/herramientas.py` | `Literal` en `programar_seguimiento`, tool de cierre de serie | modificar |
| `src/maxicare_daniela/config.py` | plantillas por tipo, interruptor, tope diario | modificar |
| `src/maxicare_daniela/runtime.py` | la tarea de fondo del barrido | modificar |
| `src/maxicare_daniela/agentes.py` | lo que el prompt dice del «no» ambiguo | modificar |
| `tests/test_reactivacion.py` | las once reglas, offline | **crear** |
| `tests/test_reactivacion_neon.py` | barrido y contador contra Neon | **crear** |
| `scripts/probar_reactivacion.py` | entregable, no gasta | **crear** |

`barrido.py` va aparte de `seguimientos.py` a propósito: `seguimientos.py` ya tiene 573 líneas y dos responsabilidades (calcular el momento de un recordatorio, y decidir/despachar la cola). Decidir **a quién meter en la cola** es una tercera, y es la única que consulta la cartera entera.

## Bloques de revisión

Las nueve tareas se revisan en **seis paradas**, no en nueve. Fusionar las tareas en sí no
quitaría trabajo —solo ceremonia—, y perder los ciclos de prueba intermedios sí costaría
robustez. Lo que se agrupa es **dónde se para a revisar**:

| Parada | Tareas | Por qué van juntas |
|---|---|---|
| **A** | 1 + 2 | el CHECK de la base y la constante del código son dos caras del mismo vocabulario; hay una prueba que las cruza, y aprobar una sin la otra no significa nada |
| **B** | 3 | las guardas son lo único que decide si un mensaje sale. Se revisa sola, a conciencia |
| **C** | 4 | el envío. Un revisor puede rechazar esto y aprobar las guardas |
| **D** | 5 + 6 | el contador y el «Ya no» son la misma pieza por dos lados: el «no» es quien lo sube |
| **E** | 7 + 8 | el barrido y su tarea de fondo; la 8 son 40 líneas copiando un patrón que ya existe |
| **F** | 9 | el entregable |

**B es la parada que no se salta.** Si hay que ir rápido en algún sitio, que no sea ahí.

---

## Task 1: Migración 021 — el vocabulario cerrado y el contador

Cierra el portillo que documenta `seguimientos.py:160-169`: `tipo` es TEXT libre sin CHECK, el modelo lo escribe desde `programar_seguimiento`, y **G0 solo comprueba la baja si el tipo no está en la lista blanca**. Hoy es inofensivo porque todo lo que no lleva `cita_id` se anula con `sin_plantilla`; la tarea 4 abre esa puerta y con ella el portillo.

**Files:**
- Create: `migraciones/021_reactivacion.sql`
- Test: se verifica con `scripts/inicializar_base.py` (paso 4) y por la tarea 5

**Interfaces:**
- Produces: columnas `contactos.seguimientos_fallidos`, `contactos.ultimo_seguimiento_en`, `seguimientos.contabilizado_en`; constraint `ck_seguimientos_tipo`; perillas `tope_diario_reactivacion`, `max_reactivaciones_12m`, `max_seguimientos_fallidos`

- [ ] **Step 1: Escribir la migración**

```sql
-- =========================================================================================
-- 021 · Reactivacion de leads: el vocabulario de `tipo`, y el freno por persona
--
-- Cierra el portillo que `seguimientos.py` documenta desde la 017: `tipo` es TEXT libre, lo
-- escribe el MODELO desde `programar_seguimiento`, y la guarda de la baja (G0) solo se aplica
-- a lo que NO esta en `TIPOS_NO_COMERCIALES`. Un `tipo='recordatorio_cita'` inventado por el
-- modelo atraviesa esa guarda con el paciente de baja. Hoy no da a ninguna parte porque el
-- despachador anula sin `cita_id` con motivo `sin_plantilla`; en cuanto haya plantillas para
-- tipos sin cita, ese portillo se abre de par en par y el resultado es un mensaje comercial a
-- quien pidio que no le escribieran. Eso es un reporte, y un numero reportado se lleva por
-- delante tambien los recordatorios de cita y los escalamientos.
--
-- El CHECK va NOT VALID a proposito: valida lo que entre de ahora en adelante y no mira las
-- filas viejas. Un CHECK normal reventaria el despliegue si en produccion hay un `tipo` que
-- el modelo invento algun dia, y tumbar el arranque entero por una fila historica es peor que
-- dejarla sin validar: esa fila ya no se va a enviar --o esta enviada, o se anula por
-- `sin_plantilla`-- y lo que hay que proteger es lo que viene.
--
-- `reactivacion_no_asistio` entra en la lista aunque NADIE la vaya a encolar: `citas.asistio`
-- sigue sin escribirse (la pantalla de agenda es cascaron, `web/src/pantallas/Agenda.tsx:50`).
-- Que quede declarada y muerta es deliberado: el dia que la fase 8 escriba `asistio`,
-- encenderla es cambiar una constante y no volver a tocar la base.
--
-- El contador de `contactos` es del SISTEMA, no de la persona: dice «a este numero no le sirve
-- que lo persigamos», no «no me escriban mas». Por eso vive aqui y no en `consentimientos`, y
-- por eso `/clearstate` SI lo resetea --al reves que `no_contactar`, que es un derecho y no se
-- toca nunca-.
-- =========================================================================================

-- El vocabulario cerrado. NOT VALID: solo para lo que entre a partir de ahora.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_seguimientos_tipo'
           AND conrelid = 'seguimientos'::regclass
    ) THEN
        ALTER TABLE seguimientos
            ADD CONSTRAINT ck_seguimientos_tipo CHECK (
                tipo IN (
                    'recordatorio_cita',          -- el unico NO comercial
                    'reactivacion_sin_agendar',   -- pregunto y no agendo
                    'reactivacion_cancelada',     -- cancelo y no volvio
                    'reactivacion_no_asistio'     -- declarada; sin disparador hasta la fase 8
                )
            ) NOT VALID;
    END IF;
END $$;

-- El freno por persona. `seguimientos_fallidos` sube cuando una serie termina en un «no» o en
-- silencio, y vuelve a 0 al agendar: alguien que ignoro dos veces y al final vino demostro lo
-- contrario de lo que el contador supone.
ALTER TABLE contactos
    ADD COLUMN IF NOT EXISTS seguimientos_fallidos SMALLINT     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ultimo_seguimiento_en TIMESTAMPTZ;

-- La marca que impide contar dos veces la misma serie. Sin ella, cada pasada del barrido
-- sumaria uno mas por la misma serie cerrada y el freno se dispararia el primer dia.
ALTER TABLE seguimientos
    ADD COLUMN IF NOT EXISTS contabilizado_en TIMESTAMPTZ;

-- El tope anual (regla 8) se cuenta sobre los enviados de reactivacion. Sin este indice, la
-- subconsulta del despachador recorre la tabla entera por cada una de las 50 filas de la tanda.
CREATE INDEX IF NOT EXISTS ix_seguimientos_enviados_de_reactivacion
    ON seguimientos (tipo, enviado_en)
    WHERE enviado_en IS NOT NULL;

-- Series completas que el barrido todavia no ha contabilizado.
CREATE INDEX IF NOT EXISTS ix_seguimientos_sin_contabilizar
    ON seguimientos (enviado_en)
    WHERE enviado_en IS NOT NULL AND contabilizado_en IS NULL;

-- Las tres perillas, en `configuracion` y no en el `.env`: la clinica las cambia desde el panel
-- sin desplegar, igual que la jornada. Son TEXT porque la tabla guarda TEXT y
-- `leer_configuracion` hace `int(valor)`.
INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('tope_diario_reactivacion', '20',
     'Cuantos mensajes de reactivacion como mucho por dia. Arranque lento: Meta castiga los picos.'),
    ('max_reactivaciones_12m', '6',
     'Tope por PERSONA en 12 meses, pase lo que pase. El limite de 2 por consulta protege la consulta, no a la persona.'),
    ('max_seguimientos_fallidos', '2',
     'Cuantas series seguidas sin exito antes de dejar de perseguir a ese numero. Se resetea al agendar.')
ON CONFLICT (clave) DO NOTHING;
```

- [ ] **Step 2: Añadir la columna nueva al espejo de Python**

`src/maxicare_daniela/persistencia.py:2493-2496` es el **único** sitio: `_fila_contacto` construye el dict desde `cur.description`, así que una columna en esta constante aparece sola en `asegurar_contacto` y `leer_contacto`.

```python
_COLUMNAS_CONTACTO = (
    "telefono, creado_en, actualizado_en, aviso_mostrado_en, politica_version, "
    "no_contactar, no_contactar_en, no_contactar_origen, "
    "seguimientos_fallidos, ultimo_seguimiento_en"
)
```

- [ ] **Step 3: Aplicar y verificar**

Run: `uv run python scripts/inicializar_base.py`
Expected: termina en OK, sin `UndefinedColumn` ni `CheckViolation`.

Run: `uv run python scripts/inicializar_base.py --solo-verificar`
Expected: OK, y no escribe nada.

- [ ] **Step 4: Comprobar que el CHECK rechaza un tipo inventado**

Run:
```bash
uv run python -c "
from maxicare_daniela import persistencia, config as c
from maxicare_daniela.config import Config, cargar_dotenv
cargar_dotenv()
cfg = Config.desde_entorno()
with persistencia.conectar(cfg.database_url) as conn:
    with conn.cursor() as cur:
        try:
            cur.execute(\"INSERT INTO seguimientos (conversacion_id, tipo, fecha_objetivo, clave_idempotencia) VALUES (gen_random_uuid(), 'inventado', now(), 'k-prueba')\")
            print('FALLA: el CHECK dejo pasar un tipo inventado')
        except Exception as e:
            conn.rollback()
            print('OK:', type(e).__name__)
"
```
Expected: `OK: CheckViolation` (o `ForeignKeyViolation` si el CHECK pasa y falla la FK — en ese caso el CHECK **no** está haciendo su trabajo y hay que revisarlo).

- [ ] **Step 5: Commit**

```bash
git add migraciones/021_reactivacion.sql src/maxicare_daniela/persistencia.py
git commit -m "$(cat <<'EOF'
feat: la 021 cierra el vocabulario de `tipo` y trae el freno por persona

`seguimientos.tipo` era TEXT libre escrito por el modelo, y G0 solo comprueba la
baja sobre lo que NO esta en la lista blanca. Un `tipo='recordatorio_cita'`
inventado atravesaba esa guarda con el paciente de baja; hoy no daba a ninguna
parte porque sin `cita_id` el despachador anula con `sin_plantilla`, y el propio
codigo dejo escrito que abrir esa puerta abre el portillo. Esta migracion lo
cierra ANTES de abrirla.

NOT VALID a proposito: un CHECK normal tumbaria el arranque si en produccion hay
un tipo historico inventado, y eso es peor que dejar sin validar una fila que ya
no se va a enviar.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: El vocabulario en el código, y la tool que deja de aceptar cualquier cosa

**Files:**
- Modify: `src/maxicare_daniela/seguimientos.py:170` (junto a `TIPOS_NO_COMERCIALES`)
- Modify: `src/maxicare_daniela/herramientas.py:1420-1492`
- Test: `tests/test_reactivacion.py` (crear)

**Interfaces:**
- Produces: `seguimientos.TIPO_RECORDATORIO`, `TIPO_SIN_AGENDAR`, `TIPO_CANCELADA`, `TIPO_NO_ASISTIO`, `TIPOS_DE_SEGUIMIENTO: frozenset[str]`, `TIPOS_DE_REACTIVACION: frozenset[str]`
- Consumes: `TIPOS_NO_COMERCIALES` (ya existe)

- [ ] **Step 1: Escribir la prueba que falla**

```python
# tests/test_reactivacion.py
"""Las once reglas anti-reporte, una prueba por regla.

Cada una tiene que FALLAR si alguien afloja su regla. No comprueban el camino feliz:
comprueban que el camino prohibido sigue prohibido.
"""
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada, ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sabado, domingo cerrado


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Septiembre de 2026: el 15 es martes, el 19 sabado, el 20 domingo."""
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA_BOGOTA)


def test_el_vocabulario_no_deja_inventar_un_tipo():
    assert "inventado" not in s.TIPOS_DE_SEGUIMIENTO


def test_recordatorio_de_cita_no_es_reactivacion():
    """Si se colaran, la baja dejaria de comprobarse sobre ellos (G0 usa lista BLANCA)."""
    assert s.TIPOS_DE_REACTIVACION.isdisjoint(s.TIPOS_NO_COMERCIALES)


def test_toda_reactivacion_es_comercial():
    """Regla 4: la baja tiene que aplicarse a las tres."""
    for tipo in s.TIPOS_DE_REACTIVACION:
        assert tipo not in s.TIPOS_NO_COMERCIALES, tipo


def test_el_vocabulario_del_codigo_y_el_de_la_migracion_no_se_separan():
    """El CHECK de la 021 y esta constante son dos listas del mismo vocabulario.

    Si se separan, el codigo deja pasar un tipo que la base rechaza y el INSERT revienta la
    transaccion del turno entero.
    """
    from pathlib import Path

    sql = Path("migraciones/021_reactivacion.sql").read_text(encoding="utf-8")
    for tipo in s.TIPOS_DE_SEGUIMIENTO:
        assert f"'{tipo}'" in sql, f"{tipo} no esta en el CHECK de la 021"
```

- [ ] **Step 2: Correr la prueba y verla fallar**

Run: `uv run pytest tests/test_reactivacion.py -v`
Expected: FAIL con `AttributeError: module 'maxicare_daniela.seguimientos' has no attribute 'TIPOS_DE_SEGUIMIENTO'`

- [ ] **Step 3: Escribir las constantes**

En `seguimientos.py`, justo **debajo** de `TIPOS_NO_COMERCIALES` (línea 170), para que quien lea una vea la otra:

```python
#: Los cuatro tipos que existen. El CHECK `ck_seguimientos_tipo` de la migracion 021 tiene la
#: MISMA lista: son dos caras de un vocabulario, y `test_el_vocabulario_del_codigo_y_el_de_la_
#: migracion_no_se_separan` las mantiene juntas. Sin el cierre, `tipo` es TEXT que escribe el
#: modelo, y un `recordatorio_cita` inventado atraviesa G0 con el paciente de baja.
TIPO_RECORDATORIO = "recordatorio_cita"
TIPO_SIN_AGENDAR = "reactivacion_sin_agendar"
TIPO_CANCELADA = "reactivacion_cancelada"

#: Declarado y SIN disparador: nadie escribe `citas.asistio` hasta que cierre la fase 8. Existe
#: aqui para que encenderlo sea cambiar una constante y no volver a tocar la base.
TIPO_NO_ASISTIO = "reactivacion_no_asistio"

TIPOS_DE_REACTIVACION = frozenset({TIPO_SIN_AGENDAR, TIPO_CANCELADA, TIPO_NO_ASISTIO})
TIPOS_DE_SEGUIMIENTO = TIPOS_DE_REACTIVACION | {TIPO_RECORDATORIO}

#: Los que el BARRIDO puede encolar hoy. `TIPO_NO_ASISTIO` no esta: sin `citas.asistio` no hay
#: forma de saber quien no vino, y encolarlo mandaria «no pudo asistir» a quien si fue.
TIPOS_QUE_EL_BARRIDO_ENCOLA = frozenset({TIPO_SIN_AGENDAR, TIPO_CANCELADA})
```

- [ ] **Step 4: Correr y ver pasar**

Run: `uv run pytest tests/test_reactivacion.py -v`
Expected: PASS (4 pruebas)

- [ ] **Step 5: Cerrar la tool — prueba primero**

```python
# tests/test_reactivacion.py (añadir)
def test_la_tool_rechaza_un_tipo_que_no_existe():
    """Regla 1 y el portillo de G0: el modelo no puede inventarse un tipo."""
    import asyncio

    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto  # reutiliza el contexto clavado

    ctx = contexto()
    respuesta = asyncio.run(_programar_seguimiento(ctx, "publicidad_masiva", "2026-12-01T10:00:00"))
    assert "publicidad_masiva" not in respuesta
    assert "no existe" in respuesta.lower() or "no es un tipo" in respuesta.lower()


def test_la_tool_no_deja_al_modelo_disfrazar_lo_comercial_de_recordatorio():
    """El portillo entero, en una prueba.

    `recordatorio_cita` esta en `TIPOS_NO_COMERCIALES`, asi que G0 no le aplica la baja. Los
    recordatorios los emite el CODIGO al crear o mover una cita; el modelo no tiene por que
    encolar uno, y si puede, tiene una puerta para saltarse la baja.
    """
    import asyncio

    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto

    ctx = contexto(pidio_no_contacto=True)
    respuesta = asyncio.run(
        _programar_seguimiento(ctx, "recordatorio_cita", "2026-12-01T10:00:00")
    )
    assert "programado" not in respuesta.lower()
```

- [ ] **Step 6: Correr y ver fallar**

Run: `uv run pytest tests/test_reactivacion.py -k tool -v`
Expected: FAIL — hoy la tool acepta cualquier `tipo: str`

- [ ] **Step 7: Implementar la validación en la tool**

En `herramientas.py`, dentro de `_programar_seguimiento`, **antes** de la comprobación de la baja (hoy línea 1433):

```python
    # El vocabulario cerrado. Va ANTES de la baja y antes de la fecha porque es lo unico que
    # protege el portillo de G0: `recordatorio_cita` esta en `TIPOS_NO_COMERCIALES`, asi que un
    # tipo disfrazado se salta la comprobacion de la baja entera. Y `recordatorio_cita` no se
    # encola desde aqui NUNCA: lo emite el codigo al crear o mover la cita (no negociable 21),
    # asi que dejarselo al modelo solo abre esa puerta y no cierra ninguna.
    if tipo not in seguimientos.TIPOS_QUE_EL_BARRIDO_ENCOLA:
        permitidos = ", ".join(sorted(seguimientos.TIPOS_QUE_EL_BARRIDO_ENCOLA))
        return (
            f"Ese tipo de seguimiento no existe. Los que puedes programar son: {permitidos}. "
            "Los recordatorios de una cita los programa el sistema solo."
        )
```

Y cerrar la firma pública con `Literal` (el SDK lo convierte en un enum del esquema de la tool, así que el modelo ve las opciones):

```python
@function_tool(failure_error_function=_fallo_seguimiento)
async def programar_seguimiento(
    wrapper: RunContextWrapper[ContextoDaniela],
    tipo: Literal["reactivacion_sin_agendar", "reactivacion_cancelada"],
    fecha_objetivo: str,
) -> str:
    """Deja programado un seguimiento comercial para más adelante.

    Si el paciente pidió que no le escribieran más, no se programa nada y te lo dice: no
    insistas ni lo intentes con otra fecha. Los recordatorios de una cita NO se piden por
    aquí — los programa el sistema solo al crear o mover la cita.

    Args:
        tipo: 'reactivacion_sin_agendar' si preguntó y no agendó, 'reactivacion_cancelada'
            si canceló y no volvió a pedir fecha.
        fecha_objetivo: cuándo debe salir, en ISO y hora de Bogotá.
    """
    return await _programar_seguimiento(wrapper.context, tipo, fecha_objetivo)
```

El `Literal` **no basta por sí solo** —un modelo puede emitir algo fuera del enum y el SDK no siempre lo rechaza—, por eso la comprobación del núcleo se queda. Las dos, no una.

- [ ] **Step 8: Correr toda la suite**

Run: `uv run pytest -q`
Expected: PASS. Si alguna prueba vieja usaba `tipo="reactivacion"` a secas, actualízala al nombre nuevo — ese tipo ya no existe.

- [ ] **Step 9: Commit**

```bash
git add src/maxicare_daniela/seguimientos.py src/maxicare_daniela/herramientas.py tests/test_reactivacion.py
git commit -m "$(cat <<'EOF'
feat: el vocabulario de seguimientos deja de ser texto libre

Dos capas, no una: `Literal` en la firma de la tool (el SDK se lo enseña al
modelo como enum) y la comprobacion en el nucleo, porque un modelo puede emitir
algo fuera del enum.

Y `recordatorio_cita` sale de lo que el modelo puede encolar. No lo necesita --lo
emite el codigo al crear o mover la cita-- y era la unica forma de disfrazar algo
comercial de no-comercial y saltarse la baja.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Las guardas que hoy no se aplican a la reactivación

Cuatro guardas (G1, G2, G3, G3bis) viven dentro de `if fila.get("cita_id") is not None:` (`seguimientos.py:214`). Un seguimiento de reactivación no tiene cita y las atraviesa. La que importa es **G3** (`llego_tarde`, 2 h): sin ella, un proceso caído el viernes suelta el lunes todos los mensajes atrasados de golpe — el pico exacto que Meta castiga.

Y se añaden tres reglas nuevas que no existen: horario propio (6), contacto reciente ampliado (7), tope anual (8) y freno del contador (5).

**Files:**
- Modify: `src/maxicare_daniela/seguimientos.py:182-299` (`decidir`)
- Modify: `src/maxicare_daniela/persistencia.py:1871-1892` (el SELECT)
- Test: `tests/test_reactivacion.py`

**Interfaces:**
- Consumes: `TIPOS_DE_REACTIVACION` (tarea 2), columnas de la 021 (tarea 1)
- Produces: motivos nuevos `llego_tarde`, `fuera_de_horario_comercial`, `hablo_hace_poco`, `tope_anual`, `seguimiento_apagado`; `decidir(...)` gana los kwargs `reactivaciones_ultimo_ano: int = 0`, `seguimientos_fallidos: int = 0`, `max_reactivaciones_12m: int = 6`, `max_seguimientos_fallidos: int = 2`

- [ ] **Step 1: Escribir las cinco pruebas que fallan**

```python
# tests/test_reactivacion.py (añadir)

def fila_de_reactivacion(**cambios) -> dict:
    """Una fila de la cola SIN cita, que es lo que distingue a la reactivacion."""
    base = dict(
        id=1,
        conversacion_id="conv-1",
        cita_id=None,
        tipo=s.TIPO_SIN_AGENDAR,
        fecha_objetivo=momento(16, 11),
        intentos=0,
        telefono="573001112233",
        nombre_completo="Marcela Rios",
        tratamiento=None,
        cita_inicio=None,
        cita_estado=None,
        tomada_por=None,
        no_contactar=False,
        reactivaciones_ultimo_ano=0,
        seguimientos_fallidos=0,
    )
    base.update(cambios)
    return base


def test_regla_3_una_reactivacion_atrasada_no_sale():
    """G3 para lo que no tiene cita.

    El proceso se cae el viernes y vuelve el lunes. Sin esto salen de golpe todos los
    "hace unos dias nos escribio" con una semana de retraso: el pico que Meta castiga.
    """
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(14, 11)),
        ahora=momento(16, 11),                  # dos dias tarde
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "llego_tarde"


@pytest.mark.parametrize("hora", [7, 8, 19, 22])
def test_regla_6_fuera_del_horario_comercial_no_sale(hora):
    """9:00-19:00 y punto. Las 8:00 valen para un recordatorio de cita, no para publicidad."""
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(16, hora)),
        ahora=momento(16, hora),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "aplazar", f"a las {hora}:00 salio una reactivacion"
    assert decision.motivo == "fuera_de_horario_comercial"


def test_regla_6_el_domingo_no_sale_aunque_la_clinica_abra():
    """No puede colgar de la jornada de la clinica.

    Un recordatorio de cita en domingo esta bien: la cita es real. Un "sigue interesada?" en
    domingo, no. Si MaxiCare abriera los domingos, la jornada dejaria pasar el segundo.
    """
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(20, 11)),
        ahora=momento(20, 11),                  # domingo
        jornada=Jornada(atiende_domingo=True),  # la clinica ABRE
        ultimo_mensaje=None,
    )
    assert decision.accion == "aplazar"
    assert decision.motivo == "fuera_de_horario_comercial"


def test_regla_7_si_hablo_hoy_no_se_le_manda_plantilla():
    """24 h, no los 60 min del recordatorio.

    Mandarle "hace unos dias nos escribio" a quien hablo contigo esta manana es lo que hace
    que la gente conteste "??" y reporte.
    """
    decision = s.decidir(
        fila_de_reactivacion(),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=momento(16, 3),          # 8 h antes
    )
    assert decision.accion == "anular"
    assert decision.motivo == "hablo_hace_poco"


def test_regla_7_el_recordatorio_de_cita_conserva_su_ventana_de_60_min():
    """La ampliacion es solo para reactivacion. Un recordatorio la vispera es util aunque la
    persona haya escrito hace tres horas."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1",
        cita_inicio=momento(17, 9), cita_estado="confirmada",
    )
    decision = s.decidir(
        fila, ahora=momento(16, 18), jornada=JORNADA, ultimo_mensaje=momento(16, 15)
    )
    assert decision.accion == "enviar"


def test_regla_8_el_tope_anual_para_aunque_el_contador_este_en_cero():
    """El contador se resetea al agendar, asi que alguien que agenda cada vez podria recibir
    muchos en un ano. Este es el techo que el contador no pone."""
    decision = s.decidir(
        fila_de_reactivacion(reactivaciones_ultimo_ano=6),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
        max_reactivaciones_12m=6,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "tope_anual"


def test_regla_5_a_quien_ya_dijo_que_no_dos_veces_no_se_le_persigue():
    decision = s.decidir(
        fila_de_reactivacion(seguimientos_fallidos=2),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
        max_seguimientos_fallidos=2,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "seguimiento_apagado"


def test_el_freno_no_alcanza_al_recordatorio_de_una_cita():
    """Regla 5 vs no negociable: el apagado es comercial. Quien tiene cita recibe su
    recordatorio aunque este apagado y aunque haya pedido la baja."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1", cita_inicio=momento(17, 9),
        cita_estado="confirmada", seguimientos_fallidos=9, no_contactar=True,
        reactivaciones_ultimo_ano=99,
    )
    decision = s.decidir(fila, ahora=momento(16, 18), jornada=JORNADA, ultimo_mensaje=None)
    assert decision.accion == "enviar"
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest tests/test_reactivacion.py -v`
Expected: FAIL — `decidir()` no acepta los kwargs nuevos y ninguna de las guardas existe.

- [ ] **Step 3: Implementar en `decidir`**

Firma nueva (`seguimientos.py:182`):

```python
def decidir(
    fila: dict[str, Any],
    *,
    ahora: datetime,
    jornada: Jornada,
    ultimo_mensaje: datetime | None,
    ya_salio_a_ese_numero: bool = False,
    hora_vispera: int = HORA_VISPERA_POR_DEFECTO,
    max_reactivaciones_12m: int = MAX_REACTIVACIONES_12M,
    max_seguimientos_fallidos: int = MAX_SEGUIMIENTOS_FALLIDOS,
) -> Decision:
```

Constantes nuevas, junto a las demás (debajo de `MINUTOS_DE_CONTACTO_RECIENTE`, línea 148):

```python
#: La ventana de contacto reciente para una REACTIVACION. Mucho mas ancha que los 60 min del
#: recordatorio a proposito: un recordatorio de cita le sirve a quien escribio hace tres horas,
#: y un «hace unos dias nos escribio» a esa misma persona es lo que hace que conteste «??».
HORAS_DE_CONTACTO_RECIENTE_COMERCIAL = 24

#: Horario propio de la reactivacion, y NO la jornada de la clinica. Si MaxiCare abriera los
#: domingos, colgar de la jornada dejaria salir publicidad en domingo. Un recordatorio de cita
#: en domingo esta bien --la cita es real--; un «sigue interesada?» no.
HORA_APERTURA_COMERCIAL = 9
HORA_CIERRE_COMERCIAL = 19

#: Defaults de las perillas de la 021. Los vivos salen de `configuracion`.
MAX_REACTIVACIONES_12M = 6
MAX_SEGUIMIENTOS_FALLIDOS = 2
```

Y el bloque de guardas. **Va justo después de G0 y antes del bloque de cita**, porque todas son de reactivación y ninguna necesita mirar una cita:

```python
    es_reactivacion = fila.get("tipo") in TIPOS_DE_REACTIVACION

    # R1. El freno por persona. Antes que nada de lo demas: si esta apagado, no importa la hora
    # ni el retraso. El apagado es del SISTEMA --«a este numero no le sirve que lo
    # persigamos»-- y no es la baja, que es de la persona y ya la mira G0.
    if es_reactivacion and fila.get("seguimientos_fallidos", 0) >= max_seguimientos_fallidos:
        return Decision("anular", "seguimiento_apagado")

    # R2. El tope por persona y ano (regla 8). No es redundante con R1: el contador vuelve a 0
    # al agendar, asi que quien agenda cada vez lo esquiva siempre. Este es el techo.
    if es_reactivacion and fila.get("reactivaciones_ultimo_ano", 0) >= max_reactivaciones_12m:
        return Decision("anular", "tope_anual")

    # R3. El gemelo de G3 para lo que no tiene cita. G3 vive dentro de `if cita_id is not None`
    # y la reactivacion la atraviesa sin evaluarse: un proceso caido el viernes soltaria el
    # lunes todos los mensajes atrasados de golpe, «hace unos dias» sobre algo de hace una
    # semana. Un pico de mensajes viejos es lo que Meta castiga y lo que hace que la gente
    # reporte. Se anula y no se aplaza: el momento oportuno ya paso, y el barrido lo volvera a
    # encolar si la persona sigue calificando.
    if es_reactivacion and ahora - fila["fecha_objetivo"] > timedelta(
        hours=HORAS_DE_RETRASO_QUE_LO_INVALIDAN
    ):
        return Decision("anular", "llego_tarde")

    # R4. Horario propio (regla 6). NO cuelga de `jornada`: ver el comentario de
    # HORA_APERTURA_COMERCIAL. Aplaza a la proxima apertura comercial, no a la de la clinica.
    if es_reactivacion:
        fuera_de_hora = (
            ahora.hour < HORA_APERTURA_COMERCIAL or ahora.hour >= HORA_CIERRE_COMERCIAL
        )
        if ahora.weekday() == 6 or fuera_de_hora:
            return Decision(
                "aplazar", "fuera_de_horario_comercial", _proxima_apertura_comercial(ahora)
            )

    # R5. No pisarle la conversacion (regla 7). 24 h en vez de los 60 min de G6.
    if (
        es_reactivacion
        and ultimo_mensaje is not None
        and ahora - ultimo_mensaje < timedelta(hours=HORAS_DE_CONTACTO_RECIENTE_COMERCIAL)
    ):
        return Decision("anular", "hablo_hace_poco")
```

Y el ayudante, junto a `_proxima_apertura` (línea 302):

```python
def _proxima_apertura_comercial(ahora: datetime) -> datetime:
    """La siguiente franja 9:00-19:00 que no caiga en domingo.

    Deliberadamente NO mira la `Jornada`: el horario comercial es propio (ver
    HORA_APERTURA_COMERCIAL). Si la clinica cerrara un lunes festivo, un «sigue interesada?»
    ese lunes es inocuo; lo que no es inocuo es un domingo a las siete de la manana.
    """
    candidato = ahora
    if candidato.hour >= HORA_CIERRE_COMERCIAL:
        candidato = (candidato + timedelta(days=1)).replace(
            hour=HORA_APERTURA_COMERCIAL, minute=0, second=0, microsecond=0
        )
    elif candidato.hour < HORA_APERTURA_COMERCIAL:
        candidato = candidato.replace(
            hour=HORA_APERTURA_COMERCIAL, minute=0, second=0, microsecond=0
        )
    while candidato.weekday() == 6:
        candidato = (candidato + timedelta(days=1)).replace(
            hour=HORA_APERTURA_COMERCIAL, minute=0, second=0, microsecond=0
        )
    return candidato
```

- [ ] **Step 4: Añadir las dos columnas nuevas al SELECT del despachador**

`persistencia.py:1871-1892`. Sin esto las guardas leen `0` siempre y **pasan sin protegerse** — el modo «verde por el motivo equivocado» que avisa `pruebas.md`.

```sql
SELECT s.id, s.conversacion_id, s.cita_id, s.tipo, s.fecha_objetivo, s.intentos,
       COALESCE(c.telefono, cv.telefono)      AS telefono,
       c.nombre_completo, c.tratamiento, c.inicio AS cita_inicio,
       c.estado AS cita_estado, cv.tomada_por,
       COALESCE(co.no_contactar, FALSE)       AS no_contactar,
       COALESCE(co.seguimientos_fallidos, 0)  AS seguimientos_fallidos,
       (SELECT count(*)
          FROM seguimientos s2
          JOIN conversaciones cv2 ON cv2.id = s2.conversacion_id
         WHERE cv2.telefono = COALESCE(c.telefono, cv.telefono)
           AND s2.enviado_en IS NOT NULL
           AND s2.enviado_en > %(ahora)s - interval '12 months'
           AND s2.tipo <> 'recordatorio_cita')  AS reactivaciones_ultimo_ano
  FROM seguimientos s
  LEFT JOIN citas c           ON c.id  = s.cita_id
  LEFT JOIN conversaciones cv ON cv.id = s.conversacion_id
  LEFT JOIN contactos co      ON co.telefono = COALESCE(c.telefono, cv.telefono)
 WHERE s.enviado_en IS NULL
   AND s.anulado_en IS NULL
   AND s.fecha_objetivo <= %(ahora)s
 ORDER BY s.fecha_objetivo
 LIMIT %(limite)s
   FOR UPDATE OF s SKIP LOCKED
```

La subconsulta usa `%(ahora)s` y no `now()` a propósito: el reloj entra como parámetro en todo este módulo, y una prueba que fije `ahora` tiene que poder fijarlo también aquí. Cambia los parámetros posicionales por nombrados (`{"ahora": ahora, "limite": limite}`).

- [ ] **Step 5: Pasar las perillas desde `despachar`**

En `seguimientos.despachar`, junto a `hora_vispera` (línea 426-431):

```python
    max_12m = configuracion.get("max_reactivaciones_12m", MAX_REACTIVACIONES_12M)
    max_fallidos = configuracion.get("max_seguimientos_fallidos", MAX_SEGUIMIENTOS_FALLIDOS)
```

y en la llamada a `decidir(...)`:

```python
            decision = decidir(
                fila,
                ahora=momento_actual,
                jornada=jornada,
                ultimo_mensaje=ultimo_mensaje,
                ya_salio_a_ese_numero=telefono in numeros_de_esta_tanda,
                hora_vispera=hora_vispera,
                max_reactivaciones_12m=max_12m,
                max_seguimientos_fallidos=max_fallidos,
            )
```

Y en `persistencia.CONFIGURACION_POR_DEFECTO` (`persistencia.py:77-98`), para que `leer_configuracion` tenga default cuando la tabla aún no tiene las filas:

```python
    # La reactivacion (migracion 021). Mismo motivo que los de la 017: `leer_configuracion` los
    # usa cuando la tabla todavia no existe.
    "tope_diario_reactivacion": 20,
    "max_reactivaciones_12m": 6,
    "max_seguimientos_fallidos": 2,
```

- [ ] **Step 6: Correr y ver pasar**

Run: `uv run pytest tests/test_reactivacion.py -v`
Expected: PASS (todas)

Run: `uv run pytest -q`
Expected: PASS. Si `tests/test_seguimientos.py` construye filas sin las claves nuevas, los `.get(..., 0)` las cubren; si alguna falla por el cambio de parámetros posicionales a nombrados en la query, ajústala.

- [ ] **Step 7: Correr las de Neon — la query cambió**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: PASS. La subconsulta nueva es lo que se está verificando; un error de SQL solo aparece aquí.

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/seguimientos.py src/maxicare_daniela/persistencia.py tests/test_reactivacion.py
git commit -m "$(cat <<'EOF'
feat: las cinco guardas que la reactivacion no tenia

Cuatro guardas (G1, G2, G3, G3bis) viven dentro de `if cita_id is not None`, y un
seguimiento de reactivacion no tiene cita: las atravesaba las cuatro. La que
importaba es G3 --el proceso caido el viernes soltando el lunes todos los
mensajes atrasados de golpe, que es el pico que Meta castiga--.

Y tres reglas nuevas: horario propio 9-19 sin domingo (que NO cuelga de la
jornada de la clinica: si abriera los domingos, saldria publicidad en domingo),
contacto reciente de 24 h en vez de los 60 min del recordatorio, y un tope anual
por persona que el contador no puede poner porque se resetea al agendar.

El recordatorio de cita conserva su comportamiento entero: el apagado es
comercial, y quien tiene cita recibe su recordatorio aunque este apagado.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Despacho multiplantilla

`_parametros_del_recordatorio` está cableado a **una** plantilla de cuatro huecos. Las de reactivación llevan **uno** (`{{1}}` = nombre). Y la puerta `sin_plantilla` (`seguimientos.py:479-484`) anula hoy todo lo que no tiene `cita_inicio`.

**Files:**
- Modify: `src/maxicare_daniela/seguimientos.py:348-375` y `:479-503`
- Modify: `src/maxicare_daniela/config.py:420-433` y `:476-479`
- Modify: `src/maxicare_daniela/runtime.py:2044-2055`
- Test: `tests/test_reactivacion.py`

**Interfaces:**
- Produces: `seguimientos.parametros_de(fila) -> list[str]`, `despachar(..., plantillas: dict[str, str])`
- Consumes: `TIPOS_DE_REACTIVACION`

- [ ] **Step 1: Pruebas primero**

```python
# tests/test_reactivacion.py (añadir)

def test_la_reactivacion_manda_UN_hueco_y_es_el_nombre_de_pila():
    """Marketing quito el tratamiento a proposito: es un dato de salud y una notificacion se
    lee en la pantalla de bloqueo. Si esta funcion devolviera cuatro huecos, Meta rechaza el
    envio y ademas se filtraria."""
    parametros = s.parametros_de(fila_de_reactivacion(nombre_completo="Marcela Rios Gomez"))
    assert parametros == ["Marcela"]


def test_el_recordatorio_de_cita_sigue_mandando_sus_cuatro_huecos():
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1",
        cita_inicio=momento(17, 9), tratamiento="Limpieza",
    )
    assert s.parametros_de(fila) == ["Marcela", "jueves 17/9", "09:00", "Limpieza"]


def test_sin_plantilla_para_ese_tipo_no_se_manda_y_no_se_marca():
    """Si falta la plantilla de un tipo, esa fila NO se marca como enviada: se queda pendiente
    hasta que la plantilla exista. Marcarla la perderia para siempre."""
    import asyncio

    enviados: list[dict] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    recuento = asyncio.run(
        _despachar_con(
            [fila_de_reactivacion()],
            whatsapp=_WhatsAppFalso(),
            plantillas={s.TIPO_RECORDATORIO: "recordatorio_cita"},  # falta la de reactivacion
            ahora=momento(16, 11),
        )
    )
    assert enviados == []
    assert recuento["enviados"] == 0


def test_cada_tipo_usa_SU_plantilla():
    import asyncio

    enviados: list[dict] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    asyncio.run(
        _despachar_con(
            [fila_de_reactivacion()],
            whatsapp=_WhatsAppFalso(),
            plantillas={
                s.TIPO_RECORDATORIO: "recordatorio_cita",
                s.TIPO_SIN_AGENDAR: "reactivacion_sin_agendar",
            },
            ahora=momento(16, 11),
        )
    )
    assert enviados[0]["plantilla"] == "reactivacion_sin_agendar"
    assert enviados[0]["parametros"] == ["Marcela"]
```

Y el ayudante que monta el despacho con dobles, al principio del archivo (copia el patrón de `tests/test_seguimientos.py:485-530`):

```python
async def _despachar_con(filas, *, whatsapp, plantillas, ahora, monkeypatch=None):
    """Corre un ciclo de `despachar` con la base entera doblada.

    Se dobla `persistencia` y no la base: lo que se prueba es la DECISION y el reparto de
    plantillas, no el SQL. El SQL lo prueban las de `-m neon`.
    """
    import pytest as _pytest
    from maxicare_daniela import persistencia

    mp = monkeypatch or _pytest.MonkeyPatch()

    class _ConexionFalsa:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
        def close(self): pass

    mp.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    mp.setattr(persistencia, "leer_configuracion", lambda conn: {})
    mp.setattr(persistencia, "seguimientos_por_despachar", lambda conn, **k: list(filas))
    mp.setattr(persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None)
    mp.setattr(persistencia, "marcar_seguimiento_enviado", lambda conn, id_seguimiento: True)
    mp.setattr(persistencia, "anular_seguimiento", lambda conn, i, **k: None)
    mp.setattr(persistencia, "aplazar_seguimiento", lambda conn, i, **k: None)
    mp.setattr(persistencia, "anotar_recordatorio_en_conversacion", lambda conn, i, **k: None)
    mp.setattr(persistencia, "contar_enviados_hoy", lambda conn, **k: 0)
    try:
        return await s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=whatsapp,
            jornada=JORNADA,
            plantillas=plantillas,
            ahora=ahora,
        )
    finally:
        mp.undo()
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest tests/test_reactivacion.py -k plantilla -v`
Expected: FAIL — `parametros_de` no existe y `despachar` no acepta `plantillas`.

- [ ] **Step 3: Implementar el reparto de parámetros**

Renombrar `_parametros_del_recordatorio` a `parametros_de` (pública: la dobla un script) y bifurcar por tipo:

```python
def parametros_de(fila: dict[str, Any]) -> list[str]:
    """Los huecos de la plantilla de ESA fila, en el orden en que Meta los aprobo.

    Dos plantillas con distinto numero de huecos: el recordatorio de cita lleva cuatro y las
    tres de reactivacion llevan UNO. Mandar cuatro a una plantilla de uno no es un detalle
    cosmetico --Meta rechaza el envio-- y mandar el tratamiento a una de reactivacion seria
    ademas una filtracion: es un dato de salud y una notificacion de WhatsApp se lee en la
    pantalla de bloqueo. Marketing lo quito a proposito el 15/09/2026.
    """
    nombre = (fila.get("nombre_completo") or "").split(" ")[0] or "paciente"
    if fila.get("tipo") in TIPOS_DE_REACTIVACION:
        return [nombre]
    return _parametros_del_recordatorio(fila, nombre)
```

y `_parametros_del_recordatorio(fila, nombre)` queda con el cuerpo de hoy (líneas 367-375) recibiendo el nombre ya calculado, para que el `split` viva en un solo sitio. **Conserva su docstring entero**: documenta la conversión a Bogotá y por qué los huecos 2 y 3 son datos distintos.

- [ ] **Step 4: Implementar el despacho por tipo**

Firma de `despachar`: `plantilla: str` → `plantillas: dict[str, str]`. En el cuerpo, sustituir la puerta `sin_plantilla` (479-503) por:

```python
            # La plantilla de ESTE tipo. Antes habia una sola y todo lo que no tuviera
            # `cita_inicio` se anulaba con `sin_plantilla`; esa puerta es la que hoy hace
            # inofensivo el portillo de `tipo`, cerrado ya por la 021 y la tool.
            nombre_plantilla = plantillas.get(fila.get("tipo") or "")

            # Un recordatorio de cita sin `cita_inicio` es una fila rota, no una que espera
            # plantilla: sin la hora no hay con que rellenar los huecos 2 y 3.
            if fila.get("tipo") == TIPO_RECORDATORIO and fila.get("cita_inicio") is None:
                await asyncio.to_thread(
                    persistencia.anular_seguimiento, conn, fila["id"], motivo="sin_cita"
                )
                recuento["anulados"] += 1
                continue

            numeros_de_esta_tanda.add(telefono)

            # Sin plantilla configurada NO se marca ni se anula: la fila se queda pendiente
            # hasta que Meta apruebe. Marcarla la perderia para siempre, y anularla obligaria
            # al barrido a volver a decidir sobre alguien que ya califico.
            if not nombre_plantilla or whatsapp is None or not telefono:
                log.info(
                    "seguimiento %s (%s): sin plantilla o sin canal; se queda pendiente",
                    fila["id"], fila.get("tipo"),
                )
                continue
```

y en el envío, `plantilla=nombre_plantilla` y `parametros=parametros_de(fila)`.

- [ ] **Step 5: La configuración de las tres plantillas nuevas**

`config.py`, junto a `plantilla_recordatorio` (línea 424). Van como **campos con default vacío**, no como constantes de módulo: el nombre sale de una aprobación de Meta que todavía no existe, y la regla dura 3 dice que lo que no se sabe se marca, no se inventa.

```python
    #: Las tres de reactivacion. Vacias --su default-- dejan su tipo SIN enviar: el despachador
    #: decide igual y la fila se queda pendiente. Es el mismo modo de comprobacion que
    #: `plantilla_recordatorio`, y aqui es ademas el estado normal hasta que Meta apruebe.
    #: Los nombres exactos que hay que pedir estan en `docs/plantillas-meta-reactivacion.md`.
    plantilla_sin_agendar: str = ""
    plantilla_cancelada: str = ""
    plantilla_no_asistio: str = ""

    #: El interruptor de panico (regla 10). `0` apaga el BARRIDO entero sin redesplegar: deja
    #: de encolar gente nueva y todo lo demas --recordatorios de cita, atencion, relevo-- sigue
    #: igual. `!= "0"` y no `== "1"` porque el default es encendido, como `daniela_responde`.
    reactivacion_encendida: bool = True
```

en `desde_entorno`:

```python
            plantilla_sin_agendar=_opcional("MAXICARE_PLANTILLA_SIN_AGENDAR"),
            plantilla_cancelada=_opcional("MAXICARE_PLANTILLA_CANCELADA"),
            plantilla_no_asistio=_opcional("MAXICARE_PLANTILLA_NO_ASISTIO"),
            reactivacion_encendida=_opcional("MAXICARE_REACTIVACION", "1") != "0",
```

y en `.env.ejemplo`, las cuatro con su comentario.

- [ ] **Step 6: Pasar el diccionario desde `runtime`**

`runtime.py:2044-2055`:

```python
                plantillas={
                    seguimientos.TIPO_RECORDATORIO: config.plantilla_recordatorio,
                    seguimientos.TIPO_SIN_AGENDAR: config.plantilla_sin_agendar,
                    seguimientos.TIPO_CANCELADA: config.plantilla_cancelada,
                    seguimientos.TIPO_NO_ASISTIO: config.plantilla_no_asistio,
                },
```

Un valor vacío es una clave presente con `""`, y `if not nombre_plantilla` lo trata igual que ausente. Es deliberado: así el diccionario documenta los cuatro tipos aunque tres estén sin aprobar.

Y en `_arrancar_despacho_de_recordatorios` (2066-2077), avisar por cada plantilla que falte, no solo por la del recordatorio:

```python
    faltantes = [
        nombre for nombre, valor in (
            ("MAXICARE_PLANTILLA_RECORDATORIO", config.plantilla_recordatorio),
            ("MAXICARE_PLANTILLA_SIN_AGENDAR", config.plantilla_sin_agendar),
            ("MAXICARE_PLANTILLA_CANCELADA", config.plantilla_cancelada),
        ) if not valor
    ]
    if faltantes:
        log.warning(
            "sin plantilla para %s: el despachador decidira y NO enviara esos tipos. "
            "Es el modo de comprobacion; para enviar hace falta la aprobacion de Meta.",
            ", ".join(faltantes),
        )
```

- [ ] **Step 7: Correr todo**

Run: `uv run pytest tests/test_reactivacion.py -v` → PASS
Run: `uv run pytest -q` → PASS (`test_seguimientos.py` llama a `despachar(plantilla=...)`; actualiza esas llamadas a `plantillas={...}`)
Run: `uv run python scripts/probar_recordatorios.py` → OK (dobla `despachar` a mano; su firma cambió)

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/seguimientos.py src/maxicare_daniela/config.py src/maxicare_daniela/runtime.py .env.ejemplo tests/test_reactivacion.py scripts/probar_recordatorios.py
git commit -m "$(cat <<'EOF'
feat: el despachador manda la plantilla de cada tipo, no una sola

Cuatro huecos para el recordatorio de cita, UNO para las de reactivacion. El
tratamiento no viaja en las de reactivacion a proposito: es un dato de salud y
una notificacion se lee en la pantalla de bloqueo (marketing lo quito el 15/09).

Una plantilla sin configurar deja su fila PENDIENTE, no marcada ni anulada:
marcarla la perderia para siempre, y hoy tres de las cuatro estan sin aprobar.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: El contador — cuándo sube y cuándo baja

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py` (funciones nuevas)
- Modify: `src/maxicare_daniela/herramientas.py` (reset al agendar)
- Test: `tests/test_reactivacion_neon.py` (crear)

**Interfaces:**
- Produces: `persistencia.sumar_seguimiento_fallido(conn, telefono) -> int`, `persistencia.reiniciar_seguimientos_fallidos(conn, telefono, *, commit=True) -> None`, `persistencia.series_por_contabilizar(conn, *, ahora, dias_de_gracia) -> list[dict]`, `persistencia.marcar_serie_contabilizada(conn, ids) -> None`

- [ ] **Step 1: Pruebas primero (Neon)**

```python
# tests/test_reactivacion_neon.py
"""El contador contra la base de verdad. El SQL es lo que se prueba aqui."""
import os
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_reactivacion"

# (copiar aqui las cuatro fixtures de tests/test_contacto_neon.py:24-87 cambiando ESQUEMA
#  y el bloque de limpieza por: DELETE FROM seguimientos; DELETE FROM consentimientos;
#  DELETE FROM contactos; DELETE FROM conversaciones;)

AHORA = datetime(2026, 9, 16, 11, 0, tzinfo=ZONA_BOGOTA)
TELEFONO = "573001112233"


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
    """Mismo riesgo que la redaccion de `detalle`: un WHERE flojo apaga a toda la cartera."""
    otro = "573009998877"
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.asegurar_contacto(conexion_pruebas, otro)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    assert persistencia.leer_contacto(conexion_pruebas, otro)["seguimientos_fallidos"] == 0


def test_clearstate_resetea_el_contador_pero_no_la_baja(conexion_pruebas):
    """El contador es del SISTEMA y se va; `no_contactar` es de la persona y se queda.

    Si la baja se fuera, resetear a alguien lo devolveria a la lista de contactables sin que
    nadie se entere (no negociable 25).
    """
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.pedir_baja(conexion_pruebas, TELEFONO, origen="paciente")
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.borrar_rastro(conexion_pruebas, TELEFONO)
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto["seguimientos_fallidos"] == 0
    assert contacto["no_contactar"] is True
```

- [ ] **Step 2: Correr y ver fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_reactivacion_neon.py -v -m neon`
Expected: FAIL con `AttributeError: module 'maxicare_daniela.persistencia' has no attribute 'sumar_seguimiento_fallido'`

- [ ] **Step 3: Implementar las funciones**

En `persistencia.py`, en el bloque de contacto y consentimiento (junto a `pedir_baja`, ~línea 2608):

```python
def sumar_seguimiento_fallido(conn, telefono: str) -> int:
    """Suma uno al contador de series de seguimiento que no sirvieron. Devuelve el nuevo valor.

    NO es la baja y no se le parece: esto lo decide el sistema --«a este numero no le sirve que
    lo persigamos»-- y `no_contactar` lo decide la persona. Por eso esto vive solo en
    `contactos`, no escribe una linea en `consentimientos`, y `/clearstate` SI lo resetea.

    El `WHERE` va por telefono y no tiene vuelta atras: aflojarlo apaga el seguimiento de la
    cartera entera. `test_el_contador_no_alcanza_a_otro_telefono` lo vigila.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET seguimientos_fallidos = seguimientos_fallidos + 1,
                   ultimo_seguimiento_en = now(),
                   actualizado_en = now()
             WHERE telefono = %s
         RETURNING seguimientos_fallidos
            """,
            (telefono,),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0] if fila else 0


def reiniciar_seguimientos_fallidos(conn, telefono: str, *, commit: bool = True) -> None:
    """Devuelve el contador a cero. Lo llama `crear_cita`: alguien que ignoro dos veces y al
    final vino demostro lo contrario de lo que el contador supone.

    `commit=False` para que el reset viaje en la MISMA transaccion que la cita, igual que el
    recordatorio. Si la cita se deshace, el reset se deshace con ella.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET seguimientos_fallidos = 0, actualizado_en = now()
             WHERE telefono = %s AND seguimientos_fallidos > 0
            """,
            (telefono,),
        )
    if commit:
        conn.commit()
```

- [ ] **Step 4: El reset en `crear_cita`**

En `herramientas.py`, dentro de la transacción de `_crear_cita`, junto al `insertar_seguimiento` (línea 952-960):

```python
        # El contador vuelve a cero: agendar es justo la prueba de que el seguimiento SI
        # servia. Va con `commit=False` para que viaje en la misma transaccion que la cita:
        # si la cita se deshace, esto se deshace con ella.
        persistencia.reiniciar_seguimientos_fallidos(conn, ctx.telefono_completo, commit=False)
```

- [ ] **Step 5: `/clearstate` resetea el contador**

En `persistencia.borrar_rastro`, **dentro** del `UPDATE contactos` que ya resetea el aviso (líneas 2821-2829), añadiendo la columna y ampliando el comentario:

```python
                UPDATE contactos
                   SET aviso_mostrado_en = NULL, politica_version = NULL,
                       seguimientos_fallidos = 0, ultimo_seguimiento_en = NULL,
                       actualizado_en = now()
                 WHERE telefono = %(tel)s
```

El comentario de arriba ya explica por qué `no_contactar` no se toca; añade una frase: el contador **sí** se va, porque es del sistema y no de la persona — y tampoco entra en `borradas`, por lo mismo que el aviso.

- [ ] **Step 6: Correr**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_reactivacion_neon.py -v -m neon` → PASS
Run: `uv run pytest -q` → PASS
Run: `uv run python scripts/probar_tools.py` → OK (`crear_cita` cambió por dentro)

- [ ] **Step 7: Commit**

```bash
git add src/maxicare_daniela/persistencia.py src/maxicare_daniela/herramientas.py tests/test_reactivacion_neon.py
git commit -m "$(cat <<'EOF'
feat: el contador de seguimientos que no sirvieron, con su reset al agendar

El apagado es del sistema y la baja es de la persona: por eso esto vive solo en
`contactos`, no escribe en la bitacora de consentimientos, y `/clearstate` SI lo
resetea --al reves que `no_contactar`, que se queda pase lo que pase--.

El reset va en la MISMA transaccion que la cita: si la cita se deshace, el reset
se deshace con ella.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: «Ya no, gracias» cierra la serie

Hoy el botón llegaría como texto (`ingesta.py:166` ya lo convierte) y Daniela contestaría amablemente, pero el segundo mensaje saldría igual a los 7 días. Eso es lo que hace que alguien reporte: decir que no y que le vuelvan a escribir.

**Files:**
- Modify: `src/maxicare_daniela/herramientas.py` (tool nueva)
- Modify: `src/maxicare_daniela/persistencia.py` (la anulación por teléfono)
- Modify: `src/maxicare_daniela/agentes.py` (el prompt)
- Test: `tests/test_reactivacion.py`, `tests/test_reactivacion_neon.py`

**Interfaces:**
- Produces: `herramientas.cerrar_seguimiento` (tool), `persistencia.anular_reactivaciones_vivas(conn, telefono, *, motivo) -> int`

- [ ] **Step 1: Prueba primero**

```python
# tests/test_reactivacion.py (añadir)
def test_cerrar_seguimiento_anula_lo_pendiente_y_sube_el_contador():
    import asyncio
    from unittest.mock import MagicMock

    from maxicare_daniela import herramientas as h
    from tests.test_herramientas import contexto

    llamadas = {}

    def _anular(conn, telefono, *, motivo):
        llamadas["anular"] = (telefono, motivo)
        return 1

    def _sumar(conn, telefono):
        llamadas["sumar"] = telefono
        return 1

    ctx = contexto()
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(h.persistencia, "anular_reactivaciones_vivas", _anular)
        mp.setattr(h.persistencia, "sumar_seguimiento_fallido", _sumar)
        mp.setattr(h, "_con_base", lambda ctx, trabajo: trabajo(MagicMock()))
        asyncio.run(h._cerrar_seguimiento(ctx, "ya no me interesa"))

    assert llamadas["anular"][0] == ctx.telefono_completo
    assert llamadas["sumar"] == ctx.telefono_completo


def test_cerrar_seguimiento_NO_marca_la_baja():
    """D2: «ya no me interesa esta consulta» no es «no me escriban nunca mas».

    Marcar la baja aqui quema a un paciente por una frase que no dijo, y es lo unico de los
    dos que no se deshace sin que la persona lo pida.
    """
    import inspect

    from maxicare_daniela import herramientas as h

    fuente = inspect.getsource(h._cerrar_seguimiento)
    assert "pedir_baja" not in fuente
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest tests/test_reactivacion.py -k cerrar_seguimiento -v`
Expected: FAIL — `_cerrar_seguimiento` no existe

- [ ] **Step 3: Implementar la consulta**

En `persistencia.py`, junto a `anular_seguimientos_de_cita` (~línea 1796):

```python
def anular_reactivaciones_vivas(conn, telefono: str, *, motivo: str) -> int:
    """Anula los seguimientos de reactivacion pendientes de ESE telefono. Devuelve cuantos.

    Va por telefono y no por conversacion a proposito: la conversacion caduca a las 24 h y el
    segundo intento de una serie sale a los 7 dias, o sea desde otra conversacion. Colgarlo de
    `id_conversacion` dejaria vivo justo el mensaje que el paciente acaba de rechazar.

    NO toca `recordatorio_cita`: quien dice «ya no me interesa» a una reactivacion no esta
    renunciando al recordatorio de una cita que tiene agendada.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE seguimientos
               SET anulado_en = now(), motivo_anulacion = %(motivo)s
             WHERE id IN (
                    SELECT s.id FROM seguimientos s
                      JOIN conversaciones cv ON cv.id = s.conversacion_id
                     WHERE cv.telefono = %(tel)s
                       AND s.tipo <> 'recordatorio_cita'
                       AND s.enviado_en IS NULL
                       AND s.anulado_en IS NULL
             )
            """,
            {"tel": telefono, "motivo": motivo[:200]},
        )
        anulados = cur.rowcount
    conn.commit()
    return anulados
```

- [ ] **Step 4: Implementar la tool**

En `herramientas.py`, sección nueva junto a la de la baja (~línea 1995):

```python
async def _cerrar_seguimiento(ctx: ContextoDaniela, nota: str | None) -> str:
    """El nucleo. Anula lo pendiente y sube el contador. **Nunca marca la baja**: ver D2."""

    def trabajo(conn):
        anulados = persistencia.anular_reactivaciones_vivas(
            conn, ctx.telefono_completo, motivo="el_paciente_dijo_que_no"
        )
        persistencia.sumar_seguimiento_fallido(conn, ctx.telefono_completo)
        return anulados

    await _con_base(ctx, trabajo)
    return (
        "Anotado: no se le vuelve a escribir sobre esta consulta. Si tiene una cita agendada, "
        "su recordatorio le sigue llegando. Si lo que quiere es no recibir NINGUN mensaje "
        "comercial nunca mas, esa es otra cosa y se registra aparte."
    )


@function_tool(failure_error_function=_fallo_seguimiento)
async def cerrar_seguimiento(
    wrapper: RunContextWrapper[ContextoDaniela], nota: str
) -> str:
    """Cierra el seguimiento de esta consulta porque el paciente dijo que ya no le interesa.

    Úsala cuando responda que no a un mensaje de seguimiento nuestro, incluido el botón
    'Ya no, gracias'. NO la uses si lo que pide es no recibir ningún mensaje más: eso es la
    baja y tiene su propia herramienta.

    Args:
        nota: lo que dijo el paciente, en sus palabras.
    """
    return await _cerrar_seguimiento(wrapper.context, nota)
```

Registrarla en `TODAS` y en `__all__` (`herramientas.py:2072-2104`).

- [ ] **Step 5: El prompt — el «no» ambiguo cae hacia el lado barato**

En `agentes.py`, dentro del bloque que ya describe las herramientas de privacidad (~línea 94):

```
Si el paciente responde que no a un seguimiento nuestro --el botón «Ya no, gracias» o
cualquier forma de decirlo--, usa `cerrar_seguimiento`. Si pide no recibir NINGÚN mensaje más,
usa `registrar_no_contactar`.

Ante la duda entre las dos, usa `cerrar_seguimiento`. Un «no gracias» a secas casi siempre
significa esta consulta, no todas; y la baja es lo único de los dos que no se deshace sin que
la persona vuelva a pedirlo.
```

- [ ] **Step 6: Prueba de Neon para la anulación por teléfono**

```python
# tests/test_reactivacion_neon.py (añadir)
def test_cerrar_anula_el_segundo_intento_que_vive_en_otra_conversacion(conexion_pruebas):
    """La razon entera de que vaya por telefono.

    La serie empieza en la conversacion A; a los 7 dias esa conversacion ya caduco y el
    segundo intento sigue colgando de ella. Si la anulacion fuera por `id_conversacion` de la
    conversacion VIVA, no lo alcanzaria -- y el paciente que acaba de decir que no recibiria
    el mensaje siete dias despues.
    """
    conv_a = persistencia.asegurar_conversacion(conexion_pruebas, TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv_a, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(days=7), clave_idempotencia="k-2",
    )
    conv_b = persistencia.asegurar_conversacion(conexion_pruebas, TELEFONO)  # la viva, otra
    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no"
    )
    assert anulados == 1, "el segundo intento de la serie sobrevivio al «no»"
```

- [ ] **Step 7: Correr todo**

Run: `uv run pytest -q` → PASS
Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` → PASS
Run: `uv run python scripts/probar_tools.py` → OK (hay una tool más)

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/herramientas.py src/maxicare_daniela/persistencia.py src/maxicare_daniela/agentes.py tests/
git commit -m "$(cat <<'EOF'
feat: decir que no cierra la serie de verdad

El boton llegaba como texto y Daniela contestaba amablemente, pero el segundo
mensaje salia igual a los 7 dias. Decir que no y que te vuelvan a escribir es
exactamente lo que hace que alguien reporte el numero.

La anulacion va por TELEFONO y no por conversacion: la conversacion caduca a las
24 h y el segundo intento sale a los 7 dias, colgando de una conversacion que ya
murio. Por `id_conversacion` no lo alcanzaria.

Y no marca la baja. «Ya no me interesa esta consulta» no es «no me escriban nunca
mas», y el prompt manda el «no» ambiguo hacia el lado barato: lo unico de los dos
que no se deshace solo es la baja.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: El barrido — quién califica y quién entra en la cola

**Files:**
- Create: `src/maxicare_daniela/barrido.py`
- Modify: `src/maxicare_daniela/persistencia.py` (las dos consultas de cartera)
- Test: `tests/test_reactivacion_neon.py`

**Interfaces:**
- Consumes: `TIPOS_QUE_EL_BARRIDO_ENCOLA`, las perillas de la 021
- Produces: `barrido.encolar(*, database_url, ahora, tope_diario, encendido, calidad) -> dict[str, int]`, `canales.WhatsApp.calidad_del_numero() -> dict[str, str]`, `persistencia.leads_sin_agendar(conn, *, ahora, limite)`, `persistencia.leads_que_cancelaron(conn, *, ahora, limite)`, `persistencia.series_por_contabilizar(...)`

### Task 7.0: El centinela de calidad (regla 11)

**Verificado el 17/09/2026 contra el número de producción**, así que esto ya no es una
suposición. La llamada devolvió `{"quality_rating": "GREEN", "messaging_limit_tier":
"TIER_250", "status": "CONNECTED"}`. Ver D7 de la spec para por qué se pregunta en vez de
esperar al webhook `phone_number_quality_update`.

- [ ] **Step 7.0.1: Prueba primero**

```python
# tests/test_reactivacion.py (añadir)
@pytest.mark.parametrize(
    "calidad,debe_encolar",
    [("GREEN", True), ("YELLOW", False), ("RED", False), ("FLAGGED", False),
     ("UNKNOWN", True), ("NA", True), (None, False)],
)
def test_regla_11_la_calidad_del_numero_manda(calidad, debe_encolar):
    """`None` = la consulta fallo, y ahi NO se encola.

    Es la asimetria CONTRARIA a la del no negociable 26: alli, ante la duda se avisa porque
    molestar al doctor de mas es barato. Aqui lo barato es callarse -- el coste de no encolar
    durante una hora son leads, y el de encolar con la calidad en rojo es el numero.

    `UNKNOWN` y `NA` SI encolan: son los valores de un numero sin historial suficiente, no una
    senal de dano. Tratarlos como rojo dejaria el sistema apagado para siempre en una cuenta
    nueva, que es justo cuando mas falta hace.
    """
    from maxicare_daniela import barrido

    assert barrido.se_puede_encolar(calidad) is debe_encolar
```

- [ ] **Step 7.0.2: Correr y ver fallar**

Run: `uv run pytest tests/test_reactivacion.py -k regla_11 -v`
Expected: FAIL — no existe `barrido.se_puede_encolar`

- [ ] **Step 7.0.3: La consulta, en `canales.py`**

Junto a `enviar_plantilla` (`canales.py:205`):

```python
    async def calidad_del_numero(self) -> dict[str, str]:
        """Lo que Meta opina hoy de este numero. Una LECTURA: no cuesta ni gasta cupo.

        Se pregunta en vez de esperar al webhook `phone_number_quality_update` a proposito
        (D7): ese webhook exige suscribir el campo en la App Dashboard --configuracion externa
        que un despliegue no garantiza-- y sobre todo **se puede perder**. Si el servidor esta
        caido cuando Meta lo manda, Meta reintenta y desiste, y el sistema se queda creyendo
        que todo va bien justo cuando no va bien. Una consulta antes de cada barrido no se
        pierde.

        Mismo patron que `canales.aviso_sigue_puesto` (no negociable 26) y que
        `telegram.estado_del_tema` (19): lo que no emite evento, se sonda.

        Devuelve `{}` si la consulta falla. Quien llama tiene que tratar el diccionario vacio
        como «no se sabe», que aqui significa NO encolar.
        """
        url = f"{self._base}/{self._phone_number_id}"
        parametros = {"fields": "quality_rating,messaging_limit_tier,status"}
        try:
            async with httpx.AsyncClient(timeout=20.0) as cliente:
                respuesta = await cliente.get(
                    url, headers=self._cabeceras, params=parametros
                )
                respuesta.raise_for_status()
                return respuesta.json()
        except Exception:  # noqa: BLE001 -- un fallo aqui no puede tumbar el barrido
            log.exception("no se pudo leer la calidad del numero en Meta")
            return {}
```

Usa el mismo `self._base` / `self._cabeceras` que `enviar_plantilla` — **no los dupliques**;
si no existen como atributos, extráelos de `enviar_plantilla` en este mismo paso.

- [ ] **Step 7.0.4: La decisión, en `barrido.py`**

```python
#: Lo que Meta puede decir de un numero. `UNKNOWN` y `NA` son de un numero sin historial
#: suficiente, no una senal de dano: tratarlos como rojo dejaria el sistema apagado para
#: siempre en una cuenta nueva, que es cuando mas falta hace.
CALIDADES_QUE_DEJAN_ENCOLAR = frozenset({"GREEN", "UNKNOWN", "NA"})


def se_puede_encolar(quality_rating: str | None) -> bool:
    """`None` --la consulta fallo-- devuelve False. Ante la duda, no mandar."""
    if not quality_rating:
        return False
    return quality_rating.upper() in CALIDADES_QUE_DEJAN_ENCOLAR
```

- [ ] **Step 7.0.5: Engancharlo en `encolar`**

`calidad` entra como **parámetro** y no se consulta aquí dentro: `barrido.encolar` no habla con
la red, igual que `seguimientos.decidir` no habla con la base. Quien consulta es `runtime`, que
ya tiene el `_whatsapp` construido, y así una prueba puede fijar la calidad sin doblar httpx.

```python
    if not se_puede_encolar(calidad.get("quality_rating") if calidad else None):
        log.warning(
            "la calidad del numero es %s: no se encola nada en este ciclo",
            (calidad or {}).get("quality_rating", "desconocida"),
        )
        recuento["frenado_por_calidad"] = 1
        return recuento
```

- [ ] **Step 7.0.6: El aviso al General, una vez al día**

En `runtime`, cuando `recuento["frenado_por_calidad"]`: mandar al tema General el mensaje con
el `quality_rating` y el `messaging_limit_tier`. **Una vez cada 24 h y no cada hora** — un
aviso repetido cada hora se convierte en ruido y acaba ignorado, que es el mismo fallo que la
inundación del General del 16/09 (no negociable 26). Guarda el instante del último aviso en
una variable de módulo, como `_relevo_minutos`.

- [ ] **Step 7.0.7: Correr**

Run: `uv run pytest tests/test_reactivacion.py -k regla_11 -v` → PASS (7 casos)

- [ ] **Step 1: Prueba primero (Neon) — la regla 1, que es la más importante**

```python
# tests/test_reactivacion_neon.py (añadir)
def test_regla_1_no_califica_quien_nunca_escribio(conexion_pruebas):
    """La defensa mas fuerte que hay: solo a quien escribio PRIMERO.

    Una fila de `contactos` sin conversacion no puede existir hoy --`asegurar_contacto` corre
    dentro de `_leer_estado`, o sea al recibir un mensaje-- pero si algun dia alguien importa
    una cartera, esta prueba es lo unico que impide que salga a mensajes en frio.
    """
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)  # contacto sin conversacion
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_quien_ya_tiene_cita_futura(conexion_pruebas):
    """La condicion que comparten los tres tipos: a quien ya tiene hora no se le persigue."""
    # (crear conversacion + cita futura confirmada para TELEFONO)
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_antes_de_las_24h(conexion_pruebas):
    """Escribio hace dos horas: todavia esta en la conversacion, no es un lead perdido."""
    # (conversacion con ultimo mensaje a AHORA - 2 h)
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_si_califica_quien_pregunto_ayer_y_no_agendo(conexion_pruebas):
    # (conversacion con ultimo mensaje a AHORA - 30 h, sin cita)
    leads = persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50)
    assert [l["telefono"] for l in leads] == [TELEFONO]


def test_el_barrido_no_encola_dos_veces_al_mismo(conexion_pruebas):
    """La clave de idempotencia la arma el CODIGO. Dos pasadas seguidas dejan UNA fila."""
    from maxicare_daniela import barrido

    # (sembrar un lead que califique)
    primero = barrido.encolar(database_url=..., ahora=AHORA, tope_diario=20, encendido=True)
    segundo = barrido.encolar(database_url=..., ahora=AHORA, tope_diario=20, encendido=True)
    assert primero["encolados"] == 1
    assert segundo["encolados"] == 0


def test_el_interruptor_apaga_el_barrido_entero(conexion_pruebas):
    """Regla 10. Con `encendido=False` no se encola nada y no se toca la base."""
    from maxicare_daniela import barrido

    recuento = barrido.encolar(database_url=..., ahora=AHORA, tope_diario=20, encendido=False)
    assert recuento["encolados"] == 0


def test_el_tope_diario_corta(conexion_pruebas):
    """Regla 9: arranque lento. Meta castiga los picos."""
    from maxicare_daniela import barrido

    # (sembrar 5 leads que califiquen)
    recuento = barrido.encolar(database_url=..., ahora=AHORA, tope_diario=2, encendido=True)
    assert recuento["encolados"] == 2
```

- [ ] **Step 2: Correr y ver fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_reactivacion_neon.py -v -m neon`
Expected: FAIL — no existe `leads_sin_agendar` ni el módulo `barrido`

- [ ] **Step 3: Implementar las consultas de cartera**

En `persistencia.py`, bloque nuevo tras el de seguimientos:

```python
#: Quien pregunto y no agendo. Las cinco condiciones, y ninguna sobra:
#:
#: 1. tiene conversacion  -- **regla 1**: solo a quien escribio PRIMERO. Es la defensa mas
#:    fuerte que hay contra un reporte: la gente no denuncia a un negocio al que le escribio.
#: 2. su ultimo mensaje fue hace >= 24 h -- antes sigue dentro de la conversacion.
#: 3. no tiene cita FUTURA -- a quien ya tiene hora no se le persigue.
#: 4. no pidio la baja, y no esta apagado por el contador.
#: 5. no tiene ya un seguimiento vivo de este tipo -- sin esto, cada pasada encola otro.
_LEADS_SIN_AGENDAR = """
SELECT cv.telefono, cv.id AS conversacion_id, max(me.recibido_en) AS ultimo_mensaje
  FROM conversaciones cv
  JOIN mensajes_entrantes me ON me.conversacion_id = cv.id
  LEFT JOIN contactos co     ON co.telefono = cv.telefono
 WHERE COALESCE(co.no_contactar, FALSE) = FALSE
   AND COALESCE(co.seguimientos_fallidos, 0) < %(max_fallidos)s
   AND NOT EXISTS (
        SELECT 1 FROM citas c
         WHERE c.telefono = cv.telefono
           AND c.inicio > %(ahora)s
           AND c.estado <> 'cancelada'
   )
   AND NOT EXISTS (
        SELECT 1 FROM seguimientos s
          JOIN conversaciones cv2 ON cv2.id = s.conversacion_id
         WHERE cv2.telefono = cv.telefono
           AND s.tipo = 'reactivacion_sin_agendar'
           AND s.anulado_en IS NULL
           AND (s.enviado_en IS NULL OR s.enviado_en > %(ahora)s - interval '30 days')
   )
 GROUP BY cv.telefono, cv.id
HAVING max(me.recibido_en) <= %(ahora)s - interval '24 hours'
   AND max(me.recibido_en) >  %(ahora)s - interval '30 days'
 ORDER BY max(me.recibido_en) DESC
 LIMIT %(limite)s
"""
```

La ventana de 30 días del `HAVING` no es adorno: un «hace unos días nos escribió» sobre algo de hace tres meses es falso, y es de las cosas por las que la gente reporta.

`leads_que_cancelaron` es la misma forma sobre `citas` con `estado = 'cancelada'` y `NOT EXISTS` de cita futura.

**Verificar antes de escribirla:** que `mensajes_entrantes` tenga `conversacion_id` y `recibido_en`, y que `citas` tenga `telefono` (por el no negociable 13, la pertenencia va por teléfono). Si los nombres no coinciden, ajústalos — no inventes columnas.

- [ ] **Step 4: Implementar `barrido.py`**

```python
"""Quien entra en la cola de reactivacion.

Va aparte de `seguimientos.py` a proposito: aquel decide QUE hacer con una fila que ya esta en
la cola, y esto decide QUIEN entra. Son las dos mitades de la misma funcion y la unica que
consulta la cartera entera es esta.

**Este modulo no manda nada.** Solo escribe filas en `seguimientos`; quien decide y envia es
`seguimientos.despachar`, con sus guardas. Esa separacion es lo que permite encender el barrido
en produccion sin plantillas y ver a quien habria escrito antes de escribirle a nadie.
"""
```

```python
def encolar(
    *,
    database_url: str,
    ahora: datetime,
    tope_diario: int,
    encendido: bool,
    max_seguimientos_fallidos: int = 2,
    limite: int = 200,
) -> dict[str, int]:
    """Una pasada. Devuelve `{"encolados": n, "contabilizados": n}`.

    `encendido=False` es el interruptor de panico (regla 10): devuelve cero sin tocar la base.
    Va aqui y no en `runtime` para que una prueba pueda comprobarlo sin levantar FastAPI.
    """
    recuento = {"encolados": 0, "contabilizados": 0}
    if not encendido:
        return recuento

    conn = persistencia.conectar(database_url)
    try:
        # El tope diario (regla 9) cuenta lo YA enviado hoy, no lo encolado: lo que Meta ve son
        # envios. Encolar de mas y que el despachador aplace seria un pico igual de grande al
        # dia siguiente.
        enviados_hoy = persistencia.contar_enviados_hoy(conn, ahora=ahora)
        cupo = max(0, tope_diario - enviados_hoy)
        if cupo == 0:
            return recuento

        recuento["contabilizados"] = _contabilizar_series_cerradas(conn, ahora=ahora)

        for tipo, consulta in (
            (seguimientos.TIPO_SIN_AGENDAR, persistencia.leads_sin_agendar),
            (seguimientos.TIPO_CANCELADA, persistencia.leads_que_cancelaron),
        ):
            for lead in consulta(
                conn, ahora=ahora, limite=cupo,
                max_seguimientos_fallidos=max_seguimientos_fallidos,
            ):
                if recuento["encolados"] >= cupo:
                    break
                # La clave la arma el CODIGO, nunca el modelo, y lleva el DIA dentro: dos
                # pasadas el mismo dia no encolan dos veces, y la del mes que viene si.
                clave = ":".join(
                    [lead["conversacion_id"], "reactivacion", tipo, ahora.date().isoformat()]
                )
                if persistencia.insertar_seguimiento(
                    conn,
                    id_conversacion=lead["conversacion_id"],
                    tipo=tipo,
                    fecha_objetivo=ahora,
                    clave_idempotencia=clave,
                ):
                    recuento["encolados"] += 1
        return recuento
    finally:
        conn.close()
```

`_contabilizar_series_cerradas` sube el contador de quien recibió su segundo intento hace más de 7 días y no escribió después, y marca esas filas con `contabilizado_en` para no contarlas dos veces.

- [ ] **Step 5: Correr**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_reactivacion_neon.py -v -m neon` → PASS
Run: `uv run pytest -q` → PASS

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/barrido.py src/maxicare_daniela/persistencia.py tests/test_reactivacion_neon.py
git commit -m "$(cat <<'EOF'
feat: el barrido decide a quien escribirle

Va en su propio modulo: `seguimientos.py` decide QUE hacer con una fila que ya
esta en la cola, y esto decide QUIEN entra. Y el barrido no manda nada -- solo
encola--, asi que se puede encender en produccion sin plantillas y ver a quien
habria escrito antes de escribirle a nadie.

La regla 1 vive en la consulta: `JOIN mensajes_entrantes`. Solo califica quien
escribio PRIMERO, y una fila de `contactos` sin conversacion no entra. Hoy no
puede existir; el dia que alguien importe una cartera, esa junta es lo unico que
impide que salga a mensajes en frio.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: La tarea de fondo, con su interruptor

**Files:**
- Modify: `src/maxicare_daniela/runtime.py` (tarea nueva junto a las cuatro que hay)
- Test: `tests/test_reactivacion.py`

- [ ] **Step 1: Prueba primero**

```python
def test_el_barrido_no_arranca_sin_base():
    """Mismo patron que el despacho de recordatorios."""
    # comprobar que `_arrancar_barrido_de_reactivacion` devuelve sin crear tarea
```

- [ ] **Step 2: Implementar**

Copiar el patrón exacto de `_despachar_recordatorios_sin_parar` (`runtime.py:2028-2088`): constante de intervalo, **referencia global de módulo** (sin ella el GC se lleva la tarea y deja de correr sin un error en el log), `while True` con el `sleep` **al principio**, `except asyncio.CancelledError: raise` y `except Exception: log.exception(...)`, más su `@app.on_event("shutdown")` con `cancel()` + `await`.

```python
#: Una vez por hora. El barrido consulta la cartera entera y lo que busca cambia despacio: un
#: lead que califica a las 10:00 sigue calificando a las 11:00. Cada minuto --como el
#: despachador-- seria 60 consultas pesadas por cada una util.
SEGUNDOS_ENTRE_BARRIDOS_DE_REACTIVACION = 3600.0
```

En el startup, tres guard clauses: sin `database_url` no arranca; con `config.reactivacion_encendida` en `False` **no arranca y lo dice en el log**; y si no hay ninguna plantilla de reactivación configurada, arranca igual con un `log.warning` — encolar sin enviar es el modo de comprobación y es justo lo que hay que poder hacer mientras Meta aprueba.

- [ ] **Step 3: Correr y commitear**

Run: `uv run pytest -q` → PASS

```bash
git commit -m "feat: la tarea de fondo del barrido, con su interruptor"
```

---

## Task 9: El entregable

**Files:**
- Create: `scripts/probar_reactivacion.py`
- Modify: `CLAUDE.md` (la tabla de entregables), `.claude/rules/scripts-entregables.md`

- [ ] **Step 1: Escribir el script**

Empieza con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y marcadores ASCII (`OK` / `FALLA` / `->`) — la consola de Windows es cp1252 y el mismo script corre en el VPS.

**No gasta tokens y no manda nada.** Monta su propio esquema (`pruebas_reactivacion_script`), siembra los dos casos del diseño —Marcela y Andrés—, corre `barrido.encolar` y `seguimientos.despachar` con un WhatsApp doblado, e imprime la decisión de cada fila. Comprueba las once reglas una por una y marca la 11 como `POR VERIFICAR`.

- [ ] **Step 2: Correr**

Run: `uv run python scripts/probar_reactivacion.py`
Expected: OK en las diez reglas comprobables.

- [ ] **Step 3: Los seis que no gastan, porque cambiaron firmas**

Run: `uv run python scripts/probar_tools.py` → OK
Run: `uv run python scripts/probar_recordatorios.py` → OK
Run: `uv run python scripts/probar_relevo.py` → OK
Run: `uv run python scripts/probar_sin_resolver.py` → OK
Run: `uv run python scripts/probar_calendario.py` → OK
Run: `uv run python scripts/probar_reactivacion.py` → OK

- [ ] **Step 4: Documentar y commitear**

Añadir la fila a la tabla del `CLAUDE.md` (`probar_reactivacion.py` · el barrido y las once reglas · no gasta) y su apartado en `.claude/rules/scripts-entregables.md` diciendo qué dobla.

---

## Lo que queda fuera de este plan, y hay que decirlo al entregar

- **Nada sale hasta que Meta apruebe las tres plantillas.** El sistema queda entero y en modo comprobación: encola, decide, registra, y no manda. Encender es rellenar tres variables.
- ~~La regla 11 queda fuera~~ **ENTRA**, y por un camino mejor que el previsto: se pregunta la calidad en vez de esperar el webhook (tarea 7.0, D7 de la spec). Verificado contra el número real el 17/09/2026. Lo que **sí** queda fuera es reaccionar a `messaging_limit_tier`: hoy solo se informa en el aviso.
- **`reactivacion_no_asistio` queda declarada y sin disparador.** Nadie escribe `citas.asistio`.
- **La memoria del reencuentro** sigue fuera: Daniela reconoce a quien vuelve, no recuerda de qué hablaron.
- **Ninguna prueba garantiza que la gente no reporte.** Lo que garantizan es que ninguna de las diez reglas comprobables se puede saltar. El resto lo sostiene el procedimiento: correr el camino entero contra la API real antes de encender, y arrancar despacio.

## Self-review

- **Cobertura de la spec:** las once reglas tienen tarea — 1→T7, 2→ya existe (G7 + el barrido), 3→las plantillas (T4 + doc), 4→G0 con el vocabulario cerrado (T1/T2), 5→T3+T5, 6→T3, 7→T3, 8→T3, 9→T7, 10→T4+T8, **11→T7.0**. Las siete decisiones (D1–D7) están en T7, T6, T5, T5, T5/T6, el documento de plantillas y T7.0.
- **Tipos:** `parametros_de` (no `_parametros_del_recordatorio`) es el nombre público desde T4 y así se usa en T4 y T9. `plantillas: dict[str, str]` es el nombre del parámetro en T4, T8 y en `_despachar_con` de las pruebas. `sumar_seguimiento_fallido` devuelve `int` y así lo asume T3 y T6.
- **Hueco conocido:** las consultas de cartera de T7 asumen nombres de columna de `mensajes_entrantes` y `citas` que hay que **verificar antes de escribirlas** — está dicho en el paso 3 de esa tarea. Es el único sitio del plan que no pude fijar sin abrir esas tablas.

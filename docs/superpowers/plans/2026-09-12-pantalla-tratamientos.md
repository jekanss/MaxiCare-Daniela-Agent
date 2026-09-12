# Pantalla de Tratamientos — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que MaxiCare edite sus 73 fichas de conocimiento y cree tratamientos nuevos desde el navegador, sin que nadie despliegue código, y sin abrir el muro que separa el contenido clínico del paciente.

**Architecture:** El `Literal` de tratamientos se parte en dos: `LecturaArchivo` conserva el literal escrito a mano (el muro, con su prueba intacta) y el vocabulario de negocio pasa a un conjunto en memoria que `runtime.py` rellena desde una tabla nueva al arrancar y en cada cambio. Un módulo `panel.py` sin framework concentra las consultas y escrituras; `runtime.py` solo expone endpoints delgados con control de rol.

**Tech Stack:** Python 3.12 · uv · FastAPI 0.141 · psycopg 3 · Pydantic 2 · openai-agents 0.22.2 · pytest 9.1.1 (sin pytest-asyncio) · React 19 + Vite + Tailwind 4 · Neon Postgres

**Spec:** [`docs/superpowers/specs/2026-09-12-pantalla-tratamientos-design.md`](../specs/2026-09-12-pantalla-tratamientos-design.md)

## Global Constraints

- **El muro no se toca.** `tests/test_contratos.py::test_tratamiento_no_admite_una_frase_clinica` debe seguir verde, sin editarlo, al final de cada tarea.
- **`contratos.py` no importa base de datos ni framework.** Lo vigila `tests/test_estructura.py`. Igual para el nuevo `panel.py`.
- **Nunca se escribe código del SDK sin comprobar la versión instalada.** Verificada 0.22.2.
- **No se registran cédulas ni documentos de identidad.** Ninguna tabla ni formulario nuevo añade esa columna.
- **Los heredoc de Bash fallan en este entorno.** Para crear un archivo, usa la herramienta Write, nunca `cat > archivo <<'EOF'`.
- **La consola de Windows es cp1252.** Todo script bajo `scripts/` empieza con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y usa marcadores ASCII (`OK` / `FALLA` / `->`).
- **Las pruebas que tocan Neon llevan `@pytest.mark.neon`** y escriben en el esquema `pruebas`. Las offline no tocan red ni base.
- **No hay `pytest-asyncio`.** Una prueba de código asíncrono es una función normal que llama a `asyncio.run(...)`.
- **La ruta comodín `@app.get("/{ruta_completa:path}")` va siempre al final de `runtime.py`.** Toda ruta nueva se inserta antes.
- **Comandos:** pruebas offline `uv run pytest -q`; contra Neon `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`.
- **Atribución de commits:** cada mensaje termina con la línea `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

---

## Mapa de archivos

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `migraciones/007_bitacora_cambios.sql` | CREAR · registro de quién cambió qué | 1 |
| `migraciones/008_tratamientos.sql` | CREAR · el vocabulario, con su CHECK y su semilla | 1 |
| `src/maxicare_daniela/contratos.py` | MODIFICAR · vocabulario vivo; el muro intacto | 2 |
| `src/maxicare_daniela/herramientas.py` | MODIFICAR · `registrar_estado_oportunidad` usa el vocabulario | 3 |
| `src/maxicare_daniela/panel.py` | CREAR · consultas y escrituras del panel, sin framework | 4 |
| `src/maxicare_daniela/agentes.py` | MODIFICAR · instrucciones dinámicas con el vocabulario al final | 5 |
| `src/maxicare_daniela/runtime.py` | MODIFICAR · endpoints, `exigir_rol`, `detalle` en los errores | 6 |
| `web/src/api.ts` | MODIFICAR · tipos y llamadas del panel | 7 |
| `web/src/pantallas/Tratamientos.tsx` | CREAR · la pantalla | 7 |
| `web/src/componentes/Sidebar.tsx`, `web/src/App.tsx` | MODIFICAR · enrutar la sección | 7 |
| `scripts/probar_panel.py` | CREAR · el entregable verificable | 8 |
| `CLAUDE.md`, `docs/agentes/plan-agentes.json` | MODIFICAR · cierre | 9 |
| `tests/test_panel.py` | CREAR · pruebas del módulo y de los endpoints | 4, 6 |

---

## Task 1: Las dos migraciones

**Files:**
- Create: `migraciones/007_bitacora_cambios.sql`
- Create: `migraciones/008_tratamientos.sql`
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `persistencia.aplicar_esquema(conn)`, que aplica todo `migraciones/*.sql` en orden de nombre.
- Produces: tablas `cambios_configuracion` y `tratamientos` con los 14 tratamientos sembrados.

- [ ] **Step 1: Escribir `migraciones/007_bitacora_cambios.sql`**

Usa la herramienta Write. Sigue el estilo de las seis migraciones existentes: cabecera de comentario que explica la decisión, y SQL idempotente.

```sql
-- =========================================================================================
-- Bitácora de cambios del panel
--
-- Editar un precio desde la interfaz cambia lo que Daniela le cotiza a un paciente real al
-- instante, sin despliegue. Esta tabla es lo único que permite reconstruir qué decía antes
-- y quién lo cambió.
--
-- SIN clave foránea a `usuarios`, a propósito: si mañana alguien borra un usuario, el
-- registro de lo que cambió tiene que sobrevivir. Una bitácora que se borra con quien la
-- escribió no es una bitácora.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS cambios_configuracion (
    id             BIGSERIAL PRIMARY KEY,
    tabla          TEXT NOT NULL
                   CHECK (tabla IN ('base_conocimiento', 'tratamientos', 'configuracion')),

    -- 'implantes/precio' para una ficha, 'carillas' para un tratamiento.
    clave          TEXT NOT NULL,

    -- NULL significa que la fila no existía: fue una creación, no una edición.
    valor_anterior TEXT,
    valor_nuevo    TEXT NOT NULL,

    usuario        TEXT NOT NULL,
    cambiado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cambios_recientes
    ON cambios_configuracion (cambiado_en DESC);
```

- [ ] **Step 2: Escribir `migraciones/008_tratamientos.sql`**

```sql
-- =========================================================================================
-- El vocabulario de tratamientos -- editable por la clínica
--
-- Hasta ahora la lista vivía SOLO en el `Literal` de `contratos.py`, y agregar uno nuevo
-- exigía un cambio de código y un despliegue. La condición de revisión de la fase 1 --«si
-- el Literal de tratamientos resulta insuficiente»-- se disparó: no por un dato que
-- faltara, sino porque el cliente tiene que poder crecer sin nosotros.
--
-- Lo que esta tabla NO abre: `LecturaArchivo.tratamiento`, que sigue con el Literal escrito
-- a mano. Ese es el muro entre el contenido clínico y el paciente, y no se abre desde una
-- pantalla. Un tratamiento creado aquí se puede cotizar y agendar; una radiografía sobre él
-- se clasifica `no_identificado` hasta que se incorpore al Literal.
--
-- La `clave` la restringe Postgres y no solo Python: es el string que termina dentro de
-- `citas.tratamiento` y en los argumentos que ve el modelo. Una frase clínica no pasa el
-- CHECK.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS tratamientos (
    clave      TEXT PRIMARY KEY CHECK (clave ~ '^[a-z][a-z0-9_]{2,23}$'),
    etiqueta   TEXT        NOT NULL,
    activo     BOOLEAN     NOT NULL DEFAULT TRUE,
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    creado_por TEXT
);

-- Los catorce del Literal. `ON CONFLICT DO NOTHING` para no pisar una etiqueta que la
-- clínica ya haya editado.
INSERT INTO tratamientos (clave, etiqueta, creado_por) VALUES
    ('cordales',        'Cordales',              'semilla'),
    ('implantes',       'Implantes',             'semilla'),
    ('blanqueamiento',  'Blanqueamiento',        'semilla'),
    ('ortodoncia',      'Ortodoncia',            'semilla'),
    ('diseno_sonrisa',  'Diseño de sonrisa',     'semilla'),
    ('microdiseno',     'Microdiseño',           'semilla'),
    ('coronas',         'Coronas',               'semilla'),
    ('periodoncia',     'Periodoncia',           'semilla'),
    ('gingivectomia',   'Gingivectomía',         'semilla'),
    ('bichectomia',     'Bichectomía',           'semilla'),
    ('limpieza',        'Limpieza',              'semilla'),
    ('endodoncia',      'Endodoncia',            'semilla'),
    ('protesis',        'Prótesis',              'semilla'),
    ('no_identificado', 'Sin identificar',       'semilla')
ON CONFLICT (clave) DO NOTHING;
```

- [ ] **Step 3: Escribir la prueba que falla**

Crea `tests/test_panel.py` con la cabecera del módulo y esta primera prueba.

**Importante:** no pongas ningún `os.environ[...] = ...` en el cuerpo del módulo. `pytest` importa todos los módulos de prueba antes de ejecutar ninguno, y eso le cambiaría el entorno a las demás — pasó de verdad con `test_web.py`, está documentado en `.claude/rules/pruebas.md`.

```python
"""Las pruebas del panel: el vocabulario de tratamientos y la edición de fichas.

Las que tocan Neon llevan `@pytest.mark.neon` y escriben en el esquema `pruebas`.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from maxicare_daniela import persistencia


def _url_de_pruebas() -> str:
    """La conexión DIRECTA con `search_path=pruebas`. El pooler de Neon rechaza `options`
    como parámetro de arranque, así que hay que quitarle el `-pooler.` al host."""
    url = os.environ.get("MAXICARE_DATABASE_URL", "")
    directa = url.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return f"{directa}{sep}options=-csearch_path%3Dpruebas"


@pytest.fixture
def conn():
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
```

- [ ] **Step 4: Correr la prueba y verla fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py`
Expected: FAIL — `relation "tratamientos" does not exist` (las migraciones existen pero el esquema `pruebas` aún no las tenía) o error de recolección si falta el archivo.

Si falla con `KeyError`/cadena vacía en `MAXICARE_DATABASE_URL`, el problema es que no cargaste `.env`: corre con `MAXICARE_PRUEBAS_NEON=1` desde la raíz del proyecto, donde el `conftest.py` ya lo carga.

- [ ] **Step 5: Correr la prueba y verla pasar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py`
Expected: PASS, 2 passed

- [ ] **Step 6: Verificar que la suite offline sigue entera**

Run: `uv run pytest -q`
Expected: 216 passed, 14 skipped (las 2 nuevas se saltan sin la variable)

- [ ] **Step 7: Commit**

```bash
git add migraciones/007_bitacora_cambios.sql migraciones/008_tratamientos.sql tests/test_panel.py
git commit -m "Migraciones 007 y 008: bitacora de cambios y tabla de tratamientos

La clave de un tratamiento la restringe Postgres con un CHECK, no solo Python:
es el string que termina en citas.tratamiento y en los argumentos del modelo.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: El vocabulario vivo, con el muro intacto

**Files:**
- Modify: `src/maxicare_daniela/contratos.py`
- Test: `tests/test_contratos.py`

**Interfaces:**
- Consumes: nada de tareas anteriores.
- Produces:
  - `contratos.vocabulario() -> frozenset[str]`
  - `contratos.fijar_vocabulario(claves: Iterable[str]) -> None`
  - `SolicitudCita.tratamiento` pasa de `Tratamiento` a `str` validado contra `vocabulario()`.
  - `contratos.Tratamiento` (el `Literal`) **no cambia**.

- [ ] **Step 1: Escribir las pruebas que fallan**

Añade al final de `tests/test_contratos.py`. Las importaciones que falten van arriba, con las que ya hay.

```python
def test_el_muro_no_se_abre_con_el_vocabulario():
    """LA PRUEBA QUE SOSTIENE TODA LA DECISIÓN.

    Meter un tratamiento nuevo en el vocabulario de negocio lo hace cotizable y agendable.
    NO lo hace válido en `LecturaArchivo`, que es contenido clínico que cruza hacia el
    paciente. Si esta prueba empieza a fallar, la pantalla de tratamientos abrió el muro.
    """
    contratos.fijar_vocabulario(["carillas", "implantes"])
    try:
        # Sí se puede agendar.
        SolicitudCita(
            telefono="573001112233",
            nombre_completo="Maria Rodriguez",
            inicio=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
            tratamiento="carillas",
            clave_idempotencia="573001112233-2026-10-01T09:00",
        )
        # No se puede clasificar un documento clínico con él.
        with pytest.raises(ValidationError):
            LecturaArchivo(**(REMISION_DE_MARIA | {"tratamiento": "carillas"}))
    finally:
        contratos.fijar_vocabulario(get_args(contratos.Tratamiento))


def test_el_vocabulario_arranca_con_los_catorce_del_literal():
    """Sin base de datos, sin `runtime`, sin nada: importar el módulo basta. Es lo que hace
    que las pruebas offline y los scripts sigan funcionando igual."""
    assert contratos.vocabulario() == frozenset(get_args(contratos.Tratamiento))


def test_una_cita_con_un_tratamiento_fuera_del_vocabulario_se_rechaza():
    with pytest.raises(ValidationError):
        SolicitudCita(
            telefono="573001112233",
            nombre_completo="Maria Rodriguez",
            inicio=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
            tratamiento="lo_que_sea",
            clave_idempotencia="573001112233-2026-10-01T09:00",
        )
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest -q tests/test_contratos.py -k "muro_no_se_abre or vocabulario"`
Expected: FAIL — `AttributeError: module 'maxicare_daniela.contratos' has no attribute 'fijar_vocabulario'`

- [ ] **Step 3: Implementar el vocabulario en `contratos.py`**

Justo **debajo** de la definición de `Tratamiento` (que no se toca), añade:

```python
# ---------------------------------------------------------------------------------------
# El vocabulario vivo
# ---------------------------------------------------------------------------------------
#
# `Tratamiento` (arriba) hace dos trabajos distintos, y solo uno es de seguridad:
#
#   - En `LecturaArchivo.tratamiento` es EL MURO. Contenido clínico que cruza hacia el
#     paciente. Cerrado, escrito a mano, y no se abre desde ninguna pantalla.
#   - En `SolicitudCita` y en `registrar_estado_oportunidad` es vocabulario de negocio: qué
#     tratamientos ofrece la clínica hoy. Eso cambia sin que cambie nada de seguridad.
#
# Este conjunto es la segunda mitad. Arranca con los catorce del Literal --así las pruebas
# offline y los scripts no necesitan base de datos-- y `runtime.py` lo reemplaza al arrancar
# con los tratamientos activos de la tabla `tratamientos`.
#
# Por qué un módulo global y no una consulta: `contratos.py` NO puede importar la base de
# datos. Rompería las pruebas offline, los scripts, y la frontera que vigila
# `tests/test_estructura.py`.

_VOCABULARIO: frozenset[str] = frozenset(get_args(Tratamiento))


def vocabulario() -> frozenset[str]:
    """Los tratamientos que la clínica ofrece ahora mismo."""
    return _VOCABULARIO


def fijar_vocabulario(claves: Iterable[str]) -> None:
    """Reemplaza el vocabulario. Lo llama `runtime.py` al arrancar y cada vez que la pantalla
    crea o desactiva un tratamiento.

    `no_identificado` se añade siempre: no es un tratamiento que se ofrezca, es el valor que
    usa el sistema cuando no sabe de cuál se trata, y quitarlo rompería el registro de
    oportunidad de cualquier conversación que todavía no tenga claro qué busca el paciente.
    """
    global _VOCABULARIO
    limpias = {c.strip().lower() for c in claves if c and c.strip()}
    _VOCABULARIO = frozenset(limpias | {"no_identificado"})
```

Añade a los imports de arriba: `from typing import Any, Iterable, Literal, get_args`.

- [ ] **Step 4: Cambiar `SolicitudCita.tratamiento`**

En `SolicitudCita` (alrededor de la línea 298), reemplaza el campo y añade su validador junto al de `nombre_completo`:

```python
    tratamiento: str = Field(
        description=(
            "El tratamiento para el que se agenda. Tiene que ser uno de los que la clínica "
            "ofrece hoy; la lista viva va al final de las instrucciones del agente."
        ),
    )
```

```python
    @field_validator("tratamiento")
    @classmethod
    def _tratamiento_del_vocabulario(cls, v: str) -> str:
        # Se valida contra la lista viva, no contra el Literal: la clínica agrega
        # tratamientos desde la interfaz web sin que nadie despliegue código. El muro sigue
        # siendo el Literal, y vive en `LecturaArchivo`, no aquí.
        clave = v.strip().lower()
        if clave not in vocabulario():
            raise ValueError(
                f"'{v}' no es un tratamiento que MaxiCare ofrezca. "
                f"Los actuales son: {', '.join(sorted(vocabulario()))}."
            )
        return clave
```

- [ ] **Step 5: Correr las pruebas**

Run: `uv run pytest -q tests/test_contratos.py`
Expected: PASS, incluidas `test_tratamiento_no_admite_una_frase_clinica` y `test_tratamiento_solo_admite_los_catorce_del_vocabulario`, **que no se editaron**.

- [ ] **Step 6: Correr la suite entera**

Run: `uv run pytest -q`
Expected: todo verde. Si algo falla en `test_herramientas.py`, es la Tarea 3 — anótalo y sigue; no debilites ninguna prueba para que pase.

- [ ] **Step 7: Commit**

```bash
git add src/maxicare_daniela/contratos.py tests/test_contratos.py
git commit -m "Separa el vocabulario de tratamientos del muro

El Literal hacia dos trabajos distintos y solo uno era de seguridad.
LecturaArchivo lo conserva escrito a mano, con su prueba intacta; SolicitudCita
pasa a validar contra la lista viva que la clinica edita.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `registrar_estado_oportunidad` usa el vocabulario

**Files:**
- Modify: `src/maxicare_daniela/herramientas.py:724-752`
- Test: `tests/test_herramientas.py`

**Interfaces:**
- Consumes: `contratos.vocabulario()` de la Tarea 2.
- Produces: la tool acepta cualquier tratamiento activo; rechaza los demás con un mensaje que el modelo puede leer.

- [ ] **Step 1: Escribir la prueba que falla**

Añade a `tests/test_herramientas.py`. Sigue la convención: se llama a la función con guion bajo, no al `FunctionTool`.

```python
def test_registrar_estado_acepta_un_tratamiento_nuevo_del_vocabulario(ctx_doble):
    contratos.fijar_vocabulario(["carillas", "implantes"])
    try:
        salida = asyncio.run(
            herramientas._registrar_estado_oportunidad(
                ctx_doble, estado="explorando", barrera="ninguna",
                tratamiento="carillas", fuera_de_alcance=False, notas=None,
            )
        )
        assert "carillas" in salida or "OK" in salida.upper()
    finally:
        contratos.fijar_vocabulario(get_args(contratos.Tratamiento))


def test_registrar_estado_rechaza_lo_que_no_esta_en_el_vocabulario(ctx_doble):
    with pytest.raises(ValueError, match="no es un tratamiento"):
        asyncio.run(
            herramientas._registrar_estado_oportunidad(
                ctx_doble, estado="explorando", barrera="ninguna",
                tratamiento="lo_que_sea", fuera_de_alcance=False, notas=None,
            )
        )
```

Ajusta los nombres de los argumentos de `_registrar_estado_oportunidad` a la firma real que veas en el archivo, y usa el doble de contexto que ya exista en ese módulo de pruebas (mismo patrón que las otras pruebas de tools).

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest -q tests/test_herramientas.py -k registrar_estado`
Expected: FAIL — la validación del `Literal` rechaza `carillas` antes de llegar al cuerpo.

- [ ] **Step 3: Implementar**

En `herramientas.py`, la `@function_tool` `registrar_estado_oportunidad` (línea ~724): cambia el tipo del parámetro `tratamiento` de `Tratamiento` a `str` y valida al entrar en la función con guion bajo:

```python
    clave = (tratamiento or "").strip().lower()
    if clave not in contratos.vocabulario():
        raise ValueError(
            f"'{tratamiento}' no es un tratamiento que MaxiCare ofrezca. "
            f"Los actuales son: {', '.join(sorted(contratos.vocabulario()))}."
        )
```

Actualiza el docstring del argumento para que el modelo sepa de dónde sale la lista:

```
        tratamiento: de qué trata la conversación, o 'no_identificado'. Uno de los
            tratamientos que MaxiCare ofrece; la lista va al final de tus instrucciones.
```

Deja de importar `Tratamiento` si ya no se usa en el módulo.

- [ ] **Step 4: Correr las pruebas**

Run: `uv run pytest -q tests/test_herramientas.py`
Expected: PASS

- [ ] **Step 5: Suite entera**

Run: `uv run pytest -q`
Expected: todo verde.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/herramientas.py tests/test_herramientas.py
git commit -m "registrar_estado_oportunidad valida contra el vocabulario vivo

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `panel.py`

**Files:**
- Create: `src/maxicare_daniela/panel.py`
- Modify: `tests/test_estructura.py:32-52`
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `persistencia.conectar`, `contratos.Tratamiento`, `contratos.vocabulario`.
- Produces:
  - `validar_clave(clave: str) -> str` — pura, sin base de datos.
  - `listar_tratamientos(conn) -> list[dict]` — claves `clave`, `etiqueta`, `activo`, `en_el_muro`, `fichas`, `faltan`.
  - `crear_tratamiento(conn, *, clave, etiqueta, usuario) -> dict`
  - `cambiar_tratamiento(conn, clave, *, etiqueta=None, activo=None, usuario) -> dict`
  - `vocabulario_activo(conn) -> list[str]`
  - `listar_conocimiento(conn) -> list[dict]`
  - `guardar_ficha(conn, *, tratamiento, concepto, contenido, aprobado, nota_pendiente, usuario) -> dict`
  - `historial(conn, limite: int = 100) -> list[dict]`

- [ ] **Step 1: Escribir las pruebas que fallan**

Añade a `tests/test_panel.py`. La primera es offline; las demás llevan `@pytest.mark.neon`.

```python
from maxicare_daniela import contratos, panel

CORE = ("precio", "duracion", "profesional")


def test_validar_clave_rechaza_lo_que_no_es_una_clave():
    """Offline: la misma regla que el CHECK de Postgres, para poder dar un mensaje decente
    antes de llegar a la base."""
    for mala in ["Carillas", "carillas esteticas", "absceso-periapical", "ca", "1carillas"]:
        with pytest.raises(ValueError):
            panel.validar_clave(mala)
    assert panel.validar_clave("  Carillas  ".strip().lower()) == "carillas"
    assert panel.validar_clave("carillas_esteticas") == "carillas_esteticas"


@pytest.mark.neon
def test_listar_tratamientos_dice_cuales_estan_en_el_muro(conn):
    filas = {f["clave"]: f for f in panel.listar_tratamientos(conn)}
    assert filas["implantes"]["en_el_muro"] is True
    panel.crear_tratamiento(conn, clave="carillas", etiqueta="Carillas", usuario="prueba")
    filas = {f["clave"]: f for f in panel.listar_tratamientos(conn)}
    assert filas["carillas"]["en_el_muro"] is False


@pytest.mark.neon
def test_listar_tratamientos_cuenta_lo_que_falta(conn):
    filas = {f["clave"]: f for f in panel.listar_tratamientos(conn)}
    assert set(filas["endodoncia"]["faltan"]) >= set(CORE)
    assert filas["endodoncia"]["fichas"] == 0


@pytest.mark.neon
def test_guardar_ficha_y_su_bitacora_son_una_sola_transaccion(conn):
    panel.guardar_ficha(
        conn, tratamiento="implantes", concepto="precio",
        contenido="$1.900.000 la fase quirurgica", aprobado=True,
        nota_pendiente=None, usuario="dra.prueba",
    )
    registros = panel.historial(conn, limite=5)
    assert registros[0]["clave"] == "implantes/precio"
    assert registros[0]["usuario"] == "dra.prueba"
    assert registros[0]["valor_nuevo"].startswith("$1.900.000")


@pytest.mark.neon
def test_crear_un_tratamiento_que_ya_existe_no_lo_pisa(conn):
    with pytest.raises(ValueError, match="ya existe"):
        panel.crear_tratamiento(conn, clave="implantes", etiqueta="Otro", usuario="prueba")


@pytest.mark.neon
def test_desactivar_lo_saca_del_vocabulario_pero_no_de_la_tabla(conn):
    panel.cambiar_tratamiento(conn, "bichectomia", activo=False, usuario="prueba")
    assert "bichectomia" not in panel.vocabulario_activo(conn)
    assert any(f["clave"] == "bichectomia" for f in panel.listar_tratamientos(conn))
    panel.cambiar_tratamiento(conn, "bichectomia", activo=True, usuario="prueba")
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest -q tests/test_panel.py::test_validar_clave_rechaza_lo_que_no_es_una_clave`
Expected: FAIL — `ModuleNotFoundError: No module named 'maxicare_daniela.panel'`

- [ ] **Step 3: Escribir `src/maxicare_daniela/panel.py`**

Usa la herramienta Write. Sigue el estilo de `persistencia.py`: docstring de módulo que explica la decisión, funciones con docstring que dice el porqué y no el qué.

```python
"""Las consultas y escrituras del panel web.

No va en `persistencia.py` porque ese módulo ya tiene 736 líneas y es el que usan las nueve
tools de Daniela; lo del panel es otra responsabilidad y cambia por otras razones.

Como `persistencia.py`, este módulo NO importa `runtime.py` ni un framework web: recibe una
conexión y devuelve diccionarios. Lo vigila `tests/test_estructura.py`.

La regla que atraviesa todo el archivo: **ningún cambio se escribe sin su registro en la
bitácora, y los dos van en la misma transacción.** Editar un precio aquí cambia lo que
Daniela le cotiza a un paciente real al instante, sin despliegue y sin vuelta atrás; la
bitácora es lo único que permite reconstruir qué decía antes y quién lo cambió.
"""

from __future__ import annotations

import re
from typing import Any, get_args

from .contratos import Tratamiento

#: La misma regla que el CHECK de `migraciones/008_tratamientos.sql`. Está duplicada a
#: propósito: Postgres es la garantía, y esta copia existe solo para dar un mensaje legible
#: antes de que la base rechace la fila con un error que nadie entiende.
CLAVE = re.compile(r"^[a-z][a-z0-9_]{2,23}$")

#: Los conceptos que hacen útil una ficha. Si faltan, la pantalla lo muestra.
CONCEPTOS_MINIMOS = ("precio", "duracion", "profesional")


def validar_clave(clave: str) -> str:
    """La clave de un tratamiento, o un error que se puede leer.

    Es lo que termina dentro de `citas.tratamiento` y en los argumentos que ve el modelo,
    así que no admite espacios, mayúsculas ni frases: minúsculas, números y guion bajo, de 3
    a 24 caracteres.
    """
    limpia = (clave or "").strip().lower()
    if not CLAVE.match(limpia):
        raise ValueError(
            f"'{clave}' no sirve como clave. Usa minúsculas sin espacios ni tildes, de 3 a "
            "24 caracteres; por ejemplo 'carillas' o 'carillas_esteticas'."
        )
    return limpia


def _anotar(cur, *, tabla: str, clave: str, anterior: str | None, nuevo: str, usuario: str) -> None:
    """Escribe la bitácora. Se llama SIEMPRE dentro de la misma transacción que el cambio."""
    cur.execute(
        "INSERT INTO cambios_configuracion (tabla, clave, valor_anterior, valor_nuevo, usuario) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tabla, clave, anterior, nuevo, usuario),
    )


# ------------------------------------------------------------------------------------------
# Tratamientos
# ------------------------------------------------------------------------------------------


def listar_tratamientos(conn) -> list[dict[str, Any]]:
    """Cada tratamiento con lo que la pantalla necesita para decir la verdad sobre él.

    `en_el_muro` se CALCULA del `Literal` real, no se guarda en una columna. Una columna
    sería una segunda fuente de verdad que se desincroniza en silencio el día que alguien
    edite una y no la otra.
    """
    del_muro = set(get_args(Tratamiento))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.clave, t.etiqueta, t.activo, "
            "       coalesce(k.n, 0) AS fichas, "
            "       coalesce(k.conceptos, ARRAY[]::text[]) AS conceptos "
            "  FROM tratamientos t "
            "  LEFT JOIN (SELECT tratamiento, count(*) AS n, "
            "                    array_agg(concepto) AS conceptos "
            "               FROM base_conocimiento GROUP BY tratamiento) k "
            "    ON k.tratamiento = t.clave "
            " ORDER BY t.activo DESC, t.clave"
        )
        filas = cur.fetchall()

    salida = []
    for clave, etiqueta, activo, fichas, conceptos in filas:
        tiene = set(conceptos or [])
        salida.append({
            "clave": clave,
            "etiqueta": etiqueta,
            "activo": activo,
            "en_el_muro": clave in del_muro,
            "fichas": fichas,
            "faltan": [c for c in CONCEPTOS_MINIMOS if c not in tiene],
        })
    return salida


def vocabulario_activo(conn) -> list[str]:
    """Lo que `contratos.fijar_vocabulario` necesita. Solo los activos."""
    with conn.cursor() as cur:
        cur.execute("SELECT clave FROM tratamientos WHERE activo ORDER BY clave")
        return [f[0] for f in cur.fetchall()]


def crear_tratamiento(conn, *, clave: str, etiqueta: str, usuario: str) -> dict[str, Any]:
    """Un tratamiento nuevo, cotizable y agendable desde ya.

    Lo que NO hace, y por eso la pantalla lo avisa: no lo mete en el `Literal` de
    `LecturaArchivo`. Una radiografía sobre este tratamiento se seguirá clasificando como
    `no_identificado` hasta que alguien lo incorpore al muro con un cambio de código.
    """
    limpia = validar_clave(clave)
    nombre = (etiqueta or "").strip()
    if not nombre:
        raise ValueError("El tratamiento necesita un nombre visible.")

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tratamientos WHERE clave = %s", (limpia,))
        if cur.fetchone():
            raise ValueError(f"'{limpia}' ya existe.")
        cur.execute(
            "INSERT INTO tratamientos (clave, etiqueta, creado_por) VALUES (%s, %s, %s)",
            (limpia, nombre, usuario),
        )
        _anotar(cur, tabla="tratamientos", clave=limpia, anterior=None,
                nuevo=f"creado: {nombre}", usuario=usuario)
    conn.commit()
    return {"clave": limpia, "etiqueta": nombre, "activo": True,
            "en_el_muro": limpia in set(get_args(Tratamiento)), "fichas": 0,
            "faltan": list(CONCEPTOS_MINIMOS)}


def cambiar_tratamiento(
    conn, clave: str, *, etiqueta: str | None = None, activo: bool | None = None, usuario: str
) -> dict[str, Any]:
    """Renombra o desactiva. Nunca borra.

    Borrar dejaría las citas históricas apuntando a una clave que ya no existe. Desactivado:
    Daniela no lo ofrece ni lo agenda, y lo que ya se agendó sigue legible.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT etiqueta, activo FROM tratamientos WHERE clave = %s", (clave,))
        fila = cur.fetchone()
        if fila is None:
            raise ValueError(f"'{clave}' no existe.")
        antes_etiqueta, antes_activo = fila

        if etiqueta is not None and etiqueta.strip() and etiqueta.strip() != antes_etiqueta:
            cur.execute(
                "UPDATE tratamientos SET etiqueta = %s WHERE clave = %s",
                (etiqueta.strip(), clave),
            )
            _anotar(cur, tabla="tratamientos", clave=clave, anterior=antes_etiqueta,
                    nuevo=etiqueta.strip(), usuario=usuario)

        if activo is not None and activo != antes_activo:
            cur.execute("UPDATE tratamientos SET activo = %s WHERE clave = %s", (activo, clave))
            _anotar(cur, tabla="tratamientos", clave=clave,
                    anterior="activo" if antes_activo else "inactivo",
                    nuevo="activo" if activo else "inactivo", usuario=usuario)
    conn.commit()
    return next(f for f in listar_tratamientos(conn) if f["clave"] == clave)


# ------------------------------------------------------------------------------------------
# Fichas de conocimiento
# ------------------------------------------------------------------------------------------


def listar_conocimiento(conn) -> list[dict[str, Any]]:
    """Las 73 fichas, crudas. El formateo que ve el modelo vive en
    `persistencia.formatear_conocimiento` y no se duplica aquí."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tratamiento, concepto, contenido, aprobado, nota_pendiente, actualizado_en "
            "FROM base_conocimiento ORDER BY tratamiento, concepto"
        )
        return [
            {"tratamiento": t, "concepto": c, "contenido": k, "aprobado": a,
             "nota_pendiente": n, "actualizado_en": u.isoformat()}
            for t, c, k, a, n, u in cur.fetchall()
        ]


def guardar_ficha(
    conn, *, tratamiento: str, concepto: str, contenido: str, aprobado: bool,
    nota_pendiente: str | None, usuario: str,
) -> dict[str, Any]:
    """Crea o edita una ficha, con su registro en la bitácora, en una sola transacción.

    `aprobado=False` NO esconde la ficha: `formatear_conocimiento` igual se la entrega al
    modelo, precedida de la advertencia de aprobación. El interruptor significa «esto
    todavía no es un compromiso comercial», no «esto no se ve».
    """
    trat = (tratamiento or "").strip().lower()
    conc = (concepto or "").strip().lower()
    texto = (contenido or "").strip()
    if not trat or not conc:
        raise ValueError("Hacen falta el tratamiento y el concepto.")
    if not texto:
        raise ValueError(
            "Una ficha vacía no es lo mismo que una ficha sin datos. Si MaxiCare no tiene "
            "esa información, deja la ficha sin crear: Daniela dirá «SIN DATO DOCUMENTADO» "
            "y escalará, que es el comportamiento correcto."
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT contenido FROM base_conocimiento WHERE tratamiento = %s AND concepto = %s",
            (trat, conc),
        )
        fila = cur.fetchone()
        anterior = fila[0] if fila else None

        cur.execute(
            "INSERT INTO base_conocimiento (tratamiento, concepto, contenido, aprobado, "
            "                               nota_pendiente, actualizado_en) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (tratamiento, concepto) DO UPDATE SET "
            "  contenido = EXCLUDED.contenido, aprobado = EXCLUDED.aprobado, "
            "  nota_pendiente = EXCLUDED.nota_pendiente, actualizado_en = now()",
            (trat, conc, texto, aprobado, nota_pendiente),
        )
        _anotar(cur, tabla="base_conocimiento", clave=f"{trat}/{conc}",
                anterior=anterior, nuevo=texto, usuario=usuario)
    conn.commit()
    return {"tratamiento": trat, "concepto": conc, "contenido": texto,
            "aprobado": aprobado, "nota_pendiente": nota_pendiente}


def historial(conn, limite: int = 100) -> list[dict[str, Any]]:
    """Lo más reciente primero. Sin paginación: cien cambios cubren meses de esta clínica."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tabla, clave, valor_anterior, valor_nuevo, usuario, cambiado_en "
            "FROM cambios_configuracion ORDER BY cambiado_en DESC LIMIT %s",
            (max(1, min(limite, 500)),),
        )
        return [
            {"tabla": t, "clave": c, "valor_anterior": a, "valor_nuevo": n,
             "usuario": u, "cambiado_en": f.isoformat()}
            for t, c, a, n, u, f in cur.fetchall()
        ]
```

- [ ] **Step 4: Añadir `panel.py` a la frontera**

En `tests/test_estructura.py`, dentro de `MODULOS_SIN_TRANSPORTE`, añade tras el bloque de `autenticacion.py`:

```python
    # `panel.py` (fase 8) recibe una conexión y devuelve diccionarios. No sabe que alguien
    # los va a serializar como JSON detrás de una cookie, y por eso sus pruebas pueden
    # comprobar que la bitácora es atómica sin levantar un servidor.
    "panel.py",
```

- [ ] **Step 5: Correr las pruebas**

Run: `uv run pytest -q tests/test_panel.py tests/test_estructura.py`
Expected: PASS la offline y las de estructura; las de Neon, skipped.

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/panel.py tests/test_panel.py tests/test_estructura.py
git commit -m "panel.py: consultas y escrituras del panel, sin framework

Ningun cambio se escribe sin su registro en la bitacora, y los dos van en la
misma transaccion. en_el_muro se calcula del Literal, no se guarda: una columna
seria una segunda fuente de verdad que se desincroniza en silencio.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Daniela se entera sin reiniciar

**Files:**
- Modify: `src/maxicare_daniela/agentes.py:56-100, 156-170`
- Test: `tests/test_agentes.py`

**Interfaces:**
- Consumes: `contratos.vocabulario()` de la Tarea 2.
- Produces: `agentes.instrucciones_daniela(ctx, agent) -> str`, y `daniela.instructions` pasa a ser ese callable.

**Verificado contra la 0.22.2 instalada:** `Agent.instructions` acepta `str | Callable[[RunContextWrapper, Agent], MaybeAwaitable[str]]`, y `get_system_prompt` exige **exactamente 2 parámetros** o lanza `TypeError`.

- [ ] **Step 1: Escribir las pruebas que fallan**

Añade a `tests/test_agentes.py`:

```python
def test_las_instrucciones_traen_el_vocabulario_vivo():
    contratos.fijar_vocabulario(["carillas", "implantes"])
    try:
        texto = asyncio.run(
            agentes.daniela.get_system_prompt(RunContextWrapper(context=None))
        )
        assert "carillas" in texto
    finally:
        contratos.fijar_vocabulario(get_args(contratos.Tratamiento))


def test_el_vocabulario_va_al_final_para_no_romper_el_cache():
    """`prompt_cache_retention="24h"` baja la entrada de $2.00 a $0.20 por millón y funciona
    por prefijo idéntico. Una lista que cambia al principio lo invalidaría en cada corrida."""
    texto = asyncio.run(agentes.daniela.get_system_prompt(RunContextWrapper(context=None)))
    assert texto.startswith(agentes.INSTRUCCIONES_DANIELA[:200])
    assert texto.index("implantes") > len(agentes.INSTRUCCIONES_DANIELA) - 50
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest -q tests/test_agentes.py -k vocabulario`
Expected: FAIL — las instrucciones son una cadena fija y no contienen `carillas`.

- [ ] **Step 3: Implementar**

En `agentes.py`, después de `INSTRUCCIONES_DANIELA` (que se deja tal cual, porque las pruebas comparan contra ella):

```python
def instrucciones_daniela(ctx, agente) -> str:
    """Las instrucciones de siempre, con el vocabulario vivo pegado AL FINAL.

    Al final, y no al principio, por dinero: `prompt_cache_retention="24h"` baja la entrada
    de $2.00 a $0.20 por millón, y el caché funciona por prefijo idéntico. Si la lista
    cambiara al comienzo del prompt, cada corrida pagaría el precio completo.

    El SDK exige exactamente dos parámetros en este callable (`get_system_prompt` lanza
    `TypeError` si no), aunque aquí no se use ninguno de los dos: el vocabulario vive en un
    módulo, no en el contexto de la corrida.
    """
    ofrecidos = sorted(c for c in contratos.vocabulario() if c != "no_identificado")
    return (
        f"{INSTRUCCIONES_DANIELA}\n\n"
        "TRATAMIENTOS QUE MAXICARE OFRECE HOY\n"
        f"{', '.join(ofrecidos)}.\n"
        "Usa exactamente una de esas claves al agendar y al registrar el estado de la "
        "oportunidad. Si el paciente pregunta por algo que no está en la lista, no lo "
        "ofrezcas: escala."
    )
```

Y en el `Agent`:

```python
    instructions=instrucciones_daniela,
```

Añade `instrucciones_daniela` a `__all__`.

- [ ] **Step 4: Correr las pruebas**

Run: `uv run pytest -q tests/test_agentes.py`
Expected: PASS

- [ ] **Step 5: Suite entera**

Run: `uv run pytest -q`
Expected: todo verde.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/agentes.py tests/test_agentes.py
git commit -m "Las instrucciones de Daniela traen el vocabulario vivo al final

Al final y no al principio por dinero: el cache de prompt funciona por prefijo
identico y vale diez veces la entrada.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Endpoints y control de rol

**Files:**
- Modify: `src/maxicare_daniela/runtime.py` (antes de la ruta comodín)
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `panel.*` (Tarea 4), `contratos.fijar_vocabulario` (Tarea 2), `usuario_actual` (ya existe).
- Produces: `exigir_rol(*roles)` y seis rutas bajo `/api`.

- [ ] **Step 1: Escribir las pruebas que fallan**

Añade a `tests/test_panel.py`, con `TestClient` y un doble de `usuario_actual` por `dependency_overrides` — así no hace falta base de datos para probar el control de rol:

```python
from fastapi.testclient import TestClient

from maxicare_daniela import runtime


def _como(rol: str):
    return lambda: {"usuario": f"x.{rol}", "nombre": "Prueba", "rol": rol}


def test_recepcion_no_puede_editar_una_ficha():
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        c = TestClient(runtime.app)
        r = c.put("/api/conocimiento", json={
            "tratamiento": "implantes", "concepto": "precio",
            "contenido": "$1", "aprobado": True, "nota_pendiente": None,
        })
        assert r.status_code == 403
        assert "detalle" in r.json()
    finally:
        runtime.app.dependency_overrides.clear()


def test_solo_admin_crea_un_tratamiento():
    for rol, esperado in [("doctor", 403), ("recepcion", 403)]:
        runtime.app.dependency_overrides[runtime.usuario_actual] = _como(rol)
        try:
            c = TestClient(runtime.app)
            r = c.post("/api/tratamientos", json={"clave": "carillas", "etiqueta": "Carillas"})
            assert r.status_code == esperado, f"{rol} obtuvo {r.status_code}"
        finally:
            runtime.app.dependency_overrides.clear()


def test_un_error_del_servidor_llega_al_usuario_como_detalle():
    """`api.ts` lee `cuerpo?.detalle`, pero `HTTPException` serializa `detail`. Sin este
    manejador, ningún mensaje del servidor llega a la pantalla y todo se ve como un error
    genérico."""
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        c = TestClient(runtime.app)
        cuerpo = c.post("/api/tratamientos", json={"clave": "x", "etiqueta": "y"}).json()
        assert cuerpo.get("detalle")
        assert "admin" in cuerpo["detalle"].lower() or "permiso" in cuerpo["detalle"].lower()
    finally:
        runtime.app.dependency_overrides.clear()
```

- [ ] **Step 2: Correr y ver fallar**

Run: `uv run pytest -q tests/test_panel.py -k "recepcion or admin or detalle"`
Expected: FAIL — 404, las rutas no existen.

- [ ] **Step 3: Escribir el bloque en `runtime.py`**

**No uses un heredoc de Bash.** Escribe el bloque con la herramienta Write en el scratchpad y anéxalo con una línea de Python, o edita el archivo directamente. Va **antes** de `@app.get("/{ruta_completa:path}")`.

```python
# ------------------------------------------------------------------------------------------
# Panel: tratamientos y base de conocimiento
# ------------------------------------------------------------------------------------------


def exigir_rol(*roles: str):
    """Dependencia que restringe una ruta a ciertos roles. Devuelve 403, no 404.

    El botón se esconde en el frontend por comodidad, pero el control vive aquí: un botón
    que desaparece no es un control de acceso.
    """
    def comprobar(quien: dict = Depends(usuario_actual)) -> dict:
        if quien["rol"] not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Hace falta permiso de {' o '.join(roles)} para esto.",
            )
        return quien
    return comprobar


def _refrescar_vocabulario(conn) -> None:
    """Deja el vocabulario del proceso igual a la tabla. Se llama al arrancar y después de
    cada cambio, para que no haga falta reiniciar nada."""
    contratos.fijar_vocabulario(panel.vocabulario_activo(conn))


@app.get("/api/tratamientos")
async def api_tratamientos(quien: dict = Depends(usuario_actual)) -> dict:
    with persistencia.conectar(config.database_url) as conn:
        return {"tratamientos": panel.listar_tratamientos(conn)}


class TratamientoNuevo(BaseModel):
    clave: str = Field(min_length=3, max_length=24)
    etiqueta: str = Field(min_length=1, max_length=120)


@app.post("/api/tratamientos")
async def api_crear_tratamiento(
    cuerpo: TratamientoNuevo, quien: dict = Depends(exigir_rol("admin"))
) -> dict:
    try:
        with persistencia.conectar(config.database_url) as conn:
            creado = panel.crear_tratamiento(
                conn, clave=cuerpo.clave, etiqueta=cuerpo.etiqueta, usuario=quien["usuario"]
            )
            _refrescar_vocabulario(conn)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("%s creó el tratamiento %s", quien["usuario"], creado["clave"])
    return creado


class TratamientoCambio(BaseModel):
    etiqueta: str | None = Field(default=None, max_length=120)
    activo: bool | None = None


@app.patch("/api/tratamientos/{clave}")
async def api_cambiar_tratamiento(
    clave: str, cuerpo: TratamientoCambio, quien: dict = Depends(exigir_rol("admin"))
) -> dict:
    try:
        with persistencia.conectar(config.database_url) as conn:
            cambiado = panel.cambiar_tratamiento(
                conn, clave, etiqueta=cuerpo.etiqueta, activo=cuerpo.activo,
                usuario=quien["usuario"],
            )
            _refrescar_vocabulario(conn)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return cambiado


@app.get("/api/conocimiento")
async def api_conocimiento(quien: dict = Depends(usuario_actual)) -> dict:
    with persistencia.conectar(config.database_url) as conn:
        return {"fichas": panel.listar_conocimiento(conn)}


class Ficha(BaseModel):
    tratamiento: str = Field(min_length=1, max_length=60)
    concepto: str = Field(min_length=1, max_length=60)
    contenido: str = Field(min_length=1, max_length=4000)
    aprobado: bool = True
    nota_pendiente: str | None = Field(default=None, max_length=400)


@app.put("/api/conocimiento")
@app.post("/api/conocimiento")
async def api_guardar_ficha(
    ficha: Ficha, quien: dict = Depends(exigir_rol("admin", "doctor"))
) -> dict:
    try:
        with persistencia.conectar(config.database_url) as conn:
            guardada = panel.guardar_ficha(
                conn, tratamiento=ficha.tratamiento, concepto=ficha.concepto,
                contenido=ficha.contenido, aprobado=ficha.aprobado,
                nota_pendiente=ficha.nota_pendiente, usuario=quien["usuario"],
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("%s editó %s/%s", quien["usuario"], ficha.tratamiento, ficha.concepto)
    return guardada


@app.get("/api/historial")
async def api_historial(quien: dict = Depends(usuario_actual)) -> dict:
    with persistencia.conectar(config.database_url) as conn:
        return {"cambios": panel.historial(conn)}
```

Añade `panel` y `contratos` a los imports de `runtime.py`, y `Depends`/`Field` si faltaran.

- [ ] **Step 4: Añadir el manejador que renombra `detail` a `detalle`**

Junto a la creación de `app`, arriba del archivo:

```python
@app.exception_handler(HTTPException)
async def _error_en_el_vocabulario_del_frontend(request: Request, exc: HTTPException):
    """`api.ts` lee `detalle`; FastAPI serializa `detail`. Sin esto, ningún mensaje del
    servidor llega a la pantalla: todos caen al texto genérico de `pedir()`."""
    return JSONResponse(
        {"detalle": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )
```

- [ ] **Step 5: Refrescar el vocabulario al arrancar**

Donde `runtime.py` hace su preparación de arranque, añade una llamada tolerante a fallos: si la base no responde al encender, el proceso **no** debe caerse — el webhook de WhatsApp está en producción.

```python
try:
    with persistencia.conectar(config.database_url) as _c:
        _refrescar_vocabulario(_c)
    log.info("vocabulario de tratamientos cargado desde la base")
except Exception:  # noqa: BLE001
    log.warning(
        "no se pudo leer la tabla de tratamientos al arrancar; se usan los catorce del "
        "Literal. El webhook sigue funcionando.", exc_info=True
    )
```

- [ ] **Step 6: Correr las pruebas**

Run: `uv run pytest -q tests/test_panel.py tests/test_web.py`
Expected: PASS. `test_ninguna_ruta_del_panel_responde_sin_sesion` cubre sola las rutas nuevas: está parametrizada sobre `runtime.app.routes`.

- [ ] **Step 7: Suite entera y contra Neon**

Run: `uv run pytest -q`
Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: las dos verdes.

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/runtime.py tests/test_panel.py
git commit -m "Endpoints del panel con control de rol

Recepcion recibe 403, no un boton escondido. Y un manejador traduce detail a
detalle: sin el, ningun mensaje del servidor llegaba a la pantalla.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: La pantalla

**Files:**
- Modify: `web/src/api.ts`
- Create: `web/src/pantallas/Tratamientos.tsx`
- Modify: `web/src/componentes/Sidebar.tsx:30-37`, `web/src/App.tsx`

**Interfaces:**
- Consumes: las seis rutas de la Tarea 6.
- Produces: la sección `tratamientos` deja de renderizar `Pendiente`.

- [ ] **Step 1: Añadir tipos y llamadas a `api.ts`**

```ts
export type TratamientoFila = {
  clave: string
  etiqueta: string
  activo: boolean
  en_el_muro: boolean
  fichas: number
  faltan: string[]
}

export type FichaFila = {
  tratamiento: string
  concepto: string
  contenido: string
  aprobado: boolean
  nota_pendiente: string | null
  actualizado_en: string
}

export async function listarTratamientos(): Promise<TratamientoFila[]> {
  const r = await pedir<{ tratamientos: TratamientoFila[] }>('/api/tratamientos')
  return r.tratamientos
}

export async function crearTratamiento(clave: string, etiqueta: string): Promise<TratamientoFila> {
  return pedir<TratamientoFila>('/api/tratamientos', {
    method: 'POST',
    body: JSON.stringify({ clave, etiqueta }),
  })
}

export async function cambiarTratamiento(
  clave: string,
  cambio: { etiqueta?: string; activo?: boolean },
): Promise<TratamientoFila> {
  return pedir<TratamientoFila>(`/api/tratamientos/${encodeURIComponent(clave)}`, {
    method: 'PATCH',
    body: JSON.stringify(cambio),
  })
}

export async function listarFichas(): Promise<FichaFila[]> {
  const r = await pedir<{ fichas: FichaFila[] }>('/api/conocimiento')
  return r.fichas
}

export async function guardarFicha(f: {
  tratamiento: string
  concepto: string
  contenido: string
  aprobado: boolean
  nota_pendiente: string | null
}): Promise<FichaFila> {
  return pedir<FichaFila>('/api/conocimiento', { method: 'PUT', body: JSON.stringify(f) })
}
```

- [ ] **Step 2: Escribir `web/src/pantallas/Tratamientos.tsx`**

Usa la herramienta Write. Sigue el estilo de `Pruebas.tsx` y `Agenda.tsx`: `const SP = "'Space Grotesk', sans-serif"`, colores literales, Tailwind para la disposición.

Estructura, de arriba abajo:

1. **Cabecera** con el título y un contador honesto: `«N fichas · M sin aprobar · K tratamientos sin precio»`.
2. **Dos pestañas**: *Tratamientos* y *La clínica* (esta última filtra `tratamiento === '_general'` y se titula así, nunca `_general`).
3. **Lista de tratamientos.** Por cada uno: etiqueta, insignia `inactivo` si no lo está, y lo que falta —`faltan` en rojo suave—. Al desplegar, sus fichas.
4. **Ficha**: el contenido en un `<textarea>`, el interruptor de aprobación con su etiqueta literal **«Es un compromiso comercial» / «Todavía no lo es»**, y el campo de nota cuando está sin aprobar. Junto al interruptor, en gris, la frase que evita el malentendido: *«Sin aprobar no la esconde: Daniela igual la usa, avisando que falta por definir.»*
5. **Crear ficha.** El concepto sale de un `<select>` construido con los conceptos que ya existen en `/api/conocimiento` —hoy son 25, no se escriben a mano en el código— más una opción «otro concepto…» que abre un campo de texto con su advertencia en gris:

   > *Un concepto nuevo no se pierde: si Daniela pregunta por uno que no existe, la tool le devuelve todos los del tratamiento. Pero un «precios» junto a un «precio» deja dos precios conviviendo.*

6. **Crear tratamiento**, visible solo si `sesion.rol === 'admin'`. Pide etiqueta y clave (sugiere la clave a partir de la etiqueta: minúsculas, sin tildes, espacios a `_`). Antes de confirmar, muestra los dos avisos, literales:
   - *«Daniela ya podrá cotizarlo y agendarlo. Una radiografía sobre este tratamiento se seguirá clasificando como "no identificado" hasta que lo incorporemos al muro.»*
   - *«Todavía no tiene fichas: Daniela dirá "SIN DATO DOCUMENTADO" y escalará. Llena precio, duración y profesional.»*
7. **Errores** del servidor mostrados tal cual llegan en `detalle`, con `role="alert"`.
8. **`SesionCaducada`** se propaga hacia arriba como en `Pruebas.tsx`, para que `App.tsx` devuelva al ingreso.

Botones de escritura deshabilitados si el rol no alcanza, con el porqué en el `title` — pero el servidor ya devuelve 403 igual.

- [ ] **Step 3: Enrutar la sección**

En `Sidebar.tsx`, cambia la entrada de tratamientos a construida:

```ts
  { id: 'tratamientos', etiqueta: 'Tratamientos', icono: '🦷', fase: null },
```

En `App.tsx`, importa la pantalla y añade su caso antes del `PantallaPendiente`, igual que `Agenda` y `Pruebas`.

- [ ] **Step 4: Compilar**

Run: `cd web && npx tsc --noEmit`
Expected: sin errores.

Run: `cd web && npm run build`
Expected: `dist/` regenerado.

- [ ] **Step 5: Probarla a mano**

Levanta los dos procesos y entra:

```bash
uv run uvicorn maxicare_daniela.runtime:app --port 8080
cd web && npm run dev
```

Comprueba: las 12 fichas se ven, endodoncia sale con sus tres faltantes, editar un precio lo guarda, y con un usuario de rol `recepcion` el servidor responde 403 con su mensaje legible.

- [ ] **Step 6: Commit**

```bash
git add web/src/api.ts web/src/pantallas/Tratamientos.tsx web/src/componentes/Sidebar.tsx web/src/App.tsx
git commit -m "Pantalla de Tratamientos

El interruptor de aprobacion dice lo que hace de verdad: sin aprobar no esconde
la ficha, la marca como todavia no comprometida.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: El entregable verificable

**Files:**
- Create: `scripts/probar_panel.py`
- Modify: `CLAUDE.md` (sección Comandos)

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: un comando que demuestra la mitad del entregable de la fase 8.

- [ ] **Step 1: Escribir `scripts/probar_panel.py`**

Usa la herramienta Write. Cópiale la forma a `scripts/probar_web.py`: `sys.path.insert`, `sys.stdout.reconfigure`, `cargar_dotenv()` antes de importar `runtime`, un `revisar(etiqueta, condicion, detalle)` que cuenta fallos, usuario temporal `zzz.` creado y borrado en un `finally`, y marcadores ASCII.

Los seis pasos que tiene que comprobar, en este orden:

1. Entra con un usuario temporal de rol `admin`.
2. `PUT /api/conocimiento` cambia el precio de implantes a un valor único generado en el momento.
3. Con `--chat`: pregunta a Daniela cuánto cuesta un implante y **comprueba que el valor nuevo aparece en su respuesta**.
4. `POST /api/tratamientos` crea `carillas`; le pone precio; con `--chat`, Daniela lo cotiza.
5. `LecturaArchivo(tratamiento="carillas")` **sigue lanzando `ValidationError`** — el muro no se abrió.
6. `GET /api/historial` trae los tres cambios con el usuario correcto.

Todo contra el esquema `pruebas_web`, reusando `runtime._preparar_esquema_de_pruebas()`. Al terminar, borra el usuario temporal y el tratamiento `carillas` del carril de pruebas.

- [ ] **Step 2: Correrlo sin chat**

Run: `uv run python scripts/probar_panel.py`
Expected: todas las líneas en `OK`, y el resumen `FASE 8 (primera mitad) -- panel de tratamientos: OK`.

- [ ] **Step 3: Correrlo con chat**

Run: `uv run python scripts/probar_panel.py --chat`
Expected: `OK` en todo, e impreso el turno real de Daniela citando el precio nuevo. **Gasta tokens.**

Si el paso 3 falla porque Daniela cita el precio viejo, el problema no es la pantalla: revisa que `guardar_ficha` haya hecho `commit` y que el chat de pruebas esté leyendo del mismo esquema.

- [ ] **Step 4: Documentar el comando**

En `CLAUDE.md`, sección **Comandos**, tras la línea de `probar_web.py`:

```markdown
- El panel de tratamientos (entregable fase 8, primera mitad):
  `uv run python scripts/probar_panel.py`
  Con `--chat` comprueba que Daniela cotiza el precio nuevo y **gasta tokens**.
```

- [ ] **Step 5: Commit**

```bash
git add scripts/probar_panel.py CLAUDE.md
git commit -m "probar_panel.py: el entregable de la primera mitad de la fase 8

Cambia un precio desde la API, le pregunta a Daniela, y comprueba que cotiza el
nuevo. Y que crear un tratamiento NO abrio el muro.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Cerrar

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/agentes/plan-agentes.json`

- [ ] **Step 1: Documentar la decisión en `CLAUDE.md`**

En la sección **Interfaz web**, añade:

```markdown
- **El `Literal` de tratamientos está partido en dos.** `LecturaArchivo` conserva los 14
  escritos a mano —es el muro, y tiene su prueba—. El vocabulario de negocio vive en la
  tabla `tratamientos` y lo carga `runtime.py` al arrancar. Agregar uno desde la pantalla
  NO lo mete en el muro: una radiografía suya se clasifica `no_identificado`.
```

- [ ] **Step 2: Anotar la condición de revisión disparada**

Con la skill `cerrar-fase`, o a mano y validando el esquema, añade a la fase 1 de
`docs/agentes/plan-agentes.json`, en su `condicion_revision`, el hallazgo verificado:

> VERIFICADO 2026-09-12: la condición se disparó, y no por lo que decía. No fue que el
> `Literal` resultara insuficiente al cargar la base —los 14 cubren los 12 que MaxiCare
> ofrece más endodoncia y prótesis— sino que el cliente tiene que poder agregar un
> tratamiento sin nosotros, que es el argumento entero de la fase 8. Se resolvió partiendo
> el `Literal` en dos: `LecturaArchivo` conserva el cerrado escrito a mano (el muro, con
> `test_tratamiento_no_admite_una_frase_clinica` intacto) y `SolicitudCita` con
> `registrar_estado_oportunidad` pasan a validar contra la tabla `tratamientos`. Un
> tratamiento creado desde la pantalla se cotiza y se agenda; una radiografía sobre él cae
> en `no_identificado` hasta que se incorpore al muro con un cambio de código. El modelo se
> entera sin reiniciar porque las instrucciones de Daniela son un callable que pega el
> vocabulario AL FINAL, para no invalidar el caché de prompt de 24 h.

- [ ] **Step 3: Verificación final completa**

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
cd web && npx tsc --noEmit && npm run build
uv run python scripts/probar_web.py
uv run python scripts/probar_panel.py --chat
```

Todo tiene que salir verde. `test_tratamiento_no_admite_una_frase_clinica` sin editar.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/agentes/plan-agentes.json
git commit -m "Cierra la primera mitad de la fase 8

La condicion de revision de la fase 1 se disparo, y no por lo que decia: no
falto un tratamiento, falto autonomia del cliente.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Lo que este plan NO hace

- Las cuatro perillas de operación, Usuarios del panel y Estado del sistema: fase 8, después de la fase 6.
- Borrar fichas o tratamientos. Solo desactivar.
- Meter tratamientos nuevos en el `Literal` de `LecturaArchivo`. Eso sigue siendo un cambio de código, a propósito.
- Los borradores del prompt de Daniela con historial de versiones: alcance que `plan-agentes.json` no tiene.

**Lo siguiente es la fase 6** — ingesta, el muro y el relevo: donde Daniela empieza a contestar de verdad.

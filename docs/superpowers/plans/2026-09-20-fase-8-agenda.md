# Fase 8 — Agenda real y marca de asistencia · Plan de implementación

> **Para quien lo ejecute:** SUB-SKILL OBLIGATORIA: `superpowers:subagent-driven-development`
> (recomendada) o `superpowers:executing-plans`. Los pasos usan casillas (`- [ ]`).

**Objetivo:** que alguien de la clínica marque una asistencia desde el panel y quede escrita en
`citas.asistio`, sobre una agenda que ya se reconcilió contra Google Calendar.

**Arquitectura:** se parte `_sincronizar_con_calendar` en núcleo (devuelve datos) y presentación
(las frases de Daniela, intactas). El panel llama al núcleo antes de pintar. El SQL nuevo vive
en `panel.py`, los dos endpoints en `runtime.py`, y `Agenda.tsx` deja de ser una maqueta.

**Stack:** Python 3.12 · FastAPI · psycopg · pytest · React 19 + Vite + Tailwind v4 · uv

**Spec:** `docs/superpowers/specs/2026-09-20-fase-8-agenda-y-asistencia-design.md` — léelo
entero antes de la tarea 1. Este plan argumenta desde él.

## Restricciones globales

- **Rama:** `fase-8-agenda`, desde `main` (`1ee143d`). No fundir nada sin pedirlo.
- **No negociable 20:** para una cita que YA existe manda Google Calendar, no Neon. Lo que
  corrige se DICE, con la hora vieja dentro.
- **No negociable 2:** ninguna clave de idempotencia la escribe el modelo. Las que ya existen
  en `_mover_porque_la_movieron` no se tocan.
- **Las tres pruebas de `tests/test_herramientas.py` (`:1625`, `:1692`, `:1780`) no se editan.**
  Son la red del refactor. Si una cae, el refactor está mal, no la prueba.
- **Windows/cp1252:** marcadores ASCII (`OK` / `FALLA` / `->`) en todo lo de `scripts/`.
- **Nada de `PENDIENTE` en pantalla de cara al usuario.** Si un dato no existe, no se pinta.
- **Fechas en pruebas offline: se clava el presente.** Nunca `datetime.now()` — una fecha
  clavada que envejece ya rompió la suite tres veces (ver `tests/test_herramientas.py::AHORA`).
- Comprobación entre tareas: `uv run pytest -q`. Las de base: `MAXICARE_PRUEBAS_NEON=1 uv run
  pytest -q -m neon`.

## Orden y paralelismo

```
Tarea 1 (refactor)  ──┬──> Tarea 2 (backend)  ──┐
   BLOQUEANTE, sola   └──> Tarea 3 (frontend) ──┴──> Tarea 4 (cierre)
                           ↑ en paralelo: el contrato del endpoint
                             ya está fijado en el spec
```

---

### Tarea 1: Partir la reconciliación en núcleo y presentación

**Archivos:**
- Modificar: `src/maxicare_daniela/herramientas.py:1687-1824` (`_sincronizar_con_calendar`)
- Probar: `tests/test_herramientas.py`

**Interfaces:**
- Produce: `herramientas.Correccion` y `herramientas.reconciliar_con_calendar(...)`, que la
  tarea 2 consume desde `runtime.py`.

- [ ] **Paso 1: escribir las dos pruebas nuevas, que deben fallar**

La primera fija el texto de Daniela carácter a carácter (es lo que el refactor no puede
cambiar). La segunda fija la propiedad que hoy no está escrita en ninguna parte.

Reutiliza el montaje de `test_si_la_movieron_en_calendar_Daniela_dice_la_hora_NUEVA`
(`:1625`): copia su preparación de `ctx`, su doble de calendario y su `monkeypatch` de
`persistencia.conectar`, que es lo que hace que `database_url` apunte a la conexión falsa.

```python
def test_el_texto_de_la_novedad_no_cambia_al_refactorizar(monkeypatch):
    """Si alguien toca las frases, esto cae. Son lo que Daniela le dice al paciente."""
    ctx, cita = ...   # el mismo montaje de :1625
    _, novedades = asyncio.run(h._sincronizar_con_calendar(ctx, [dict(cita)]))
    assert novedades == [
        "La clínica movió la cita de limpieza que estaba para el "
        "jueves 17/9 a las 09:00: ahora es el viernes 18/9 a las 15:00. "
        "Es un cambio confirmado, no es un error del sistema y no hay nada que verificar."
    ]   # <- copia el literal EXACTO del código actual antes de tocarlo


def test_reconciliar_una_cita_pasada_no_programa_recordatorio(monkeypatch):
    """La agenda reconcilia cualquier día. Un día de la semana pasada no puede
    programar recordatorios de algo que ya ocurrió."""
    conn = _conexion_falsa()   # monkeypatchea persistencia.conectar para devolverla
    cita = _cita(inicio=AHORA - timedelta(days=7))
    calendario = _CalendarioDoble(evento_en=AHORA - timedelta(days=7) + timedelta(hours=2))
    asyncio.run(h.reconciliar_con_calendar(
        database_url="postgres://doble", calendario=calendario, citas=[cita],
        ahora=AHORA, jornada=JORNADA, capacidad_por_hora=2, duracion_cita_minutos=60,
        hora_recordatorio_vispera=18, horas_minimas_para_recordar=4,
    ))
    assert conn.seguimientos_insertados == []
```

- [ ] **Paso 2: correr y ver que fallan**

`uv run pytest -q tests/test_herramientas.py -k "no_cambia_al_refactorizar or cita_pasada"`
Esperado: la primera FALLA si el literal no coincide (cópialo del código, no lo inventes); la
segunda FALLA con `AttributeError: module 'herramientas' has no attribute 'reconciliar_con_calendar'`.

- [ ] **Paso 3: extraer el núcleo**

Añadir encima de `_sincronizar_con_calendar`:

```python
@dataclass(frozen=True)
class Correccion:
    cita_id: str
    que_paso: Literal["movida", "cancelada"]
    hora_vieja: datetime
    hora_nueva: datetime | None      # None cuando la borraron de Calendar
    tratamiento: str
    nombre_completo: str


async def reconciliar_con_calendar(
    *,
    database_url: str,
    calendario: Any | None,
    citas: list[dict[str, Any]],
    ahora: datetime,
    jornada: Jornada,
    capacidad_por_hora: int,
    duracion_cita_minutos: int,
    hora_recordatorio_vispera: int,
    horas_minimas_para_recordar: int,
) -> tuple[list[dict[str, Any]], list[Correccion]]:
    """Esas citas como están HOY en Google Calendar, corrigiendo Neon si hace falta."""
```

Mueve el cuerpo actual tal cual, sustituyendo cada uso de `ctx`: `ctx.calendario` ->
`calendario`, `ctx.ahora` -> `ahora`, `_con_base(ctx, f)` -> una copia local que abra con
`database_url`, y `ctx.capacidad_por_hora` -> el parámetro. Donde hoy hace
`novedades.append("La clínica movió…")`, ahora hace `correcciones.append(Correccion(...))`.
No cambies ni el orden de las operaciones ni las claves de idempotencia.

- [ ] **Paso 4: dejar `_sincronizar_con_calendar` como envoltura**

```python
async def _sincronizar_con_calendar(
    ctx: ContextoDaniela, citas: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Igual que antes para quien llama: las frases que Daniela le dice al paciente."""
    al_dia, correcciones = await reconciliar_con_calendar(
        database_url=ctx.database_url, calendario=ctx.calendario, citas=citas,
        ahora=ctx.ahora, jornada=ctx.jornada,
        capacidad_por_hora=ctx.capacidad_por_hora,
        duracion_cita_minutos=ctx.duracion_cita_minutos,
        hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
        horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
    )
    return al_dia, [_frase_de(c) for c in correcciones]
```

`_frase_de(correccion)` contiene los dos literales de hoy, copiados sin tocar una coma.

- [ ] **Paso 5: correr toda la suite**

`uv run pytest -q` — todo verde, **incluidas `:1625`, `:1692` y `:1780` sin editarlas**.
Luego `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`.

- [ ] **Paso 6: commit**

```bash
git add src/maxicare_daniela/herramientas.py tests/test_herramientas.py
git commit -m "refactor: la reconciliacion con Calendar deja de ser solo de Daniela"
```

---

### Tarea 2: El SQL y los dos endpoints

**Puede correr en paralelo con la tarea 3.**

**Archivos:**
- Modificar: `src/maxicare_daniela/panel.py` (al final), `src/maxicare_daniela/runtime.py`
  (rutas nuevas ANTES del comodín de `:2155`; `_NOMBRE_DEL_CAMPO` en `:117`)
- Probar: `tests/test_panel.py`

**Interfaces:**
- Consume: `herramientas.reconciliar_con_calendar` y `herramientas.Correccion` (tarea 1).
- Produce: `GET /api/agenda?dia=YYYY-MM-DD` y `PATCH /api/agenda/citas/{cita_id}`, con la forma
  de respuesta que la tarea 3 consume (ver el apartado 4 del spec, copiada abajo).

- [ ] **Paso 1: escribir las pruebas de `panel.py`, que deben fallar**

```python
@pytest.mark.neon
def test_marcar_asistencia_escribe_la_columna_y_su_bitacora(conn):
    cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
    panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="recepcion")
    assert _asistio(conn, cita) is True
    fila = panel.historial(conn, limite=1)[0]
    assert (fila["tabla"], fila["clave"], fila["valor_nuevo"]) == ("citas", cita, "asistio")

@pytest.mark.neon
def test_corregir_una_marca_deja_el_valor_anterior_en_la_bitacora(conn):
    cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
    panel.marcar_asistencia(conn, cita_id=cita, valor=False, usuario="recepcion")
    panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="admin")
    fila = panel.historial(conn, limite=1)[0]
    assert (fila["valor_anterior"], fila["valor_nuevo"]) == ("no_asistio", "asistio")

@pytest.mark.neon
def test_una_cita_que_todavia_no_ha_ocurrido_no_se_puede_marcar(conn):
    cita = _sembrar_cita(conn, inicio=MANANA_A_LAS_NUEVE)
    with pytest.raises(ValueError, match="todavía no"):
        panel.marcar_asistencia(conn, cita_id=cita, valor=False, usuario="recepcion")

@pytest.mark.neon
def test_el_dia_trae_las_citas_vivas_en_orden_y_no_las_canceladas(conn): ...

@pytest.mark.neon
def test_sin_marcar_no_devuelve_las_ya_marcadas(conn): ...
```

Y las offline con `TestClient`, en el estilo de `test_panel.py:336`:

```python
def test_recepcion_puede_marcar_asistencia(cliente_recepcion): ...      # 200
def test_un_rol_inventado_no_puede_marcar(cliente_con_rol("mercadeo")): ...  # 403
def test_sin_calendario_la_agenda_responde_igual(cliente): ...
    # 200 y {"calendario_disponible": False}
```

- [ ] **Paso 2: correr y ver que fallan**

`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q tests/test_panel.py -k "agenda or asistencia"`
Esperado: `AttributeError: module 'panel' has no attribute 'marcar_asistencia'`.

- [ ] **Paso 3: las tres funciones de `panel.py`**

Firmas exactas (el cuerpo sigue el patrón de `crear_tratamiento`, `panel.py:129`):

```python
def citas_del_dia(conn, *, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
    """Citas vivas de ese rango, con `asistio`, ordenadas por `inicio`.

    SELECT id, conversacion_id, telefono, nombre_completo, tratamiento, inicio,
           duracion_minutos, estado, asistio, evento_calendar_id, reserva_id
      FROM citas
     WHERE inicio >= %s AND inicio < %s
       AND estado IN ('confirmada','reprogramada')
     ORDER BY inicio
    """

def marcar_asistencia(conn, *, cita_id: str, valor: bool | None, usuario: str) -> dict[str, Any]:
    """UPDATE citas SET asistio + su fila de bitácora, en UNA transacción.

    `_anotar` no admite un `nuevo` nulo, así que los tres literales son
    'asistio' / 'no_asistio' / 'sin_marcar'. ValueError si la cita no existe
    ('esa cita no existe'), si su estado es 'cancelada', o si `inicio` es futuro
    ('esa cita todavía no ha ocurrido'). El commit va al final, como `guardar_ficha`.
    """

def citas_sin_marcar(conn, *, desde: datetime, hasta: datetime, limite: int = 50) -> list[dict[str, Any]]:
    """Las de ese rango cuya hora pasó y siguen con `asistio IS NULL`."""
```

- [ ] **Paso 4: los dos endpoints en `runtime.py`**

Antes del comodín de `:2155`. El GET:

```python
@app.get("/api/agenda")
async def api_agenda(dia: str | None = None, quien: dict = Depends(usuario_actual)) -> dict:
    """El día ya reconciliado contra Google Calendar.

    OJO: este GET ESCRIBE. Reconciliar corrige Neon (mueve, cancela, toca cupos), y
    esta pantalla es el mejor disparador que esa reconciliación va a tener: hoy solo
    corre cuando un paciente pregunta por su cita, y no hay ningún barrido.
    """
```

Cuerpo, en este orden: parsear `dia` (sin él, hoy en `calendario.ZONA_BOGOTA`) y armar
`[00:00, 24:00)`; `persistencia.leer_configuracion(conn)` para capacidad y duración;
`panel.citas_del_dia`; `atencion._calendario_por_defecto(config)` dentro de un `try` que al
fallar deja `calendario = None`; si hay calendario, `herramientas.reconciliar_con_calendar(...)`
y `calendario.bloqueos(desde, hasta)`; `panel.citas_sin_marcar` de los 7 días anteriores.

Respuesta:

```json
{"dia": "2026-09-20", "citas": [...], "bloqueos": [...],
 "correcciones": [{"cita_id": "...", "que_paso": "movida", "hora_vieja": "...",
                   "hora_nueva": "...", "tratamiento": "...", "nombre_completo": "..."}],
 "sin_marcar": [...], "calendario_disponible": true}
```

El PATCH:

```python
class MarcaDeAsistencia(BaseModel):
    asistio: bool | None = Field(...)   # obligatorio y nullable: null ES desmarcar

@app.patch("/api/agenda/citas/{cita_id}")
async def api_marcar_asistencia(
    cita_id: str, cuerpo: MarcaDeAsistencia,
    quien: dict = Depends(exigir_rol("admin", "doctor", "recepcion")),
) -> dict:
```

`ValueError` -> `HTTPException(400)`; cita inexistente -> `404`, como `cambiar_tratamiento`
(`runtime.py:1752`). Añadir `"dia"` y `"asistio"` a `_NOMBRE_DEL_CAMPO` (`:117`).

- [ ] **Paso 5: correr las tres suites**

`uv run pytest -q` · `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`.
La parametrizada `test_web.py:100` cubre sola el 401 de las dos rutas nuevas: si falla, falta
la dependencia.

- [ ] **Paso 6: commit**

```bash
git add src/maxicare_daniela/panel.py src/maxicare_daniela/runtime.py tests/test_panel.py
git commit -m "feat: la agenda del dia y la marca de asistencia, con su bitacora"
```

---

### Tarea 3: La pantalla

**Puede correr en paralelo con la tarea 2** — el contrato del endpoint está fijado arriba.

**Archivos:**
- Modificar: `web/src/api.ts`, `web/src/pantallas/Agenda.tsx` (reescritura),
  `web/src/App.tsx`, `web/src/componentes/Sidebar.tsx`
- Comprobar: `cd web && npm run build`

- [ ] **Paso 1: los tipos y las dos funciones de `api.ts`**

Al final del archivo, en el estilo de `listarTratamientos` (`api.ts:119`), pasando por `pedir`:

```ts
export type CitaDeAgenda = {
  id: string; telefono: string; nombre_completo: string; tratamiento: string
  inicio: string; duracion_minutos: number; estado: string; asistio: boolean | null
}
export type BloqueoDeAgenda = { inicio: string; fin: string; titulo: string }
export type CorreccionDeAgenda = {
  cita_id: string; que_paso: 'movida' | 'cancelada'
  hora_vieja: string; hora_nueva: string | null
  tratamiento: string; nombre_completo: string
}
export async function leerAgenda(dia: string | null): Promise<{...}>
export async function marcarAsistencia(citaId: string, valor: boolean | null): Promise<CitaDeAgenda>
```

- [ ] **Paso 2: reescribir `Agenda.tsx`**

Conservar la jerarquía visual de la maqueta —**el botón de asistencia sigue siendo lo más
grande de la aplicación**— y quitar de ella:

- El bloque de comentario de arriba que dice que no guarda nada, y el aviso que lo repite.
- `FRANJAS`, `AYER_SIN_MARCAR` y `MINUTO_ACTUAL`: datos de ejemplo.
- **El `origen` del paciente** (`'whatsapp' | 'google_ads' | 'instagram' | 'referido'`) y
  `ORIGEN_ETIQUETA`. Ese dato NO EXISTE en la base: `citas` no tiene la columna y
  `conversaciones.canal` solo distingue `whatsapp` de `web`. La maqueta lo inventó.

Añadir: carga con `leerAgenda`, navegación ayer/hoy/mañana con selector de fecha, franja de
correcciones arriba, aviso cuando `calendario_disponible` es `false`, lista de «sin marcar» de
días anteriores, y el botón de marcar **solo si la hora de inicio ya pasó**.

**La trampa obligatoria** (`web/CLAUDE.md`, regla 3), idéntica a `Tratamientos.tsx:1045`:

```tsx
const caducar = useRef(alCaducarSesion)
useEffect(() => { caducar.current = alCaducarSesion }, [alCaducarSesion])
const recargar = useCallback(async () => { ... }, [])   // deps VACÍAS a propósito
```

Meter `alCaducarSesion` en las deps deja la pantalla releyendo Neon en bucle, y nada lo
atraparía: se vería como lentitud y una factura rara.

Cabecera repetida en los tres estados (cargando / error / contenido), `SP` como fuente,
Tailwind para layout y `style` con hex para color.

- [ ] **Paso 3: engancharla**

En `Sidebar.tsx:30` quitarle a `agenda` la marca de fase pendiente (`fase: null` = construida).
En `App.tsx:67-86` añadir la rama pasándole `alCaducarSesion`.

- [ ] **Paso 4: compilar**

`cd web && npm run build` — sin errores de TypeScript. Luego mirarla de verdad: `npm run dev`
(:5173) con `uv run uvicorn maxicare_daniela.runtime:app --port 8080` al lado. **Hacen falta
los dos.**

- [ ] **Paso 5: commit**

```bash
git add web/
git commit -m "feat: la Agenda deja de ser una maqueta"
```

---

### Tarea 4: Cierre

**Archivos:**
- Modificar: `scripts/probar_panel.py`, `web/CLAUDE.md`, `docs/agentes/plan-agentes.json`

- [ ] **Paso 1: el bloque de agenda en `probar_panel.py`**

Siguiendo el estilo del script (marcadores `OK` / `FALLA` / `->`, sin gastar tokens salvo con
`--chat`): siembra una cita de ayer, la lee por `GET /api/agenda?dia=<ayer>`, la marca, la
corrige, y comprueba que la bitácora tiene las dos filas con el valor anterior correcto.

- [ ] **Paso 2: las renuncias, por escrito**

En `web/CLAUDE.md`, una regla nueva: **`GET /api/agenda` escribe** — reconcilia contra Calendar
y puede mover, cancelar y tocar cupos.

En `docs/agentes/plan-agentes.json`, en el `condicion_revision` de la fase 8 (usa
`scripts/ver_plan.py fases` para leerlo; el archivo está bloqueado para lectura directa):
que las tres perillas NO se hicieron —el cliente rechazó esa pantalla el 13/09/2026— y que
`cierre_relevo_minutos` y `aviso_relevo_minutos` se cachean en memoria al arrancar
(`runtime.py:203`), así que un `UPDATE` en Neon no surte efecto hasta reiniciar: quien
construya esa pantalla algún día tiene que saberlo antes de prometer «sin desplegar».

- [ ] **Paso 3: la verificación completa**

```
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_panel.py
uv run python scripts/probar_tools.py
uv run python scripts/probar_calendario.py
```

Los tres scripts porque la tarea 1 cambió una firma que `probar_tools.py` y
`probar_calendario.py` doblan a mano, y `pytest -q` no los corre: los rompería en silencio con
la suite entera en verde.

- [ ] **Paso 4: commit**

```bash
git add scripts/probar_panel.py web/CLAUDE.md docs/agentes/plan-agentes.json
git commit -m "docs: la fase 8 cierra, y las tres perillas quedan como renuncia escrita"
```

---

## Lo que este plan NO hace

- **Las tres perillas.** Decisión del cliente, 13/09/2026.
- **Paralelizar las llamadas a Google.** No se mezcla una optimización con el refactor que toca
  el no negociable 20. Si los 3-5 segundos de un día lleno molestan, va después y aparte.
- **Encender `reactivacion_no_asistio`.** Vive en `reactivacion-leads`, que no está fundida.
  Con `asistio` ya escribiéndose, encenderla será añadir `TIPO_NO_ASISTIO` a
  `TIPOS_QUE_EL_BARRIDO_ENCOLA`. Se anota allí, no aquí.

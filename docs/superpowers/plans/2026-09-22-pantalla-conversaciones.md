# Pantalla de Conversaciones · plan de implementación

**Spec:** `docs/superpowers/specs/2026-09-22-pantalla-conversaciones-design.md`
**Rama:** `pantalla-conversaciones` (sale de `main`, con Inicio ya fundido)

## DÓNDE VA ESTO (22/09/2026)

Pausado a petición de MaxiCare para atender un comportamiento de Daniela. Rama
`pantalla-conversaciones`, árbol limpio, cuatro commits sobre `main`.

| Tarea | Estado |
|---|---|
| 1 · Migración 026 y el registro de lo que escribe el doctor | **HECHA** (`84d80e2`) |
| 2 · `panel.listar_conversaciones` / `hilo` / `puede_escribir` y los dos GET | **HECHA** (`d5f647b`) |
| 3 · La pantalla, el hilo y el refresco cada 10 s | **HECHA** (`5ebdbe6`) |
| 4 · Tomar, escribir y cerrar (backend) | pendiente |
| 5 · Tomar, escribir y cerrar (pantalla) | pendiente |

Verde al pausar: `uv run pytest -q` → 1222 · `-m neon` → 230 (y 29 en `test_panel.py` con
lo nuevo) · `npm run build` limpio.

**La migración 026 YA está aplicada en el `public` de producción** (tabla vacía + índice; no
tocó ninguna fila existente). `public` pasó de 23 a 24 tablas. No hace falta volver a
aplicarla; `desplegar.sh` la encontrará idempotente.

**Nada desplegado y nada empujado.** `daniela.maxicarecol.com` sigue con el panel viejo.

Un fallo que ya se cazó y no hay que volver a buscar: la clave del diccionario del relevo es
`doctor`, **no** `tomada_por`. Como el guardado va envuelto en un `try` que se lo traga todo,
habría fallado en silencio en cada relevo. Lo cazó el doble de las pruebas.

---

**Objetivo:** ver el hilo completo de cada paciente en el panel, refrescándose solo, y poder
tomar la conversación, responderle y devolvérsela a Daniela sin salir de ahí.

## Restricciones globales

- Toda la lógica en `panel.py`. `runtime.py` es transporte. Lo exige `tests/test_estructura.py`.
- Los dos GET **no escriben**. Ni un `UPDATE`, ni un `commit`.
- Pruebas de Neon: **aserciones de delta** (medir antes y después). El esquema `pruebas` no se
  borra nunca y siete archivos más escriben en las mismas tablas.
- Ninguna fecha clavada: `ahora` entra por parámetro, como en `resumen_inicio`.
- `(fallo_respuesta IS NULL OR fallo_respuesta NOT LIKE 'relevo:%%')` — las dos mitades. En
  psycopg el `%` va duplicado dentro de una cadena con parámetros.
- Commits en español, sin acentos (la consola es cp1252).
- Tras tocar `panel.py`: `-m neon`. Tras tocar `relevar_mensaje`: `scripts/probar_relevo.py`.

---

## Tarea 1 · La tabla y el registro de lo que escribe el doctor

**Archivos:** `migraciones/026_mensajes_del_doctor.sql` (crear) · `persistencia.py` · `relevo.py`
· `tests/test_relevo.py`

Las migraciones se descubren con `sorted(carpeta.glob("*.sql"))`: basta con crear el archivo.

**SQL:**

```sql
CREATE TABLE IF NOT EXISTS mensajes_del_doctor (
    id              BIGSERIAL   PRIMARY KEY,
    telefono        TEXT        NOT NULL,
    conversacion_id UUID        REFERENCES conversaciones(id),
    autor           TEXT        NOT NULL,
    origen          TEXT        NOT NULL CHECK (origen IN ('panel', 'telegram')),
    texto           TEXT        NOT NULL,
    wamid           TEXT,
    enviado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    fallo           TEXT
);
CREATE INDEX IF NOT EXISTS ix_mensajes_del_doctor_telefono
    ON mensajes_del_doctor (telefono, enviado_en DESC);
```

**`persistencia.py`:**

- Renombrar `_texto_de_daniela` → `texto_de_daniela` (único llamador: `transcripcion`).
- `guardar_mensaje_del_doctor(conn, *, telefono, conversacion_id, autor, origen, texto, wamid=None, fallo=None) -> None`
- `mensajes_del_doctor(conn, telefono, *, limite=60) -> list[dict]` — `texto, autor, origen, enviado_en, fallo`, ordenado ascendente, cortado por el final.

**`relevo.py`:** helper de hilo `_guardar_del_doctor(database_url, **campos)` que abre conexión
y llama a persistencia, más las dos llamadas dentro de `relevar_mensaje`:

- tras el envío correcto (justo antes del `_tocar`): guardar con el `wamid` que devuelva
  `enviar_texto`. Para un adjunto sin pie, el texto es `"(el doctor envio un archivo)"` — un
  hueco en el hilo es peor que una línea genérica.
- en el `except` del envío, **antes** del `return`: guardar con `fallo=str(e)[:400]` y sin wamid.

Las dos van en un `try` propio que se traga todo y solo registra en el log: si el registro
revienta se pierde una fila, **nunca un mensaje al paciente** (no negociable 22).

`autor = relevo["doctor"]` --**no** `tomada_por`: esa es la columna de la tabla, pero la clave del diccionario que devuelve `relevo_por_tema` es `doctor`--, `telefono = relevo["telefono"]`,
`conversacion_id = relevo["id_conversacion"]`.

**Verificación:**
```
uv run python scripts/inicializar_base.py --solo-verificar
uv run pytest -q tests/test_relevo.py
uv run python scripts/probar_relevo.py
git commit -m "feat(conversaciones): se guarda lo que escribe el doctor"
```

---

## Tarea 2 · Las consultas del panel y los dos GET

**Archivos:** `panel.py` · `runtime.py` · `tests/test_panel.py` · `tests/test_runtime.py`

**`panel.listar_conversaciones(conn, *, ahora: datetime, limite: int = 50) -> list[dict]`**

Una fila por teléfono. Campos: `telefono, nombre, ultimo_en, vista_previa, estado, sin_contestar, tomada_por`.

Consulta: `DISTINCT ON (telefono)` sobre `mensajes_entrantes` ordenado por
`(telefono, recibido_en DESC)` para la vista previa y la hora; `LEFT JOIN` a la conversación
viva para `tomada_por`; subconsulta agregada para `sin_contestar` con la condición de arriba.
El nombre: `pacientes.nombre_completo` si hay ficha **y no es `persistencia.NOMBRE_PENDIENTE`**,
si no `nombre_perfil`, si no `None`.

Estado, en este orden de precedencia: `relevo` → `esperando` → `activa` → `cerrada`.

**`panel.hilo(conn, telefono, *, limite: int = 60) -> list[dict]`**

Tres consultas y un `sort` por fecha, modelado sobre `persistencia.transcripcion`:
`mensajes_entrantes` (quien `paciente`), `agent_messages` con
`created_at AT TIME ZONE 'UTC'` y `texto_de_daniela` (quien `daniela`), y
`mensajes_del_doctor` (quien `doctor`, con `autor` y `fallo`). Corte por el final.

**`panel.puede_escribir(conn, telefono, *, ahora) -> dict`** → `{puede: bool, horas: float|None}`,
sobre `persistencia.ultimo_mensaje_del_paciente`. Sin ningún mensaje: `puede=False`.

**`runtime.py`**, calcados de `/api/inicio` (transporte y nada más):

```python
@app.get("/api/conversaciones")
async def api_conversaciones(quien: dict = Depends(usuario_actual)) -> dict: ...

@app.get("/api/conversaciones/{telefono}")
async def api_conversacion(telefono: str, quien: dict = Depends(usuario_actual)) -> dict: ...
```

El segundo devuelve `{telefono, nombre, estado, tomada_por, tomada_en, puede_escribir, horas, mensajes}`.

**Pruebas (Neon, delta):** la lista devuelve una fila por teléfono con el estado correcto en
los cuatro casos; el hilo devuelve las tres voces ordenadas.

**Prueba offline (la que importa):** reutilizar `_ConexionDeSoloLectura` / `_CursorQueDelata` de
`tests/test_panel.py` —ejecutando el SQL de verdad, no un doble de la función— y exigir
`set(verbos) == {"SELECT"}` y `commits == 0` para las dos.

**Verificación:**
```
uv run pytest -q && MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
git commit -m "feat(conversaciones): la lista y el hilo salen de Neon"
```

---

## Tarea 3 · La pantalla (fase 1: solo ver)

**Archivos:** `web/src/api.ts` · `Sidebar.tsx` · `App.tsx` · `pantallas/Conversaciones.tsx` (crear)

- `SeccionId` gana `'conversaciones'`; entrada entre `inicio` y `bandeja`, etiqueta
  «Conversaciones», icono de bocadillo (SVG con `stroke`, como los demás).
- `App.tsx`: una rama más en el ternario.
- `api.ts`: `ResumenConversacion`, `HiloConversacion`, `leerConversaciones()`, `leerHilo(tel)`.
- `Conversaciones.tsx`: dos columnas (`flex-wrap`), lista a la izquierda con buscador y cuatro
  chips —filtrado **en cliente**, la lista son ≤50 filas—, hilo a la derecha con burbujas,
  separadores de día y el pie de cada mensaje (`quien · hora`).
- Refresco: `setInterval` de 10 s que **se pausa con `document.visibilityState !== 'visible'`**
  y solo pide el hilo si hay uno abierto. Patrón `useRef` para `alCaducarSesion`, como en
  `Inicio.tsx`.
- Horas en `America/Bogota` con `Intl.DateTimeFormat`, **nunca** el reloj del navegador.
- Estados vacíos: los seis de la tabla del spec.

**Verificación:** `npm run build` limpio · mirarlo en http://localhost:5173
```
git commit -m "feat(conversaciones): la pantalla, el hilo y el refresco"
```

---

## Tarea 4 · Tomar, escribir y cerrar (backend)

**Archivos:** `panel.py` · `runtime.py` · `tests/test_runtime.py`

Los tres con `Depends(exigir_rol("admin", "doctor"))`.

- **`POST /api/conversaciones/{telefono}/tomar`** → `relevo._conversacion_de()` para resolver la
  conversación, y luego la misma maquinaria que el botón de Telegram, con `callback_id=None` y
  `mensaje_id=None`. Si ya la tiene otro, se devuelve su nombre y un 409.
- **`POST /api/conversaciones/{telefono}/mensaje`** → cuerpo `{texto}` con `max_length=4000`
  (igual que el chat web). Comprueba `puede_escribir` **en el servidor**; fuera de las 24 h
  responde 409 con las horas. Envía con `whatsapp.enviar_texto`, y **guarde o falle, escribe la
  fila** con `origen='panel'` y `autor=quien["nombre"]`. Eco al hilo de Telegram para que las
  dos ventanas vean lo mismo.
- **`POST /api/conversaciones/{telefono}/cerrar`** → cuerpo `{hubo_cita, nombre?, tratamiento?, cuando?}`.
  Si hay nombre: `persistencia.asegurar_paciente`. Si hay tratamiento: validarlo contra
  `panel.vocabulario_activo` (aquí sí se valida: hay desplegable). Luego
  `relevo.cerrar(id, motivo='devuelto_por_doctor', cita=..., doctor=quien["nombre"], telegram=..., database_url=...)`.

**Pruebas offline:** `recepcion` recibe 403 en los tres; fuera de las 24 h no se llama a
`enviar_texto` **y la prueba comprueba además que se consultó el último mensaje del paciente**
(no negociable 28: una aserción sobre algo que no pasa también pasa si no corrió nada); un
`ErrorDeCanal` deja fila con `fallo` y sin `wamid`.

```
uv run pytest -q && MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
git commit -m "feat(conversaciones): tomar, escribir y cerrar desde el panel"
```

---

## Tarea 5 · Tomar, escribir y cerrar (pantalla)

**Archivos:** `api.ts` · `Conversaciones.tsx`

Pie del hilo: la nota de estado y el botón que cambia según dónde esté la conversación
(«Hablar yo con el paciente» → «Devolvérsela a Daniela»). La caja de escribir solo aparece con
el relevo tomado; apagada y con la explicación fuera de las 24 h. El formulario de cierre, con
los cuatro campos del spec, plegado hasta que se pulsa devolver.

Los botones de escritura **no se pintan** para `recepcion` (comodidad; el control vive en el
servidor).

```
npm run build && uv run pytest -q
git commit -m "feat(conversaciones): la puerta del relevo en la pantalla"
```

---

## Al terminar

`uv run pytest -q` · `-m neon` · `npm run build` · `scripts/probar_relevo.py`, y la skill
`finishing-a-development-branch`. **No se empuja sin que lo pida el usuario.**

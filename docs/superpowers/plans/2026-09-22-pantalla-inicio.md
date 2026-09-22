# Pantalla de Inicio — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar al panel una portada que conteste en cinco segundos si Daniela está funcionando, con cinco números que salen de Neon y nunca de un valor inventado.

**Architecture:** Una función de lectura en `panel.py` con las cinco consultas, un endpoint de solo lectura en `runtime.py`, y una pantalla nueva que copia el patrón de `SinResolver.tsx`. El sidebar pasa a oscuro en el mismo movimiento porque es un solo archivo y es lo primero que se ve.

**Tech Stack:** Python 3.12 · FastAPI · psycopg 3 · Postgres (Neon) · React 19 · Vite 7 · Tailwind 4

**Spec:** `docs/superpowers/specs/2026-09-22-pantalla-inicio-design.md`

**Referencia visual:** `docs/diseno/panel-2026-09-22.dc.html` (el archivo de Claude Design, versionado). El sidebar está en sus líneas 40-135; la pantalla de Inicio, en las 178-330.

## Global Constraints

- **La lógica va en `panel.py`, nunca en `runtime.py`**, que es solo transporte. Lo vigila `tests/test_estructura.py`.
- **`GET /api/inicio` es de solo lectura y NO toca Google Calendar.** Es la primera pantalla de cada sesión; reconciliar aquí convertiría cada ingreso en escrituras en Neon y llamadas a la API de Google.
- **Ninguna cifra del mockup sobrevive en el código** — ni como valor por defecto, ni comentada, ni como respaldo cuando la consulta falla. Si la consulta falla, la pantalla dice que falló.
- **Nada de fechas clavadas en las pruebas.** `ahora` se pasa siempre por parámetro. Una fecha fija en una prueba ya ha amanecido en rojo tres veces en este proyecto.
- **Los tokens `--color-sp-*` de `index.css` no se renombran ni se amplían.** La paleta nueva va en hex literales inline, que es el dialecto de las pantallas del panel.
- **Ninguna prueba nueva inventa un doble de Neon.** Las que tocan la base llevan `@pytest.mark.neon` y usan el fixture `conn` de `tests/test_panel.py`, que escribe en el esquema `pruebas` por la conexión DIRECTA (el pooler rechaza `options`).
- **Consola cp1252:** nada de emojis ni flechas Unicode en la salida de scripts de Python.

---

### Task 1: El resumen en `panel.py` y su endpoint

**Files:**
- Modify: `src/maxicare_daniela/panel.py` (añadir al final, tras `citas_sin_marcar`)
- Modify: `src/maxicare_daniela/runtime.py:2104` (justo después de `api_sin_resolver`, antes del separador `# Panel: la agenda del día`)
- Modify: `web/src/api.ts` (al final, tras `marcarAsistencia`)
- Test: `tests/test_panel.py` (añadir al final)

**Interfaces:**
- Consumes: `panel.citas_del_dia(conn, *, desde, hasta)`, `persistencia.casos_recientes(conn)`, `persistencia.conectar(url)`, `runtime.usuario_actual`
- Produces: `panel.LINEA_BASE`, `panel.resumen_inicio(conn, *, ahora: datetime, dias: int = 30) -> dict[str, Any]`, `GET /api/inicio`, `api.leerInicio(): Promise<ResumenInicio>`

- [ ] **Step 1: Escribir la prueba que falla**

Al final de `tests/test_panel.py`:

```python
# ------------------------------------------------------------------------------------------
# La portada: los cinco números
# ------------------------------------------------------------------------------------------


@pytest.mark.neon
def test_resumen_inicio_cuenta_personas_y_no_conversaciones(conn):
    """Dos conversaciones del MISMO teléfono son UNA persona.

    Una conversación caduca por inactividad de 24 h, así que una negociación de tres días
    son tres filas en `conversaciones`. Contar filas inflaría el denominador y haría
    parecer a Daniela peor de lo que es.
    """
    ahora = datetime(2026, 9, 22, 15, 0, tzinfo=ZONA_BOGOTA)
    telefono = f"57300{uuid.uuid4().hex[:7]}"
    with conn.cursor() as cur:
        conv = uuid.uuid4()
        cur.execute(
            "INSERT INTO conversaciones (id, telefono, canal, creada_en) VALUES (%s,%s,'whatsapp',%s)",
            (conv, telefono, ahora - timedelta(days=2)),
        )
        for i in range(3):
            cur.execute(
                "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en,"
                " conversacion_id, respondido_en)"
                " VALUES (%s,%s,'text','hola',%s,%s,%s)",
                (f"wamid-{uuid.uuid4().hex}", telefono, ahora - timedelta(days=i),
                 conv, ahora - timedelta(days=i)),
            )
    conn.commit()

    resumen = panel.resumen_inicio(conn, ahora=ahora)

    assert resumen["escribieron"] == 1
    assert resumen["sin_contestar"] == 0
    assert resumen["linea_base"]["citas_mes"] == 2


@pytest.mark.neon
def test_resumen_inicio_solo_mira_citas_cuya_hora_ya_paso(conn):
    """Una cita de mañana no es una inasistencia: es una cita de mañana.

    Contarla en el denominador de «llegaron» convertiría el futuro en un fracaso y haría
    bajar el número cada vez que Daniela agenda a alguien, que es justo lo contrario de lo
    que la pantalla quiere decir.
    """
    ahora = datetime(2026, 9, 22, 15, 0, tzinfo=ZONA_BOGOTA)
    telefono = f"57301{uuid.uuid4().hex[:7]}"
    with conn.cursor() as cur:
        conv = uuid.uuid4()
        cur.execute(
            "INSERT INTO conversaciones (id, telefono, canal, creada_en) VALUES (%s,%s,'whatsapp',%s)",
            (conv, telefono, ahora - timedelta(days=3)),
        )
        for cuando, asistio in (
            (ahora - timedelta(days=2), True),    # vino
            (ahora - timedelta(days=1), None),    # pasó y nadie marcó
            (ahora + timedelta(days=1), None),    # mañana: no cuenta
        ):
            cur.execute(
                "INSERT INTO citas (id, conversacion_id, nombre_completo, telefono,"
                " tratamiento, inicio, duracion_minutos, estado, asistio, creada_en)"
                " VALUES (%s,%s,'Prueba',%s,'valoracion',%s,60,'confirmada',%s,%s)",
                (uuid.uuid4(), conv, telefono, cuando, asistio, ahora - timedelta(days=3)),
            )
    conn.commit()

    resumen = panel.resumen_inicio(conn, ahora=ahora)

    assert resumen["con_cita"] == 1, "tres citas del mismo teléfono son UNA persona"
    assert resumen["asistencia"] == {"llegaron": 1, "marcadas": 1, "cumplibles": 2}


@pytest.mark.neon
def test_resumen_inicio_no_cuenta_como_sin_contestar_lo_que_atendio_un_doctor(conn):
    """Un mensaje que llegó durante un relevo lleva `fallo_respuesta` con prefijo `relevo:`
    sin ser un fallo (no negociable 15). Contarlo convertiría cada relevo --que es el
    sistema funcionando-- en un paciente desatendido."""
    ahora = datetime(2026, 9, 22, 15, 0, tzinfo=ZONA_BOGOTA)
    telefono = f"57302{uuid.uuid4().hex[:7]}"
    with conn.cursor() as cur:
        conv = uuid.uuid4()
        cur.execute(
            "INSERT INTO conversaciones (id, telefono, canal, creada_en, tomada_en,"
            " relevo_cerrado_en) VALUES (%s,%s,'whatsapp',%s,%s,%s)",
            (conv, telefono, ahora - timedelta(days=1),
             ahora - timedelta(minutes=30), ahora - timedelta(minutes=10)),
        )
        for fallo in ("relevo: lo atiende el doctor", None):
            cur.execute(
                "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en,"
                " conversacion_id, fallo_respuesta)"
                " VALUES (%s,%s,'text','hola',%s,%s,%s)",
                (f"wamid-{uuid.uuid4().hex}", telefono, ahora - timedelta(hours=1), conv, fallo),
            )
    conn.commit()

    resumen = panel.resumen_inicio(conn, ahora=ahora)

    assert resumen["sin_contestar"] == 1, "solo el que NO fue de relevo"
    assert resumen["relevo"]["minutos"] == 20
```

- [ ] **Step 2: Correr las pruebas y verlas fallar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_panel.py -q -m neon -k resumen_inicio`
Expected: FAIL con `AttributeError: module 'maxicare_daniela.panel' has no attribute 'resumen_inicio'`

- [ ] **Step 3: Escribir `resumen_inicio` en `panel.py`**

Añadir al final de `src/maxicare_daniela/panel.py`. El import de `timedelta` se suma al de la línea 20: `from datetime import datetime, timedelta`.

```python
# ------------------------------------------------------------------------------------------
# La portada: los cinco números
# ------------------------------------------------------------------------------------------


#: La clínica ANTES de Daniela, medida por MaxiCare sobre un mes de su propio WhatsApp y
#: congelada en `docs/agentes/brief-agentes.json` -> `exito`.
#:
#: Viaja con cada respuesta a propósito. Un número sin su anterior no dice nada: «6 personas
#: escribieron» no es bueno ni malo hasta que al lado pone que antes eran 97 al mes y que de
#: esas 97 solo 2 acababan con cita. Y vive aquí y no en el frontend porque es un dato
#: medido, no una constante de presentación: si alguna vez se corrige, se corrige en el sitio
#: donde se puede escribir por qué.
LINEA_BASE = {
    "conversaciones_mes": 97,
    "citas_mes": 2,
    "sin_responder_pct": 50,
}

#: Cuántos minutos tiene que llevar un mensaje sin respuesta para contarlo como desatendido.
#: Cinco, el mismo margen que `persistencia.contar_sin_responder`: por debajo de eso lo más
#: probable es que el turno esté vivo dentro de la ventana de silencio del búfer.
MARGEN_SIN_CONTESTAR = "5 minutes"


def resumen_inicio(conn, *, ahora: datetime, dias: int = 30) -> dict[str, Any]:
    """Los cinco números de la portada, más el volumen por día y la agenda de hoy.

    SOLO LECTURA, y esa es la diferencia que más importa con `/api/agenda`: aquella
    reconcilia contra Google Calendar al abrirse --puede mover una cita, soltar un cupo y
    reprogramar un recordatorio-- y se quiso así porque abrir la Agenda es el mejor
    disparador que esa reconciliación tiene. Esta pantalla es la PRIMERA de cada sesión: si
    reconciliara, cada ingreso al panel serían escrituras en Neon y llamadas a la API de
    Google, varias veces al día y por cada persona que entre.

    `ahora` no tiene default a propósito. Un `now()` dentro de la consulta convierte
    cualquier prueba en una que envejece, y en este proyecto eso ya ha amanecido en rojo
    tres veces. Quien llama decide qué instante es el presente.

    La unidad es el TELÉFONO y no la conversación. Una conversación caduca por inactividad
    de 24 h (`persistencia.conversacion_viva`), así que una negociación de tres días son
    tres filas y una sola persona: contar filas inflaría el denominador de todas las tasas.

    Devuelve las cifras CRUDAS, nunca porcentajes. Quién se divide entre quién lo decide la
    pantalla, y así el numerador y el denominador viajan los dos -- que es lo que permite
    escribir «llegaron 8 de 12» en vez de un 67 % que no dice sobre cuántas citas se calculó.
    """
    desde = ahora - timedelta(days=dias)
    inicio_del_dia = ahora.astimezone(ZONA_BOGOTA).replace(hour=0, minute=0, second=0, microsecond=0)

    with conn.cursor() as cur:
        # 1. Escribieron: personas distintas, no conversaciones.
        cur.execute(
            "SELECT count(DISTINCT telefono) FROM mensajes_entrantes WHERE recibido_en >= %s",
            (desde,),
        )
        escribieron = cur.fetchone()[0]

        # 2. Quedaron con cita: personas distintas con una cita viva creada en el periodo.
        cur.execute(
            "SELECT count(DISTINCT telefono) FROM citas"
            " WHERE creada_en >= %s AND estado <> 'cancelada'",
            (desde,),
        )
        con_cita = cur.fetchone()[0]

        # 3. Llegaron. Solo citas cuya hora YA pasó: una cita de mañana no es una
        #    inasistencia. Y las tres cifras salen juntas porque `llegaron` sin `marcadas`
        #    miente en cuanto la recepción se retrasa una semana en marcar.
        cur.execute(
            "SELECT count(*) FILTER (WHERE asistio IS TRUE),"
            "       count(*) FILTER (WHERE asistio IS NOT NULL),"
            "       count(*)"
            "  FROM citas"
            " WHERE inicio >= %s AND inicio < %s AND estado <> 'cancelada'",
            (desde, ahora),
        )
        llegaron, marcadas, cumplibles = cur.fetchone()

        # 4. Sin contestar. El `NOT LIKE 'relevo:%%'` saca los mensajes que atendió un doctor
        #    durante un relevo: llevan ese prefijo sin ser un fallo (no negociable 15), y
        #    contarlos convertiría cada relevo en un paciente desatendido. Un
        #    `fallo_respuesta` que NO sea de relevo sí cuenta: al paciente no le llegó nada, y
        #    que el motivo esté anotado no cambia lo que le pasó a él.
        cur.execute(
            "SELECT count(*) FROM mensajes_entrantes"
            " WHERE recibido_en >= %s"
            "   AND recibido_en < %s - interval '" + MARGEN_SIN_CONTESTAR + "'"
            "   AND respondido_en IS NULL"
            "   AND (fallo_respuesta IS NULL OR fallo_respuesta NOT LIKE 'relevo:%%')",
            (desde, ahora),
        )
        sin_contestar = cur.fetchone()[0]

        # 5. El tiempo del doctor. `relevo.cerrar` pone `tomada_por = NULL` pero no toca
        #    `tomada_en`, así que la duración sobrevive al cierre; un relevo todavía abierto
        #    cuenta hasta `ahora`.
        #
        #    SUBCUENTA, y hay que saberlo: `activar_relevo` limpia `relevo_cerrado_en` al
        #    reactivar, así que un segundo relevo en la misma conversación borra el rastro
        #    del primero. Se prefiere un número bajo y honesto a uno inventado.
        cur.execute(
            "SELECT coalesce(sum(EXTRACT(EPOCH FROM"
            "         (coalesce(relevo_cerrado_en, %s) - tomada_en))) / 60, 0)::int,"
            "       count(*)"
            "  FROM conversaciones WHERE tomada_en >= %s",
            (ahora, desde),
        )
        minutos_doctor, conversaciones_con_relevo = cur.fetchone()

        # El volumen por día, en hora de Bogotá. Sin `AT TIME ZONE` el corte del día lo pone
        # el reloj del servidor y las barras se desplazan respecto de lo que vivió la clínica.
        cur.execute(
            "SELECT date(creada_en AT TIME ZONE 'America/Bogota') AS dia, count(*)"
            "  FROM conversaciones"
            " WHERE creada_en >= %s AND canal = 'whatsapp'"
            " GROUP BY 1 ORDER BY 1",
            (desde,),
        )
        volumen = [{"dia": d.isoformat(), "conversaciones": n} for d, n in cur.fetchall()]

    return {
        "desde": desde.isoformat(),
        "dias": dias,
        "escribieron": escribieron,
        "con_cita": con_cita,
        "asistencia": {
            "llegaron": llegaron,
            "marcadas": marcadas,
            "cumplibles": cumplibles,
        },
        "sin_contestar": sin_contestar,
        "relevo": {
            "minutos": minutos_doctor,
            "conversaciones": conversaciones_con_relevo,
        },
        "linea_base": LINEA_BASE,
        "volumen": volumen,
        "agenda_hoy": citas_del_dia(
            conn, desde=inicio_del_dia, hasta=inicio_del_dia + timedelta(days=1)
        ),
    }
```

- [ ] **Step 4: Correr las pruebas y verlas pasar**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_panel.py -q -m neon -k resumen_inicio`
Expected: 3 passed

- [ ] **Step 5: Añadir el endpoint**

En `src/maxicare_daniela/runtime.py`, justo después del `return {"casos": casos, "es_admin": ...}` de `api_sin_resolver` (línea 2104) y antes del separador `# Panel: la agenda del día`:

```python
@app.get("/api/inicio")
async def api_inicio(quien: dict = Depends(usuario_actual)) -> dict:
    """La portada. SOLO LECTURA: ni una escritura, ni una llamada a Google.

    Es la diferencia deliberada con `/api/agenda`, que reconcilia contra Calendar al
    abrirse. Esta es la primera pantalla de cada sesión, así que reconciliar aquí
    convertiría cada ingreso al panel en escrituras en Neon y llamadas a la API de Google.
    Quien quiera el día contrastado entra a Agenda, que es donde vive esa promesa.

    Los tres casos de «necesita su atención» se piden aquí y no dentro de `resumen_inicio`
    por el mismo motivo por el que `/api/sin-resolver` los pide así: `casos_recientes` ya
    filtra los teléfonos en el servidor (`sin_resolver.sin_telefonos`) y no hay nada que
    componer, solo dos lecturas que caben en la misma conexión.
    """
    with persistencia.conectar(config.database_url) as conn:
        resumen = panel.resumen_inicio(conn, ahora=_ahora_en_bogota())
        casos = persistencia.casos_recientes(conn)
    resumen["atencion"] = casos[:3]
    resumen["usuario"] = quien["nombre"]
    return resumen
```

**El reloj sale de `_ahora_en_bogota()` (`runtime.py:2117`), nunca de un `datetime.now()`
escrito aquí.** Esa función existe precisamente como costura para que una prueba offline
pueda clavar el presente; `api_agenda` ya la usa por el mismo motivo. Escribir el `now()` a
mano dejaría este endpoint fuera de esa costura y sería la cuarta vez que una fecha suelta
envejece en este proyecto.

`datetime` y `ZONA_BOGOTA` ya están importados en `runtime.py` (líneas 46 y 78): no hay que
añadir nada.

- [ ] **Step 6: Escribir la prueba offline del endpoint**

En `tests/test_panel.py`, al final. **No lleva `@pytest.mark.neon`**: es la que `uv run pytest -q` sí corre.

```python
class _ConexionMuda:
    """Una conexión que no conecta: esta prueba dobla las dos lecturas, así que nadie
    debería llegar a pedirle un cursor."""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): raise AssertionError("esta prueba no debería consultar la base")


def test_api_inicio_no_toca_el_calendario(monkeypatch):
    """La invariante que sostiene toda la decisión: esta pantalla es la PRIMERA de cada
    sesión, así que un solo `calendario.*` en este camino son llamadas a Google en cada
    ingreso al panel, varias veces al día y por cada persona que entre.

    Entra CON sesión, por `dependency_overrides`, y eso no es un detalle: comprobar el 401
    de un anónimo dejaría el cuerpo del endpoint sin ejecutar y la prueba pasaría sin
    ejercer nada. Verde por el motivo equivocado es el fallo que este proyecto ya pagó una
    vez (no negociable 28).
    """
    llamadas: list[str] = []

    class CalendarioEspia:
        def __getattr__(self, nombre):
            llamadas.append(nombre)
            raise AssertionError(f"/api/inicio llamó al calendario: {nombre}")

    # El objetivo es `runtime._calendario`, la instancia global que se construye al
    # arrancar (`runtime.py:250`) -- `runtime.calendario` NO existe: el módulo hace
    # `from .calendario import (...)` y solo trae nombres sueltos, así que un espía puesto
    # ahí con `raising=False` no vigilaría nada y la prueba pasaría vacía.
    monkeypatch.setattr(runtime, "_calendario", CalendarioEspia())
    monkeypatch.setattr(
        runtime.panel, "resumen_inicio",
        lambda conn, **kw: {"escribieron": 0, "con_cita": 0,
                            "asistencia": {"llegaron": 0, "marcadas": 0, "cumplibles": 0},
                            "sin_contestar": 0, "relevo": {"minutos": 0, "conversaciones": 0},
                            "linea_base": panel.LINEA_BASE, "volumen": [], "agenda_hoy": []},
    )
    monkeypatch.setattr(runtime.persistencia, "casos_recientes", lambda conn: [])
    monkeypatch.setattr(runtime.persistencia, "conectar", lambda url: _ConexionMuda())

    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        r = TestClient(runtime.app).get("/api/inicio")
        assert r.status_code == 200, r.text
        assert r.json()["usuario"] == "Prueba", "el saludo sale del servidor, no del front"
    finally:
        runtime.app.dependency_overrides.clear()

    assert llamadas == [], "la portada no reconcilia: eso es de /api/agenda"


def test_api_inicio_es_de_solo_lectura():
    """Ni un `commit` en el camino. `GET /api/agenda` escribe a propósito y está
    documentado; este no, y la diferencia tiene que quedar vigilada."""
    escrituras: list[str] = []

    class ConexionQueDelata(_ConexionMuda):
        def commit(self): escrituras.append("commit")
        def execute(self, *a, **k): escrituras.append("execute")

    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        import unittest.mock as mock
        with mock.patch.object(runtime.persistencia, "conectar",
                               lambda url: ConexionQueDelata()), \
             mock.patch.object(runtime.panel, "resumen_inicio", lambda conn, **kw: {}), \
             mock.patch.object(runtime.persistencia, "casos_recientes", lambda conn: []):
            assert TestClient(runtime.app).get("/api/inicio").status_code == 200
    finally:
        runtime.app.dependency_overrides.clear()

    assert escrituras == []
```

`_como` ya existe en `tests/test_panel.py:342`; no se duplica.

- [ ] **Step 7: Correr la suite offline**

Run: `uv run pytest -q`
Expected: todo en verde, con una prueba más que antes.

- [ ] **Step 8: Añadir `leerInicio` a `api.ts`**

Al final de `web/src/api.ts`:

```ts
// ------------------------------------------------------------------------------------------
// La portada
// ------------------------------------------------------------------------------------------

/** Lo que pinta la portada.
 *
 * Las cifras llegan CRUDAS y los porcentajes se calculan aquí: así `llegaron` viaja con
 * `marcadas` y `cumplibles` al lado, que es lo que permite escribir «8 de 12 marcadas» en
 * vez de un 67 % que no dice sobre cuántas citas se calculó.
 *
 * La respuesta trae más claves de las que se declaran aquí; esta es la convención del
 * archivo (ver `CitaDeAgenda`): solo lo que la pantalla pinta. */
export type ResumenInicio = {
  usuario: string
  dias: number
  escribieron: number
  con_cita: number
  asistencia: { llegaron: number; marcadas: number; cumplibles: number }
  sin_contestar: number
  relevo: { minutos: number; conversaciones: number }
  linea_base: { conversaciones_mes: number; citas_mes: number; sin_responder_pct: number }
  volumen: { dia: string; conversaciones: number }[]
  agenda_hoy: CitaDeAgenda[]
  atencion: { huella: string; contador: number; informe: string | null }[]
}

export async function leerInicio(): Promise<ResumenInicio> {
  return pedir<ResumenInicio>('/api/inicio')
}
```

- [ ] **Step 9: Comprobar que compila y commit**

Run: `cd web && npm run build`
Expected: sin errores de `tsc`

```bash
git add src/maxicare_daniela/panel.py src/maxicare_daniela/runtime.py web/src/api.ts tests/test_panel.py docs/superpowers/specs/2026-09-22-pantalla-inicio-design.md docs/superpowers/plans/2026-09-22-pantalla-inicio.md docs/diseno/
git commit -m "feat(inicio): los cinco numeros de la portada salen de Neon

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: El sidebar oscuro y la sección Inicio

**Files:**
- Modify: `web/src/componentes/Sidebar.tsx` (reescritura visual completa)
- Modify: `web/src/App.tsx:11-21,60-89`
- Modify: `web/src/index.css:9`
- Create: `web/src/pantallas/Inicio.tsx`

**Interfaces:**
- Consumes: `api.leerInicio()`, `api.SesionCaducada`, `Marca.SimboloSinPiloto`, `Marca.PalabraSinPiloto`
- Produces: `SeccionId` con `'inicio'`, `<Inicio alCaducarSesion={() => void} />`

- [ ] **Step 1: Ampliar el import de fuentes**

`web/src/index.css`, línea 9. Sustituir la línea del `@import` de Google Fonts por:

```css
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Sora:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
```

Los tokens `--color-sp-*` del bloque `@theme` no se tocan.

- [ ] **Step 2: Añadir `'inicio'` al tipo y a las secciones**

`web/src/componentes/Sidebar.tsx`:

```ts
export type SeccionId =
  | 'inicio'
  | 'bandeja'
  | 'agenda'
  | 'leads'
  | 'tratamientos'
  | 'metricas'
  | 'estado'
  | 'configuracion'
  | 'pruebas'
```

y como primera entrada de `SECCIONES`:

```ts
  { id: 'inicio', etiqueta: 'Inicio', icono: '◇', fase: null },
```

- [ ] **Step 3: Pasar el sidebar a oscuro**

Reescribir el cuerpo visual de `Sidebar.tsx` siguiendo `docs/diseno/panel-2026-09-22.dc.html` líneas 40-135. Los valores exactos, para no tener que deducirlos:

| Elemento | Valor |
|---|---|
| Fondo del `<aside>` | `#0B0912` |
| Texto | `#EDEAF4` |
| Ancho | `clamp(230px, 18vw, 272px)` |
| Fuente de los botones | `'Sora', sans-serif`, 14.5px, peso 400, `letter-spacing: -0.008em` |
| Botón activo: barra izquierda | 3px de ancho, `#A78BFA`, `transition: height 220ms cubic-bezier(.22,1,.36,1)` |
| Hover | `background: rgba(255,255,255,0.08)`, `color: #FFFFFF` |
| Altura mínima del botón | 44px |
| Separador y bordes | `1px solid rgba(255,255,255,0.1)` |
| Rótulos monoespaciados (fase, rol) | `'JetBrains Mono', monospace`, 9-10.5px, `letter-spacing: 0.16em`, `text-transform: uppercase`, `#8B84A0` |

Tres cosas que se conservan tal cual del sidebar de hoy:
- La insignia **TEST** de la sección Pruebas, con esas palabras. La especificación la pide explícitamente: «nunca un interruptor escondido en un menú».
- La etiqueta `F{fase}` en las secciones pendientes, con su `title="La construye la fase N"`. Las pendientes no se esconden del menú.
- El pie con iniciales, rol y cerrar sesión.

El pie añade, bajo el separador, el logo de Sinpiloto con el rótulo «Operado por». **Reutiliza los componentes que ya existen y nadie usa** — `SimboloSinPiloto` y `PalabraSinPiloto` de `@/componentes/Marca`, cuyos trazados vienen de Figma con la geometría original. No se dibujan SVG nuevos ni se copian los del archivo de diseño.

Los emojis de `SECCIONES` se sustituyen por los iconos SVG de trazo del archivo de diseño (`stroke="currentColor"`, `stroke-width="1.7"`, `stroke-linecap="square"`, 17x17): están en las líneas 62, 68, 75, 81, 88, 94, 101, 109 y 115, en el mismo orden del menú.

- [ ] **Step 4: Comprobar el sidebar a ojo**

Run: `cd web && npm run dev` (con `uv run uvicorn maxicare_daniela.runtime:app --port 8080` levantado aparte)
Expected: el menú se ve oscuro, «Inicio» es la primera entrada y lleva a la pantalla pendiente, las demás secciones siguen funcionando, la insignia TEST sigue ahí.

- [ ] **Step 5: Escribir `Inicio.tsx` con los cinco números**

Crear `web/src/pantallas/Inicio.tsx`. Copia la estructura de `SinResolver.tsx`: `const SP`, `Props`, formateadores fuera del componente, `<Cabecera />` repetida en los tres estados, y **la `ref` para `alCaducarSesion` con `useCallback` de dependencias vacías** — meter esa prop en las dependencias deja la pantalla releyendo Neon en bucle, porque `App.tsx` la pasa como una flecha nueva en cada render, y no hay ninguna prueba que lo atrape.

El corazón de la pantalla es esta función, que es donde vive la regla de los estados vacíos:

```tsx
type Tarjeta = { rotulo: string; valor: string; pie: string; barra: number | null }

/** Las cinco tarjetas, con sus estados vacíos resueltos ANTES que los llenos.
 *
 * Hoy la clínica lleva seis días funcionando y tiene cero citas: ese es el estado en que
 * MaxiCare va a ver esta pantalla las próximas semanas, así que es el que tiene que quedar
 * bien. Un cero no es un mal resultado y la pantalla no puede sugerir que lo sea -- por eso
 * «llegaron» con cero citas cumplidas dice «sin citas cumplidas todavía» y no `0 %`, que se
 * lee como un fracaso.
 *
 * `barra` es null cuando no hay denominador: una barra de progreso sobre cero es una barra
 * vacía, y una barra vacía dice «fallaste», no «todavía no». */
function tarjetas(r: ResumenInicio): Tarjeta[] {
  const { llegaron, marcadas, cumplibles } = r.asistencia
  return [
    {
      rotulo: 'Personas que escribieron',
      valor: String(r.escribieron),
      pie: `antes: ${r.linea_base.conversaciones_mes} conversaciones al mes`,
      barra: null,
    },
    {
      rotulo: 'Quedaron con cita',
      valor: String(r.con_cita),
      pie: r.escribieron
        ? `${r.con_cita} de ${r.escribieron} · antes: ${r.linea_base.citas_mes} al mes`
        : `antes: ${r.linea_base.citas_mes} al mes`,
      barra: r.escribieron ? r.con_cita / r.escribieron : null,
    },
    {
      rotulo: 'Llegaron a la cita',
      valor: cumplibles === 0 ? '—' : marcadas === 0 ? '—' : String(llegaron),
      pie:
        cumplibles === 0
          ? 'sin citas cumplidas todavía'
          : marcadas === 0
            ? `ninguna de ${cumplibles} marcada todavía`
            : `${marcadas} de ${cumplibles} marcadas`,
      barra: marcadas ? llegaron / marcadas : null,
    },
    {
      rotulo: 'Sin contestar',
      valor: String(r.sin_contestar),
      pie:
        r.sin_contestar === 0
          ? `ninguno · antes quedaba sin respuesta el ${r.linea_base.sin_responder_pct} %`
          : 'hay mensajes esperando respuesta',
      barra: null,
    },
    {
      rotulo: 'Tu tiempo',
      valor: `${r.relevo.minutos} min`,
      pie:
        r.relevo.conversaciones === 0
          ? 'no tuviste que entrar en ninguna conversación'
          : `en ${r.relevo.conversaciones} conversaciones`,
      barra: null,
    },
  ]
}
```

La rejilla y las tarjetas copian el bloque `aria-label="Indicadores de hoy"` del archivo de diseño (líneas 212-250). Valores exactos: tarjeta `background:#FFFFFF`, `border:1px solid #DCD8E6`, `padding:18px 18px 16px`; rótulo en JetBrains Mono 10.5px `letter-spacing:0.15em` uppercase `#6E6880`; cifra en Space Grotesk 600, `clamp(30px,3.2vw,40px)`, `letter-spacing:-0.03em`, `#16111F`; barra de 3px sobre `#EDE9FE` con relleno `#7C3AED`; pie 13px peso 300 `#4A4458`. Rejilla: `repeat(auto-fit, minmax(min(100%,208px), 1fr))`, `gap: clamp(12px,1.4vw,18px)`.

La cabecera lleva el saludo con el nombre real (`r.usuario`, que viene del servidor), el punto verde `#16A34A` con la animación `mcPulse`, y la frase de la ventana: «Últimos 30 días». **No copiar el texto «Resumen de las últimas 24 horas» del mockup**: la ventana real es de 30 días y las cinco cifras la comparten.

- [ ] **Step 6: Cablear la ruta en `App.tsx`**

Importar la pantalla, y en `seccionDelHash` cambiar el destino por defecto de `'agenda'` a `'inicio'`:

```tsx
import Inicio from '@/pantallas/Inicio'
// ...
  return TODAS.some((s) => s.id === id) ? id : 'inicio'
```

Y añadir la rama como primer ternario del bloque de render, antes de `activa === 'agenda'`:

```tsx
      {activa === 'inicio' ? (
        // Pantalla de solo lectura, como SinResolver: vuelve al ingreso por el mismo camino
        // si la sesión caduca a mitad de la mañana.
        <Inicio alCaducarSesion={() => setSesion(null)} />
      ) : activa === 'agenda' ? (
```

- [ ] **Step 7: Verificar a ojo y con la build**

Run: `cd web && npm run build`
Expected: sin errores de `tsc`

Con el panel levantado, entrar y comprobar: la portada es Inicio, se ven las cinco tarjetas, **«Llegaron» dice «sin citas cumplidas todavía» y no `0 %`** (hoy hay 0 citas en la base), y el nombre del saludo es el del usuario que entró.

- [ ] **Step 8: Commit**

```bash
git add web/src/componentes/Sidebar.tsx web/src/pantallas/Inicio.tsx web/src/App.tsx web/src/index.css
git commit -m "feat(inicio): la portada y el sidebar oscuro

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Los tres bloques de abajo

**Files:**
- Modify: `web/src/pantallas/Inicio.tsx`

**Interfaces:**
- Consumes: `ResumenInicio.agenda_hoy`, `ResumenInicio.atencion`, `ResumenInicio.volumen`
- Produces: nada que use otra tarea

- [ ] **Step 1: La agenda de hoy**

Bajo las tarjetas, una lista con la hora, el nombre y el tratamiento de cada cita de `r.agenda_hoy`, siguiendo el bloque «La agenda de hoy» del archivo de diseño (líneas 255-285). Cabecera con el recuento («6 citas»).

Estado vacío, que hoy es el caso normal: «No hay citas para hoy.» Y un enlace «Ver agenda» que lleva a `#/agenda` — **desde ahí, y no desde aquí, es donde se reconcilia contra Google**.

- [ ] **Step 2: Qué necesita su atención**

Los tres casos de `r.atencion`, con su `contador` a la izquierda y el título legible a la derecha. **Reutilizar la función `titulo()` de `SinResolver.tsx`**: traduce la `huella` cruda a una frase para la clínica, y duplicarla dejaría dos traducciones que se separan en silencio. Extraerla a `web/src/pantallas/SinResolver.tsx` como `export function titulo(...)` e importarla aquí.

Estado vacío: «Nada sin resolver. Buena señal.», el mismo tono que ya usa SinResolver.

Enlace «Ver todo» a `#/bandeja`.

- [ ] **Step 3: El volumen por día**

Barras verticales pintadas con divs, altura proporcional al máximo de `r.volumen`. Sin librería de gráficos: no hay ninguna en `package.json` y una dependencia nueva para ocho barras no se paga.

**El rótulo dice el primer día que tiene datos, no «14 días»**: `desde el 16 de septiembre`. Un eje de catorce días con diez vacíos dibuja una caída que nunca ocurrió. Si `r.volumen` está vacío, el bloque entero no se pinta.

- [ ] **Step 4: Verificar**

Run: `cd web && npm run build`
Expected: sin errores

A ojo, con el panel levantado: los tres bloques aparecen, la agenda dice que no hay citas, «atención» muestra los casos reales, y el gráfico rotula el 16 de septiembre.

- [ ] **Step 5: La suite completa**

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_panel.py
cd web && npm run build
```

Las de Neon son obligatorias: `pytest -q` a secas no caza las pruebas de panel, y una colisión de helper ya dejó dos en rojo durante tres commits con la suite offline entera en verde.

- [ ] **Step 6: Commit**

```bash
git add web/src/pantallas/Inicio.tsx web/src/pantallas/SinResolver.tsx
git commit -m "feat(inicio): la agenda del dia, lo que necesita atencion y el volumen

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Después de las tres tareas

Actualizar `web/CLAUDE.md` con dos trampas nuevas, que es donde este proyecto guarda lo que cuesta caro volver a aprender:

1. **`GET /api/inicio` NO reconcilia, y `GET /api/agenda` SÍ.** Es la asimetría deliberada: Inicio es la primera pantalla de cada sesión y reconciliar ahí serían llamadas a Google en cada ingreso al panel.
2. **La unidad de la portada es el teléfono, no la conversación**, porque una conversación caduca a las 24 h y contar filas infla el denominador de todas las tasas.

Y añadir `resumen_inicio` a la lista de funciones cuyo cambio obliga a correr `-m neon`.

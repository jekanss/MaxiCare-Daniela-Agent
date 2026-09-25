# Configuración y Estado del sistema — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cerrar el panel: eliminar Métricas y construir las dos pantallas que faltaban, Configuración y Estado del sistema.

**Architecture:** Backend primero, frontend después. Configuración estrena la primera escritura de la tabla `configuracion` copiando el patrón de auditoría de `panel.cambiar_tratamiento` (una transacción, una fila de bitácora por perilla, nada si el valor no cambia) y refrescando los globals del proceso tras el commit. Estado del sistema separa lo barato —`GET /api/estado`, proceso y Neon— de las sondas a Meta y Telegram, que van en un `POST` detrás de un botón.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pytest (marca `neon`) · React 19 + Vite + Tailwind, TypeScript.

**Spec:** `docs/superpowers/specs/2026-09-25-panel-estado-configuracion-design.md`

## Global Constraints

- **Las rutas nuevas se declaran ANTES del catch-all** `GET /{ruta_completa:path}` que cierra `runtime.py`. Declaradas después devuelven `index.html` y producen un `SyntaxError` de JSON en la consola del navegador, sin pista de la ruta.
- **Ningún cambio se escribe sin su fila en `cambios_configuracion`, y los dos van en la MISMA transacción.** Es la regla que atraviesa `panel.py`. `_anotar(cur, ...)` recibe un cursor, no una conexión, y eso es lo que la garantiza.
- **`panel.py` y `persistencia.py` NO importan `runtime.py` ni FastAPI.** Reciben una conexión y devuelven diccionarios. Lo vigila `tests/test_estructura.py`.
- **Las pruebas que tocan Neon llevan `@pytest.mark.neon`** y escriben en el esquema `pruebas`. `uv run pytest -q` a secas NO las corre, y el no negociable 8 dice que tampoco caza `tests/test_panel.py`.
- **La consola de Windows es cp1252:** los scripts empiezan con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y usan marcadores ASCII (`OK` / `FALLA` / `->`).
- **Los heredoc de Bash fallan en este entorno.** Para crear un archivo, la herramienta `Write`; para modificar uno existente, `Edit` (PowerShell destroza el encoding del repo).
- **No se registran cédulas ni documentos de identidad.** No aplica a este trabajo, pero ninguna pantalla nueva puede introducir ese campo.
- **Lo que no se sabe se marca con el literal `"PENDIENTE"`**, nunca con un valor plausible.

## Review Focus

Cinco entradas que la spec implica y que ninguna tarea probaría por defecto. Cada una tiene su prueba asignada a la tarea que posee el código.

1. **`atiende_domingo` llegando como booleano JSON (`true`) en vez de `0`/`1`.** Se guardaría como `'True'` en una columna `TEXT`, y `leer_configuracion` lo descarta **en silencio** en su `int(valor)`: la clínica se queda sin domingos y no hay un error en ningún log. → Tarea 1, paso 3.
2. **`PATCH` con cuerpo vacío o con valores idénticos a los guardados.** No puede escribir ni un `UPDATE` ni una fila de bitácora: abrir la pantalla y pulsar Guardar ensuciaría el historial con trece filas que no cambiaron nada. → Tarea 1, paso 7.
3. **`PATCH` con una clave desconocida** (un `name` mal escrito en el formulario). Tiene que devolver 422, no ignorarla: una perilla que se pierde en silencio es indistinguible de una que se guardó. → Tarea 2, pasos 1 y 3.
4. **`GET /api/estado` con Neon caído.** La pantalla que existe para saber qué está roto tiene que cargar precisamente cuando algo lo está: devuelve `base.ok = false` y el resto de bloques que no dependen de la base, nunca un 500. → Tarea 4, pasos 1 y 3.
5. **`POST /api/estado/sondas` sin credenciales o con Meta caída.** Cada bloque en su propio `try`: que la Graph API no conteste no puede dejar sin respuesta lo de Telegram. → Tarea 4, paso 5.

---

## Tarea 1: `panel.guardar_configuracion` y `panel.configuracion_editable`

**Files:**
- Modify: `src/maxicare_daniela/panel.py` (bloque nuevo al final, antes del bloque de conversaciones)
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `panel._anotar(cur, *, tabla, clave, anterior, nuevo, usuario)` — ya existe, `panel.py:79`.
- Produces:
  - `panel.CLAVES_EDITABLES: dict[str, tuple[int, int]]` — clave → `(minimo, maximo)`
  - `panel.CLAVES_FIJAS: dict[str, str]` — clave → por qué no se edita
  - `panel.configuracion_editable(conn) -> dict[str, Any]`
  - `panel.guardar_configuracion(conn, cambios: dict[str, int], *, usuario: str) -> dict[str, Any]`

- [ ] **Step 1: Escribir las pruebas que fallan (rangos e invariantes, sin base)**

En `tests/test_panel.py`, al final:

```python
# ------------------------------------------------------------------------------------------
# Configuración operativa
# ------------------------------------------------------------------------------------------


def test_las_trece_claves_editables_y_ni_una_mas():
    """Las dos fijas no pueden colarse en la lista editable: `telegram_topic_general` manda
    los escalamientos a un tema que no existe, y `medicion_sin_resolver_desde` es la única
    clave no entera de la tabla."""
    assert len(panel.CLAVES_EDITABLES) == 13
    assert "telegram_topic_general" not in panel.CLAVES_EDITABLES
    assert "medicion_sin_resolver_desde" not in panel.CLAVES_EDITABLES
    assert set(panel.CLAVES_FIJAS) == {"telegram_topic_general", "medicion_sin_resolver_desde"}


def test_toda_clave_editable_existe_en_los_defaults():
    """Una clave editable que no esté en la tabla no se puede escribir: `guardar_configuracion`
    hace UPDATE, nunca INSERT."""
    for clave in panel.CLAVES_EDITABLES:
        assert clave in persistencia.CONFIGURACION_POR_DEFECTO, clave
```

- [ ] **Step 2: Correr y ver que fallan**

Run: `uv run pytest -q tests/test_panel.py -k configuracion`
Expected: FAIL — `AttributeError: module 'maxicare_daniela.panel' has no attribute 'CLAVES_EDITABLES'`

- [ ] **Step 3: Escribir las constantes y la validación por campo**

En `src/maxicare_daniela/panel.py`, al final del archivo:

```python
# ------------------------------------------------------------------------------------------
# Configuración operativa
# ------------------------------------------------------------------------------------------

#: Las trece perillas que el panel deja tocar, con su rango. El rango se comprueba aquí y
#: otra vez en el modelo Pydantic de la ruta: el de arriba da un 422 legible al formulario,
#: este protege a quien llame a la función desde un script.
#:
#: `tope_diario_reactivacion` y `max_reactivaciones_12m` admiten `0` a propósito: es la forma
#: de frenar la reactivación desde la pantalla sin tocar el `.env` del VPS.
CLAVES_EDITABLES: dict[str, tuple[int, int]] = {
    "capacidad_por_hora": (1, 6),
    "duracion_cita_minutos": (15, 180),
    "hora_apertura": (0, 23),
    "hora_cierre": (0, 23),
    "hora_cierre_sabado": (0, 23),
    "atiende_domingo": (0, 1),
    "aviso_relevo_minutos": (5, 1440),
    "cierre_relevo_minutos": (10, 1440),
    "hora_recordatorio_vispera": (0, 23),
    "horas_minimas_para_recordar": (1, 48),
    "tope_diario_reactivacion": (0, 50),
    "max_reactivaciones_12m": (0, 24),
    "max_seguimientos_fallidos": (1, 10),
}

#: Las dos que se VEN y no se editan. El texto es lo que lee quien busca la perilla y no la
#: encuentra: esconderlas haría que la buscara en vano.
CLAVES_FIJAS: dict[str, str] = {
    "telegram_topic_general": (
        "La fija la migración 005. Un valor equivocado manda los escalamientos a un tema "
        "que no existe, y Telegram acepta el envío sin error: nadie los lee y nadie se entera."
    ),
    "medicion_sin_resolver_desde": (
        "La escribe la migración 029 una sola vez. Es desde cuándo cuentan los casos de «sin "
        "resolver»; reescribirla borraría el sentido de «3 veces» en esa pantalla."
    ),
}


def _entero_de_configuracion(clave: str, valor: Any) -> int:
    """El valor que va a la tabla, o `ValueError`.

    **Un booleano NO vale, ni siquiera para `atiende_domingo`.** `True` pasa cualquier
    `isinstance(v, int)` --en Python un bool ES un int-- y acabaría en la columna como el
    texto `'True'`, que `persistencia.leer_configuracion` descarta EN SILENCIO en su
    `int(valor)`: la clínica se quedaría sin domingos sin un error en ningún log. La
    migración 012 ya avisó de esto por escrito; aquí se hace cumplir.
    """
    if clave not in CLAVES_EDITABLES:
        raise ValueError(f"'{clave}' no es una perilla que el panel pueda cambiar.")
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ValueError(f"'{clave}' tiene que ser un número entero, no {type(valor).__name__}.")
    minimo, maximo = CLAVES_EDITABLES[clave]
    if not minimo <= valor <= maximo:
        raise ValueError(f"'{clave}' tiene que estar entre {minimo} y {maximo}.")
    return valor
```

- [ ] **Step 4: Correr las dos pruebas**

Run: `uv run pytest -q tests/test_panel.py -k configuracion`
Expected: PASS

- [ ] **Step 5: Probar las invariantes cruzadas (fallan)**

```python
def test_el_aviso_del_relevo_no_puede_pasar_del_cierre():
    """Se comprueba contra el estado RESULTANTE, no contra el que llega: subir solo el aviso
    es válido campo a campo y deja avisando de un relevo que ya se cerró."""
    with pytest.raises(ValueError, match="aviso"):
        panel._comprobar_invariantes({"aviso_relevo_minutos": 200, "cierre_relevo_minutos": 180})


def test_el_cierre_tiene_que_ir_despues_de_la_apertura():
    with pytest.raises(ValueError, match="cierre"):
        panel._comprobar_invariantes({"hora_apertura": 18, "hora_cierre": 17,
                                      "hora_cierre_sabado": 15})


def test_una_combinacion_sana_no_se_queja():
    panel._comprobar_invariantes({
        "aviso_relevo_minutos": 120, "cierre_relevo_minutos": 180,
        "hora_apertura": 8, "hora_cierre": 17, "hora_cierre_sabado": 15,
    })
```

Run: `uv run pytest -q tests/test_panel.py -k invariante` → FAIL (`_comprobar_invariantes` no existe)

- [ ] **Step 6: Implementar las invariantes**

```python
def _comprobar_invariantes(resultante: dict[str, int]) -> None:
    """Las tres reglas que solo se ven mirando dos perillas juntas.

    `resultante` es lo que va a quedar en la tabla: lo que llega en la petición mezclado con
    lo que ya había. Validar solo lo que llega deja pasar la combinación rota.
    """
    aviso = resultante.get("aviso_relevo_minutos")
    cierre = resultante.get("cierre_relevo_minutos")
    if aviso is not None and cierre is not None and aviso >= cierre:
        raise ValueError(
            f"El aviso del relevo ({aviso} min) tiene que ir ANTES del cierre ({cierre} min), "
            "o se avisaría de un relevo que ya se cerró solo."
        )

    apertura = resultante.get("hora_apertura")
    for clave, nombre in (("hora_cierre", "entre semana"), ("hora_cierre_sabado", "el sábado")):
        hora_cierre = resultante.get(clave)
        if apertura is not None and hora_cierre is not None and hora_cierre <= apertura:
            raise ValueError(
                f"La hora de cierre {nombre} ({hora_cierre}) tiene que ser posterior a la de "
                f"apertura ({apertura})."
            )
```

Run: `uv run pytest -q tests/test_panel.py -k invariante` → PASS

- [ ] **Step 7: Probar la escritura contra Neon (falla)**

```python
@pytest.mark.neon
def test_guardar_configuracion_escribe_valor_y_bitacora(esquema):
    with psycopg.connect(_url_de_pruebas()) as conn:
        antes = persistencia.leer_configuracion(conn)["capacidad_por_hora"]
        nuevo = antes + 1
        panel.guardar_configuracion(conn, {"capacidad_por_hora": nuevo}, usuario="prueba")

        assert persistencia.leer_configuracion(conn)["capacidad_por_hora"] == nuevo
        with conn.cursor() as cur:
            cur.execute(
                "SELECT valor_anterior, valor_nuevo, usuario FROM cambios_configuracion "
                "WHERE tabla = 'configuracion' AND clave = 'capacidad_por_hora' "
                "ORDER BY id DESC LIMIT 1"
            )
            assert cur.fetchone() == (str(antes), str(nuevo), "prueba")
        panel.guardar_configuracion(conn, {"capacidad_por_hora": antes}, usuario="prueba")


@pytest.mark.neon
def test_guardar_lo_mismo_no_escribe_ni_una_fila(esquema):
    """Abrir la pantalla y pulsar Guardar sin tocar nada no puede dejar trece filas."""
    with psycopg.connect(_url_de_pruebas()) as conn:
        actual = persistencia.leer_configuracion(conn)
        cambios = {c: actual[c] for c in panel.CLAVES_EDITABLES if c in actual}
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM cambios_configuracion")
            antes = cur.fetchone()[0]

        panel.guardar_configuracion(conn, cambios, usuario="prueba")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM cambios_configuracion")
            assert cur.fetchone()[0] == antes


@pytest.mark.neon
def test_una_invariante_rota_no_deja_nada_a_medias(esquema):
    """La transacción entera o nada: si el aviso choca con el cierre, tampoco se guarda la
    capacidad que venía en la misma petición."""
    with psycopg.connect(_url_de_pruebas()) as conn:
        antes = persistencia.leer_configuracion(conn)
        with pytest.raises(ValueError):
            panel.guardar_configuracion(
                conn,
                {"capacidad_por_hora": antes["capacidad_por_hora"] + 1,
                 "aviso_relevo_minutos": 999},
                usuario="prueba",
            )
        assert persistencia.leer_configuracion(conn) == antes


@pytest.mark.neon
def test_atiende_domingo_no_acepta_un_booleano(esquema):
    """Un `true` de JSON acabaría en la columna como 'True' y `leer_configuracion` lo
    descartaría en silencio: la clínica sin domingos y sin un error en ningún log."""
    with psycopg.connect(_url_de_pruebas()) as conn:
        with pytest.raises(ValueError, match="entero"):
            panel.guardar_configuracion(conn, {"atiende_domingo": True}, usuario="prueba")
```

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py -k configuracion` → FAIL

- [ ] **Step 8: Implementar la lectura y la escritura**

```python
def configuracion_editable(conn) -> dict[str, Any]:
    """Lo que la pantalla de Configuración necesita para dibujarse.

    Lee la tabla y no `CONFIGURACION_POR_DEFECTO`: los defaults son el respaldo de un arranque
    en frío, no lo que está guardado.
    """
    valores: dict[str, int] = {}
    descripciones: dict[str, str] = {}
    actualizado: dict[str, str | None] = {}
    fijas: list[dict[str, str]] = []
    with conn.cursor() as cur:
        cur.execute("SELECT clave, valor, descripcion, actualizado_en FROM configuracion")
        for clave, valor, descripcion, cuando in cur.fetchall():
            if clave in CLAVES_EDITABLES:
                try:
                    valores[clave] = int(valor)
                except (TypeError, ValueError):
                    # Una fila corrupta no puede dejar la pantalla en blanco: se cae al
                    # default y el resto de las perillas se siguen pudiendo editar.
                    valores[clave] = persistencia.CONFIGURACION_POR_DEFECTO.get(clave, 0)
                descripciones[clave] = descripcion
                actualizado[clave] = cuando.isoformat() if cuando else None
            elif clave in CLAVES_FIJAS:
                fijas.append({"clave": clave, "valor": valor, "por_que": CLAVES_FIJAS[clave]})
    return {
        "valores": valores,
        "descripciones": descripciones,
        "actualizado_en": actualizado,
        "rangos": {c: list(r) for c, r in CLAVES_EDITABLES.items()},
        "fijas": sorted(fijas, key=lambda f: f["clave"]),
    }


def guardar_configuracion(conn, cambios: dict[str, int], *, usuario: str) -> dict[str, Any]:
    """Escribe las perillas que de verdad cambian, cada una con su fila de bitácora.

    Tres cosas que no son obvias:

    1. **UPDATE, nunca INSERT.** Las trece filas existen desde su migración y `descripcion` es
       NOT NULL: un INSERT desde la pantalla tendría que inventarse una descripción. Si una
       clave no está en la tabla es un error del despliegue, y se dice.
    2. **`actualizado_en` se pone a mano.** El DEFAULT de la columna solo actúa en el INSERT.
    3. **El valor va a la bitácora tal cual**, no traducido a palabras como los booleanos de
       `cambiar_tratamiento`: aquí la columna guarda un entero en texto, y '0' es lo que de
       verdad quedó escrito.
    """
    limpios = {clave: _entero_de_configuracion(clave, valor) for clave, valor in cambios.items()}
    if not limpios:
        return configuracion_editable(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT clave, valor FROM configuracion WHERE clave = ANY(%s) FOR UPDATE",
            (list(CLAVES_EDITABLES),),
        )
        actuales: dict[str, int] = {}
        for clave, valor in cur.fetchall():
            try:
                actuales[clave] = int(valor)
            except (TypeError, ValueError):
                continue

        faltan = [c for c in limpios if c not in actuales]
        if faltan:
            raise ValueError(
                f"La base no tiene la fila de {', '.join(sorted(faltan))}. Corre "
                "scripts/inicializar_base.py antes de tocar la configuración."
            )

        _comprobar_invariantes({**actuales, **limpios})

        for clave, valor in limpios.items():
            if actuales[clave] == valor:
                continue
            cur.execute(
                "UPDATE configuracion SET valor = %s, actualizado_en = now() WHERE clave = %s",
                (str(valor), clave),
            )
            _anotar(cur, tabla="configuracion", clave=clave,
                    anterior=str(actuales[clave]), nuevo=str(valor), usuario=usuario)
    conn.commit()
    return configuracion_editable(conn)
```

- [ ] **Step 9: Correr todo lo de esta tarea**

Run: `uv run pytest -q tests/test_panel.py` y luego
`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py`
Expected: PASS las dos.

- [ ] **Step 10: Commit**

```bash
git add src/maxicare_daniela/panel.py tests/test_panel.py
git commit -m "feat(panel): la configuracion operativa se puede escribir, con su bitacora"
```

---

## Tarea 2: las rutas de configuración

**Files:**
- Modify: `src/maxicare_daniela/runtime.py` (junto a las rutas de tratamientos, ~línea 2190)
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `panel.configuracion_editable`, `panel.guardar_configuracion`, `panel.CLAVES_EDITABLES` (Tarea 1); `runtime.exigir_rol`, `runtime.usuario_actual`, `runtime._cargar_configuracion_operativa` — ya existen.
- Produces: `GET /api/configuracion` y `PATCH /api/configuracion`.

- [ ] **Step 1: Escribir la prueba de la ruta (falla)**

El molde de autenticación de este proyecto es `_como(rol)` con `dependency_overrides`, **no una
fixture de cliente** — ya está en `tests/test_panel.py:399`. Y estas dos pruebas van **sin
Neon**: el rechazo lo decide Pydantic y el 403 lo decide `exigir_rol`, así que ninguna de las
dos llega a tocar la base.

```python
def test_la_ruta_de_configuracion_rechaza_una_clave_desconocida():
    """422 y no un 200 que la ignore: una perilla que se pierde en silencio es
    indistinguible de una que se guardó."""
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        r = TestClient(runtime.app).patch("/api/configuracion", json={"capacidad_por_hroa": 3})
        assert r.status_code == 422
    finally:
        runtime.app.dependency_overrides.clear()


def test_solo_admin_cambia_la_configuracion():
    for rol in ("doctor", "recepcion"):
        runtime.app.dependency_overrides[runtime.usuario_actual] = _como(rol)
        try:
            r = TestClient(runtime.app).patch("/api/configuracion", json={"capacidad_por_hora": 3})
            assert r.status_code == 403, rol
        finally:
            runtime.app.dependency_overrides.clear()
```

Run: `uv run pytest -q tests/test_panel.py -k configuracion` → FAIL 404

- [ ] **Step 2: Correr para ver el 404**

Expected: FAIL — la ruta no existe todavía (`assert 404 == 422`).

- [ ] **Step 3: Implementar las dos rutas**

En `runtime.py`, **antes** del bloque final que sirve el frontend:

```python
class CambioDeConfiguracion(BaseModel):
    """Solo las perillas que cambian. `extra='forbid'` es la mitad del contrato: sin él, una
    clave mal escrita en el formulario se descartaría en silencio y el usuario vería un 200
    sobre un cambio que no ocurrió."""

    model_config = ConfigDict(extra="forbid")

    capacidad_por_hora: int | None = Field(default=None, ge=1, le=6)
    duracion_cita_minutos: int | None = Field(default=None, ge=15, le=180)
    hora_apertura: int | None = Field(default=None, ge=0, le=23)
    hora_cierre: int | None = Field(default=None, ge=0, le=23)
    hora_cierre_sabado: int | None = Field(default=None, ge=0, le=23)
    atiende_domingo: int | None = Field(default=None, ge=0, le=1)
    aviso_relevo_minutos: int | None = Field(default=None, ge=5, le=1440)
    cierre_relevo_minutos: int | None = Field(default=None, ge=10, le=1440)
    hora_recordatorio_vispera: int | None = Field(default=None, ge=0, le=23)
    horas_minimas_para_recordar: int | None = Field(default=None, ge=1, le=48)
    tope_diario_reactivacion: int | None = Field(default=None, ge=0, le=50)
    max_reactivaciones_12m: int | None = Field(default=None, ge=0, le=24)
    max_seguimientos_fallidos: int | None = Field(default=None, ge=1, le=10)


@app.get("/api/configuracion")
async def api_configuracion(quien: dict = Depends(usuario_actual)) -> dict:
    """Solo lectura para cualquier rol: recepción puede necesitar saber a qué hora cierra la
    clínica según el sistema. `es_admin` es para que la pantalla enseñe los campos apagados a
    quien no puede escribir -- el control de verdad está en el PATCH."""
    with persistencia.conectar(config.database_url) as conn:
        datos = panel.configuracion_editable(conn)
    return {**datos, "es_admin": quien["rol"] == "admin"}


@app.patch("/api/configuracion")
async def api_guardar_configuracion(
    cuerpo: CambioDeConfiguracion, quien: dict = Depends(exigir_rol("admin"))
) -> dict:
    """Guarda y RELEE los globals del proceso.

    Sin el refresco final, `aviso_relevo_minutos` y `cierre_relevo_minutos` se quedarían en el
    valor del arranque para los cuatro sitios que beben de `_relevo_minutos` --el barrido de
    relevos, las dos ramas del webhook de Telegram y la ruta de conversaciones-- mientras el
    turno de Daniela ya usaría el nuevo. En desacuerdo consigo mismo, que es peor que viejo.

    Es el mismo patrón que `_refrescar_vocabulario` tras tocar un tratamiento.
    """
    cambios = {c: v for c, v in cuerpo.model_dump().items() if v is not None}
    try:
        with persistencia.conectar(config.database_url) as conn:
            datos = panel.guardar_configuracion(conn, cambios, usuario=quien["usuario"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _cargar_configuracion_operativa()
    if cambios:
        log.info("%s cambió la configuración: %s", quien["usuario"], cambios)
    return {**datos, "es_admin": True}
```

Comprueba que `ConfigDict` y `Field` estén importados de `pydantic` en la cabecera de `runtime.py`; si no, añádelos al import que ya existe.

- [ ] **Step 4: Correr las pruebas de ruta**

Run: `uv run pytest -q tests/test_panel.py -k configuracion`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/maxicare_daniela/runtime.py tests/test_panel.py
git commit -m "feat(panel): rutas de configuracion, con refresco de los globals del proceso"
```

---

## Tarea 3: las tres señales que no existen

**Files:**
- Modify: `src/maxicare_daniela/persistencia.py` (junto a `gasto_del_dia`, ~línea 4663)
- Modify: `src/maxicare_daniela/canales.py` (en `WhatsApp`, junto a `calidad_del_numero`; y en `Telegram`)
- Modify: `scripts/probar_plantilla.py`, `scripts/configurar_webhook_telegram.py`
- Test: `tests/test_panel.py`

**Interfaces:**
- Produces:
  - `persistencia.citas_sin_evento_calendar(conn, *, ahora: datetime) -> int`
  - `canales.WhatsApp.estado_de_plantillas() -> list[dict[str, str]]` — `[{"nombre", "estado", "idioma"}]`
  - `canales.Telegram.estado_del_webhook() -> dict[str, Any]` — `{"url", "pendientes", "ultimo_error"}`

- [ ] **Step 1: Prueba de las citas huérfanas (falla)**

```python
@pytest.mark.neon
def test_cuenta_las_citas_futuras_sin_evento_de_calendar(esquema):
    """Una cita futura sin `evento_calendar_id` es una que no llegó a Google: el paciente la
    tiene confirmada y la clínica no la ve en su agenda. Las pasadas no cuentan: ya no hay
    nada que hacer con ellas."""
    ahora = datetime.now(ZONA_BOGOTA)
    with psycopg.connect(_url_de_pruebas()) as conn:
        assert isinstance(persistencia.citas_sin_evento_calendar(conn, ahora=ahora), int)
```

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py -k huerfanas` → FAIL

- [ ] **Step 2: Implementar la consulta**

En `persistencia.py`:

```python
def citas_sin_evento_calendar(conn, *, ahora: datetime) -> int:
    """Citas futuras y vivas que nunca llegaron a Google Calendar.

    Es el hecho medible que sustituye a la «última sincronización», que no existe: la
    reconciliación corre bajo demanda --cuando alguien abre la Agenda-- y no guarda la hora.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM citas "
            "WHERE evento_calendar_id IS NULL AND inicio >= %s "
            "AND estado IN ('confirmada', 'reprogramada')",
            (ahora,),
        )
        return int(cur.fetchone()[0])
```

**Los DOS estados vivos, no solo `'confirmada'`.** El CHECK de `citas` admite
`('confirmada', 'reprogramada', 'cancelada')`, y una reprogramada es una cita a la que el
paciente va a ir: dejarla fuera escondería el caso que más se rompe, porque reprogramar es la
operación que más veces toca Google. `'cancelada'` sí queda fuera — que no tenga evento es lo
correcto.

- [ ] **Step 3: Correr**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_panel.py -k huerfanas` → PASS

- [ ] **Step 4: Portar el estado de plantillas a `canales.py`**

Mueve el cuerpo de `_waba_ids()` y `_mostrar_estado()` de `scripts/probar_plantilla.py` a un método de `WhatsApp`:

```python
    async def estado_de_plantillas(self) -> list[dict[str, str]]:
        """Qué plantillas tiene aprobadas Meta HOY. Una LECTURA: no gasta cupo.

        Vivía solo en `scripts/probar_plantilla.py`, con la firma escrita a mano. El script
        pasa a llamar a este método: una plantilla rechazada es de las pocas cosas que se
        rompen sin ruido, y el panel tiene que poder verla sin abrir una terminal.
        """
```

Devuelve `[{"nombre": ..., "estado": ..., "idioma": ...}]` leyendo `GET {BASE_GRAPH}/{waba}/message_templates`. Reusa el cliente HTTP y el manejo de error que ya usa `calidad_del_numero`.

Después, en `scripts/probar_plantilla.py`, sustituye el cuerpo de `_mostrar_estado()` por una llamada a este método. **No borres el script**: su valor es que manda una plantilla de verdad, y eso sigue ahí.

- [ ] **Step 5: Portar `getWebhookInfo` a `canales.Telegram`**

```python
    async def estado_del_webhook(self) -> dict[str, Any]:
        """Lo que Telegram dice del webhook. `getWebhookInfo` es solo lectura y NO rompe nada:
        lo que rompe el `getUpdates` de `obtener_chat_telegram.py` es llamar a `setWebhook`.

        **No dice si hay `secret_token`**, así que quien lo pinte tiene que enseñar por
        separado «la URL está puesta» (esto) y «el secreto está configurado» (`config`).
        """
```

Devuelve `{"url": str, "pendientes": int, "ultimo_error": str | None}`. Luego, en `scripts/configurar_webhook_telegram.py`, deja que `_estado(token)` llame a este método en vez de doblarlo.

- [ ] **Step 6: Correr los seis entregables que no gastan tokens**

Run, uno a uno:
```
uv run python scripts/probar_tools.py
uv run python scripts/probar_relevo.py
uv run python scripts/probar_recordatorios.py
uv run python scripts/probar_sin_resolver.py
uv run python scripts/probar_reactivacion.py
uv run python scripts/probar_calendario.py
```
Expected: ninguno en `FALLA`. Se corren porque esta tarea cambia firmas que los scripts doblaban, y `pytest -q` no los ejecuta.

Y además: `uv run python scripts/probar_plantilla.py --estado` (no gasta) y `uv run python scripts/configurar_webhook_telegram.py --estado`.

- [ ] **Step 7: Commit**

```bash
git add src/maxicare_daniela/persistencia.py src/maxicare_daniela/canales.py scripts/ tests/test_panel.py
git commit -m "feat(estado): citas huerfanas, plantillas de Meta y webhook de Telegram salen de scripts a src"
```

---

## Tarea 4: las rutas de estado

**Files:**
- Modify: `src/maxicare_daniela/runtime.py`
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: todo lo de la Tarea 3, más `persistencia.contar_sin_responder`, `contar_relevos_abiertos`, `seguimientos_por_despachar`, `contar_comprometidos_hoy`, `gasto_del_dia`; `guardrails.fallos_recientes_de_evaluador`; `canales.WhatsApp.calidad_del_numero` (ya existe). Todos ellos ya están importados en `runtime.py`, incluidos `guardrails`, `ingesta` y `ZONA_BOGOTA`: no hace falta ningún import nuevo.
- Produces: `GET /api/estado`, `POST /api/estado/sondas`.

- [ ] **Step 1: Prueba de que el estado sobrevive a una base caída (falla)**

Las dos van **sin Neon**: una finge que la base no contesta y la otra solo mira qué claves
viajan. Mismo molde `_como(rol)` de la Tarea 2.

```python
def test_el_estado_carga_aunque_neon_este_caido(monkeypatch):
    """La pantalla que existe para saber qué está roto tiene que cargar justo cuando algo lo
    está. 200 con `base.ok = False`, nunca un 500."""
    def revienta(*a, **k):
        raise RuntimeError("Neon no contesta")

    monkeypatch.setattr(persistencia, "conectar", revienta)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        r = TestClient(runtime.app).get("/api/estado")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["base"]["ok"] is False
        assert cuerpo["frenos"]["daniela_responde"] in (True, False)
    finally:
        runtime.app.dependency_overrides.clear()


def test_el_gasto_no_viaja_a_quien_no_es_admin(monkeypatch):
    """No se esconde en pantalla: no viaja. Un botón que desaparece no es un control."""
    monkeypatch.setattr(persistencia, "conectar", lambda url: (_ for _ in ()).throw(RuntimeError()))
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        cuerpo = TestClient(runtime.app).get("/api/estado").json()
        assert "gasto" not in cuerpo
        assert "entorno" not in cuerpo
    finally:
        runtime.app.dependency_overrides.clear()
```

Run: `uv run pytest -q tests/test_panel.py -k estado` → FAIL 404

- [ ] **Step 2: Correr para ver el 404**

Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Implementar `GET /api/estado`**

```python
@app.get("/api/estado")
async def api_estado(quien: dict = Depends(usuario_actual)) -> dict:
    """La salud operativa, con detalle. Exige sesión, y por eso puede decir lo que `/salud`
    calla: aquella es pública --la consumen el healthcheck de Docker y `desplegar.sh`-- y
    nunca revela ni el texto del error de base ni qué credenciales faltan.

    **El bloque de base va en su propio `try` y el resto no depende de él.** Una pantalla de
    diagnóstico que no carga cuando la base está caída no sirve para nada.
    """
    es_admin = quien["rol"] == "admin"
    ahora = datetime.now(ZONA_BOGOTA)
    base: dict[str, Any] = {"ok": False, "citas_sin_calendar": None}
    atencion_: dict[str, Any] = {}
    cola: dict[str, Any] = {}
    gasto: float | None = None
    try:
        with persistencia.conectar(config.database_url) as conn:
            base = {
                "ok": True,
                "citas_sin_calendar": persistencia.citas_sin_evento_calendar(conn, ahora=ahora),
            }
            atencion_ = {
                "sin_responder": persistencia.contar_sin_responder(
                    conn, tipos_sin_turno=ingesta.TIPOS_QUE_NO_ABREN_TURNO
                ),
                "relevos_abiertos": persistencia.contar_relevos_abiertos(conn),
            }
            cola = {
                "recordatorios_por_despachar": len(
                    persistencia.seguimientos_por_despachar(conn, ahora, limite=200)
                ),
                "reactivaciones_hoy": persistencia.contar_comprometidos_hoy(conn, ahora=ahora),
            }
            if es_admin:
                gasto = persistencia.gasto_del_dia(conn)
    except Exception as e:  # noqa: BLE001
        log.warning("la pantalla de estado no pudo leer la base: %s", e)

    salida: dict[str, Any] = {
        "base": base,
        "calendario": {"clase": type(_calendario).__name__},
        "frenos": {
            "daniela_responde": config.daniela_responde,
            "leer_archivos": config.leer_archivos,
            "transcribir_audio": config.transcribir_audio,
        },
        "atencion": atencion_,
        "cola": cola,
        "relevo": {
            "secreto_del_webhook": bool(config.telegram_webhook_secret),
            "tema_general": _tema_general,
        },
        "evaluador": {
            # En memoria del proceso, no en la base: un despliegue lo borra. La pantalla lo
            # rotula «desde el último arranque», y no «hoy», porque si no mentiría.
            "fallos_en_la_ventana": guardrails.fallos_recientes_de_evaluador(),
        },
    }
    if es_admin:
        salida["gasto"] = {"usd_hoy": gasto, "umbral_usd": config.alerta_gasto_diario_usd}
        salida["entorno"] = {"faltan": _variables_que_faltan()}
    return salida
```

`_variables_que_faltan()` sale de extraer a una función la lista de seis variables que `/salud` ya comprueba en su bloque de `configuracion`, para no escribirla dos veces. **`/salud` sigue usando esa misma función y su respuesta no cambia.**

- [ ] **Step 4: Correr**

Run: `uv run pytest -q tests/test_panel.py -k estado` → PASS

- [ ] **Step 5: Prueba de las sondas con un tercero caído (falla), luego implementar**

```python
def test_una_sonda_caida_no_tumba_a_la_otra(monkeypatch):
    """Que la Graph API no conteste no puede dejar sin respuesta lo de Telegram."""
    async def revienta(*a, **k):
        raise RuntimeError("Meta no contesta")

    monkeypatch.setattr(runtime._whatsapp, "estado_de_plantillas", revienta)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        cuerpo = TestClient(runtime.app).post("/api/estado/sondas").json()
        assert "error" in cuerpo["whatsapp"]
        assert "telegram" in cuerpo
    finally:
        runtime.app.dependency_overrides.clear()
```

Implementación:

```python
@app.post("/api/estado/sondas")
async def api_sondas_de_estado(quien: dict = Depends(exigir_rol("admin"))) -> dict:
    """Pregunta en vivo a Meta y a Telegram. Detrás de un botón y no en la carga: si esto
    corriera al abrir, una caída de Meta dejaría sin cargar la pantalla que existe para saber
    qué está roto.

    Es POST aunque no escriba en la base: hace dos llamadas a terceros, así que no puede
    dispararse con un prefetch del navegador ni quedar cacheada.

    Cada bloque en su propio `try`: que la Graph API no conteste no puede dejar sin respuesta
    lo de Telegram.
    """
    salida: dict[str, Any] = {"secreto_del_webhook": bool(config.telegram_webhook_secret)}
    try:
        salida["whatsapp"] = {
            "plantillas": await _whatsapp.estado_de_plantillas(),
            "calidad": await _whatsapp.calidad_del_numero(),
        }
    except Exception as e:  # noqa: BLE001
        salida["whatsapp"] = {"error": str(e)}
    try:
        salida["telegram"] = await _telegram.estado_del_webhook()
    except Exception as e:  # noqa: BLE001
        salida["telegram"] = {"error": str(e)}
    return salida
```

Run: `uv run pytest -q tests/test_panel.py -k sonda` → PASS

- [ ] **Step 6: Correr la suite entera**

Run: `uv run pytest -q` y `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
Expected: PASS las dos. Si `tests/test_estructura.py` se queja, es que algo en `panel.py` o `persistencia.py` importó `runtime`: no lo silencies, muévelo.

- [ ] **Step 7: Commit**

```bash
git add src/maxicare_daniela/runtime.py tests/test_panel.py
git commit -m "feat(estado): rutas de salud operativa con detalle, y sondas a Meta y Telegram"
```

---

## Tarea 5: fuera Métricas y `Pendiente.tsx`

**Files:**
- Delete: `web/src/pantallas/Pendiente.tsx`
- Modify: `web/src/componentes/Sidebar.tsx`, `web/src/App.tsx`

- [ ] **Step 1: Quitar Métricas y el campo `fase` del menú**

En `Sidebar.tsx`:
- Fuera `| 'metricas'` de `SeccionId`.
- Fuera la entrada `metricas` de `SECCIONES`.
- Fuera el campo `fase` del tipo `Seccion`, su valor en las nueve entradas, y el bloque `{seccion.fase !== null && (<span …>F{seccion.fase}</span>)}` del componente `Boton`.
- Sustituye el comentario largo de `SECCIONES` que justifica no esconder las secciones sin construir por: `/* Las ocho secciones de la especificación, en el orden que puso el diseño. Todas construidas desde el 25/09/2026: la novena, «Métricas», se eliminó por decisión de MaxiCare, y con ella el cartel de «todavía no construida» que ya no alcanzaba nadie. */`

- [ ] **Step 2: Borrar la pantalla y su rama**

```bash
git rm web/src/pantallas/Pendiente.tsx
```

En `App.tsx`: fuera el `import PantallaPendiente`, y la rama final `) : ( <PantallaPendiente seccion={seccion} /> )` se queda como `) : null` **de momento** — las Tareas 6 y 7 la rellenan.

- [ ] **Step 3: Comprobar que compila**

Run: `cd web && npm run build`
Expected: sin errores de TypeScript. Si `seccion` queda sin usar en `App.tsx`, déjalo: la cabecera de móvil lo sigue usando para el título.

- [ ] **Step 4: Commit**

```bash
git add -A web/src
git commit -m "refactor(panel): fuera Metricas, y con ella el cartel de pantalla sin construir"
```

---

## Tarea 6: la pantalla de Configuración

**Files:**
- Create: `web/src/pantallas/Configuracion.tsx`
- Modify: `web/src/api.ts`, `web/src/App.tsx`

**Interfaces:**
- Consumes: `GET`/`PATCH /api/configuracion` (Tarea 2).
- Produces: `api.leerConfiguracion()`, `api.guardarConfiguracion(cambios)`, tipo `Configuracion`.

- [ ] **Step 1: El cliente de la API**

En `web/src/api.ts`, junto a las funciones de tratamientos:

```typescript
export type Configuracion = {
  valores: Record<string, number>
  descripciones: Record<string, string>
  actualizado_en: Record<string, string | null>
  rangos: Record<string, [number, number]>
  fijas: { clave: string; valor: string; por_que: string }[]
  es_admin: boolean
}

export async function leerConfiguracion(): Promise<Configuracion> {
  return pedir<Configuracion>('/api/configuracion')
}

/** Solo las perillas que cambian. El servidor rechaza una clave desconocida con 422: una que
 *  se perdiera en silencio sería indistinguible de una guardada. */
export async function guardarConfiguracion(
  cambios: Record<string, number>,
): Promise<Configuracion> {
  return pedir<Configuracion>('/api/configuracion', {
    method: 'PATCH',
    body: JSON.stringify(cambios),
  })
}
```

- [ ] **Step 2: La pantalla**

Crea `web/src/pantallas/Configuracion.tsx` con el molde de `Leads.tsx`: `CargandoPantalla` y `Fallo` de `@/componentes/Estado`, y `CabeceraDePanel`, `TituloDePanel`, `Rotulo`, `AccionDeCabecera`, `BOTON`, `CAMPO`, `ESTILO_CAMPO`, `SG`, `MONO` de `@/componentes/Panel`. Prop `alCaducarSesion: () => void`, y se atrapa `SesionCaducada` igual que allí.

Estructura:

- **Estado local:** `datos: Configuracion | null`, `borrador: Record<string, number>`, `guardando: boolean`, `error: string | null`, `guardado: boolean`.
- **Cuatro grupos** con su `Rotulo`: Agenda, Relevo, Recordatorios, Reactivación, con las claves en el orden de `panel.CLAVES_EDITABLES`.
- **Cada perilla**: etiqueta en español, `<input type="number">` con `min`/`max` sacados de `datos.rangos[clave]`, la `descripcion` debajo en gris, y `disabled={!datos.es_admin || guardando}`.
- **`atiende_domingo`** se dibuja como dos botones (Sí/No) que escriben `1` o `0`, nunca un `<input type="checkbox">` cuyo `checked` acabaría viajando como booleano. Es el fallo número 1 de Review Focus, cerrado en el borde donde se origina.
- **Solo se manda lo que cambió:** `const cambios = Object.fromEntries(Object.entries(borrador).filter(([c, v]) => v !== datos.valores[c]))`. Si queda vacío, el botón Guardar va deshabilitado.
- **El aviso del horario duplicado**, visible en cuanto `hora_apertura`, `hora_cierre` o `hora_cierre_sabado` difieran de lo guardado:

```tsx
<p className="text-xs leading-relaxed" style={{ color: '#92400E' }}>
  Esto cambia los horarios que Daniela <strong>ofrece</strong>. Lo que <strong>dice</strong>
  cuando le preguntan «¿a qué hora abren?» sale de la ficha <code>General → horario</code> de
  Tratamientos, y hay que moverla a mano: si no, dirá una hora y ofrecerá otra.
</p>
```

- **Las fijas** al pie, en una sección de solo lectura: `clave`, `valor` y `por_que`, sin campo.
- **El error del servidor** (400 de una invariante rota) se pinta con `<Fallo mensaje={error} />` arriba del formulario, no en un `alert`.

- [ ] **Step 3: Enchufarla en `App.tsx`**

```tsx
) : activa === 'configuracion' ? (
  // Escribe, y solo `admin`. Como Tratamientos y Agenda, puede toparse con un 401 a mitad
  // de la tarde y vuelve al ingreso por el mismo camino.
  <Configuracion alCaducarSesion={() => setSesion(null)} />
) : (
```

- [ ] **Step 4: Compilar y mirarla**

Run: `cd web && npm run build`, luego `npm run dev` con `uv run uvicorn maxicare_daniela.runtime:app --port 8080` en otra terminal. Entra, cambia `capacidad_por_hora`, guarda, recarga: tiene que seguir el valor nuevo.

- [ ] **Step 5: Commit**

```bash
git add web/src/pantallas/Configuracion.tsx web/src/api.ts web/src/App.tsx
git commit -m "feat(panel): pantalla de Configuracion, con el aviso del horario que vive en dos sitios"
```

---

## Tarea 7: la pantalla de Estado del sistema

**Files:**
- Create: `web/src/pantallas/EstadoDelSistema.tsx`
- Modify: `web/src/api.ts`, `web/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/estado`, `POST /api/estado/sondas` (Tarea 4).
- Produces: `api.leerEstado()`, `api.sondearEstado()`, tipos `EstadoDelSistema` y `SondasDelSistema`.

**El nombre del archivo es `EstadoDelSistema.tsx` y no `Estado.tsx`**: ya existe `web/src/componentes/Estado.tsx`, que es el loader genérico, y dos módulos llamados igual en un proyecto con alias `@/` es una tarde perdida.

- [ ] **Step 1: El cliente de la API**

```typescript
export type EstadoDelSistema = {
  base: { ok: boolean; citas_sin_calendar: number | null }
  calendario: { clase: string }
  frenos: { daniela_responde: boolean; leer_archivos: boolean; transcribir_audio: boolean }
  atencion: { sin_responder?: number; relevos_abiertos?: number }
  cola: { recordatorios_por_despachar?: number; reactivaciones_hoy?: number }
  relevo: { secreto_del_webhook: boolean; tema_general: number | null }
  evaluador: { fallos_en_la_ventana: number }
  /** Solo si eres admin: no se esconde en pantalla, no viaja. */
  gasto?: { usd_hoy: number | null; umbral_usd: number }
  entorno?: { faltan: string[] }
}

export type SondasDelSistema = {
  secreto_del_webhook: boolean
  whatsapp: { plantillas: { nombre: string; estado: string; idioma: string }[]; calidad: Record<string, string> } | { error: string }
  telegram: { url: string; pendientes: number; ultimo_error: string | null } | { error: string }
}

export async function leerEstado(): Promise<EstadoDelSistema> {
  return pedir<EstadoDelSistema>('/api/estado')
}

/** POST aunque no escriba: son dos llamadas a terceros y no pueden dispararse con un
 *  prefetch del navegador. */
export async function sondearEstado(): Promise<SondasDelSistema> {
  return pedir<SondasDelSistema>('/api/estado/sondas', { method: 'POST' })
}
```

- [ ] **Step 2: La pantalla**

Crea `web/src/pantallas/EstadoDelSistema.tsx`, mismo molde que la anterior. Una rejilla de tarjetas, una por bloque:

| Tarjeta | Verde cuando | Rojo / ámbar cuando |
|---|---|---|
| Base de datos | `base.ok` | no conecta |
| Citas en Google | `citas_sin_calendar === 0` | ámbar si > 0, con la frase «confirmadas al paciente y que la clínica no ve en su agenda» |
| Calendario | `clase === 'CalendarioGoogle'` | rojo con `CalendarioCaido`; si algún día dijera `CalendarioDoble`, **rojo y con el texto «esto no debería poder pasar»** (no negociable 1) |
| Daniela responde | `frenos.daniela_responde` | ámbar: está apagada a propósito |
| Lector de archivos | `frenos.leer_archivos` | ámbar |
| Transcripción de audios | `frenos.transcribir_audio` | ámbar |
| Sin responder | `=== 0` | ámbar si > 0 |
| Relevos abiertos | siempre informativo | — |
| Relevo | `relevo.secreto_del_webhook` | **rojo**: sin secreto, `/webhook/telegram` responde 403 a todo y el botón «Hablar yo con el paciente» no hace nada, sin error en ningún log |
| Cola | informativo | — |
| Evaluador | `fallos_en_la_ventana === 0` | ámbar. Rótulo literal: **«desde el último arranque»** |
| Gasto (admin) | por debajo del umbral | ámbar al cruzarlo |
| Entorno (admin) | `faltan.length === 0` | rojo con la lista |

Debajo, la sección de sondas: un botón `AccionDeCabecera` «Comprobar ahora» que llama a `sondearEstado()`, con su propio `cargando`. Mientras nadie lo pulse, un texto que diga que esas dos comprobaciones salen a internet y por eso no se hacen solas. Al volver, si un bloque trae `error`, se pinta ese bloque en rojo con el mensaje y **el otro se pinta igual**.

En el bloque de Telegram, dos filas separadas y nunca mezcladas: «URL del webhook» (lo dice Telegram) y «Secreto configurado» (lo dice este proceso), con una nota de que `getWebhookInfo` no informa del secreto.

Al pie, la frase sobre la sincronización con Calendar:

```tsx
<p className="text-xs" style={{ color: '#6B7280' }}>
  No hay una marca de «última sincronización»: la reconciliación con Google corre cuando
  alguien abre la Agenda o cuando un paciente pregunta por su cita, y no deja hora. Lo que se
  mide arriba son las citas que no llegaron.
</p>
```

- [ ] **Step 3: Enchufarla en `App.tsx`**

```tsx
) : activa === 'estado' ? (
  // De solo lectura salvo el botón de sondas, que no escribe en la base. Vuelve al ingreso
  // por el mismo camino que las demás si la sesión caduca.
  <EstadoDelSistema alCaducarSesion={() => setSesion(null)} />
) : (
```

Y ahora la rama final `: null` de la Tarea 5 desaparece: las ocho secciones tienen destino.

- [ ] **Step 4: Compilar y mirarla**

Run: `cd web && npm run build` y el par `npm run dev` + uvicorn. Entra como admin, mira que salgan el gasto y el entorno; entra como recepción y comprueba que **no salen**.

- [ ] **Step 5: La comprobación final**

Run:
```
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_web.py
uv run python scripts/probar_panel.py
```

`probar_panel.py` siembra en el `public` de producción a propósito y esa base ya tiene pacientes: léelo antes de lanzarlo y confirma que su `finally` restaura el precio.

- [ ] **Step 6: Commit**

```bash
git add web/src/pantallas/EstadoDelSistema.tsx web/src/api.ts web/src/App.tsx
git commit -m "feat(panel): pantalla de Estado del sistema, con las sondas detras de un boton"
```

---

## Cierre

Con la Tarea 7 el menú queda sin una sola sección muerta. Lo que NO entra, y está dicho en la spec: el borrador de prompts con historial de versiones, editar la ficha `_general/horario` desde Configuración, y una marca persistida de última sincronización con Calendar.

Antes de desplegar: `uv run python scripts/inicializar_base.py --solo-verificar`. No hay migración nueva en este trabajo —el CHECK de `cambios_configuracion` ya admite `'configuracion'` desde la 007—, así que si ese comando pasa, `bash scripts/desplegar.sh` es todo.

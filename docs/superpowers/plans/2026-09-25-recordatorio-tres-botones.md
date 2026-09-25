# Los tres botones del recordatorio — plan de implementación

> **Para quien lo ejecute:** SUB-SKILL OBLIGATORIA: `superpowers:executing-plans` con
> `superpowers:test-driven-development`. Los pasos van en checkbox (`- [ ]`).

**Goal:** que los tres botones del recordatorio de cita hagan algo — confirmar deja fila y
avisa, «no puedo asistir» pasa por Daniela antes de cancelar, y toda cancelación o
reprogramación le llega al doctor por WhatsApp.

**Architecture:** el registro y el aviso de «Confirmar» los ejecuta el código en
`runtime._entregar`, en la lista donde el proyecto ya decide si un mensaje se atiende. Los
avisos de cancelación y cambio cuelgan de las tools (`_cancelar_cita`, `_reprogramar_cita`),
no del botón, así que cubren también al paciente que lo pide escribiendo. El canal es el que
ya existe: una plantilla de WhatsApp a `WHATSAPP_DOCTORES`, con el patrón de `aviso_citas`.

**Tech Stack:** Python 3.12, psycopg 3, FastAPI, Graph API de Meta.

**Spec:** `docs/superpowers/specs/2026-09-25-recordatorio-tres-botones-design.md`

## Global Constraints

- **Los rótulos son literales exactos y con tildes**: `Confirmar`, `Necesito cambiarla`,
  `No puedo asistir`. Viven en `seguimientos.py` y en ningún otro sitio de `src/`.
- **La plantilla del aviso es `movimiento_agenda`, idioma `es`**, cinco huecos en el orden
  `asunto · nombre · teléfono · tratamiento · cuándo`. Ningún hueco lleva saltos de línea.
- **Con `MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA` vacía se decide igual y no se manda nada**,
  dejando en el log destinatarios y parámetros. Mismo patrón que `aviso_citas.avisar`.
- **Ningún aviso puede tumbar una cita ni un turno**: tarea de fondo propia, `try` por
  destinatario dentro del bucle, nunca propaga.
- **Nada de esto toca Telegram** (no negociable 14).
- **El tipo del mensaje sale del webhook de Meta, nunca del modelo** (no negociables 23, 12).
- Pruebas: `uv run pytest -q` offline; `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
  para lo que toca la base. Quien toque el panel corre las de Neon (no negociable 8).

## Review Focus

Las cinco entradas que la spec implica y que ningún paso de abajo ejercita por sí solo. Cada
una tiene su prueba en la tarea que posee el código:

1. **El mismo botón pulsado tres veces** (Meta reintenta, y el paciente también): la cita se
   confirma una vez y el doctor recibe **un** aviso. — Tarea 1 y Tarea 4.
2. **El rótulo con otra grafía** («Confirmar cita», «confirmar»): no se confirma nada y el
   mensaje sigue su camino hasta Daniela, sin error. — Tarea 4.
3. **El paciente con dos citas futuras**: se confirma la del último recordatorio despachado,
   no la más próxima ni la más lejana. — Tarea 1.
4. **Un destinatario que falla**: los otros dos doctores reciben el aviso igual. — Tarea 2.
5. **Reprogramar que revienta a mitad**: no sale ningún aviso de cambio. — Tarea 3.

---

### Tarea 1: Los rótulos, la migración 031 y las dos consultas

**Files:**
- Create: `migraciones/031_confirmacion_del_paciente.sql`
- Modify: `src/maxicare_daniela/seguimientos.py` (junto a `TIPO_RECORDATORIO`)
- Modify: `src/maxicare_daniela/persistencia.py` (junto a `leer_cita`)
- Test: `tests/test_persistencia_neon.py`, `tests/test_seguimientos.py`

**Interfaces:**
- Produces: `seguimientos.BOTON_CONFIRMAR`, `BOTON_CAMBIAR`, `BOTON_NO_ASISTIR`,
  `BOTONES_DEL_RECORDATORIO: tuple[str, str, str]`;
  `persistencia.cita_del_ultimo_recordatorio(conn, telefono: str, *, ahora: datetime) -> dict | None`;
  `persistencia.marcar_cita_confirmada(conn, id_cita: str, *, cuando: datetime) -> bool`.

- [ ] **Paso 1: la migración**

```sql
-- =========================================================================================
-- Que el paciente haya confirmado su cita deja de ser invisible
--
-- `citas.estado` arranca en 'confirmada' por DEFAULT desde la 001, asi que esa palabra nunca
-- significo que el paciente dijera nada: significa que la cita existe y no esta cancelada.
-- Con los tres botones del recordatorio hay por primera vez alguien diciendolo, y no habia
-- donde escribirlo.
--
-- NULLABLE y sin default a proposito. `NULL` es «no ha dicho nada», que es distinto de «dijo
-- que no» -- eso ultimo ya es `estado = 'cancelada'`. Un BOOLEAN con default `false` haria
-- esas dos cosas indistinguibles, y son la diferencia entre llamar a un paciente y no.
-- =========================================================================================

ALTER TABLE citas ADD COLUMN IF NOT EXISTS confirmada_por_paciente_en TIMESTAMPTZ;
```

- [ ] **Paso 2: las constantes, con su comentario**

En `seguimientos.py`, debajo de `TIPO_RECORDATORIO`:

```python
#: Los rótulos EXACTOS de los tres quick replies que Meta aprobó en `recordatorio_cita`.
#:
#: Van aquí y no en `runtime` porque son de la plantilla, y la plantilla es de este módulo.
#: **El código compara contra el literal**, tildes incluidas: un rótulo aprobado con otra
#: grafía --«No puedo asistir.» con punto, «Confirmar cita»-- deja el botón sin camino y el
#: mensaje se atiende como texto libre. No falla nada y no lo ve nadie, así que están en un
#: solo sitio y `scripts/probar_plantilla.py --estado` los imprime para compararlos a ojo
#: con lo que se ve en el teléfono.
BOTON_CONFIRMAR = "Confirmar"
BOTON_CAMBIAR = "Necesito cambiarla"
BOTON_NO_ASISTIR = "No puedo asistir"
BOTONES_DEL_RECORDATORIO = (BOTON_CONFIRMAR, BOTON_CAMBIAR, BOTON_NO_ASISTIR)
```

- [ ] **Paso 3: la prueba que falla (offline, los rótulos)**

En `tests/test_seguimientos.py`:

```python
def test_los_tres_rotulos_del_recordatorio_son_los_que_meta_aprobo():
    """Si alguien los toca, que sea a sabiendas: el código compara contra el literal."""
    assert seguimientos.BOTONES_DEL_RECORDATORIO == (
        "Confirmar",
        "Necesito cambiarla",
        "No puedo asistir",
    )
```

- [ ] **Paso 4: correr y ver el fallo**

Run: `uv run pytest tests/test_seguimientos.py -k rotulos -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'BOTONES_DEL_RECORDATORIO'`
(antes del paso 2) o PASS si ya se escribió; en ese caso quitar las constantes, ver el rojo
y volver a ponerlas.

- [ ] **Paso 5: las dos consultas**

En `persistencia.py`, junto a `leer_cita`:

```python
def cita_del_ultimo_recordatorio(
    conn, telefono: str, *, ahora: datetime
) -> dict[str, Any] | None:
    """La cita de la que habla el último recordatorio que se le despachó a ese número.

    Un quick reply no dice de qué cita habla: solo trae su rótulo. Esto es lo que lo
    convierte en una cita concreta.

    Va por TELÉFONO, como `ultimo_recordatorio`, `_es_ajena` y `citas_activas_de_telefono`:
    `seguimientos` cuelga de la conversación, que dura 24 h, y el recordatorio de la víspera
    sale de otra conversación distinta de aquella en la que el paciente pulsa el botón.

    **No se filtra por `fallo IS NULL`**, aunque la fila se marca ANTES de enviar (no
    negociable 21) y puede quedar con las dos columnas puestas. Si el envío falló de verdad,
    el paciente no recibió el botón y no puede haber pulsado nada; y si falló el primer
    intento y salió el reintento, la fila conserva el `fallo` viejo. Filtrar por él dejaría
    sin camino justo al paciente cuyo recordatorio costó dos intentos.

    La cita tiene que seguir VIVA y en el FUTURO. Eso es lo que hace innecesaria una ventana
    de tiempo sobre el envío: un botón pulsado tres días tarde apunta a una cita que ya pasó.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.nombre_completo, c.telefono, c.tratamiento, c.inicio, c.estado,
                   c.confirmada_por_paciente_en
              FROM seguimientos s
              JOIN citas c ON c.id = s.cita_id
             WHERE c.telefono = %s
               AND s.tipo = %s
               AND s.enviado_en IS NOT NULL
               AND c.estado IN ('confirmada', 'reprogramada')
               AND c.inicio >= %s
             ORDER BY s.enviado_en DESC
             LIMIT 1
            """,
            (telefono, "recordatorio_cita", ahora),
        )
        fila = cur.fetchone()
    if not fila:
        return None
    columnas = (
        "id", "nombre_completo", "telefono", "tratamiento", "inicio", "estado",
        "confirmada_por_paciente_en",
    )
    return dict(zip(columnas, fila))


def marcar_cita_confirmada(conn, id_cita: str, *, cuando: datetime) -> bool:
    """Deja constancia de que el paciente confirmó. Devuelve si esta llamada cambió algo.

    El `IS NULL` del WHERE no es una optimización: **es la deduplicación del aviso al
    doctor.** Meta reintenta los webhooks y un paciente puede pulsar el botón tres veces;
    confirmar de nuevo es inocuo, pero tres WhatsApps a tres doctores por la misma cita no lo
    es. Quien decide es Postgres con el `rowcount`, no un `if` que lea antes de escribir:
    dos webhooks simultáneos pasarían los dos por esa lectura.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas
               SET confirmada_por_paciente_en = %s, actualizada_en = now()
             WHERE id = %s AND confirmada_por_paciente_en IS NULL
            """,
            (cuando, id_cita),
        )
        cambio = cur.rowcount > 0
    conn.commit()
    return cambio
```

- [ ] **Paso 6: las pruebas contra Neon**

En `tests/test_persistencia_neon.py`, con los helpers del archivo (`_conexion`, y la siembra
que ya usan las demás pruebas de citas):

```python
@pytest.mark.neon
def test_confirmar_dos_veces_solo_cuenta_la_primera(conexion):
    """El segundo toque no puede costar un segundo aviso a tres doctores."""
    id_cita = _sembrar_cita(conexion, telefono="573001112233", dentro_de_horas=20)
    ahora = datetime.now(ZONA_BOGOTA)

    assert persistencia.marcar_cita_confirmada(conexion, id_cita, cuando=ahora) is True
    assert persistencia.marcar_cita_confirmada(conexion, id_cita, cuando=ahora) is False


@pytest.mark.neon
def test_con_dos_citas_futuras_se_confirma_la_del_ultimo_recordatorio(conexion):
    """No la más próxima ni la más lejana: la del recordatorio que el paciente está mirando."""
    ahora = datetime.now(ZONA_BOGOTA)
    lejana = _sembrar_cita(conexion, telefono="573001112233", dentro_de_horas=100)
    proxima = _sembrar_cita(conexion, telefono="573001112233", dentro_de_horas=20)
    _sembrar_recordatorio_despachado(conexion, cita_id=lejana, enviado_hace_horas=3)
    _sembrar_recordatorio_despachado(conexion, cita_id=proxima, enviado_hace_horas=1)

    cita = persistencia.cita_del_ultimo_recordatorio(
        conexion, "573001112233", ahora=ahora
    )

    assert cita is not None and cita["id"] == proxima


@pytest.mark.neon
def test_una_cita_pasada_no_se_puede_confirmar(conexion):
    """El botón pulsado tres días tarde no confirma nada; por eso no hace falta ventana."""
    ahora = datetime.now(ZONA_BOGOTA)
    id_cita = _sembrar_cita(conexion, telefono="573001112233", dentro_de_horas=-5)
    _sembrar_recordatorio_despachado(conexion, cita_id=id_cita, enviado_hace_horas=30)

    assert persistencia.cita_del_ultimo_recordatorio(
        conexion, "573001112233", ahora=ahora
    ) is None


@pytest.mark.neon
def test_una_cita_cancelada_no_se_puede_confirmar(conexion):
    ahora = datetime.now(ZONA_BOGOTA)
    id_cita = _sembrar_cita(conexion, telefono="573001112233", dentro_de_horas=20)
    _sembrar_recordatorio_despachado(conexion, cita_id=id_cita, enviado_hace_horas=1)
    persistencia.marcar_cita_cancelada(conexion, id_cita, motivo="prueba")

    assert persistencia.cita_del_ultimo_recordatorio(
        conexion, "573001112233", ahora=ahora
    ) is None
```

Si `_sembrar_cita` / `_sembrar_recordatorio_despachado` no existen en el archivo, escríbelos
como helpers locales siguiendo el estilo de los que ya hay — **no reutilices un helper de
otro archivo de pruebas**: una colisión de helpers ya dejó dos pruebas en rojo durante tres
commits (no negociable 8).

- [ ] **Paso 7: correr las de Neon**

Run: `MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_persistencia_neon.py -q -m neon -k "confirm or recordatorio"`
Expected: 4 passed. Antes de implementar el paso 5, las mismas 4 en rojo por
`AttributeError`.

- [ ] **Paso 8: aplicar la migración y commit**

Run: `uv run python scripts/inicializar_base.py --solo-verificar`
Expected: sin errores. Después `uv run python scripts/inicializar_base.py` para aplicarla.

```bash
git add migraciones/031_confirmacion_del_paciente.sql src/maxicare_daniela/seguimientos.py src/maxicare_daniela/persistencia.py tests/
git commit -m "feat(citas): la confirmacion del paciente deja fila, y el boton sabe de que cita habla"
```

---

### Tarea 2: El aviso de movimiento de agenda

**Files:**
- Modify: `src/maxicare_daniela/config.py` (junto a `plantilla_cita_nueva`)
- Modify: `src/maxicare_daniela/aviso_citas.py`
- Modify: `.env.ejemplo`
- Test: `tests/test_aviso_citas.py`

**Interfaces:**
- Consumes: nada de la Tarea 1.
- Produces: `aviso_citas.MovimientoDeAgenda(asunto, nombre_paciente, telefono_paciente,
  tratamiento, cuando)`; `ASUNTO_CONFIRMADA`, `ASUNTO_NO_ASISTIRA`, `ASUNTO_CAMBIADA`;
  `cuando_una_cita(inicio: datetime) -> str`; `cuando_un_cambio(antes: datetime, ahora:
  datetime) -> str`; `avisar_movimiento_en_segundo_plano(mov: MovimientoDeAgenda) -> asyncio.Task | None`.

- [ ] **Paso 1: la configuración**

En `config.py`, junto a `plantilla_cita_nueva`:

```python
    #: La plantilla del aviso de movimiento de agenda: confirmó, no vendrá o cambió la hora.
    #: UNA sola para los tres, con el asunto en el primer hueco: así la redacción vive en
    #: Python y cambiarla es un commit y no un trámite con Meta. Vacía = se decide y no se
    #: manda, igual que `plantilla_cita_nueva`.
    plantilla_movimiento_agenda: str = ""
    plantilla_movimiento_agenda_idioma: str = "es"
```

y en `desde_entorno()`:

```python
            plantilla_movimiento_agenda=_opcional("MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA"),
            plantilla_movimiento_agenda_idioma=_opcional(
                "MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA_IDIOMA"
            ) or "es",
```

En `.env.ejemplo`, las dos líneas con el valor vacío y un comentario de una línea.

- [ ] **Paso 2: la prueba que falla (los cinco huecos en su orden)**

En `tests/test_aviso_citas.py`:

```python
def test_los_cinco_huecos_del_movimiento_van_en_el_orden_que_meta_aprobo():
    """Cambiar el orden no cambia la plantilla: manda otro dato en otro hueco."""
    mov = aviso_citas.MovimientoDeAgenda(
        asunto=aviso_citas.ASUNTO_CONFIRMADA,
        nombre_paciente="Andrea Rodríguez",
        telefono_paciente="573001112233",
        tratamiento="limpieza",
        cuando="jueves 25 de septiembre a las 10:00 a. m.",
    )

    assert aviso_citas.parametros_de_movimiento(mov) == [
        "Confirmó su cita",
        "Andrea Rodríguez",
        "573001112233",
        "limpieza",
        "jueves 25 de septiembre a las 10:00 a. m.",
    ]


def test_ningun_hueco_lleva_saltos_de_linea():
    """Meta rechaza el mensaje ENTERO, no el renglón."""
    mov = aviso_citas.MovimientoDeAgenda(
        asunto=aviso_citas.ASUNTO_CAMBIADA,
        nombre_paciente="Andrea\nRodríguez",
        telefono_paciente="573001112233",
        tratamiento="limpieza",
        cuando=aviso_citas.cuando_un_cambio(
            datetime(2026, 9, 25, 10, 0, tzinfo=ZONA_BOGOTA),
            datetime(2026, 9, 26, 15, 0, tzinfo=ZONA_BOGOTA),
        ),
    )

    for hueco in aviso_citas.parametros_de_movimiento(mov):
        assert "\n" not in hueco and "\r" not in hueco
```

- [ ] **Paso 3: correr y ver el fallo**

Run: `uv run pytest tests/test_aviso_citas.py -q -k "huecos or saltos"`
Expected: FAIL — `AttributeError: module 'aviso_citas' has no attribute 'MovimientoDeAgenda'`

- [ ] **Paso 4: implementar**

En `aviso_citas.py`:

```python
#: Lo que va en el primer hueco. Son texto para un doctor, no claves: si mañana la clínica
#: quiere que diga otra cosa, se cambia aquí y no en el Business Manager.
ASUNTO_CONFIRMADA = "Confirmó su cita"
ASUNTO_NO_ASISTIRA = "No podrá asistir"
ASUNTO_CAMBIADA = "Cambió su cita"


@dataclass(frozen=True)
class MovimientoDeAgenda:
    """Un cambio en la agenda que el doctor tiene que saber hoy.

    `cuando` llega ya escrito y no como fecha porque el cambio de hora necesita DOS --de
    dónde a dónde-- y la confirmación una sola. Meterlas en huecos distintos habría exigido
    una plantilla por caso, que es justo lo que esta evita.
    """

    asunto: str
    nombre_paciente: str
    telefono_paciente: str
    tratamiento: str
    cuando: str


def cuando_una_cita(inicio: datetime) -> str:
    """«jueves 25 de septiembre a las 10:00 a. m.»"""
    local = _en_bogota(inicio)
    return f"{fecha_en_palabras(local)} a las {hora_en_palabras(local)}"


def cuando_un_cambio(antes: datetime, ahora: datetime) -> str:
    """«antes: jueves 25, 10:00 a. m. · ahora: viernes 26, 3:00 p. m.»

    En UN renglón, y no es estética: un salto de línea dentro de un parámetro hace que Meta
    rechace el mensaje entero.
    """
    viejo, nuevo = _en_bogota(antes), _en_bogota(ahora)
    return (
        f"antes: {fecha_en_palabras(viejo)}, {hora_en_palabras(viejo)} · "
        f"ahora: {fecha_en_palabras(nuevo)}, {hora_en_palabras(nuevo)}"
    )


def _en_un_renglon(valor: str) -> str:
    """Un hueco con un salto de línea no cuesta un renglón: cuesta el mensaje entero."""
    return " ".join((valor or "").split())


def parametros_de_movimiento(mov: MovimientoDeAgenda) -> list[str]:
    """Los CINCO huecos, en el orden en que Meta los aprobó.

        {{1}} asunto · {{2}} nombre · {{3}} teléfono · {{4}} tratamiento · {{5}} cuándo
    """
    return [
        _en_un_renglon(mov.asunto),
        _o_pendiente(_en_un_renglon(mov.nombre_paciente)),
        _o_pendiente(_en_un_renglon(mov.telefono_paciente)),
        _o_pendiente(_en_un_renglon(mov.tratamiento)),
        _en_un_renglon(mov.cuando),
    ]
```

Más el envío, calcado de `avisar` / `_avisar_con_los_ajustes_vivos` /
`avisar_en_segundo_plano`: `avisar_movimiento`, con su `try` **dentro** del bucle de
destinatarios, la rama de «decide y no manda» cuando la plantilla está vacía, y reutilizando
`_avisos_vivos` y `tareas_en_vuelo`. Los ajustes salen de un `ajustes_del_movimiento()`
hermano de `ajustes_del_entorno()` que lee `plantilla_movimiento_agenda`.

- [ ] **Paso 5: la prueba del destinatario que falla (Review Focus 4)**

```python
@pytest.mark.asyncio
async def test_un_destinatario_que_falla_no_se_lleva_a_los_otros_dos():
    """El `try` va DENTRO del bucle: un número mal escrito en el .env no calla a los demás."""
    class CanalCojo:
        def __init__(self):
            self.enviados = []

        async def enviar_plantilla(self, telefono, *, plantilla, parametros, idioma):
            if telefono == "573000000000":
                raise RuntimeError("Meta dijo que no")
            self.enviados.append(telefono)
            return "wamid.x"

    canal = CanalCojo()
    salieron = await aviso_citas.avisar_movimiento(
        _movimiento(),
        whatsapp=canal,
        doctores=("573000000000", "573106492282", "573185790008"),
        plantilla="movimiento_agenda",
    )

    assert salieron == 2
    assert canal.enviados == ["573106492282", "573185790008"]


@pytest.mark.asyncio
async def test_sin_plantilla_decide_y_no_manda_nada():
    canal = CanalQueCuenta()
    assert await aviso_citas.avisar_movimiento(
        _movimiento(), whatsapp=canal, doctores=("573106492282",), plantilla=""
    ) == 0
    assert canal.llamadas == 0
```

- [ ] **Paso 6: correr y commit**

Run: `uv run pytest tests/test_aviso_citas.py -q`
Expected: todas en verde.

```bash
git add src/maxicare_daniela/config.py src/maxicare_daniela/aviso_citas.py .env.ejemplo tests/test_aviso_citas.py
git commit -m "feat(aviso): una plantilla para los tres movimientos de agenda"
```

---

### Tarea 3: Enganchar el aviso en cancelar y reprogramar

**Files:**
- Modify: `src/maxicare_daniela/herramientas.py` (final de `_cancelar_cita` y de `_reprogramar_cita`)
- Test: `tests/test_herramientas.py`

**Interfaces:**
- Consumes: todo lo que produce la Tarea 2.

- [ ] **Paso 1: las pruebas que fallan**

En `tests/test_herramientas.py`, siguiendo el estilo de las que ya cubren `crear_cita` y su
aviso:

```python
@pytest.mark.asyncio
async def test_cancelar_una_cita_avisa_a_los_doctores(monkeypatch, ctx_con_cita):
    avisos = []
    monkeypatch.setattr(
        herramientas.aviso_citas, "avisar_movimiento_en_segundo_plano", avisos.append
    )

    await herramientas._cancelar_cita(ctx_con_cita, SolicitudCancelacion(...))

    assert len(avisos) == 1
    assert avisos[0].asunto == aviso_citas.ASUNTO_NO_ASISTIRA
    assert avisos[0].cuando == aviso_citas.cuando_una_cita(INICIO_DE_LA_CITA)


@pytest.mark.asyncio
async def test_reprogramar_avisa_con_las_dos_horas(monkeypatch, ctx_con_cita):
    avisos = []
    monkeypatch.setattr(
        herramientas.aviso_citas, "avisar_movimiento_en_segundo_plano", avisos.append
    )

    await herramientas._reprogramar_cita(ctx_con_cita, ID_CITA, NUEVO_INICIO.isoformat())

    assert avisos[0].asunto == aviso_citas.ASUNTO_CAMBIADA
    assert "antes:" in avisos[0].cuando and "ahora:" in avisos[0].cuando


@pytest.mark.asyncio
async def test_una_reprogramacion_que_revienta_no_avisa_de_nada(monkeypatch, ctx_con_cita):
    """Review Focus 5: no se anuncia como hecho un cambio que no ocurrió."""
    avisos = []
    monkeypatch.setattr(
        herramientas.aviso_citas, "avisar_movimiento_en_segundo_plano", avisos.append
    )
    monkeypatch.setattr(
        herramientas.persistencia, "mover_cita", _que_revienta
    )

    with pytest.raises(Exception):
        await herramientas._reprogramar_cita(ctx_con_cita, ID_CITA, NUEVO_INICIO.isoformat())

    assert avisos == []
```

- [ ] **Paso 2: correr y ver el rojo**

Run: `uv run pytest tests/test_herramientas.py -q -k "avisa"`
Expected: FAIL — `AssertionError: assert 0 == 1` en las dos primeras; la tercera pasa ya
(nada avisa todavía), así que **se vuelve a correr tras el paso 3** para comprobar que sigue
verde por el motivo correcto.

- [ ] **Paso 3: implementar**

Al final de `_cancelar_cita`, **después** de `await _con_base(ctx, aplicar)` y antes del
`return`:

```python
    # El aviso cuelga de la TOOL y no del botón del recordatorio: al doctor le cambia la
    # agenda igual si el paciente lo pidió escribiendo. Va después de `aplicar`, así que
    # cuando sale, el cupo ya está libre y el evento ya no está en Calendar. Nada de lo que
    # pase aquí puede tumbar la cancelación: la tarea es de fondo y no propaga.
    aviso_citas.avisar_movimiento_en_segundo_plano(
        aviso_citas.MovimientoDeAgenda(
            asunto=aviso_citas.ASUNTO_NO_ASISTIRA,
            nombre_paciente=cita["nombre_completo"],
            telefono_paciente=cita["telefono"],
            tratamiento=cita["tratamiento"],
            cuando=aviso_citas.cuando_una_cita(cita["inicio"]),
        )
    )
```

Y al final de `_reprogramar_cita`, igual, con `ASUNTO_CAMBIADA` y
`cuando=aviso_citas.cuando_un_cambio(cita["inicio"], destino)`. **Después** de
`await _con_base(ctx, aplicar)`: esa línea solo se alcanza cuando la cita nueva ya está en
Neon y en Calendar y el cupo viejo está liberado, que es lo que cumple el requisito de no
anunciar una reprogramación a medias.

- [ ] **Paso 4: correr y commit**

Run: `uv run pytest tests/test_herramientas.py -q`
Expected: todas en verde, las tres nuevas incluidas.

```bash
git add src/maxicare_daniela/herramientas.py tests/test_herramientas.py
git commit -m "feat(agenda): cancelar y reprogramar avisan a los doctores"
```

---

### Tarea 4: El corte de «Confirmar» en `_entregar`

**Files:**
- Modify: `src/maxicare_daniela/runtime.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Consumes: `seguimientos.BOTON_CONFIRMAR`, `persistencia.cita_del_ultimo_recordatorio`,
  `persistencia.marcar_cita_confirmada`, `aviso_citas.*` de las tareas 1 y 2.

- [ ] **Paso 1: las pruebas que fallan**

En `tests/test_runtime.py`, con los dobles que el archivo ya usa para `_entregar`:

```python
@pytest.mark.asyncio
async def test_confirmar_marca_la_cita_avisa_y_no_abre_turno(monkeypatch, ...):
    atendidos = []
    monkeypatch.setattr(runtime.atencion, "atender", _que_anota(atendidos))
    ...
    await runtime._entregar(_boton("Confirmar"))

    assert atendidos == []          # no se pagó una corrida del modelo
    assert confirmadas == [ID_CITA]
    assert len(avisos) == 1 and avisos[0].asunto == aviso_citas.ASUNTO_CONFIRMADA


@pytest.mark.asyncio
async def test_confirmar_dos_veces_avisa_una_sola(monkeypatch, ...):
    """Review Focus 1: Meta reintenta, y el paciente también pulsa dos veces."""
    ...
    await runtime._entregar(_boton("Confirmar", wamid="wamid.1"))
    await runtime._entregar(_boton("Confirmar", wamid="wamid.2"))

    assert len(avisos) == 1


@pytest.mark.asyncio
async def test_un_rotulo_con_otra_grafia_sigue_hasta_daniela(monkeypatch, ...):
    """Review Focus 2: «Confirmar cita» no confirma nada y NO se traga el mensaje."""
    atendidos = []
    monkeypatch.setattr(runtime.atencion, "atender", _que_anota(atendidos))

    await runtime._entregar(_boton("Confirmar cita"))

    assert confirmadas == []
    assert len(atendidos) == 1


@pytest.mark.asyncio
async def test_sin_cita_que_confirmar_el_mensaje_sigue_su_camino(monkeypatch, ...):
    """El botón llegó tarde: la cita ya pasó. No se inventa nada."""
    monkeypatch.setattr(
        runtime.persistencia, "cita_del_ultimo_recordatorio", lambda *a, **k: None
    )
    atendidos = []
    monkeypatch.setattr(runtime.atencion, "atender", _que_anota(atendidos))

    await runtime._entregar(_boton("Confirmar"))

    assert len(atendidos) == 1


@pytest.mark.asyncio
async def test_la_confirmacion_se_anota_como_respondida(monkeypatch, ...):
    """O el panel pinta como desatendido a quien confirmó su cita (no negociable 32)."""
    await runtime._entregar(_boton("Confirmar", wamid="wamid.1"))

    assert respondidos == [("wamid.1", "wamid.salida")]
```

- [ ] **Paso 2: correr y ver el rojo**

Run: `uv run pytest tests/test_runtime.py -q -k confirm`
Expected: FAIL en las cinco — hoy `_entregar` manda todo a `atender`.

- [ ] **Paso 3: implementar**

Primero el import: `runtime.py` importa `seguimientos`, `persistencia` y `ZONA_BOGOTA`, pero
**no `aviso_citas`**. Añadirlo a la lista `from . import (...)` de la línea 57, en orden
alfabético (entre `autenticacion` y `barrido`).

En `_entregar`, **después** del bloque de la cuota y justo antes del `try` que llama a
`atencion.atender`:

```python
    # «Confirmar»: el único de los tres botones del recordatorio que no necesita a Daniela.
    #
    # Va AQUÍ, en el último lugar de la lista con la que este módulo ya decide si un mensaje
    # se atiende. Después del dedupe de Meta porque confirmar dos veces es inocuo pero avisar
    # al doctor tres veces no lo es; y después de la cuota porque cada confirmación cuesta
    # tres WhatsApps de plantilla, y el perímetro tiene que seguir siendo un techo real.
    #
    # Lo ejecuta el CÓDIGO y no el modelo por la lección del no negociable 14c: cuando algo
    # tiene que pasar siempre, pedírselo al prompt es pedir un favor. `m.tipo` sale del
    # webhook de Meta, nunca del modelo.
    #
    # Si no encuentra cita que confirmar NO se traga el mensaje: devuelve `False` y el
    # turno sigue su camino hasta Daniela, que es el lado barato de equivocarse.
    if m.tipo == "button" and (m.texto or "").strip() == seguimientos.BOTON_CONFIRMAR:
        if await _confirmar_la_cita_del_recordatorio(m):
            return
```

y la función, junto a `_resetear_numero`:

```python
async def _confirmar_la_cita_del_recordatorio(m: ingesta.MensajeEntrante) -> bool:
    """Marca la cita, avisa al doctor y le contesta al paciente. Devuelve si cortó el turno.

    **Nunca lanza.** Un fallo aquí no puede dejar al paciente sin respuesta: se registra y se
    devuelve `False`, y el mensaje sigue hasta Daniela como cualquier otro.
    """
    ahora = datetime.now(ZONA_BOGOTA)
    try:
        cita, es_nueva = await asyncio.to_thread(_confirmar_en_la_base, m.telefono, ahora)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo confirmar la cita de %s; sigue hasta Daniela", m.telefono)
        return False

    if cita is None:
        log.info("%s pulsó «%s» y no tiene cita futura que confirmar", m.telefono, m.texto)
        return False

    # El aviso solo con `es_nueva`: el `IS NULL` del UPDATE es lo que impide que el segundo
    # toque cueste tres WhatsApps más. Al paciente se le contesta igual las dos veces -- él
    # no tiene por qué saber que ya lo había pulsado.
    if es_nueva:
        aviso_citas.avisar_movimiento_en_segundo_plano(
            aviso_citas.MovimientoDeAgenda(
                asunto=aviso_citas.ASUNTO_CONFIRMADA,
                nombre_paciente=cita["nombre_completo"],
                telefono_paciente=cita["telefono"],
                tratamiento=cita["tratamiento"],
                cuando=aviso_citas.cuando_una_cita(cita["inicio"]),
            )
        )

    texto = (
        f"¡Gracias por confirmar! Te esperamos el "
        f"{aviso_citas.cuando_una_cita(cita['inicio'])}. Si algo cambia, escríbenos por aquí."
    )
    try:
        respuesta = await _whatsapp.enviar_texto(m.telefono, texto)
    except Exception:  # noqa: BLE001
        log.exception("la cita de %s quedó confirmada; falló el acuse al paciente", m.telefono)
        return True

    # Y se ANOTA, por lo mismo que `/clearstate`: sin esto el mensaje queda con
    # `respondido_en` NULL y `fallo_respuesta` NULL, que es la firma de «entró y nadie lo
    # procesó», y el panel pintaría como desatendido a quien confirmó su cita.
    try:
        await asyncio.to_thread(_marcar_reseteo_respondido, m.wamid, respuesta)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo anotar la confirmación de %s", m.wamid, exc_info=True)
    return True


def _confirmar_en_la_base(telefono: str, ahora: datetime) -> tuple[dict | None, bool]:
    with persistencia.conectar(config.database_url) as conn:
        cita = persistencia.cita_del_ultimo_recordatorio(conn, telefono, ahora=ahora)
        if cita is None:
            return None, False
        return cita, persistencia.marcar_cita_confirmada(conn, cita["id"], cuando=ahora)
```

`_marcar_reseteo_respondido` ya hace exactamente lo que hace falta; si su nombre estorba,
renómbralo a `_marcar_respondido_sin_turno` y actualiza los dos llamadores — es un `Ruling`
que se anota en el ledger.

- [ ] **Paso 4: correr todo y commit**

Run: `uv run pytest -q`
Expected: la suite entera en verde.

```bash
git add src/maxicare_daniela/runtime.py tests/test_runtime.py
git commit -m "feat(recordatorio): «Confirmar» lo ejecuta el codigo y no abre turno"
```

---

### Tarea 5: El prompt y el script de plantillas

**Files:**
- Modify: `src/maxicare_daniela/agentes.py:670-695` (el bloque «YA LE ESCRIBIMOS NOSOTROS»)
- Modify: `scripts/probar_plantilla.py:105-118`
- Test: `tests/test_agentes.py`

- [ ] **Paso 1: la prueba que falla**

```python
def test_el_prompt_le_dice_a_daniela_los_tres_botones_del_recordatorio():
    """Hasta hoy solo enumeraba los de las reactivaciones, y uno de estos pide cancelar."""
    prompt = agentes.instrucciones(_contexto(ultimo_recordatorio_tipo="recordatorio_cita"))

    for rotulo in seguimientos.BOTONES_DEL_RECORDATORIO:
        assert rotulo in prompt
    assert "confírmalo con él antes de cancelar" in prompt.lower()
```

- [ ] **Paso 2: correr y ver el rojo**

Run: `uv run pytest tests/test_agentes.py -q -k botones`
Expected: FAIL — el bloque está cerrado con `if recordatorio in
seguimientos.TIPOS_DE_REACTIVACION`.

- [ ] **Paso 3: implementar**

Abrir el bloque para que el recordatorio de cita también enumere los suyos, con la
instrucción de que «No puedo asistir» **se confirma antes de cancelar** y de que «Confirmar»
normalmente no llega hasta ella (lo atiende el código) pero puede llegar si la cita ya pasó.
Toca el prompt: rompe el caché de entrada una vez, y eso es un coste de un despliegue.

- [ ] **Paso 4: el script**

En `scripts/probar_plantilla.py`, `_Plantilla.botones` pasa de `tuple[str, str]` a
`tuple[str, ...]` y el recordatorio declara los tres. El script **dobla a propósito** (los
escribe a mano para que una persona los compare con el teléfono), así que **no los importa**
de `seguimientos`.

- [ ] **Paso 5: correr y commit**

Run: `uv run pytest -q` y `uv run python scripts/probar_plantilla.py --estado`
Expected: suite en verde; el script imprime los tres rótulos y no gasta nada.

```bash
git add src/maxicare_daniela/agentes.py scripts/probar_plantilla.py tests/test_agentes.py
git commit -m "feat(prompt): Daniela sabe que el recordatorio trae tres botones"
```

---

## Verificación final

- `uv run pytest -q` — entera.
- `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`.
- Los seis scripts que no gastan, porque esta rama toca firmas que doblan:
  `probar_tools.py`, `probar_recordatorios.py`, `probar_reactivacion.py`, `probar_web.py`,
  `probar_relevo.py`, `probar_calendario.py`.
- `scripts/probar_plantilla.py --estado` — sin gastar.
- **NO correr `scripts/probar_panel.py`**: cambia un precio y siembra un día de agenda en el
  `public` de producción, que desde el 21/09/2026 tiene pacientes reales.

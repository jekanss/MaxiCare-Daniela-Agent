# Fase 8 — La agenda real y la marca de asistencia

Fecha: 20/09/2026 · Rama: `fase-8-agenda`, desde `main` (`1ee143d`)

## El problema

`web/src/pantallas/Agenda.tsx` es una maqueta declarada: datos de ejemplo, `marcar()` solo
toca el estado de React, y un aviso que no se puede cerrar diciéndolo. `citas.asistio` existe
desde la migración 001 con este comentario:

> «`asistio` es NULL hasta que alguien de la clínica lo marque desde la interfaz web. Es la
> ÚNICA fuente de ese dato: Daniela no sabe qué pasó dentro del consultorio.»

Nadie la lee ni la escribe. Eso deja tres cosas paradas:

1. La métrica de asistencia, que es un criterio de éxito del brief, no existe.
2. `reactivacion_no_asistio` está aprobada por Meta y cableada entera en la rama
   `reactivacion-leads`, y no la dispara nadie.
3. El entregable de la fase 8 sigue sin cumplirse.

## Alcance

**Entra:** la agenda del día leyendo datos reales, y marcar asistencia de verdad.

**No entra: las tres perillas** (`capacidad_por_hora`, `duracion_cita_minutos`,
`cierre_relevo_minutos`). El entregable de la fase 8 las pide; los pendientes registran que el
cliente rechazó esa pantalla el 13/09/2026 —«se cambian por SQL y así se queda»—. Manda la
decisión del cliente. Queda escrito como renuncia (ver el último apartado), porque hoy el plan
y la realidad dicen cosas distintas.

## Decisiones, con su alternativa descartada

| Decisión | Alternativa descartada |
|---|---|
| **Reconciliar contra Calendar al abrir el día**, corrigiendo Neon. | Pintar Neon a secas. Cuesta marcar «no asistió» sobre una cita que el doctor movió, y ese es el disparador de un WhatsApp a un paciente que sí tiene cita. |
| **Rama nueva desde `main`.** | Construir sobre `reactivacion-leads`. Dejaría la fase 8 rehén de una respuesta legal que no depende de nosotros. |
| **Corregir una marca se puede, y queda en `cambios_configuracion`.** | Sin registro. Equivocarse marcando es normal; que la métrica del mes cambie sin que nadie sepa por qué, no. |
| **Los tres roles marcan** (`admin`, `doctor`, `recepcion`). | Solo admin. Es la tarea más repetida del producto y quien ve entrar al paciente es recepción: un permiso más estrecho termina en un dato que nadie marca. |
| **El botón solo aparece cuando la hora de inicio ya pasó.** | Marcar cualquier cita. Un «no asistió» puesto a las 8:00 sobre una cita de las 16:00 es un mensaje automático a alguien que todavía va a venir. |

## 1. El refactor: una reconciliación, dos presentaciones

`herramientas._sincronizar_con_calendar(ctx, citas)` (`herramientas.py:1687`) no se puede
llamar desde el panel: depende del `ContextoDaniela` de un turno de WhatsApp y devuelve frases
redactadas para el modelo («díselo al paciente con naturalidad, discúlpate por el cambio»).

Se parte en núcleo y presentación:

```
                          ┌─ _sincronizar_con_calendar(ctx, citas)
                          │     traduce a las frases de HOY, sin cambiar una coma
reconciliar_con_calendar ─┤
                          └─ runtime.api_agenda(...)
                                traduce al aviso de la pantalla
```

Firma del núcleo, en `herramientas.py` (donde ya viven sus dos ayudantes y sus pruebas):

```python
async def reconciliar_con_calendar(
    *,
    database_url: str,
    calendario: CalendarioGoogle | None,
    citas: list[dict[str, Any]],
    ahora: datetime,
    jornada: Jornada,
    capacidad_por_hora: int,
    duracion_cita_minutos: int,
    hora_recordatorio_vispera: int,
    horas_minimas_para_recordar: int,
) -> tuple[list[dict[str, Any]], list[Correccion]]:
```

`Correccion` es un dato, no una frase:

```python
@dataclass(frozen=True)
class Correccion:
    cita_id: str
    que_paso: Literal["movida", "cancelada"]
    hora_vieja: datetime
    hora_nueva: datetime | None   # None cuando la borraron
    tratamiento: str
    nombre_completo: str
```

`_sincronizar_con_calendar` queda como envoltura: llama al núcleo con los campos de `ctx` y
traduce cada `Correccion` a **exactamente el mismo texto que produce hoy**. Ese «exactamente»
es el requisito: lo que le llega a Daniela no cambia.

**La red del refactor son tres pruebas que ya existen y que no se tocan:**
`test_herramientas.py:1625` (la movieron, dice la hora nueva), `:1692` (la borraron, se
cancela) y `:1780` (sincronizar dos veces no deja la cita sin recordatorio). Tienen que seguir
verdes sin editarlas. Se añade una cuarta que fija el texto de las novedades carácter a
carácter, para que un refactor futuro no lo cambie por descuido.

Esto toca el no negociable 20, en producción. Es la parte de mayor riesgo del trabajo y va
primero, sola, con la suite en verde antes de seguir.

## 2. Qué ocurre al abrir la agenda

```
GET /api/agenda?dia=2026-09-20
  ① panel.citas_del_dia(conn, desde, hasta)        <- Neon
  ② reconciliar_con_calendar(...)                  <- Google manda (no negociable 20)
       ├─ misma hora    -> nada
       ├─ hora distinta -> mueve en Neon (cupo + recordatorio) + Correccion
       └─ ya no está    -> cancela en Neon + libera cupo + Correccion
  ③ calendario.bloqueos(desde, hasta)              <- las franjas del doctor
  ④ panel.citas_sin_marcar(conn, 7 días atrás)     <- el «ayer sin marcar»
```

**Abrir esta pantalla escribe en la base.** Puede mover una cita, cancelarla y tocar cupos.
Es deliberado: la pantalla es el mejor disparador que esa reconciliación va a tener —hoy solo
corre cuando un paciente pregunta por su cita, y no hay ningún barrido—. Queda dicho en el
docstring del endpoint, porque un `GET` que escribe sorprende a cualquiera que lo lea después.

Reglas que lo acotan:

- **Se reconcilia cualquier día que se abra**, pasado o futuro. Para una cita ya pasada,
  `seguimientos.momento_del_recordatorio` devuelve `None` y no se programa nada: eso se fija
  con una prueba propia, porque hoy es una propiedad que nadie ha escrito.
  > **SUPERADO el 20/09/2026, en la revisión final de la rama.** «Cualquier día» abría un
  > camino que este spec no vio: si la clínica limpia de Google Calendar los eventos de una
  > semana vieja, mirar ese día en la Agenda daba esas citas por canceladas en Neon y las
  > dejaba **inmarcables para siempre** —con ellas, la métrica de asistencia, que es el
  > entregable de esta fase—. La reconciliación del panel queda acotada a
  > `herramientas.DIAS_HACIA_ATRAS_AL_SINCRONIZAR` hacia atrás, en `runtime._fuera_de_la_ventana`
  > (el núcleo no cambia), y el día más viejo se pinta con `calendario_disponible: false`.
- **Sin calendario, la agenda sigue funcionando.** Si `ctx.calendario` no se puede construir o
  Google falla, se pinta Neon tal cual y la respuesta lleva `calendario_disponible: false`,
  que la pantalla muestra como aviso. Una pantalla de operación no puede caerse porque Google
  tenga un mal día.
- **Una cita movida a otro día desaparece del día que se está mirando.** Sin decirlo, la
  recepcionista ve un hueco inexplicable. Por eso las correcciones se pintan arriba, con la
  hora vieja dentro, igual que hace el texto de la tool (no negociable 20).

Coste: un día con 20 citas son 20 llamadas a Google, secuenciales como hoy. Del orden de 3 a 5
segundos. Paralelizarlas es una mejora posterior y no entra aquí: no se mezcla una optimización
con el refactor que toca el no negociable 20.

## 3. El SQL, en `panel.py`

Tres funciones nuevas. Reciben `conn`, no importan FastAPI, y las que escriben anotan con
`_anotar` (`panel.py:72`) dentro de la misma transacción, como ya hacen tratamientos y fichas.

```python
def citas_del_dia(conn, *, desde: datetime, hasta: datetime) -> list[dict]:
    """Las citas vivas de ese rango, con `asistio`. Ordenadas por `inicio`.

    Estado IN ('confirmada','reprogramada'). Devuelve id, telefono, nombre_completo,
    tratamiento, inicio, duracion_minutos, estado, asistio, evento_calendar_id,
    reserva_id, conversacion_id. El índice `ix_citas_inicio` ya la soporta.
    """

def marcar_asistencia(conn, *, cita_id: str, valor: bool | None, usuario: str) -> dict:
    """UPDATE citas SET asistio + su fila de bitácora, en UNA transacción.

    ValueError si la cita no existe, si su estado es 'cancelada', o si `inicio` es
    futuro. La bitácora va con tabla='citas', clave=cita_id y los literales
    'asistio' / 'no_asistio' / 'sin_marcar' (este último para el desmarcado, porque
    `_anotar` no admite un `nuevo` nulo).
    """

def citas_sin_marcar(conn, *, desde: datetime, hasta: datetime, limite: int = 50) -> list[dict]:
    """Las de días anteriores cuya hora pasó y siguen con `asistio IS NULL`."""
```

El rango de un día es `[00:00, 24:00)` en hora de Bogotá (`ZONA_BOGOTA`, que ya existe), no la
jornada: una cita fuera de horario tiene que verse, no esconderse.

## 4. Los endpoints, en `runtime.py`

Los dos van **antes** de la ruta comodín (`runtime.py:2155`), que si no se los traga.

| Ruta | Dependencia | Qué hace |
|---|---|---|
| `GET /api/agenda?dia=YYYY-MM-DD` | `usuario_actual` | el día reconciliado |
| `PATCH /api/agenda/citas/{cita_id}` | `exigir_rol("admin","doctor","recepcion")` | marca o desmarca |

`dia` es opcional; sin él, hoy en Bogotá.

Cuerpo del PATCH: `{"asistio": true | false | null}`, campo **obligatorio y nullable** —`null`
es desmarcar, y tiene que distinguirse de «no lo mandé»—.

Respuesta del GET:

```json
{
  "dia": "2026-09-20",
  "citas": [...],
  "bloqueos": [...],
  "correcciones": [...],
  "sin_marcar": [...],
  "calendario_disponible": true
}
```

La lista blanca de roles es explícita en vez de `usuario_actual` aunque hoy sean los tres que
existen: un rol nuevo no debe heredar el permiso de escribir el único dato que nadie más tiene.

`dia` y `asistio` entran en `_NOMBRE_DEL_CAMPO` (`runtime.py:117`) para que el 422 salga en
castellano. `ValueError` de `panel` se traduce a `HTTPException(400)`, y a `404` cuando la
cita no existe, como ya hace `cambiar_tratamiento`.

## 5. La pantalla

Se reescribe `Agenda.tsx` sobre su propia maqueta, conservando la jerarquía visual que trajo de
Figma: **la acción de asistencia sigue siendo lo más grande y lo más fácil de encontrar de toda
la aplicación**, que es lo que pide la especificación de la pantalla. El aviso permanente de
«esto no guarda nada» desaparece, que es el punto del trabajo.

Cambios obligados por la realidad de los datos:

- **Se quita el `origen` del paciente** («Google Ads», «Instagram», «referido»). No existe:
  `citas` no tiene esa columna y `conversaciones.canal` solo distingue `whatsapp` de `web`.
  La maqueta lo inventó. No se fabrica el dato ni se pone `PENDIENTE` en una pantalla de cara
  al usuario: sale.
- El botón de marcar aparece solo si la hora de inicio ya pasó. Antes de eso, la franja se
  pinta sin botones.
- Franja de correcciones arriba, con la hora vieja dentro.
- Navegación por día: ayer / hoy / mañana y un selector de fecha.
- La lista de «sin marcar» de días anteriores, que es lo que impide que el dato se pierda por
  un día de mucho trabajo.

Se respetan las convenciones que ya siguen las otras pantallas: `SP` como fuente, cabecera
repetida en los tres estados (cargando, error, contenido), Tailwind para layout y `style` con
hex para color, y **`alCaducarSesion` consumido por `ref` con el `useCallback` de deps
vacías** — la trampa documentada en `web/CLAUDE.md`, que si se «corrige» deja la pantalla
releyendo Neon en bucle.

En `web/src/api.ts`: los tipos `CitaDeAgenda`, `BloqueoDeAgenda`, `CorreccionDeAgenda` y las
funciones `leerAgenda(dia)` y `marcarAsistencia(citaId, valor)`, pasando por `pedir`. El id
`agenda` ya existe en `Sidebar.SeccionId`; solo falta quitarle la marca de fase pendiente y
añadir la rama en `App.tsx`.

## 6. Pruebas

- **El 401 de las dos rutas nuevas se cubre solo**: `test_web.py:100` descubre las rutas de
  `runtime.app.routes` y parametriza sobre ellas. Si se olvida la dependencia, cae.
- `test_panel.py` con `@pytest.mark.neon` (esquema `pruebas`): el día devuelve lo que se
  insertó y en orden; marcar escribe la columna **y** su fila de bitácora en una sola
  transacción; corregir deja dos filas con el valor anterior correcto; una cita futura y una
  cancelada se rechazan; `citas_sin_marcar` no devuelve las ya marcadas.
- Offline con `TestClient` y `monkeypatch`: `recepcion` puede marcar, un rol inventado no; la
  forma del 400 y del 404; y que sin calendario el GET responde 200 con
  `calendario_disponible: false`.
- `test_herramientas.py`: las tres existentes intactas y verdes, más la que fija el texto de
  las novedades y la que fija que reconciliar una cita pasada no programa recordatorio.
- `scripts/probar_panel.py`: un bloque nuevo que recorre la agenda de punta a punta contra
  Neon. No gasta tokens salvo con `--chat`, como el resto del script.

## 7. Lo que queda escrito

1. **Las tres perillas no se hacen.** Se anota en el `condicion_revision` de la fase 8 del
   plan, con el dato que hoy no está en ninguna parte: `cierre_relevo_minutos` y
   `aviso_relevo_minutos` se cachean en memoria al arrancar (`runtime.py:203`), así que un
   `UPDATE` en Neon no surte efecto hasta reiniciar el proceso. Quien construya esa pantalla
   algún día tiene que saberlo antes de prometer «sin desplegar».
2. **`reactivacion_no_asistio` queda a un paso.** Con `asistio` ya escribiéndose, encenderla
   es añadir `TIPO_NO_ASISTIO` a `TIPOS_QUE_EL_BARRIDO_ENCOLA` el día que
   `reactivacion-leads` se funda. Se anota en esa rama, no en esta.
3. **Un `GET` que escribe.** Queda en el docstring del endpoint y en `web/CLAUDE.md`.

## Riesgos

| Riesgo | Qué lo contiene |
|---|---|
| El refactor cambia lo que Daniela le dice al paciente sobre una cita movida | Tres pruebas existentes sin tocar, más una que fija el texto literal |
| Abrir la agenda de un día pasado programa recordatorios raros | Prueba propia de que una cita pasada no programa nada |
| Google lento o caído deja la pantalla inservible | `calendario_disponible: false` y Neon a secas |
| Marcar la cita equivocada porque Neon estaba desfasado | La reconciliación corre antes de pintar, que es el motivo de todo el apartado 1 |
| Dos personas marcando la misma cita a la vez | La segunda gana y su cambio queda en la bitácora con el valor anterior. Mismo límite aceptado que las fichas, por el mismo motivo |

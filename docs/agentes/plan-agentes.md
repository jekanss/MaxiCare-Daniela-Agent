# Daniela — arquitectura del agente conversacional de MaxiCare

**Cliente:** MaxiCare, clínica dental en Puente Largo, Bogotá
**Proyecto:** `maxicare-daniela`
**Fecha:** 2026-09-12
**Contrato de origen:** `docs/agentes/brief-agentes.json`
**Contrato que producen estas páginas:** `docs/agentes/plan-agentes.json`

Este documento es el registro de **por qué** la arquitectura es como es. El `.json` es lo único
que leen las herramientas de construcción; esto es lo que leen las personas. Cada bloque trae
las cuatro partes con las que se aprobó: qué se decidió, por qué, qué se descartó y qué
costaba, y qué evento obliga a revisarlo.

Las once decisiones se tomaron una por una, en orden de dependencia, con aprobación explícita
entre cada una.

---

## La arquitectura en un diagrama

```
                        mensaje entrante  ·  WhatsApp / web
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │  ¿tomada_por un doctor?       │
                    └───────────────────────────────┘
                          sí │              │ no
                             ▼              ▼
                        ╔═════════╗   ┌──────────────────────┐
                        ║ SILENCIO║   │  ¿trae adjunto?      │
                        ║  total  ║   └──────────────────────┘
                        ╚═════════╝       sí │         │ no
                             │                ▼         │
                    cierre automático  ┌──────────────────────┐
                    a las 3 h          │ CAPA DE INGESTA      │
                                       │ descargar el archivo │
                                       │ reenviar a Telegram  │
                                       │ transcribir si audio │
                                       └──────────────────────┘
                                                  │              │
                                                  ▼              │
                                       ┌──────────────────────┐  │
                                       │  lector_archivos     │  │
                                       │  (agente 2)          │  │
                                       └──────────────────────┘  │
                                          │              │       │
                              contexto_clinico   datos_conversacion
                                          │              │       │
                                          ▼              └───┬───┘
                                    ╔═══════════╗            │
                                    ║ TELEGRAM  ║      ══ EL MURO ══
                                    ║ doctores  ║            │
                                    ╚═══════════╝            ▼
                                                   retardo variable (seg.)
                                                             ▼
                                       ┌──────────────────────────────┐
                                       │  daniela  (agente 1)         │
                                       │  9 tools · max_turns = 15    │
                                       │  5 guardrails                │
                                       └──────────────────────────────┘
                                                             │
                                                             ▼
                                       ┌──────────────────────────────┐
                                       │ ¿requiere_escalamiento?      │
                                       └──────────────────────────────┘
                                            sí │            │ no
                                               ▼            ▼
                                         TELEGRAM +    responder
                                         responder     y seguir
```

**El principio que atraviesa todo el diseño:** cada regla que importa está hecha cumplir por
una estructura —un tipo, una restricción de base de datos, una línea de enrutamiento— y no por
una instrucción que el modelo deba recordar. Las instrucciones se olvidan cuando el contexto
crece; los tipos no.

---

## Bloque 1 — Agentes y responsabilidades

### Qué se decidió

Dos agentes, en modalidad texto, sin sandbox, los dos con un modelo de **clase general** en
vez de la clase económica que el manual trae por defecto. `lector_archivos` necesita visión.

### Por qué

- **Modalidad texto.** `usuario_canal.canal` nombra WhatsApp y una interfaz web: canales
  escritos. Las notas de voz **no** hacen de esto un agente de voz: se transcriben en la capa
  de ingesta y entran al agente como texto. Eso evita `pip install 'openai-agents[voice]'`, dos
  etapas de modelo por turno y una superficie de fallo completa.
- **Visión obligatoria** en `lector_archivos`: llegan fotos y PDF.
- **Clase general**, contra el default, por tres campos del brief que empujan igual:
  `limites.costo_por_turno` sin techo, `usuario_canal.volumen_esperado` de ~2.400 turnos al
  mes, y `limites.latencia_maxima` que no solo tolera un modelo lento sino que **pide** que la
  respuesta tarde.

La razón de fondo es otra: `autonomia.criterio_limite` pide *«leer para orientar sí, leer para
opinar no»*. Es la instrucción condicional más delicada del sistema, y la debilidad documentada
de la clase económica es precisamente la adherencia a instrucciones condicionales largas cuando
el contexto crece. Aquí el contexto crece: 8 mensajes por conversación más historial.

### Qué se descartó y qué costaba

**La clase económica.** Ahorro real: del orden de unas decenas de dólares al mes con este
volumen. Lo que se paga a cambio no aparece en un log: aparece como Daniela diciéndole a un
paciente un precio que MaxiCare nunca aprobó, y solo lo detectaría la revisión manual de
veracidad, que por definición mira una muestra.

**La clase de razonamiento**, aunque la latencia la permitiría: paga tokens invisibles para un
trabajo que no es encadenar restricciones sino sostener una conversación con reglas.

**Procesar video.** Es el adjunto más caro y el de mayor riesgo de comentario clínico, a cambio
de casi nada de información útil. El video se reenvía y se escala, no se interpreta.

### Cuándo se reabre

Hacia abajo, si la suite de evals del bloque 10 pasa al 100% con la clase económica sobre 50
imágenes reales. Hacia arriba, si el volumen real resulta un orden de magnitud mayor que lo
estimado — `usuario_canal.volumen_esperado` ya registra la tensión sin resolver entre 97/mes,
20–30/día y 10/día. El identificador exacto del modelo no se fija aquí: `Agent(model=...)`
acepta cualquier string y el SDK no valida que exista, así que se confirma contra el catálogo
del proveedor al construir.

---

## Bloque 2 — Uno vs. varios

### Qué se decidió

Dos agentes: `daniela` y `lector_archivos`. Contra el default del manual, que es uno.

### Por qué

La frontera cae exactamente sobre la única fila que justifica partir sin discusión: **dos voces
de salida con distinto destinatario** (`usuario_canal.usuario_final`).

Del mismo archivo salen dos salidas con dos audiencias y **umbrales opuestos** de qué se puede
decir:

| Salida | Destinatario | Qué puede contener | Por dónde sale |
|---|---|---|---|
| `contexto_clinico` | El doctor | Lo que el documento dice, clínico incluido, citado con fidelidad | Telegram, junto al archivo |
| `datos_conversacion` | El paciente | Solo tratamiento, origen y fecha. Nada clínico | La conversación de Daniela |

Un solo agente tendría que sostener en su prompt *«cuando le escribas al doctor puedes decir
esto, cuando le escribas al paciente no»*, aplicado a datos de salud. Partido, la separación
deja de ser una instrucción y pasa a ser **enrutamiento**: el código manda un campo a Telegram
y el otro al contexto de Daniela. El campo clínico nunca entra a la conversación porque el
código nunca lo pone ahí.

> **Nota sobre una corrección que hizo el cliente y que mejoró el diseño.** La primera versión
> de este bloque argumentaba «Daniela no puede ver los píxeles». El cliente la corrigió: leer
> está bien, lo que importa es **a quién se le dice**. Interpretar para el doctor es justamente
> el propósito del escalamiento. Eso reubicó la frontera sobre el destinatario y la volvió más
> sólida.

### Qué se descartó y qué costaba

**Un solo agente.** Gana un prompt, un trace, una suite de evals, y no paga **depuración
cruzada** — que es el costo principal de partir y no se puede bajar: cuando Daniela responda
raro ante una remisión, habrá que leer dos tramos para saber si extrajo mal o redactó mal.
Pierde que la separación entre lo que ve el doctor y lo que ve el paciente vuelva a depender
del prompt.

**Un agente aparte para los doctores.** Parecía el candidato obvio y no lo es: cuando el doctor
toma la conversación, Daniela reenvía su texto **literal**. Copiar un string no necesita un
modelo. El modo relevo completo cuesta **cero llamadas de modelo**.

**Un agente por canal.** El chat web prueba contra la misma Daniela; partirlo garantizaría que
lo que se prueba no es lo que corre.

Costos pagados: latencia solo en turnos con adjunto (con 60 s de presupuesto), prompt duplicado
bajo (el lector no comparte tono ni base de conocimiento), tools duplicadas ninguna (el lector
no tiene tools), y depuración cruzada, que es real. En dinero: del orden de 50–100 llamadas
extra al mes.

### Cuándo se reabre

Si el cliente decide que el contexto para el doctor también puede ser mínimo y no clínico, las
dos salidas se vuelven una y la frontera desaparece. Si los evals muestran que el lector pierde
matiz que Daniela sí habría usado y eso degrada respuestas medibles. Si aparece un tercer
candidato a agente, no se agrega sin volver a la tabla de costos.

---

## Bloque 3 — Orquestación

### Qué se decidió

Patrón **`codigo`**: código determinista alrededor de los dos agentes.

### Por qué

No hay un solo rombo en el flujo. Las tres bifurcaciones son `if` sobre campos que ya existen:

| Bifurcación | Qué la decide |
|---|---|
| ¿Un doctor tiene tomada esta conversación? | Una columna en Neon |
| ¿El mensaje trae adjunto? | El `type` del webhook de WhatsApp |
| ¿Daniela pidió escalar? | Un booleano de `RespuestaDaniela` |

Ninguna necesita un modelo leyendo la intención del usuario.

`lector_archivos` corre **antes** de `Runner.run(daniela)`, en la capa de ingesta.

### Qué se descartó y qué costaba

**`agentes_como_tools`** — el default del manual cuando dos opciones parecen aplicar. Es
técnicamente imposible aquí: para que Daniela llame al lector pasándole la imagen, tendría que
tener la imagen. El muro se cae.

**`handoffs`** — le entregaría el turno al único agente que sí tiene el contenido clínico,
poniéndolo a hablar con el paciente. Es literalmente el escenario que el diseño existe para
impedir, y su fallo es silencioso: no lanza excepción, solo produce una respuesta que no debía
existir.

**`hibrido`** — no hay un rombo real que lo justifique, y cuesta una regla de «cuál mecanismo
aplica cuándo» que vive en la cabeza de quien lo escribió.

**Lo que `codigo` cuesta, y es real:** cada rama nueva es un cambio de código y un despliegue.
Con un flujo fijo es el costo correcto; con requisitos que cambian cada semana sería el
equivocado.

### Cuándo se reabre

Cuando aparezca un rombo de verdad. El caso concreto: que MaxiCare quiera especialistas con voz
propia —un agente de ortodoncia y uno de cirugía hablándole directo al paciente— y ahí
`handoffs` vuelve a la mesa. O si los `if` del orquestador crecen hasta parecer una máquina de
estados que nadie entiende.

---

## Bloque 4 — Contratos Pydantic

### Qué se decidió

Cinco modelos: dos `output_type` y tres modelos de entrada de tool. Las tools de solo lectura
van con argumentos sueltos.

### El contrato que sostiene el diseño

```python
class LecturaArchivo(BaseModel):          # output_type de lector_archivos
    tipo_documento:   Literal['remision_externa','radiografia','foto_clinica',
                              'examen','cotizacion','video','otro']
    tratamiento:      Literal['cordales','implantes','blanqueamiento','ortodoncia',
                              'diseno_sonrisa','microdiseno','coronas','periodoncia',
                              'gingivectomia','bichectomia','limpieza','endodoncia',
                              'protesis','no_identificado']
    origen:           str | None
    fecha_documento:  date | None
    confianza:        Literal['alta','media','baja']
    contexto_clinico: str                  # ── SOLO va a Telegram ──
```

**`tratamiento` es un `Literal`, no un `str`.** Ahí está toda la decisión. Si fuera `str`, el
lector podría escribir *«extracción de cuatro cordales incluidos 18, 28, 38 y 48»* y eso
llegaría a Daniela. Siendo un `Literal` cerrado, el único valor que Pydantic acepta es
`'cordales'`. No hay forma de que quepa una frase clínica en ese campo — no porque el modelo se
contenga, sino porque el tipo no lo admite.

```python
class RespuestaDaniela(BaseModel):        # output_type de daniela
    mensaje_al_paciente:   str
    estado_oportunidad:    Literal['explorando','comparando','con_barrera',
                                   'listo_para_agendar','agendado','post_atencion']
    barrera_detectada:     Literal['precio','miedo','tiempo','desplazamiento',
                                   'confianza','comparacion','ninguna']
    requiere_escalamiento: bool
    motivo_escalamiento:   Literal['clinico','excepcion_comercial','agenda_llena',
                                   'archivo_recibido','dato_faltante','ninguno']
    fuera_de_alcance:      bool
```

Cada campo paga algo concreto: `requiere_escalamiento` es el `if` del orquestador,
`estado_oportunidad` es lo que `trabajo.pasos` pide (estado comercial y operativo, no
«frío/tibio/caliente»), `barrera_detectada` permite no repetir un argumento ya rechazado, y
`fuera_de_alcance` mide el criterio de alcance temático. Sin estructura, seis de las siete
métricas del bloque 10 dejarían de calcularse con SQL.

### Dos ausencias deliberadas en `SolicitudCita`

```python
class SolicitudCita(BaseModel):           # crear_cita y reprogramar_cita
    nombre_completo:     str
    inicio:              datetime
    tratamiento:         Literal[...]
    clave_idempotencia:  str
    # telefono ── la tool lo lee del contexto local
    # cedula   ── no existe, por prohibición expresa del cliente
```

**No hay cédula**, porque `datos.quien_ve_que` la prohíbe expresamente: un campo que no existe
no se puede llenar por error. **No hay teléfono**, porque viene del webhook y vive en el
contexto local; si el modelo lo escribiera, podría escribir otro — el caso real es la hija
agendando por su madre y el recordatorio yéndose al número equivocado.

### Dónde vive cada validación

| Regla | Dónde va | Qué pasa si va mal |
|---|---|---|
| `inicio` es una fecha válida | `schema` | ✅ |
| El tratamiento es uno de los 14 | `schema` | ✅ |
| **«Ese horario ya tiene 2 pacientes»** | **`codigo_tool`** | ❌ En el schema, el modelo lo lee como error de forma: cambia la hora y vuelve a llamar a ciegas hasta agotar `max_turns` |
| «No se responden preguntas clínicas» | `guardrail` | Es política sobre el turno completo, no sobre un campo |

La regla: **una regla de negocio se devuelve como resultado de la tool, con su explicación —
nunca como error de validación.**

### Una excepción declarada

El manual dice que ningún `output_type` debe llevar un dato sensible.
`LecturaArchivo.contexto_clinico` lo lleva, a propósito, porque es el único camino para que el
doctor reciba el contexto que el cliente pidió. Consecuencias asumidas: `trace_include_sensitive_data`
queda obligatoriamente en `False`, el campo no se persiste en Neon, y es lo primero que hay que
poner sobre la mesa cuando ocurra la revisión legal pendiente.

### Cuándo se reabre

Cuando MaxiCare agregue un tratamiento desde la interfaz web y el `Literal` deje de reflejar la
base de conocimiento. Si `confianza: 'baja'` resulta frecuente. Cuando la revisión legal se
pronuncie sobre `contexto_clinico`.

---

## Bloque 5 — Herramientas

### Qué se decidió

**Nueve tools, todas `function_tool`, ninguna `hosted`.** `lector_archivos` tiene cero.

| Tool | Sistema | Acceso | Clave de idempotencia |
|---|---|---|---|
| `consultar_base_conocimiento` | Neon | lee | — |
| `consultar_disponibilidad` | Calendar + Neon | lee | — |
| `identificar_paciente` | Neon | lee | — |
| `crear_cita` | Neon + Calendar | escribe | `telefono + inicio` |
| `reprogramar_cita` | Neon + Calendar | escribe | `id_cita + inicio_destino` |
| `cancelar_cita` | Neon + Calendar | escribe | `id_cita` |
| `registrar_estado_oportunidad` | Neon | escribe | `id_conversacion` (upsert) |
| `programar_seguimiento` | Neon | escribe | `id_conversacion + tipo + fecha` |
| `escalar_a_doctores` | Telegram | escribe | `id_conversacion + turno` |

**Ninguna `hosted`:** una tool alojada corre del lado del proveedor y el dato de entrada sale de
la infraestructura propia. Con datos de salud y `restricciones.legales` en PENDIENTE, no es el
momento.

### `crear_cita` — la tool que decide si el diseño funciona

`sistemas.lista[Google Calendar].notas` lo dejó escrito: *«el control de capacidad tiene que ser
real y no optimista»*. Google Calendar no sabe contar hasta 2 ni tiene candados. Si tres
conversaciones consultan a la vez, las tres leen «hay cupo» y las tres escriben.

**Neon es la verdad del cupo; Calendar es el espejo.**

```
   crear_cita(María, jueves 10am, cordales)
              │
              ▼
   INSERT en reservas ── UNIQUE (inicio, cupo_num), cupo_num ∈ {1,2}
        │                          │
   insert OK                  insert falla
   (tomó cupo 2)              (los 2 ocupados)
        │                          │
        ▼                          ▼
   crear evento          ╔═══════════════════════════╗
   en Calendar           ║ NO es un error.           ║
     │        │          ║ Es un RESULTADO:          ║
    ok      falla        ║ "lleno, libres 11am y 2pm"║
     │        │          ╚═══════════════════════════╝
     ▼        ▼
 confirmar  DELETE de la reserva + raise → escalar
 a María    (nunca se le dice "listo" a María)
```

La base de datos decide con un `UNIQUE`. Es atómico: tres conversaciones simultáneas, dos
entran y la tercera rebota, sin candados que mantener.

Es la **única** tool con `failure_error_function=None`, para el caso Neon-ok/Calendar-falla:
seguir significaría decirle a la paciente que quedó agendada cuando el calendario que miran los
doctores no la tiene.

### `consultar_base_conocimiento` — la ausencia como dato

La sección 2.12 del documento maestro dice que endodoncia y prótesis **no tienen precio
documentado**. Entonces:

```
  ❌ devolver ""      ← el vacío invita a rellenar
  ❌ devolver null
  ✅ devolver "SIN DATO DOCUMENTADO. No existe precio aprobado para
               endodoncia. Prohibido estimar o extrapolar. Escalar."
```

Un resultado vacío es una invitación a que el modelo complete el hueco con algo razonable, y
«algo razonable» sobre un precio es el fallo que rompe el primer objetivo del brief.

### Tools que se esconden

Mientras `identidad_verificada` sea `False`, `is_enabled` hace que `crear_cita`,
`reprogramar_cita` y `cancelar_cita` **no existan** para el modelo. Una hija escribiendo desde
un número nuevo no puede cancelarle la cita a su madre porque la herramienta no está en la mesa.
Cuesta cero llamadas de modelo.

### Cómo falla cada una

`timeout` explícito en todas las que salen por red, con `error_as_result`, para que un sistema
colgado no se lleve el turno entero y rompa el tope de un minuto. `failure_error_function`
**propia** en todas, no el formateador por defecto: con el default, el modelo recibe un error
genérico y decide qué inventar, y la invención más natural ante un fallo de Calendar es
tranquilizar al paciente con un horario que nadie reservó.

### Qué se descartó y qué costaba

**Contar el cupo leyendo Calendar.** Es lo que se escribe primero. Con 10 conversaciones al día
la carrera es improbable — hasta el día que dos personas escriben al tiempo y un paciente llega
a una cita que no existe.

**Una `hosted` tool de búsqueda web** para lo que no está en la base de conocimiento. Muy barata
de agregar y cuesta el objetivo 1 entero. Descartada sin matices.

**`failure_error_function` por defecto.** Ahorra escribir nueve mapeos de error y cuesta que el
modelo improvise ante fallos.

**Todas las tools siempre visibles.** Ahorra el `is_enabled` y cuesta que el modelo tenga a mano
una herramienta de escritura en un turno donde no sabe con quién está hablando.

### Cuándo se reabre

Si aparece un segundo calendario, la clave `telefono + inicio` deja de ser única. Si el `UNIQUE`
empieza a rebotar seguido, el tope de 2 se quedó corto — y eso se configura desde la interfaz
web, no se cambia en código.

---

## Bloque 6 — Contexto, identidad y aprobaciones

### Qué se decidió

```
   ┌─ CONTEXTO LOCAL ────────────────────┐   ┌─ LO QUE VE EL MODELO ──────┐
   │  id_conversacion, id_paciente        │   │  nombre de pila            │
   │  telefono_completo                   │   │  "está identificado: sí"   │
   │  identidad_verificada (bool)         │   │  tratamiento, origen,      │
   │  intentos_identificacion             │   │    fecha del documento     │
   │  turno_actual                        │   │  las reglas del negocio    │
   │  tomada_por (doctor)                 │   │  resultados de tools       │
   │  capacidad_por_hora, duracion_cita,  │   │  historial de la charla    │
   │    tiempo_cierre_relevo              │   │                            │
   │  credenciales de los 4 sistemas      │   │                            │
   └─────────────────────────────────────┘   └────────────────────────────┘
```

`contexto_clinico` **no aparece en ninguna de las dos columnas**: la capa de ingesta lo manda a
Telegram y muere ahí, antes de que `Runner.run` arranque. Ni siquiera está en el contexto local,
porque una tool podría leerlo de ahí y ponerlo en un mensaje.

### Identidad

El canal trae un identificador débil: el número de WhatsApp. Si ese número ya está en Neon, la
identidad queda verificada sin fricción — y ese es el caso común. Si es nuevo o la persona
pregunta por un tercero, `identificar_paciente` pide nombre completo y el teléfono registrado,
con **máximo dos intentos**.

Dos y no tres, porque `trabajo.pasos` dice *«sin interrogatorio»* y la línea base dice que el
50% de las conversaciones ya se pierde: un tercer *«no me coincide, ¿me confirmas otra vez?»* es
una persona que se va.

**Descartado:** que el modelo «reconozca» a la persona por la conversación. No hay nada que
auditar el día que alguien reclame, y alguien que afirme ser otro con suficiente naturalidad
pasa. **Descartado también:** doble factor por SMS — `restricciones.legales` no lo exige hoy y
son dos o tres turnos más de fricción sobre una conversión que está en 2%.

### Ninguna aprobación que interrumpa

El SDK ofrece `needs_approval=True`, que **corta la corrida** hasta que alguien apruebe. Choca
de frente con `autonomia.criterio_limite`: *«una consulta escalada no detiene la conversación»*.
Con `needs_approval`, una paciente que manda su remisión a las 7 pm queda congelada hasta que un
doctor abra Telegram — exactamente el problema que Daniela vino a resolver.

| Acción que requiere autorización | Mecanismo |
|---|---|
| Agendar sobre un horario al tope | `escalar_a_doctores` + seguir ofreciendo alternativas |
| Conceder una excepción comercial | `escalar_a_doctores` + nunca prometerla antes |
| Criterio clínico individual | `escalar_a_doctores` + orientar hasta el límite seguro |
| Agendar sin poder verificar en Calendar | `escalar_a_doctores` + pedir cita manual |
| Decidir sobre un archivo clínico recibido | `escalar_a_doctores` con el contexto adjunto |
| **Describirle al paciente contenido clínico** | **Guardrail que niega. No se aprueba: no ocurre.** |

Las cinco primeras son cosas que un doctor puede autorizar. La sexta no es una decisión
pendiente de permiso.

### Cuándo se reabre

Si los doctores empiezan a responder consistentemente en menos de cinco minutos,
`needs_approval` deja de ser inviable y sería más seguro. Si la revisión legal exige doble
factor, se reabre el mecanismo de identidad completo.

---

## Bloque 7 — Persistencia

### Qué se decidió

`SQLAlchemySession` sobre la misma Neon Postgres. Historial recortado por cantidad. Cifrado del
almacén, **provisional**.

⚠️ Requiere `pip install 'openai-agents[sqlalchemy]'`. Sin ese extra, el import falla **en el
arranque**, no en una prueba.

### Por qué

| Pregunta | Respuesta | Consecuencia |
|---|---|---|
| ¿Más de un proceso atiende? | **Sí** | `SQLiteSession` fuera |
| ¿Sobrevive a un reinicio? | **Sí** | `ninguna` fuera |
| ¿El dato puede salir de la infraestructura propia? | **No** | `OpenAIConversationsSession` fuera |

Aunque sean 10 conversaciones al día, hay tres procesos tocando la misma conversación: el
webhook de WhatsApp, el scheduler de recordatorios y el listener de Telegram. Dos procesos
escribiendo el mismo archivo SQLite es corrupción o bloqueo **por diseño**, no «a veces».

### Las dos memorias — y por qué no son la misma

```
   ┌─────────────────────────┐     ┌──────────────────────────────┐
   │  SESIÓN (SDK)           │     │  ESTADO ESTRUCTURADO         │
   │  últimos ~40 turnos     │     │  estado_oportunidad          │
   │  texto crudo            │     │  barrera_detectada           │
   │  se recorta y se pierde │     │  citas, asistencias          │
   │                         │     │  seguimientos, notas         │
   │  "qué nos dijimos       │     │  "qué sé de esta persona"    │
   │   hace un rato"         │     │                              │
   └─────────────────────────┘     └──────────────────────────────┘
      se recorta por cantidad           no caduca nunca
```

La continuidad longitudinal —objetivo 4 del brief— no se logra guardando ocho meses de chat: se
logra con seis campos que no se degradan, porque son campos y no prosa.

**María en septiembre** deja `con_barrera / precio / cordales`. **María en diciembre** escribe
*«hola, sigues ahí?»* y Daniela retoma por donde quedaron, sin tener ni un mensaje de aquella
conversación en la sesión.

### Qué se descartó y qué costaba

**`EncryptedSession`.** Sería lo correcto si el requisito fuera que el historial no sea legible
desde el almacén. Se descarta por un detalle concreto: su `ttl` es de 600 segundos por defecto y
los ítems vencidos **se saltan en silencio** al leer el historial. Las conversaciones de MaxiCare
duran días, así que Daniela perdería contexto a mitad de una conversación legítima y nada lo
reportaría como error.

**`OpenAIResponsesCompactionSession`.** Pagar una llamada de modelo para producir un resumen en
prosa que puede perder el dato que importaba, cuando el estado estructurado ya tiene esos datos
como campos, es pagar dos veces por algo peor.

**`SQLiteSession`** (el default del manual): un archivo, cero infraestructura, y el scheduler
peleándose con el webhook. **`RedisSession`**: más rápido, un servicio más que vigilar, y un
Redis configurado como caché **pierde historial sin avisar**.

### ⚠️ Lo que la revisión legal tiene que cubrir

Neon es Postgres administrado: el dato **no vive en el VPS de DigitalOcean**, vive en la
infraestructura de Neon, probablemente fuera de Colombia. Eso es una transferencia internacional
de datos personales sensibles y la Ley 1581 tiene algo que decir.

No se propone cambiar Neon —está en `restricciones.stack_obligatorio`— pero la revisión legal
pendiente tiene que cubrir **tres** cosas, no una:

1. La validez del consentimiento por continuación de la conversación.
2. El envío de archivos clínicos a Telegram.
3. La ubicación de Neon.

---

## Bloque 8 — Guardrails

### Qué se decidió

Cinco guardrails: tres deterministas, dos con evaluador.

| # | Guardrail | Ámbito | Mecanismo | Ejecución |
|---|---|---|---|---|
| 1 | `identidad_antes_de_datos` | tool | determinista | bloqueante |
| 2 | `sin_cifra_no_documentada` | salida | determinista | bloqueante |
| 3 | `sin_hora_no_verificada` | salida | determinista | bloqueante |
| 4 | `sin_lectura_clinica` | salida | evaluador LLM | bloqueante |
| 5 | `uso_indebido` | entrada | evaluador LLM | paralelo |

### Lo que **no** es un guardrail

El cliente pidió que Daniela reconduzca **con sutileza** las preguntas ajenas a MaxiCare. Un
guardrail de entrada, cuando salta, lanza una excepción y produce un mensaje fijo de rechazo —
exactamente el bloqueo automático que se pidió evitar. Un guardrail no puede ser sutil: es un
interruptor.

La reconducción suave vive en el **prompt**, y el campo `fuera_de_alcance` de `RespuestaDaniela`
la **mide**. Lo que sí necesita guardrail es otra cosa: jailbreak, extracción del prompt, uso de
Daniela como asistente general. Eso es `uso_indebido`, y son dos problemas distintos.

### `sin_cifra_no_documentada` — el más valioso, y es gratis

```
   Daniela redacta: "...el rango va de $300.000 a $1.000.000..."
              │
              ▼
   extraer toda cifra de dinero → {300000, 1000000}
              │
              ▼
   ¿cada una salió de consultar_base_conocimiento en ESTE turno?
        sí → pasa          no → TRIPWIRE (la inventó el modelo)
```

Una regex y una comparación de conjuntos. Cero tokens, microsegundos, mismo veredicto siempre. Y
cubre el caso de endodoncia: como la tool devuelve «SIN DATO DOCUMENTADO» y ninguna cifra,
cualquier número en la respuesta salta.

### `sin_hora_no_verificada` — dos riesgos con una regla

Misma técnica sobre fechas y horas. Cierra a la vez ofrecer un horario que no existe
(`fallos.que_pasa_si_falla`: *«Daniela no inventa un horario bajo ninguna circunstancia»*) y
confirmar una cita que no se creó (`trabajo.pasos`: *«confirmarla únicamente cuando la operación
quedó realmente ejecutada»*).

### `sin_lectura_clinica` — el único que necesita otro modelo

La frase que justifica el evaluador: **«eso suena a un absceso» no se puede escribir como
regla.**

| Lo que escribe Daniela | ¿Está bien? |
|---|---|
| «Eso suena a una **infección**, tómate un ibuprofeno» | ❌ Le dice qué tiene |
| «En la valoración el doctor revisa si hay **infección**» | ✅ Explica el proceso |
| «Por lo que me cuentas, **te urge** que te vean» | ✅ No dice qué tiene |
| «Por lo que me cuentas, **tienes un absceso**» | ❌ Diagnóstico |

Las dos primeras comparten la misma palabra y solo una está mal. La diferencia está en el
significado, no en el vocabulario: ninguna lista de palabras prohibidas las separa.

El riesgo ya **no está en los archivos** —el muro del bloque 2 lo cubre— sino en los síntomas
que el paciente escribe en texto.

**Optimización de costo:** un prefiltro determinista decide si vale la pena llamar al evaluador,
y solo corre cuando el mensaje del paciente traía síntomas o hubo un adjunto. Una conversación
de precios y horarios no gasta ni un token. Y como `limites.latencia_maxima` da hasta un minuto
y *pide* que la respuesta tarde, el costo de latencia de un guardrail bloqueante es casi gratis:
es el único lugar del diseño donde el retardo obligatorio juega a favor.

### Qué ve el paciente cuando salta un tripwire

Regenerar **una** vez con la corrección explícita. Si vuelve a saltar: mensaje seguro en el
vocabulario del cliente (*«déjame confirmarte ese dato con los doctores para no decirte algo que
después no cuadre»*) más `escalar_a_doctores`. Un solo reintento: dos consumen turnos contra
`max_turns` y el segundo tripwire ya es señal de que el modelo no va a corregirse solo.

### Lo que se resolvió con un `if` en vez de un guardrail

El riesgo de que `LecturaArchivo.confianza` venga en `'baja'` y Daniela cotice el tratamiento
equivocado no necesita guardrail:

```python
if lectura.confianza != 'alta':
    lectura.tratamiento = 'no_identificado'
```

Sin nada que asumir, Daniela pregunta. Más barato, más claro, imposible de saltarse.

### Qué se descartó y qué costaba

**Un evaluador LLM para precios y horas** — es lo que se escribe por instinto. Cuesta una llamada
de modelo por turno, veredictos que cambian entre corridas, y **peor detección**: el evaluador
puede dejar pasar un número que suena razonable, mientras que la comparación de conjuntos no
sabe qué es razonable, solo sabe si el número salió de la base de conocimiento.

**Una lista determinista de términos clínicos prohibidos** — gratis y reproducible, y bloquearía
frases legítimas mientras deja pasar un diagnóstico dicho con palabras comunes.

---

## Bloque 9 — Fallos e idempotencia

### Qué se decidió

`max_turns = 15`, reintento único y solo donde es seguro, y una regla que atraviesa todo: **el
paciente nunca se queda sin respuesta.**

### `max_turns = 15` — de dónde sale el número

Un «turno» no es un mensaje del paciente: es **cada paso interno** que Daniela da antes de
contestar. El peor caso **legítimo** —no un error, una conversación normal que se complica— ya
consume 12:

```
   1 identificar · 2 consultar precio · 3 consultar disponibilidad
   4 "10am lleno" · 5 intentar 11am · 6 "se acaba de llenar"
   7 agendar 2pm · 8 registrar estado · 9 programar seguimiento
   10 redactar · 11-12 regenerar por guardrail
```

Con el default de 10, esa conversación **se corta en el turno 6** —justo cuando Daniela iba a
ofrecer las 2 pm— y el paciente no recibe nada.

**Descartado `None`:** quita el único límite que acota un loop desbocado, y el costo es
proporcional al tiempo que nadie esté mirando. **Descartado 20 o más:** si no resolvió en 15,
el problema no es el límite sino que se confundió, y lo que necesita es escalar.

### Idempotencia, con nombre y dueño

Todas las claves las construye el orquestador **antes** de llamar a la tool, y las guarda en
Neon. *«Muévela dos horas»* aplicado dos veces son cuatro horas; *«ponla a las 2 pm»* aplicado
dos veces son las 2 pm — por eso la clave de reprogramación usa el **valor destino**, nunca el
delta.

### La regla que atraviesa todo

Pase lo que pase —Calendar caído, modelo confundido, 15 turnos agotados, un fallo que nadie
previó— ocurren dos cosas: se avisa a los doctores por Telegram, y **sale un mensaje al
paciente**. Nunca un error técnico, nunca silencio, nunca una confirmación falsa.

Hoy el 50% de las conversaciones no recibe respuesta. Un agente que se cae en silencio reproduce
exactamente el problema que vino a resolver, pero con mejor tecnología.

### Los dos agujeros que se cerraron

**El doctor que se queda con la conversación.** El cliente decidió silencio total durante el
relevo y aceptó el costo. Se le puso un cierre automático:

```
   /tomar → Daniela en silencio
   2 h sin mensaje del doctor  → 🔔 recordatorio en Telegram
   3 h sin mensaje del doctor  → la conversación vuelve sola a Daniela
                                  (tiempo configurable desde la interfaz web)
```

El reloj cuenta desde el **último mensaje del doctor**, no desde el `/tomar`: un doctor que está
conversando activamente nunca pierde la conversación. Al retomar, Daniela responde los mensajes
represados y dispara los recordatorios cuya cita no haya pasado, **sin delatar al doctor**.

**El archivo que no se pudo bajar.** Los enlaces de WhatsApp caducan y no hay segunda
oportunidad. Daniela no puede fingir: pide que se lo reenvíen. Decir *«ya se la pasé a los
doctores»* cuando el archivo se perdió es una mentira que se descubre el día de la cita.

---

## Bloque 10 — Observabilidad y evals

### Las siete métricas — una por criterio de éxito, ni una más

| Criterio del brief | Métrica | Línea base |
|---|---|---|
| 7 de cada 10 quedan con cita sin doctor | Citas ÷ conversaciones con intención | **2 de 97 = 2%** |
| Bajan las inasistencias | Asistió ÷ citas que llegaron a su fecha | **No medida** — el primer mes ES la línea base |
| Los doctores dejan de contestar WhatsApp | Conversaciones sin escalar ÷ total | — |
| Ningún mensaje sin responder | Con respuesta ÷ total | **50% sin responder** |
| La primera respuesta deja de medirse en horas | p50 y p95 | **De minutos a un día** |
| Ningún archivo se pierde, ninguna lectura clínica | Entregados ÷ recibidos · tripwires | — |
| Nada fuera de alcance se responde | Conteo de `fuera_de_alcance` + muestreo | — |

**Percentiles, no promedios:** un promedio de 20 segundos esconde la conversación que tardó
cuatro minutos, y es justo esa la que el paciente recuerda.

Seis de las siete son consultas SQL sobre Neon, porque `RespuestaDaniela` las dejó como campos.
La de asistencia viene de la interfaz web.

### Dos cosas que no son métricas de operación

**Valor agendado** mide plata, no un criterio de éxito. Se conserva porque es del documento de
caso de éxito, con el nombre honesto: *valor del tratamiento consultado que asistió*, no
facturación — Daniela nunca sabe qué le hizo el doctor al paciente.

**Revisión manual de veracidad.** Alguien lee una muestra y verifica que cada dato afirmado
salió de la base de conocimiento. **No se puede hacer con SQL** y es el único control que
protege el primer objetivo del brief. Si nadie la hace, no existe.

### Tracing: la forma sí, el contenido no

```
   ┌── EL TRACE MUESTRA ───────┐   ┌── EL TRACE NO MUESTRA ─────┐
   │ qué tools, en qué orden   │   │ qué escribió el paciente    │
   │ cuánto tardó cada una     │   │ qué respondió Daniela       │
   │ qué guardrail saltó       │   │ el contexto clínico         │
   │ cuántos tokens            │   │ ningún texto de nadie       │
   └───────────────────────────┘   └─────────────────────────────┘
```

`group_id` es el UUID de la conversación, **no el teléfono**: meterlo en el trace sería sacar por
la puerta de atrás el dato que el contexto local metió adentro. Para leer qué se dijo se va a
Neon. Dos lugares a propósito: el trace se exporta fuera, Neon no.

Sin hooks al inicio: las métricas salen de Neon con un `SELECT`, y un hook corre dentro del
turno sumando a la latencia.

### La suite: 22 casos, partida en dos

5 de camino feliz · 5 de guardrail (cada uno **debe** disparar su tripwire) · 6 de ruta de fallo
· 5 de escalamiento (cada uno **debe** terminar en pendiente, nunca en el efecto) · 1 fuera de
alcance.

**Rápida** (~1 min, los 10 casi deterministas) en cada cambio. **Completa** (~10 min) antes de
cada despliegue. Una suite que tarda veinte minutos es una suite que nadie corre, y una suite
que nadie corre no existe.

### ⚠️ Dependencia externa

Hace falta un lote de **al menos 50 radiografías, fotos y remisiones reales y anonimizadas** de
MaxiCare. Sin ellas, el guardrail más delicado del sistema se desplegaría sin haberse probado
nunca contra material de verdad, y la primera prueba real sería una paciente.

Ese mismo lote decide si se puede bajar a la clase económica de modelo y ahorrar ese dinero.

---

## Bloque 11 — Fases

### Por qué el riesgo va primero

El manual recomienda una vertical delgada sobre el primer criterio de éxito, y **cambiar a «por
riesgo» cuando una integración pueda tumbar el proyecto entero**. Aquí hay una:
`sistemas.lista[WhatsApp].notas` dice que el proveedor exacto no se especificó y que los enlaces
de media caducan. Si bajar una foto antes de que el enlace muera no funciona, el lector de
archivos, el muro y el segundo agente **no tienen sentido**. Eso no se descubre en la semana ocho.

### Las diez fases

| # | Fase | Se comprueba con |
|---|---|---|
| 1 | Esqueleto, contratos y base de conocimiento | Los modelos rechazan lo inválido; `endodoncia` devuelve «SIN DATO DOCUMENTADO» |
| 2 | 🔴 **WhatsApp y el viaje del archivo** | Una radiografía real llega al Telegram de los doctores |
| 3 | Las nueve tools contra un doble | Tres `crear_cita` simultáneas → **exactamente dos citas** |
| 4 | Los dos agentes con sus guardrails | Los 5 guardrails disparan su tripwire |
| 5 | 🌐 Cascarón web y chat de pruebas | Alguien conversa con Daniela desde el navegador |
| 6 | Ingesta, el muro y el relevo | «incluidos» no aparece en la entrada de Daniela; `/tomar` la silencia; a las 3 h vuelve |
| 7 | Persistencia y observabilidad | Una conversación sobrevive al reinicio; el trace no tiene texto |
| 8 | 🌐 Pantallas de operación | La clínica cambia un precio y Daniela cotiza el nuevo **sin desplegar** |
| 9 | Evals y piloto real | Las 22 pasan; las métricas se calculan sobre conversaciones reales |
| 10 | Documento de caso de éxito | Existe el documento con la comparación contra 97 conversaciones y 2 citas |

### La fase 6 es la que prueba el diseño

```
   1. mandar una remisión
   2. capturar TODO lo que entra a Runner.run(daniela)
   3. buscar ahí dentro la palabra "incluidos"

      ¿aparece?    → ❌ el muro no existe
      ¿no aparece? → ✅ el muro es real
```

Dos bloques enteros de discusión se reducen a una prueba automática que corre en segundos y en
cada despliegue.

### La interfaz web, en dos tandas

Empezó como un chat de pruebas y hoy tiene cinco funciones. **Es un producto, no una pantalla.**
Se construye en dos apariciones y no en cuatro rebanadas, con el cascarón —login, navegación,
despliegue— hecho **una sola vez** para que no terminen con cuatro diseños y cuatro formas de
autenticar.

Durante la construcción, un script SQL reemplaza casi todo. La única excepción sin alternativa
es **marcar asistencia**, que no tiene otra fuente y sin la cual la métrica de asistencia no
existe. Eso deja claro para qué existe la interfaz web: **no es una herramienta de construcción,
es la autonomía de MaxiCare respecto de quien construye.**

### Por qué el caso de éxito sí es una fase

El manual dice que «documentación» no es una fase, y tiene razón cuando se trata de escribir al
final sobre lo que ya nadie recuerda. Esto es otra cosa: un entregable comercial con un hecho
comprobable detrás y una dependencia real que obliga a ponerlo último — no se puede escribir
hasta que el piloto haya producido datos.

```
   ANTES (ya existe, congelado en el brief)      DESPUÉS (fase 9)
   ────────────────────────────────────────     ────────────────
   97 conversaciones/mes                         ?
   50% sin responder                             ?
   primera respuesta: minutos → 1 día            ?
   2 citas agendadas (2%)                        ?
   todas personas nuevas                         ?
```

La columna izquierda es el activo más valioso del caso, y solo vale porque se capturó **antes**
de lanzar.

---

## Decisiones provisionales

Dos decisiones dependen de un `PENDIENTE` del brief y quedan marcadas como provisionales:

| Decisión | De qué depende | Qué la reabre |
|---|---|---|
| `persistencia.cifrado` = cifrado del almacén subyacente | `restricciones.legales` está en PENDIENTE: la revisión formal no ha ocurrido | Si la revisión concluye que el historial no puede ser legible desde el almacén, vuelve `EncryptedSession` con un TTL dimensionado a conversaciones de días |
| `LecturaArchivo.contexto_clinico` como excepción a la regla de no poner datos sensibles en un `output_type` | Ídem | Es el primer campo que hay que poner sobre la mesa en esa revisión |

Además, `datos.datos_sensibles` sigue en PENDIENTE por el **texto de la política de tratamiento
de datos**, que según el cliente aún no existe en su forma final. No bloquea la construcción,
pero sí el lanzamiento con pacientes reales: Daniela no puede pedir un dato sensible diciendo
que existe una política que nadie puede mostrar.

---

## Dependencias externas

| Qué hace falta | Para qué | Quién lo tiene |
|---|---|---|
| 50 radiografías, fotos y remisiones reales anonimizadas | La suite de evals del guardrail clínico, y decidir si se puede bajar la clase de modelo | MaxiCare |
| Texto final de la política de tratamiento de datos | Lanzar con pacientes reales | MaxiCare |
| Revisión legal (consentimiento por continuación · archivos a Telegram · ubicación de Neon) | Cerrar las dos decisiones provisionales | MaxiCare |

---

## Nota sobre cómo se aprobó este plan

Los once bloques se presentaron uno a la vez y **el cliente retó varios**, que es la señal de
que el formato funcionó:

- **Bloque 2** — corrigió la premisa entera: interpretar para el doctor no es lo mismo que
  describirle al paciente. La frontera se reubicó sobre el destinatario y quedó mejor
  justificada.
- **Bloque 9** — cambió la política del relevo: cierre automático a las 3 horas, y después pidió
  que ese tiempo fuera configurable.
- **Bloque 11** — retó que la interfaz web fuera una fase monolítica; se partió en dos tandas
  pegadas a lo que cada una habilita.
- **Bloques 8 y 9** — pidió que se explicaran en términos más simples antes de aprobarlos.

Un plan que nadie reta no demuestra ser bueno: demuestra que las alternativas no se presentaron
con lo que de verdad costaban. Este sí se retó.

# Recordatorios de citas · diseño

Fecha: 2026-09-14 · Estado: aprobado en conversación, pendiente de plan de implementación

## El problema

La tool `programar_seguimiento` escribe en `seguimientos` desde la fase 3. La migración 001
llama a esa tabla «la cola que lee otro proceso» y el plan lo repite en
`herramientas[7].descripcion`: «para que otro proceso lo ejecute en su momento».

Ese proceso no existe. `enviado_en` no se escribe en ninguna línea del repositorio. Todo lo
que Daniela haya programado desde que arrancó producción sigue ahí, con `enviado_en` en NULL,
sin que nadie lo mire.

Esta spec construye ese proceso, y arregla dos cosas que impiden que funcione aunque exista.

## Los dos huecos que hay que cerrar primero

**1. `seguimientos` no sabe de qué cita habla.** La tabla cuelga de `conversacion_id`, no de
`cita_id` (001:148). Si el paciente reprograma o cancela, el seguimiento no se entera: el
recordatorio sale igual y le recuerda al paciente una cita que ya no existe. Es el fallo más
grave del estado actual, y no se puede tapar desde el despachador — hace falta la columna.

**2. El recordatorio depende de que el modelo se acuerde.** `programar_seguimiento` es una
tool, y ni el prompt de `agentes.py` ni `crear_cita` la mencionan. En la práctica la cola está
casi vacía. Es el no-negociable 2 del proyecto aplicado a otro caso: lo que tiene que ocurrir
siempre no lo decide el modelo.

## Alcance

Dentro:

- Tres recordatorios, todos atados a una cita real.
- El despachador, colgado del barrido que ya corre en `runtime.py`.
- La programación automática desde `crear_cita`, `reprogramar_cita` y `cancelar_cita`.
- La cascada de anulación cuando la cita cambia.
- `WhatsApp.enviar_plantilla`.
- El parte diario de citas a los doctores, por WhatsApp.

Fuera, por decisión explícita:

- **La reactivación comercial** (quien preguntó precios y nunca agendó). Es otro subsistema,
  con otro riesgo: convierte un recordatorio en publicidad. Si se quiere, va en su propia spec.
- **El parte diario por Telegram.** Se propuso como camino sin plantilla y el cliente decidió
  esperar a WhatsApp. No se vuelve a proponer.
- **El seguimiento post-tratamiento** («¿cómo va?»). El sistema no sabe qué le hicieron al
  paciente: `asistio` es lo único que cruza esa puerta, y es un booleano.

## 1. Los tres recordatorios

| Tipo | Se programa cuando… | Sale |
|---|---|---|
| `recordatorio_cita` | se crea o reprograma una cita | ver la tabla de la sección 2 |
| `reactivacion_inasistencia` | alguien marca `asistio = false` | 24 h después de la cita perdida |
| `reactivacion_cancelacion` | el paciente cancela | 7 días después, si no reagendó |

`reactivacion_inasistencia` tiene una **dependencia dura que hoy no está construida**: nada en
`src/` escribe la columna `asistio`. La pantalla de Agenda existe en el frontend con datos de
maqueta (`web/src/pantallas/Agenda.tsx:50`) y no hay endpoint que la persista. Mientras eso no
exista, este recordatorio no se dispara nunca. No es un fallo del despachador: es que su
disparador no existe. Se construye igual, y se prueba con la columna escrita a mano por SQL.

## 2. Cuándo sale el recordatorio de cita

Depende de con cuánta antelación se agendó:

| Antelación al agendar | Recordatorio | Por qué |
|---|---|---|
| menos de 4 h | **ninguno** | acaba de hablar con Daniela; sería ruido |
| 4 h a 24 h | 2 h antes de la cita | da tiempo de salir de casa |
| más de 24 h | **la víspera a las 6:00 p. m.** | hora fija, no «24 h antes» |

La víspera a hora fija y no a 24 horas exactas es una decisión, no un detalle de
implementación. Con 24 horas exactas, la cita de las 7:00 a. m. dispara su recordatorio a las
7:00 a. m. del día anterior, hora a la que mucha gente no mira el teléfono, y la de las 4:00
p. m. lo dispara a las 4:00 p. m. Con hora fija salen todos juntos, a una hora en que la gente
está disponible, y quien quiera cambiar la cita todavía alcanza a avisar esa noche para que la
clínica libere el cupo al día siguiente.

La hora vive en `configuracion` como `hora_recordatorio_vispera`, no como constante en el
código: es una perilla de la clínica, como las tres de la fase 8.

**El choque entre «2 h antes» y la jornada.** Una cita a las 8:00 a. m. agendada la víspera a
las 10:00 a. m. tiene 22 h de antelación, cae en la banda de las 2 h, y su recordatorio sale a
las 6:00 a. m. — con la clínica cerrada. G5 lo aplazaría a la apertura, que son las 8:00 a. m.:
la hora de la cita. Un recordatorio que llega cuando el paciente ya debería estar en el sillón
no sirve para nada.

La regla: si el recordatorio de 2 h cae antes de la apertura, se adelanta a la **víspera a la
hora de cierre** en vez de retrasarse a la apertura. El paciente lo recibe la noche anterior, que
es tarde para esa banda pero útil, en lugar de recibirlo tarde y ya inútil. Es la única excepción
en la que un recordatorio se mueve hacia atrás en el tiempo, y existe porque las dos primeras
horas de la jornada son las únicas en que «2 h antes» cae fuera del horario.

Alternativa descartada: recordatorio doble (víspera **y** 2 h antes) para todas las citas.
Dobla el costo por cita en plantillas de Meta y dobla la probabilidad de que el paciente
perciba el número como insistente. Si la métrica de inasistencia no baja lo suficiente con uno,
se añade el segundo con un cambio de una fila en `configuracion`.

## 3. Quién programa

El código, dentro de la misma transacción de la tool que ya escribe la cita:

```
crear_cita()  ──┬─→ tomar_cupo
                ├─→ Google Calendar
                ├─→ INSERT en citas
                └─→ INSERT en seguimientos     ← nuevo
```

| Tool / evento | Efecto sobre la cola |
|---|---|
| `crear_cita` | programa el recordatorio de esa cita |
| `reprogramar_cita` | anula el de la cita vieja, programa el de la hora nueva |
| `cancelar_cita` | anula el recordatorio, programa `reactivacion_cancelacion` a 7 días |
| marcar `asistio = false` | programa `reactivacion_inasistencia` a 24 h |

`programar_seguimiento` **se conserva** y no cambia de firma: sigue siendo la vía para lo que
sí es criterio del modelo — «llámenme el lunes que lo pienso». Lo que deja de depender del
modelo es el recordatorio de una cita que ya existe.

La clave de idempotencia la sigue armando `ctx.clave(...)`, como las otras cuatro. No hay
excepción al no-negociable 2 aquí.

## 4. El despachador y sus siete guardas

Vive en un módulo nuevo, `src/maxicare_daniela/seguimientos.py`, y se cuelga del barrido que ya
corre (`runtime.py:1765-1817`, `SEGUNDOS_ENTRE_BARRIDOS = 60.0`). No se crea un proceso aparte:
un segundo proceso es un segundo despliegue, un segundo sitio donde mirar logs y una segunda
cosa que puede llevar días caída sin que nadie lo note — que es exactamente lo que pasó con el
barrido de relevos y quedó anotado en `runtime.py:1081`.

```
Cada 60 s:

  SELECT ... FROM seguimientos
   WHERE enviado_en IS NULL AND anulado_en IS NULL AND fecha_objetivo <= now()
   FOR UPDATE SKIP LOCKED

  Por cada fila:

  G1  ¿La cita sigue viva y a la misma hora?      no → anular ('cita_cambio')
  G2  ¿La cita ya pasó?                           sí → anular ('cita_pasada')
  G3  ¿Llega con más de 2 h de retraso?           sí → anular ('llego_tarde')
      ¿Quedan menos de 75 min para la cita?       sí → anular ('cita_inminente')
  G4  ¿conversaciones.tomada_por está puesto?     sí → aplazar 30 min
  G5  ¿Estamos dentro de la jornada de envío?     no → aplazar a la apertura
  G6  ¿El paciente escribió hace menos de 1 h?    sí → anular ('contacto_reciente')
  G7  ¿Ya salió algo a ese número?                sí → aplazar a la próxima apertura

  UPDATE enviado_en = now()  +  COMMIT     ← PRIMERO
  enviar                                   ← DESPUÉS
  si falla → UPDATE fallo = ...            ← se anota, no se desmarca
```

**El orden importa y va en contra del instinto.** Marcar y enviar no pueden ir en la misma
transacción: no hay transacción que cubra una llamada HTTP a Meta. Si se envía primero y el
proceso muere antes del COMMIT, la fila sigue pendiente y el siguiente barrido —sesenta segundos
después— manda el mismo recordatorio otra vez, y ninguna de las siete guardas lo detecta, porque
todas siguen diciendo que sí.

Así que se marca primero, se hace COMMIT, y después se envía. Si el envío falla, se reintenta
**dentro del mismo ciclo** (3 intentos con espera corta) y, si sigue fallando, se anota en
`fallo` y se escala a los doctores por Telegram. La fila no vuelve a la cola.

El precio de este orden es que un fallo de red puede costar un recordatorio. El precio del orden
contrario es mandar el mismo mensaje varias veces a un paciente. El principio del proyecto ya
resolvió este empate en otro sitio: **antes marcar y no mandar, que mandar y no marcar.**

Por qué cada una:

- **G1** es la que exige `cita_id`. Sin ella todo lo demás es decorativo.
- **G2 y G3**: un recordatorio que llega después de la cita no es tarde, es dañino — le dice al
  paciente que el sistema no sabe lo que pasó. Si el proceso estuvo caído toda la noche, lo
  correcto es callarse.

  G3 mide **dos veces**, y la segunda no estaba en la primera versión de esta sección.
  `aplazar_seguimiento` reescribe `fecha_objetivo`, así que la cuenta contra ella se pone a cero
  en cada aplazamiento: una fila de víspera que a las 18:00 pilla al doctor en relevo encadena
  G4 → G5 → la mañana siguiente y llega **fresca** según esa cuenta, a una hora de la cita. La
  garantía que la sección 11 le atribuye a G3 solo valía para la caída dura. La segunda medida va
  contra la hora de la **cita**, que es lo único de la fila que ningún aplazamiento puede tocar.

  El umbral son **75 minutos y no las 2 h de la banda corta**, y el margen no es holgura: la
  banda corta programa el recordatorio exactamente a 2 h de la cita y el ciclo recoge la fila
  siempre unos segundos después de su `fecha_objetivo`, así que medir contra las 2 h redondas
  anularía esa banda entera todos los días. El margen cubre además el aplazamiento de 30 min de
  G4 — un recordatorio a hora y media de la cita todavía sirve para salir de casa, y mandarlo
  ayuda al paciente a llegar mientras que anularlo no ayuda a nadie.
- **G4** es la misma regla que ya aplica al programar (`herramientas.py:1211`): mientras un
  doctor tiene el relevo, el sistema no se le atraviesa. **Aplaza, no anula** — el doctor puede
  devolver la conversación en diez minutos y el recordatorio sigue siendo válido.
- **G5** lee `hora_apertura` / `hora_cierre` / `hora_cierre_sabado` / `atiende_domingo` de
  `configuracion` (migración 012). No se inventa una constante nueva: la jornada ya está
  modelada, y duplicarla deja dos horarios que se contradicen, que es el problema que la propia
  012 documenta entre la rejilla y la base de conocimiento.
- **G6**: si el paciente está conversando con Daniela ahora mismo, recordarle la cita que acaba
  de agendar la hace ver desmemoriada.
- **G7**: un número recibe **un** recordatorio por ventana de envío.

  **No agrupa, y esa es una renuncia consciente.** La primera versión de esta sección decía «un
  mensaje con las dos», y eso exige una plantilla con sitio para dos citas: la que Meta aprueba
  tiene cuatro huecos y sitio para una. Otra plantilla es otra spec, no un detalle de
  implementación de esta.

  De las dos salidas posibles, anular la segunda fila deja a un paciente sin recordatorio de una
  cita real, que es clínicamente lo peor. Así que la segunda fila **se aplaza a la próxima
  apertura de la ventana de envío**: el paciente recibe hoy el recordatorio de la cita más
  próxima y el segundo le llega a la mañana siguiente, que para una segunda cita de esa misma
  semana sigue llegando a tiempo. Donde no llegue, G2 o G3 lo anulan y la fila registra el
  motivo — la limitación queda visible en los datos y no escondida en un silencio.

Los seguimientos anulados **no se borran**, se marcan con `anulado_en` y `motivo_anulacion`.
Borrarlos deja al sistema sin poder responder «¿por qué este paciente no recibió recordatorio?»,
que es la primera pregunta que hace la clínica cuando alguien no llega.

## 5. La cascada

```
Lunes  · agenda para el jueves 9:00 am
         └─ seguimiento #1 → miércoles 6:00 pm

Martes · reprograma al viernes 2:00 pm
         ├─ #1 → anulado ('cita_reprogramada')
         └─ #2 → jueves 6:00 pm

Jueves · cancela
         ├─ #2 → anulado ('cita_cancelada')
         └─ #3 → reactivación, jueves + 7 días

Viernes· vuelve y agenda otra cita
         └─ #3 → anulado ('reagendo')
```

La anulación va en la **misma transacción** que el cambio de la cita. Si se hiciera después, una
caída entre las dos escrituras deja un recordatorio vivo apuntando a una cita muerta — y el
despachador lo mandaría, porque G1 solo compara contra lo que hay en la base.

## 6. El mensaje

Texto fijo con variables, aprobado por MaxiCare y por Meta. **Nunca generado por el modelo.**
Con plantilla esto deja de ser una elección de diseño: Meta aprueba el texto palabra por
palabra, y un texto que cambia es un texto que no está aprobado.

```
Hola {{1}}, le recordamos su cita en MaxiCare
el {{2}} a las {{3}} para {{4}}.

[ Confirmar ]   [ Necesito cambiarla ]
```

**«mañana» no puede ir en este texto, y la primera redacción de esta sección lo llevaba.** La
sección 2 decide que la víspera retrocede al día hábil anterior cuando la víspera está cerrada:
la víspera de un lunes es siempre domingo, así que **toda cita de lunes agendada con más de 24 h
de antelación recibe su recordatorio el sábado**. No es un caso raro, es un quinto de la semana,
y el paciente leería «mañana» sobre una cita que es pasado mañana. El segundo camino es por
composición de guardas: una fila de víspera que a las 18:00 pilla al doctor en relevo encadena
G4 → G5 → la mañana siguiente, y llega una hora antes de la cita diciendo «mañana».

Las dos secciones se escribieron en tareas distintas y ninguna revisión de tarea podía ver que
se contradecían. El texto no lleva ninguna palabra relativa al día: `{{2}}` dice la fecha
completa («jueves 17/9») y se lee igual de bien salga cuando salga. `{{3}}` es SOLO la hora
(«09:00»): son dos huecos con dos datos distintos, no la misma cadena dos veces.

**Y la hora va en el huso de la clínica.** `citas.inicio` es `TIMESTAMPTZ` y psycopg la devuelve
normalizada a UTC: el valor que se mete en `{{3}}` se convierte a Bogotá antes de formatearlo, o
el paciente lee «14:00» sobre una cita de las 9:00.

Los dos botones son quick replies de la plantilla, y hacen algo que un texto no puede: al
tocarlos el paciente **emite un mensaje**, lo que abre la ventana de 24 h y deja a Daniela
conversando con normalidad, sin plantilla y sin costo adicional. El recordatorio deja de ser un
aviso y pasa a ser una puerta.

PENDIENTE, y no se rellena con un valor plausible:

- El nombre de la plantilla en el Business Manager.
- **El código de idioma con el que quede registrada la traducción**
  (`MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA`). Meta no busca la traducción más parecida: si el
  código no coincide al carácter rechaza el envío entero con el error 132001, y una plantilla
  creada como `es_CO` o `es_MX` **no acepta `es`**. El default del código es `es` porque es lo
  probable, y por eso mismo no es una comprobación: un idioma equivocado no falla un envío,
  falla el 100 % de ellos.
- La categoría con la que Meta la apruebe (`utility` es lo que corresponde; **confirmar**, la
  categoría decide el precio por mensaje).
- El texto definitivo, que lo aprueba MaxiCare.
- Que Meta acepte los dos quick replies en esa categoría.

## 7. Cuando el paciente responde

El recordatorio lo mandó un proceso, no una conversación: el historial del agente no lo
contiene. Si el paciente responde «sí, confirmo», Daniela no sabe a qué dice que sí.

El despachador anota en `conversaciones` qué recordatorio salió y cuándo, y
`atencion._leer_estado` lo carga en `ContextoDaniela` como un campo más que sale de la base —
igual que `telefono_sin_paciente`. **No se inyecta un mensaje en `agent_messages`**: esas tablas
las fija el SDK y escribir en ellas a mano va contra el no-negociable 10.

El paciente que responde entra por el webhook normal. No hace falta ninguna ruta nueva.

## 8. Cuando el envío falla

La fila ya está marcada (sección 4), así que el fallo **no** la devuelve a la cola. Se reintenta
tres veces dentro del mismo ciclo, con espera corta entre una y otra, subiendo `intentos`. Si las
tres fallan, se escribe el motivo en `fallo` y se escala a los doctores por Telegram, por la ruta
de escalamiento que ya existe — la clínica se entera de que a ese paciente no le llegó nada y
puede llamarlo.

`fallo` con contenido y `enviado_en` puesto es la señal a vigilar, igual que `reenviado_en` NULL
lo es en `mensajes_entrantes` (no-negociable 14): la fila dice «lo intenté» y no «salió».

El principio: **antes marcar y no mandar, que mandar y no marcar.** Un recordatorio perdido es
un paciente que quizá no llega, y la clínica se entera; el mismo recordatorio tres veces es un
paciente que bloquea el número de la clínica, y de eso no se entera nadie.

## 9. El parte diario a los doctores

Un mensaje al WhatsApp de cada doctor con las citas del día siguiente, a la misma hora que
salen los recordatorios de víspera.

Depende de dos cosas que **no existen hoy** y que hay que construir antes:

1. Una plantilla propia, distinta a la del paciente. La ventana de 24 h se cuenta por
   destinatario: para el número de la clínica, el WhatsApp personal de un doctor es un usuario
   cualquiera.
2. Los números de los doctores en configuración. Hoy solo está
   `MAXICARE_TELEGRAM_CHAT_DOCTORES` (`config.py:400`); no hay ningún teléfono de doctor en
   ninguna parte del proyecto.

Hasta que existan las dos, este componente no se construye.

## 10. Migración 017

Idempotente, como las dieciséis anteriores:

```sql
ALTER TABLE seguimientos
    ADD COLUMN IF NOT EXISTS cita_id          UUID REFERENCES citas(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS anulado_en       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS motivo_anulacion TEXT,
    ADD COLUMN IF NOT EXISTS intentos         SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS fallo            TEXT;

CREATE INDEX IF NOT EXISTS ix_seguimientos_por_despachar ON seguimientos (fecha_objetivo)
    WHERE enviado_en IS NULL AND anulado_en IS NULL;

INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('hora_recordatorio_vispera',   '18',
     'Hora a la que salen los recordatorios de las citas del día siguiente. Entero, hora de Bogotá.'),
    ('horas_minimas_para_recordar', '4',
     'Antelación mínima para que una cita reciba recordatorio. Por debajo, el paciente acaba de hablar con Daniela.')
ON CONFLICT (clave) DO NOTHING;
```

`cita_id` es **nullable**: `programar_seguimiento` sigue pudiendo encolar algo que no cuelga de
ninguna cita («llámenme el lunes»). Un seguimiento con `cita_id` NULL se salta G1, G2 y G3.

El índice parcial reemplaza a `ix_seguimientos_pendientes` de la 001, que no conoce `anulado_en`.

Las dos filas de `configuracion` son enteros porque `leer_configuracion` hace `int(valor)` sobre
todo lo que encuentra — la misma razón por la que `atiende_domingo` es 0/1 y no un booleano
(migración 012).

## 11. Errores y fallos

| Qué falla | Qué pasa |
|---|---|
| Meta rechaza el envío | 3 intentos en el mismo ciclo; si siguen fallando, `fallo` y escala por Telegram |
| La base no responde | la transacción no se cierra, la fila queda pendiente, se retoma al siguiente barrido |
| Dos instancias del despachador | `FOR UPDATE SKIP LOCKED`: cada fila la toma una sola |
| El proceso muere después de marcar | ese recordatorio se pierde, y es la pérdida elegida (sección 4) |
| El barrido lleva horas caído | G3 anula todo lo vencido; no hay avalancha de mensajes viejos |

El último es el que más importa y el que motiva G3. Sin ella, arrancar el proceso tras un fin de
semana caído manda de golpe todos los recordatorios atrasados.

## 12. Pruebas

| Prueba | Dónde | ¿Gasta? |
|---|---|---|
| Las siete guardas, una por una, con reloj fijado | `tests/test_seguimientos.py` | no |
| La cascada completa del ejemplo de la sección 5 | `tests/test_seguimientos_neon.py` (`-m neon`) | no |
| Dos despachadores contra la misma fila | `tests/test_seguimientos_neon.py` | no |
| El despachador de punta a punta contra Neon | `scripts/probar_recordatorios.py` | no |
| Envío real de la plantilla a un número de prueba | `scripts/probar_recordatorios.py --enviar` | **sí** |

`scripts/probar_recordatorios.py` entra en la tabla de entregables del CLAUDE.md y en la lista
de los cinco que hay que correr al cambiar una firma que un script dobla.

El reloj se fija por `ctx.ahora`, como ya hace `_programar_seguimiento` (`herramientas.py:1201`):
ninguna prueba depende del reloj de la máquina.

La prueba de concurrencia exige la conexión **directa** a Neon, sin el `-pooler.` del host: con
el pooler no hay dos sesiones de verdad y `SKIP LOCKED` no se ejercita.

## 13. Orden de construcción

1. Migración 017 y `scripts/inicializar_base.py` al día.
2. `seguimientos.py` con las siete guardas, contra dobles. Sin enviar nada.
3. La programación automática en `crear_cita` / `reprogramar_cita` / `cancelar_cita` y la
   cascada.
4. `WhatsApp.enviar_plantilla`.
5. Colgar el despachador del barrido de `runtime.py`.
6. El campo de respuesta en `ContextoDaniela`.
7. El parte a los doctores, cuando existan plantilla y números.

Del 1 al 3 y el 6 no dependen de Meta. El 5 se puede colgar con la rama de envío apagada y
comprobar en producción que el despachador decide bien antes de que mande un solo mensaje.

## Lo que esta spec NO resuelve

- La línea base de inasistencia sigue sin medirse. El brief dice que el primer mes de operación
  es la línea base (`exito.criterios_exito`). Sin ella, «las inasistencias bajaron» no se puede
  afirmar, solo suponer.
- `asistio` no lo escribe nadie (sección 1). Hasta que la fase 8 lo cierre, una de las tres
  reactivaciones está construida pero muda.
- Cuántas citas se agendan con más de 48 h de antelación no se sabe. Es el dato que dice cuánto
  vale la plantilla, y sale de una consulta sobre `citas` cuando haya volumen.

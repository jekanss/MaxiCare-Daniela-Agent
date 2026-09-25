# Los tres botones del recordatorio de cita

**Fecha:** 25/09/2026
**Estado:** aprobado por MaxiCare, pendiente de dos trámites con Meta.

## El problema

El recordatorio de víspera sale desde el 20/09/2026 con dos quick replies que Meta ya
aprobó — «Confirmar» y «Necesito cambiarla» — y **ninguno de los dos hace nada**. El rótulo
entra al turno como texto entre corchetes (`atencion._entrada_para_el_modelo`), Daniela lee
«El paciente pulsó el botón «Confirmar»» y contesta lo que le parece. Después de eso:

- no queda **ni una fila** que diga que ese paciente confirmó — `citas.estado` arranca en
  `'confirmada'` por defecto al crearse, así que esa palabra no significa que el paciente
  dijera nada;
- **el doctor no se entera de nada**: ni de quién confirmó, ni de quién no va a venir, ni de
  quién movió su hora. De las dos últimas se entera cuando el paciente no llega, o cuando
  abre el calendario y ve otra cosa de la que recordaba.

Falta además el botón que el paciente necesita de verdad. «Necesito cambiarla» sirve a quien
puede otro día; a quien no puede ir y ya está, hoy le toca escribirlo a mano.

## Lo que se construye

Tres botones en el recordatorio, tres caminos distintos, y un aviso por WhatsApp a los
doctores que es el mismo para los tres.

| Botón | Quién lo ejecuta | Qué pasa |
|---|---|---|
| `Confirmar` | el CÓDIGO | marca la cita, avisa al doctor, contesta una frase fija, no abre turno |
| `No puedo asistir` | Daniela | pregunta si cancela; solo un sí llama a `cancelar_cita` |
| `Necesito cambiarla` | Daniela | acompaña la reprogramación entera, como hoy |

## Bloqueantes externos: los dos trámites de Meta

Nada de esto funciona hasta que Meta apruebe dos cosas, y las dos las pide MaxiCare desde el
Business Manager. **El código se despliega antes y no manda nada**, que es el mismo patrón
con el que se desplegaron `plantilla_recordatorio` y `plantilla_cita_nueva`.

**1. Editar `recordatorio_cita`** para que lleve tres quick replies, con estos rótulos
exactos y con tildes:

```
Confirmar   ·   Necesito cambiarla   ·   No puedo asistir
```

Meta admite hasta tres quick replies, así que el techo no estorba. Lo que estorba es el
ritmo: una plantilla aprobada se puede editar aproximadamente una vez cada 24 h, y la edición
vuelve a pasar revisión. Los cuatro huecos del cuerpo no se tocan.

**El código compara contra el literal.** Un rótulo aprobado con otra grafía —«No puedo
asistir.» con punto, «Confirmar cita»— deja el botón sin camino y el mensaje se atiende como
texto libre. No falla nada y no lo ve nadie: por eso los rótulos viven en constantes de un
solo sitio y `scripts/probar_plantilla.py --estado` los imprime para que una persona los
compare con lo que ve en el teléfono.

**2. Crear una plantilla nueva** para el aviso a los doctores, categoría Utility, cinco
huecos y sin botones:

```
{{1}} asunto       -> "Confirmó su cita" | "No podrá asistir" | "Cambió su cita"
{{2}} nombre
{{3}} teléfono
{{4}} tratamiento
{{5}} cuándo       -> "jueves 25 de septiembre a las 10:00 a. m."
                      o "antes: jueves 25, 10:00 a. m. · ahora: viernes 26, 3:00 p. m."
```

Ningún hueco puede llevar saltos de línea: Meta rechaza el mensaje entero, no el renglón.

Se registra como `movimiento_agenda`, en **Spanish a secas** (código `es`, nunca `es_CO` ni
`es_MX`: `canales.enviar_plantilla` llama con `es` y Meta no busca la traducción más
parecida — devuelve 132001 y no manda nada), y su nombre viaja en
`MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA`, con `MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA_IDIOMA`
por si algún día hace falta otra.

**Las cinco variables van en el `body`, y el header no puede llevar ninguna.**
`canales.enviar_plantilla` solo manda ese componente; una variable en el header espera un
componente que nadie manda y Meta responde 132000. El header, si lo hay, es texto fijo.
Tampoco lleva botones: el doctor no le responde a este mensaje.

### Por qué UNA plantilla y no tres

Tres plantillas se leen mejor —el doctor sabe qué pasó por el encabezado, sin leer el
cuerpo— y cuestan tres aprobaciones más la del botón, con cada retoque de redacción
volviendo a pasar por revisión. Con una sola, el asunto y el cuándo son datos, así que la
redacción vive en Python: el día que MaxiCare quiera que diga otra cosa es un commit, no un
trámite. Es la misma razón por la que `aviso_citas.parametros_de` separa la fecha y la hora
en dos huecos en vez de mandarlas juntas.

Lo que se paga: los tres avisos comparten encabezado, y el doctor tiene que leer la primera
línea para saber de cuál se trata.

## Decisión 1 — «Confirmar» lo ejecuta el código, no el modelo

El registro y el aviso **no pueden depender de que el modelo decida llamar a una tool.** El
proyecto ya pagó esta lección entera: el no negociable 14c existe porque se dio por hecho que
Daniela escalaría sola al recibir una radiografía, y de los ocho archivos que el sistema
recibió en su vida **ninguno lo hizo**. Cuando algo tiene que pasar siempre, en este código
lo decide el código.

**Dónde va el corte:** en `runtime._entregar`, en la misma lista donde el proyecto ya decide
si un mensaje se atiende — el dedupe de Meta, las reacciones, `/clearstate`, la cuota. Va en
el **último** lugar de esa lista, justo antes de `atencion.atender`:

- **después del dedupe de Meta**, porque confirmar dos veces es inocuo pero avisar al doctor
  tres veces no lo es, y Meta reintenta los webhooks;
- **después de la cuota**, porque cada confirmación cuesta tres WhatsApps de plantilla y el
  perímetro tiene que seguir siendo un techo real. Quien ya disparó la cuota está haciendo
  algo que no es confirmar una cita.

`_entregar` corre **por mensaje**, antes del búfer que agrupa lo que el paciente escribe en
20 segundos, así que en ese punto no se sabe si va a escribir algo más. No estorba: si pulsa
el botón *y* escribe, son dos mensajes con dos caminos — el botón se registra y se cierra
solo, el texto abre su turno y Daniela contesta lo que haya escrito. La marca ya está en la
base cuando ese turno corre.

**A qué cita se refiere.** El botón no dice de qué cita habla: solo trae su rótulo. Se busca
el último `seguimientos` de tipo `recordatorio_cita` con `enviado_en IS NOT NULL` y
`cita_id IS NOT NULL` cuya cita sea de ese teléfono, ordenando por `enviado_en DESC`. La cita
tiene que seguir viva (`estado IN ('confirmada','reprogramada')`) y estar en el futuro.

No se filtra por `fallo IS NULL` a propósito, aunque la fila se marca ANTES de enviar (no
negociable 21) y puede quedar con las dos columnas puestas. Si el envío falló de verdad, el
paciente no recibió el botón y no puede haber pulsado nada; y si falló el primer intento y
salió el reintento, la fila conserva el `fallo` viejo. Filtrar por él dejaría sin camino justo
al paciente cuyo recordatorio costó dos intentos. No hay ventana de tiempo sobre el envío, y no hace falta: un botón pulsado tres
días tarde apunta a una cita que ya pasó, y esa condición lo descarta sola.

**Si no hay cita que confirmar, no se inventa nada:** el mensaje sigue su camino y Daniela lo
atiende como cualquier otro. Es el lado barato de equivocarse.

**Qué se le contesta.** Una frase fija con la fecha y la hora dentro, por
`canales.enviar_texto` — el mismo borde por el que pasan todos los caminos que le escriben a
un paciente, y que desde el 24/09/2026 se niega a mandar un texto en blanco. Como no hay
turno, no hay modelo, y por tanto tampoco `sin_hora_no_verificada`: esa hora sale de la base,
en esta misma llamada.

**Se anota `respondido_en`.** Con `persistencia.marcar_respondido(wamid, wamid_respuesta=...)`.
Sin esto el mensaje queda con `respondido_en` NULL y `fallo_respuesta` NULL, que es la firma
de «entró y nadie lo procesó» (no negociable 32): el panel pintaría al paciente como
desatendido y `contar_sin_responder` de `/salud` lo contaría, por haber confirmado su cita.

**Y si el acuse NO sale, se anota `fallo_respuesta`.** Mismo patrón que
`atencion._anotar_resultado`. Sin él, un Meta caído dejaba la fila con las dos columnas en
NULL sobre un mensaje que sí se procesó —la cita confirmada, el doctor avisado— y el barrido
del siguiente arranque le abría un turno del modelo a un botón cuya cita ya estaba
confirmada.

### Durante un relevo, la cita se confirma y Daniela no habla

`conversaciones.tomada_por` puesto significa **Daniela callada** (no negociable 15), y esa
comprobación vive dentro de `atencion.atender` — por donde este camino no pasa. Un paciente
que pulsa «Confirmar» mientras un doctor le está escribiendo por el hilo recibiría un
«¡Gracias por confirmar!» automático en medio de una conversación humana, que es exactamente
lo que el relevo existe para impedir.

Así que con relevo activo: **la cita se marca y el doctor recibe su aviso** —eso es lo que él
quiere saber, y perder una confirmación porque alguien está hablando sería peor— y **no se le
contesta al paciente**, dejando un `fallo_respuesta` que empieza por `relevo:` sin ser un
fallo, que es la convención que el turno normal ya usa para este caso.

El relevo se consulta **después** de marcar la cita: un doctor hablando no es razón para
perder la confirmación, solo para no contestar.

## Decisión 2 — «No puedo asistir» NO cancela al primer toque

Cancelar es irreversible por partida triple: libera el cupo de `reservas`, borra el evento de
Google Calendar y deja el hueco disponible para otro paciente en el mismo minuto. Un dedo que
roza el botón equivocado no se puede deshacer — cuando el paciente escriba «perdón, me
equivoqué», la hora puede ser ya de otro.

Así que el botón no cancela nada por sí solo. Entra al turno como hoy y Daniela pregunta:
«¿te confirmo que cancelo tu cita del jueves a las 10:00?». Solo un sí llama a `cancelar_cita`.

Lo que cuesta, dicho a sabiendas: **si el paciente no contesta, la cita sigue en pie y nadie
avisa al doctor.** Se acepta. El principio que decide los empates de este proyecto es que la
seguridad clínica prevalece, y entre «un cupo que tarda unas horas más en liberarse» y «una
cita cancelada por error», el empate lo gana la cita.

## Decisión 3 — el aviso cuelga de las tools, no del botón

El aviso de cancelación y el de cambio **no cuelgan del botón: cuelgan de
`herramientas._cancelar_cita` y `herramientas._reprogramar_cita`**, en el mismo sitio donde
`_crear_cita` ya llama a `aviso_citas.avisar_en_segundo_plano`.

Al doctor le cambia la agenda igual si el paciente lo pidió escribiendo que si pulsó un
botón, y colgarlo del botón dejaría mudo el camino por el que hoy pasa casi todo. Además
cumple por construcción el requisito de MaxiCare de no anunciar una reprogramación que no
ocurrió: `_reprogramar_cita` solo llega a su última línea cuando la cita nueva **ya está** en
Neon y en Google Calendar y el cupo viejo está liberado. Si la reprogramación falla o queda a
medias, no hay aviso que mandar porque no se llega ahí.

**El aviso no puede tumbar nada.** Cuando corre, la cita ya está donde tiene que estar. Se
hereda tal cual la disciplina de `aviso_citas`: tarea de fondo propia, `try` por destinatario
dentro del bucle, nunca propaga, y con la plantilla sin configurar **decide igual y no manda
nada**, dejando en el log a quién se le habría escrito y con qué.

Y las dos guardas que impiden avisar dos veces de lo mismo, una por tool:

- `_cancelar_cita` sale antes por la suya (`estado == 'cancelada'`).
- `_reprogramar_cita` **solo avisa si la hora cambió**. La clave de idempotencia de
  `tomar_cupo` no lleva componente de turno, así que repetir el mismo movimiento devuelve la
  misma reserva y termina sin excepción: sin esta guarda salía un segundo «Cambió su cita»
  con las dos horas iguales dentro, un aviso que dice que nada cambió.

## Cambios en la base: migración 031

```sql
ALTER TABLE citas ADD COLUMN IF NOT EXISTS confirmada_por_paciente_en TIMESTAMPTZ;
```

Nullable a propósito y sin default. `NULL` es «no ha dicho nada», que es distinto de «dijo que
no» — eso último ya es `estado = 'cancelada'`. Una columna booleana con default `false` haría
esas dos cosas indistinguibles.

No se añade ninguna columna para «no puede asistir»: eso ES una cancelación, y la tabla ya
tiene `estado` y `motivo_cancelacion`.

## Cambios en el prompt

`agentes.py` le enumera hoy a Daniela los botones de las **tres plantillas de reactivación**
(«Traía dos botones: "Sí, me interesa" y "Ya no, gracias"»), y el bloque está cerrado con
`if recordatorio in seguimientos.TIPOS_DE_REACTIVACION`. El recordatorio de cita queda fuera:
ella sabe que salió un recordatorio, pero no qué rótulos leyó el paciente.

Con tres botones esa asimetría deja de ser inocua, porque dos de los tres llegan a Daniela y
uno de ellos pide cancelar. Se extiende el bloque para que el recordatorio de cita también
enumere los suyos, con la instrucción explícita de que «No puedo asistir» **se confirma antes
de cancelar**.

Toca el prompt, así que rompe el caché de entrada una vez. Es un coste de un despliegue.

## Lo que NO hace

- **No toca Telegram.** El no negociable 14 dice que al General solo van las alertas de
  escalamiento y que el hilo del paciente es mudo salvo dos excepciones. Una confirmación no
  es ninguna de las dos.
- **No confirma citas pasadas ni canceladas.**
- **No agrupa los avisos.** Un aviso por pulsación, a los tres números de
  `WHATSAPP_DOCTORES`. Con el volumen medido de la clínica —4 conversaciones y 11 turnos al
  día el 21/09/2026— eso no satura a nadie. El día que sature, la salida es un resumen, y es
  otra spec.
- **No toca el panel.** La marca de confirmación no se pinta en la pantalla de Agenda. Queda
  anotado como lo primero que pedir si MaxiCare lo echa en falta.
- **No manda nada mientras las plantillas no estén aprobadas**: lo registra todo y lo escribe
  en el log.

## Cómo se comprueba

- **Offline** (`uv run pytest -q`): el corte de `_entregar` en sus cinco caminos —confirma,
  no encuentra cita, la cita ya pasó, la cita está cancelada, el rótulo no coincide—; los
  cinco huecos del aviso en su orden exacto; que la plantilla vacía decide y no manda; que
  el aviso no propaga cuando un destinatario falla.
- **Contra Neon** (`-m neon`): la migración 031 sobre el esquema `pruebas`, la búsqueda de la
  cita del último recordatorio con dos citas del mismo teléfono, y que `marcar_respondido`
  deja el mensaje fuera de «sin contestar».
- **A mano, en producción y en este orden**: `scripts/probar_plantilla.py --estado` para ver
  los tres rótulos tal como Meta los aprobó; después una cita de prueba con el recordatorio
  adelantado, y pulsar los tres botones desde un teléfono de `MAXICARE_TELEFONOS_PRUEBA`.

**Lo que ninguna prueba caza**, y hay que mirar con los ojos: que los rótulos que Meta aprobó
sean carácter por carácter los que compara el código. Un acento distinto no rompe nada — deja
el botón sin camino, en silencio.

## Riesgos conocidos

1. **Los rótulos.** Ya dicho arriba, y es el riesgo número uno de esta spec.
2. **El paciente con dos citas futuras.** Se confirma la del último recordatorio despachado,
   que es de la que habla el botón que está pulsando. Si le llegaron dos recordatorios
   seguidos, confirma el más reciente — y la guarda G7 del despachador ya impide que salgan
   dos recordatorios al mismo número en la misma ventana.
3. **Confirmar y escribir a la vez.** El botón cierra su camino y el texto abre el suyo, así
   que Daniela contesta lo escrito sin saber por sí misma que hubo un botón. La marca está en
   la base antes de que ese turno corra, pero el prompt no la lee hoy. Se acepta: lo que
   importa —el registro y el aviso— ya ocurrió.

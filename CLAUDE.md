# MaxiCare · Daniela

Agente conversacional por WhatsApp para una clínica dental en Bogotá.
El diseño cerrado vive en `docs/agentes/plan-agentes.json` (11 decisiones, cada una
con su alternativa descartada). No rediseñes nada sin leer la decisión que aplica.

**El principio que decide los empates:** la seguridad clínica prevalece sobre
cualquier objetivo comercial. El éxito no es acumular citas: es que el paciente
llegue a la cita correcta.

# Comandos

- Pruebas: `uv run pytest -q`
- Las que tocan la base: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
  — escriben en el esquema `pruebas`, nunca en `public`, y lo borran al terminar.
- Base de datos (idempotente: migraciones + carga + verificación):
  `uv run python scripts/inicializar_base.py` · `--solo-verificar` no escribe.
- Ver el diseño sin leerlo entero: `uv run python scripts/ver_plan.py <clave>`
  (`fases`, `herramientas`, `guardrails`, `agentes`, `contexto`, `fallos`…).
- Cuánto historial gasta un turno: `uv run python scripts/medir_historial.py` (solo lee).
  Es el único camino para cerrar el límite MEDIDO del historial, y exige **20 turnos en 5
  conversaciones reales** antes de dar un número. No lo confundas con
  `config.LIMITE_HISTORIAL_SESION = 230`, que es un tope de seguridad y no una medición.
- Desplegar en el VPS: `bash scripts/desplegar.sh`
- Usuarios del panel: `uv run python scripts/crear_usuario.py` (`--listar`, `--quitar-acceso`)
- Revisar el grupo de Telegram: `uv run python scripts/obtener_chat_telegram.py`
- **Encender el relevo** (6C): `uv run python scripts/configurar_webhook_telegram.py --url
  https://daniela.maxicarecol.com/webhook/telegram`. Telegram NO valida la URL como hace
  Meta: hasta que alguien llame a `setWebhook`, el botón «Hablar yo con el paciente» no hace
  nada y no hay error en ningún log. `--estado` mira qué hay; `--quitar` lo apaga. **Poner
  webhook rompe el `getUpdates` de `obtener_chat_telegram.py`**: son excluyentes.
- Comprobar que el relevo puede funcionar: `uv run python scripts/probar_relevo.py` (no
  gasta, no toca la base; crea y borra un tema de usar y tirar).
- Resetear a primer contacto: escribir `/clearstate`. **Es irreversible** y borra el rastro
  entero, historial del agente incluido. **Por WhatsApp** solo funciona si el número está en
  `MAXICARE_TELEFONOS_PRUEBA`; con esa variable vacía —su default— el comando no existe para
  nadie. En el chat web del panel funciona sin lista y solo toca `pruebas_web`.
  El detalle, en `.claude/rules/atencion-whatsapp.md`.

Entregables por fase. **Los marcados gastan tokens**; los demás, ni uno:

| Comando | Qué prueba | ¿Gasta? |
|---|---|---|
| `scripts/probar_tools.py` | las trece tools contra Neon (fase 3) | no |
| `scripts/probar_agentes.py` | los dos agentes contra la API real (fase 4) | **sí** |
| `scripts/probar_web.py` | el cascarón web (fase 5) | solo con `--chat` |
| `scripts/probar_atencion.py` | el turno de WhatsApp de punta a punta (fase 6A) | solo con `--chat` |
| `scripts/probar_lectura.py` | el muro y el tema del paciente (fase 6B) | solo con `--chat` |
| `scripts/probar_relevo.py` | que el bot PUEDA relevar: permisos y webhook (fase 6C) | no |
| `scripts/probar_persistencia.py` | que una conversación sobrevive a reiniciar (fase 7) | solo con `--chat` |
| `scripts/probar_panel.py` | el panel de tratamientos y la agenda (fase 8) | solo con `--chat` |
| `scripts/probar_recordatorios.py` | la cola de recordatorios y su despachador | no |
| `scripts/probar_sin_resolver.py` | el informe de lo que Daniela no pudo (fase PENDIENTE) | no |
| `scripts/probar_reactivacion.py` | el barrido de reactivación de leads y las once reglas anti-reporte | no |
| `scripts/probar_plantilla.py` | la plantilla de Meta, y manda UNA de verdad | **sí** (`--estado` no) |
| `scripts/probar_calendario.py` | `CalendarioGoogle` contra el calendario real | no |
| `scripts/probar_transcripcion.py` | las notas de voz reales contra la API de audio | **sí** (poco: se factura por duración) |
| `scripts/probar_webhook.py <url>` | el webhook en producción | **sí** (despierta a Daniela) |

**Estos scripts doblan funciones de `src/` con firmas escritas a mano, y `pytest -q` no los
corre.** Cambiarle la firma a algo que un script dobla los rompe en silencio, con la suite
entera en verde: quien toque una de esas firmas corre los seis que no gastan. Lo demás de
cada script —en qué esquema escribe, qué dobla, qué trampa tiene— está en
`.claude/rules/scripts-entregables.md`.

# No negociables

Cada una es una línea porque tiene que sobrevivir a una compactación. El porqué de cada
una —qué se midió, qué costó— está en la regla que cubre ese archivo.

1. **Si el calendario no arranca, Daniela queda con `CalendarioCaido`, NUNCA con
   `CalendarioDoble`.** El doble dice que sí a todo y le confirma al paciente una cita que
   no existe: llega a una clínica donde nadie lo espera.
2. **Ninguna clave de idempotencia la escribe el modelo.** Las cuatro las arma el código con
   `ctx.clave(...)`. Con la del modelo, dos pacientes salían confirmados sobre un solo cupo.
3. **El candado de `atencion.py` va por TELÉFONO y `_leer_estado` va DENTRO.** Sacar la
   lectura fuera devuelve dos carreras medidas: conversaciones duplicadas y escalamientos
   que el doctor no ve.
4. **Meta reintenta los webhooks y hay DOS deduplicaciones, no una**: `ON CONFLICT (wamid)`
   protege el reenvío al doctor, `Resultado.nuevo` protege el turno de Daniela.
5. **`asegurar_conversacion` SIEMPRE inserta**, pese al nombre. En WhatsApp se usa
   `conversacion_viva`.
6. **El búfer de mensajes: la ventana va antes del candado, el retardo se descuenta (no se
   suma) y el búfer se saca en un `finally`.** Mover cualquiera de las tres rompe algo en
   silencio.
7. **Una cita de Daniela NO es un bloqueo del doctor.** La marca
   `extendedProperties.private.origen = "daniela"` es lo que impide que la clínica atienda
   a uno por hora en vez de a dos.
8. **`uv run pytest -q` a secas NO caza una regresión en `tocar_conversacion`.** Quien toque
   esa función corre además las de Neon y `scripts/probar_atencion.py`. **Ni caza las de
   `tests/test_panel.py`, que también son de Neon**: una colisión de helper dejó dos en rojo
   durante tres commits con la suite offline entera en verde. Quien toque el panel corre `-m neon`.
9. **El historial del diálogo YA está en Postgres** (`agent_sessions` / `agent_messages`,
   `session_id = id_conversacion`). Quien toque `/clearstate` tiene que borrarlo, y va
   ANTES del `DELETE FROM conversaciones`: los `session_id` SON esos ids.
10. **Las columnas de la 010 las fija el SDK, no nosotros.** `SQLAlchemySession` corre con
   `create_tables=False`: `agent_sessions` y `agent_messages` tienen que coincidir con lo que
   el SDK espera, incluido el `TIMESTAMP` **sin zona**. Quien suba la versión compara columna
   por columna; lo caza `tests/test_sesion_neon.py`, solo con `-m neon`.
11. **La regeneración corre SIN los guardrails de ENTRADA, y un tripwire de entrada no se
   regenera nunca.** Va sobre `agente.clone(input_guardrails=[])` y conserva los tres de
   SALIDA. El `{motivo}` que viaja en `CORRECCION` es el TEXTO del guardrail, jamás su nombre.
   **Y PARAR no es ESCALAR: `uso_indebido` clasifica lo que paró.** `tarea_ajena` --«hazme un
   código»-- se corta igual pero **no interrumpe a nadie**: frase amable, sin `escalado_por` y
   sin `fallo`. `ataque` termina donde siempre. Hasta el 21/09/2026 quien pedía código recibía
   «ya le paso tu mensaje al doctor»: dos promesas falsas y un Telegram por algo que no era un
   ataque. **El default es `ataque` en los DOS sitios** y solo el literal exacto `tarea_ajena`
   compra el silencio: ahorrarse un escalamiento no puede salir de un dato que no llegó. Y
   `escalado_por = None` NO apaga nada --hay un respaldo al final de `responder`--: el
   interruptor es `_respuesta_de_emergencia(..., escala=False)`.
12. **Un teléfono SIN ficha en `pacientes` puede crear su primera cita; mover o cancelar,
   nunca.** El permiso lo da `ctx.telefono_sin_paciente`, que sale de la base y **nunca del
   modelo**, y la excepción es una lista blanca de UNA tool (`_ESCRITURAS_PARA_DESCONOCIDO`).
   **`crear_cita` registra al paciente**, así que desde el turno siguiente sí puede mover lo suyo.
   **Y una ficha cuyo nombre es `persistencia.NOMBRE_PENDIENTE` NO cuenta como ficha**: es la
   tercera categoría que no puede existir —ni desconocido ni verificado— y encierra al paciente
   sin un error en ningún log. El relevo la abría y dejó de hacerlo el 16/09/2026; las que
   quedaron las repara `asegurar_paciente` al agendar, escribiendo encima del marcador y **solo**
   de él. **Y desde la 020 hay con QUÉ agendarlo: `valoracion`.** Poder agendar no servía de
   nada sin una clave de tratamiento, y el paciente con dolor y sin diagnóstico --el caso
   normal, no el raro-- no tenía ninguna: escalaba en vez de agendar con el cupo libre
   delante. `no_identificado` NO es esa clave y sigue fuera de la lista que se le ofrece al
   modelo: es lo que usa el sistema cuando no sabe, y una cita con él diría que nadie sabe a
   qué va el paciente.
13. **La pertenencia de una cita va por TELÉFONO (`_es_ajena`), nunca por el UUID, y toda hora
   que una tool confirma queda autorizada** — incluida la vieja al reprogramar y la cancelada
   al cancelar. Si no, `sin_hora_no_verificada` bloquea la confirmación de una escritura **que
   ya ocurrió**: la cita movida y el paciente yendo a la hora vieja.
14. **Al General solo va lo que le pide algo al doctor, y la señal de alarma es `reenviado_en`
   NULL, no `telegram_message_id` NULL.** Un texto va mudo al tema de su paciente, o a ninguna
   parte si no tiene tema —nunca lo crea: eso es del primer archivo—. El aviso de archivos
   suena UNA vez por tanda (`_primer_archivo_de_la_tanda`, 24 h). **Y un escalamiento va a los
   DOS sitios**: al General con su resumen, y al hilo del paciente solo con el motivo y el
   botón (`relevo.ofrecer_la_puerta_en_el_hilo`, que SUENA — segunda y última excepción al
   silencio del hilo, tras la bienvenida del relevo). El doctor mira el hilo, no el General:
   con la puerta solo en el General, un doctor que borró su mensaje da por hecho que el
   sistema dejó de ofrecerle tomar la conversación. El resumen y la pregunta NO bajan al
   hilo: son la deliberación, y su sitio es el General.
15. **El relevo tiene UNA puerta de salida, y `/webhook/telegram` se cierra cuando falta el
   secreto.** `conversaciones.tomada_por` puesto significa Daniela callada **y** tema abierto:
   las dos dejan de ser verdad juntas, por `relevo.cerrar` con uno de los tres motivos del
   CHECK de la 003. El reloj cuenta desde `GREATEST(tomada_en, ultimo_mensaje_doctor_en)`. Sin
   `MAXICARE_TELEGRAM_WEBHOOK_SECRET` responde 403 a todo. Y un mensaje que entra durante un
   relevo se anota con `fallo_respuesta` empezando por `relevo:` **sin ser un fallo**.
16. **El hilo de Telegram va por TELÉFONO (`temas_telegram`), no por ficha, y tener hilo NO
   es estar verificado.** `lectura.asegurar_tema` ya no exige ficha y sigue sin crear ninguna;
   la identidad sigue derivando de la EXISTENCIA de la fila en `pacientes`.
17. **Lo único que cruza del relevo hacia Daniela es que hubo relevo y, si la hubo, una
   CITA.** Nunca lo que escribió el doctor, ni literal ni resumido. La cita se crea en el orden
   cupo → Calendar → fila, y `tomar_cupo` puede decir que no: es lo único que impide que la
   clínica le dé esa hora a otro. El estado vive en `conversaciones.cierre_pendiente`, **en la
   base y no en memoria**. Y el MIME de un archivo que baja de Telegram sale de la EXTENSIÓN.
18. **El relevo tiene CUATRO salidas, y la cuarta es cerrar el hilo a mano.**
   `relevo.cerrar_por_tema_cerrado` la atiende desde `forum_topic_closed` con motivo
   `devuelto_por_doctor`, **sin** preguntar por la cita, y es idempotente. El botón «Listo» va
   anclado (`can_pin_messages`). La transcripción se desempaqueta DOS niveles
   (`persistencia._solo_la_frase`).
19. **Borrar un tema NO emite ningún evento, y el `tratamiento` del relevo lo escribe el
   doctor.** Lo detecta el barrido con `telegram.estado_del_tema`, que pregunta con
   **`reopenForumTopic`** y devuelve TRES estados más un `None` que **no** cierra nada. Al
   cerrar por `tema_perdido` el hilo se OLVIDA (`persistencia.olvidar_tema`), no se marca
   cerrado. Ninguna prueba offline lo caza: quien toque la sonda corre
   `scripts/probar_relevo.py`. Y el `tratamiento` se guarda tal cual, **sin validar contra la
   lista viva** — única excepción del proyecto. **Y un tema borrado sigue diciendo que sí
   durante varios segundos**: `reopenForumTopic` responde `ok: true` mientras `sendMessage`
   ya rechaza, así que `activar` reintenta UNA vez --olvidar, crear, bienvenida-- cuando la
   bienvenida cae en un hilo muerto. Sin eso quedaba `tomada_por` puesto y ningún hilo:
   Daniela callada y nadie hablando con el paciente. Y `reabrir_tema` distingue TRES cosas,
   no dos: `HiloInvalido` si el tema no existe, éxito si `TOPIC_NOT_MODIFIED` --ya estaba
   abierto, que es lo que promete-- y `ErrorDeCanal` para lo demás.
20. **Para una cita que YA existe manda Google Calendar, no Neon.**
   `herramientas._sincronizar_con_calendar` la contrasta en cada `consultar_citas` y corrige
   Neon: **antes de cortar el pasado**, dejando la fila quieta si hay `ErrorDeCalendario` (un
   timeout no es «la borraron») y moviendo igual si la hora destino está llena. **Y lo que
   corrige lo DICE en el texto de la tool, con la hora vieja dentro**: corregir en silencio
   dejaba al modelo viendo al sistema desdecirse, y escalaba. Esas horas quedan autorizadas
   como la vieja al reprogramar (13).
21. **Un recordatorio se MARCA antes de enviarse, y lo emite el código, no el modelo.** No hay
   transacción que cubra una llamada a Meta: enviar primero y marcar después manda el mismo
   recordatorio otra vez sesenta segundos más tarde, y **ninguna de las NUEVE guardas lo
   detecta** —todas siguen diciendo que sí—. (Eran siete hasta el 21/09/2026, cuando la
   reactivación de leads añadió dos; una reactivación pasa por trece. El número que manda vive
   en el docstring de `seguimientos.decidir`, no aquí: esta línea ya envejeció una vez.) La
   cola cuelga de `cita_id` y no solo de la
   conversación: sin eso, reprogramar deja vivo un recordatorio de una cita que ya no existe.
   El despacho vive en su **propia** tarea de `runtime.py`, no en la de relevos, que no arranca
   sin Telegram.
22. **El informe de «sin resolver» se escribe DESPUÉS de responderle al paciente, y su huella
   la arma el código.** Va dentro del `try` de `_anotar_resultado` que ya traga: si revienta
   se pierde un caso, nunca un turno. Con huellas del modelo, dos casos iguales salen
   distintos y la agrupación —que es todo el valor— se rompe sin un solo error en el log. El
   `ROTO` agrupa por `type(e).__name__`, **nunca** por el mensaje: con el mensaje cada error
   es único. Un `fallo_respuesta` que empieza por `relevo:` no entra: contarlo inundaría el
   informe con un caso por cada relevo y ahogaría los fallos de verdad bajo ruido que no lo
   es. Y `/clearstate` borra los ejemplos **sin** bajar el contador: bajarlo borraría de la
   cuenta a un paciente real cada vez que alguien resetea su número, y «doce personas
   preguntaron por ortodoncia» dejaría de ser cierto.
23. **El rótulo de un quick reply llega en `button.text`, y `uso_indebido` NO lo evalúa.** Meta
   manda `type: "button"`, no `text`: mirar solo `text.body` deja el turno MUDO —sin un error en
   ningún log— y los dos botones que Meta aprobó sin servir para nada. Y el rótulo suelto
   («Confirmar») el evaluador lo lee como una inyección, con mensaje seguro al paciente y alerta
   falsa al doctor, de forma **intermitente**: pasó a las 22:04 y disparó a las 22:11. La señal
   `ctx.entrada_solo_de_botones` sale del `type` del webhook y **nunca del modelo**, y exige
   TODOS los mensajes del grupo, no alguno: con uno bastando, el botón es el portillo por donde
   entra texto libre sin evaluar.
24. **El aviso de la política se MARCA DESPUÉS del envío, justo al revés que la 21, y es
   deliberado.** En un recordatorio el riesgo es mandarlo dos veces, así que se marca antes;
   en el aviso el riesgo es dejar constancia de uno que nunca salió, y esa constancia ES la
   prueba legal. Repetir un aviso es inocuo; falsificar una prueba, no. Sale **una sola vez
   en la vida del número** —el dato vive en `contactos`, que no caduca a las 24 h— y el
   literal `PENDIENTE` lo apaga entero: mientras lo esté, la frase de avisarlo vive en el
   prompt, **condicionada a ese `PENDIENTE`**, o el sistema no informa de la política ni una
   vez. **Desde el 16/09/2026 hay URL y vive en `config.py`, no en el `.env`** —es pública, y
   el día que cambie tiene que quedar en `git log`; un despliegue que olvide una variable no
   puede significar dejar de informar—, así que manda el código y la frase del prompt
   desapareció sola. La `politica_version` de cada fila **tiene que tener su PDF congelado con
   su SHA-256 en `docs/politica/`**: un consentimiento que no se puede contrastar con un
   documento no acredita nada, y el destino es Drive, donde una versión nueva se sube sobre el
   mismo enlace.
25. **`/clearstate` resetea el aviso y NUNCA la baja, y la fila de `contactos` no se borra
   jamás.** Ese «no» es del paciente, no del sistema: si la fila se fuera, resetear a alguien
   lo devolvería a la lista de contactables sin que nadie se entere. Mismo precedente que los
   ejemplos de casos sin resolver (22). De la bitácora `consentimientos` no se BORRA nunca una
   fila —el `ON DELETE RESTRICT` lo hace imposible aunque alguien lo intente—, que no es lo
   mismo que «no se toca», como decía esta línea hasta el 21/09/2026: `/clearstate` le pone el
   `detalle` a NULL (la frase literal del paciente, que §13 deja pedir suprimir) y le AÑADE una
   fila `rastro_borrado`. Lo que se conserva intacto es lo que acredita: el hecho, la fecha, el
   origen y la versión. Y la baja es
   **comercial**: no apaga el recordatorio de una cita, y eso lo sostienen la lista blanca
   `TIPOS_NO_COMERCIALES` en G0 **y** la misma guarda dentro de `programar_seguimiento`, que
   es lo que impide que dependa de que el modelo obedezca.

26. **Un escalamiento se deduplica por TURNO y por ASUNTO, y hacen falta las dos.**
   `requiere_escalamiento` lo LEE el orquestador como un flanco («avisa ahora») y el modelo lo
   EMITE como un estado («esto sigue necesitando a un humano»): lo deja en `true` mientras el
   asunto siga abierto. La clave `escalamiento:{turno}` no puede pararlo --y no debe: congelada,
   el doctor se entera del primero y de ninguno más--, así que cada turno se volvía un Telegram
   al General con el texto entero de lo que se le respondió al paciente. Cuatro en seis minutos
   el 16/09/2026. Lo para `persistencia.escalamiento_vivo_con_motivo`, con sus condiciones:
   **entregado** (`telegram_message_id` NO nulo, o se quemaría al INTENTAR y volvería el agujero
   de la 6A), **sin responder**, y **mismo motivo** --sin esta última se callaría el
   `dato_faltante` que viene detrás de un `clinico`, que es justo el que pedía algo nuevo--. Va
   ANTES del INSERT: la fila tampoco se escribe, o `telegram_message_id` NULL dejaría de
   significar «el Telegram no salió». El prompt ya decía «escalas una vez por asunto» y no
   bastó: la guarda va en el código, como la baja de la 25. **Y «delante del doctor» se
   COMPRUEBA, no se supone**: borrar un mensaje no emite ningún evento --como borrar un tema--,
   así que la guarda le pregunta a Telegram con `canales.aviso_sigue_puesto`
   (`editMessageReplyMarkup` con el mismo teclado: si sigue igual, «not modified» y no toca
   nada). El doctor borraba el aviso del General para dejar la bandeja limpia y el sistema
   seguía creéndolo vivo: sin aviso, sin botón y sin hilo durante 24 h (17/09/2026, aviso 855).
   Un aviso ya TOMADO (`relevo_activado`, que es BOOLEAN) no se sondea --su teclado ya es el
   enlace al hilo-- y ante la duda se AVISA, que es el lado barato de equivocarse. **Solo
   silencia la RED DE SEGURIDAD, nunca la tool**: `herramientas._escalar_a_doctores` escribe su fila y manda su
   propio Telegram sin pasar por `_registrar_escalamiento`, así que lo que el modelo escala de
   verdad --llamando-- sale siempre, y lo único que se calla es el flanco que dejó encendido
   sin llamar a nadie. Ahí está la frontera con la seguridad clínica, y quien mueva esta guarda
   a un sitio por el que pase la tool la cruza. Dos cosas más que saber: **`respondido_en` la
   escribe `relevo.cerrar`, y solo él** --desde el 17/09/2026; antes no la escribía nadie y el
   silencio duraba las 24 h de la conversación aunque un doctor ya hubiera atendido el asunto
   EN PERSONA y lo hubiera devuelto--. Cerrar el relevo es el ciclo completo, no `relevo_activado`
   (que se pone al TOMAR y por eso no vale): contrastado contra las filas del caso medido, el
   ruido del 16/09 se produjo con el relevo aún sin cerrar, así que esta marca no lo deja pasar.
   Lo que devuelve es solo lo que llega DESPUÉS del cierre, y sin ella no volvía ni el aviso, ni
   el botón, **ni el hilo** --`rescatar_hilo` cuelga del aviso que se calla--. **Y un aviso
   que se calla NO se
   cuenta como interrupción**: `_avisar_a_doctores` devuelve un booleano que viaja en
   `Resultado.doctor_avisado` hasta `atencion`, porque la pantalla imprime «se interrumpió al
   doctor N de M veces» y ese N tiene que ser cierto. Es la misma mentira que ya evita el
   camino del reventón, por la otra puerta.

27. **El perímetro de coste falla ABIERTO, así que su ausencia no se ve en ningún log.**
   `cuotas.revisar` atrapa toda excepción y deja pasar --deliberado: un freno que tumba turnos
   cuando Postgres tiene un mal minuto ES la caída que pretendía evitar-- de modo que **sin las
   tablas de la 022 el perímetro no frena a nadie y nada falla**. Lo caza `inicializar_base.py`
   (bloque 9), que `desplegar.sh` corre antes de levantar. La cuota vive en `runtime._entregar`
   y NO en `atencion.atender`: metida ahí añade una conexión a Neon al principio del turno y
   desordena los dobles de tres pruebas medidas, incluida la invariante de que Neon se cierra
   antes de llamar al modelo. **Y `MAXICARE_LEER_ARCHIVOS` es INDEPENDIENTE de
   `MAXICARE_DANIELA_RESPONDE`**: aquel promete por escrito que el doctor sigue recibiendo sus
   archivos y sus lecturas, así que colgar el lector de él le quitaría justo lo que promete
   conservar, y en el momento en que alguien lo acciona. **`escalar_a_doctores` se queda SIN
   tope**, y eso es la 26 mirada de cerca: lo que el modelo escala llamando a la tool sale
   siempre. El detalle, en `.claude/rules/perimetro-seguridad.md`.

28. **Un guardrail de ENTRADA no recibe el mensaje del paciente: recibe la CONVERSACIÓN
   ENTERA**, como lista de items, porque hay `session`. Comprobado con una sonda contra la
   0.22.2. `uso_indebido` hacía `str(entrada)` y le mandaba el bulto al evaluador, que
   disparaba por lo que había dicho el paciente HACE TRES TURNOS. **Y es un trinquete**: al
   disparar, el modelo no contesta pero el SDK guarda igual el mensaje, así que el historial
   acumula `[user]` sin una sola respuesta y cada disparo hace el siguiente más seguro. El
   21/09/2026 alguien pidió un resumen de un ensayo a las 14:50 y a partir de ahí «¿qué
   tratamientos tienes?» y «quiero agendar una cita» recibieron «yo solo sé de MaxiCare»:
   cuatro `[user]` en `agent_messages`, cero de Daniela, conversación muerta y **ni un error
   en ningún log**. Lo arregla `guardrails._mensaje_del_paciente`, que se queda con el ÚLTIMO
   item de rol `user`; si no reconoce la forma devuelve el bulto entero y **nunca la cadena
   vacía** —a un evaluador al que no se le enseña nada no dispara jamás, y eso es apagar el
   guardrail de inyección en silencio—. **La suite llevaba en verde por el motivo equivocado
   desde el principio**: las seis pruebas le pasaban una cadena escrita a mano, que es lo
   único que este guardrail no recibe nunca. Hacen falta LAS DOS pruebas —la que dobla la
   forma y la que atraviesa `Runner.run` con sesión de verdad— o una versión del SDK que
   cambie el formato pasa entera. Y esto acota lo que promete el truncado de la 27: `_acotar`
   limita cada turno, pero el historial lo rearma el SDK DESPUÉS.

29. **Una nota de voz se TRANSCRIBE y entra al turno como texto del paciente, y el audio le
   llega al doctor igual que siempre.** `audio` estaba en `TIPOS_CON_ARCHIVO` y no en
   `TIPOS_QUE_SE_LEEN`, así que a Daniela le llegaba «nadie lo ha revisado y tú no puedes
   verlo» y ella hacía lo que esa frase pide: confirmar y escalar. Las CINCO notas de voz que
   vio el sistema en su vida cerraron con `motivo = 'archivo_recibido'` y «Ya recibimos tu
   audio». Esa frase es la correcta para una radiografía --que hay que interpretar, y eso es
   del doctor-- y absurda para alguien que dijo «¿cuánto cuesta la limpieza?» en voz alta:
   un audio solo hay que oírlo. **El modelo es `gpt-transcribe` y de los cuatro de la cuenta
   es el ÚNICO que sirve**: `gpt-4o-transcribe` y su mini devuelven algo que parece español
   y no lo es («Con xenáula se una doctora»), que es peor que no transcribir porque Daniela
   contestaría a una frase inventada. **`.oga` es un `400` («Unsupported file format oga») y
   es lo que produce `canales._nombre_sugerido`**, así que el nombre se FUERZA a `.ogg` en el
   borde que habla con la API; pasarle `archivo.nombre` --lo natural-- no transcribe ni un
   audio y no deja nada rojo. `language="es"` mejora de forma medible; un `prompt` con el
   vocabulario de la clínica EMPEORA («por favor» → «por ambos») y no se usa: sesgar hacia lo
   que esperas oír es la dirección equivocada cuando al otro lado alguien describe un
   síntoma. **Sin transcripción se le pide al paciente que lo escriba y NO se escala** --él lo
   resuelve en un segundo y el doctor ya tiene el audio delante--. **Y la transcripción
   alimenta `menciona_sintomas`**: `m.texto` es `None` en un audio, así que un dolor DICHO en
   voz alta daba `False` y el prefiltro de `sin_lectura_clinica` quedaba colgando solo de
   `hubo_adjunto`; van las dos cosas, y `adjunto_del_mensaje` sigue en `True`.
   `MAXICARE_TRANSCRIBIR_AUDIO` es el TERCER freno de mano y no cuelga de los otros dos: el
   de archivos promete no tocar lo del paciente, y esto ES del paciente.

# Dónde está el resto

El detalle de cada área se carga solo cuando tocas sus archivos:

| Archivo | Cubre |
|---|---|
| `web/CLAUDE.md` | la interfaz del panel, y la trampa `public` vs `pruebas_web` |
| `.claude/rules/atencion-whatsapp.md` | el turno de WhatsApp: candado, búfer, idempotencia |
| `.claude/rules/relevo-telegram.md` | el relevo: el botón, el webhook y las tres salidas |
| `.claude/rules/calendario.md` | Google Calendar y la cuenta de servicio |
| `.claude/rules/despliegue.md` | `desplegar.sh`, el Dockerfile y el `.env` del VPS |
| `.claude/rules/scripts-entregables.md` | qué dobla cada script de `scripts/` y su trampa |
| `.claude/rules/perimetro-seguridad.md` | las cuotas, el contador de gasto y los dos frenos de mano |
| `.claude/rules/pruebas.md` · `frontera-agentes.md` · `migraciones.md` · `base-conocimiento.md` · `contratos-diseno.md` | lo que ya había |

# Trampas de este entorno

- **Los heredoc de Bash fallan** aquí (`unexpected EOF looking for matching`).
  Para escribir un archivo usa la herramienta Write, no `cat > archivo <<'EOF'`.
- **La consola de Windows es cp1252** y revienta con `→`, `✅`, acentos. Todo script
  bajo `scripts/` empieza con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
  y usa marcadores ASCII (`OK` / `FALLA` / `->`). El mismo script corre en el VPS.
- `uv run` avisa de que `VIRTUAL_ENV` no coincide. Es ruido, se ignora.
- Es un repositorio git desde el commit `7e12d6b`, que congela las fases 1 a 5. Las
  búsquedas respetan `.gitignore`: `.venv/`, `web/node_modules/`, `web/dist/`, `.env` y
  `.superpowers/` no aparecen. El remoto es `origin` y **se empuja a `main`**, pero solo
  cuando el usuario lo pide: no empujes por tu cuenta. (Esta nota decía que `main` iba
  «decenas de commits» por delante de `origin/main`; el 14/09/2026 la diferencia era de UNO
  y se empujó. Si vuelves a citarla, compruébala con `git rev-list --left-right --count`.)
- **Una fecha clavada en una prueba envejece, y ya ha pasado TRES veces.** El 16/09/2026
  `uv run pytest -q` amaneció con doce pruebas en rojo sin que nadie tocara nada: `INICIO`
  apuntaba al día anterior y `ContextoDaniela.ahora` cae al reloj de verdad si nadie se lo da.
  Antes le tocó a `probar_tools.py::hora` y al bloque 10 de `probar_agentes.py`. Los scripts
  cuentan bloques hábiles hacia delante --hablan con la agenda real--; las pruebas offline
  hacen lo contrario y **clavan el presente** (`tests/test_herramientas.py::AHORA`). El detalle,
  en `.claude/rules/pruebas.md`.
- **El pooler de Neon rechaza `options` como parámetro de arranque** (`unsupported startup
  parameter in options: search_path`). Para fijar un `search_path` —o para una prueba de
  concurrencia de verdad— hay que usar la conexión directa: quitarle el `-pooler.` al host.
- **Hay UNA sola base de Neon, y es la que atiende pacientes.** Desde el 21/09/2026 no existe
  `MAXICARE_DATABASE_URL_ALT`: se borró del `.env` y desarrollo y producción comparten base.
  Eso cambia lo que significan dos comandos de arriba: **`scripts/inicializar_base.py` y las
  pruebas `-m neon` escriben ahora en producción.** Las segundas solo tocan el esquema
  `pruebas` y lo borran al terminar, pero el `public` de esa conexión ya es el de verdad, así
  que lo que se haga sin `-m neon` de por medio cae sobre datos reales. **El que hay que mirar
  dos veces es `scripts/probar_panel.py`**, que cambia un precio y siembra un día de agenda
  **en `public` a propósito**: está construido para eso —restaura el precio en un `finally` y
  las citas van con `evento_calendar_id` NULL y un teléfono imposible, así que no tocan Google
  ni ocupan cupo— pero hasta hoy caía sobre una base sin pacientes y desde hoy no. La alterna nunca fue
  un interruptor —ningún archivo de `src/` la leyó jamás—, así que «trabajar contra otra base»
  siempre fue cambiar a mano el valor de `MAXICARE_DATABASE_URL`, y lo sigue siendo. No se
  deja un respaldo en `config.py` a propósito: dejaría que producción arrancara contra la base
  equivocada sin que nadie lo note.
- **Una variable de entorno vacía no es una variable ausente.** Si TODA llamada al modelo
  muere en `APIConnectionError: Connection error.` mientras `/salud` dice `configuracion:
  ok`, no es la red: es una clave vacía que el `env_file` de Docker sí exporta. Ya pasó en
  producción. El caso entero, en `.claude/rules/despliegue.md`.
- **La herramienta `Write` interpreta las secuencias `\uXXXX` del contenido como caracteres
  de verdad.** A un implementador le dejó un byte NUL dentro de un `.tsx` y caracteres
  combinantes invisibles dentro de un regex. Se esquiva escribiendo esos archivos con
  here-strings de PowerShell, y conviene comprobar el resultado (contar bytes NUL y
  caracteres de categoría `Cc`/`Cf`/`Mn`) cuando el contenido lleve `\u`.
- **Pero PowerShell no vale para EDITAR un archivo que ya existe.** El here-string de arriba
  sirve para crear uno nuevo; el ciclo leer-modificar-escribir con `Get-Content -Raw` /
  `Set-Content` **destroza el encoding de este repo** y deja mojibake en todos los acentos
  (`configuración` → `configuraciÃ³n`). Pasó en la fase 7 y hubo que restaurar con
  `git checkout`. Para modificar un archivo del proyecto, la herramienta `Edit`.
- **Tres documentos de diseño son enormes y están versionados.** `plan-agentes.json`
  (~22.000 tokens) está bloqueado por `.claude/settings.json`: léelo con `ver_plan.py`.
  `plan-agentes.md` (~12.000) y `brief-agentes.json` (~7.000) no están bloqueados porque no
  tienen alternativa — léelos por rangos, nunca enteros.

# Reglas duras

1. **`from agents import tool` está prohibido.** `agents.tool` es un módulo, no el
   decorador. Usa `from agents.decorators import tool` o `from agents import function_tool`.
2. **Nunca se escribe código del SDK sin comprobar la versión instalada.**
   Verificada 0.22.1, instalada 0.22.2.
3. **Lo que no se sabe se marca con el literal `"PENDIENTE"`.** Nunca con un valor
   plausible: una suposición razonable no se distingue de un hecho verificado.
4. **No se registran cédulas ni documentos de identidad de ningún tipo.**
   Prohibición expresa del cliente. La tabla `pacientes` no tiene esa columna, y esa
   ausencia ES la política: no le agregues una.

# Datos y secretos

- `.env` tiene credenciales reales (Neon, OpenAI, Telegram, WhatsApp). Nunca lo
  imprimas ni lo pegues en una respuesta. `.env.ejemplo` sí se versiona.
- La base de Neon es exclusiva de este proyecto.
- Al mostrar una cadena de conexión, enmascárala: `***@host`.

# Contexto

- Delega la exploración a subagentes: que vuelva el resumen, no los archivos.
- Lee rangos concretos, no archivos enteros, cuando sepas dónde está lo que buscas.
- Al cambiar a una tarea sin relación con la anterior, `/clear`.
- Tras dos correcciones fallidas sobre lo mismo, `/clear` y reformula.
- Para cambios que tocan varios archivos, plan mode antes de editar: el plan se escribe a
  archivo y se re-inyecta tras cada compactación.
- `/compact céntrate en X` conserva lo que tú eliges, no lo que el resumen adivine.

# Compact instructions

Al compactar, conserva: las decisiones de diseño ya aprobadas, la fase en curso y su
entregable verificable, y lo que quedó PENDIENTE de MaxiCare.

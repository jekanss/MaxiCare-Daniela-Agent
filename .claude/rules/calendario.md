---
paths:
  - "src/maxicare_daniela/calendario.py"
  - "src/maxicare_daniela/herramientas.py"
  - "scripts/probar_calendario.py"
  - "tests/test_calendario_google.py"
---

# Google Calendar

El `CLAUDE.md` raíz lleva la regla en una línea. Aquí está el porqué.

- **Una cita de Daniela NO es un bloqueo del doctor.** Es la trampa central de
  `CalendarioGoogle` y con `CalendarioDoble` era invisible: el doble guarda eventos y
  bloqueos en listas separadas, Google los devuelve juntos. Sin filtrarlos, la primera cita
  de una hora taparía el bloque y la clínica atendería **uno** por hora en vez de dos. Cada
  evento que crea Daniela lleva `extendedProperties.private.origen = "daniela"` y
  `bloqueos()` lo descarta. Un evento sin marca es de los doctores y sí tapa.
- **La rejilla tiene horario, y hasta el 13/09/2026 no lo tenía.** `_huecos_libres` cruzaba
  tres cosas —la rejilla, el cupo de Neon y los bloqueos del doctor— y la rejilla salía tal
  cual de la ventana que el modelo pidiera: **la ventana ERA la oferta**. Se vio en
  producción a las 6:22 p. m.: el paciente pidió cita «el próximo martes 15», el modelo
  consultó el día completo (`{"desde":"2026-09-15T00:00:00-05:00","hasta":"2026-09-15T23:59:59-05:00"}`,
  leído de `agent_messages`) y Daniela ofreció «12:00 am, 1:00 am o 2:00 am» — la traducción
  CORRECTA de 00:00, 01:00 y 02:00. El modelo no alucinó: el sistema le dio esas horas.
  Ahora `calendario.Jornada` filtra, y las tres tools de agenda la respetan. Pedir el día
  entero sigue siendo razonable; acotar la respuesta es trabajo del código.
- **«Cerrado» y «lleno» son opuestos, y hasta el 13/09/2026 decían lo mismo.** Una ventana
  entera fuera de jornada devolvía «No quedan bloques libres en esa ventana», el mismo texto
  que con la agenda saturada, y eso manda al paciente a buscar otro **día** cuando lo que
  necesita es otra **hora**. Ahora `_consultar_disponibilidad` comprueba si algún bloque de
  la ventana cae dentro de la jornada —`bloques_del_dia` sin `no_antes_de`, que es justo la
  pregunta «¿existe algún bloque aquí?»— y si no, contesta con `_texto_fuera_de_horario`.
  Las tres tools de agenda dan la misma respuesta: el horario **y** las horas libres más
  cercanas, buscadas con `VENTANA_PROXIMO_HUECO` (tres días), que cruza el día y el fin de
  semana. `VENTANA_ALTERNATIVAS` (ocho horas) no sirve aquí: quien pide las siete de la
  tarde no tiene nada más ese día, y ocho horas hacia adelante siguen cayendo de madrugada.
  La búsqueda va **hacia adelante** desde la hora pedida y no mira antes: a quien pide las
  7 p. m. se le ofrece el día siguiente, no las 4 p. m. de hoy, que es una hora a la que ya
  dijo que no puede. Cuesta una consulta a Neon y una llamada a Google en un camino que
  antes no tocaba ninguna: se paga antes de tomar ningún cupo, y a cambio la conversación
  no muere en «esa hora no puede ser».
- **La oferta de un día entero se REPARTE; las alternativas de una hora llena, no.** Son dos
  preguntas distintas con la misma función detrás. `_huecos_libres` cortaba en los seis
  primeros bloques seguidos, y un día completo devolvía 08:00–13:00: **la tarde no le llegaba
  al modelo**, así que Daniela ofreció «8:00 am, 9:00 am o 10:00 am» con el miércoles entero
  libre (producción, 13/09/2026). Ahora `consultar_disponibilidad` pasa `repartir=True` y
  `_repartidas` conserva el primero y el último repartiendo el resto a pasos iguales. Los
  otros dos usos —las alternativas de `crear_cita` y `_proximos_huecos`— siguen SIN repartir
  a propósito: quien pidió las nueve quiere lo más parecido a las nueve, no las cinco de la
  tarde. La otra mitad es del prompt: con la lista repartida, tomar las tres primeras seguía
  dejando fuera la tarde.
- **El horario vive en DOS sitios y se mueven juntos.** Los números (`hora_apertura`,
  `hora_cierre`, `hora_cierre_sabado`, `atiende_domingo`, tabla `configuracion`, migración
  012) filtran la rejilla; la fila `_general`/`horario` de la base de conocimiento es la que
  Daniela **recita** cuando le preguntan. Si divergen, dice una cosa y ofrece otra.
  `test_la_jornada_configurada_coincide_con_el_horario_que_daniela_recita` caza la mitad
  —quien cambie los defaults del código sin tocar el documento—; la otra mitad, entre la fila
  de Neon y ese texto, hoy no la caza nadie.
- **Google Calendar manda también al ESCRIBIR, no solo al ofrecer.**
  `consultar_disponibilidad` respeta los bloqueos desde la fase 3, pero hasta el 13/09/2026
  `crear_cita` y `reprogramar_cita` no los miraban: comprobaban la hora pasada y el cupo de
  Neon, y se iban derechas a `crear_evento`. Dos caminos llegaban ahí con una hora que el
  doctor había apartado —el paciente que pide «las 3» y el modelo que agenda sin consultar
  antes, y el doctor que bloquea DESPUÉS de que Daniela ofreció esa hora, que en WhatsApp
  son minutos—. El guardia es `_bloqueo_que_tapa`, y va **antes de tocar la base**: un cupo
  consumido por una cita que nunca se pudo crear hay que ir a devolverlo. El mensaje no
  nombra el título del evento: es la agenda privada del doctor.
- **El evento lleva el teléfono del paciente, y lo pone el CÓDIGO.** MaxiCare lo pidió el
  13/09/2026: «el nombre, el número de teléfono y por qué agendó, o sea el servicio que está
  interesado». Antes la descripción entera era «Agendado por Daniela. Conversación <uuid>»,
  con la que no se puede llamar a nadie. Ahora `_descripcion_del_evento` arma tres líneas:
  teléfono, servicio y —si el modelo lo escribió— motivo. El número sale de
  `ctx.telefono_completo` y **nunca de la solicitud**: `SolicitudCita` no tiene campo de
  teléfono justamente para que el modelo no pueda escribir otro, y así el número del evento
  es el número desde el que se escribió. `motivo` es opcional a propósito —el «por qué» ya
  lo garantiza `tratamiento`, que siempre llega— y pasa por
  `rechazar_documento_de_identidad`: es texto libre que acaba GUARDADO, y una cédula dictada
  en la conversación no puede terminar apuntada en un evento. Al reprogramar, `mover_evento`
  conserva título y descripción: el motivo NO se actualiza.
- **No hay caché de bloqueos, y es deliberado.** Cada consulta le pregunta a Google, que es
  lo que hace que borrar el evento libere la hora sin sincronizar nada. Los pasos 6b y 6c de
  `probar_calendario.py` existen para cazar a quien meta uno «para bajar la latencia».
- **El socket se muere solo, y hasta el 2/10/2026 no había ni un reintento.** El servicio se
  construye UNA vez al arrancar (`runtime._construir_el_calendario`) y `httplib2` guarda la
  conexión en keep-alive. Tras unos minutos sin tráfico Google la cierra por su lado y la
  primera llamada siguiente muere al escribir. Medido con una paciente delante, conversación
  `156ab45c` (+57 319 661 5042):

  ```
  17:39:17  consultar_disponibilidad  -> OK, se le ofrecen 12:00, 1:00 y 2:00 del sábado
  17:44:xx  «Este sabado a las dos esta bien»
            crear_cita -> _bloqueo_que_tapa -> bloqueos() -> BrokenPipeError
  ```

  Cinco minutos de silencio bastaron. No había `num_retries` en ningún `.execute()` ni nada
  que rehiciera el cliente, así que un hipo de red de milisegundos se convirtió en el turno
  entero perdido. Cuatro cosas de `_con_un_reintento` que no se pueden mover:

  - **Rehace el servicio, no solo repite la llamada.** Comprobado contra la httplib2
    instalada (0.32.0): su `_conn_request` solo reintenta con `ENETUNREACH` y
    `EADDRNOTAVAIL`, y para cualquier otro `socket.error` hace `raise` **sin cerrar la
    conexión**. El socket muerto se queda en su caché con `conn.sock` puesto, así que el
    `if conn.sock is None: conn.connect()` del intento siguiente no reconecta. Reintentar sin
    rehacer es gastar dos veces para fallar igual, y la prueba que lo fija cuenta cuántos
    servicios se construyeron.
  - **`events.insert` se queda FUERA.** Es la única llamada no idempotente del módulo y la
    API no acepta clave de idempotencia: si la petición sí llegó y lo que se perdió fue la
    respuesta, el reintento crea un segundo evento — dos citas sobre la misma hora en el
    calendario del doctor, puestas por el sistema que existe para que eso no pase. Tampoco le
    hace falta: `_bloqueo_que_tapa` acaba de leer por ese mismo socket microsegundos antes.
    El resto sí reintenta porque es idempotente: un `get` y un `list` son lecturas, un `patch`
    con cuerpo fijo deja el evento igual, y un `delete` repetido es el 404 que `YA_NO_EXISTE`
    ya cuenta como éxito.
  - **Un timeout NO está en `SOCKET_MUERTO`, y es la ausencia que importa.** Un
    `BrokenPipeError` dice que la petición no llegó; un timeout no dice nada, así que repetir
    una escritura tras uno puede duplicarla. Tampoco entran un 500 ni una credencial
    caducada: no se arreglan rehaciendo el socket y reintentarlos gasta el minuto del turno.
  - **No traduce nada.** Cada método conserva su propio `except`, y de eso cuelgan las dos
    conductas que leen un código de estado: el 404 que `obtener_evento` lee como «lo borraron»
    —lo que hace que `_sincronizar_con_calendar` se entere— y el mismo 404 que
    `eliminar_evento` cuenta como éxito. Un reintento que lo tradujera todo a
    `ErrorDeCalendario` se llevaría las dos por delante sin dejar nada rojo.

  Lo que esto NO arregla: Calendar caído de verdad. Para eso está el punto siguiente.
- **Si el sondeo previo no se puede hacer, no se agenda — pero el turno ya no muere.**
  `_bloqueo_que_tapa` corre ANTES de tomar el cupo, y `crear_cita` es la única tool con
  `failure_error_function=None`. Las dos cosas juntas significaban que un fallo ahí mataba la
  corrida: `UserError`, `conversacion.responder` dejándolo subir a propósito, mensaje seguro
  al paciente y un `[user]` sin respuesta en `agent_messages` (el trinquete del no negociable
  28). Y el `failure_error_function=None` está justificado por escrito para otra cosa —«cuando
  el cupo **ya está tomado**»—, que aquí todavía no ha pasado: no hay nada apartado, nada que
  liberar y ninguna cita a medias.

  Hoy devuelve un texto del tipo 1, y lo que lo hace seguro son tres cerrojos, no uno:

  | | Qué sostiene |
  |---|---|
  | el texto | «la cita NO quedó agendada: no existe. NO le confirmes esta hora ni ninguna otra» |
  | `sin_hora_no_verificada` | el camino no autoriza NI UNA hora, así que el guardrail bloquea la confirmación (no negociable 13) |
  | `calendario_sin_verificar` | `conversacion.responder` escala por CÓDIGO, no porque el texto lo pida |

  **El segundo es el que no se puede aflojar.** Si alguien le añade un `horas_autorizadas |=`
  «para que Daniela pueda explicarse mejor», el texto pasa a ser lo único que separa al
  paciente de una cita que no existe. Y el tercero existe porque el segundo tiene un hueco
  conocido: si `consultar_disponibilidad` ya autorizó esa hora antes en el MISMO turno, el
  guardrail no distingue «te ofrezco las 2» de «te agendé a las 2». Ahí lo que queda es que un
  humano esté mirando, y eso no puede depender de que el modelo obedezca una frase.

  `reprogramar_cita` no lleva nada de esto: tiene su propia `failure_error_function`, así que
  un fallo ahí ya llegaba al modelo como texto («NO le confirmes ningún horario nuevo: la cita
  sigue como estaba») sin matar la corrida.
- **Para una cita que YA existe, manda Calendar.** Es donde está el doctor que va a atender,
  y mover la cita arrastrándola con el ratón es el gesto natural — nadie va a abrir el panel
  después para repetirlo. El cliente lo hizo el 14/09/2026, movió su cita al día siguiente, y
  Daniela le siguió recitando la hora vieja: la de Neon. Medido entonces contra producción:

  ```
  Neon   dice  16/09 16:00      ← lo que Daniela repetía
  Google dice  17/09 16:00      ← donde la dejó el doctor
  ```

  `herramientas._sincronizar_con_calendar` lo contrasta en cada `consultar_citas` y corrige
  Neon: si se movió, mueve la fila, suelta el cupo viejo y toma el nuevo; si ya no está, la
  cancela y suelta el cupo. **El cupo no es un adorno** — sin moverlo, la hora vieja sigue
  contando como llena y la nueva como libre, y Daniela puede darle a otro paciente una hora
  ya ocupada. Tres cosas que hay que respetar si alguien lo toca:

  - **Un fallo NO es «la borraron».** `ErrorDeCalendario` deja la cita como está en Neon y
    sigue. Tratar un timeout como una cancelación cancelaría citas buenas en silencio, y es
    la razón de que `CalendarioCaido.obtener_evento` lance en vez de devolver `None`.
  - **Se contrasta ANTES de cortar el pasado.** La cita que el doctor arrastró de ayer a
    mañana está en el pasado según Neon; con el corte delante nunca se corregiría. Por eso la
    consulta mira `DIAS_HACIA_ATRAS_AL_SINCRONIZAR` hacia atrás y filtra después, en Python.
  - **Si la hora destino está llena, la cita se mueve igual**, sin reserva y con un
    `log.warning`. El doctor ya decidió meterla ahí; negarle esa realidad a Neon solo
    consigue que Daniela vuelva a mentir.

  El paso 5b de `probar_calendario.py` lo sostiene contra Google de verdad, y la mitad que
  importa no se puede doblar: que un evento borrado devuelva `None` depende de que
  `HttpError` traiga `status_code`. Medido — vivo devuelve su hora, id inventado `None`, y
  recién borrado `None`.
- **Y corregir Neon en silencio no bastaba: la tool tiene que DECIR lo que acaba de cambiar.**
  Esa misma tarde del 14/09/2026 el cliente borró su evento a mano. La cancelación fue
  correcta; la respuesta no: «no me aparece una cita futura registrada. Ya estoy confirmando
  ese punto con el equipo» — escaló. El motivo lo dejó escrito el propio modelo en la fila de
  `escalamientos`:

  > «La consulta actual no muestra citas futuras, **aunque en turnos previos** del mismo chat
  > aparecía una cita de cordales para el 15/09 a las 4:00 pm.»
  > «Confirmar si la cita fue cancelada o si requiere corrección en el sistema.»

  ```
  18:43:48.220   citas.actualizada_en   ← el turno la cancela
  18:43:53.933   escalamientos.creado_en ← el mismo turno le pregunta al doctor si se canceló
  ```

  Cinco segundos. Le preguntó a un humano algo que su propio turno acababa de resolver. **Y
  escalar ahí era lo correcto:** quien ve que el sistema se desdice y no sabe por qué, llama a
  alguien. El arreglo no es enseñarle a callarse — es quitarle la contradicción. Así que
  `_sincronizar_con_calendar` devuelve también las **novedades**, y van delante de la lista:

  ```
  movida    «La clínica movió en su calendario la cita de X: estaba para el <vieja> y ahora
             es el <nueva>. Es un hecho confirmado, no es un error del sistema…»
  borrada   «La clínica eliminó de su calendario la cita de X que estaba para el <vieja>, así
             que acaba de quedar CANCELADA… díselo con naturalidad y ofrécele otro horario.»
  ```

  La **hora vieja va dentro a propósito**: es la que el paciente oyó el turno pasado, y sin
  ella el modelo ve un hueco en vez de una explicación. Solo se escribe cuando hay algo que
  contar — un «tu cita sigue donde estaba» en cada consulta es ruido que acabaría repitiéndole
  al paciente. Y esas horas **quedan autorizadas** igual que la vieja al reprogramar (no
  negociable 13): sin eso, «la cita del 15/09 la eliminó la clínica» dispara
  `sin_hora_no_verificada` y el escalamiento vuelve a entrar por la otra puerta.
- **La credencial es `MAXICARE_GOOGLE_SA_B64`, no una ruta a un archivo.** `config.py`
  decía `MAXICARE_GOOGLE_CREDENTIALS_PATH`, que no existía en ningún `.env`; nadie lo notó
  porque ningún módulo leía ese campo. El nombre correcto es el que documenta `.env.ejemplo`.
- **El 404 casi nunca es el código: es el permiso.** La cuenta de servicio se autentica
  perfectamente aunque no tenga acceso a nada. Hay que compartir el calendario con su
  `client_email` dándole «Hacer cambios en los eventos». `CalendarioGoogle` lo comprueba
  **al construirse**, con una lectura real, y el mensaje nombra el correo.
- **El calendario de la clínica es una cuenta personal de Gmail, y es una decisión tomada
  a conciencia** (MaxiCare, 12/09/2026: «sí va a ser ese correo, no pasa nada»). Lo que
  cuesta: las citas de los pacientes conviven con la agenda personal de esa persona —ya
  hubo un evento suyo borrado a mano durante una prueba— y el día que esa cuenta no esté,
  el calendario se va con ella. La salida, si algún día deja de ser aceptable, es barata:
  crear un calendario secundario, compartirlo con la misma cuenta de servicio y cambiar
  `MAXICARE_GOOGLE_CALENDAR_ID`. Ni una línea de código cambia.
- **El chat web sigue con `CalendarioDoble` a propósito.** Probar en la pestaña Pruebas
  crearía eventos falsos en el calendario donde los doctores miran su día —el mismo error
  de categoría que `public` vs `pruebas_web`—. En WhatsApp sí entra el de verdad:
  `calendario_desde_config`, construido una vez al arrancar `runtime.py`.
- `ZONA_BOGOTA` vive en `calendario.py` y `herramientas.py` la reexporta. Una sola
  definición: dos copias de un desfase horario son dos cosas que un día divergen.

Cuando Calendar «no funciona», lo primero es `uv run python scripts/probar_calendario.py
--diagnosticar`: solo lee, comprueba el acceso y lista los bloqueos de los próximos 14 días.

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

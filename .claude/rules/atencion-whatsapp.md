---
paths:
  - "src/maxicare_daniela/atencion.py"
  - "src/maxicare_daniela/runtime.py"
  - "src/maxicare_daniela/ingesta.py"
  - "src/maxicare_daniela/conversacion.py"
  - "src/maxicare_daniela/persistencia.py"
  - "src/maxicare_daniela/reseteo.py"
  - "tests/test_atencion.py"
  - "tests/test_webhook_responde.py"
  - "tests/test_reseteo.py"
  - "tests/test_reseteo_cable.py"
  - "tests/test_reseteo_neon.py"
  - "scripts/probar_atencion.py"
---

# El turno de WhatsApp

Las reglas están en una línea cada una en el `CLAUDE.md` raíz, porque son las que no pueden
perderse en una compactación. Aquí está lo que cada una costó averiguar — casi todo medido
en producción o por mutación, no razonado.

## La conversación

- **`asegurar_conversacion` SIEMPRE inserta una fila nueva**, pese al nombre: no es un
  get-or-create. En el camino de WhatsApp se usa `conversacion_viva`, que reutiliza la de las
  últimas 24 h. Usar la primera aquí abriría una conversación por mensaje: Daniela no
  recordaría ni la frase anterior, `turno_actual` sería siempre 1 y las claves de
  idempotencia (`id_conversacion + turno`) no colisionarían nunca, con lo que dejarían de
  proteger.
- **El historial YA NO vive en memoria, en ningún carril.** Desde la fase 7,
  `atencion.atender` le pide la sesión a `persistencia.sesion_de_agente`
  (`SQLAlchemySession`, sobre `agent_messages` en Neon) por omisión, así que un reinicio del
  proceso ya no borra el hilo del diálogo de un paciente de WhatsApp -- igual que ya no
  borraba los datos: paciente, citas y estado de oportunidad. Desde la Tarea 6, el chat web
  del panel usa la misma fábrica (`runtime._contexto_de_prueba`), apuntada con
  `esquema="pruebas_web"` -- `config.database_url` sin el `options=-csearch_path=` que sí
  lleva la conexión síncrona del carril, para que el aislamiento lo dé
  `schema_translate_map` y no el `search_path`, igual que en el resto de esta fase (ver
  `tests/test_sesion_neon.py::_sin_options`). `conversacion.SesionEnMemoria` queda como el
  doble de las pruebas offline, y ya no corre en ningún carril real.

## El candado

- **Va por TELÉFONO, no por conversación, y `_leer_estado` va DENTRO.** El teléfono se
  conoce desde el mensaje y la conversación no, así que un candado por conversación obliga a
  leer la base antes de cerrarlo. Con esa lectura fuera se midió lo siguiente: dos mensajes
  simultáneos de un número nuevo abren **dos conversaciones** —Daniela contesta dos veces sin
  saber de la otra mitad, y del tercer mensaje en adelante una de las dos se pierde—, y dos
  de una conversación existente leen el **mismo `turno_actual`**, así que arman la misma
  clave de escalamiento y el doctor se entera de uno solo. Si alguien mueve `_leer_estado`
  fuera del candado «para que el candado dure menos», vuelven los dos.
- **Es de proceso.** Con más de un worker o más de una réplica deja de proteger y haría
  falta un `pg_advisory_lock`; la clave natural ya es el teléfono, que se conoce sin tocar la
  base. El contenedor corre con un solo worker, y por eso hoy alcanza.

## El búfer de mensajes

En WhatsApp nadie escribe párrafos: el saludo va en un mensaje, la pregunta en otro y lo que
se le ocurrió después en un tercero. Sin agrupar, cada trozo abría su turno y su respuesta.
Medido con el primer paciente real de la clínica: tres mensajes en 48 s, tres respuestas, y
siete segundos entre las dos últimas. `atencion` acumula en `_buferes` los mensajes de un
número y espera `VENTANA_SILENCIO_SEGUNDOS` (20) sin mensajes nuevos, con tope de
`TOPE_BUFER_SEGUNDOS` (45) contado desde el primero.

- **La ventana va ANTES del candado del turno.** Si el segundo mensaje tuviera que esperar
  ese candado, no podría sumarse al grupo hasta que terminara el turno del primero — es
  decir, hasta después de la respuesta que se quería evitar.
- **El bloque que mete el mensaje en el búfer no tiene un solo `await`, y por eso no lleva
  candado**: en un único bucle de eventos eso lo vuelve atómico por construcción. Añadir un
  `await` ahí reintroduce la carrera y nada lo delataría.
- **El retardo se descuenta, no se suma.** `momento_inicio` es la llegada del PRIMER mensaje
  del grupo. Reiniciarlo después del búfer saca la respuesta del minuto que fija
  `limites.latencia_maxima`. Hay prueba, y cae al mutarlo.
- **El búfer se saca SIEMPRE en un `finally`.** Uno que sobreviviera a su turno se tragaría
  todos los mensajes siguientes de ese número: cada uno se sumaría a un grupo que ya no
  espera a nadie. En silencio, y solo para ese teléfono.
- **Las banderas del turno salen del GRUPO, no del último mensaje.** Mandar la radiografía y
  escribir «¿esto qué es?» justo después son dos mensajes: leyendo solo uno,
  `sin_lectura_clinica` se queda sin nada que vigilar en el turno que sí habla de la imagen.

## Idempotencia y reintentos

- **Meta reintenta los webhooks, y hay DOS deduplicaciones, no una.** `procesar_mensaje`
  protege el reenvío al doctor con `ON CONFLICT (wamid)`; el turno de Daniela lo protege
  `_entregar` mirando `Resultado.nuevo`. Sin lo segundo, el mismo POST tres veces daba **un
  reenvío y tres turnos**: el paciente recibía la misma pregunta contestada tres veces con
  tres textos distintos. Si `procesar_mensaje` revienta antes de devolver nada, se atiende
  igual — un fallo de Telegram no puede dejar al paciente sin respuesta.
- **Ninguna clave de idempotencia la escribe el modelo.** Las cuatro (`cita`, `reprogramar`,
  `seguimiento`, `escalamiento`) las arma el código con `ctx.clave(...)`. `crear_cita` fue
  la última en caer: con la clave del modelo, dos pacientes distintos pidiendo el mismo
  bloque generaban la misma cadena y **ambos salían confirmados sobre un solo cupo**.

## El aviso de la política — se marca DESPUÉS, y la 21 dice lo contrario

Es la trampa más fácil de «corregir» de todo el módulo, porque contradice de frente a un no
negociable que está dos líneas más arriba en el mismo archivo. Las dos son correctas: lo que
cambia es qué se pierde al fallar.

- **El aviso se marca DESPUÉS de que `enviar_texto` vuelva bien. Un recordatorio se marca
  ANTES (no negociable 21).** En el recordatorio el riesgo es mandar el mismo mensaje dos
  veces: marcar después significa que un fallo entre el envío y el marcado lo repite sesenta
  segundos más tarde, y ninguna de las NUEVE guardas por las que pasa un recordatorio de cita
  lo detecta -- todas siguen diciendo que sí. (Eran siete cuando se escribió esto; el número
  vive en el docstring de `seguimientos.decidir`, que además cuenta las trece de una
  reactivación.) En el aviso el riesgo es el
  contrario: marcar antes deja en `consentimientos` la constancia de un aviso que un
  `ReadTimeout` impidió que saliera, y **esa constancia es precisamente la prueba legal**.
  Repetir un aviso es inocuo; falsificar una prueba, no. Si el marcado falla, el turno sigue
  y el aviso se vuelve a enseñar en el mensaje siguiente: el `try` interno lo traga y solo
  deja un `log.exception`.
- **Sale UNA vez en la vida del número, no una por conversación.** El dato viene de
  `contactos.aviso_mostrado_en`, que no caduca a las 24 h como `conversaciones`: un paciente
  que escribe cada semana no ve el aviso legal cada semana. Lo decide `_toca_avisar`, que es
  una función y no un `if` suelto porque sus dos condiciones **son** la política.
- **Con `politica_datos_url` en `PENDIENTE` el aviso no se emite, y por eso la frase vuelve al
  prompt condicionada a ese mismo `PENDIENTE`.** Las dos mitades son correctas por separado y
  juntas dejaban un hueco en el que nadie informaba de la política ni una vez: menos cobertura
  que antes de que el código emitiera nada. El valor viaja en
  `ContextoDaniela.politica_datos_url` (default `PENDIENTE`, el lado que avisa) y lo único
  que lo lee es `agentes.instrucciones_daniela`.
- **Desde el 16/09/2026 la URL existe, así que en producción manda el código y el bloque del
  prompt ya no sale.** Vive en `config.POLITICA_DATOS_URL`, en el código y no en el `.env`, y
  es la única URL del proyecto que lo hace: es un documento público, el día que cambie tiene
  que quedar en `git log`, y una variable olvidada en un despliegue no puede significar dejar
  de informar. El `.env` la sigue pudiendo pisar. Ojo con la asimetría de `_opcional`:
  **vacía cae al valor del código y NO apaga nada**; lo único que apaga es el literal
  `PENDIENTE`.
- **`config_falso()` de `tests/test_atencion.py` la blanquea a propósito**, igual que las
  credenciales de Google y que `scripts/probar_atencion.py`. Sin eso, el pie del aviso se le
  pega a la respuesta de todas las pruebas de extremo a extremo de ese archivo y de
  `test_muro.py` —que no van de eso— y vuelven a romperse el día que la URL cambie. Las tres
  que sí prueban el aviso pasan la URL explícitamente.
- **Lo que el código NO puede sostener solo: que el documento detrás del enlace siga siendo el
  que la gente aceptó.** El destino es Drive, y Drive deja subir una versión nueva sobre el
  mismo archivo sin que el enlace cambie. Por eso cada `politica_version` tiene su PDF
  congelado con su SHA-256 en `docs/politica/`: es lo que convierte la fila de
  `consentimientos` en algo contrastable. Ninguna prueba lo caza, y el comando para
  comprobarlo está en el README de esa carpeta.
- **El relevo NO lleva aviso, y es un hueco conocido.** `relevo.py` escribe al paciente por su
  cuenta (`whatsapp.enviar_texto` directo), así que un número cuyo primerísimo contacto lo
  releve el doctor conversa sin verlo. Se cura solo —el aviso no se marca hasta que sale, así
  que sale en cuanto Daniela vuelva a contestar— y por eso no se tocó el relevo, que es el
  subsistema más frágil del proyecto. Si alguna vez deja de curarse solo (por ejemplo, si el
  relevo pasara a poder cerrar una conversación entera sin devolvérsela a Daniela), hay que
  cerrarlo ahí.
- **Lo que prueba el puente es una prueba de Neon, no la suite offline.**
  `atencion._marcar_aviso` son tres líneas que las pruebas de extremo a extremo sustituyen con
  `monkeypatch` y que `scripts/probar_atencion.py` apaga con `politica_datos_url=""`. Es el
  único eslabón entre el envío que salió bien y la fila que se enseña si alguien reclama:
  `test_contacto_neon.py::test_marcar_aviso_de_atencion_escribe_la_fila_de_verdad` lo llama de
  verdad, con la URL del esquema de pruebas — abre su propia conexión, así que con la URL de
  producción escribiría en `public`.

## El calendario y el interruptor

- **Si el calendario no arranca, Daniela queda con `CalendarioCaido`, nunca con
  `CalendarioDoble`**, y la diferencia es la razón de ser del proyecto. El doble dice que sí
  a todo: `crear_cita` tomaría el cupo, «crearía» el evento en un diccionario y le
  confirmaría la cita al paciente, que llegaría a una clínica donde nadie lo espera. El caído
  lanza `ErrorDeCalendario`, y las tools ya saben qué hacer con eso: liberar el cupo, no
  confirmar nada y escalar. `/salud` publica qué clase acabó ahí.
- **`MAXICARE_DANIELA_RESPONDE`**: por defecto activa (cualquier valor que no sea `0`). Con
  `0`, el webhook sigue registrando el mensaje y reenviando el archivo a Telegram exactamente
  como antes, y lo único que se apaga es la respuesta al paciente. Existe para poder callarla
  en diez segundos sin desplegar código.

## El muro

La fase 6B le añadió un archivo al turno de WhatsApp: cuando llega una radiografía o una
remisión, un lector automático la lee y el contenido clínico va SOLO a los doctores por
Telegram, mientras Daniela recibe únicamente lo no clínico. Seis cosas de ese camino no se
pueden mover.

- **El reparto es un TIPO sin el campo, no una instrucción que alguien podría desobedecer.**
  `lectura.repartir` parte lo que devolvió el lector en dos: `contexto_clinico` (texto libre,
  destino Telegram) y `LecturaNoClinica` (`tipo_documento`, `tratamiento`, `origen`,
  `fecha_documento`, `confianza` — sin `contexto_clinico`). Esa ausencia no es que el código
  se acuerde de no copiarlo: es que la clase no tiene dónde ponerlo, y lleva
  `extra="forbid"` para que ni un `**lectura.model_dump()` a medio pensar lo cuele como
  atributo extra. `atencion._entrada_para_el_modelo` solo puede leer de `LecturaNoClinica`
  porque es literalmente lo único que le llega: `leer_y_repartir` nunca devuelve la lectura
  entera, sea cual sea el prompt del lector ese día.

- **El archivo nunca espera al modelo, y el orden dentro de `procesar_mensaje` es este:**
  descargar → tema (con tope) → **entregar el archivo** → arrancar el lector → avisar al
  General. El archivo va PRIMERO; el `asyncio.create_task` del lector va inmediatamente
  después, en cuanto el archivo ya está entregado; y el aviso al General es lo ÚLTIMO y va en
  su propio `try/except` porque es degradación, no entrega. El lector va antes que el aviso a
  propósito: un aviso que revienta no puede quitarle al doctor su lectura clínica, y a esas
  alturas el archivo ya está depositado, así que nada de lo que siga puede convertir esa
  entrega en un fallo. Es la garantía de la fase 2 (nada de lo que se escriba puede retrasar
  la entrega al doctor), y sigue vigente: si el lector se cayera, se colgara o tardara un
  minuto, el doctor ya tiene el archivo en su tema desde antes de que la tarea existiera.
  (Una versión anterior de esta regla decía que el aviso iba antes del lector y que el
  `create_task` era lo último de la función. Las dos afirmaciones eran falsas y el código
  nunca fue así; lo sostienen
  `test_ingesta.py::test_el_aviso_al_general_no_invalida_una_entrega_que_ya_ocurrio` y
  `::test_el_lector_no_retrasa_la_entrega_del_archivo`.)

- **Buscar o crear el tema también tiene reloj: `lectura.TOPE_SEGUNDOS_TEMA` (5 s).**
  `asegurar_tema` encadena `crear_tema` y `cerrar_tema` en el primer archivo de un paciente,
  cada una con `TIMEOUT_NORMAL` (15 s): sin tope, con Telegram lento la radiografía esperaba
  medio minuto delante del doctor, y el candado por teléfono se lo sumaba al segundo archivo
  del mismo número. Vencido el tope, el archivo va al General. La espera va con
  `wait_for(shield(tarea))` y no con un `wait_for` pelado: **cancelar la operación a mitad de
  `crear_tema` dejaría un tema huérfano en el grupo** —Telegram ya lo creó y nadie lo
  guardó—, así que la tarea sigue por detrás (`ingesta._temas_en_curso` la sostiene) y el
  hilo queda listo para el próximo archivo de esa persona.

- **La ingesta NO crea filas en `pacientes` — pero desde la 014 un desconocido SÍ abre tema.**
  Las dos mitades son independientes y conviene no confundirlas. La que no cambia: la ingesta
  nunca escribe en `pacientes`, por seguridad clínica y no por orden. `atencion._leer_estado`
  deriva `identidad_verificada` de la EXISTENCIA de esa fila, y `runtime._entregar` corre
  `procesar_mensaje` antes que `atender`. Con la ingesta creando la fila —con el
  `nombre_perfil` que el propio desconocido escribió—, mandar una foto **verificaba a un
  desconocido en ese mismo turno**: `revisar_identidad` y el `tool_input_guardrail`
  `identidad_antes_de_datos` quedaban desactivados para él, y `_mismo_nombre` comparaba
  después el nombre que él decía contra el nombre que él mismo había puesto. Eso sigue igual.
  La que cambió: mientras el hilo vivía en `pacientes.telegram_topic_id`, no crear la fila
  significaba no poder abrir tema, y `asegurar_tema` devolvía `None` para un desconocido —su
  archivo al General—. La 014 movió el hilo a `temas_telegram`, que cuelga del TELÉFONO y no
  de la ficha, así que **`asegurar_tema` ya no exige ficha y sigue sin crear ninguna**: un
  lead tiene hilo desde su primer archivo sin quedar verificado por ello. El detalle de la
  014, en `.claude/rules/relevo-telegram.md`.

- **El lector corre con `run_config`, y `trace_include_sensitive_data` va en `False`.** Es la
  CUARTA salida del muro y la única que no se ve: `RunConfig()` nace con ese campo en `True`
  en la 0.22.2, así que sin pasarlo el data URL entero de la radiografía y el
  `LecturaArchivo` completo —`contexto_clinico` incluido— subían a los traces de OpenAI, que
  se exportan fuera de la clínica. `config.TRACE_INCLUDE_SENSITIVE_DATA` existía desde la
  fase 1 y no la cableaba nadie. Con `False` los spans se siguen creando (latencia, coste,
  errores) y solo se omiten entradas y salidas. **CERRADO el 13/09/2026 en los tres sitios**
  — ver «El tracing» abajo. Este fue el primero.

- **El lector corre en PARALELO con la ventana del búfer, nunca delante.** La tarea nace en
  `procesar_mensaje` y viaja como `Resultado.lectura`; `atencion.atender` no la espera al
  recibirla, la guarda en `bufer.lecturas[wamid]` y solo la recoge (`_recoger_lecturas`,
  `asyncio.shield` con un margen corto) DESPUÉS de que la ventana de silencio cierra.
  Encadenarla delante sumaría los 4-8 s que tarda el lector a los 20 s de la ventana, y
  contra `limites.latencia_maxima` (un minuto) no cabe: medido el 12/09, un documento recibido
  a las 19:03:28 se entregó después de un texto recibido a las 19:03:29 — si el lector hubiera
  ido delante, Daniela habría contestado el texto sin saber todavía que había una foto. Lo que
  no llegue a tiempo se descarta con un `log.info`, nunca con una excepción que tumbe el turno.

## Las notas de voz: el segundo carril, y no es el muro

Desde el 21/09/2026 una nota de voz se transcribe y entra al turno **como texto del paciente**.
Antes no: `audio` estaba en `TIPOS_CON_ARCHIVO` y no en `TIPOS_QUE_SE_LEEN`, así que a Daniela
le llegaba «nadie lo ha revisado y tú no puedes verlo» y ella hacía lo que esa frase pide.
Medido: las cinco notas de voz que vio el sistema en su vida cerraron con
`motivo = 'archivo_recibido'` y «Ya recibimos tu audio.»

```
procesar_mensaje ─┬─ descarga  ──→ Telegram del doctor       ← INTOCABLE (fase 2)
                  ├─ Task lector        image/document  ──→ margen 3 s
                  └─ Task transcriptor  audio/voice     ──→ margen 8 s
```

- **Es un carril aparte, no el del lector reaprovechado**, y la diferencia es de qué promete
  cada uno. Una lectura es información DE MÁS: si no llega, el doctor la recibe igual por su
  lado y Daniela dice con razón que no sabe qué contiene. Una transcripción **es el mensaje
  del paciente**: si no llega, hay alguien preguntando algo a quien nadie contestó. De ahí
  salen los dos márgenes distintos y los dos diccionarios distintos en `_Bufer`.
- **Esto NO es una grieta en el muro de `lectura.py`.** El muro existe porque del análisis de
  una radiografía sale contenido clínico que el paciente no puede recibir; de una nota de voz
  salen las palabras que el propio paciente acaba de decir. Por eso `transcribir_y_repartir`
  no reparte nada: **lo que ve el doctor y lo que ve Daniela son lo mismo**, y esconderle a
  Daniela la mitad de lo que le dijeron no protegería a nadie.
- **Los dos carriles son DISJUNTOS**, y de eso depende que `atender` pueda esperar los dos
  plazos uno detrás de otro sin que cueste nada: uno de los dos diccionarios está siempre
  vacío. Hay prueba (`test_los_dos_carriles_son_DISJUNTOS`).
- **`.oga` es un `400` de la API, y es lo que produce el proyecto.**
  `canales._nombre_sugerido` usa `mimetypes.guess_extension("audio/ogg")`, que devuelve
  `.oga`; la API contesta `Unsupported file format oga` en el 100 % de los casos. El nombre se
  fuerza a `.ogg` en `transcripcion._nombre_para_la_api`, o sea en el borde que habla con la
  API y no en el helper de Telegram, que existe para otra cosa. Pasarle `archivo.nombre` —lo
  natural, y lo que parece más correcto— no transcribe ni un audio **y no deja nada rojo en
  ningún sitio**: el error se lo traga `transcribir` y Daniela vuelve a «no te entendí».
  `test_el_nombre_que_se_le_manda_a_la_API_NUNCA_es_el_del_archivo` es lo único que lo impide.
- **Sin transcripción se pide por escrito y NO se escala.** El paciente lo resuelve en un
  segundo y el audio ya está en el hilo del doctor: avisarle sería interrumpirlo por algo que
  ya tiene delante. El `archivo_recibido` de `contratos.MotivoEscalamiento` sigue vivo y lo
  sigue usando la radiografía.
- **La transcripción alimenta `menciona_sintomas`, y esa es la costura clínica.** `m.texto` es
  `None` en un audio, así que «me duele muchísimo y me sangra la encía» DICHO en voz alta daba
  `False` y el prefiltro de `sin_lectura_clinica` quedaba colgando solo de `hubo_adjunto` —que
  hoy lo salva por accidente, porque un audio es un archivo—. Van **las dos**: la transcripción
  entra en la comprobación y `adjunto_del_mensaje` se queda en `True`. Estrictamente más
  vigilante que antes, nunca menos.
- **Y cuenta como su frase para el informe.** `frase_para_el_informe` leía `m.texto`; sin
  esto, todas las notas de voz se agruparían bajo un caso sin frase y se perdería justo la
  pregunta que Daniela no supo contestar, solo porque el paciente la dijo en vez de escribirla.
- **Una nota de voz ENTENDIDA no timbra en el General, y eso lo decide el transcriptor.**
  El aviso «📎 fulano mandó archivos» existe para que un humano ABRA el archivo: una
  radiografía hay que mirarla. Una nota de voz que Daniela entendió y contestó no le pide
  nada a nadie, y la regla de la sección de abajo es que al General solo va lo que le pide
  algo al doctor. MaxiCare lo vio el 22/09/2026: mandó una nota de voz, Daniela la contestó
  bien —`requiere_escalamiento: false`, cero filas en `escalamientos`— y el General timbró
  igual, con el botón «Hablar yo con el paciente» debajo. Lo leyó como un escalamiento, y
  desde fuera es indistinguible de uno.
  **Si NO se entendió, sí timbra, y el texto dice por qué**: «mandó una nota de voz que no se
  pudo entender», porque ahí la acción del doctor es OÍRLA, que no es la misma que mirar una
  radiografía. Y **sin transcriptor —interruptor, cuota, audio enorme— timbra como siempre**:
  nadie va a entender ese audio si no lo oye una persona. La decisión vive dentro de
  `_transcribir_con_grupo` porque solo ahí se sabe si hubo texto; `_primer_archivo_de_la_tanda`
  da lo mismo desde los dos sitios gracias a su `wamid <>`, que está puesto justo para eso.
- **Y se GUARDA, desde la migración 027** (`mensajes_entrantes.transcripcion`). Hasta
  entonces el texto se le pasaba a Daniela, sonaba en el hilo del doctor y se tiraba: la
  columna `texto` de un audio se quedaba en NULL para siempre. Dos consecuencias, y la
  segunda es la grave. En el panel, `panel._MARCA_POR_TIPO` pintaba la cadena literal
  «(nota de voz)» sobre algo que el sistema sí había entendido —los SEIS audios que vio el
  sistema en su vida estaban así—. Y en `persistencia.transcripcion` —el volcado que recibe
  un doctor al TOMAR la conversación— el filtro `texto IS NOT NULL AND texto <> ''` se las
  comía **enteras**: ni siquiera salían como «(nota de voz)», así que el doctor entraba a
  conversar sin saber que el paciente había hablado. Ahí el destinatario es un humano
  decidiendo, que es el peor sitio para un hueco silencioso. Hoy las dos consultas van con
  `coalesce(texto, transcripcion)`, y **el orden importa**: lo que el paciente ESCRIBIÓ manda
  sobre lo que una máquina entendió que dijo. La escritura vive en
  `ingesta._guardar_transcripcion`, se llama DESPUÉS de repartir —a esa altura el audio ya
  está entregado, la transcripción ya sonó y el turno ya va en camino— y se traga sus propios
  fallos, igual que `consumo.anotar`: lo que se puede perder aquí es una fila, nunca una
  respuesta. **No se escribe la cadena vacía**: `panel.hilo` la leería como «sí se
  transcribió» y pintaría una burbuja en blanco marcada como transcripción, que es afirmar
  que el paciente dijo algo sin decir el qué.
- **El barrido de arranque las degrada, y está dicho en el código.** Los bytes del audio viven
  en memoria, así que un proceso que muere se los lleva; el rescate no vuelve a canjear el
  `media_id` contra Meta. El paciente rescatado recibe «no te entendí, ¿me lo escribes?». Es
  una degradación, no una pérdida: él puede resolverlo, no se escala, y el doctor ya tiene el
  audio y su texto.

## Al General solo va lo que le pide algo al doctor

El General era un vertedero: cada texto que entraba sonaba ahí. Con un solo paciente activo
ya era ruido, y el ruido en el canal de avisos se traduce en avisos que nadie mira.

La regla es por DESTINATARIO, no por tipo de mensaje: **al General va lo que le pide algo al
doctor**. Un texto normal no le pide nada, así que va **mudo al tema de su paciente** —o a
ninguna parte, si ese número todavía no tiene tema—. Y **nunca lo crea**: abrir hilo es del
primer ARCHIVO, no de un texto, porque el hilo existe para colgar lo que el doctor tiene que
ver. El aviso de archivos sí suena, pero **UNA vez por tanda**: lo decide
`_primer_archivo_de_la_tanda` con una ventana de 24 h, para que una serie de seis
radiografías seguidas no sean seis pitidos.

**Esto INVIERTE una columna, y es la trampa de esta sección.** La migración 004 escribió que
`telegram_message_id` NULL con `fallo` NULL significa «entró y no llegó a nadie» — una
alarma. Después de este cambio ese estado pasó a ser **lo normal**: es exactamente lo que
deja un texto que va mudo al tema de su paciente. Quien lo lea como antes verá una avería
permanente donde no hay ninguna.

**La señal de alarma es `reenviado_en` NULL**, que además es sobre lo que ya estaba
construido `ix_mensajes_sin_reenviar`. El índice no hubo que tocarlo; lo que había que
cambiar era qué se le pregunta.

El SQL de las dos consultas nuevas **solo lo ejercita `tests/test_ingesta_neon.py`, con
`-m neon`**. Offline van dobladas y degradan en silencio: `uv run pytest -q` a secas se queda
verde con una consulta rota. Quien las toque corre
`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`.

## `/clearstate` — resetear un número a primer contacto

Un número listado en `MAXICARE_TELEFONOS_PRUEBA` puede escribir `/clearstate` y quedar, para
Daniela, como uno que nunca ha escrito. Existe para poder probar conversaciones enteras sin
estrenar una línea de teléfono. Vive en `reseteo.py`, y `runtime._entregar` lo intercepta.

- **La garantía es una igualdad, no una promesa:** tras el reseteo, `atencion._leer_estado`
  devuelve para ese teléfono los mismos ocho campos que para un número virgen — solo cambia
  `id_conversacion`, que es un UUID nuevo por definición. Lo sostiene
  `test_reseteo_neon.py::test_tras_el_reset_daniela_ve_lo_mismo_que_en_un_primer_contacto`,
  con su control: antes del borrado, esa misma prueba comprueba que Daniela SÍ lo conocía.
  El historial del diálogo **también se borra, desde la Tarea 8.** Desde la fase 7 vive en
  `agent_messages`/`agent_sessions`, en Neon, y `persistencia.borrar_rastro` lo borra dentro
  de la MISMA transacción que el resto del rastro -- el `DELETE FROM agent_sessions` va
  ANTES que `DELETE FROM conversaciones`, porque `session_id` ES el `id` de esas
  conversaciones (la migración 010 lo declara sin clave foránea a propósito, así que no hay
  CASCADE que salve el orden contrario): al revés, la subconsulta no encontraría a qué
  apuntar y el historial quedaría huérfano e inalcanzable, con la agravante de que el
  borrado parecería haber funcionado. `agent_messages` no se borra a mano: se va sola por su
  propio `ON DELETE CASCADE`. Lo único de memoria del proceso que hay que sacar aparte es el
  búfer, y de eso se encarga `atencion.olvidar`.
- **La excepción, y es el punto entero de la migración 019: `/clearstate` resetea el aviso y
  NUNCA la baja.** `contactos.aviso_mostrado_en` y `politica_version` vuelven a NULL —lo
  volverá a ver, que es lo correcto en un reseteo—, pero `no_contactar` no se toca y **la fila
  no se borra jamás**. Si se fuera, resetear a alguien lo devolvería a la lista de
  contactables sin que nadie se enterara, y ese «no» es del paciente, no del sistema. La
  bitácora `consentimientos` no se toca en ningún caso, y el `ON DELETE RESTRICT` de su FK lo
  hace imposible aunque alguien lo intente; el borrado se anota ahí como `rastro_borrado`, con
  un `WHERE EXISTS` porque un `/clearstate` sobre un número que nunca escribió violaría la FK
  y tumbaría la transacción entera. Precedente exacto: los ejemplos de casos sin resolver, que
  se borran **sin** bajar el contador (no negociable 22). Y ese reseteo **no** entra en el
  dict que devuelve `borrar_rastro`: contarlo como fila borrada dejaría inalcanzable la rama
  «no había nada que borrar» de `reseteo.confirmacion` en cuanto todo número conocido tuviera
  fila en `contactos`, y el paciente oiría «borré todo lo tuyo» sobre un número ya limpio.
- **La lista vacía apaga el comando para todo el mundo, y ese es el default.** Sin números
  listados, `/clearstate` llega a Daniela como cualquier otro texto. Dos pruebas lo vigilan
  (`test_reseteo_cable.py`), y las dos caen al mutar la condición de `_entregar`: sin ellas,
  un error ahí convertiría el borrado en algo disponible para cualquier paciente.
- **La interceptación va DESPUÉS de la deduplicación por `wamid` y ANTES de `atender`.**
  Después, porque un reintento de Meta no puede borrar dos veces. Antes, porque el turno
  escribiría sobre la conversación recién borrada. Y como `procesar_mensaje` ya corrió, el
  comando queda reenviado a Telegram: un borrado irreversible que deja rastro visible para
  los doctores es mejor que uno silencioso.
- **El orden es lo externo primero, y Calendar ABORTA mientras Telegram degrada.** Mientras
  las filas sigan en la base, un borrado a medias se puede reintentar; al revés no, porque el
  `evento_calendar_id` deja de existir y el evento se queda ocupando un cupo real de la
  clínica que ya nadie puede cancelar desde el sistema — un paciente de verdad que no puede
  agendar a esa hora. Un tema huérfano en Telegram, en cambio, es ruido y no daño. La
  asimetría la decide el principio del proyecto, y la vigila
  `test_si_calendar_falla_no_se_borra_ni_una_fila`.
- **El orden de los DELETE lo impone la base, no el gusto:** `citas`, `reservas` y
  `mensajes_entrantes` apuntan a `conversaciones` sin `ON DELETE CASCADE`, y
  `conversaciones.paciente_id` a `pacientes` igual. Empezar por el paciente falla con un
  error de integridad y no borra nada.
- **Se conserva la fila de `mensajes_entrantes` del propio comando**, con `conversacion_id`
  en NULL. Si se borrara, el reintento de Meta ejecutaría el comando otra vez. No vuelve
  conocido a nadie: `_leer_estado` no mira esa tabla.
- **También se limpia `pruebas_web`**, que tiene las mismas tablas que `public` y no se purga
  nunca. Sin eso, un número probado desde el chat del panel seguiría conocido por esa mitad.
  **Ese borrado secundario exige que `pruebas_web` esté al día, y hasta el 13/09/2026 no lo
  estaba:** su única puesta al día era `runtime._preparar_esquema_de_pruebas`, que es
  perezosa —corre cuando alguien abre el chat web— y nadie lo había abierto desde las
  migraciones 009 y 010. Medido contra la base real: le faltaban `agent_sessions`,
  `agent_messages` y cuatro columnas de `mensajes_entrantes`, y `borrar_rastro` reventaba ahí
  con `UndefinedColumn` mientras este documento afirmaba que funcionaba. Ahora lo pone al día
  `scripts/inicializar_base.py`, que es lo que corre `desplegar.sh` en cada despliegue, y su
  verificación comprueba las dos tablas del historial **en los dos esquemas**. Comprobado
  después del arreglo: `borrar_rastro` sobre `pruebas_web` devuelve ceros en vez de reventar.
- **Lo que NO borra, y está dicho en el código:** las trazas que ya se subieron a OpenAI
  entre la fase 6A y el 13/09/2026 —la fuga está cerrada desde entonces, pero lo que salió
  vive en el dashboard de otra empresa, no en la base— y los logs del contenedor. Ninguna de
  las dos afecta al comportamiento de Daniela; el rastro existe.
- **Telegram necesita `can_delete_messages`**, que `obtener_chat_telegram.py` no comprueba
  hoy — solo mira `can_manage_topics`. Si falta, el tema no se borra, se informa en la
  confirmación y el resto del reseteo sigue.
- **El mismo comando funciona en el chat web del panel, y ahí NO exige la lista blanca.** La
  diferencia es deliberada: el «teléfono» de ese carril es `web-<usuario>`, una cadena que no
  existe ni puede existir en `public`; la conexión apunta con `search_path` a `pruebas_web`; y
  para llegar hace falta sesión abierta en el panel. Cada persona de la clínica borra su
  propio carril y solo el suyo (`test_el_chat_web_no_le_corta_el_hilo_a_otra_persona`). El
  endpoint devuelve `conversacion: null`, que es lo que hace que el turno siguiente abra una
  conversación nueva en vez de pedir un id que acaba de borrarse.
- **`/api/pruebas/reiniciar` no es esto y sigue como estaba:** olvida la conversación en
  memoria y DEJA las filas en `pruebas_web` — a propósito, para que quede rastro de qué se
  probó. Por eso el botón «reiniciar» no te devuelve a primer contacto: el paciente que te
  inventaste sigue en la tabla y el turno siguiente te reconoce. Para eso está `/clearstate`.

## La regeneración: el freno que se frenaba a sí mismo

Cuando un guardrail de salida salta, `conversacion.responder` regenera UNA vez mandándole al
modelo el texto `CORRECCION`. Dos cosas de ese camino estuvieron rotas hasta el 13/09/2026, y
juntas producían el síntoma que la clínica describió como **«Daniela no agenda»**.

- **La corrección pasaba por `uso_indebido`, que es un guardrail de ENTRADA.** `CORRECCION`
  empieza con «AVISO DEL SISTEMA» y le reescribe la conducta a Daniela: es, palabra por
  palabra, la forma de una inyección de prompt. El evaluador la clasificaba como ataque
  —medido contra el modelo real, dos veces de dos: «Intenta imponer instrucciones del sistema
  y modificar el comportamiento de la asistente»—, así que el segundo tripwire estaba
  **garantizado** y toda regeneración acababa en mensaje seguro más escalamiento. El
  reintento existía en el código y no llegaba a correr nunca. Cuanto mejor hacía su trabajo el
  guardrail de salida, más seguido pasaba. Hoy la regeneración corre sobre
  `agente.clone(input_guardrails=[])`; los de SALIDA se conservan los tres.
- **Un tripwire de ENTRADA ya no se regenera.** Salta antes de que el modelo responda, así
  que «tu respuesta anterior fue bloqueada» sería falso y el segundo intento se le pediría
  sobre un mensaje que acabamos de clasificar como ataque. Mensaje seguro y escalamiento
  directos: mismo destino que antes, una llamada al modelo menos. Lo sostiene
  `test_un_tripwire_de_entrada_no_se_regenera_nunca`, y **es esa prueba la que ahora impide
  que quitar el guardrail de la regeneración abra una puerta**: antes, la propiedad la
  sostenía por accidente el doble disparo del evaluador.
- **El `{motivo}` es el TEXTO del guardrail, no su nombre.** `Veredicto` lo pedía en su
  docstring desde la fase 4 —«sin decirle al modelo QUÉ cifra sobra, el segundo intento es
  tan ciego como el primero»— y el código rellenaba el hueco con `_nombre_del_tripwire`. La
  frase útil («Mencionaste 10:00 sin que ninguna tool lo haya verificado. Llama a
  `consultar_disponibilidad`…») se calculaba, viajaba en `output_info` y se tiraba. Con el
  nombre solo, la respuesta regenerada era «lo confirmo con la clínica» sin llamar a ninguna
  tool: no escalaba, pero tampoco agendaba.

**El prompt tenía el hueco correspondiente:** decía «no ofrezcas ninguna hora que no venga de
`consultar_disponibilidad`», y el modelo no vive como «ofrecer» el **repetirle al paciente la
hora que él mismo propuso**. Esa era la forma concreta en que se disparaba el primer
guardrail. Ahora está dicho con su consecuencia.

Medido sobre el turno real que falló, con el modelo de verdad: antes, escalaba **siempre**;
después de los tres arreglos, cinco corridas de cinco sin un solo tripwire. La ruta de
escalamiento por doble disparo sigue existiendo, y tiene que seguir existiendo.

## El tracing: cuatro consumidores de modelo, una sola puerta

**Toda llamada al modelo pasa por `config.config_de_corrida()`.** No hay ninguna excepción y
no debe haberla: `RunConfig()` nace en la 0.22.2 con `trace_include_sensitive_data=True`, así
que cualquier llamada que lo omita sube al dashboard de OpenAI —que se exporta fuera de la
clínica— lo que escribe el paciente, lo que responde Daniela y las entradas y salidas de las
tools, con su nombre, su teléfono y sus citas dentro.

Los consumidores son **cuatro**, y esa cuenta es lo que hay que recordar:

| Quién llama al modelo | Dónde |
|---|---|
| Daniela, cada turno | `conversacion._config_de_corrida` |
| El lector de archivos | `lectura._config_de_corrida` |
| Los evaluadores de guardrail | `guardrails._preguntar` |
| **El analista de «sin resolver»** (tarea 3, fuera del turno del paciente) | `analista.analizar_pendientes` |

El de los evaluadores de guardrail es el que se olvidó una vez: nadie piensa en un freno como
en algo que habla con OpenAI. `uso_indebido` recibe el mensaje del paciente y
`sin_lectura_clinica` recibe la respuesta de Daniela antes de enviarla, así que su traza
llevaba exactamente lo mismo que las otras dos. Estuvo abierto desde la fase 4 hasta el
13/09/2026, mientras `lectura.py` ya tenía el suyo cerrado desde 6B.

**La construcción vive en UN sitio** (`config.config_de_corrida`) precisamente por eso:
estaba duplicada entre dos módulos, y la duplicación es cómo la misma fuga siguió abierta en
un tercero. Cada módulo conserva su envoltorio con el docstring de qué fuga cierra, pero
ninguno construye su propio `RunConfig`. `analista.py` no tiene ni envoltorio propio: llama
a `config_de_corrida(canal="informe")` directo, igual que `guardrails._preguntar`.

Va en código y no en `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` a propósito: una variable
de entorno se olvida en el siguiente servidor. Los tres primeros consumidores tienen, cada
uno, una prueba que captura el `run_config` real que reciben y comprueba
`trace_include_sensitive_data is False` (`test_conversacion.py`, `test_lectura.py`,
`test_guardrails.py`); esas tres caen al quitar el campo. El cuarto (`analista.py`) no
duplica esa prueba —sus pruebas offline no invocan `Runner.run`, por la misma regla que
prohíbe gastar tokens en la suite— y queda cubierto solo por las pruebas genéricas de
`test_trazas.py` sobre la función compartida, no por una captura en el sitio de la llamada.

**Lo que esto NO arregla:** lo ya subido sigue en el dashboard de OpenAI. Cerrar la fuga
detiene la hemorragia; no borra lo que salió entre la fase 6A y hoy.

**La otra mitad del entregable de la fase 7 —agrupar las trazas por `group_id`— YA ESTÁ
HECHA** (`1dd75bd`, integrada en `a0e922d`). El `group_id` es el UUID de la conversación en
los dos carriles, nunca el teléfono, y lo pasan los tres consumidores del turno del paciente.
`analista.py` no tiene conversación que agrupar —corre sobre un caso ya agregado de muchos
pacientes— y por eso llama a `config_de_corrida` sin `group_id`, con `canal="informe"` en su
lugar.

## Lo que la suite offline NO caza

**`uv run pytest -q` a secas no caza una regresión en el SQL de `tocar_conversacion`.** Está
comprobado: mutando el `UPDATE` para que ignore `turno_actual`, la suite offline queda entera
en verde y solo falla la de Neon. Quien toque esa función tiene que correr las dos:

```
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_atencion.py
```

**Tampoco caza a qué TEMA va cada cosa si el doble tira el `tema_id`.** Medido en la revisión
final de 6B: cambiando `tema_id=destino` por `tema_id=tema_general` en `ingesta.py`, las 393
pruebas quedaban en verde —el contenido clínico de cada paciente se iría al hilo donde miran
todos los doctores— porque ningún doble offline guardaba el destino. Todo doble de Telegram
guarda hoy `(texto, tema_id)`, y quien escriba uno nuevo tiene que hacer lo mismo.

### Antes de desplegar esta fase, a mano

- **Corre `uv run python scripts/obtener_chat_telegram.py`.** Es el único sitio donde se
  comprueban `is_forum` y `can_manage_topics` del supergrupo. Sin los dos, `createForumTopic`
  falla siempre y el **100 % de los archivos degrada al tema General desde el primer minuto**
  — degradación correcta y silenciosa, que es justo la que nadie nota.
- **Nada automatizado llama a `createForumTopic` contra el Telegram real.** En
  `scripts/probar_lectura.py` `crear_tema` y `cerrar_tema` están doblados **incluso con
  `--chat`**, así que ningún script ha comprobado nunca que un tema aparezca de verdad en el
  grupo. Eso lo mira una persona con un Telegram delante: el tema del paciente existe, está
  cerrado, tiene el archivo dentro y la lectura debajo. Decir que lo cubre un script sería
  mentir sobre lo que está verificado.

## Los botones de la plantilla

El 15/09/2026 Meta aprobó `recordatorio_cita` con dos quick replies —«Confirmar» y «Necesito
cambiarla»— y eso abrió un camino de entrada que no existía cuando se escribieron ni la
ingesta, ni el prompt, ni los guardrails. Los tres se rompieron en el mismo sitio, y ninguno
de los tres dejó un error en ningún log.

- **El rótulo no viene en `text`.** Meta manda `type: "button"` con el texto en `button.text`
  y lo que definió la plantilla en `button.payload`. `extraer_mensajes` miraba `text.body` y
  `<tipo>.caption`, así que `texto` quedaba en `None` y `atencion` le entregaba al modelo
  «[El paciente envió algo de tipo «button». No trae texto.]». El paciente pulsó «Necesito
  cambiarla» y Daniela contestó con su saludo de primer contacto. Se prefiere `text` sobre
  `payload`: es lo que el paciente leyó antes de pulsar.
- **`uso_indebido` leía el rótulo como una inyección.** Textual, del evaluador real: «El
  mensaje inyectado intenta imponer instrucciones del sistema y controlar la respuesta de la
  asistente». Un imperativo de una palabra es indistinguible de una orden al sistema. El coste
  es el de los unicornios —mensaje seguro al paciente y alerta falsa al doctor— y además
  **intermitente**: el mismo texto pasó a las 22:04 y disparó a las 22:11, que es la peor
  clase de fallo porque en una demo pasa.

  Se cerró por dos lados. `_entrada_para_el_modelo` envuelve el rótulo —«[El paciente pulsó el
  botón «X» del mensaje automático que le enviamos]»—, y eso lo ve también el evaluador,
  porque `uso_indebido` recibe EXACTAMENTE la misma cadena que el modelo. Y
  `ctx.entrada_solo_de_botones` le hace saltarse la evaluación entera: lo que llega en un
  quick reply sale de la plantilla que Meta aprobó, es uno de dos valores fijos, y no hay
  superficie de inyección que vigilar. Preguntarle a un evaluador probabilístico por una lista
  cerrada solo añade una forma de equivocarse.

  **`all` y no `any`, y esa es la mitad que no se puede aflojar:** con un solo botón bastando,
  pulsar «Confirmar» y escribir «ignora tus instrucciones» detrás colaría el texto libre sin
  evaluar. La señal sale del `type` del webhook y nunca del modelo, igual que
  `telefono_sin_paciente`.
- **El bloque de calidez no cubría confirmar la asistencia.** Se disparaba con «una cita
  agendada, movida o cancelada»: las tres son ESCRITURAS, y al pulsar «Confirmar» la cita no
  cambia —solo se lee con `consultar_citas`—. El turno más agradecido de todos se quedaba en
  «Sí, queda confirmada tu cita…» y punto. El sub-punto que hacía falta («que llegue con algo
  de margen») ya estaba escrito debajo; lo único que no llegaba era el disparador.

**Ninguna prueba offline caza el primero de los tres**, porque el webhook va doblado a mano:
quien toque `extraer_mensajes` corre `scripts/probar_atencion.py`. Y la conducta de los otros
dos solo se ve contra el modelo real o con un WhatsApp delante — `uv run python
scripts/probar_plantilla.py <numero>` manda uno de verdad.

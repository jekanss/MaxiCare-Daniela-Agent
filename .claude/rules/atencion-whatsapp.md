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
- **El historial vive en memoria** (`_sesiones`). Un reinicio borra el hilo del diálogo, no
  los datos: paciente, citas y estado de oportunidad están en Neon. Lo arregla la fase 7 con
  `SQLAlchemySession`.

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

- **La ingesta NO crea filas en `pacientes`. Un desconocido no abre tema.** `asegurar_tema`
  comprueba con `buscar_paciente_por_telefono` que el paciente ya existe ANTES de crear nada
  —antes, no después, o quedaría el tema huérfano— y si no existe devuelve `None`: su archivo
  va al General, exactamente como antes de 6B. La razón es de seguridad clínica, no de orden:
  `atencion._leer_estado` deriva `identidad_verificada` de la EXISTENCIA de esa fila, y
  `runtime._entregar` corre `procesar_mensaje` antes que `atender`. Con la ingesta creando la
  fila —con el `nombre_perfil` que el propio desconocido escribió—, mandar una foto
  **verificaba a un desconocido en ese mismo turno**: `revisar_identidad` y el
  `tool_input_guardrail` `identidad_antes_de_datos` quedaban desactivados para él, y
  `_mismo_nombre` comparaba después el nombre que él decía contra el nombre que él mismo
  había puesto. Lo que se pierde es poco: el hilo vale por lo que CONSERVA —el historial de
  esa persona— y un desconocido no tiene historial. En cuanto se identifique o le abran una
  cita, su siguiente archivo le abrirá el hilo.

- **El lector corre con `run_config`, y `trace_include_sensitive_data` va en `False`.** Es la
  CUARTA salida del muro y la única que no se ve: `RunConfig()` nace con ese campo en `True`
  en la 0.22.2, así que sin pasarlo el data URL entero de la radiografía y el
  `LecturaArchivo` completo —`contexto_clinico` incluido— subían a los traces de OpenAI, que
  se exportan fuera de la clínica. `config.TRACE_INCLUDE_SENSITIVE_DATA` existía desde la
  fase 1 y no la cableaba nadie. Con `False` los spans se siguen creando (latencia, coste,
  errores) y solo se omiten entradas y salidas. **`conversacion.py` tiene el mismo hueco y
  está aplazado a la fase 7 por decisión expresa**: si lo cierras, cierra también su prueba.

- **El lector corre en PARALELO con la ventana del búfer, nunca delante.** La tarea nace en
  `procesar_mensaje` y viaja como `Resultado.lectura`; `atencion.atender` no la espera al
  recibirla, la guarda en `bufer.lecturas[wamid]` y solo la recoge (`_recoger_lecturas`,
  `asyncio.shield` con un margen corto) DESPUÉS de que la ventana de silencio cierra.
  Encadenarla delante sumaría los 4-8 s que tarda el lector a los 20 s de la ventana, y
  contra `limites.latencia_maxima` (un minuto) no cabe: medido el 12/09, un documento recibido
  a las 19:03:28 se entregó después de un texto recibido a las 19:03:29 — si el lector hubiera
  ido delante, Daniela habría contestado el texto sin saber todavía que había una foto. Lo que
  no llegue a tiempo se descarta con un `log.info`, nunca con una excepción que tumbe el turno.

## `/clearstate` — resetear un número a primer contacto

Un número listado en `MAXICARE_TELEFONOS_PRUEBA` puede escribir `/clearstate` y quedar, para
Daniela, como uno que nunca ha escrito. Existe para poder probar conversaciones enteras sin
estrenar una línea de teléfono. Vive en `reseteo.py`, y `runtime._entregar` lo intercepta.

- **La garantía es una igualdad, no una promesa:** tras el reseteo, `atencion._leer_estado`
  devuelve para ese teléfono los mismos ocho campos que para un número virgen — solo cambia
  `id_conversacion`, que es un UUID nuevo por definición. Lo sostiene
  `test_reseteo_neon.py::test_tras_el_reset_daniela_ve_lo_mismo_que_en_un_primer_contacto`,
  con su control: antes del borrado, esa misma prueba comprueba que Daniela SÍ lo conocía.
  El historial del diálogo no hay que borrarlo porque `_sesiones` se indexa por
  `id_conversacion`: conversación borrada, id nuevo, sesión vacía. Lo único de memoria que sí
  hay que sacar es el búfer, y de eso se encarga `atencion.olvidar`.
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
- **También se limpia `pruebas_web`**, que tiene las mismas doce tablas y no se purga nunca.
  Sin eso, un número probado desde el chat del panel seguiría conocido por esa mitad.
- **Lo que NO borra, y está dicho en el código:** las trazas de OpenAI (`conversacion.py`
  sigue sin `trace_include_sensitive_data=False`, aplazado a la fase 7) y los logs del
  contenedor. Ninguna de las dos afecta al comportamiento de Daniela; el rastro existe.
- **Telegram necesita `can_delete_messages`**, que `obtener_chat_telegram.py` no comprueba
  hoy — solo mira `can_manage_topics`. Si falta, el tema no se borra, se informa en la
  confirmación y el resto del reseteo sigue.

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

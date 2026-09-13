---
paths:
  - "src/maxicare_daniela/atencion.py"
  - "src/maxicare_daniela/runtime.py"
  - "src/maxicare_daniela/ingesta.py"
  - "src/maxicare_daniela/conversacion.py"
  - "src/maxicare_daniela/persistencia.py"
  - "tests/test_atencion.py"
  - "tests/test_webhook_responde.py"
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
Telegram, mientras Daniela recibe únicamente lo no clínico. Tres cosas de ese camino no se
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

- **El archivo nunca espera al modelo.** `ingesta.procesar_mensaje` entrega el archivo al
  tema del paciente y avisa al General ANTES de arrancar el lector — el `asyncio.create_task`
  del lector es lo último que pasa en esa función, no lo primero. Es la misma garantía de la
  fase 2 (nada de lo que se escriba puede retrasar la entrega al doctor) y sigue vigente: si
  el lector se cayera, se colgara o tardara un minuto, el doctor ya tiene el archivo en su
  tema desde antes de que la tarea existiera.

- **El lector corre en PARALELO con la ventana del búfer, nunca delante.** La tarea nace en
  `procesar_mensaje` y viaja como `Resultado.lectura`; `atencion.atender` no la espera al
  recibirla, la guarda en `bufer.lecturas[wamid]` y solo la recoge (`_recoger_lecturas`,
  `asyncio.shield` con un margen corto) DESPUÉS de que la ventana de silencio cierra.
  Encadenarla delante sumaría los 4-8 s que tarda el lector a los 20 s de la ventana, y
  contra `limites.latencia_maxima` (un minuto) no cabe: medido el 12/09, un documento recibido
  a las 19:03:28 se entregó después de un texto recibido a las 19:03:29 — si el lector hubiera
  ido delante, Daniela habría contestado el texto sin saber todavía que había una foto. Lo que
  no llegue a tiempo se descarta con un `log.info`, nunca con una excepción que tumbe el turno.

## Lo que la suite offline NO caza

**`uv run pytest -q` a secas no caza una regresión en el SQL de `tocar_conversacion`.** Está
comprobado: mutando el `UPDATE` para que ignore `turno_actual`, la suite offline queda entera
en verde y solo falla la de Neon. Quien toque esa función tiene que correr las dos:

```
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_atencion.py
```

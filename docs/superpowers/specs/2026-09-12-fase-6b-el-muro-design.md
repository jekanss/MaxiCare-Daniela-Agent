# Fase 6B — El muro: el lector de archivos entra al camino en vivo

**Fase del plan:** 6, «Ingesta, el muro y el relevo» — mitad B.
**Fecha:** 2026-09-12
**Estado:** aprobado en conversación, pendiente de plan de implementación.

El relevo (6C) queda fuera. Aquí solo entra lo que el `plan-agentes.json` marca como 6B:
interpretar el contenido de un archivo y crear los temas de Telegram por paciente.

---

## 1. El problema

`lector_archivos` está construido, probado y **no lo llama nadie**. Su docstring en
`ingesta.componer_aviso` lo dice con todas las letras: el pie que ven los doctores dice lo
que el archivo ES, nunca lo que MUESTRA, «esa segunda cosa la produce `lector_archivos` en
la fase 6». Hoy un paciente manda una radiografía, el doctor recibe una imagen con un pie
que dice «Mandó una imagen — image/jpeg · 240 KB», y Daniela le contesta al paciente que no
puede verla.

Falta también el hilo por paciente. `migraciones/002` creó `pacientes.telegram_topic_id` y
`pacientes.telegram_topic_abierto` hace fases, con su índice único, y nada los escribe: todo
cae en el tema General.

El entregable de la fase 6 en el plan nombra el hecho que cierra esta mitad:

> Una prueba manda una remisión, captura TODO lo que entra a `Runner.run(daniela)` y
> comprueba que el contenido clínico no aparece ahí, mientras sí aparece en el mensaje
> enviado a Telegram.

Eso es el muro. No es un prompt ni un guardrail: es de dónde el código saca cada cosa.

---

## 2. Alcance

**Entra:**

- `lector_archivos` corriendo sobre los archivos que llegan por WhatsApp, en vivo.
- El reparto de `LecturaArchivo` en sus dos mitades, con destinos distintos.
- El tema de Telegram por paciente: se crea con el primer archivo y **nace cerrado**.
- El archivo depositado en el tema del paciente; el aviso, en el General.
- La mitad no clínica en el contexto de Daniela, para que reconozca el documento.
- Una frase más en el evaluador de `sin_lectura_clinica` (ver §4.7).

**No entra:**

- **El relevo, en cualquiera de sus formas: es 6C.** Aquí no se escribe
  `reopenForumTopic`, no se atiende ningún `callback_query`, y
  `pacientes.telegram_topic_abierto` se escribe una sola vez, en `FALSE`, al crear el tema.
- **Transcripción de notas de voz.** Decidido explícitamente: es una fase propia, no un
  añadido a esta. `audio`, `voice`, `video` y `sticker` siguen con el aviso factual de hoy.
- **Persistir la mitad clínica.** Nunca, en ninguna tabla. Ver §4.3.
- Sesión persistente y `group_id` en los traces: es la fase 7.

---

## 3. Arquitectura

### 3.1 El flujo, paso por paso

```
llega un archivo de tipo image | document
  |
  +-- 1. descarga .................... el enlace de Meta caduca, va primero (ya existe)
  +-- 2. tema del paciente ........... se crea si no existe, NACE CERRADO
  +-- 3. el archivo entra al tema .... pie factual de hoy, sin interpretar
  +-- 4. aviso al tema General ....... «llego un archivo de Ana Perez»
  +-- 5. registro en la base ......... (ya existe)
  |
  +-- 6. arranca el lector ........... tarea aparte; NO se espera aqui
  |        |
  |        +--> contexto_clinico ----> al tema del paciente, 4-8 s despues
  |        +--> mitad no clinica ----> queda disponible para el turno de Daniela
  |
  +-- 7. el turno de Daniela ......... la ventana de 20 s del bufer corre EN PARALELO
                                       con el lector, no despues
```

### 3.2 Un módulo nuevo: `lectura.py`

Siguiendo el patrón que la fase 6A usó con `atencion.py`: la responsabilidad nueva va a un
módulo nuevo en vez de engordar uno que ya hace otra cosa. `ingesta.py` son 300 líneas y su
trabajo es el acuse de recibo de un canal; meterle el lector, el reparto y la gestión de
temas lo llevaría a 500 y a tres responsabilidades.

`lectura.py` expone cuatro cosas y nada más:

| Símbolo | Qué hace |
|---|---|
| `repartir(...)` | El reparto. Función **pura**: `LecturaArchivo -> (str, LecturaNoClinica)` |
| `leer_archivo(...)` | Corre `lector_archivos` sobre los bytes. Devuelve `LecturaArchivo` o `None` |
| `asegurar_tema(...)` | Encuentra o crea el tema del paciente. Idempotente |
| `leer_y_repartir(...)` | Une los tres: lee, manda lo clínico a Telegram, devuelve la mitad no clínica |

Que el reparto sea una función pura, y no lógica repartida por tres archivos, es lo que
permite probar el muro sin levantar nada.

### 3.3 Qué cambia en cada archivo

| Archivo | Cambio |
|---|---|
| `src/maxicare_daniela/lectura.py` | **NUEVO** |
| `src/maxicare_daniela/contratos.py` | `LecturaNoClinica`, la mitad que sí cruza |
| `src/maxicare_daniela/canales.py` | `Telegram.crear_tema` y `Telegram.cerrar_tema` |
| `src/maxicare_daniela/ingesta.py` | destino del archivo, aviso al General, arranca el lector |
| `src/maxicare_daniela/atencion.py` | recoge la lectura al cerrar la ventana; la usa en el prompt |
| `src/maxicare_daniela/runtime.py` | pasa la lectura pendiente de `procesar_mensaje` a `atender` |
| `src/maxicare_daniela/guardrails.py` | una frase en `_evaluador_clinico` (§4.7) |

**Sin migración.** `migraciones/002` ya trae las dos columnas y el índice único
`uq_pacientes_topic`.

---

## 4. Decisiones

### 4.1 El archivo nunca espera al modelo

**Decisión:** el archivo se deposita en Telegram en cuanto se descarga, con el pie factual
de hoy. El lector corre después y su `contexto_clinico` llega como un **segundo mensaje** en
el mismo tema, segundos más tarde.

**Por qué:** la entrega del archivo al doctor es la garantía de la fase 2, y `_entregar` ya
está construido alrededor de esa garantía —dos `try` separados, con un comentario que
explica que un fallo del turno de Daniela no puede llevarse por delante la radiografía—.
Poner una llamada al modelo con timeout de 90 s delante de esa entrega la anularía.

**Consecuencia que se acepta:** el doctor ve dos mensajes en vez de uno. A cambio, si el
lector se cae, tarda o devuelve basura, el doctor ya tiene la imagen.

### 4.2 El lector corre EN PARALELO con la ventana del búfer

**Decisión:** `procesar_mensaje` arranca el lector como `asyncio.Task` y lo devuelve sin
esperarlo. `atender` lo espera **después** de que la ventana de silencio cierre, justo antes
de componer la entrada del modelo, con `asyncio.wait_for` y el tiempo que quede.

**Por qué:** es el mismo principio que ya rige el retardo humano y que el `CLAUDE.md` lista
como no negociable —*el retardo se descuenta, no se suma*—. Encadenar el lector delante de
`atender` sumaría sus 4-8 s a los 20 de la ventana, y en el peor caso (timeout) Daniela
habría contestado ya el texto que vino junto a la foto **sin saber que había una foto**.

Esa no es una molestia de latencia: es el fallo que la nota de la fase 4 en el plan señaló
como **requisito de la fase**, medido con mensajes reales el 12/09 —un documento recibido a
las 19:03:28 se entregó después de un texto recibido a las 19:03:29, porque el texto no
tenía nada que descargar—.

**Qué pasa si no llega a tiempo:** Daniela usa la entrada de hoy, la que dice que nadie ha
revisado el archivo. Nunca se bloquea esperando.

### 4.3 El muro es un reparto, no una promesa

**Decisión:** una función pura parte `LecturaArchivo` en dos y devuelve un objeto
—`LecturaNoClinica`— que **no tiene el campo** `contexto_clinico`. Lo que cruza hacia Daniela
es ese objeto, no el original con un campo que alguien debe acordarse de no leer.

```
LecturaArchivo  --+--> contexto_clinico -------------> Telegram, tema del paciente
                  +--> tipo_documento, tratamiento,
                       origen, fecha_documento,
                       confianza ------------------->  ContextoDaniela
```

**Por qué así y no con disciplina:** un campo que existe y no se debe usar se acaba usando.
Si el tipo que llega a `atencion.py` no tiene el campo, no hay forma de filtrarlo por error
—falla al importar, no en producción—.

`contexto_clinico` **no se persiste en ninguna tabla de Neon**, y eso está en el docstring
del contrato desde la fase 1. Vive en memoria el tiempo que tarda en salir hacia Telegram.

### 4.4 `confianza` por debajo de `alta` fuerza `no_identificado`

**Decisión:** el reparto, no el prompt, fuerza `tratamiento = "no_identificado"` cuando
`confianza` no es `alta`.

**Por qué:** lo dice el contrato desde la fase 1 —«por debajo de 'alta', la capa de ingesta
fuerza tratamiento='no_identificado' para que Daniela pregunte en vez de asumir»— y es
determinista. Una instrucción puede desobedecerse; una línea de código no.

**Qué ve el paciente:** con `alta`, Daniela nombra el tratamiento y de dónde viene el
documento. Por debajo, pregunta.

### 4.5 El tema del paciente nace cerrado

**Decisión:** al llegar el **primer archivo** de un paciente se crea su tema con
`createForumTopic` y se cierra en el acto con `closeForumTopic`. El id va a
`pacientes.telegram_topic_id` y `telegram_topic_abierto` queda en `FALSE`.

**Por qué nace cerrado:** lo fijó la revisión del 12/09 en el plan, y la garantía es de
Telegram, no nuestra: un tema cerrado **impide físicamente** escribir a quien no es
administrador del grupo, mientras el bot, que sí lo es, sigue pudiendo depositar. El modo
deja de ser algo que el doctor tenga que recordar — si puede escribir, está en relevo; si no
puede, está mirando el archivo.

**Por qué con el primer archivo y no con el primer mensaje:** si cada saludo abriera un
tema, el grupo sería inservible en una semana.

**Un mensaje de texto suelto no cambia de destino.** Sigue yendo al tema General con
`componer_aviso`, exactamente como hoy. Lo único que se muda al tema del paciente es el
archivo.

**El nombre del tema** sale de `m.nombre_perfil` más el teléfono —`Ana Pérez · +573...`—. Si
WhatsApp no manda nombre de perfil, el tema se llama solo con el teléfono: un tema sin
nombre es peor que uno feo. `persistencia.asegurar_paciente` recibe ese mismo valor, y ya
resuelve el caso del número desconocido que manda un archivo como primer mensaje.

**Hueco conocido que esto NO cierra:** Telegram no permite quitarle privilegios al creador
del grupo, así que esa persona puede escribir en un tema cerrado y su mensaje llegaría al
paciente fuera de relevo. Ya estaba documentado; 6B no lo agrava ni lo resuelve.

### 4.6 La carrera de los dos archivos simultáneos

**Decisión:** un candado `asyncio.Lock` por teléfono alrededor de «buscar o crear el tema»,
en el mismo proceso.

**Por qué:** dos archivos del mismo número nuevo llegando a la vez crearían dos temas, y el
índice único `uq_pacientes_topic` no lo impide —son dos ids distintos—. El candado es el
mismo mecanismo que `atencion.py` ya usa para el turno.

**Límite conocido, idéntico al del turno:** es de proceso. Con más de un worker o más de una
réplica deja de proteger y haría falta un `pg_advisory_lock`. El contenedor corre con un solo
worker a propósito, y por eso hoy alcanza.

### 4.7 El evaluador clínico necesita una frase más

**Decisión:** ampliar la excepción de `_evaluador_clinico` en `guardrails.py`.

Hoy dice: *«Hablar de un tratamiento que el paciente ya mencionó no es diagnosticar»*.

En 6B el tratamiento viene de un **documento que el paciente envió**, no de su boca. Con
`hubo_adjunto = True` el evaluador corre siempre en ese turno, y «ya me llegó tu remisión
para ortodoncia» cae en una zona que la excepción no cubre: Daniela podría autobloquearse
justo en el turno que 6B existe para mejorar.

La frase pasa a cubrir también «un tratamiento nombrado en un documento que el propio
paciente envió». **Es el único punto de 6B que toca las instrucciones de un agente**, y es un
evaluador, no Daniela.

### 4.8 Qué tipos van al lector

**Decisión:** `image` y `document`. Nada más.

**Por qué:** son los dos que el lector puede leer de verdad. `sol` es el modelo caro; correrlo
sobre un sticker es dinero tirado, y sobre una nota de voz no funcionaría sin una API de
transcripción que esta fase no incorpora.

**Cómo se le pasa el archivo al SDK** (verificado por introspección de la 0.22.2 instalada,
no de memoria):

- imagen: `{"type": "input_image", "image_url": "data:<mime>;base64,<...>", "detail": "auto"}`
- documento: `{"type": "input_file", "filename": "<nombre>", "file_data": "<...>"}`

`ArchivoDescargado` ya trae `contenido: bytes`, `mime` y `nombre`, así que no hay que volver
a descargar nada.

**PENDIENTE de confirmar contra una llamada real en la primera tarea del plan:** si
`file_data` espera el data URL completo (`data:application/pdf;base64,...`) o el base64 a
secas. Los nombres de los campos están verificados; este detalle de formato no, y una
suposición razonable aquí no se distinguiría de un hecho comprobado.

**Tope de tamaño:** los archivos por encima de 20 MB no van al lector. Llegan al doctor igual
—eso no cambia— y Daniela usa la entrada de hoy.

### 4.9 Lo que cambia para la clínica

Hoy el archivo cae en el General. Después de 6B cae en el tema del paciente, y al General
llega un aviso de que llegó. Lo decidió la revisión del 12/09 y tiene sentido —una persona,
un hilo, con su historial, que es el objetivo 4 del brief— pero **es un cambio visible para
los doctores el día del despliegue** y hay que avisarles antes, no después.

---

## 5. Cuando algo falla

Ninguna de estas rutas puede dejar al doctor sin el archivo.

| Falla | Qué pasa |
|---|---|
| El lector revienta o se pasa de tiempo | El doctor ya tiene el archivo. Al tema va una línea diciendo que no se pudo leer. Daniela usa la entrada de hoy |
| No se puede crear el tema | El archivo va al General, como hoy. Se degrada, no se pierde |
| `closeForumTopic` falla tras crear el tema | El tema queda abierto. Se registra el id igual y se deja constancia en el log: un tema abierto sin relevo es un hueco, no una pérdida |
| Dos archivos a la vez de un número nuevo | El candado por teléfono de §4.6 |
| Meta reintenta el webhook | Ya resuelto aguas arriba: `ON CONFLICT (wamid)` y `Resultado.nuevo`. El lector no llega a correr dos veces |
| El archivo pasa de 20 MB | No va al lector. Todo lo demás, igual |

---

## 6. Verificación

### Offline (`uv run pytest -q`), sin red ni base

1. **EL ENTREGABLE — el muro.** Una remisión con una frase centinela en `contexto_clinico`.
   Se captura TODO lo que entra a `Runner.run(daniela)` con `ModeloGuionizado` de
   `tests/dobles.py` y se comprueba que la centinela **no aparece por ningún lado**, mientras
   sí aparece en lo que recibió el doble de Telegram.
2. El reparto fuerza `no_identificado` con `confianza` media y baja, y respeta el tratamiento
   con `alta`.
3. `LecturaNoClinica` **no tiene** un campo `contexto_clinico` — la prueba falla al construirlo.
4. El tema se crea una vez y se reusa en el segundo archivo del mismo paciente.
5. El tema nace cerrado: `closeForumTopic` se llamó, y `telegram_topic_abierto` quedó en `FALSE`.
6. Dos archivos simultáneos de un número nuevo crean **un** tema, no dos.
7. El lector caído no calla a Daniela: contesta con la entrada de hoy.
8. El lector lento no se suma a la ventana: con una ventana corta y un lector que tarda más,
   el turno sale igual y sin la lectura.
9. `sticker`, `voice`, `audio` y `video` no llaman al lector.
10. Un archivo de más de 20 MB no llama al lector y sí llega a Telegram.

### Contra Neon (`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`)

11. `telegram_topic_id` se persiste y se recupera por teléfono, en el esquema de pruebas.

### Entregable ejecutable

`scripts/probar_lectura.py`, con un doble de Telegram que captura en vez de enviar y un
archivo de ejemplo del repositorio. Con `--chat` corre el lector real y gasta tokens; sin él,
no gasta ni uno.

### Lo que ningún script comprueba

Que el doctor vea el tema del paciente en su Telegram con el archivo dentro y la lectura
debajo. Eso lo mira una persona, y decir que lo cubre un script sería mentir sobre lo que
está verificado.

---

## 7. Límites conocidos y aceptados

1. **El candado de temas es de proceso**, igual que el del turno. Vale con un worker.
2. **El doctor recibe dos mensajes por archivo**, no uno. Es el precio de §4.1.
3. **La lectura no se reintenta.** Si falla, falla para ese archivo; el doctor tiene la
   imagen y puede pedirle al paciente que la reenvíe. Un reintento automático sobre el modelo
   caro, sin nadie mirando, puede costar más que el problema que resuelve.
4. **La mitad no clínica tampoco se persiste** en esta fase. Vive en el contexto del turno. Si
   el proceso se reinicia entre la lectura y el turno, se pierde — el mismo límite que el
   historial en memoria, y lo arregla la misma fase 7.
5. **`telegram_topic_abierto` se escribe una sola vez y siempre en `FALSE`.** La columna existe
   desde la migración 002 esperando a 6C; aquí no se abre nada.

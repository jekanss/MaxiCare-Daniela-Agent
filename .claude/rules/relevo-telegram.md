---
paths:
  - "src/maxicare_daniela/relevo.py"
  - "src/maxicare_daniela/canales.py"
  - "tests/test_relevo*.py"
  - "tests/test_webhook_telegram.py"
  - "scripts/configurar_webhook_telegram.py"
  - "scripts/probar_relevo.py"
---

# El relevo (fase 6C)

Un doctor pulsa «Hablar yo con el paciente» y, a partir de ahí, **habla él**. Daniela calla,
el tema de ese paciente se abre y lo que se escriba dentro le llega a su WhatsApp literal.

## El único estado peligroso

`conversaciones.tomada_por` puesto significa DOS cosas a la vez:

- a Daniela **no se la llama** — `atencion.atender` corta antes del modelo;
- el tema de Telegram está **abierto**, o sea que es un canal en vivo hacia el teléfono de
  una persona.

Si una se queda sin la otra, o hay un paciente con nadie al otro lado, o hay un hilo por el
que cualquiera del grupo le escribe sin que nadie lo vigile. Por eso **las tres salidas pasan
por `relevo.cerrar`** y por eso esa función es idempotente: se solapan de verdad, el doctor
pulsa «Listo» en el mismo minuto en que el barrido lo da por vencido.

Los tres motivos son los del CHECK cerrado de la migración 003 y no hay un cuarto:
`devuelto_por_doctor`, `tiempo_agotado`, `tema_perdido`.

## El candado lo pone Telegram, no nuestra disciplina

Un tema **cerrado** impide físicamente escribir a quien no es administrador del grupo,
mientras el bot —que sí lo es— sigue pudiendo depositar. De ahí que el modo no sea algo que
el doctor tenga que recordar: si puede escribir, está en relevo; si no puede, está mirando un
expediente.

**Hueco conocido y no cerrable:** Telegram no deja quitarle privilegios al CREADOR del grupo,
así que esa persona sí puede escribir en un tema cerrado. Lo que hace `relevar_mensaje` es no
reenviar nada sin relevo vivo y decirlo en el hilo. Estrechado, no cerrado.

## El botón no puede navegar. La notificación sí

`answerCallbackQuery(url=...)` hacia un tema del propio supergrupo responde `URL_INVALID`
—comprobado contra la API—. Por eso `activar` **escribe en el tema**, sin silenciar: esa
notificación es toda la navegación que hay. Es el único sitio del proyecto donde un tema de
paciente suena, y es la excepción que la NOTA DEL SILENCIO de `canales.py` ya preveía.

En el General se cambia **solo el teclado** (`editMessageReplyMarkup`), nunca el texto:
reescribirlo obligaría a recomponer un aviso que escribió un modelo, a partir del texto plano
que trae el callback, y perdería el formato justo cuando deja de leerse de un vistazo.

### Y aun así no llegaba (14/09/2026)

«Le da al botón y no lo lleva al topic». Con el botón imposibilitado de navegar, quedaban dos
caminos, y los dos estaban mal puestos:

| | Cómo estaba | Por qué fallaba | Cómo está |
|---|---|---|---|
| La notificación | mensaje normal en el tema | depende de cómo tenga cada uno sus avisos: con el grupo silenciado, o sin seguir ese hilo, no suena nada | **mención** `tg://user?id=` al doctor que pulsó — Telegram la notifica igual con el grupo silenciado |
| El enlace del General | se ponía **después** de volcar la transcripción | durante esos segundos el botón seguía diciendo «Hablar yo con el paciente»: ninguna puerta | va justo después de la bienvenida; el volcado, que es lo lento, detrás |
| A dónde apuntaba | `t.me/c/<chat>/<tema>` | en un foro ese segundo número es un id de **mensaje**: que abriera el hilo era casualidad —el id de un tema es el del mensaje de servicio que lo creó— | `t.me/c/<chat>/<tema>/<mensaje>`, que aterriza dentro del hilo y en la bienvenida |

Medido contra el grupo real: el `sendMessage` con la mención vuelve con una entidad
`text_mention` y el usuario resuelto dentro, y `editMessageReplyMarkup` acepta la URL de tres
tramos. La mención **se escapa** (`_mencion` → `_escapar`): el nombre lo pone el doctor en
Telegram y un «Ana &lt;3» sin escapar rompe el mensaje que lleva el botón de salida. Sin
`doctor_id` degrada al nombre a secas, nunca a un `<a href>` a medias — ese mensaje es el que
ancla el botón que cierra el relevo.

Y el acuse del callback dejó de decir «te llevo a su hilo»: no llevaba a nadie, el doctor lo
leía, no pasaba nada y volvía a pulsar.

## El reloj mide abandono, no duración

`persistencia.relevos_activos` cuenta contra
`GREATEST(tomada_en, ultimo_mensaje_doctor_en)`. Medir desde la activación sería un
cronómetro: a las tres horas justas corta, y una conversación difícil de tres horas es
precisamente la que sigue viva. `ultimo_mensaje_doctor_en` existe desde la 001 y hasta la 6C
no la escribía nadie.

El aviso previo (`aviso_relevo_minutos`) se recuerda **en memoria**, en `relevo._avisados`.
Es deliberado: guardarlo costaría una migración por un matiz, y el peor caso de perderlo —un
reinicio y un aviso repetido— es un mensaje de más en un hilo que el doctor está mirando.

## La puerta: vacío significa CERRADO

`/webhook/telegram` sin `MAXICARE_TELEGRAM_WEBHOOK_SECRET` responde **403 a todo**. Degrada
al revés que el resto del proyecto, y tiene que ser así: en los demás sitios, degradar cuesta
un mensaje; aquí cuesta el control de a quién le habla la clínica, porque es la única puerta
por la que algo de fuera puede hacer que el bot escriba a un WhatsApp.

Dos comprobaciones, no una: el secreto de la cabecera prueba que el update viene de Telegram;
`_es_nuestro_grupo` prueba que viene de DONDE tiene que venir. Sin la segunda, un
`callback_data` copiado a mano en cualquier chat activaría un relevo real.

**Telegram no valida la URL como hace Meta.** Hay que registrarla con `setWebhook` a mano
(`scripts/configurar_webhook_telegram.py`), y poner webhook **rompe el `getUpdates`** de
`scripts/obtener_chat_telegram.py`: son excluyentes.

## El hilo va por TELÉFONO, no por ficha (migración 014)

Vivía en `pacientes.telegram_topic_id`, y ese era el defecto de raíz: `pacientes` es la tabla
de la que sale `identidad_verificada`, así que `lectura.asegurar_tema` no podía crear la fila
sin regalarle una identidad a un desconocido — y sin fila no había dónde colgar el hilo.

Medido en el primer relevo real (14/09/2026): los 5 textos del lead quedaron con
`telegram_message_id` NULL (no llegaron a ninguna parte), sus dos imágenes y la lectura
clínica cayeron al General, y el hilo que le abrió el botón **nació vacío**.

Ahora vive en `temas_telegram(telefono, topic_id, abierto)`. **Tener hilo y estar verificado
son cosas distintas**, y por eso el guardrail de identidad no se tocó. La 014 deja caer las
dos columnas viejas: el mismo hecho en dos sitios es como nace el bug del mes siguiente.

`temas_telegram` **no cuelga de nada**, así que ningún CASCADE la vacía: `borrar_rastro` la
borra a mano, y los fixtures de las pruebas de Neon tienen que limpiarla aparte.

## La transcripción trae la FRASE, no el JSON de Daniela

`daniela` tiene `output_type`, así que lo que el SDK guarda como contenido del item no es su
frase: es el JSON entero de `RespuestaDaniela`. Medido en el segundo relevo real
(14/09/2026), el doctor recibía esto como transcripción:

```
{"mensaje_al_paciente":"Claro que sí. Ya nos llegó el archivo...",
 "estado_oportunidad":"explorando","barrera_detectada":"ninguna",
 "requiere_escalamiento":true,"motivo_escalamiento":"archivo_recibido", ...}
```

Ilegible, y con la telemetría comercial delante de quien solo quiere saber qué le dijeron a
su paciente. Lo desempaqueta `persistencia._solo_la_frase`, en **dos** niveles: el item del
SDK y, dentro, la `RespuestaDaniela`. Un contenido que no sea ese JSON —un guardrail que
respondió en texto plano— sale tal cual; un dict que no la sea no sale, porque volcarlo
sería el bug otra vez.

## De qué es la cita: texto libre, y es la única excepción del proyecto

Hasta la 015, toda cita salida de un relevo se registraba con `tratamiento="valoracion"`,
fijo — y `"valoracion"` no es ninguna de las catorce claves de `tratamientos`. Dos defectos
en una línea: un valor inventado donde la regla dura 3 pide marcar lo desconocido, y una
categoría fantasma en cualquier informe por tratamiento.

Ahora se pregunta, y **la respuesta se guarda tal cual la escriba el doctor, sin validar
contra la lista viva**. Es el único sitio del proyecto donde eso pasa —en todos los demás,
`herramientas._marcar_estado` rechaza lo que no esté en la tabla— y es una decisión explícita
del cliente (14/09/2026), tomada después de plantearle que **Daniela LEE `citas.tratamiento`
y se lo repite al paciente** al comprobar su cita. `test_el_tratamiento_del_cierre_NO_se_
valida_contra_el_catalogo` existe para que quien añada esa validación «por coherencia» sepa
que está deshaciendo una decisión, no arreglando un descuido.

El orden es **nombre → de qué es → fecha → agendar**, para que la cita se cree de una vez con
todo en vez de insertarla y corregirla. `conversaciones.cierre_tratamiento` sostiene el dato
entre las preguntas, en la base y no en memoria, por el mismo motivo que `cierre_pendiente`.

### El nombre, y por qué solo a veces (migración 016)

`_agendar` titulaba el evento `f"{nombre} · {tratamiento}"` con el nombre de la ficha, y la
ficha de un número nuevo dice `PENDIENTE`. O sea que la cita entraba en la agenda de la
clínica como **«PENDIENTE · Cordales»** — y ese es el caso *normal* de una cita salida de un
relevo, no el raro: el paciente nuevo con dolor agudo es justo el que más escala. El cliente
lo pidió como «que quede igual que si la hubiera hecho el paciente», y una que hace el
paciente lleva su nombre porque Daniela se lo pregunta antes de agendar.

Se pregunta **solo cuando falta** (`_nombre_del_paciente` devuelve `None` también para
`PENDIENTE`, que es un marcador y no un nombre). Con nombre de verdad, el cierre sigue siendo
dos preguntas: preguntar de más es como se consigue que el doctor deje el diálogo a medias, y
un diálogo a medias es una cita que no se registra.

Y el nombre que conteste **no pisa uno que ya exista**: `persistencia.nombrar_si_esta_pendiente`
solo escribe sobre el marcador. Es la misma regla que `asegurar_paciente` lleva desde el
principio — si el número de la casa lo usan dos personas, pisarlo haría que el historial del
primero apareciera bajo el nombre del segundo.

Si guardarlo falla, **el cierre sigue igual**: una cita con el nombre a medias vale muchísimo
más que ninguna cita, porque el cupo queda tomado y el evento existe. El nombre lo arregla un
humano desde el panel.

## Alguien borró el hilo: Telegram no lo dice, hay que preguntarlo

**No hay evento `forum_topic_deleted`** en la Bot API — al revés que cerrarlo, que sí manda
`forum_topic_closed`. Así que borrar el hilo de un paciente en relevo era el peor de los
cuatro agujeros: el relevo seguía TOMADO, Daniela callada, el doctor sin hilo donde escribir,
y **el paciente escribiendo sin que le conteste nadie** hasta que el barrido cortara por
tiempo tres horas después.

Lo detecta `barrer`, que ya corre cada 60 s y ya lee los relevos activos: una llamada por
relevo abierto a `telegram.estado_del_tema`.

### La sonda que decía que sí a todo

La primera versión preguntaba con `editForumTopic` **sin `name` ni `icon_custom_emoji_id`**,
razonando que sin nada que cambiar la llamada no tendría efecto. El razonamiento era correcto
y la conclusión, al revés: **no tiene efecto, y por eso mismo no valida el id**. Medido contra
el grupo real el 14/09/2026, sobre un tema ya borrado devolvía `ok: true`.

O sea que el agujero que venía a tapar siguió abierto entero un día, **sin un solo error en el
log ni una prueba en rojo** — las offline doblan a Telegram, así que ninguna podía verlo. Se
descubrió como se descubren estas cosas: el cliente borró un hilo y su paciente se quedó sin
nadie que le contestara, con 4 mensajes sin entregar.

Las cuatro candidatas, medidas. «Rastro» es si deja mensajes de servicio en el hilo, que a un
barrido por minuto lo llenarían de basura:

| sonda | tema vivo | tema borrado | rastro |
|---|---|---|---|
| `editForumTopic` sin argumentos | `ok: true` | `ok: true` | — |
| `editForumTopic` con `name` distinto | `ok: true` | `TOPIC_ID_INVALID` | sí |
| `editForumTopic` con `icon_custom_emoji_id: ""` | `ok: true` | `TOPIC_ID_INVALID` | sí (1) |
| **`reopenForumTopic`** | `TOPIC_NOT_MODIFIED` | `TOPIC_ID_INVALID` | **no** |

`reopenForumTopic` es la única que distingue sin dejar rastro, y además no necesita saber cómo
se llama el tema — pasar un nombre equivocado lo renombraría. Sobre un tema ya abierto no hace
nada, que es el caso normal: durante un relevo el tema está abierto siempre.

**Tarda ~3 s en enterarse** (medido el 14/09/2026): recién borrado el tema sigue contestando
`TOPIC_NOT_MODIFIED` durante unos tres segundos y solo después pasa a `TOPIC_ID_INVALID`, ya
para siempre. Al barrido le da igual —pasa como pronto un minuto después—, pero no a quien
quiera comprobar la sonda borrando un tema y preguntando de inmediato: por eso
`scripts/probar_relevo.py` sondea con espera y no de un tiro. Una guarda que falla siempre se
acaba ignorando, y esa es la única que caza el fallo de verdad. (`sendMessage` sí responde
`message thread not found` desde el segundo cero, pero no sirve de sonda: escribiría en el
hilo de un paciente cada vez que el tema está vivo.)

### Tres estados, no un booleano

- `"abierto"` — todo en orden, sigue su curso.
- `"reabierto"` — existía pero estaba **cerrado**, y la sonda acaba de abrirlo. Significa que
  se perdió un `forum_topic_closed`: si el bot está caído cuando el doctor cierra el hilo,
  Telegram deja de reintentar y ese evento no vuelve nunca. Se cierra el relevo con
  `devuelto_por_doctor` —es la cuarta salida, llegando tarde— y `cerrar` vuelve a cerrar el
  tema, así que la reapertura de la sonda no queda.
- `"borrado"` — se cierra con `tema_perdido`.
- `None` — **un timeout no es un tema borrado.** Cerrar un relevo vivo por un fallo de red
  sería peor que esperar al siguiente barrido. No se actúa.

**La lección, que es la que hay que llevarse:** ninguna prueba offline podía cazar esto, porque
todas doblan a Telegram y el doble responde lo que se le diga. Lo que lo caza es
`scripts/probar_relevo.py`, que crea un tema, lo borra y sondea los dos contra la API de
verdad. Quien cambie la sonda corre ese script, y no le basta con la suite en verde.

Y al cerrar por `tema_perdido` el hilo se **olvida** (`persistencia.olvidar_tema`), no se
marca cerrado: si la fila se quedara apuntando a un `topic_id` muerto, `asegurar_tema` lo
daría por bueno sin crear ninguno y cada archivo futuro de esa persona fallaría al
depositarse, para siempre y en silencio. Olvidarlo hace que el siguiente archivo le abra un
hilo nuevo, que es la recuperación correcta.

## Cuatro salidas, no tres — y la cuarta es cerrar el hilo

Cerrar el tema a mano **es** devolver el control, y es el gesto que sale natural al terminar
de hablar. Hasta la 6E dejaba el sistema justo en el estado que prohíbe el no negociable 15:
tema cerrado —sin canal hacia el paciente— con `tomada_por` todavía puesto, o sea Daniela
callada frente a alguien con quien ya nadie podía hablar, hasta que el barrido lo cortara
tres horas después. Lo atiende `relevo.cerrar_por_tema_cerrado`, desde el evento de servicio
`forum_topic_closed`.

**El motivo sigue siendo `devuelto_por_doctor`**: lo fue. El CHECK de la 003 sigue cerrado
con tres motivos y no hace falta un cuarto.

**No abre el diálogo de la cita, a propósito.** Preguntar deja el relevo TOMADO mientras se
espera la respuesta, y con el tema ya cerrado eso es exactamente el estado prohibido. Así
que ésta es la salida rápida —sin cita— y el botón «Listo» la completa.

Es idempotente por construcción: cuando es `relevo.cerrar` quien cierra el tema, Telegram
emite este mismo evento, pero para entonces ya no hay relevo vivo que encontrar. Sin eso
sería un bucle, con una despedida por vuelta.

## El botón de salida va ANCLADO

El botón viaja con la bienvenida, que es el PRIMER mensaje del hilo. Tras veinte frases había
que subir hasta arriba del todo para devolver el control, y el que no subía dejaba el relevo
abierto hasta el barrido. `telegram.anclar_mensaje` lo fija en la cabecera del tema;
`cerrar` lo suelta con `desanclar_todo_del_tema` —por tema y no por mensaje, para no guardar
en ninguna parte el id de la bienvenida—.

Necesita `can_pin_messages`, confirmado en este grupo el 14/09/2026. Y la despedida lleva el
botón de **volver a entrar**: cerrar es un gesto de un toque, y el barrido lo hace solo, así
que tiene que poder deshacerse con otro. Un tema cerrado no deja escribir, pero sí deja
pulsar un botón inline.

## El cierre es una conversación, no un botón

«Listo, que siga Daniela» ya no cierra de golpe: pregunta si quedó agendada una cita.

```
[Listo] → ¿quedó agendada una cita?   [Sí, quedó agendada] [No hubo cita]
   [No]  → cerrar(devuelto_por_doctor)
   [Sí]  → esperando_nombre        ← SOLO si el número no tiene nombre todavía
           esperando_tratamiento   ← de qué es la cita
           esperando_fecha         ← «DD/MM HH:MM»
             cada uno de esos mensajes del doctor se LEE, no se releva
             hora llena / fecha ilegible -> se lo dice y SIGUE esperando
             ok -> cupo + Calendar + fila, y cerrar()
```

Mientras dura, el relevo sigue **tomado**: Daniela sigue callada. Si el doctor lo abandona,
lo cierra el barrido por `tiempo_agotado`.

`cierre_pendiente` vive en la BASE y no en memoria por un motivo concreto: si el proceso
reiniciara mientras se espera la fecha, el «15/09 14:30» se le aparecería al **paciente** en
su WhatsApp. El parseo es estricto y nunca adivina «mañana a las 10» — una fecha mal
interpretada es un paciente presentándose a la hora equivocada.

## Lo único que cruza el muro hacia Daniela

`_avisar_a_daniela` escribe en el contexto del agente que le habla **directamente al
paciente**. Por ahí pasan dos hechos y ninguno es clínico:

- que hubo un relevo, con quién, y que **no sabe qué se dijeron**;
- si la hubo, la fecha de la cita, ya registrada.

**Lo que escribió el doctor no viaja nunca**, ni literal ni resumido. Esa es la razón de que
el cierre PREGUNTE en vez de resumir: así lo que cruza lo decide un humano, no un modelo.
`test_lo_que_escribio_el_doctor_NUNCA_cruza` lo sostiene y no se debilita.

## El MIME de un archivo de Telegram sale de la EXTENSIÓN

Comprobado contra la API: el servidor de archivos de Telegram responde
`Content-Type: application/octet-stream` para todo, incluida una foto cuyo `file_path` es
`photos/file_1.jpg`. Pasárselo a Meta hace que rechace la subida entera con
`(#100) Param file must be a file with one of the following types`, y el doctor ve su foto
sin entregar. Lo resuelve `canales._mime_de`.

## La ficha PENDIENTE: ya casi nunca hace falta

El relevo sabe crear la fila de `pacientes` con `nombre_completo = "PENDIENTE"` —nunca el
nombre del perfil de WhatsApp— para un número que no la tenga. Se escribió el 13/09/2026,
cuando el hilo colgaba de esa tabla y era la única forma de darle uno a un lead.

Desde la 014 **ya no se dispara casi nunca**: el hilo se crea solo con el primer archivo, sin
ficha. Queda para el caso en que el relevo es lo primero que le pasa a ese número —alguien que
solo escribió texto y a quien un doctor decide escribir—, y sigue siendo aceptable por lo
mismo de siempre: lo autoriza un humano pulsando un botón, no una cámara. Lo que sí crea
identidad verificada es esa fila, así que si algún día deja de hacer falta, quítala.

## Qué prueba qué

| | |
|---|---|
| `tests/test_relevo.py` | la lógica, con dobles. No abre una conexión |
| `tests/test_relevo_neon.py` (`-m neon`) | la carrera de dos doctores, el CHECK y el `GREATEST` |
| `tests/test_webhook_telegram.py` | la puerta y el cableado del endpoint |
| `scripts/probar_relevo.py` | que el bot TENGA los permisos en este grupo, y que haya webhook |

Lo que ninguna prueba ve: que el doctor tenga `can_manage_topics` y `can_delete_messages` en
el grupo real. Un bot sin ellos pasa la suite entera en verde y, el día que alguien pulse el
botón, el relevo se activa en la base y el hilo no se abre. Eso lo caza el script.

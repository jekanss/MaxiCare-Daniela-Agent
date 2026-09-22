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

**Mientras dura el relevo, una columna se usa al revés, y es la segunda inversión del
proyecto.** Un mensaje del paciente que entra con el relevo tomado se anota con
`fallo_respuesta` empezando por `relevo:` **sin ser un fallo**: es la marca de «esto lo
atendió un humano». Sin ella el mensaje se queda con `fallo_respuesta` NULL y sin respuesta
de Daniela, que es justo la forma que tiene `mensajes_sin_responder` de reconocer un pendiente
— se lo entregaría a Daniela media hora después y **contestaría por encima del doctor**,
delante del paciente. Quien lea esa columna como un registro de averías contará relevos como
fallos; quien la escriba sin el prefijo reabre el agujero.

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

El estado vigente de la función, que es lo que hay que recordar al tocarla: **`asegurar_tema`
ya no exige ficha y sigue sin crear ninguna.** Las dos mitades importan. Que no exija ficha es
lo que le da hilo a un lead desde su primer archivo; que siga sin crearla es lo que impide que
mandar una foto verifique a un desconocido, porque `identidad_verificada` deriva de la
EXISTENCIA de la fila en `pacientes` y de nada más.

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
fijo — y entonces `"valoracion"` no era ninguna de las catorce claves de `tratamientos`. Dos
defectos en una línea: un valor inventado donde la regla dura 3 pide marcar lo desconocido, y
una categoría fantasma en cualquier informe por tratamiento.

**Desde la 020 esa clave SÍ existe**, y no deshace nada de lo de abajo. Se creó por el otro
lado del sistema: Daniela no tenía con qué agendar a un paciente con dolor y sin diagnóstico
(no negociable 12). Las dos vías siguen siendo distintas y por el mismo motivo de siempre —
Daniela escribe una clave del catálogo, validada; el doctor que acaba de hablar con el
paciente escribe lo que quiera, sin validar, porque sabe de qué es la cita mejor que un
catálogo. Lo que se acabó es que Daniela no tuviera NINGUNA forma de decirlo.

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

## Un ESCALAMIENTO abre el hilo. Un texto sigue sin abrirlo (16/09/2026)

El no negociable 14 dice que un texto va mudo al tema de su paciente «o a ninguna parte si no
tiene tema — nunca lo crea: eso es del primer archivo». Sigue siendo cierto, y la razón sigue
en pie: cada «hola» de un número equivocado estrenaría expediente.

Pero había una ventana sin dueño. Entre que un número se queda sin hilo y que un archivo se
lo vuelva a abrir, **sus textos no se archivan en ninguna parte**. Medido en producción:

    23:57  /clearstate                   -> borra la fila de temas_telegram
    23:57  "Hola buenas noches"          -> telegram_message_id NULL, a ningún sitio
    23:57  "Quiero sacarme una muela"    -> a ningún sitio
    23:57  "Duele mucho?"                -> a ningún sitio
    23:58  escalamiento dato_faltante    -> al GENERAL
    23:59  el botón del relevo abre el hilo nuevo

Lo único que el doctor vio de esa persona fue el escalamiento del General, cuyo resumen
parafrasea lo que acababa de preguntar. **De ahí venía el informe de que «los mensajes del
paciente caen en el General»: no caían ahí, no caían en ninguna parte**, y el escalamiento
era la única huella. El expediente del paciente, entretanto, vacío.

`lectura.rescatar_hilo` es ahora la única puerta por la que algo que no es un archivo abre un
hilo. Lo que decide **no es el texto, es el escalamiento**: un número equivocado no hace
escalar a Daniela, así que no estrena expediente; el paciente con dolor, sí. Cuelga de los
dos sitios que escalan --`herramientas._escalar_a_doctores`, la tool, y
`runtime._avisar_a_doctores`, la red de seguridad-- porque el turno puede escalar por
cualquiera de los dos (no negociable 26).

Cuatro decisiones que parecen de estilo y no lo son:

- **Un solo mensaje con todas las frases**, no uno por frase. Son mudas, pero veinte
  depósitos seguidos convierten el expediente en un muro por el que hay que bajar.
- **Se marca DESPUÉS de enviar**, como el aviso de la política (no negociable 24) y al revés
  que un recordatorio (21). Aquí el riesgo es dejar constancia de un volcado que nunca salió
  --y perder esas frases para siempre--; repetir un volcado es inocuo.
- **`fallo IS NULL` en la consulta.** Un mensaje con `fallo` sí se intentó entregar y está
  registrado como perdido: volcarlo aquí lo borraría del índice por el que se vigila lo que
  de verdad falló.
- **El HTML se escapa.** `enviar_mensaje` va en `parse_mode=HTML` y RECHAZA el mensaje
  entero si no cierra: un paciente que escriba «me duele el <3» dejaría el rescate en nada.

Idempotente por construcción: lo volcado queda con su `telegram_message_id`, así que
`_textos_sin_archivar` deja de devolverlo y el siguiente escalamiento no lo repite.

**`/clearstate` sí borra el tema en Telegram, y funciona.** Se comprobó el 16/09/2026 porque
parecía lo contrario: `reseteo.resetear` llama a `telegram.borrar_tema` y el log dijo
`tema_borrado=True`. El bot tiene `can_delete_messages` y `can_manage_topics`. Lo que sí
falla a veces es el borrado de los mensajes sueltos previos (`message to delete not found`),
y da igual: el tema se los lleva a todos.

## La puerta va a los DOS sitios, y el General no basta (17/09/2026)

El doctor no vive en el General. Vive en el hilo del paciente: ahí ve llegar sus mensajes y
ahí está su expediente. El escalamiento colgaba «Hablar yo con el paciente» **solo en el
General**, así que un doctor mirando el hilo veía al paciente insistir sin ninguna señal de
que Daniela había pedido ayuda, y sin nada que pulsar.

El caso, reconstruido de `escalamientos`, de los logs del VPS y de la API de Telegram:

    06:32  paciente: "me quiero sacar una muela"
    06:33  escalamiento dato_faltante -> General, msg 689, CON botón
    06:34  el doctor lo pulsa; habla con el paciente
    06:35  "Listo, que siga Daniela" -> relevo cerrado (devuelto_por_doctor)
           el doctor BORRA el hilo Y BORRA el mensaje 689 del General
    06:37  cuatro preguntas del paciente -> "su número no tiene tema", a ningún sitio
    06:40  escalamiento excepcion_comercial -> General, msg 713, CON botón
           rescatar_hilo recrea el hilo (714) y vuelca las cuatro frases (716)

Todo funcionó. El sistema mandó la puerta nueva --comprobado con `editMessageReplyMarkup`
contra la API: el 689 responde `message to edit not found` (borrado) y el 713 responde
`message is not modified ... exactly the same` (vivo, con el botón puesto)--. Pero cayó en el
General, y el doctor estaba en el hilo. Desde su lado, el sistema había dejado de ofrecerle
tomar la conversación.

**Y se salvó por casualidad:** el motivo cambió (`dato_faltante` -> `excepcion_comercial`).
Con el mismo motivo, `escalamiento_vivo_con_motivo` habría callado el aviso --el del 06:33
seguía con `respondido_en` NULL pese a que un humano lo atendió y lo devolvió-- y con el
aviso se habría callado **también el hilo**, porque `rescatar_hilo` cuelga de él. Ver el no
negociable 26 y `persistencia.marcar_escalamientos_respondidos`.

`relevo.ofrecer_la_puerta_en_el_hilo` lo cierra, y la cuelgan los dos sitios que escalan.
Cuatro decisiones:

- **Al hilo baja el motivo y el botón, NUNCA el resumen ni la pregunta.** Los escribe el
  modelo para los doctores: son la deliberación del caso, y su sitio es el General. Que
  `relevar_mensaje` filtre por `is_bot` --lo que escribe el bot no se le reenvía al paciente--
  hace esto seguro, pero no es la razón de recortarlo: un expediente no es el sitio de la
  deliberación aunque nadie de fuera pueda leerlo. Lo fija
  `test_el_escalamiento_deja_la_puerta_en_el_hilo_DEL_PACIENTE_pero_no_el_resumen`, junto a
  `test_el_escalamiento_va_al_tema_general_y_nunca_al_del_paciente`, que no se tocó.
- **El botón va por TELÉFONO (`teclado_tomar`, `PREFIJO_TOMAR_TEL`), no por id de
  conversación.** Este mensaje se queda en el expediente para siempre y una conversación
  caduca a las 24 h: con el id dentro, pulsarlo al día siguiente contestaría «esa conversación
  ya no existe». El del General sí va por id (`teclado_tomar_conversacion`): ahí el aviso es
  del turno que acaba de escalar.
- **Suena.** Segunda y última excepción a la NOTA DEL SILENCIO de `canales.py`, tras la
  bienvenida del relevo. Un aviso mudo en un hilo que el doctor no tiene abierto no avisa.
- **No propaga.** El escalamiento ya salió por el General, que es la garantía; esta es la
  puerta cómoda, no la única.

Y la red de seguridad (`runtime._avisar_a_doctores`) mandaba su aviso al General **sin
teclado**, al revés que la tool. Ese camino corre justo cuando el modelo no llamó a ninguna
tool --el turno se rompió solo, o cerró con la bandera puesta--: el aviso de los casos en que
el sistema menos sabe qué hacer era el único que llegaba sin puerta. Hoy los dos usan
`relevo.teclado_tomar_conversacion`.

## El botón que aparecía y no servía: `reabrir_tema` sobre un hilo borrado (17/09/2026)

La puerta de la sección anterior no bastaba, y el motivo estaba un paso más allá: **el botón
aparecía, y al pulsarlo el relevo se deshacía.** Medido en producción:

    08:32:32  turno 5 escalado por clinico          ← el botón sale
    08:32:35  ERROR  no se pudo abrir el hilo de +57...; se deshace el relevo
              ErrorDeCanal: Telegram no reabrió el tema: Bad Request: TOPIC_ID_INVALID

El doctor había borrado el hilo. La fila de `temas_telegram` seguía apuntando al `topic_id`
muerto —borrar un tema no emite ningún evento, y `barrer` solo sondea a quien está EN RELEVO,
así que fuera de un relevo nadie la limpia—. `_tema_abierto_para` llamaba a `reabrir_tema`,
Telegram rechazaba, y `activar` lo trataba como «no hay hilo»: deshacía el relevo entero.

**Y el segundo intento fallaba igual.** Ese camino cierra con `_cerrar_en_base` directo, no
con `relevo.cerrar`, así que **no llama a `olvidar_tema`**: la fila muerta se quedaba puesta
hasta que por casualidad llegara un texto del paciente y fuera `ingesta` quien la olvidara.
Desde el lado del doctor: «no puedo volver a tomar la conversación», indefinidamente.

Dos piezas, una por capa:

- **`canales.reabrir_tema` lanza `HiloInvalido`**, no un `ErrorDeCanal` cualquiera, cuando
  `es_hilo_invalido` reconoce el rechazo. Quien llama tiene que poder distinguir «Telegram
  falló» de «alguien borró este hilo»: la reacción es opuesta.
- **`_tema_abierto_para` lo olvida y abre uno nuevo.** Un hilo borrado no es un fallo: es un
  hilo que hay que rehacer. Misma reacción que `ingesta` ante `HiloInvalido` y que `cerrar`
  con `tema_perdido`. Faltaba justo en el único camino por el que el doctor entra.

**`crear_tema` sigue SIN `try`**, y la asimetría es la que importa: si tampoco se puede crear,
eso sí es un fallo de Telegram y quien llama tiene que deshacer el relevo. Es la diferencia
entre «no hay hilo» y «no hay Telegram», y `test_si_el_hilo_no_se_puede_abrir_el_relevo_se_
deshace` sigue vigilando ese lado.

**La lección repetida:** ninguna suite offline vio esto, porque el doble de Telegram responde
lo que se le diga. Lo que lo destapó fue un ejercicio real y los logs del VPS. La prueba que
lo fija ahora dobla httpx con la respuesta LITERAL de Telegram
(`test_reabrir_un_tema_borrado_lanza_HiloInvalido_y_no_un_error_cualquiera`), con su falso
positivo al lado: un `not enough rights` tiene que seguir llegando como `ErrorDeCanal`, o cada
tropiezo pasajero de Telegram le abriría un hilo nuevo a un paciente cuyo expediente está vivo.

## Y la otra mitad del mismo renglón: el tema que YA estaba abierto (17/09/2026)

El arreglo de arriba dejó la puerta cerrada solo a medias, y lo destapó la comprobación de
punta a punta del ciclo entero --no la suite--: `reopenForumTopic` sobre un tema que **ya
está abierto** responde `Bad Request: TOPIC_NOT_MODIFIED`, y eso subía como `ErrorDeCanal`
genérico. Mismo desenlace exacto que el hilo borrado: `_tema_abierto_para` lo propaga,
`activar` lo lee como «no hay hilo» y **deshace el relevo entero** --esta vez con el hilo del
paciente perfectamente vivo delante--.

La incoherencia estaba a la vista: **`estado_del_tema` y `reabrir_tema` llaman al MISMO
`reopenForumTopic`**, y la tabla medida que vive en el docstring de `estado_del_tema` ya
decía que `TOPIC_NOT_MODIFIED` es «abierto». Una de las dos sondas lo leía como un sí y la
otra como un fallo.

`TOPIC_NOT_MODIFIED` es un SÍ. Lo que `reabrir_tema` promete no es «haber cambiado algo», es
**dejar el tema abierto**, y ese es justo el estado al que se llega. Ahora devuelve sin lanzar.

Cómo se llega a un tema abierto fuera de un relevo, que no es un caso de laboratorio:

- un doctor lo reabre a mano en Telegram --el mismo doctor que borra topics a mano--;
- `lectura.asegurar_tema` no consiguió cerrarlo al crearlo (`quedo_abierto`, que ya se
  registra como ERROR porque deja un canal hacia el paciente sin vigilar);
- el cierre del relevo anterior falló en `cerrar_tema` --`cerrar` lo registra y sigue--.

Lo fija `test_reabrir_un_tema_QUE_YA_ESTABA_ABIERTO_no_es_un_fallo`, con la respuesta literal
de Telegram doblada en httpx. **Y la lección, otra vez la misma:** las dos mitades de este
renglón las encontró recorrer el ciclo completo contra la API de verdad, no el doble.

## Lo que de verdad rompía el ciclo: el AVISO borrado del General (17/09/2026)

Las tres secciones de arriba arreglan el hilo. Ninguna arregla el caso que el doctor
reportaba, y los logs del VPS lo dijeron a la primera:

    09:14:07  relevo cerrado (devuelto_por_doctor)      ← «Que siga Daniela»
    09:14:22  escalamiento 127, telegram_message_id=855 ← el aviso sale, con su botón
              (el doctor borra el topic Y borra el 855 del General)
    09:15:23  turno 3 ... ya tiene un escalamiento por clinico; no se repite
    09:16:16  turno 4 ... ya tiene un escalamiento por clinico; no se repite

`escalamiento_vivo_con_motivo` calla todo escalamiento del mismo motivo mientras haya uno
«delante del doctor y sin responder», y eso lo medía por `telegram_message_id IS NOT NULL`.
**Esa premisa se cae en cuanto el doctor borra el mensaje** --que es lo que hace para dejar el
General limpio de un paciente ya atendido--. La fila 127 siguió diciendo «lo tiene delante»
con el 855 ya borrado, y a partir de ahí se calló TODO durante las 24 h de vida de la
conversación: sin aviso, sin botón y **sin hilo**, porque `rescatar_hilo` cuelga del aviso.

Es el mismo fallo por tercera vez, y conviene decirlo así de claro: **el sistema daba por
vivo un objeto de Telegram porque algún día lo estuvo.** Borrar un mensaje no emite ningún
evento, igual que borrar un tema.

Ahora la guarda tiene dos mitades, y `runtime._el_doctor_ya_lo_tiene_delante` las junta:

1. **La base** (`escalamiento_vivo_con_motivo`, que ya no devuelve un `bool` sino
   `(telegram_message_id, lo tomó un doctor)`).
2. **Telegram** (`canales.aviso_sigue_puesto`), que es `editMessageReplyMarkup` con el MISMO
   teclado: si el mensaje está igual responde `message is not modified` y **no toca nada**.
   Sonda sin rastro, como `reopenForumTopic`. Si responde `message to edit not found`, lo
   borraron y el siguiente aviso SALE.

Dos cosas que no se pueden mover:

- **Un aviso ya TOMADO no se sondea.** `relevo.activar` le cambia el teclado al mensaje del
  General por el enlace «Ir al hilo» y marca `relevo_activado` (un BOOLEAN, no una marca de
  tiempo --lo cazó la suite de Neon--). Sondear ahí con el teclado de tomar le devolvería el
  botón a una conversación que alguien ya tiene.
- **Ante la duda se AVISA.** `aviso_sigue_puesto` devuelve `None` cuando Telegram no contesta,
  y eso no se toma por «sigue puesto». Un aviso de más es ruido; uno de menos es un paciente
  con dolor que nadie ve, y el principio que decide los empates de este proyecto está escrito.

**Y esto NO reabre el ruido del 16/09.** Lo que se calla sigue siendo lo mismo: un aviso que
el doctor tiene delante sin responder. Lo único que cambia es que ahora «tenerlo delante» se
comprueba en vez de suponerse.

## La ventana de los segundos: un tema borrado que todavía dice que sí (17/09/2026)

Medido contra la API recorriendo el ciclo entero: durante **varios segundos** después de
`deleteForumTopic`, Telegram sigue respondiendo `ok: true` a `reopenForumTopic` --«lo he
reabierto»-- mientras `sendMessage` sobre ese mismo tema ya rechaza con `message thread not
found`. Los ~3 s que decía el docstring de `estado_del_tema` se quedan cortos.

En esa ventana `_tema_abierto_para` da el hilo muerto por bueno --no tiene con qué verlo-- y
la bienvenida revienta. El `except` de fuera de `activar` se lo tragaba y dejaba **el peor
estado posible: `tomada_por` puesto --Daniela callada-- y ningún hilo por el que hablarle al
paciente**, sin que nadie se entere hasta que el barrido corta por tiempo agotado.

No es de laboratorio: es exactamente lo que hace un doctor que borra el topic y sigue
probando con el mismo paciente. `activar` reintenta UNA vez --olvidar la fila, crear tema,
bienvenida-- y si el segundo intento también falla, eso sí es Telegram y el relevo se deshace.
Lo fija `test_si_la_bienvenida_cae_en_un_hilo_MUERTO_el_relevo_se_rehace_en_uno_NUEVO`.

## El barrido solo mira relevos VIVOS, y ahí quedaba el agujero (16/09/2026)

La sección de arriba resuelve el hilo borrado **de un paciente en relevo**: lo sondea
`barrer`, cierra con `tema_perdido` y llama a `persistencia.olvidar_tema`. Pero `barrer`
itera sobre `_activos`, que filtra por `tomada_por IS NOT NULL`.

**El hilo de alguien que NO está en relevo no lo sondeaba nadie.** Y como borrar un tema no
emite ningún evento, la fila de `temas_telegram` se quedaba apuntando a un `topic_id` muerto
para siempre. El propio docstring de `olvidar_tema` ya describía la consecuencia palabra por
palabra —«cada archivo que mandara esa persona fallaría al depositarse. Para siempre, y en
silencio»— pero solo se cerraba la puerta del relevo.

Lo que se midió contra la API el 16/09/2026, y por qué importa cada fila:

    tema ABIERTO                 -> deposita
    tema CERRADO                 -> deposita  (el bot es admin: es el estado NORMAL)
    tema CERRADO + silencioso    -> deposita
    tema BORRADO                 -> "Bad Request: message thread not found"

La tercera fila es la que impide el arreglo ingenuo: **los temas de paciente se crean
cerrados**, así que tratar «cerrado» como «muerto» le borraría el expediente a todos. Y la
cuarta descarta la otra sospecha: Telegram **no** degrada al General por su cuenta, rechaza.

Ahora `canales.es_hilo_invalido` mira el rechazo y `enviar_mensaje`/`enviar_archivo` lanzan
`HiloInvalido`, que hereda de `ErrorDeCanal` —todo lo que ya lo capturaba sigue igual—.
`ingesta` reacciona distinto según qué llegue, y la asimetría es deliberada:

| Llega | Qué pasa |
|---|---|
| Un **texto** | Se olvida el hilo y **no se archiva**. Un texto nunca abre hilo (no negociable 14), y mandarlo al General es justo lo que ese no negociable prohíbe. |
| Un **archivo** | Se olvida el hilo y **cae al General**, sonando. Degradar es aceptable; perder el archivo no. |

Dos detalles que parecen de estilo y no lo son. El texto **no se marca como fallo**: el
estado al que llega es el del número sin tema, que es normal y ya tiene su registro
(`reenviado_en` puesta, `telegram_message_id` nulo); marcarlo llenaría de falsos positivos el
índice por el que se vigila lo que de verdad se perdió. Y en la rama del archivo se reasigna
`tema = None`, de donde cuelgan las otras tres decisiones: dónde deposita el lector, que el
envío suene, y que **no** salga el aviso «están en su tema» —porque ya no es verdad—.

`es_hilo_invalido` compara contra una lista corta y explícita, nunca contra un «not found»
suelto: un error de permisos o un límite de tasa son pasajeros, y tratarlos como hilo muerto
costaría un expediente.

**Ninguna prueba offline cubre el eslabón con la API.** Las cuatro de
`tests/test_ingesta.py` doblan Telegram, así que prueban la reacción, no el reconocimiento
del rechazo real. Quien toque `es_hilo_invalido` o `_HILO_MUERTO` mide contra el grupo de
verdad: crear tema, borrarlo, esperar los ~3 s que Telegram tarda en enterarse, y comprobar
que sale `HiloInvalido` y no un `ErrorDeCanal` genérico.

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

El orden `cupo + Calendar + fila` no es arbitrario y `tomar_cupo` va primero a propósito:
**es lo único que impide que la clínica le dé esa hora a otro**. Puede decir que no, y esa
negativa es el caso normal, no el raro — el doctor está tecleando una hora que eligió
hablando con el paciente, sin mirar la agenda. Por eso el diálogo se lo dice y SIGUE
esperando en vez de abortar el cierre: el relevo sigue tomado y él escribe otra. Un cierre
que creara la fila sin pasar por el cupo mandaría a dos personas a la misma hora, que es
exactamente el fallo que este proyecto no se permite.

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

## La ficha PENDIENTE: QUITADA el 16/09/2026

**El relevo ya no crea ninguna fila en `pacientes`.** Esta sección decía «si algún día deja de
hacer falta, quítala», y ese día llegó por la peor vía: hizo daño en producción.

Existía desde el 13/09/2026, cuando el hilo colgaba de `pacientes.telegram_topic_id` y era la
única forma de darle uno a un lead. La 014 movió el hilo al teléfono y la ficha se quedó por
inercia, defendida por lo único que seguía haciendo: dar identidad verificada.

**Eso era el daño, no el beneficio.** Medido con una paciente de prueba el 16/09/2026:

```
doctor pulsa «Hablar yo con el paciente»  →  ficha con nombre = 'PENDIENTE'
doctor cierra el hilo SIN agendar          →  nadie le pregunta el nombre
                                              (el cierre solo lo pregunta si hubo cita,
                                               y es lo único que pisa el marcador)
Daniela retoma:
  _leer_estado  →  identidad_verificada = True
                   nombre_paciente      = 'PENDIENTE'
                   telefono_sin_paciente = False   ← pierde el permiso de su 1ª cita
  «Sora Patricia Delgado»  →  no coincide con 'PENDIENTE'  → intento 1
  «Es primera vez»          →  intento 2 agotado → la tool ordena escalar
```

Ni desconocida —que puede pedir su primera cita— ni verificada: **el único hueco sin salida de
los tres**, y sin un solo error en ningún log. La paciente se fue sin cita.

**Quién registra el nombre, que es la pregunta que decide si quitarla era seguro: quien
AGENDA.** Nunca fue el relevo.

| Quién agenda | Dónde se crea la ficha, con el nombre de verdad |
|---|---|
| Daniela | `herramientas._crear_cita` → `persistencia.asegurar_paciente` |
| El doctor, al cerrar | `relevo._nombrar` → `nombrar_si_esta_pendiente`, que **crea la fila si no hay ninguna** |

Esa última mitad es la que hace seguro el cambio, y la fija
`test_el_nombre_del_cierre_crea_la_ficha_si_el_relevo_ya_no_la_dejo` corriendo la función real
contra una conexión falsa: doblarla con un `lambda` dejaría pasar en verde el día que alguien
la convierta en un `UPDATE`.

### El marcador sigue existiendo, y tres sitios tienen que conocerlo

Vive en `persistencia.NOMBRE_PENDIENTE` —**un solo sitio**; hasta ese día solo `relevo` sabía
qué era, y los otros dos módulos lo trataban como el nombre del paciente—. Sigue haciendo
falta por las fichas que quedaron de antes, que son justo las de los números ya relevados:

- `relevo._nombre_del_paciente` lo devuelve como `None` (lo que evitaba «PENDIENTE · Cordales»
  en la agenda);
- `atencion._leer_estado` no lo cuenta como identidad ni lo pasa como nombre — el `id` de la
  ficha sí sale, porque es una fila real a la que apuntan sus citas;
- `herramientas._identificar_paciente` lo trata como «este número no está registrado», que es
  lo que devuelve el permiso de la primera cita.

Y **agendar repara la ficha vieja**: `asegurar_paciente` escribe encima del marcador —y solo
del marcador—, así que el primer paciente que agende deja de estar marcado. Sin eso se
quedaban así para siempre. Lo sostienen dos pruebas de Neon, la que repara y su falso
positivo: `test_agendar_NO_le_pisa_el_nombre_a_un_paciente_de_verdad`, que es la mitad que no
se puede aflojar —la hija que agenda desde el teléfono de la casa no puede renombrar la ficha
de su madre—.

## La tercera puerta: el panel (22/09/2026)

Hasta la pantalla de Conversaciones, un relevo solo se abría desde Telegram. Ahora también
desde el panel, y **es la misma maquinaria**: `relevo.activar`, `relevo.cerrar`, `_agendar`.
Lo que vive en `relevo.escribir_desde_el_panel` y `relevo.cerrar_desde_el_panel` es el
cableado, no una segunda implementación. Tres cosas que hay que saber antes de tocarlo:

- **`activar` devuelve `str | None`, y el `None` significa «es tuya».** Un POST tiene que
  poder contestar 409 con el nombre del que se adelantó, y preguntar antes con una lectura
  suelta deja en medio la ventana por la que pasan los dos doctores. Quien decide sigue
  siendo el `UPDATE ... WHERE tomada_por IS NULL`; lo único que cambió es que su respuesta ya
  no muere dentro de la función. La bandera `ya_es_suya` es lo que impide que un fallo
  adornando el hilo --anclar, volcar-- se cuente como «no la tomaste»: el panel pintaría un
  error sobre una conversación que en Neon ya está a su nombre.
- **`callback_id` acepta `None`.** Pasárselo igual a Telegram es una llamada que ya sabemos
  que va a fallar, y el acuse que traería es para un botón que nadie pulsó.
- **El cierre con cita AGENDA, y si no puede no cierra.** Es la trampa que traía el plan de
  esa pantalla escrita al revés: `cerrar(cita=...)` solo se lo CUENTA a Daniela
  (`_nota_del_relevo`), no crea nada. Cerrar sin pasar por `_agendar` deja al doctor
  marcando «sí hubo cita», a Daniela creyendo que existe y al paciente presentándose en una
  clínica donde nadie lo espera: el no negociable 1 por la puerta de atrás. El panel hace lo
  mismo que el diálogo del hilo -- cupo, Calendar, fila, y si el cupo dice que no, se lo dice
  y el relevo **sigue abierto** para que escriba otra hora.

Dos asimetrías deliberadas con el carril de Telegram:

| | Telegram | Panel |
|---|---|---|
| Un envío que WhatsApp rechaza | se anota y se sigue | se anota **y propaga** (502) |
| El `tratamiento` del cierre | texto libre, sin validar | **validado** contra `vocabulario_activo` |

La primera es porque allí el doctor ya ve su mensaje escrito en el hilo y lo único que cabe
es reaccionar, mientras que aquí está mirando la pantalla y esperando la respuesta. La
segunda no deshace la decisión del cliente de 14/09/2026: esa era sobre el doctor tecleando
a mano en mitad de una conversación. En el panel hay un desplegable, así que un valor fuera
del catálogo solo puede ser una pantalla desincronizada de la tabla.

Y el eco: lo que se escribe desde el panel baja **mudo** al hilo del paciente, para que las
dos ventanas enseñen lo mismo. **No abre hilo si no hay** (no negociable 14): un texto nunca
estrena expediente.

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

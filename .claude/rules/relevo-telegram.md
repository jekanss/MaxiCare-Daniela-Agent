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

## Un número sin ficha SÍ puede recibir el relevo

`lectura.asegurar_tema` se niega a crear la fila de `pacientes` —un desconocido que manda una
foto no puede volverse paciente verificado por mandarla—. El relevo sí la crea, con
`nombre_completo = "PENDIENTE"` y nunca el nombre del perfil de WhatsApp. La diferencia es
quién lo decide: allá lo dispara una cámara, aquí un humano pulsando un botón para hablar con
esa persona. Y el precio de no hacerlo era peor: el número sin ficha es el paciente nuevo con
dolor agudo, el que más escala.

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

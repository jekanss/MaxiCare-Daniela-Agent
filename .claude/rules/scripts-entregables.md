---
paths:
  - "scripts/**"
---

# Los scripts de entregable

Cuál gasta tokens y cuál no está en la tabla del `CLAUDE.md` raíz, que carga siempre. Aquí
está el porqué de cada uno: lo que hace falta al **tocarlos**, no al usarlos.

## La trampa que los cubre a todos

**Estos scripts doblan funciones de `src/` con firmas escritas a mano, y `pytest -q` no los
corre.** Añadirle un parámetro a algo que un script dobla —`lectura.leer_archivo`,
`conversacion.responder`— los rompe en silencio: la suite entera sigue verde. Pasó en la fase
7, y solo apareció al ejecutarlos (`TypeError: lector_doblado() got an unexpected keyword
argument 'group_id'`). **Quien cambie una de esas firmas corre los seis que no gastan.**

## Uno por uno

- `probar_atencion.py` escribe en `pruebas_atencion` —lo crea y lo borra comprobando el
  borrado— y su WhatsApp es falso: no le llega nada a ningún paciente. **Su `--chat` corre
  bajo un `SelectorEventLoop`**, como el de la fase 7: desde que `atender` pide la sesión a
  `SQLAlchemySession`, el `ProactorEventLoop` de Windows la rechaza en el propio `connect()`
  y el script moría con «a `responder` no se le llamó ni una vez», que no nombra la causa.
  Solo pasaba con `--chat` —el modo que gasta—, así que estuvo roto toda la fase 7 sin que
  nadie lo viera. En Linux, donde corre el VPS, no aplica.
- `probar_lectura.py` escribe en `pruebas_lectura` —mismo patrón de creación y borrado
  comprobado—; su Telegram y su WhatsApp son falsos, y el lector va doblado salvo con
  `--chat`, donde además corre una vez de verdad sobre un PDF generado en el momento (no
  versionado) y el evaluador clínico corre sobre dos frases fijas.
- `probar_persistencia.py` escribe en `pruebas_persistencia` —mismo patrón de creación y
  borrado comprobado—; su `--chat` **no levanta uvicorn ni usa el chat web**: hace el
  reinicio con DOS intérpretes de Python sobre el carril de WhatsApp, porque el chat web
  pierde el reenganche al reiniciar por diseño (ver `runtime._conversaciones_de_prueba`) y
  probaría lo contrario. Y comprueba que el nombre NO está en `pacientes`: si estuviera, el
  segundo turno acertaría con el historial borrado.
- `probar_relevo.py` **no toca la base en absoluto** —ni un esquema de pruebas— y no crea
  ningún paciente ni ningún relevo: abre un tema de usar y tirar en el grupo REAL, lo cierra,
  lo reabre, le manda un mensaje con teclado, le cambia el teclado, reacciona y lo borra. Lo
  que prueba es lo que ninguna suite puede: que el bot tenga `can_manage_topics` y
  `can_delete_messages` **en este grupo concreto**. Sin ellos, `pytest -q` pasa entero en
  verde y el relevo se activa en la base con el hilo sin abrir. Si sale `FALLA borrar_tema`,
  el tema `[PRUEBA DE RELEVO] borrar` se queda en el grupo y hay que quitarlo a mano.
- `configurar_webhook_telegram.py` **no es una prueba: es un interruptor**, y el único que
  enciende el relevo. Telegram no valida la URL como hace Meta, así que sin llamarlo el botón
  no hace nada y no hay error en ningún log. `--url` genera el secreto si el `.env` no lo
  tiene y lo imprime UNA vez para pegarlo; pasa `drop_pending_updates` para que al encender
  no entre de golpe todo lo que el grupo acumuló. **Y pone en conflicto a
  `obtener_chat_telegram.py`**: con webhook activo, su `getUpdates` devuelve 409 (`getChat`,
  `getChatMember` y `getChatAdministrators` siguen bien). Para usarlo, `--quitar`, y volver a
  ponerlo después.
- `probar_panel.py` MITAD A cambia un precio por HTTP contra `public`, la base real de la
  clínica, y **lo restaura en un `finally` comprobando la restauración con una aserción**.
- `probar_calendario.py --diagnosticar` **solo lee**: es lo primero que hay que correr
  cuando Calendar «no funciona».
- `probar_atencion.py` tiene **un fallo intermitente conocido**, y no es del producto: la
  comprobación «el segundo turno empezó sin esperar al primero (el candado es POR
  conversación)» compara el orden de eventos de dos corrutinas que tardan 0,15 s. Si la
  latencia de Neon hace que la segunda llegue tarde al candado, la secuencia sale
  `entra:1, sale:1, entra:2, sale:2` y la prueba lo lee como un candado global. Medido el
  13/09/2026 sobre `main` limpio: **falla aproximadamente una de cada dos corridas**, sin
  ningún cambio de por medio. Si aparece, repite antes de investigar; si falla dos veces
  seguidas, ahí sí hay algo.
- `probar_tools.py`: su helper `hora(n)` cuenta **bloques hábiles**, no horas de reloj. Era
  `ahora + 45 días + N horas` y dejó de valer cuando la rejilla aprendió el horario de la
  clínica: corriendo de noche, la base caía fuera de jornada y las tools rechazaban todo
  —seis comprobaciones en rojo, incluida la de concurrencia que cierra la fase 3—. Quien
  añada un `hora(N)` grande no está pidiendo N horas después.
- `probar_agentes.py` es el único que gasta siempre, y gasta en TRECE bloques. Iterar sobre
  la conducta de un bloque pagando los otros doce es la forma más cara de trabajar aquí;
  el cliente ya lo señaló. Los bloques comparten teléfono salvo donde se parametriza
  `nuevo_contexto`, así que lo que siembra un bloque sigue vivo en el siguiente: un `FALLA`
  puede ser del escenario y no del modelo.
- `probar_recordatorios.py` escribe en `pruebas` —el mismo esquema y el mismo molde
  (`url_de_pruebas`, `montar_esquema`, `limpiar`) que `probar_tools.py`— y no gasta un token:
  el despachador corre con `whatsapp=None` o con la plantilla vacía en todo el camino. Dobla a
  mano las firmas de `h._crear_cita`, `h._reprogramar_cita`, `h._cancelar_cita` y de
  `persistencia.seguimientos_por_despachar`; quien les cambie la firma rompe este script en
  silencio, igual que a los demás de esta lista. Su trampa es la comprobación 6: exige la
  conexión DIRECTA de Neon (sin `-pooler.`) porque necesita dos sesiones de verdad compitiendo
  por la misma fila con `FOR UPDATE ... SKIP LOCKED` — con el pooler, PgBouncer reparte las
  dos entre sesiones compartidas y la comprobación pasa en verde sin haber probado nada. La
  comprobación 7 es la única prueba contra Postgres real de `persistencia.marcar_seguimiento_
  enviado`: llama la función dos veces sobre la misma fila y exige `True` la primera vez y
  `False` la segunda — es el único guardián real contra mandar el mismo recordatorio dos
  veces, y hasta este script ninguna prueba lo había ejercitado contra la base de verdad.

## `medir_historial.py`

Es el único camino para cerrar el límite MEDIDO del historial: mientras no haya **20 turnos
en 5 conversaciones con filas en `public.agent_messages`** el script lo dice y esos cuatro
números siguen en `PENDIENTE`. Exige desplegar primero: hasta que corra en producción no hay
nada que medir.

Ojo con no confundirlo con el otro número: `config.LIMITE_HISTORIAL_SESION = 230` es un
**tope de seguridad** derivado del techo de tokens de la cuenta —impide que un historial
crezca hasta reventar la petición y dejar al paciente atascado en el mensaje seguro—, no la
medición.

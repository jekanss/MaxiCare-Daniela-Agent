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
  Desde la fase 8 **siembra también un día de agenda en `public`**: una conversación y tres
  citas (dos de ayer, una de mañana) con un teléfono imposible, `TELEFONO_SEMBRADO`. Tres
  cosas suyas que no se pueden cambiar sin entender qué sostienen:
  - **Las citas se insertan con SQL, con `evento_calendar_id`, `reserva_id` y `paciente_id`
    en NULL.** No es pereza: `reconciliar_con_calendar` se salta toda cita sin evento, así
    que la siembra no se contrasta contra Google ni puede acabar cancelada por él; no toca
    ningún cupo; y no crea ninguna ficha en `pacientes`. Sembrar con `crear_cita` le dejaría
    al doctor un evento fantasma en su calendario real y una hora ocupada.
  - **`TestClient(app)` sin `with` no dispara los `startup`**, así que `runtime._calendario`
    es `None` y la agenda no reconcilia nada. El script lo **afirma**
    (`calendario_disponible` tiene que ser `False`) en vez de callárselo: el día que alguien
    envuelva ese cliente en un `with`, esas aserciones caen y le avisan de que acaba de
    poner a un entregable a hablar con el Google Calendar de la clínica.
  - **La limpieza va por TELÉFONO, no por los ids que devolvió la siembra**, que no existen
    si la siembra revienta a mitad. Y **sí borra sus filas de `cambios_configuracion`**, al
    revés que el cambio de precio: aquellas cuentan algo cierto sobre una ficha real, estas
    apuntarían con `clave` al UUID de una cita que el propio script acaba de borrar.
- `probar_calendario.py --diagnosticar` **solo lee**: es lo primero que hay que correr
  cuando Calendar «no funciona».
- `probar_transcripcion.py` **imprime las frases en vez de contar OK**, y esa es su razón de
  ser. Tres de los cuatro modelos de audio de la cuenta devuelven un `200` con algo que parece
  español y no lo es —«Con xenáula se una doctora»—, así que una comprobación automática los
  daría por buenos. Quien lo corra tiene que LEER lo que salió. Usa audios REALES sacados de
  `mensajes_entrantes` (solo lee) y no uno sintetizado: lo que hay que comprobar es que la API
  acepte lo que graba el WhatsApp de un teléfono, no lo que sepa generar esta máquina. Si Meta
  ya caducó los `media_id` guardados lo dice y sale con 1 — no finge que pasó. `--comparar`
  corre los otros tres modelos al lado, que es como se eligió el que corre.
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
- `probar_agentes.py` es el único que gasta siempre, y gasta en CATORCE bloques. Iterar sobre
  la conducta de un bloque pagando los otros trece es la forma más cara de trabajar aquí;
  el cliente ya lo señaló. Los bloques comparten teléfono salvo donde se parametriza
  `nuevo_contexto`, así que lo que siembra un bloque sigue vivo en el siguiente: un `FALLA`
  puede ser del escenario y no del modelo. Tres cosas suyas, medidas el 16/09/2026 corriéndolo
  tres veces seguidas, que costaron tres corridas pagadas y conviene no volver a pagar:
  - **Hay UN solo `CalendarioDoble` para toda la corrida (`CALENDARIO`), y tiene que seguir
    siéndolo.** `eventos` es un dict por instancia; con uno por contexto, una cita sembrada
    con un contexto y mirada desde el siguiente aparece como borrada en Google y
    `_sincronizar_con_calendar` la cancela en Neon — correctamente (no negociable 20). El
    bloque 13 certificaba así sobre una cita que se borraba sola.
  - **Se siembra con `hora_habil()`, que cuenta bloques hábiles**, igual que
    `probar_tools.py::hora`. Con `ahora + N días` la siembra cae en domingo según el día en
    que se corra, `_crear_cita` devuelve «fuera de horario» y el escenario conversa contra una
    base vacía sin decirlo.
  - **`nuevo_contexto` pasa la URL REAL de la política, no el default `PENDIENTE`.** Ese
    default mete en el prompt un bloque que producción ya no manda; certificar conducta sobre
    un prompt que no existe es la única forma en que un entregable miente sin fallar.
  - **El bloque 10 falla de forma intermitente y NO es del producto: 1 de 3 corridas.** Exige
    `consultar_citas` en el PRIMER turno, y cuando falla Daniela pide el nombre completo —que
    no es «pedirle un código al paciente», que es lo que esa línea dice vigilar—. La capacidad
    está viva y se ve en el bloque 13, que llega a la tool desde una frase mucho peor escrita.
    Imprime el texto completo al fallar: léelo antes de investigar.
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
- `probar_plantilla.py` **no toca la base en ningún momento** y es el paso que va entre «Meta
  aprobó la plantilla» y `desplegar.sh`. `--estado` no gasta; con un teléfono manda UN mensaje
  de plantilla de verdad. **Los huecos salen de `seguimientos.parametros_de`
  (renombrada de `_parametros_del_recordatorio` en la tarea 4; ya no es privada), no de
  literales escritos a mano**, y esa es la única razón por la que la prueba vale: con
  literales comprobaría que Meta acepta *una* plantilla, no la que manda el despachador —un
  orden de huecos cambiado o la conversión de zona rota se verían en el WhatsApp que llega al
  teléfono—. Dobla la FILA que el despachador lee de la base (`cita_inicio`, `nombre_completo`,
  `tratamiento` para el recordatorio; `tipo`, `nombre_ficha`, `nombre_perfil` para las
  reactivaciones): si esa consulta cambia de nombres de columna, aquí no se entera nadie. Su
  trampa: **el WABA no se puede derivar del token**, así que `--estado` no llega a leer la
  plantilla en Meta y lo dice en vez de callarse —el token es de usuario de sistema con
  acceso total, y por eso sus `granular_scopes` vienen SIN `target_ids`—. El envío real es la
  comprobación definitiva, y un rechazo de Meta no se cobra.
  **Cubre LAS CUATRO desde el 20/09/2026, no solo la de recordatorio** (`--tipo
  recordatorio|sin_agendar|cancelada|no_asistio`, default `recordatorio`; `--estado` las
  recorre todas y una vacía no es un fallo sino el modo de comprobación). Antes estaba
  cableado a `config.plantilla_recordatorio` y las tres de reactivación no tenían forma de
  probarse: habrían llegado al día del encendido sin que nadie hubiera visto una en un
  teléfono. **La fila de ejemplo de una reactivación NO lleva `nombre_completo`** —toda fila
  de reactivación tiene `cita_id` NULL, así que el `LEFT JOIN` a `citas` no produce ese
  campo; ponerlo aquí haría pasar la prueba por un eslabón que en producción está vacío el
  100% de las veces, que es exactamente lo que escondió el «Hola paciente»—. Y **`--plantilla
  NOMBRE` manda sobre el `.env` solo en esa corrida**: existe porque escribir el nombre en el
  `.env` de un servidor con este código ES el encendido, y probar no puede exigir encender.
- `probar_sin_resolver.py` escribe en `pruebas_sin_resolver`, propio y no compartido con
  `probar_tools.py` ni con `probar_recordatorios.py` — a propósito: los tres montan con
  `DROP SCHEMA ... CASCADE`, y correr dos de ellos a la vez sobre el mismo nombre hace que
  uno le borre el esquema al otro a mitad de corrida. Medido: 22 `FALLA` de
  `UndefinedTable: relation "reservas" does not exist` en `-m neon` por correrlo junto a
  `probar_recordatorios.py`, ninguna una regresión real — desaparecieron corriéndolos uno a
  la vez. Dobla a mano las firmas de `sin_resolver.casos_del_turno` y
  `persistencia.registrar_caso` en su helper `volcar()`; quien les cambie la firma rompe este
  script en silencio, igual que a los demás de esta lista. Su comprobación 8 es la única que
  no dobla nada: llama a `persistencia.borrar_rastro`, el camino REAL de `/clearstate`
  (`olvidar_ejemplos_de` sobrevive suelta solo para mantenimiento — su propio docstring dice
  que `/clearstate` no pasa por ahí), y de paso comprueba que la clave `casos_sin_resolver`
  aparece en lo que esa función devuelve y que tiene etiqueta legible en
  `reseteo.ETIQUETAS_DE_TABLA` sin guion bajo: sin esa etiqueta el paciente recibe «1 en
  casos_sin_resolver» por WhatsApp, el nombre interno de una tabla de la clínica que no es
  suya.

- `probar_reactivacion.py` escribe en `pruebas_reactivacion_script`, propio y no compartido
  con ninguno de los otros esquemas de esta lista -- mismo motivo que `probar_sin_resolver.py`
  (dos `DROP SCHEMA ... CASCADE` sobre el mismo nombre a la vez se borran el uno al otro a
  mitad de corrida). No gasta un token y **no manda nada**: las tres plantillas de
  reactivación de leads no están aprobadas por Meta, así que corre `seguimientos.despachar`
  con `plantillas={}` en todo el camino y su WhatsApp doblado (`WhatsAppQueRevienta`) revienta
  si alguien llega a llamar `enviar_plantilla` de verdad, en vez de devolver un éxito
  silencioso -- con `plantillas={}` esa llamada nunca debería ocurrir, y que `.llamadas` siga
  vacío al final es una de las comprobaciones. Siembra los dos casos del diseño (Marcela,
  que preguntó y no agendó; Andrés, que canceló y no volvió a agendar), corre `barrido.
  encolar` y `seguimientos.despachar` sobre ellos e imprime la decisión de cada fila, y
  después comprueba las once reglas anti-reporte de la spec una por una -- diez con
  `OK`/`FALLA` y la 11 (que el barrido se apague solo si la calidad del número baja) con
  `POR VERIFICAR`, porque el mecanismo se prueba en frío contra `barrido.se_puede_encolar`
  pero la consulta REAL a `graph.facebook.com` es de `scripts/probar_plantilla.py --estado` o
  de una comprobación manual, nunca de un script que no gasta ni toca la red. Dobla a mano
  las firmas de `barrido.encolar`, `seguimientos.despachar`, `seguimientos.decidir` y
  `persistencia.contar_comprometidos_hoy`; quien les cambie la firma rompe este script en
  silencio, igual que a los demás de esta lista. **Desde la revisión final dobla también
  `persistencia.registrar_negativa_de_reactivacion` y `marcar_seguimiento_enviado`**: la regla
  3 recorre ahora la SECUENCIA REAL -encolar, marcar ENVIADO, y solo entonces el «no» del
  paciente-, porque la versión anterior sembraba a mano una fila PENDIENTE justo antes de
  anular y ese estado en producción no existe (el botón «Ya no, gracias» solo aparece DESPUÉS
  de que el mensaje salió). Con el estado fabricado la comprobación pasaba en verde sobre un
  bloqueo que no se escribía nunca. **Es la única red que hoy existe para
  `contar_comprometidos_hoy`**: no hay ni una prueba offline de esa función (toda su
  protección vivía solo en `-m neon`), así que la regla 9 (arranque lento) siembra a mano los
  tres casos de la Ruling E9 -enviado hoy, pendiente de hoy, aplazado lejos- y los dos que NO
  deben contar -lejos y nunca aplazado, y un recordatorio de cita- y exige el número exacto
  antes de tocar `barrido.encolar`. Las reglas 10 y 11 comprueban con un DSN que no resuelve
  que `encolar` de verdad NO ABRE NINGUNA CONEXIÓN cuando el interruptor está apagado o la
  calidad frena -- si lo intentara, reventaría ahí mismo en vez de devolver ceros en silencio.
  Al final imprime, sin que se puedan pasar por alto, las tres cosas que este entregable no
  garantiza: que el ensayo en seco (sin plantilla) muestra más DECISIONES que mensajes reales
  porque anular libera cupo; que ninguna prueba garantiza que la gente no reporte el número,
  solo que las reglas comprobables no se puedan saltar; y que nada sale hasta que Meta
  apruebe las tres plantillas.

## `medir_historial.py`

Es el único camino para cerrar el límite MEDIDO del historial: mientras no haya **20 turnos
en 5 conversaciones con filas en `public.agent_messages`** el script lo dice y esos cuatro
números siguen en `PENDIENTE`. Exige desplegar primero: hasta que corra en producción no hay
nada que medir.

Ojo con no confundirlo con el otro número: `config.LIMITE_HISTORIAL_SESION = 230` es un
**tope de seguridad** derivado del techo de tokens de la cuenta —impide que un historial
crezca hasta reventar la petición y dejar al paciente atascado en el mensaje seguro—, no la
medición.

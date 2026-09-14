# MaxiCare · Daniela

Agente conversacional por WhatsApp para una clínica dental en Bogotá.
El diseño cerrado vive en `docs/agentes/plan-agentes.json` (11 decisiones, cada una
con su alternativa descartada). No rediseñes nada sin leer la decisión que aplica.

**El principio que decide los empates:** la seguridad clínica prevalece sobre
cualquier objetivo comercial. El éxito no es acumular citas: es que el paciente
llegue a la cita correcta.

# Comandos

- Pruebas: `uv run pytest -q`
- Las que tocan la base: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
  — escriben en el esquema `pruebas`, nunca en `public`, y lo borran al terminar.
- Base de datos (idempotente: migraciones + carga + verificación):
  `uv run python scripts/inicializar_base.py` · `--solo-verificar` no escribe.
- Ver el diseño sin leerlo entero: `uv run python scripts/ver_plan.py <clave>`
  (`fases`, `herramientas`, `guardrails`, `agentes`, `contexto`, `fallos`…).
- Cuánto historial gasta un turno: `uv run python scripts/medir_historial.py` (solo lee).
  Es el único camino para cerrar el límite MEDIDO del historial, y exige **20 turnos en 5
  conversaciones reales** antes de dar un número. No lo confundas con
  `config.LIMITE_HISTORIAL_SESION = 230`, que es un tope de seguridad y no una medición.
- Desplegar en el VPS: `bash scripts/desplegar.sh`
- Usuarios del panel: `uv run python scripts/crear_usuario.py` (`--listar`, `--quitar-acceso`)
- Revisar el grupo de Telegram: `uv run python scripts/obtener_chat_telegram.py`
- **Encender el relevo** (6C): `uv run python scripts/configurar_webhook_telegram.py --url
  https://daniela.maxicarecol.com/webhook/telegram`. Telegram NO valida la URL como hace
  Meta: hasta que alguien llame a `setWebhook`, el botón «Hablar yo con el paciente» no hace
  nada y no hay error en ningún log. `--estado` mira qué hay; `--quitar` lo apaga. **Poner
  webhook rompe el `getUpdates` de `obtener_chat_telegram.py`**: son excluyentes.
- Comprobar que el relevo puede funcionar: `uv run python scripts/probar_relevo.py` (no
  gasta, no toca la base; crea y borra un tema de usar y tirar).
- Resetear a primer contacto: escribir `/clearstate`. **Es irreversible** y borra el rastro
  entero, historial del agente incluido. **Por WhatsApp** solo funciona si el número está en
  `MAXICARE_TELEFONOS_PRUEBA`; con esa variable vacía —su default— el comando no existe para
  nadie. En el chat web del panel funciona sin lista y solo toca `pruebas_web`.
  El detalle, en `.claude/rules/atencion-whatsapp.md`.

Entregables por fase. **Los marcados gastan tokens**; los demás, ni uno:

| Comando | Qué prueba | ¿Gasta? |
|---|---|---|
| `scripts/probar_tools.py` | las diez tools contra Neon (fase 3) | no |
| `scripts/probar_agentes.py` | los dos agentes contra la API real (fase 4) | **sí** |
| `scripts/probar_web.py` | el cascarón web (fase 5) | solo con `--chat` |
| `scripts/probar_atencion.py` | el turno de WhatsApp de punta a punta (fase 6A) | solo con `--chat` |
| `scripts/probar_lectura.py` | el muro y el tema del paciente (fase 6B) | solo con `--chat` |
| `scripts/probar_relevo.py` | que el bot PUEDA relevar: permisos y webhook (fase 6C) | no |
| `scripts/probar_persistencia.py` | que una conversación sobrevive a reiniciar (fase 7) | solo con `--chat` |
| `scripts/probar_panel.py` | el panel de tratamientos (fase 8) | solo con `--chat` |
| `scripts/probar_calendario.py` | `CalendarioGoogle` contra el calendario real | no |
| `scripts/probar_webhook.py <url>` | el webhook en producción | **sí** (despierta a Daniela) |

**Estos scripts doblan funciones de `src/` con firmas escritas a mano, y `pytest -q` no los
corre.** Cambiarle la firma a algo que un script dobla los rompe en silencio, con la suite
entera en verde: quien toque una de esas firmas corre los cinco que no gastan. Lo demás de
cada script —en qué esquema escribe, qué dobla, qué trampa tiene— está en
`.claude/rules/scripts-entregables.md`.

# No negociables

Cada una es una línea porque tiene que sobrevivir a una compactación. El porqué de cada
una —qué se midió, qué costó— está en la regla que cubre ese archivo.

1. **Si el calendario no arranca, Daniela queda con `CalendarioCaido`, NUNCA con
   `CalendarioDoble`.** El doble dice que sí a todo y le confirma al paciente una cita que
   no existe: llega a una clínica donde nadie lo espera.
2. **Ninguna clave de idempotencia la escribe el modelo.** Las cuatro las arma el código con
   `ctx.clave(...)`. Con la del modelo, dos pacientes salían confirmados sobre un solo cupo.
3. **El candado de `atencion.py` va por TELÉFONO y `_leer_estado` va DENTRO.** Sacar la
   lectura fuera devuelve dos carreras medidas: conversaciones duplicadas y escalamientos
   que el doctor no ve.
4. **Meta reintenta los webhooks y hay DOS deduplicaciones, no una**: `ON CONFLICT (wamid)`
   protege el reenvío al doctor, `Resultado.nuevo` protege el turno de Daniela.
5. **`asegurar_conversacion` SIEMPRE inserta**, pese al nombre. En WhatsApp se usa
   `conversacion_viva`.
6. **El búfer de mensajes: la ventana va antes del candado, el retardo se descuenta (no se
   suma) y el búfer se saca en un `finally`.** Mover cualquiera de las tres rompe algo en
   silencio.
7. **Una cita de Daniela NO es un bloqueo del doctor.** La marca
   `extendedProperties.private.origen = "daniela"` es lo que impide que la clínica atienda
   a uno por hora en vez de a dos.
8. **`uv run pytest -q` a secas NO caza una regresión en `tocar_conversacion`.** Quien toque
   esa función corre además las de Neon y `scripts/probar_atencion.py`.
9. **El historial del diálogo YA está en Postgres** (`agent_sessions` / `agent_messages`,
   `session_id = id_conversacion`). Quien toque `/clearstate` tiene que borrarlo, y va
   ANTES del `DELETE FROM conversaciones`: los `session_id` SON esos ids.
10. **Las columnas de la 010 las fija el SDK, no nosotros.** `SQLAlchemySession` corre con
   `create_tables=False`, así que `agent_sessions` y `agent_messages` tienen que coincidir
   con lo que el SDK espera —incluido el `TIMESTAMP` **sin zona**, al revés que el resto del
   esquema—. Quien suba la versión del SDK compara columna por columna; lo que caza el
   desajuste es `tests/test_sesion_neon.py`, y solo corre con `-m neon`.
11. **La regeneración corre SIN los guardrails de ENTRADA, y un tripwire de entrada no se
   regenera nunca.** `CORRECCION` empieza con «AVISO DEL SISTEMA» y le reescribe la conducta
   a Daniela: pasada por `uso_indebido`, el evaluador la clasificaba como inyección
   **siempre**, así que cualquier guardrail de salida que saltara acababa en mensaje seguro
   más escalamiento y el paciente se iba sin su cita. Los de SALIDA se conservan los tres. Y
   el `{motivo}` que viaja en la corrección es el TEXTO del guardrail, jamás su nombre: con
   el nombre, el segundo intento es tan ciego como el primero.
12. **Un teléfono SIN ficha en `pacientes` puede crear su primera cita; mover o cancelar,
   nunca.** `identidad_antes_de_datos` protege los datos de alguien que ya existe —«que no se
   mezclen cuando alguien escribe por un familiar»—, y quien no tiene ficha no tiene datos
   que proteger: bloquearlo dejaba a la clínica sin pacientes nuevos, con `citas.paciente_id`
   NULLABLE desde la 001 justo para ese caso. El permiso lo da `ctx.telefono_sin_paciente`,
   que sale de la base y **nunca del modelo**, y la excepción es una lista blanca de UNA tool.
   Lo que impide que eso lo deje encerrado: **`crear_cita` registra al paciente**, así que
   desde el turno siguiente sí puede mover y cancelar lo suyo.
13. **La pertenencia de una cita va por TELÉFONO, y toda hora que una tool confirma queda
   autorizada — incluida la vieja al reprogramar y la cancelada al cancelar.** El id de una
   cita es un UUID que el sistema le mandó al paciente: no es un control de acceso. Y si la
   hora no queda autorizada, `sin_hora_no_verificada` bloquea la confirmación de una escritura
   **que ya ocurrió**: la cita movida y el paciente yendo a la hora vieja.
14. **Al General solo va lo que le pide algo al doctor, y `telegram_message_id` NULL ya no es
   una alarma.** Un texto va mudo al tema de su paciente, o a ninguna parte si no tiene tema
   —nunca lo crea: eso es del primer archivo—. El aviso de archivos suena UNA vez por tanda
   (`_primer_archivo_de_la_tanda`, ventana de 24 h). Lo que invierte: la migración 004 dice
   que `telegram_message_id` NULL + `fallo` NULL es «entró y no llegó a nadie», y eso pasó a
   ser lo normal; **la señal de alarma es `reenviado_en` NULL**, que es sobre lo que ya está
   construido `ix_mensajes_sin_reenviar`. El SQL de las dos consultas nuevas solo lo ejercita
   `tests/test_ingesta_neon.py`, con `-m neon`: offline van dobladas y degradan en silencio.
15. **El relevo tiene UNA puerta de salida, y `/webhook/telegram` se cierra cuando falta el
   secreto.** `conversaciones.tomada_por` puesto significa dos cosas a la vez: a Daniela **no
   se la llama** (`atencion.atender` corta antes del modelo) y el tema de ese paciente está
   **abierto** en Telegram, o sea que es un canal en vivo hacia su WhatsApp. Las dos tienen
   que dejar de ser verdad juntas, y por eso los tres motivos del CHECK de la 003
   —`devuelto_por_doctor`, `tiempo_agotado`, `tema_perdido`— salen todos por `relevo.cerrar`.
   El reloj de cierre cuenta desde `GREATEST(tomada_en, ultimo_mensaje_doctor_en)`: desde la
   activación sería un cronómetro que corta a un doctor a mitad de frase. `/webhook/telegram`
   **sin `MAXICARE_TELEGRAM_WEBHOOK_SECRET` responde 403 a todo** —degrada al revés que el
   resto del proyecto, porque es la única puerta por la que algo de fuera puede hacer que el
   bot le escriba al WhatsApp de un paciente—. Y la segunda inversión de una columna, después
   de la 14: un mensaje que entra durante un relevo se anota con `fallo_respuesta` empezando
   por `relevo:` **sin ser un fallo**; sin eso, `mensajes_sin_responder` se lo entregaría a
   Daniela media hora después y contestaría por encima del doctor.
16. **El hilo de Telegram va por TELÉFONO (`temas_telegram`), no por ficha, y tener hilo NO
   es estar verificado.** La 014 lo sacó de `pacientes` y dejó caer las dos columnas viejas.
   Eso es lo que permite que un lead tenga hilo desde su primer archivo —antes sus
   radiografías caían al General, sus textos no se archivaban en ninguna parte y el hilo que
   le abría el relevo nacía vacío— **sin** tocar el guardrail de identidad, que sigue
   derivando de la EXISTENCIA de la fila en `pacientes`. `lectura.asegurar_tema` ya no exige
   ficha y sigue sin crear ninguna.
17. **Lo único que cruza del relevo hacia Daniela es que hubo relevo y, si la hubo, una
   CITA.** Nunca lo que escribió el doctor, ni literal ni resumido: eso puede ser clínico, y
   `relevo._avisar_a_daniela` escribe en el contexto del agente que le habla al paciente. Por
   eso el cierre PREGUNTA («¿quedó agendada una cita?») en vez de resumir — decide un humano.
   La cita se crea de verdad (cupo → Calendar → fila, ese orden), y `tomar_cupo` puede decir
   que no: es lo único que impide que la clínica le dé esa hora a otro. El estado de esa
   pregunta vive en `conversaciones.cierre_pendiente`, **en la base y no en memoria**: un
   reinicio a mitad le mandaría al PACIENTE el «15/09 14:30» que el doctor estaba escribiendo.
   Y el MIME de un archivo que baja de Telegram sale de la EXTENSIÓN: su servidor responde
   `application/octet-stream` siempre, y Meta rechaza la subida entera con eso.
18. **El relevo tiene CUATRO salidas, y la cuarta es cerrar el hilo a mano.** Es el gesto que
   sale natural al terminar, y dejaba el estado que prohíbe la 15: tema cerrado con
   `tomada_por` puesto. Lo atiende `relevo.cerrar_por_tema_cerrado` desde el evento
   `forum_topic_closed`, con motivo `devuelto_por_doctor` —el CHECK de la 003 sigue cerrado
   con tres— y **sin** preguntar por la cita: preguntar deja el relevo tomado, y con el tema
   cerrado eso es el estado prohibido otra vez. Es idempotente porque `cerrar` también
   dispara ese evento al cerrar el tema. El botón «Listo» va **anclado** (`can_pin_messages`)
   porque viaja en el primer mensaje del hilo, y la despedida lleva el de volver a entrar.
   Y lo que el doctor lee como transcripción es la FRASE: `daniela` tiene `output_type`, así
   que el contenido del item es el JSON entero de `RespuestaDaniela` y hay que desempaquetar
   DOS niveles (`persistencia._solo_la_frase`).
19. **Borrar un tema NO emite ningún evento, y el `tratamiento` del relevo lo escribe el
   doctor.** Lo primero hacía el peor de los agujeros —relevo tomado, Daniela callada y el
   paciente sin nadie que le conteste durante 3 h—, y lo detecta el barrido con
   `telegram.estado_del_tema`, que pregunta con **`reopenForumTopic`**: `editForumTopic` sin
   argumentos no cambia nada y **por eso mismo no valida el id** —devolvía `ok: true` para un
   tema borrado, así que la sonda decía que sí a todo y el agujero siguió abierto un día
   entero, con la suite en verde—. Ninguna prueba offline puede cazar eso, porque todas
   doblan a Telegram: lo caza `scripts/probar_relevo.py` contra la API de verdad, y quien
   toque la sonda lo corre. Devuelve TRES estados y no un booleano: `"reabierto"` significa
   que el tema existía pero estaba cerrado —un `forum_topic_closed` perdido con el bot
   caído— y se cierra como `devuelto_por_doctor`; su `None` es «no se pudo saber» y **no**
   cierra nada, porque un timeout no es un tema borrado. Al cerrar por
   `tema_perdido` el hilo se OLVIDA (`persistencia.olvidar_tema`), no se marca cerrado: una
   fila apuntando a un `topic_id` muerto deja a ese número sin poder recibir archivos nunca
   más. Y lo segundo: la 015 sustituyó el `tratamiento="valoracion"` fijo —que ni siquiera
   era una de las catorce claves— por lo que el doctor escriba, **sin validar contra la lista
   viva**; es la única excepción del proyecto, decidida por el cliente sabiendo que Daniela
   lee ese campo y se lo repite al paciente. La 016 añadió delante el paso del NOMBRE, y solo
   se dispara cuando la ficha dice `PENDIENTE`: sin él la cita entraba en la agenda de la
   clínica como «PENDIENTE · Cordales», que es el caso normal de una cita salida de un relevo
   —el paciente nuevo es el que más escala—. Nunca pisa un nombre de verdad.

# Dónde está el resto

El detalle de cada área se carga solo cuando tocas sus archivos:

| Archivo | Cubre |
|---|---|
| `web/CLAUDE.md` | la interfaz del panel, y la trampa `public` vs `pruebas_web` |
| `.claude/rules/atencion-whatsapp.md` | el turno de WhatsApp: candado, búfer, idempotencia |
| `.claude/rules/relevo-telegram.md` | el relevo: el botón, el webhook y las tres salidas |
| `.claude/rules/calendario.md` | Google Calendar y la cuenta de servicio |
| `.claude/rules/despliegue.md` | `desplegar.sh`, el Dockerfile y el `.env` del VPS |
| `.claude/rules/scripts-entregables.md` | qué dobla cada script de `scripts/` y su trampa |
| `.claude/rules/pruebas.md` · `frontera-agentes.md` · `migraciones.md` · `base-conocimiento.md` · `contratos-diseno.md` | lo que ya había |

# Trampas de este entorno

- **Los heredoc de Bash fallan** aquí (`unexpected EOF looking for matching`).
  Para escribir un archivo usa la herramienta Write, no `cat > archivo <<'EOF'`.
- **La consola de Windows es cp1252** y revienta con `→`, `✅`, acentos. Todo script
  bajo `scripts/` empieza con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
  y usa marcadores ASCII (`OK` / `FALLA` / `->`). El mismo script corre en el VPS.
- `uv run` avisa de que `VIRTUAL_ENV` no coincide. Es ruido, se ignora.
- Es un repositorio git desde el commit `7e12d6b`, que congela las fases 1 a 5. Las
  búsquedas respetan `.gitignore`: `.venv/`, `web/node_modules/`, `web/dist/`, `.env` y
  `.superpowers/` no aparecen. El remoto es `origin` y **se empuja a `main`**, pero solo
  cuando el usuario lo pide: no empujes por tu cuenta. (Esta nota decía que `main` iba
  «decenas de commits» por delante de `origin/main`; el 14/09/2026 la diferencia era de UNO
  y se empujó. Si vuelves a citarla, compruébala con `git rev-list --left-right --count`.)
- **El pooler de Neon rechaza `options` como parámetro de arranque** (`unsupported startup
  parameter in options: search_path`). Para fijar un `search_path` —o para una prueba de
  concurrencia de verdad— hay que usar la conexión directa: quitarle el `-pooler.` al host.
- **Una variable de entorno vacía no es una variable ausente.** Si TODA llamada al modelo
  muere en `APIConnectionError: Connection error.` mientras `/salud` dice `configuracion:
  ok`, no es la red: es una clave vacía que el `env_file` de Docker sí exporta. Ya pasó en
  producción. El caso entero, en `.claude/rules/despliegue.md`.
- **La herramienta `Write` interpreta las secuencias `\uXXXX` del contenido como caracteres
  de verdad.** A un implementador le dejó un byte NUL dentro de un `.tsx` y caracteres
  combinantes invisibles dentro de un regex. Se esquiva escribiendo esos archivos con
  here-strings de PowerShell, y conviene comprobar el resultado (contar bytes NUL y
  caracteres de categoría `Cc`/`Cf`/`Mn`) cuando el contenido lleve `\u`.
- **Pero PowerShell no vale para EDITAR un archivo que ya existe.** El here-string de arriba
  sirve para crear uno nuevo; el ciclo leer-modificar-escribir con `Get-Content -Raw` /
  `Set-Content` **destroza el encoding de este repo** y deja mojibake en todos los acentos
  (`configuración` → `configuraciÃ³n`). Pasó en la fase 7 y hubo que restaurar con
  `git checkout`. Para modificar un archivo del proyecto, la herramienta `Edit`.
- **Tres documentos de diseño son enormes y están versionados.** `plan-agentes.json`
  (~22.000 tokens) está bloqueado por `.claude/settings.json`: léelo con `ver_plan.py`.
  `plan-agentes.md` (~12.000) y `brief-agentes.json` (~7.000) no están bloqueados porque no
  tienen alternativa — léelos por rangos, nunca enteros.

# Reglas duras

1. **`from agents import tool` está prohibido.** `agents.tool` es un módulo, no el
   decorador. Usa `from agents.decorators import tool` o `from agents import function_tool`.
2. **Nunca se escribe código del SDK sin comprobar la versión instalada.**
   Verificada 0.22.1, instalada 0.22.2.
3. **Lo que no se sabe se marca con el literal `"PENDIENTE"`.** Nunca con un valor
   plausible: una suposición razonable no se distingue de un hecho verificado.
4. **No se registran cédulas ni documentos de identidad de ningún tipo.**
   Prohibición expresa del cliente. La tabla `pacientes` no tiene esa columna, y esa
   ausencia ES la política: no le agregues una.

# Datos y secretos

- `.env` tiene credenciales reales (Neon, OpenAI, Telegram, WhatsApp). Nunca lo
  imprimas ni lo pegues en una respuesta. `.env.ejemplo` sí se versiona.
- La base de Neon es exclusiva de este proyecto.
- Al mostrar una cadena de conexión, enmascárala: `***@host`.

# Contexto

- Delega la exploración a subagentes: que vuelva el resumen, no los archivos.
- Lee rangos concretos, no archivos enteros, cuando sepas dónde está lo que buscas.
- Al cambiar a una tarea sin relación con la anterior, `/clear`.
- Tras dos correcciones fallidas sobre lo mismo, `/clear` y reformula.
- Para cambios que tocan varios archivos, plan mode antes de editar: el plan se escribe a
  archivo y se re-inyecta tras cada compactación.
- `/compact céntrate en X` conserva lo que tú eliges, no lo que el resumen adivine.

# Compact instructions

Al compactar, conserva: las decisiones de diseño ya aprobadas, la fase en curso y su
entregable verificable, y lo que quedó PENDIENTE de MaxiCare.

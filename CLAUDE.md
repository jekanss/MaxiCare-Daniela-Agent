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
- Desplegar en el VPS: `bash scripts/desplegar.sh`
- Usuarios del panel: `uv run python scripts/crear_usuario.py` (`--listar`, `--quitar-acceso`)
- Revisar el grupo de Telegram: `uv run python scripts/obtener_chat_telegram.py`
- Resetear a primer contacto: escribir `/clearstate`. **Por WhatsApp** solo funciona si el
  número está en `MAXICARE_TELEFONOS_PRUEBA` (varios, separados por coma); con esa variable
  vacía —su default— el comando no existe para nadie. Borra paciente, conversaciones,
  mensajes, citas (y sus eventos de Calendar), **el historial del agente** (`agent_sessions`
  y, por cascada, `agent_messages`: desde la fase 7 el diálogo vive en la base, no en la
  memoria del proceso) y el tema de Telegram, en `public` y en `pruebas_web`. **En el chat web del panel** funciona sin lista: ahí el teléfono es
  `web-<usuario>` y solo se toca `pruebas_web`. Es irreversible.
  Ver `.claude/rules/atencion-whatsapp.md`.

Entregables por fase. **Los marcados gastan tokens**; los demás, ni uno:

| Comando | Qué prueba | ¿Gasta? |
|---|---|---|
| `scripts/probar_tools.py` | las nueve tools contra Neon (fase 3) | no |
| `scripts/probar_agentes.py` | los dos agentes contra la API real (fase 4) | **sí** |
| `scripts/probar_web.py` | el cascarón web (fase 5) | solo con `--chat` |
| `scripts/probar_atencion.py` | el turno de WhatsApp de punta a punta (fase 6A) | solo con `--chat` |
| `scripts/probar_lectura.py` | el muro y el tema del paciente (fase 6B) | solo con `--chat` |
| `scripts/probar_persistencia.py` | que una conversación sobrevive a reiniciar (fase 7) | solo con `--chat` |
| `scripts/probar_panel.py` | el panel de tratamientos (fase 8) | solo con `--chat` |
| `scripts/probar_calendario.py` | `CalendarioGoogle` contra el calendario real | no |
| `scripts/probar_webhook.py <url>` | el webhook en producción | **sí** (despierta a Daniela) |

- `probar_atencion.py` escribe en `pruebas_atencion` —lo crea y lo borra comprobando el
  borrado— y su WhatsApp es falso: no le llega nada a ningún paciente.
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
- `probar_panel.py` MITAD A cambia un precio por HTTP contra `public`, la base real de la
  clínica, y **lo restaura en un `finally` comprobando la restauración con una aserción**.
- `probar_calendario.py --diagnosticar` **solo lee**: es lo primero que hay que correr
  cuando Calendar «no funciona».

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

# Dónde está el resto

El detalle de cada área se carga solo cuando tocas sus archivos:

| Archivo | Cubre |
|---|---|
| `web/CLAUDE.md` | la interfaz del panel, y la trampa `public` vs `pruebas_web` |
| `.claude/rules/atencion-whatsapp.md` | el turno de WhatsApp: candado, búfer, idempotencia |
| `.claude/rules/calendario.md` | Google Calendar y la cuenta de servicio |
| `.claude/rules/despliegue.md` | `desplegar.sh`, el Dockerfile y el `.env` del VPS |
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
  `.superpowers/` no aparecen. No hay remoto todavía.
- **El pooler de Neon rechaza `options` como parámetro de arranque** (`unsupported startup
  parameter in options: search_path`). Para fijar un `search_path` —o para una prueba de
  concurrencia de verdad— hay que usar la conexión directa: quitarle el `-pooler.` al host.
- **Una variable de entorno vacía no es una variable ausente, y hay DOS puertas.** El
  `.env` trae casi todas las claves presentes y sin valor. `cargar_dotenv` no exporta las
  vacías y `_opcional` cae al default; eso cubre la puerta local. La otra es el `env_file`
  de Docker, que **sí** exporta las vacías y ante el cual ese filtro no llega a correr,
  porque dentro del contenedor no hay `.env` que leer. Ya pasó en producción: con
  `OPENAI_BASE_URL=` vacía, el cliente de OpenAI la prefiere sobre su propio default, arma
  `base_url=""` y **toda** llamada al modelo muere en `APIConnectionError: Connection
  error.` Se lee como un problema de red y no lo es: las trazas subían a esa misma API sin
  problema, `/salud` decía `configuracion: ok`, y el paciente recibía el mensaje seguro de
  `atencion.py` como si Daniela hubiera decidido no saber. Lo cierran `runtime.py` con
  `descartar_vacias_de_terceros()` al arrancar y `desplegar.sh`, que no manda ninguna clave
  vacía al servidor.
- **La herramienta `Write` interpreta las secuencias `\uXXXX` del contenido como caracteres
  de verdad.** A un implementador le dejó un byte NUL dentro de un `.tsx` y caracteres
  combinantes invisibles dentro de un regex. Se esquiva escribiendo esos archivos con
  here-strings de PowerShell, y conviene comprobar el resultado (contar bytes NUL y
  caracteres de categoría `Cc`/`Cf`/`Mn`) cuando el contenido lleve `\u`.
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

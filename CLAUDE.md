# MaxiCare · Daniela

Agente conversacional por WhatsApp para una clínica dental en Bogotá.
El diseño cerrado vive en `docs/agentes/plan-agentes.json` (11 decisiones, cada una
con su alternativa descartada). No rediseñes nada sin leer la decisión que aplica.

**El principio que decide los empates:** la seguridad clínica prevalece sobre
cualquier objetivo comercial. El éxito no es acumular citas: es que el paciente
llegue a la cita correcta.

# Comandos

- Pruebas: `uv run pytest -q`
- Base de datos (idempotente, aplica migraciones + carga + verifica):
  `uv run python scripts/inicializar_base.py`
- Solo verificar, sin escribir: `uv run python scripts/inicializar_base.py --solo-verificar`
- Revisar el grupo de Telegram: `uv run python scripts/obtener_chat_telegram.py`
- Las nueve tools contra Neon y un calendario de pruebas (entregable fase 3):
  `uv run python scripts/probar_tools.py`
- Las pruebas que tocan la base: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
  Escriben en el esquema `pruebas`, nunca en `public`, y lo borran al terminar.
- Los dos agentes contra la API real (entregable fase 4, **gasta tokens**):
  `uv run python scripts/probar_agentes.py`
- El webhook en producción: `uv run python scripts/probar_webhook.py https://daniela.maxicarecol.com`
- El turno de WhatsApp de punta a punta (entregable fase 6A):
  `uv run python scripts/probar_atencion.py`
  Escribe en el esquema `pruebas_atencion` —lo crea y lo borra, comprobando el borrado—, y el
  WhatsApp es falso: no le llega nada a ningún paciente. Sin `--chat` no gasta un token; con
  `--chat` los turnos corren contra el modelo de verdad y **gasta tokens**.
- El cascarón web (entregable fase 5): `uv run python scripts/probar_web.py`
  Con `--chat` habla de verdad con Daniela y **gasta tokens**.
- El panel de tratamientos (entregable fase 8, primera mitad):
  `uv run python scripts/probar_panel.py`
  Dos mitades: la MITAD A cambia un precio por HTTP contra `public` —la base real de la
  clínica— y sin gastar un token, y **restaura ese precio en un `finally`, comprobando la
  restauración con una aserción** (nunca se la da por hecha). Con `--chat`, la MITAD B
  además escribe un precio y crea un tratamiento en `pruebas_web` y comprueba que Daniela lo
  cotiza de verdad; **gasta tokens**.
- Usuarios del panel: `uv run python scripts/crear_usuario.py` (`--listar`, `--quitar-acceso`)
- `CalendarioGoogle` contra el calendario real (no gasta tokens):
  `uv run python scripts/probar_calendario.py`
  Crea un evento en **2029 a las 3 a.m.**, lo mueve, comprueba que **no vuelve como
  bloqueo**, y lo borra en un `finally` **verificando** que desapareció. Con
  `--diagnosticar` solo lee: comprueba el acceso y lista los bloqueos de los próximos
  14 días. Es lo primero que hay que correr cuando Calendar «no funciona».

# Interfaz web

El frontend es React + Vite + Tailwind y vive en `web/`, aparte del paquete de Python. Sale
de un archivo de Figma Make; `web/src/marca/` y los tokens `--color-sp-*` de `index.css`
vienen de allí y no se renombran, o la siguiente pantalla que llegue de Figma deja de encajar.

```
cd web && npm install && npm run build     # deja web/dist, que es lo que sirve runtime.py
cd web && npm run dev                      # :5173 con proxy a :8080 — hacen falta LOS DOS
uv run uvicorn maxicare_daniela.runtime:app --port 8080
```

- **El chat de pruebas escribe en el esquema `pruebas_web`, nunca en `public`.** No es
  `pruebas`: ese lo BORRAN `probar_tools.py` y `probar_agentes.py` al terminar.
- Sin `MAXICARE_SECRETO_SESION` el panel se apaga con un 503 y **el webhook sigue vivo**.
  Es deliberado: WhatsApp está en producción y no puede caerse por una variable del panel.
- La ruta comodín que sirve `index.html` va **al final** de `runtime.py`. Antes se tragaría
  `/api`, `/salud` y el webhook.
- **El `Literal` de tratamientos está partido en dos.** `LecturaArchivo` conserva los 14
  escritos a mano —es el muro, y tiene su prueba `test_tratamiento_no_admite_una_frase_clinica`—;
  el vocabulario de negocio vive en la tabla `tratamientos` y lo carga `runtime.py` al
  arrancar. Crear un tratamiento desde la pantalla **no** lo mete en el muro: una
  radiografía suya se clasifica `no_identificado`. Verificado por `probar_panel.py`.
- **LA TRAMPA QUE MÁS VA A COSTAR: el panel escribe en `public`, el chat de pruebas lee de
  `pruebas_web`.** Son dos bases distintas. Editar un precio en la pantalla y preguntarle a
  Daniela en la pestaña Pruebas **no** sirve para comprobar el cambio: ella cita el valor de
  la semilla y parece un fallo. Ya hizo que el entregable de esta fase se escribiera mal la
  primera vez. Para comprobar el camino completo: `probar_panel.py --chat`, que prueba cada
  mitad por su lado.
- **Dos personas editando la misma ficha: la segunda pisa a la primera.** No hay bloqueo
  optimista, es deliberado. Lo que lo hace aceptable no es que sea improbable, sino que
  `cambios_configuracion` guarda el valor anterior: una edición pisada es recuperable, no
  perdida. Eso vale para el contenido **y para `aprobado`**, que se registra en su propia
  fila. Si algún día se añade un campo editable a la ficha, tiene que anotarse también, o
  esta frase vuelve a ser mentira para ese campo y el límite deja de ser aceptable.
- **En `web/src/pantallas/Tratamientos.tsx`, el `useCallback` de `recargar` tiene
  dependencias vacías a propósito, y `alCaducarSesion` se consume por una `ref`.** Meter esa
  prop en las dependencias —lo que pediría cualquier regla de hooks— deja la pantalla
  releyendo Neon en bucle, porque `App.tsx` la pasa como una flecha nueva en cada render. No
  hay `eslint-plugin-react-hooks` ni arnés de pruebas de frontend que lo atrape: se vería
  como una pantalla lenta y una factura rara.

# Google Calendar

- **Una cita de Daniela NO es un bloqueo del doctor.** Es la trampa central de
  `CalendarioGoogle` y con `CalendarioDoble` era invisible: el doble guarda eventos y
  bloqueos en listas separadas, Google los devuelve juntos. Sin filtrarlos, la primera cita
  de una hora taparía el bloque y la clínica atendería **uno** por hora en vez de dos. Cada
  evento que crea Daniela lleva `extendedProperties.private.origen = "daniela"` y
  `bloqueos()` lo descarta. Un evento sin marca es de los doctores y sí tapa.
- **La credencial es `MAXICARE_GOOGLE_SA_B64`, no una ruta a un archivo.** `config.py`
  decía `MAXICARE_GOOGLE_CREDENTIALS_PATH`, que no existía en ningún `.env`; nadie lo notó
  porque ningún módulo leía ese campo. El nombre correcto es el que documenta `.env.ejemplo`.
- **El 404 casi nunca es el código: es el permiso.** La cuenta de servicio se autentica
  perfectamente aunque no tenga acceso a nada. Hay que compartir el calendario con su
  `client_email` dándole «Hacer cambios en los eventos». `CalendarioGoogle` lo comprueba
  **al construirse**, con una lectura real, y el mensaje nombra el correo.
- **El calendario de la clínica es una cuenta personal de Gmail, y es una decisión tomada
  a conciencia** (MaxiCare, 12/09/2026: «sí va a ser ese correo, no pasa nada»). Lo que
  cuesta: las citas de los pacientes conviven con la agenda personal de esa persona —ya
  hubo un evento suyo borrado a mano durante una prueba— y el día que esa cuenta no esté,
  el calendario se va con ella. La salida, si algún día deja de ser aceptable, es barata:
  crear un calendario secundario, compartirlo con la misma cuenta de servicio y cambiar
  `MAXICARE_GOOGLE_CALENDAR_ID`. Ni una línea de código cambia.
- **`calendario_desde_config` todavía no la llama nadie.** El chat web sigue con
  `CalendarioDoble` a propósito: probar en la pestaña Pruebas crearía eventos falsos en el
  calendario donde los doctores miran su día —el mismo error de categoría que `public` vs
  `pruebas_web`—. El cableado real es de la fase 6, en el camino de WhatsApp.
- `ZONA_BOGOTA` vive en `calendario.py` y `herramientas.py` la reexporta. Una sola
  definición: dos copias de un desfase horario son dos cosas que un día divergen.

# Daniela en WhatsApp

- **`asegurar_conversacion` SIEMPRE inserta una fila nueva**, pese al nombre: no es un
  get-or-create. En el camino de WhatsApp se usa `conversacion_viva`, que reutiliza la de las
  últimas 24 h. Usar la primera aquí abriría una conversación por mensaje: Daniela no
  recordaría ni la frase anterior, `turno_actual` sería siempre 1 y las claves de
  idempotencia (`id_conversacion + turno`) no colisionarían nunca, con lo que dejarían de
  proteger.
- **El candado de `atencion.py` va por TELÉFONO, no por conversación, y la lectura de la
  base va DENTRO.** No es un detalle: el teléfono se conoce desde el mensaje y la
  conversación no, así que un candado por conversación obliga a leer la base antes de
  cerrarlo. Con esa lectura fuera se medió lo siguiente: dos mensajes simultáneos de un
  número nuevo abren **dos conversaciones** —Daniela contesta dos veces sin saber de la otra
  mitad, y del tercer mensaje en adelante una de las dos se pierde—, y dos de una
  conversación existente leen el **mismo `turno_actual`**, así que arman la misma clave de
  escalamiento y el doctor se entera de uno solo. Si alguien mueve `_leer_estado` fuera del
  candado «para que el candado dure menos», vuelven los dos.
- **El candado es de proceso.** Con más de un worker o más de una réplica deja de proteger y
  haría falta un `pg_advisory_lock`; la clave natural ya es el teléfono, que se conoce sin
  tocar la base. El contenedor corre con un solo worker, y por eso hoy alcanza.
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
- **El historial vive en memoria** (`_sesiones`). Un reinicio borra el hilo del diálogo, no
  los datos: paciente, citas y estado de oportunidad están en Neon. Lo arregla la fase 7 con
  `SQLAlchemySession`.
- **Si el calendario no arranca, Daniela queda con `CalendarioCaido`, nunca con
  `CalendarioDoble`**, y la diferencia es la razón de ser del proyecto. El doble dice que sí a
  todo: `crear_cita` tomaría el cupo, «crearía» el evento en un diccionario y le confirmaría
  la cita al paciente, que llegaría a una clínica donde nadie lo espera. El caído lanza
  `ErrorDeCalendario`, y las tools ya saben qué hacer con eso: liberar el cupo, no confirmar
  nada y escalar. `/salud` publica qué clase acabó ahí.
- **`MAXICARE_DANIELA_RESPONDE`**: por defecto activa (cualquier valor que no sea `0`). Con
  `0`, el webhook sigue registrando el mensaje y reenviando el archivo a Telegram exactamente
  como antes, y lo único que se apaga es la respuesta al paciente. Existe para poder callarla
  en diez segundos sin desplegar código.
- **`uv run pytest -q` a secas NO caza una regresión en el SQL de `tocar_conversacion`.** Está
  comprobado: mutando el `UPDATE` para que ignore `turno_actual`, la suite offline queda
  entera en verde y solo falla la de Neon. Quien toque esa función tiene que correr las dos
  —`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` y `scripts/probar_atencion.py`—.

# Trampas de este entorno

- **Los heredoc de Bash fallan** aquí (`unexpected EOF looking for matching`).
  Para escribir un archivo usa la herramienta Write, no `cat > archivo <<'EOF'`.
- **La consola de Windows es cp1252** y revienta con `→`, `✅`, acentos. Todo script
  bajo `scripts/` empieza con `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
  y usa marcadores ASCII (`OK` / `FALLA` / `->`). El mismo script corre en el VPS.
- `uv run` avisa de que `VIRTUAL_ENV` no coincide. Es ruido, se ignora.
- Es un repositorio git desde el commit `7e12d6b`, que congela las fases 1 a 5. Las
  búsquedas respetan `.gitignore`: `.venv/`, `web/node_modules/`, `web/dist/` y `.env`
  no aparecen. No hay remoto todavía.
- **El pooler de Neon rechaza `options` como parámetro de arranque** (`unsupported startup
  parameter in options: search_path`). Para fijar un `search_path` —o para una prueba de
  concurrencia de verdad— hay que usar la conexión directa: quitarle el `-pooler.` al host.
- **Una variable de entorno vacía no es una variable ausente.** El `.env` trae casi todas
  las claves presentes y sin valor. `cargar_dotenv` ya no exporta las vacías y `_opcional`
  cae al default: sin eso, `OPENAI_BASE_URL=` rompía toda llamada al modelo con un error
  que no menciona el `.env` por ninguna parte.
- **La herramienta `Write` interpreta las secuencias `\uXXXX` del contenido como caracteres
  de verdad.** A un implementador le dejó un byte NUL dentro de un `.tsx` y caracteres
  combinantes invisibles dentro de un regex. Se esquiva escribiendo esos archivos con
  here-strings de PowerShell, y conviene comprobar el resultado (contar bytes NUL y
  caracteres de categoría `Cc`/`Cf`/`Mn`) cuando el contenido lleve `\u`.

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
- **Nunca leas `docs/agentes/plan-agentes.json` entero.** Pasa de 21.000 tokens y crece
  cada fase, porque ahí se van incrustando los hallazgos verificados. Usa:
  `uv run python scripts/ver_plan.py <clave>` — con `fases`, `herramientas`, `guardrails`,
  `agentes`, `contexto`, `fallos`... Sin argumentos lista las claves y lo que pesa cada una.
- Al cambiar a una tarea sin relación con la anterior, `/clear`.
- Tras dos correcciones fallidas sobre lo mismo, `/clear` y reformula.
- Para cambios que tocan varios archivos, plan mode antes de editar.

# Compact instructions

Al compactar, conserva: las decisiones de diseño ya aprobadas, la fase en curso y su
entregable verificable, y lo que quedó PENDIENTE de MaxiCare.

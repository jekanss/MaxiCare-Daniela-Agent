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
  perdida.
- **En `web/src/pantallas/Tratamientos.tsx`, el `useCallback` de `recargar` tiene
  dependencias vacías a propósito, y `alCaducarSesion` se consume por una `ref`.** Meter esa
  prop en las dependencias —lo que pediría cualquier regla de hooks— deja la pantalla
  releyendo Neon en bucle, porque `App.tsx` la pasa como una flecha nueva en cada render. No
  hay `eslint-plugin-react-hooks` ni arnés de pruebas de frontend que lo atrape: se vería
  como una pantalla lenta y una factura rara.

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

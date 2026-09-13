---
paths:
  - "scripts/desplegar.sh"
  - "Dockerfile"
  - "docker-compose.yml"
  - ".dockerignore"
  - ".env.ejemplo"
  - "src/maxicare_daniela/runtime.py"
  - "src/maxicare_daniela/config.py"
---

# El despliegue

`bash scripts/desplegar.sh` empaqueta, copia, construye la imagen, **aplica las migraciones
antes de levantar** y espera a que el contenedor esté sano. Traefik sigue mandando los
webhooks de Meta al contenedor viejo hasta que el nuevo responde `/salud`, así que un
despliegue fallido no deja a los doctores sin radiografías.

- **`desplegar.sh` empaqueta a mano lo que el Dockerfile copia, y ya se desincronizó una
  vez.** El script es de la fase 2; el Dockerfile creció su etapa de Node en la fase 5 y el
  tar nunca creció con él, así que **todo lo construido entre la fase 2 y la 8 se quedó sin
  desplegar** sin que nadie lo supiera: el primer intento moría en `COPY web/ ./` con un
  error sobre checksums que no nombra el script por ninguna parte. Ahora el script deriva la
  lista del propio Dockerfile y aborta antes de subir nada si falta una ruta. Si alguien
  añade un `COPY`, esa comprobación es lo único que lo atrapa.
- **El `.env` que viaja al VPS no es el local tal cual.** Se le quitan las claves vacías
  —ver «Una variable vacía no es una variable ausente», más abajo— y `MAXICARE_COOKIE_INSEGURA`
  se fija en `0`: en local vale `1` porque `http://localhost` rechaza una cookie `Secure`, y
  en el VPS ese mismo `1` publicaría la cookie de sesión del panel sin esa marca.
- **`probar_webhook.py` ya gasta tokens.** Manda un mensaje firmado de verdad, y desde la
  fase 6A eso despierta a Daniela: un turno completo contra el modelo por cada corrida. No
  le escribe a ningún paciente —el número de prueba no existe— pero no es gratis.
- **El `/salud` desplegado es la forma barata de saber qué versión corre.** Publica la fase,
  la clase de calendario y si Daniela responde. Si dice `CalendarioCaido`, Google no arrancó
  y Daniela no puede agendar aunque todo lo demás funcione.
- **Un solo worker, a propósito**, y el `Dockerfile` explica por qué: hay dos estados en
  memoria del proceso (`runtime._tema_general` y `contratos._VOCABULARIO`, que el panel
  reescribe en caliente). Con dos workers, un tratamiento creado desde la pantalla lo
  conocería solo uno de ellos.

## Una variable vacía no es una variable ausente, y hay DOS puertas

El `.env` trae casi todas las claves presentes y **sin valor**. `cargar_dotenv` no exporta las
vacías y `_opcional` cae al default; eso cubre la puerta local. La otra es el `env_file` de
Docker, que **sí** exporta las vacías y ante el cual ese filtro no llega a correr, porque
dentro del contenedor no hay `.env` que leer.

Ya pasó en producción. Con `OPENAI_BASE_URL=` vacía, el cliente de OpenAI la prefiere sobre su
propio default, arma `base_url=""` y **toda** llamada al modelo muere en `APIConnectionError:
Connection error.` **Se lee como un problema de red y no lo es:** las trazas subían a esa misma
API sin problema, `/salud` decía `configuracion: ok`, y el paciente recibía el mensaje seguro
de `atencion.py` como si Daniela hubiera decidido no saber.

Lo cierran dos sitios, y hay que tocar los dos: `runtime.py` con `descartar_vacias_de_terceros()`
al arrancar, y `desplegar.sh`, que no manda ninguna clave vacía al servidor.

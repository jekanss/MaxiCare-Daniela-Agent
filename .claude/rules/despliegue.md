---
paths:
  - "scripts/desplegar.sh"
  - "Dockerfile"
  - "docker-compose.yml"
  - ".dockerignore"
  - ".env.ejemplo"
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
  —ver la trampa de las variables vacías en el `CLAUDE.md` raíz— y `MAXICARE_COOKIE_INSEGURA`
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

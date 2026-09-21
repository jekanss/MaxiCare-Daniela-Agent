#!/usr/bin/env bash
# Despliega Daniela en el VPS.
#
#   bash scripts/desplegar.sh
#
# Copia el código, aplica las migraciones y levanta el contenedor detrás de Traefik. Es
# idempotente: correrlo dos veces no rompe nada.
#
# No toca el túnel de Cloudflare ni Traefik, que viven en /opt/sinpiloto y ya enrutan
# daniela.maxicarecol.com. Este script solo pone lo que responde detrás de ese nombre.

set -euo pipefail

VPS="${VPS:-root@104.248.121.114}"
DESTINO="${DESTINO:-/opt/maxicare-daniela}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$RAIZ"

if [[ ! -f .env ]]; then
  echo "ERROR: falta .env en la raiz del proyecto." >&2
  exit 1
fi

echo "==> Empaquetando"
# Se excluye lo que no hace falta para correr. `docs/` lleva el plan con las decisiones de
# diseño y no tiene por que estar en un servidor de produccion.
TAR="$(mktemp -t daniela-XXXXXX.tar.gz)"
tar --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='docs' --exclude='.claude' --exclude='*.pyc' --exclude='web/node_modules' --exclude='web/dist' \
    -czf "$TAR" \
    pyproject.toml uv.lock Dockerfile docker-compose.yml .dockerignore \
    src migraciones datos scripts web

echo "==> Comprobando que el paquete trae lo que el Dockerfile copia"
# Esta comprobacion existe porque la lista de arriba ya se desincronizo una vez, y costo
# un despliegue entero: web/ entro al Dockerfile en la fase 5 y nunca entro a este tar,
# asi que el primer intento de desplegar algo posterior a la fase 2 murio en el VPS,
# minutos despues, con un error sobre checksums que no menciona este archivo por ninguna
# parte.
#
# Las rutas se leen del propio Dockerfile en vez de mantenerse a mano aqui: una lista a
# mano se vuelve a desincronizar el dia que alguien anada un COPY, y el fallo reaparece
# en el sitio equivocado. Se saltan los 'COPY --from=', que traen cosas de otra etapa de
# la construccion y no de este paquete.
CONTENIDO="$(tar -tzf "$TAR")"
FALTAN=""
RUTAS="$(grep -E '^COPY ' Dockerfile | grep -v -- '--from=' | sed -E 's/^COPY +//; s/ +[^ ]+$//' | sed -E 's/[*]//g')"
for ruta in $RUTAS; do
  grep -qE "^[.]?/?${ruta}" <<<"$CONTENIDO" || FALTAN="$FALTAN $ruta"
done
if [[ -n "$FALTAN" ]]; then
  echo "ERROR: el Dockerfile copia rutas que el paquete no lleva:$FALTAN" >&2
  echo "       Anadelas al 'tar' de este script." >&2
  rm -f "$TAR"
  exit 1
fi


echo "==> Copiando a $VPS:$DESTINO"
ssh "$VPS" "mkdir -p '$DESTINO'"
scp -q "$TAR" "$VPS:$DESTINO/paquete.tar.gz"
rm -f "$TAR"

# El .env viaja aparte y con permisos restringidos: lleva las credenciales de Neon, OpenAI,
# WhatsApp y Telegram.
#
# MAXICARE_COOKIE_INSEGURA es la unica variable del proyecto que significa cosas distintas
# en los dos sitios, asi que NO viaja: se neutraliza aqui. En local vale 1 porque
# http://localhost rechaza una cookie marcada `Secure`; en el VPS --detras de Traefik, que
# sirve HTTPS-- ese mismo 1 publicaria la cookie de sesion del panel sin esa marca. Copiar
# el .env tal cual llevaria el valor de desarrollo a produccion, y nadie lo notaria: el
# panel entra igual de bien con la cookie insegura. Poniendola en 0 aqui, el desarrollador
# no tiene que acordarse de nada antes de desplegar.
ENV_REMOTO="$(mktemp -t daniela-env-XXXXXX)"
chmod 600 "$ENV_REMOTO"
# Las claves presentes y SIN VALOR tampoco viajan, y esto costo un despliegue en falso.
# `cargar_dotenv` ya se niega a exportarlas, pero ese filtro no corre en el contenedor:
# alli las variables las pone el `env_file` de Docker --que exporta las vacias, una por
# cada linea CLAVE= del archivo-- y dentro de la imagen no hay ningun .env que leer. Con
# OPENAI_BASE_URL= vacia, el cliente de OpenAI la prefiere sobre su propio default, arma
# base_url="" y toda llamada al modelo muere en APIConnectionError: un error de red, que
# manda a revisar cortafuegos, mientras /salud sigue diciendo que todo esta bien.
#
# `runtime.py` tambien las descarta al arrancar, asi que esto es el cinturon y aquello
# los tirantes. Ninguno de los dos sobra: el de alla cubre las variables que no vienen de
# este archivo, y el de aca deja el .env del servidor legible para quien lo abra.
grep -vE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[[:space:]]*$' .env | grep -v '^[[:space:]]*MAXICARE_COOKIE_INSEGURA=' > "$ENV_REMOTO"
echo 'MAXICARE_COOKIE_INSEGURA=0' >> "$ENV_REMOTO"
scp -q "$ENV_REMOTO" "$VPS:$DESTINO/.env"
rm -f "$ENV_REMOTO"
ssh "$VPS" "chmod 600 '$DESTINO/.env'"

echo "==> Desempaquetando y construyendo"
ssh "$VPS" bash -s <<EOF
set -euo pipefail
cd '$DESTINO'
tar -xzf paquete.tar.gz && rm -f paquete.tar.gz

# La red la crea el compose de /opt/sinpiloto. Si no existe, Traefik tampoco esta corriendo
# y desplegar aqui no serviria de nada: mejor fallar ahora y con un motivo claro.
docker network inspect maxicare_interna >/dev/null 2>&1 || {
  echo "ERROR: la red maxicare_interna no existe. Levanta primero /opt/sinpiloto." >&2
  exit 1
}

docker compose build
EOF

echo "==> Aplicando migraciones"
# Antes de levantar el servicio, no despues: si el esquema no esta al dia, el contenedor
# nuevo aceptaria webhooks que no puede registrar. Corre en un contenedor de un solo uso.
#
# ESTE PASO ES LO QUE HACE SEGURO SUBIR LA FASE 8. La marca de asistencia del panel escribe
# `citas.asistio` y su fila de bitacora en la MISMA transaccion, y hasta la migracion 021 el
# CHECK de `cambios_configuracion.tabla` no admitia 'citas': sin ella, el INSERT revienta y
# se lleva por delante el UPDATE, asi que la columna queda inescribible y la pantalla de
# Agenda sale rota en produccion. Saltarse este paso --o desplegar por cualquier otro
# camino-- es lo unico que puede dejar ese estado. `inicializar_base.py` verifica la 021 en
# los dos esquemas al terminar y sale con codigo 1 si falta, asi que este comando falla
# RUIDOSAMENTE antes de levantar nada.
ssh "$VPS" "cd '$DESTINO' && docker compose run --rm --no-deps daniela \
    uv run python scripts/inicializar_base.py"

echo "==> Levantando"
ssh "$VPS" "cd '$DESTINO' && docker compose up -d"

echo "==> Esperando a que este sano"
ssh "$VPS" bash -s <<'EOF'
set -euo pipefail
for i in $(seq 1 30); do
  estado="$(docker inspect --format '{{.State.Health.Status}}' maxicare-daniela-daniela-1 2>/dev/null || echo 'sin-contenedor')"
  if [[ "$estado" == "healthy" ]]; then
    echo "contenedor healthy"
    break
  fi
  sleep 3
done
[[ "$estado" == "healthy" ]] || {
  echo "El contenedor no llego a healthy. Ultimos logs:" >&2
  docker logs --tail 40 maxicare-daniela-daniela-1 >&2 || true
  exit 1
}

echo
echo "--- a traves de Traefik, como llega el trafico real ---"
curl -s -H 'Host: daniela.maxicarecol.com' http://127.0.0.1/salud
echo
EOF

echo
echo "==> Listo. Comprueba desde fuera:"
echo "    uv run python scripts/probar_webhook.py https://daniela.maxicarecol.com"

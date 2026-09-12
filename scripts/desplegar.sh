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
    --exclude='docs' --exclude='.claude' --exclude='*.pyc' \
    -czf "$TAR" \
    pyproject.toml uv.lock Dockerfile docker-compose.yml .dockerignore \
    src migraciones datos scripts

echo "==> Copiando a $VPS:$DESTINO"
ssh "$VPS" "mkdir -p '$DESTINO'"
scp -q "$TAR" "$VPS:$DESTINO/paquete.tar.gz"
rm -f "$TAR"

# El .env viaja aparte y con permisos restringidos: lleva las credenciales de Neon, OpenAI,
# WhatsApp y Telegram.
scp -q .env "$VPS:$DESTINO/.env"
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

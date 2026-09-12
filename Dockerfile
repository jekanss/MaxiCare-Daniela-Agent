# La imagen de Daniela.
#
# Dos etapas de `uv sync` y no una: la primera instala solo las dependencias, que cambian
# una vez al mes; la segunda instala el proyecto, que cambia cada despliegue. Así un cambio
# en `ingesta.py` no vuelve a bajar FastAPI y psycopg desde cero.

# ══════════════════════════════════════════════════════════════════════════════════════════
# Etapa 0 · la interfaz web
# ══════════════════════════════════════════════════════════════════════════════════════════
#
# Node vive aquí y solo aquí. De esta etapa sale una carpeta de archivos estáticos —HTML, CSS
# y JavaScript ya compilados— y nada más: ni Node, ni los 82 paquetes de `node_modules`, ni
# el compilador de TypeScript llegan a la imagen final. Eso es lo que compra una etapa
# aparte; con un solo `FROM`, la imagen de producción cargaría con todo el andamiaje.
#
# La construcción falla si el frontend no compila. Es deliberado: un despliegue que sube un
# backend nuevo con un frontend viejo es exactamente el estado que nadie sabe diagnosticar.

FROM node:22-slim AS web

WORKDIR /web

# `package*.json` primero y en su propia capa: las dependencias cambian una vez al mes y el
# código cada despliegue. Copiarlo todo junto volvería a bajar React en cada cambio de CSS.
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
RUN npm run build


# ══════════════════════════════════════════════════════════════════════════════════════════
# Etapa 1 · el servidor
# ══════════════════════════════════════════════════════════════════════════════════════════

FROM python:3.12-slim

# `uv` viene de su propia imagen oficial: instalarlo con pip dentro de la imagen añade una
# resolución de dependencias que no hace falta.
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=America/Bogota

WORKDIR /app

# ── capa 1: dependencias ──────────────────────────────────────────────────────────────────
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── capa 2: el proyecto ───────────────────────────────────────────────────────────────────
# `docs/` y `tests/` no se copian: el servidor no los necesita para correr, y el plan con
# las decisiones de diseño no tiene por qué vivir en producción.
COPY src ./src
COPY migraciones ./migraciones
COPY datos ./datos
COPY scripts ./scripts

# La interfaz web ya compilada, tal como la busca `runtime.RUTA_WEB`. Si esta ruta cambia,
# cambia también esa constante: son las dos mitades del mismo acuerdo.
COPY --from=web /web/dist ./web/dist

RUN uv sync --frozen --no-dev

# No corre como root. Si alguien lograra ejecutar algo dentro del contenedor, no sería
# administrador de él.
RUN useradd --create-home --uid 10001 daniela && chown -R daniela:daniela /app
USER daniela

EXPOSE 8080

# `/salud` no dice solo «estoy vivo»: comprueba Neon y la configuración. Un contenedor que
# arranca pero no alcanza la base queda marcado unhealthy en vez de aceptar webhooks que no
# puede registrar.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/salud',timeout=8).status==200 else 1)"

# Un solo worker a propósito. El volumen de una clínica no lo necesita, y con varios habría
# que sacar a un sitio compartido los DOS estados en memoria del proceso:
#
#   - `runtime._tema_general`, el tema del supergrupo de Telegram, que se lee al arrancar.
#   - `contratos._VOCABULARIO`, los tratamientos que la clínica ofrece hoy, que el panel
#     reescribe en caliente con cada POST o PATCH de `/api/tratamientos`.
#
# El segundo es el que no se puede dejar pasar. Con `--workers 2`, un tratamiento creado
# desde el panel lo conocería solo el worker que atendió esa petición: Daniela lo cotizaría
# o diría que no existe según a qué worker le tocara el siguiente mensaje de WhatsApp. Eso
# no se lee como una decisión de despliegue, se lee como un modelo caprichoso, y se
# depuraría durante días en el sitio equivocado.
CMD ["uv", "run", "uvicorn", "maxicare_daniela.runtime:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

"""La fábrica de sesiones persistidas, sin tocar la red.

Todo lo de aquí corre offline: construir un `AsyncEngine` no abre ninguna conexión --el
pool es perezoso-- así que se puede comprobar el dialecto, el esquema y el pool sin Neon.
Lo que sí necesita base vive en `tests/test_sesion_neon.py`.
"""

from __future__ import annotations

import pytest

from maxicare_daniela import persistencia

URL_NEON = (
    "postgresql://usuario:clave@ep-algo-pooler.c-5.us-east-2.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def test_una_url_sincrona_se_reescribe_al_dialecto_asincrono():
    assert persistencia.url_asincrona("postgresql://u:c@host/db").startswith(
        "postgresql+psycopg://"
    )


def test_los_parametros_de_consulta_sobreviven_intactos():
    """`sslmode` y `channel_binding` son de psycopg y son obligatorios en Neon. Perderlos
    no daría un error de sintaxis: daría una conexión rechazada en producción."""
    reescrita = persistencia.url_asincrona(URL_NEON)

    assert "sslmode=require" in reescrita
    assert "channel_binding=require" in reescrita
    assert "ep-algo-pooler.c-5.us-east-2.aws.neon.tech/neondb" in reescrita


def test_una_url_ya_reescrita_no_se_reescribe_dos_veces():
    """Idempotente a propósito: esta función la puede llamar más de un sitio, y
    `postgresql+psycopg+psycopg://` no es un dialecto."""
    una_vez = persistencia.url_asincrona(URL_NEON)

    assert persistencia.url_asincrona(una_vez) == una_vez


def test_otro_dialecto_asincrono_se_respeta():
    """Si alguien fija `asyncpg` a propósito en el entorno, esto no se lo pisa."""
    url = "postgresql+asyncpg://u:c@host/db"

    assert persistencia.url_asincrona(url) == url


def test_una_url_vacia_se_rechaza_con_un_mensaje_util():
    with pytest.raises(ValueError, match="MAXICARE_DATABASE_URL"):
        persistencia.url_asincrona("")

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
    with pytest.raises(ValueError, match="vacía"):
        persistencia.url_asincrona("")


def test_url_malformada_no_filtra_credenciales():
    """Si la URL llega malformada (sin ://), el mensaje de error NO debe echar el valor
    crudamente. Si `MAXICARE_DATABASE_URL` se copió mal pero aun así trae credenciales,
    el error no debe filtrarlas en los logs."""
    url_con_clave = "usuario:clave-secreta@host/db"

    with pytest.raises(ValueError) as exc_info:
        persistencia.url_asincrona(url_con_clave)

    # El mensaje de error NO debe contener la cadena que se le pasó
    assert "clave-secreta" not in str(exc_info.value)
    # Sí debe mencionar el problema real
    assert "://" in str(exc_info.value)


# ==========================================================================================
# `sesion_de_agente` -- la fábrica de sesiones persistidas (Tarea 3)
# ==========================================================================================


def test_la_sesion_no_crea_sus_tablas():
    """`create_tables=False` es un no negociable de la fase: las tablas las crea la
    migración 010. Con `True`, el proceso web ejecutaría DDL al arrancar y el esquema del
    proyecto dejaría de leerse entero en `migraciones/`."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._create_tables is False


def test_la_sesion_guarda_los_acentos_sin_escapar():
    """El default del SDK es `ensure_ascii=True` y deja «informacio\\u00f3n» en la base. El
    ida y vuelta sería correcto igual, pero el texto no se lee a ojo, y eso son horas
    perdidas depurando el día que haya que mirar un historial a mano."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._ensure_ascii is False


def test_la_sesion_lleva_el_id_de_la_conversacion():
    sesion = persistencia.sesion_de_agente("conv-abc", database_url=URL_NEON)

    assert sesion.session_id == "conv-abc"


def test_sin_esquema_no_se_traduce_nada():
    """En producción las tablas están en `public`, que es donde apunta la conexión."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    opciones = sesion._engine.sync_engine.get_execution_options()

    assert "schema_translate_map" not in opciones


def test_con_esquema_las_sentencias_se_cualifican_al_compilarlas():
    """`schema_translate_map` y NO `options=-csearch_path=`: el pooler de Neon rechaza
    `options` como parámetro de arranque («unsupported startup parameter in options:
    search_path»). SQLAlchemy cualifica al compilar, no al abrir la conexión, así que el
    pooler no tiene nada que rechazar."""
    sesion = persistencia.sesion_de_agente(
        "conv-1", database_url=URL_NEON, esquema="pruebas_web"
    )
    opciones = sesion._engine.sync_engine.get_execution_options()

    assert opciones["schema_translate_map"] == {None: "pruebas_web"}


def test_el_engine_usa_el_dialecto_asincrono():
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._engine.dialect.name == "postgresql"
    assert sesion._engine.dialect.is_async is True


def test_sin_limite_el_historial_va_entero():
    """Paso 1 de los tres del spec: persistir SIN límite. Medir viene después, y fijar el
    número viene después de medir. Un límite puesto ahora sería otra suposición."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion.session_settings.limit is None


# ==========================================================================================
# Un engine por base, no uno por turno (Tarea 4)
# ==========================================================================================


def test_dos_sesiones_de_la_misma_base_comparten_el_engine():
    """Un `AsyncEngine` por turno es un pool de conexiones por turno, y Neon tiene techo.
    El engine se comparte; lo que es barato de crear es la `SQLAlchemySession`."""
    a = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    b = persistencia.sesion_de_agente("conv-2", database_url=URL_NEON)

    assert a._engine is b._engine


def test_esquemas_distintos_no_comparten_engine():
    """`schema_translate_map` va en el engine, así que `public` y `pruebas_web` no pueden
    compartir uno: compartirlo mandaría las filas del chat de pruebas a la base real."""
    produccion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    pruebas = persistencia.sesion_de_agente(
        "conv-1", database_url=URL_NEON, esquema="pruebas_web"
    )

    assert produccion._engine is not pruebas._engine


def test_el_pool_esta_dimensionado_a_mano():
    """No el default de SQLAlchemy, que nadie eligió para este proyecto ni para el techo de
    conexiones de este plan de Neon."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    pool = sesion._engine.pool

    assert pool.size() == persistencia.TAMANO_POOL_SESIONES


def test_cerrar_engines_vacia_la_cache():
    """Sin esto, una prueba deja un engine atado a su bucle de eventos y la siguiente se
    encuentra un `got Future attached to a different loop`."""
    import asyncio

    persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    assert persistencia._engines

    asyncio.run(persistencia.cerrar_engines())

    assert not persistencia._engines

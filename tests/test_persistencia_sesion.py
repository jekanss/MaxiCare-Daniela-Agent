"""La fábrica de sesiones persistidas, sin tocar la red.

Todo lo de aquí corre offline: construir un `AsyncEngine` no abre ninguna conexión --el
pool es perezoso-- así que se puede comprobar el dialecto, el esquema y el pool sin Neon.
Lo que sí necesita base vive en `tests/test_sesion_neon.py`.
"""

from __future__ import annotations

import pytest

from maxicare_daniela import config, persistencia

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


# `create_tables=False` NO se comprueba aquí, y la ausencia es deliberada.
#
# Aquí vivía `test_la_sesion_no_crea_sus_tablas`, con `assert sesion._create_tables is
# False`. No podía caer: el default del SDK 0.22.2 es ese mismo valor, así que borrar el
# argumento de `sesion_de_agente` dejaba la prueba en verde -- comprobado por la revisión
# final borrándolo entero: 25 passed. Una comprobación no puede distinguir un valor de su
# default idéntico.
#
# Lo que sí vigila el invariante son dos sondas de comportamiento, y las dos necesitan base:
# `tests/test_sesion_neon.py::test_la_sesion_no_se_crea_sus_tablas` y la MITAD A de
# `scripts/probar_persistencia.py`. Las dos hacen lo mismo: contra un esquema SIN las tablas,
# la sesión tiene que fallar en vez de creárselas. Que `uv run pytest -q` no las corra es
# cierto y está dicho; una prueba offline que no puede caer no lo arregla, lo disimula.


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


def test_el_historial_va_acotado_por_un_tope_de_seguridad():
    """Por defecto el historial va ACOTADO, y esta prueba existe para que no pueda volver a
    `None` sin que alguien lo note.

    Aquí decía lo contrario --`assert ... is None`-- y era correcto mientras «sin límite» solo
    significara «más caro en tokens». No era eso. La ventana de conversación de 24 h es
    deslizante, así que un historial puede crecer sin tope; cuando la petición cruza el techo
    de la cuenta, la API devuelve un `openai.BadRequestError` que NO es una `AgentsException`,
    nadie lo traduce, y el paciente recibe el mensaje seguro en ese turno y en todos los
    siguientes, sin caducidad y sin `/clearstate` si su número no está en la lista. Un
    paciente atascado para siempre no es un problema de coste.

    Lo que se afirma aquí es solo que hay tope. NO se afirma el número: la aritmética que lo
    deriva --techo de la cuenta x fracción segura / tokens por item, todo medido-- vive junto
    a la constante, y el día que la 13b ponga el límite MEDIDO (más pequeño, porque optimiza
    coste y no seguridad) esta prueba tiene que seguir en verde sin tocarla.
    """
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion.session_settings.limit is not None, (
        "el historial volvió a ir entero: sin tope, una conversación larga acaba en un "
        "`context_length_exceeded` del que el paciente no sale nunca"
    )
    assert sesion.session_settings.limit == config.LIMITE_HISTORIAL_SESION


def test_un_limite_explicito_gana_al_de_config():
    """Para poder probar el borde del recorte sin depender del número de producción."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON, limite=4)

    assert sesion.session_settings.limit == 4


def test_sin_limite_explicito_el_default_sale_de_config(monkeypatch):
    """La que caza el cableado, y por eso no mira el número de producción sino uno inventado.

    Con la constante cableada, cambiarla cambia lo que la sesión recorta. Sin cablear
    --`limite: int | None = None` en la firma, que es como estuvo toda la fase-- la sesión
    saldría con `None` pase lo que pase en `config`, y el día que la Tarea 13b escriba el
    número medido no pasaría absolutamente nada: el historial seguiría yendo entero y nadie
    se enteraría hasta que a un paciente se le reventara el turno por contexto.

    El `12` no significa nada y es a propósito: esta prueba tiene que seguir en verde cuando
    la 13b sustituya el `PENDIENTE` por el número real.
    """
    monkeypatch.setattr(config, "LIMITE_HISTORIAL_SESION", 12)

    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion.session_settings.limit == 12


def test_limite_none_explicito_no_recorta(monkeypatch):
    """`None` es un valor legítimo --«sin límite»-- y NO puede significar «usa el default».

    Por eso el centinela del parámetro es `-1` y no `None`: las pruebas del borde necesitan
    poder pedir un historial sin recortar aunque `config` traiga un número, y con `None` como
    centinela esa petición sería indistinguible de no pedir nada.
    """
    monkeypatch.setattr(config, "LIMITE_HISTORIAL_SESION", 12)

    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON, limite=None)

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


def test_el_desbordo_del_pool_esta_dimensionado_a_mano():
    """`max_overflow` es la otra mitad del presupuesto de conexiones contra el techo de
    Neon: el pico real de un engine es `pool_size + max_overflow`, no `pool_size`. Con el
    default de SQLAlchemy (10) el pico se triplicaría en silencio."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._engine.pool._max_overflow == persistencia.DESBORDO_POOL_SESIONES


def test_el_pool_comprueba_la_conexion_antes_de_usarla():
    """Neon cierra las conexiones ociosas por su cuenta. Sin `pool_pre_ping`, la primera
    consulta después de un rato tranquilo revienta con una conexión muerta -- y sería el
    primer paciente de la mañana quien se lo encontrara.

    El docstring de `_engine_de` ya decía esto y no lo comprobaba nadie: quitar el ajuste
    dejaba la suite entera en verde."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._engine.pool._pre_ping is True


def test_el_pool_recicla_las_conexiones_antes_que_neon():
    """`pool_recycle` es el cinturón del `pre_ping`: descarta por edad una conexión que
    lleva demasiado tiempo abierta en vez de esperar a descubrir que está muerta. Cinco
    minutos es lo que se eligió; el default de SQLAlchemy es -1, o sea nunca reciclar."""
    sesion = persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)

    assert sesion._engine.pool._recycle == 300


def test_cerrar_engines_vacia_la_cache():
    """Sin esto, una prueba deja un engine atado a su bucle de eventos y la siguiente se
    encuentra un `got Future attached to a different loop`."""
    import asyncio

    persistencia.sesion_de_agente("conv-1", database_url=URL_NEON)
    assert persistencia._engines

    asyncio.run(persistencia.cerrar_engines())

    assert not persistencia._engines


class _EngineFalso:
    """Un engine que puede negarse a cerrarse. No toca la red: `cerrar_engines` solo llama
    a `dispose()`, así que un objeto con ese método basta."""

    def __init__(self, *, revienta: bool = False) -> None:
        self.revienta = revienta
        self.dispuesto = False

    async def dispose(self) -> None:
        if self.revienta:
            raise RuntimeError("este pool ya estaba roto")
        self.dispuesto = True


def test_un_engine_que_no_cierra_no_impide_cerrar_los_demas(monkeypatch):
    """El peor estado posible era el que dejaba la versión anterior: con el bucle delante
    del `clear()` y sin `try`, el primer `dispose()` que lanzara dejaba los engines
    siguientes SIN cerrar y el diccionario LLENO de engines a medio disponer -- que es
    justo lo que esta función existe para impedir, porque la siguiente
    `sesion_de_agente` los encontraría en la caché y escribiría contra ellos."""
    import asyncio

    roto = _EngineFalso(revienta=True)
    sano = _EngineFalso()
    monkeypatch.setattr(
        persistencia, "_engines", {("url-a", None): roto, ("url-b", None): sano}
    )

    asyncio.run(persistencia.cerrar_engines())

    assert sano.dispuesto is True, "un pool roto se llevó por delante a los demás"
    assert not persistencia._engines, "la caché quedó con engines a medio disponer"


def test_un_engine_que_no_cierra_no_tumba_el_apagado(monkeypatch):
    """`cerrar_engines` la llama el `shutdown` del servidor. Si dejara subir la excepción,
    un pool ya roto convertiría un apagado ordenado en un error."""
    import asyncio

    monkeypatch.setattr(
        persistencia, "_engines", {("url-a", None): _EngineFalso(revienta=True)}
    )

    asyncio.run(persistencia.cerrar_engines())  # no debe lanzar

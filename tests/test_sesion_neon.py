"""La sesión persistida contra Neon de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Escribe en el esquema `pruebas_sesion`, que se crea y se borra aquí. NUNCA en `public`:
allí hay pacientes reales de una clínica.

Hoy el aislamiento va por `search_path`: este archivo habla `psycopg` crudo, sin SQLAlchemy
de por medio, así que el DDL (crear y borrar el esquema) y las comprobaciones contra
`information_schema` viajan por la conexión directa --sin `-pooler.`-- con
`options=-csearch_path={ESQUEMA}`. La conexión directa no es un capricho: el pooler de Neon
rechaza `options` como parámetro de arranque, así que sin quitarle el `-pooler.` al host no
hay forma de fijar el `search_path` de la sesión.

Los dos mecanismos conviven en este archivo, cada uno con lo suyo: `search_path` para el
`psycopg` crudo de aquí, `schema_translate_map` para lo que el SDK escriba encima. Y
conviven SEPARADOS, que es la parte que costó: ver `_sin_options`.

------------------------------------------------------------------------------------------
`_correr` -- por qué las pruebas de la Tarea 3 no usan `asyncio.run` a secas
------------------------------------------------------------------------------------------
En Windows, `asyncio.run` arranca un `ProactorEventLoop`, y el modo async de psycopg lo
rechaza en el propio `connect()`: `InterfaceError: Psycopg cannot use the 'ProactorEvent
Loop' to run in async mode`. En Linux -- donde corre el VPS -- este problema no existe:
`SelectorEventLoop` ya es el loop por defecto, así que en producción `asyncio.run` a secas
funciona sin este envoltorio. Se aísla en una función de este módulo, y no se toca la
política global de `asyncio` para toda la sesión de pytest, para no arriesgar otros módulos
que sí puedan depender del loop por defecto de la plataforma.
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from typing import Any, Coroutine, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_sesion"

_T = TypeVar("_T")


def _correr(corutina: Coroutine[Any, Any, _T]) -> _T:
    """`asyncio.run`, salvo en Windows, donde fuerza un `SelectorEventLoop` -- ver el
    docstring del módulo."""
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        try:
            return loop.run_until_complete(corutina)
        finally:
            loop.close()
    return asyncio.run(corutina)


def _url_cruda() -> str:
    """La `MAXICARE_DATABASE_URL` tal cual la trae el entorno: CON `-pooler.`, que es por
    donde entra producción."""
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("falta MAXICARE_DATABASE_URL")
    return base


def _url_directa() -> str:
    """La URL de Neon sin el pooler. Hace falta para crear y borrar el esquema con
    `search_path`, que es lo único de aquí que sigue necesitando la conexión directa."""
    return _url_cruda().replace("-pooler.", ".")


def _sin_options(url: str) -> str:
    """La misma URL sin el parámetro de consulta `options`, y con todo lo demás intacto.

    ------------------------------------------------------------------------------------
    Por qué esto NO se puede hacer con un `split("?options=")`
    ------------------------------------------------------------------------------------

    Es lo que hacía antes, y no cortaba nada. La `MAXICARE_DATABASE_URL` de Neon YA trae
    query (`sslmode`, `channel_binding`), así que la fixture añadió el parámetro con `&` y
    no con `?`: la cadena `"?options="` no aparecía en ninguna parte y el corte devolvía la
    URL entera.

    Lo que eso rompía no era la corrección --las filas cayeron donde tenían que caer, se
    comprobó-- sino la ATRIBUCIÓN: la sesión del SDK viajaba con `search_path` Y con
    `schema_translate_map` a la vez, así que
    `test_las_tablas_de_sesion_no_se_crean_en_public` habría pasado igual con
    `schema_translate_map` borrado de `_engine_de`, porque el `search_path` de la conexión
    mandaba la fila al mismo sitio. La prueba que más peso carga de la fase no probaba el
    mecanismo que dice probar.

    Con el parámetro fuera de verdad, `schema_translate_map` queda solo frente al
    aislamiento, que es lo único que hay en producción.
    """
    partes = urlsplit(url)
    query = [
        (clave, valor)
        for clave, valor in parse_qsl(partes.query, keep_blank_values=True)
        if clave != "options"
    ]
    return urlunsplit(partes._replace(query=urlencode(query)))


class _UrlOculta(str):
    """Una cadena de conexión que no se imprime entera. Es un `str` y funciona como uno.

    Cuando una prueba de este archivo falla, pytest encabeza el informe con los valores de
    sus argumentos -- y el argumento `esquema` ES la URL de Neon, con la contraseña de la
    base de la clínica en claro. No hace falta ninguna aserción torcida para filtrarla:
    basta con que la prueba caiga. Ya pasó una vez en esta fase por otro camino (Tarea 6);
    esto cierra el que queda.

    `repr` y no `str`: pytest usa `saferepr` para ese encabezado, y `persistencia.conectar`
    y `urlsplit` siguen recibiendo la cadena entera, que es lo que necesitan.
    """

    def __repr__(self) -> str:
        return "'***@neon (oculta: ver _UrlOculta)'"


@pytest.fixture(scope="module")
def url() -> str:
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    return _UrlOculta(_url_directa())


@pytest.fixture(scope="module")
def esquema(url: str):
    """Crea el esquema, aplica TODAS las migraciones dentro, y lo borra pase lo que pase."""
    separador = "&" if "?" in url else "?"
    con_esquema = f"{url}{separador}options=-csearch_path%3D{ESQUEMA}"

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()

    with persistencia.conectar(con_esquema) as conn:
        persistencia.aplicar_esquema(conn)

    yield _UrlOculta(con_esquema)

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()


def _columnas(conn, tabla: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
            """,
            (ESQUEMA, tabla),
        )
        return {fila[0]: fila[1] for fila in cur.fetchall()}


def test_la_migracion_crea_las_dos_tablas_del_sdk(esquema):
    with persistencia.conectar(esquema) as conn:
        sesiones = _columnas(conn, "agent_sessions")
        mensajes = _columnas(conn, "agent_messages")

    assert set(sesiones) == {"session_id", "created_at", "updated_at"}
    assert set(mensajes) == {"id", "session_id", "message_data", "created_at"}


def test_las_marcas_de_tiempo_son_sin_zona_como_las_escribe_el_sdk(esquema):
    """`TIMESTAMP WITHOUT TIME ZONE`, no `TIMESTAMPTZ` como el resto de este esquema.

    Se respeta lo que el SDK espera en vez de mejorarlo: una columna «mejor» que la que el
    SDK escribe es una incompatibilidad esperando a que alguien active `create_tables=True`
    en una base nueva y se encuentre dos esquemas distintos con el mismo nombre.
    """
    with persistencia.conectar(esquema) as conn:
        sesiones = _columnas(conn, "agent_sessions")
        mensajes = _columnas(conn, "agent_messages")

    assert sesiones["created_at"] == "timestamp without time zone"
    assert sesiones["updated_at"] == "timestamp without time zone"
    assert mensajes["created_at"] == "timestamp without time zone"


def test_borrar_la_sesion_arrastra_sus_mensajes(esquema):
    """El `ON DELETE CASCADE` es lo que hace que `/clearstate` pueda borrar el historial
    con una sola sentencia. Sin él quedarían mensajes huérfanos de una sesión que ya no
    existe, invisibles y sin forma de llegar a ellos."""
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO agent_sessions (session_id) VALUES ('conv-cascada')")
            cur.execute(
                "INSERT INTO agent_messages (session_id, message_data) VALUES (%s, %s)",
                ("conv-cascada", '{"role":"user","content":"hola"}'),
            )
            cur.execute("DELETE FROM agent_sessions WHERE session_id = 'conv-cascada'")
            cur.execute(
                "SELECT count(*) FROM agent_messages WHERE session_id = 'conv-cascada'"
            )
            quedan = cur.fetchone()[0]
        conn.commit()

    assert quedan == 0


def test_el_indice_por_sesion_y_tiempo_existe(esquema):
    """El SDK ordena por `(session_id, created_at)` en cada `get_items`, que es una vez por
    turno. Sin índice eso es un scan de la tabla entera de historiales."""
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (ESQUEMA, "agent_messages"),
        )
        indices = {fila[0] for fila in cur.fetchall()}

    assert "idx_agent_messages_session_time" in indices


# ==========================================================================================
# `sesion_de_agente` contra las tablas reales de la migración (Tarea 3)
# ==========================================================================================


def test_ida_y_vuelta_contra_las_tablas_de_la_migracion(esquema):
    """LA PRUEBA QUE CAZA QUE EL SDK HAYA CAMBIADO SU ESQUEMA.

    Con `create_tables=False`, el SDK escribe contra las tablas que creó la 010. Si una
    versión futura del SDK añade o renombra una columna, esta prueba cae con un error de
    Postgres -- que es exactamente lo que se quiere, porque la alternativa es que la
    migración se quede vieja en silencio y nadie se entere hasta producción.
    """
    sesion = persistencia.sesion_de_agente(
        "conv-ida-vuelta", database_url=_sin_options(esquema), esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "quiero agendar una valoración"},
                {"role": "assistant", "content": "Claro, ¿qué tratamiento te interesa?"},
            ]
        )
        return await sesion.get_items()

    items = _correr(correr())
    _correr(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == [
        "quiero agendar una valoración",
        "Claro, ¿qué tratamiento te interesa?",
    ]


def test_las_tablas_de_sesion_no_se_crean_en_public(esquema):
    """El aislamiento por `schema_translate_map` es lo único que separa una prueba de la
    base real de la clínica. Se comprueba consultando el catálogo, no asumiendo.

    La primera aserción no es ceremonia: es lo que hace verdadera a la frase anterior. Con
    un `options=-csearch_path=` colado en la URL, la conexión mandaría la fila al esquema
    de pruebas por su cuenta y esta prueba pasaría igual con `schema_translate_map` borrado
    de `_engine_de`. Ver `_sin_options`.
    """
    base = _sin_options(esquema)
    assert "options" not in base, (
        "la URL de la sesión lleva un search_path: el aislamiento ya no lo sostiene "
        "schema_translate_map solo, y esta prueba dejó de probar lo que dice"
    )

    sesion = persistencia.sesion_de_agente(
        "conv-aislada", database_url=base, esquema=ESQUEMA
    )
    _correr(sesion.add_items([{"role": "user", "content": "hola"}]))
    _correr(persistencia.cerrar_engines())

    with persistencia.conectar(_url_directa()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            " WHERE table_schema = 'public' AND table_name = 'agent_messages'"
        )
        en_public = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM %s.agent_messages WHERE session_id = 'conv-aislada'"
            % ESQUEMA
        )
        en_pruebas = cur.fetchone()[0]

    # `public` SÍ tiene la tabla, porque `inicializar_base.py` aplica la 010 allí. Lo que
    # esta prueba exige es que la FILA no haya caído ahí.
    assert en_public == 1, "la 010 debería estar aplicada en public"
    assert en_pruebas == 1, "la fila tenía que caer en el esquema de pruebas"

    with persistencia.conectar(_url_directa()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.agent_messages WHERE session_id = 'conv-aislada'"
        )
        assert cur.fetchone()[0] == 0, "la fila de una prueba acabó en la base de la clínica"


def test_el_historial_sobrevive_a_tirar_la_sesion(esquema):
    """El reinicio simulado: una sesión escribe, se tira, otra con el mismo `session_id`
    lee, y está todo. Es la mitad del entregable de la fase que se puede comprobar sin
    levantar dos procesos; la otra mitad la hace `scripts/probar_persistencia.py`."""
    base = _sin_options(esquema)

    primera = persistencia.sesion_de_agente(
        "conv-reinicio", database_url=base, esquema=ESQUEMA
    )
    _correr(primera.add_items([{"role": "user", "content": "me llamo Ana"}]))
    _correr(persistencia.cerrar_engines())
    del primera

    segunda = persistencia.sesion_de_agente(
        "conv-reinicio", database_url=base, esquema=ESQUEMA
    )
    items = _correr(segunda.get_items())
    _correr(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == ["me llamo Ana"]


# ==========================================================================================
# El camino de producción: el POOLER, con la conexión reusada
# ==========================================================================================


def test_la_sesion_funciona_contra_el_pooler(esquema):
    """EL ÚNICO SITIO DEL REPOSITORIO QUE EJERCITA EL CAMINO DE PRODUCCIÓN.

    Todo lo demás de este archivo va por el host DIRECTO --sin `-pooler.`-- porque necesita
    fijar `search_path`, que el pooler rechaza como parámetro de arranque. Producción no:
    producción entra por el pooler, y la `SQLAlchemySession` no necesita `search_path`
    porque se aísla al compilar. Sin esta prueba, la pieza sobre la que se construyen diez
    tareas más no la tocaba nadie por el camino en el que va a correr.

    ------------------------------------------------------------------------------------
    Por qué DIEZ vueltas y no una
    ------------------------------------------------------------------------------------

    Esta es la primera vez en todo el proyecto en que una conexión a Neon se REUSA:
    `persistencia.conectar` abre y cierra una por llamada, así que nunca se topó con lo que
    un pool sí provoca. Y lo que provoca es esto: psycopg 3 auto-prepara una sentencia tras
    5 ejecuciones en la misma conexión (`prepare_threshold=5`), y un pooler en modo
    transacción es históricamente hostil a las prepared statements. Diez vueltas cruzan ese
    umbral con margen.

    Medido el 13/09/2026 contra el pooler real antes de escribir nada: pasa. Y el pooler de
    Neon sí soporta prepared statements -- ejecutando doce veces la MISMA consulta sobre una
    conexión del pool, `pg_prepared_statements` llegó a 1 y se reusó sin error. Por eso
    `_engine_de` NO lleva `prepare_threshold=None`: no hay problema que desactivar.

    (El matiz medido, para que nadie lo dé por más de lo que es: el pool de SQLAlchemy hace
    `ROLLBACK` al devolver cada conexión, y psycopg descarta su estado de preparadas en cada
    `ROLLBACK` -- `PrepareManager._should_discard`. Así que por este camino el contador
    vuelve a cero en cada checkout y el umbral, en la práctica, no se cruza. Lo que esta
    prueba garantiza de verdad es el ida y vuelta repetido contra el pooler con la conexión
    reusada; que las preparadas funcionen se comprobó aparte y está dicho arriba.)
    """
    cruda = _url_cruda()
    if "-pooler." not in cruda:
        pytest.skip("MAXICARE_DATABASE_URL no apunta al pooler: no hay camino que probar")
    assert "options" not in cruda, (
        "el pooler rechaza `options` como parámetro de arranque: esta URL no es la de "
        "producción"
    )
    # La fixture ya creó el esquema y aplicó las migraciones: es la misma base, se llegue
    # por el pooler o por el host directo.
    assert esquema

    sesion = persistencia.sesion_de_agente(
        "conv-pooler", database_url=cruda, esquema=ESQUEMA
    )

    async def correr():
        for vuelta in range(1, 11):
            await sesion.add_items([{"role": "user", "content": f"vuelta {vuelta}"}])
            await sesion.get_items()
        return await sesion.get_items()

    try:
        items = _correr(correr())
    finally:
        _correr(persistencia.cerrar_engines())

    assert [i["content"] for i in items] == [f"vuelta {v}" for v in range(1, 11)]


# ==========================================================================================
# El recorte no puede partir un par tool/salida (Tarea 7)
#
# QUÉ BORDE ES, exactamente, y por qué la primera versión de estas dos pruebas miraba el
# otro lado.
#
# `SQLAlchemySession.get_items(limit=n)` de la 0.22.2 es, leído en el SDK instalado,
# `ORDER BY created_at DESC, id DESC LIMIT n`, invertido después, más `items[-n:]`. Ni una
# línea sobre `call_id`. Es un «últimos N» puro, y un «últimos N» tiene una asimetría:
#
#     [ user | assistant | LLAMADA | SALIDA ]
#                                  ^-- limit=1 se lleva SOLO la salida
#
# Si la LLAMADA entra en la ventana, su SALIDA --escrita después, con `id` mayor-- entra
# siempre. Así que `llamadas <= salidas` es una TAUTOLOGÍA bajo esta semántica: no hay
# recorte que pueda romperla, y una prueba que la afirma no prueba nada. El huérfano que
# este recorte sí produce es el INVERSO: un `function_call_output` cuya llamada se cayó por
# delante de la ventana. La API de OpenAI rechaza los dos por igual.
#
# De ahí la aserción de estas dos pruebas: `salidas <= llamadas`.
# ==========================================================================================

#: Un turno con una tool, en items del SDK. La llamada y su salida son DOS items: es la
#: diferencia que hace falsa la equivalencia «40 items = 5 conversaciones».
_LLAMADA = {
    "type": "function_call",
    "call_id": "call_abc",
    "name": "consultar_disponibilidad",
    "arguments": '{"inicio":"2026-09-16T10:00:00-05:00"}',
}
_SALIDA = {
    "type": "function_call_output",
    "call_id": "call_abc",
    "output": "10:00; 11:00; 14:00",
}


def _pares(items) -> tuple[set, set]:
    """Los `call_id` de las llamadas y los de las salidas que hay en lo que volvió."""
    llamadas = {i.get("call_id") for i in items if i.get("type") == "function_call"}
    salidas = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
    return llamadas, salidas


def test_el_recorte_no_deja_una_salida_de_tool_sin_su_llamada(esquema):
    """El borde que más va a doler, y que no aparecería en desarrollo: aparecería en la
    conversación número siete de un paciente real.

    `limit=1` sobre `[user, assistant, LLAMADA, SALIDA]` devuelve, sin recorte propio,
    exactamente `[function_call_output]`: la salida sola, sin la llamada que la explica. La
    API de OpenAI rechaza esa petición igual que rechaza la contraria.

    `_sin_options` y no `split("?options=")`: la URL de Neon ya trae query, así que el
    parámetro entró con `&` y ese corte no cortaba nada -- la sesión viajaba con
    `search_path`, y el aislamiento dejaba de sostenerlo `schema_translate_map` solo. Ver el
    docstring de `_sin_options`.
    """
    base = _sin_options(esquema)
    sesion = persistencia.sesion_de_agente(
        "conv-borde", database_url=base, esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "hola"},
                {"role": "assistant", "content": "hola, ¿en qué te ayudo?"},
                _LLAMADA,
                _SALIDA,
            ]
        )
        # limit=1 se lleva SOLO la salida: la llamada queda por delante de la ventana.
        return await sesion.get_items(limit=1)

    try:
        items = _correr(correr())
    finally:
        _correr(persistencia.cerrar_engines())

    llamadas, salidas = _pares(items)

    assert salidas <= llamadas, (
        f"el recorte dejó una salida de tool sin su llamada: {salidas - llamadas}. "
        "La API de OpenAI rechaza esa petición."
    )


def test_un_corte_impar_tampoco_deja_la_salida_huerfana(esquema):
    """El mismo borde con el par en medio y no al final: `limit=2` sobre
    `[user, LLAMADA, SALIDA, assistant]` devuelve `[function_call_output, assistant]`.

    La segunda aserción es la otra mitad del encargo: el recorte tiene que quitar la salida
    huérfana y NADA MÁS. Descartar el turno entero por arrastre sería tirar contexto que sí
    cabía, y el paciente lo notaría como una Daniela que se olvida de lo que acaba de decir.
    """
    base = _sin_options(esquema)
    sesion = persistencia.sesion_de_agente(
        "conv-borde-impar", database_url=base, esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "hola"},
                _LLAMADA,
                _SALIDA,
                {"role": "assistant", "content": "tengo las 10, 11 y 2"},
            ]
        )
        return await sesion.get_items(limit=2)

    try:
        items = _correr(correr())
    finally:
        _correr(persistencia.cerrar_engines())

    llamadas, salidas = _pares(items)

    assert salidas <= llamadas, (
        f"el recorte dejó una salida de tool sin su llamada: {salidas - llamadas}"
    )
    assert [i.get("content") for i in items if i.get("role") == "assistant"] == [
        "tengo las 10, 11 y 2"
    ], "el recorte se llevó por delante contexto que sí cabía en la ventana"


def test_con_dos_tools_en_paralelo_solo_cae_la_salida_que_perdio_su_llamada(esquema):
    """La única forma de que la salida huérfana NO sea el primer item de la ventana.

    Con llamadas en paralelo el modelo emite `[LLAMADA_A, LLAMADA_B, SALIDA_A, SALIDA_B]`,
    y un «últimos N» puede partir por el medio: con `limit=4` la ventana empieza en
    `LLAMADA_B`, así que `SALIDA_A` queda huérfana con algo DELANTE que sí hay que conservar.

    En los otros dos escenarios la huérfana es el primer item, y ahí un recorte que además
    tirase todo lo ya acumulado pasaría inadvertido -- se comprobó rompiéndolo a propósito.
    Esta prueba es la que lo caza, y a la vez es el caso real: Daniela consulta
    disponibilidad y precio en el mismo turno.
    """
    llamada_b = {
        "type": "function_call",
        "call_id": "call_def",
        "name": "consultar_precio",
        "arguments": '{"tratamiento":"valoracion"}',
    }
    salida_b = {
        "type": "function_call_output",
        "call_id": "call_def",
        "output": "SIN DATO DOCUMENTADO",
    }

    base = _sin_options(esquema)
    sesion = persistencia.sesion_de_agente(
        "conv-borde-paralelo", database_url=base, esquema=ESQUEMA
    )

    async def correr():
        await sesion.add_items(
            [
                {"role": "user", "content": "hola"},
                _LLAMADA,
                llamada_b,
                _SALIDA,
                salida_b,
                {"role": "assistant", "content": "tengo las 10, 11 y 2"},
            ]
        )
        # La ventana empieza en LLAMADA_B: `_SALIDA` pierde la suya, `salida_b` conserva la
        # suya, y delante de la huérfana queda una llamada que NO se puede tirar.
        return await sesion.get_items(limit=4)

    try:
        items = _correr(correr())
    finally:
        _correr(persistencia.cerrar_engines())

    llamadas, salidas = _pares(items)

    assert salidas <= llamadas, (
        f"el recorte dejó una salida de tool sin su llamada: {salidas - llamadas}"
    )
    assert [i.get("type") or i.get("role") for i in items] == [
        "function_call",
        "function_call_output",
        "assistant",
    ], "el recorte tenía que quitar la salida huérfana y nada más"

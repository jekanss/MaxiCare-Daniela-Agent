"""Acceso a Neon Postgres: conexión, aplicación del esquema y los repositorios que las tools
de `herramientas.py` van a usar.

Por qué existe este módulo y no está todo en `herramientas.py`: una tool es lógica de
negocio, y mezclarle el SQL la vuelve imposible de probar sin una base. Aquí vive el acceso
a datos; allá vive la decisión de qué hacer con ellos.

Este módulo NO importa de `runtime.py` -- no es transporte. Una base de datos es un sistema
de `sistemas.lista[]`, igual que Google Calendar, y da lo mismo si la petición que disparó
la consulta llegó por WhatsApp, por el chat web o por un cron.

------------------------------------------------------------------------------------------
La regla que este módulo hace cumplir
------------------------------------------------------------------------------------------

Cuando no hay dato documentado, la respuesta NO es una cadena vacía ni `None`: es el literal
`SIN DATO DOCUMENTADO` con la instrucción explícita de no estimar y de escalar.

El vacío es una invitación a que el modelo complete el hueco con algo razonable, y "algo
razonable" sobre un precio es exactamente el fallo que rompe el primer objetivo del brief.
La ausencia de dato tiene que ser un dato explícito.

La sección 2.12 del documento maestro dice que endodoncia y prótesis no tienen precio
documentado. La sección 4 lista otros diez puntos pendientes de aprobación de MaxiCare: esos
sí se cargan, pero marcados `aprobado = FALSE`, y se devuelven con una advertencia para que
Daniela no los comunique como compromiso comercial.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("maxicare.persistencia")

# ==========================================================================================
# Los literales que el resto del sistema busca
# ==========================================================================================

#: Lo que devuelve una consulta sin fila. El guardrail `sin_cifra_no_documentada` del bloque
#: 8 se apoya en que esta respuesta no trae ninguna cifra: cualquier número que aparezca en
#: el mensaje al paciente sin respaldo de una consulta exitosa dispara el tripwire.
SIN_DATO = (
    "SIN DATO DOCUMENTADO. MaxiCare no tiene información aprobada sobre {que}. "
    "PROHIBIDO estimar una cifra, extrapolar de un tratamiento parecido o responder de "
    "memoria. Dile al paciente que lo vas a confirmar con los doctores y escala."
)

#: Lo que se antepone a un dato que existe pero que MaxiCare todavía no aprobó (sección 4
#: del documento maestro).
PENDIENTE_APROBACION = (
    "PENDIENTE DE APROBACIÓN POR MAXICARE — no lo comuniques como compromiso comercial. "
    "Puedes mencionarlo como referencia general aclarando que se confirma en la valoración."
)

#: Defaults de la configuración operativa. El valor vigente vive en la tabla `configuracion`
#: de Neon, que la clínica edita desde la interfaz web; estos son el respaldo para cuando la
#: tabla todavía no existe (arranque en frío) y la documentación de qué significa cada uno.
CONFIGURACION_POR_DEFECTO: dict[str, int] = {
    "capacidad_por_hora": 2,
    "duracion_cita_minutos": 60,
    "cierre_relevo_minutos": 180,
    "aviso_relevo_minutos": 120,
}

RAIZ_PROYECTO = Path(__file__).resolve().parents[2]
RUTA_MIGRACIONES = RAIZ_PROYECTO / "migraciones"
RUTA_SEMILLA_CONOCIMIENTO = RAIZ_PROYECTO / "datos" / "base_conocimiento.json"

#: El carril de pruebas del panel. Vive AQUÍ y no en `runtime.py` porque no es transporte:
#: es un esquema de esta base, y hay dos sitios que lo tienen que poner al día -- el chat web
#: (perezosamente, la primera vez que alguien lo abre) y `scripts/inicializar_base.py`, que
#: es la puerta del despliegue. Con la constante duplicada, un renombre dejaría al segundo
#: actualizando un esquema que ya no existe, en silencio. `runtime` la reexporta.
ESQUEMA_PRUEBAS_WEB = "pruebas_web"


def url_directa(database_url: str) -> str:
    """La misma URL sin el `-pooler.` del host.

    PgBouncer rechaza `options` como parámetro de arranque (`unsupported startup parameter
    in options: search_path`), así que todo lo que necesite fijar un `search_path` --que es
    como se aísla el carril de pruebas del panel-- tiene que ir por el host directo.
    """
    return database_url.replace("-pooler.", ".")


def url_con_search_path(database_url: str, esquema: str) -> str:
    """La conexión DIRECTA con el `search_path` fijado a `esquema`.

    El aislamiento que da es FÍSICO, no una convención: con `search_path=pruebas_web`, una
    consulta que diga `INSERT INTO citas` no puede tocar `public.citas` ni queriendo.
    """
    directa = url_directa(database_url)
    separador = "&" if "?" in directa else "?"
    return f"{directa}{separador}options=-csearch_path%3D{esquema}"


# ==========================================================================================
# Lógica pura -- se prueba sin base de datos
# ==========================================================================================


@dataclass(frozen=True)
class FilaConocimiento:
    """Una fila de `base_conocimiento` tal como la devuelve la consulta."""

    tratamiento: str
    concepto: str
    contenido: str
    aprobado: bool
    nota_pendiente: str | None = None


def formatear_conocimiento(
    filas: Sequence[FilaConocimiento], *, tratamiento: str, concepto: str | None = None
) -> str:
    """Convierte el resultado de una consulta en el texto que recibe el modelo.

    Es una función pura a propósito: es la regla más importante del sistema y tiene que
    poder probarse sin levantar una base de datos.

    - Sin filas -> el literal `SIN DATO DOCUMENTADO`, nunca una cadena vacía.
    - Filas no aprobadas -> el contenido, precedido de la advertencia de aprobación.
    - Filas aprobadas -> el contenido tal cual.
    """
    if not filas:
        que = f"«{concepto}» de {tratamiento}" if concepto else f"«{tratamiento}»"
        return SIN_DATO.format(que=que)

    partes: list[str] = []
    for fila in filas:
        encabezado = fila.concepto.replace("_", " ").upper()
        cuerpo = fila.contenido.strip()
        if not fila.aprobado:
            aviso = PENDIENTE_APROBACION
            if fila.nota_pendiente:
                aviso = f"{aviso} Falta por definir: {fila.nota_pendiente}"
            partes.append(f"[{encabezado}] {aviso}\n{cuerpo}")
        else:
            partes.append(f"[{encabezado}] {cuerpo}")
    return "\n\n".join(partes)


def cargar_semilla(ruta: Path | None = None) -> list[dict[str, Any]]:
    """Lee `datos/base_conocimiento.json`, la transcripción del documento maestro."""
    archivo = ruta or RUTA_SEMILLA_CONOCIMIENTO
    datos = json.loads(archivo.read_text(encoding="utf-8"))
    if not isinstance(datos, list):
        raise ValueError(f"{archivo} debe contener una lista de filas")
    return datos


# ==========================================================================================
# Acceso a Neon
# ==========================================================================================


def conectar(database_url: str):
    """Abre una conexión a Postgres. `psycopg` se importa aquí y no arriba para que el resto
    del módulo -- incluida la lógica pura de `formatear_conocimiento` -- se pueda importar y
    probar sin el driver instalado."""
    import psycopg

    return psycopg.connect(database_url)


#: El dialecto asíncrono de este proyecto. `SQLAlchemySession.from_url` llama a
#: `create_async_engine`, que exige un driver async: `postgresql://` a secas falla EN EL
#: CONSTRUCTOR, antes de tocar la red, con `ModuleNotFoundError: No module named 'psycopg2'`
#: -- un paquete que este proyecto no usa, así que el rastro apunta al sitio equivocado.
#:
#: `psycopg` y no `asyncpg`, aunque los dos están instalados y los dos funcionan: la URL de
#: Neon lleva `sslmode=require` y `channel_binding=require` como parámetros de consulta.
#: psycopg 3 los entiende porque son suyos; asyncpg usa otro vocabulario (`ssl=`) y habría
#: que traducirlos a mano. Además psycopg 3 ya es el driver del proyecto: entra un dialecto
#: nuevo, no una librería nueva.
DIALECTO_ASINCRONO = "postgresql+psycopg"


def url_asincrona(url: str) -> str:
    """La misma URL, con el dialecto que `create_async_engine` acepta.

    Idempotente: una URL que ya trae `+driver` se devuelve tal cual, incluso si el driver
    es otro. Quien fije `asyncpg` a propósito en el entorno no se lo encuentra pisado.
    """
    if not url.strip():
        raise ValueError(
            "MAXICARE_DATABASE_URL está vacía: no hay base a la que persistir el historial"
        )
    esquema, separador, resto = url.partition("://")
    if not separador:
        raise ValueError(
            "MAXICARE_DATABASE_URL no parece una URL de base de datos: le falta el «://»"
        )
    if "+" in esquema:
        return url
    return f"{DIALECTO_ASINCRONO}{separador}{resto}"


#: Conexiones que el pool de sesiones mantiene abiertas contra Neon, más las de desbordo.
#:
#: Dimensionado a mano y no heredado del default de SQLAlchemy (5 + 10), que nadie eligió
#: para este proyecto. Los dos datos que lo justifican, medidos el 13/09/2026 contra la Neon
#: de la clínica: 901 (`SHOW max_connections`) y 2 en uso. El otro consumidor,
#: `persistencia.conectar`, NO tiene pool: abre una conexión por llamada y la cierra, así
#: que su pico es el número de turnos concurrentes -- un dígito con un solo worker.
#:
#: La suma tiene que caber debajo del techo CONTANDO EL DOBLE, porque un despliegue solapa
#: brevemente el contenedor viejo y el nuevo. Con 901 de techo y 2 en uso, (3 + 2) * 2 = 10
#: deja un margen enorme; no hizo falta ajustar los valores del brief.
TAMANO_POOL_SESIONES = 3
DESBORDO_POOL_SESIONES = 2

#: Un engine por (base, esquema). Se comparte entre todas las conversaciones: lo caro es el
#: pool, no la `SQLAlchemySession`, que es un objeto con dos tablas y un factory.
_engines: dict[tuple[str, str | None], Any] = {}


def _engine_de(database_url: str, esquema: str | None):
    """El `AsyncEngine` que respalda una sesión, uno por `(database_url, esquema)`.

    Construir un `AsyncEngine` por turno abriría un pool de conexiones por turno, y Neon
    tiene un techo. Se cachea aquí y `cerrar_engines` es lo único que lo vacía -- ni esta
    función ni `sesion_de_agente` disponen nada por su cuenta.

    `esquema` va por `schema_translate_map` en las opciones de EJECUCIÓN, no por `options=
    -csearch_path=` en la URL: el pooler de Neon rechaza `options` como parámetro de
    arranque, y SQLAlchemy cualifica las sentencias al compilarlas, no al abrir la conexión.
    Por eso la clave de caché lleva el esquema: `public` y `pruebas_web` no pueden compartir
    engine, o compartirían pool y las filas de una prueba podrían acabar mezcladas con las
    de la clínica real bajo carga.

    ------------------------------------------------------------------------------------
    Por qué NO lleva `connect_args={"prepare_threshold": None}`
    ------------------------------------------------------------------------------------

    Este es el primer sitio del proyecto donde una conexión a Neon se REUSA --`conectar`
    abre y cierra una por llamada-- y con el reuso aparece un riesgo que hasta ahora no
    podía darse: psycopg 3 auto-prepara una sentencia tras 5 ejecuciones en la misma
    conexión (`prepare_threshold=5`, comprobado en el driver), y un pooler en modo
    transacción es históricamente hostil a las prepared statements. Si el de Neon no las
    soportara, el historial reventaría en producción y no en las pruebas, que hasta hoy
    iban todas por el host directo.

    Se midió el 13/09/2026 contra el pooler real antes de tocar nada, y lo soporta: sobre
    una conexión del pool se creó una prepared statement (`pg_prepared_statements` = 1) y
    se reusó sin error. Desactivar el auto-prepare habría sido pagar un coste por un
    problema que esta base no tiene. Lo vigila
    `test_sesion_neon.py::test_la_sesion_funciona_contra_el_pooler`, que es el único sitio
    del repositorio que ejercita el camino de producción.
    """
    clave = (database_url, esquema)
    engine = _engines.get(clave)
    if engine is not None:
        return engine

    from sqlalchemy.ext.asyncio import create_async_engine

    opciones = {"schema_translate_map": {None: esquema}} if esquema else {}
    engine = create_async_engine(
        url_asincrona(database_url),
        execution_options=opciones,
        pool_size=TAMANO_POOL_SESIONES,
        max_overflow=DESBORDO_POOL_SESIONES,
        # Neon cierra las conexiones ociosas por su cuenta. Sin `pre_ping`, la primera
        # consulta después de un rato tranquilo revienta con una conexión muerta -- y sería
        # el primer paciente de la mañana quien se lo encontrara.
        pool_pre_ping=True,
        pool_recycle=300,
    )
    _engines[clave] = engine
    return engine


async def cerrar_engines() -> None:
    """Cierra y olvida todos los pools. Lo llaman el apagado del servidor y las pruebas.

    En las pruebas hace falta de verdad: un `AsyncEngine` queda atado al bucle de eventos
    en el que se usó por primera vez, y reusarlo desde otro `asyncio.run` da un
    `got Future attached to a different loop` que no se lee como lo que es.

    ------------------------------------------------------------------------------------
    Por qué se vacía la caché ANTES de disponer, y por qué cada `dispose` va en su `try`
    ------------------------------------------------------------------------------------

    Con el bucle delante y el `clear()` detrás, un `dispose()` que lanzara dejaba el peor
    estado posible: los engines siguientes sin cerrar Y el diccionario lleno de engines a
    medio disponer -- exactamente lo que esta función existe para impedir. La siguiente
    `sesion_de_agente` los encontraría en la caché y escribiría contra ellos.

    Vaciar primero cierra además una segunda rendija: un engine creado por otra corrutina
    durante uno de los `await` ya no se pierde del diccionario sin disponerse, porque lo que
    se dispone es la lista copiada y lo que quede en `_engines` después es de quien lo puso.

    El `try` por engine es lo que hace que un pool roto no se lleve por delante a los demás.
    """
    engines = list(_engines.values())
    _engines.clear()
    for engine in engines:
        try:
            await engine.dispose()
        except Exception:
            # Cerrar el resto importa más que este fallo, y el apagado del servidor no
            # puede reventar por un pool que ya estaba roto.
            log.exception("cerrar_engines: no se pudo disponer un engine")


def sin_salidas_huerfanas(items: list[Any]) -> list[Any]:
    """Los mismos items, sin ningún `function_call_output` que se haya quedado sin su
    `function_call` delante.

    ------------------------------------------------------------------------------------
    Qué rompe esto, y por qué en ESTA dirección y no en la contraria
    ------------------------------------------------------------------------------------

    `SQLAlchemySession.get_items(limit=n)` de la 0.22.2 --leído en el SDK instalado-- es
    `ORDER BY created_at DESC, id DESC LIMIT n`, invertido después, más `items[-n:]`. No
    mira los `call_id` en ninguna parte; su propio docstring promete solo «the latest N
    items in chronological order». Es un «últimos N» puro.

    Un «últimos N» no puede dejar una LLAMADA huérfana: si el `function_call` entra en la
    ventana, su `function_call_output` --escrito después, con `id` mayor-- entra siempre.
    Lo que sí deja es lo contrario, y se midió:

        [ user | assistant | LLAMADA | SALIDA ]
                                     ^-- limit=1 devuelve SOLO la salida

    Un `function_call_output` sin su llamada es una petición que la API de OpenAI rechaza,
    igual que la contraria. Y no pasaría en desarrollo: pasaría en la conversación número
    siete de un paciente real, el día que la Tarea 13 fije el límite.

    Se descarta la salida y NADA MÁS: lo demás de la ventana es contexto que sí cabía. Un
    `function_call` sin salida no se filtra porque el recorte no lo produce; si algún día
    aparece uno, será porque la corrida se cortó entre la llamada y su resultado, que es
    otro fallo y merece verse en vez de taparse aquí.
    """
    vistas: set[Any] = set()
    limpios: list[Any] = []
    for item in items:
        tipo = item.get("type") if isinstance(item, dict) else None
        if tipo == "function_call":
            vistas.add(item.get("call_id"))
        elif tipo == "function_call_output" and item.get("call_id") not in vistas:
            log.debug(
                "recorte del historial: se descarta la salida huérfana de %s",
                item.get("call_id"),
            )
            continue
        limpios.append(item)
    return limpios


#: La subclase de `SQLAlchemySession` con el recorte seguro, construida una sola vez. Ver
#: `_clase_con_corte_seguro`.
_clase_sesion: Any = None


def _clase_con_corte_seguro():
    """`SQLAlchemySession` + `sin_salidas_huerfanas` en su `get_items`.

    Se define aquí dentro y no a nivel de módulo porque heredar de `SQLAlchemySession`
    obliga a importarla, y ese import arrastra el SDK entero -- `persistencia` se importa
    también desde sitios que no lo necesitan, que es la misma razón por la que el import de
    `sesion_de_agente` es perezoso. Y se cachea porque definir la clase en cada turno
    crearía un tipo nuevo por turno.
    """
    global _clase_sesion
    if _clase_sesion is not None:
        return _clase_sesion

    from agents.extensions.memory import SQLAlchemySession

    class _SesionConCorteSeguro(SQLAlchemySession):  # type: ignore[misc]
        """El historial de Neon, garantizando que lo que sale es aceptable para la API.

        El recorte por cantidad de items del SDK no sabe de pares `call_id`: puede dejar un
        `function_call_output` cuya llamada se cayó por delante de la ventana. Ver
        `sin_salidas_huerfanas`.
        """

        async def get_items(self, limit: int | None = None) -> list[Any]:
            return sin_salidas_huerfanas(await super().get_items(limit))

    _clase_sesion = _SesionConCorteSeguro
    return _clase_sesion


def sesion_de_agente(
    id_conversacion: str,
    *,
    database_url: str,
    esquema: str | None = None,
    limite: int | None = -1,
):
    """El historial de una conversación, guardado en Neon y no en la memoria del proceso.

    `session_id = id_conversacion` a propósito: es la unidad que sobrevive a un reinicio y
    la que `/clearstate` borra. Un `session_id` por teléfono ataría para siempre a una
    persona con todo lo que dijo alguna vez, y resetear a primer contacto dejaría de ser
    posible sin perder el historial entero.

    `esquema` existe para los carriles de prueba (`pruebas`, `pruebas_web`, `pruebas_sesion`
    y `pruebas_persistencia`, que estrenó el entregable de la fase 7). Va por
    `schema_translate_map` y NO por `search_path`: el pooler de
    Neon rechaza `options` como parámetro de arranque, y eso ya costó una tarde en la fase 3.
    SQLAlchemy cualifica las sentencias al COMPILARLAS, así que el pooler no ve nada raro.

    `limite` recorta el historial que se le manda al modelo, contando ITEMS y no mensajes
    -- una llamada a tool y su resultado son dos. Por omisión sale de
    `config.LIMITE_HISTORIAL_SESION`, que hoy es un TOPE DE SEGURIDAD derivado del techo de
    tokens de la cuenta: acota el crecimiento para que una conversación larga no acabe en un
    `context_length_exceeded` del que el paciente no sale. NO es todavía el límite medido de
    la 13b, que será más pequeño porque optimiza coste y no seguridad. La aritmética de los
    dos está junto a la constante.

    El centinela del parámetro es `-1` y NO `None`, aunque `None` sea lo que parecería
    natural: `None` es un valor legítimo --«sin límite»-- y las pruebas del borde necesitan
    poder pedirlo aunque `config` traiga un número. Con `None` como centinela, «no recortes»
    y «usa el default» serían la misma llamada.

    El import va DENTRO por la misma razón que el del SDK: `config` se lee en el momento de
    construir la sesión y no al importar el módulo, así que una prueba puede sustituir la
    constante y el cambio llega hasta aquí. Con el import arriba, el valor quedaría pegado
    al primer import del proceso y el cableado dejaría de ser cableado.

    Lo que vuelve NO es una `SQLAlchemySession` pelada: es la subclase que filtra la salida
    de tool cuya llamada se cayó por delante de la ventana, porque el recorte del SDK no
    sabe de pares `call_id` y esa petición la rechaza la API. Ver `sin_salidas_huerfanas`.
    """
    from agents.memory.session_settings import SessionSettings

    from .config import LIMITE_HISTORIAL_SESION

    efectivo = LIMITE_HISTORIAL_SESION if limite == -1 else limite

    return _clase_con_corte_seguro()(
        id_conversacion,
        engine=_engine_de(database_url, esquema),
        create_tables=False,
        session_settings=SessionSettings(limit=efectivo),
        # Sin esto los acentos quedan escapados (`ó`) en `message_data`. El ida y vuelta
        # es correcto igual; lo que se pierde es poder leer un historial a ojo el día que
        # haga falta mirarlo, y ese día no se avisa con antelación.
        ensure_ascii=False,
    )


def aplicar_esquema(conn, ruta: Path | None = None) -> list[str]:
    """Aplica todas las migraciones en orden de nombre. Idempotente: el SQL usa
    `CREATE TABLE IF NOT EXISTS` y `ON CONFLICT DO NOTHING`."""
    carpeta = ruta or RUTA_MIGRACIONES
    aplicadas: list[str] = []
    for archivo in sorted(carpeta.glob("*.sql")):
        with conn.cursor() as cur:
            cur.execute(archivo.read_text(encoding="utf-8"))
        aplicadas.append(archivo.name)
    conn.commit()
    return aplicadas


def cargar_base_conocimiento(conn, filas: Iterable[dict[str, Any]]) -> int:
    """Inserta o actualiza la base de conocimiento. Idempotente por (tratamiento, concepto):
    volver a correrlo con el documento actualizado pisa el contenido viejo en vez de
    duplicarlo."""
    total = 0
    with conn.cursor() as cur:
        for fila in filas:
            cur.execute(
                """
                INSERT INTO base_conocimiento
                    (tratamiento, concepto, contenido, aprobado, nota_pendiente)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tratamiento, concepto) DO UPDATE SET
                    contenido      = EXCLUDED.contenido,
                    aprobado       = EXCLUDED.aprobado,
                    nota_pendiente = EXCLUDED.nota_pendiente,
                    actualizado_en = now()
                """,
                (
                    fila["tratamiento"],
                    fila["concepto"],
                    fila["contenido"],
                    fila.get("aprobado", True),
                    fila.get("nota_pendiente"),
                ),
            )
            total += 1
    conn.commit()
    return total


def leer_conocimiento(
    conn, tratamiento: str, concepto: str | None = None
) -> list[FilaConocimiento]:
    """Devuelve las filas crudas. El formateo -- y la regla de `SIN DATO DOCUMENTADO` -- vive
    en `formatear_conocimiento`, que es pura."""
    sql = (
        "SELECT tratamiento, concepto, contenido, aprobado, nota_pendiente "
        "FROM base_conocimiento WHERE tratamiento = %s"
    )
    params: list[Any] = [tratamiento]
    if concepto:
        sql += " AND concepto = %s"
        params.append(concepto)
    sql += " ORDER BY concepto"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [FilaConocimiento(*fila) for fila in cur.fetchall()]


def consultar_conocimiento(conn, tratamiento: str, concepto: str | None = None) -> str:
    """Lo que la tool `consultar_base_conocimiento` va a envolver en la fase 3."""
    filas = leer_conocimiento(conn, tratamiento, concepto)
    return formatear_conocimiento(filas, tratamiento=tratamiento, concepto=concepto)


def leer_configuracion(conn) -> dict[str, int]:
    """La configuración operativa vigente, con los defaults como respaldo."""
    valores = dict(CONFIGURACION_POR_DEFECTO)
    with conn.cursor() as cur:
        cur.execute("SELECT clave, valor FROM configuracion")
        for clave, valor in cur.fetchall():
            try:
                valores[clave] = int(valor)
            except (TypeError, ValueError):
                continue
    return valores


# ==========================================================================================
# Pacientes y conversaciones
# ==========================================================================================


def buscar_paciente_por_telefono(conn, telefono: str) -> tuple[int, str] | None:
    """`(id, nombre_completo)` del paciente registrado con ese número, o `None`.

    El número de WhatsApp es el identificador débil del que habla `contexto.identidad_
    solicitante`: si ya está en Neon, la identidad queda verificada sin fricción, y ese es
    el caso común.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, nombre_completo FROM pacientes WHERE telefono = %s", (telefono,)
        )
        fila = cur.fetchone()
    return (fila[0], fila[1]) if fila else None


def asegurar_paciente(conn, *, nombre_completo: str, telefono: str) -> int:
    """Devuelve el id del paciente, creándolo si el teléfono no estaba.

    No actualiza el nombre de uno que ya existe: si el número de la casa lo usan dos
    personas, pisarlo haría que el historial del primero apareciera bajo el nombre del
    segundo. Resolver eso es trabajo de un humano, no de un UPDATE silencioso.
    """
    existente = buscar_paciente_por_telefono(conn, telefono)
    if existente:
        return existente[0]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pacientes (nombre_completo, telefono) VALUES (%s, %s)
            ON CONFLICT (telefono) DO NOTHING
            RETURNING id
            """,
            (nombre_completo, telefono),
        )
        fila = cur.fetchone()
        if fila is None:
            # Otra conexión lo insertó entre el SELECT y el INSERT.
            cur.execute("SELECT id FROM pacientes WHERE telefono = %s", (telefono,))
            fila = cur.fetchone()
    conn.commit()
    return fila[0]


def tema_del_paciente(conn, telefono: str) -> int | None:
    """El tema de Telegram de ese número, o `None` si todavía no tiene.

    El tema se ata al PACIENTE, no a la conversación: una persona puede tener varios
    episodios a lo largo del tiempo y todos comparten hilo. Lo dice la migración 002.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_topic_id FROM pacientes WHERE telefono = %s",
            (telefono,),
        )
        fila = cur.fetchone()
    return fila[0] if fila else None


def guardar_tema(conn, *, id_paciente: int, topic_id: int, abierto: bool = False) -> None:
    """Ata el tema al paciente. El tema nace cerrado, y por eso `abierto` es FALSE por
    defecto: el relevo (6C) es quien lo abre. El parámetro existe para el caso en que
    Telegram no dejó cerrarlo -- ahí la base tiene que decir la verdad («quedó abierto»),
    no la intención con la que se creó."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pacientes
               SET telegram_topic_id = %s, telegram_topic_abierto = %s
             WHERE id = %s
            """,
            (topic_id, abierto, id_paciente),
        )
    conn.commit()


def asegurar_conversacion(
    conn, *, telefono: str, paciente_id: int | None = None, canal: str = "whatsapp"
) -> str:
    """Crea una conversación y devuelve su UUID como texto.

    El id lo genera el código, no la base, porque el orquestador lo necesita para construir
    claves de idempotencia antes de haber escrito nada.
    """
    id_conversacion = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversaciones (id, paciente_id, telefono, canal)
            VALUES (%s, %s, %s, %s)
            """,
            (id_conversacion, paciente_id, telefono, canal),
        )
    conn.commit()
    return id_conversacion


def conversacion_viva(
    conn, telefono: str, *, ventana_horas: int = 24
) -> tuple[str, int, bool, int] | None:
    """La conversación reciente de ese teléfono:
    (id_conversacion, turno_actual, identidad_verificada, intentos_identificacion).
    `None` si no hay ninguna dentro de la ventana.

    `asegurar_conversacion` engaña con el nombre: SIEMPRE inserta una fila nueva, y no es un
    get-or-create. En el chat web da igual, porque el id se guarda en un dict en memoria,
    pero en WhatsApp -- usada tal cual -- abriría una conversación por cada mensaje: Daniela
    no recordaría la frase anterior, `turno_actual` sería siempre 1, y las claves de
    idempotencia (`id_conversacion + turno`) nunca colisionarían con nada, con lo que
    dejarían de proteger. Esta función es el lookup que falta -- get-or-none, nunca
    get-or-create: decidir si toca crear una fila nueva es trabajo de quien llama.

    -----------------------------------------------------------------------------------
    Por qué el ORDER BY lleva `id DESC` además de `actualizada_en DESC`
    -----------------------------------------------------------------------------------
    `now()` en Postgres es la hora de la TRANSACCIÓN, no la del statement: dos
    conversaciones creadas dentro de la misma transacción -- dos mensajes que llegan juntos
    -- comparten el instante exacto de `actualizada_en`. Sin un desempate estable, cuál de
    las dos vuelve primero queda en manos del planificador, y la respuesta cambia de una
    corrida a otra. Ya mordió así en la fase 8 de este proyecto.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, turno_actual, identidad_verificada, intentos_identificacion
              FROM conversaciones
             WHERE telefono = %s
               AND actualizada_en >= now() - (%s * interval '1 hour')
             ORDER BY actualizada_en DESC, id DESC
             LIMIT 1
            """,
            (telefono, ventana_horas),
        )
        fila = cur.fetchone()
    return (fila[0], fila[1], fila[2], fila[3]) if fila else None


def tocar_conversacion(conn, id_conversacion: str, *, turno_actual: int | None = None) -> None:
    """Pone `actualizada_en = now()` y, si se lo dan, guarda el turno en que va la charla.

    Cada turno que Daniela atiende tiene que adelantar la ventana que vigila
    `conversacion_viva`, o una conversación en curso se declararía vieja a mitad de la charla
    y el paciente volvería a empezar de cero -- perdiendo el turno, la identidad ya
    verificada y los intentos de identificación ya gastados.

    -----------------------------------------------------------------------------------
    Por qué `turno_actual` se escribe aquí y no en otro sitio
    -----------------------------------------------------------------------------------

    La columna existía desde la migración 001 y **ninguna sentencia del proyecto la
    actualizaba**: `conversacion.responder` sube el contador en memoria y ahí se queda. Como
    `atencion.py` construye un `ContextoDaniela` nuevo por mensaje --leyendo el turno de
    Neon, que es lo correcto: un reinicio no puede hacer que la conversación empiece de
    cero--, todos los mensajes de una conversación leían `0` y todos eran el turno 1.

    Lo que costaba: las claves de idempotencia se arman con `id_conversacion + turno_actual`.
    Con el turno congelado, el segundo escalamiento de una conversación comparte clave con el
    primero, `insertar_escalamiento` lo descarta como duplicado y **el doctor no se entera**.
    Un paciente que escala por dolor en el mensaje 3 y otra vez, peor, en el mensaje 9, llega
    una sola vez.

    Es opcional --`None` no toca la columna-- porque hay un sitio que solo quiere adelantar
    la ventana sin saber nada del turno, y porque así ninguna llamada existente cambia de
    comportamiento por haber añadido un parámetro.
    """
    with conn.cursor() as cur:
        if turno_actual is None:
            cur.execute(
                "UPDATE conversaciones SET actualizada_en = now() WHERE id = %s",
                (id_conversacion,),
            )
        else:
            cur.execute(
                "UPDATE conversaciones SET actualizada_en = now(), turno_actual = %s "
                "WHERE id = %s",
                (turno_actual, id_conversacion),
            )
    conn.commit()


# ------------------------------------------------------------------------------------------
# `mensajes_entrantes` -- por qué estas tres funciones viven aquí y no en `ingesta.py`
# ------------------------------------------------------------------------------------------
# `ingesta.py` sigue siendo el dueño de `_registrar`, `_marcar_reenviado` y `_marcar_fallo`:
# esas escrituras pasan por el webhook antes de que exista ninguna conversación. Las tres de
# aquí las llama `atencion.py`, después de que Daniela ya respondió -- o falló al intentarlo
# -- y a esa altura ya hay una conversación de por medio. Por eso viven junto al resto del
# acceso a conversaciones, y no junto a la ingesta cruda del webhook.


def ligar_mensaje_a_conversacion(conn, wamid: str, id_conversacion: str) -> None:
    """Apunta el mensaje entrante a la conversación que lo atendió."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mensajes_entrantes SET conversacion_id = %s WHERE wamid = %s",
            (id_conversacion, wamid),
        )
        if cur.rowcount == 0:
            log.warning("ligar_mensaje_a_conversacion: %s no existe en mensajes_entrantes", wamid)
    conn.commit()


def marcar_respondido(conn, wamid: str, *, wamid_respuesta: str) -> None:
    """Deja constancia de que a este mensaje sí se le contestó, y con qué mensaje de salida.

    Sin esto, `mensajes_entrantes` solo cuenta el viaje hacia Telegram: no hay forma de saber
    si al paciente, del otro lado, alguien le respondió alguna vez.

    Limpia `fallo_respuesta` a propósito, igual que `_marcar_reenviado` limpia `fallo` en
    `ingesta.py`: si el primer intento falló por timeout y el reintento sí llegó, el motivo
    viejo no puede quedar pegado para siempre -- o un informe que cuente
    `fallo_respuesta IS NOT NULL` contaría como fallida una respuesta que sí salió.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mensajes_entrantes
               SET respondido_en   = now(),
                   wamid_respuesta = %s,
                   fallo_respuesta = NULL
             WHERE wamid = %s
            """,
            (wamid_respuesta, wamid),
        )
        if cur.rowcount == 0:
            log.warning("marcar_respondido: %s no existe en mensajes_entrantes", wamid)
    conn.commit()


def marcar_fallo_respuesta(conn, wamid: str, *, motivo: str) -> None:
    """Deja el motivo del fallo al responder.

    NO toca `respondido_en` -- se queda en NULL a propósito. Si un fallo marcara respondido,
    la consulta que justifica esta migración entera -- ¿a quién no le contestamos? --
    devolvería vacío justo cuando más importa.

    El motivo se trunca a 2000 caracteres, igual que `_marcar_fallo` en `ingesta.py`: un
    traceback completo de OpenAI no tiene por qué entrar íntegro en la base de la clínica.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mensajes_entrantes SET fallo_respuesta = %s WHERE wamid = %s",
            (motivo[:2000], wamid),
        )
        if cur.rowcount == 0:
            log.warning("marcar_fallo_respuesta: %s no existe en mensajes_entrantes", wamid)
    conn.commit()


def conversacion_tomada(conn, id_conversacion: str) -> str | None:
    """El doctor que tiene el relevo de esa conversación, o `None` si la tiene Daniela.

    `programar_seguimiento` la consulta antes de escribir: mientras un doctor está hablando,
    el sistema no programa recordatorios por su cuenta.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tomada_por FROM conversaciones WHERE id = %s", (id_conversacion,)
        )
        fila = cur.fetchone()
    return fila[0] if fila and fila[0] else None


def marcar_intento_identificacion(conn, id_conversacion: str, *, verificada: bool) -> int:
    """Suma un intento y, si acertó, deja la identidad verificada. Devuelve los intentos."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET intentos_identificacion = intentos_identificacion + 1,
                   identidad_verificada    = identidad_verificada OR %s,
                   actualizada_en          = now()
             WHERE id = %s
            RETURNING intentos_identificacion
            """,
            (verificada, id_conversacion),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0] if fila else 0


# ==========================================================================================
# Cupos -- el control de capacidad
# ==========================================================================================


def tomar_cupo(
    conn,
    *,
    inicio: datetime,
    capacidad: int,
    clave_idempotencia: str,
    conversacion_id: str | None = None,
) -> tuple[int, int] | None:
    """Aparta uno de los cupos de ese horario. Devuelve `(reserva_id, cupo_num)` o `None`.

    `None` significa «ese horario está lleno», y es una respuesta normal, no un error.

    ------------------------------------------------------------------------------------
    Por qué no hay un `SELECT count(*)` en ninguna parte
    ------------------------------------------------------------------------------------

    La forma intuitiva sería contar cuántas reservas hay y comparar con la capacidad. Está
    mal, y de una manera que no se nota en pruebas manuales: entre el `count` y el `INSERT`
    cabe otra transacción haciendo exactamente lo mismo. Dos pacientes preguntan a la vez,
    los dos ven un cupo libre, los dos reservan, y la clínica descubre el choque cuando
    ambos están en la sala de espera.

    Lo que hace esto es intentar tomar cada cupo por número --1, 2, ... hasta la capacidad--
    y dejar que `uq_reservas_cupo UNIQUE (inicio, cupo_num)` decida. Esa decisión la toma
    Postgres, de forma atómica, y no hay ventana donde colarse. `ON CONFLICT DO NOTHING`
    sin objetivo cubre las DOS restricciones únicas de la tabla (el cupo y la clave), así
    que ninguna colisión llega como excepción y la transacción nunca queda abortada.

    La capacidad viene de `configuracion.capacidad_por_hora`, no de una constante: la
    clínica la cambia desde la interfaz web sin tocar código.
    """
    if capacidad < 1:
        raise ValueError("la capacidad por bloque tiene que ser al menos 1")

    with conn.cursor() as cur:
        # 1. ¿Ya se hizo esta misma reserva? La idempotencia va primero: un reintento del
        #    mismo intento no puede consumir un segundo cupo.
        cur.execute(
            "SELECT id, cupo_num FROM reservas WHERE clave_idempotencia = %s",
            (clave_idempotencia,),
        )
        fila = cur.fetchone()
        if fila:
            return (fila[0], fila[1])

        # 2. Tomar el primer cupo libre.
        for cupo_num in range(1, capacidad + 1):
            cur.execute(
                """
                INSERT INTO reservas (inicio, cupo_num, conversacion_id, clave_idempotencia)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id, cupo_num
                """,
                (inicio, cupo_num, conversacion_id, clave_idempotencia),
            )
            fila = cur.fetchone()
            if fila:
                conn.commit()
                return (fila[0], fila[1])

        # 3. Ningún cupo entró. Puede ser que el horario esté lleno, o que una conexión
        #    gemela con la misma clave se nos haya adelantado; lo segundo se distingue
        #    volviendo a mirar por clave.
        cur.execute(
            "SELECT id, cupo_num FROM reservas WHERE clave_idempotencia = %s",
            (clave_idempotencia,),
        )
        fila = cur.fetchone()

    conn.commit()
    return (fila[0], fila[1]) if fila else None


def liberar_cupo(conn, reserva_id: int) -> None:
    """Devuelve el cupo a la disponibilidad.

    Se llama cuando el cupo se tomó pero la cita no llegó a existir --Calendar falló-- y en
    cada reprogramación, con el horario viejo. Un cupo que nadie libera es un horario que la
    clínica pierde sin que nadie sepa por qué.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reservas WHERE id = %s", (reserva_id,))
    conn.commit()


def bloques_ocupados(conn, desde: datetime, hasta: datetime) -> dict[datetime, int]:
    """Cuántos cupos hay tomados en cada bloque de la ventana."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT inicio, count(*) FROM reservas
             WHERE inicio >= %s AND inicio < %s
             GROUP BY inicio
            """,
            (desde, hasta),
        )
        return {fila[0]: fila[1] for fila in cur.fetchall()}


# ==========================================================================================
# Citas
# ==========================================================================================


def registrar_cita(
    conn,
    *,
    reserva_id: int,
    conversacion_id: str,
    paciente_id: int | None,
    nombre_completo: str,
    telefono: str,
    tratamiento: str,
    inicio: datetime,
    duracion_minutos: int,
    evento_calendar_id: str | None,
) -> str:
    """Escribe la cita ya confirmada en los dos lados y devuelve su id."""
    id_cita = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO citas (id, reserva_id, conversacion_id, paciente_id, nombre_completo,
                               telefono, tratamiento, inicio, duracion_minutos,
                               evento_calendar_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                id_cita, reserva_id, conversacion_id, paciente_id, nombre_completo,
                telefono, tratamiento, inicio, duracion_minutos, evento_calendar_id,
            ),
        )
    conn.commit()
    return id_cita


def cita_viva_de_reserva(conn, reserva_id: int) -> dict[str, Any] | None:
    """La cita NO cancelada que cuelga de esa reserva, o `None` si la reserva no tiene.

    Es lo que hace idempotente a `crear_cita` de verdad. `tomar_cupo` ya lo era en el cupo
    --un acierto de clave devuelve la reserva que ya existía-- pero la tool seguía adelante
    y creaba OTRO evento en Google y OTRA fila en `citas`. Resultado medido llamando dos
    veces con la misma conversación y el mismo horario: una reserva, **dos eventos en el
    calendario del doctor** y dos citas, las dos confirmadas al paciente con ids distintos.

    Se filtra por `estado <> 'cancelada'` porque una cita cancelada no debe impedir volver a
    agendar: `liberar_cupo` borra la reserva al cancelar, así que en la práctica la siguiente
    reserva es otra -- pero apoyarse en eso sería apoyarse en un detalle de otra función.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, reserva_id, conversacion_id, paciente_id, nombre_completo, telefono,
                   tratamiento, inicio, duracion_minutos, evento_calendar_id, estado
              FROM citas
             WHERE reserva_id = %s AND estado <> 'cancelada'
             ORDER BY creada_en
             LIMIT 1
            """,
            (reserva_id,),
        )
        fila = cur.fetchone()
    if not fila:
        return None
    columnas = (
        "id", "reserva_id", "conversacion_id", "paciente_id", "nombre_completo", "telefono",
        "tratamiento", "inicio", "duracion_minutos", "evento_calendar_id", "estado",
    )
    return dict(zip(columnas, fila))


def leer_cita(conn, id_cita: str) -> dict[str, Any] | None:
    """La cita completa, o `None`. Incluye `paciente_id` para comprobar la pertenencia."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, reserva_id, conversacion_id, paciente_id, nombre_completo, telefono,
                   tratamiento, inicio, duracion_minutos, evento_calendar_id, estado
              FROM citas WHERE id = %s
            """,
            (id_cita,),
        )
        fila = cur.fetchone()
    if not fila:
        return None
    columnas = (
        "id", "reserva_id", "conversacion_id", "paciente_id", "nombre_completo", "telefono",
        "tratamiento", "inicio", "duracion_minutos", "evento_calendar_id", "estado",
    )
    return dict(zip(columnas, fila))


def mover_cita(conn, id_cita: str, *, reserva_id: int, inicio: datetime) -> None:
    """Apunta la cita al cupo nuevo. El cupo viejo lo libera quien llama."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas SET reserva_id = %s, inicio = %s, estado = 'reprogramada',
                             actualizada_en = now()
             WHERE id = %s
            """,
            (reserva_id, inicio, id_cita),
        )
    conn.commit()


def marcar_cita_cancelada(conn, id_cita: str, *, motivo: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas SET estado = 'cancelada', motivo_cancelacion = %s,
                             actualizada_en = now()
             WHERE id = %s
            """,
            (motivo, id_cita),
        )
    conn.commit()


# ==========================================================================================
# Estado, seguimientos y escalamientos
# ==========================================================================================


def upsert_estado_oportunidad(
    conn,
    id_conversacion: str,
    *,
    estado: str,
    barrera: str,
    tratamiento: str | None = None,
    fuera_de_alcance: bool = False,
    notas: str | None = None,
) -> None:
    """Guarda el estado comercial de la conversación. De esta tabla salen seis de las siete
    métricas de `observabilidad.metricas`, así que un fallo aquí se registra aunque no
    detenga la conversación."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO estado_oportunidad
                (conversacion_id, estado, barrera, tratamiento, fuera_de_alcance, notas)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (conversacion_id) DO UPDATE SET
                estado           = EXCLUDED.estado,
                barrera          = EXCLUDED.barrera,
                tratamiento      = COALESCE(EXCLUDED.tratamiento, estado_oportunidad.tratamiento),
                fuera_de_alcance = EXCLUDED.fuera_de_alcance,
                notas            = COALESCE(EXCLUDED.notas, estado_oportunidad.notas),
                actualizado_en   = now()
            """,
            (id_conversacion, estado, barrera, tratamiento, fuera_de_alcance, notas),
        )
    conn.commit()


def insertar_seguimiento(
    conn,
    *,
    id_conversacion: str,
    tipo: str,
    fecha_objetivo: datetime,
    clave_idempotencia: str,
) -> bool:
    """`True` si quedó programado ahora, `False` si ya existía. Nunca duplica."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seguimientos
                (conversacion_id, tipo, fecha_objetivo, clave_idempotencia)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (clave_idempotencia) DO NOTHING
            RETURNING id
            """,
            (id_conversacion, tipo, fecha_objetivo, clave_idempotencia),
        )
        nuevo = cur.fetchone() is not None
    conn.commit()
    return nuevo


def insertar_escalamiento(
    conn,
    *,
    id_conversacion: str,
    motivo: str,
    resumen: str,
    pregunta: str,
    clave_idempotencia: str,
) -> int | None:
    """El id del escalamiento si es nuevo; `None` si ese turno ya había escalado.

    `None` no es un error: es la defensa contra mandarle al doctor la misma alerta tres
    veces. A la cuarta deja de mirarlas, y así es como muere un sistema de escalamiento.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO escalamientos
                (conversacion_id, motivo, resumen, pregunta, clave_idempotencia)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (clave_idempotencia) DO NOTHING
            RETURNING id
            """,
            (id_conversacion, motivo, resumen, pregunta, clave_idempotencia),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0] if fila else None


def escalamiento_pendiente_de_aviso(conn, clave_idempotencia: str) -> int | None:
    """El id del escalamiento con esa clave si se escribió pero NUNCA se avisó; si no, `None`.

    ------------------------------------------------------------------------------------
    La diferencia entre «ya se avisó» y «se intentó avisar»
    ------------------------------------------------------------------------------------

    `insertar_escalamiento` devuelve `None` en los dos casos, porque lo único que mira es si
    la clave ya existe. Y no son lo mismo:

    - La fila existe **y tiene `telegram_message_id`**: el doctor ya recibió su alerta.
      Volver a mandarla es el ruido que hace que a la cuarta deje de mirarlas.
    - La fila existe **y `telegram_message_id` es `NULL`**: alguien llegó a escribir la fila y
      el Telegram no salió --un 5xx, un límite de tasa, un HTML que Telegram rechazó--. Ahí
      no hay ningún aviso que duplicar: hay un aviso que falta.

    Sin esta consulta, la clave se quemaba al INTENTAR y no al CONSEGUIR: cualquier fallo de
    Telegram dejaba la fila escrita, el doctor sin enterarse, y ningún reintento posible.
    Un paciente con dolor quedaba «escalado» en una tabla que nadie mira.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM escalamientos "
            "WHERE clave_idempotencia = %s AND telegram_message_id IS NULL",
            (clave_idempotencia,),
        )
        fila = cur.fetchone()
    return fila[0] if fila else None


def anotar_telegram_en_escalamiento(conn, escalamiento_id: int, message_id: int) -> None:
    """Guarda el mensaje de Telegram para poder editarle el botón cuando alguien lo toque.

    Y, desde la 6A, algo más: es lo que distingue un escalamiento avisado de uno que se
    quedó a medias. Ver `escalamiento_pendiente_de_aviso`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE escalamientos SET telegram_message_id = %s WHERE id = %s",
            (message_id, escalamiento_id),
        )
    conn.commit()


# ==========================================================================================
# Usuarios de la interfaz web (migración 006)
# ==========================================================================================
#
# Este módulo guarda el hash tal como se lo den y lo devuelve tal cual: NO importa
# `autenticacion.py` ni sabe qué es un `scrypt$...`. La comprobación de la contraseña vive
# allá, donde se puede probar sin base de datos, y la de aquí se puede probar sin criptografía.


def buscar_usuario(conn, usuario: str) -> dict[str, Any] | None:
    """El usuario por su nombre de ingreso, activo o no, o `None`.

    Devuelve también los inactivos a propósito. Quien llama necesita distinguir «no existe»
    de «existe pero se le quitó el acceso»: lo primero es un nombre mal escrito, lo segundo
    es alguien que se fue de la clínica y sigue intentando entrar -- y eso merece quedar en
    el log aunque al que lo intenta se le responda lo mismo.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, usuario, nombre, hash_contrasena, rol, activo
            FROM usuarios WHERE usuario = %s
            """,
            (usuario.strip().lower(),),
        )
        fila = cur.fetchone()
    if fila is None:
        return None
    return {
        "id": fila[0],
        "usuario": fila[1],
        "nombre": fila[2],
        "hash_contrasena": fila[3],
        "rol": fila[4],
        "activo": fila[5],
    }


def crear_usuario(
    conn, *, usuario: str, nombre: str, hash_contrasena: str, rol: str = "recepcion"
) -> int:
    """Crea el usuario y devuelve su id. Si el nombre ya existe, ACTUALIZA la contraseña.

    El `ON CONFLICT DO UPDATE` es deliberado y es lo que hace que el script de creación sirva
    también para restablecer una contraseña olvidada, que es la operación que de verdad se va
    a necesitar. Sin él haría falta un segundo script para lo mismo.

    No toca `activo`: restablecerle la contraseña a alguien a quien se le quitó el acceso no
    se lo devuelve. Reactivar es una decisión distinta y se toma aparte.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO usuarios (usuario, nombre, hash_contrasena, rol)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (usuario) DO UPDATE SET
                nombre           = EXCLUDED.nombre,
                hash_contrasena  = EXCLUDED.hash_contrasena,
                rol              = EXCLUDED.rol
            RETURNING id
            """,
            (usuario.strip().lower(), nombre.strip(), hash_contrasena, rol),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0]


def listar_usuarios(conn) -> list[dict[str, Any]]:
    """Todos, con los inactivos al final. Sin el hash: no hace falta para mostrar una lista,
    y lo que no sale de la base no se puede filtrar por accidente a una respuesta HTTP."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, usuario, nombre, rol, activo, ultimo_acceso_en
            FROM usuarios ORDER BY activo DESC, nombre
            """
        )
        filas = cur.fetchall()
    return [
        {
            "id": f[0],
            "usuario": f[1],
            "nombre": f[2],
            "rol": f[3],
            "activo": f[4],
            "ultimo_acceso_en": f[5],
        }
        for f in filas
    ]


def cambiar_acceso(conn, usuario: str, *, activo: bool) -> bool:
    """Da o quita el acceso. True si el usuario existía.

    Quitarlo tiene efecto en la siguiente petición que haga esa persona -- ver el comentario
    de la columna `activo` en la migración 006.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usuarios SET activo = %s WHERE usuario = %s",
            (activo, usuario.strip().lower()),
        )
        toco = cur.rowcount
    conn.commit()
    return toco > 0


def marcar_acceso(conn, usuario: str) -> None:
    """Sella la hora del último ingreso. Nunca lanza hacia arriba: que no se pueda escribir
    una marca de tiempo no puede impedirle entrar a nadie."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usuarios SET ultimo_acceso_en = now() WHERE usuario = %s",
            (usuario.strip().lower(),),
        )
    conn.commit()


# ==========================================================================================
# Borrar todo rastro de un teléfono -- `/clearstate`
#
# Las dos únicas funciones del proyecto que borran filas de un paciente. Viven juntas y
# aquí abajo porque se leen juntas: `rastro_de` dice qué hay que eliminar FUERA de la base
# antes de que `borrar_rastro` haga imposible saberlo.
# ==========================================================================================

#: Las conversaciones de un teléfono, por los DOS caminos que tiene el esquema: la columna
#: `telefono` desnormalizada y el `paciente_id`. Buscar solo por una deja filas atrás --una
#: conversación puede existir sin `paciente_id`, y un paciente puede tener conversaciones
#: cuyo `telefono` alguien normalizó distinto.
_CONVERSACIONES_DEL_TELEFONO = """
    SELECT id FROM conversaciones
     WHERE telefono = %(tel)s
        OR paciente_id IN (SELECT id FROM pacientes WHERE telefono = %(tel)s)
"""


def rastro_de(conn, telefono: str) -> dict:
    """Qué hay que borrar fuera de Postgres: eventos de Calendar y mensajes de Telegram.

    Se lee ANTES de borrar y no después, por una razón que no tiene vuelta atrás: una vez
    borrada la fila de `citas`, su `evento_calendar_id` no existe en ningún sitio y el evento
    se queda en el calendario de la clínica ocupando un hueco que ya nadie puede cancelar
    desde el sistema.

    Las citas canceladas se excluyen: su evento ya se eliminó al cancelarlas, y pedirle a
    Google que borre dos veces el mismo id es pedir un 404 por gusto.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT evento_calendar_id FROM citas
             WHERE (telefono = %(tel)s
                    OR conversacion_id IN ({_CONVERSACIONES_DEL_TELEFONO}))
               AND evento_calendar_id IS NOT NULL
               AND estado <> 'cancelada'
            """,
            {"tel": telefono},
        )
        eventos = [f[0] for f in cur.fetchall()]

        cur.execute(
            "SELECT telegram_topic_id FROM pacientes WHERE telefono = %(tel)s",
            {"tel": telefono},
        )
        fila = cur.fetchone()
        topic_id = fila[0] if fila else None

        cur.execute(
            f"""
            SELECT telegram_message_id FROM mensajes_entrantes
             WHERE telefono = %(tel)s AND telegram_message_id IS NOT NULL
             UNION
            SELECT e.telegram_message_id FROM escalamientos e
             WHERE e.conversacion_id IN ({_CONVERSACIONES_DEL_TELEFONO})
               AND e.telegram_message_id IS NOT NULL
            """,
            {"tel": telefono},
        )
        mensajes = sorted(f[0] for f in cur.fetchall())

    return {"eventos": eventos, "topic_id": topic_id, "mensajes_telegram": mensajes}


def borrar_rastro(conn, telefono: str, *, conservar_wamid: str | None = None) -> dict[str, int]:
    """Borra de la base todo lo que ata un teléfono a este sistema. Devuelve el conteo.

    EL ORDEN NO ES ESTILO: ES LO QUE LA BASE PERMITE. `citas`, `reservas` y
    `mensajes_entrantes` apuntan a `conversaciones` sin `ON DELETE CASCADE`, y
    `conversaciones.paciente_id` apunta a `pacientes` igual de desnudo. Empezar por el
    paciente --que es por donde uno empezaría-- falla con un error de integridad y no borra
    nada. De la hoja a la raíz, y las cuatro tablas con CASCADE (`estado_oportunidad`,
    `notas_archivo`, `seguimientos`, `escalamientos`) se van solas al caer la conversación.

    `agent_sessions` va justo ANTES que `conversaciones`, y por una razón que no tiene
    marcha atrás: `session_id` ES el `id` de esas conversaciones (la migración 010 lo declara
    sin clave foránea a propósito, así que no hay CASCADE que lo salve). Borrar primero
    `conversaciones` deja a la subconsulta sin nada que encontrar, y el historial del diálogo
    -- `agent_sessions` y, por su `ON DELETE CASCADE`, `agent_messages` -- sobrevive huérfano
    e inalcanzable, con la agravante de que el borrado parece haber funcionado.

    `conservar_wamid` deja en pie la fila de `mensajes_entrantes` del propio mensaje que pidió
    el borrado, con su `conversacion_id` en NULL. Sin eso, un reintento del webhook de Meta
    --que reintenta, y por eso existe la deduplicación por `wamid`-- ejecutaría el comando una
    segunda vez. La fila que se queda no vuelve conocido a nadie: `atencion._leer_estado` no
    mira esta tabla.

    Todo va en una transacción. Un borrado a medias es peor que ninguno: dejaría, por
    ejemplo, un paciente sin conversaciones, que es un estado que el resto del código no
    espera ver nunca.
    """
    parametros = {"tel": telefono, "wamid": conservar_wamid}
    borradas: dict[str, int] = {}

    try:
        with conn.cursor() as cur:
            if conservar_wamid is not None:
                # Antes de borrar la conversación a la que apunta, o la clave foránea lo
                # impide. Queda apuntando a nada, que es exactamente lo que es.
                cur.execute(
                    "UPDATE mensajes_entrantes SET conversacion_id = NULL WHERE wamid = %(wamid)s",
                    parametros,
                )

            filtro_wamid = "" if conservar_wamid is None else "AND wamid <> %(wamid)s"
            cur.execute(
                f"""
                DELETE FROM mensajes_entrantes
                 WHERE (telefono = %(tel)s
                        OR conversacion_id IN ({_CONVERSACIONES_DEL_TELEFONO}))
                   {filtro_wamid}
                """,
                parametros,
            )
            borradas["mensajes_entrantes"] = cur.rowcount

            cur.execute(
                f"""
                DELETE FROM citas
                 WHERE telefono = %(tel)s
                    OR conversacion_id IN ({_CONVERSACIONES_DEL_TELEFONO})
                    OR paciente_id IN (SELECT id FROM pacientes WHERE telefono = %(tel)s)
                """,
                parametros,
            )
            borradas["citas"] = cur.rowcount

            cur.execute(
                f"""
                DELETE FROM reservas
                 WHERE conversacion_id IN ({_CONVERSACIONES_DEL_TELEFONO})
                """,
                parametros,
            )
            borradas["reservas"] = cur.rowcount

            # El historial del diálogo, desde la fase 7. Va ANTES de borrar `conversaciones`
            # porque los `session_id` SON los ids de esas conversaciones: después del DELETE
            # no habría forma de saber cuáles eran, y el historial quedaría huérfano y vivo.
            #
            # `agent_messages` no se borra a mano: se va sola por el `ON DELETE CASCADE` de
            # la migración 010. Un segundo DELETE aquí sería un sitio más que mantener.
            cur.execute(
                f"""
                DELETE FROM agent_sessions
                 WHERE session_id IN (
                        SELECT id::text FROM conversaciones
                         WHERE id IN ({_CONVERSACIONES_DEL_TELEFONO})
                 )
                """,
                parametros,
            )
            borradas["agent_sessions"] = cur.rowcount

            cur.execute(
                f"DELETE FROM conversaciones WHERE id IN ({_CONVERSACIONES_DEL_TELEFONO})",
                parametros,
            )
            borradas["conversaciones"] = cur.rowcount

            cur.execute("DELETE FROM pacientes WHERE telefono = %(tel)s", parametros)
            borradas["pacientes"] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return borradas

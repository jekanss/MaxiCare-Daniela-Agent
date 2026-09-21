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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .calendario import ZONA_BOGOTA

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

#: El marcador que ocupa el nombre de una ficha cuando todavía no se sabe. **No es un
#: nombre**, y todo el que lea `pacientes.nombre_completo` tiene que saberlo: una ficha con
#: esto puesto significa «este número existe», nunca «esta persona se llama así».
#:
#: Vive aquí, y no en `relevo.py` donde nació, porque lo consultan tres módulos que no se
#: importan entre sí -- `relevo` al componer el nombre del hilo, `atencion` al armar el turno
#: y `herramientas` al identificar--. Una copia por módulo es exactamente cómo se separan:
#: hasta el 16/09/2026 solo `relevo` sabía qué era esto, y los otros dos lo trataban como el
#: nombre del paciente. El resultado, medido en producción: una paciente que dio su nombre de
#: verdad, no «coincidió» con el marcador, gastó los dos intentos de identificación y acabó
#: escalada en vez de agendada.
NOMBRE_PENDIENTE = "PENDIENTE"

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
    # La jornada de la clínica. Los valores son los del documento aprobado por MaxiCare
    # («Lunes a viernes de 8:00 am a 5:00 pm. Sábados de 8:00 am a 3:00 pm.»), y el domingo
    # cerrado, que ese texto dice por omisión. Sin esto, la rejilla ofrecía la madrugada:
    # ver `calendario.Jornada`.
    #
    # OJO: el horario vive también como TEXTO en la fila `_general`/`horario` de la base de
    # conocimiento, que es la que Daniela recita cuando le preguntan. Los dos se mueven
    # juntos o dice una cosa y ofrece otra.
    "hora_apertura": 8,
    "hora_cierre": 17,
    "hora_cierre_sabado": 15,
    "atiende_domingo": 0,
    # La cola de recordatorios (migración 017). Los defaults viven aquí por lo mismo que los
    # de la jornada: `leer_configuracion` los usa cuando la tabla todavía no existe.
    "hora_recordatorio_vispera": 18,
    "horas_minimas_para_recordar": 4,
    # La reactivacion (migracion 021). Mismo motivo que los de la 017: `leer_configuracion` los
    # usa cuando la tabla todavia no existe.
    "tope_diario_reactivacion": 20,
    "max_reactivaciones_12m": 6,
    "max_seguimientos_fallidos": 2,
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


def nombrar_si_esta_pendiente(
    conn, *, telefono: str, nombre: str, pendiente: str
) -> bool:
    """Le pone nombre al número que todavía no lo tenía. `True` si escribió.

    Crea la ficha si no había —el mismo permiso que `relevo._ficha_para_el_relevo`: lo
    dispara un doctor que acaba de hablar con esa persona— y, si ya había, **solo escribe
    sobre el marcador `pendiente`**. Un nombre de verdad no se pisa nunca, por lo mismo que
    `asegurar_paciente` no lo actualiza: si el número de la casa lo usan dos personas,
    pisarlo haría que el historial del primero apareciera bajo el nombre del segundo.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pacientes (nombre_completo, telefono) VALUES (%s, %s)
            ON CONFLICT (telefono) DO UPDATE
               SET nombre_completo = EXCLUDED.nombre_completo
             WHERE pacientes.nombre_completo = %s
            RETURNING id
            """,
            (nombre, telefono, pendiente),
        )
        escribio = cur.fetchone() is not None
    conn.commit()
    return escribio


def asegurar_paciente(conn, *, nombre_completo: str, telefono: str) -> int:
    """Devuelve el id del paciente, creándolo si el teléfono no estaba.

    No actualiza el nombre de uno que ya existe: si el número de la casa lo usan dos
    personas, pisarlo haría que el historial del primero apareciera bajo el nombre del
    segundo. Resolver eso es trabajo de un humano, no de un UPDATE silencioso.

    **Con UNA excepción, y es la misma regla, no un agujero en ella:** si lo que hay guardado
    es el marcador `NOMBRE_PENDIENTE`, se escribe encima. Eso no pisa el nombre de nadie --el
    marcador no es un nombre-- y es lo que repara las fichas que el relevo dejó abiertas en
    blanco antes del 16/09/2026. Sin esto se quedaban marcadas para siempre: el único camino
    que pisaba el marcador era un relevo que terminara CON cita, y el caso que lo creó es
    justo el del doctor que cierra sin agendar.

    Quien llama aquí siempre trae un nombre de verdad: `_crear_cita` el que le dio el
    paciente, `relevo._agendar` el que escribió el doctor.
    """
    existente = buscar_paciente_por_telefono(conn, telefono)
    if existente and existente[1] == NOMBRE_PENDIENTE:
        nombrar_si_esta_pendiente(
            conn, telefono=telefono, nombre=nombre_completo, pendiente=NOMBRE_PENDIENTE
        )
        return existente[0]
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


# ==========================================================================================
# Los hilos de Telegram -- por TELÉFONO, no por paciente
# ==========================================================================================
#
# Vivían en `pacientes` hasta la migración 014, y ese era el defecto que hacía que un lead se
# quedara sin hilo: `pacientes` es la tabla de la que sale `identidad_verificada`, así que
# `lectura.asegurar_tema` no podía crear la fila sin regalarle una identidad a un desconocido
# --y sin fila no había dónde colgar el tema--. El resultado, medido en producción: sus
# textos no se archivaban en ninguna parte y sus radiografías caían en el General.
#
# Ahora tener hilo y estar verificado son cosas distintas. El teléfono es la clave porque es
# lo único que se conoce del primer archivo de un desconocido, y es además la misma clave por
# la que va la pertenencia de una cita (no negociable 13).


def tema_del_paciente(conn, telefono: str) -> int | None:
    """El tema de Telegram de ese número, o `None` si todavía no tiene.

    El tema se ata al TELÉFONO, no a la conversación ni a la ficha: una persona puede tener
    varios episodios a lo largo del tiempo y todos comparten hilo --eso lo dijo la 002 y sigue
    valiendo-- y además puede no ser paciente todavía, que es lo que arregló la 014.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT topic_id FROM temas_telegram WHERE telefono = %s", (telefono,))
        fila = cur.fetchone()
    return fila[0] if fila else None


def guardar_tema(conn, *, telefono: str, topic_id: int, abierto: bool = False) -> None:
    """Ata el hilo a ese número. Nace CERRADO, y por eso `abierto` es FALSE por defecto: el
    relevo es quien lo abre. El parámetro existe para el caso en que Telegram no dejó cerrarlo
    -- ahí la base tiene que decir la verdad («quedó abierto»), no la intención con la que se
    creó.

    `ON CONFLICT` sobre el teléfono y no un `UPDATE`: quien llama a esto está creando el hilo
    por primera vez, pero dos archivos del mismo número pueden llegar casi a la vez, y el
    candado de `lectura._candados_de_tema` es de proceso -- no protege entre réplicas.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO temas_telegram (telefono, topic_id, abierto) VALUES (%s, %s, %s)
            ON CONFLICT (telefono) DO UPDATE
               SET topic_id = EXCLUDED.topic_id, abierto = EXCLUDED.abierto
            """,
            (telefono, topic_id, abierto),
        )
    conn.commit()


def marcar_tema_abierto(conn, telefono: str, *, abierto: bool) -> None:
    """Anota si el hilo de ese número está abierto en Telegram ahora mismo.

    `guardar_tema` no sirve para esto: pisa el `topic_id`, y lo que cambia al abrir y cerrar
    un relevo es solo el candado. La columna importa porque es lo único que, mirando la base,
    distingue «expediente» de «canal en vivo hacia el WhatsApp de una persona».
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE temas_telegram SET abierto = %s WHERE telefono = %s", (abierto, telefono)
        )
    conn.commit()


def olvidar_tema(conn, telefono: str) -> bool:
    """Borra la fila del hilo de ese número. `True` si había una.

    Se llama cuando se comprueba que el tema YA NO EXISTE en Telegram --alguien lo borró a
    mano--, y sin esto el sistema no se recupera nunca: `temas_telegram` seguiría apuntando a
    un `topic_id` muerto, `lectura.asegurar_tema` lo daría por bueno sin crear ninguno, y cada
    archivo que mandara esa persona fallaría al depositarse. Para siempre, y en silencio.

    Olvidarlo hace que el siguiente archivo le abra un hilo nuevo, que es la recuperación
    correcta. Lo que se pierde es lo que ya se perdió al borrar el tema: el expediente
    anterior. Por eso `relevo.cerrar` avisa en el General de que alguien lo hizo.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM temas_telegram WHERE telefono = %s", (telefono,))
        borradas = cur.rowcount
    conn.commit()
    return borradas > 0


def transcripcion(conn, telefono: str, *, limite: int = 40) -> list[tuple[str, str, Any]]:
    """Lo que se dijeron el paciente y Daniela, en orden. `(quien, texto, cuando)`.

    Es lo que se vuelca en el hilo cuando un doctor toma la conversación, y existe porque el
    primer relevo real lo estrenó un doctor entrando a un tema RECIÉN CREADO y vacío: sus
    archivos estaban en el General y sus textos no se habían archivado en ninguna parte.

    Sale de dos sitios porque las dos mitades viven separadas: lo que escribió el paciente
    está en `mensajes_entrantes`, y lo que contestó Daniela solo existe dentro del historial
    del SDK (`agent_messages`). Juntarlas por hora es la única forma de que se lea como una
    conversación y no como dos listas.

    **No cruza el muro**: todo esto es contenido que el paciente ya vio en su WhatsApp, y va
    hacia el doctor, que es la dirección permitida. Lo que nunca puede viajar al revés es lo
    clínico -- eso lo decide `relevo.cerrar`, no esta función.

    `limite` corta por arriba: un hilo de dos meses no cabe en un mensaje de Telegram, y lo
    que el doctor necesita para entrar en contexto es el final, no el principio.
    """
    parametros = {"tel": telefono}
    lineas: list[tuple[str, str, Any]] = []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT texto, recibido_en FROM mensajes_entrantes
             WHERE telefono = %(tel)s AND texto IS NOT NULL AND texto <> ''
            """,
            parametros,
        )
        lineas += [("paciente", f[0], f[1]) for f in cur.fetchall()]

        # `created_at` es TIMESTAMP **sin zona** --lo fija el SDK, no nosotros: no negociable
        # 10-- mientras todo lo demás del esquema es TIMESTAMPTZ. Sin el `AT TIME ZONE`, las
        # dos mitades no se pueden ordenar juntas: Python no compara un datetime con zona
        # contra uno sin ella, revienta con TypeError.
        cur.execute(
            """
            SELECT m.message_data, m.created_at AT TIME ZONE 'UTC'
              FROM agent_messages m
             WHERE m.session_id IN (
                    SELECT id::text FROM conversaciones WHERE telefono = %(tel)s
             )
            """,
            parametros,
        )
        for crudo, cuando in cur.fetchall():
            texto = _texto_de_daniela(crudo)
            if texto:
                lineas.append(("daniela", texto, cuando))

    lineas.sort(key=lambda l: l[2])
    # El corte va por el FINAL: lo que el doctor necesita para entrar en contexto es lo
    # último que se dijeron, no cómo empezó todo hace dos meses.
    return lineas[-limite:]


def _texto_de_daniela(crudo: str) -> str | None:
    """Saca la frase de un item del historial del SDK, o `None` si ese item no es una frase.

    **Un item NO es un mensaje** (lo dice la migración 010): una llamada a tool y su
    resultado son dos filas más. Volcar el JSON tal cual en el hilo del paciente le pondría
    al doctor delante los argumentos de `consultar_disponibilidad` en vez de una
    conversación, así que aquí se queda solo lo que Daniela dijo en voz alta.

    Degrada a `None` ante cualquier forma que no reconozca --el SDK puede cambiar el formato
    en una versión-- porque una transcripción incompleta sigue siendo útil y una excepción
    dejaría al doctor sin ninguna.
    """
    try:
        item = json.loads(crudo)
    except Exception:  # noqa: BLE001 -- ver docstring
        return None
    if not isinstance(item, dict) or item.get("role") != "assistant":
        return None

    contenido = item.get("content")
    if isinstance(contenido, str):
        return _solo_la_frase(contenido)
    if isinstance(contenido, list):
        trozos = [
            t.get("text", "")
            for t in contenido
            if isinstance(t, dict) and isinstance(t.get("text"), str)
        ]
        return _solo_la_frase(" ".join(p for p in trozos if p))
    return None


def _solo_la_frase(contenido: str) -> str | None:
    """Lo que Daniela DIJO, sin los cinco campos con los que ramifica el orquestador.

    Hace falta un segundo desempaquetado porque `daniela` tiene `output_type`: lo que el SDK
    guarda como contenido del item no es su frase, es el JSON entero de `RespuestaDaniela`.
    Sin esto, el doctor que tomaba la conversación recibía en el hilo la transcripción así
    --medido el 14/09/2026, en el segundo relevo real--:

        {"mensaje_al_paciente":"Claro que sí. Ya nos llegó el archivo y el doctor lo va a
        revisar...","estado_oportunidad":"explorando","barrera_detectada":"ninguna",
        "requiere_escalamiento":true,"motivo_escalamiento":"archivo_recibido", ...}

    Ilegible, y encima con la telemetría comercial del sistema delante de quien solo quiere
    saber qué le dijeron a su paciente. Los otros cinco campos no son secretos --el doctor
    puede verlos en el panel-- pero aquí son ruido que tapa la única línea que importa.

    Si el contenido no es ese JSON --un guardrail que respondió en texto plano, una versión
    del SDK que cambie el formato-- se devuelve tal cual: una frase de más es mejor que una
    transcripción con huecos.
    """
    limpio = (contenido or "").strip()
    if not limpio:
        return None
    try:
        respuesta = json.loads(limpio)
    except Exception:  # noqa: BLE001 -- no era JSON; es texto plano y vale tal cual
        return limpio
    if isinstance(respuesta, dict):
        frase = respuesta.get("mensaje_al_paciente")
        if isinstance(frase, str) and frase.strip():
            return frase.strip()
        # Un dict que no es una `RespuestaDaniela`: volcar su JSON sería repetir el bug.
        return None
    return limpio


def telefono_de_conversacion(conn, id_conversacion: str) -> str | None:
    """El número de esa conversación. Lo que el `callback_data` del botón no puede llevar.

    El botón viaja con el id de la conversación --y no con el teléfono-- a propósito: el
    `callback_data` de Telegram es visible para cualquiera del grupo y cabe en 64 bytes. Un
    UUID no le dice nada a nadie; un teléfono, sí.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT telefono FROM conversaciones WHERE id = %s", (id_conversacion,))
        fila = cur.fetchone()
    return fila[0] if fila else None


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


#: Columnas que hacen falta para reconstruir un `ingesta.MensajeEntrante` y volver a
#: atenderlo. `nombre_archivo` no está en la tabla y no se echa de menos: cuando se
#: reintenta, el archivo YA llegó al doctor --eso lo hizo `procesar_mensaje` antes de morir--
#: y lo que falta es la respuesta al paciente.
_COLUMNAS_SIN_RESPONDER = (
    "wamid",
    "telefono",
    "nombre_perfil",
    "tipo",
    "texto",
    "media_id",
    "mime",
    "recibido_en",
)


def mensajes_sin_responder(
    conn,
    *,
    ventana_minutos: int = 30,
    margen_segundos: int = 90,
    limite: int = 20,
) -> list[dict[str, Any]]:
    """Los mensajes que entraron y se quedaron sin respuesta Y sin fallo.

    Es la consulta que `marcar_fallo_respuesta` lleva nombrando en su docstring desde la
    migración que creó estas columnas --«¿a quién no le contestamos?»-- y que nadie había
    escrito. Hacía falta el 13/09/2026, cuando un despliegue mató un turno en vuelo: el
    mensaje quedó con `respondido_en` NULL y `fallo_respuesta` NULL, o sea invisible para
    todo informe. El proceso no falló, lo mataron, y un proceso muerto no escribe su motivo.

    Los dos NULL juntos son la firma exacta de esa muerte: un fallo de verdad deja motivo, y
    una respuesta que salió deja fecha.

    Tres límites, y ninguno sobra:

    - `margen_segundos` descarta lo que todavía puede estar en vuelo. Un turno tarda la
      ventana del búfer (20 s) más el modelo más el retardo humano; contar un mensaje de hace
      diez segundos como perdido es contar el trabajo en curso.
    - `ventana_minutos` descarta lo viejo. Contestar media hora tarde ya es raro; contestar
      al día siguiente a alguien que se fue es peor que no contestar.
    - `limite` impide que un incidente largo se convierta en una tormenta de turnos al
      arrancar, cada uno con su llamada al modelo.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_COLUMNAS_SIN_RESPONDER)}
              FROM mensajes_entrantes
             WHERE respondido_en  IS NULL
               AND fallo_respuesta IS NULL
               AND recibido_en >  now() - make_interval(mins => %s)
               AND recibido_en <= now() - make_interval(secs => %s)
             ORDER BY recibido_en
             LIMIT %s
            """,
            (ventana_minutos, margen_segundos, limite),
        )
        return [dict(zip(_COLUMNAS_SIN_RESPONDER, fila)) for fila in cur.fetchall()]


def contar_sin_responder(conn, *, margen_segundos: int = 300, ventana_horas: int = 24) -> int:
    """Cuántos mensajes quedaron sin respuesta y sin fallo. Para `/salud`.

    Dos límites, y los dos existen para que el número signifique algo:

    - `margen_segundos` es más ancho que el de `mensajes_sin_responder` a propósito. Este
      número lo mira una persona para decidir si algo va mal, y un turno en curso contado
      como pérdida convierte el indicador en ruido. Mide «lleva cinco minutos sin respuesta y
      nadie anotó por qué».
    - `ventana_horas` lo hace **volver a cero solo**. Sin él arrastraría para siempre las
      pérdidas viejas --medidas el 13/09/2026: seis, entre pruebas del webhook y `/clearstate`
      de antes de que se anotara-- y un indicador que nunca baja es un indicador que nadie
      mira. Lo que interesa es si está pasando AHORA.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM mensajes_entrantes
             WHERE respondido_en  IS NULL
               AND fallo_respuesta IS NULL
               AND recibido_en <= now() - make_interval(secs => %s)
               AND recibido_en >  now() - make_interval(hours => %s)
            """,
            (margen_segundos, ventana_horas),
        )
        return cur.fetchone()[0]


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


# ==========================================================================================
# El relevo -- quién tiene la conversación, y hasta cuándo
# ==========================================================================================
#
# Todo lo de aquí escribe columnas que las migraciones 002 y 003 dejaron puestas y que nadie
# había llegado a usar. No hace falta migración para la 6C: hace falta cablearla.
#
# La regla que sostiene el resto: `tomada_por` distinto de NULL significa que DANIELA ESTÁ
# CALLADA y que el tema de ese paciente está ABIERTO en Telegram. Las dos cosas tienen que
# dejar de ser verdad a la vez, y por eso el cierre tiene una sola puerta (`relevo.cerrar`).


def activar_relevo(conn, *, id_conversacion: str, doctor: str) -> str | None:
    """Le da la conversación a un doctor. Devuelve `None` si se la quedó, o el nombre del
    que ya la tenía.

    El `WHERE tomada_por IS NULL` es lo que decide la carrera, y la carrera es real: el
    escalamiento llega al General de TODOS los doctores a la vez, y dos pulsando el botón con
    segundos de diferencia es el caso normal, no el raro. Sin esa condición el segundo
    pisaría al primero, el `UPDATE` diría que sí a los dos y ambos creerían tener el hilo
    -- con el paciente recibiendo dos conversaciones distintas por el mismo WhatsApp.

    Limpia el cierre anterior a propósito: un mismo paciente puede volver meses después y su
    conversación nueva no puede arrastrar el motivo por el que se cerró la de antes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET tomada_por               = %s,
                   tomada_en                = now(),
                   ultimo_mensaje_doctor_en = NULL,
                   relevo_activado_por      = %s,
                   relevo_activado_en       = now(),
                   relevo_cerrado_en        = NULL,
                   relevo_motivo_cierre     = NULL,
                   actualizada_en           = now()
             WHERE id = %s AND tomada_por IS NULL
            RETURNING id
            """,
            (doctor, doctor, id_conversacion),
        )
        gano = cur.fetchone() is not None
        if gano:
            conn.commit()
            return None
        # No la ganó: o ya la tiene alguien, o la conversación no existe.
        cur.execute("SELECT tomada_por FROM conversaciones WHERE id = %s", (id_conversacion,))
        fila = cur.fetchone()
    conn.rollback()
    return (fila[0] if fila and fila[0] else "alguien más")


def cerrar_relevo(conn, id_conversacion: str, *, motivo: str) -> bool:
    """Se la devuelve a Daniela. `False` si ya estaba cerrado -- y eso no es un error.

    Es idempotente porque las tres salidas pueden solaparse: el doctor pulsa «Listo» en el
    mismo minuto en que el barrido lo da por vencido. Con un `False` claro, la segunda no
    vuelve a cerrar el tema ni a escribir la nota, y el hilo no queda con dos despedidas.

    `motivo` tiene que ser uno de los tres del CHECK de la migración 003
    (`devuelto_por_doctor`, `tiempo_agotado`, `tema_perdido`). Un motivo inventado revienta
    aquí, que es donde se quiere que reviente: el CHECK cerrado existe para que un estado
    nuevo pase por una migración y no se cuele como un string cualquiera.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET tomada_por           = NULL,
                   relevo_cerrado_en    = now(),
                   relevo_motivo_cierre = %s,
                   actualizada_en       = now()
             WHERE id = %s AND tomada_por IS NOT NULL
            RETURNING id
            """,
            (motivo, id_conversacion),
        )
        cerrado = cur.fetchone() is not None
    conn.commit()
    return cerrado


def relevo_por_tema(conn, topic_id: int) -> dict[str, Any] | None:
    """De un tema de Telegram al relevo vivo que hay dentro, o `None` si no hay ninguno.

    Es la consulta del camino doctor -> paciente: llega un mensaje en un hilo y lo único que
    lo acompaña es el `message_thread_id`. De ahí hay que sacar a quién se le reenvía.

    Ese `None` es una respuesta legítima y frecuente, no un fallo: significa que alguien
    escribió en el expediente de un paciente sin tener el relevo, y lo que hay que hacer
    entonces es NO reenviar nada y decírselo. Es el hueco conocido del creador del grupo, al
    que Telegram no le puede quitar permisos.

    `conversacion_viva` no sirve aquí --va por teléfono y por ventana de horas-- porque lo
    que decide no es que la conversación esté fresca sino que esté TOMADA.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.telefono, c.tomada_por, c.cierre_pendiente, c.cierre_tratamiento
              FROM temas_telegram t
              JOIN conversaciones c ON c.telefono = t.telefono
             WHERE t.topic_id = %s
               AND c.tomada_por IS NOT NULL
             ORDER BY c.tomada_en DESC
             LIMIT 1
            """,
            (topic_id,),
        )
        fila = cur.fetchone()
    if fila is None:
        return None
    return {
        "id_conversacion": str(fila[0]),
        "telefono": fila[1],
        "doctor": fila[2],
        # Lo que decide qué SIGNIFICA el próximo mensaje del doctor en ese hilo: o es para el
        # paciente, o es de qué es la cita, o es su fecha. Sin esto, un «15/09 10:00» se le
        # aparecería al paciente en su WhatsApp.
        "cierre_pendiente": fila[3],
        # De qué es la cita, ya contestado, mientras se espera la fecha. Ver la 015.
        "cierre_tratamiento": fila[4],
    }


def marcar_cierre_pendiente(conn, id_conversacion: str, estado: str | None) -> None:
    """En qué punto va el diálogo de cierre. `None` lo apaga.

    El relevo sigue TOMADO mientras esto no es `None` --Daniela tiene que seguir callada
    mientras se le pregunta al doctor si agendó-- así que quien lo ponga tiene que
    garantizar que algo lo va a quitar: lo hacen `relevo.cerrar` y, si el doctor abandona a
    medias, el barrido por `tiempo_agotado`.

    Los valores los cierra el CHECK, ampliado por la 015: `preguntado`,
    `esperando_tratamiento` y `esperando_fecha`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE conversaciones SET cierre_pendiente = %s, actualizada_en = now() "
            " WHERE id = %s",
            (estado, id_conversacion),
        )
    conn.commit()


def guardar_tratamiento_del_cierre(conn, id_conversacion: str, tratamiento: str | None) -> None:
    """De qué es la cita que el doctor está registrando, tal cual la escribió.

    Va a la base y no a memoria por lo mismo que `cierre_pendiente`: el dato llega en la
    pregunta anterior a la fecha, y entre las dos puede reiniciarse el proceso. Perderlo
    significaría registrar la cita con el tratamiento de otra, o sin ninguno.

    **Texto libre a propósito**, y es el único sitio del proyecto donde `citas.tratamiento`
    no se valida contra la lista viva de `tratamientos`. Decisión explícita del cliente
    (14/09/2026): el doctor que acaba de hablar con el paciente sabe de qué es la cita mejor
    que un catálogo cerrado. Lo que implica, y está dicho: Daniela lee ese campo y se lo
    repite al paciente al comprobar su cita.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE conversaciones SET cierre_tratamiento = %s, actualizada_en = now() "
            " WHERE id = %s",
            (tratamiento, id_conversacion),
        )
    conn.commit()


def relevos_activos(conn) -> list[dict[str, Any]]:
    """Los relevos abiertos ahora mismo, con cuántos minutos llevan CALLADOS.

    El reloj no cuenta desde que se activó el relevo sino desde la última señal de vida del
    doctor, y por eso existe `ultimo_mensaje_doctor_en` desde la migración 001 -- una columna
    que hasta la 6C no escribía nadie. Contar desde la activación cortaría a un doctor a
    mitad de frase a las tres horas justas de haber empezado, que es exactamente cuando una
    conversación difícil sigue viva.

    Trae el `topic_id` porque cerrar el relevo es también cerrar el tema, y el barrido no
    puede permitirse una consulta por fila. El `LEFT JOIN` no es cosmética: si fuera un JOIN
    normal, un relevo cuyo hilo se hubiera perdido desaparecería del barrido y no se cerraría
    nunca -- justo el caso que el motivo `tema_perdido` existe para registrar.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   c.telefono,
                   c.tomada_por,
                   t.topic_id,
                   EXTRACT(EPOCH FROM (
                       now() - GREATEST(c.tomada_en,
                                        COALESCE(c.ultimo_mensaje_doctor_en, c.tomada_en))
                   )) / 60.0
              FROM conversaciones c
              LEFT JOIN temas_telegram t ON t.telefono = c.telefono
             WHERE c.tomada_por IS NOT NULL
             ORDER BY c.tomada_en
            """
        )
        filas = cur.fetchall()
    return [
        {
            "id_conversacion": str(f[0]),
            "telefono": f[1],
            "doctor": f[2],
            "topic_id": f[3],
            "minutos_callado": float(f[4] or 0.0),
        }
        for f in filas
    ]


def contar_relevos_abiertos(conn) -> int:
    """Cuántas conversaciones tiene un doctor ahora mismo. Para `/salud`.

    Aparte de `relevos_activos` a propósito: aquel trae cinco columnas por fila para que el
    barrido pueda cerrar sin volver a preguntar, y un indicador de salud no necesita nada de
    eso -- solo el número. Contar `len()` de una lista de diccionarios sería pagar el join y
    el `GREATEST` de todos ellos para tirarlos.

    Es el número que delata un relevo atascado: mientras esté por encima de cero hay
    pacientes a los que Daniela NO está contestando y temas abiertos de par en par.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM conversaciones WHERE tomada_por IS NOT NULL")
        fila = cur.fetchone()
    return fila[0] if fila else 0


def tocar_mensaje_doctor(conn, id_conversacion: str) -> None:
    """Empuja el reloj de cierre. Se llama por cada cosa que el doctor manda al tema.

    Sin esto, `relevos_activos` mediría siempre desde la activación y el cierre por tiempo
    sería un cronómetro y no un detector de inactividad.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET ultimo_mensaje_doctor_en = now(), actualizada_en = now()
             WHERE id = %s
            """,
            (id_conversacion,),
        )
    conn.commit()


def marcar_relevo_activado(conn, telegram_message_id: int) -> None:
    """Deja constancia de que ESE escalamiento fue el que abrió el relevo.

    Va por `telegram_message_id` porque es lo único que trae el `callback_query`: el doctor
    pulsó un botón que cuelga de un mensaje concreto. Es lo que permite, más adelante,
    separar los escalamientos que alguien atendió de los que nadie tocó -- que es la métrica
    que dice si el grupo de doctores está funcionando.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE escalamientos SET relevo_activado = TRUE WHERE telegram_message_id = %s",
            (telegram_message_id,),
        )
    conn.commit()


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
    commit: bool = True,
) -> str:
    """Escribe la cita ya confirmada en los dos lados y devuelve su id.

    `commit=False` es lo que permite que la cita y su recordatorio de víspera nazcan en UNA
    transacción: separadas, una caída entre las dos deja una cita sin recordatorio y nadie se
    entera hasta que el paciente no llega.
    """
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
    if commit:
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


def citas_activas_de_telefono(
    conn, telefono: str, *, desde: datetime, limite: int = 5
) -> list[dict[str, Any]]:
    """Las citas vivas y futuras de ese número, de la más próxima a la más lejana.

    Es la consulta que faltaba. `leer_cita` necesita el UUID y `cita_viva_de_reserva` necesita
    la reserva: las dos exigen algo que solo está en la conversación donde la cita se creó, y
    esa conversación muere a las 24 horas (`conversacion_viva`). Sin esto, el paciente que
    agenda el lunes y escribe el miércoles «muéveme la cita» pedía algo que Daniela no tenía
    forma de encontrar.

    El filtro va por TELÉFONO y no por `paciente_id`: es el criterio más estrecho de los dos
    --una ficha puede tener citas pedidas desde otro número-- y es el mismo al que
    `herramientas._es_ajena` ancla la pertenencia para escribir.

    `desde` corta el pasado. Una cita de ayer no se puede mover ni cancelar, y ofrecerla solo
    sirve para que el modelo proponga algo imposible.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, reserva_id, conversacion_id, paciente_id, nombre_completo, telefono,
                   tratamiento, inicio, duracion_minutos, evento_calendar_id, estado
              FROM citas
             WHERE telefono = %s
               AND estado IN ('confirmada', 'reprogramada')
               AND inicio >= %s
             ORDER BY inicio
             LIMIT %s
            """,
            (telefono, desde, limite),
        )
        filas = cur.fetchall()
    columnas = (
        "id", "reserva_id", "conversacion_id", "paciente_id", "nombre_completo", "telefono",
        "tratamiento", "inicio", "duracion_minutos", "evento_calendar_id", "estado",
    )
    return [dict(zip(columnas, fila)) for fila in filas]


def mover_cita(
    conn, id_cita: str, *, reserva_id: int, inicio: datetime, commit: bool = True
) -> None:
    """Apunta la cita al cupo nuevo. El cupo viejo lo libera quien llama.

    `commit=False` es lo que permite que el movimiento y la cascada de sus recordatorios
    --anular el de la hora vieja, programar el de la nueva-- viajen en UNA transacción. Una
    caída entre las dos deja un recordatorio vivo apuntando a una hora de la que el paciente
    ya salió, y el despachador lo mandaría: sus guardas solo miran lo que hay en la base.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas SET reserva_id = %s, inicio = %s, estado = 'reprogramada',
                             actualizada_en = now()
             WHERE id = %s
            """,
            (reserva_id, inicio, id_cita),
        )
    if commit:
        conn.commit()


def marcar_cita_cancelada(
    conn, id_cita: str, *, motivo: str | None = None, commit: bool = True
) -> None:
    """`commit=False`, por lo mismo que en `mover_cita`: la cancelación y la anulación de los
    recordatorios de esa cita tienen que ser atómicas, o el paciente que canceló recibe la
    víspera el recordatorio de la cita que canceló."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas SET estado = 'cancelada', motivo_cancelacion = %s,
                             actualizada_en = now()
             WHERE id = %s
            """,
            (motivo, id_cita),
        )
    if commit:
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
    cita_id: str | None = None,
    commit: bool = True,
) -> bool:
    """`True` si quedó programado ahora, `False` si ya existía. Nunca duplica.

    `cita_id` es NULLABLE a propósito: `programar_seguimiento` sigue pudiendo encolar algo que
    no cuelga de ninguna cita («llámenme el lunes»). Un seguimiento sin cita se salta las tres
    primeras guardas del despachador.

    `commit=False` es lo que permite que la cita y su recordatorio nazcan en UNA transacción.
    Quien lo use se queda a cargo del `commit`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seguimientos
                (conversacion_id, tipo, fecha_objetivo, clave_idempotencia, cita_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (clave_idempotencia) DO NOTHING
            RETURNING id
            """,
            (id_conversacion, tipo, fecha_objetivo, clave_idempotencia, cita_id),
        )
        nuevo = cur.fetchone() is not None
    if commit:
        conn.commit()
    return nuevo


def anular_seguimientos_de_cita(
    conn, cita_id: str, *, motivo: str, excepto_clave: str | None = None, commit: bool = True
) -> int:
    """Anula los seguimientos vivos de esa cita y devuelve cuántos. Idempotente.

    No borra: un seguimiento anulado con su motivo es lo que permite responder «¿por qué este
    paciente no recibió recordatorio?», que es la primera pregunta que hace la clínica cuando
    alguien no llega.

    `commit=False` para que la anulación viaje en la MISMA transacción que el cambio de la
    cita. Separadas, una caída entre las dos deja un recordatorio vivo apuntando a una cita
    muerta -- y el despachador lo mandaría, porque sus guardas solo miran lo que hay en la base.

    `excepto_clave` perdona UNA fila: la que quien llama acaba de programar. Existe porque
    `reprogramar_cita` inserta el recordatorio nuevo ANTES de anular los viejos, y sin este
    parámetro la anulación se llevaría por delante el que acaba de crear. Y no basta con
    anular primero e insertar después: un reintento de la misma reprogramación encontraría su
    propia fila recién anulada, chocaría en `ON CONFLICT DO NOTHING` al reinsertarla, y la
    cita quedaría movida y SIN ningún recordatorio vivo -- el fallo exacto que la cascada
    existe para evitar.

    El `IS DISTINCT FROM` y no un `<>`: con `NULL <> 'x'` el resultado es NULL, la fila no
    entra en el `UPDATE`, y una fila con la clave sin poner se quedaría viva para siempre.
    Hoy la columna es `NOT NULL`, pero apoyar la corrección en eso es apoyarla en otra tabla.
    """
    condicion_clave = "" if excepto_clave is None else " AND clave_idempotencia IS DISTINCT FROM %s"
    parametros: tuple[Any, ...] = (motivo, cita_id)
    if excepto_clave is not None:
        parametros = (*parametros, excepto_clave)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE seguimientos SET anulado_en = now(), motivo_anulacion = %s
             WHERE cita_id = %s AND enviado_en IS NULL AND anulado_en IS NULL
                   {condicion_clave}
            """,
            parametros,
        )
        anulados = cur.rowcount
    if commit:
        conn.commit()
    return anulados


#: El motivo de anulación que la condición 7 de las dos consultas de cartera busca, y el
#: único que NO caduca con el reloj. Vivía como literal suelto en cuatro sitios -- las dos
#: consultas, la tool y las pruebas--: si uno se separaba de los otros, el bloqueo permanente
#: dejaba de encontrarse sin que nada fallara. Se escribe una vez y las dos consultas lo
#: interpolan (son f-strings) para que separarlos deje de ser posible.
MOTIVO_NEGATIVA_DEL_PACIENTE = "el_paciente_dijo_que_no"


def anular_reactivaciones_vivas(
    conn, telefono: str, *, motivo: str, commit: bool = True
) -> int:
    """Anula los seguimientos de reactivación pendientes de ESE teléfono. Devuelve cuántos.

    Va por teléfono y no por conversación a propósito: la conversación caduca a las 24 h y el
    segundo intento de una serie sale a los 7 días, o sea desde OTRA conversación. Colgarlo de
    `id_conversacion` dejaría vivo justo el mensaje que el paciente acaba de rechazar.

    NO toca `recordatorio_cita`: quien dice «ya no me interesa» a una reactivación no está
    renunciando a que le avisen de su propia cita.

    **`enviado_en IS NULL` es lo que hace que esta función, POR SÍ SOLA, no pueda guardar el
    «no» del paciente** (hallazgo crítico de la revisión final): cuando el paciente PUEDE
    decir que no, la fila que originó ese mensaje ya está enviada, así que devolvía 0 y el
    motivo permanente no se escribía jamás. Es correcto que sea así -- una fila ya enviada no
    se "desenvía"--; lo que faltaba era dejar constancia aparte. Eso lo hace
    `registrar_negativa_de_reactivacion`, que es la puerta que debe usar el código de
    producto: esta función sola solo cierra lo que todavía no ha salido.

    `commit=False` para que la anulación y la lápida viajen en la MISMA transacción.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE seguimientos
               SET anulado_en = now(), motivo_anulacion = %(motivo)s
             WHERE id IN (
                    SELECT s.id FROM seguimientos s
                      JOIN conversaciones cv ON cv.id = s.conversacion_id
                     WHERE cv.telefono = %(tel)s
                       AND s.tipo <> 'recordatorio_cita'
                       AND s.enviado_en IS NULL
                       AND s.anulado_en IS NULL
             )
            """,
            {"tel": telefono, "motivo": motivo[:200]},
        )
        anulados = cur.rowcount
    if commit:
        conn.commit()
    return anulados


def registrar_negativa_de_reactivacion(
    conn, telefono: str, *, id_conversacion: str, tipos: Iterable[str]
) -> dict[str, Any]:
    """El «no» del paciente, escrito donde la condición 7 sabe encontrarlo. Una transacción.

    Devuelve `{"anulados": n, "lapidas": [tipo, ...]}`.

    **El problema que resuelve.** `anular_reactivaciones_vivas` sola no puede guardar un «no»:
    su WHERE exige `enviado_en IS NULL`, y el paciente solo puede decir que no DESPUÉS de que
    el mensaje salió. Medido contra Neon con el código real: `anulados` valía 0 siempre y
    `motivo_anulacion = 'el_paciente_dijo_que_no'` no llegaba nunca a la tabla, así que la
    condición 7 de `_LEADS_SIN_AGENDAR` y `_LEADS_QUE_CANCELARON` -- la que este módulo
    documenta como «la decisión más importante» -- estaba muerta en producción. Lo único que
    quedaba en pie era `contactos.seguimientos_fallidos += 1`, y eso NO sirve como «no»:
    su tope es una perilla editable de `configuracion` y `crear_cita` lo devuelve a 0, así que
    una sola cita -- o subir la perilla de 2 a 3-- borraba el «no» entero.

    **La lápida.** Una fila de `seguimientos` que nace ANULADA, con el motivo permanente y sin
    `enviado_en`. No hace falta migración ni columna nueva: la condición 7 pregunta por
    `(tipo, motivo_anulacion)` y no mira ninguna otra columna, así que encuentra exactamente
    lo que fue escrita para encontrar. Y es INERTE para todo lo demás, que es lo que la hace
    barata -- comprobado condición por condición contra las cinco consultas que leen esta
    tabla:

    - `seguimientos_por_despachar` exige `anulado_en IS NULL`: no la recoge.
    - `contar_comprometidos_hoy` exige `enviado_en` de hoy, o `enviado_en IS NULL AND
      anulado_en IS NULL`: no la cuenta, así que no come cupo del tope diario.
    - `envios_por_contabilizar` exige `enviado_en IS NOT NULL`: no sube ningún contador.
    - La condición 5 («en juego») exige lo mismo que las dos de arriba: no bloquea al OTRO
      tipo, que es justo lo que el bloqueo por tipo quiere permitir.
    - La condición 6 exige `enviado_en IS NOT NULL`: no gasta intentos de serie.

    **Sobrevive a las cuatro cosas que borraban el «no»**, que es el requisito: que la fila ya
    estuviera enviada (la lápida es una fila nueva), que no existiera ninguna fila (idem),
    que la persona agende después (`crear_cita` resetea el contador, no toca `seguimientos`) y
    que alguien mueva una perilla de `configuracion` (la condición 7 no lee ninguna).

    **Va por TIPO y no por persona, y la elección del tipo es lo único con criterio aquí.**
    Quien dijo que no a «¿sigues interesada en agendar?» sí puede recibir después «¿pudiste
    reagendar tu cita?»: son dos asuntos distintos. Así que la lápida se escribe sobre los
    tipos que NOSOTROS le habíamos abierto a esa persona -- los que alguna vez se le enviaron,
    más los que estaban vivos en la cola en este instante--, que es a lo que su «no» puede
    estar contestando.

    Y si no le habíamos abierto NINGUNO -- el paciente preguntó un precio y dijo «no gracias»
    dentro de la conversación normal, que es el caso más común y el que medido contra Neon
    volvía a recibir mensaje 25 h después-- entonces no hay asunto al que atribuir el «no», y
    se escribe sobre `tipos` entero. Es el lado barato de equivocarse: de más, se pierde un
    lead que quizá habría contestado a otro discurso; de menos, se le escribe a alguien a
    quien Daniela acaba de prometerle por escrito que no se le volvería a escribir, que es
    literalmente el fallo por el que se reporta un número.

    `tipos` entra como parámetro y no se importa de `seguimientos.py`: la flecha de imports de
    este proyecto va de `seguimientos` hacia `persistencia`, nunca al revés.
    """
    tipos = tuple(dict.fromkeys(tipos))
    if not tipos:
        return {"anulados": 0, "lapidas": []}

    with conn.cursor() as cur:
        # ANTES de anular: una fila viva todavía tiene `anulado_en IS NULL`, y es
        # precisamente una de las que dicen a qué asunto puede estar contestando el «no».
        cur.execute(
            """
            SELECT DISTINCT s.tipo
              FROM seguimientos s
              JOIN conversaciones cv ON cv.id = s.conversacion_id
             WHERE cv.telefono = %(tel)s
               AND s.tipo = ANY(%(tipos)s)
               AND (s.enviado_en IS NOT NULL OR s.anulado_en IS NULL)
            """,
            {"tel": telefono, "tipos": list(tipos)},
        )
        abiertos = {fila[0] for fila in cur.fetchall()}

    anulados = anular_reactivaciones_vivas(
        conn, telefono, motivo=MOTIVO_NEGATIVA_DEL_PACIENTE, commit=False
    )

    lapidas: list[str] = []
    con_lapida = [tipo for tipo in tipos if not abiertos or tipo in abiertos]
    with conn.cursor() as cur:
        for tipo in con_lapida:
            # La clave la arma el CÓDIGO, nunca el modelo (no negociable 2), y NO lleva el día
            # dentro -- al revés que la del barrido, y por la misma razón que aquella sí lo
            # lleva: la del barrido tiene que poder repetirse mañana, y esta tiene que no
            # poder repetirse nunca. Una por teléfono y tipo, para siempre.
            cur.execute(
                """
                INSERT INTO seguimientos
                    (conversacion_id, tipo, fecha_objetivo, clave_idempotencia,
                     anulado_en, motivo_anulacion)
                VALUES (%(conv)s, %(tipo)s, now(), %(clave)s, now(), %(motivo)s)
                ON CONFLICT (clave_idempotencia) DO NOTHING
                RETURNING id
                """,
                {
                    "conv": id_conversacion,
                    "tipo": tipo,
                    "clave": f"{telefono}:negativa:{tipo}",
                    "motivo": MOTIVO_NEGATIVA_DEL_PACIENTE,
                },
            )
            if cur.fetchone() is not None:
                lapidas.append(tipo)
    conn.commit()
    return {"anulados": anulados, "lapidas": lapidas}


# ==========================================================================================
# El barrido de reactivación (tarea 7) -- quién entra en la cola
#
# `barrido.encolar` es el único consumidor de este bloque. Va aquí y no en `barrido.py`
# porque el SQL es lo que se prueba contra Neon (`tests/test_reactivacion_neon.py`), y ese es
# el criterio que ya separa el resto de este archivo de `seguimientos.py`.
# ==========================================================================================

#: Cuántos días se espera entre un envío de reactivación y el siguiente intento AL MISMO
#: tipo, y también cuántos días sin acabar en cita (Ronda 1, I-2) hacen falta para dar por
#: fallido un envío ya hecho (`envios_por_contabilizar`, más abajo).
#:
#: **Ronda 2 de revisión: I-5 se REVIRTIÓ. Esta nota corrige la de la ronda 1, que decía lo
#: contrario y estaba mal.** La ronda 1 intentó separar «intentos dentro de una serie» de
#: «series por persona», con un contador por serie (`DISTINCT ON`, marcando solo el envío más
#: reciente). Verificado por el revisor: ese diseño NO contaba series -- subiendo la perilla
#: de 2 a 3, el calendario medido daba 3 mensajes con contador 3, no 3 series de 2 (6
#: mensajes); y una serie normal de dos envíos, contabilizada pasada a pasada -que es el caso
#: de cada día-, subía el contador DOS veces, no una. La razón de fondo: en este diseño no
#: existe ningún punto donde nazca una "serie" nueva -- no hay tabla, columna ni evento que
#: agrupe dos envíos como una unidad--, así que "serie" y "envío" son la misma cosa lo mires
#: como lo mires, y separarlas era simular una distinción que el resto del sistema no tiene.
#:
#: **Se simplifica**, y el comportamiento resultante es el correcto -- «como mucho 2 mensajes
#: de reactivación por persona hasta que agende», que es lo que pide el cliente:
#:
#:   - El contador (`contactos.seguimientos_fallidos`, tope `max_seguimientos_fallidos`)
#:     cuenta ENVÍOS de reactivación que no acabaron en cita. Uno por envío. Determinista:
#:     `envios_por_contabilizar` ya no usa `DISTINCT ON` y cuenta cada fila que cumple la
#:     condición, sin agrupar.
#:   - La migración 023 corrige la `descripcion` de `max_seguimientos_fallidos` en
#:     `configuracion`, que hasta ahora decía «series» sin que el código hiciera eso.
#:
#: **`INTENTOS_POR_SERIE_DE_REACTIVACION` (más abajo) NO es la misma cota** -- ronda 3,
#: corrección de una afirmación mía de la ronda 2 ("las dos tienen que moverse juntas"), que
#: el revisor marcó como falsa. Ver su propio docstring para las dos diferencias reales.
#:
#: El calendario que produce, con el default en 2: el primer envío sale a las 24 h de la
#: última señal; si a los 7 días no hay CITA, ese envío cuenta como fallido
#: (`seguimientos_fallidos` pasa a 1) en el MISMO instante en que expira su propio bloqueo de
#: reenvío, así que el segundo envío sale enseguida; si a los 7 días de ESE tampoco hay cita,
#: cuenta otra vez (`seguimientos_fallidos` pasa a 2) y R1 -- aquí, como filtro de cartera, y
#: en `seguimientos.decidir`, como guarda de despacho-- deja de ofrecer ese tipo a esa
#: persona. Total: 2 mensajes, contador en 2 -- verificado con la simulación de
#: `test_el_calendario_medido_de_las_tres_personas_da_2`.
#:
#: Las tareas 1-6 de este plan no dejaron construido un calendario 24 h / 7 días explícito
#: para la reactivación: esta sigue siendo la interpretación de este módulo, documentada
#: porque es una decisión de diseño y no un hecho verificado contra el resto del código. Los
#: dos números son constantes y se pueden ajustar sin tocar la forma de ninguna consulta.
DIAS_ENTRE_INTENTOS_DE_REACTIVACION = 7

#: La ventana de cartera: pasado esto, «hace unos días nos escribió» es falso y decirlo es de
#: las cosas por las que la gente reporta un número. Es el borde superior de la condición 2 de
#: las dos consultas de abajo, y las dos lo interpolan.
#:
#: Y es además la cota superior de `fecha_objetivo` en `herramientas._programar_seguimiento`
#: (revisión final). Ahí no había ninguna: `_a_fecha` solo exigía que la fecha fuera futura,
#: así que el MODELO podía programar un seguimiento a seis meses. La doctrina del proyecto es
#: que lo que el modelo escribe se acota en el código (no negociables 2 y 12), y el valor no
#: hay que elegirlo: un seguimiento programado más allá de esta ventana saldría diciendo
#: «hace unos días nos escribió» sobre algo que la cartera ya no considera reciente. Encima
#: interactúa con `contar_comprometidos_hoy`: una fila a semanas vista no cuenta contra el
#: cupo del día y luego cae fuera de todo ritmo.
DIAS_DE_VENTANA_DE_CARTERA = 30

#: Ronda 3 de revisión: corregido. La ronda 2 documentaba esto como «la MISMA cota que
#: `max_seguimientos_fallidos`, expresada una segunda vez» y ordenaba moverlas juntas -- las
#: dos afirmaciones eran mías y las dos eran falsas, y el revisor lo señaló. Son dos cotas
#: DISTINTAS que se solapan pero miden cosas diferentes:
#:
#:   - `max_seguimientos_fallidos` (R1, condición 4 de abajo) es POR PERSONA, cruza los dos
#:     tipos de reactivación, y cuenta lo YA CONTABILIZADO -- el historial cerrado de envíos
#:     que no llegaron a cita, para siempre hasta que agende.
#:   - `INTENTOS_POR_SERIE_DE_REACTIVACION` (condición 6) es POR TIPO, no cruza tipos, y
#:     cuenta lo SIN CONTABILIZAR TODAVÍA -- cuántos envíos de ESTE tipo están "en el aire",
#:     esperando su turno en `envios_por_contabilizar`. Es el freno que impide un envío DE
#:     MÁS mientras esos dos siguen sin contabilizar (ver su condición 6) -- no protege de la
#:     inanición, al revés de lo que un nombre anterior de este comentario sugería: mientras
#:     `_contabilizar_envios_vencidos` no haya corrido sobre un envío viejo, esta cuenta lo
#:     sigue viendo como pendiente aunque R1 (que mira `contactos`, ya actualizado o no) no se
#:     haya enterado todavía.
#:
#: **Por eso NO se mueven juntas.** Subir `max_seguimientos_fallidos` no tiene por qué subir
#: esta constante, y viceversa: la primera decide cuánta paciencia tiene la clínica con una
#: persona a lo largo de su historial; la segunda decide cuántos envíos del MISMO tipo pueden
#: quedar sin resolver a la vez antes de frenar en seco, que es una pregunta sobre el ritmo
#: del barrido, no sobre la persona. Bajarla sin tocar la otra sigue siendo seguro (el freno
#: de cartera se dispara antes); subirla sin tocar la otra también (R1 sigue siendo el límite
#: real). Documentarlas como "la misma cota" es lo que aflojaría el fail-safe el día que
#: alguien cambie una sin la otra creyendo que ya no hace falta.
INTENTOS_POR_SERIE_DE_REACTIVACION = 2

#: Quien preguntó y no agendó. Las condiciones, y ninguna sobra:
#:
#: 1. Tiene conversación de **WhatsApp** -- regla 1: solo a quien escribió PRIMERO, y solo
#:    por ese canal (Ruling C4: `conversaciones.canal` admite también `'web'`, y una
#:    conversación del chat del panel no es un número de WhatsApp al que mandarle una
#:    plantilla).
#: 2. Su último mensaje fue hace >= 24 h y < 30 días -- antes sigue en la conversación, y
#:    «hace unos días nos escribió» sobre algo de hace tres meses es falso: de las cosas por
#:    las que la gente reporta un número.
#: 3. No tiene cita FUTURA no cancelada -- a quien ya tiene hora no se le persigue.
#: 4. No pidió la baja, y el contador de envíos fallidos no llegó al tope (R1, por
#:    duplicado: `seguimientos.decidir` la vuelve a mirar al despachar, pero no hay motivo
#:    para encolar aquí una fila que esa guarda va a anular de todas formas). El contador
#:    (Ronda 2, I-5 revertida: ver `DIAS_ENTRE_INTENTOS_DE_REACTIVACION`) cuenta ENVÍOS que
#:    no acabaron en cita, uno por envío -- no "series".
#: 5. No tiene ya un seguimiento de **NINGÚN tipo de reactivación** EN JUEGO -- pendiente de
#:    decidir, o enviado hace menos de `DIAS_ENTRE_INTENTOS_DE_REACTIVACION` días. **Ronda 1
#:    de revisión, I-1: antes miraba solo su PROPIO tipo**, así que quien preguntó y no
#:    agendó Y ADEMÁS canceló una cita vieja calificaba por las DOS consultas a la vez y
#:    recibía dos discursos distintos («¿sigues interesada?» y «¿pudiste reagendar tu
#:    cita?») en la misma ventana de 24 h. Ahora "en juego" es de la PERSONA, no del tipo:
#:    quien tiene algo pendiente o reciente de cualquier tipo no recibe otro mientras tanto.
#:
#:    **Consecuencia de I-1, dicha y no escondida:** quien cae en las dos listas gasta su
#:    presupuesto entero en el tipo que el barrido procese PRIMERO en la pasada
#:    (`reactivacion_sin_agendar`, por el orden fijo de `barrido.encolar`) y NUNCA llega a
#:    recibir `reactivacion_cancelada` -- el freno por persona (R1, condición 4) es
#:    compartido entre tipos, así que se agota antes de que el segundo tipo tenga su turno.
#:    Es una decisión de producto, no un descuido: MaxiCare tiene que poder verla aquí.
#: 6. El tope de intentos DE ESTE TIPO (`INTENTOS_POR_SERIE_DE_REACTIVACION`) tampoco se
#:    alcanzó -- cuántos envíos de este tipo siguen SIN CONTABILIZAR (`enviado_en NOT NULL
#:    AND contabilizado_en IS NULL`). **Ronda 3, corrección: esto NO es la misma cota que la
#:    condición 4** (`max_seguimientos_fallidos`) -- ver el docstring de
#:    `INTENTOS_POR_SERIE_DE_REACTIVACION` para las dos diferencias reales (por persona sobre
#:    lo contabilizado, contra por tipo sobre lo sin contabilizar). Esta condición es el freno
#:    TRANSITORIO contra un envío de MÁS -- no una guarda de inanición, que sería lo
#:    contrario: una guarda de inanición impide que alguien se quede SIN turno, y esta impide
#:    mandar UNO DE MÁS-- mientras `barrido._contabilizar_envios_vencidos` no haya corrido
#:    sobre un envío que ya venció, esta cuenta lo sigue viendo como "en el aire" aunque el
#:    contador de `contactos` (condición 4) todavía no se haya enterado. En la operación
#:    normal casi nunca es la que dispara primero -- `envios_por_contabilizar` corre
#:    antes que esta consulta en cada pasada que llega a abrir conexión (ver `barrido.
#:    encolar`) -- pero "casi nunca" no es "nunca": por eso hace falta como capa aparte, y por
#:    eso NO se puede alojar donde pasa la tool ni donde pasa `seguimientos.decidir`, solo
#:    aquí, en el filtro de cartera.
#: 7. **NUNCA se le anuló una serie de este tipo con motivo `el_paciente_dijo_que_no`.** Esta
#:    es la condición que el encargo original NO traía. `herramientas._cerrar_seguimiento`
#:    anula con ese motivo EXACTO cuando el paciente dice explícitamente que no quiere que le
#:    insistan sobre ESTA consulta, y las otras condiciones de arriba -en particular la 5,
#:    que solo mira `anulado_en IS NULL`- no la distinguen de una anulación por
#:    `llego_tarde` o por `fuera_de_horario_comercial`: esas SÍ deben poder volver a
#:    ofrecerse, porque el motivo por el que no salieron ya dejó de ser cierto. Un «no»
#:    explícito es distinto: no caduca con el reloj, así que este bloqueo NO lleva ventana de
#:    tiempo -- es permanente para esta consulta. Es la decisión más importante de este
#:    módulo: sin ella, Daniela le dice al paciente «anotado, no se le vuelve a escribir
#:    sobre esta consulta» y el barrido se lo vuelve a ofrecer al día siguiente, que es
#:    literalmente el fallo por el que se reporta un número.
#:
#:    **Y es deliberadamente por TIPO, no por persona (Ronda 1, observación del revisor,
#:    dicha y no escondida): quien dijo que no a "¿sigues interesada en agendar?" SÍ puede
#:    recibir después "¿pudiste reagendar tu cita?" sobre una consulta distinta.** Son dos
#:    asuntos diferentes -- eso es lo que defiende que el bloqueo sea por tipo-- pero el
#:    paciente no necesariamente percibe la diferencia: si Daniela dijo «no se le vuelve a
#:    escribir sobre esta consulta» y lo siguiente que le llega es el otro tipo, puede sentir
#:    que la promesa no se cumplió. Se acepta el riesgo porque cerrar el bloqueo a nivel de
#:    PERSONA (en vez de tipo) apagaría la reactivación entera -incluida una consulta legítima
#:    y distinta- por un "no" que solo hablaba de la primera.
#:
#: **Límite conocido y aceptado, dicho y no escondido:** esta consulta NO repite el tope
#: anual (R2, `max_reactivaciones_12m`) ni las guardas de horario o contacto reciente
#: (R4, R5) de `seguimientos.decidir`. Solo R1 (el freno por persona) se mira aquí, como
#: filtro barato de cartera. Encolar una fila que R2, R4 o R5 van a anular al despachar es
#: trabajo de sobra, nunca un envío de más: `seguimientos.decidir` vuelve a evaluar las
#: cinco guardas de reactivación sobre CADA fila antes de mandar nada, así que ninguna de
#: las dos consultas de este módulo es, por sí sola, la última palabra sobre a quién se le
#: escribe -- lo es `decidir`.
_LEADS_SIN_AGENDAR = f"""
WITH ultima_conversacion AS (
    -- Por TELÉFONO, no por fila de mensaje: `mensajes_entrantes` NO TIENE
    -- `conversacion_id` (verificado contra la migración 004 -- Ruling C3 --: la tabla es
    -- wamid/telefono/recibido_en y nada más), así que la única conversación que se puede
    -- nombrar aquí es la MÁS RECIENTE de ese número. Sin el DISTINCT ON, un número con
    -- varias conversaciones históricas (`conversacion_viva` cierra a las 24 h, así que
    -- cualquier lead de más de un día ya tiene más de una) entraría una vez por cada una.
    SELECT DISTINCT ON (telefono) telefono, id AS conversacion_id
      FROM conversaciones
     WHERE canal = 'whatsapp'
     ORDER BY telefono, creada_en DESC
),
ultimo_mensaje AS (
    SELECT telefono, max(recibido_en) AS cuando
      FROM mensajes_entrantes
     GROUP BY telefono
)
SELECT uc.telefono, uc.conversacion_id, um.cuando AS ultimo_mensaje
  FROM ultima_conversacion uc
  JOIN ultimo_mensaje um ON um.telefono = uc.telefono
  LEFT JOIN contactos co ON co.telefono = uc.telefono
 WHERE COALESCE(co.no_contactar, FALSE) = FALSE
   AND COALESCE(co.seguimientos_fallidos, 0) < %(max_fallidos)s
   AND um.cuando <= %(ahora)s - interval '24 hours'
   AND um.cuando >  %(ahora)s - interval '{DIAS_DE_VENTANA_DE_CARTERA} days'
   AND NOT EXISTS (
        SELECT 1 FROM citas c
         WHERE c.telefono = uc.telefono
           AND c.inicio > %(ahora)s
           AND c.estado <> 'cancelada'
   )
   AND NOT EXISTS (
        -- I-1: TODOS los tipos de reactivacion, no solo el de esta consulta -- ver la
        -- condicion 5 de arriba.
        SELECT 1 FROM seguimientos s
          JOIN conversaciones cv2 ON cv2.id = s.conversacion_id
         WHERE cv2.telefono = uc.telefono
           AND s.tipo IN ('reactivacion_sin_agendar', 'reactivacion_cancelada')
           AND (
                (s.anulado_en IS NULL AND s.enviado_en IS NULL)
             OR (s.enviado_en IS NOT NULL
                 AND s.enviado_en > %(ahora)s
                                    - interval '{DIAS_ENTRE_INTENTOS_DE_REACTIVACION} days')
           )
   )
   AND (
        -- Ronda 3: NO es la misma cota que el contador de R1 (condicion 4) -- es el
        -- freno TRANSITORIO contra un envio DE MAS, no una guarda de inanicion (esa seria
        -- lo contrario: proteger de que alguien se quede SIN turno). Por tipo y sobre lo
        -- SIN contabilizar. Ver el docstring de INTENTOS_POR_SERIE_DE_REACTIVACION y la
        -- condicion 6 de `_LEADS_SIN_AGENDAR`.
        -- Tipo-especifico, al reves que el bloqueo de arriba.
        SELECT count(*) FROM seguimientos s4
          JOIN conversaciones cv4 ON cv4.id = s4.conversacion_id
         WHERE cv4.telefono = uc.telefono
           AND s4.tipo = 'reactivacion_sin_agendar'
           AND s4.enviado_en IS NOT NULL
           AND s4.contabilizado_en IS NULL
   ) < {INTENTOS_POR_SERIE_DE_REACTIVACION}
   AND NOT EXISTS (
        -- El "no" explicito, condicion 7: por TIPO, a proposito -- ver el comentario largo.
        SELECT 1 FROM seguimientos s3
          JOIN conversaciones cv3 ON cv3.id = s3.conversacion_id
         WHERE cv3.telefono = uc.telefono
           AND s3.tipo = 'reactivacion_sin_agendar'
           AND s3.motivo_anulacion = '{MOTIVO_NEGATIVA_DEL_PACIENTE}'
   )
 ORDER BY um.cuando DESC
 LIMIT %(limite)s
"""


def leads_sin_agendar(
    conn, *, ahora: datetime, limite: int = 200, max_seguimientos_fallidos: int = 2
) -> list[dict[str, Any]]:
    """Quien preguntó y no agendó, y todavía puede recibir un mensaje. Ver `_LEADS_SIN_AGENDAR`.

    `max_seguimientos_fallidos` lleva DEFAULT (Ruling C9): las pruebas del encargo original
    la llaman sin ese argumento.
    """
    with conn.cursor() as cur:
        cur.execute(
            _LEADS_SIN_AGENDAR,
            {"ahora": ahora, "limite": limite, "max_fallidos": max_seguimientos_fallidos},
        )
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


#: La misma forma que `_LEADS_SIN_AGENDAR`, sobre `citas` con `estado = 'cancelada'` en vez
#: de sobre `mensajes_entrantes`. El «último mensaje» de aquella es aquí «cuándo se canceló»
#: (`citas.actualizada_en`, que `marcar_cita_cancelada` toca a propósito): es el evento que
#: convierte a esta persona en un lead, así que es contra el que se mide la ventana de
#: 24 h-30 días.
#:
#: **Ronda 1 de revisión, C4 (quedó a medias): SÍ hace falta el `JOIN` a `conversaciones` y
#: el filtro `canal = 'whatsapp'`.** El argumento de que "una cita solo nace de una
#: conversación real de WhatsApp" es cierto hoy, pero no estaba protegido por ninguna
#: prueba: mutar el filtro de la OTRA consulta a `WHERE TRUE` dejaba la suite de Neon entera
#: en verde. Ahora las dos consultas repiten el mismo filtro, con el mismo criterio.
_LEADS_QUE_CANCELARON = f"""
WITH ultima_cancelada AS (
    SELECT DISTINCT ON (c.telefono) c.telefono, c.conversacion_id, c.actualizada_en
      FROM citas c
      JOIN conversaciones cv ON cv.id = c.conversacion_id
     WHERE c.estado = 'cancelada'
       AND cv.canal = 'whatsapp'
     ORDER BY c.telefono, c.actualizada_en DESC
)
SELECT uc.telefono, uc.conversacion_id, uc.actualizada_en AS ultimo_mensaje
  FROM ultima_cancelada uc
  LEFT JOIN contactos co ON co.telefono = uc.telefono
 WHERE COALESCE(co.no_contactar, FALSE) = FALSE
   AND COALESCE(co.seguimientos_fallidos, 0) < %(max_fallidos)s
   AND uc.actualizada_en <= %(ahora)s - interval '24 hours'
   AND uc.actualizada_en >  %(ahora)s - interval '{DIAS_DE_VENTANA_DE_CARTERA} days'
   AND NOT EXISTS (
        SELECT 1 FROM citas c2
         WHERE c2.telefono = uc.telefono
           AND c2.inicio > %(ahora)s
           AND c2.estado <> 'cancelada'
   )
   AND NOT EXISTS (
        -- I-1: TODOS los tipos de reactivacion -- ver la condicion 5 de `_LEADS_SIN_AGENDAR`.
        SELECT 1 FROM seguimientos s
          JOIN conversaciones cv ON cv.id = s.conversacion_id
         WHERE cv.telefono = uc.telefono
           AND s.tipo IN ('reactivacion_sin_agendar', 'reactivacion_cancelada')
           AND (
                (s.anulado_en IS NULL AND s.enviado_en IS NULL)
             OR (s.enviado_en IS NOT NULL
                 AND s.enviado_en > %(ahora)s
                                    - interval '{DIAS_ENTRE_INTENTOS_DE_REACTIVACION} days')
           )
   )
   AND (
        -- Ronda 3: NO es la misma cota que el contador de R1 (condicion 4) -- es el
        -- freno TRANSITORIO contra un envio DE MAS, no una guarda de inanicion (esa seria
        -- lo contrario: proteger de que alguien se quede SIN turno). Por tipo y sobre lo
        -- SIN contabilizar. Ver el docstring de INTENTOS_POR_SERIE_DE_REACTIVACION y la
        -- condicion 6 de `_LEADS_SIN_AGENDAR`.
        SELECT count(*) FROM seguimientos s4
          JOIN conversaciones cv4 ON cv4.id = s4.conversacion_id
         WHERE cv4.telefono = uc.telefono
           AND s4.tipo = 'reactivacion_cancelada'
           AND s4.enviado_en IS NOT NULL
           AND s4.contabilizado_en IS NULL
   ) < {INTENTOS_POR_SERIE_DE_REACTIVACION}
   AND NOT EXISTS (
        -- El "no" explicito: por TIPO, a proposito -- ver el comentario largo en
        -- `_LEADS_SIN_AGENDAR`.
        SELECT 1 FROM seguimientos s2
          JOIN conversaciones cv2 ON cv2.id = s2.conversacion_id
         WHERE cv2.telefono = uc.telefono
           AND s2.tipo = 'reactivacion_cancelada'
           AND s2.motivo_anulacion = '{MOTIVO_NEGATIVA_DEL_PACIENTE}'
   )
 ORDER BY uc.actualizada_en DESC
 LIMIT %(limite)s
"""


def leads_que_cancelaron(
    conn, *, ahora: datetime, limite: int = 200, max_seguimientos_fallidos: int = 2
) -> list[dict[str, Any]]:
    """Quien canceló y no volvió a agendar. Ver `_LEADS_QUE_CANCELARON`.

    `max_seguimientos_fallidos` lleva DEFAULT por la misma razón que en `leads_sin_agendar`
    (Ruling C9).
    """
    with conn.cursor() as cur:
        cur.execute(
            _LEADS_QUE_CANCELARON,
            {"ahora": ahora, "limite": limite, "max_fallidos": max_seguimientos_fallidos},
        )
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def contar_comprometidos_hoy(conn, *, ahora: datetime) -> int:
    """Cuánto cuenta HOY contra el tope diario: lo ya ENVIADO hoy, más lo PENDIENTE que es
    "de hoy" -- lo que vence antes de que acabe el día de Bogotá, o lo que YA se aplazó
    alguna vez, sea cual sea la hora a la que quedó.

    **CRÍTICO, ronda 1 de revisión sobre la parada E.** Antes de esta función, el cupo
    contaba solo `enviado_en`: una fila encolada y luego APLAZADA (R4 fuera de 9-19h,
    `seguimientos.decidir`) se queda pendiente -`enviado_en` sigue en NULL- e invisible para
    ese conteo. R4 aplaza, no anula, así que fuera del horario comercial el cupo se veía
    libre TODA LA NOCHE, y cada pasada horaria del barrido volvía a encolar el tope entero
    sobre gente NUEVA. Medido por el revisor con 400 leads y tope 20: 280 mensajes reales de
    golpe a las 9:00 del día siguiente.

    **Ronda 2, bug 2.1: contar CUALQUIER pendiente tampoco vale.** `programar_seguimiento`
    (la tool que llama el MODELO) encola una reactivación con `fecha_objetivo` arbitraria y
    SIN cota superior (`_a_fecha` solo exige que sea futura), así que contar cualquier
    pendiente sin mirar su fecha hacía que un puñado de seguimientos programados a semanas
    vista apagara el barrido entero durante días: no se resuelven hasta su fecha, muy lejana,
    así que nunca dejan de "contar".

    **Ronda 2, intento fallido: una ventana de horas fijas.** La primera corrección puso una
    ventana de 48 h (`fecha_objetivo <= ahora + 48h`), calculada a mano estimando "el peor
    aplazamiento realista". Estaba mal en las dos direcciones, y el revisor lo midió:

    - **Insuficiente**: `_proxima_apertura(sábado 08:00, Jornada())` cae el LUNES a las
      08:00 -- 48,0 horas exactas, cero margen-- y la ventana usaba `<=`, así que ese caso
      límite SÍ debía contar y con redondeos de reloj real podía no hacerlo. El propio texto
      que justificaba el 48 estaba equivocado: decía que el peor caso era "un viernes de
      noche" mirando R4, y **R4 no consulta `Jornada` en absoluto** -- usa las constantes fijas
      `HORA_APERTURA_COMERCIAL`/`HORA_CIERRE_COMERCIAL` (9-19), nunca `jornada.cierre_de()`.
      El peor caso real sale de G5/G7, que SÍ usan la `Jornada` de la clínica (cierra sábado a
      las 15h, domingo cerrado), y es el sábado, no el viernes.
    - **Frágil por diseño**: cualquier cambio futuro al horario comercial, a la `Jornada` o a
      las guardas de `seguimientos.decidir` puede alargar el aplazamiento máximo real sin que
      nada avise -- el número quedaba grabado en una constante, desconectado de las reglas que
      lo producen. Verificado: el mutante "48 -> 24" sobrevivía con toda la suite en verde.

    **Ruling E9: se quita el número y se pone la semántica.** En vez de calcular cuántas horas
    puede durar un aplazamiento, se pregunta lo que de verdad hace falta saber: ¿esta fila,
    aunque esté pendiente, es del tipo que puede cruzar la medianoche? Dos maneras, ninguna
    basada en una duración:

    1. Su `fecha_objetivo` cae DENTRO del día de Bogotá de `ahora` -- es, literalmente, "de
       hoy", sin importar si ya se aplazó o no.
    2. `aplazado_desde` NO es NULL -- la migración 022 ya la escribe (con `COALESCE`, la
       primera vez que `aplazar_seguimiento` toca la fila) exactamente en el conjunto de
       filas que alguna guarda de `decidir` empujó hacia delante, sea cual sea la nueva
       `fecha_objetivo`. Es la señal semántica de "esto ya se movió una vez y puede seguir
       moviéndose", y no depende de contar horas: una fila aplazada de un sábado 19:00 a un
       lunes 09:00 (38 h) cuenta igual que una aplazada de un viernes 20:00 a un lunes 08:00.

    Lo que NUNCA entra por la vía 2 es justo lo que había que excluir: una fila que
    `programar_seguimiento` creó a futuro y que nadie ha aplazado todavía tiene
    `aplazado_desde IS NULL` por construcción, así que solo cuenta si además cae dentro del
    día de hoy -- y si `fecha_objetivo` está a dos semanas vista, no cae.

    **Cómo se libera una fila aplazada, para que el cupo no quede clavado en 0**: R3/R3bis de
    `seguimientos.decidir` (ver `seguimientos.py`) acaban ANULANDO una reactivación que lleva
    demasiado aplazada (`llego_tarde` si se retrasa más de 2 h sobre su `fecha_objetivo`,
    `reactivacion_estancada` a las 96 h desde el primer aplazamiento, `HORAS_DE_ESPERA_QUE_
    INVALIDAN_UN_APLAZAMIENTO_SOSTENIDO`), y una fila anulada deja de cumplir `anulado_en IS
    NULL`, así que sale del conteo. **Las 96 h son el techo SOLO para el caso que nombra ese
    umbral -- el relevo sostenido (ver su propio docstring)**: no en "para siempre", pero
    tampoco en general. R3bis solo se evalúa cuando `despachar` vuelve a mirar esa fila --
    es decir, cuando `ahora` alcanza su `fecha_objetivo` de nuevo--, así que el techo real es
    esas 96 h MÁS el horizonte hasta esa próxima recogida: con la jornada por defecto (cierra
    el domingo, y el sábado a las 15h) eso puede sumar hasta ~144 h, no 96.

    Solo se libera del conteo cuando se ENVÍA (pasa a contar como "enviado hoy", ese día en
    concreto) o se ANULA. Esto también dice, a propósito, que un backlog de aplazadas de un
    fin de semana entero sigue ocupando el cupo hasta que se resuelve: es la misma cautela
    que la regla 9 pide.

    **Decisión, para quien se pregunte por qué el barrido no se apaga fuera de horario en vez
    de esto:** no hace falta. Encolar de madrugada es inofensivo por sí solo -- el envío de
    verdad lo decide `seguimientos.decidir` con R4/R5, que sí conocen el horario-- y este
    conteo ya acota cuánto puede acumularse mientras tanto.

    Excluye `recordatorio_cita` (Ruling C2): un día con muchas citas agendadas no puede
    comerse el cupo de reactivación. **M13, ronda 2**: sin este filtro, con recordatorios de
    cita pendientes contando -y siempre los hay- el cupo de reactivación sería 0 de forma
    permanente. `test_contar_comprometidos_hoy_no_cuenta_recordatorios_de_cita_pendientes` lo
    fija.
    """
    inicio_del_dia = ahora.astimezone(ZONA_BOGOTA).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    fin_del_dia = inicio_del_dia + timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM seguimientos
             WHERE tipo <> 'recordatorio_cita'
               AND (
                    (enviado_en >= %(inicio)s AND enviado_en < %(fin)s)
                 OR (enviado_en IS NULL AND anulado_en IS NULL
                     AND (fecha_objetivo < %(fin)s OR aplazado_desde IS NOT NULL))
               )
            """,
            {"inicio": inicio_del_dia, "fin": fin_del_dia},
        )
        (total,) = cur.fetchone()
    return total


def envios_por_contabilizar(
    conn,
    *,
    ahora: datetime,
    dias: int = DIAS_ENTRE_INTENTOS_DE_REACTIVACION,
    limite: int = 200,
) -> list[dict[str, Any]]:
    """Envíos de reactivación que llevan más de `dias` días sin acabar en una cita.

    **Ronda 1 de revisión, I-2 -- el hallazgo más grave de toda la parada.** La versión
    anterior descartaba el envío si había CUALQUIER mensaje del paciente después de mandarlo
    (`NOT EXISTS mensajes_entrantes ... recibido_en > enviado_en`). Eso exoneraba para
    SIEMPRE a quien contestaba algo -- un «ahora no, gracias» que Daniela no interpreta como
    un cierre, o cualquier cosa- del contador: nunca llegaba a `seguimientos_fallidos`, y
    encima cada respuesta reiniciaba la ventana de 24 h-30 días de `leads_sin_agendar`.
    Medido por el revisor: un número que siempre contesta pero nunca agenda recibía 6
    mensajes en 40 días, contra 2 de quien se queda callado -- exactamente al revés de lo que
    el freno por persona (R1) existe para lograr.

    El criterio correcto es si terminó en una CITA, no si hubo una RESPUESTA: es la misma
    vara que ya usa `crear_cita` para resetear el contador
    (`persistencia.reiniciar_seguimientos_fallidos`), así que agendar es lo único que
    exonera, en los dos sitios.

    **Ronda 2 de revisión: I-5 se REVIRTIÓ -- ver el docstring de `DIAS_ENTRE_INTENTOS_DE_
    REACTIVACION` para el porqué completo.** La ronda 1 puso aquí un `DISTINCT ON
    (telefono, tipo)` para contar "una unidad por serie". Verificado por el revisor: eso NO
    contaba series -- una de dos envíos, contabilizada pasada a pasada (el caso normal),
    subía el contador DOS veces, no una, porque el `DISTINCT ON` solo colapsa dos filas
    cuando vencen EN LA MISMA CONSULTA, y en la operación normal cada envío vence en un
    momento distinto. Y subiendo la perilla de 2 a 3, el resultado eran 3 mensajes con
    contador 3, no 3 series de 2 (6 mensajes) -- la propia prueba estrella de la ronda 1
    (`test_no_contabiliza_dos_veces_la_misma_serie`) había congelado el defecto como
    comportamiento esperado. Ahora cada fila que cumple la condición cuenta como UNA unidad,
    sin agrupar: determinista, y "serie" pasa a ser simplemente "envío" -- que es lo único
    que este diseño puede distinguir, porque no existe ningún punto donde nazca una serie
    nueva.

    `tipo IN (...)` lleva los literales de `seguimientos.TIPOS_QUE_EL_BARRIDO_ENCOLA` a mano
    y no importados: la flecha de imports de este proyecto va de `seguimientos.py` hacia
    `persistencia.py`, nunca al revés, y esta función tiene que quedarse de este lado para no
    crear un ciclo.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, cv.telefono
              FROM seguimientos s
              JOIN conversaciones cv ON cv.id = s.conversacion_id
             WHERE s.tipo IN ('reactivacion_sin_agendar', 'reactivacion_cancelada')
               AND s.enviado_en IS NOT NULL
               AND s.enviado_en <= %(ahora)s - (%(dias)s * interval '1 day')
               AND s.contabilizado_en IS NULL
               AND NOT EXISTS (
                    -- Se cuenta como fallido salvo que la persona haya terminado con una
                    -- cita VIVA agendada DESPUÉS de este envío (I-2: la vara es agendar, no
                    -- contestar).
                    SELECT 1 FROM citas c
                     WHERE c.telefono = cv.telefono
                       AND c.estado <> 'cancelada'
                       AND c.creada_en > s.enviado_en
               )
             ORDER BY s.enviado_en
             LIMIT %(limite)s
            """,
            {"ahora": ahora, "dias": dias, "limite": limite},
        )
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def marcar_envio_contabilizado(conn, id_seguimiento: int, *, commit: bool = True) -> None:
    """Deja constancia de que esta fila ya subió el contador, para que no lo vuelva a subir.

    Ruling D5: quien la llame junto a `sumar_seguimiento_fallido(commit=False)` tiene que
    llamar a ESTA función DESPUÉS, sobre la misma conexión, y dejar que su `commit()`
    confirme las dos escrituras juntas -- ver el docstring corregido de
    `sumar_seguimiento_fallido`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET contabilizado_en = now() WHERE id = %s",
            (id_seguimiento,),
        )
    if commit:
        conn.commit()


def seguimientos_por_despachar(
    conn, *, ahora: datetime, limite: int = 50
) -> list[dict[str, Any]]:
    """Las filas vencidas, con TODO lo que el despachador necesita para decidir.

    El `LEFT JOIN` a `citas` y a `conversaciones` no es una optimización: sin él, cada guarda
    sería una consulta más por fila, y el barrido de las 6 p. m. --que es cuando salen todos
    los recordatorios del día a la vez-- haría cientos de viajes a Neon. El `LEFT JOIN` a
    `pacientes` y la subconsulta a `mensajes_entrantes` son la cascada de nombre de una
    reactivación (`nombre_ficha`, `nombre_perfil`; ver `seguimientos._nombre_de_reactivacion`),
    añadidos en la ronda 1 de revisión de la tarea 4 tras medir que el 100% de esas filas
    salía con "Hola paciente".

    `FOR UPDATE ... SKIP LOCKED` es lo que permite que dos instancias no manden el mismo
    recordatorio dos veces -pero solo HASTA el primer `commit` de la conexión que hizo esta
    lectura. Después de ese `commit`, quien impide el duplicado ya no es este candado: sigue
    leyendo.

    Esta función no hace `commit` ni `rollback`. El candado es de la TRANSACCIÓN, no de la
    fila: mientras `conn` no confirme ni revierta, las N filas del lote entero siguen tomadas.
    Pero en cuanto `conn` haga su PRIMER `commit()` -y quien llama esta función normalmente
    escribe sobre cada fila con su propio `commit()` interno, uno por fila- se suelta el
    candado del LOTE COMPLETO, no solo el de la fila que se acaba de escribir: las filas
    2..N, que siguen con `enviado_en`/`anulado_en` en NULL, quedan libres para que OTRA
    instancia las recoja en su propio ciclo mientras esta sigue procesando las suyas.

    Por eso el candado de aquí NO es, por sí solo, la protección contra el envío duplicado
    -solo lo es hasta esa primera escritura-. La protección real, para la escritura que de
    verdad importa, vive en la propia sentencia de esa escritura: `marcar_seguimiento_enviado`
    solo marca si `enviado_en` seguía en NULL en ese instante, así que una fila que ya se llevó
    otra instancia no se vuelve a marcar ni se manda dos veces, la haya recogido quien la haya
    recogido.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.conversacion_id, s.cita_id, s.tipo, s.fecha_objetivo, s.intentos,
                   COALESCE(c.telefono, cv.telefono)      AS telefono,
                   c.nombre_completo, c.tratamiento, c.inicio AS cita_inicio,
                   c.estado AS cita_estado, cv.tomada_por,
                   -- El nombre para el ÚNICO hueco de una reactivación (hallazgo CRÍTICO,
                   -- ronda 1 de revisión de la tarea 4). `c.nombre_completo` de arriba
                   -- SIEMPRE es NULL para estas filas -toda reactivación tiene `cita_id`
                   -- NULL, o G1 la habría anulado-, así que sin esto `seguimientos.
                   -- parametros_de` caía a su respaldo "paciente" siempre: la firma exacta
                   -- de un mensaje masivo. Dos fuentes más, en cascada
                   -- (`seguimientos._nombre_de_reactivacion` decide el orden):
                   --   1. La ficha de `pacientes`. Solo existe si la persona ya agendó
                   --      alguna vez -`crear_cita` es quien la registra-, así que resuelve
                   --      `TIPO_CANCELADA` y `TIPO_NO_ASISTIO` pero no un lead que nunca
                   --      agendó (`TIPO_SIN_AGENDAR`, el caso normal de la reactivación).
                   --   2. El nombre de perfil de WhatsApp, el ÚNICO dato que existe para
                   --      ese lead: el más reciente que dejó en `mensajes_entrantes`, texto
                   --      libre que la persona escribió ella misma.
                   p.nombre_completo                       AS nombre_ficha,
                   (SELECT me.nombre_perfil
                      FROM mensajes_entrantes me
                     WHERE me.telefono = COALESCE(c.telefono, cv.telefono)
                       AND me.nombre_perfil IS NOT NULL
                     ORDER BY me.recibido_en DESC
                     LIMIT 1)                              AS nombre_perfil,
                   -- La baja comercial. Entra como columna de este SELECT --que ya hace el
                   -- LEFT JOIN para sacar el teléfono-- y no como una consulta por fila: una
                   -- tanda son hasta 50. El COALESCE hace explícito que un número sin fila
                   -- de contacto NO está de baja: el LEFT JOIN devuelve NULL, y NULL no es
                   -- FALSE para un `if`.
                   COALESCE(co.no_contactar, FALSE)       AS no_contactar,
                   -- El freno por persona (regla 5, migración 021). Mismo motivo que la baja:
                   -- sin esta columna, R1 lee `.get(..., 0)` siempre en 0 y pasa sin
                   -- protegerse -- verde por el motivo equivocado, no porque el freno no haga
                   -- falta.
                   COALESCE(co.seguimientos_fallidos, 0)  AS seguimientos_fallidos,
                   -- El tope anual (regla 8). Cuenta REACTIVACIONES enviadas de verdad en los
                   -- últimos 12 meses para este teléfono, excluyendo el recordatorio de cita
                   -- --que no es publicidad y no debe contar para el tope--. Es una subconsulta
                   -- y no otro JOIN porque lo que hace falta es un conteo por teléfono, no una
                   -- fila más por cada envío histórico.
                   (SELECT count(*)
                      FROM seguimientos s2
                      JOIN conversaciones cv2 ON cv2.id = s2.conversacion_id
                     WHERE cv2.telefono = COALESCE(c.telefono, cv.telefono)
                       AND s2.enviado_en IS NOT NULL
                       AND s2.enviado_en > %(ahora)s - interval '12 months'
                       AND s2.tipo <> 'recordatorio_cita')  AS reactivaciones_ultimo_ano,
                   -- R3bis (ronda 2 de revisión de la tarea 3, migración 022).
                   -- `fecha_objetivo` se reescribe en cada aplazamiento; `aplazado_desde` se
                   -- fija SOLO la primera vez que `aplazar_seguimiento` toca la fila (ver su
                   -- COALESCE) y queda NULL en la que nunca se aplazó. Es la referencia
                   -- correcta -a diferencia de `creado_en`, que mide la edad total de la fila
                   -- y no cuánto lleva atascada: ver el comentario de R3bis en `seguimientos.py`.
                   s.aplazado_desde
              FROM seguimientos s
              LEFT JOIN citas c           ON c.id  = s.cita_id
              LEFT JOIN conversaciones cv ON cv.id = s.conversacion_id
              LEFT JOIN contactos co      ON co.telefono = COALESCE(c.telefono, cv.telefono)
              -- `pacientes.telefono` es UNIQUE (migración 001): este LEFT JOIN no multiplica
              -- filas, a lo sumo una coincidencia por teléfono.
              LEFT JOIN pacientes p       ON p.telefono  = COALESCE(c.telefono, cv.telefono)
             WHERE s.enviado_en IS NULL
               AND s.anulado_en IS NULL
               AND s.fecha_objetivo <= %(ahora)s
             ORDER BY s.fecha_objetivo
             LIMIT %(limite)s
               FOR UPDATE OF s SKIP LOCKED
            """,
            {"ahora": ahora, "limite": limite},
        )
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def marcar_seguimiento_enviado(conn, id_seguimiento: int) -> bool:
    """Va ANTES del envío y con su propio commit, y el orden es deliberado.

    No hay transacción que cubra una llamada HTTP a Meta. Si se enviara primero y el proceso
    muriera antes del commit, la fila seguiría pendiente y el barrido de sesenta segundos
    después mandaría el mismo recordatorio otra vez -- sin que ninguna de sus guardas lo
    detectara, porque todas seguirían diciendo que sí.

    Antes marcar y no mandar, que mandar y no marcar.

    Devuelve `True` si ESTA llamada fue la que marcó la fila, `False` si ya la había marcado
    otra. El `WHERE ... AND enviado_en IS NULL` es la comprobación: es lo que sostiene la
    garantía de no duplicado desde el instante en que `seguimientos_por_despachar` suelta el
    candado del lote entero con el primer `commit` de la conexión (ver su docstring) -a partir
    de ahí dos instancias pueden tener la MISMA fila en su lista en memoria, y sin este
    `AND` las dos la marcarían y las dos mandarían. Quien llama tiene que mirar el resultado:
    con `False`, no se manda nada y no se cuenta como enviada -otra instancia ya se la llevó.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = now() WHERE id = %s AND enviado_en IS NULL",
            (id_seguimiento,),
        )
        marcada = cur.rowcount == 1
    conn.commit()
    return marcada


def aplazar_seguimiento(conn, id_seguimiento: int, *, hasta: datetime) -> None:
    """Lo mueve en el tiempo sin gastarlo. Es lo que hacen las guardas del relevo y del horario:
    el motivo por el que no sale ahora deja de ser cierto más tarde.

    También anota `aplazado_desde` (migración 022), con `COALESCE(aplazado_desde, now())` y
    no `now()` a secas: lo que R3bis necesita es la PRIMERA vez que esta fila no pudo salir,
    no la última. `fecha_objetivo` se reescribe en CADA aplazamiento -eso es justo lo que
    hace este UPDATE, dos líneas más abajo- así que con `now()` a secas `aplazado_desde` se
    reiniciaría junto con ella y R3bis quedaría exactamente tan ciega como R3, solo que con
    otro nombre de columna.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE seguimientos
               SET fecha_objetivo = %(hasta)s,
                   aplazado_desde = COALESCE(aplazado_desde, now())
             WHERE id = %(id)s
            """,
            {"hasta": hasta, "id": id_seguimiento},
        )
    conn.commit()


def anular_seguimiento(conn, id_seguimiento: int, *, motivo: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET anulado_en = now(), motivo_anulacion = %s WHERE id = %s",
            (motivo[:200], id_seguimiento),
        )
    conn.commit()


def anotar_fallo_de_seguimiento(conn, id_seguimiento: int, *, fallo: str) -> None:
    """`enviado_en` puesto Y `fallo` con contenido es la señal a vigilar: la fila dice «lo
    intenté» y no «salió». Es el mismo par que `reenviado_en` NULL en `mensajes_entrantes`.

    El motivo se trunca a 2000, igual que `marcar_fallo_respuesta`: un traceback entero no
    tiene por qué ocupar la base de la clínica.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET fallo = %s, intentos = intentos + 1 WHERE id = %s",
            (fallo[:2000], id_seguimiento),
        )
    conn.commit()


def ultimo_mensaje_del_paciente(conn, telefono: str) -> datetime | None:
    """Cuándo escribió ese número por última vez, o `None` si nunca.

    Es la fuente exacta de dos cosas distintas: la guarda de contacto reciente --si está
    hablando con Daniela ahora, recordarle la cita la hace ver desmemoriada-- y la ventana de
    24 h de Meta, que se cuenta desde aquí y no desde `conversaciones.actualizada_en`, que
    también la toca Daniela al responder.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(recibido_en) FROM mensajes_entrantes WHERE telefono = %s",
            (telefono,),
        )
        fila = cur.fetchone()
    return fila[0] if fila else None


def anotar_recordatorio_en_conversacion(
    conn, id_conversacion: str, *, tipo: str, cuando: datetime
) -> None:
    """Para que Daniela sepa a qué dice «sí» el paciente que responde a un recordatorio.

    El mensaje lo mandó un proceso, no una conversación: el historial del agente no lo
    contiene. Se anota aquí y `atencion._leer_estado` lo carga en el contexto. NO se inyecta
    un mensaje en `agent_messages`: esas tablas las fija el SDK.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET ultimo_recordatorio_tipo = %s, ultimo_recordatorio_en = %s
             WHERE id = %s
            """,
            (tipo, cuando, id_conversacion),
        )
    conn.commit()


def ultimo_recordatorio(conn, telefono: str) -> tuple[str, datetime] | None:
    """El último recordatorio que le salió a ese NÚMERO, o `None` si no salió ninguno.

    La lectura que le falta a `anotar_recordatorio_en_conversacion`, y de ella sale el campo
    del contexto que le dice a Daniela a qué contesta un «sí, confirmo» que no tiene
    antecedente en el historial.

    **Va por teléfono y no por `id_conversacion`, y esa es la única forma en que sirve para
    algo.** El despachador anota en la conversación que CREÓ la cita; `atencion._leer_estado`
    lee la conversación VIVA, y `conversacion_viva` tiene una ventana de 24 h. Un recordatorio
    de víspera sale, por definición de su banda, más de 24 h después de la conversación que
    agendó, y `anotar_recordatorio_en_conversacion` no toca `actualizada_en`: cuando el
    paciente responde «sí, confirmo» se abre una conversación NUEVA y la consulta por id
    devolvía `None`. El bloque «YA LE ESCRIBIMOS NOSOTROS» no se emitía JAMÁS para la banda
    mayoritaria -- solo funcionaba en la de 2 h, la única que cabe dentro de la ventana.

    Es el mismo criterio que ya rige `_es_ajena` y `ultimo_mensaje_del_paciente`: **la
    identidad de este proyecto va por teléfono.** El `ORDER BY ... DESC LIMIT 1` es lo que
    convierte «alguna conversación de este número» en «el último», que es lo que se pregunta.

    Devuelve `None` también cuando la fila tiene el tipo pero no la fecha: media verdad aquí
    sería que Daniela creyera que hubo un recordatorio sin saber cuándo, y el «sí» del
    paciente se referiría a algo que no se puede situar en el tiempo. El `IS NOT NULL` del
    `WHERE` va sobre la fecha por lo mismo: ordenar por una columna nula pondría delante una
    conversación sin recordatorio.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ultimo_recordatorio_tipo, ultimo_recordatorio_en
              FROM conversaciones
             WHERE telefono = %s AND ultimo_recordatorio_en IS NOT NULL
             ORDER BY ultimo_recordatorio_en DESC
             LIMIT 1
            """,
            (telefono,),
        )
        fila = cur.fetchone()
    if not fila or not fila[0] or fila[1] is None:
        return None
    return (fila[0], fila[1])


def tratamiento_de_la_consulta_previa(conn, telefono: str) -> str | None:
    """Sobre QUÉ preguntó este número la última vez, o `None` si nunca se supo.

    Solo tiene sentido para quien contesta una REACTIVACIÓN, y por eso `atencion` la llama
    únicamente en ese caso: es una consulta más dentro del candado por teléfono, y el turno
    normal no tiene por qué pagarla.

    **El problema que resuelve.** Una reactivación sale, por la definición de su banda, más
    de 24 h después del último mensaje -- o sea siempre fuera de la ventana de
    `conversacion_viva`. Cuando la persona contesta «Sí, me interesa» se abre una
    conversación NUEVA, con una sesión nueva del SDK y sin una línea de historial. Daniela
    sabía que le habíamos escrito (`ultimo_recordatorio`, que va por teléfono) pero no sobre
    qué, así que lo primero que hacía era preguntarle el tratamiento **a alguien a quien le
    escribimos precisamente porque ya lo había dicho**. Pedirle a la persona que repita lo
    que contó hace una semana es la firma de un mensaje masivo, que es justo la lectura que
    hay que evitar.

    **Sale de `estado_oportunidad` y no del historial**, y esa es la decisión que importa.
    Esa tabla es la memoria larga del diseño --«retomar a una paciente meses después con
    seis campos en vez de ocho meses de chat»--. Rescatar la frase cruda de
    `mensajes_entrantes.texto` habría metido texto libre de hace días en el prompt de hoy,
    sin pasar por los guardrails de entrada de este turno: más valor aparente y una
    superficie que no hace falta abrir.

    **Lo que hace seguro devolver esto: la columna no la escribe el modelo a pelo.** El
    parámetro de la tool es un `str` --no un `Literal`, al contrario que
    `LecturaArchivo.tratamiento`--, pero `herramientas._registrar_estado_oportunidad` lo
    contrasta contra `contratos.vocabulario()` y lanza `ValueError` ANTES de tocar la base,
    así que lo guardado siempre es una clave de la lista, en minúsculas y sin espacios. La
    otra escritura de la tabla (`conversacion._guardar_estado`) ni siquiera manda el campo, y
    el `COALESCE` del upsert conserva el que hubiera. Quien relaje esa validación abre esto:
    el valor viaja al prompt de un turno posterior tal cual.

    Lo que NO garantiza: que el tratamiento siga en el catálogo. La lista está viva, y uno
    que la clínica retiró hace un mes sigue aquí. No hace falta filtrarlo --«la última vez
    preguntó por X» es cierto igual, y el prompt ya le prohíbe a Daniela ofrecer lo que no
    esté en la lista de hoy--, pero conviene no confundir «está validado» con «está vigente».

    `no_identificado` NO cuenta como respuesta (regla dura 12, y el mismo criterio que usa
    `instrucciones_daniela` al listar el vocabulario): es lo que el sistema escribe cuando no
    sabe, y tratarlo como un tratamiento haría que Daniela diera por conocido justo lo que
    nadie llegó a saber. Devolver `None` la deja preguntar, que es lo correcto ahí.

    Va por TELÉFONO, como `ultimo_recordatorio`, `_es_ajena` y `ultimo_mensaje_del_paciente`:
    la identidad de este proyecto va por teléfono, y la conversación donde se anotó el
    tratamiento no es la conversación donde la persona está contestando.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT eo.tratamiento
              FROM estado_oportunidad eo
              JOIN conversaciones c ON c.id = eo.conversacion_id
             WHERE c.telefono = %s
               AND eo.tratamiento IS NOT NULL
               AND eo.tratamiento <> 'no_identificado'
             ORDER BY eo.actualizado_en DESC
             LIMIT 1
            """,
            (telefono,),
        )
        fila = cur.fetchone()
    return fila[0] if fila else None


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


def escalamiento_vivo_con_motivo(
    conn, id_conversacion: str, motivo: str
) -> tuple[int, bool] | None:
    """El escalamiento de ESE motivo que el doctor ya tiene delante y sin responder.

    Devuelve `(telegram_message_id, lo tomó un doctor)`, o `None` si no hay ninguno. Devolvía
    un `bool`, y el `message_id` es lo que permite ir a PREGUNTARLE a Telegram si ese aviso
    sigue existiendo -- ver el tercer punto de las condiciones.

    ------------------------------------------------------------------------------------
    Para qué existe: el escalamiento rancio
    ------------------------------------------------------------------------------------

    `RespuestaDaniela.requiere_escalamiento` lo LEE el orquestador como un flanco --«avisa
    ahora»-- y el modelo lo EMITE como un estado --«esto sigue necesitando a un humano»--.
    Mientras el asunto siga abierto lo deja en `true`, y como la clave de idempotencia es por
    TURNO (y tiene que serlo: congelada, el doctor se enteraría del primer escalamiento y de
    ninguno más), cada turno se convertía en un Telegram nuevo al General.

    Medido el 16/09/2026 sobre +573196842471: cuatro avisos en seis minutos. El turno 2
    escaló de verdad por `clinico`; los turnos 3 y 4 repitieron el mismo motivo mientras
    Daniela solo ofrecía horarios, y cada repetición arrastraba al General el texto íntegro
    de lo que se le había respondido al paciente. El General se leía como la transcripción de
    una conversación que nadie había pedido ver.

    El prompt ya dice «Escalas una vez por asunto, no una vez por mensaje» y no bastó. Por
    eso la guarda vive aquí y no en la obediencia del modelo, igual que la baja comercial del
    no negociable 25.

    ------------------------------------------------------------------------------------
    Las tres condiciones, y qué agujero cierra cada una
    ------------------------------------------------------------------------------------

    - **`telegram_message_id IS NOT NULL`** -- nunca la fila sola. Es la misma regla que
      sostiene `escalamiento_pendiente_de_aviso`: un aviso que no salió no es un aviso
      duplicado. Sin esto, la clave se quemaría al INTENTAR y no al CONSEGUIR, y un 5xx de
      Telegram dejaría al paciente «escalado» en una tabla que nadie mira.
    - **`respondido_en IS NULL`** -- si el doctor ya contestó, ese asunto se cerró y lo que
      venga después es nuevo, aunque el motivo se repita. La escribe `relevo.cerrar`, y solo él
      (17/09/2026): un doctor tomó la conversación, habló con el paciente y la devolvió.
      Antes no la escribía nadie y el silencio duraba las 24 h aunque el asunto estuviera
      atendido EN PERSONA. Y NO vale `relevo_activado`
      en su lugar: en el caso medido el doctor tomó el relevo en el turno 2 y lo devolvió, y
      los turnos 3 y 4 son justo los que hay que callar.
    - **y el aviso TIENE QUE SEGUIR EN EL GENERAL**, cosa que este SQL no puede saber. Por
      eso devuelve el `telegram_message_id` en vez de un `bool`: quien llama le pregunta a
      Telegram con `canales.aviso_sigue_puesto`. **Borrar un mensaje no emite ningún
      evento**, igual que borrar un tema, así que la fila sigue diciendo «lo tiene delante»
      después de que el doctor lo haya borrado -- y eso callaba TODO lo que viniera después
      durante las 24 h de la conversación. Medido el 17/09/2026 sobre el aviso 855.
      El segundo valor, `relevo_activado` (un BOOLEAN, no una marca de tiempo), evita
      sondear un aviso que
      ya sirvió: ahí el teclado del General es el enlace «Ir al hilo», y la sonda se lo
      cambiaría por el botón de tomar una conversación que alguien ya tiene.

    ------------------------------------------------------------------------------------
    Cuánto silencia esto de verdad, dicho para que nadie lo descubra solo
    ------------------------------------------------------------------------------------

    Con `respondido_en` sin escribir, el alcance real es: **un aviso de la red de seguridad
    por motivo y por conversación**, y una conversación caduca a las 24 h sin contacto
    (`conversacion_viva`). No es «uno para siempre».

    Y sobre todo, **no toca la tool**. `herramientas._escalar_a_doctores` escribe su fila y
    manda su propio Telegram sin pasar por `runtime._registrar_escalamiento`: si el modelo
    escala algo de verdad nuevo --el paciente pasa de dolor a dificultad para tragar--
    llamando a la tool, ese aviso sale siempre. Lo único que se calla es el flanco que el
    modelo dejó encendido sin llamar a nadie. Ahí está la frontera, y es la que sostiene que
    esto no compita con la seguridad clínica.
    - **mismo `motivo`** -- un `dato_faltante` detrás de un `clinico` es otro asunto y pasa.
      En el caso medido, esto habría callado los dos avisos del medio y conservado el
      primero y el último, que son los que decían algo.

    Lo que NO toca: la tool. `escalar_a_doctores` escribe su fila y manda su propio Telegram
    con un resumen de verdad; `_avisar_a_doctores` corre después, encuentra la clave usada y
    ya se callaba sola. Lo único que esta consulta silencia es la red de seguridad repitiendo
    un aviso que el doctor tiene delante sin responder.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_message_id, COALESCE(relevo_activado, FALSE) FROM escalamientos "
            "WHERE conversacion_id = %s AND motivo = %s "
            "  AND telegram_message_id IS NOT NULL AND respondido_en IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (id_conversacion, motivo),
        )
        fila = cur.fetchone()
        return (fila[0], bool(fila[1])) if fila else None


def marcar_escalamientos_respondidos(conn, id_conversacion: str) -> int:
    """Da por respondidos los escalamientos vivos de esa conversación. Devuelve cuántos.

    La escribe UNA sola cosa: `relevo.cerrar`. Un doctor tomó la conversación, habló con el
    paciente y la devolvió -- eso es exactamente lo que `respondido_en` significa desde la
    migración 001, y hasta el 17/09/2026 no la ponía nadie.

    ------------------------------------------------------------------------------------
    Qué se rompía sin esto
    ------------------------------------------------------------------------------------

    `escalamiento_vivo_con_motivo` pregunta por `respondido_en IS NULL`. Con la columna
    muerta, un escalamiento que un humano ya había atendido y cerrado seguía contando como
    «delante del doctor y sin responder» durante las 24 h que vive la conversación, y callaba
    todo aviso posterior del mismo motivo. Y no solo el aviso: `runtime._avisar_a_doctores`
    sale antes de llamar a `lectura.rescatar_hilo`, así que al callarse el aviso **tampoco se
    recreaba el hilo del paciente**. El doctor que había borrado el hilo se quedaba sin las
    dos puertas a la vez.

    Medido el 17/09/2026 sobre +573196842471: relevo tomado por `dato_faltante` y devuelto a
    los 100 segundos. Lo que salvó el caso fue que el escalamiento siguiente cambió de motivo.

    ------------------------------------------------------------------------------------
    Por qué esto NO reabre el ruido que se calló el día antes
    ------------------------------------------------------------------------------------

    `escalamiento_vivo_con_motivo` advierte que «NO vale `relevo_activado` en su lugar», y
    tiene razón: esa columna se pone al TOMAR. Esta se pone al CERRAR, que es otra cosa --el
    ciclo completo, con un humano decidiendo que terminó--. Contrastado contra las filas del
    caso medido (conversación 5807c84b, escalamientos 99/101/103/105): el ruido del 16/09 se
    produjo con el relevo todavía sin cerrar o sin haber existido, así que esta marca no lo
    habría dejado pasar. Lo único que devuelve es lo que llega DESPUÉS del cierre.

    `respondido_en IS NULL` en el `WHERE` y no un `UPDATE` a secas: el cierre puede correr dos
    veces --las salidas del relevo se solapan-- y la marca tiene que conservar la hora del
    primero, que es cuando el asunto se atendió de verdad.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE escalamientos SET respondido_en = now() "
            " WHERE conversacion_id = %s AND respondido_en IS NULL",
            (id_conversacion,),
        )
        marcados = cur.rowcount
    conn.commit()
    return marcados


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

#: Cuántas FRASES de ese teléfono hay guardadas. Va con el `UPDATE` de abajo y siempre
#: ANTES: después, el teléfono ya no está en ninguna parte y la cuenta daría cero.
#:
#: Existe porque el número que devuelve el borrado sale por WhatsApp diciendo «frases», y el
#: `rowcount` del `UPDATE` cuenta FILAS. Un paciente que preguntó dos veces lo mismo deja dos
#: entradas en `ejemplos` del MISMO caso: se le borraban dos y se le decía «1 frase tuya».
#:
#: Dos sentencias y no una sola con CTE, a propósito: en un `UPDATE ... FROM cte`, el valor
#: nuevo se calcula con la instantánea de la consulta, así que un `registrar_caso` de otro
#: turno que entrara entre medias se perdería. La forma de abajo --el `SET` que se lee a sí
#: mismo-- la vuelve a evaluar sobre la fila que acaba de bloquear, y no pierde nada. Entre
#: las dos sentencias cabe una desviación de la CUENTA, nunca del borrado; y las dos van en
#: la misma transacción.
_CONTAR_EJEMPLOS_DEL_TELEFONO = """
    SELECT count(*)::int
      FROM casos_sin_resolver c, json_array_elements(c.ejemplos::json) AS e
     WHERE e ->> 'telefono' = %(tel)s
"""

#: Quitar las frases de un teléfono de `casos_sin_resolver`, sin tocar el contador.
#:
#: Vive en una constante y no dentro de una función porque lo usan DOS: `borrar_rastro`, que
#: es `/clearstate`, y `olvidar_ejemplos_de`, que lo expone suelto para el mantenimiento.
#: Con dos copias, afinar el filtro en una deja a la otra contando filas que no cambió.
#:
#: **El `WHERE` es un EXISTS y no un `LIKE`, y esa diferencia la lee un paciente.** El
#: conteo que devuelve esto sale por WhatsApp en la confirmación del reseteo, así que tiene
#: que ser el de las filas que CAMBIARON. `ejemplos LIKE '%573001110001%'` cuenta las que
#: COINCIDEN, que es otra cosa: casa con un número del que este es prefijo, y casa con un
#: número escrito DENTRO del `texto` de la frase de otro paciente. En los dos casos el
#: `UPDATE` no cambiaba nada y el paciente recibía «1 frase tuya» igual.
_OLVIDAR_EJEMPLOS_DEL_TELEFONO = """
    UPDATE casos_sin_resolver SET ejemplos = (
        -- `WITH ORDINALITY` numera cada elemento en el orden en que salió del arreglo, y el
        -- `ORDER BY` sobre esa columna es lo que garantiza que el reagrupado después del
        -- filtro conserva el orden cronológico.
        SELECT coalesce(json_agg(v.elem ORDER BY v.n)::text, '[]')
          FROM json_array_elements(ejemplos::json) WITH ORDINALITY AS v(elem, n)
         WHERE v.elem ->> 'telefono' IS DISTINCT FROM %(tel)s
    )
    WHERE EXISTS (
        SELECT 1 FROM json_array_elements(ejemplos::json) AS e
         WHERE e ->> 'telefono' = %(tel)s
    )
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
            "SELECT topic_id FROM temas_telegram WHERE telefono = %(tel)s",
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


# ==========================================================================================
# La persona del teléfono, y lo que ha decidido sobre sus datos (migración 019)
# ==========================================================================================


#: Las columnas de `contactos`, en un solo sitio. Las dos lecturas devuelven un `dict` con
#: exactamente estas claves, así que quien las consuma no depende del orden del SELECT.
_COLUMNAS_CONTACTO = (
    "telefono, creado_en, actualizado_en, aviso_mostrado_en, politica_version, "
    "no_contactar, no_contactar_en, no_contactar_origen, "
    "seguimientos_fallidos, ultimo_seguimiento_en"
)


def _fila_contacto(cur) -> dict[str, Any] | None:
    fila = cur.fetchone()
    if fila is None:
        return None
    return dict(zip([d[0] for d in cur.description], fila))


def asegurar_contacto(conn, telefono: str) -> dict[str, Any]:
    """La fila de ese número, creándola en blanco si no estaba. Nunca falla por existir.

    Nace sin aviso y sin baja: contactable, porque la baja es algo que el paciente pide y
    nunca un default. Tener fila aquí NO significa estar verificado -- eso lo sigue diciendo
    la existencia de la fila en `pacientes` (no negociable 12).

    `ON CONFLICT DO NOTHING` y no un `UPDATE`: dos mensajes del mismo número pueden entrar a
    la vez y el candado de `atencion` es de proceso, no protege entre réplicas.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO contactos (telefono) VALUES (%s) ON CONFLICT (telefono) DO NOTHING",
            (telefono,),
        )
        cur.execute(
            f"SELECT {_COLUMNAS_CONTACTO} FROM contactos WHERE telefono = %s", (telefono,)
        )
        fila = _fila_contacto(cur)
    conn.commit()
    if fila is None:
        # Imposible salvo bug: o la insertó esta llamada, o ya estaba. Un `assert` no vale
        # --`python -O` los borra-- y devolver `None` dejaría a `_leer_estado` reventando
        # más tarde con un `TypeError` sin relación aparente con la causa.
        raise RuntimeError(f"no se pudo asegurar el contacto de {telefono}")
    return fila


def leer_contacto(conn, telefono: str) -> dict[str, Any] | None:
    """Lo que ese número ha decidido, o `None` si nunca ha escrito. NO crea la fila.

    Hoy no la llama nadie en producción, y queda igual a propósito: es la lectura natural de
    esta tabla y lo que usan las pruebas para comprobar el estado sin depender de la función
    que lo escribió. Quien busque el camino real: el despachador saca `no_contactar` como una
    columna más del `LEFT JOIN` de `seguimientos_por_despachar` --una tanda son hasta 50
    filas y no puede hacer una consulta por cada una--, `atencion._leer_estado` lo saca del
    `asegurar_contacto` que ya hace, y las dos tools de privacidad escriben con `pedir_baja`
    y `revocar_baja`, que no necesitan leer antes porque su UPDATE es condicional.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS_CONTACTO} FROM contactos WHERE telefono = %s", (telefono,)
        )
        return _fila_contacto(cur)


def anotar_consentimiento(
    conn,
    telefono: str,
    *,
    evento: str,
    origen: str,
    version: str | None = None,
    detalle: str | None = None,
) -> None:
    """Una línea en la bitácora. Nada la borra, y solo una cosa la modifica.

    Esa cosa es `borrar_rastro`, que pone `detalle` en NULL --la frase del paciente-- y deja
    intacto todo lo demás. Es el derecho de supresión de la política §13, y es la única
    excepción: ninguna fila se va, ningún otro campo cambia.

    No hace `commit()` a propósito -- las tres funciones de abajo la llaman dentro de su
    propia transacción, para que el estado y su rastro entren o no entren juntos. Un estado
    cambiado sin rastro es exactamente lo que esta tabla existe para impedir.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO consentimientos (telefono, evento, origen, politica_version, detalle)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (telefono, evento, origen, version, (detalle or None) and detalle[:500]),
        )


def marcar_aviso_mostrado(conn, telefono: str, *, version: str) -> None:
    """Deja constancia de que a este número se le enseñó la política, y cuál.

    Se llama DESPUÉS de que el envío haya salido bien, nunca antes: ver el comentario de
    `atencion` donde se usa. Es al revés que un recordatorio (no negociable 21) y es
    deliberado.

    Empieza por `asegurar_contacto`: sin fila padre, el `INSERT` de `anotar_consentimiento`
    de abajo viola la FK de `consentimientos` y tumba la transacción ENTERA de `conn`, no
    solo este `UPDATE` -- y con ella, cualquier otro trabajo pendiente de ese turno.
    """
    asegurar_contacto(conn, telefono)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET aviso_mostrado_en = now(), politica_version = %s, actualizado_en = now()
             WHERE telefono = %s
            """,
            (version, telefono),
        )
    anotar_consentimiento(
        conn, telefono, evento="aviso_mostrado", origen="codigo", version=version
    )
    conn.commit()


def pedir_baja(
    conn, telefono: str, *, origen: str = "paciente", detalle: str | None = None
) -> bool:
    """Apaga las comunicaciones COMERCIALES de ese número. Devuelve si el estado cambió.

    Nunca apaga el recordatorio de una cita: eso lo garantiza la lista blanca de
    `seguimientos.TIPOS_NO_COMERCIALES`, no esta función. Pedir que no te manden publicidad
    no es renunciar a que te avisen de tu propia cita, y confundir las dos cosas deja a un
    paciente sin llegar a la clínica.

    La bitácora se escribe SIEMPRE, aunque el estado ya estuviera puesto: pedirlo dos veces
    son dos hechos distintos, y los dos ocurrieron.

    Empieza por `asegurar_contacto`: sin fila padre, el `INSERT` de `anotar_consentimiento`
    de abajo viola la FK de `consentimientos` y tumba la transacción ENTERA de `conn`, no
    solo este `UPDATE` -- y con ella, cualquier otro trabajo pendiente de ese turno. Con la
    fila asegurada, un número que nunca había escrito sí cambia de estado: devuelve `True`.
    """
    asegurar_contacto(conn, telefono)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET no_contactar = TRUE, no_contactar_en = now(),
                   no_contactar_origen = %s, actualizado_en = now()
             WHERE telefono = %s AND no_contactar = FALSE
            """,
            (origen, telefono),
        )
        cambio = cur.rowcount > 0
    anotar_consentimiento(
        conn, telefono, evento="baja_solicitada", origen=origen, detalle=detalle
    )
    conn.commit()
    return cambio


def sumar_seguimiento_fallido(conn, telefono: str, *, commit: bool = True) -> int:
    """Suma uno al contador de ENVÍOS de reactivación que no sirvieron. Devuelve el nuevo
    valor.

    **Ronda 3 de revisión: este docstring decía «contador de series» hasta ahora** -- el
    tipo exacto de mentira que llevó a la ronda 2 a diseñar (y a la ronda 1 antes, con otro
    intento) un mecanismo para contar "series" que este sistema nunca tuvo dónde anclar. Ver
    el docstring de `persistencia.DIAS_ENTRE_INTENTOS_DE_REACTIVACION` para la historia
    completa: el contador cuenta ENVÍOS, uno por uno, determinista.

    NO es la baja y no se le parece: esto lo decide el sistema --«a este número no le sirve que
    lo persigamos»-- y `no_contactar` lo decide la persona. Por eso esto vive solo en
    `contactos`, no escribe una línea en `consentimientos`, y `/clearstate` SÍ lo resetea.

    El `WHERE` va por teléfono y no tiene vuelta atrás: aflojarlo apaga el seguimiento de la
    cartera entera. `test_el_contador_no_alcanza_a_otro_telefono` lo vigila.

    Empieza por `asegurar_contacto`, igual que sus hermanas `pedir_baja` y `revocar_baja`: sin
    fila padre, el `UPDATE` de abajo no toca ninguna fila y la llamada se pierde en silencio.
    En WhatsApp no muerde hoy porque `atencion._leer_estado` ya asegura el contacto antes de
    llamar al modelo, pero el chat web del panel (`runtime.py`) NO pasa por ahí: sin este
    `asegurar_contacto`, la primera vez que alguien prueba «ya no, gracias» desde el panel
    Daniela confirmaría el cierre y el contador se quedaría en cero -- un freno que se da por
    ejercitado sin haberlo sido.

    `asegurar_contacto` hace su propio `commit()` INCONDICIONAL. **Corrección (tarea 7, Ruling
    D5): esto NO es inocuo en todos los casos**, al revés de lo que decía una versión anterior
    de este párrafo. Es inocuo cuando esta función se llama sola --asegurar que la fila exista
    es una precondición idempotente-- pero deja de serlo en cuanto hay otra escritura
    pendiente en la MISMA conexión antes de llamar aquí con `commit=False`: ese `commit()`
    interno confirma TODO lo que estuviera pendiente en `conn`, no solo la fila de
    `contactos`.

    Por eso `commit=False` es para `barrido._contabilizar_envios_vencidos` (tarea 7), que
    tiene que subir este contador y marcar `seguimientos.contabilizado_en` como una sola
    unidad, y el ORDEN importa: quien llame aquí con `commit=False` tiene que llamar a
    `marcar_envio_contabilizado` DESPUÉS, sobre la MISMA conexión, y dejar que sea el
    `commit()` de esa segunda llamada el que confirme las dos escrituras juntas. Al revés
    --marcar primero, sumar después-- el `commit()` interno de `asegurar_contacto` confirmaría
    la marca SOLA, y una caída justo ahí deja la fila con `contabilizado_en` puesto pero el
    contador SIN SUBIR: el envío fallido queda marcado como ya contado sin haberlo sido nunca,
    y no hay ninguna pasada futura que la vuelva a mirar -- el agujero exacto que la columna
    `contabilizado_en` existe para tapar.
    """
    asegurar_contacto(conn, telefono)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET seguimientos_fallidos = seguimientos_fallidos + 1,
                   ultimo_seguimiento_en = now(),
                   actualizado_en = now()
             WHERE telefono = %s
         RETURNING seguimientos_fallidos
            """,
            (telefono,),
        )
        fila = cur.fetchone()
    if commit:
        conn.commit()
    if fila is None:
        # Imposible salvo bug, con el `asegurar_contacto` de arriba ya hecho: o la fila
        # existía, o se acaba de crear. Devolver 0 aquí confundiría «no había fila» con «el
        # contador de verdad está en cero», que es justo la ambigüedad que el hallazgo I3
        # señaló -- mejor reventar alto que mentir sobre cuántas veces se le insistió a
        # alguien.
        raise RuntimeError(f"no se pudo sumar el seguimiento fallido de {telefono}")
    return fila[0]


def reiniciar_seguimientos_fallidos(conn, telefono: str, *, commit: bool = True) -> None:
    """Devuelve el contador a cero. Lo llama `crear_cita`: alguien que ignoró dos veces y al
    final vino demostró lo contrario de lo que el contador supone.

    `commit=False` para que el reset viaje en la MISMA transacción que la cita, igual que el
    recordatorio. Si la cita se deshace, el reset se deshace con ella.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET seguimientos_fallidos = 0, actualizado_en = now()
             WHERE telefono = %s AND seguimientos_fallidos > 0
            """,
            (telefono,),
        )
    if commit:
        conn.commit()


def revocar_baja(
    conn, telefono: str, *, origen: str = "paciente", detalle: str | None = None
) -> bool:
    """Vuelve a permitir lo comercial. Devuelve si el estado cambió.

    Que el paciente escriba de nuevo NO llama a esto: la baja solo se levanta si la persona
    lo pide. La bitácora conserva las dos decisiones con sus fechas, que es lo que hace el
    historial acreditable.

    Empieza por `asegurar_contacto`: sin fila padre, el `INSERT` de `anotar_consentimiento`
    de abajo viola la FK de `consentimientos` y tumba la transacción ENTERA de `conn`, no
    solo este `UPDATE` -- y con ella, cualquier otro trabajo pendiente de ese turno.
    """
    asegurar_contacto(conn, telefono)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contactos
               SET no_contactar = FALSE, no_contactar_en = NULL,
                   no_contactar_origen = NULL, actualizado_en = now()
             WHERE telefono = %s AND no_contactar = TRUE
            """,
            (telefono,),
        )
        cambio = cur.rowcount > 0
    anotar_consentimiento(
        conn, telefono, evento="baja_revocada", origen=origen, detalle=detalle
    )
    conn.commit()
    return cambio


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

    `contactos` es la única tabla que este borrado NO borra. Se le resetea el aviso y nada
    más: el `no_contactar` sobrevive, porque es una decisión del paciente y no un estado del
    sistema. La bitácora `consentimientos` no se toca en ningún caso -- la FK con ON DELETE
    RESTRICT lo hace imposible aunque alguien lo intente.
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

            # Las frases que este número dejó en el informe de «sin resolver». Es el único
            # sitio del borrado que NO borra una fila: el caso vive --«doce personas
            # preguntaron el precio de la ortodoncia» sigue siendo cierto-- y lo que se va es
            # la frase textual, que es lo único suyo que hay ahí dentro. Por eso el contador
            # no baja: es historia de la clínica, no dato del paciente.
            #
            # Va aquí dentro y no en una llamada aparte por la TRANSACCIÓN, no por el orden:
            # el teléfono de una frase viaja dentro del propio JSON de `ejemplos`, así que
            # después del DELETE de `conversaciones` se seguiría sabiendo cuáles borrar --a
            # diferencia de `agent_sessions`, aquí arriba, donde el orden sí es obligatorio.
            # Lo que no se puede es dejarlo fuera: un `UPDATE` en su propia transacción
            # triunfaría mientras la purga revienta y se deshace, y el reseteo quedaría a
            # medias justo por la mitad que nadie mira.
            cur.execute(_CONTAR_EJEMPLOS_DEL_TELEFONO, parametros)
            fila = cur.fetchone()
            borradas["casos_sin_resolver"] = fila[0] if fila else 0
            cur.execute(_OLVIDAR_EJEMPLOS_DEL_TELEFONO, parametros)

            cur.execute(
                f"DELETE FROM conversaciones WHERE id IN ({_CONVERSACIONES_DEL_TELEFONO})",
                parametros,
            )
            borradas["conversaciones"] = cur.rowcount

            cur.execute("DELETE FROM pacientes WHERE telefono = %(tel)s", parametros)
            borradas["pacientes"] = cur.rowcount

            # El hilo de Telegram. Desde la migración 014 vive en su propia tabla y **no
            # cuelga de `conversaciones` ni de `pacientes`**, así que no hay CASCADE que se lo
            # lleve: antes desaparecía solo, al caer la fila del paciente donde era una
            # columna. Una fila que sobreviviera apuntaría a un tema que `reseteo` acaba de
            # borrar en Telegram, y el siguiente archivo de ese número moriría con
            # «message thread not found» -- una radiografía perdida por una fila de más.
            cur.execute("DELETE FROM temas_telegram WHERE telefono = %(tel)s", parametros)
            borradas["temas_telegram"] = cur.rowcount

            # La excepción de `/clearstate`, y el punto entero de la migración 019. Se resetea
            # el aviso --lo volverá a ver, que es lo correcto en un reseteo-- pero el
            # `no_contactar` NO se toca, y la fila NO se borra: si se fueran, resetear a
            # alguien lo devolvería a la lista de contactables sin que nadie se enterara.
            # Mismo criterio que los ejemplos de `casos_sin_resolver`, que se borran sin bajar
            # el contador (no negociable 22).
            #
            # Este conteo NO entra en `borradas`, a propósito: `reseteo.Borrado.filas` --y su
            # `total_filas`-- alimentan la rama «no había nada que borrar» de
            # `reseteo.confirmacion`. Resetear el aviso no es un borrado, y contarlo como uno
            # dejaría esa rama inalcanzable en cuanto todo número conocido tenga fila en
            # `contactos` -- el reseteo pasaría a "borrar" siempre al menos 1, y el paciente
            # oiría «borré todo lo tuyo» sobre un número que ya estaba limpio.
            # `seguimientos_fallidos` y `ultimo_seguimiento_en` van en el mismo `UPDATE`: ese
            # contador es del SISTEMA, no de la persona -- lo contrario exacto del
            # `no_contactar` de dos párrafos más abajo, que es de la persona y por eso NO se
            # toca aquí. Tampoco entra en `borradas`, por la misma razón que el aviso: no es
            # un borrado, y contarlo falsearía la rama «no había nada que borrar».
            cur.execute(
                """
                UPDATE contactos
                   SET aviso_mostrado_en = NULL, politica_version = NULL,
                       seguimientos_fallidos = 0, ultimo_seguimiento_en = NULL,
                       actualizado_en = now()
                 WHERE telefono = %(tel)s
                """,
                parametros,
            )

            # La frase que el paciente dejó al pedir la baja, y lo ÚNICO de esta tabla que
            # es suyo: `detalle` lo escribe el modelo copiando lo que la persona dijo. Todo
            # lo demás --qué evento fue, cuándo, quién lo pidió, sobre qué versión de la
            # política-- es el hecho, y el hecho es justo lo que la Ley 1581 pide poder
            # acreditar. Así que se redacta la frase y se conserva la fila.
            #
            # Sin esto, la política publicada prometía en su §13 el derecho a «solicitar la
            # supresión de datos» y el esquema lo impedía: la FK es `ON DELETE RESTRICT` y
            # ninguna ruta de borrado tocaba la bitácora. Un documento que promete lo que el
            # sistema no puede cumplir es peor que no prometerlo.
            #
            # El `WHERE` va por teléfono y esto NO tiene vuelta atrás: aflojarlo se lleva por
            # delante la frase de otra persona y no hay de dónde recuperarla.
            # `test_la_redaccion_no_alcanza_a_otro_telefono` lo vigila.
            #
            # Tampoco entra en `borradas`, por lo mismo que el reseteo del aviso de aquí
            # arriba: no se borró ninguna fila, y contarlo como borrada le diría al paciente
            # que se retiró algo que sigue ahí.
            cur.execute(
                """
                UPDATE consentimientos
                   SET detalle = NULL
                 WHERE telefono = %(tel)s
                   AND detalle IS NOT NULL
                """,
                parametros,
            )

            # El `WHERE EXISTS` es obligatorio: `consentimientos.telefono` tiene una FK, y un
            # `/clearstate` sobre un número que nunca escribió la violaría y tumbaría la
            # transacción entera -- dejando el reseteo a medias justo por la mitad que nadie
            # mira. El origen es 'codigo' y no 'clinica': lo dispara `/clearstate`, que es el
            # sistema actuando sobre un número de prueba, no alguien de MaxiCare.
            cur.execute(
                """
                INSERT INTO consentimientos (telefono, evento, origen)
                SELECT %(tel)s, 'rastro_borrado', 'codigo'
                 WHERE EXISTS (SELECT 1 FROM contactos WHERE telefono = %(tel)s)
                """,
                parametros,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return borradas


# ==========================================================================================
# Lo que Daniela no pudo resolver
# ==========================================================================================


def registrar_caso(
    conn, *, huella: str, tipo: str, escalo: int = 0,
    ejemplo: str | None = None, telefono: str = "",
) -> None:
    """Suma uno al caso de esa huella, o lo crea. UN solo viaje a la base.

    El recorte a cinco ejemplos se hace en SQL y no leyendo-modificando-escribiendo en
    Python a proposito: entre el SELECT y el UPDATE cabe el turno de otro paciente, y esa
    carrera se come un ejemplo cada vez que dos personas preguntan lo mismo a la vez. Con
    `ON CONFLICT ... DO UPDATE` el recorte ocurre dentro de la misma sentencia atomica.

    `escalo` se SUMA, no se pisa: es la metrica que le dice a la clinica cuanto le costo en
    interrupciones al doctor la ficha que falta.

    Es INSTRUMENTACION: no puede tumbar nada del turno clinico que la llama. Si el `INSERT`
    revienta -- un `tipo` fuera del CHECK, lo que sea -- se hace `rollback` antes de dejar
    subir la excepcion. Sin eso, la conexion del llamador (`atencion._anotar_resultado`,
    `relevo.activar`, las dos con escrituras clinicas ya hechas en esa misma conexion) queda
    con la transaccion abortada, y toda sentencia posterior sobre ella muere con «current
    transaction is aborted», aunque no tenga nada que ver con este caso.
    """
    from .sin_resolver import MAX_EJEMPLOS

    nuevo = (
        json.dumps([{"texto": ejemplo.strip(), "telefono": telefono}], ensure_ascii=False)
        if ejemplo and ejemplo.strip()
        else "[]"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO casos_sin_resolver (huella, tipo, escalo, ejemplos)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (huella) DO UPDATE SET
                    contador   = casos_sin_resolver.contador + 1,
                    escalo     = casos_sin_resolver.escalo + EXCLUDED.escalo,
                    ultima_vez = now(),
                    ejemplos   = (
                        -- La concatenacion `||` de arreglos solo existe para `jsonb`, no
                        -- para `json` (Postgres: "operator does not exist: json || json").
                        -- La columna sigue siendo TEXT -- el cast a `jsonb` es solo para
                        -- esta cuenta, y el resultado vuelve a `::text` antes de guardarse.
                        -- `WITH ORDINALITY` y no `row_number() OVER ()`: el segundo no
                        -- lleva `ORDER BY` y su orden no lo garantiza nada por contrato,
                        -- justo en la cuenta que decide QUE cinco ejemplos sobreviven.
                        -- `WITH ORDINALITY` numera en el orden en que salieron del arreglo,
                        -- que es el cronologico. Es el mismo mecanismo que usa
                        -- `_OLVIDAR_EJEMPLOS_DEL_TELEFONO`.
                        SELECT coalesce(jsonb_agg(e.v ORDER BY e.n)::text, '[]')
                          FROM jsonb_array_elements(
                                casos_sin_resolver.ejemplos::jsonb || EXCLUDED.ejemplos::jsonb
                               ) WITH ORDINALITY AS e(v, n)
                         WHERE e.n > greatest(
                            0,
                            jsonb_array_length(
                                casos_sin_resolver.ejemplos::jsonb || EXCLUDED.ejemplos::jsonb
                            ) - %s
                         )
                    )
                """,
                (huella, tipo, escalo, nuevo, MAX_EJEMPLOS),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def casos_recientes(conn, *, dias: int = 30, limite: int = 50) -> list[dict[str, Any]]:
    """La ventana que ve la pantalla: lo de los ultimos `dias`, lo que mas paso primero.

    La ventana ES el mecanismo de limpieza. Lo que se arregla deja de acumular, sale de
    `dias`, y se hunde solo -- sin que nadie marque nada como resuelto. Por eso esta pantalla
    no tiene botones: no le hacen falta.

    El telefono se filtra AQUI y no en la pantalla, para que ni siquiera viaje al navegador.

    De solo lectura, pero con el mismo `rollback` que la escritura: un `SELECT` que revienta
    deja la transaccion de la conexion tan abortada como un `INSERT`, y esta funcion puede
    correr sobre una conexion que el llamador siga usando despues.
    """
    from .sin_resolver import sin_telefonos

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT huella, tipo, contador, escalo, primera_vez, ultima_vez, ejemplos, "
                "       informe "
                "  FROM casos_sin_resolver "
                " WHERE ultima_vez > now() - make_interval(days => %s) "
                " ORDER BY contador DESC, ultima_vez DESC "
                " LIMIT %s",
                (max(1, min(dias, 365)), max(1, min(limite, 200))),
            )
            return [
                {
                    "huella": huella, "tipo": tipo, "contador": contador, "escalo": escalo,
                    "primera_vez": primera.isoformat(), "ultima_vez": ultima.isoformat(),
                    "ejemplos": sin_telefonos(json.loads(ejemplos)),
                    "informe": json.loads(informe) if informe else None,
                }
                for huella, tipo, contador, escalo, primera, ultima, ejemplos, informe
                in cur.fetchall()
            ]
    except Exception:
        conn.rollback()
        raise


def casos_sin_informe(conn, *, limite: int = 5, dias: int = 30) -> list[dict[str, Any]]:
    """Los que esperan informe. Un caso se analiza UNA vez.

    Se re-analiza solo si crecio por cinco Y pasaron siete dias: que el contador suba de 12 a
    40 no tiene por que costar otra llamada, porque el informe seguiria diciendo lo mismo.

    Y solo dentro de la MISMA ventana que ve la pantalla. Un caso de hace noventa dias con
    `informe IS NULL` pagaba su llamada al modelo para un informe que `casos_recientes` no va
    a mostrar nunca. La ventana es la que hunde lo que dejo de pasar; analizarlo era pagar
    por escribirle un informe a algo que ya se hundio.

    De solo lectura, pero con el mismo `rollback` que la escritura: ver `casos_recientes`.
    """
    from .sin_resolver import sin_telefonos

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT huella, tipo, contador, escalo, primera_vez, ultima_vez, ejemplos "
                "  FROM casos_sin_resolver "
                " WHERE ultima_vez > now() - make_interval(days => %s) "
                "   AND (informe IS NULL "
                "        OR (contador >= informe_sobre * 5 AND informe_en < now() - interval "
                "            '7 days')) "
                " ORDER BY contador DESC "
                " LIMIT %s",
                (max(1, min(dias, 365)), max(1, min(limite, 50))),
            )
            return [
                {
                    "huella": huella, "tipo": tipo, "contador": contador, "escalo": escalo,
                    "primera_vez": primera.isoformat(), "ultima_vez": ultima.isoformat(),
                    "ejemplos": sin_telefonos(json.loads(ejemplos)),
                }
                for huella, tipo, contador, escalo, primera, ultima, ejemplos in cur.fetchall()
            ]
    except Exception:
        conn.rollback()
        raise


def guardar_informe(conn, *, huella: str, informe: dict[str, Any], sobre: int) -> None:
    """Deja el informe y el contador sobre el que se escribio.

    Instrumentacion, igual que `registrar_caso`: si el `UPDATE` revienta, `rollback` antes de
    dejar subir la excepcion, para no dejar la conexion del llamador con la transaccion
    abortada.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE casos_sin_resolver "
                "   SET informe = %s, informe_en = now(), informe_sobre = %s "
                " WHERE huella = %s",
                (json.dumps(informe, ensure_ascii=False), sobre, huella),
            )
            if cur.rowcount == 0:
                log.warning("guardar_informe: la huella %s ya no existe", huella)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def olvidar_ejemplos_de(conn, telefono: str) -> int:
    """Quita las frases de ese telefono de todos los casos. Devuelve cuantas FRASES quito.

    `/clearstate` NO pasa por aqui: corre el mismo SQL --`_OLVIDAR_EJEMPLOS_DEL_TELEFONO`,
    la constante que las dos comparten-- desde dentro de `borrar_rastro`, para que el olvido
    caiga en la unica transaccion del borrado. Esta funcion sobrevive suelta para el
    mantenimiento: quitar las frases de un numero sin desmontarle la conversacion.

    El contador NO baja, a proposito. El conteo es historia de la clinica, no dato del
    paciente: «doce personas preguntaron por ortodoncia» sigue siendo cierto aunque se borre
    una de esas conversaciones.

    Instrumentacion tambien, igual que `registrar_caso`: `rollback` si el `UPDATE` revienta.
    """
    if not telefono:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(_CONTAR_EJEMPLOS_DEL_TELEFONO, {"tel": telefono})
            fila = cur.fetchone()
            cuantas = fila[0] if fila else 0
            cur.execute(_OLVIDAR_EJEMPLOS_DEL_TELEFONO, {"tel": telefono})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cuantas


# ==========================================================================================
# El perimetro: contar el gasto y contar las cuotas
# ==========================================================================================
#
# Migracion 022. Las tres tablas y el indice estan explicados alli; aqui va solo lo que el
# codigo tiene que saber para usarlas.
#
# Lo que une a todas estas funciones: **ninguna puede tumbar un turno**. Son instrumentacion y
# frenos, no atencion al paciente. Quien las llama lo hace dentro de un `try` que traga, con
# el mismo criterio que `_anotar_resultado`: si la contabilidad revienta se pierde una fila,
# nunca una respuesta. Por eso aqui SI se deja propagar --el que llama decide-- y por eso
# ninguna hace `conn.commit()` sin su `rollback` correspondiente.


def anotar_consumo(
    conn,
    *,
    agente: str,
    modelo: str,
    llamadas: int,
    tokens_entrada: int,
    tokens_entrada_cacheados: int,
    tokens_salida: int,
    costo_usd: float,
    id_conversacion: str | None = None,
    telefono: str | None = None,
) -> None:
    """Una fila por corrida del modelo. Ver `consumo_modelo` en la 022.

    El desglose por `agente` es lo que hace accionable el numero: un pico en `lector` y uno en
    `daniela` se arreglan de forma distinta --el primero es alguien mandando archivos, el
    segundo alguien conversando-- y sumados en una sola cifra serian indistinguibles.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO consumo_modelo (
                    agente, modelo, id_conversacion, telefono,
                    llamadas, tokens_entrada, tokens_entrada_cacheados, tokens_salida,
                    costo_usd
                ) VALUES (
                    %(agente)s, %(modelo)s, %(conv)s, %(tel)s,
                    %(llamadas)s, %(entrada)s, %(cacheados)s, %(salida)s,
                    %(costo)s
                )
                """,
                {
                    "agente": agente,
                    "modelo": modelo,
                    "conv": id_conversacion,
                    "tel": telefono,
                    "llamadas": llamadas,
                    "entrada": tokens_entrada,
                    "cacheados": tokens_entrada_cacheados,
                    "salida": tokens_salida,
                    "costo": costo_usd,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def gasto_del_dia(conn) -> float:
    """Cuantos dolares lleva gastados el dia de HOY, en la zona del servidor.

    `COALESCE` porque un dia sin filas devuelve `NULL` y no `0`: sin el, el primer minuto de
    cada dia compararia `None` contra el umbral y reventaria el vigilante entero.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(costo_usd), 0) FROM consumo_modelo
             WHERE momento >= date_trunc('day', now())
            """
        )
        fila = cur.fetchone()
    return float(fila[0]) if fila else 0.0


def mensajes_en_la_ultima_hora(conn, telefono: str) -> int:
    """Cuantos mensajes ha mandado este numero en la ultima hora.

    Se cuenta sobre `mensajes_entrantes` --la tabla de los HECHOS-- y no sobre un contador
    aparte, que podria desincronizarse de lo que cuenta. Lo hace barato el indice
    `mensajes_entrantes_telefono_recibido_idx` de la 022: sin el, esto seria un scan de la
    tabla en cada mensaje que entra, y el freno costaria mas que lo que frena.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM mensajes_entrantes
             WHERE telefono = %(tel)s
               AND recibido_en >= now() - interval '1 hour'
            """,
            {"tel": telefono},
        )
        fila = cur.fetchone()
    return int(fila[0]) if fila else 0


def archivos_del_dia(conn, telefono: str, tipos: Sequence[str]) -> int:
    """Cuantos archivos LEIBLES ha mandado este numero hoy.

    `tipos` entra por parametro y no se cablea aqui: la lista de lo que el lector sabe leer
    vive en `lectura.TIPOS_QUE_SE_LEEN` y es suya. Duplicarla aqui significaria que el dia que
    el lector aprenda un tipo nuevo, la cuota siga contando los de antes -- y nadie lo notaria,
    porque el fallo es que un freno deja de frenar.

    No cuenta `audio`, `voice`, `video` ni `sticker` aunque tambien se descarguen: esos no
    llegan al modelo, asi que no son gasto de tokens. A ellos los acotan el tope de bytes y el
    semaforo, que es donde esta su riesgo (la RAM), no aqui.
    """
    if not tipos:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM mensajes_entrantes
             WHERE telefono = %(tel)s
               AND tipo = ANY(%(tipos)s)
               AND recibido_en >= date_trunc('day', now())
            """,
            {"tel": telefono, "tipos": list(tipos)},
        )
        fila = cur.fetchone()
    return int(fila[0]) if fila else 0


def toca_avisar_cuota(conn, telefono: str, clase: str, *, ventana_horas: int = 1) -> bool:
    """`True` si a este numero hay que avisarle --y avisar al doctor-- de que se paso.

    Sin esto, un numero que se pasa recibe una frase fija por CADA mensaje y el doctor un
    Telegram por cada uno: exactamente la inundacion que la cuota existe para evitar, servida
    por la propia defensa. Es el error que el no negociable 26 ya pago una vez con los
    escalamientos.

    **Un solo `INSERT ... ON CONFLICT DO UPDATE ... WHERE`, y no leer-y-despues-escribir.** Dos
    mensajes simultaneos del mismo numero pasarian los dos por la lectura antes de que ninguno
    escribiera, y saldrian dos avisos. Aqui la unicidad la decide Postgres y no el orden de
    llegada, igual que en `tomar_cupo` y que la deduplicacion por `wamid`.

    El `RETURNING` es la respuesta: si devuelve fila, esta corrida es la que gano y le toca
    avisar. Si no devuelve nada, es que ya se aviso dentro de la ventana y esta se calla.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cuotas_avisadas (telefono, clase)
                     VALUES (%(tel)s, %(clase)s)
                ON CONFLICT (telefono, clase) DO UPDATE
                        SET avisado_en = now()
                      WHERE cuotas_avisadas.avisado_en
                            < now() - make_interval(hours => %(ventana)s)
                  RETURNING telefono
                """,
                {"tel": telefono, "clase": clase, "ventana": ventana_horas},
            )
            gano = cur.fetchone() is not None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return gano


def toca_avisar_gasto(conn, *, gasto_usd: float) -> bool:
    """`True` si hay que avisar HOY de que el gasto cruzo el umbral. Una vez por dia.

    Mismo mecanismo que `toca_avisar_cuota` y por la misma razon: el vigilante corre cada
    minuto, asi que sin una marca el dia que se cruce el umbral el doctor recibiria 1.440
    Telegram identicos.

    La clave es el DIA y no un contador, para que a medianoche vuelva a avisar solo: un
    contador habria que acordarse de reiniciarlo, y de eso no se acuerda nadie.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alertas_gasto (dia, gasto_usd)
                     VALUES (current_date, %(gasto)s)
                ON CONFLICT (dia) DO NOTHING
                  RETURNING dia
                """,
                {"gasto": gasto_usd},
            )
            gano = cur.fetchone() is not None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return gano

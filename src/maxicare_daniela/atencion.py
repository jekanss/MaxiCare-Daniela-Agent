"""El turno completo de WhatsApp: de un mensaje que entra a una respuesta que sale.

------------------------------------------------------------------------------------------
Por qué no vive en `runtime.py`
------------------------------------------------------------------------------------------

Por la misma razón que `conversacion.py` no vive ahí, y la razón es concreta: un turno que
supiera de HTTP no se podría probar sin levantar un servidor, y el día que los mensajes
entren por otra vía --un segundo número, una cola, un canal nuevo-- habría que reescribirlo
entero. `runtime.py` aporta una línea: llamar a `atender`. Todo lo demás es de aquí.

La división con `conversacion.py` también es deliberada. `responder` es EL TURNO: guardrails,
reintento, escalamiento, y sirve igual al chat web. Este módulo es el turno DE WHATSAPP: de
dónde sale la conversación, qué lleva el contexto, el doble check azul, el retardo humano y
el envío. Nada de eso tiene sentido en un navegador.

------------------------------------------------------------------------------------------
El candado, y qué pasa exactamente sin él
------------------------------------------------------------------------------------------

En WhatsApp un paciente no manda un mensaje: manda tres seguidos. «hola» · «una pregunta» ·
«cuánto vale un implante», en menos de dos segundos. Meta entrega los tres webhooks casi a la
vez, y sin candado eso son tres `Runner.run` simultáneos sobre la MISMA conversación:

1. **El historial se cruza.** Los tres comparten la MISMA sesión persistida
   (`persistencia.sesion_de_agente`, sobre `agent_messages` en Neon) de la conversación --y
   desde la fase 7 eso ya no es un detalle de un solo proceso: es una fila compartida en la
   base, así que el cruce sobreviviría incluso si cada turno corriera en un worker distinto.
   El segundo lee el historial antes de que el primero haya escrito su respuesta, así que
   Daniela contesta dos veces al mismo contexto y puede contradecirse; y los items se
   escriben en el orden en que terminan, no en el que ocurrieron.
2. **La clave de idempotencia deja de proteger.** Se arma con `id_conversacion + turno_actual`
   y los tres leyeron el mismo `turno_actual` de Neon: dos acciones distintas del mismo
   paciente comparten clave, y la segunda se descarta como si fuera un duplicado de la
   primera -- o, al revés, una cita que sí era la misma se crea dos veces.
3. **Las respuestas salen en desorden**, porque cada turno tarda lo suyo y el retardo se
   sortea aparte.

Y hay un cuarto caso que hoy no ocurre pero que este candado es lo único que impediría: si
algún día el `ContextoDaniela` se guardara entre mensajes --como ya se guarda la sesión--,
dos turnos solapados lo compartirían, y `responder` hace `ctx.turno.reiniciar()` al empezar.
El segundo turno le borraría al primero las cifras y las horas que sus tools autorizaron, y
el guardrail de salida del primero bloquearía **por falso positivo**: el paciente recibiría
el mensaje genérico de seguridad sin que nada estuviera mal, y en el log no habría más que un
tripwire que nadie sabría explicar.

------------------------------------------------------------------------------------------
Cuatro límites conocidos, y ninguno es un descuido
------------------------------------------------------------------------------------------

1. **El historial ya NO vive en memoria.** Desde la fase 7 lo guarda `persistencia
   .sesion_de_agente` en Neon (`SQLAlchemySession`), así que un reinicio del proceso ya no
   le borra el hilo del diálogo a nadie -- ni los datos (paciente, citas, estado de la
   oportunidad) ni la conversación misma.
2. **El candado es de proceso.** Con varios workers de uvicorn o varias réplicas deja de
   proteger, porque cada proceso tiene su propio `_candados`. La versión que sí escalaría es
   un `pg_advisory_lock` sobre el uuid de la conversación. Hoy corre un solo worker.
3. **`_candados` se poda por antigüedad**, con la misma ventana de 24 h de
   `conversacion_viva`: pasada esa ventana la conversación ya está muerta para la base, así
   que serializarla ya no tiene sentido. Sin la poda, el diccionario crece con cada número
   que escriba a la clínica y no baja nunca.
4. **`tomada_por` distinto de `None` significa que aquí no se llama al modelo.** Es el
   relevo (6C): un doctor tiene la conversación y Daniela calla hasta que la devuelva. El
   corte está dentro del candado, justo después de `_leer_estado`, y su comentario explica
   por qué el mensaje se anota con un `fallo_respuesta` que no es un fallo.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from . import conversacion, guardrails, ingesta
from . import lectura as lectura_mod
from . import persistencia, sin_resolver
from .calendario import CalendarioCaido, CalendarioDoble, Jornada, calendario_desde_config
from .canales import Telegram, WhatsApp
from .config import (
    MARGEN_LECTURA_SEGUNDOS,
    RETARDO_RESPUESTA_SEGUNDOS,
    TOPE_BUFER_SEGUNDOS,
    VENTANA_SILENCIO_SEGUNDOS,
    Config,
)
from .contratos import ContextoDaniela, DatosDelTurno, LecturaNoClinica
from .ingesta import MensajeEntrante

log = logging.getLogger("maxicare.atencion")

#: La misma ventana que usa `persistencia.conversacion_viva`. Un mensaje que llega 25 horas
#: después no es la continuación de nada: es una conversación nueva.
VENTANA_CONVERSACION_HORAS = 24

#: El aviso de la política, en un solo sitio y con un solo texto. Lo pega el CÓDIGO al primer
#: mensaje saliente de un número que nunca lo ha visto, y no Daniela: una frase en el prompt
#: sirve para que lo diga, pero no sirve como prueba -- nadie sabría si lo dijo, con qué
#: palabras, ni si un día el modelo decidió resumirlo. Mismo principio que las claves de
#: idempotencia (no negociable 2) y la huella de los casos sin resolver (no negociable 22):
#: lo que tiene que ser demostrable lo escribe el código.
AVISO_POLITICA = "Al continuar aceptas nuestra política de tratamiento de datos: {url}"

#: El tope de un mensaje de texto de WhatsApp (Meta). No hay una constante de esto en el
#: resto del repo porque hasta ahora nada armaba un texto lo bastante largo como para
#: acercarse: `mensaje_al_paciente` no tiene tope propio (`contratos.py`) y
#: `canales.enviar_texto` no trunca, así que pegarle el pie a una respuesta que ya lo rozara
#: haría fallar el envío ENTERO -- justo en el primer contacto de ese número, el peor momento
#: posible para dejarlo mudo.
LIMITE_TEXTO_WHATSAPP = 4096


def _con_aviso(respuesta: str, *, url: str) -> str:
    """La respuesta con el aviso pegado al final, o tal cual si no hay URL que enseñar.

    Va como línea aparte y al final: es un pie, no una interrupción. Lo que el paciente
    preguntó se responde primero.

    Con `url` vacía o en `PENDIENTE` devuelve la respuesta intacta. La regla dura 3 es para
    el código: nunca fue permiso para mandarle el marcador a un paciente.

    Y si pegar el pie hace que el conjunto rebase `LIMITE_TEXTO_WHATSAPP`, también devuelve
    la respuesta intacta: perder el aviso de un mensaje así es inocuo -- no se marca, y vuelve
    a salir en el siguiente turno --; perder el mensaje entero por culpa del pie no lo es.
    """
    if not url or url == "PENDIENTE":
        return respuesta
    pie = f"\n\n{AVISO_POLITICA.format(url=url)}"
    if len(respuesta) + len(pie) > LIMITE_TEXTO_WHATSAPP:
        return respuesta
    return respuesta + pie


def _toca_avisar(estado: _Estado, *, url: str) -> bool:
    """Si a este número hay que enseñarle el aviso en este mensaje.

    Es una función y no un `if` suelto porque las dos condiciones son la política entera:
    sale UNA vez en la vida del número --el dato viene de `contactos`, que no caduca a las
    24 h, así que un paciente que escribe cada día no ve el aviso legal cada día-- y no sale
    en absoluto mientras no haya una URL que enseñar.
    """
    if not url or url == "PENDIENTE":
        return False
    return not estado.aviso_visto


# ==========================================================================================
# Estado del módulo -- SOLO vale con un worker
# ==========================================================================================
#
# Los dos diccionarios viven en el proceso. Con `--workers 2` cada proceso tendría su propia
# copia: el candado dejaría de serializar nada y la mitad de los mensajes de una conversación
# no encontraría su historial. El despliegue de la clínica corre un solo worker y el volumen
# lo justifica de sobra; si eso cambia, lo que hay que cambiar es esto (ver el límite 2).

#: El candado va por TELÉFONO, y esa elección es la que hace que sirva de algo.
#:
#: La versión anterior lo indexaba por `id_conversacion`, que parece lo natural --es la
#: conversación lo que hay que serializar-- y dejaba dos huecos, porque el id de la
#: conversación SALE DE LA BASE: había que leer antes de poder cerrar el candado, así que la
#: lectura quedaba fuera y dos mensajes simultáneos la hacían a la vez.
#:
#: 1. **Conversación existente:** los dos leían el mismo `turno_actual` y armaban la misma
#:    clave de idempotencia. Un paciente que escala por dolor en un mensaje y por otra cosa
#:    en el siguiente llegaba al doctor UNA vez: `insertar_escalamiento` descartaba el
#:    segundo como duplicado del primero. Es exactamente lo que el docstring de este módulo
#:    decía que pasaba *sin* candado -- y seguía pasando con él.
#: 2. **Primer contacto:** dos mensajes a la vez de un número nuevo abrían DOS
#:    conversaciones, cada una con su candado y su historial. Daniela contestaba dos veces
#:    sin saber de la otra mitad, y del tercer mensaje en adelante `conversacion_viva` elegía
#:    una y la otra mitad del hilo se perdía. En el momento de más valor: alguien que escribe
#:    a la clínica por primera vez.
#:
#: El teléfono, en cambio, viene EN EL MENSAJE: se conoce antes de tocar la base, así que
#: `_leer_estado` cabe dentro del candado. Y es estrictamente más fuerte que el otro, porque
#: un teléfono no tiene dos conversaciones vivas a la vez: todo lo que serializaba el candado
#: por conversación lo serializa este, y además las dos carreras de arriba.
#:
#: Guarda `(candado, último uso)` porque la poda necesita su propia marca de tiempo: desde
#: que el historial vive en Neon (fase 7) ya no hay `_sesiones` de la que colgarse, y un
#: candado sin marca propia no se podría tirar nunca.
_candados: dict[str, tuple[asyncio.Lock, float]] = {}


@dataclass
class _Bufer:
    """Los mensajes de un número que todavía no han abierto turno. Ver `atender`."""

    mensajes: list[MensajeEntrante]
    #: `time.monotonic()` del primero y del último. El primero manda el tope; el último, la
    #: ventana de silencio.
    primero: float
    ultimo: float
    #: Despierta al que espera cuando llega un mensaje nuevo, para que la cuenta vuelva a
    #: empezar sin tener que sondear.
    despierta: asyncio.Event
    #: La tarea del lector de cada mensaje que traía archivo, por `wamid`. No todas las
    #: entradas del grupo tienen una: un texto suelto no tiene nada que leer.
    lecturas: dict[str, asyncio.Task]


#: El búfer por teléfono. A diferencia de los otros dos diccionarios, este NO necesita poda:
#: un búfer vive como mucho `TOPE_BUFER_SEGUNDOS` y siempre se saca en un `finally`.
_buferes: dict[str, _Bufer] = {}


@dataclass
class _DatosDelMensaje(DatosDelTurno):
    """`DatosDelTurno` que recuerda lo que traía el mensaje aunque lo reinicien.

    Existe por una trampa real: `conversacion.responder` llama a `ctx.turno.reiniciar()`
    ANTES de correr --tiene que hacerlo, es lo que impide que un precio de hace diez mensajes
    siga autorizando esa cifra hoy--, y ese reinicio también borra `hubo_adjunto` y
    `menciona_sintomas`. Ponerlos a mano antes de llamar a `responder` no sirve de nada: se
    borran medio milisegundo después.

    Y no son lo mismo que las cifras autorizadas. Una cifra la autoriza una tool DURANTE el
    turno; que el paciente haya mandado una radiografía es un hecho del mensaje que ya entró,
    y sigue siendo cierto cuando el turno empieza.

    Lo que hay en juego: los dos campos son el prefiltro de `sin_lectura_clinica`
    (`guardrails.vale_la_pena_revisar_lo_clinico`). En `False`, ese guardrail no llega ni a
    preguntarle al evaluador, y el único control que impide que Daniela le diga a un paciente
    qué tiene se queda apagado justo en los dos mensajes donde importa: el que trae una
    radiografía y el que describe un dolor.
    """

    #: Lo que trajo el mensaje. `reiniciar()` los vuelve a poner, no los borra.
    adjunto_del_mensaje: bool = False
    sintomas_del_mensaje: bool = False

    def __post_init__(self) -> None:
        # Para que el invariante sea cierto desde que el objeto existe y no solo a partir del
        # primer `reiniciar()`. Hoy `responder` reinicia siempre antes de correr, así que sin
        # esto igual funcionaría -- pero un `ContextoDaniela` recién construido que ya diga la
        # verdad no depende de que nadie llame a nada, y cualquiera que lo inspeccione (una
        # prueba, un hook, la tool de la 6B) ve lo que trajo el mensaje.
        self.hubo_adjunto = self.adjunto_del_mensaje
        self.menciona_sintomas = self.sintomas_del_mensaje

    def reiniciar(self) -> None:
        super().reiniciar()
        self.hubo_adjunto = self.adjunto_del_mensaje
        self.menciona_sintomas = self.sintomas_del_mensaje


@dataclass(frozen=True)
class Atendido:
    """Lo que pasó con un mensaje.

    Se devuelve en vez de quedarse solo en el log para que el script entregable pueda
    comprobarlo sin leer journald -- y para que `runtime.py` pueda decidir qué registrar sin
    volver a preguntarle a la base.
    """

    wamid: str
    id_conversacion: str | None
    respondido: bool
    texto_enviado: str | None = None
    #: Por qué NO se respondió. También lleva el fallo cuando sí se respondió pero con el
    #: mensaje de emergencia: perder ese motivo dejaría un turno reventado indistinguible de
    #: uno normal.
    motivo: str | None = None
    turno: int = 0
    escalado_por: str | None = None
    #: Este mensaje se sumó a un grupo que ya estaba esperando y NO abrió turno propio.
    #: No va en `motivo` a propósito: `runtime.py` registra cualquier motivo como warning, y
    #: agrupar no es una incidencia -- es lo que pasa cuando alguien escribe dos veces.
    agrupado: bool = False
    #: A cuántos mensajes contestó esta respuesta. 1 en el caso normal.
    mensajes_agrupados: int = 1


@dataclass(frozen=True)
class _Estado:
    """Todo lo que hay que leer de Neon para armar el contexto, en una sola pasada."""

    id_conversacion: str
    turno_actual: int
    identidad_verificada: bool
    intentos_identificacion: int
    id_paciente: int | None
    nombre_paciente: str | None
    #: La clínica NO tiene ficha de este teléfono. Sale del mismo
    #: `buscar_paciente_por_telefono` que ya decide `identidad_verificada`, así que no cuesta
    #: una consulta más. Es lo que le permite a un paciente nuevo pedir su primera cita sin
    #: que `identidad_antes_de_datos` lo frene -- ver `guardrails.revisar_identidad`.
    telefono_sin_paciente: bool
    tomada_por: str | None
    #: El último recordatorio que le salió a este paciente, si lo hubo. Viaja por el mismo
    #: camino que `tomada_por` --una consulta por id sobre `conversaciones`-- porque lo mandó
    #: el despachador y no la conversación: el historial del agente no lo contiene. Sin esto,
    #: un «sí, confirmo» llega sin que Daniela sepa a qué contesta.
    ultimo_recordatorio_tipo: str | None = field(default=None)
    ultimo_recordatorio_en: datetime | None = field(default=None)
    #: `None` si la tabla `configuracion` no respondió. No es lo mismo que un diccionario
    #: vacío: quien lo recibe tiene que poder distinguir «no se pudo leer» de «está vacía».
    operativa: dict[str, int] | None = field(default=None)
    #: Este número pidió que no le escribieran más. Sale de `contactos` --tabla propia por
    #: teléfono, migración 019-- y nunca del modelo: si lo pusiera él, bastaría con que un
    #: paciente dijera «no me escriban» en una frase ambigua para apagarle la reactivación a
    #: otro. Con esto puesto, Daniela no ofrece el seguimiento ni lo menciona.
    pidio_no_contacto: bool = field(default=False)
    #: Este número ya vio el aviso de la política. Se lee aquí, en la misma pasada, y lo
    #: consume el pegado del aviso antes del envío.
    aviso_visto: bool = field(default=False)


# ==========================================================================================
# La base -- sincrónica, y siempre dentro de `asyncio.to_thread`
# ==========================================================================================


def _leer_estado(database_url: str, telefono: str, wamids: list[str]) -> _Estado:
    """Las siete lecturas del turno en una sola conexión, que se cierra al volver.

    Cerrarla antes de llamar al modelo no es higiene: es el ruling 5 de la revisión de la
    tarea 1. `conectar` abre con `autocommit=False`, así que una conexión sostenida durante
    `Runner.run` dejaría la sesión `idle in transaction` los ocho o diez segundos que tarda
    un turno, reteniendo el snapshot y una conexión del pooler de Neon por cada paciente que
    esté escribiendo a la vez.

    La única de estas llamadas que escribe --y que por tanto comitea-- es
    `asegurar_contacto`, que inserta la fila del número si no estaba. Es inocuo para lo de
    arriba y para las lecturas que la siguen: ese `commit()` cierra la transacción abierta y
    la siguiente consulta abre otra, así que lo que se lee después sigue siendo un snapshot
    consistente de sí mismo; lo que no puede pasar --sostener una transacción durante la
    llamada al modelo-- lo impide el `with`, que cierra la conexión antes de volver.
    """
    with persistencia.conectar(database_url) as conn:
        viva = persistencia.conversacion_viva(
            conn, telefono, ventana_horas=VENTANA_CONVERSACION_HORAS
        )
        paciente = persistencia.buscar_paciente_por_telefono(conn, telefono)

        # La fila nace aquí, con el primer mensaje que entra, y no cuando alguien agenda: los
        # que preguntan y no agendan son justo los que hay que poder recordar. `asegurar_`
        # porque puede existir desde hace meses; es idempotente.
        try:
            contacto = persistencia.asegurar_contacto(conn, telefono)
            pidio_no_contacto = bool(contacto["no_contactar"])
            aviso_visto = contacto["aviso_mostrado_en"] is not None
        except Exception:  # noqa: BLE001 -- degradar hacia el lado seguro, nunca tumbar el turno
            # El mismo mecanismo de `leer_configuracion`, un poco más abajo: `conectar` abre
            # con `autocommit=False`, así que un `statement_timeout` o un esquema a medio
            # migrar aquí deja la transacción ABORTADA y las lecturas que siguen dentro de
            # este mismo `with` --paciente, conversación, configuración-- revientan en
            # cadena con `InFailedSqlTransaction` si nadie hace `rollback()`.
            #
            # A diferencia de `leer_configuracion`, esta es una ESCRITURA nueva que no es
            # esencial para el turno: hoy nadie consume `pidio_no_contacto` ni `aviso_visto`
            # más allá de guardarlos en el contexto -- la Tarea 3 es quien los usa de
            # verdad. Dejar que la excepción se propague cambiaría un turno clínico entero
            # (Daniela sin contestar, `MENSAJE_SEGURO` de emergencia) por una señal de
            # consentimiento que todavía no hace nada: exactamente el empate que decide el
            # principio del proyecto, a favor de lo clínico y nunca de lo comercial.
            #
            # Se degrada hacia el lado seguro en las DOS direcciones: `pidio_no_contacto=True`
            # porque no ofrecer nada comercial nunca es un daño, y `aviso_visto=False` porque
            # volver a enseñar un aviso ya visto es inocuo. Y no dura más que este mensaje:
            # `asegurar_contacto` es idempotente, así que la fila nace sola en el turno
            # siguiente en cuanto la base vuelva a responder.
            conn.rollback()
            log.warning(
                "no se pudo asegurar el contacto de %s; se degrada a no-contactar", telefono
            )
            pidio_no_contacto = True
            aviso_visto = False

        if viva is None:
            id_conversacion = persistencia.asegurar_conversacion(
                conn,
                telefono=telefono,
                paciente_id=paciente[0] if paciente else None,
                canal="whatsapp",
            )
            turno_actual, verificada, intentos = 0, False, 0
        else:
            id_conversacion, turno_actual, verificada, intentos = viva

        try:
            operativa = persistencia.leer_configuracion(conn)
        except Exception:  # noqa: BLE001 -- los defaults del dataclass son los mismos
            # El `rollback()` es lo que hace que este `except` degrade de verdad. `conectar`
            # abre la conexión con `autocommit=False`: un error de SQL --un
            # `statement_timeout` leyendo `configuracion`, por ejemplo-- deja la transacción
            # ABORTADA, y las dos llamadas que vienen después, dentro de este mismo `with`,
            # revientan con `InFailedSqlTransaction`. Sin el rollback, `_leer_estado` se cae
            # entero y TODOS los mensajes de la clínica reciben el mensaje de emergencia
            # mientras dure el problema -- por no poder leer tres enteros que ya tienen
            # default. La degradación elegante no degradaba nada.
            conn.rollback()
            log.warning("sin configuración operativa para %s; se usan los defaults", telefono)
            operativa = None

        tomada_por = persistencia.conversacion_tomada(conn, id_conversacion)
        # El recordatorio que el despachador ya le mandó a este NÚMERO. Va aquí y no en una
        # conexión aparte por lo mismo que las otras seis lecturas: una conexión por turno,
        # cerrada antes de llamar al modelo.
        #
        # Por teléfono y NO por `id_conversacion`, aunque quede al lado de una consulta que sí
        # va por id: el despachador anota en la conversación que creó la cita, y un
        # recordatorio de víspera sale más de 24 h después de esa conversación -- que es
        # justo la ventana de `conversacion_viva`. Cuando el paciente contesta «sí, confirmo»
        # ya está en una conversación NUEVA, y buscar por su id devolvía `None` siempre. Ver
        # `persistencia.ultimo_recordatorio`.
        #
        # Y por eso siguen siendo dos consultas y no una: preguntan cosas distintas sobre la
        # misma tabla --una por el id de ESTA conversación, la otra por el máximo de todas las
        # de este número-- y unirlas exigiría un `LEFT JOIN` que devolviera `tomada_por`
        # incluso cuando no hay ningún recordatorio, que es el caso normal.
        recordatorio = persistencia.ultimo_recordatorio(conn, telefono)
        # Todos los del grupo, no solo el que abrió el turno: si se ligara solo ese, los
        # demás quedarían en `mensajes_entrantes` sin conversación, y «¿de qué charla
        # salió este mensaje?» dejaría de tener respuesta justo para los mensajes que
        # más se parten -- los que alguien escribe de corrido.
        for uno in wamids:
            persistencia.ligar_mensaje_a_conversacion(conn, uno, id_conversacion)

    #: La ficha SOLO cuando dice quién es alguien. Una con el marcador `PENDIENTE` es una
    #: fila que existe y no identifica a nadie, y el turno tiene que verla como lo que es.
    conocido = paciente if paciente and paciente[1] != persistencia.NOMBRE_PENDIENTE else None

    return _Estado(
        id_conversacion=id_conversacion,
        turno_actual=turno_actual,
        # Si el número ya está en Neon, la identidad queda verificada sin fricción: lo cerró
        # el plan, no este módulo. El `or` conserva una verificación que ya se había hecho
        # dentro de la conversación -- desverificar a alguien a mitad de la charla sería
        # pedirle sus datos dos veces.
        #
        # `conocido` y no `paciente` a secas: una ficha cuyo nombre es el marcador
        # `PENDIENTE` existe pero no dice quién es nadie. Tratarla como identidad metía al
        # paciente en el único hueco sin salida del sistema -- ni desconocido, que puede pedir
        # su primera cita, ni verificado -- y ahí se quedaba, porque el marcador solo lo pisa
        # el cierre de un relevo CON cita. Medido en producción el 16/09/2026.
        identidad_verificada=bool(conocido) or bool(verificada),
        intentos_identificacion=intentos,
        # El id SÍ sale de la ficha aunque lleve el marcador: es una fila real, y es a lo que
        # tienen que apuntar sus citas. Lo que no sale es el nombre.
        id_paciente=paciente[0] if paciente else None,
        # El nombre del perfil de WhatsApp NO entra aquí: lo escribe el propio desconocido y
        # tratarlo como identidad sería regalarle el nombre de un paciente a cualquiera. El
        # marcador tampoco: con él puesto, Daniela comparaba lo que le decía el paciente
        # contra la palabra «PENDIENTE».
        nombre_paciente=conocido[1] if conocido else None,
        telefono_sin_paciente=conocido is None,
        tomada_por=tomada_por,
        ultimo_recordatorio_tipo=recordatorio[0] if recordatorio else None,
        ultimo_recordatorio_en=recordatorio[1] if recordatorio else None,
        operativa=operativa,
        pidio_no_contacto=pidio_no_contacto,
        aviso_visto=aviso_visto,
    )


def _marcar_aviso(database_url: str, telefono: str, version: str) -> None:
    """Sincrónica, y siempre dentro de `asyncio.to_thread`, como el resto del módulo."""
    with persistencia.conectar(database_url) as conn:
        persistencia.marcar_aviso_mostrado(conn, telefono, version=version)


def _anotar_resultado(
    database_url: str,
    id_conversacion: str,
    wamids: list[str],
    *,
    wamid_respuesta: str | None,
    motivo: str | None,
    turno: int,
    senales: list[sin_resolver.Senal] | None = None,
    tripwires: list[str] | None = None,
    escalado_por: str | None = None,
    frase: str | None = None,
    telefono: str = "",
) -> None:
    """El segundo bloque de base: qué pasó con la respuesta. Nunca propaga.

    Un fallo escribiendo esto importa --de aquí sale «¿a quién no le contestamos?»-- pero
    importa menos que reventar un turno al que el paciente ya recibió su respuesta.

    Los cinco últimos son lo que este turno deja en el informe de «sin resolver», y van con
    default porque los caminos de error llegan aquí sin la mitad de ellos: un turno que
    reventó antes de correr no tiene `tripwires`, y el mensaje que entra durante un relevo no
    tiene turno siquiera. Un default por argumento es también lo que deja intactas las
    llamadas de los scripts de `scripts/`, que doblan esta firma a mano.
    """
    senales = senales or []
    tripwires = tripwires or []
    try:
        with persistencia.conectar(database_url) as conn:
            # Una sola respuesta puede contestar a varios mensajes. Los tres quedan
            # marcados con el MISMO `wamid_respuesta`, que es la verdad: hubo un solo
            # globo. Marcar solo uno dejaría a los otros «sin responder» para siempre, y
            # la barrida que busca a quién no le contestamos los recogería sin fin.
            for wamid in wamids:
                if wamid_respuesta is not None:
                    persistencia.marcar_respondido(conn, wamid, wamid_respuesta=wamid_respuesta)
                    if motivo:
                        # Respondido Y con fallo interno: son compatibles, y perder el segundo
                        # dato era un agujero real. Un turno que reventó --dos tripwires
                        # seguidos, `MaxTurnsExceeded`, una excepción del SDK-- sale con
                        # `MENSAJE_SEGURO`, que ES una respuesta, así que entraba por la rama de
                        # arriba y `marcar_respondido` ponía `fallo_respuesta = NULL`. En
                        # `mensajes_entrantes` quedaba EXACTAMENTE IGUAL que un turno que fue
                        # bien: la pregunta «¿a cuántos pacientes les contestamos con el mensaje
                        # de emergencia?» no se podía responder.
                        #
                        # El orden importa y es este a propósito: `marcar_respondido` limpia el
                        # motivo --y tiene razón en hacerlo, porque un reintento que sí sale
                        # tiene que borrar el fallo del intento anterior-- así que el motivo se
                        # escribe DESPUÉS.
                        persistencia.marcar_fallo_respuesta(conn, wamid, motivo=motivo)
                elif motivo:
                    persistencia.marcar_fallo_respuesta(conn, wamid, motivo=motivo)
            # Siempre, incluso si el envío falló: el turno ocurrió, y la conversación tiene
            # que seguir viva o `conversacion_viva` la declararía vieja a mitad de la charla.
            #
            # Y con el turno, que es lo que lo devuelve al único sitio donde sobrevive a un
            # reinicio. Sin esto la columna se quedaba en 0 para siempre, todos los turnos de
            # una conversación eran el turno 1, y la clave de idempotencia
            # `id_conversacion + turno` dejaba de distinguir un escalamiento nuevo de un
            # reintento del anterior -- el doctor solo se enteraba del primero.
            persistencia.tocar_conversacion(conn, id_conversacion, turno_actual=turno)

            # Lo que este turno deja en el informe de «sin resolver». Va aquí, al final y
            # dentro del `try` que ya traga, por una razón que no se puede mover: cuando esta
            # línea corre, el paciente YA tiene su respuesta en pantalla. Si esto revienta se
            # pierde un caso y queda en el `log.exception` de abajo; nunca se rompe un turno.
            # Y `registrar_caso` hace `rollback` antes de propagar, así que un caso que falle
            # tampoco deja abortada la transacción del `tocar_conversacion` de arriba.
            #
            # Corre en el hilo de `to_thread` de quien llama, que sí está dentro del candado
            # del teléfono. No lo mueve ni lo alarga en nada que importe: es un UPSERT por
            # caso sobre la conexión que este bloque ya tenía abierta, y lo normal es que la
            # lista venga vacía. Lo que NO puede hacer es subir más arriba, a donde el
            # paciente todavía está esperando.
            for caso in sin_resolver.casos_del_turno(
                senales=senales,
                tripwires=tripwires,
                escalado_por=escalado_por,
                motivo=motivo,
                frase=frase,
            ):
                # Uno por uno, y cada uno con su red. `registrar_caso` hace `rollback` y
                # propaga, así que sin este `try` el primero que falla mata el bucle: un
                # turno con hueco Y guardrail perdía el guardrail por culpa del hueco. Y el
                # `log.exception` de abajo diría «no se pudo anotar el resultado», que suena
                # a turno reventado cuando lo único perdido es instrumentación.
                try:
                    persistencia.registrar_caso(
                        conn,
                        huella=caso.huella,
                        tipo=caso.tipo,
                        escalo=caso.escalo,
                        ejemplo=caso.ejemplo,
                        telefono=telefono,
                    )
                except Exception:  # noqa: BLE001 -- instrumentación, nunca el turno
                    log.warning("no se pudo registrar el caso %s", caso.huella)
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo anotar el resultado de %s", ", ".join(wamids))


# ==========================================================================================
# Candados y sesiones
# ==========================================================================================


def _podar_candados(ahora: float) -> None:
    """Tira los candados que nadie ha usado en la ventana. Sin esto, el diccionario crece
    con cada número que le escriba a la clínica y no baja nunca.

    **Un candado cogido no se tira jamás**, y no es una precaución de más: borrar un `Lock`
    que alguien está usando es peor que no borrarlo. El siguiente mensaje crearía uno nuevo
    y los dos turnos correrían a la vez, cada uno creyendo que tiene la exclusiva -- es
    decir, exactamente el fallo que este candado existe para impedir, pero solo cuando la
    conversación lleva un día abierta.
    """
    limite = VENTANA_CONVERSACION_HORAS * 3600
    viejos = [
        tel
        for tel, (candado, ultimo) in _candados.items()
        if ahora - ultimo > limite and not candado.locked()
    ]
    for tel in viejos:
        del _candados[tel]


def _candado_de(telefono: str, ahora: float) -> asyncio.Lock:
    """El candado de ese número, creándolo si es el primer mensaje.

    Se poda ANTES de buscar: si el candado de este teléfono ya estaba caducado, se tira y se
    crea uno limpio, que es lo correcto porque nadie lo tiene cogido. Y el que se devuelve
    queda con la marca de ahora, así que nunca se poda el que se acaba de pedir.
    """
    _podar_candados(ahora)
    guardado = _candados.get(telefono)
    candado = guardado[0] if guardado else asyncio.Lock()
    _candados[telefono] = (candado, ahora)
    return candado


def candado_de(telefono: str) -> asyncio.Lock:
    """El candado del turno de ese número, para quien necesite serializarse con él.

    Existe para `reseteo.resetear`, que borra la conversación de un teléfono y no puede
    hacerlo mientras un turno de ese mismo teléfono está en vuelo: el turno tiene una fila de
    `conversaciones` leída en la mano y escribiría sobre algo que ya no existe. Es el mismo
    candado que coge `atender`, no uno paralelo -- dos candados distintos para el mismo
    recurso no serializan nada.
    """
    return _candado_de(telefono, time.monotonic())


def olvidar(telefono: str) -> None:
    """Saca de la memoria del proceso todo lo de este número. Parte de `/clearstate`.

    Lo que borra de verdad es el búfer: uno que sobreviviera al reseteo metería en el turno
    siguiente --el primero de la conversación «nueva»-- los mensajes de la anterior, y la
    prueba de que Daniela no recuerda nada fallaría por el único sitio que no es la base.

    El historial del diálogo ya NO está aquí: desde la fase 7 vive en `agent_messages`, en
    Neon. Y desde la Tarea 8, `persistencia.borrar_rastro` también lo borra -- dentro de la
    misma transacción que el resto del rastro, y ANTES de `DELETE FROM conversaciones`,
    porque los `session_id` son esos ids: al revés no habría forma de encontrar cuáles borrar.
    Esta función no lo toca porque no le hace falta: el borrado de la base ya ocurrió antes de
    que `olvidar` se llame (ver `reseteo.resetear`), y lo único que queda vivo en el proceso
    es el búfer, que es justo lo que esta función limpia.

    Se llama con el candado del teléfono cogido; el candado en sí se deja donde está, porque
    quien llama lo tiene tomado en ese momento.
    """
    _buferes.pop(telefono, None)
    # El candado de tema del lector, que también se indexa por teléfono. Si se quedara,
    # seguiría serializando a un paciente que ya no existe -- inofensivo, pero sería memoria
    # colgando de un número que el sistema dice no conocer.
    lectura_mod._candados_de_tema.pop(telefono, None)


def _sesion_de(id_conversacion: str, database_url: str):
    """El historial de esta conversación, que ahora vive en Neon y no en este proceso.

    Ya no hay diccionario que podar: el estado se fue a la base. Lo que se construye aquí es
    barato --dos definiciones de tabla y un factory-- porque el pool de conexiones lo tiene
    el engine, que `persistencia` comparte entre todas las conversaciones.
    """
    return persistencia.sesion_de_agente(id_conversacion, database_url=database_url)


# ==========================================================================================
# Lo que se le dice al modelo
# ==========================================================================================


def _calendario_por_defecto(config: Config) -> Any:
    """El calendario de producción, o uno que se comporta como caído. Nunca uno que finge.

    `CalendarioGoogle.__init__` hace una lectura real contra Google --es su comprobación de
    acceso, y es deliberada-- así que puede fallar al construirse con las credenciales bien
    puestas: Google caído, o el calendario sin compartir con la cuenta de servicio. Sin este
    `try`, ese fallo salía de `atender` y el paciente se quedaba sin respuesta por una
    dependencia que ni siquiera hacía falta para contestarle un precio.

    Lo que NO se hace aquí es caer a `CalendarioDoble`, que es lo que pediría el instinto: un
    doble en producción le confirma al paciente una cita que no existe en ningún calendario y
    lo manda a una clínica donde nadie lo espera. `CalendarioCaido` lanza `ErrorDeCalendario`,
    que es justo lo que las tools de la fase 3 saben manejar -- liberan el cupo y escalan.

    Se atrapa `Exception` y no solo `ErrorDeCalendario` por la misma razón que abajo con el
    envío: el constructor traduce lo que conoce (auth, HTTP, red), pero un `ImportError` de
    las bibliotecas de Google en un despliegue a medias no está traducido, y dejaría mudos a
    todos los pacientes por una dependencia que falta.
    """
    try:
        calendario = calendario_desde_config(config)
    except Exception as e:  # noqa: BLE001 -- ver docstring
        log.error(
            "EL CALENDARIO NO ARRANCÓ (%s): Daniela puede conversar, pero NO puede agendar, "
            "mover ni cancelar citas. Las tools van a escalar a los doctores cada vez que lo "
            "intenten. Revisa MAXICARE_GOOGLE_SA_B64 y que el calendario esté compartido con "
            "la cuenta de servicio.",
            e,
        )
        return CalendarioCaido(motivo=str(e))

    # El camino que NO lanza, y es el que de verdad ocurre: `calendario_desde_config`
    # devuelve un `CalendarioDoble()` cuando faltan las credenciales, con un `warning` y
    # nada más. En una máquina de desarrollo es lo correcto; en WhatsApp es el fallo que
    # este proyecto existe para evitar, porque **una variable presente y vacía no es una
    # variable ausente** --el `.env` trae casi todas las claves así-- y un
    # `MAXICARE_GOOGLE_SA_B64=` vacío no da error de arranque: da un doble en producción,
    # que toma el cupo, «crea» el evento en un diccionario, le confirma la cita al paciente
    # y lo manda a una clínica donde nadie lo espera.
    #
    # Este es el único sitio de `atencion.py` por el que entra el calendario de WhatsApp; el
    # doble solo es legítimo cuando alguien lo pasa a mano (`atender(calendario=...)`, el
    # chat de pruebas web), que es justo lo que este camino no es.
    if isinstance(calendario, CalendarioDoble):
        log.error(
            "EL CALENDARIO NO ESTÁ CONFIGURADO (falta MAXICARE_GOOGLE_SA_B64 o "
            "MAXICARE_GOOGLE_CALENDAR_ID, o están presentes y VACÍAS). Daniela puede "
            "conversar, pero NO puede agendar: cada intento escala a los doctores. Se usa "
            "un calendario CAÍDO y nunca uno de mentira."
        )
        return CalendarioCaido(
            motivo="faltan MAXICARE_GOOGLE_SA_B64 o MAXICARE_GOOGLE_CALENDAR_ID"
        )

    return calendario


async def _recoger_lecturas(
    tareas: dict[str, asyncio.Task], margen: float | None = None
) -> dict[str, LecturaNoClinica]:
    """Lo que el lector alcanzó a producir. Lo que no, no llegó.

    `margen` es corto a propósito: la ventana ya le dio al lector sus 20 segundos. Esto es
    la cola, no la espera.

    Y es un plazo COMPARTIDO, no uno por archivo. Los lectores corrieron todos a la vez
    durante la ventana, así que a todos les queda lo mismo por terminar; dárselo a cada uno
    por separado convertiría las tres páginas de una remisión --el caso que el búfer existe
    para agrupar-- en 3 × 3 s de cola. Contra el presupuesto de `limites.latencia_maxima`
    eso no cabe: 45 de tope + 9 de cola + 6 del turno se pasan del minuto. Con el plazo
    compartido el techo es uno y no depende de cuántos archivos mandara el paciente.

    El default se resuelve AQUÍ y no en la firma porque un valor por defecto se evalúa al
    definir la función: con `margen: float = MARGEN_LECTURA_SEGUNDOS`, un `monkeypatch` del
    módulo no cambiaría nada y la prueba del lector lento mediría la constante de verdad.
    """
    margen = MARGEN_LECTURA_SEGUNDOS if margen is None else margen
    fin = time.monotonic() + margen
    recogidas: dict[str, LecturaNoClinica] = {}
    for wamid, tarea in tareas.items():
        try:
            # `shield` para que el timeout NO cancele la tarea: el lector sigue corriendo y
            # su mensaje a Telegram llega igual, tarde pero llega. Cancelarla dejaría al
            # doctor sin la lectura solo porque Daniela ya no la necesitaba.
            valor = await asyncio.wait_for(
                asyncio.shield(tarea), timeout=max(0.0, fin - time.monotonic())
            )
        except TimeoutError:
            log.info("la lectura de %s no llegó a tiempo; el turno sale sin ella", wamid)
            continue
        except Exception:  # noqa: BLE001 -- ninguna puede tumbar el turno
            # Hoy no debería verse: `leer_y_repartir` se traga lo suyo y devuelve `None`. Si
            # aparece es que algo cambió, y un `log.info` sin traza diciendo «no llegó a
            # tiempo» sería la pista equivocada.
            log.exception("la lectura de %s falló de una forma inesperada", wamid)
            continue
        if valor is not None:
            recogidas[wamid] = valor
    return recogidas


def _entrada_para_el_modelo(
    mensaje: MensajeEntrante, lectura: LecturaNoClinica | None = None
) -> str:
    """El texto del paciente, o --si vino un archivo-- QUÉ llegó. Nunca qué muestra.

    Sin `lectura` --un audio, un sticker, o un lector que no llegó a tiempo-- nadie ha visto
    el contenido, y el prompt tiene que decirlo con todas las letras. Si aquí se colara una
    interpretación --«parece una radiografía con una caries»--, `sin_lectura_clinica` no
    tendría nada que bloquear: la invención vendría de dentro del sistema, ya con aspecto de
    dato.

    Con `lectura` cambia QUÉ se sabe, no el muro: `LecturaNoClinica` no tiene
    `contexto_clinico` --no es que no se copie, es que no hay dónde ponerlo--, así que lo
    más que puede decir esta rama es de qué tratamiento es el documento y de dónde viene.
    Qué muestra sigue siendo cosa del doctor, y el aviso lo repite para que el modelo no
    complete el hueco por su cuenta.

    Es la misma regla que `ingesta.componer_aviso` respeta con los doctores, y se escribe
    igual a propósito.
    """
    if mensaje.trae_archivo:
        que = ingesta.NOMBRE_HUMANO.get(mensaje.tipo, f"algo de tipo «{mensaje.tipo}»")
        if lectura is not None:
            aviso = f"[El paciente acaba de enviar {que}"
            if mensaje.nombre_archivo:
                # Igual que la rama sin lectura. Que el lector acierte no es razón para que
                # Daniela deje de saber cómo se llamaba el archivo.
                aviso += f", con el nombre «{mensaje.nombre_archivo}»"
            aviso += ". Un lector automático lo revisó y el doctor ya lo tiene. "
            if lectura.tratamiento != "no_identificado":
                aviso += f"Es sobre {lectura.tratamiento.replace('_', ' ')}. "
            else:
                aviso += "No se pudo identificar para qué tratamiento es: pregúntaselo. "
            if lectura.origen:
                aviso += f"Viene de «{lectura.origen}». "
            aviso += (
                "NO sabes qué muestra el archivo y no debes suponerlo: eso lo ve el "
                "doctor.]"
            )
            if mensaje.texto:
                aviso += f"\nEl paciente escribió junto al archivo: {mensaje.texto}"
            return aviso

        aviso = f"[El paciente acaba de enviar {que}"
        if mensaje.nombre_archivo:
            aviso += f", con el nombre «{mensaje.nombre_archivo}»"
        aviso += (
            ". Nadie lo ha revisado todavía y tú no puedes verlo: no sabes qué contiene. "
            "Confirma que llegó y que el doctor lo va a revisar; no supongas qué es ni qué "
            "significa.]"
        )
        if mensaje.texto:
            aviso += f"\nEl paciente escribió junto al archivo: {mensaje.texto}"
        return aviso

    # Un quick reply de plantilla llega como el rótulo pelado: «Confirmar». Sin decir de
    # dónde sale, el modelo no sabe a qué contesta -- y el evaluador de `uso_indebido`, que
    # recibe EXACTAMENTE esta misma cadena, ve un imperativo suelto. El 15/09/2026 lo
    # clasificó como «intenta imponer instrucciones del sistema» y el paciente que confirmaba
    # su cita se llevó el mensaje seguro, con su alerta falsa al doctor.
    #
    # El corchete no es decorativo: es la misma convención que el aviso de más abajo y la del
    # grupo del búfer -- lo que va entre corchetes lo escribe el SISTEMA, no el paciente.
    if mensaje.texto and mensaje.tipo == "button":
        return (
            f"[El paciente pulsó el botón «{mensaje.texto}» del mensaje automático que le "
            "enviamos. No lo escribió él: eligió una de las opciones.]"
        )

    if mensaje.texto:
        return mensaje.texto

    # `location`, `contacts`, un tipo que Meta añada mañana. Sin esto, el modelo recibiría
    # una cadena vacía y respondería a nada.
    que = ingesta.NOMBRE_HUMANO.get(mensaje.tipo, f"algo de tipo «{mensaje.tipo}»")
    return f"[El paciente envió {que}. No trae texto.]"


def _entrada_del_grupo(
    mensajes: list[MensajeEntrante], leidas: dict[str, LecturaNoClinica] | None = None
) -> str:
    """Los mensajes del grupo como UNA sola entrada para el modelo.

    El aviso de la cabecera no es decorativo. Sin él, el modelo recibe tres frases sueltas y
    las contesta una por una dentro del mismo globo --«1) ¡Hola! 2) Ofrecemos... 3) Sí,
    hacemos...»--, que es exactamente la sensación que el búfer existe para quitar, solo que
    concentrada en un mensaje en vez de repartida en tres.

    Cada lectura se busca por `wamid`, no por posición: la foto y el «¿esto qué es?» que
    viene detrás son dos mensajes del mismo grupo, y atar la lectura al segundo haría que la
    entrada dijera que el mensaje de TEXTO trae una remisión, que es falso.
    """
    leidas = leidas or {}
    if len(mensajes) == 1:
        return _entrada_para_el_modelo(mensajes[0], leidas.get(mensajes[0].wamid))
    partes = [_entrada_para_el_modelo(m, leidas.get(m.wamid)) for m in mensajes]
    return (
        "[El paciente escribió esto en varios mensajes seguidos, como se escribe en "
        "WhatsApp. Es una sola idea partida en trozos: léela entera y contéstale UNA vez, "
        "sin ir mensaje por mensaje ni numerar las respuestas.]\n" + "\n".join(partes)
    )


async def _esperar_el_silencio(bufer: _Bufer, ventana: float, tope: float) -> None:
    """Espera a que el paciente deje de escribir, o a que se agote el tope.

    Cada mensaje nuevo reinicia la cuenta de `ventana`. Por eso hay un `Event` y no un
    `sleep` a secas: quien escribe tres veces seguidas no tiene que esperar tres ventanas, y
    el que espera se entera en el acto en vez de sondeando. El tope, en cambio, se cuenta
    desde el primer mensaje y no se reinicia nunca -- es lo único que impide que alguien
    escribiendo sin parar deje la ventana abierta para siempre.

    El `clear()` va ANTES de mirar el reloj, y ese orden es deliberado. Al revés, un mensaje
    que llegara entre la mirada y el `clear()` perdería su aviso, y su texto no entraría en
    el grupo hasta una vuelta siguiente que podría no llegar nunca.
    """
    while True:
        bufer.despierta.clear()
        ahora = time.monotonic()
        espera = min(ventana - (ahora - bufer.ultimo), tope - (ahora - bufer.primero))
        if espera <= 0:
            return
        try:
            await asyncio.wait_for(bufer.despierta.wait(), timeout=espera)
        except TimeoutError:
            return


# ==========================================================================================
# El turno
# ==========================================================================================


async def atender(
    mensaje: MensajeEntrante,
    *,
    whatsapp: WhatsApp,
    telegram: Telegram,
    config: Config,
    calendario: Any | None = None,
    al_escalar: Callable[..., Awaitable[None]] | None = None,
    dormir: Callable[[float], Awaitable[None]] | None = None,
    ventana: float | None = None,
    tope: float | None = None,
    lectura: asyncio.Task | None = None,
    sesion_de: Callable[[str], Any] | None = None,
) -> Atendido:
    """Atiende un mensaje de WhatsApp de principio a fin y deja constancia de qué pasó.

    `calendario`, `dormir`, `ventana` y `tope` existen **solo para que esto se pueda
    probar**. Los dos últimos son el búfer: con `ventana=0` no se agrupa nada y el turno
    corre como antes, que es lo que quiere casi toda la suite.

    `calendario` y `dormir` existen **solo para que esto se pueda probar**. Por omisión son
    `calendario_desde_config(config)` y `asyncio.sleep`, que es lo que corre en producción;
    una prueba que esperase 55 segundos de verdad es una prueba que alguien acaba saltándose,
    y un `CalendarioGoogle` construido por mensaje pagaría una lectura contra Google cada vez.

    `sesion_de` existe **solo para que esto se pueda probar** sin Neon. Por omisión es
    `_sesion_de`, la persistida, que es lo que corre en producción: la suite offline le pasa
    una `SesionEnMemoria` y así sigue corriendo en dos segundos y sin señal.

    `telegram` no se usa en el camino normal: los avisos a los doctores salen de
    `escalar_a_doctores` --que arma su propio cliente con las credenciales del contexto-- y
    de `al_escalar`, que lo aporta `runtime.py`. Está en la firma porque el relevo (6C) lo va
    a necesitar aquí y porque es el mismo juego de dependencias que recibe
    `ingesta.procesar_mensaje`: dos funciones del mismo webhook que se piden lo mismo se leen
    mucho mejor que dos que no.

    `lectura` es la `asyncio.Task` del lector de archivos que arrancó `procesar_mensaje`, y
    llega YA CORRIENDO: no se espera aquí antes de la ventana, se recoge después de que
    cierre y solo si ya terminó. Lo que no llegue, se descarta -- el doctor lo recibe igual
    por su lado, que es el camino que no se puede retrasar.

    Nunca propaga. Quien llama es un BackgroundTask de FastAPI, donde una excepción se pierde
    en el log del servidor sin dejar rastro consultable.
    """
    # Lo primero de todo: es lo que hace honesto el descuento del retardo. Medido después de
    # las lecturas de base, un turno lento se sumaría al retardo en vez de comérselo.
    momento_inicio = time.monotonic()

    if not config.daniela_responde:
        # Antes de tocar la base y antes de gastar un token. El interruptor existe para
        # callar a Daniela en diez segundos; si costara lo mismo que tenerla encendida, no
        # serviría para lo que existe.
        log.info("MAXICARE_DANIELA_RESPONDE=0: %s se registra pero no se responde", mensaje.wamid)
        return Atendido(
            wamid=mensaje.wamid, id_conversacion=None, respondido=False, motivo="apagado"
        )

    # El doble check azul mientras Daniela «escribe» es lo que hace creíble la espera. Después
    # del retardo no sirve de nada: para entonces ya llegó la respuesta.
    #
    # `canales.WhatsApp.marcar_leido` se traga los `httpx.HTTPError` y nada más. Un timeout de
    # lectura los cubre, pero un token vencido que devuelva algo raro, o cualquier otra cosa,
    # saldría de aquí y tumbaría el turno ANTES de tocar la base: el paciente sin respuesta y
    # sin rastro, por un detalle cosmético.
    try:
        await whatsapp.marcar_leido(mensaje.wamid)
    except Exception:  # noqa: BLE001 -- ver arriba
        log.warning("no se pudo marcar leído %s; se sigue igual", mensaje.wamid, exc_info=True)

    # ------------------------------------------------------------------------------------
    # El búfer: juntar lo que el paciente escribió de corrido
    # ------------------------------------------------------------------------------------
    #
    # Antes, cada mensaje abría su turno y su respuesta. Tres mensajes en menos de un minuto
    # --el saludo, la pregunta, y lo que se le ocurrió después-- daban tres globos de Daniela
    # contestando a una sola idea, con siete segundos entre los dos últimos. Está medido en
    # la primera conversación real de la clínica, y se lee como lo que es: una máquina.
    #
    # Aquí NO hace falta candado, y conviene decirlo porque parece que sí. Este bloque no
    # tiene un solo `await`: en un único bucle de eventos eso lo vuelve atómico por
    # construcción. Un candado no lo haría más seguro, solo escondería que la seguridad
    # depende de que nadie meta un `await` en medio.
    #
    # Y va ANTES del candado del turno, que es lo que lo hace funcionar. Si el segundo
    # mensaje tuviera que esperar ese candado, no podría sumarse al grupo hasta que el turno
    # del primero terminara: es decir, hasta después de la respuesta que se quería evitar.
    ahora = time.monotonic()
    esperando = _buferes.get(mensaje.telefono)
    if esperando is not None:
        esperando.mensajes.append(mensaje)
        esperando.ultimo = ahora
        if lectura is not None:
            esperando.lecturas[mensaje.wamid] = lectura
        esperando.despierta.set()
        log.info(
            "%s se suma al grupo de %s (van %d); no abre turno propio",
            mensaje.wamid,
            mensaje.telefono,
            len(esperando.mensajes),
        )
        return Atendido(
            wamid=mensaje.wamid, id_conversacion=None, respondido=False, agrupado=True
        )

    bufer = _Bufer(
        [mensaje],
        primero=ahora,
        ultimo=ahora,
        despierta=asyncio.Event(),
        lecturas={mensaje.wamid: lectura} if lectura is not None else {},
    )
    _buferes[mensaje.telefono] = bufer
    try:
        await _esperar_el_silencio(
            bufer,
            VENTANA_SILENCIO_SEGUNDOS if ventana is None else ventana,
            TOPE_BUFER_SEGUNDOS if tope is None else tope,
        )
    finally:
        # En un `finally`, siempre. Un búfer que quedara registrado tras una cancelación se
        # tragaría todos los mensajes siguientes de ese número: cada uno se sumaría a un
        # grupo que ya no espera a nadie, y ese teléfono no volvería a recibir respuesta.
        _buferes.pop(mensaje.telefono, None)

    # El lector corrió EN PARALELO con la ventana, no delante. Aquí solo se recoge lo que ya
    # esté listo: lo que quede se descarta.
    #
    # Encadenarlo delante sumaría sus 4-8 s a los 20 de la ventana y, en el peor caso,
    # Daniela habría contestado ya el texto que vino junto a la foto sin saber que había una
    # foto. Eso está medido: el 12/09, un documento recibido a las 19:03:28 se entregó
    # después de un texto recibido a las 19:03:29.
    leidas = await _recoger_lecturas(bufer.lecturas)

    mensajes = bufer.mensajes
    wamids = [m.wamid for m in mensajes]
    if len(mensajes) > 1:
        log.info("%s: %d mensajes en un solo turno", mensaje.telefono, len(mensajes))
    texto = _entrada_del_grupo(mensajes, leidas)
    # Y aparte, lo que el paciente escribió de verdad. NO es `texto`: esa es la entrada que
    # se le arma al modelo, con la cabecera de «esto vino en varios mensajes» y, si hubo
    # archivo, un aviso que lleva el NOMBRE del archivo dentro. Eso acaba en la pantalla de
    # la clínica y en el informe, donde no pinta nada -- y un nombre de archivo puede ser una
    # cédula (regla dura 4). Ver `sin_resolver.frase_para_el_informe`.
    frase_del_paciente = sin_resolver.frase_para_el_informe([m.texto for m in mensajes])

    # El candado se coge ANTES de leer la base, y ese orden es el arreglo entero.
    #
    # Antes se leía primero --hacía falta, porque el candado iba por `id_conversacion` y ese
    # id sale de la lectura-- así que el candado serializaba la ejecución del turno pero no
    # su lectura. Dos mensajes simultáneos leían a la vez: el mismo `turno_actual` (y por
    # tanto la misma clave de idempotencia, con lo que el segundo escalamiento se descartaba
    # como duplicado del primero) o, en un primer contacto, ninguna conversación -- y cada
    # uno creaba la suya. Ver el comentario de `_candados`.
    #
    # El teléfono viene en el mensaje, así que aquí ya se conoce y la lectura cabe dentro.
    candado = _candado_de(mensaje.telefono, time.time())

    async with candado:
        try:
            # `persistencia` es psycopg SÍNCRONO. Llamarlo directo desde aquí bloquearía el
            # bucle de eventos mientras Neon responde, es decir, a todos los demás pacientes
            # a la vez. Dentro del candado sigue siendo cierto: `to_thread` cede el control,
            # y lo único que espera es otro mensaje DEL MISMO número.
            estado = await asyncio.to_thread(
                _leer_estado, config.database_url, mensaje.telefono, wamids
            )
        except Exception as e:  # noqa: BLE001
            # Sin base no hay contexto, y sin contexto no hay turno. Pero la regla de
            # `fallos.escalamiento` no admite excepciones: pase lo que pase sale un mensaje.
            # Y la promesa que hace `MENSAJE_SEGURO` se cumple igual, porque
            # `ingesta.procesar_mensaje` ya le reenvió este mensaje a los doctores.
            log.exception(
                "no se pudo leer el estado de %s; se responde lo mínimo", mensaje.telefono
            )
            try:
                await whatsapp.enviar_texto(mensaje.telefono, conversacion.MENSAJE_SEGURO)
            except Exception:  # noqa: BLE001 -- mismo motivo que en el envío de abajo
                log.exception("tampoco se pudo responder a %s", mensaje.telefono)
                return Atendido(
                    mensaje.wamid,
                    None,
                    False,
                    motivo=f"sin base y sin envío: {e}",
                    mensajes_agrupados=len(mensajes),
                )
            return Atendido(
                mensaje.wamid,
                None,
                True,
                texto_enviado=conversacion.MENSAJE_SEGURO,
                motivo=f"sin base: {e}",
                mensajes_agrupados=len(mensajes),
            )

        if estado.tomada_por:
            # ==========================================================================
            # EL RELEVO: aquí Daniela CALLA. Fase 6C.
            # ==========================================================================
            #
            # No es que se le pida que no conteste: es que no se la llama. Ni al modelo, ni
            # a los guardrails, ni a una sola tool. La alternativa --dejarla correr y tirar
            # su respuesta-- gastaría un turno de contexto por cada frase que el paciente
            # escriba durante el relevo, y ese historial se lo encontraría entero al
            # retomar, como si hubiera estado hablando ella.
            #
            # El mensaje del paciente YA le llegó al doctor: `ingesta.procesar_mensaje`
            # corre antes que esto en `runtime._entregar` y lo deposita en su tema, que
            # durante un relevo además suena.
            #
            # --------------------------------------------------------------------------
            # Por qué esto se anota como `fallo_respuesta` sin ser un fallo
            # --------------------------------------------------------------------------
            #
            # `persistencia.mensajes_sin_responder` busca la firma «`respondido_en` NULL Y
            # `fallo_respuesta` NULL», que significa «entró y nadie lo procesó». Un mensaje
            # de relevo tiene esa misma forma --nadie le contestó por WhatsApp-- así que sin
            # marcarlo, el barrido de arranque se lo entregaría a Daniela media hora después
            # y le contestaría por encima del doctor.
            #
            # No hay columna para un tercer estado y añadir una costaría una migración por
            # un matiz de informe. El motivo empieza por `relevo:` para que se pueda separar
            # de un fallo de verdad con un `LIKE`, y por eso se escribe así y no de otra
            # forma. Mismo criterio que el no negociable 14: antes invertir el significado
            # de una columna y documentarlo que inventarse otra.
            motivo_relevo = f"relevo: la tiene {estado.tomada_por}"
            await asyncio.to_thread(
                _anotar_resultado,
                config.database_url,
                estado.id_conversacion,
                wamids,
                wamid_respuesta=None,
                # Y esto es lo que hace que un relevo NO ensucie el informe de «sin
                # resolver»: `sin_resolver.casos_del_turno` filtra el motivo que empieza por
                # `relevo:`, así que esta llamada no escribe ni un caso. Los cinco argumentos
                # del informe se quedan en su default a propósito -- aquí no hubo turno, no
                # existe `ctx`, y no hay señal ninguna que contar.
                motivo=motivo_relevo,
                # El turno NO avanza: no hubo turno. Y `_anotar_resultado` llama igualmente a
                # `tocar_conversacion`, que es lo que mantiene la conversación viva mientras
                # dure el relevo -- sin eso, tres horas de charla con el doctor la dejarían
                # caducada para `conversacion_viva` y el paciente volvería a ser un primer
                # contacto en cuanto Daniela retomara.
                turno=estado.turno_actual,
            )
            log.info(
                "%s está en relevo con %s; Daniela no contesta este mensaje",
                mensaje.telefono,
                estado.tomada_por,
            )
            return Atendido(
                mensaje.wamid,
                estado.id_conversacion,
                False,
                motivo=motivo_relevo,
                turno=estado.turno_actual,
                mensajes_agrupados=len(mensajes),
            )

        operativa = estado.operativa or {}
        ctx = ContextoDaniela(
            id_conversacion=estado.id_conversacion,
            telefono_completo=mensaje.telefono,
            database_url=config.database_url,
            # Nunca un `CalendarioDoble()` fijo: este es el sitio por donde el calendario de
            # verdad entra en producción, y un doble aquí dejaría a la clínica sin ver una
            # sola cita, sin un solo error en el log.
            calendario=calendario if calendario is not None else _calendario_por_defecto(config),
            id_paciente=estado.id_paciente,
            nombre_paciente=estado.nombre_paciente,
            identidad_verificada=estado.identidad_verificada,
            intentos_identificacion=estado.intentos_identificacion,
            telefono_sin_paciente=estado.telefono_sin_paciente,
            pidio_no_contacto=estado.pidio_no_contacto,
            # No la usa ninguna tool: la lee `agentes.instrucciones_daniela` para saber si
            # el aviso del código está apagado (`PENDIENTE`) y, en ese caso, devolverle a
            # Daniela la frase de avisarlo con sus palabras. Con URL, manda el código.
            politica_datos_url=config.politica_datos_url,
            turno_actual=estado.turno_actual,
            tomada_por=estado.tomada_por,
            # El tema propio de la conversación llega con el relevo (6C). Hasta entonces todo
            # va al General.
            topic_id=None,
            capacidad_por_hora=operativa.get("capacidad_por_hora", 2),
            duracion_cita_minutos=operativa.get("duracion_cita_minutos", 60),
            cierre_relevo_minutos=operativa.get("cierre_relevo_minutos", 180),
            # Las perillas de los recordatorios. Las leen `crear_cita` y `reprogramar_cita`
            # para decidir CUÁNDO sale el de cada cita, y los dos campos de abajo le dicen a
            # Daniela cuál salió ya -- un dato que no está en el historial porque no lo
            # escribió ninguna conversación.
            hora_recordatorio_vispera=operativa.get("hora_recordatorio_vispera", 18),
            horas_minimas_para_recordar=operativa.get("horas_minimas_para_recordar", 4),
            ultimo_recordatorio_tipo=estado.ultimo_recordatorio_tipo,
            ultimo_recordatorio_en=estado.ultimo_recordatorio_en,
            # Sale del `type` que mandó Meta, nunca del modelo. `all` y no `any`: un solo
            # botón en un grupo que además trae texto libre dejaría ese texto sin evaluar.
            # Ver el campo en `contratos.ContextoDaniela`, que explica qué costó.
            entrada_solo_de_botones=bool(mensajes)
            and all(m.tipo == "button" for m in mensajes),
            jornada=Jornada(
                apertura=operativa.get("hora_apertura", 8),
                cierre=operativa.get("hora_cierre", 17),
                cierre_sabado=operativa.get("hora_cierre_sabado", 15),
                atiende_domingo=bool(operativa.get("atiende_domingo", 0)),
            ),
            tema_general=operativa.get("telegram_topic_general", 0),
            # El chat de pruebas web los deja vacíos a propósito: allí no hay a quién avisar.
            # Copiar eso aquí dejaría a `escalar_a_doctores` construyendo un `Telegram("", "")`
            # que falla EN SILENCIO -- el doctor nunca se entera de que había que escalar.
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_doctores=config.telegram_chat_doctores,
            turno=_DatosDelMensaje(
                # Del GRUPO entero, no del último mensaje. Mandar la radiografía y escribir
                # «¿esto qué es?» justo después son dos mensajes: leyendo solo el segundo,
                # `sin_lectura_clinica` se quedaría sin nada que vigilar precisamente en el
                # turno que sí habla de la imagen.
                adjunto_del_mensaje=any(m.trae_archivo for m in mensajes),
                sintomas_del_mensaje=any(
                    guardrails.menciona_sintomas(m.texto or "") for m in mensajes
                ),
            ),
        )

        fabricar = sesion_de or (lambda id_: _sesion_de(id_, config.database_url))
        sesion = fabricar(estado.id_conversacion)

        try:
            resultado = await conversacion.responder(
                texto, ctx=ctx, sesion=sesion, al_escalar=al_escalar
            )
            respuesta = resultado.respuesta.mensaje_al_paciente
            turno, escalado_por, fallo = resultado.turno, resultado.escalado_por, None
            # Se saca del `resultado` aquí y no en la llamada de abajo porque el camino del
            # envío fallido lo necesita igual, y ahí `resultado` puede no existir.
            tripwires = resultado.tripwires
            # Aquí el escalamiento sí ocurrió: `al_escalar` corrió y el doctor está avisado.
            escalamiento_real = escalado_por
        except Exception as e:  # noqa: BLE001
            # `responder` ya traduce lo que lanza el SDK, pero no lo que lanza una tool con un
            # bug ni un fallo de red a mitad de turno. El silencio es la única respuesta que
            # no vale.
            log.exception("el turno de %s reventó; sale el mensaje seguro", estado.id_conversacion)
            respuesta = conversacion.MENSAJE_SEGURO
            turno, escalado_por, fallo = ctx.turno_actual, "dato_faltante", f"{type(e).__name__}: {e}"
            # No hubo `Resultado` que preguntar. Las señales sí sobreviven: viven en el
            # contexto, y una consulta sin dato que ocurrió antes del reventón ocurrió igual.
            tripwires = []
            # Y el escalamiento NO ocurrió. Ese `"dato_faltante"` es un marcador sintético de
            # este camino --lo lee `Atendido` y el log-- pero `conversacion.responder` lanzó
            # antes de llegar a `al_escalar`: ningún doctor fue avisado. Pasárselo al informe
            # abría un `humano:dato_faltante` con `escalo=1` ADEMÁS del `roto:X` --dos
            # tarjetas para una historia, que es la regla que esta tabla existe para no
            # romper-- y la pantalla imprimía «se interrumpió al doctor 1 de N veces» sobre
            # una interrupción que no existió: un número falso en la pantalla del cliente.
            escalamiento_real = None

        # `limites.latencia_maxima`: nunca instantánea, nunca más de un minuto. Se descuenta
        # lo que ya tardó el turno; sumarlo daría respuestas de minuto y medio y el paciente
        # ya se fue.
        objetivo = random.uniform(*RETARDO_RESPUESTA_SEGUNDOS)
        espera = max(0.0, objetivo - (time.monotonic() - momento_inicio))
        await (dormir or asyncio.sleep)(espera)

        # El aviso va pegado al primer saliente de un número que nunca lo ha visto. Se decide
        # aquí, con el texto ya cerrado, para que valga igual si la respuesta salió del modelo
        # o si es el mensaje seguro: a alguien que entra por primera vez y se encuentra un
        # fallo también se le está atendiendo.
        toca_avisar = _toca_avisar(estado, url=config.politica_datos_url)
        if toca_avisar:
            respuesta = _con_aviso(respuesta, url=config.politica_datos_url)

        try:
            wamid_respuesta = await whatsapp.enviar_texto(mensaje.telefono, respuesta)

            if toca_avisar:
                # DESPUÉS del envío, nunca antes. Es al revés que un recordatorio (no
                # negociable 21) y es deliberado: allí el riesgo es mandarlo dos veces, así
                # que se marca antes; aquí el riesgo es dar por mostrado un aviso que no
                # salió, y esa constancia es precisamente la prueba. Repetir un aviso es
                # inocuo; falsificar una prueba, no.
                #
                # Si esto falla, el turno sigue: el paciente ya tiene su respuesta y el aviso
                # se le volverá a enseñar en el siguiente mensaje.
                try:
                    await asyncio.to_thread(
                        _marcar_aviso,
                        config.database_url,
                        mensaje.telefono,
                        config.politica_datos_version,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("no se pudo registrar el aviso de %s", mensaje.telefono)
        except Exception as e:  # noqa: BLE001 -- ver abajo
            # `ErrorDeCanal` NO basta, y esto está comprobado: `canales.enviar_texto` hace el
            # POST sin envolver los errores de httpx, así que un `ReadTimeout` o un
            # `ConnectError` contra la Graph API salen crudos -- y también un `KeyError` si
            # Meta devuelve un 200 con otra forma. Con el `except` estrecho, esa tarde de
            # timeouts se traducía en: excepción perdida en el BackgroundTask, paciente sin
            # respuesta, `mensajes_entrantes` sin motivo, y la conversación sin tocar, así que
            # a las 24 horas se declaraba muerta a mitad de la charla. Este módulo promete en
            # su docstring que nunca propaga; esta línea es la que lo hace verdad.
            log.exception("no se le pudo responder a %s", mensaje.telefono)
            await asyncio.to_thread(
                _anotar_resultado,
                config.database_url,
                estado.id_conversacion,
                wamids,
                wamid_respuesta=None,
                motivo=f"{type(e).__name__}: {e}",
                turno=turno,
                # El turno ocurrió entero: lo que falló fue entregarlo. El hueco de
                # conocimiento que Daniela encontró es el mismo, y el `motivo` añade encima
                # un caso `ROTO` por el envío -- que es justo lo que hay que poder contar.
                senales=ctx.turno.senales,
                tripwires=tripwires,
                escalado_por=escalamiento_real,
                frase=frase_del_paciente,
                telefono=mensaje.telefono,
            )
            return Atendido(
                wamid=mensaje.wamid,
                id_conversacion=estado.id_conversacion,
                respondido=False,
                motivo=f"{type(e).__name__}: {e}",
                turno=turno,
                escalado_por=escalado_por,
                mensajes_agrupados=len(mensajes),
            )

        await asyncio.to_thread(
            _anotar_resultado,
            config.database_url,
            estado.id_conversacion,
            wamids,
            wamid_respuesta=wamid_respuesta,
            motivo=fallo,
            turno=turno,
            # El turno normal: el paciente ya tiene su respuesta y esto solo deja el rastro.
            # La frase es la del GRUPO entero, no la del último mensaje: ese puede ser «?» a
            # secas, y la pregunta estar en el anterior.
            senales=ctx.turno.senales,
            tripwires=tripwires,
            escalado_por=escalamiento_real,
            frase=frase_del_paciente,
            telefono=mensaje.telefono,
        )
        return Atendido(
            wamid=mensaje.wamid,
            id_conversacion=estado.id_conversacion,
            respondido=True,
            texto_enviado=respuesta,
            motivo=fallo,
            turno=turno,
            escalado_por=escalado_por,
            mensajes_agrupados=len(mensajes),
        )


__all__ = ["VENTANA_CONVERSACION_HORAS", "Atendido", "atender"]

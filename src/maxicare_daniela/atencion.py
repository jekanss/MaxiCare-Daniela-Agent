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

1. **El historial se cruza.** Los tres comparten la `SesionEnMemoria` de la conversación.
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

1. **El historial vive en memoria.** Un reinicio del proceso borra el hilo del diálogo -- no
   los datos: paciente, citas y estado de la oportunidad están en Neon. Lo arregla la fase 7
   cambiando `SesionEnMemoria` por `SQLAlchemySession`.
2. **El candado es de proceso.** Con varios workers de uvicorn o varias réplicas deja de
   proteger, porque cada proceso tiene su propio `_candados`. La versión que sí escalaría es
   un `pg_advisory_lock` sobre el uuid de la conversación. Hoy corre un solo worker.
3. **`_sesiones` y `_candados` se podan por antigüedad**, con la misma ventana de 24 h de
   `conversacion_viva`: pasada esa ventana la conversación ya está muerta para la base, así
   que su historial tampoco sirve. Sin la poda, los diccionarios crecen con cada número que
   escriba a la clínica y no bajan nunca. Se podan por separado porque tienen claves
   distintas --`_sesiones` por conversación, `_candados` por teléfono-- y un candado cogido
   no se tira nunca.
4. **`tomada_por` siempre será `None` hasta la entrega 6C**, que es la que trae el relevo a
   los doctores. Se lee desde ya porque no cuesta una consulta aparte y evita volver aquí.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import conversacion, guardrails, ingesta, persistencia
from .calendario import CalendarioCaido, CalendarioDoble, calendario_desde_config
from .canales import Telegram, WhatsApp
from .config import RETARDO_RESPUESTA_SEGUNDOS, Config
from .contratos import ContextoDaniela, DatosDelTurno
from .ingesta import MensajeEntrante

log = logging.getLogger("maxicare.atencion")

#: La misma ventana que usa `persistencia.conversacion_viva`. Un mensaje que llega 25 horas
#: después no es la continuación de nada: es una conversación nueva.
VENTANA_CONVERSACION_HORAS = 24

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
#: Guarda `(candado, último uso)` porque la poda ya no puede colgarse de `_sesiones`: las dos
#: tablas tienen claves distintas --teléfono aquí, id de conversación allá-- y un candado sin
#: marca de tiempo propia no se podría tirar nunca.
_candados: dict[str, tuple[asyncio.Lock, float]] = {}
_sesiones: dict[str, tuple[conversacion.SesionEnMemoria, float]] = {}


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


@dataclass(frozen=True)
class _Estado:
    """Todo lo que hay que leer de Neon para armar el contexto, en una sola pasada."""

    id_conversacion: str
    turno_actual: int
    identidad_verificada: bool
    intentos_identificacion: int
    id_paciente: int | None
    nombre_paciente: str | None
    tomada_por: str | None
    #: `None` si la tabla `configuracion` no respondió. No es lo mismo que un diccionario
    #: vacío: quien lo recibe tiene que poder distinguir «no se pudo leer» de «está vacía».
    operativa: dict[str, int] | None = field(default=None)


# ==========================================================================================
# La base -- sincrónica, y siempre dentro de `asyncio.to_thread`
# ==========================================================================================


def _leer_estado(database_url: str, telefono: str, wamid: str) -> _Estado:
    """Las seis lecturas del turno en una sola conexión, que se cierra al volver.

    Cerrarla antes de llamar al modelo no es higiene: es el ruling 5 de la revisión de la
    tarea 1. Ninguna de estas funciones hace `commit()` tras su SELECT, así que una conexión
    sostenida durante `Runner.run` dejaría la sesión `idle in transaction` los ocho o diez
    segundos que tarda un turno, reteniendo el snapshot y una conexión del pooler de Neon por
    cada paciente que esté escribiendo a la vez.
    """
    with persistencia.conectar(database_url) as conn:
        viva = persistencia.conversacion_viva(
            conn, telefono, ventana_horas=VENTANA_CONVERSACION_HORAS
        )
        paciente = persistencia.buscar_paciente_por_telefono(conn, telefono)

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
        persistencia.ligar_mensaje_a_conversacion(conn, wamid, id_conversacion)

    return _Estado(
        id_conversacion=id_conversacion,
        turno_actual=turno_actual,
        # Si el número ya está en Neon, la identidad queda verificada sin fricción: lo cerró
        # el plan, no este módulo. El `or` conserva una verificación que ya se había hecho
        # dentro de la conversación -- desverificar a alguien a mitad de la charla sería
        # pedirle sus datos dos veces.
        identidad_verificada=bool(paciente) or bool(verificada),
        intentos_identificacion=intentos,
        id_paciente=paciente[0] if paciente else None,
        # El nombre del perfil de WhatsApp NO entra aquí: lo escribe el propio desconocido y
        # tratarlo como identidad sería regalarle el nombre de un paciente a cualquiera.
        nombre_paciente=paciente[1] if paciente else None,
        tomada_por=tomada_por,
        operativa=operativa,
    )


def _anotar_resultado(
    database_url: str,
    id_conversacion: str,
    wamid: str,
    *,
    wamid_respuesta: str | None,
    motivo: str | None,
    turno: int,
) -> None:
    """El segundo bloque de base: qué pasó con la respuesta. Nunca propaga.

    Un fallo escribiendo esto importa --de aquí sale «¿a quién no le contestamos?»-- pero
    importa menos que reventar un turno al que el paciente ya recibió su respuesta.
    """
    try:
        with persistencia.conectar(database_url) as conn:
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
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo anotar el resultado de %s", wamid)


# ==========================================================================================
# Candados y sesiones
# ==========================================================================================


def _podar_sesiones(ahora: float) -> None:
    """Tira las sesiones que ya pasaron la ventana.

    Ya no tira candados: desde que el candado va por teléfono, las dos tablas tienen claves
    distintas y borrar `_candados[id_conversacion]` no habría borrado nunca nada. De los
    candados se encarga `_podar_candados`.
    """
    limite = VENTANA_CONVERSACION_HORAS * 3600
    viejas = [id_ for id_, (_, ultimo) in _sesiones.items() if ahora - ultimo > limite]
    for id_ in viejas:
        del _sesiones[id_]


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


def _sesion_de(id_conversacion: str, ahora: float) -> conversacion.SesionEnMemoria:
    _podar_sesiones(ahora)
    guardada = _sesiones.get(id_conversacion)
    sesion = guardada[0] if guardada else conversacion.SesionEnMemoria(id_conversacion)
    _sesiones[id_conversacion] = (sesion, ahora)
    return sesion


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


def _entrada_para_el_modelo(mensaje: MensajeEntrante) -> str:
    """El texto del paciente, o --si vino un archivo-- QUÉ llegó. Nunca qué muestra.

    El lector de archivos llega en la entrega 6B; hoy nadie ha visto el contenido, y el
    prompt tiene que decirlo con todas las letras. Si aquí se colara una interpretación
    --«parece una radiografía con una caries»--, `sin_lectura_clinica` no tendría nada que
    bloquear: la invención vendría de dentro del sistema, ya con aspecto de dato.

    Es la misma regla que `ingesta.componer_aviso` respeta con los doctores, y se escribe
    igual a propósito: cuando llegue 6B no habrá que revisar este camino para entenderlo.
    """
    if mensaje.trae_archivo:
        que = ingesta.NOMBRE_HUMANO.get(mensaje.tipo, f"algo de tipo «{mensaje.tipo}»")
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

    if mensaje.texto:
        return mensaje.texto

    # `location`, `contacts`, un tipo que Meta añada mañana. Sin esto, el modelo recibiría
    # una cadena vacía y respondería a nada.
    que = ingesta.NOMBRE_HUMANO.get(mensaje.tipo, f"algo de tipo «{mensaje.tipo}»")
    return f"[El paciente envió {que}. No trae texto.]"


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
) -> Atendido:
    """Atiende un mensaje de WhatsApp de principio a fin y deja constancia de qué pasó.

    `calendario` y `dormir` existen **solo para que esto se pueda probar**. Por omisión son
    `calendario_desde_config(config)` y `asyncio.sleep`, que es lo que corre en producción;
    una prueba que esperase 55 segundos de verdad es una prueba que alguien acaba saltándose,
    y un `CalendarioGoogle` construido por mensaje pagaría una lectura contra Google cada vez.

    `telegram` no se usa en el camino normal: los avisos a los doctores salen de
    `escalar_a_doctores` --que arma su propio cliente con las credenciales del contexto-- y
    de `al_escalar`, que lo aporta `runtime.py`. Está en la firma porque el relevo (6C) lo va
    a necesitar aquí y porque es el mismo juego de dependencias que recibe
    `ingesta.procesar_mensaje`: dos funciones del mismo webhook que se piden lo mismo se leen
    mucho mejor que dos que no.

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

    texto = _entrada_para_el_modelo(mensaje)

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
                _leer_estado, config.database_url, mensaje.telefono, mensaje.wamid
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
                return Atendido(mensaje.wamid, None, False, motivo=f"sin base y sin envío: {e}")
            return Atendido(
                mensaje.wamid,
                None,
                True,
                texto_enviado=conversacion.MENSAJE_SEGURO,
                motivo=f"sin base: {e}",
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
            turno_actual=estado.turno_actual,
            tomada_por=estado.tomada_por,
            # El tema propio de la conversación llega con el relevo (6C). Hasta entonces todo
            # va al General.
            topic_id=None,
            capacidad_por_hora=operativa.get("capacidad_por_hora", 2),
            duracion_cita_minutos=operativa.get("duracion_cita_minutos", 60),
            cierre_relevo_minutos=operativa.get("cierre_relevo_minutos", 180),
            tema_general=operativa.get("telegram_topic_general", 0),
            # El chat de pruebas web los deja vacíos a propósito: allí no hay a quién avisar.
            # Copiar eso aquí dejaría a `escalar_a_doctores` construyendo un `Telegram("", "")`
            # que falla EN SILENCIO -- el doctor nunca se entera de que había que escalar.
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_doctores=config.telegram_chat_doctores,
            turno=_DatosDelMensaje(
                adjunto_del_mensaje=mensaje.trae_archivo,
                sintomas_del_mensaje=guardrails.menciona_sintomas(mensaje.texto or ""),
            ),
        )

        sesion = _sesion_de(estado.id_conversacion, momento_inicio)

        try:
            resultado = await conversacion.responder(
                texto, ctx=ctx, sesion=sesion, al_escalar=al_escalar
            )
            respuesta = resultado.respuesta.mensaje_al_paciente
            turno, escalado_por, fallo = resultado.turno, resultado.escalado_por, None
        except Exception as e:  # noqa: BLE001
            # `responder` ya traduce lo que lanza el SDK, pero no lo que lanza una tool con un
            # bug ni un fallo de red a mitad de turno. El silencio es la única respuesta que
            # no vale.
            log.exception("el turno de %s reventó; sale el mensaje seguro", estado.id_conversacion)
            respuesta = conversacion.MENSAJE_SEGURO
            turno, escalado_por, fallo = ctx.turno_actual, "dato_faltante", f"{type(e).__name__}: {e}"

        # `limites.latencia_maxima`: nunca instantánea, nunca más de un minuto. Se descuenta
        # lo que ya tardó el turno; sumarlo daría respuestas de minuto y medio y el paciente
        # ya se fue.
        objetivo = random.uniform(*RETARDO_RESPUESTA_SEGUNDOS)
        espera = max(0.0, objetivo - (time.monotonic() - momento_inicio))
        await (dormir or asyncio.sleep)(espera)

        try:
            wamid_respuesta = await whatsapp.enviar_texto(mensaje.telefono, respuesta)
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
                mensaje.wamid,
                wamid_respuesta=None,
                motivo=f"{type(e).__name__}: {e}",
                turno=turno,
            )
            return Atendido(
                wamid=mensaje.wamid,
                id_conversacion=estado.id_conversacion,
                respondido=False,
                motivo=f"{type(e).__name__}: {e}",
                turno=turno,
                escalado_por=escalado_por,
            )

        await asyncio.to_thread(
            _anotar_resultado,
            config.database_url,
            estado.id_conversacion,
            mensaje.wamid,
            wamid_respuesta=wamid_respuesta,
            motivo=fallo,
            turno=turno,
        )
        return Atendido(
            wamid=mensaje.wamid,
            id_conversacion=estado.id_conversacion,
            respondido=True,
            texto_enviado=respuesta,
            motivo=fallo,
            turno=turno,
            escalado_por=escalado_por,
        )


__all__ = ["VENTANA_CONVERSACION_HORAS", "Atendido", "atender"]

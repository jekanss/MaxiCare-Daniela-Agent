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
3. **`_sesiones` se poda por antigüedad**, con la misma ventana de 24 h de
   `conversacion_viva`: pasada esa ventana la conversación ya está muerta para la base, así
   que su historial tampoco sirve. Sin la poda, el diccionario crece con cada número que
   escriba a la clínica y no baja nunca.
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
from .calendario import calendario_desde_config
from .canales import ErrorDeCanal, Telegram, WhatsApp
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

_candados: dict[str, asyncio.Lock] = {}
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
) -> None:
    """El segundo bloque de base: qué pasó con la respuesta. Nunca propaga.

    Un fallo escribiendo esto importa --de aquí sale «¿a quién no le contestamos?»-- pero
    importa menos que reventar un turno al que el paciente ya recibió su respuesta.
    """
    try:
        with persistencia.conectar(database_url) as conn:
            if wamid_respuesta is not None:
                persistencia.marcar_respondido(conn, wamid, wamid_respuesta=wamid_respuesta)
            elif motivo:
                persistencia.marcar_fallo_respuesta(conn, wamid, motivo=motivo)
            # Siempre, incluso si el envío falló: el turno ocurrió, y la conversación tiene
            # que seguir viva o `conversacion_viva` la declararía vieja a mitad de la charla.
            persistencia.tocar_conversacion(conn, id_conversacion)
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo anotar el resultado de %s", wamid)


# ==========================================================================================
# Candados y sesiones
# ==========================================================================================


def _podar_sesiones(ahora: float) -> None:
    """Tira las sesiones que ya pasaron la ventana, y sus candados con ellas.

    El candado solo se tira si nadie lo tiene cogido. Borrar un `Lock` que alguien está
    usando es peor que no borrarlo: el siguiente mensaje crearía uno nuevo y los dos turnos
    correrían a la vez creyendo cada uno que tiene la exclusiva.
    """
    limite = VENTANA_CONVERSACION_HORAS * 3600
    viejas = [id_ for id_, (_, ultimo) in _sesiones.items() if ahora - ultimo > limite]
    for id_ in viejas:
        del _sesiones[id_]
        candado = _candados.get(id_)
        if candado is not None and not candado.locked():
            del _candados[id_]


def _sesion_de(id_conversacion: str, ahora: float) -> conversacion.SesionEnMemoria:
    _podar_sesiones(ahora)
    guardada = _sesiones.get(id_conversacion)
    sesion = guardada[0] if guardada else conversacion.SesionEnMemoria(id_conversacion)
    _sesiones[id_conversacion] = (sesion, ahora)
    return sesion


# ==========================================================================================
# Lo que se le dice al modelo
# ==========================================================================================


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
    # del retardo no sirve de nada: para entonces ya llegó la respuesta. Falla en silencio por
    # diseño -- ver `canales.WhatsApp.marcar_leido`.
    await whatsapp.marcar_leido(mensaje.wamid)

    texto = _entrada_para_el_modelo(mensaje)

    try:
        # `persistencia` es psycopg SÍNCRONO. Llamarlo directo desde aquí bloquearía el bucle
        # de eventos mientras Neon responde, es decir, a todos los demás pacientes a la vez.
        estado = await asyncio.to_thread(
            _leer_estado, config.database_url, mensaje.telefono, mensaje.wamid
        )
    except Exception as e:  # noqa: BLE001
        # Sin base no hay contexto, y sin contexto no hay turno. Pero la regla de
        # `fallos.escalamiento` no admite excepciones: pase lo que pase sale un mensaje. Y la
        # promesa que hace `MENSAJE_SEGURO` se cumple igual, porque `ingesta.procesar_mensaje`
        # ya le reenvió este mensaje a los doctores antes de llegar aquí.
        log.exception("no se pudo leer el estado de %s; se responde lo mínimo", mensaje.telefono)
        try:
            await whatsapp.enviar_texto(mensaje.telefono, conversacion.MENSAJE_SEGURO)
        except ErrorDeCanal:
            log.exception("tampoco se pudo responder a %s", mensaje.telefono)
            return Atendido(mensaje.wamid, None, False, motivo=f"sin base y sin envío: {e}")
        return Atendido(
            mensaje.wamid,
            None,
            True,
            texto_enviado=conversacion.MENSAJE_SEGURO,
            motivo=f"sin base: {e}",
        )

    candado = _candados.setdefault(estado.id_conversacion, asyncio.Lock())

    async with candado:
        operativa = estado.operativa or {}
        ctx = ContextoDaniela(
            id_conversacion=estado.id_conversacion,
            telefono_completo=mensaje.telefono,
            database_url=config.database_url,
            # Nunca un `CalendarioDoble()` fijo: este es el sitio por donde el calendario de
            # verdad entra en producción, y un doble aquí dejaría a la clínica sin ver una
            # sola cita, sin un solo error en el log.
            calendario=calendario if calendario is not None else calendario_desde_config(config),
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
        except ErrorDeCanal as e:
            log.error("no se le pudo responder a %s: %s", mensaje.telefono, e)
            await asyncio.to_thread(
                _anotar_resultado,
                config.database_url,
                estado.id_conversacion,
                mensaje.wamid,
                wamid_respuesta=None,
                motivo=str(e),
            )
            return Atendido(
                wamid=mensaje.wamid,
                id_conversacion=estado.id_conversacion,
                respondido=False,
                motivo=str(e),
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

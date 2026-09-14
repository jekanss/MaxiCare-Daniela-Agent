"""El relevo: el botón «Hablar yo con el paciente» y todo lo que pasa después.

Fase 6C. Hasta aquí el botón se mandaba --`herramientas._teclado_relevo`-- y no estaba
conectado a nada: el doctor lo pulsaba y no ocurría nada. Esto es lo que lo conecta.

==========================================================================================
QUÉ ES UN RELEVO
==========================================================================================

Un doctor toma la conversación. Mientras la tiene:

    · Daniela CALLA. No se le llama al modelo ni una vez -- ver `atencion.atender`.
    · El tema de ese paciente queda ABIERTO en Telegram, y lo que se escriba ahí dentro
      llega a su WhatsApp tal cual, sin firma y sin prefijo.
    · Ese tema SUENA, que es la única excepción de la NOTA DEL SILENCIO de `canales.py`:
      durante un relevo el hilo deja de ser un expediente y pasa a ser una conversación.

El candado lo pone Telegram y no nuestra disciplina: un tema CERRADO impide físicamente
escribir a quien no es administrador del grupo, mientras el bot, que sí lo es, sigue
pudiendo depositar. De ahí que el modo no sea algo que el doctor tenga que recordar --si
puede escribir, está en relevo; si no puede, está mirando el expediente.

==========================================================================================
UNA SOLA PUERTA DE SALIDA, TRES MANERAS DE LLEGAR A ELLA
==========================================================================================

    devuelto_por_doctor  -> pulsó «Listo, que siga Daniela»
    tiempo_agotado       -> `cierre_relevo_minutos` sin que el doctor escriba
    tema_perdido         -> borraron el tema, o Telegram ya no deja escribir en él

Las tres pasan por `cerrar()`, y no es una comodidad: el estado peligroso de este módulo es
«Daniela callada y el tema abierto sin que nadie esté mirando», o sea un canal en vivo hacia
el WhatsApp de una persona que nadie vigila. Con dos caminos de salida, tarde o temprano uno
de ellos se olvida de la mitad del trabajo.

==========================================================================================
EL RELOJ CUENTA DESDE LA ÚLTIMA SEÑAL DEL DOCTOR, NO DESDE LA ACTIVACIÓN
==========================================================================================

`persistencia.relevos_activos` mide contra `GREATEST(tomada_en, ultimo_mensaje_doctor_en)`.
Medir desde la activación convertiría el cierre en un cronómetro: a las tres horas justas se
corta, y una conversación difícil de tres horas es precisamente la que sigue viva. Así es lo
que tiene que ser -- un detector de abandono.

==========================================================================================
LO QUE ESTE MÓDULO NO PUEDE ARREGLAR
==========================================================================================

Telegram no permite quitarle privilegios al CREADOR del grupo, así que esa persona puede
escribir dentro de un tema cerrado. Lo que hace `relevar_mensaje` es no reenviar nada cuando
no hay relevo vivo y decirlo en el propio hilo, con lo que el hueco pasa de «su mensaje llega
al paciente sin que nadie lo sepa» a «su mensaje no llega y se le avisa». Estrechado, no
cerrado; está documentado así en el plan.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from . import persistencia
from .canales import _TIPO_WHATSAPP_DE_TELEGRAM
from .lectura import nombre_del_tema

log = logging.getLogger(__name__)

#: Lo que viaja en el `callback_data` de cada botón. Telegram lo limita a 64 bytes y lo deja
#: VISIBLE para cualquiera del grupo, así que lleva el uuid de la conversación y nunca el
#: teléfono del paciente.
PREFIJO_TOMAR = "relevo:"
PREFIJO_DEVOLVER = "devolver:"

#: El nombre con el que el relevo abre la ficha de un número que no tenía ninguna. Es el
#: literal de la regla dura 3 del proyecto, y no el nombre del perfil de WhatsApp: ese lo
#: escribe el propio desconocido, y guardarlo sería dejar que alguien se registre solo con el
#: nombre que le apetezca. Lo que no se sabe se marca como no sabido.
NOMBRE_PENDIENTE = "PENDIENTE"

#: A quién ya se le avisó de que se le acaba el tiempo. En memoria y no en la base a
#: propósito: añadir una columna para esto obligaría a una migración, y el peor caso de
#: perderlo --que el proceso reinicie y un doctor reciba el aviso dos veces-- es un mensaje
#: de más en un hilo que él mismo está mirando. La alternativa, un aviso por barrido, sí
#: sería insoportable.
_avisados: set[str] = set()


def _escapar(texto: str) -> str:
    """Telegram va en `parse_mode=HTML` y RECHAZA el mensaje entero si el HTML no cierra.

    Aquí entra el nombre que un doctor se puso en Telegram y el texto que escribió un
    paciente: los dos son de fuera. Un doctor llamado «Ana <3» basta para que el aviso del
    relevo no exista.
    """
    return html.escape(texto or "", quote=False)


def nombre_de_quien_pulsa(desde: dict[str, Any] | None) -> str:
    """Cómo se le llama al doctor en los mensajes y en `conversaciones.tomada_por`.

    Telegram no garantiza ninguno de los tres campos a la vez, así que se prueban en orden de
    utilidad para quien lo va a leer: el nombre que se puso, luego el `@usuario`, y el id
    numérico como último recurso. Un «None tomó la conversación» en el hilo de un paciente es
    peor que un id feo.
    """
    desde = desde or {}
    partes = [desde.get("first_name") or "", desde.get("last_name") or ""]
    nombre = " ".join(p for p in partes if p).strip()
    if nombre:
        return nombre[:120]
    if desde.get("username"):
        return f"@{desde['username']}"[:120]
    return f"doctor {desde.get('id', '?')}"


def teclado_devolver(id_conversacion: str) -> dict:
    """El botón que cierra el relevo. Va DENTRO del tema, no en el General.

    Ahí es donde el doctor está cuando termina de hablar, y un botón que hay que ir a buscar
    a otro sitio es un relevo que se cierra por tiempo agotado.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Listo, que siga Daniela",
                    "callback_data": f"{PREFIJO_DEVOLVER}{id_conversacion}",
                }
            ]
        ]
    }


def teclado_ir_al_hilo(enlace: str, doctor: str) -> dict:
    """Lo que reemplaza a «Hablar yo con el paciente» en el General una vez tomada.

    Cumple dos funciones a la vez y por eso es un botón y no texto: deja el General ordenado
    --se ve de un vistazo quién la tiene, sin abrir nada-- y le da a los demás la puerta de
    entrada al hilo por si quieren leer. Un botón-enlace no dispara ningún `callback_query`,
    así que nadie puede volver a tomar la conversación desde aquí.
    """
    return {"inline_keyboard": [[{"text": f"✅ La tiene {doctor} · ir al hilo", "url": enlace}]]}


# ==========================================================================================
# La base -- sincrónica, siempre dentro de `asyncio.to_thread`
# ==========================================================================================


def _telefono(database_url: str, id_conversacion: str) -> str | None:
    with persistencia.conectar(database_url) as conn:
        return persistencia.telefono_de_conversacion(conn, id_conversacion)


def _activar_en_base(database_url: str, id_conversacion: str, doctor: str) -> str | None:
    with persistencia.conectar(database_url) as conn:
        return persistencia.activar_relevo(
            conn, id_conversacion=id_conversacion, doctor=doctor
        )


def _cerrar_en_base(database_url: str, id_conversacion: str, motivo: str) -> bool:
    with persistencia.conectar(database_url) as conn:
        return persistencia.cerrar_relevo(conn, id_conversacion, motivo=motivo)


def _tema_de(database_url: str, telefono: str) -> int | None:
    with persistencia.conectar(database_url) as conn:
        return persistencia.tema_del_paciente(conn, telefono)


def _ficha_para_el_relevo(database_url: str, telefono: str) -> int:
    """Crea la ficha del paciente si no la tiene, y devuelve su id.

    ------------------------------------------------------------------------------------
    Esto hace justo lo que `lectura.asegurar_tema` se niega a hacer, y la diferencia es
    quién lo decide
    ------------------------------------------------------------------------------------

    `asegurar_tema` no crea fichas porque un desconocido que manda una foto no puede
    convertirse en paciente verificado por el hecho de mandarla: `atencion._leer_estado`
    deriva `identidad_verificada` de la EXISTENCIA de esta fila, así que crearla ahí
    desactivaba `revisar_identidad` para cualquiera con una cámara.

    Aquí lo dispara un humano pulsando un botón a propósito para hablar con esa persona. Esa
    es la autorización que allá no existe, y el precio de no hacerlo es peor que el de
    hacerlo: el número sin ficha es exactamente el paciente nuevo con dolor agudo --el que
    más escala-- y dejarlo sin relevo deja el botón muerto justo donde hace falta.

    El nombre va como `PENDIENTE` y no como el del perfil de WhatsApp. La ficha dice «este
    número existe y un doctor habló con él», no «esta persona se llama así».
    """
    with persistencia.conectar(database_url) as conn:
        return persistencia.asegurar_paciente(
            conn, nombre_completo=NOMBRE_PENDIENTE, telefono=telefono
        )


def _guardar_tema_abierto(database_url: str, id_paciente: int, tema: int) -> None:
    with persistencia.conectar(database_url) as conn:
        persistencia.guardar_tema(conn, id_paciente=id_paciente, topic_id=tema, abierto=True)


def _marcar_abierto(database_url: str, telefono: str, abierto: bool) -> None:
    with persistencia.conectar(database_url) as conn:
        persistencia.marcar_tema_abierto(conn, telefono, abierto=abierto)


def _relevo_de_tema(database_url: str, topic_id: int) -> dict[str, Any] | None:
    with persistencia.conectar(database_url) as conn:
        return persistencia.relevo_por_tema(conn, topic_id)


def _activos(database_url: str) -> list[dict[str, Any]]:
    with persistencia.conectar(database_url) as conn:
        return persistencia.relevos_activos(conn)


def _tocar(database_url: str, id_conversacion: str) -> None:
    with persistencia.conectar(database_url) as conn:
        persistencia.tocar_mensaje_doctor(conn, id_conversacion)


def _marcar_escalamiento(database_url: str, mensaje_id: int) -> None:
    with persistencia.conectar(database_url) as conn:
        persistencia.marcar_relevo_activado(conn, mensaje_id)


# ==========================================================================================
# Activar
# ==========================================================================================


async def _tema_abierto_para(
    *, telefono: str, database_url: str, telegram
) -> int:
    """El tema de ese paciente, abierto y listo para conversar. Lanza si no se pudo.

    Aquí sí se propaga, al revés que en `lectura.asegurar_tema`. Allá un fallo degradaba al
    General y el archivo llegaba igual; aquí no hay nada que degradar: sin tema no hay por
    dónde hablarle al paciente, y un relevo activado sin hilo dejaría a Daniela callada
    frente a alguien con quien nadie puede hablar. Quien llama deshace el relevo.
    """
    tema = await asyncio.to_thread(_tema_de, database_url, telefono)

    if tema is None:
        # Ver `_ficha_para_el_relevo`: esto es deliberado y lo autoriza el doctor.
        id_paciente = await asyncio.to_thread(_ficha_para_el_relevo, database_url, telefono)
        tema = await telegram.crear_tema(nombre_del_tema(telefono, None))
        # Nace ABIERTO, que es la diferencia con el tema que abre el primer archivo. No hace
        # falta `reabrir_tema` después: `createForumTopic` ya lo deja así.
        await asyncio.to_thread(_guardar_tema_abierto, database_url, id_paciente, tema)
        log.info("el relevo abrió el tema %s para +%s, que no tenía", tema, telefono)
        return tema

    await telegram.reabrir_tema(tema)
    await asyncio.to_thread(_marcar_abierto, database_url, telefono, True)
    return tema


def _texto_de_bienvenida(doctor: str, minutos: int) -> str:
    """Lo primero que el doctor lee al entrar al hilo. Tiene que caber en una notificación.

    Dice las dos cosas que no puede no saber: que lo que escriba SALE hacia un teléfono real,
    y que esto se cierra solo. Lo demás sobra -- un manual dentro del hilo no lo lee nadie.
    """
    horas = minutos / 60.0
    cuanto = f"{horas:.0f} h" if horas >= 1 else f"{minutos} min"
    return (
        f"🔔 <b>Tienes la conversación</b> — {_escapar(doctor)}\n\n"
        "Lo que escribas <b>aquí dentro</b> le llega al paciente por WhatsApp tal cual, "
        "sin firma. También le llegan las fotos y los audios que mandes.\n\n"
        f"Daniela está callada. Se vuelve a activar sola tras {cuanto} sin que escribas, "
        "o cuando toques el botón."
    )


async def activar(
    *,
    id_conversacion: str,
    doctor: str,
    callback_id: str,
    mensaje_id: int | None,
    telegram,
    database_url: str,
    cierre_relevo_minutos: int = 180,
    tema_general: int = 0,
) -> None:
    """El doctor pulsó «Hablar yo con el paciente». Nunca propaga.

    ------------------------------------------------------------------------------------
    El orden de los pasos está elegido, no es el que salió
    ------------------------------------------------------------------------------------

    La escritura en la base va ANTES de responder el callback, aunque Telegram solo dé diez
    segundos para responderlo. Es un `UPDATE` de una fila por clave primaria: tarda
    milisegundos, y a cambio el acuse puede decir la verdad -- «te llevo a su hilo» o «ya la
    tiene Fulano», que es justo lo que el segundo doctor necesita leer. Respondiendo primero,
    el acuse tendría que ser un «recibido» vacío y la carrera se resolvería en silencio.

    Y la escritura EN EL TEMA no es cosmética ni es el último paso por casualidad: es lo que
    de verdad lleva al doctor al hilo. `answerCallbackQuery(url=...)` hacia un tema del
    propio supergrupo responde `URL_INVALID` --comprobado contra la API--, así que el botón
    no puede navegar a nadie. Lo que navega es la NOTIFICACIÓN de ese mensaje.
    """
    try:
        telefono = await asyncio.to_thread(_telefono, database_url, id_conversacion)
        if telefono is None:
            await telegram.responder_callback(
                callback_id, "Esa conversación ya no existe.", alerta=True
            )
            return

        ocupada_por = await asyncio.to_thread(
            _activar_en_base, database_url, id_conversacion, doctor
        )
        if ocupada_por is not None:
            # La carrera normal: el escalamiento suena en el teléfono de todos a la vez.
            await telegram.responder_callback(
                callback_id, f"Ya la tiene {ocupada_por}.", alerta=True
            )
            return

        await telegram.responder_callback(callback_id, "Listo. Te llevo a su hilo.")

        try:
            tema = await _tema_abierto_para(
                telefono=telefono, database_url=database_url, telegram=telegram
            )
        except Exception:  # noqa: BLE001 -- `ErrorDeCanal` y también un timeout de red
            # Sin hilo no hay relevo. Deshacerlo es obligatorio: si no, Daniela queda callada
            # frente a un paciente con el que nadie puede hablar.
            log.exception("no se pudo abrir el hilo de +%s; se deshace el relevo", telefono)
            await asyncio.to_thread(
                _cerrar_en_base, database_url, id_conversacion, "tema_perdido"
            )
            await _decir_en_general(
                telegram,
                tema_general,
                "⚠️ No se pudo abrir el hilo de ese paciente, así que el relevo no quedó "
                "activo. Daniela sigue atendiéndolo.",
            )
            return

        await telegram.enviar_mensaje(
            _texto_de_bienvenida(doctor, cierre_relevo_minutos),
            tema_id=tema,
            teclado=teclado_devolver(id_conversacion),
            # El ÚNICO sitio del proyecto que manda un tema de paciente a sonar. Ver la NOTA
            # DEL SILENCIO de `canales.py`: es esta notificación la que lleva al doctor.
            silencioso=False,
        )

        if mensaje_id is not None:
            # Dejar el General ordenado. Los dos fallan en silencio a propósito: el relevo ya
            # está vivo y el doctor ya está en el hilo, así que un botón sin actualizar es
            # feo pero no es un fallo que valga deshacer nada.
            try:
                await telegram.editar_teclado(
                    mensaje_id, teclado_ir_al_hilo(telegram.enlace_al_tema(tema), doctor)
                )
            except Exception:  # noqa: BLE001
                log.warning("el botón del escalamiento %s quedó sin actualizar", mensaje_id)
            try:
                await asyncio.to_thread(_marcar_escalamiento, database_url, mensaje_id)
            except Exception:  # noqa: BLE001
                log.warning("no se pudo marcar el escalamiento %s como atendido", mensaje_id)

        _avisados.discard(id_conversacion)
        log.info("%s tomó la conversación %s (+%s)", doctor, id_conversacion, telefono)
    except Exception:  # noqa: BLE001 -- el webhook ya devolvió 200; nada puede propagar
        log.exception("falló la activación del relevo de %s", id_conversacion)


# ==========================================================================================
# Doctor -> paciente
# ==========================================================================================


def _adjunto(mensaje: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """`(file_id, tipo de Telegram, nombre)` de lo que trae el mensaje, o `None` si es texto.

    Las fotos llegan como una LISTA de tamaños, del más pequeño al más grande, y hay que
    coger el último: los primeros son miniaturas: mandarle al paciente una foto de 90 píxeles
    donde el doctor señaló algo no es entregar el mensaje.
    """
    if mensaje.get("photo"):
        return (mensaje["photo"][-1]["file_id"], "photo", None)
    for clave in ("document", "voice", "audio", "video", "video_note"):
        adjunto = mensaje.get(clave)
        if isinstance(adjunto, dict) and adjunto.get("file_id"):
            return (adjunto["file_id"], clave, adjunto.get("file_name"))
    return None


async def relevar_mensaje(
    mensaje: dict[str, Any], *, telegram, whatsapp, database_url: str
) -> None:
    """Un doctor escribió dentro de un tema. Lo manda al paciente si hay relevo. Nunca propaga.

    Lo que NO se reenvía, y por qué:

    · lo que se escribe en el General -- ahí los doctores hablan entre ellos, y ese es el
      sitio entero del diseño donde pueden hacerlo sin que el paciente lea una palabra;
    · lo que escribe otro bot;
    · los mensajes de servicio de Telegram (se creó el tema, se cerró, se reabrió), que
      llegan como mensajes normales y sin texto;
    · lo que se escribe en un tema SIN relevo vivo. Es el hueco del creador del grupo, al que
      Telegram no le puede quitar permisos: puede escribir en un tema cerrado. No se reenvía
      y se le dice en el propio hilo, que es todo lo que se puede hacer.
    """
    tema = mensaje.get("message_thread_id")
    # `is_topic_message` y no solo `message_thread_id`, y no es cinturón sobre tirante:
    # **el id de un tema ES el id de un mensaje del mismo supergrupo** --el del aviso de
    # servicio que lo creó--, y responder a un mensaje dentro del General también rellena
    # `message_thread_id`, con el id del mensaje al que se responde. Sin este campo, un
    # doctor contestando en el General a un mensaje cuyo id coincidiera con el tema de algún
    # paciente estaría escribiéndole a ese paciente. Telegram solo pone `is_topic_message`
    # cuando el mensaje está de verdad dentro de un tema.
    if not tema or not mensaje.get("is_topic_message"):
        return
    if (mensaje.get("from") or {}).get("is_bot"):
        return

    mensaje_id = mensaje.get("message_id")
    texto = mensaje.get("text") or mensaje.get("caption")
    adjunto = _adjunto(mensaje)
    if not texto and adjunto is None:
        # Un mensaje de servicio, una encuesta, una ubicación, un sticker. Callarse es lo
        # correcto: avisar por cada «el tema se reabrió» llenaría el hilo de ruido.
        return

    try:
        relevo = await asyncio.to_thread(_relevo_de_tema, database_url, tema)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo consultar el relevo del tema %s", tema)
        await _avisar_en_tema(
            telegram, tema, "⚠️ No pude comprobar si esta conversación está en relevo. "
            "<b>Esto NO le llegó al paciente.</b>"
        )
        return

    if relevo is None:
        await _avisar_en_tema(
            telegram,
            tema,
            "⚠️ <b>Esto NO le llegó al paciente.</b> Esta conversación no está en relevo: "
            "el hilo es su expediente, no un chat. Para hablarle, toca «Hablar yo con el "
            "paciente» en el escalamiento del General.",
        )
        return

    telefono = relevo["telefono"]
    try:
        if adjunto is not None:
            file_id, tipo_telegram, nombre = adjunto
            archivo = await telegram.descargar_archivo(file_id, nombre=nombre)
            media_id = await whatsapp.subir_media(archivo)
            await whatsapp.enviar_archivo(
                telefono,
                media_id,
                tipo=_TIPO_WHATSAPP_DE_TELEGRAM.get(tipo_telegram, "document"),
                pie=texto,
                nombre=archivo.nombre,
            )
        else:
            # LITERAL. Sin firma, sin prefijo y sin «te escribe el doctor»: quedó decidido que
            # el paciente no se entera de que cambió el interlocutor.
            await whatsapp.enviar_texto(telefono, texto)
    except Exception as e:  # noqa: BLE001 -- `ErrorDeCanal`, timeouts, un 429 de Meta
        log.exception("no se pudo relevar el mensaje %s hacia +%s", mensaje_id, telefono)
        await _avisar_en_tema(
            telegram,
            tema,
            f"⚠️ <b>Esto NO le llegó al paciente.</b>\n{_escapar(str(e))[:400]}\n\n"
            "Vuelve a mandarlo.",
        )
        return

    try:
        await asyncio.to_thread(_tocar, database_url, relevo["id_conversacion"])
    except Exception:  # noqa: BLE001
        # Solo empuja el reloj de cierre. Perderlo adelanta un cierre, no pierde un mensaje.
        log.warning("no se pudo empujar el reloj del relevo %s", relevo["id_conversacion"])

    if mensaje_id is not None:
        # El acuse. Una reacción y no una respuesta: un «entregado» por cada frase dejaría el
        # expediente ilegible.
        await telegram.reaccionar(mensaje_id)


# ==========================================================================================
# Cerrar -- la puerta única
# ==========================================================================================


#: Qué se escribe en el hilo según por qué se cerró. El doctor tiene que poder distinguir
#: «lo devolví yo» de «se me pasó el tiempo», porque lo segundo significa que el paciente
#: estuvo esperando.
_DESPEDIDA = {
    "devuelto_por_doctor": "🔒 Relevo cerrado. Daniela sigue con el paciente.",
    "tiempo_agotado": (
        "🔒 Relevo cerrado por inactividad. Daniela retoma la conversación.\n"
        "Si seguías en ello, vuelve a tomarla desde el escalamiento del General."
    ),
    "tema_perdido": "🔒 Relevo cerrado: el hilo dejó de estar disponible.",
}


async def cerrar(
    id_conversacion: str,
    *,
    motivo: str,
    telegram,
    database_url: str,
    telefono: str | None = None,
    tema_id: int | None = None,
    tema_general: int = 0,
) -> bool:
    """Se la devuelve a Daniela y vuelve a cerrar el tema. `False` si ya estaba cerrado.

    Idempotente porque las salidas se solapan de verdad: el doctor pulsa «Listo» en el mismo
    minuto en que el barrido lo da por vencido. El `False` corta la segunda antes de que
    escriba una segunda despedida en el hilo.

    El orden --base, luego Telegram-- es el que hay que tener si algo falla por el camino: si
    reventara al cerrar el tema, lo que queda es un tema abierto con Daniela ya despierta, y
    eso es un hilo de más. Al revés quedaría Daniela callada para siempre, que es un paciente
    sin nadie.
    """
    try:
        cerrado = await asyncio.to_thread(
            _cerrar_en_base, database_url, id_conversacion, motivo
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo cerrar en base el relevo de %s", id_conversacion)
        return False

    if not cerrado:
        return False

    _avisados.discard(id_conversacion)

    if telefono is None:
        try:
            telefono = await asyncio.to_thread(_telefono, database_url, id_conversacion)
        except Exception:  # noqa: BLE001
            telefono = None
    if tema_id is None and telefono:
        try:
            tema_id = await asyncio.to_thread(_tema_de, database_url, telefono)
        except Exception:  # noqa: BLE001
            tema_id = None

    if tema_id:
        # La despedida va ANTES de cerrar: en un tema ya cerrado el bot sigue pudiendo
        # escribir --es administrador-- pero si algo cambiara en Telegram, el mensaje que se
        # perdería sería el que explica por qué el doctor se quedó sin poder escribir.
        #
        # Y va silenciosa: el relevo terminó, el hilo vuelve a ser un expediente.
        await _avisar_en_tema(telegram, tema_id, _DESPEDIDA.get(motivo, "🔒 Relevo cerrado."))
        try:
            await telegram.cerrar_tema(tema_id)
        except Exception:  # noqa: BLE001
            log.error(
                "el tema %s quedó ABIERTO tras cerrar el relevo de %s: cualquiera del grupo "
                "puede escribirle al paciente hasta que alguien lo cierre a mano",
                tema_id,
                id_conversacion,
            )

    if telefono:
        try:
            await asyncio.to_thread(_marcar_abierto, database_url, telefono, False)
        except Exception:  # noqa: BLE001
            log.warning("no se pudo anotar el cierre del tema de +%s", telefono)

    if motivo == "tema_perdido":
        # El único motivo que señala un problema de USO y no de disponibilidad, y el único
        # invisible: Telegram no emite ningún evento al borrar un tema. Si no se dice aquí,
        # no se entera nadie.
        await _decir_en_general(
            telegram,
            tema_general,
            "⚠️ El hilo de un paciente en relevo dejó de existir. Daniela retomó la "
            "conversación sola. Si alguien borró el tema, que no lo haga: ahí vive su "
            "expediente.",
        )

    log.info("relevo de %s cerrado (%s)", id_conversacion, motivo)
    return True


# ==========================================================================================
# El barrido -- el cierre por tiempo
# ==========================================================================================


async def barrer(
    *,
    telegram,
    database_url: str,
    cierre_minutos: int,
    aviso_minutos: int,
    tema_general: int = 0,
) -> int:
    """Avisa a los que se acercan al límite y cierra los que lo pasaron. Devuelve cuántos cerró.

    Nunca propaga: lo llama una tarea de fondo que tiene que seguir viva mañana.
    """
    try:
        activos = await asyncio.to_thread(_activos, database_url)
    except Exception:  # noqa: BLE001
        log.warning("el barrido de relevos no pudo leer la base", exc_info=True)
        return 0

    cerrados = 0
    for uno in activos:
        id_conversacion = uno["id_conversacion"]
        callado = uno["minutos_callado"]

        if callado >= cierre_minutos:
            if await cerrar(
                id_conversacion,
                motivo="tiempo_agotado",
                telegram=telegram,
                database_url=database_url,
                telefono=uno["telefono"],
                tema_id=uno["topic_id"],
                tema_general=tema_general,
            ):
                cerrados += 1
        elif callado >= aviso_minutos and id_conversacion not in _avisados:
            _avisados.add(id_conversacion)
            if uno["topic_id"]:
                faltan = max(1, int(cierre_minutos - callado))
                # Este SÍ suena: es lo único que puede rescatar una conversación que el
                # doctor dejó a medias sin darse cuenta.
                await _avisar_en_tema(
                    telegram,
                    uno["topic_id"],
                    f"⏳ Llevas un rato sin escribirle. Si en {faltan} min no dices nada, "
                    "Daniela retoma la conversación sola.",
                    silencioso=False,
                )

    return cerrados


# ==========================================================================================
# Dos envíos que no pueden tumbar nada
# ==========================================================================================


async def _avisar_en_tema(
    telegram, tema_id: int, texto: str, *, silencioso: bool = True
) -> None:
    try:
        await telegram.enviar_mensaje(texto, tema_id=tema_id, silencioso=silencioso)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo escribir en el tema %s", tema_id, exc_info=True)


async def _decir_en_general(telegram, tema_general: int, texto: str) -> None:
    try:
        await telegram.enviar_mensaje(texto, tema_id=tema_general)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo escribir en el General", exc_info=True)


__all__ = [
    "PREFIJO_DEVOLVER",
    "PREFIJO_TOMAR",
    "activar",
    "barrer",
    "cerrar",
    "nombre_de_quien_pulsa",
    "relevar_mensaje",
    "teclado_devolver",
    "teclado_ir_al_hilo",
]

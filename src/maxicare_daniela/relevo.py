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
from datetime import datetime
from typing import Any

from . import persistencia
from .canales import _TIPO_WHATSAPP_DE_TELEGRAM
from .contratos import ZONA_BOGOTA
from .lectura import nombre_del_tema

log = logging.getLogger(__name__)

#: Lo que viaja en el `callback_data` de cada botón. Telegram lo limita a 64 bytes y lo deja
#: VISIBLE para cualquiera del grupo, así que lleva el uuid de la conversación y nunca el
#: teléfono del paciente.
PREFIJO_TOMAR = "relevo:"
PREFIJO_DEVOLVER = "devolver:"

#: El mismo botón, pero colgado del aviso de archivos del General, que es donde de verdad
#: hace falta: el escalamiento ocurre una vez y los archivos siguen llegando. Sin esto, el
#: doctor que ve entrar la tercera radiografía de alguien no tiene forma de tomar la
#: conversación -- medido el 14/09/2026, es lo primero que el usuario echó en falta.
#:
#: Lleva el TELÉFONO y no el uuid porque no hay uuid todavía: `ingesta.procesar_mensaje`
#: manda ese aviso ANTES de que `atencion.atender` cree la conversación. No es una fuga
#: nueva: el teléfono está escrito, en claro, en el texto de ese mismo aviso.
PREFIJO_TOMAR_TEL = "relevotel:"

#: Los dos botones del cierre conversado.
PREFIJO_SI_AGENDO = "agendo:"
PREFIJO_NO_AGENDO = "sincita:"

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


def teclado_tomar(telefono: str) -> dict:
    """«Hablar yo con el paciente», colgado del aviso de archivos. Ver `PREFIJO_TOMAR_TEL`."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Hablar yo con el paciente",
                    "callback_data": f"{PREFIJO_TOMAR_TEL}{telefono}",
                }
            ]
        ]
    }


def teclado_hubo_cita(id_conversacion: str) -> dict:
    """La pregunta del cierre. Dos botones y ninguna tercera vía a propósito.

    Un «cerrar sin contestar» parece amable y es el que todo el mundo pulsaría, y entonces la
    cita que el doctor acaba de acordar de viva voz no existiría en ninguna parte: sin cupo
    tomado --la clínica se la puede dar a otro-- y sin recordatorio. Quien no quiera contestar
    simplemente deja de tocar, y el barrido lo cierra por tiempo.
    """
    return {
        "inline_keyboard": [
            [{"text": "Sí, quedó agendada", "callback_data": f"{PREFIJO_SI_AGENDO}{id_conversacion}"}],
            [{"text": "No hubo cita", "callback_data": f"{PREFIJO_NO_AGENDO}{id_conversacion}"}],
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


def _transcripcion(database_url: str, telefono: str) -> list:
    with persistencia.conectar(database_url) as conn:
        return persistencia.transcripcion(conn, telefono)


def _marcar_cierre(database_url: str, id_conversacion: str, estado: str | None) -> None:
    with persistencia.conectar(database_url) as conn:
        persistencia.marcar_cierre_pendiente(conn, id_conversacion, estado)


def _guardar_tratamiento(database_url: str, id_conversacion: str, tratamiento: str) -> None:
    with persistencia.conectar(database_url) as conn:
        persistencia.guardar_tratamiento_del_cierre(conn, id_conversacion, tratamiento)


def _nombre_del_paciente(database_url: str, telefono: str) -> str | None:
    """Cómo se llama ese número, o `None` si no se sabe.

    `PENDIENTE` cuenta como no saberlo: es el marcador que pone `_ficha_para_el_relevo`, no
    un nombre. Devolverlo como si lo fuera es lo que metía «PENDIENTE · Cordales» en la
    agenda de la clínica.
    """
    with persistencia.conectar(database_url) as conn:
        fila = persistencia.buscar_paciente_por_telefono(conn, telefono)
    if not fila:
        return None
    nombre = (fila[1] or "").strip()
    if not nombre or nombre == NOMBRE_PENDIENTE:
        return None
    return nombre


def _nombrar(database_url: str, telefono: str, nombre: str) -> bool:
    with persistencia.conectar(database_url) as conn:
        return persistencia.nombrar_si_esta_pendiente(
            conn, telefono=telefono, nombre=nombre, pendiente=NOMBRE_PENDIENTE
        )


def _olvidar_tema(database_url: str, telefono: str) -> bool:
    with persistencia.conectar(database_url) as conn:
        return persistencia.olvidar_tema(conn, telefono)


def _conversacion_de(database_url: str, telefono: str) -> str:
    """La conversación viva de ese número, o una nueva. Para el botón del aviso de archivos.

    Ese botón viaja con el teléfono porque cuando se manda todavía no hay conversación
    --`ingesta.procesar_mensaje` corre antes que `atencion.atender`-- así que aquí hay que
    resolverla. Normalmente ya existe: entre que el aviso sale y el doctor lo pulsa, el turno
    de Daniela ya corrió.

    Y si no existe --con `MAXICARE_DANIELA_RESPONDE=0`, por ejemplo, donde Daniela no corre
    nunca-- se abre una. Es lo correcto: el doctor está empezando una conversación con esa
    persona, y sin fila no hay dónde anotar quién la tiene.
    """
    with persistencia.conectar(database_url) as conn:
        viva = persistencia.conversacion_viva(conn, telefono)
        if viva is not None:
            return viva[0]
        paciente = persistencia.buscar_paciente_por_telefono(conn, telefono)
        return persistencia.asegurar_conversacion(
            conn,
            telefono=telefono,
            paciente_id=paciente[0] if paciente else None,
            canal="whatsapp",
        )


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


def _mencion(doctor: str, doctor_id: int | None) -> str:
    """El nombre del doctor como MENCIÓN, que es lo que hace sonar el hilo en su teléfono.

    ------------------------------------------------------------------------------------
    Esto es la navegación, y por eso no es cosmético
    ------------------------------------------------------------------------------------

    Un botón de callback NO puede llevar a nadie a ninguna parte: `answerCallbackQuery(url=)`
    hacia un tema del propio supergrupo responde `URL_INVALID` --medido--, así que lo único
    que mueve al doctor del General al hilo del paciente es la NOTIFICACIÓN de este mensaje.
    Y un mensaje normal en un tema de foro depende de cómo tenga cada uno sus avisos: quien
    tenga el grupo silenciado o no siga ese hilo no ve nada, pulsa el botón y se queda donde
    estaba. Fue exactamente la queja del 14/09/2026.

    Una mención no depende de eso: Telegram la notifica igual con el grupo silenciado, y al
    tocarla abre el tema. `tg://user?id=` y no `@usuario` porque el `@` no lo tiene todo el
    mundo y no se puede inventar.

    Sin `doctor_id` degrada al nombre a secas: un enlace mal formado rompería el mensaje
    entero --el que lleva el botón de salida-- y eso es peor que no notificar.
    """
    nombre = _escapar(doctor)
    if not doctor_id:
        return nombre
    return f'<a href="tg://user?id={int(doctor_id)}">{nombre}</a>'


def _texto_de_bienvenida(doctor: str, minutos: int, doctor_id: int | None = None) -> str:
    """Lo primero que el doctor lee al entrar al hilo. Tiene que caber en una notificación.

    Dice las dos cosas que no puede no saber: que lo que escriba SALE hacia un teléfono real,
    y que esto se cierra solo. Lo demás sobra -- un manual dentro del hilo no lo lee nadie.
    """
    horas = minutos / 60.0
    cuanto = f"{horas:.0f} h" if horas >= 1 else f"{minutos} min"
    return (
        f"🔔 <b>Tienes la conversación</b> — {_mencion(doctor, doctor_id)}\n\n"
        "Lo que escribas <b>aquí dentro</b> le llega al paciente por WhatsApp tal cual, "
        "sin firma. También le llegan las fotos y los audios que mandes.\n\n"
        f"Daniela está callada. Se vuelve a activar sola tras {cuanto} sin que escribas, "
        "con el botón de abajo, o si cierras este hilo.\n\n"
        "<i>Este mensaje queda anclado arriba: el botón está siempre a mano.</i>"
    )


#: Telegram corta un mensaje en 4096 caracteres y RECHAZA el que se pase. Una transcripción
#: larga no puede quedarse en «no salió»: se recorta por el principio, que es la parte que
#: menos falta le hace al doctor para entrar en contexto.
TOPE_TRANSCRIPCION = 3500


def _formatear_transcripcion(lineas: list) -> str:
    """La conversación, lista para leer en el hilo. Cadena vacía si no hay nada que contar."""
    if not lineas:
        return ""
    partes = []
    for quien, texto, cuando in lineas:
        hora = cuando.strftime("%d/%m %H:%M") if hasattr(cuando, "strftime") else ""
        etiqueta = "🦷 <b>Paciente</b>" if quien == "paciente" else "🤖 <b>Daniela</b>"
        partes.append(f"{etiqueta} <i>{hora}</i>\n{_escapar(texto)}")
    cuerpo = "\n\n".join(partes)
    if len(cuerpo) > TOPE_TRANSCRIPCION:
        cuerpo = "…\n\n" + cuerpo[-TOPE_TRANSCRIPCION:]
    return cuerpo


async def _volcar_contexto(telegram, tema: int, telefono: str, database_url: str) -> None:
    """Le deja al doctor, dentro del hilo, lo que se dijeron antes de que él llegara.

    Existe por el primer relevo real (14/09/2026): el doctor entró a un hilo RECIÉN CREADO y
    no había nada. Todo lo del paciente estaba en otro sitio --sus archivos en el General,
    sus textos sin archivar en ninguna parte-- y tuvo que empezar preguntando quién era.

    Va SILENCIOSO aunque el hilo esté en relevo: el aviso de «tienes la conversación» ya sonó
    y es el que lleva al doctor aquí. Dos notificaciones seguidas por lo mismo es ruido.

    Nunca propaga ni bloquea: si la base no responde, el relevo se activa igual y el doctor
    entra a ciegas. Peor es que no pueda entrar.
    """
    try:
        lineas = await asyncio.to_thread(_transcripcion, database_url, telefono)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo leer la transcripción de +%s", telefono, exc_info=True)
        return

    cuerpo = _formatear_transcripcion(lineas)
    if not cuerpo:
        # Honesto y útil: decirle que no hay nada es distinto de dejarle el hilo en blanco y
        # que se pregunte si el sistema falló.
        cuerpo = (
            "<i>No hay conversación previa registrada con este número.</i>\n"
            "Si mandó archivos antes de tener hilo, están en el General."
        )
    await _avisar_en_tema(
        telegram, tema, f"📄 <b>Lo que se habló hasta ahora</b>\n\n{cuerpo}", silencioso=True
    )


async def activar(
    *,
    id_conversacion: str,
    doctor: str,
    callback_id: str,
    mensaje_id: int | None,
    telegram,
    database_url: str,
    doctor_id: int | None = None,
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

    Y la escritura EN EL TEMA no es cosmética: es lo que de verdad lleva al doctor al hilo.
    `answerCallbackQuery(url=...)` hacia un tema del propio supergrupo responde `URL_INVALID`
    --comprobado contra la API--, así que el botón NO PUEDE navegar. Lo que navega es la
    notificación de ese mensaje, y por eso lleva una mención (ver `_mencion`).

    ------------------------------------------------------------------------------------
    Lo que va antes del volcado, y por qué (14/09/2026)
    ------------------------------------------------------------------------------------

    El teclado del General se cambiaba al FINAL, después de volcar la transcripción. El
    volcado lee la base y manda otro mensaje, así que durante esos segundos el doctor que
    acababa de pulsar seguía viendo «Hablar yo con el paciente» y ninguna puerta hacia el
    hilo: si la notificación no le llegaba, se quedaba en el General mirando el mismo botón
    de antes. Ahora el enlace se pone en cuanto existe el mensaje al que apunta, y el volcado
    --que es lo lento y lo que menos prisa tiene-- va detrás.
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

        # El acuse no promete lo que el botón no puede hacer. Decía «te llevo a su hilo» y no
        # llevaba a nadie: el doctor leía eso, no pasaba nada, y volvía a pulsar.
        await telegram.responder_callback(
            callback_id, "Es tuya. Te acabo de escribir en su hilo: toca el aviso."
        )

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

        bienvenida = await telegram.enviar_mensaje(
            _texto_de_bienvenida(doctor, cierre_relevo_minutos, doctor_id),
            tema_id=tema,
            teclado=teclado_devolver(id_conversacion),
            # El ÚNICO sitio del proyecto que manda un tema de paciente a sonar. Ver la NOTA
            # DEL SILENCIO de `canales.py`: es esta notificación la que lleva al doctor.
            silencioso=False,
        )

        # Anclado, porque este mensaje lleva el botón de salida y es el PRIMERO del hilo.
        # Sin esto, el doctor que ha hablado veinte frases tiene que subir hasta arriba del
        # todo para devolver el control -- y el que no lo hace deja el relevo abierto hasta
        # que lo corta el barrido, con Daniela callada mientras tanto.
        await telegram.anclar_mensaje(bienvenida)

        if mensaje_id is not None:
            # LA PUERTA. Va aquí, en cuanto existe el mensaje al que apunta, y no al final:
            # es lo que el doctor tiene bajo el dedo un segundo después de pulsar. El enlace
            # lleva el id de la bienvenida, así que aterriza DENTRO del hilo y en ese mensaje
            # --ver `canales.enlace_al_tema`--, no arriba del todo.
            #
            # Falla en silencio a propósito: el relevo ya está vivo, y un botón sin actualizar
            # es feo pero no es un fallo que valga deshacer nada.
            try:
                await telegram.editar_teclado(
                    mensaje_id,
                    teclado_ir_al_hilo(
                        telegram.enlace_al_tema(tema, bienvenida), doctor
                    ),
                )
            except Exception:  # noqa: BLE001
                log.warning("el botón del escalamiento %s quedó sin actualizar", mensaje_id)
            try:
                await asyncio.to_thread(_marcar_escalamiento, database_url, mensaje_id)
            except Exception:  # noqa: BLE001
                log.warning("no se pudo marcar el escalamiento %s como atendido", mensaje_id)

        # Y justo debajo de la bienvenida, lo que se habló antes de que él llegara. Después
        # del aviso y no antes: lo primero que tiene que ver al abrir la notificación es que
        # la conversación es suya y que lo que escriba SALE, no un muro de texto. Y después
        # del enlace, porque esto lee la base y es lo único lento del camino.
        await _volcar_contexto(telegram, tema, telefono, database_url)

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


async def cerrar_por_tema_cerrado(
    mensaje: dict[str, Any],
    *,
    telegram,
    database_url: str,
    tema_general: int = 0,
) -> None:
    """El doctor cerró el hilo a mano. Eso ES devolver el control. Nunca propaga.

    ------------------------------------------------------------------------------------
    Por qué esta salida existe, y por qué NO pregunta por la cita
    ------------------------------------------------------------------------------------

    Cerrar el tema es el gesto que sale natural al terminar de hablar, y hasta hoy dejaba el
    sistema justo en el estado que el no negociable 15 prohíbe: el tema cerrado --o sea, sin
    canal hacia el paciente-- pero `tomada_por` todavía puesto, con Daniela callada hasta que
    el barrido lo cortara tres horas después. Las dos cosas tienen que dejar de ser verdad
    juntas, y esta es la cuarta forma de conseguirlo.

    No abre el diálogo de la cita a propósito, y es la misma razón: preguntar deja el relevo
    TOMADO mientras se espera la respuesta, y con el tema ya cerrado eso es exactamente el
    estado prohibido. El diálogo completo vive en el botón «Listo, que siga Daniela», que
    desde hoy va ANCLADO en la cabecera del hilo y no hay que ir a buscarlo. Así que esta es
    la salida rápida --sin cita-- y aquella la completa; la despedida lo dice y deja el botón
    de volver a entrar para quien cerró sin querer.

    El motivo es `devuelto_por_doctor` y no uno nuevo: lo fue. El CHECK de la 003 sigue
    cerrado con tres.

    Idempotente por construcción: cuando es `relevo.cerrar` quien cierra el tema, Telegram
    emite este mismo evento, pero para entonces ya no hay relevo vivo que encontrar.
    """
    tema = mensaje.get("message_thread_id")
    if not tema:
        return
    try:
        vivo = await asyncio.to_thread(_relevo_de_tema, database_url, tema)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo mirar el relevo del tema %s", tema, exc_info=True)
        return
    if not vivo:
        return

    await cerrar(
        vivo["id_conversacion"],
        motivo="devuelto_por_doctor",
        telegram=telegram,
        database_url=database_url,
        telefono=vivo.get("telefono"),
        tema_id=tema,
        tema_general=tema_general,
        doctor=vivo.get("doctor") or "un doctor",
    )


async def relevar_mensaje(
    mensaje: dict[str, Any],
    *,
    telegram,
    whatsapp,
    database_url: str,
    calendario=None,
    tema_general: int = 0,
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

    if relevo.get("cierre_pendiente") == "esperando_nombre":
        # Igual que los otros dos pasos: ESTE mensaje contesta a la pregunta anterior. Un
        # «María Fernanda Ríos» llegándole a su propio WhatsApp sería, como mínimo, raro.
        await recibir_el_nombre(
            texto or "",
            relevo,
            telegram=telegram,
            database_url=database_url,
            tema_id=tema,
        )
        return

    if relevo.get("cierre_pendiente") == "esperando_tratamiento":
        # Igual que la fecha: ESTE mensaje contesta a la pregunta anterior y no sale hacia el
        # paciente. Un «control post-operatorio» llegándole a su WhatsApp sin contexto sería
        # como mínimo raro, y con la frase equivocada, alarmante.
        await recibir_el_tratamiento(
            texto or "",
            relevo,
            telegram=telegram,
            database_url=database_url,
            tema_id=tema,
        )
        return

    if relevo.get("cierre_pendiente") == "esperando_fecha":
        # El doctor acaba de decir que agendó y se le pidió la fecha: ESTE mensaje es la
        # fecha, no algo para el paciente. Es justo por esto que el estado vive en la base y
        # no en memoria -- un reinicio del proceso aquí le mandaría «15/09 14:30» al paciente.
        await recibir_la_fecha(
            texto or "",
            relevo,
            telegram=telegram,
            database_url=database_url,
            tema_id=tema,
            calendario=calendario,
            tema_general=tema_general,
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
    "devuelto_por_doctor": (
        "🔒 Relevo cerrado. Daniela sigue con el paciente.\n"
        "Si acordaste una cita y no la registraste, vuelve a tomar la conversación y "
        "ciérrala con «Listo, que siga Daniela»."
    ),
    "tiempo_agotado": (
        "🔒 Relevo cerrado por inactividad. Daniela retoma la conversación.\n"
        "Si seguías en ello, vuelve a tomarla aquí mismo."
    ),
    "tema_perdido": "🔒 Relevo cerrado: el hilo dejó de estar disponible.",
}


# ==========================================================================================
# El cierre conversado: «¿quedó agendada una cita?»
# ==========================================================================================
#
# Pulsar «Listo» ya no cierra de golpe. Antes de soltar la conversación se le pregunta al
# doctor si acordó una cita, y si dijo que sí se le piden fecha y hora para agendarla DE
# VERDAD. Sale de dos agujeros que el primer relevo real dejó a la vista:
#
#   · el doctor le dijo al paciente «ven mañana» y esa cita no existía en ninguna parte: sin
#     cupo tomado --la clínica se la podía dar a otro-- y sin recordatorio;
#   · Daniela retomó la conversación sin saber que había pasado nada.
#
# Y resuelve los dos respetando el muro, que es lo que hace que sea esta pregunta y no un
# resumen de lo hablado: **lo único que cruza hacia Daniela es una CITA**, que no es
# contenido clínico, y lo decide un humano pulsando un botón. Lo que el doctor escribió no
# viaja nunca -- ni literal ni resumido.
#
# Mientras dura la pregunta el relevo sigue TOMADO, así que Daniela sigue callada. Si el
# doctor abandona a medias, lo cierra el barrido por `tiempo_agotado`.


#: Cómo se le pide la fecha. Estricto a propósito y sin adivinar «mañana a las 10»: una fecha
#: mal interpretada es un paciente presentándose a la hora equivocada, y eso es peor que
#: pedirle al doctor que escriba diez caracteres bien.
FORMATOS_FECHA = ("%d/%m/%Y %H:%M", "%d/%m %H:%M", "%Y-%m-%d %H:%M")

EJEMPLO_FECHA = "15/09 14:30"


def _leer_fecha(texto: str, ahora: datetime) -> datetime | None:
    """La fecha que escribió el doctor, o `None` si no se entiende. Nunca adivina.

    Sin año, se asume el año en curso -- y si eso deja la fecha en el pasado, el siguiente,
    porque nadie agenda hacia atrás: un «02/01 09:00» escrito el 28 de diciembre es de enero
    del año que viene, no de hace once meses.
    """
    limpio = " ".join((texto or "").strip().replace(".", "/").split())
    for formato in FORMATOS_FECHA:
        try:
            leida = datetime.strptime(limpio, formato)
        except ValueError:
            continue
        if "%Y" not in formato:
            leida = leida.replace(year=ahora.year)
            if leida.replace(tzinfo=ahora.tzinfo) < ahora:
                leida = leida.replace(year=ahora.year + 1)
        return leida.replace(tzinfo=ahora.tzinfo)
    return None


def _agendar(
    database_url: str,
    *,
    id_conversacion: str,
    telefono: str,
    inicio: datetime,
    calendario,
    doctor: str,
    tratamiento: str,
) -> tuple[bool, str]:
    """Crea la cita que el doctor acordó de viva voz. `(se_agendo, texto para el hilo)`.

    Mismo orden que `herramientas._crear_cita`, y por la misma razón: **cupo, luego Google,
    luego la fila**. El cupo es lo que reserva la hora de verdad --`reservas` tiene
    `UNIQUE (inicio, cupo_num)` y ESE constraint es el control de capacidad-- así que si está
    lleno hay que decirlo antes de tocar nada más.

    Que `tomar_cupo` pueda decir que no es justo el valor de esto: el doctor que acaba de
    prometer las 10:00 se entera AHORA de que las 10:00 ya están dadas, y no cuando se
    presenten dos pacientes a la vez.

    Sincrónica: `persistencia` es psycopg y `calendario` es el cliente de Google. Siempre
    dentro de `asyncio.to_thread`.
    """
    from .calendario import ErrorDeCalendario

    with persistencia.conectar(database_url) as conn:
        operativa = persistencia.leer_configuracion(conn)
        capacidad = int(operativa.get("capacidad_por_hora", 2))
        duracion = int(operativa.get("duracion_cita_minutos", 60))
        paciente = persistencia.buscar_paciente_por_telefono(conn, telefono)
        nombre = paciente[1] if paciente else NOMBRE_PENDIENTE

        cupo = persistencia.tomar_cupo(
            conn,
            inicio=inicio,
            capacidad=capacidad,
            # La clave la arma el código, nunca el modelo ni el doctor. Lleva el id de la
            # conversación delante, así que pulsar el botón dos veces no toma dos cupos.
            clave_idempotencia=f"relevo:{id_conversacion}:{inicio.isoformat()}",
            conversacion_id=id_conversacion,
        )
        if cupo is None:
            return (
                False,
                f"⚠️ <b>No se pudo agendar {_formatear(inicio)}</b>: esa hora ya está llena "
                f"({capacidad} pacientes por hora). Escribe otra hora, o agéndala desde el "
                "panel si hay que hacer una excepción.",
            )

    try:
        evento = calendario.crear_evento(
            inicio=inicio,
            duracion_minutos=duracion,
            titulo=f"{nombre} · {tratamiento}",
            descripcion=(
                f"Cita acordada directamente por {doctor} durante un relevo.\n"
                f"Teléfono: +{telefono}"
            ),
        )
    except (ErrorDeCalendario, Exception) as e:  # noqa: BLE001
        # El cupo queda tomado a propósito: prefiero una hora bloqueada de más --que un
        # humano puede liberar-- a una cita confirmada de viva voz que el calendario de la
        # clínica no muestra. Es el mismo criterio del no negociable 1.
        log.exception("no se pudo crear el evento del relevo de %s", id_conversacion)
        return (
            False,
            "⚠️ <b>La hora quedó reservada pero Google Calendar no respondió</b>, así que el "
            f"evento no aparece en la agenda: {_escapar(str(e))[:200]}. Créalo a mano.",
        )

    with persistencia.conectar(database_url) as conn:
        paciente_id = persistencia.asegurar_paciente(
            conn, nombre_completo=nombre, telefono=telefono
        )
        persistencia.registrar_cita(
            conn,
            reserva_id=cupo[0],
            conversacion_id=id_conversacion,
            paciente_id=paciente_id,
            nombre_completo=nombre,
            telefono=telefono,
            # Tal cual lo escribió el doctor. La ÚNICA vía del proyecto que no lo valida
            # contra la lista viva de `tratamientos`: ver `pedir_el_tratamiento`.
            tratamiento=tratamiento,
            inicio=inicio,
            duracion_minutos=duracion,
            evento_calendar_id=evento,
        )

    return (
        True,
        f"✅ <b>Cita registrada: {_formatear(inicio)}</b>\n"
        f"{_escapar(tratamiento)}\n"
        "Toma el cupo, está en el calendario de la clínica y el paciente entra en los "
        "recordatorios.",
    )


def _formatear(cuando: datetime) -> str:
    return cuando.strftime("%d/%m/%Y a las %H:%M")


async def _avisar_a_daniela(database_url: str, id_conversacion: str, texto: str) -> None:
    """Mete una nota en la memoria de Daniela para que no retome a ciegas. Nunca propaga.

    ------------------------------------------------------------------------------------
    LO QUE ENTRA AQUÍ ES EL MURO
    ------------------------------------------------------------------------------------

    Esto escribe en el contexto del agente que le habla DIRECTAMENTE al paciente. Lo que
    escribió el doctor no puede pasar por aquí --ni literal ni resumido-- porque puede ser
    contenido clínico, y que eso no llegue a Daniela es la decisión central del proyecto
    (`frontera-agentes.md`). Los únicos que llaman a esta función le pasan hechos no
    clínicos: que hubo un relevo, y la fecha de una cita.

    Va como «AVISO DEL SISTEMA» con rol de usuario, que es la misma convención que usa
    `CORRECCION` en la regeneración: un rol `system` a mitad de historial no está garantizado
    que el SDK lo conserve, y el modelo trata este prefijo como lo que es.
    """
    try:
        sesion = persistencia.sesion_de_agente(id_conversacion, database_url=database_url)
        # Transporte puro: el «AVISO DEL SISTEMA» lo trae ya el texto (`_nota_del_relevo`),
        # para que lo que acaba en el contexto del modelo sea UNA cadena que una prueba pueda
        # mirar entera. Componerla aquí la partiría en dos mitades y la mitad que decide si
        # el muro aguanta quedaría fuera de lo que se comprueba.
        await sesion.add_items([{"role": "user", "content": texto}])
    except Exception:  # noqa: BLE001
        log.warning(
            "no se pudo avisar a Daniela del relevo de %s; retomará sin contexto",
            id_conversacion,
            exc_info=True,
        )


def _nota_del_relevo(doctor: str, cita: datetime | None) -> str:
    """Lo único que cruza del relevo hacia Daniela. Hechos, nunca lo que se dijo."""
    base = (
        "AVISO DEL SISTEMA. "
        f"Un doctor de la clínica ({doctor}) acaba de hablar directamente con este paciente "
        "por este mismo WhatsApp. NO sabes qué se dijeron y no puedes suponerlo. Si el "
        "paciente da algo por hablado, no lo contradigas ni lo inventes: pregúntale a qué se "
        "refiere, o escala."
    )
    if cita is None:
        return base
    return (
        f"{base} Lo único que quedó registrado es que le agendaron una cita para el "
        f"{_formatear(cita)}. Esa cita ya está en el sistema: no la vuelvas a agendar y no "
        "ofrezcas otra hora salvo que el paciente pida cambiarla."
    )


async def iniciar_cierre(
    id_conversacion: str, *, telegram, database_url: str, tema_id: int | None = None
) -> None:
    """El doctor pulsó «Listo». Antes de soltar, la pregunta. Nunca propaga."""
    try:
        if tema_id is None:
            telefono = await asyncio.to_thread(_telefono, database_url, id_conversacion)
            tema_id = (
                await asyncio.to_thread(_tema_de, database_url, telefono) if telefono else None
            )
        if tema_id is None:
            # Sin hilo no hay dónde preguntar. Se cierra como siempre.
            await cerrar(
                id_conversacion,
                motivo="devuelto_por_doctor",
                telegram=telegram,
                database_url=database_url,
            )
            return

        await asyncio.to_thread(_marcar_cierre, database_url, id_conversacion, "preguntado")
        await telegram.enviar_mensaje(
            "Antes de soltarla: <b>¿quedó agendada una cita?</b>\n\n"
            "Si acordaste una hora y no la registras aquí, el sistema no la conoce: el cupo "
            "sigue libre para otro paciente y nadie le va a recordar nada.",
            tema_id=tema_id,
            teclado=teclado_hubo_cita(id_conversacion),
            silencioso=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("falló la pregunta de cierre de %s", id_conversacion)


#: Lo que cabe en `citas.tratamiento` sin que el título del evento de Google Calendar se
#: vuelva ilegible. No es un límite de la columna --es TEXT-- sino de dónde se lee después.
TOPE_TRATAMIENTO = 80

#: Lo mismo para el nombre, que comparte ese título.
TOPE_NOMBRE = 80


async def pedir_los_datos_de_la_cita(
    id_conversacion: str, *, telegram, database_url: str, tema_id: int
) -> None:
    """Dijo que sí agendó. Empieza por el primer dato que falte. Nunca propaga.

    La cita tiene que quedar «como si la hubiera hecho el paciente», y una que hace el
    paciente por WhatsApp lleva su nombre porque Daniela se lo pregunta antes de agendar. La
    que sale de un relevo no lo llevaba: el número que escribe por primera vez no tiene ficha
    y el relevo se la crea como `PENDIENTE`, así que la cita entraba en la agenda de la
    clínica como «PENDIENTE · Cordales». Y ese no es el caso raro -- el paciente nuevo con
    dolor agudo es justo el que más escala.

    Se pregunta SOLO cuando falta. Con nombre de verdad, el cierre sigue siendo dos preguntas.

    Si la base no contesta, sigue por el tratamiento como hasta ahora: preguntar de más un
    nombre que ya se tiene es molesto, pero quedarse aquí dejaría la cita sin registrar.
    """
    try:
        telefono = await asyncio.to_thread(_telefono, database_url, id_conversacion)
        falta = telefono is not None and (
            await asyncio.to_thread(_nombre_del_paciente, database_url, telefono)
        ) is None
    except Exception:  # noqa: BLE001
        log.warning(
            "no se pudo mirar el nombre de %s; se sigue sin preguntarlo",
            id_conversacion,
            exc_info=True,
        )
        falta = False

    if falta:
        await pedir_el_nombre(
            id_conversacion, telegram=telegram, database_url=database_url, tema_id=tema_id
        )
        return
    await pedir_el_tratamiento(
        id_conversacion, telegram=telegram, database_url=database_url, tema_id=tema_id
    )


async def pedir_el_nombre(
    id_conversacion: str, *, telegram, database_url: str, tema_id: int
) -> None:
    """Su próximo mensaje en el hilo es el nombre del paciente, no algo para el paciente."""
    try:
        await asyncio.to_thread(
            _marcar_cierre, database_url, id_conversacion, "esperando_nombre"
        )
        await telegram.enviar_mensaje(
            "Este número todavía no tiene nombre en el sistema. "
            "<b>¿Cómo se llama el paciente?</b>\n\n"
            "<code>María Fernanda Ríos</code>\n\n"
            "<b>Tu próximo mensaje en este hilo NO le llega al paciente.</b>\n"
            "<i>Con esto queda su cita en la agenda y sus recordatorios.</i>",
            tema_id=tema_id,
            silencioso=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo pedir el nombre en %s", id_conversacion)


async def recibir_el_nombre(
    texto: str,
    relevo: dict[str, Any],
    *,
    telegram,
    database_url: str,
    tema_id: int,
) -> None:
    """El doctor escribió cómo se llama. Se guarda y se sigue. Nunca propaga."""
    id_conversacion = relevo["id_conversacion"]
    limpio = " ".join((texto or "").split())[:TOPE_NOMBRE]
    if not limpio:
        await _avisar_en_tema(
            telegram,
            tema_id,
            "No leí nada. Escribe el nombre del paciente.",
            silencioso=False,
        )
        return  # sigue esperando: el estado no se toca

    try:
        await asyncio.to_thread(_nombrar, database_url, relevo["telefono"], limpio)
    except Exception:  # noqa: BLE001
        # No se para el cierre por esto: la cita con el nombre a medias vale mucho más que
        # ninguna cita. `_agendar` volverá a leer la ficha y usará lo que haya.
        log.exception("no se pudo guardar el nombre de %s", id_conversacion)

    await _avisar_en_tema(
        telegram, tema_id, f"Anotado: <b>{_escapar(limpio)}</b>", silencioso=True
    )
    await pedir_el_tratamiento(
        id_conversacion, telegram=telegram, database_url=database_url, tema_id=tema_id
    )


async def pedir_el_tratamiento(
    id_conversacion: str, *, telegram, database_url: str, tema_id: int
) -> None:
    """Dijo que sí agendó. Primero DE QUÉ es la cita; la fecha viene después.

    Se pregunta en vez de rellenarlo: hasta hoy toda cita salida de un relevo se registraba
    como `"valoracion"`, un valor fijo que además no es ninguna de las catorce claves de
    `tratamientos`. La regla dura 3 dice justo eso -- lo que no se sabe no se rellena con un
    valor plausible.

    **Texto libre, y aquí no se valida contra la lista viva.** Es el único sitio del proyecto
    donde eso pasa, y es una decisión explícita del cliente (14/09/2026): el doctor que acaba
    de hablar con el paciente sabe de qué es la cita mejor que un catálogo cerrado. Queda
    dicho lo que implica, porque no es invisible: Daniela LEE `citas.tratamiento` y se lo
    repite al paciente al comprobar su cita.

    **«Solo eso — la fecha te la pido después» no es relleno.** En el primer cierre real
    (14/09/2026) el doctor contestó a esta pregunta «Cordales fecha 14/09 del 2026 a las 4 pm»
    --lo metió todo de golpe, que es lo natural-- y eso acabó entero en el título del evento y
    en la frase que Daniela le repite al paciente. La pregunta tiene que decir dónde termina.
    Lo que NO se hace es intentar sacar la fecha de aquí: una fecha adivinada es un paciente
    presentándose a la hora equivocada, y por eso el paso siguiente la pide con formato.
    """
    try:
        await asyncio.to_thread(
            _marcar_cierre, database_url, id_conversacion, "esperando_tratamiento"
        )
        await telegram.enviar_mensaje(
            "¿De qué es la cita? <b>Solo eso — la fecha te la pido después.</b>\n\n"
            "<code>Cordales</code>\n"
            "<code>Control post-operatorio</code>\n"
            "<code>Valoración para implantes</code>\n\n"
            "<b>Tu próximo mensaje en este hilo NO le llega al paciente.</b>\n"
            "<i>Ojo: esto es lo que Daniela le dirá al paciente cuando le recuerde su cita.</i>",
            tema_id=tema_id,
            silencioso=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo pedir el tratamiento en %s", id_conversacion)


async def recibir_el_tratamiento(
    texto: str,
    relevo: dict[str, Any],
    *,
    telegram,
    database_url: str,
    tema_id: int,
) -> None:
    """El doctor escribió de qué es la cita. Se guarda y se le pide la fecha. Nunca propaga."""
    id_conversacion = relevo["id_conversacion"]
    limpio = " ".join((texto or "").split())[:TOPE_TRATAMIENTO]
    if not limpio:
        await _avisar_en_tema(
            telegram,
            tema_id,
            "No leí nada. Escribe de qué es la cita, por ejemplo <code>Cordales</code>.",
            silencioso=False,
        )
        return  # sigue esperando: el estado no se toca

    try:
        await asyncio.to_thread(
            _guardar_tratamiento, database_url, id_conversacion, limpio
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo guardar el tratamiento de %s", id_conversacion)
        await _avisar_en_tema(
            telegram,
            tema_id,
            "⚠️ No pude guardar eso. Vuelve a escribirlo.",
            silencioso=False,
        )
        return

    await _avisar_en_tema(
        telegram, tema_id, f"Anotado: <b>{_escapar(limpio)}</b>", silencioso=True
    )
    await pedir_la_fecha(
        id_conversacion, telegram=telegram, database_url=database_url, tema_id=tema_id
    )


async def pedir_la_fecha(
    id_conversacion: str, *, telegram, database_url: str, tema_id: int
) -> None:
    """Ya dijo de qué es. Su PRÓXIMO mensaje en el hilo es la fecha, no algo para el paciente."""
    try:
        await asyncio.to_thread(
            _marcar_cierre, database_url, id_conversacion, "esperando_fecha"
        )
        await telegram.enviar_mensaje(
            "Ahora la fecha y la hora, así:\n\n"
            f"<code>{EJEMPLO_FECHA}</code>\n\n"
            "<b>Tu próximo mensaje en este hilo NO le llega al paciente</b>: lo leo como la "
            "fecha.",
            tema_id=tema_id,
            silencioso=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo pedir la fecha en %s", id_conversacion)


async def recibir_la_fecha(
    texto: str,
    relevo: dict[str, Any],
    *,
    telegram,
    database_url: str,
    tema_id: int,
    calendario,
    tema_general: int = 0,
) -> None:
    """El doctor escribió la fecha. Se agenda y se cierra. Nunca propaga."""
    id_conversacion = relevo["id_conversacion"]
    ahora = datetime.now(ZONA_BOGOTA)
    cuando = _leer_fecha(texto, ahora)

    if cuando is None:
        await _avisar_en_tema(
            telegram,
            tema_id,
            f"No entendí «{_escapar(texto[:60])}» como una fecha. Escríbela así: "
            f"<code>{EJEMPLO_FECHA}</code>",
            silencioso=False,
        )
        return  # sigue esperando: el estado no se toca

    if cuando < ahora:
        await _avisar_en_tema(
            telegram,
            tema_id,
            f"{_formatear(cuando)} ya pasó. Escribe una hora futura.",
            silencioso=False,
        )
        return

    if calendario is None:
        agendada, aviso = False, (
            "⚠️ No hay calendario disponible ahora mismo, así que la cita no se registró. "
            "Agéndala desde el panel."
        )
    else:
        agendada, aviso = await asyncio.to_thread(
            _agendar,
            database_url,
            id_conversacion=id_conversacion,
            telefono=relevo["telefono"],
            inicio=cuando,
            calendario=calendario,
            doctor=relevo["doctor"],
            # Lo que contestó en el paso anterior. El `or` es la red por si alguien llega
            # aquí sin pasar por ahí --un relevo empezado con la versión de ayer, un estado
            # a medias-- y dice lo que es en vez de inventar un tratamiento plausible.
            tratamiento=relevo.get("cierre_tratamiento") or "Sin identificar",
        )

    await _avisar_en_tema(telegram, tema_id, aviso, silencioso=False)
    if not agendada:
        # Se queda esperando otra hora: el doctor acaba de prometerle una cita a alguien y
        # cerrar aquí dejaría esa promesa sin registrar, que es el agujero que esto cierra.
        return

    await cerrar(
        id_conversacion,
        motivo="devuelto_por_doctor",
        telegram=telegram,
        database_url=database_url,
        telefono=relevo["telefono"],
        tema_id=tema_id,
        tema_general=tema_general,
        cita=cuando,
        doctor=relevo["doctor"],
    )


async def cerrar(
    id_conversacion: str,
    *,
    motivo: str,
    telegram,
    database_url: str,
    telefono: str | None = None,
    tema_id: int | None = None,
    tema_general: int = 0,
    cita: datetime | None = None,
    doctor: str = "un doctor",
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
    # Apagar el diálogo de cierre SIEMPRE, salga por donde salga: si se quedara puesto, el
    # próximo mensaje del doctor en ese hilo se leería como una fecha en vez de reenviarse.
    try:
        await asyncio.to_thread(_marcar_cierre, database_url, id_conversacion, None)
    except Exception:  # noqa: BLE001
        log.warning("el diálogo de cierre de %s quedó puesto", id_conversacion)

    # Lo único que cruza el muro hacia Daniela. Ver `_avisar_a_daniela`.
    await _avisar_a_daniela(
        database_url, id_conversacion, _nota_del_relevo(doctor, cita)
    )

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
        # Quitar el anclaje ANTES de la despedida: un botón «Listo, que siga Daniela» fijo en
        # la cabecera de un relevo que ya cerró invita a pulsarlo y a creer que sigue vivo.
        try:
            await telegram.desanclar_todo_del_tema(tema_id)
        except Exception:  # noqa: BLE001
            log.warning("el anclaje del tema %s quedó puesto", tema_id)

        # La despedida va ANTES de cerrar: en un tema ya cerrado el bot sigue pudiendo
        # escribir --es administrador-- pero si algo cambiara en Telegram, el mensaje que se
        # perdería sería el que explica por qué el doctor se quedó sin poder escribir.
        #
        # Y va silenciosa: el relevo terminó, el hilo vuelve a ser un expediente.
        #
        # Con el botón de volver a entrar: cerrar es un gesto de un toque --y el barrido lo
        # hace SOLO, sin preguntarle a nadie--, así que tiene que poder deshacerse con otro.
        # Un tema cerrado no deja escribir, pero sí deja pulsar un botón inline: es la única
        # forma de volver sin salir a buscar el hilo al General.
        await _avisar_en_tema(
            telegram,
            tema_id,
            _DESPEDIDA.get(motivo, "🔒 Relevo cerrado."),
            teclado=teclado_tomar(telefono) if telefono else None,
        )
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
        if motivo == "tema_perdido":
            # El hilo ya no existe: hay que OLVIDARLO, no marcarlo cerrado. Si la fila se
            # quedara apuntando a un `topic_id` muerto, `asegurar_tema` lo daría por bueno y
            # cada archivo futuro de esa persona fallaría al depositarse, para siempre.
            try:
                await asyncio.to_thread(_olvidar_tema, database_url, telefono)
            except Exception:  # noqa: BLE001
                log.warning("el hilo muerto de +%s quedó anotado en la base", telefono)
        else:
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

        # ¿Sigue vivo el hilo, y sigue abierto? Telegram NO avisa cuando alguien borra un
        # tema, así que esto es lo único que lo detecta. Sin ello, borrar el hilo de un
        # paciente en relevo lo deja sin nadie al otro lado: Daniela callada porque
        # `tomada_por` sigue puesto, y el doctor sin hilo donde escribir. El paciente escribe
        # y no le contesta nadie. Medido en producción el 14/09/2026.
        if uno["topic_id"]:
            estado = await telegram.estado_del_tema(uno["topic_id"])
            # `None` es «no se pudo saber» --un timeout de Telegram-- y no se actúa: cerrar
            # un relevo vivo por un fallo de red sería peor que esperar al siguiente barrido,
            # que llega en un minuto.
            motivo_del_hilo = {
                "borrado": "tema_perdido",
                # El hilo existe pero estaba CERRADO, o sea que se perdió un
                # `forum_topic_closed` --el bot estaba caído cuando el doctor lo cerró--. Se
                # trata igual que el evento: es la cuarta salida, llegando tarde. `cerrar`
                # vuelve a cerrar el tema, así que la reapertura de la sonda no queda.
                "reabierto": "devuelto_por_doctor",
            }.get(estado or "")
            if motivo_del_hilo:
                if await cerrar(
                    id_conversacion,
                    motivo=motivo_del_hilo,
                    telegram=telegram,
                    database_url=database_url,
                    telefono=uno["telefono"],
                    tema_id=uno["topic_id"],
                    tema_general=tema_general,
                    doctor=uno.get("doctor") or "un doctor",
                ):
                    cerrados += 1
                continue

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
    telegram,
    tema_id: int,
    texto: str,
    *,
    silencioso: bool = True,
    teclado: dict | None = None,
) -> None:
    try:
        await telegram.enviar_mensaje(
            texto, tema_id=tema_id, silencioso=silencioso, teclado=teclado
        )
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
    "cerrar_por_tema_cerrado",
    "nombre_de_quien_pulsa",
    "relevar_mensaje",
    "teclado_devolver",
    "teclado_ir_al_hilo",
]

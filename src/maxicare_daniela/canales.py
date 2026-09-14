"""Clientes de los dos canales: WhatsApp (por donde entra y sale la conversación) y
Telegram (donde viven los doctores).

Por qué existe este módulo y no está todo en `runtime.py`: `runtime.py` es el transporte
*entrante* —quién nos llama y cómo respondemos— y cambia entero si mañana el webhook lo
recibe otro servicio. Estos clientes son el transporte *saliente* y no cambiarían: seguirían
descargando de WhatsApp y escribiendo en Telegram igual. Separarlos permite además probar la
ingesta sin levantar un servidor.

Lo que NO va aquí: ninguna decisión sobre qué hacer con un mensaje. Estos clientes mueven
bytes; `ingesta.py` decide.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# La versión de la API de Meta va fija y explícita. Si se deja implícita, Meta puede mover
# el comportamiento por debajo sin que nada en el repositorio lo registre.
VERSION_GRAPH = "v21.0"
BASE_GRAPH = f"https://graph.facebook.com/{VERSION_GRAPH}"
BASE_TELEGRAM = "https://api.telegram.org"

#: WhatsApp entrega el archivo en un enlace de un solo uso y vida corta. El tope está en
#: `sistemas.lista[WhatsApp].notas` del brief: no hay segunda oportunidad, así que la
#: descarga no espera y no se agrupa con nada.
TIMEOUT_DESCARGA = httpx.Timeout(30.0, connect=10.0)
TIMEOUT_NORMAL = httpx.Timeout(15.0, connect=10.0)


class ErrorDeCanal(RuntimeError):
    """Falló una llamada a WhatsApp o a Telegram.

    Se distingue de un error de programación para que `ingesta.py` pueda registrarlo en la
    fila del mensaje en vez de tumbar el proceso: un archivo que no se pudo reenviar es un
    hecho que hay que poder consultar después, no una excepción que se pierde en un log.
    """


@dataclass(frozen=True)
class ArchivoDescargado:
    contenido: bytes
    mime: str
    nombre: str

    @property
    def tamano(self) -> int:
        return len(self.contenido)


#: Lo que Telegram responde cuando no quiere mojarse. Ver `Telegram.descargar_archivo`.
_SIN_DECLARAR = ("", "application/octet-stream", "binary/octet-stream")


def _mime_de(nombre: str, cabecera: str | None) -> str:
    """El tipo real de un archivo, con la extensión por delante de la cabecera.

    Es la mitad que le faltaba a `_nombre_sugerido`: aquella construye un nombre a partir de
    un mime, y esta deduce el mime a partir de un nombre. Hace falta porque los dos canales
    mienten en direcciones opuestas -- WhatsApp da el mime y no el nombre, Telegram da el
    nombre y no el mime.

    El orden importa: `photos/file_1.jpg` con cabecera `application/octet-stream` tiene que
    salir como `image/jpeg`, no como octet-stream, o Meta rechaza la subida entera.
    """
    limpia = (cabecera or "").split(";")[0].strip().lower()
    if limpia and limpia not in _SIN_DECLARAR:
        return limpia
    adivinado, _ = mimetypes.guess_type(nombre)
    return adivinado or "application/octet-stream"


def _nombre_sugerido(media_id: str, mime: str, nombre_original: str | None) -> str:
    """Telegram muestra el nombre del archivo al doctor, así que vale la pena que diga algo.

    WhatsApp solo manda `filename` en los documentos; en una foto o un audio no hay nombre y
    hay que construirlo, porque un archivo sin extensión llega a Telegram como binario
    opaco y el doctor no lo puede ni previsualizar.
    """
    if nombre_original:
        return nombre_original
    extension = mimetypes.guess_extension(mime.split(";")[0].strip()) or ".bin"
    # `.jpe` es lo que devuelve mimetypes para image/jpeg en algunas plataformas y ningún
    # visor lo reconoce.
    if extension == ".jpe":
        extension = ".jpg"
    return f"{media_id}{extension}"


# ==========================================================================================
# WhatsApp
# ==========================================================================================


class WhatsApp:
    def __init__(self, token: str, phone_number_id: str) -> None:
        self._token = token
        self._phone_number_id = phone_number_id

    @property
    def _cabeceras(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def descargar_media(
        self, media_id: str, *, nombre_original: str | None = None
    ) -> ArchivoDescargado:
        """Trae el archivo. Son DOS llamadas y no una, y el orden importa.

        La primera canjea el `media_id` por una URL temporal; la segunda baja los bytes.
        Entre las dos no se hace nada más: esa URL caduca, y si caduca el archivo se perdió
        para siempre — el paciente tendría que reenviarlo.
        """
        async with httpx.AsyncClient(timeout=TIMEOUT_DESCARGA) as cliente:
            meta = await cliente.get(f"{BASE_GRAPH}/{media_id}", headers=self._cabeceras)
            if meta.status_code != 200:
                raise ErrorDeCanal(
                    f"WhatsApp no entregó la URL del media {media_id}: "
                    f"{meta.status_code} {meta.text[:200]}"
                )
            datos = meta.json()
            url = datos.get("url")
            if not url:
                raise ErrorDeCanal(f"la respuesta del media {media_id} no trae 'url': {datos}")

            # La descarga también va con el Bearer: la URL sola no autoriza nada.
            archivo = await cliente.get(url, headers=self._cabeceras)
            if archivo.status_code != 200:
                raise ErrorDeCanal(
                    f"no se pudo descargar el media {media_id}: {archivo.status_code}"
                )

            mime = datos.get("mime_type") or archivo.headers.get(
                "content-type", "application/octet-stream"
            )
            return ArchivoDescargado(
                contenido=archivo.content,
                mime=mime,
                nombre=_nombre_sugerido(media_id, mime, nombre_original),
            )

    async def enviar_texto(self, telefono: str, texto: str) -> str:
        """Devuelve el wamid del mensaje enviado."""
        cuerpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "text",
            # `preview_url` en False a propósito: una vista previa de un enlace en un chat
            # de salud puede mostrar contenido que nadie revisó.
            "text": {"preview_url": False, "body": texto},
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(
                f"{BASE_GRAPH}/{self._phone_number_id}/messages",
                headers=self._cabeceras,
                json=cuerpo,
            )
        if r.status_code != 200:
            raise ErrorDeCanal(f"WhatsApp rechazó el envío: {r.status_code} {r.text[:300]}")
        return r.json()["messages"][0]["id"]

    async def enviar_plantilla(
        self,
        telefono: str,
        *,
        plantilla: str,
        parametros: list[str],
        idioma: str = "es",
    ) -> str:
        """Manda una plantilla aprobada y devuelve el wamid.

        Es la única forma de escribirle a alguien FUERA de la ventana de 24 h, que se cuenta
        desde el último mensaje del paciente. Un recordatorio la víspera cae fuera de esa
        ventana casi siempre, así que `enviar_texto` no sirve aquí: Meta lo rechaza.

        Los parámetros van posicionales, en el orden en que aparecen los `{{1}}`, `{{2}}`... del
        texto que Meta aprobó. Cambiar el orden aquí no cambia la plantilla: manda otro dato en
        otro hueco, y el paciente lee una hora donde esperaba un nombre.
        """
        cuerpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "template",
            "template": {
                "name": plantilla,
                "language": {"code": idioma},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": v} for v in parametros],
                    }
                ],
            },
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(
                f"{BASE_GRAPH}/{self._phone_number_id}/messages",
                headers=self._cabeceras,
                json=cuerpo,
            )
        if r.status_code != 200:
            raise ErrorDeCanal(
                f"WhatsApp rechazó la plantilla '{plantilla}': {r.status_code} {r.text[:300]}"
            )
        return r.json()["messages"][0]["id"]

    async def subir_media(self, archivo: ArchivoDescargado) -> str:
        """Sube los bytes a Meta y devuelve el `media_id`. La primera mitad del relevo.

        Hasta la 6C este proyecto solo bajaba archivos de WhatsApp; esta es la ruta contraria
        y solo la usa el relevo, cuando un doctor manda una foto dentro del tema.

        Son DOS llamadas y no una, igual que al bajar: Meta no acepta los bytes dentro del
        `messages`. El `media_id` que sale de aquí caduca a los 30 días, pero lo que importa
        es que caduca AL USARSE una vez -- si `enviar_archivo` falla, hay que volver a subir.
        """
        datos_form = {"messaging_product": "whatsapp", "type": archivo.mime}
        archivos = {"file": (archivo.nombre, archivo.contenido, archivo.mime)}
        async with httpx.AsyncClient(timeout=TIMEOUT_DESCARGA) as cliente:
            r = await cliente.post(
                f"{BASE_GRAPH}/{self._phone_number_id}/media",
                headers=self._cabeceras,
                data=datos_form,
                files=archivos,
            )
        if r.status_code != 200:
            raise ErrorDeCanal(f"WhatsApp no aceptó el archivo: {r.status_code} {r.text[:300]}")
        media_id = r.json().get("id")
        if not media_id:
            raise ErrorDeCanal(f"la subida no devolvió 'id': {r.text[:200]}")
        return media_id

    async def enviar_archivo(
        self, telefono: str, media_id: str, *, tipo: str, pie: str | None = None,
        nombre: str | None = None,
    ) -> str:
        """Manda un archivo ya subido. Devuelve el wamid.

        El `pie` NO viaja en un audio: Meta rechaza el mensaje entero si se lo pone, y el
        doctor se quedaría sin saber que su nota de voz no salió. En un documento va además
        el `filename`, que es lo que el paciente ve en su teléfono -- sin él aparece un
        nombre inventado por WhatsApp.
        """
        contenido: dict[str, str] = {"id": media_id}
        if pie and tipo != "audio":
            contenido["caption"] = pie[:1024]
        if nombre and tipo == "document":
            contenido["filename"] = nombre

        cuerpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": tipo,
            tipo: contenido,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(
                f"{BASE_GRAPH}/{self._phone_number_id}/messages",
                headers=self._cabeceras,
                json=cuerpo,
            )
        if r.status_code != 200:
            raise ErrorDeCanal(f"WhatsApp rechazó el archivo: {r.status_code} {r.text[:300]}")
        return r.json()["messages"][0]["id"]

    async def marcar_leido(self, wamid: str) -> None:
        """El doble check azul. Falla en silencio a propósito: es cortesía, no correo.

        Si esto tumbara el proceso, un detalle cosmético impediría que el archivo llegara a
        los doctores.
        """
        cuerpo = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                await cliente.post(
                    f"{BASE_GRAPH}/{self._phone_number_id}/messages",
                    headers=self._cabeceras,
                    json=cuerpo,
                )
        except httpx.HTTPError:
            pass


# ==========================================================================================
# Telegram
# ==========================================================================================

#: Qué método de Telegram corresponde a cada tipo de WhatsApp, y con qué nombre de campo.
#: `sendDocument` para las imágenes NO es un descuido: `sendPhoto` recomprime y baja la
#: resolución, y una radiografía recomprimida es una radiografía que el doctor no puede
#: leer. El brief lo pide en `trabajo.pasos`: el archivo llega como lo mandó el paciente.
_ENVIO_POR_TIPO = {
    "image": ("sendDocument", "document"),
    "document": ("sendDocument", "document"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendVoice", "voice"),
    "video": ("sendVideo", "video"),
    "sticker": ("sendDocument", "document"),
}

#: El camino de vuelta, que solo existe desde el relevo (6C): qué manda un doctor dentro del
#: tema y en qué tipo de WhatsApp se convierte.
#:
#: `voice` -> `audio` no es un descuido: WhatsApp NO tiene un tipo `voice` al enviar, y
#: mandarlo así hace que Meta rechace el mensaje entero. La nota de voz del doctor le llega
#: al paciente como audio, que es lo mismo que oye.
#:
#: `photo` -> `image` sí recomprime, y aquí da igual: lo que va en esa dirección es una
#: indicación del doctor, no una radiografía que alguien tenga que diagnosticar.
_TIPO_WHATSAPP_DE_TELEGRAM = {
    "photo": "image",
    "document": "document",
    "voice": "audio",
    "audio": "audio",
    "video": "video",
    "video_note": "video",
}


# ══════════════════════════════════════════════════════════════════════════════════════════
# NOTA DEL TEMA GENERAL — comprobado contra la API, no deducido
# ══════════════════════════════════════════════════════════════════════════════════════════
#
# Telegram identifica el tema General como thread 1 cuando ENTREGA un mensaje, y por eso es
# fácil suponer que también lo acepta al ENVIAR. No lo acepta: `sendMessage` con
# `message_thread_id=1` responde `Bad Request: message thread not found`.
#
# Al General se escribe OMITIENDO el campo. Por eso los métodos de abajo usan `if tema_id:`
# en vez de `if tema_id is not None`: el 0 guardado en `configuracion.telegram_topic_general`
# significa «el General», y omitir es justo lo que hay que hacer con él.
#
# El fallo importa porque es silencioso en el peor momento: el 200 ya se le devolvió a Meta,
# así que WhatsApp da el mensaje por entregado mientras el archivo no llegó a nadie. Lo único
# que lo delata es la columna `fallo` de `mensajes_entrantes`.
#
# ══════════════════════════════════════════════════════════════════════════════════════════
# NOTA DEL SILENCIO — para qué existe `silencioso=`
# ══════════════════════════════════════════════════════════════════════════════════════════
#
# El grupo tiene dos clases de sitio y NO se notifican igual:
#
#   General          -> es la bandeja de entrada de los doctores. Suena.
#   Tema de paciente -> es el expediente de esa persona. NO suena nunca.
#
# La razón es que el tema se consulta, no se vigila: dentro caen sus archivos, la lectura
# clínica de cada uno y lo que va escribiendo por WhatsApp mientras Daniela lo atiende sola.
# Si cada «buenas tardes» vibrara, los doctores apagarían las notificaciones del grupo entero
# y con ellas los escalamientos, que son lo único que de verdad les pide algo.
#
# `silencioso` es `disable_notification` de Telegram: el mensaje entra y queda en el hilo,
# pero no genera aviso. No es lo mismo que no mandarlo — el historial sigue completo.
#
# El interruptor lo decide QUIEN LLAMA, no este módulo, porque hay un caso en que el tema sí
# tiene que sonar: durante un relevo (fase 6C) el doctor está conversando por ese hilo y
# necesita enterarse. Ese es el único sitio que pasará `silencioso=False` a un tema.


class Telegram:
    def __init__(self, token: str, chat_id: str | int) -> None:
        self._token = token
        self._chat_id = str(chat_id)

    def _url(self, metodo: str) -> str:
        return f"{BASE_TELEGRAM}/bot{self._token}/{metodo}"

    def enlace_al_tema(self, tema_id: int, mensaje_id: int | None = None) -> str:
        """El enlace que abre ese hilo en la app del doctor.

        Un supergrupo privado no tiene `@usuario`, así que la forma que funciona es
        `t.me/c/<id sin el -100>/<tema>`. El `-100` es un prefijo que Telegram le pone al id
        interno del chat y que el enlace no lleva; dejarlo produce un enlace que no abre
        nada y el doctor se queda mirando un error.

        **Con `mensaje_id` el enlace lleva tres tramos y esa es la forma buena.** En un foro,
        `t.me/c/<chat>/<n>` es ambiguo: ese `<n>` es un id de MENSAJE, y que hasta hoy
        funcionara para abrir un hilo es una casualidad --el id de un tema es el id del
        mensaje de servicio que lo creó--. La forma de tres tramos,
        `t.me/c/<chat>/<tema>/<mensaje>`, dice hilo y posición por separado y es la que
        aterriza al doctor DENTRO del tema, en el mensaje que le importa, en vez de dejarlo
        arriba del todo o en el General.

        Es el complemento del relevo: la NOTIFICACIÓN de escribir en el tema es la que lo
        lleva allí, y este enlace es para cuando vuelve al General y quiere entrar a mano.
        """
        interno = str(self._chat_id).lstrip("-")
        if interno.startswith("100"):
            interno = interno[3:]
        if mensaje_id is not None:
            return f"https://t.me/c/{interno}/{tema_id}/{mensaje_id}"
        return f"https://t.me/c/{interno}/{tema_id}"

    async def enviar_mensaje(
        self,
        texto: str,
        *,
        tema_id: int | None = None,
        teclado: dict | None = None,
        silencioso: bool = False,
    ) -> int:
        cuerpo: dict = {"chat_id": self._chat_id, "text": texto, "parse_mode": "HTML"}
        # `if tema_id:` y no `is not None` a propósito — ver NOTA DEL TEMA GENERAL abajo.
        if tema_id:
            cuerpo["message_thread_id"] = tema_id
        if teclado is not None:
            cuerpo["reply_markup"] = teclado
        # Ver NOTA DEL SILENCIO abajo: el depósito en el tema de un paciente entra sin
        # vibrar el celular de nadie; lo que suena es el General.
        if silencioso:
            cuerpo["disable_notification"] = True
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("sendMessage"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram rechazó el mensaje: {datos.get('description')}")
        return datos["result"]["message_id"]

    async def enviar_archivo(
        self,
        archivo: ArchivoDescargado,
        *,
        tipo_whatsapp: str,
        pie: str,
        tema_id: int | None = None,
        silencioso: bool = False,
    ) -> int:
        """Sube los bytes a Telegram. Devuelve el `message_id`, que es la prueba de entrega."""
        metodo, campo = _ENVIO_POR_TIPO.get(tipo_whatsapp, ("sendDocument", "document"))

        datos_form: dict[str, str] = {"chat_id": self._chat_id, "parse_mode": "HTML"}
        # Telegram corta el pie de un adjunto en 1024 caracteres y devuelve error si se pasa.
        datos_form["caption"] = pie[:1024]
        if tema_id:
            datos_form["message_thread_id"] = str(tema_id)
        # Este va como `data` de un multipart, así que el booleano tiene que ir en el texto
        # que Telegram acepta ("true"), no como el `True` de Python.
        if silencioso:
            datos_form["disable_notification"] = "true"

        archivos = {campo: (archivo.nombre, archivo.contenido, archivo.mime)}
        async with httpx.AsyncClient(timeout=TIMEOUT_DESCARGA) as cliente:
            r = await cliente.post(self._url(metodo), data=datos_form, files=archivos)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(
                f"Telegram rechazó el archivo ({metodo}): {datos.get('description')}"
            )
        return datos["result"]["message_id"]

    async def crear_tema(self, nombre: str) -> int:
        """Abre un tema en el supergrupo y devuelve su `message_thread_id`.

        Telegram corta el nombre en 128 caracteres y rechaza la llamada entera si se pasa,
        así que se recorta aquí: un tema con el nombre corto es mejor que ningún tema.
        """
        cuerpo = {"chat_id": self._chat_id, "name": nombre[:128]}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("createForumTopic"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no creó el tema: {datos.get('description')}")
        return datos["result"]["message_thread_id"]

    async def cerrar_tema(self, tema_id: int) -> None:
        """Cierra un tema. Un tema cerrado impide FÍSICAMENTE escribir a quien no es
        administrador del grupo, mientras el bot, que sí lo es, sigue pudiendo depositar.

        Esa asimetría es la garantía dura del diseño: el doctor no tiene que acordarse de
        nada, porque cuando no debe escribirle al paciente, sencillamente no puede.
        """
        cuerpo = {"chat_id": self._chat_id, "message_thread_id": tema_id}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("closeForumTopic"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no cerró el tema: {datos.get('description')}")

    async def borrar_tema(self, tema_id: int) -> None:
        """Borra un tema del supergrupo, con todos los mensajes que tenga dentro.

        Lo usa `/clearstate` y nada más. Es irreversible y se lleva por delante los archivos
        y las lecturas clínicas que se depositaron ahí, que es exactamente lo que se le pide:
        un número reseteado no puede conservar el hilo de la persona que era antes.

        Necesita que el bot sea administrador **con `can_delete_messages`**. Hoy
        `scripts/obtener_chat_telegram.py` solo comprueba `can_manage_topics`, así que este
        permiso puede faltar sin que nada lo haya avisado: el fallo se informa y el resto del
        borrado continúa -- quedarse sin borrar el tema no puede impedir borrar la base.
        """
        cuerpo = {"chat_id": self._chat_id, "message_thread_id": tema_id}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("deleteForumTopic"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no borró el tema: {datos.get('description')}")

    async def borrar_mensaje(self, mensaje_id: int) -> None:
        """Borra un mensaje suelto del grupo. Para los que quedaron en el General.

        Los que están dentro del tema de un paciente no hace falta borrarlos uno a uno:
        `borrar_tema` se los lleva. Estos son los otros -- el aviso de «llegó un archivo de
        X» y los escalamientos--, que van al General y llevan nombre y teléfono en el texto.
        """
        cuerpo = {"chat_id": self._chat_id, "message_id": mensaje_id}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("deleteMessage"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no borró el mensaje: {datos.get('description')}")

    # ======================================================================================
    # El relevo (6C) -- todo lo que hace falta para que el botón deje de ser decorativo
    # ======================================================================================

    async def reabrir_tema(self, tema_id: int) -> None:
        """Lo contrario de `cerrar_tema`: quita el candado que pone Telegram.

        Mientras está abierto, cualquiera del grupo puede escribir en él y lo que escriba
        LLEGA AL PACIENTE. Por eso el relevo tiene una sola salida y tres disparadores: un
        tema que se queda abierto es un canal hacia el WhatsApp de alguien que nadie vigila.
        """
        cuerpo = {"chat_id": self._chat_id, "message_thread_id": tema_id}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("reopenForumTopic"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no reabrió el tema: {datos.get('description')}")

    async def responder_callback(
        self, callback_id: str, texto: str = "", *, alerta: bool = False
    ) -> None:
        """Le quita el relojito al botón. Telegram da DIEZ SEGUNDOS y luego lo da por muerto.

        Va siempre lo primero, antes de tocar la base o de reabrir nada: si se responde
        después del trabajo, el doctor ve el botón girando y lo pulsa otra vez.

        **Sin `url=`.** Es la tentación evidente --llevar al doctor a su hilo de un salto--
        y está comprobado contra la API que no se puede: `answerCallbackQuery` con una `url`
        hacia un tema del propio supergrupo responde `URL_INVALID`. De ahí que el relevo
        ESCRIBA en el tema: esa notificación es lo que de verdad lleva al doctor allí.

        No propaga: un acuse perdido no puede impedir que el relevo se active.
        """
        cuerpo: dict = {"callback_query_id": callback_id}
        if texto:
            # Telegram corta en 200 y rechaza la llamada entera si se pasa.
            cuerpo["text"] = texto[:200]
        if alerta:
            cuerpo["show_alert"] = True
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                await cliente.post(self._url("answerCallbackQuery"), json=cuerpo)
        except httpx.HTTPError:
            pass

    async def editar_teclado(self, mensaje_id: int, teclado: dict | None = None) -> None:
        """Cambia los botones de un mensaje ya enviado, SIN tocar su texto.

        Es lo que mantiene el General ordenado. Sin esto, un escalamiento tomado sigue
        mostrando «Hablar yo con el paciente» para siempre y el segundo doctor que pase lo
        pulsa creyendo que nadie lo ha visto.

        `editMessageReplyMarkup` y no `editMessageText` a propósito: reescribir el texto
        obligaría a recomponer el aviso entero --que lo escribió un modelo, con su formato y
        con todo lo ajeno ya escapado-- a partir de lo que trae el callback, que viene en
        texto plano. El resultado sería un escalamiento que pierde las negritas justo cuando
        deja de poder leerse de un vistazo. El teclado es lo único que cambió; se cambia solo
        el teclado.

        `teclado=None` deja el mensaje sin botones.
        """
        cuerpo: dict = {
            "chat_id": self._chat_id,
            "message_id": mensaje_id,
            # `{}` y no la ausencia del campo: omitirlo CONSERVA el teclado que ya tenía.
            "reply_markup": teclado if teclado is not None else {},
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("editMessageReplyMarkup"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no editó el teclado: {datos.get('description')}")

    async def reaccionar(self, mensaje_id: int, emoji: str = "👍") -> None:
        """El acuse de que lo que escribió el doctor SÍ salió hacia el paciente.

        Una reacción y no una respuesta a propósito: un «entregado» por cada frase llenaría
        el hilo de ruido y el expediente dejaría de poder leerse. Lo que sí merece un mensaje
        visible es el fallo -- ahí el doctor tiene que enterarse sin buscar.

        No propaga: quedarse sin el visto bueno no puede deshacer un mensaje ya entregado.
        """
        cuerpo = {
            "chat_id": self._chat_id,
            "message_id": mensaje_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                await cliente.post(self._url("setMessageReaction"), json=cuerpo)
        except httpx.HTTPError:
            pass

    async def estado_del_tema(self, tema_id: int) -> str | None:
        """¿Ese tema existe todavía, y sigue abierto? `None` si no se pudo averiguar.

        **Telegram no emite ningún evento cuando alguien borra un tema** --al revés que
        cerrarlo, que sí manda `forum_topic_closed`--, así que la única forma de enterarse es
        preguntar. Sin esto, borrar el hilo de un paciente en relevo deja a Daniela callada y
        al doctor sin hilo: el paciente escribe y no le contesta nadie.

        ------------------------------------------------------------------------------------
        POR QUÉ `reopenForumTopic` Y NO `editForumTopic`
        ------------------------------------------------------------------------------------

        La primera versión preguntaba con `editForumTopic` sin `name` ni
        `icon_custom_emoji_id`, razonando que sin nada que cambiar la llamada no tendría
        efecto. Es verdad que no tiene efecto, y por eso mismo **no valida el id**: contra el
        tema 328 ya borrado, medido el 14/09/2026, devolvía `ok: true`. La sonda decía que sí
        a todo y el agujero que venía a tapar seguía abierto entero, sin un solo error en el
        log. Razonar sobre una API no es medirla.

        Las cuatro candidatas, medidas contra un tema vivo y contra uno borrado. «Rastro» es
        si deja mensajes de servicio en el hilo, que a un barrido por minuto lo llenarían:

            sonda                          tema vivo              tema borrado       rastro
            editForumTopic sin argumentos  ok: true               ok: true           --
            editForumTopic name distinto   ok: true               TOPIC_ID_INVALID   sí
            editForumTopic icon=""         ok: true               TOPIC_ID_INVALID   sí (1)
            reopenForumTopic               TOPIC_NOT_MODIFIED     TOPIC_ID_INVALID   NO

        `reopenForumTopic` es la única que distingue sin dejar rastro, y no necesita saber
        cómo se llama el tema --pasar un nombre equivocado lo renombraría--. Sobre un tema ya
        abierto no hace nada, que es el caso normal: durante un relevo el tema está abierto
        siempre (no negociable 15).

        **Tarda unos 3 segundos en enterarse.** Medido: justo después de `deleteForumTopic`
        sigue contestando `TOPIC_NOT_MODIFIED` --o sea, «abierto»-- durante unos 3 s, y a
        partir de ahí `TOPIC_ID_INVALID` ya para siempre. No afecta a nada aquí: quien
        pregunta es el barrido, que pasa como pronto un minuto después. Sí afecta a quien
        quiera comprobar la sonda borrando un tema y sondeando de inmediato -- ver
        `scripts/probar_relevo.py`, que por esto sondea con espera y no de un tiro.

        Devuelve:

        - `"abierto"`  — existe y ya estaba abierto. Todo en orden.
        - `"reabierto"`— existía pero estaba CERRADO, y esta llamada acaba de abrirlo. Ver
          abajo: quien llama tiene que cerrar el relevo, no seguir.
        - `"borrado"`  — ya no existe.
        - `None`       — no se pudo saber. Un timeout o un 500 de Telegram no es un tema
          borrado, y cerrar un relevo vivo por un fallo de red sería peor que esperar al
          siguiente barrido. Quien llama no actúa ante `None`.

        El `"reabierto"` no es un caso de laboratorio: si el bot está caído cuando el doctor
        cierra el hilo, el `forum_topic_closed` se pierde --Telegram deja de reintentar-- y el
        relevo se queda con el tema cerrado, que es justo el estado que prohíbe la 15. Esta
        sonda es lo que lo encuentra después, y por eso no basta con un booleano.
        """
        cuerpo = {"chat_id": self._chat_id, "message_thread_id": tema_id}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                r = await cliente.post(self._url("reopenForumTopic"), json=cuerpo)
            datos = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if datos.get("ok"):
            # Existía y estaba cerrado: la llamada lo ha abierto. No se deja así.
            return "reabierto"
        descripcion = str(datos.get("description") or "").upper()
        # Los dos que dicen «ese tema no está»; cualquier otro error se trata como «no sé».
        if "TOPIC_ID_INVALID" in descripcion or "THREAD NOT FOUND" in descripcion:
            return "borrado"
        if "TOPIC_NOT_MODIFIED" in descripcion:
            return "abierto"
        log.warning("no se pudo comprobar el tema %s: %s", tema_id, datos.get("description"))
        return None

    async def anclar_mensaje(self, mensaje_id: int) -> None:
        """Fija un mensaje arriba del hilo, donde no haya que ir a buscarlo.

        Existe por una queja medida en el segundo relevo real: el botón «Listo, que siga
        Daniela» viaja con el mensaje de bienvenida, o sea el PRIMERO del hilo, y después de
        veinte frases el doctor tenía que subir hasta arriba del todo para devolver el
        control. Anclado, Telegram lo deja siempre a la vista en la cabecera del tema.

        En un foro no hace falta decir el tema: el anclaje se aplica al que contenga el
        mensaje. Va en silencio porque el doctor ya está mirando ese hilo.

        No propaga: un relevo sin el botón anclado sigue siendo un relevo, y el botón sigue
        estando arriba. Requiere `can_pin_messages` --confirmado en este grupo el
        14/09/2026--, un permiso que ningún script comprobaba hasta hoy.
        """
        cuerpo = {
            "chat_id": self._chat_id,
            "message_id": mensaje_id,
            "disable_notification": True,
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                r = await cliente.post(self._url("pinChatMessage"), json=cuerpo)
            if not r.json().get("ok"):
                log.warning("no se ancló el mensaje %s: %s", mensaje_id, r.json().get("description"))
        except httpx.HTTPError:
            log.warning("no se ancló el mensaje %s", mensaje_id)

    async def desanclar_todo_del_tema(self, tema_id: int) -> None:
        """Quita los anclajes de un tema. Se llama al cerrar el relevo.

        Un botón anclado que ya no hace nada es peor que ninguno: invita a pulsarlo y a creer
        que el relevo sigue vivo. Va por TEMA y no por mensaje para no tener que guardar en
        ninguna parte el id de la bienvenida -- una columna más, y otra cosa que puede
        quedarse desincronizada, por un anclaje.

        No propaga, por lo mismo que `anclar_mensaje`.
        """
        cuerpo = {"chat_id": self._chat_id, "message_thread_id": tema_id}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                await cliente.post(self._url("unpinAllForumTopicMessages"), json=cuerpo)
        except httpx.HTTPError:
            pass

    async def descargar_archivo(
        self, file_id: str, *, nombre: str | None = None
    ) -> ArchivoDescargado:
        """Baja un archivo que mandó un doctor. Dos llamadas, como en WhatsApp.

        La primera canjea el `file_id` por un `file_path`; la segunda baja los bytes de
        `/file/bot<token>/<path>`, que es una URL DISTINTA de la de la API y lleva el token
        en la ruta -- escribirla a mano en un log filtraría el token del bot.

        Tope de Telegram: 20 MB por descarga de bot. Más grande no se puede bajar y el error
        lo dice, que es mejor que un archivo truncado llegando al paciente.

        ------------------------------------------------------------------------------------
        EL TIPO LO DICE LA EXTENSIÓN, NO LA CABECERA. Medido contra la API el 14/09/2026
        ------------------------------------------------------------------------------------

        El servidor de archivos de Telegram **no declara de qué tipo es lo que entrega**:

            file_path      photos/file_1.jpg
            Content-Type   application/octet-stream        <- «bytes, no sé qué son»

        Pasarle eso a Meta hace que rechace la subida entera con
        `(#100) Param file must be a file with one of the following types: ...`, y el doctor
        ve su foto sin entregar. Pasó en el primer relevo real.

        Así que el MIME sale de la extensión de `file_path`, que Telegram sí conserva, y la
        cabecera solo se usa cuando dice algo -- si algún día empieza a declararlo bien, se
        aprovecha sin tocar nada.
        """
        async with httpx.AsyncClient(timeout=TIMEOUT_DESCARGA) as cliente:
            r = await cliente.post(self._url("getFile"), json={"file_id": file_id})
            datos = r.json()
            if not datos.get("ok"):
                raise ErrorDeCanal(f"Telegram no entregó el archivo: {datos.get('description')}")
            ruta = datos["result"].get("file_path")
            if not ruta:
                raise ErrorDeCanal(f"getFile no trae 'file_path': {datos}")

            bajado = await cliente.get(f"{BASE_TELEGRAM}/file/bot{self._token}/{ruta}")
            if bajado.status_code != 200:
                raise ErrorDeCanal(f"no se pudo bajar el archivo: {bajado.status_code}")

        nombre_final = nombre or ruta.rsplit("/", 1)[-1]
        return ArchivoDescargado(
            contenido=bajado.content,
            # El nombre que trae el doctor manda sobre la ruta: un `remision.pdf` dice más
            # que un `documents/file_7.pdf`, y su extensión es igual de buena.
            mime=_mime_de(nombre_final, bajado.headers.get("content-type")),
            # El nombre real si Telegram lo trae; si no, el que da la ruta, que conserva la
            # extensión y es lo que decide con qué app lo abre el paciente.
            nombre=nombre_final,
        )

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

import mimetypes
from dataclasses import dataclass

import httpx

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


class Telegram:
    def __init__(self, token: str, chat_id: str | int) -> None:
        self._token = token
        self._chat_id = str(chat_id)

    def _url(self, metodo: str) -> str:
        return f"{BASE_TELEGRAM}/bot{self._token}/{metodo}"

    async def enviar_mensaje(
        self, texto: str, *, tema_id: int | None = None, teclado: dict | None = None
    ) -> int:
        cuerpo: dict = {"chat_id": self._chat_id, "text": texto, "parse_mode": "HTML"}
        # `if tema_id:` y no `is not None` a propósito — ver NOTA DEL TEMA GENERAL abajo.
        if tema_id:
            cuerpo["message_thread_id"] = tema_id
        if teclado is not None:
            cuerpo["reply_markup"] = teclado
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
    ) -> int:
        """Sube los bytes a Telegram. Devuelve el `message_id`, que es la prueba de entrega."""
        metodo, campo = _ENVIO_POR_TIPO.get(tipo_whatsapp, ("sendDocument", "document"))

        datos_form: dict[str, str] = {"chat_id": self._chat_id, "parse_mode": "HTML"}
        # Telegram corta el pie de un adjunto en 1024 caracteres y devuelve error si se pasa.
        datos_form["caption"] = pie[:1024]
        if tema_id:
            datos_form["message_thread_id"] = str(tema_id)

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

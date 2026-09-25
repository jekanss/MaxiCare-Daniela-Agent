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
from typing import Any

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

#: Megabytes por encima de los cuales un media de WhatsApp ni se descarga. El default vive
#: aquí --y no se importa de `config`-- porque este módulo no importa nada del paquete a
#: propósito: habla con dos APIs y no sabe nada del resto del sistema. `runtime` le pasa el
#: valor efectivo, que es el que el `.env` puede mover.
#:
#: 55 y no menos: Telegram rechaza por encima de 50 MB, así que este tope no le quita al
#: doctor ningún archivo que hoy le llegue. Ver el comentario dentro de `descargar_media`.
TOPE_DESCARGA_MB = 55


class ErrorDeCanal(RuntimeError):
    """Falló una llamada a WhatsApp o a Telegram.

    Se distingue de un error de programación para que `ingesta.py` pueda registrarlo en la
    fila del mensaje en vez de tumbar el proceso: un archivo que no se pudo reenviar es un
    hecho que hay que poder consultar después, no una excepción que se pierde en un log.
    """


class HiloInvalido(ErrorDeCanal):
    """El `message_thread_id` al que se escribía ya no existe: alguien borró ese tema.

    Es un `ErrorDeCanal` a propósito --todo lo que ya lo captura sigue capturándolo-- pero
    con nombre propio porque exige una reacción distinta de cualquier otro rechazo: el resto
    se reintenta o se registra, y este obliga a OLVIDAR la fila de `temas_telegram`. Sin eso
    el sistema no se recupera nunca, y el docstring de `persistencia.olvidar_tema` ya lo
    decía: cada mensaje futuro de esa persona se estrella contra el mismo hilo muerto.

    `relevo.barrer` ya lo resolvía, pero solo para quien está EN RELEVO: itera sobre
    `tomada_por IS NOT NULL`. Fuera de un relevo nadie sondea nada, porque **Telegram no
    emite ningún evento cuando alguien borra un tema**.
    """


#: Lo que dice Telegram cuando el hilo ya no existe. Medido contra la API el 16/09/2026:
#: `sendMessage` con un `message_thread_id` inexistente responde «Bad Request: message
#: thread not found»; los métodos de foro responden `TOPIC_ID_INVALID`. Un tema CERRADO no
#: entra aquí y no debe: el bot es administrador y sigue depositando en él -- de hecho todos
#: los temas de paciente se crean cerrados, así que confundir los dos casos borraría el
#: expediente de cada paciente que tiene el hilo como debe estar.
_HILO_MUERTO = ("message thread not found", "topic_id_invalid", "thread not found")


def es_hilo_invalido(descripcion: str | None) -> bool:
    """¿Ese rechazo de Telegram significa «ese tema ya no existe»?

    Se compara contra una lista corta y explícita, nunca con un «not found» suelto: un error
    de permisos o un límite de tasa son pasajeros, y tratarlos como hilo muerto le costaría
    el expediente a un paciente cuyo hilo está perfectamente vivo.
    """
    if not descripcion:
        return False
    bajo = descripcion.lower()
    return any(marca in bajo for marca in _HILO_MUERTO)


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
    def __init__(
        self, token: str, phone_number_id: str, *, tope_descarga_mb: int = TOPE_DESCARGA_MB
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._tope_descarga = max(tope_descarga_mb, 1) * 1024 * 1024

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

            # El tope va AQUÍ, entre las dos llamadas, y por eso no cuesta nada: Meta ya
            # declaró `file_size` en la metadata, así que un archivo enorme se rechaza SIN
            # haber bajado un solo byte.
            #
            # Sin esto, el archivo se cargaba entero en memoria (`archivo.content`) y después
            # se re-serializaba en el multipart hacia Telegram: DOS copias simultáneas por
            # archivo, más una tercera del base64 si además iba al modelo. Y `runtime` encola
            # cada mensaje sin límite de concurrencia, en un contenedor de un solo worker. Era
            # el vector de agotamiento más barato del sistema: no hacía falta vulnerar nada,
            # solo mandar archivos grandes a la vez.
            #
            # El número está elegido para NO perder nada que hoy funcione: Telegram rechaza
            # por encima de 50 MB, así que un archivo que cruce este tope ya estaba fallando
            # --más tarde, y después de haber ocupado la RAM--. Lo que cambia es dónde falla,
            # no si falla.
            tamano = datos.get("file_size")
            if tamano and int(tamano) > self._tope_descarga:
                raise ErrorDeCanal(
                    f"el media {media_id} pesa {int(tamano)} bytes y el tope son "
                    f"{self._tope_descarga}: no se descarga"
                )

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
        """Devuelve el wamid del mensaje enviado.

        Un texto en blanco NO se manda, y lanza. Meta lo acepta --un cuerpo con un solo
        espacio devuelve 200 y su wamid-- así que esto no lo para nadie más: medido el
        24/09/2026 con Andrea Rodríguez, que recibió un mensaje vacío de la clínica porque
        el modelo devolvió `mensaje_al_paciente = " "` y `min_length=1` no lo distingue de
        una palabra.

        Aquí y no en el camino que lo produjo, por dos razones. La causa raíz no es aquel
        turno sino que el contrato no promete lo que parece prometer, y eso vale para
        cualquier turno futuro. Y por este borde pasan TODOS los caminos que le escriben a
        un paciente: Daniela, la frase de la cuota, la confirmación del reseteo, el relevo y
        los recordatorios.

        **Lanza en vez de callar, y la diferencia es la fila del mensaje.** `atencion`
        distingue tres estados: respondido (fecha y wamid), fallido (motivo) y ninguno de
        los dos. Un envío que se salta en silencio queda como RESPONDIDO --quien llama no
        tendría de qué enterarse-- y la única prueba de que al paciente no le llegó nada
        desaparece justo en el caso en que hace falta. Con la excepción, el camino de
        `fallo_respuesta` que ya existe hace su trabajo.
        """
        if not texto.strip():
            raise ErrorDeCanal(
                f"no se manda un mensaje en blanco a {telefono}: WhatsApp lo aceptaría "
                f"({texto!r}) y el paciente recibiría una burbuja vacía"
            )
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

        `idioma` tiene que coincidir EXACTAMENTE con el código de la traducción registrada en
        el Business Manager. Meta no busca la traducción más parecida: una plantilla creada
        como `es_CO` y llamada con `es` devuelve el error 132001 y no se manda nada. Por eso
        el valor sale de `config` y no de aquí -- el default es el caso probable, no un hecho.

        **Solo manda el componente `body`.** Para los dos quick replies del recordatorio es
        correcto: un quick reply lleva su carga útil dentro de la plantilla aprobada y el
        emisor no tiene nada que rellenar. Si Meta aprueba algún día un botón con carga
        DINÁMICA --una URL con una variable, o un `flow`--, el cuerpo hay que ampliarlo con su
        `{"type": "button", "sub_type": ..., "index": ...}`: sin él, ese botón se manda vacío.

        **Y sin parámetros no manda NINGÚN componente**, que no es lo mismo que mandarlo con
        la lista vacía: a una plantilla sin variables, un `body` con `parameters: []` le
        responde 132000 («number of parameters does not match»). Hizo falta el 23/09/2026,
        cuando `reactivacion_sin_agendar` perdió su único hueco --el nombre; ver
        `seguimientos.TIPOS_SIN_HUECOS`--. Lo que está en juego no es un envío menos: la fila
        se marca ANTES de enviar (no negociable 21), así que un rechazo de Meta la pierde para
        siempre tras los tres intentos, y encima despierta al doctor con un aviso de fallo.
        """
        plantilla_aprobada = {"name": plantilla, "language": {"code": idioma}}
        if parametros:
            plantilla_aprobada["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": v} for v in parametros],
                }
            ]
        cuerpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "template",
            "template": plantilla_aprobada,
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

    async def calidad_del_numero(self) -> dict[str, str]:
        """Lo que Meta opina HOY de este número. Una LECTURA: no cuesta ni gasta cupo.

        Se pregunta en vez de esperar al webhook `phone_number_quality_update` a propósito
        (tarea 7.0 de la reactivación de leads, regla 11): ese webhook exige suscribir el
        campo en la consola de Meta --configuración externa que un despliegue no
        garantiza-- y sobre todo SE PUEDE PERDER: si el servidor está caído cuando Meta lo
        manda, Meta reintenta un rato y desiste, y el sistema se queda creyendo que todo va
        bien justo cuando no va bien. Una consulta antes de cada barrido no se pierde nunca.

        Mismo patrón que `canales.aviso_sigue_puesto` (no negociable 26) y que
        `telegram.estado_del_tema` (no negociable 19): lo que no emite evento, se sonda.

        Devuelve `{}` si la consulta falla. Quien llama tiene que tratar el diccionario
        vacío como «no se sabe», que para `barrido.se_puede_encolar` significa NO encolar
        -- ante la duda, no mandar.

        Usa la constante de módulo `BASE_GRAPH` y la propiedad `_cabeceras`, igual que los
        otros cuatro sitios de esta clase que hablan con Graph: esta clase no tiene ningún
        atributo `self._base`, así que no hay nada que extraer.
        """
        url = f"{BASE_GRAPH}/{self._phone_number_id}"
        parametros = {"fields": "quality_rating,messaging_limit_tier,status"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                respuesta = await cliente.get(
                    url, headers=self._cabeceras, params=parametros
                )
                respuesta.raise_for_status()
                return respuesta.json()
        except Exception:  # noqa: BLE001 -- un fallo aquí no puede tumbar el barrido
            log.exception("no se pudo leer la calidad del número en Meta")
            return {}

    async def _waba_ids(self) -> list[str]:
        """Los WABA que este token puede tocar, sacados del propio token.

        El proyecto guarda el `phone_number_id` pero no el id de la cuenta de negocio, y
        listar plantillas cuelga de la cuenta y no del número. `debug_token` lo dice sin
        pedir un secreto más: los `granular_scopes` de un token de usuario de sistema traen
        los ids de destino.
        """
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                r = await cliente.get(
                    f"{BASE_GRAPH}/debug_token",
                    params={"input_token": self._token, "access_token": self._token},
                )
            if r.status_code != 200:
                return []
            datos = r.json().get("data", {})
        except Exception:  # noqa: BLE001
            log.exception("no se pudo derivar el WABA del token")
            return []
        ids: list[str] = []
        for permiso in datos.get("granular_scopes", []):
            if permiso.get("scope") in (
                "whatsapp_business_messaging",
                "whatsapp_business_management",
            ):
                ids.extend(permiso.get("target_ids", []))
        return list(dict.fromkeys(ids))

    async def estado_de_plantillas(self) -> list[dict[str, str]]:
        """Qué plantillas tiene Meta y en qué estado. Una LECTURA: no cuesta ni gasta cupo.

        Vivía solo en `scripts/probar_plantilla.py`, con la firma escrita a mano. Una
        plantilla rechazada es de las pocas cosas que se rompen sin ruido --el despachador
        sigue encolando y Meta sigue negándose-- y hasta hoy solo se veía abriendo una
        terminal.

        Devuelve TODAS las del WABA y no solo las que el `.env` nombra, a propósito: la
        pregunta que se hace delante de esta pantalla es «¿qué tengo aprobado?», y una lista
        filtrada por lo que el `.env` ya sabe no puede contestarla.

        Mismo contrato que `calidad_del_numero`: lista vacía si no se pudo averiguar. Quien
        llama la trata como «no se sabe», nunca como «no hay ninguna».
        """
        plantillas: list[dict[str, str]] = []
        for waba in await self._waba_ids():
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                    r = await cliente.get(
                        f"{BASE_GRAPH}/{waba}/message_templates",
                        params={"access_token": self._token, "limit": 50},
                    )
                    r.raise_for_status()
                    datos = r.json()
            except Exception:  # noqa: BLE001 -- una sonda no puede tumbar la pantalla
                log.exception("no se pudieron leer las plantillas del WABA %s", waba)
                continue
            for p in datos.get("data", []):
                plantillas.append(
                    {
                        "nombre": p.get("name", ""),
                        "estado": p.get("status", ""),
                        "idioma": p.get("language", ""),
                    }
                )
        return plantillas

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
            descripcion = datos.get("description")
            if es_hilo_invalido(descripcion):
                raise HiloInvalido(f"Telegram rechazó el mensaje: {descripcion}")
            raise ErrorDeCanal(f"Telegram rechazó el mensaje: {descripcion}")
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
            descripcion = datos.get("description")
            if es_hilo_invalido(descripcion):
                raise HiloInvalido(f"Telegram rechazó el archivo ({metodo}): {descripcion}")
            raise ErrorDeCanal(f"Telegram rechazó el archivo ({metodo}): {descripcion}")
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
            descripcion = datos.get("description")
            # `HiloInvalido` y no un `ErrorDeCanal` cualquiera cuando el tema ya no existe:
            # quien llama tiene que poder distinguir «Telegram falló» de «alguien borró este
            # hilo», porque la reacción es opuesta --reintentar frente a olvidar la fila y
            # abrir uno nuevo--. Medido en producción el 17/09/2026: el doctor borró el hilo,
            # pulsó «Hablar yo con el paciente» y esto llegaba como `ErrorDeCanal` genérico,
            # así que `relevo.activar` deshacía el relevo entero. Hereda de `ErrorDeCanal`:
            # todo lo que ya lo capturaba sigue igual.
            if es_hilo_invalido(descripcion):
                raise HiloInvalido(f"Telegram no reabrió el tema: {descripcion}")
            # `TOPIC_NOT_MODIFIED` es un SÍ: el tema existe y ya estaba abierto, que es
            # exactamente lo que esta función promete dejar. `estado_del_tema` ya lo lee así
            # --devuelve "abierto"-- y las dos sondas llaman al MISMO `reopenForumTopic`.
            # Tratarlo como fallo reabría el agujero del 17/09/2026 por la otra puerta:
            # `_tema_abierto_para` lo propaga y `activar` deshace el relevo entero, con el
            # hilo del paciente vivo delante. Pasa cuando un doctor reabre el tema a mano,
            # cuando `asegurar_tema` no consiguió cerrarlo al crearlo, o cuando falló el
            # cierre del relevo anterior.
            if "TOPIC_NOT_MODIFIED" in str(descripcion).upper():
                return
            raise ErrorDeCanal(f"Telegram no reabrió el tema: {descripcion}")

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

    async def estado_del_webhook(self) -> dict[str, Any]:
        """Lo que Telegram dice del webhook del relevo.

        `getWebhookInfo` es solo LECTURA y no rompe nada. Lo que rompe el `getUpdates` de
        `scripts/obtener_chat_telegram.py` es llamar a `setWebhook`, y aquí no se llama.

        **No dice si hay `secret_token`, y no hay forma de averiguarlo desde aquí.** Por eso
        esta función no devuelve ningún campo que lo insinúe: quien pinte esto tiene que
        enseñar por separado «la URL está puesta» --esto-- y «el secreto está configurado en
        este proceso» --`config.telegram_webhook_secret`--. La única prueba de que el secreto
        COINCIDE es que `ultimo_error` no traiga un 403.

        Mismo contrato que las otras sondas de esta clase: `{}` si no se pudo averiguar.
        """
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                r = await cliente.get(self._url("getWebhookInfo"))
            datos = r.json()
        except (httpx.HTTPError, ValueError):
            log.warning("no se pudo consultar el webhook de Telegram")
            return {}
        if not datos.get("ok"):
            log.warning("Telegram rechazó getWebhookInfo: %s", datos.get("description"))
            return {}
        info = datos.get("result", {})
        return {
            "url": info.get("url") or "",
            "pendientes": int(info.get("pending_update_count", 0) or 0),
            # Donde aparece un 403 si el secreto del `.env` no coincide con el registrado.
            "ultimo_error": info.get("last_error_message") or None,
        }

    async def aviso_sigue_puesto(self, mensaje_id: int, teclado: dict) -> bool | None:
        """¿Ese mensaje sigue en el grupo? `None` si no se pudo averiguar.

        **Borrar un mensaje no emite ningún evento**, exactamente igual que borrar un tema
        (ver `estado_del_tema`). Y el sistema dependía de que el aviso siguiera puesto: la
        guarda `persistencia.escalamiento_vivo_con_motivo` calla todo escalamiento del mismo
        motivo mientras haya uno «delante del doctor y sin responder», y eso lo mide por
        `telegram_message_id IS NOT NULL`. En cuanto el doctor borra ese mensaje --que es lo
        que hace para dejar el General limpio-- la premisa es falsa y el silencio dura las
        24 h de la conversación: ni aviso, ni botón, ni hilo. Medido en producción el
        17/09/2026 sobre el aviso 855.

        POR QUÉ `editMessageReplyMarkup` CON EL MISMO TECLADO, y no otra cosa: por lo que NO
        hace. Si el mensaje está igual, Telegram responde `message is not modified` y no toca
        nada -- ni mensaje de servicio, ni notificación, ni reordenar el General. Es la misma
        clase de sonda sin rastro que `reopenForumTopic`, y el teclado tiene que ser el mismo
        que le puso quien mandó el aviso, o la llamada sí tendría efecto.

        Devuelve:

        - `True`  — sigue puesto. También cuando responde `ok: true`: existía con otro
          teclado y se le acaba de poner el que toca. Lo que importa aquí es que el mensaje
          está, no cuál era su teclado.
        - `False` — el doctor lo borró.
        - `None`  — no se pudo saber. Un 429 o un timeout no es un mensaje borrado; quien
          llama decide, y en `runtime` decide AVISAR: callar una alerta clínica porque
          Telegram tuvo un mal minuto es el error caro de los dos.
        """
        cuerpo = {
            "chat_id": self._chat_id,
            "message_id": mensaje_id,
            "reply_markup": teclado,
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
                r = await cliente.post(self._url("editMessageReplyMarkup"), json=cuerpo)
            datos = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if datos.get("ok"):
            return True
        descripcion = str(datos.get("description") or "").lower()
        if "not modified" in descripcion:
            return True
        if "message to edit not found" in descripcion:
            return False
        log.warning(
            "no se pudo comprobar si el aviso %s sigue en el General: %s",
            mensaje_id,
            datos.get("description"),
        )
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

"""Una nota de voz es un mensaje dicho en voz alta, no un archivo que alguien tenga que abrir.

Hasta el 21/09/2026 este módulo no existía y eso tenía una consecuencia medida: `audio` está
en `ingesta.TIPOS_CON_ARCHIVO` pero no en `lectura.TIPOS_QUE_SE_LEEN`, así que la nota de voz
se descargaba, se le entregaba al doctor --bien-- y a Daniela le llegaba «*[El paciente acaba
de enviar una nota de voz. Nadie lo ha revisado todavía y tú no puedes verlo]*». Daniela
obedecía esa frase al pie de la letra: confirmaba que llegó y escalaba. Las CINCO notas de
voz que vio el sistema en su vida terminaron así, con `motivo = 'archivo_recibido'` y la
respuesta «Ya recibimos tu audio.»

Esa frase es la correcta para una radiografía --que Daniela no debe interpretar nunca, y ese
muro vive en `lectura.py`-- y es absurda para alguien que dijo «¿cuánto cuesta la limpieza?»
sin escribirlo.

-------------------------------------------------------------------------------------------
La diferencia con `lectura.py`, que es de qué se promete y no de cómo está hecho
-------------------------------------------------------------------------------------------

Los dos módulos hacen lo mismo de lejos: cogen un archivo que ya se descargó, llaman a un
modelo y dejan algo en el tema del doctor. La diferencia está en qué pasa cuando fallan.

    lectura        lo que devuelve es información DE MÁS. Si no llega, el doctor recibe el
                   archivo igual y Daniela dice que no sabe qué contiene: todo cierto.

    transcripción  lo que devuelve ES EL MENSAJE DEL PACIENTE. Si no llega, no hay turno que
                   valga -- hay alguien preguntando algo a quien nadie le contestó.

De ahí salen las dos decisiones que no son obvias: el margen de `atencion` es más largo aquí
(`MARGEN_TRANSCRIPCION_SEGUNDOS`), y cuando no hay texto NO se escala: se le pide al paciente
que lo escriba, porque el audio ya está con el doctor y avisarle otra vez es interrumpirlo por
algo que ya tiene.

La frontera es la misma que la de `lectura.py` e `ingesta.py`: aquí no se importa `runtime.py`
ni ningún framework web.
"""

from __future__ import annotations

import html
import io
import logging

from . import consumo
from .canales import ArchivoDescargado
from .config import MODELO_TRANSCRIPTOR

log = logging.getLogger("maxicare.transcripcion")

#: Los tipos de WhatsApp que son alguien hablando. `voice` no lo manda WhatsApp --manda
#: `audio` con la marca `voice: true`, y así llegaron las cinco de producción-- pero sí lo
#: manda Telegram y está en `ingesta.NOMBRE_HUMANO`, así que entra por simetría y para que el
#: día que Meta lo emita no haya que acordarse de esto.
TIPOS_QUE_SE_TRANSCRIBEN = frozenset({"audio", "voice"})

#: Por encima de esto no se manda a transcribir. El audio le llega al doctor igual --eso no
#: lo apaga nada-- y Daniela usa la entrada de siempre.
#:
#: El número sale de una medición, no del gusto: las notas de voz reales de producción pesan
#: **~2 KB por segundo** de opus (22.961 bytes para 10 s, 36.441 para 17 s). 8 MB son del
#: orden de una hora de audio, que es un tope que ninguna nota de voz legítima roza y que sí
#: corta un archivo de música reenviado o una grabación de dos horas.
#:
#: No baja más porque no hace falta: el freno contra la inundación es la cuota diaria
#: (`cuotas.puede_transcribir`), igual que en el lector. Este tope es contra la pieza suelta
#: enorme, no contra el ritmo.
TOPE_BYTES_TRANSCRIPCION = 8 * 1024 * 1024

#: El idioma. Fijo y no autodetectado, y se midió la diferencia sobre el mismo audio:
#:
#:     sin idioma   «Ya, conchena hablo de una doctora, un doctor, no, y una doctora.»
#:     language=es  «Ya, ¿con quién hablo yo, una doctora o un doctor? Necesito una doctora.»
#:
#: La clínica está en Bogotá y le escribe gente de Colombia. El día que eso deje de ser cierto
#: esta constante es el sitio donde se ve que había una decisión.
IDIOMA = "es"


def vale_la_pena_transcribir(tipo: str, tamano: int) -> bool:
    """Si el archivo es alguien hablando y no pasa el tope de tamaño."""
    return tipo in TIPOS_QUE_SE_TRANSCRIBEN and tamano <= TOPE_BYTES_TRANSCRIPCION


#: Del mime que declara WhatsApp a la extensión que la API acepta.
#:
#: **Esta tabla existe por un `400` medido, no por prolijidad.** Ver `_nombre_para_la_api`.
_EXTENSION_PARA_LA_API = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "video/mp4": ".mp4",
}


def _nombre_para_la_api(archivo: ArchivoDescargado) -> str:
    """El nombre del archivo que se le manda a la API, y NUNCA `archivo.nombre`.

    -------------------------------------------------------------------------------------
    La trampa, que es un 400 en el 100 % de los casos
    -------------------------------------------------------------------------------------

    La API decide el formato **por la extensión del nombre**, no por el mime ni por los
    bytes. Y `canales._nombre_sugerido` construye ese nombre con
    `mimetypes.guess_extension("audio/ogg")`, que en esta plataforma devuelve **`.oga`**:

        1960902764606339.oga  ->  400 invalid_request_error: Unsupported file format oga

    Comprobado contra las cinco notas de voz reales de producción, las cinco fallaron, y con
    el mismo byte a byte renombrado a `.ogg` las cinco transcribieron bien. `.oga` es un
    alias legítimo de Ogg y la documentación de OpenAI hasta lo lista; la API lo rechaza
    igual. La medición manda sobre la documentación.

    Por qué se arregla AQUÍ y no en `canales._nombre_sugerido`, que es donde nace el `.oga`
    --y donde ya vive el arreglo hermano de `.jpe` -> `.jpg`--: aquel nombre existe para que
    el doctor vea algo legible en Telegram, y cambiarlo por eso sería cambiarlo por un
    motivo que no es el suyo. Peor: dejaría la transcripción colgando de una decisión
    cosmética, y el día que alguien la revisara se caería esto sin que nada lo dijera. El
    requisito es de la API, así que el nombre se fuerza en el borde que habla con la API.

    El default es `.ogg` porque es lo ÚNICO que manda WhatsApp en una nota de voz. Un mime
    que no esté en la tabla --un `audio/amr` reenviado-- sale igual y lo rechaza la API, que
    es lo correcto: `transcribir` lo convierte en «no se entendió» y el paciente recibe una
    frase pidiéndole que lo escriba, en vez de un silencio.
    """
    base = (archivo.mime or "").split(";")[0].strip().lower()
    return f"nota{_EXTENSION_PARA_LA_API.get(base, '.ogg')}"


def _cliente_por_defecto():
    """El cliente de OpenAI, construido tarde y no al importar.

    Tarde porque `OPENAI_API_KEY` la pone `cargar_dotenv`, que corre DESPUÉS de los imports
    en `runtime.py`. Construirlo en el módulo lo ataría a un entorno que todavía no está
    completo, que es la misma razón por la que `lectura._permiso_para_leer` es perezoso.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI()


async def transcribir(
    archivo: ArchivoDescargado,
    *,
    cliente=None,
    modelo: str = MODELO_TRANSCRIPTOR,
    database_url: str | None = None,
    telefono: str | None = None,
    id_conversacion: str | None = None,
) -> str | None:
    """Lo que dijo el paciente. `None` si no se pudo, y NUNCA propaga.

    `cliente` existe solo para poder probar esto sin red.

    Que devuelva `None` en vez de lanzar es la misma decisión que toma `lectura.leer_archivo`,
    con una razón de más: quien llama es una tarea de fondo que ya le entregó el audio al
    doctor, así que una excepción aquí solo llegaría a un log -- y encima dejaría al paciente
    sin la frase que le pide escribirlo.

    **Sin `prompt`, y eso es deliberado.** La tentación era pasarle el vocabulario de la
    clínica --ortodoncia, cordales, bichectomía-- para que acertara los términos. Se midió y
    EMPEORA: sobre los mismos audios convirtió «por favor» en «por ambos», «doctora» en
    «doctor» y «droga» en «buróga». Un `prompt` de transcripción no es una instrucción, es un
    sesgo, y sesgar hacia lo que esperas oír es la dirección equivocada de error cuando al
    otro lado puede haber alguien describiendo un síntoma.
    """
    usar = cliente or _cliente_por_defecto()
    try:
        # `BytesIO` con `.name`, que es como el SDK le pasa el nombre a la API. No se
        # reutiliza `archivo.nombre`: ver `_nombre_para_la_api`.
        envoltorio = io.BytesIO(archivo.contenido)
        envoltorio.name = _nombre_para_la_api(archivo)
        respuesta = await usar.audio.transcriptions.create(
            model=modelo, file=envoltorio, language=IDIOMA
        )
    except Exception:  # noqa: BLE001 -- ver el docstring
        log.exception("no se pudo transcribir %s (%s)", archivo.nombre, archivo.mime)
        return None

    texto = (getattr(respuesta, "text", "") or "").strip()
    if not texto:
        # Un audio de puro ruido, o de dos segundos de silencio. Devolverlo vacío haría que
        # `atencion` compusiera una entrada con unas comillas y nada dentro.
        log.info("la transcripción de %s salió vacía", archivo.nombre)
        return None

    if database_url:
        # Opcional por lo mismo que en `lectura.leer_archivo`: las pruebas llaman a esto con
        # un doble y sin base, y transcribir no puede exigir Postgres para funcionar.
        await consumo.anotar_transcripcion(
            respuesta,
            modelo=modelo,
            database_url=database_url,
            id_conversacion=id_conversacion,
            telefono=telefono,
        )
    return texto


def formatear_para_el_doctor(texto: str) -> str:
    """La transcripción como la ve el doctor en el hilo, debajo del audio.

    Se escapa porque `canales` manda todo con `parse_mode: "HTML"` y esto es texto de un
    desconocido: un paciente que diga «menos de 3 < 5» reventaría el mensaje entero. Misma
    razón y mismo orden que `lectura.formatear_para_el_doctor`.

    Va en cursiva y entre comillas, y eso no es adorno: el doctor tiene que poder distinguir
    de un vistazo lo que dijo el paciente de lo que escribió el sistema. La marca de que es
    automático va fuera de las comillas por la misma razón.
    """
    return f"🎙️ <i>«{html.escape(texto, quote=False)}»</i>"


async def transcribir_y_repartir(
    archivo: ArchivoDescargado,
    *,
    telegram,
    tema_id: int | None,
    cliente=None,
    modelo: str = MODELO_TRANSCRIPTOR,
    silencioso: bool = False,
    database_url: str | None = None,
    telefono: str | None = None,
    id_conversacion: str | None = None,
) -> str | None:
    """Transcribe, lo deja en el tema del paciente y devuelve el texto para el turno.

    A diferencia de `lectura.leer_y_repartir` no hay nada que repartir: **lo que ve el doctor
    y lo que ve Daniela son lo mismo**, y tiene que serlo. El muro de `lectura.py` existe
    porque del análisis de una radiografía sale contenido clínico que el paciente no puede
    recibir; aquí lo único que hay son las palabras que el propio paciente acaba de decir, y
    esconderle a Daniela la mitad de lo que le dijeron no protegería a nadie.

    `silencioso` lo fija quien llama y vale lo mismo que valió para el audio: la
    transcripción acompaña al archivo y suena exactamente donde sonó él. Ver NOTA DEL
    SILENCIO en `canales`.
    """
    texto = await transcribir(
        archivo,
        cliente=cliente,
        modelo=modelo,
        database_url=database_url,
        telefono=telefono,
        id_conversacion=id_conversacion,
    )

    aviso = (
        formatear_para_el_doctor(texto)
        if texto
        else "🎙️ <i>No se pudo entender esta nota de voz.</i>"
    )
    try:
        await telegram.enviar_mensaje(aviso, tema_id=tema_id, silencioso=silencioso)
    except Exception:  # noqa: BLE001
        # El audio YA está entregado cuando se llega aquí. Que su transcripción no consiga
        # bajar al hilo no puede llevarse por delante el turno del paciente, que es lo que
        # sigue a esto -- por eso el `return` está fuera del `try` y devuelve el texto igual.
        log.exception("la transcripción no llegó a Telegram; el audio sí está")
    return texto

"""La nota de voz: que llegue a la API en un formato que acepte, y que fallar no duela.

Sin red y sin base. Lo que estas pruebas NO pueden comprobar es que la transcripción acierte
--eso es del modelo y se mide con `scripts/probar_transcripcion.py` contra audios reales-- ni
que la API siga aceptando el ogg/opus de WhatsApp, que offline va doblado.
"""

from __future__ import annotations

import asyncio

import pytest

from maxicare_daniela import transcripcion
from maxicare_daniela.canales import ArchivoDescargado


def _audio(mime: str = "audio/ogg", tamano: int = 20_000) -> ArchivoDescargado:
    """Un audio como el que produce `canales.descargar_media` para una nota de voz.

    El nombre lleva `.oga` a propósito: es lo que devuelve
    `mimetypes.guess_extension("audio/ogg")` y por tanto lo que hay en producción.
    """
    return ArchivoDescargado(contenido=b"\x00" * tamano, mime=mime, nombre="1234567890.oga")


class ClienteFalso:
    """Imita `AsyncOpenAI` hasta donde llega este módulo, y guarda con qué lo llamaron."""

    def __init__(self, texto: str = "hola, quiero una cita", revienta: Exception | None = None):
        self.texto = texto
        self.revienta = revienta
        self.llamadas: list[dict] = []
        self.audio = self

    @property
    def transcriptions(self):
        return self

    async def create(self, **kwargs):
        envoltorio = kwargs["file"]
        self.llamadas.append(
            {
                "modelo": kwargs.get("model"),
                "nombre": getattr(envoltorio, "name", None),
                "language": kwargs.get("language"),
                # Se guarda la CLAVE, no el valor: la prueba del `prompt` afirma que no
                # existe, y `kwargs.get` no distingue «ausente» de «None».
                "claves": sorted(kwargs),
            }
        )
        if self.revienta is not None:
            raise self.revienta
        return type("Respuesta", (), {"text": self.texto, "usage": None})()


def _transcribir(archivo, cliente):
    return asyncio.run(transcripcion.transcribir(archivo, cliente=cliente))


# ==========================================================================================
# El formato: la trampa que valía un 400 en el 100 % de los casos
# ==========================================================================================


def test_el_nombre_que_se_le_manda_a_la_API_NUNCA_es_el_del_archivo():
    """LA PRUEBA QUE NO SE PUEDE RELAJAR de este módulo.

    `canales._nombre_sugerido` produce `1234567890.oga` para un `audio/ogg`, porque eso es lo
    que devuelve `mimetypes.guess_extension`. Y la API responde, textual:

        400 invalid_request_error: Unsupported file format oga

    Comprobado contra las cinco notas de voz reales de producción: las cinco fallaron con el
    nombre del proyecto y las cinco transcribieron bien con el mismo byte a byte renombrado a
    `.ogg`. `.oga` es un alias legítimo de Ogg y la documentación de OpenAI lo lista; la API
    lo rechaza igual.

    Sin esta prueba, alguien «simplifica» a `archivo.nombre` --que es lo natural, y encima
    parece más correcto-- y la transcripción deja de funcionar ENTERA, en silencio: el error
    lo traga `transcribir`, Daniela vuelve a «no te entendí el audio» y no hay nada rojo en
    ningún sitio.
    """
    cliente = ClienteFalso()

    _transcribir(_audio(), cliente)

    assert cliente.llamadas[0]["nombre"].endswith(".ogg")
    assert not cliente.llamadas[0]["nombre"].endswith(".oga")


@pytest.mark.parametrize(
    ("mime", "extension"),
    [
        ("audio/ogg", ".ogg"),
        ("audio/ogg; codecs=opus", ".ogg"),  # lo que declara WhatsApp, con parámetros
        ("audio/mpeg", ".mp3"),
        ("audio/mp4", ".m4a"),
        ("audio/algo-que-no-existe", ".ogg"),  # el default: lo que manda WhatsApp
    ],
)
def test_cada_mime_sale_con_una_extension_que_la_API_conoce(mime, extension):
    cliente = ClienteFalso()

    _transcribir(_audio(mime=mime), cliente)

    assert cliente.llamadas[0]["nombre"] == f"nota{extension}"


# ==========================================================================================
# Lo que se le manda al modelo, y lo que NO
# ==========================================================================================


def test_el_idioma_va_fijo_en_espanol():
    """Medido sobre el mismo audio: sin idioma salió «Ya, conchena hablo de una doctora»;
    con `language=es`, «Ya, ¿con quién hablo yo, una doctora o un doctor?»."""
    cliente = ClienteFalso()

    _transcribir(_audio(), cliente)

    assert cliente.llamadas[0]["language"] == "es"


def test_NO_se_le_pasa_un_prompt_de_vocabulario():
    """La tentación era sesgarlo hacia los términos de la clínica. Se midió y EMPEORA: sobre
    los mismos audios convirtió «por favor» en «por ambos», «doctora» en «doctor» y «droga»
    en «buróga».

    Un `prompt` de transcripción no es una instrucción, es un sesgo, y sesgar hacia lo que
    esperas oír es la dirección equivocada de error cuando al otro lado puede haber alguien
    describiendo un síntoma.
    """
    cliente = ClienteFalso()

    _transcribir(_audio(), cliente)

    assert "prompt" not in cliente.llamadas[0]["claves"]


# ==========================================================================================
# El tope y la dirección de fallo
# ==========================================================================================


@pytest.mark.parametrize(
    ("tipo", "tamano", "esperado"),
    [
        ("audio", 20_000, True),
        ("voice", 20_000, True),
        ("audio", transcripcion.TOPE_BYTES_TRANSCRIPCION, True),  # justo en el tope
        ("audio", transcripcion.TOPE_BYTES_TRANSCRIPCION + 1, False),
        ("image", 20_000, False),
        ("document", 20_000, False),
    ],
)
def test_que_vale_la_pena_transcribir(tipo, tamano, esperado):
    assert transcripcion.vale_la_pena_transcribir(tipo, tamano) is esperado


def test_los_dos_carriles_son_DISJUNTOS():
    """`atencion` da por hecho que un mensaje no puede traer lectura Y transcripción a la
    vez: de ahí sale que las dos esperas secuenciales no cuesten nada. Si algún día se
    solapan, esa suposición deja de ser cierta sin que nada lo diga."""
    from maxicare_daniela import lectura

    assert not (transcripcion.TIPOS_QUE_SE_TRANSCRIBEN & lectura.TIPOS_QUE_SE_LEEN)


def test_si_la_API_revienta_devuelve_None_y_no_propaga():
    """Quien llama es una tarea de fondo que YA le entregó el audio al doctor. Una excepción
    aquí solo llegaría a un log, y encima dejaría al paciente sin la frase que le pide
    escribirlo."""
    cliente = ClienteFalso(revienta=RuntimeError("la API dijo que no"))

    assert _transcribir(_audio(), cliente) is None


def test_una_transcripcion_vacia_cuenta_como_no_entendida():
    """Un audio de puro ruido, o dos segundos de silencio. Devolver la cadena vacía haría que
    `atencion` compusiera una entrada con unas comillas y nada dentro."""
    assert _transcribir(_audio(), ClienteFalso(texto="   ")) is None


def test_el_texto_vuelve_sin_espacios_de_sobra():
    assert _transcribir(_audio(), ClienteFalso(texto="  hola  ")) == "hola"


# ==========================================================================================
# Lo que baja al hilo del doctor
# ==========================================================================================


class TelegramFalso:
    def __init__(self, revienta: Exception | None = None):
        self.mensajes: list[tuple] = []
        self.revienta = revienta

    async def enviar_mensaje(self, texto, *, tema_id=None, silencioso=False, **_):
        if self.revienta is not None:
            raise self.revienta
        # Se guarda el `tema_id`, no solo el texto: un doble que lo tirara dejaría pasar que
        # la transcripción de un paciente cayera en el hilo de otro. Misma regla que el resto
        # de dobles de Telegram del proyecto.
        self.mensajes.append((texto, tema_id, silencioso))
        return 1


def _repartir(cliente, telegram, **extra):
    return asyncio.run(
        transcripcion.transcribir_y_repartir(
            _audio(), telegram=telegram, tema_id=77, cliente=cliente, **extra
        )
    )


def test_la_transcripcion_baja_al_hilo_del_paciente_y_vuelve_para_el_turno():
    telegram = TelegramFalso()

    texto = _repartir(ClienteFalso(texto="quiero una cita"), telegram)

    assert texto == "quiero una cita"
    assert telegram.mensajes[0][1] == 77
    assert "quiero una cita" in telegram.mensajes[0][0]


def test_lo_que_dice_el_paciente_se_escapa_antes_de_ir_a_telegram():
    """`canales` manda todo con `parse_mode: "HTML"`, y esto es texto de un desconocido: un
    paciente que diga «menos de 3 < 5» reventaría el mensaje entero."""
    telegram = TelegramFalso()

    _repartir(ClienteFalso(texto="me duele el <3 diente"), telegram)

    assert "&lt;3" in telegram.mensajes[0][0]
    assert "<3" not in telegram.mensajes[0][0].replace("&lt;3", "")


def test_el_silencio_lo_decide_quien_llama():
    """La transcripción acompaña al audio y suena exactamente donde sonó él. Si notificara
    por su cuenta, haber callado el archivo no habría servido de nada."""
    telegram = TelegramFalso()

    _repartir(ClienteFalso(), telegram, silencioso=True)

    assert telegram.mensajes[0][2] is True


def test_si_telegram_falla_el_turno_del_paciente_sigue_teniendo_su_texto():
    """El audio YA está entregado cuando se llega aquí. Que su transcripción no consiga bajar
    al hilo no puede llevarse por delante lo que el paciente preguntó."""
    telegram = TelegramFalso(revienta=RuntimeError("Telegram dijo que no"))

    assert _repartir(ClienteFalso(texto="quiero una cita"), telegram) == "quiero una cita"


def test_cuando_no_se_entiende_el_doctor_tambien_se_entera():
    """El hilo es el expediente del paciente. Un audio suelto sin nada debajo deja al doctor
    sin saber si el sistema lo intentó."""
    telegram = TelegramFalso()

    assert _repartir(ClienteFalso(texto=""), telegram) is None
    assert "No se pudo entender" in telegram.mensajes[0][0]

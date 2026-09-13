"""Pruebas del entregable de la fase 2: que un archivo entre y llegue.

Ninguna toca la red ni la base. Todo lo que se prueba aquí es puro a propósito — el parseo
del webhook, la firma y el texto que ven los doctores — porque son las tres cosas que, si
fallan, fallan en silencio: un payload mal leído no lanza excepción, simplemente pierde el
archivo.

Los payloads son la forma real de WhatsApp Cloud API v21.0, no una versión simplificada.
Una prueba contra un payload inventado solo prueba que el parseador entiende lo inventado.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from maxicare_daniela.ingesta import (
    MensajeEntrante,
    componer_aviso,
    extraer_mensajes,
    firma_valida,
)

SECRETO = "un-app-secret-de-prueba"


def _sobre(*mensajes, contactos=None, statuses=None) -> dict:
    """La envoltura de cuatro niveles que Meta manda siempre."""
    valor = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"}}
    if contactos is not None:
        valor["contacts"] = contactos
    if mensajes:
        valor["messages"] = list(mensajes)
    if statuses is not None:
        valor["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": valor}]}],
    }


# ==========================================================================================
# EL TEST DE LA FASE 2
# ==========================================================================================


def test_una_radiografia_se_extrae_con_su_media_id():
    """El entregable entero depende de este `media_id`: es lo único que se puede canjear
    por el archivo, y el canje tiene fecha de caducidad. Si el parseo lo pierde, no hay
    forma de recuperarlo — el paciente tendría que volver a mandarlo."""
    payload = _sobre(
        {
            "from": "573001234567",
            "id": "wamid.HBgMNTczMDAxMjM0NTY3",
            "timestamp": "1757700000",
            "type": "image",
            "image": {
                "mime_type": "image/jpeg",
                "sha256": "abc123",
                "id": "1234567890123456",
                "caption": "esta es la radiografia que me pidieron",
            },
        },
        contactos=[{"profile": {"name": "Carlos Mendoza"}, "wa_id": "573001234567"}],
    )

    (m,) = extraer_mensajes(payload)

    assert m.media_id == "1234567890123456"
    assert m.trae_archivo is True
    assert m.tipo == "image"
    assert m.mime == "image/jpeg"
    assert m.telefono == "573001234567"
    assert m.nombre_perfil == "Carlos Mendoza"
    # El caption es contexto clínico del paciente sobre su propio archivo: perderlo sería
    # entregarle al doctor una imagen sin saber por qué la mandaron.
    assert m.texto == "esta es la radiografia que me pidieron"


def test_el_caption_de_un_adjunto_no_se_pierde():
    payload = _sobre({
        "from": "573001112222", "id": "wamid.X", "type": "document",
        "document": {"id": "999", "mime_type": "application/pdf",
                     "filename": "remision.pdf", "caption": "me remitieron a ortodoncia"},
    })
    (m,) = extraer_mensajes(payload)
    assert m.texto == "me remitieron a ortodoncia"
    assert m.nombre_archivo == "remision.pdf"


# ==========================================================================================
# Lo que NO debe llegar a los doctores
# ==========================================================================================


def test_los_acuses_de_entrega_no_son_mensajes():
    """Meta manda un webhook por cada cambio de estado de lo que NOSOTROS enviamos: enviado,
    entregado, leído. Llegan constantemente. Si se confundieran con mensajes entrantes, los
    doctores recibirían un aviso cada vez que Daniela contesta."""
    payload = _sobre(statuses=[{
        "id": "wamid.enviado.por.nosotros", "status": "delivered",
        "timestamp": "1757700000", "recipient_id": "573001234567",
    }])
    assert extraer_mensajes(payload) == []


@pytest.mark.parametrize("payload", [
    {},
    {"entry": []},
    {"entry": [{"changes": []}]},
    {"entry": [{"changes": [{"value": {}}]}]},
    {"object": "otra_cosa", "entry": None},
])
def test_un_payload_raro_no_lanza(payload):
    """Una excepción aquí devolvería 500 a Meta, que reintentaría el mismo payload roto en
    bucle. Un webhook que no se entiende se ignora, no tumba el servidor."""
    assert extraer_mensajes(payload) == []


def test_un_mensaje_sin_id_o_sin_remitente_se_descarta():
    """Sin `wamid` no hay deduplicación posible, y sin `from` no se sabe de quién es."""
    payload = _sobre(
        {"id": "wamid.sin.remitente", "type": "text", "text": {"body": "hola"}},
        {"from": "573001234567", "type": "text", "text": {"body": "hola"}},
    )
    assert extraer_mensajes(payload) == []


def test_varios_mensajes_en_un_solo_webhook():
    """Meta puede agrupar. Procesar solo el primero perdería los demás en silencio."""
    payload = _sobre(
        {"from": "573001", "id": "wamid.1", "type": "text", "text": {"body": "hola"}},
        {"from": "573002", "id": "wamid.2", "type": "image", "image": {"id": "m1"}},
    )
    assert [m.wamid for m in extraer_mensajes(payload)] == ["wamid.1", "wamid.2"]


# ==========================================================================================
# La firma: la URL del webhook es pública
# ==========================================================================================


def _firmar(cuerpo: bytes, secreto: str = SECRETO) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def test_una_firma_de_meta_se_acepta():
    cuerpo = json.dumps(_sobre()).encode()
    assert firma_valida(cuerpo, _firmar(cuerpo), SECRETO) is True


def test_un_cuerpo_alterado_se_rechaza():
    """Lo que protege no es solo quién llama, sino que el cuerpo no se haya tocado en el
    camino: la firma cubre los bytes exactos."""
    cuerpo = json.dumps(_sobre()).encode()
    firma = _firmar(cuerpo)
    assert firma_valida(cuerpo + b" ", firma, SECRETO) is False


@pytest.mark.parametrize("cabecera", [None, "", "sha256=", "deadbeef", "sha1=abc"])
def test_una_firma_ausente_o_con_otro_formato_se_rechaza(cabecera):
    assert firma_valida(b"{}", cabecera, SECRETO) is False


def test_sin_app_secret_configurado_no_se_acepta_nada():
    """Un secreto vacío no puede significar «déjalo pasar»: sería abrir el webhook al mundo
    justo cuando la configuración está incompleta, que es cuando nadie lo está mirando."""
    cuerpo = b"{}"
    assert firma_valida(cuerpo, _firmar(cuerpo, ""), "") is False


# ==========================================================================================
# El pie del mensaje: qué ES el archivo, nunca qué MUESTRA
# ==========================================================================================


def test_el_aviso_no_interpreta_el_contenido():
    """En la fase 2 no hay modelo. El pie describe el continente —tipo, peso, quién lo
    mandó— y jamás el contenido. La lectura clínica llega en la fase 6 y su destino sigue
    siendo este mismo tema."""
    m = MensajeEntrante(
        wamid="wamid.1", telefono="573001234567", nombre_perfil="Sofía Ramírez",
        tipo="image", media_id="m1", mime="image/jpeg",
    )
    aviso = componer_aviso(m, tamano=1_572_864)

    assert "Sofía Ramírez" in aviso
    assert "+573001234567" in aviso
    assert "image/jpeg" in aviso
    assert "1.5 MB" in aviso
    assert "una imagen" in aviso


def test_el_texto_del_paciente_no_puede_romper_el_formato():
    """El pie va en HTML y el texto lo escribe un desconocido. Sin escapar, un `<b>` del
    paciente cambiaría el formato del mensaje que ven los doctores, y un `<` suelto haría
    que Telegram rechazara el envío entero — perdiendo el archivo."""
    m = MensajeEntrante(
        wamid="w", telefono="573001", nombre_perfil="<b>Falso</b>", tipo="text",
        texto="me duele <mucho> & no aguanto",
    )
    aviso = componer_aviso(m)

    assert "&lt;b&gt;Falso&lt;/b&gt;" in aviso
    assert "&lt;mucho&gt;" in aviso
    assert "&amp;" in aviso


def test_sin_nombre_de_perfil_se_dice_en_vez_de_quedar_vacio():
    m = MensajeEntrante(wamid="w", telefono="573001", nombre_perfil=None, tipo="text",
                        texto="hola")
    assert "Sin nombre en el perfil" in componer_aviso(m)


@pytest.mark.parametrize("tipo,esperado", [
    ("voice", "una nota de voz"), ("video", "un video"),
    ("document", "un documento"), ("audio", "un audio"),
])
def test_cada_tipo_se_nombra_en_castellano(tipo, esperado):
    """«Mandó un voice» no lo entiende un doctor."""
    m = MensajeEntrante(wamid="w", telefono="573001", nombre_perfil="Ana", tipo=tipo,
                        media_id="m1", mime="application/octet-stream")
    assert esperado in componer_aviso(m, tamano=1000)


# ==========================================================================================
# El tema General: comprobado contra la API, no deducido
# ==========================================================================================


@pytest.mark.parametrize("tema_id", [None, 0])
def test_al_tema_general_no_se_le_manda_message_thread_id(tema_id):
    """Telegram usa el thread 1 para el General al ENTREGAR, pero lo rechaza al ESCRIBIR:
    `sendMessage` con `message_thread_id=1` responde `message thread not found`.

    Al General se escribe omitiendo el campo, y el 0 guardado en configuración significa
    justamente eso. Este test vigila el `if tema_id:` que lo implementa — con
    `if tema_id is not None`, el 0 se enviaría y el mensaje se perdería.

    Se prueba sin red: se mira el cuerpo que se habría enviado.
    """
    import httpx

    from maxicare_daniela.canales import Telegram

    enviados: list[dict] = []

    def capturar(request: httpx.Request) -> httpx.Response:
        enviados.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    transporte = httpx.MockTransport(capturar)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        import asyncio

        tg = Telegram("token-falso", "-1001234567890")
        asyncio.run(tg.enviar_mensaje("hola", tema_id=tema_id))
    finally:
        httpx.AsyncClient = original

    assert "message_thread_id" not in enviados[0], (
        "se envió message_thread_id al tema General: Telegram lo rechaza y el mensaje se "
        "pierde en silencio, porque a Meta ya se le respondió 200"
    )


def test_un_tema_de_paciente_si_lleva_su_thread():
    """El complemento del anterior: con un tema real, el campo tiene que ir."""
    import asyncio

    import httpx

    from maxicare_daniela.canales import Telegram

    enviados: list[dict] = []
    transporte = httpx.MockTransport(
        lambda r: (enviados.append(json.loads(r.content)),
                   httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}))[1]
    )
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        asyncio.run(Telegram("t", "-100123").enviar_mensaje("hola", tema_id=42))
    finally:
        httpx.AsyncClient = original

    assert enviados[0]["message_thread_id"] == 42


def test_una_radiografia_va_como_documento_y_no_como_foto():
    """`sendPhoto` recomprime y baja la resolución. Una radiografía recomprimida es una
    radiografía que el doctor no puede leer, así que las imágenes van por `sendDocument`."""
    from maxicare_daniela.canales import _ENVIO_POR_TIPO

    assert _ENVIO_POR_TIPO["image"][0] == "sendDocument"
    assert _ENVIO_POR_TIPO["document"][0] == "sendDocument"
    assert _ENVIO_POR_TIPO["voice"][0] == "sendVoice"


# ==========================================================================================
# Crear y cerrar temas en Telegram
# ==========================================================================================


def test_crear_tema_pide_el_nombre_y_devuelve_el_id():
    import asyncio
    import httpx

    from maxicare_daniela.canales import Telegram

    llamadas: list[tuple[str, dict]] = []

    def capturar(request: httpx.Request) -> httpx.Response:
        llamadas.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_thread_id": 91, "name": "x"}}
        )

    transporte = httpx.MockTransport(capturar)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        tema = asyncio.run(Telegram("t", "-100123").crear_tema("Ana Perez · +573001112233"))
    finally:
        httpx.AsyncClient = original

    assert tema == 91
    assert llamadas[0][0].endswith("/createForumTopic")
    assert llamadas[0][1]["name"] == "Ana Perez · +573001112233"


def test_cerrar_tema_manda_el_thread_id():
    import asyncio
    import httpx

    from maxicare_daniela.canales import Telegram

    llamadas: list[tuple[str, dict]] = []

    def capturar(request: httpx.Request) -> httpx.Response:
        llamadas.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    transporte = httpx.MockTransport(capturar)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        asyncio.run(Telegram("t", "-100123").cerrar_tema(91))
    finally:
        httpx.AsyncClient = original

    assert llamadas[0][0].endswith("/closeForumTopic")
    assert llamadas[0][1]["message_thread_id"] == 91


def test_un_tema_que_telegram_rechaza_no_pasa_por_bueno():
    """Sin esto, un `ok: false` devolveria un KeyError sin nombre y el archivo se perderia
    buscando un tema que no existe."""
    import asyncio
    import httpx

    from maxicare_daniela.canales import ErrorDeCanal, Telegram

    transporte = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"ok": False, "description": "not enough rights to manage topics"}
        )
    )
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        with pytest.raises(ErrorDeCanal, match="not enough rights"):
            asyncio.run(Telegram("t", "-100123").crear_tema("Ana"))
    finally:
        httpx.AsyncClient = original


# ==========================================================================================
# El destino del archivo (fase 6B)
# ==========================================================================================

TEMA_GENERAL = 0
TEMA_DE_ANA = 901


class TelegramConTemas:
    """Registra a qué tema fue cada cosa. Es lo único que estas pruebas miden."""

    def __init__(self) -> None:
        self.archivos: list[tuple[str, int | None]] = []
        self.mensajes: list[tuple[str, int | None]] = []

    async def enviar_archivo(self, archivo, *, tipo_whatsapp, pie, tema_id=None) -> int:
        self.archivos.append((archivo.nombre, tema_id))
        return 10 + len(self.archivos)

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None) -> int:
        self.mensajes.append((texto, tema_id))
        return 20 + len(self.mensajes)


class WhatsAppConArchivo:
    def __init__(self, *, tamano: int = 2048) -> None:
        self.tamano = tamano

    async def descargar_media(self, media_id, *, nombre_original=None):
        from maxicare_daniela.canales import ArchivoDescargado

        return ArchivoDescargado(
            contenido=b"x" * self.tamano, mime="image/jpeg", nombre="radio.jpg"
        )


def _devuelve_async(valor):
    async def _fn(*a, **kw):
        return valor

    return _fn


async def _revienta_si_se_llama(*a, **kw):
    raise AssertionError("se arranco el lector cuando no debia: eso es dinero tirado")


def _sin_base(monkeypatch):
    """`procesar_mensaje` registra en Neon; aquí eso no es lo que se mide."""
    from maxicare_daniela import ingesta as mod

    monkeypatch.setattr(mod, "_registrar", lambda url, m: True)
    monkeypatch.setattr(mod, "_marcar_reenviado", lambda url, w, t, s: None)
    monkeypatch.setattr(mod, "_marcar_fallo", lambda url, w, e: None)


def _mensaje_con_foto(**cambios):
    from maxicare_daniela.ingesta import MensajeEntrante

    campos = dict(
        wamid="wamid-foto-1",
        telefono="573001112233",
        nombre_perfil="Ana Perez",
        tipo="image",
        media_id="media-1",
        mime="image/jpeg",
    )
    campos.update(cambios)
    return MensajeEntrante(**campos)


def test_el_archivo_va_al_tema_del_paciente_y_el_aviso_al_general(monkeypatch):
    """El cambio visible de 6B: el archivo deja de caer en el General.

    Al General va un aviso, porque es donde los doctores miran; el archivo se deposita en
    el hilo de esa persona, que es el que conserva su historial.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(
        lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA)
    )
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert tg.archivos == [("radio.jpg", TEMA_DE_ANA)]
    assert len(tg.mensajes) == 1
    texto, tema = tg.mensajes[0]
    assert tema == TEMA_GENERAL
    assert "Ana Perez" in texto


def test_un_texto_suelto_sigue_yendo_al_general(monkeypatch):
    """Lo único que se muda al tema del paciente es el ARCHIVO."""
    import asyncio

    from maxicare_daniela import ingesta
    from maxicare_daniela.ingesta import MensajeEntrante

    _sin_base(monkeypatch)
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            MensajeEntrante(
                wamid="w-1", telefono="573001112233", nombre_perfil="Ana",
                tipo="text", texto="Hola",
            ),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert tg.archivos == []
    assert tg.mensajes[0][1] == TEMA_GENERAL


def test_el_lector_no_retrasa_la_entrega_del_archivo(monkeypatch):
    """La garantía de la fase 2, como aserción y no como comentario.

    Con un lector que tarda un segundo, `procesar_mensaje` tiene que haber vuelto --y el
    archivo estar ya en Telegram-- mucho antes de que el lector termine. Si alguien pone un
    `await` delante de la entrega, esta prueba cae.
    """
    import asyncio
    import time

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))

    async def lector_lento(*a, **kw):
        await asyncio.sleep(1.0)
        return None

    monkeypatch.setattr(lectura, "leer_y_repartir", lector_lento)
    tg = TelegramConTemas()

    async def corrida():
        arranque = time.monotonic()
        resultado = await ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
        tardo = time.monotonic() - arranque
        if resultado.lectura is not None:
            resultado.lectura.cancel()
        return resultado, tardo

    resultado, tardo = asyncio.run(corrida())

    assert resultado.reenviado is True
    assert tg.archivos, "el archivo no llego a Telegram"
    assert tardo < 0.3, f"la entrega del archivo espero al lector: tardo {tardo:.2f} s"
    assert resultado.lectura is not None, "no se arranco el lector"


def test_si_no_hay_tema_el_archivo_cae_al_general(monkeypatch):
    """Degradar, no perder. Un fallo de Telegram al crear el tema no puede dejar al doctor
    sin la radiografia."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(None))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert tg.archivos == [("radio.jpg", TEMA_GENERAL)]
    assert tg.mensajes == [], "sin tema propio no hay nada que avisar: el archivo YA esta ahi"


@pytest.mark.parametrize("tipo,mime", [("audio", "audio/ogg"), ("sticker", "image/webp")])
def test_lo_que_no_se_lee_no_arranca_el_lector(monkeypatch, tipo, mime):
    """`sol` es el modelo caro y no se paga por un sticker."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _revienta_si_se_llama)

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(tipo=tipo, mime=mime),
            whatsapp=WhatsAppConArchivo(),
            telegram=TelegramConTemas(),
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert resultado.lectura is None


def test_un_archivo_enorme_no_arranca_el_lector(monkeypatch):
    """Llega al doctor igual; lo unico que no ocurre es la lectura."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _revienta_si_se_llama)
    tg = TelegramConTemas()

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(tamano=lectura.TOPE_BYTES_LECTOR + 1),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert resultado.lectura is None
    assert tg.archivos, "el archivo grande tiene que llegar al doctor igual"
    """Sin esto, un `ok: false` devolveria un KeyError sin nombre y el archivo se perderia
    buscando un tema que no existe."""
    import asyncio
    import httpx

    from maxicare_daniela.canales import ErrorDeCanal, Telegram

    transporte = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"ok": False, "description": "not enough rights to manage topics"}
        )
    )
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        with pytest.raises(ErrorDeCanal, match="not enough rights"):
            asyncio.run(Telegram("t", "-100123").crear_tema("Ana"))
    finally:
        httpx.AsyncClient = original

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
        #: Lo mismo que `mensajes`/`archivos` pero con el `silencioso` de cada envío. Van en
        #: listas aparte para no tocar las aserciones que ya existían.
        self.mensajes_con_silencio: list[tuple[str, int | None, bool]] = []
        self.archivos_con_silencio: list[tuple[str, int | None, bool]] = []

    async def enviar_archivo(
        self, archivo, *, tipo_whatsapp, pie, tema_id=None, silencioso=False
    ) -> int:
        self.archivos.append((archivo.nombre, tema_id))
        self.archivos_con_silencio.append((archivo.nombre, tema_id, silencioso))
        return 10 + len(self.archivos)

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.mensajes.append((texto, tema_id))
        self.mensajes_con_silencio.append((texto, tema_id, silencioso))
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
    """`procesar_mensaje` registra en Neon; aquí eso no es lo que se mide.

    `_conversacion_viva` va aquí desde la ronda de arreglo de la revisión de las Tareas
    9-11: sin doblarla, la tarea de fondo del lector (`_leer_con_grupo`, en cualquier
    prueba donde el mensaje trae una imagen que sí vale la pena leer) abre una conexión de
    verdad a `postgresql://x` en un hilo. `.cancel()` no cancela ese hilo -- el coste se
    paga en el teardown de `asyncio.run`, que espera al executor -- y cinco pruebas de esta
    suite pasaron de instantáneas a tardar entre 2.27 s y 2.77 s cada una. Medido: la suite
    entera pasó de ser prácticamente instantánea a 14.2 s.
    """
    from maxicare_daniela import ingesta as mod

    monkeypatch.setattr(mod, "_registrar", lambda url, m: True)
    monkeypatch.setattr(mod, "_marcar_reenviado", lambda url, w, t, s: None)
    monkeypatch.setattr(mod, "_marcar_fallo", lambda url, w, e: None)
    monkeypatch.setattr(mod, "_conversacion_viva", lambda url, telefono: None)
    # Por defecto: el número no tiene tema, y cada archivo es el primero de su tanda. Las
    # pruebas que miden lo contrario lo sobrescriben.
    monkeypatch.setattr(mod, "_tema_existente", lambda url, telefono: None)
    # Y nadie está en relevo (6C), que es el caso normal. Sin doblarla, `_en_relevo` abre
    # una conexión de verdad a `postgresql://x` -- exactamente el coste que describe el
    # docstring de arriba, y que ya hizo caer a
    # `test_el_lector_no_retrasa_la_entrega_del_archivo` con 2.78 s de espera.
    monkeypatch.setattr(mod, "_en_relevo", lambda url, telefono: False)
    # Y guardar la transcripción (migración 027) tampoco abre conexión: mismo motivo que las
    # de arriba. La prueba que mide que se guarda la sobrescribe con un capturador.
    monkeypatch.setattr(mod, "_guardar_transcripcion", lambda url, wamid, texto: None)


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


def test_el_archivo_va_al_tema_del_paciente_y_el_General_NO_se_entera(monkeypatch):
    """El archivo va al hilo de esa persona, y el General se queda sin una sola línea.

    Esta prueba decía otra cosa hasta el 22/09/2026 --se llamaba `..._y_el_aviso_al_general`
    y exigía el «📎 Ana Perez mandó archivos»--. MaxiCare pidió que al escritorio común de
    los doctores solo lleguen las alertas de escalamiento: un archivo no le pide nada a
    nadie mientras Daniela lo esté atendiendo. Ver NOTA DEL DESTINO ÚNICO en `ingesta.py`.
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
        )
    )

    assert tg.archivos == [("radio.jpg", TEMA_DE_ANA)]
    assert tg.mensajes == [], f"el General recibió algo: {tg.mensajes}"


# ==========================================================================================
# El General se queda solo con lo que pide algo del doctor (13/09/2026)
# ==========================================================================================
#
# Ver NOTA DEL TEXTO SIN TEMA en `ingesta.py`. Estas cinco pruebas son el contrato entero:
# lo que suena, lo que se archiva mudo y lo que no se manda.


def _texto(**cambios):
    from maxicare_daniela.ingesta import MensajeEntrante

    campos = dict(
        wamid="w-1",
        telefono="573001112233",
        nombre_perfil="Ana",
        tipo="text",
        texto="Hola",
    )
    campos.update(cambios)
    return MensajeEntrante(**campos)


def _correr_texto(tg, monkeypatch, **cambios):
    import asyncio

    from maxicare_daniela import ingesta

    return asyncio.run(
        ingesta.procesar_mensaje(
            _texto(**cambios),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
    )


def test_un_texto_no_llega_nunca_al_general(monkeypatch):
    """El ruido que el doctor vio en producción: siete notificaciones por una conversación
    que Daniela resolvió sola, con el escalamiento enterrado entre ellas.

    Con tema, el texto se archiva en el hilo del paciente. Al General, NADA.
    """
    from maxicare_daniela import ingesta

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: TEMA_DE_ANA)
    tg = TelegramConTemas()

    _correr_texto(tg, monkeypatch)

    assert tg.archivos == []
    assert [tema for _, tema in tg.mensajes] == [TEMA_DE_ANA]
    assert TEMA_GENERAL not in [tema for _, tema in tg.mensajes]


def test_el_texto_archivado_no_notifica(monkeypatch):
    """Mudarlo de sitio sin callarlo habría cambiado el ruido de sitio, no quitado."""
    from maxicare_daniela import ingesta

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: TEMA_DE_ANA)
    tg = TelegramConTemas()

    _correr_texto(tg, monkeypatch)

    _, _, silencioso = tg.mensajes_con_silencio[0]
    assert silencioso is True


def test_un_texto_sin_tema_no_se_manda_a_ninguna_parte(monkeypatch):
    """El número que todavía no tiene hilo no estrena uno por escribir «buenas tardes».

    Es la regla que cerró la fase 6: si cada saludo abriera un tema, el grupo sería
    inservible en una semana. Y como no hay hilo donde archivarlo, no se manda nada: la
    conversación vive en `mensajes_entrantes` y en el historial del agente.
    """
    _sin_base(monkeypatch)  # el default ya es «sin tema»
    tg = TelegramConTemas()

    resultado = _correr_texto(tg, monkeypatch)

    assert tg.mensajes == []
    assert tg.archivos == []
    # No reenviado, pero tampoco un fallo: son dos cosas distintas y la base las distingue.
    assert resultado.reenviado is False
    assert resultado.fallo is None


def test_un_texto_nunca_crea_el_tema(monkeypatch):
    """`_tema_existente` es una CONSULTA. Si alguien la cambia por `lectura.asegurar_tema`,
    cada saludo abriría un hilo y esta prueba cae."""
    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)

    async def _revienta(*a, **kw):
        raise AssertionError("un texto suelto no puede abrir el tema de nadie")

    monkeypatch.setattr(lectura, "asegurar_tema", _revienta)
    monkeypatch.setattr(lectura, "crear_tema", _revienta, raising=False)
    tg = TelegramConTemas()

    _correr_texto(tg, monkeypatch)

    assert tg.mensajes == []


def test_una_TANDA_de_archivos_no_timbra_ni_una_vez(monkeypatch):
    """La radiografía, la foto de la encía y la del carné: tres archivos, cero timbrazos.

    Aquí vivía `test_el_segundo_archivo_de_la_tanda_no_vuelve_a_avisar`, que comprobaba que
    el aviso del General salía UNA vez por tanda y no una por archivo. Desde el 22/09/2026
    no sale ninguna: `_avisar_de_la_tanda` y su ventana de 24 h se fueron enteros. Lo que se
    conserva intacto --y es lo que esta prueba defiende ahora-- es que los tres archivos SÍ
    se depositan en el expediente del paciente.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    for n in range(3):
        asyncio.run(
            ingesta.procesar_mensaje(
                _mensaje_con_foto(wamid=f"wamid-foto-{n}"),
                whatsapp=WhatsAppConArchivo(),
                telegram=tg,
                database_url="postgresql://x",
            )
        )

    assert [tema for _, tema in tg.archivos] == [TEMA_DE_ANA] * 3
    assert tg.mensajes == [], f"el General recibió algo: {tg.mensajes}"


def test_un_archivo_SIN_hilo_no_se_deposita_en_ninguna_parte(monkeypatch):
    """La línea que MaxiCare reportó como «cierro el tema y sus mensajes van al General».

    Era `destino = tema or tema_general`, y con `silencioso=bool(tema) and ...` el archivo
    salía además SONANDO. Los tres caminos que llegan aquí --el número sin hilo todavía, el
    hilo borrado y el tope de 5 s-- convertían un problema de infraestructura en el mensaje
    de un paciente vibrando en el teléfono de todos los doctores.

    Lo que se conserva: el archivo se descarga, se registra y Daniela contesta. Lo único que
    no ocurre es el depósito, y su constancia la vuelca el próximo escalamiento
    (`lectura.rescatar_hilo`).
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(None))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
    )

    assert tg.archivos == [], "el archivo de un paciente cayó en el General"
    assert tg.mensajes == [], "algo del paciente cayó en el General"
    assert resultado.nuevo is True, "el mensaje se procesó igual: no se pierde el turno"
    assert resultado.reenviado is False


def test_el_archivo_entra_MUDO_en_el_hilo_del_paciente(monkeypatch):
    """Fuera de un relevo el hilo es un expediente y no suena nunca.

    Esta prueba tenía una segunda mitad --«sin tema, el archivo ES lo que llega al General,
    así que tiene que sonar»-- que dejó de existir el 22/09/2026: sin tema ya no llega a
    ninguna parte, y eso lo fija la prueba de arriba.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
    )

    assert tg.archivos_con_silencio[0][2] is True


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


def test_un_tema_lento_tampoco_retrasa_la_entrega_del_archivo(monkeypatch):
    """IMPORTANTE de la revisión final: `asegurar_tema` tenía hasta 30 s de reloj delante.

    En el primer archivo de un paciente, `asegurar_tema` encadena `crear_tema` y
    `cerrar_tema`, cada una con `TIMEOUT_NORMAL` (15 s). Con Telegram lento, la radiografía
    del doctor esperaba medio minuto por algo que el propio diseño clasifica como degradable
    --y el candado por teléfono se lo sumaba al segundo archivo del mismo número.

    Lo que se afirma: vencido el tope, `procesar_mensaje` vuelve en seguida y el archivo se
    queda sin depositar --hasta el 22/09/2026 se iba al General, y era uno de los tres
    caminos del ruido--. Y la operación NO se cancela: sigue por detrás, para que el hilo
    esté listo para el próximo archivo en vez de quedar huérfano en Telegram a medio crear.
    """
    import asyncio
    import time

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    monkeypatch.setattr(lectura, "TOPE_SEGUNDOS_TEMA", 0.05)

    termino = []

    async def tema_lentisimo(**kw):
        await asyncio.sleep(0.5)
        termino.append(TEMA_DE_ANA)
        return TEMA_DE_ANA

    monkeypatch.setattr(lectura, "asegurar_tema", tema_lentisimo)
    tg = TelegramConTemas()

    async def corrida():
        arranque = time.monotonic()
        resultado = await ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
        tardo = time.monotonic() - arranque
        if resultado.lectura is not None:
            resultado.lectura.cancel()
        # La tarea del tema sigue viva a propósito: se le da tiempo de acabar para
        # comprobar que NO se canceló --si se cancelara a mitad de `crear_tema`, Telegram
        # se quedaría con un tema que nadie guardó.
        pendientes = list(ingesta._temas_en_curso)
        await asyncio.gather(*pendientes, return_exceptions=True)
        return resultado, tardo

    resultado, tardo = asyncio.run(corrida())

    assert resultado.reenviado is False
    assert tg.archivos == [], (
        "vencido el tope, el archivo se fue al General: eso es justo el ruido que se cortó"
    )
    assert tardo < 0.3, f"la entrega del archivo espero al tema: tardo {tardo:.2f} s"
    assert termino == [TEMA_DE_ANA], (
        "la creación del tema se canceló: eso deja un tema a medio crear en Telegram y el "
        "siguiente archivo del mismo número abriría otro"
    )


def test_el_tope_del_tema_cabe_dentro_de_la_entrega():
    """El valor, no solo el mecanismo: con el default de 15 s + 15 s no habría tope."""
    from maxicare_daniela import lectura

    assert lectura.TOPE_SEGUNDOS_TEMA == 5.0


def test_un_archivo_del_paciente_NO_rehace_un_hilo_que_borraron(monkeypatch):
    """La otra mitad de lo que pidió MaxiCare: el gesto del doctor tiene que durar.

    Borrar el tema es decir «este paciente deja de aparecer en el grupo». Si la foto que
    manda diez minutos después le abriera un hilo nuevo, el gesto no serviría de nada y
    encima quedarían dos temas para la misma persona.

    Lo que se comprueba es el ARGUMENTO, no el resultado: la decisión vive dentro de
    `asegurar_tema` --que sabe leer la lápida de la 030-- y lo que a `ingesta` le toca es
    pedirla. Quien la pide con el default es `lectura.rescatar_hilo`, o sea el escalamiento.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    pedidos: list[dict] = []

    async def espiar(**kw):
        pedidos.append(kw)
        return None

    monkeypatch.setattr(lectura, "asegurar_tema", espiar)
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))

    asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=TelegramConTemas(),
            database_url="postgresql://x",
        )
    )

    assert pedidos and pedidos[0]["rehacer_si_lo_borraron"] is False, (
        "la ingesta pidió el hilo con el default: un mensaje del paciente volvería a abrir "
        "el tema que un doctor borró a propósito"
    )


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
        )
    )

    assert resultado.lectura is None
    assert tg.archivos, "el archivo grande tiene que llegar al doctor igual"


def test_el_aviso_al_general_no_invalida_una_entrega_que_ya_ocurrio(monkeypatch):
    """Hallazgo 3 de la ronda de arreglo.

    Cuando el aviso al General se intenta, el archivo YA esta depositado en el tema del
    paciente. Si ese aviso revienta, la fila tiene que seguir diciendo `reenviado=True`
    --lo que paso de verdad-- y el lector tiene que haber arrancado igual: un aviso fallido
    no puede quitarle al doctor ni la entrega ni la lectura.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura
    from maxicare_daniela.canales import ErrorDeCanal

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))

    class TelegramQueFallaElAviso(TelegramConTemas):
        async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
            raise ErrorDeCanal("Telegram no respondio")

    tg = TelegramQueFallaElAviso()

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
    )

    assert resultado.reenviado is True, "el archivo SI llego; el aviso fallido no lo cambia"
    assert tg.archivos == [("radio.jpg", TEMA_DE_ANA)]
    assert resultado.lectura is not None, "el lector tiene que arrancar aunque el aviso falle"
    resultado.lectura.cancel()


# ==========================================================================================
# El group_id que agrupa las trazas del lector (fase 7, Tarea 11)
# ==========================================================================================


def test_ingesta_resuelve_la_conversacion_viva_y_se_la_pasa_al_lector(monkeypatch):
    """Cierra el Importante 1 de la revision del lote B.

    Nadie probaba que `ingesta.procesar_mensaje` resolviera la conversacion viva del
    telefono y se la pasara a `leer_y_repartir` como `group_id`: la unica prueba que
    atravesaba `leer_archivo` de verdad lo llamaba SIN grupo. Borrar el `group_id=grupo` de
    `_leer_con_grupo`, o cambiar la llamada a `_conversacion_viva` por otra cosa, dejaba la
    suite entera en verde.

    Se dobla `ingesta._conversacion_viva` (no `persistencia.conversacion_viva`: la frontera
    de esta prueba es el cable de `procesar_mensaje`, no la consulta SQL) para que devuelva
    un id de conversacion fijo, y se espera la tarea del lector hasta que termine -- si no
    se espera, `_leer_con_grupo` puede no haber corrido todavia cuando se mira `capturado`.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_conversacion_viva", lambda url, telefono: "conv-viva-1")
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))

    capturado: dict = {}

    async def capturar_group_id(*a, **kw):
        capturado["group_id"] = kw.get("group_id")
        return None

    monkeypatch.setattr(lectura, "leer_y_repartir", capturar_group_id)
    tg = TelegramConTemas()

    async def corrida():
        resultado = await ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
        assert resultado.lectura is not None, "el lector no arranco"
        await resultado.lectura
        return resultado

    asyncio.run(corrida())

    assert capturado["group_id"] == "conv-viva-1", (
        "el group_id resuelto no llego al lector: se rompio el cable entre "
        "_conversacion_viva y leer_y_repartir dentro de _leer_con_grupo"
    )


def test_sin_conversacion_viva_el_lector_no_recibe_un_grupo_inventado(monkeypatch):
    """El complemento del anterior: el primer archivo de un paciente nuevo, sin conversacion
    todavia, no debe inventarle un `group_id` a la tarea del lector."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))

    capturado: dict = {}

    async def capturar_group_id(*a, **kw):
        capturado["group_id"] = kw.get("group_id")
        return None

    monkeypatch.setattr(lectura, "leer_y_repartir", capturar_group_id)
    tg = TelegramConTemas()

    async def corrida():
        resultado = await ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
        await resultado.lectura
        return resultado

    asyncio.run(corrida())

    assert capturado["group_id"] is None


# ==========================================================================================
# El relevo (6C): la única excepción al silencio del hilo
# ==========================================================================================
#
# Fuera del relevo, el tema de un paciente es su expediente y no suena NUNCA -- es el no
# negociable 14. Durante un relevo deja de ser un expediente: es una conversación en vivo, y
# un doctor que no oye la respuesta del paciente es un doctor hablando solo.


def _correr_archivo(tg, **cambios):
    import asyncio

    from maxicare_daniela import ingesta

    return asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(**cambios),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
        )
    )


def test_durante_un_relevo_el_texto_del_paciente_suena_en_su_hilo(monkeypatch):
    from maxicare_daniela import ingesta

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: TEMA_DE_ANA)
    monkeypatch.setattr(ingesta, "_en_relevo", lambda url, tel: True)
    tg = TelegramConTemas()

    _correr_texto(tg, monkeypatch)

    assert [(destino, silencio) for _, destino, silencio in tg.mensajes_con_silencio] == [
        (TEMA_DE_ANA, False)
    ]


def test_sin_relevo_el_texto_sigue_entrando_mudo(monkeypatch):
    """La otra mitad: que abrir esta puerta no haya reabierto el ruido que cerró la 14."""
    from maxicare_daniela import ingesta

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: TEMA_DE_ANA)
    tg = TelegramConTemas()

    _correr_texto(tg, monkeypatch)

    assert tg.mensajes_con_silencio[0][2] is True


def test_sin_tema_no_se_pregunta_siquiera_por_el_relevo(monkeypatch):
    """Sin hilo no hay dónde sonar, así que la consulta sería una ida a Neon para no usar el
    resultado -- delante de la entrega de un mensaje de un paciente."""
    from maxicare_daniela import ingesta

    _sin_base(monkeypatch)
    preguntas: list[str] = []
    monkeypatch.setattr(
        ingesta, "_en_relevo", lambda url, tel: preguntas.append(tel) or False
    )
    tg = TelegramConTemas()

    _correr_texto(tg, monkeypatch)  # el default de `_sin_base` es «sin tema»

    assert preguntas == []


def test_durante_un_relevo_el_archivo_tambien_suena(monkeypatch):
    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    monkeypatch.setattr(ingesta, "_en_relevo", lambda url, tel: True)
    tg = TelegramConTemas()

    _correr_archivo(tg)

    assert tg.archivos_con_silencio[0][2] is False


def test_durante_un_relevo_el_general_no_recibe_el_aviso_de_archivos(monkeypatch):
    """El doctor YA tiene el archivo sonándole en el hilo donde está conversando. El aviso al
    General sería el mismo timbrazo por segunda vez, en el sitio donde menos falta hace."""
    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    monkeypatch.setattr(ingesta, "_en_relevo", lambda url, tel: True)
    tg = TelegramConTemas()

    _correr_archivo(tg)

    assert [m for m in tg.mensajes if m[1] == TEMA_GENERAL] == []


# ==========================================================================================
# Los botones de la plantilla de recordatorio
# ==========================================================================================


def test_el_texto_de_un_quick_reply_de_plantilla_no_se_pierde():
    """Pulsar «Confirmar» tiene que llegar como texto, no como un mensaje mudo.

    La plantilla `recordatorio_cita` lleva dos quick replies, y la respuesta a uno de ellos NO
    viene como `text`: Meta manda `type: "button"` con el rótulo dentro de `button.text`. Sin
    esta línea, el paciente que pulsa el botón llega con `texto = None` y `atencion` le entrega
    al modelo «[El paciente envió algo de tipo «button». No trae texto.]» -- que es lo que pasó
    en la primera prueba real, el 15/09/2026: Daniela contestó con su saludo de primer contacto
    a alguien que acababa de pedir mover su cita.

    Es decir: sin esto los dos botones que Meta aprobó no sirven para nada.
    """
    payload = _sobre({
        "from": "573001234567",
        "id": "wamid.boton",
        "type": "button",
        "button": {"payload": "Necesito cambiarla", "text": "Necesito cambiarla"},
    })
    [m] = extraer_mensajes(payload)
    assert m.tipo == "button"
    assert m.texto == "Necesito cambiarla"


def test_un_quick_reply_sin_rotulo_cae_al_payload():
    """`text` es lo que el paciente vio; `payload` lo que la plantilla definió.

    En un quick reply los dos suelen coincidir. Se prefiere `text` --es literalmente lo que el
    paciente leyó antes de pulsar-- y se cae a `payload` porque un mensaje mudo es justo el
    fallo que esta pareja de pruebas existe para evitar.
    """
    payload = _sobre({
        "from": "573001234567",
        "id": "wamid.boton.sin.texto",
        "type": "button",
        "button": {"payload": "Confirmar"},
    })
    [m] = extraer_mensajes(payload)
    assert m.texto == "Confirmar"


# ==========================================================================================
# El hilo que alguien borró a mano
# ==========================================================================================
#
# `relevo.barrer` recupera el hilo muerto de un paciente EN RELEVO: sondea con
# `estado_del_tema`, cierra con motivo `tema_perdido` y llama a `persistencia.olvidar_tema`.
# Pero `barrer` itera sobre `tomada_por IS NOT NULL`, así que un hilo borrado fuera de un
# relevo no lo mira nadie: Telegram no emite ningún evento al borrar un tema.
#
# La fila de `temas_telegram` se quedaba apuntando a un `topic_id` muerto y cada mensaje de
# esa persona se estrellaba contra «message thread not found». Para siempre, y en silencio
# --exactamente lo que el docstring de `olvidar_tema` predice--. Medido contra la API el
# 16/09/2026: un tema inexistente NO se degrada al General, se rechaza.


def test_telegram_distingue_el_hilo_muerto_de_cualquier_otro_rechazo():
    """Sin esta distinción, el arreglo olvidaría el hilo ante un error de permisos o un
    límite de tasa, y cada tropiezo pasajero le borraría el expediente a un paciente."""
    from maxicare_daniela.canales import ErrorDeCanal, HiloInvalido, es_hilo_invalido

    assert es_hilo_invalido("Bad Request: message thread not found")
    assert es_hilo_invalido("Bad Request: TOPIC_ID_INVALID")
    assert not es_hilo_invalido("Bad Request: not enough rights")
    assert not es_hilo_invalido("Too Many Requests: retry after 30")
    # Tiene que poder atraparse como `ErrorDeCanal`: todo lo que ya lo captura sigue igual.
    assert issubclass(HiloInvalido, ErrorDeCanal)


@pytest.mark.parametrize(
    "descripcion, esperado",
    [
        ("Bad Request: TOPIC_ID_INVALID", "HiloInvalido"),
        ("Bad Request: not enough rights", "ErrorDeCanal"),
    ],
    ids=["hilo_borrado", "fallo_pasajero"],
)
def test_reabrir_un_tema_borrado_lanza_HiloInvalido_y_no_un_error_cualquiera(
    descripcion, esperado
):
    """La línea de la que dependía que el doctor pudiera volver a entrar.

    Medido en producción el 17/09/2026 a las 08:32:35. El doctor había borrado el hilo; al
    pulsar «Hablar yo con el paciente», `reopenForumTopic` respondió `TOPIC_ID_INVALID`, esto
    subió como `ErrorDeCanal` genérico y `relevo.activar` **deshizo el relevo entero**. El
    botón aparecía y no servía para nada.

    Las dos mitades importan, y por eso son dos casos: un hilo borrado hay que rehacerlo, y
    un error de permisos o un límite de tasa NO -- tratarlos igual le abriría un hilo nuevo a
    un paciente cuyo expediente está perfectamente vivo, cada vez que Telegram tosa.
    """
    import asyncio
    import httpx

    from maxicare_daniela.canales import ErrorDeCanal, HiloInvalido, Telegram

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": descripcion})

    transporte = httpx.MockTransport(responder)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        tg = Telegram("token-falso", "-1001234567890")
        with pytest.raises(ErrorDeCanal) as caido:
            asyncio.run(tg.reabrir_tema(123))
    finally:
        httpx.AsyncClient = original

    es_hilo_muerto = isinstance(caido.value, HiloInvalido)
    assert es_hilo_muerto == (esperado == "HiloInvalido"), (
        f"{descripcion!r} tenía que llegar como {esperado}"
    )


def test_reabrir_un_tema_QUE_YA_ESTABA_ABIERTO_no_es_un_fallo():
    """`TOPIC_NOT_MODIFIED` es un sí, no un no: el tema existe y ya estaba abierto.

    Lo que `reabrir_tema` promete es dejar el tema abierto, y ese es justo el estado al que
    se llega. Tratarlo como error vuelve a abrir el agujero del 17/09/2026 por la otra
    puerta: `relevo._tema_abierto_para` lo propaga, `activar` lo lee como «no hay hilo» y
    **deshace el relevo entero** -- el mismo «el botón aparece y no sirve», con el hilo del
    paciente intacto delante.

    No es un caso de laboratorio: un tema está abierto cuando un doctor lo reabrió a mano,
    cuando `lectura.asegurar_tema` no consiguió cerrarlo al crearlo (`quedo_abierto`), o
    cuando el cierre del relevo anterior falló al cerrar el tema. `estado_del_tema` ya lo
    lee así --devuelve `"abierto"`-- y las dos sondas usan el MISMO `reopenForumTopic`: que
    una de ellas lo llamara fallo era la incoherencia.
    """
    import asyncio
    import httpx

    from maxicare_daniela.canales import Telegram

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "description": "Bad Request: TOPIC_NOT_MODIFIED"}
        )

    transporte = httpx.MockTransport(responder)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        tg = Telegram("token-falso", "-1001234567890")
        # No lanza: el postestado que promete la función --tema abierto-- ya se cumple.
        asyncio.run(tg.reabrir_tema(123))
    finally:
        httpx.AsyncClient = original


@pytest.mark.parametrize(
    "respuesta, esperado",
    [
        ({"ok": False, "description": "Bad Request: message is not modified"}, True),
        ({"ok": False, "description": "Bad Request: message to edit not found"}, False),
        ({"ok": True, "result": {"message_id": 855}}, True),
        ({"ok": False, "description": "Too Many Requests: retry after 30"}, None),
    ],
    ids=["sigue_puesto", "el_doctor_lo_borro", "existe_con_otro_teclado", "no_se_pudo_saber"],
)
def test_preguntarle_a_telegram_si_el_aviso_del_General_sigue_ahi(respuesta, esperado):
    """La tercera cosa que Telegram no avisa: **borrar un mensaje tampoco emite evento.**

    Igual que borrar un tema. Y el sistema dependía de que el aviso siguiera puesto: la
    guarda `escalamiento_vivo_con_motivo` calla todo escalamiento del mismo motivo mientras
    haya uno «delante del doctor sin responder», y mide eso por `telegram_message_id IS NOT
    NULL`. En cuanto el doctor borra ese mensaje, la premisa es falsa y el silencio dura las
    24 h de la conversación: ni aviso, ni botón, ni hilo.

    La sonda es `editMessageReplyMarkup` con el MISMO teclado, y se eligió por lo que NO
    hace: si el mensaje está igual, Telegram responde «not modified» y **no toca nada**. No
    deja mensaje de servicio, no reordena el General, no notifica a nadie.

    Los cuatro casos, que son los cuatro que manda Telegram:

    - «not modified» -> sigue puesto, con su botón. Se calla, que es lo correcto.
    - «message to edit not found» -> lo borraron. NO se calla.
    - `ok: true` -> existía con otro teclado, y se le acaba de poner el que toca. Sigue
      puesto: no se calla por eso, pero el aviso existe.
    - cualquier otra cosa -> `None`, no se sabe. Quien llama decide, y decide avisar:
      callar una alerta clínica porque Telegram tuvo un mal minuto es el error caro.
    """
    import asyncio
    import httpx

    from maxicare_daniela.canales import Telegram

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=respuesta)

    transporte = httpx.MockTransport(responder)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        tg = Telegram("token-falso", "-1001234567890")
        visto = asyncio.run(tg.aviso_sigue_puesto(855, {"inline_keyboard": [[]]}))
    finally:
        httpx.AsyncClient = original

    assert visto is esperado, f"{respuesta} tenía que leerse como {esperado}"


def test_un_texto_contra_un_hilo_borrado_olvida_el_hilo_y_no_cuenta_como_fallo(monkeypatch):
    """El texto no se archiva --un texto nunca abre hilo, no negociable 14-- pero el hilo
    muerto se olvida, así que el primer archivo que llegue después abre uno nuevo.

    Y NO se marca como fallo: el estado al que se llega es el del número sin tema, que es
    normal y ya tiene su registro (`reenviado_en` puesta, `telegram_message_id` nulo).
    """
    import asyncio

    from maxicare_daniela import ingesta
    from maxicare_daniela.canales import HiloInvalido

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: 777)

    olvidados: list[str] = []
    monkeypatch.setattr(ingesta, "_olvidar_tema", lambda url, tel: olvidados.append(tel))

    fallos: list[str] = []
    monkeypatch.setattr(ingesta, "_marcar_fallo", lambda url, w, e: fallos.append(e))

    class TelegramConHiloMuerto(TelegramConTemas):
        async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False):
            if tema_id == 777:
                raise HiloInvalido("Telegram rechazó el mensaje: message thread not found")
            return await super().enviar_mensaje(
                texto, tema_id=tema_id, teclado=teclado, silencioso=silencioso
            )

    tg = TelegramConHiloMuerto()
    m = MensajeEntrante(
        wamid="wamid-texto-hilo-muerto",
        telefono="573001112233",
        nombre_perfil="Ana Perez",
        tipo="text",
        texto="Hola, sigo esperando",
    )
    res = asyncio.run(
        ingesta.procesar_mensaje(
            m, whatsapp=WhatsAppConArchivo(), telegram=tg,
            database_url="postgresql://x",
        )
    )

    assert olvidados == ["573001112233"], "el hilo muerto tiene que olvidarse"
    assert fallos == [], "un hilo borrado no es un fallo de entrega del mensaje"
    assert res.reenviado is False
    assert tg.mensajes == [], "el texto NO se reencamina a ninguna parte"


def test_un_archivo_contra_un_hilo_borrado_le_pone_lapida_y_NO_cae_al_general(monkeypatch):
    """Ahora el archivo se trata igual que un texto, y eso es el arreglo del 22/09/2026.

    Esta prueba decía lo contrario --se llamaba `..._y_cae_al_general` y exigía que el
    archivo acabara ahí SONANDO, «porque ya no hay hilo donde reposar»--. Era el tercero de
    los tres caminos por los que el mensaje de un paciente terminaba en el escritorio común
    de los doctores, y el más difícil de ver: hacía falta que alguien hubiera borrado el
    tema.

    Lo que se conserva: la fila se marca perdida (`_olvidar_tema`), porque si no, cada
    archivo suyo se estrellaría contra el mismo hilo muerto para siempre. Lo que cambia: no
    hay segundo intento contra el General, y `reenviado` dice la verdad --`False`--.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura
    from maxicare_daniela.canales import HiloInvalido

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(777))
    monkeypatch.setattr(ingesta.lectura_mod, "vale_la_pena_leer", lambda tipo, tam: False)

    olvidados: list[str] = []
    monkeypatch.setattr(ingesta, "_olvidar_tema", lambda url, tel: olvidados.append(tel))

    fallos: list[str] = []
    monkeypatch.setattr(ingesta, "_marcar_fallo", lambda url, w, e: fallos.append(e))

    class TelegramConHiloMuerto(TelegramConTemas):
        async def enviar_archivo(
            self, archivo, *, tipo_whatsapp, pie, tema_id=None, silencioso=False
        ):
            if tema_id == 777:
                raise HiloInvalido("Telegram rechazó el archivo: message thread not found")
            return await super().enviar_archivo(
                archivo, tipo_whatsapp=tipo_whatsapp, pie=pie,
                tema_id=tema_id, silencioso=silencioso,
            )

    tg = TelegramConHiloMuerto()
    res = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(), whatsapp=WhatsAppConArchivo(), telegram=tg,
            database_url="postgresql://x",
        )
    )

    assert olvidados == ["573001112233"], "el hilo muerto tiene que marcarse perdido"
    assert fallos == [], "un hilo borrado no es un fallo de entrega: se decidió no reenviar"
    assert res.reenviado is False
    assert tg.archivos == [], "el archivo acabó en el General: es el ruido que se cortó"
    assert tg.mensajes == [], "y tampoco un aviso"


def test_otro_rechazo_de_telegram_sigue_siendo_un_fallo_y_no_borra_el_hilo(monkeypatch):
    """La contraparte del primero: un error que NO es de hilo inválido no puede costarle el
    expediente a un paciente."""
    import asyncio

    from maxicare_daniela import ingesta
    from maxicare_daniela.canales import ErrorDeCanal

    _sin_base(monkeypatch)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: 777)

    olvidados: list[str] = []
    monkeypatch.setattr(ingesta, "_olvidar_tema", lambda url, tel: olvidados.append(tel))
    fallos: list[str] = []
    monkeypatch.setattr(ingesta, "_marcar_fallo", lambda url, w, e: fallos.append(e))

    class TelegramCaido(TelegramConTemas):
        async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False):
            raise ErrorDeCanal("Telegram rechazó el mensaje: not enough rights")

    m = MensajeEntrante(
        wamid="wamid-texto-sin-derechos",
        telefono="573001112233",
        nombre_perfil="Ana Perez",
        tipo="text",
        texto="Hola",
    )
    res = asyncio.run(
        ingesta.procesar_mensaje(
            m, whatsapp=WhatsAppConArchivo(), telegram=TelegramCaido(),
            database_url="postgresql://x",
        )
    )

    assert olvidados == [], "un error de permisos NO puede borrar el hilo del paciente"
    assert len(fallos) == 1 and "not enough rights" in fallos[0]
    assert res.reenviado is False


# ==========================================================================================
# Las notas de voz
# ==========================================================================================
#
# El carril del transcriptor es el mismo del lector y hereda su garantia: **el audio le llega
# al doctor pase lo que pase**. Lo que estas pruebas fijan es que siga siendo cierto ahora que
# hay una segunda tarea colgando del mismo sitio.


class WhatsAppConAudio:
    def __init__(self, *, tamano: int = 20_000) -> None:
        self.tamano = tamano

    async def descargar_media(self, media_id, *, nombre_original=None):
        from maxicare_daniela.canales import ArchivoDescargado

        # `.oga` y no `.ogg`: es lo que produce `canales._nombre_sugerido` de verdad, y que
        # este doble mienta ahi seria taparle los ojos a la prueba justo en la trampa.
        return ArchivoDescargado(
            contenido=b"x" * self.tamano, mime="audio/ogg", nombre="12345.oga"
        )


def _nota_de_voz(**cambios):
    from maxicare_daniela.ingesta import MensajeEntrante

    campos = dict(
        wamid="wamid-voz-1",
        telefono="573001112233",
        nombre_perfil="Ana Perez",
        tipo="audio",
        media_id="media-voz",
        mime="audio/ogg; codecs=opus",
    )
    campos.update(cambios)
    return MensajeEntrante(**campos)


def test_el_transcriptor_no_retrasa_la_entrega_del_audio(monkeypatch):
    """Espejo de `test_el_lector_no_retrasa_la_entrega_del_archivo`, y hace falta por
    separado: son dos `create_task` distintos y el segundo se puede encadenar mal sin que el
    primero se entere."""
    import asyncio
    import time

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))

    async def transcriptor_lento(*a, **kw):
        await asyncio.sleep(1.0)
        return None

    monkeypatch.setattr(transcripcion, "transcribir_y_repartir", transcriptor_lento)
    tg = TelegramConTemas()

    async def corrida():
        arranque = time.monotonic()
        resultado = await ingesta.procesar_mensaje(
            _nota_de_voz(),
            whatsapp=WhatsAppConAudio(),
            telegram=tg,
            database_url="postgresql://x",
        )
        tardo = time.monotonic() - arranque
        if resultado.transcripcion is not None:
            resultado.transcripcion.cancel()
        return resultado, tardo

    resultado, tardo = asyncio.run(corrida())

    assert resultado.reenviado is True
    assert tg.archivos, "el audio no llego a Telegram"
    assert tardo < 0.3, f"la entrega del audio espero al transcriptor: tardo {tardo:.2f} s"
    assert resultado.transcripcion is not None, "no se arranco el transcriptor"


def test_con_el_interruptor_apagado_el_audio_LLEGA_IGUAL_al_doctor(monkeypatch):
    """`MAXICARE_TRANSCRIBIR_AUDIO=0` y la cuota diaria entran los dos por este parametro.

    Lo que se apaga es entender el audio, nunca entregarlo: esa es la garantia de la fase 2 y
    no la toca ningun freno de este perimetro.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))

    llamadas = []

    async def no_deberia_correr(*a, **kw):
        llamadas.append(True)
        return None

    monkeypatch.setattr(transcripcion, "transcribir_y_repartir", no_deberia_correr)
    tg = TelegramConTemas()

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _nota_de_voz(),
            whatsapp=WhatsAppConAudio(),
            telegram=tg,
            database_url="postgresql://x",
            transcribir=False,
        )
    )

    assert resultado.reenviado is True, "el audio tiene que llegar al doctor igual"
    assert tg.archivos, "el audio no llego a Telegram"
    assert resultado.transcripcion is None
    assert llamadas == []


def test_un_audio_enorme_se_entrega_pero_no_se_transcribe(monkeypatch):
    """El tope de bytes. Mismo criterio que el del lector: el archivo sigue su camino y lo
    unico que se salta es la llamada."""
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(
        transcripcion, "transcribir_y_repartir", _devuelve_async("no deberia salir")
    )
    tg = TelegramConTemas()

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _nota_de_voz(),
            whatsapp=WhatsAppConAudio(
                tamano=transcripcion.TOPE_BYTES_TRANSCRIPCION + 1
            ),
            telegram=tg,
            database_url="postgresql://x",
        )
    )

    assert resultado.reenviado is True
    assert resultado.transcripcion is None


def test_una_FOTO_no_arranca_el_transcriptor(monkeypatch):
    """Los dos carriles son disjuntos, y de eso depende que `atencion` pueda esperar los dos
    plazos uno detras de otro sin que cueste nada."""
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    monkeypatch.setattr(
        transcripcion, "transcribir_y_repartir", _devuelve_async("no deberia salir")
    )

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=TelegramConTemas(),
            database_url="postgresql://x",
        )
    )

    assert resultado.lectura is not None
    assert resultado.transcripcion is None


# ==========================================================================================
# Una nota de voz entendida es un mensaje mas, no un archivo que alguien tenga que abrir
# ==========================================================================================
#
# MaxiCare, 22/09/2026: mando una nota de voz, Daniela la entendio y la contesto bien, y aun
# asi el General timbro con el boton «Hablar yo con el paciente» debajo. No era un
# escalamiento -`escalamientos` no tenia ni una fila- sino el aviso «mando archivos» de la
# fase 2, que salta con CUALQUIER archivo.
#
# Ese aviso existe para que un humano ABRA el archivo, porque una radiografia hay que
# mirarla. Una nota de voz que Daniela entendio y contesto no le pide nada a nadie, y la
# regla del proyecto es que al General solo va lo que le pide algo al doctor.


def _mensajes_al_general(tg, tema_general: int = TEMA_GENERAL) -> list[str]:
    return [t for t, tema in tg.mensajes if tema == tema_general]


def test_una_nota_de_voz_ENTENDIDA_no_timbra_en_el_General(monkeypatch):
    """La prueba de la que va este cambio."""
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(
        transcripcion, "transcribir_y_repartir", _devuelve_async("cuanto vale la limpieza?")
    )
    tg = TelegramConTemas()

    async def corrida():
        r = await ingesta.procesar_mensaje(
            _nota_de_voz(),
            whatsapp=WhatsAppConAudio(),
            telegram=tg,
            database_url="postgresql://x",
        )
        # La decision del aviso vive DENTRO de la tarea, asi que hay que dejarla terminar.
        if r.transcripcion is not None:
            await r.transcripcion
        return r

    resultado = asyncio.run(corrida())

    assert resultado.reenviado is True, "el audio tiene que llegar al doctor igual"
    assert tg.archivos, "el audio no llego a Telegram"
    assert _mensajes_al_general(tg) == [], (
        "timbro en el General por una nota de voz que Daniela ya contesto"
    )


def test_lo_que_se_entendio_se_GUARDA_con_el_wamid_de_su_audio(monkeypatch):
    """La migracion 027. Este es el unico punto del proyecto donde coexisten las dos cosas.

    Mas adelante la transcripcion viaja suelta hasta el turno --por `Resultado.transcripcion`,
    luego al bufer, luego a la entrada del modelo-- y ya nadie sabe de que mensaje salio. Sin
    guardarla aqui, el sistema entiende la nota de voz, la contesta y despues la olvida: en el
    panel queda «(nota de voz)» sobre algo que si se entendio.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(
        transcripcion, "transcribir_y_repartir", _devuelve_async("cuanto vale la limpieza?")
    )
    guardadas: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ingesta,
        "_guardar_transcripcion",
        lambda url, wamid, texto: guardadas.append((wamid, texto)),
    )

    async def corrida():
        r = await ingesta.procesar_mensaje(
            _nota_de_voz(),
            whatsapp=WhatsAppConAudio(),
            telegram=TelegramConTemas(),
            database_url="postgresql://x",
        )
        if r.transcripcion is not None:
            await r.transcripcion
        return r

    asyncio.run(corrida())

    assert guardadas == [(_nota_de_voz().wamid, "cuanto vale la limpieza?")], (
        f"no se guardo la transcripcion con su wamid: {guardadas}"
    )


def test_un_audio_que_NO_se_entendio_no_guarda_una_fila_vacia(monkeypatch):
    """Sin texto no hay nada que guardar, y escribir la cadena vacia seria peor que no
    escribir: `panel.hilo` la leeria como «se transcribio» y pintaria una burbuja en blanco
    marcada como transcripcion, que es afirmar que el paciente dijo algo y no decir el que."""
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(transcripcion, "transcribir_y_repartir", _devuelve_async(None))
    guardadas: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ingesta,
        "_guardar_transcripcion",
        lambda url, wamid, texto: guardadas.append((wamid, texto)),
    )

    async def corrida():
        r = await ingesta.procesar_mensaje(
            _nota_de_voz(),
            whatsapp=WhatsAppConAudio(),
            telegram=TelegramConTemas(),
            database_url="postgresql://x",
        )
        if r.transcripcion is not None:
            await r.transcripcion
        return r

    asyncio.run(corrida())

    assert guardadas == [], f"se guardo algo sin haber entendido nada: {guardadas}"


def test_NADA_de_lo_que_manda_un_paciente_timbra_en_el_General(monkeypatch):
    """Las tres ramas que timbraban, ahora mudas. Es el contrato entero en una prueba.

    Hasta el 22/09/2026 esto eran tres pruebas que afirmaban lo contrario:

        `test_una_nota_de_voz_que_NO_se_entendio_SI_timbra_y_dice_por_que`
        `test_sin_transcriptor_el_audio_timbra_COMO_SIEMPRE`
        `test_una_FOTO_sigue_timbrando_igual`

    Las tres eran correctas bajo la regla vieja --«al General va lo que le pide algo al
    doctor», y un archivo que hay que mirar u oír se lo pide-- y MaxiCare cambió la regla:
    **al General solo van las alertas de escalamiento**. Un doctor que cierra el hilo de un
    paciente está diciendo que deja de querer verlo en el grupo, y el timbrazo de la tanda
    era el único que le pasaba por encima.

    Lo que NO se perdió, y por eso las tres ramas siguen aquí: el archivo se deposita en el
    expediente del paciente en las tres, y `reenviado` lo dice.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura, transcripcion

    casos = {
        "una nota de voz que no se entendió": (_nota_de_voz(), WhatsAppConAudio(), True),
        "un audio sin transcriptor": (_nota_de_voz(), WhatsAppConAudio(), False),
        "una radiografía": (_mensaje_con_foto(), WhatsAppConArchivo(), True),
    }
    for rotulo, (mensaje, wa, transcribir) in casos.items():
        _sin_base(monkeypatch)
        monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
        monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
        monkeypatch.setattr(transcripcion, "transcribir_y_repartir", _devuelve_async(None))
        tg = TelegramConTemas()

        async def corrida():
            r = await ingesta.procesar_mensaje(
                mensaje,
                whatsapp=wa,
                telegram=tg,
                database_url="postgresql://x",
                transcribir=transcribir,
            )
            if r.transcripcion is not None:
                await r.transcripcion
            return r

        resultado = asyncio.run(corrida())

        assert _mensajes_al_general(tg) == [], f"{rotulo} timbró en el General"
        assert resultado.reenviado is True, f"{rotulo} no llegó a su expediente"

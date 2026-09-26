"""El tope de descarga: rechazar un archivo SIN haberlo bajado.

Offline. `httpx.AsyncClient` va sustituido por uno con `MockTransport`, igual que en
`test_ingesta.py`: aquí no sale ni una petición a la Graph API.

------------------------------------------------------------------------------------------
Por qué el sitio del tope es todo el punto
------------------------------------------------------------------------------------------

`descargar_media` son DOS llamadas: la primera canjea el `media_id` por una URL temporal, la
segunda baja los bytes. Meta ya declara `file_size` en la metadata de la primera, así que el
tope va entre las dos y no cuesta nada -- un archivo enorme se rechaza sin haber transferido
un solo byte.

Un tope puesto DESPUÉS de bajar el archivo no protegería de nada, y esa es la parte que una
prueba de «lanza `ErrorDeCanal`» no distingue: la excepción sale igual en los dos casos. Por
eso lo que se cuenta aquí son las PETICIONES, no las excepciones.

Lo que había antes: el archivo se cargaba entero en memoria (`archivo.content`) y después se
re-serializaba en el multipart hacia Telegram --dos copias simultáneas por archivo, más una
tercera del base64 si además iba al modelo-- mientras `runtime` encola cada mensaje sin
límite de concurrencia, en un contenedor de un solo worker. Era el vector de agotamiento más
barato del sistema: no hacía falta vulnerar nada, solo mandar archivos grandes a la vez.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from maxicare_daniela.canales import BASE_GRAPH, ErrorDeCanal, Telegram, WhatsApp

MEDIA_ID = "media-de-prueba"
URL_TEMPORAL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/algo"
UN_MEGA = 1024 * 1024


def _cliente_con(transporte: httpx.MockTransport):
    """Un `AsyncClient` que habla con el transporte de mentira y con nadie más.

    Se hereda del original en vez de imitarlo para que el resto del comportamiento de httpx
    --timeouts, cabeceras, redirecciones-- siga siendo el de verdad: lo único sustituido es
    por dónde salen los bytes. Mismo patrón que `test_ingesta.py`.
    """
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    return ClienteFalso


class Graph:
    """La Graph API de mentira. Anota cada URL pedida, en orden.

    El orden es el dato: `[metadata]` significa que se rechazó antes de bajar nada, y
    `[metadata, descarga]` que los bytes ya viajaron. Son los dos mundos que esta prueba
    existe para distinguir.
    """

    def __init__(self, *, file_size: int | None, bytes_: bytes = b"PNG-de-mentira") -> None:
        self.file_size = file_size
        self.bytes = bytes_
        self.pedidas: list[str] = []

    def instalar(self, monkeypatch) -> Graph:
        monkeypatch.setattr(
            httpx, "AsyncClient", _cliente_con(httpx.MockTransport(self._responder))
        )
        return self

    def _responder(self, peticion: httpx.Request) -> httpx.Response:
        url = str(peticion.url)
        self.pedidas.append(url)
        if url.startswith(f"{BASE_GRAPH}/{MEDIA_ID}"):
            cuerpo: dict = {"url": URL_TEMPORAL, "mime_type": "image/jpeg"}
            if self.file_size is not None:
                cuerpo["file_size"] = self.file_size
            return httpx.Response(200, json=cuerpo)
        return httpx.Response(
            200, content=self.bytes, headers={"content-type": "image/jpeg"}
        )

    @property
    def descargo_los_bytes(self) -> bool:
        return any(u.startswith(URL_TEMPORAL) for u in self.pedidas)


def descargar(tope_mb: int = 55):
    return asyncio.run(
        WhatsApp("token", "123", tope_descarga_mb=tope_mb).descargar_media(MEDIA_ID)
    )


# ==========================================================================================
# El tope
# ==========================================================================================


def test_un_archivo_por_encima_del_tope_se_rechaza_SIN_descargarlo(monkeypatch):
    """La prueba entera del perímetro de descarga, y lo que la hace valer es la segunda
    aserción.

    Que lance `ErrorDeCanal` no demuestra nada por sí solo: un tope comprobado DESPUÉS de
    bajar los bytes lanzaría exactamente la misma excepción, con la RAM ya ocupada y el tramo
    de red ya pagado. Lo que se comprueba es que la segunda petición --la que trae el
    archivo-- nunca llegó a hacerse.
    """
    graph = Graph(file_size=80 * UN_MEGA).instalar(monkeypatch)

    with pytest.raises(ErrorDeCanal) as fallo:
        descargar(tope_mb=55)

    assert graph.descargo_los_bytes is False, "se bajó el archivo y DESPUÉS se rechazó"
    assert len(graph.pedidas) == 1, f"se hicieron {len(graph.pedidas)} peticiones, no una"
    # El mensaje lleva los dos números: sin ellos, quien lea el log de un archivo rechazado no
    # sabe si el tope está mal puesto o si el archivo era de verdad enorme.
    assert "83886080" in str(fallo.value) and str(55 * UN_MEGA) in str(fallo.value)


def test_un_archivo_normal_sigue_bajando_entero(monkeypatch):
    """El caso que NO debe disparar, que es la mitad que le falta a todo freno.

    Una radiografía son unos megas y es exactamente lo que la clínica no puede perder: un tope
    que muerda a un archivo legítimo es un tope que alguien acaba quitando, y entonces deja de
    proteger también del caso real. Por eso 55 MB y no menos -- Telegram ya rechaza por encima
    de 50, así que nada que hoy funcione cruza este número.
    """
    graph = Graph(file_size=3 * UN_MEGA, bytes_=b"x" * 64).instalar(monkeypatch)

    archivo = descargar(tope_mb=55)

    assert graph.descargo_los_bytes is True
    assert archivo.contenido == b"x" * 64
    assert archivo.mime == "image/jpeg"


def test_justo_en_el_tope_todavia_pasa(monkeypatch):
    """La comparación es `>` y no `>=`: un archivo que pese exactamente el tope entra.

    El borde se fija aquí porque el número sale del `.env` y se lee como «hasta 55 MB», no
    como «55 MB ya no». Un freno cuyo límite significa una cosa en el `.env` y otra en el
    código es un freno que se calibra a ciegas.
    """
    graph = Graph(file_size=55 * UN_MEGA).instalar(monkeypatch)

    descargar(tope_mb=55)

    assert graph.descargo_los_bytes is True


def test_el_tope_lo_pone_QUIEN_CONSTRUYE_el_cliente(monkeypatch):
    """`runtime` le pasa `config.tope_descarga_mb`, y ese es el sentido de que sea un
    parámetro: poder bajarlo en caliente el día que alguien esté inundando el número.

    `canales.py` no importa nada del paquete a propósito --habla con dos APIs y no sabe nada
    del resto del sistema-- así que el valor efectivo tiene que entrar por el constructor. Si
    el parámetro no llegara a la comprobación, el `.env` no movería nada y no habría un solo
    error en ningún log.
    """
    graph = Graph(file_size=10 * UN_MEGA).instalar(monkeypatch)

    with pytest.raises(ErrorDeCanal):
        descargar(tope_mb=5)

    assert graph.descargo_los_bytes is False


def test_el_limite_conocido_sin_file_size_no_hay_tope_que_valga(monkeypatch):
    """El hueco aceptado a conciencia, escrito para que sea una decisión y no una sorpresa.

    El tope cuelga de un dato que declara Meta. Si la metadata no trae `file_size` --no se ha
    visto, pero la Graph API no promete nada-- el archivo se baja entero: `if tamano and ...`
    deja pasar tanto el campo ausente como un 0.

    Es el lado correcto del error. La alternativa es rechazar todo archivo cuyo tamaño Meta no
    declare, y eso convierte un cambio de formato de un tercero en «la clínica dejó de recibir
    radiografías», que es justo lo que este proyecto no puede permitirse. Lo que queda cubierto
    mientras tanto es la RAM: el semáforo de lectores concurrentes y la cuota de archivos por
    día siguen puestos, y ninguno de los dos depende de este campo.
    """
    graph = Graph(file_size=None).instalar(monkeypatch)

    archivo = descargar(tope_mb=1)

    assert graph.descargo_los_bytes is True
    assert archivo.contenido == b"PNG-de-mentira"


def test_un_tope_absurdo_no_deja_el_sistema_sin_descargas(monkeypatch):
    """`max(tope_descarga_mb, 1)`: un 0 en el `.env` --o un campo vacío que se lea como 0--
    significaría rechazar TODOS los archivos, incluidos los de un kilobyte.

    Un despliegue que se equivoque escribiendo una variable no puede dejar al doctor sin
    recibir ni una radiografía; el suelo de 1 MB convierte la equivocación en un tope
    incómodo en vez de en un apagón.
    """
    graph = Graph(file_size=200 * 1024).instalar(monkeypatch)

    descargar(tope_mb=0)

    assert graph.descargo_los_bytes is True


def test_una_metadata_sin_url_no_llega_a_descargar_nada(monkeypatch):
    """El otro camino que corta antes de la segunda petición, y conviene que siga cortando:
    sin `url` no hay nada que pedir, y dejarlo seguir haría un GET contra `None`."""
    pedidas: list[str] = []

    def sin_url(peticion: httpx.Request) -> httpx.Response:
        pedidas.append(str(peticion.url))
        return httpx.Response(200, json={"mime_type": "image/jpeg"})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(sin_url)))

    with pytest.raises(ErrorDeCanal) as fallo:
        descargar()

    assert pedidas == [f"{BASE_GRAPH}/{MEDIA_ID}"]
    assert "url" in str(fallo.value)


def test_la_descarga_va_con_el_bearer_puesto(monkeypatch):
    """La URL temporal sola no autoriza nada: sin la cabecera, Meta devuelve un 401 y el
    archivo se pierde para siempre --esa URL caduca, y el paciente tendría que reenviarlo--."""
    cabeceras: list[dict] = []

    def anotar(peticion: httpx.Request) -> httpx.Response:
        cabeceras.append(dict(peticion.headers))
        if str(peticion.url).startswith(f"{BASE_GRAPH}/{MEDIA_ID}"):
            return httpx.Response(
                200, json={"url": URL_TEMPORAL, "mime_type": "image/jpeg", "file_size": 10}
            )
        return httpx.Response(200, content=b"ok", headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(anotar)))

    descargar()

    assert len(cabeceras) == 2
    assert all(c.get("authorization") == "Bearer token" for c in cabeceras)


# ==========================================================================================
# El cuerpo que se le manda a Meta al enviar una plantilla
# ==========================================================================================
#
# Offline y sobre el JSON, no sobre la excepcion: el fallo que esto vigila no lanza nada en
# casa. Sale en produccion, como un 132000 de Meta, y como la fila se marca ANTES de enviar
# (no negociable 21) ese rechazo no cuesta un envio: pierde la fila para siempre tras los tres
# intentos y despierta al doctor con un aviso de fallo.


def _enviar_plantilla_capturando(parametros: list[str], monkeypatch) -> dict:
    """Manda una plantilla contra un transporte de mentira y devuelve el JSON que salio."""
    cuerpos: list[dict] = []

    def anotar(peticion: httpx.Request) -> httpx.Response:
        import json

        cuerpos.append(json.loads(peticion.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(anotar)))
    asyncio.run(
        WhatsApp("token", "123").enviar_plantilla(
            "573001112233", plantilla="una_plantilla", parametros=parametros
        )
    )
    return cuerpos[0]


def test_una_plantilla_SIN_variables_no_manda_el_componente_body(monkeypatch):
    """`reactivacion_sin_agendar` se quedo sin hueco el 23/09/2026. A una plantilla estatica,
    un `body` con `parameters: []` le vale un 132000 («number of parameters does not match»),
    asi que la clave `components` no va: no es que vaya vacia, es que no esta."""
    cuerpo = _enviar_plantilla_capturando([], monkeypatch)

    assert "components" not in cuerpo["template"]
    assert cuerpo["template"]["name"] == "una_plantilla"


def test_una_plantilla_CON_variables_sigue_mandando_su_body(monkeypatch):
    """La otra mitad, o el arreglo de arriba apagaria en silencio los cuatro huecos del
    recordatorio de cita y el nombre de las otras dos reactivaciones."""
    cuerpo = _enviar_plantilla_capturando(["Marcela", "jueves 17/9"], monkeypatch)

    assert cuerpo["template"]["components"] == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Marcela"},
                {"type": "text", "text": "jueves 17/9"},
            ],
        }
    ]


# ==========================================================================================
# Un mensaje en blanco no es un mensaje
# ==========================================================================================
#
# Medido el 24/09/2026 con Andrea Rodriguez (+57 321 998 0137): el modelo devolvio
# `mensaje_al_paciente = " "` y ese espacio salio hacia Meta, que lo acepto y lo entrego.
# La paciente recibio un mensaje en blanco de la clinica.
#
# La guarda va en el BORDE y no en el camino que lo produjo, por dos razones. La primera es
# que la causa raiz no es aquel turno: `RespuestaDaniela.mensaje_al_paciente` promete
# `min_length=1` y un espacio mide 1, asi que el contrato no dice lo que parece decir --y
# eso vale para cualquier turno futuro, no solo para el de una reaccion--. La segunda es que
# por aqui pasan TODOS los caminos que le escriben a un paciente: Daniela, la frase de la
# cuota, la confirmacion del reseteo, el relevo y los recordatorios.


def _enviar_texto(texto: str, monkeypatch) -> list[httpx.Request]:
    """Intenta mandar `texto` y devuelve las peticiones que salieron de verdad."""
    peticiones: list[httpx.Request] = []

    def anotar(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(anotar)))
    asyncio.run(WhatsApp("token", "123").enviar_texto("573001112233", texto))
    return peticiones


@pytest.mark.parametrize("en_blanco", [" ", "", "   ", "\n", "\t \n"])
def test_no_se_le_manda_a_un_paciente_un_mensaje_en_blanco(en_blanco, monkeypatch):
    """Lo que se cuenta son las PETICIONES, no la excepcion: que lance esta bien, pero lo
    que importa es que no salga nada hacia Meta."""
    peticiones: list[httpx.Request] = []

    def anotar(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(anotar)))

    with pytest.raises(ErrorDeCanal):
        asyncio.run(WhatsApp("token", "123").enviar_texto("573001112233", en_blanco))

    assert peticiones == [], f"salio un mensaje en blanco hacia Meta ({en_blanco!r})"


def test_lanzar_y_no_callar_es_lo_que_deja_el_rastro(monkeypatch):
    """Por que `ErrorDeCanal` y no un `return` silencioso.

    `atencion` distingue tres estados en la fila del mensaje: respondido (con fecha y
    wamid), fallido (con motivo) y ninguno de los dos --«entro y nadie lo proceso»--. Un
    envio que se salta sin avisar quedaria como RESPONDIDO, porque quien llama no tendria
    forma de enterarse: la unica prueba de que al paciente no le llego nada desapareceria
    justo en el caso en que hace falta.
    """
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _cliente_con(httpx.MockTransport(lambda _p: httpx.Response(200, json={}))),
    )

    with pytest.raises(ErrorDeCanal, match="en blanco"):
        asyncio.run(WhatsApp("token", "123").enviar_texto("573001112233", " "))


def test_un_mensaje_normal_sigue_saliendo_igual(monkeypatch):
    """La otra mitad: sin esto, la guarda de arriba se pasa apagando los envios de verdad."""
    peticiones = _enviar_texto("Listo, Andrea. Nos vemos el lunes.", monkeypatch)

    assert len(peticiones) == 1
    assert f"{BASE_GRAPH}/123/messages" in str(peticiones[0].url)


# ==========================================================================================
# Las dos sondas de la pantalla de Estado del sistema
# ==========================================================================================


def _correr(corrutina):
    return asyncio.run(corrutina)


def test_las_plantillas_salen_vacias_si_no_se_puede_derivar_el_waba(monkeypatch):
    """Mismo contrato que `calidad_del_numero`: vacío significa «no se sabe», nunca «no hay
    ninguna». Una pantalla que pinte «0 plantillas» sobre un token caducado estaría diciendo
    que Meta las rechazó todas."""

    def sin_permisos(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "token invalido"}})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(sin_permisos)))

    assert _correr(WhatsApp("token", "123").estado_de_plantillas()) == []


def test_las_plantillas_se_juntan_de_todos_los_waba_del_token(monkeypatch):
    """El proyecto guarda el `phone_number_id` y no el id de la cuenta de negocio, así que el
    WABA sale del propio token. Un token con dos cuentas devuelve las de las dos."""

    def responder(peticion: httpx.Request) -> httpx.Response:
        url = str(peticion.url)
        if "debug_token" in url:
            return httpx.Response(200, json={"data": {"granular_scopes": [
                {"scope": "whatsapp_business_management", "target_ids": ["waba1", "waba2"]},
                {"scope": "pages_messaging", "target_ids": ["no-es-un-waba"]},
            ]}})
        cual = "waba1" if "/waba1/" in url else "waba2"
        return httpx.Response(200, json={"data": [
            {"name": f"plantilla_{cual}", "status": "APPROVED", "language": "es"},
        ]})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(responder)))

    plantillas = _correr(WhatsApp("token", "123").estado_de_plantillas())

    assert [p["nombre"] for p in plantillas] == ["plantilla_waba1", "plantilla_waba2"]
    assert {p["estado"] for p in plantillas} == {"APPROVED"}


def test_el_estado_del_webhook_trae_la_url_y_el_ultimo_error(monkeypatch):
    """`last_error_message` es lo más útil cuando algo no funciona: un 403 ahí significa que
    el secreto del `.env` no coincide con el que Telegram tiene registrado."""

    def responder(peticion: httpx.Request) -> httpx.Response:
        assert "getWebhookInfo" in str(peticion.url)
        return httpx.Response(200, json={"ok": True, "result": {
            "url": "https://daniela.maxicarecol.com/webhook/telegram",
            "pending_update_count": 3,
            "last_error_message": "Wrong response from the webhook: 403 Forbidden",
        }})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(responder)))

    estado = _correr(Telegram("token", -1001).estado_del_webhook())

    assert estado["url"].endswith("/webhook/telegram")
    assert estado["pendientes"] == 3
    assert "403" in estado["ultimo_error"]


def test_el_estado_del_webhook_es_vacio_si_telegram_dice_que_no(monkeypatch):
    """`{}` es «no se sabe». Devolver `{"url": ""}` diría que el webhook NO está puesto, que
    es una afirmación distinta y apagaría el relevo a ojos de quien mira la pantalla."""

    def responder(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(responder)))

    assert _correr(Telegram("token", -1001).estado_del_webhook()) == {}


def test_la_autorizacion_de_las_sondas_va_en_la_CABECERA_y_no_en_la_url(monkeypatch):
    """Como los otros cinco sitios de esta clase que hablan con Graph.

    En la query string el token acaba en el log del contenedor justo el día que falla: el
    mensaje de `raise_for_status()` de httpx lleva la URL entera dentro. Es la misma lección
    que ya se pagó con `/salud`, que dejó de devolver el texto del error de psycopg porque
    llevaba credenciales de Neon.

    **`debug_token` es la excepción y no se puede evitar**: su `input_token` es el token que
    se INSPECCIONA, y la Graph API no lo acepta de otra forma. Lo que se hace con él es lo
    otro: que nada registre el mensaje de un error de esa llamada (ver el `log.warning` con
    `type(e).__name__` en `_waba_ids`).
    """
    urls: list[str] = []
    cabeceras: list[dict] = []

    def responder(peticion: httpx.Request) -> httpx.Response:
        urls.append(str(peticion.url))
        cabeceras.append(dict(peticion.headers))
        if "debug_token" in str(peticion.url):
            return httpx.Response(200, json={"data": {"granular_scopes": [
                {"scope": "whatsapp_business_management", "target_ids": ["waba1"]},
            ]}})
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "AsyncClient", _cliente_con(httpx.MockTransport(responder)))

    _correr(WhatsApp("token-secretisimo", "123").estado_de_plantillas())

    assert len(urls) == 2, urls
    # Las dos autorizan por cabecera.
    assert all(c.get("authorization") == "Bearer token-secretisimo" for c in cabeceras)
    # Ninguna lleva `access_token=` en la URL, que es lo que la volvía registrable.
    assert not any("access_token" in u for u in urls), urls
    # Y la de plantillas --la única con `raise_for_status()`-- no lleva el token de ninguna
    # forma. La de `debug_token` sí, por lo que dice el docstring.
    plantillas = next(u for u in urls if "message_templates" in u)
    assert "token-secretisimo" not in plantillas, plantillas

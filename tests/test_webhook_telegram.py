"""La puerta del relevo: `POST /webhook/telegram`.

Offline. `TestClient` habla con la aplicación en el mismo proceso y `relevo.activar`,
`relevo.cerrar` y `relevo.relevar_mensaje` se sustituyen por espías: aquí no hay red, ni
Neon, ni Telegram. Lo que se prueba es el CABLEADO y, sobre todo, la puerta.

------------------------------------------------------------------------------------------
Por qué la mitad de este archivo son pruebas de rechazo
------------------------------------------------------------------------------------------

Esta URL es pública y es la única del sistema por la que algo de fuera puede hacer que el bot
**le escriba al WhatsApp de un paciente**. El webhook de Meta se defiende con una firma HMAC
del cuerpo; Telegram no firma nada: manda una cabecera con un secreto compartido.

Y degrada al revés que todo lo demás del proyecto. En el resto, cuando falta una
configuración se sigue adelante para no perder el mensaje de un paciente. Aquí, sin secreto
configurado, se cierra: lo que se pierde por degradar no es un mensaje, es el control de a
quién le habla la clínica.
"""

from __future__ import annotations

import os

import pytest

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()
os.environ.setdefault("MAXICARE_DATABASE_URL", "postgresql://prueba:prueba@localhost/nada")

from dataclasses import replace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from maxicare_daniela import runtime  # noqa: E402

SECRETO = "un-secreto-de-prueba"
CABECERA = "X-Telegram-Bot-Api-Secret-Token"
GRUPO = "-1009998887776"
CONV = "11111111-2222-3333-4444-555555555555"
RUTA = "/webhook/telegram"


@pytest.fixture
def espias(monkeypatch):
    """Sustituye las tres puertas de `relevo` y devuelve lo que se les pidió."""
    visto: dict[str, list] = {"activar": [], "cerrar": [], "relevar": []}

    async def _activar(**kw):
        visto["activar"].append(kw)

    async def _cerrar(id_conversacion, **kw):
        visto["cerrar"].append((id_conversacion, kw))
        return True

    async def _relevar(mensaje, **kw):
        visto["relevar"].append(mensaje)

    async def _responder_callback(callback_id, texto="", *, alerta=False):
        visto.setdefault("callbacks", []).append((callback_id, texto))

    async def _editar_teclado(mensaje_id, teclado=None):
        visto.setdefault("teclados", []).append((mensaje_id, teclado))

    monkeypatch.setattr(runtime.relevo, "activar", _activar)
    monkeypatch.setattr(runtime.relevo, "cerrar", _cerrar)
    monkeypatch.setattr(runtime.relevo, "relevar_mensaje", _relevar)
    monkeypatch.setattr(runtime._telegram, "responder_callback", _responder_callback)
    monkeypatch.setattr(runtime._telegram, "editar_teclado", _editar_teclado)
    return visto


def _cliente(monkeypatch, *, secreto: str = SECRETO) -> TestClient:
    # `Config` es `frozen`: se sustituye el objeto entero, no uno de sus campos.
    monkeypatch.setattr(
        runtime,
        "config",
        replace(
            runtime.config,
            telegram_webhook_secret=secreto,
            telegram_chat_doctores=GRUPO,
        ),
    )
    return TestClient(runtime.app)


def _callback(datos: str, *, chat: str = GRUPO, mensaje_id: int = 4242) -> dict:
    return {
        "callback_query": {
            "id": "cb-1",
            "data": datos,
            "from": {"id": 9, "first_name": "Ruiz"},
            "message": {"message_id": mensaje_id, "chat": {"id": chat}},
        }
    }


def _mensaje(chat: str = GRUPO) -> dict:
    return {
        "message": {
            "message_id": 300,
            "message_thread_id": 777,
            "is_topic_message": True,
            "chat": {"id": chat},
            "from": {"id": 9, "first_name": "Ruiz", "is_bot": False},
            "text": "Ven manana a las 8",
        }
    }


# ==========================================================================================
# La puerta
# ==========================================================================================


def test_sin_secreto_configurado_la_puerta_esta_cerrada(monkeypatch, espias):
    """Vacío significa CERRADO, no abierto. Es al revés de como degrada el resto del
    proyecto, y tiene que serlo: aquí lo que se pierde por degradar es el control de a quién
    le habla la clínica."""
    cliente = _cliente(monkeypatch, secreto="")

    r = cliente.post(RUTA, json=_callback(f"{runtime.relevo.PREFIJO_TOMAR}{CONV}"))

    assert r.status_code == 403
    assert espias["activar"] == []


def test_sin_la_cabecera_no_pasa_nada(monkeypatch, espias):
    cliente = _cliente(monkeypatch)

    r = cliente.post(RUTA, json=_callback(f"{runtime.relevo.PREFIJO_TOMAR}{CONV}"))

    assert r.status_code == 403
    assert espias["activar"] == []


def test_con_un_secreto_que_no_es_tampoco(monkeypatch, espias):
    cliente = _cliente(monkeypatch)

    r = cliente.post(
        RUTA,
        json=_callback(f"{runtime.relevo.PREFIJO_TOMAR}{CONV}"),
        headers={CABECERA: "otro-secreto"},
    )

    assert r.status_code == 403
    assert espias["activar"] == []


def test_un_cuerpo_que_no_es_json_se_acepta_para_que_no_reintente(monkeypatch, espias):
    """Telegram reintenta lo que no conteste. Un reintento de un `callback_query` de relevo
    es otro doctor tomando la conversación."""
    cliente = _cliente(monkeypatch)

    r = cliente.post(RUTA, content=b"esto no es json", headers={CABECERA: SECRETO})

    assert r.status_code == 200


# ==========================================================================================
# El cable
# ==========================================================================================


def _postear(cliente, cuerpo):
    return cliente.post(RUTA, json=cuerpo, headers={CABECERA: SECRETO})


def test_el_boton_de_tomar_llega_a_activar(monkeypatch, espias):
    cliente = _cliente(monkeypatch)

    r = _postear(cliente, _callback(f"{runtime.relevo.PREFIJO_TOMAR}{CONV}"))

    assert r.status_code == 200
    assert len(espias["activar"]) == 1
    llamada = espias["activar"][0]
    assert llamada["id_conversacion"] == CONV
    assert llamada["doctor"] == "Ruiz"
    # El mensaje del que cuelga el botón: hay que dejarlo marcado como atendido.
    assert llamada["mensaje_id"] == 4242


def test_el_boton_de_devolver_cierra_y_se_lleva_su_propio_boton(monkeypatch, espias):
    """Pulsar «Listo» dos veces no puede parecer que hace algo la segunda."""
    cliente = _cliente(monkeypatch)

    _postear(cliente, _callback(f"{runtime.relevo.PREFIJO_DEVOLVER}{CONV}", mensaje_id=55))

    assert espias["cerrar"] == [(CONV, espias["cerrar"][0][1])]
    assert espias["cerrar"][0][1]["motivo"] == "devuelto_por_doctor"
    assert espias.get("teclados") == [(55, None)]


def test_lo_que_escribe_un_doctor_en_un_tema_llega_a_relevar(monkeypatch, espias):
    cliente = _cliente(monkeypatch)

    _postear(cliente, _mensaje())

    assert len(espias["relevar"]) == 1
    assert espias["relevar"][0]["text"] == "Ven manana a las 8"


def test_un_callback_desde_otro_chat_no_activa_nada(monkeypatch, espias):
    """El mismo bot puede estar en más grupos, y a un bot se le puede escribir por privado.

    El secreto de la cabecera prueba que el update viene de Telegram; esto prueba que viene
    de DONDE tiene que venir. Sin esta comprobación, un `callback_data` copiado a mano en
    cualquier chat activaría un relevo de verdad sobre un paciente de verdad.
    """
    cliente = _cliente(monkeypatch)

    _postear(cliente, _callback(f"{runtime.relevo.PREFIJO_TOMAR}{CONV}", chat="-100111"))

    assert espias["activar"] == []


def test_un_mensaje_desde_otro_chat_no_se_releva(monkeypatch, espias):
    cliente = _cliente(monkeypatch)

    _postear(cliente, _mensaje(chat="-100111"))

    assert espias["relevar"] == []


def test_un_boton_que_no_reconozco_no_revienta_nada(monkeypatch, espias):
    cliente = _cliente(monkeypatch)

    r = _postear(cliente, _callback("cualquier:cosa"))

    assert r.status_code == 200
    assert espias["activar"] == [] and espias["cerrar"] == []
    # Pero se le responde: un botón que se queda girando se pulsa otra vez.
    assert espias.get("callbacks")


def test_un_mensaje_editado_no_se_reenvia(monkeypatch, espias):
    """Editar en Telegram un mensaje que ya salió hacia WhatsApp no puede deshacerlo --Meta
    no tiene edición-- y reenviar la versión corregida le dejaría al paciente las dos."""
    cliente = _cliente(monkeypatch)
    cuerpo = _mensaje()
    cuerpo["edited_message"] = cuerpo.pop("message")

    _postear(cliente, cuerpo)

    assert espias["relevar"] == []


def test_un_update_que_no_es_ni_una_cosa_ni_la_otra_se_ignora(monkeypatch, espias):
    """Telegram manda muchas clases de update. Los que no son del relevo no son un error."""
    cliente = _cliente(monkeypatch)

    r = _postear(cliente, {"my_chat_member": {"chat": {"id": GRUPO}}})

    assert r.status_code == 200
    assert espias["activar"] == [] and espias["relevar"] == []

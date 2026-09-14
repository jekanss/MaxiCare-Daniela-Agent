"""El cable: que un mensaje de WhatsApp llegue a Daniela, y que el archivo llegue igual.

Offline. `TestClient` habla con la aplicación en el mismo proceso; `ingesta.procesar_mensaje`
y `atencion.atender` se sustituyen por espías, así que aquí no hay red, ni Neon, ni modelo.
Lo que se prueba es el CABLEADO de `runtime._entregar`, que es lo único que esta tarea añade
al camino de producción.

------------------------------------------------------------------------------------------
La prueba que justifica el archivo entero
------------------------------------------------------------------------------------------

`test_el_archivo_sigue_llegando_al_doctor_pase_lo_que_pase`. La garantía de la fase 2 es que
un archivo de un paciente llega a los doctores aunque todo lo demás falle, y esta tarea es
exactamente donde se podía romper sin que nadie lo notara: basta con meter el turno de
Daniela en el mismo `try` que la entrega, o antes de ella, para que un fallo del modelo se
lleve por delante una radiografía urgente. Nada en producción lo avisaría -- el webhook
seguiría devolviendo 200.

`test_el_orden_es_primero_el_doctor_y_luego_daniela` es su otra mitad: con Daniela primero,
un turno lento no pierde el archivo, pero lo retrasa los diez segundos que tarde el modelo.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime

import pytest

# Mismo preámbulo que `test_web.py`, y por el mismo motivo: `runtime.py` construye su
# `Config` al importarse y `MAXICARE_DATABASE_URL` es obligatoria. Se carga el `.env` real
# primero y solo se inventa una URL si de verdad no hay ninguna.
#
# `setdefault` y NUNCA `os.environ[...] = ...`: pytest importa todos los módulos de prueba
# antes de ejecutar ninguno, así que una asignación aquí se queda fijada para toda la sesión
# y deja a `test_tools_neon.py` conectándose a `localhost/nada`. Ya pasó una vez.
from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()
os.environ.setdefault("MAXICARE_DATABASE_URL", "postgresql://prueba:prueba@localhost/nada")

from dataclasses import replace  # noqa: E402

from agents import Agent, RunConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from maxicare_daniela import (  # noqa: E402
    atencion,
    conversacion,
    herramientas,
    ingesta,
    persistencia,
    runtime,
)
from maxicare_daniela.calendario import (  # noqa: E402
    CalendarioCaido,
    CalendarioDoble,
    ErrorDeCalendario,
)
from maxicare_daniela.contratos import (  # noqa: E402
    ContextoDaniela,
    RespuestaDaniela,
    SolicitudEscalamiento,
)

from .dobles import ModeloGuionizado, responde, respuesta_daniela, usa_tool  # noqa: E402

APP_SECRET = "un-app-secret-de-prueba"


# ==========================================================================================
# El sobre de Meta y su firma
# ==========================================================================================


def _sobre(*mensajes, statuses=None) -> dict:
    """La envoltura de cuatro niveles que manda Meta. La forma real, no una simplificada."""
    valor = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"}}
    if mensajes:
        valor["messages"] = list(mensajes)
    if statuses is not None:
        valor["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": valor}]}],
    }


def _mensaje_de_texto(wamid: str = "wamid.de.prueba.1") -> dict:
    return _sobre(
        {
            "from": "573001234567",
            "id": wamid,
            "timestamp": "1757700000",
            "type": "text",
            "text": {"body": "buenas, cuánto vale una limpieza?"},
        }
    )


def _firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()


def _enviar(cliente: TestClient, payload: dict, *, firma: str | None = None):
    """El POST tal como lo hace Meta: bytes crudos y la firma sobre esos mismos bytes.

    Se manda `content=` y no `json=` porque la firma cubre los bytes exactos: dejar que el
    cliente reserialice el diccionario daría otra cadena y otra firma.
    """
    crudo = json.dumps(payload).encode()
    return cliente.post(
        "/webhook/whatsapp",
        content=crudo,
        headers={
            "x-hub-signature-256": firma if firma is not None else _firmar(crudo),
            "content-type": "application/json",
        },
    )


# ==========================================================================================
# Espías
# ==========================================================================================


class Espias:
    """Qué se llamó, con qué, y en qué orden."""

    def __init__(self) -> None:
        self.orden: list[str] = []
        self.procesados: list[ingesta.MensajeEntrante] = []
        self.atendidos: list[ingesta.MensajeEntrante] = []
        #: Lo pone la prueba que quiere ver reventar el turno de Daniela.
        self.daniela_revienta = False


@pytest.fixture
def espias(monkeypatch) -> Espias:
    registro = Espias()

    async def procesar_falso(m, **_kwargs):
        registro.orden.append("doctor")
        registro.procesados.append(m)
        return ingesta.Resultado(m.wamid, nuevo=True, reenviado=True)

    async def atender_falso(m, **_kwargs):
        registro.orden.append("daniela")
        registro.atendidos.append(m)
        if registro.daniela_revienta:
            raise RuntimeError("el modelo se cayó a mitad del turno")
        return atencion.Atendido(wamid=m.wamid, id_conversacion="conv-1", respondido=True)

    # Se sustituye el atributo del módulo, no un nombre importado: `runtime.py` llama
    # `ingesta.procesar_mensaje(...)` y `atencion.atender(...)`, así que esto es lo que ve.
    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_falso)
    monkeypatch.setattr(atencion, "atender", atender_falso)
    return registro


@pytest.fixture
def cliente(monkeypatch) -> TestClient:
    # `Config` es `frozen`, así que se sustituye el objeto entero y no uno de sus campos.
    monkeypatch.setattr(
        runtime, "config", replace(runtime.config, whatsapp_app_secret=APP_SECRET)
    )
    return TestClient(runtime.app)


# ==========================================================================================
# El cable
# ==========================================================================================


def test_un_mensaje_de_texto_llega_a_daniela(cliente, espias):
    """Lo que esta tarea existe para conseguir: que el paciente reciba respuesta.

    El webhook lleva meses registrando mensajes y reenviándolos a Telegram sin contestarle
    a nadie. Esta es la línea que lo cambia.
    """
    r = _enviar(cliente, _mensaje_de_texto("wamid.hola"))

    assert r.status_code == 200
    assert [m.wamid for m in espias.atendidos] == ["wamid.hola"]


def test_el_archivo_sigue_llegando_al_doctor_pase_lo_que_pase(cliente, espias):
    """LA prueba de esta tarea.

    La garantía de la fase 2 no admite excepciones: la radiografía que manda un paciente
    llega a los doctores aunque todo lo demás falle. Daniela es «todo lo demás».

    Si alguien mete `atender` dentro del mismo `try` que `procesar_mensaje` --o antes-- esta
    prueba es lo único que lo caza. En producción no se vería: el webhook seguiría
    devolviendo 200, el log tendría una excepción de Daniela, y el archivo simplemente no
    estaría en Telegram.
    """
    espias.daniela_revienta = True

    r = _enviar(cliente, _mensaje_de_texto("wamid.radiografia"))

    assert r.status_code == 200
    assert [m.wamid for m in espias.procesados] == ["wamid.radiografia"], (
        "el turno de Daniela se llevó por delante la entrega del archivo al doctor"
    )


def test_el_orden_es_primero_el_doctor_y_luego_daniela(cliente, espias):
    """Con Daniela primero el archivo no se pierde, pero llega tarde: los segundos que tarde
    el modelo en contestar son segundos que el doctor no tiene la radiografía."""
    _enviar(cliente, _mensaje_de_texto("wamid.orden"))

    assert espias.orden == ["doctor", "daniela"]


def test_un_reintento_de_Meta_no_hace_que_Daniela_conteste_otra_vez(cliente, espias, monkeypatch):
    """Meta reintenta el mismo webhook, y el proyecto ya lo tenía asumido.

    `procesar_mensaje` deduplica por `wamid` con un `ON CONFLICT DO NOTHING` y devuelve
    `nuevo=False` cuando reconoce un reintento --hasta lo dice en el log--. Ese dato se
    tiraba: el archivo llegaba al doctor una sola vez, pero Daniela corría el turno entero
    otra vez. Medido antes del arreglo, el mismo POST firmado tres veces: 1 reenvío al
    doctor y **3 respuestas al paciente**, con sus tres corridas del modelo pagadas.
    """
    vistos: set[str] = set()

    async def procesar_deduplicando(m, **_kwargs):
        espias.orden.append("doctor")
        nuevo = m.wamid not in vistos
        vistos.add(m.wamid)
        if nuevo:
            espias.procesados.append(m)
        return ingesta.Resultado(m.wamid, nuevo=nuevo, reenviado=nuevo)

    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_deduplicando)

    payload = _mensaje_de_texto("wamid.reintentado")
    for _ in range(3):
        r = _enviar(cliente, payload)
        assert r.status_code == 200

    assert len(espias.procesados) == 1, "el doctor recibió el archivo más de una vez"
    assert len(espias.atendidos) == 1, (
        "Daniela contestó a un reintento de Meta: el paciente recibe respuestas repetidas "
        "y la clínica paga una corrida del modelo por cada reintento"
    )


def test_si_la_entrega_al_doctor_revienta_Daniela_contesta_igual(cliente, espias, monkeypatch):
    """La garantía espejo, y no es simétrica por gusto.

    `procesar_mensaje` NO atrapa todo: los fallos de Neon de su `_registrar` salen crudos.
    Con las dos llamadas en el mismo `try`, un parpadeo de la base al registrar el mensaje
    dejaría al paciente sin respuesta --sin que nada lo avisara, porque el webhook seguiría
    devolviendo 200--. Que cada una tenga el suyo es lo que hace que el fallo de una no se
    lleve a la otra en NINGUNA de las dos direcciones.
    """

    async def procesar_revienta(m, **_kwargs):
        espias.orden.append("doctor")
        raise RuntimeError("Neon no responde al registrar el mensaje")

    # Pisa al espía del fixture, con `monkeypatch` para que se deshaga solo: una prueba no
    # le cambia el entorno a las demás.
    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_revienta)

    r = _enviar(cliente, _mensaje_de_texto("wamid.neon.caido"))

    assert r.status_code == 200
    assert [m.wamid for m in espias.atendidos] == ["wamid.neon.caido"], (
        "un fallo registrando el mensaje dejó al paciente sin respuesta"
    )
    assert espias.orden == ["doctor", "daniela"]


def test_si_procesar_mensaje_revienta_Daniela_contesta_aunque_no_sepa_si_es_reintento(
    cliente, espias, monkeypatch
):
    """El matiz del corte por reintento, y hace falta escribirlo.

    Solo se corta cuando `procesar_mensaje` DIJO que es un reintento. Si revienta antes de
    devolver nada --Telegram caído, la descarga del archivo-- no sabemos si es nuevo, y
    entonces se atiende: un fallo entregando al doctor no puede dejar además al paciente sin
    respuesta. Un `if not resultado.nuevo` escrito sobre un `resultado` que puede ser `None`
    convertiría cada fallo de Telegram en un paciente ignorado.
    """

    async def procesar_revienta(m, **_kwargs):
        espias.orden.append("doctor")
        raise RuntimeError("Telegram no responde")

    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_revienta)

    _enviar(cliente, _mensaje_de_texto("wamid.sin.resultado"))

    assert [m.wamid for m in espias.atendidos] == ["wamid.sin.resultado"]


def test_a_daniela_se_le_pasa_el_calendario_construido_al_arrancar(cliente, espias, monkeypatch):
    """Sin el argumento `calendario=`, `atender` cae a construir uno POR MENSAJE.

    `CalendarioGoogle.__init__` hace una lectura real contra Google --es su comprobación de
    acceso--, así que eso es una llamada a Google por cada WhatsApp que entre. No se vería
    como un fallo: se vería como latencia y como una factura rara.
    """
    recibidos: list[object] = []

    async def atender_falso(m, **kwargs):
        recibidos.append(kwargs.get("calendario", "NO VINO"))
        return atencion.Atendido(wamid=m.wamid, id_conversacion="conv-1", respondido=True)

    monkeypatch.setattr(atencion, "atender", atender_falso)
    centinela = object()
    monkeypatch.setattr(runtime, "_calendario", centinela)

    _enviar(cliente, _mensaje_de_texto("wamid.calendario"))

    assert recibidos == [centinela], (
        "atender no recibió el calendario del arranque: lo construiría por mensaje"
    )


def test_el_motivo_de_un_turno_con_incidencia_queda_en_el_log(cliente, monkeypatch, caplog):
    """El `Atendido` traía `motivo`, `turno` y `escalado_por`, y `_entregar` lo descartaba
    entero (`await atencion.atender(...)` sin asignar).

    Un turno que respondió con el mensaje de emergencia se veía en el log igual que uno que
    fue bien, y el único rastro quedaba en `mensajes_entrantes` -- que hay que ir a
    consultar sabiendo ya que pasó algo.
    """

    async def procesar_falso(m, **_kwargs):
        return ingesta.Resultado(m.wamid, nuevo=True, reenviado=True)

    async def atender_con_incidencia(m, **_kwargs):
        return atencion.Atendido(
            wamid=m.wamid,
            id_conversacion="conv-7",
            respondido=True,
            texto_enviado="mensaje seguro",
            motivo="RuntimeError: una tool con un bug",
            turno=4,
            escalado_por="dato_faltante",
        )

    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_falso)
    monkeypatch.setattr(atencion, "atender", atender_con_incidencia)

    with caplog.at_level("WARNING", logger="maxicare.runtime"):
        _enviar(cliente, _mensaje_de_texto("wamid.incidencia"))

    registrado = "\n".join(r.getMessage() for r in caplog.records)
    assert "una tool con un bug" in registrado
    assert "conv-7" in registrado


def test_un_webhook_sin_mensajes_no_llama_a_daniela(cliente, espias):
    """Meta manda un webhook por cada cambio de estado de lo que NOSOTROS enviamos: enviado,
    entregado, leído. Llegan constantemente, y cada uno que llegara a Daniela sería una
    corrida del modelo pagada para contestarle a nadie."""
    payload = _sobre(
        statuses=[
            {
                "id": "wamid.enviado.por.nosotros",
                "status": "delivered",
                "timestamp": "1757700000",
                "recipient_id": "573001234567",
            }
        ]
    )

    r = _enviar(cliente, payload)

    assert r.status_code == 200
    assert espias.atendidos == []
    assert espias.procesados == []


def test_una_firma_invalida_no_llega_a_daniela(cliente, espias):
    """La URL del webhook es pública. Sin esta puerta, cualquiera que la adivine pone a
    Daniela a conversar --y a gastar tokens-- con un mensaje que ningún paciente mandó."""
    r = _enviar(cliente, _mensaje_de_texto("wamid.falso"), firma="sha256=deadbeef")

    assert r.status_code == 403
    assert espias.atendidos == []
    assert espias.procesados == []


def test_el_webhook_responde_200_aunque_daniela_reviente(cliente, espias):
    """Un 500 le dice a Meta que reintente, y cada reintento es otra copia del archivo para
    el doctor. El 200 no dice «lo entregué»: dice «lo recibí»."""
    espias.daniela_revienta = True

    r = _enviar(cliente, _mensaje_de_texto("wamid.revienta"))

    assert r.status_code == 200


# ==========================================================================================
# El calendario
# ==========================================================================================


def test_el_calendario_se_construye_una_sola_vez_al_arrancar():
    """`CalendarioGoogle.__init__` hace una lectura real contra Google --es su comprobación
    de acceso--, así que construirlo dentro de `_entregar` la pagaría en cada WhatsApp que
    entre. Que esté colgado del startup es lo que lo impide."""
    assert runtime._construir_el_calendario in runtime.app.router.on_startup


def test_un_calendario_que_no_arranca_queda_CAIDO_y_jamas_un_doble(monkeypatch):
    """La diferencia entre los dos objetos es la razón de ser del proyecto.

    `CalendarioDoble` guarda los eventos en un diccionario en memoria y dice que sí a todo:
    con uno aquí, `crear_cita` tomaría el cupo en Neon, «crearía» el evento en el vacío y
    Daniela le confirmaría la cita al paciente --que llegaría a una clínica donde nadie lo
    espera, y sin una línea roja en el log--. `CalendarioCaido` lanza, que es lo que las
    tools de la fase 3 saben manejar: liberan el cupo y escalan.

    Y el servidor tiene que arrancar igual: dejarlo caído deja a los doctores sin recibir
    las radiografías de sus pacientes, que es peor que una Daniela que no agenda.
    """

    def no_arranca(_config):
        raise RuntimeError("Google devolvió 403: el calendario no está compartido")

    monkeypatch.setattr(runtime, "calendario_desde_config", no_arranca)
    monkeypatch.setattr(runtime, "_calendario", "sin tocar")

    runtime._construir_el_calendario()  # no propaga: el webhook arranca igual

    assert isinstance(runtime._calendario, CalendarioCaido)
    assert not isinstance(runtime._calendario, CalendarioDoble)
    with pytest.raises(ErrorDeCalendario):
        runtime._calendario.bloqueos(datetime.now(), datetime.now())


def test_con_las_credenciales_de_google_VACIAS_tampoco_queda_un_doble(monkeypatch):
    """La otra mitad de la puerta, y la que de verdad iba a pasar en producción.

    `calendario_desde_config` NO LANZA cuando faltan las credenciales: devuelve un
    `CalendarioDoble()` y lo deja en un `warning`. La prueba de arriba solo cubría la rama
    que lanza, así que este camino --el realista-- estaba abierto con su nombre puesto.

    Y es realista porque en este proyecto **una variable presente y vacía no es una variable
    ausente**: el `.env` trae casi todas las claves escritas y sin valor. Un
    `MAXICARE_GOOGLE_SA_B64=` en el VPS no da error de arranque; daría un doble en
    producción, y con él `crear_cita` toma el cupo, «crea» el evento en un diccionario,
    guarda un `evento_calendar_id` que no existe en ningún calendario y Daniela confirma la
    cita. El paciente llega a una clínica donde nadie lo espera, y el único rastro es un INFO
    diciendo que el calendario está listo.

    Aquí NO se sustituye `calendario_desde_config`: se la deja correr de verdad con las
    credenciales vacías, que es justo lo que la prueba tiene que ejercitar.
    """
    monkeypatch.setattr(
        runtime, "config", replace(runtime.config, google_sa_b64="", google_calendar_id="")
    )
    monkeypatch.setattr(runtime, "_calendario", "sin tocar")

    runtime._construir_el_calendario()

    assert not isinstance(runtime._calendario, CalendarioDoble), (
        "arrancó con un calendario de mentira: confirmaría citas que no existen"
    )
    assert isinstance(runtime._calendario, CalendarioCaido)
    with pytest.raises(ErrorDeCalendario):
        runtime._calendario.crear_evento(inicio=datetime.now(), duracion_minutos=60, titulo="x")


def test_atencion_tampoco_cae_a_un_doble_con_las_credenciales_vacias():
    """El mismo hueco vivía en `atencion._calendario_por_defecto`, que es por donde entra el
    calendario cuando `runtime` no le pasa ninguno. Cerrar solo uno de los dos dejaba la
    puerta abierta por el otro lado."""
    config_vacia = replace(runtime.config, google_sa_b64="", google_calendar_id="")

    calendario = atencion._calendario_por_defecto(config_vacia)

    assert not isinstance(calendario, CalendarioDoble)
    assert isinstance(calendario, CalendarioCaido)


# ==========================================================================================
# El aviso a los doctores -- y el aviso que NO se manda dos veces
# ==========================================================================================


class TelegramFalso:
    def __init__(self) -> None:
        self.enviados: list[dict] = []

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None) -> int:
        self.enviados.append({"texto": texto, "tema_id": tema_id, "teclado": teclado})
        return 4242


class ConexionFalsa:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _contexto(**cambios) -> ContextoDaniela:
    base = dict(
        id_conversacion="conv-441",
        telefono_completo="573001112233",
        database_url="postgresql://no-se-usa",
        calendario=CalendarioDoble(),
        turno_actual=3,
        nombre_paciente="Ana Gómez",
    )
    base.update(cambios)
    return ContextoDaniela(**base)


@pytest.fixture
def base_falsa(monkeypatch) -> list[dict]:
    """Captura lo que se le pasa a `insertar_escalamiento` sin abrir una conexión."""
    registradas: list[dict] = []

    def insertar_falso(_conn, **kwargs):
        registradas.append(kwargs)
        return 7

    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
    monkeypatch.setattr(persistencia, "insertar_escalamiento", insertar_falso)
    monkeypatch.setattr(
        persistencia, "anotar_telegram_en_escalamiento", lambda conn, eid, mid: None
    )
    return registradas


def test_el_aviso_al_doctor_sale_por_telegram_con_lo_que_se_le_dijo_al_paciente(
    monkeypatch, base_falsa
):
    ctx = _contexto()
    telegram = TelegramFalso()
    monkeypatch.setattr(runtime, "_telegram", telegram)

    asyncio.run(runtime._avisar_a_doctores(ctx, "clinico", "Déjame confirmarlo y te escribo."))

    assert len(telegram.enviados) == 1
    texto = telegram.enviados[0]["texto"]
    assert "clinico" in texto
    assert "+573001112233" in texto
    assert "Déjame confirmarlo" in texto


def test_el_aviso_del_orquestador_usa_la_clave_del_turno_y_no_una_inventada(
    monkeypatch, base_falsa
):
    """`plan.contexto`: las claves de idempotencia las arma el orquestador. Una clave que
    dependiera de otra cosa --la hora, un uuid, lo que escriba el modelo-- no deduplicaría
    nada, y el doctor recibiría dos Telegram del mismo escalamiento."""
    ctx = _contexto(turno_actual=3)
    monkeypatch.setattr(runtime, "_telegram", TelegramFalso())

    asyncio.run(runtime._avisar_a_doctores(ctx, "dato_faltante", "Ya te confirmo."))

    assert base_falsa[0]["clave_idempotencia"] == "conv-441:escalamiento:3"


def test_el_aviso_al_doctor_escapa_de_verdad_lo_que_escribio_el_modelo(monkeypatch, base_falsa):
    """`_escapar` tenía que hacer algo, no solo existir: devolver el texto tal cual dejaba
    toda la suite en verde. Telegram rechaza el mensaje entero si el HTML no cierra, y un
    aviso rechazado es un doctor que no se entera."""
    ctx = _contexto(nombre_paciente="Ana <3 Gómez")
    telegram = TelegramFalso()
    monkeypatch.setattr(runtime, "_telegram", telegram)

    asyncio.run(
        runtime._avisar_a_doctores(ctx, "clinico", "Te paso con el doctor <ya mismo>.")
    )

    texto = telegram.enviados[0]["texto"]
    assert "&lt;3" in texto
    assert "&lt;ya mismo&gt;" in texto
    assert "<3" not in texto
    assert "<ya mismo>" not in texto
    # Y sin romper el formato propio.
    assert texto.startswith("<b>Escalamiento")


def test_el_aviso_va_al_General_y_el_cero_no_se_convierte_en_el_tema_1(monkeypatch, base_falsa):
    """`0` significa «el General». Un `or` lo cambiaba por el tema del proceso --que suele
    ser `1`-- y Telegram contesta `Bad Request: message thread not found`: el aviso no
    llega. `canales.enviar_mensaje` ya resuelve el `0` con su `if tema_id:`."""
    ctx = _contexto(tema_general=0)
    telegram = TelegramFalso()
    monkeypatch.setattr(runtime, "_telegram", telegram)
    monkeypatch.setattr(runtime, "_tema_general", 1)

    asyncio.run(runtime._avisar_a_doctores(ctx, "clinico", "Ya te confirmo."))

    assert telegram.enviados[0]["tema_id"] == 0


def test_un_turno_que_ya_escalo_no_manda_un_segundo_telegram(monkeypatch):
    """`insertar_escalamiento` devuelve `None` cuando esa clave ya existe, y ese `None` es
    toda la defensa: significa que la tool `escalar_a_doctores` ya avisó en este mismo turno.
    A la cuarta alerta repetida el doctor deja de mirarlas."""
    ctx = _contexto()
    telegram = TelegramFalso()
    monkeypatch.setattr(runtime, "_telegram", telegram)
    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
    monkeypatch.setattr(persistencia, "insertar_escalamiento", lambda _conn, **kw: None)

    asyncio.run(runtime._avisar_a_doctores(ctx, "clinico", "Ya te confirmo."))

    assert telegram.enviados == [], "se le avisó dos veces al doctor del mismo turno"


def test_un_escalamiento_escrito_pero_NUNCA_avisado_se_reintenta(monkeypatch):
    """La clave se quema al CONSEGUIR, no al INTENTAR.

    El caso real: la tool escribe su fila y el Telegram no sale --Telegram rechazó el HTML
    porque el paciente se llama «Ana <3 Gómez», o devolvió un 5xx, o hubo límite de tasa--.
    `failure_error_function` se traga el `ErrorDeCanal` para que el modelo siga conversando, y
    al cerrar el turno este aviso encontraba la clave ya escrita y se callaba. Resultado
    medido antes del arreglo: dos filas de escalamiento en Neon y CERO Telegram al doctor.

    Un aviso que no salió no es un aviso duplicado. Lo que distingue los dos casos es
    `telegram_message_id`, que hasta ahora nadie consultaba.
    """
    ctx = _contexto()
    telegram = TelegramFalso()
    anotados: list[tuple[int, int]] = []

    monkeypatch.setattr(runtime, "_telegram", telegram)
    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
    # La clave ya existe: `insertar_escalamiento` no escribe nada.
    monkeypatch.setattr(persistencia, "insertar_escalamiento", lambda _conn, **kw: None)
    # ...pero esa fila se quedó sin Telegram.
    monkeypatch.setattr(
        persistencia, "escalamiento_pendiente_de_aviso", lambda _conn, clave: 31
    )
    monkeypatch.setattr(
        persistencia,
        "anotar_telegram_en_escalamiento",
        lambda conn, eid, mid: anotados.append((eid, mid)),
    )

    asyncio.run(runtime._avisar_a_doctores(ctx, "clinico", "Ya te confirmo."))

    assert len(telegram.enviados) == 1, "el doctor se quedó sin enterarse del escalamiento"
    assert anotados == [(31, 4242)], "el reintento no quedó anotado; se repetiría para siempre"


def test_un_escalamiento_YA_avisado_no_se_reintenta(monkeypatch):
    """La otra mitad, y hace falta: si `escalamiento_pendiente_de_aviso` devolviera un id
    para una fila que sí se avisó, el arreglo de arriba habría cambiado un aviso perdido por
    un aviso repetido, que es la otra forma de que el doctor deje de mirarlos."""
    ctx = _contexto()
    telegram = TelegramFalso()
    monkeypatch.setattr(runtime, "_telegram", telegram)
    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
    monkeypatch.setattr(persistencia, "insertar_escalamiento", lambda _conn, **kw: None)
    monkeypatch.setattr(
        persistencia, "escalamiento_pendiente_de_aviso", lambda _conn, clave: None
    )

    asyncio.run(runtime._avisar_a_doctores(ctx, "clinico", "Ya te confirmo."))

    assert telegram.enviados == []


def test_la_tool_y_el_orquestador_arman_exactamente_la_misma_clave(monkeypatch, base_falsa):
    """Si no coinciden, la deduplicación no deduplica nada.

    Y no coincidían: `_escalar_a_doctores` usaba `solicitud.clave_idempotencia`, un campo que
    rellena EL MODELO. Escribiera lo que escribiera, el `INSERT` de la tool y el del
    orquestador eran dos filas distintas y el doctor recibía los dos Telegram.
    """
    ctx = _contexto(turno_actual=5)
    telegram = TelegramFalso()
    monkeypatch.setattr(runtime, "_telegram", telegram)

    asyncio.run(
        herramientas._escalar_a_doctores(
            ctx,
            SolicitudEscalamiento(
                motivo="clinico",
                resumen_para_doctor="Pregunta por una lesión que ve en su radiografía.",
                pregunta_concreta="¿Se puede responder algo de esto por WhatsApp?",
                clave_idempotencia="lo-que-al-modelo-se-le-ocurrio",
            ),
            telegram=telegram,
        )
    )
    asyncio.run(runtime._avisar_a_doctores(ctx, "clinico", "Ya te confirmo."))

    claves = [k["clave_idempotencia"] for k in base_falsa]
    assert len(claves) == 2
    assert claves[0] == claves[1] == "conv-441:escalamiento:5"
    assert "lo-que-al-modelo-se-le-ocurrio" not in claves


def test_en_un_turno_de_verdad_el_doctor_recibe_UN_telegram_y_no_dos(monkeypatch):
    """La comprobación que de verdad importa, y por el camino completo.

    Las de arriba comparan claves; esta corre un turno entero a través del SDK --con el
    `escalar_a_doctores` de producción entre las tools-- en el peor caso posible: el modelo
    llama a la tool Y ADEMÁS marca `requiere_escalamiento`, así que las dos rutas de aviso se
    disparan sobre el mismo turno. El doctor tiene que recibir un solo Telegram.

    Lo que se comprueba no es un `if`: es que la clave que arma la tool durante la corrida y
    la que arma `_avisar_a_doctores` después coinciden de verdad cuando nadie las puso una al
    lado de la otra a mano.
    """
    ctx = _contexto(turno_actual=7)
    telegram = TelegramFalso()
    claves: list[str] = []
    vistas: set[str] = set()

    def insertar_con_deduplicacion(_conn, **kwargs):
        """El `ON CONFLICT (clave_idempotencia) DO NOTHING` de Neon, en diez líneas."""
        clave = kwargs["clave_idempotencia"]
        claves.append(clave)
        if clave in vistas:
            return None
        vistas.add(clave)
        return len(vistas)

    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
    monkeypatch.setattr(persistencia, "insertar_escalamiento", insertar_con_deduplicacion)
    monkeypatch.setattr(
        persistencia, "anotar_telegram_en_escalamiento", lambda conn, eid, mid: None
    )
    # La tool arma su propio cliente con las credenciales del contexto; sin esto le sonaría
    # el teléfono a un doctor de verdad.
    monkeypatch.setattr(herramientas, "Telegram", lambda *a, **k: telegram)
    monkeypatch.setattr(runtime, "_telegram", telegram)
    monkeypatch.setattr(conversacion, "_guardar_estado", lambda ctx, resultado: None)

    agente = Agent(
        name="daniela_de_prueba",
        model=ModeloGuionizado(
            usa_tool(
                "escalar_a_doctores",
                solicitud={
                    "motivo": "clinico",
                    "resumen_para_doctor": "Dice que le duele desde hace tres días.",
                    "pregunta_concreta": "¿Lo citamos hoy mismo?",
                    "clave_idempotencia": "la-que-al-modelo-se-le-ocurrio",
                },
            ),
            responde(
                respuesta_daniela(
                    "Ya le estoy avisando al doctor.",
                    requiere_escalamiento=True,
                    motivo_escalamiento="clinico",
                )
            ),
        ),
        instructions="Responde.",
        output_type=RespuestaDaniela,
        tools=[herramientas.escalar_a_doctores],
    )

    asyncio.run(
        conversacion.responder(
            "me duele mucho",
            ctx=ctx,
            agente=agente,
            run_config=RunConfig(tracing_disabled=True),
            al_escalar=runtime._avisar_a_doctores,
        )
    )

    assert claves == ["conv-441:escalamiento:8", "conv-441:escalamiento:8"], (
        "la tool y el orquestador escribieron dos filas distintas para el mismo turno"
    )
    assert len(telegram.enviados) == 1, (
        "el doctor recibió el mismo escalamiento dos veces; a la cuarta deja de mirarlas"
    )


# ==========================================================================================
# Que apagar el servidor no le mate el turno a nadie
# ==========================================================================================
#
# El 13/09/2026 un despliegue recreó el contenedor con un turno a medias. Leído de
# `mensajes_entrantes`:
#
#     02:50:00  RESPONDIDO     '?'
#     02:47:22  SIN RESPUESTA  'Sabes alguna cosa de unicornios?'
#
# `respondido_en` NULL y `fallo_respuesta` NULL: ni respuesta ni fallo. El proceso no falló,
# lo mataron, y un proceso muerto no escribe su motivo. Con el `stop_grace_period` por
# defecto --10 segundos, contra un turno de 20 s de búfer más el modelo-- eso no era mala
# suerte: pasaba en todo despliegue que pillara a alguien escribiendo.


def test_un_turno_en_vuelo_queda_anotado_para_que_el_apagado_lo_espere(cliente, monkeypatch):
    """Sin este registro el apagado no tiene a qué esperar, y el drenaje es decorativo."""
    visto: list[set[str]] = []
    empezado = asyncio.Event()

    async def atender_lento(m, **_kwargs):
        visto.append(set(runtime._EN_VUELO))
        empezado.set()
        return atencion.Atendido(wamid=m.wamid, id_conversacion="conv-1", respondido=True)

    async def procesar_falso(m, **_kwargs):
        return ingesta.Resultado(m.wamid, nuevo=True, reenviado=True)

    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_falso)
    monkeypatch.setattr(atencion, "atender", atender_lento)

    _enviar(cliente, _mensaje_de_texto("wamid.enVuelo"))

    assert visto == [{"wamid.enVuelo"}], "el turno corrió sin quedar anotado en _EN_VUELO"
    assert runtime._EN_VUELO == set(), "el wamid quedó pegado: cada apagado esperaría en vano"


def test_un_turno_que_revienta_tampoco_deja_el_wamid_pegado(cliente, espias):
    """El `finally` de `_rastreado`. `_entregar` promete no propagar, y este envoltorio no
    depende de esa promesa: un `wamid` colgado haría que TODOS los apagados siguientes se
    quedaran los 75 segundos completos esperando a algo que ya no existe."""
    espias.daniela_revienta = True

    _enviar(cliente, _mensaje_de_texto("wamid.revienta"))

    assert runtime._EN_VUELO == set()


def test_el_apagado_espera_a_que_el_turno_termine():
    """Lo que compra los segundos: mientras quede algo en vuelo, no se apaga."""
    runtime._EN_VUELO.add("wamid.lento")

    async def escenario():
        async def soltarlo_despues():
            await asyncio.sleep(0.3)
            runtime._EN_VUELO.discard("wamid.lento")

        asyncio.create_task(soltarlo_despues())
        empezo = time.monotonic()
        quedaron = await runtime._esperar_a_que_drenen(limite=5.0)
        return quedaron, time.monotonic() - empezo

    try:
        quedaron, tardo = asyncio.run(escenario())
    finally:
        runtime._EN_VUELO.clear()

    assert quedaron == 0, "se apagó dejando un turno a medias"
    assert tardo >= 0.25, "no esperó: devolvió antes de que el turno soltara el wamid"


def test_el_apagado_NO_espera_para_siempre():
    """Un turno colgado no puede impedir un despliegue: se espera, se avisa y se sigue."""
    runtime._EN_VUELO.add("wamid.colgado")
    try:
        quedaron = asyncio.run(runtime._esperar_a_que_drenen(limite=0.4))
    finally:
        runtime._EN_VUELO.clear()

    assert quedaron == 1, "el apagado se habría quedado esperando indefinidamente"


def test_el_barrido_de_arranque_NO_pasa_por_la_deduplicacion(monkeypatch):
    """La trampa de este arreglo, y la única forma de que sea un no-op silencioso.

    `_entregar` empieza por `procesar_mensaje`, que deduplica por `wamid`. Para un mensaje
    que ya se registró devolvería `nuevo=False` --con razón-- y se iría SIN correr el turno,
    que es justo la mitad que falta. Por eso el barrido llama a `atender` directo.

    Quien "simplifique" esto reutilizando `_entregar` deja el arreglo en nada, y en
    producción se vería igual que antes: el paciente sin respuesta.
    """
    atendidos: list[str] = []
    procesados: list[str] = []

    async def procesar_falso(m, **_kwargs):
        procesados.append(m.wamid)
        return ingesta.Resultado(m.wamid, nuevo=False, reenviado=True)

    async def atender_falso(m, **_kwargs):
        atendidos.append(m.wamid)
        return atencion.Atendido(wamid=m.wamid, id_conversacion="conv-1", respondido=True)

    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_falso)
    monkeypatch.setattr(atencion, "atender", atender_falso)
    monkeypatch.setattr(
        runtime.persistencia,
        "mensajes_sin_responder",
        lambda conn, **_kw: [
            {
                "wamid": "wamid.perdido",
                "telefono": "573001112233",
                "nombre_perfil": "Jean",
                "tipo": "text",
                "texto": "Sabes alguna cosa de unicornios?",
                "media_id": None,
                "mime": None,
                "recibido_en": datetime(2026, 9, 13, 21, 47),
            }
        ],
    )

    class ConexionFalsa:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(runtime.persistencia, "conectar", lambda *_a, **_k: ConexionFalsa())

    recogidos = asyncio.run(runtime._recoger_lo_que_quedo_sin_responder())

    assert recogidos == 1
    assert atendidos == ["wamid.perdido"], "el barrido no corrió el turno que faltaba"
    assert procesados == [], (
        "pasó por `procesar_mensaje`: la deduplicación lo habría descartado como reintento "
        "de Meta y el paciente seguiría sin respuesta"
    )
    assert runtime._EN_VUELO == set()

"""El relevo (fase 6C): el botón «Hablar yo con el paciente» y lo que pasa después.

Offline. Todo lo que habla con Neon en `relevo.py` son funciones sueltas de módulo llamadas
dentro de `asyncio.to_thread`, así que se doblan con `monkeypatch` y aquí no se abre una sola
conexión. El SQL de verdad lo ejercita `tests/test_relevo_neon.py`, que solo corre con
`-m neon`.

Lo que estas pruebas vigilan, en una frase: que **nunca quede Daniela callada con el tema
abierto sin que nadie esté mirando**, que es el único estado peligroso de esta fase.
"""

from __future__ import annotations

import asyncio

import pytest

from maxicare_daniela import relevo
from maxicare_daniela.canales import Telegram

CONV = "11111111-2222-3333-4444-555555555555"
TEL = "573001110101"
TEMA = 777
MENSAJE_DEL_GENERAL = 4242
URL = "postgresql://x"


# ==========================================================================================
# Dobles
# ==========================================================================================


class TelegramFalso:
    """Apunta todo lo que se le pide. No valida nada: eso lo hacen las aserciones."""

    def __init__(self, *, falla_al_crear_tema: bool = False) -> None:
        self.mensajes: list[tuple[str, int | None, bool, dict | None]] = []
        self.callbacks: list[tuple[str, str, bool]] = []
        self.reabiertos: list[int] = []
        self.cerrados: list[int] = []
        self.creados: list[str] = []
        self.teclados: list[tuple[int, dict | None]] = []
        self.reacciones: list[int] = []
        self.descargas: list[str] = []
        self._falla_al_crear_tema = falla_al_crear_tema

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.mensajes.append((texto, tema_id, silencioso, teclado))
        return 900 + len(self.mensajes)

    async def responder_callback(self, callback_id, texto="", *, alerta=False) -> None:
        self.callbacks.append((callback_id, texto, alerta))

    async def reabrir_tema(self, tema_id) -> None:
        self.reabiertos.append(tema_id)

    async def cerrar_tema(self, tema_id) -> None:
        self.cerrados.append(tema_id)

    async def crear_tema(self, nombre) -> int:
        if self._falla_al_crear_tema:
            raise RuntimeError("Telegram dijo que no")
        self.creados.append(nombre)
        return TEMA

    async def editar_teclado(self, mensaje_id, teclado=None) -> None:
        self.teclados.append((mensaje_id, teclado))

    async def reaccionar(self, mensaje_id, emoji="OK") -> None:
        self.reacciones.append(mensaje_id)

    async def descargar_archivo(self, file_id, *, nombre=None):
        self.descargas.append(file_id)
        return ArchivoFalso(nombre or "radiografia.jpg")

    def enlace_al_tema(self, tema_id) -> str:
        return f"https://t.me/c/99/{tema_id}"

    # -- ayudas de lectura ----------------------------------------------------------------

    def textos_en(self, tema_id: int | None) -> list[str]:
        return [t for t, destino, _, _ in self.mensajes if destino == tema_id]


class ArchivoFalso:
    def __init__(self, nombre: str) -> None:
        self.nombre = nombre
        self.contenido = b"bytes"
        self.mime = "image/jpeg"

    @property
    def tamano(self) -> int:
        return len(self.contenido)


class WhatsAppFalso:
    def __init__(self, *, revienta: bool = False) -> None:
        self.textos: list[tuple[str, str]] = []
        self.subidas: list[str] = []
        self.archivos: list[tuple[str, str, str, str | None]] = []
        self._revienta = revienta

    async def enviar_texto(self, telefono, texto) -> str:
        if self._revienta:
            raise RuntimeError("Meta devolvio 400")
        self.textos.append((telefono, texto))
        return f"wamid.{len(self.textos)}"

    async def subir_media(self, archivo) -> str:
        self.subidas.append(archivo.nombre)
        return "media-1"

    async def enviar_archivo(self, telefono, media_id, *, tipo, pie=None, nombre=None) -> str:
        self.archivos.append((telefono, media_id, tipo, pie))
        return "wamid.archivo"


@pytest.fixture(autouse=True)
def _sin_base(monkeypatch):
    """Nadie abre una conexión aquí. Los defaults son el caso normal: la conversación existe,
    el paciente ya tiene tema y nadie la tiene tomada todavía."""
    monkeypatch.setattr(relevo, "_telefono", lambda url, conv: TEL)
    monkeypatch.setattr(relevo, "_activar_en_base", lambda url, conv, doctor: None)
    monkeypatch.setattr(relevo, "_cerrar_en_base", lambda url, conv, motivo: True)
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: TEMA)
    monkeypatch.setattr(relevo, "_ficha_para_el_relevo", lambda url, tel: 55)
    monkeypatch.setattr(relevo, "_guardar_tema_abierto", lambda url, pid, tema: None)
    monkeypatch.setattr(relevo, "_marcar_abierto", lambda url, tel, abierto: None)
    monkeypatch.setattr(relevo, "_tocar", lambda url, conv: None)
    monkeypatch.setattr(relevo, "_marcar_escalamiento", lambda url, mid: None)
    monkeypatch.setattr(
        relevo,
        "_relevo_de_tema",
        lambda url, tema: {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
    )
    monkeypatch.setattr(relevo, "_activos", lambda url: [])
    # El aviso previo se recuerda en memoria; sin limpiarlo, una prueba contamina a la otra.
    relevo._avisados.clear()
    yield
    relevo._avisados.clear()


def _activar(tg, **cambios):
    argumentos = dict(
        id_conversacion=CONV,
        doctor="Dra. Ruiz",
        callback_id="cb-1",
        mensaje_id=MENSAJE_DEL_GENERAL,
        telegram=tg,
        database_url=URL,
    )
    argumentos.update(cambios)
    asyncio.run(relevo.activar(**argumentos))


def _mensaje_del_doctor(**cambios):
    base = {
        "message_id": 300,
        "message_thread_id": TEMA,
        "is_topic_message": True,
        "from": {"id": 9, "first_name": "Ruiz", "is_bot": False},
        "chat": {"id": -1001},
        "text": "Hola, soy el doctor. Ven manana a las 8.",
    }
    base.update(cambios)
    return base


# ==========================================================================================
# Activar
# ==========================================================================================


def test_el_boton_abre_el_hilo_y_lo_hace_sonar():
    """La escritura en el tema no es cortesía: es lo ÚNICO que lleva al doctor allí.

    `answerCallbackQuery(url=...)` hacia un tema del propio supergrupo responde
    `URL_INVALID` --comprobado contra la API--, así que el botón no puede navegar a nadie.
    Lo que navega es la notificación de este mensaje, y por eso tiene que ir SONANDO. Es el
    único sitio del proyecto donde un tema de paciente suena.
    """
    tg = TelegramFalso()

    _activar(tg)

    assert tg.reabiertos == [TEMA]
    en_el_tema = [m for m in tg.mensajes if m[1] == TEMA]
    assert len(en_el_tema) == 1, "el doctor no recibió nada en el hilo"
    texto, _, silencioso, teclado = en_el_tema[0]
    assert silencioso is False, "el aviso del relevo entró mudo: el doctor no llega al hilo"
    assert "Dra. Ruiz" in texto
    assert teclado is not None, "sin el botón de devolver, el relevo solo cierra por tiempo"
    assert relevo.PREFIJO_DEVOLVER in str(teclado)


def test_el_acuse_del_boton_sale_antes_de_que_telegram_lo_de_por_muerto():
    """Telegram da diez segundos para responder un `callback_query`. Sin acuse, el doctor ve
    el botón girando y lo vuelve a pulsar -- y la segunda pulsación es otra carrera."""
    tg = TelegramFalso()

    _activar(tg)

    assert tg.callbacks, "el botón se quedó girando"
    assert tg.callbacks[0][0] == "cb-1"


def test_el_segundo_doctor_se_entera_de_quien_la_tiene(monkeypatch):
    """El escalamiento suena a la vez en el teléfono de todos: dos pulsando con segundos de
    diferencia es lo normal, no lo raro. El segundo no puede quedarse creyendo que la tiene.
    """
    monkeypatch.setattr(relevo, "_activar_en_base", lambda url, conv, doctor: "Dr. Gómez")
    tg = TelegramFalso()

    _activar(tg, doctor="Dra. Ruiz")

    assert tg.callbacks and "Gómez" in tg.callbacks[0][1]
    assert tg.callbacks[0][2] is True, "un aviso que no es alerta pasa desapercibido"
    assert tg.reabiertos == [], "se reabrió el tema de una conversación que ya tenía dueño"
    assert tg.mensajes == [], "el segundo doctor escribió en un hilo que no es suyo"


def test_un_numero_sin_ficha_estrena_tema_y_ficha_PENDIENTE(monkeypatch):
    """El caso que decidía si el botón vale de algo: el paciente nuevo con dolor.

    `lectura.asegurar_tema` se niega a crear la ficha --con razón: un desconocido que manda
    una foto no puede volverse paciente verificado por mandarla-- y ese mismo número es el
    que más escala. Aquí lo autoriza un humano pulsando un botón, y el nombre va como
    `PENDIENTE` y NUNCA como el del perfil de WhatsApp: la ficha dice «este número existe»,
    no «esta persona se llama así».
    """
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: None)
    fichas: list[tuple[str, str]] = []

    def _ficha(url, telefono):
        fichas.append((url, telefono))
        return 55

    monkeypatch.setattr(relevo, "_ficha_para_el_relevo", _ficha)
    tg = TelegramFalso()

    _activar(tg)

    assert fichas == [(URL, TEL)]
    assert tg.creados, "no se le abrió hilo al paciente nuevo"
    assert TEL in tg.creados[0]
    # Nace ya abierto: `createForumTopic` lo deja así, al revés que el que abre un archivo.
    assert tg.reabiertos == [], "se reabrió un tema recién creado"
    assert relevo.NOMBRE_PENDIENTE == "PENDIENTE"


def test_el_general_deja_de_ofrecer_un_boton_que_ya_no_aplica():
    """Sin esto, el escalamiento sigue mostrando «Hablar yo con el paciente» para siempre y
    el siguiente doctor lo pulsa creyendo que nadie lo ha visto."""
    tg = TelegramFalso()

    _activar(tg)

    assert tg.teclados, "el botón del General se quedó como estaba"
    mensaje_id, teclado = tg.teclados[0]
    assert mensaje_id == MENSAJE_DEL_GENERAL
    boton = teclado["inline_keyboard"][0][0]
    assert "Dra. Ruiz" in boton["text"]
    # Un botón-enlace no dispara ningún `callback_query`: nadie puede volver a tomarla desde
    # aquí, que es justo lo que se quiere.
    assert boton["url"].endswith(f"/{TEMA}")
    assert "callback_data" not in boton


def test_si_el_hilo_no_se_puede_abrir_el_relevo_se_deshace(monkeypatch):
    """El peor estado posible de esta fase: Daniela callada frente a un paciente con el que
    nadie puede hablar. Si el hilo no sale, el relevo se deshace ENTERO."""
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: None)
    cierres: list[tuple[str, str]] = []

    def _cerrar(url, conv, motivo):
        cierres.append((conv, motivo))
        return True

    monkeypatch.setattr(relevo, "_cerrar_en_base", _cerrar)
    tg = TelegramFalso(falla_al_crear_tema=True)

    _activar(tg, tema_general=0)

    assert cierres == [(CONV, "tema_perdido")], "el relevo quedó activo sin hilo"
    assert tg.textos_en(0), "nadie se enteró de que el relevo no llegó a activarse"


# ==========================================================================================
# Doctor -> paciente
# ==========================================================================================


def test_lo_que_escribe_el_doctor_llega_literal():
    """Sin firma, sin prefijo y sin «te escribe el doctor»: quedó decidido que el paciente no
    se entera de que cambió el interlocutor."""
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(), telegram=tg, whatsapp=wa, database_url=URL
        )
    )

    assert wa.textos == [(TEL, "Hola, soy el doctor. Ven manana a las 8.")]
    assert tg.reacciones == [300], "el doctor se quedó sin saber si salió"


def test_una_foto_del_doctor_le_llega_al_paciente():
    """Telegram entrega las fotos como una LISTA de tamaños, de la miniatura al original.
    Mandarle al paciente la miniatura de 90 píxeles donde el doctor señaló algo no es
    entregar el mensaje: hay que coger el ÚLTIMO."""
    tg, wa = TelegramFalso(), WhatsAppFalso()
    mensaje = _mensaje_del_doctor(
        text=None,
        caption="Asi se ve tu muela",
        photo=[{"file_id": "chica"}, {"file_id": "mediana"}, {"file_id": "grande"}],
    )

    asyncio.run(relevo.relevar_mensaje(mensaje, telegram=tg, whatsapp=wa, database_url=URL))

    assert tg.descargas == ["grande"]
    assert wa.subidas == ["radiografia.jpg"]
    telefono, _, tipo, pie = wa.archivos[0]
    assert (telefono, tipo, pie) == (TEL, "image", "Asi se ve tu muela")
    assert wa.textos == [], "el pie se mandó además como texto suelto"


def test_una_nota_de_voz_sale_como_audio():
    """WhatsApp NO tiene un tipo `voice` al enviar: mandarlo así hace que Meta rechace el
    mensaje entero y el doctor cree que su nota salió."""
    tg, wa = TelegramFalso(), WhatsAppFalso()
    mensaje = _mensaje_del_doctor(text=None, voice={"file_id": "v1"})

    asyncio.run(relevo.relevar_mensaje(mensaje, telegram=tg, whatsapp=wa, database_url=URL))

    assert wa.archivos[0][2] == "audio"


def test_sin_relevo_vivo_no_se_reenvia_nada_y_se_dice(monkeypatch):
    """El hueco conocido: Telegram no le puede quitar privilegios al CREADOR del grupo, así
    que esa persona puede escribir dentro de un tema cerrado.

    No se cierra --no se puede-- pero se estrecha: pasa de «su mensaje llega al paciente sin
    que nadie lo sepa» a «no llega, y se le avisa en el propio hilo».
    """
    monkeypatch.setattr(relevo, "_relevo_de_tema", lambda url, tema: None)
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(), telegram=tg, whatsapp=wa, database_url=URL
        )
    )

    assert wa.textos == [], "llegó al paciente un mensaje escrito fuera de relevo"
    assert tg.textos_en(TEMA), "se tragó el mensaje sin decírselo a nadie"
    assert "NO le llegó" in tg.textos_en(TEMA)[0]


def test_lo_que_se_habla_en_el_general_no_sale_de_ahi():
    """El General es el sitio entero del diseño donde los doctores pueden discutir un caso
    sin que el paciente lea una palabra. Un mensaje sin `message_thread_id` es del General.
    """
    tg, wa = TelegramFalso(), WhatsAppFalso()
    mensaje = _mensaje_del_doctor(text="ojo que este ya vino por lo mismo")
    del mensaje["message_thread_id"]

    asyncio.run(relevo.relevar_mensaje(mensaje, telegram=tg, whatsapp=wa, database_url=URL))

    assert wa.textos == []
    assert tg.mensajes == [], "se contestó a algo que no había que contestar"


def test_los_mensajes_de_servicio_de_telegram_se_ignoran_en_silencio():
    """«Se creó el tema», «se cerró», «se reabrió» llegan como mensajes normales y sin texto.
    Avisar por cada uno llenaría el expediente de ruido -- y los dispara este mismo módulo.
    """
    tg, wa = TelegramFalso(), WhatsAppFalso()
    mensaje = _mensaje_del_doctor(text=None, forum_topic_reopened={})

    asyncio.run(relevo.relevar_mensaje(mensaje, telegram=tg, whatsapp=wa, database_url=URL))

    assert wa.textos == []
    assert tg.mensajes == []


def test_lo_que_escribe_otro_bot_no_se_reenvia():
    tg, wa = TelegramFalso(), WhatsAppFalso()
    mensaje = _mensaje_del_doctor(**{"from": {"id": 1, "is_bot": True}})

    asyncio.run(relevo.relevar_mensaje(mensaje, telegram=tg, whatsapp=wa, database_url=URL))

    assert wa.textos == []


def test_un_envio_que_falla_se_le_dice_al_doctor_a_la_cara():
    """Aquí NO vale una reacción discreta. El doctor acaba de darle una indicación clínica a
    alguien y tiene que saber que no salió, sin ir a buscarlo."""
    tg, wa = TelegramFalso(), WhatsAppFalso(revienta=True)

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(), telegram=tg, whatsapp=wa, database_url=URL
        )
    )

    avisos = tg.textos_en(TEMA)
    assert avisos and "NO le llegó" in avisos[0]
    assert "Vuelve a mandarlo" in avisos[0]
    assert tg.reacciones == [], "se dio por entregado algo que falló"


# ==========================================================================================
# Cerrar -- la puerta única
# ==========================================================================================


def _cerrar(tg, motivo="devuelto_por_doctor", **cambios):
    argumentos = dict(
        motivo=motivo, telegram=tg, database_url=URL, telefono=TEL, tema_id=TEMA
    )
    argumentos.update(cambios)
    return asyncio.run(relevo.cerrar(CONV, **argumentos))


def test_cerrar_devuelve_la_conversacion_y_vuelve_a_poner_el_candado():
    """Las dos cosas tienen que dejar de ser verdad a la vez: Daniela despierta Y el tema
    cerrado. Un tema que se queda abierto es un canal en vivo hacia el WhatsApp de una
    persona que ya nadie vigila."""
    tg = TelegramFalso()

    assert _cerrar(tg) is True
    assert tg.cerrados == [TEMA]
    despedidas = [m for m in tg.mensajes if m[1] == TEMA]
    assert despedidas and despedidas[0][2] is True, "la despedida sonó: el relevo ya terminó"


def test_cerrar_dos_veces_no_deja_dos_despedidas(monkeypatch):
    """Las salidas se solapan de verdad: el doctor pulsa «Listo» en el mismo minuto en que el
    barrido lo da por vencido."""
    monkeypatch.setattr(relevo, "_cerrar_en_base", lambda url, conv, motivo: False)
    tg = TelegramFalso()

    assert _cerrar(tg) is False
    assert tg.cerrados == []
    assert tg.mensajes == []


def test_cada_motivo_dice_algo_distinto():
    """`tiempo_agotado` significa que el paciente estuvo esperando; `devuelto_por_doctor`, no.
    Un texto único los haría indistinguibles justo para quien puede corregirlo."""
    textos = set()
    for motivo in ("devuelto_por_doctor", "tiempo_agotado", "tema_perdido"):
        tg = TelegramFalso()
        _cerrar(tg, motivo)
        textos.add(tg.textos_en(TEMA)[0])
    assert len(textos) == 3


def test_un_tema_perdido_se_anuncia_en_el_general():
    """Es el único motivo que señala un problema de USO y no de disponibilidad, y el único
    invisible: Telegram no emite ningún evento al borrar un tema. Si no se dice aquí, no se
    entera nadie."""
    tg = TelegramFalso()

    _cerrar(tg, "tema_perdido", tema_general=0)

    en_general = tg.textos_en(0)
    assert en_general and "Daniela retomó" in en_general[0]


def test_un_tema_que_no_se_deja_cerrar_no_tumba_el_cierre(monkeypatch):
    """Si `closeForumTopic` falla, lo que queda es un tema abierto con Daniela ya despierta:
    un hilo de más. Propagar dejaría lo contrario -- Daniela callada para siempre, que es un
    paciente sin nadie."""
    tg = TelegramFalso()

    async def revienta(tema_id):
        raise RuntimeError("Telegram caido")

    monkeypatch.setattr(tg, "cerrar_tema", revienta)

    assert _cerrar(tg) is True


# ==========================================================================================
# El barrido
# ==========================================================================================


def _activo(minutos: float) -> dict:
    return {
        "id_conversacion": CONV,
        "telefono": TEL,
        "doctor": "Dra. Ruiz",
        "topic_id": TEMA,
        "minutos_callado": minutos,
    }


def _barrer(tg, minutos, **cambios):
    argumentos = dict(
        telegram=tg, database_url=URL, cierre_minutos=180, aviso_minutos=120, tema_general=0
    )
    argumentos.update(cambios)
    return asyncio.run(relevo.barrer(**argumentos))


def test_el_barrido_no_toca_un_relevo_vivo(monkeypatch):
    monkeypatch.setattr(relevo, "_activos", lambda url: [_activo(5.0)])
    tg = TelegramFalso()

    assert _barrer(tg, 5.0) == 0
    assert tg.mensajes == []
    assert tg.cerrados == []


def test_el_barrido_avisa_antes_de_cerrar_y_ese_aviso_suena(monkeypatch):
    """Es lo único que puede rescatar una conversación que el doctor dejó a medias sin darse
    cuenta. Un aviso mudo dentro de un hilo que no está mirando no rescata nada."""
    monkeypatch.setattr(relevo, "_activos", lambda url: [_activo(130.0)])
    tg = TelegramFalso()

    assert _barrer(tg, 130.0) == 0
    assert tg.cerrados == [], "se cerró antes de tiempo"
    avisos = [m for m in tg.mensajes if m[1] == TEMA]
    assert avisos and avisos[0][2] is False, "el aviso de cierre entró mudo"
    assert "Daniela retoma" in avisos[0][0]


def test_el_aviso_no_se_repite_en_cada_barrido(monkeypatch):
    """El barrido corre cada minuto. Sin memoria, el doctor recibiría sesenta avisos por hora
    -- y a base de avisos que no piden nada se apagan las notificaciones del grupo entero."""
    monkeypatch.setattr(relevo, "_activos", lambda url: [_activo(130.0)])
    tg = TelegramFalso()

    _barrer(tg, 130.0)
    _barrer(tg, 130.0)
    _barrer(tg, 130.0)

    assert len([m for m in tg.mensajes if m[1] == TEMA]) == 1


def test_el_barrido_cierra_lo_que_se_paso_del_limite(monkeypatch):
    monkeypatch.setattr(relevo, "_activos", lambda url: [_activo(181.0)])
    tg = TelegramFalso()

    assert _barrer(tg, 181.0) == 1
    assert tg.cerrados == [TEMA]


def test_el_barrido_no_se_muere_si_la_base_no_responde(monkeypatch):
    """Lo llama una tarea de fondo que tiene que seguir viva mañana."""

    def revienta(url):
        raise RuntimeError("Neon caido")

    monkeypatch.setattr(relevo, "_activos", revienta)

    assert _barrer(TelegramFalso(), 0) == 0


# ==========================================================================================
# Detalles que se rompen solos
# ==========================================================================================


def test_el_enlace_al_hilo_le_quita_el_prefijo_100_al_chat():
    """`t.me/c/<id sin el -100>/<tema>`. Dejando el prefijo, el enlace no abre nada y el
    doctor se queda mirando un error."""
    tg = Telegram("token", "-1001234567890")

    assert tg.enlace_al_tema(55) == "https://t.me/c/1234567890/55"


def test_quien_pulsa_siempre_tiene_nombre():
    """Un «None tomó la conversación» en el hilo de un paciente es peor que un id feo."""
    assert relevo.nombre_de_quien_pulsa({"first_name": "Ana", "last_name": "Ruiz"}) == "Ana Ruiz"
    assert relevo.nombre_de_quien_pulsa({"username": "ruiz"}) == "@ruiz"
    assert relevo.nombre_de_quien_pulsa({"id": 7}) == "doctor 7"
    assert relevo.nombre_de_quien_pulsa(None)


def test_el_callback_data_no_lleva_el_telefono_de_nadie():
    """Telegram deja el `callback_data` VISIBLE para cualquiera del grupo y lo limita a 64
    bytes. Por eso el botón viaja con el uuid de la conversación: un UUID no le dice nada a
    nadie; un teléfono, sí."""
    datos = relevo.teclado_devolver(CONV)["inline_keyboard"][0][0]["callback_data"]

    assert TEL not in datos
    assert len(datos.encode()) <= 64


def test_una_respuesta_en_el_general_no_se_cuela_como_si_fuera_de_un_tema():
    """El id de un tema ES el id de un mensaje del mismo supergrupo --el del aviso de
    servicio que lo creó--, y responder dentro del General también rellena
    `message_thread_id`, con el id del mensaje al que se responde.

    Sin mirar `is_topic_message`, un doctor contestando en el General a un mensaje cuyo id
    coincidiera con el tema de algún paciente estaría escribiéndole a ese paciente.
    """
    tg, wa = TelegramFalso(), WhatsAppFalso()
    mensaje = _mensaje_del_doctor(text="ojo, este ya vino por lo mismo")
    del mensaje["is_topic_message"]

    asyncio.run(relevo.relevar_mensaje(mensaje, telegram=tg, whatsapp=wa, database_url=URL))

    assert wa.textos == []
    assert tg.mensajes == []

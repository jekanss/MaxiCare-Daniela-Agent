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
import json

import pytest

from maxicare_daniela import persistencia, relevo
from maxicare_daniela.canales import Telegram

CONV = "11111111-2222-3333-4444-555555555555"

#: Lo que se le inyectó a Daniela en la prueba en curso. Lo llena el doble de `_sin_base`.
avisos: list[tuple[str, str]] = []

#: Cada vez que un relevo activado queda contado en el informe de «sin resolver».
relevos_contados: list[str] = []
TEL = "573001110101"
TEMA = 777
MENSAJE_DEL_GENERAL = 4242
URL = "postgresql://x"


# ==========================================================================================
# Dobles
# ==========================================================================================


class TelegramFalso:
    """Apunta todo lo que se le pide. No valida nada: eso lo hacen las aserciones."""

    def __init__(
        self, *, falla_al_crear_tema: bool = False, estado_tema: str | None = "abierto"
    ) -> None:
        self.comprobados: list[int] = []
        self._estado_tema = estado_tema
        self.mensajes: list[tuple[str, int | None, bool, dict | None]] = []
        self.callbacks: list[tuple[str, str, bool]] = []
        self.reabiertos: list[int] = []
        self.cerrados: list[int] = []
        self.creados: list[str] = []
        self.teclados: list[tuple[int, dict | None]] = []
        self.reacciones: list[int] = []
        self.descargas: list[str] = []
        self.anclados: list[int] = []
        self.desanclados: list[int] = []
        #: Una sola linea de tiempo. Las listas de arriba dicen QUE paso; esta dice en que
        #: ORDEN, que es lo unico que distingue un enlace al hilo que aparece de inmediato de
        #: uno que aparece despues de volcar la transcripcion.
        self.orden: list[str] = []
        self._falla_al_crear_tema = falla_al_crear_tema

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.mensajes.append((texto, tema_id, silencioso, teclado))
        self.orden.append(f"mensaje:{tema_id}")
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
        self.orden.append(f"teclado:{mensaje_id}")

    async def reaccionar(self, mensaje_id, emoji="OK") -> None:
        self.reacciones.append(mensaje_id)

    async def anclar_mensaje(self, mensaje_id) -> None:
        self.anclados.append(mensaje_id)

    async def estado_del_tema(self, tema_id):
        """Por defecto «abierto»: es el caso normal. Las pruebas del tema borrado lo cambian
        a «borrado», las del `forum_topic_closed` perdido a «reabierto», y las del fallo de
        red a `None`."""
        self.comprobados.append(tema_id)
        return self._estado_tema

    async def desanclar_todo_del_tema(self, tema_id) -> None:
        self.desanclados.append(tema_id)

    async def descargar_archivo(self, file_id, *, nombre=None):
        self.descargas.append(file_id)
        return ArchivoFalso(nombre or "radiografia.jpg")

    def enlace_al_tema(self, tema_id, mensaje_id=None) -> str:
        if mensaje_id is not None:
            return f"https://t.me/c/99/{tema_id}/{mensaje_id}"
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


class ConexionFalsa:
    """Una conexión que no habla con Postgres: solo apunta el SQL que le mandan.

    Existe para UN caso concreto, y es el que costó el bug del hilo del paciente sin archivo:
    dejar correr de verdad una de las funciones que `_sin_base` dobla. Un doble no ejecuta ni
    la llamada que hay dentro, así que tapaba un `TypeError` garantizado en producción.

    No valida nada ni imita a `psycopg`. Si algún día hace falta comprobar lo que el SQL
    DEVUELVE, esto no sirve: eso es de `tests/test_relevo_neon.py`.
    """

    def __init__(self, escrituras: list[tuple[str, tuple | None]]) -> None:
        self.escrituras = escrituras

    def __enter__(self) -> "ConexionFalsa":
        return self

    def __exit__(self, *_) -> bool:
        return False

    def cursor(self) -> "ConexionFalsa":
        return self

    def execute(self, sql: str, parametros: tuple | None = None) -> None:
        self.escrituras.append((sql, parametros))

    def fetchone(self) -> tuple[int] | None:
        #: Lo mínimo para que `nombrar_si_esta_pendiente` --que hace `INSERT ... RETURNING
        #: id`-- pueda correr de verdad aquí. No imita a psycopg: dice «la fila se escribió».
        return (18,)

    def commit(self) -> None:
        pass


#: La función DE VERDAD, capturada al importar el módulo --antes de que ningún `monkeypatch`
#: la tape--. La prueba del hilo del paciente sin archivo la vuelve a poner en su sitio.
_GUARDAR_TEMA_ABIERTO_REAL = relevo._guardar_tema_abierto

#: Igual, y por lo mismo: `_sin_base` la dobla con un `lambda` que devuelve `True` sin tocar
#: nada, así que la prueba del nombre del cierre tiene que volver a ponerla en su sitio.
_NOMBRAR_REAL = relevo._nombrar


@pytest.fixture(autouse=True)
def _sin_base(monkeypatch):
    """Nadie abre una conexión aquí. Los defaults son el caso normal: la conversación existe,
    el paciente ya tiene tema y nadie la tiene tomada todavía."""
    monkeypatch.setattr(relevo, "_telefono", lambda url, conv: TEL)
    monkeypatch.setattr(relevo, "_activar_en_base", lambda url, conv, doctor: None)
    monkeypatch.setattr(relevo, "_cerrar_en_base", lambda url, conv, motivo: True)
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: TEMA)
    monkeypatch.setattr(relevo, "_guardar_tema_abierto", lambda url, tel, tema: None)
    monkeypatch.setattr(relevo, "_marcar_abierto", lambda url, tel, abierto: None)
    monkeypatch.setattr(relevo, "_tocar", lambda url, conv: None)
    monkeypatch.setattr(relevo, "_marcar_escalamiento", lambda url, mid: None)
    # El caso «humano:relevo» del informe. Es la unica escritura de `casos_sin_resolver` que
    # no sale de un turno, asi que tampoco sale de `atencion._anotar_resultado`.
    relevos_contados.clear()
    monkeypatch.setattr(
        relevo, "_registrar_relevo", lambda url: relevos_contados.append(url)
    )
    monkeypatch.setattr(
        relevo,
        "_relevo_de_tema",
        lambda url, tema: {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
    )
    monkeypatch.setattr(relevo, "_activos", lambda url: [])
    monkeypatch.setattr(relevo, "_transcripcion", lambda url, tel: [])
    monkeypatch.setattr(relevo, "_marcar_cierre", lambda url, conv, estado: None)
    monkeypatch.setattr(relevo, "_conversacion_de", lambda url, tel: CONV)
    monkeypatch.setattr(relevo, "_guardar_tratamiento", lambda url, conv, t: None)
    # Por defecto el numero YA tiene nombre, que es el caso normal: entonces el cierre son
    # dos preguntas. Las pruebas del paciente nuevo lo ponen en `None`.
    monkeypatch.setattr(relevo, "_nombre_del_paciente", lambda url, tel: "Ana Ruiz")
    monkeypatch.setattr(relevo, "_nombrar", lambda url, tel, nombre: True)
    # Sin doblarlo, las pruebas del hilo borrado abren una conexión de verdad a
    # `postgresql://x` y esperan a que el `except` la dé por perdida: cinco segundos de la
    # suite, repartidos donde nadie los busca.
    monkeypatch.setattr(relevo, "_olvidar_tema", lambda url, tel: True)

    # Lo que cruza el muro hacia Daniela. Doblado con un espía y NO con un `lambda` vacío:
    # varias pruebas de aquí comprueban QUÉ se le inyecta, y sin doblarlo `sesion_de_agente`
    # abre una conexión de verdad a `postgresql://x` -- 26 segundos de espera repartidos por
    # la suite antes de que el `except` la diera por perdida.
    avisos.clear()

    async def _avisar(url, conv, texto):
        avisos.append((conv, texto))

    monkeypatch.setattr(relevo, "_avisar_a_daniela", _avisar)
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
    assert en_el_tema, "el doctor no recibió nada en el hilo"
    texto, _, silencioso, teclado = en_el_tema[0]
    assert silencioso is False, "el aviso del relevo entró mudo: el doctor no llega al hilo"
    assert "Dra. Ruiz" in texto
    assert teclado is not None, "sin el botón de devolver, el relevo solo cierra por tiempo"
    assert relevo.PREFIJO_DEVOLVER in str(teclado)
    # El volcado de contexto va DEBAJO y mudo: lo primero que el doctor tiene que ver al
    # abrir la notificación es que la conversación es suya, no un muro de texto.
    assert len(en_el_tema) == 2 and en_el_tema[1][2] is True


def test_un_relevo_activado_queda_contado_en_el_informe():
    """Un doctor dejando lo que hacía es la señal más cara que produce el sistema, y la
    ÚNICA que no pasa por ningún turno: el relevo se abre desde Telegram, así que no hay
    `ctx.turno` donde acumularla ni `_anotar_resultado` que la vuelque. Sin esta escritura
    sería la única que no queda contada."""
    _activar(TelegramFalso())

    assert relevos_contados == [URL]


def test_si_el_informe_falla_el_doctor_se_queda_con_la_conversacion_igual(monkeypatch):
    """La instrumentación no puede impedir un relevo. Y va en su propio `try` aunque todo
    `activar` corra dentro de uno: el de fuera registra «falló la activación», y a estas
    alturas el relevo ESTÁ activo -- diría una falsedad en el log y se saltaría el
    `_avisados.discard` de después."""
    def revienta(url):
        raise RuntimeError("tabla sin migrar")

    monkeypatch.setattr(relevo, "_registrar_relevo", revienta)
    relevo._avisados.add(CONV)
    tg = TelegramFalso()

    _activar(tg)

    assert [m for m in tg.mensajes if m[1] == TEMA], "el hilo tiene que haberse abierto"
    assert CONV not in relevo._avisados, "el aviso previo se olvida igual"


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


def test_un_numero_sin_ficha_estrena_tema_y_NINGUNA_ficha(monkeypatch):
    """El relevo abre hilo y NO abre ficha. Esta prueba afirmaba lo contrario hasta hoy.

    ------------------------------------------------------------------------------------
    Por qué se invirtió, el 16/09/2026
    ------------------------------------------------------------------------------------

    La ficha `PENDIENTE` existía por una razón que caducó: hasta la migración 014 el hilo de
    Telegram colgaba de `pacientes.telegram_topic_id`, así que sin ficha no había dónde
    guardarlo. La 014 lo movió al TELÉFONO (`temas_telegram`) y la ficha se quedó por
    inercia -- el propio comentario del código decía ya que el id «no lo usa nadie».

    Lo que sí hacía era daño, y se midió en producción con una paciente de prueba: el relevo
    le dejó la ficha, el doctor cerró el hilo SIN agendar --así que nadie llegó a preguntarle
    el nombre, que es lo único que pisa el marcador-- y cuando Daniela retomó, la paciente
    había dejado de ser una desconocida. Dio su nombre, no coincidió con «PENDIENTE», gastó
    los dos intentos y Daniela escaló en vez de agendar. Ni desconocida --que puede pedir su
    primera cita-- ni verificada: el único hueco sin salida de los tres.

    Y no rompe el camino del doctor que SÍ agenda: `nombrar_si_esta_pendiente` crea la ficha
    ella misma si no hay ninguna. Lo fija
    `test_el_nombre_del_cierre_crea_la_ficha_si_el_relevo_ya_no_la_dejo`.
    """
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: None)
    # Si alguien vuelve a introducir la creación de la ficha, esto revienta la prueba en vez
    # de dejarla pasar en verde: no es un doble que la tolera, es una trampa.
    monkeypatch.setattr(
        persistencia,
        "asegurar_paciente",
        lambda *a, **k: pytest.fail("el relevo volvió a abrir una ficha en blanco"),
    )
    tg = TelegramFalso()

    _activar(tg)

    assert tg.creados, "no se le abrió hilo al paciente nuevo"
    assert TEL in tg.creados[0]
    # Nace ya abierto: `createForumTopic` lo deja así, al revés que el que abre un archivo.
    assert tg.reabiertos == [], "se reabrió un tema recién creado"


def test_el_hilo_del_paciente_sin_archivo_se_guarda_DE_VERDAD(monkeypatch):
    """El relevo de quien nunca mandó un archivo, con la escritura del hilo SIN doblar.

    ------------------------------------------------------------------------------------
    Por qué esta prueba tiene que correr la función real
    ------------------------------------------------------------------------------------

    `_guardar_tema_abierto` llamaba a `persistencia.guardar_tema(conn, id_paciente=...)`, y
    desde la migración 014 esa firma es `(conn, *, telefono, topic_id, abierto)`: el hilo se
    ata al TELÉFONO y no a la ficha. `TypeError` garantizado, en el único camino que lo pasa
    -- el del número que todavía no tiene hilo, o sea el que nunca mandó un archivo.

    Y ese es justo el paciente del que habla `_ficha_para_el_relevo`: el desconocido con dolor
    agudo, el que más escala. `activar` traga la excepción, deshace el relevo con
    `tema_perdido` y avisa al General de que «no se pudo abrir el hilo» -- dejando además un
    tema huérfano en Telegram por cada intento, porque `crear_tema` ya había corrido.

    No lo cazaba nadie porque `_sin_base` dobla `_guardar_tema_abierto` con un `lambda`, y un
    `lambda` no ejecuta la llamada de dentro. Por eso aquí se le devuelve la función real y se
    dobla un escalón más abajo, en la conexión.
    """
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: None)
    monkeypatch.setattr(relevo, "_guardar_tema_abierto", _GUARDAR_TEMA_ABIERTO_REAL)
    escrituras: list[tuple[str, tuple | None]] = []
    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa(escrituras))
    tg = TelegramFalso()

    _activar(tg)

    assert escrituras, "el hilo del paciente nuevo no se guardó en ninguna parte"
    sql, parametros = escrituras[0]
    assert "temas_telegram" in sql
    # El teléfono, no el id de la ficha. Y `abierto` en TRUE: `createForumTopic` lo deja así,
    # y la base tiene que decir la verdad o `cerrar` no sabría que hay un canal en vivo.
    assert parametros == (TEL, TEMA, True)
    assert tg.mensajes, "el doctor se quedó sin bienvenida: el relevo no llegó a activarse"


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
    # Tres tramos, `.../<tema>/<mensaje>`: ver `test_el_enlace_del_general_aterriza_DENTRO...`.
    assert f"/{TEMA}/" in boton["url"]
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


# ==========================================================================================
# 6D · El hilo ya no nace vacío
# ==========================================================================================


def test_al_tomarla_el_doctor_recibe_lo_que_se_hablo(monkeypatch):
    """El agujero del primer relevo real: el doctor entró a un hilo recién creado y no había
    nada. Los archivos del paciente estaban en el General y sus textos no se habían archivado
    en ninguna parte, así que tuvo que empezar preguntando quién era."""
    from datetime import datetime, timezone

    t = datetime(2026, 9, 14, 9, 26, tzinfo=timezone.utc)
    monkeypatch.setattr(
        relevo,
        "_transcripcion",
        lambda url, tel: [
            ("paciente", "hola, me duele una muela", t),
            ("daniela", "Hola, cuentame desde cuando", t),
            ("paciente", "desde ayer", t),
        ],
    )
    tg = TelegramFalso()

    _activar(tg)

    volcado = tg.textos_en(TEMA)[1]
    assert "me duele una muela" in volcado
    assert "desde ayer" in volcado
    assert "cuentame desde cuando" in volcado, "falta el lado de Daniela: es media conversación"


def test_sin_conversacion_previa_se_dice_en_vez_de_dejar_el_hilo_en_blanco():
    """Un hilo vacío hace pensar que el sistema falló. Decir que no hay nada es otra cosa."""
    tg = TelegramFalso()

    _activar(tg)

    assert "No hay conversación previa" in tg.textos_en(TEMA)[1]


def test_una_transcripcion_larga_se_recorta_en_vez_de_no_salir(monkeypatch):
    """Telegram RECHAZA un mensaje de más de 4096 caracteres. Sin recorte, el doctor no
    recibiría la transcripción entera -- recibiría nada."""
    from datetime import datetime, timezone

    t = datetime(2026, 9, 14, 9, 26, tzinfo=timezone.utc)
    monkeypatch.setattr(
        relevo, "_transcripcion", lambda url, tel: [("paciente", "x" * 200, t)] * 60
    )
    tg = TelegramFalso()

    _activar(tg)

    volcado = tg.textos_en(TEMA)[1]
    assert len(volcado) < 4096
    assert volcado.count("…") >= 1, "se recortó sin decir que se recortó"


# ==========================================================================================
# 6D · El cierre conversado y la cita del doctor
# ==========================================================================================


def _relevo_esperando_fecha(url, tema):
    return {
        "id_conversacion": CONV,
        "telefono": TEL,
        "doctor": "Dra. Ruiz",
        "cierre_pendiente": "esperando_fecha",
    }


def test_devolverla_pregunta_por_la_cita_antes_de_soltar():
    tg = TelegramFalso()

    asyncio.run(relevo.iniciar_cierre(CONV, telegram=tg, database_url=URL, tema_id=TEMA))

    texto, _, silencioso, teclado = tg.mensajes[0]
    assert "quedó agendada una cita" in texto
    assert silencioso is False, "la pregunta entró muda: el doctor no la ve y se cierra sola"
    botones = str(teclado)
    assert relevo.PREFIJO_SI_AGENDO in botones and relevo.PREFIJO_NO_AGENDO in botones
    # Y NO se ha cerrado todavía: Daniela tiene que seguir callada mientras se pregunta.
    assert tg.cerrados == []


def test_mientras_se_espera_la_fecha_el_mensaje_NO_le_llega_al_paciente(monkeypatch):
    """La razón por la que `cierre_pendiente` vive en la base y no en memoria: si el proceso
    reiniciara aquí, un «15/09 14:30» se le aparecería al paciente en su WhatsApp."""
    monkeypatch.setattr(relevo, "_relevo_de_tema", _relevo_esperando_fecha)
    agendadas = []

    def _agendar_falso(url, **kw):
        agendadas.append(kw["inicio"])
        return (True, "cita registrada")

    monkeypatch.setattr(relevo, "_agendar", _agendar_falso)
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(text="15/09 14:30"),
            telegram=tg,
            whatsapp=wa,
            database_url=URL,
            calendario=object(),
        )
    )

    assert wa.textos == [], "la fecha se le reenvió al paciente"
    assert len(agendadas) == 1 and agendadas[0].strftime("%d/%m %H:%M") == "15/09 14:30"
    assert tg.cerrados == [TEMA], "se agendó pero el relevo no se cerró"


def test_una_fecha_ilegible_se_repite_en_vez_de_adivinarse(monkeypatch):
    """Nunca adivina. Una fecha mal interpretada es un paciente presentándose a la hora
    equivocada, y eso es peor que pedirle al doctor que la escriba otra vez."""
    monkeypatch.setattr(relevo, "_relevo_de_tema", _relevo_esperando_fecha)
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(text="manana como a las 10"),
            telegram=tg,
            whatsapp=wa,
            database_url=URL,
            calendario=object(),
        )
    )

    assert "No entendí" in tg.textos_en(TEMA)[0]
    assert relevo.EJEMPLO_FECHA in tg.textos_en(TEMA)[0]
    assert tg.cerrados == [], "se cerró sin registrar la cita que el doctor prometió"


def test_una_hora_llena_no_agenda_y_no_cierra(monkeypatch):
    """El valor entero de esto: el doctor que acaba de prometer las 10:00 se entera AHORA de
    que ya están dadas, y no cuando se presenten dos pacientes a la vez."""
    monkeypatch.setattr(relevo, "_relevo_de_tema", _relevo_esperando_fecha)
    monkeypatch.setattr(
        relevo, "_agendar", lambda url, **kw: (False, "esa hora ya esta llena")
    )
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(text="15/09 14:30"),
            telegram=tg,
            whatsapp=wa,
            database_url=URL,
            calendario=object(),
        )
    )

    assert "llena" in tg.textos_en(TEMA)[0]
    assert tg.cerrados == [], "se soltó la conversación con la cita sin registrar"


@pytest.mark.parametrize(
    "escrito, esperado",
    [
        ("15/09 14:30", "15/09 14:30"),
        ("15/09/2027 08:00", "15/09 08:00"),
        ("2027-09-15 08:00", "15/09 08:00"),
        ("15.09 14:30", "15/09 14:30"),
    ],
)
def test_los_formatos_de_fecha_que_se_aceptan(escrito, esperado):
    from datetime import datetime

    from maxicare_daniela.contratos import ZONA_BOGOTA

    leida = relevo._leer_fecha(escrito, datetime(2026, 9, 14, 10, 0, tzinfo=ZONA_BOGOTA))

    assert leida is not None and leida.strftime("%d/%m %H:%M") == esperado


@pytest.mark.parametrize(
    "escrito", ["manana", "el martes", "14:30", "", "15/09", "99/99 10:00"]
)
def test_lo_que_NO_se_acepta_como_fecha(escrito):
    from datetime import datetime

    from maxicare_daniela.contratos import ZONA_BOGOTA

    assert relevo._leer_fecha(escrito, datetime(2026, 9, 14, 10, 0, tzinfo=ZONA_BOGOTA)) is None


def test_sin_ano_una_fecha_pasada_se_va_al_siguiente():
    """Un «02/01 09:00» escrito el 28 de diciembre es de enero del año que viene, no de hace
    once meses. Nadie agenda hacia atrás."""
    from datetime import datetime

    from maxicare_daniela.contratos import ZONA_BOGOTA

    leida = relevo._leer_fecha("02/01 09:00", datetime(2026, 12, 28, 10, 0, tzinfo=ZONA_BOGOTA))

    assert leida is not None and leida.year == 2027


# ==========================================================================================
# 6D · Lo que cruza el muro hacia Daniela
# ==========================================================================================


def test_al_cerrar_daniela_se_entera_de_que_hubo_un_relevo():
    """Sin esto retoma como si los últimos minutos no hubieran existido, que es lo que pasó
    en la prueba real: contestó por encima de lo que el doctor acababa de acordar."""
    _cerrar(TelegramFalso(), doctor="Dra. Ruiz")

    assert len(avisos) == 1
    conv, texto = avisos[0]
    assert conv == CONV
    assert texto.startswith("AVISO DEL SISTEMA")
    assert "Dra. Ruiz" in texto
    assert "NO sabes qué se dijeron" in texto


def test_la_cita_del_doctor_si_cruza_y_con_su_fecha():
    from datetime import datetime

    from maxicare_daniela.contratos import ZONA_BOGOTA

    cuando = datetime(2027, 9, 15, 14, 30, tzinfo=ZONA_BOGOTA)

    _cerrar(TelegramFalso(), cita=cuando, doctor="Dra. Ruiz")

    texto = avisos[0][1]
    assert "15/09/2027 a las 14:30" in texto
    assert "no la vuelvas a agendar" in texto


def test_lo_que_escribio_el_doctor_NUNCA_cruza():
    """EL MURO, y es la prueba que no se puede debilitar.

    `_avisar_a_daniela` escribe en el contexto del agente que le habla DIRECTAMENTE al
    paciente. Si por ahí pasara lo que escribió el doctor --literal o resumido-- una frase
    como «se ve una lesión periapical en el 46» acabaría en boca de Daniela. Que eso no pueda
    ocurrir es la decisión central del proyecto (`frontera-agentes.md`).
    """
    clinica = "se observa lesion periapical en el 46, hay que hacer endodoncia"
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_del_doctor(text=clinica), telegram=tg, whatsapp=wa, database_url=URL
        )
    )
    assert wa.textos == [(TEL, clinica)], "al paciente sí le llega literal, eso es el relevo"

    _cerrar(tg, doctor="Dra. Ruiz")

    inyectado = avisos[0][1].lower()
    for palabra in ("lesion", "periapical", "endodoncia", "46"):
        assert palabra not in inyectado, f"«{palabra}» cruzó hacia Daniela: el muro está roto"


def test_cerrar_apaga_siempre_el_dialogo_de_cierre(monkeypatch):
    """Si `cierre_pendiente` se quedara puesto, el próximo mensaje del doctor en ese hilo se
    leería como una fecha en vez de reenviarse al paciente."""
    estados = []
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )

    _cerrar(TelegramFalso(), "tiempo_agotado")

    assert estados == [None]


# ==========================================================================================
# 6D · El botón que ahora cuelga del aviso de archivos
# ==========================================================================================


def test_el_boton_del_aviso_de_archivos_viaja_con_el_telefono():
    """No hay uuid cuando ese aviso se manda: `ingesta.procesar_mensaje` corre ANTES de que
    `atencion.atender` cree la conversación. No es una fuga: el teléfono está escrito en
    claro en el texto de ese mismo aviso."""
    boton = relevo.teclado_tomar(TEL)["inline_keyboard"][0][0]

    assert boton["callback_data"] == f"{relevo.PREFIJO_TOMAR_TEL}{TEL}"
    assert len(boton["callback_data"].encode()) <= 64, "Telegram corta en 64 bytes"


# ==========================================================================================
# 6E · Que el hilo se pueda LEER, y que la salida este siempre a mano
# ==========================================================================================


def test_la_transcripcion_no_le_vuelca_al_doctor_el_JSON_de_Daniela(monkeypatch):
    """EL BUG DE LA CAPTURA del 14/09/2026, y es de los que solo se ven en produccion.

    `daniela` tiene `output_type`, asi que lo que el SDK guarda como contenido del item no es
    su frase: es el JSON entero de `RespuestaDaniela`. El doctor que tomaba la conversacion
    recibia en el hilo, como transcripcion, esto:

        {"mensaje_al_paciente":"Claro que si...","estado_oportunidad":"explorando",
         "barrera_detectada":"ninguna","requiere_escalamiento":true, ...}

    Ilegible, y con la telemetria comercial del sistema delante de quien solo quiere saber
    que le dijeron a su paciente.
    """
    crudo = json.dumps(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "mensaje_al_paciente": "Ya nos llego el archivo y el doctor lo revisara.",
                    "estado_oportunidad": "explorando",
                    "barrera_detectada": "ninguna",
                    "requiere_escalamiento": True,
                    "motivo_escalamiento": "archivo_recibido",
                    "fuera_de_alcance": False,
                }
            ),
        }
    )

    frase = persistencia._texto_de_daniela(crudo)

    assert frase == "Ya nos llego el archivo y el doctor lo revisara."
    for campo in (
        "estado_oportunidad",
        "barrera_detectada",
        "requiere_escalamiento",
        "motivo_escalamiento",
        "fuera_de_alcance",
    ):
        assert campo not in frase, f"{campo} se colo en el hilo del paciente"


def test_una_respuesta_en_texto_plano_sigue_saliendo_entera():
    """No todo lo que escribe el agente pasa por `output_type` --un guardrail puede responder
    en texto plano-- y una transcripcion con huecos es peor que una con una linea de mas."""
    crudo = json.dumps({"role": "assistant", "content": "Con gusto, te espero el martes."})

    assert persistencia._texto_de_daniela(crudo) == "Con gusto, te espero el martes."


def test_un_dict_que_no_es_respuesta_de_daniela_no_se_vuelca_crudo():
    """El arreglo no puede consistir en 'si parsea, imprimelo': eso es el bug otra vez."""
    crudo = json.dumps({"role": "assistant", "content": json.dumps({"otra_cosa": 1})})

    assert persistencia._texto_de_daniela(crudo) is None


def test_el_boton_de_salida_queda_anclado():
    """La segunda queja del relevo real: el boton viaja con la bienvenida, que es el PRIMER
    mensaje del hilo, y despues de veinte frases habia que subir hasta arriba del todo para
    devolver el control. El que no sube deja el relevo abierto hasta que lo corta el barrido
    --con Daniela callada tres horas."""
    tg = TelegramFalso()

    _activar(tg)

    # El id que devuelve el doble para el primer mensaje que se le manda.
    assert tg.anclados == [901], "la bienvenida con el boton no quedo anclada"
    texto, _, _, teclado = tg.mensajes[0]
    assert relevo.PREFIJO_DEVOLVER in str(teclado)


def test_al_cerrar_se_quita_el_anclaje():
    """Un boton anclado que ya no hace nada invita a pulsarlo y a creer que el relevo sigue
    vivo."""
    tg = TelegramFalso()

    _cerrar(tg)

    assert tg.desanclados == [TEMA]


def test_al_cerrar_queda_el_boton_de_volver_a_entrar():
    """Cerrar es un gesto de un toque, y el barrido lo hace SOLO: tiene que poder deshacerse
    con otro toque. Un tema cerrado no deja escribir, pero si deja pulsar un boton inline."""
    tg = TelegramFalso()

    _cerrar(tg)

    despedidas = [m for m in tg.mensajes if m[1] == TEMA and "Relevo cerrado" in m[0]]
    assert despedidas, "no se despidio en el hilo"
    assert relevo.PREFIJO_TOMAR_TEL in str(despedidas[-1][3]), (
        "sin el boton, volver a entrar obliga a salir a buscar el hilo al General"
    )


# ==========================================================================================
# 6E · Cerrar el hilo a mano ES devolver el control
# ==========================================================================================


def _cerrar_el_tema(tg, **cambios):
    mensaje = {
        "message_thread_id": TEMA,
        "forum_topic_closed": {},
        "chat": {"id": -100},
    }
    mensaje.update(cambios)
    return asyncio.run(
        relevo.cerrar_por_tema_cerrado(mensaje, telegram=tg, database_url=URL)
    )


def test_cerrar_el_hilo_a_mano_le_devuelve_la_conversacion_a_daniela():
    """EL ESTADO PROHIBIDO por el no negociable 15, y era alcanzable con un gesto natural:
    el doctor termina de hablar y cierra el hilo. Hasta hoy eso dejaba el tema cerrado --sin
    canal hacia el paciente-- con `tomada_por` todavia puesto: Daniela callada frente a
    alguien con quien ya nadie podia hablar, hasta que el barrido lo cortara."""
    tg = TelegramFalso()

    _cerrar_el_tema(tg)

    assert avisos, "Daniela no se entero de que la conversacion vuelve a ser suya"
    assert tg.desanclados == [TEMA]


def test_cerrar_un_hilo_sin_relevo_no_hace_nada(monkeypatch):
    """Es el caso NORMAL, no el raro: cuando `relevo.cerrar` cierra el tema, Telegram emite
    este mismo evento. Sin esto seria un bucle, y con una despedida repetida cada vuelta."""
    monkeypatch.setattr(relevo, "_relevo_de_tema", lambda url, tema: None)
    tg = TelegramFalso()

    _cerrar_el_tema(tg)

    assert tg.mensajes == []
    assert avisos == []


def test_un_evento_de_cierre_sin_tema_se_ignora():
    """Defensivo y barato: `message_thread_id` puede no venir, y sin el no hay nada que
    resolver. Reventar aqui dejaria el relevo abierto."""
    tg = TelegramFalso()

    _cerrar_el_tema(tg, message_thread_id=None)

    assert tg.mensajes == []


# ==========================================================================================
# 6F · De que es la cita (migracion 015)
# ==========================================================================================


def _mensaje_en_cierre(estado, texto, tratamiento=None, monkeypatch=None):
    """Un mensaje del doctor mientras el dialogo de cierre espera algo."""
    monkeypatch.setattr(
        relevo,
        "_relevo_de_tema",
        lambda url, tema: {
            "id_conversacion": CONV,
            "telefono": TEL,
            "doctor": "Dra. Ruiz",
            "cierre_pendiente": estado,
            "cierre_tratamiento": tratamiento,
        },
    )
    return _mensaje_del_doctor(text=texto)


def test_lo_que_escribe_el_doctor_como_tratamiento_NO_le_llega_al_paciente(monkeypatch):
    """Mismo motivo que la fecha: es la respuesta a una pregunta del bot, no un mensaje. Un
    «control post-operatorio» apareciendo solo en el WhatsApp de alguien es, como minimo,
    raro; con la frase equivocada, alarmante."""
    guardados = []
    monkeypatch.setattr(
        relevo, "_guardar_tratamiento", lambda url, conv, t: guardados.append(t)
    )
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_en_cierre("esperando_tratamiento", "Cordales", monkeypatch=monkeypatch),
            telegram=tg,
            whatsapp=wa,
            database_url=URL,
        )
    )

    assert wa.textos == [], "lo que contesto al bot se le reenvio al paciente"
    assert guardados == ["Cordales"]


def test_el_tratamiento_se_guarda_TAL_CUAL_lo_escribe_el_doctor(monkeypatch):
    """Decision explicita del cliente (14/09/2026): aqui NO se valida contra la lista viva de
    `tratamientos`. Es el unico sitio del proyecto donde `citas.tratamiento` acepta texto
    libre, y es a proposito -- el doctor que acaba de hablar con el paciente sabe de que es
    la cita mejor que un catalogo cerrado."""
    guardados = []
    monkeypatch.setattr(
        relevo, "_guardar_tratamiento", lambda url, conv, t: guardados.append(t)
    )

    asyncio.run(
        relevo.recibir_el_tratamiento(
            "Control post-operatorio de la cirugia del jueves",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=TelegramFalso(),
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert guardados == ["Control post-operatorio de la cirugia del jueves"]


def test_un_tratamiento_vacio_no_avanza_el_dialogo(monkeypatch):
    """Sigue esperando en vez de guardar una cadena vacia: `citas.tratamiento` es NOT NULL, y
    una cita sin de-que es justo el dato que esto existe para no perder."""
    guardados = []
    monkeypatch.setattr(
        relevo, "_guardar_tratamiento", lambda url, conv, t: guardados.append(t)
    )
    tg = TelegramFalso()

    asyncio.run(
        relevo.recibir_el_tratamiento(
            "   ",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=tg,
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert guardados == []
    assert any("No lei nada" in t or "No leí nada" in t for t in tg.textos_en(TEMA))


def test_el_tratamiento_se_recorta_para_que_el_evento_siga_siendo_legible(monkeypatch):
    """Va al titulo del evento de Google Calendar, que la clinica lee en una rejilla."""
    guardados = []
    monkeypatch.setattr(
        relevo, "_guardar_tratamiento", lambda url, conv, t: guardados.append(t)
    )

    asyncio.run(
        relevo.recibir_el_tratamiento(
            "x" * 500,
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=TelegramFalso(),
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert len(guardados[0]) == relevo.TOPE_TRATAMIENTO


def test_tras_decir_de_que_es_se_pide_la_fecha(monkeypatch):
    """El orden importa: de que es primero y fecha despues, para que la cita se cree de una
    vez con todo en vez de insertarla y corregirla."""
    estados = []
    monkeypatch.setattr(relevo, "_guardar_tratamiento", lambda url, conv, t: None)
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )
    tg = TelegramFalso()

    asyncio.run(
        relevo.recibir_el_tratamiento(
            "Cordales",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=tg,
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert estados == ["esperando_fecha"]
    assert any(relevo.EJEMPLO_FECHA in t for t in tg.textos_en(TEMA))


def test_la_cita_se_registra_con_lo_que_dijo_el_doctor(monkeypatch):
    """La prueba de que el dato viaja entero: del hilo a `citas.tratamiento`. Antes de la 015
    aqui iba `"valoracion"` fijo, que ademas no es ninguna de las catorce claves que la
    clinica tiene."""
    registradas = []
    monkeypatch.setattr(
        relevo,
        "_agendar",
        lambda url, **kw: (registradas.append(kw["tratamiento"]), (True, "listo"))[1],
    )
    monkeypatch.setattr(relevo, "_marcar_abierto", lambda url, tel, abierto: None)

    asyncio.run(
        relevo.recibir_la_fecha(
            "15/12 14:30",
            {
                "id_conversacion": CONV,
                "telefono": TEL,
                "doctor": "Dra. Ruiz",
                "cierre_tratamiento": "Control post-operatorio",
            },
            telegram=TelegramFalso(),
            database_url=URL,
            tema_id=TEMA,
            calendario=object(),
        )
    )

    assert registradas == ["Control post-operatorio"]


def test_sin_tratamiento_se_dice_que_no_se_sabe_en_vez_de_inventarlo(monkeypatch):
    """La red para un relevo empezado con la version de ayer, o un estado a medias. Regla
    dura 3: lo que no se sabe no se rellena con un valor plausible."""
    registradas = []
    monkeypatch.setattr(
        relevo,
        "_agendar",
        lambda url, **kw: (registradas.append(kw["tratamiento"]), (True, "listo"))[1],
    )
    monkeypatch.setattr(relevo, "_marcar_abierto", lambda url, tel, abierto: None)

    asyncio.run(
        relevo.recibir_la_fecha(
            "15/12 14:30",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=TelegramFalso(),
            database_url=URL,
            tema_id=TEMA,
            calendario=object(),
        )
    )

    assert registradas == ["Sin identificar"]
    assert "valoracion" not in registradas


# ==========================================================================================
# 6G · Alguien borro el hilo a mano
# ==========================================================================================


def _barrer_con_hilo(tg, monkeypatch, *, topic_id=TEMA, minutos=5):
    monkeypatch.setattr(
        relevo,
        "_activos",
        lambda url: [
            {
                "id_conversacion": CONV,
                "telefono": TEL,
                "topic_id": topic_id,
                "minutos_callado": minutos,
                "doctor": "Dra. Ruiz",
            }
        ],
    )
    return asyncio.run(
        relevo.barrer(
            telegram=tg,
            database_url=URL,
            cierre_minutos=180,
            aviso_minutos=120,
        )
    )


def test_si_borran_el_hilo_daniela_recupera_la_conversacion(monkeypatch):
    """EL AGUJERO que cierra esto, y era el peor de los cuatro: Telegram NO emite ningun
    evento al borrar un tema --al reves que cerrarlo-- asi que nadie se enteraba. El relevo
    seguia TOMADO: Daniela callada, el doctor sin hilo donde escribir, y el paciente
    escribiendo sin que le conteste nadie hasta que el barrido cortara por tiempo, tres horas
    despues."""
    monkeypatch.setattr(relevo, "_olvidar_tema", lambda url, tel: True)
    tg = TelegramFalso(estado_tema="borrado")

    cerrados = _barrer_con_hilo(tg, monkeypatch)

    assert cerrados == 1
    assert avisos, "Daniela no recupero la conversacion"


def test_el_hilo_muerto_se_OLVIDA_para_que_el_paciente_pueda_tener_otro(monkeypatch):
    """Si la fila se quedara apuntando a un `topic_id` muerto, `asegurar_tema` lo daria por
    bueno sin crear ninguno y CADA archivo futuro de esa persona fallaria al depositarse.
    Para siempre, y en silencio."""
    olvidados = []
    monkeypatch.setattr(
        relevo, "_olvidar_tema", lambda url, tel: olvidados.append(tel) or True
    )
    marcados = []
    monkeypatch.setattr(
        relevo, "_marcar_abierto", lambda url, tel, abierto: marcados.append(abierto)
    )

    _barrer_con_hilo(TelegramFalso(estado_tema="borrado"), monkeypatch)

    assert olvidados == [TEL]
    assert marcados == [], "se marco cerrado un hilo que ya no existe, en vez de olvidarlo"


def test_un_fallo_de_red_NO_cierra_un_relevo_vivo(monkeypatch):
    """`None` es «no se pudo saber», y es distinto de `False`. Cerrarle el relevo a un doctor
    que esta hablando porque Telegram tardo en contestar seria peor que esperar al siguiente
    barrido, que llega en un minuto."""
    monkeypatch.setattr(relevo, "_olvidar_tema", lambda url, tel: True)
    tg = TelegramFalso(estado_tema=None)

    cerrados = _barrer_con_hilo(tg, monkeypatch)

    assert cerrados == 0
    assert avisos == []


def test_un_relevo_con_el_hilo_vivo_sigue_su_curso(monkeypatch):
    """El caso normal, y la otra mitad de la prueba de arriba: comprobar el tema no puede
    convertirse en una forma nueva de cortarle el relevo a nadie."""
    tg = TelegramFalso(estado_tema="abierto")

    cerrados = _barrer_con_hilo(tg, monkeypatch)

    assert cerrados == 0
    assert tg.comprobados == [TEMA]
    assert avisos == []


def test_un_forum_topic_closed_PERDIDO_lo_recoge_el_barrido(monkeypatch):
    """La cuarta salida, llegando tarde.

    Si el bot esta caido cuando el doctor cierra el hilo, el `forum_topic_closed` se pierde
    --Telegram deja de reintentar-- y el relevo se queda con el tema cerrado: exactamente el
    estado que prohibe el no negociable 15. Nadie volveria a enterarse nunca, porque el evento
    no se repite.

    Por eso la sonda devuelve tres estados y no un booleano: «existe» no basta.
    """
    tg = TelegramFalso(estado_tema="reabierto")

    cerrados = _barrer_con_hilo(tg, monkeypatch)

    assert cerrados == 1
    assert tg.cerrados == [TEMA], (
        "la sonda reabrio el hilo para comprobarlo y nadie lo volvio a cerrar: queda un tema "
        "abierto sin relevo, por el que cualquiera del grupo le escribe al paciente"
    )


def test_el_hilo_que_estaba_cerrado_NO_se_olvida(monkeypatch):
    """La diferencia con `tema_perdido`: ese hilo existe y sigue siendo el expediente de esa
    persona. Olvidarlo le abriria uno nuevo con el siguiente archivo y partiria su historia en
    dos."""
    olvidados = []
    monkeypatch.setattr(
        relevo, "_olvidar_tema", lambda url, tel: olvidados.append(tel) or True
    )

    _barrer_con_hilo(TelegramFalso(estado_tema="reabierto"), monkeypatch)

    assert olvidados == []


def test_borrar_el_hilo_se_dice_en_el_GENERAL(monkeypatch):
    """Es el unico motivo de cierre que señala un problema de USO, y el unico invisible: si
    no se dice aqui, no se entera nadie de que alguien esta borrando expedientes."""
    monkeypatch.setattr(relevo, "_olvidar_tema", lambda url, tel: True)
    tg = TelegramFalso(estado_tema="borrado")

    _barrer_con_hilo(tg, monkeypatch)

    en_general = tg.textos_en(0)
    assert any("dejó de existir" in t or "dejo de existir" in t for t in en_general), (
        f"el General no se entero: {en_general}"
    )


# ==========================================================================================
# 6I · La sonda del tema, contra las respuestas REALES de la Bot API
# ==========================================================================================
#
# Estas cuatro respuestas estan copiadas de una medicion contra el grupo de verdad
# (14/09/2026), no inventadas. Lo que NINGUNA prueba de aqui puede demostrar es que Telegram
# siga respondiendo asi: eso solo lo ve `scripts/probar_relevo.py`, que crea un tema, lo
# borra y sondea los dos. La version anterior de la sonda --`editForumTopic` sin argumentos--
# habria pasado cualquier prueba offline que alguien escribiera, porque el doble responde lo
# que le digan; contra la API devolvia `ok: true` para un tema ya borrado.


class _RespuestaFalsa:
    def __init__(self, cuerpo: dict) -> None:
        self._cuerpo = cuerpo

    def json(self) -> dict:
        return self._cuerpo


class _ClienteFalso:
    """Un `httpx.AsyncClient` de mentira que apunta a donde se llamo y devuelve lo dado."""

    llamadas: list[tuple[str, dict]] = []

    def __init__(self, cuerpo: dict) -> None:
        self._cuerpo = cuerpo

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _ClienteFalso.llamadas.append((url, json or {}))
        return _RespuestaFalsa(self._cuerpo)


def _sondear(monkeypatch, cuerpo: dict) -> tuple[str | None, list]:
    from maxicare_daniela import canales

    _ClienteFalso.llamadas = []
    monkeypatch.setattr(canales.httpx, "AsyncClient", _ClienteFalso(cuerpo))
    salida = asyncio.run(Telegram("token", "-1001234567890").estado_del_tema(TEMA))
    return salida, _ClienteFalso.llamadas


@pytest.mark.parametrize(
    "cuerpo, esperado",
    [
        ({"ok": False, "error_code": 400, "description": "Bad Request: TOPIC_NOT_MODIFIED"},
         "abierto"),
        ({"ok": True, "result": True}, "reabierto"),
        ({"ok": False, "error_code": 400, "description": "Bad Request: TOPIC_ID_INVALID"},
         "borrado"),
        ({"ok": False, "error_code": 400, "description": "Bad Request: message thread not found"},
         "borrado"),
        ({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 5"},
         None),
    ],
)
def test_la_sonda_traduce_cada_respuesta_de_telegram(monkeypatch, cuerpo, esperado):
    salida, _ = _sondear(monkeypatch, cuerpo)

    assert salida == esperado, f"{cuerpo['description'] if not cuerpo['ok'] else 'ok'} -> {salida!r}"


def test_la_sonda_pregunta_con_reopenForumTopic_y_no_con_editForumTopic(monkeypatch):
    """EL FALLO, convertido en prueba.

    `editForumTopic` sin `name` ni `icon_custom_emoji_id` no cambia nada, y por eso mismo no
    valida el id: devuelve `ok: true` hasta para un tema borrado. Se eligio razonando sobre la
    API en vez de midiendola, y dejo el agujero del hilo borrado abierto entero durante un dia.

    De las cuatro candidatas medidas, `reopenForumTopic` es la unica que distingue SIN dejar
    mensajes de servicio en el hilo --a un barrido por minuto, cualquiera de las otras lo
    llenaria de basura-- y sin necesitar saber como se llama el tema, que pasarlo mal lo
    renombraria.
    """
    _, llamadas = _sondear(monkeypatch, {"ok": False, "description": "TOPIC_NOT_MODIFIED"})

    assert len(llamadas) == 1
    url, cuerpo = llamadas[0]
    assert url.endswith("/reopenForumTopic"), f"la sonda volvio a una que no valida: {url}"
    assert cuerpo == {"chat_id": "-1001234567890", "message_thread_id": TEMA}
    assert "name" not in cuerpo and "icon_custom_emoji_id" not in cuerpo


# ==========================================================================================
# 6K · Que pulsar el boton LLEVE al doctor al hilo
#
# La queja del 14/09/2026: «cuando el doctor le da el boton de atender la conversacion
# deberia llevarlo de una al topic del paciente, no esta pasando».
#
# Un boton de callback no puede navegar --`answerCallbackQuery(url=)` hacia un tema del
# propio supergrupo responde URL_INVALID, medido--, asi que solo hay dos caminos y aqui se
# comprueban los dos: la MENCION, que hace sonar el aviso aunque el grupo este silenciado, y
# el ENLACE del General, que tiene que estar puesto de inmediato y aterrizar dentro del hilo.
# ==========================================================================================


def test_la_bienvenida_MENCIONA_al_doctor_y_no_solo_lo_nombra():
    """Sin mencion, el aviso del hilo depende de como tenga cada uno sus notificaciones: quien
    tenga el grupo silenciado pulsa el boton y se queda donde estaba. Una mencion suena igual.
    """
    tg = TelegramFalso()

    _activar(tg, doctor_id=777)

    bienvenida = tg.mensajes[0][0]
    assert '<a href="tg://user?id=777">Dra. Ruiz</a>' in bienvenida


def test_sin_id_del_doctor_la_bienvenida_degrada_al_nombre_y_NO_a_un_enlace_roto():
    """Ese mensaje lleva el boton de salida del relevo. Un `<a href>` a medias lo rompe
    entero --Telegram rechaza el HTML mal formado-- y entonces no hay ni aviso ni boton."""
    tg = TelegramFalso()

    _activar(tg, doctor_id=None)

    bienvenida = tg.mensajes[0][0]
    assert "Dra. Ruiz" in bienvenida
    assert "tg://user" not in bienvenida and "<a " not in bienvenida


def test_el_nombre_del_doctor_se_ESCAPA_dentro_de_la_mencion():
    """El nombre se lo pone el doctor en Telegram: es texto de fuera. Un «Ana <3» sin escapar
    rompe el mensaje que lleva el boton de salida."""
    tg = TelegramFalso()

    _activar(tg, doctor="Ana <3", doctor_id=5)

    assert '<a href="tg://user?id=5">Ana &lt;3</a>' in tg.mensajes[0][0]


def test_el_enlace_del_general_aterriza_DENTRO_del_hilo():
    """`t.me/c/<chat>/<n>` es ambiguo en un foro: ese `<n>` es un id de MENSAJE. La forma de
    tres tramos dice hilo y posicion por separado, y deja al doctor en la bienvenida."""
    tg = TelegramFalso()

    _activar(tg)

    mensaje_id, teclado = tg.teclados[0]
    assert mensaje_id == MENSAJE_DEL_GENERAL
    # 901 es el id que el doble le da a la bienvenida, que es el primer mensaje que se manda.
    assert teclado["inline_keyboard"][0][0]["url"] == f"https://t.me/c/99/{TEMA}/901"


def test_el_enlace_al_hilo_se_pone_ANTES_de_volcar_la_transcripcion():
    """EL ARREGLO. Se ponia al final, despues de leer la base y mandar la transcripcion: el
    doctor que acababa de pulsar seguia viendo «Hablar yo con el paciente» durante esos
    segundos, sin ninguna puerta hacia el hilo. Si ademas el aviso no le llegaba, se quedaba
    en el General mirando el mismo boton de antes -- que es justo lo que se reporto."""
    tg = TelegramFalso()

    _activar(tg)

    assert tg.orden == [
        f"mensaje:{TEMA}",                   # la bienvenida
        f"teclado:{MENSAJE_DEL_GENERAL}",    # la puerta, en cuanto existe a donde apuntar
        f"mensaje:{TEMA}",                   # y solo entonces la transcripcion
    ], tg.orden


def test_el_acuse_del_boton_no_promete_una_navegacion_que_no_existe():
    """Decia «Listo. Te llevo a su hilo» y no llevaba a nadie. El doctor lo leia, no pasaba
    nada, y volvia a pulsar."""
    tg = TelegramFalso()

    _activar(tg)

    _, texto, _ = tg.callbacks[0]
    assert "te llevo" not in texto.lower()
    assert len(texto) <= 200, "Telegram rechaza la llamada entera si se pasa de 200"


def test_el_enlace_de_tres_tramos_tambien_le_quita_el_prefijo_100():
    tg = Telegram("token", "-1001234567890")

    assert tg.enlace_al_tema(55, 901) == "https://t.me/c/1234567890/55/901"


# ==========================================================================================
# 6L · La cita del relevo lleva NOMBRE
#
# «La cita deberia saber la fecha, la hora y el asunto (...) agenda en base de datos y tambien
# en google calendar como si el paciente la hubiera hecho» -- 14/09/2026.
#
# Lo que faltaba era el nombre. El numero que escribe por primera vez no tiene ficha, el
# relevo se la crea como PENDIENTE, y la cita entraba en la agenda de la clinica como
# «PENDIENTE - Cordales». Y ese es el caso NORMAL de una cita salida de un relevo: el paciente
# nuevo con dolor agudo es justo el que mas escala.
# ==========================================================================================


def _pedir_datos(tg):
    asyncio.run(
        relevo.pedir_los_datos_de_la_cita(
            CONV, telegram=tg, database_url=URL, tema_id=TEMA
        )
    )


def test_al_paciente_SIN_nombre_se_le_pregunta_antes_de_nada(monkeypatch):
    estados = []
    monkeypatch.setattr(relevo, "_nombre_del_paciente", lambda url, tel: None)
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )
    tg = TelegramFalso()

    _pedir_datos(tg)

    assert estados == ["esperando_nombre"]
    assert any("se llama" in t for t in tg.textos_en(TEMA))


def test_PENDIENTE_no_cuenta_como_nombre():
    """El marcador sobrevive aunque el relevo ya no lo escriba: quedan fichas viejas con el,
    y `_nombre_del_paciente` es lo que impide que «PENDIENTE - Cordales» llegue a la agenda.
    """
    assert relevo.NOMBRE_PENDIENTE == "PENDIENTE"
    assert relevo.NOMBRE_PENDIENTE == persistencia.NOMBRE_PENDIENTE, (
        "el marcador vive en UN sitio; dos copias es como se separan"
    )


def test_el_nombre_del_cierre_crea_la_ficha_si_el_relevo_ya_no_la_dejo(monkeypatch):
    """La otra mitad de quitar la ficha en blanco, y la razon por la que quitarla es seguro.

    Quien registra el nombre no es ni fue nunca el relevo: es quien AGENDA. Si agenda
    Daniela, `crear_cita` crea la ficha con el nombre que le dio el paciente; si agenda el
    doctor, el cierre le pregunta el nombre y lo escribe aqui. Sin la ficha en blanco delante,
    este camino tiene que CREARLA, no solo pisar un marcador.

    Es lo que hace `nombrar_si_esta_pendiente` con su `INSERT ... ON CONFLICT`, y esta prueba
    lo ejercita con la funcion real contra una conexion falsa: doblarla con un `lambda`
    dejaria pasar en verde justo el dia que alguien la cambie por un `UPDATE`.
    """
    escrituras: list[tuple[str, tuple | None]] = []
    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa(escrituras))

    escribio = _NOMBRAR_REAL(URL, TEL, "Sora Patricia Delgado")

    sql = " ".join(s for s, _ in escrituras)
    assert "INSERT INTO pacientes" in sql, "no crearia la ficha del que no tenia ninguna"
    assert "ON CONFLICT (telefono) DO UPDATE" in sql, "no pisaria un marcador ya existente"
    parametros = [p for _, p in escrituras if p]
    assert any("Sora Patricia Delgado" in p for p in parametros)
    assert any(relevo.NOMBRE_PENDIENTE in p for p in parametros), (
        "sin el marcador como condicion, pisaria el nombre de un paciente de verdad"
    )
    assert escribio is True


def test_al_paciente_que_YA_tiene_nombre_no_se_le_pregunta(monkeypatch):
    """El caso normal sigue siendo dos preguntas. Preguntar de mas un dato que ya se tiene es
    como se consigue que el doctor deje el dialogo a medias."""
    estados = []
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )
    tg = TelegramFalso()

    _pedir_datos(tg)

    assert estados == ["esperando_tratamiento"]


def test_si_la_base_no_contesta_se_sigue_sin_preguntar_el_nombre(monkeypatch):
    """Quedarse aqui dejaria la cita sin registrar, que es justo lo que este dialogo existe
    para no perder. Preguntar de mas es molesto; no agendar es una promesa rota."""
    def revienta(url, tel):
        raise RuntimeError("Neon caido")

    estados = []
    monkeypatch.setattr(relevo, "_nombre_del_paciente", revienta)
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )

    _pedir_datos(TelegramFalso())

    assert estados == ["esperando_tratamiento"]


def test_lo_que_escribe_el_doctor_como_nombre_NO_le_llega_al_paciente(monkeypatch):
    """Un «Maria Fernanda Rios» apareciendo solo en el WhatsApp de Maria Fernanda Rios."""
    nombrados = []
    monkeypatch.setattr(
        relevo, "_nombrar", lambda url, tel, nombre: nombrados.append((tel, nombre))
    )
    tg, wa = TelegramFalso(), WhatsAppFalso()

    asyncio.run(
        relevo.relevar_mensaje(
            _mensaje_en_cierre(
                "esperando_nombre", "Maria Fernanda Rios", monkeypatch=monkeypatch
            ),
            telegram=tg,
            whatsapp=wa,
            database_url=URL,
        )
    )

    assert wa.textos == [], "lo que contesto al bot se le reenvio al paciente"
    assert nombrados == [(TEL, "Maria Fernanda Rios")]


def test_tras_el_nombre_se_pide_de_que_es_la_cita(monkeypatch):
    estados = []
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )
    tg = TelegramFalso()

    asyncio.run(
        relevo.recibir_el_nombre(
            "Maria Fernanda Rios",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=tg,
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert estados == ["esperando_tratamiento"]


def test_un_nombre_vacio_no_avanza_el_dialogo(monkeypatch):
    nombrados = []
    monkeypatch.setattr(
        relevo, "_nombrar", lambda url, tel, nombre: nombrados.append(nombre)
    )
    estados = []
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )
    tg = TelegramFalso()

    asyncio.run(
        relevo.recibir_el_nombre(
            "   ",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=tg,
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert nombrados == [] and estados == []
    assert any("No lei nada" in t or "No leí nada" in t for t in tg.textos_en(TEMA))


def test_si_el_nombre_no_se_puede_guardar_el_cierre_SIGUE(monkeypatch):
    """Una cita con el nombre a medias vale muchisimo mas que ninguna cita: el cupo queda
    tomado y el evento existe. El nombre lo arregla un humano desde el panel."""
    def revienta(url, tel, nombre):
        raise RuntimeError("Neon caido")

    estados = []
    monkeypatch.setattr(relevo, "_nombrar", revienta)
    monkeypatch.setattr(
        relevo, "_marcar_cierre", lambda url, conv, estado: estados.append(estado)
    )

    asyncio.run(
        relevo.recibir_el_nombre(
            "Maria Fernanda Rios",
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=TelegramFalso(),
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert estados == ["esperando_tratamiento"]


def test_el_nombre_se_recorta_como_el_tratamiento(monkeypatch):
    """Comparten el titulo del evento de Google Calendar, que la clinica lee en una rejilla."""
    nombrados = []
    monkeypatch.setattr(
        relevo, "_nombrar", lambda url, tel, nombre: nombrados.append(nombre)
    )

    asyncio.run(
        relevo.recibir_el_nombre(
            "x" * 500,
            {"id_conversacion": CONV, "telefono": TEL, "doctor": "Dra. Ruiz"},
            telegram=TelegramFalso(),
            database_url=URL,
            tema_id=TEMA,
        )
    )

    assert len(nombrados[0]) == relevo.TOPE_NOMBRE

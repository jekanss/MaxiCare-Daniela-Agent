"""El comando `/clearstate`: reconocerlo, autorizarlo y borrar.

Sin red y sin base. La prueba que demuestra la garantía --que tras el reset Daniela se
comporta como en un primer contacto-- vive aquí abajo, con dobles; la que comprueba que el
orden de los DELETE es el que las claves foraneas permiten vive en `test_reseteo_neon.py`,
porque esa solo la puede contestar la base.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from maxicare_daniela import atencion, lectura, reseteo
from maxicare_daniela.canales import ErrorDeCanal

TEL = "573001234567"


# ==========================================================================================
# Reconocer el comando
# ==========================================================================================


@pytest.mark.parametrize(
    "texto",
    ["/clearstate", "  /clearstate  ", "/ClearState", "/CLEARSTATE\n"],
)
def test_reconoce_el_comando(texto):
    assert reseteo.es_comando(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        None,
        "",
        "   ",
        "hola",
        "quiero /clearstate",
        # Exacto y solo exacto. Un prefijo abriria la puerta a que una frase que empieza
        # igual borre la conversacion de alguien sin que esa persona lo haya pedido.
        "/clearstate ahora",
        "/clear",
        "clearstate",
    ],
)
def test_no_reconoce_nada_mas(texto):
    assert reseteo.es_comando(texto) is False


# ==========================================================================================
# Autorizar
# ==========================================================================================


def test_sin_lista_el_comando_no_existe():
    """El default es el seguro: `MAXICARE_TELEFONOS_PRUEBA` vacia deja el comando apagado.

    Esto es lo que hace que desplegar esto en produccion no abra una puerta. Quien quiera
    resetear un numero tiene que listarlo a proposito.
    """
    assert reseteo.autorizado("573001234567", ()) is False


def test_autoriza_solo_al_numero_listado():
    permitidos = ("573001234567",)

    assert reseteo.autorizado("573001234567", permitidos) is True
    assert reseteo.autorizado("573009999999", permitidos) is False


def test_la_lista_se_compara_por_digitos():
    """En el `.env` el numero se escribe como lo escribe una persona; en el webhook llega
    como lo manda Meta. Comparar las dos cadenas tal cual dejaria el comando sin funcionar
    por un `+` o un espacio, y el fallo seria mudo: el texto se lo tragaria Daniela."""
    permitidos = ("+57 300 123-4567",)

    assert reseteo.autorizado("573001234567", permitidos) is True


# ==========================================================================================
# Los dos eslabones de la garantia que no necesitan base
#
# El primero --que `_leer_estado` devuelve lo mismo que para un numero virgen-- solo lo
# puede contestar Postgres y vive en `test_reseteo_neon.py`. Estos dos cierran la cadena:
# con el estado igual, la sesion vacia y el bufer limpio, lo que recibe el modelo en el
# turno siguiente es lo mismo que recibiria en un primer contacto.
# ==========================================================================================


def test_una_conversacion_nueva_nace_con_la_sesion_vacia():
    """Por esto el reseteo no tiene que limpiar el historial del dialogo.

    `_sesiones` se indexa por `id_conversacion`. Borrada la conversacion, la siguiente nace
    con un UUID nuevo, y `_sesion_de` de un id que nunca se ha visto construye una sesion
    vacia. La vieja queda inalcanzable y se poda sola a las 24 h.
    """
    de_antes = atencion._sesion_de("conversacion-vieja", time.monotonic())
    asyncio.run(de_antes.add_items([{"role": "user", "content": "me llamo Ana"}]))
    assert asyncio.run(de_antes.get_items()) != []

    nueva = atencion._sesion_de("conversacion-nueva", time.monotonic())

    assert asyncio.run(nueva.get_items()) == []


def test_olvidar_saca_el_bufer_del_numero():
    """Un bufer que sobreviviera al reseteo metería los mensajes de la conversacion anterior
    en el primer turno de la nueva, y la garantia se caeria por el unico sitio que no es la
    base."""
    atencion._buferes[TEL] = "lo que sea"  # type: ignore[assignment]
    lectura._candados_de_tema[TEL] = asyncio.Lock()

    atencion.olvidar(TEL)

    assert TEL not in atencion._buferes
    assert TEL not in lectura._candados_de_tema


def test_olvidar_no_toca_el_bufer_del_vecino():
    atencion._buferes[TEL] = "mio"  # type: ignore[assignment]
    atencion._buferes["573009999999"] = "del vecino"  # type: ignore[assignment]

    atencion.olvidar(TEL)

    assert atencion._buferes["573009999999"] == "del vecino"
    del atencion._buferes["573009999999"]


# ==========================================================================================
# El orquestador: que lo de afuera se borre antes, y que falle como debe
# ==========================================================================================


class TelegramFalso:
    def __init__(self, *, revienta: bool = False) -> None:
        self.revienta = revienta
        self.mensajes_borrados: list[int] = []
        self.temas_borrados: list[int] = []

    async def borrar_mensaje(self, mensaje_id: int) -> None:
        if self.revienta:
            raise ErrorDeCanal("el bot no tiene can_delete_messages")
        self.mensajes_borrados.append(mensaje_id)

    async def borrar_tema(self, tema_id: int) -> None:
        if self.revienta:
            raise ErrorDeCanal("el bot no tiene can_delete_messages")
        self.temas_borrados.append(tema_id)


class CalendarioFalso:
    def __init__(self, *, revienta: bool = False) -> None:
        self.revienta = revienta
        self.eliminados: list[str] = []

    def eliminar_evento(self, evento_id: str) -> None:
        if self.revienta:
            raise RuntimeError("Google dijo que no")
        self.eliminados.append(evento_id)


def _montar(monkeypatch, *, eventos=("ev-1",), topic_id=77, mensajes=(900, 901)):
    """Dobla las dos funciones que tocan la base. Devuelve la lista de borrados registrados."""
    borrados: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(
        reseteo,
        "_rastro",
        lambda url, tel: {
            "eventos": list(eventos),
            "topic_id": topic_id,
            "mensajes_telegram": list(mensajes),
        },
    )

    def borrar(url, tel, conservar):
        borrados.append((url, tel, conservar))
        return {"pacientes": 1, "conversaciones": 1}

    monkeypatch.setattr(reseteo, "_borrar", borrar)
    return borrados


def test_borra_afuera_antes_que_la_base(monkeypatch):
    borrados = _montar(monkeypatch)
    telegram, calendario = TelegramFalso(), CalendarioFalso()

    borrado = asyncio.run(
        reseteo.resetear(
            TEL,
            database_url="postgres://falsa",
            telegram=telegram,
            calendario=calendario,
            conservar_wamid="wamid-del-comando",
        )
    )

    assert calendario.eliminados == ["ev-1"]
    assert telegram.temas_borrados == [77]
    assert telegram.mensajes_borrados == [900, 901]
    assert borrados == [("postgres://falsa", TEL, "wamid-del-comando")]
    assert borrado.eventos == 1
    assert borrado.tema_borrado is True
    assert borrado.filas == {"pacientes": 1, "conversaciones": 1}


def test_si_calendar_falla_no_se_borra_ni_una_fila(monkeypatch):
    """La decision mas importante del modulo, y la que va contra la comodidad.

    Un evento que no se pudo eliminar y una fila borrada dejan un cupo ocupado por nadie en
    el calendario de la clinica: un paciente REAL que no puede agendar a esa hora y nadie
    que pueda liberarla desde el sistema. Mientras las filas sigan ahi, el comando se puede
    repetir cuando Calendar vuelva.
    """
    borrados = _montar(monkeypatch)
    calendario = CalendarioFalso(revienta=True)

    with pytest.raises(reseteo.ErrorDeReseteo):
        asyncio.run(
            reseteo.resetear(
                TEL,
                database_url="postgres://falsa",
                telegram=TelegramFalso(),
                calendario=calendario,
            )
        )

    assert borrados == [], "se borro la base pese a que Calendar fallo"


def test_si_telegram_falla_el_borrado_sigue(monkeypatch):
    """La asimetria con Calendar: un tema huerfano es ruido en el grupo de los doctores, no
    un cupo bloqueado. No puede dejar sin resetear el numero."""
    borrados = _montar(monkeypatch)
    telegram = TelegramFalso(revienta=True)

    borrado = asyncio.run(
        reseteo.resetear(
            TEL,
            database_url="postgres://falsa",
            telegram=telegram,
            calendario=CalendarioFalso(),
        )
    )

    assert len(borrados) == 1, "un fallo de Telegram dejo el numero sin resetear"
    assert borrado.tema_borrado is False
    assert len(borrado.fallos) == 3  # dos mensajes y el tema
    assert "Telegram" in reseteo.confirmacion(borrado)


def test_tambien_limpia_la_base_del_chat_del_panel(monkeypatch):
    """`pruebas_web` tiene las mismas doce tablas y no se purga nunca. Sin esto, un numero
    que alguna vez se probo desde la interfaz web seguiria conocido por esa mitad."""
    borrados = _montar(monkeypatch)

    borrado = asyncio.run(
        reseteo.resetear(
            TEL,
            database_url="postgres://principal",
            telegram=TelegramFalso(),
            calendario=CalendarioFalso(),
            bases_extra=("postgres://pruebas_web",),
        )
    )

    assert [b[0] for b in borrados] == ["postgres://principal", "postgres://pruebas_web"]
    # Los conteos se suman: el usuario ve el total de lo que se borro, no el de una mitad.
    assert borrado.filas["pacientes"] == 2


def test_sin_calendario_no_se_intenta_borrar_nada_de_google(monkeypatch):
    """`_calendario` puede ser `None` si Google no arranco. Eso no puede impedir el reseteo:
    lo que no se pudo crear tampoco dejo eventos que borrar."""
    borrados = _montar(monkeypatch, eventos=())

    borrado = asyncio.run(
        reseteo.resetear(TEL, database_url="postgres://falsa", telegram=None, calendario=None)
    )

    assert len(borrados) == 1
    assert borrado.eventos == 0


# ==========================================================================================
# La confirmacion que recibe quien lo pidio
# ==========================================================================================


def test_un_numero_ya_limpio_tambien_recibe_respuesta():
    """Un comando destructivo que se queda mudo es peor que uno que falla: quien lo mando no
    sabe si borro, si no borro o si borro a medias."""
    texto = reseteo.confirmacion(reseteo.Borrado())

    assert "nada que borrar" in texto
    assert "como si no me conocieras" in texto


def test_la_confirmacion_dice_que_se_borro():
    borrado = reseteo.Borrado(filas={"pacientes": 1, "mensajes_entrantes": 12}, eventos=2)
    borrado.tema_borrado = True

    texto = reseteo.confirmacion(borrado)

    assert "12 en mensajes_entrantes" in texto
    assert "2 cita(s)" in texto
    assert "Telegram" in texto

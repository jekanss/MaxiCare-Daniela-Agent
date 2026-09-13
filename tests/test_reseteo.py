"""El comando `/clearstate`: reconocerlo, autorizarlo y borrar.

Sin red y sin base. La prueba que demuestra la garantía --que tras el reset Daniela se
comporta como en un primer contacto-- vive aquí abajo, con dobles; la que comprueba que el
orden de los DELETE es el que las claves foraneas permiten vive en `test_reseteo_neon.py`,
porque esa solo la puede contestar la base. La posición RELATIVA de `agent_sessions` frente
a `conversaciones` --que los `session_id` SON esos ids, y por eso tiene que borrarse antes--
se fija aquí abajo con una conexión de mentira que solo anota el SQL, sin tocar Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

from maxicare_daniela import atencion, config, lectura, persistencia, reseteo
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


def test_autoriza_a_varios_numeros():
    """Varios telefonos separados por coma: el caso normal, porque quien prueba suele tener
    mas de una linea --la suya y la de alguien de la clinica-- y estrenar una para cada
    prueba es justo lo que este comando existe para evitar."""
    permitidos = ("573001110001", "573001110002", "573001110003")

    assert reseteo.autorizado("573001110001", permitidos) is True
    assert reseteo.autorizado("573001110002", permitidos) is True
    assert reseteo.autorizado("573001110003", permitidos) is True
    assert reseteo.autorizado("573009999999", permitidos) is False


def test_la_variable_de_entorno_parte_por_comas(monkeypatch):
    """Con espacios alrededor y una coma de mas, que es como queda un `.env` editado a mano.

    `monkeypatch.setenv` y no `os.environ[...]`: pytest lo deshace al terminar la prueba. Una
    asignacion directa se quedaria fijada para toda la sesion y rompería a quien corra
    despues.
    """
    monkeypatch.setenv("MAXICARE_TELEFONOS_PRUEBA", " +57 300 111 0001 , 573001110002 ,")

    assert config._lista("MAXICARE_TELEFONOS_PRUEBA") == ("+57 300 111 0001", "573001110002")


def test_sin_la_variable_la_lista_queda_vacia(monkeypatch):
    monkeypatch.delenv("MAXICARE_TELEFONOS_PRUEBA", raising=False)

    assert config._lista("MAXICARE_TELEFONOS_PRUEBA") == ()


def test_la_lista_se_compara_por_digitos():
    """En el `.env` el numero se escribe como lo escribe una persona; en el webhook llega
    como lo manda Meta. Comparar las dos cadenas tal cual dejaria el comando sin funcionar
    por un `+` o un espacio, y el fallo seria mudo: el texto se lo tragaria Daniela."""
    permitidos = ("+57 300 123-4567",)

    assert reseteo.autorizado("573001234567", permitidos) is True


# ==========================================================================================
# El orden del borrado y el olvido de la memoria del proceso -- lo que no necesita base
#
# Que `_leer_estado` devuelve lo mismo que para un numero virgen, y que el historial del
# dialogo (en `agent_messages` desde la fase 7) queda de verdad borrado, solo lo puede
# contestar Postgres: eso vive en `test_reseteo_neon.py`. Lo que SÍ se puede fijar sin base
# es la POSICIÓN de las sentencias -- con una conexión que solo anota lo que se ejecuta -- y
# que el búfer en memoria se olvida. Las dos cierran la cadena junto con la prueba de Neon.
# ==========================================================================================


class _CursorQueRegistra:
    """Un cursor de mentira: anota el SQL que recibe y no toca ninguna base."""

    def __init__(self, ejecutadas: list[str]) -> None:
        self._ejecutadas = ejecutadas
        self.rowcount = 0

    def execute(self, sql, parametros=None) -> None:
        self._ejecutadas.append(sql)

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self) -> "_CursorQueRegistra":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _ConexionQueRegistra:
    """Una conexión de mentira que solo anota el SQL que `borrar_rastro` ejecuta, sin
    tocar ninguna base. Sirve para fijar el ORDEN de las sentencias, no su resultado --
    eso último solo lo puede contestar Postgres, en `test_reseteo_neon.py`."""

    def __init__(self) -> None:
        self.ejecutadas: list[str] = []

    def cursor(self) -> _CursorQueRegistra:
        return _CursorQueRegistra(self.ejecutadas)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_el_historial_se_borra_antes_que_las_conversaciones():
    """El orden no es estilo: los `session_id` SON los ids de las conversaciones. Al revés,
    el DELETE de `agent_sessions` no encontraría a qué apuntar y el historial se quedaría
    vivo, invisible, ligado a un número que el sistema dice no conocer."""
    conn = _ConexionQueRegistra()

    persistencia.borrar_rastro(conn, "573001112233")

    sentencias = [s.lower() for s in conn.ejecutadas]
    posicion_historial = next(i for i, s in enumerate(sentencias) if "agent_sessions" in s)
    posicion_conversaciones = next(
        i for i, s in enumerate(sentencias) if "delete from conversaciones" in s
    )

    assert posicion_historial < posicion_conversaciones


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

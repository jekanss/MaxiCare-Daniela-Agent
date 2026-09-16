"""El turno de WhatsApp: lo que pasa entre que llega un mensaje y sale una respuesta.

Offline y sin base. Aquí no corre `Runner.run` -- eso ya lo prueba `test_conversacion.py`
con el `ModeloGuionizado`; lo que se prueba aquí es el CABLEADO del turno: de dónde sale la
conversación, qué lleva el contexto, quién espera a quién, y qué pasa cuando algo falla.

Son funciones síncronas que llaman a `asyncio.run`, igual que `test_conversacion.py`: el
proyecto no tiene `pytest-asyncio` y añadirlo por unas pruebas sería una dependencia nueva
para lo que una línea resuelve. Tampoco hay `conftest.py`.

Dos reglas que estas pruebas respetan y conviene no romper:

* **Ninguna toca `os.environ`.** pytest importa todos los módulos de prueba antes de
  ejecutar ninguno, así que una variable puesta en el cuerpo de un módulo se la come otra
  prueba cualquiera. Todo va con `monkeypatch`, que pytest deshace solo.
* **`dormir` SIEMPRE se sustituye.** El retardo real es de 4 a 55 segundos por mensaje: una
  suite que los esperase de verdad es una suite que alguien acaba saltándose.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from maxicare_daniela import atencion, contratos, conversacion, ingesta, persistencia
from maxicare_daniela.calendario import CalendarioCaido, CalendarioDoble, ErrorDeCalendario
from maxicare_daniela.canales import ErrorDeCanal
from maxicare_daniela.config import MARGEN_LECTURA_SEGUNDOS, Config
from maxicare_daniela.contratos import LecturaNoClinica, RespuestaDaniela
from maxicare_daniela.sin_resolver import Senal

TELEFONO = "573001112233"
OTRO_TELEFONO = "573009998877"

CONFIGURACION_OPERATIVA = {
    "capacidad_por_hora": 3,
    "duracion_cita_minutos": 45,
    "cierre_relevo_minutos": 120,
    "telegram_topic_general": 7,
}

#: `contactos.creado_en`/`actualizado_en` son `TIMESTAMPTZ NOT NULL DEFAULT now()` (migración
#: 019): la base nunca los devuelve en `None`. Un doble que sí lo hiciera certificaría en
#: verde código que revienta contra una fila real -- lo que sí lee la Tarea 3.
FECHA_CONTACTO = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ==========================================================================================
# Dobles
# ==========================================================================================


def config_falso(**cambios) -> Config:
    """Un `Config` de verdad, no un `SimpleNamespace`.

    Si mañana alguien le añade un campo obligatorio, estas pruebas lo dicen en vez de pasar
    con un objeto que ya no se parece al que corre en producción.
    """
    campos = dict(
        database_url="postgresql://no-se-usa/na",
        whatsapp_token="token-wa",
        whatsapp_phone_number_id="123",
        whatsapp_verify_token="verify",
        whatsapp_app_secret="secreto",
        telegram_bot_token="token-telegram",
        telegram_chat_doctores="-100999",
        # Vacíos a propósito: con credenciales de Google, `calendario_desde_config` intentaría
        # hablar con Google de verdad. Las pruebas le pasan el calendario a mano.
        google_sa_b64="",
        google_calendar_id="",
        modelo_daniela="modelo-de-prueba",
        modelo_lector="modelo-de-prueba",
        modelo_evaluador="modelo-de-prueba",
        secreto_sesion="",
        permitir_cookie_insegura=False,
        daniela_responde=True,
    )
    campos.update(cambios)
    return Config(**campos)


class WhatsAppFalso:
    """Acumula lo que se envió. `enviar_texto` puede reventar si se le pide."""

    def __init__(self, *, falla_con: Exception | None = None) -> None:
        self.enviados: list[tuple[str, str]] = []
        self.leidos: list[str] = []
        self._falla_con = falla_con

    async def marcar_leido(self, wamid: str) -> None:
        self.leidos.append(wamid)

    async def enviar_texto(self, telefono: str, texto: str) -> str:
        if self._falla_con is not None:
            raise self._falla_con
        self.enviados.append((telefono, texto))
        return f"wamid-salida-{len(self.enviados)}"

    @property
    def textos(self) -> list[str]:
        return [texto for _, texto in self.enviados]


class TelegramFalso:
    def __init__(self) -> None:
        self.mensajes: list[str] = []

    async def enviar_mensaje(self, texto: str, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.mensajes.append(texto)
        return len(self.mensajes)


class DormirFalso:
    """Se queda con los segundos que le pidieron dormir, sin dormirlos."""

    def __init__(self) -> None:
        self.dormidas: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.dormidas.append(segundos)


class ConexionFalsa:
    """Imita lo único de psycopg que importa aquí: `autocommit=False`.

    Cuando una sentencia falla, Postgres deja la transacción **abortada** y cualquier
    sentencia siguiente revienta con `InFailedSqlTransaction` hasta que alguien haga
    `rollback()`. Un doble que se limite a lanzar la excepción pedida y siga tan campante
    deja pasar exactamente ese fallo, que es lo que le pasó a la primera versión de la
    prueba de la configuración: pasaba en verde mientras el código real se caía entero.
    """

    def __init__(self) -> None:
        self.abortada = False
        self.rollbacks = 0
        self.cerrada = False

    def comprobar(self) -> None:
        if self.abortada:
            raise RuntimeError(
                "InFailedSqlTransaction: current transaction is aborted, commands ignored "
                "until end of transaction block"
            )

    def abortar(self) -> None:
        self.abortada = True

    def rollback(self) -> None:
        self.rollbacks += 1
        self.abortada = False


class BaseFalsa:
    """Sustituye las funciones de `persistencia` que usa `atencion`, y anota cada llamada.

    No sustituye a `atencion._leer_estado`: si lo hiciera, las pruebas 2 y 3 --las que
    vigilan si se reutiliza o se abre una conversación-- no probarían nada.
    """

    def __init__(
        self,
        *,
        viva: tuple[str, int, bool, int] | None = None,
        paciente: tuple[int, str] | None = None,
        tomada: str | None = None,
        configuracion: dict | None = None,
        nueva: str = "conv-nueva",
        recordatorio: tuple[str, datetime] | None = None,
        caso_revienta: Exception | None = None,
        contacto: dict | Exception | None = None,
    ) -> None:
        self.viva = viva
        #: Lo que `_anotar_resultado` dejó en `casos_sin_resolver`, en orden. Cada elemento
        #: es el `dict` de argumentos con que se llamó a `persistencia.registrar_caso`.
        self.casos: list[dict] = []
        #: Para comprobar que un fallo de la instrumentación no puede tumbar un turno.
        self.caso_revienta = caso_revienta
        self.paciente = paciente
        self.tomada = tomada
        #: Lo que devuelve `persistencia.ultimo_recordatorio`: el par (tipo, cuándo) del
        #: último mensaje que el despachador le mandó a este paciente, o `None`.
        self.recordatorio = recordatorio
        self.configuracion = CONFIGURACION_OPERATIVA if configuracion is None else configuracion
        #: Lo que devuelve `persistencia.asegurar_contacto`: por defecto, un contacto recién
        #: nacido -- contactable y sin el aviso mostrado, que es lo que produce la fila en
        #: blanco de verdad. Una `Exception` simula el error de SQL que `_leer_estado`
        #: tiene que degradar.
        self.contacto = contacto
        self.nueva = nueva
        self.llamadas: list[tuple] = []
        #: Las conexiones que se abrieron, en orden. Sirven para comprobar el ruling 5 --que
        #: ninguna sigue abierta mientras corre el modelo-- y los rollbacks.
        self.conexiones: list[ConexionFalsa] = []

    @property
    def nombres(self) -> list[str]:
        return [llamada[0] for llamada in self.llamadas]

    def argumentos(self, nombre: str) -> tuple:
        for llamada in self.llamadas:
            if llamada[0] == nombre:
                return llamada[1:]
        raise AssertionError(f"nunca se llamó a {nombre}; se llamó a {self.nombres}")

    def instalar(self, monkeypatch) -> BaseFalsa:
        @contextlib.contextmanager
        def conectar(url):
            conexion = ConexionFalsa()
            self.conexiones.append(conexion)
            self.llamadas.append(("conectar", url))
            try:
                yield conexion
            finally:
                # El cierre se anota igual que la apertura: es la mitad que permite afirmar
                # que NINGUNA conexión sigue abierta mientras corre el modelo (ruling 5).
                conexion.cerrada = True
                self.llamadas.append(("cerrar", url))

        def anotar(nombre, *args):
            self.llamadas.append((nombre, *args))

        def conversacion_viva(conn, telefono, *, ventana_horas=24):
            conn.comprobar()
            anotar("conversacion_viva", telefono, ventana_horas)
            return self.viva

        def buscar_paciente_por_telefono(conn, telefono):
            conn.comprobar()
            anotar("buscar_paciente_por_telefono", telefono)
            return self.paciente

        def asegurar_contacto(conn, telefono):
            conn.comprobar()
            anotar("asegurar_contacto", telefono)
            if isinstance(self.contacto, Exception):
                # Un error de SQL de verdad aborta la transacción -- igual que
                # `leer_configuracion` unas líneas abajo. Sin este `abortar()`, la prueba de
                # la degradación pasaría por la razón equivocada.
                conn.abortar()
                raise self.contacto
            if self.contacto is not None:
                return dict(self.contacto)
            return {
                "telefono": telefono,
                "creado_en": FECHA_CONTACTO,
                "actualizado_en": FECHA_CONTACTO,
                "aviso_mostrado_en": None,
                "politica_version": None,
                "no_contactar": False,
                "no_contactar_en": None,
                "no_contactar_origen": None,
            }

        def asegurar_conversacion(conn, *, telefono, paciente_id=None, canal="whatsapp"):
            conn.comprobar()
            anotar("asegurar_conversacion", telefono, paciente_id, canal)
            # `nueva` puede ser una función del teléfono: dos números distintos NO pueden
            # recibir el mismo id de conversación, o el doble estaría fabricando justo la
            # colisión que la prueba 7 existe para descartar.
            return self.nueva(telefono) if callable(self.nueva) else self.nueva

        def leer_configuracion(conn):
            conn.comprobar()
            anotar("leer_configuracion")
            if isinstance(self.configuracion, Exception):
                # Un error de SQL de verdad aborta la transacción. Sin esta línea, la prueba
                # de la degradación pasaría por la razón equivocada.
                conn.abortar()
                raise self.configuracion
            return dict(self.configuracion)

        def conversacion_tomada(conn, id_conversacion):
            conn.comprobar()
            anotar("conversacion_tomada", id_conversacion)
            return self.tomada

        def ultimo_recordatorio(conn, telefono):
            # Por TELÉFONO, no por `id_conversacion`: el recordatorio de víspera sale más de
            # 24 h después de la conversación que agendó, así que el «sí, confirmo» del
            # paciente entra en una conversación nueva y la búsqueda por id devolvía `None`.
            conn.comprobar()
            anotar("ultimo_recordatorio", telefono)
            return self.recordatorio

        def ligar_mensaje_a_conversacion(conn, wamid, id_conversacion):
            conn.comprobar()
            anotar("ligar_mensaje_a_conversacion", wamid, id_conversacion)

        def tocar_conversacion(conn, id_conversacion, *, turno_actual=None):
            conn.comprobar()
            anotar("tocar_conversacion", id_conversacion, turno_actual)

        def marcar_respondido(conn, wamid, *, wamid_respuesta):
            conn.comprobar()
            anotar("marcar_respondido", wamid, wamid_respuesta)

        def marcar_fallo_respuesta(conn, wamid, *, motivo):
            conn.comprobar()
            anotar("marcar_fallo_respuesta", wamid, motivo)

        def registrar_caso(conn, *, huella, tipo, escalo=0, ejemplo=None, telefono=""):
            conn.comprobar()
            anotar("registrar_caso", huella, tipo, escalo, ejemplo, telefono)
            self.casos.append(
                {
                    "huella": huella, "tipo": tipo, "escalo": escalo,
                    "ejemplo": ejemplo, "telefono": telefono,
                }
            )
            if self.caso_revienta is not None:
                raise self.caso_revienta

        monkeypatch.setattr(persistencia, "conectar", conectar)
        monkeypatch.setattr(persistencia, "conversacion_viva", conversacion_viva)
        monkeypatch.setattr(
            persistencia, "buscar_paciente_por_telefono", buscar_paciente_por_telefono
        )
        monkeypatch.setattr(persistencia, "asegurar_contacto", asegurar_contacto)
        monkeypatch.setattr(persistencia, "asegurar_conversacion", asegurar_conversacion)
        monkeypatch.setattr(persistencia, "leer_configuracion", leer_configuracion)
        monkeypatch.setattr(persistencia, "conversacion_tomada", conversacion_tomada)
        monkeypatch.setattr(persistencia, "ultimo_recordatorio", ultimo_recordatorio)
        monkeypatch.setattr(
            persistencia, "ligar_mensaje_a_conversacion", ligar_mensaje_a_conversacion
        )
        monkeypatch.setattr(persistencia, "tocar_conversacion", tocar_conversacion)
        monkeypatch.setattr(persistencia, "marcar_respondido", marcar_respondido)
        monkeypatch.setattr(persistencia, "marcar_fallo_respuesta", marcar_fallo_respuesta)
        monkeypatch.setattr(persistencia, "registrar_caso", registrar_caso)
        return self


class Turnos:
    """El `conversacion.responder` falso: guarda lo que le llegó en cada llamada.

    Imita el contrato del de verdad --vaciar `DatosDelTurno` y contar el turno ANTES de
    correr--, y eso no es un adorno: si el doble no vaciara, la prueba del adjunto pasaría
    contra un turno que en producción llega con `hubo_adjunto` ya borrado.
    """

    def __init__(
        self,
        texto: str = "Claro que sí, con mucho gusto.",
        *,
        antes=None,
        revienta: Exception | None = None,
        escalado_por: str | None = None,
        tripwires: list[str] | None = None,
    ) -> None:
        self.texto = texto
        self.llamadas: list[dict] = []
        self._antes = antes
        self._revienta = revienta
        self._escalado_por = escalado_por
        #: Los guardrails que saltaron. El `Resultado` de verdad los trae llenos --
        #: `conversacion.responder` les hace `append` en las tres ramas de tripwire -- y son
        #: lo que convierte un guardrail en un caso del informe.
        self._tripwires = tripwires or []

    async def __call__(self, entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        ctx.turno.reiniciar()
        ctx.turno_actual += 1
        if self._antes is not None:
            await self._antes(ctx)
        if self._revienta is not None:
            raise self._revienta
        self.llamadas.append(
            {
                "entrada": entrada,
                "ctx": ctx,
                "sesion": sesion,
                "hubo_adjunto": ctx.turno.hubo_adjunto,
                "menciona_sintomas": ctx.turno.menciona_sintomas,
            }
        )
        return conversacion.Resultado(
            respuesta=RespuestaDaniela(
                mensaje_al_paciente=self.texto,
                estado_oportunidad="explorando",
                barrera_detectada="ninguna",
                requiere_escalamiento=False,
                motivo_escalamiento="ninguno",
                fuera_de_alcance=False,
            ),
            turno=ctx.turno_actual,
            escalado_por=self._escalado_por,  # type: ignore[arg-type]
            tripwires=list(self._tripwires),
        )

    @property
    def ctx(self):
        assert self.llamadas, "a `responder` no se le llamó ni una vez"
        return self.llamadas[-1]["ctx"]

    @property
    def entrada(self) -> str:
        assert self.llamadas, "a `responder` no se le llamó ni una vez"
        return self.llamadas[-1]["entrada"]


# ==========================================================================================
# Andamiaje
# ==========================================================================================


def limpiar_estado() -> None:
    """`_candados` es estado de módulo: sobrevive entre pruebas.

    Sin esto, una prueba pasa sola y falla dentro de la suite -- o al revés, que es peor.
    """
    atencion._candados.clear()
    atencion._buferes.clear()


def preparar(monkeypatch, base: BaseFalsa | None = None, turnos: Turnos | None = None):
    limpiar_estado()
    base = (base or BaseFalsa()).instalar(monkeypatch)
    turnos = turnos or Turnos()
    monkeypatch.setattr(conversacion, "responder", turnos)
    return base, turnos


def mensaje_texto(texto: str = "Hola, quiero información", **cambios) -> ingesta.MensajeEntrante:
    campos = dict(
        wamid="wamid-entrada-1",
        telefono=TELEFONO,
        nombre_perfil="Ana",
        tipo="text",
        texto=texto,
    )
    campos.update(cambios)
    return ingesta.MensajeEntrante(**campos)


async def _atender(mensaje, **cambios):
    argumentos = dict(
        whatsapp=WhatsAppFalso(),
        telegram=TelegramFalso(),
        config=config_falso(),
        calendario=CalendarioDoble(),
        dormir=DormirFalso(),
        # Sin ventana: el bufer no agrupa nada y el turno corre como antes. Lo que mide casi
        # toda esta suite es el turno, no el bufer; las pruebas del bufer pasan su ventana.
        ventana=0,
        tope=0,
        # La suite offline no toca Neon: `sesion_de` es lo único que decide quién construye
        # el historial, y aquí siempre es el doble en memoria.
        sesion_de=lambda id_: conversacion.SesionEnMemoria(id_),
    )
    argumentos.update(cambios)
    return await atencion.atender(mensaje, **argumentos)


def atender(mensaje, **cambios) -> atencion.Atendido:
    return asyncio.run(_atender(mensaje, **cambios))


# ==========================================================================================
# 1-3 · De dónde sale la conversación
# ==========================================================================================


def test_un_mensaje_de_texto_recibe_respuesta(monkeypatch):
    """El camino feliz. Lo que sale por WhatsApp es el `mensaje_al_paciente`, ni un resumen
    ni el objeto entero."""
    preparar(monkeypatch, turnos=Turnos("Hola Ana, claro que sí."))
    whatsapp = WhatsAppFalso()

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert whatsapp.textos == ["Hola Ana, claro que sí."]
    assert whatsapp.enviados[0][0] == TELEFONO
    assert resultado.respondido is True
    assert resultado.texto_enviado == "Hola Ana, claro que sí."
    assert resultado.motivo is None
    assert whatsapp.leidos == ["wamid-entrada-1"], "el doble check azul va al principio"


def test_la_conversacion_reciente_se_reutiliza(monkeypatch):
    """Sin esto, cada mensaje abriría una conversación nueva: Daniela no recordaría la frase
    anterior y las claves de idempotencia dejarían de colisionar con nada."""
    base, turnos = preparar(
        monkeypatch, base=BaseFalsa(viva=("conv-viva", 4, True, 1))
    )

    resultado = atender(mensaje_texto())

    assert "asegurar_conversacion" not in base.nombres
    assert resultado.id_conversacion == "conv-viva"
    assert turnos.ctx.id_conversacion == "conv-viva"
    assert turnos.ctx.intentos_identificacion == 1
    assert base.argumentos("conversacion_viva") == (
        TELEFONO,
        atencion.VENTANA_CONVERSACION_HORAS,
    )


def test_sin_conversacion_reciente_se_abre_una(monkeypatch):
    """El reverso del anterior: si no hay ninguna dentro de la ventana, hay que crearla, y
    con el paciente ya ligado si el número se reconoce."""
    base, turnos = preparar(
        monkeypatch, base=BaseFalsa(viva=None, paciente=(7, "Ana Restrepo"), nueva="conv-recien")
    )

    resultado = atender(mensaje_texto())

    assert base.argumentos("asegurar_conversacion") == (TELEFONO, 7, "whatsapp")
    assert resultado.id_conversacion == "conv-recien"
    assert turnos.ctx.id_conversacion == "conv-recien"
    assert base.argumentos("ligar_mensaje_a_conversacion") == (
        "wamid-entrada-1",
        "conv-recien",
    )


# ==========================================================================================
# 4-5 · Identidad
# ==========================================================================================


def test_un_numero_conocido_entra_identificado(monkeypatch):
    """«Si ese número ya está en Neon, la identidad queda verificada sin fricción y ese es el
    caso común» -- lo cerró el plan, no es una decisión de este módulo."""
    _, turnos = preparar(monkeypatch, base=BaseFalsa(paciente=(7, "Ana Restrepo")))

    atender(mensaje_texto())

    assert turnos.ctx.identidad_verificada is True
    assert turnos.ctx.id_paciente == 7
    assert turnos.ctx.nombre_paciente == "Ana Restrepo"
    assert turnos.ctx.telefono_sin_paciente is False


def test_un_numero_desconocido_no_entra_identificado(monkeypatch):
    """El falso positivo de la anterior.

    Sin esta, un `buscar_paciente_por_telefono` que devolviera siempre algo --o un
    `identidad_verificada=True` puesto a mano-- pasaría la prueba 4 sin que nada funcione.
    El nombre del perfil de WhatsApp NO cuenta como identidad: lo escribe el propio
    desconocido.
    """
    _, turnos = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 2, False, 0), paciente=None),
    )

    atender(mensaje_texto(nombre_perfil="Ana Restrepo"))

    assert turnos.ctx.identidad_verificada is False
    assert turnos.ctx.id_paciente is None
    assert turnos.ctx.nombre_paciente is None
    # El cable que permite su PRIMERA cita. Sin esta línea, `_leer_estado` podría dejar de
    # calcularlo --se quedaría en el default `False` del dataclass-- y volvería el fallo del
    # 13/09/2026: un paciente nuevo bloqueado por `identidad_antes_de_datos`, con la suite
    # entera en verde. No es lo mismo que `identidad_verificada`: ese dice «no sabemos quién
    # es», este dice «la clínica no lo conoce».
    assert turnos.ctx.telefono_sin_paciente is True


# ==========================================================================================
# 6-7 · El candado
# ==========================================================================================


def test_dos_mensajes_del_mismo_telefono_se_serializan(monkeypatch):
    """Dos mensajes seguidos del mismo paciente no pueden correr a la vez.

    La secuencia tiene que ser `entra A, sale A, entra B, sale B`. Solapadas, las dos
    corridas comparten el historial y el número de turno: la clave de idempotencia deja de
    proteger y el paciente puede acabar con dos respuestas cruzadas.
    """
    orden: list[str] = []
    limpiar_estado()
    BaseFalsa(viva=("conv-viva", 1, True, 0)).instalar(monkeypatch)
    dentro = {"cuantas": 0}
    solapadas = asyncio.Event()

    async def responder(entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        ctx.turno.reiniciar()
        ctx.turno_actual += 1
        orden.append(f"entra {entrada}")
        dentro["cuantas"] += 1
        if dentro["cuantas"] > 1:
            solapadas.set()
        # Un cuarto de segundo de reloj de verdad esperando a la otra corrida. No es un
        # `sleep(0)`: ceder el bucle unas cuantas veces NO basta, porque la lectura de base
        # va en un `to_thread` y su resultado puede tardar más vueltas que las que se cedan
        # -- el turno entero acaba antes de que el otro despierte y la prueba pasa sin
        # candado. Esperar de verdad le da a la otra corrida todas las oportunidades.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(solapadas.wait(), timeout=0.25)
        dentro["cuantas"] -= 1
        orden.append(f"sale {entrada}")
        return conversacion.Resultado(
            respuesta=RespuestaDaniela(
                mensaje_al_paciente=f"respuesta a {entrada}",
                estado_oportunidad="explorando",
                barrera_detectada="ninguna",
                requiere_escalamiento=False,
                motivo_escalamiento="ninguno",
                fuera_de_alcance=False,
            ),
            turno=ctx.turno_actual,
        )

    monkeypatch.setattr(conversacion, "responder", responder)

    async def correr():
        await asyncio.gather(
            _atender(mensaje_texto("A", wamid="wamid-a")),
            _atender(mensaje_texto("B", wamid="wamid-b")),
        )

    asyncio.run(asyncio.wait_for(correr(), timeout=10))

    assert orden in (
        ["entra A", "sale A", "entra B", "sale B"],
        ["entra B", "sale B", "entra A", "sale A"],
    ), f"las dos corridas se solaparon: {orden}"


def _respuesta_simple(ctx, texto: str = "ok") -> conversacion.Resultado:
    return conversacion.Resultado(
        respuesta=RespuestaDaniela(
            mensaje_al_paciente=texto,
            estado_oportunidad="explorando",
            barrera_detectada="ninguna",
            requiere_escalamiento=False,
            motivo_escalamiento="ninguno",
            fuera_de_alcance=False,
        ),
        turno=ctx.turno_actual,
    )


def test_dos_mensajes_a_la_vez_de_un_numero_NUEVO_abren_UNA_sola_conversacion(monkeypatch):
    """El primer contacto es donde el candado por conversación no podía proteger nada.

    El id de la conversación SALE DE LA BASE, así que con el candado indexado por ese id
    había que leer antes de cerrarlo: dos mensajes simultáneos de un número nuevo leían los
    dos «no hay conversación viva» y cada uno abría la suya. Daniela contestaba dos veces sin
    saber de la otra mitad --y podía contradecirse-- y del tercer mensaje en adelante
    `conversacion_viva` elegía una de las dos y la otra mitad del hilo se perdía.

    Y es el escenario que abre el docstring del módulo («un paciente no manda un mensaje:
    manda tres seguidos») en el momento de más valor: alguien que escribe por primera vez.

    El candado por TELÉFONO lo cierra porque el teléfono viene en el mensaje: se conoce antes
    de tocar la base, así que la lectura cabe dentro.
    """
    limpiar_estado()
    contador = itertools.count(1)
    # Un id distinto por llamada: con uno fijo, dos conversaciones se verían como una y la
    # prueba pasaría sin comprobar nada.
    base = BaseFalsa(viva=None, nueva=lambda tel: f"conv-{next(contador)}").instalar(monkeypatch)

    # La ventana de la carrera es diminuta --lo que hay entre `conversacion_viva` y
    # `asegurar_conversacion`, dentro del MISMO `to_thread`-- así que dejarla al azar del
    # planificador hace una prueba que pasa por suerte: comprobado, sin el arreglo pasaba
    # igual la mitad de las veces. Esta barrera la fuerza.
    #
    # Es `threading` y no `asyncio` porque `_leer_estado` corre en un hilo. Y tiene timeout:
    # CON el candado la segunda lectura no llega nunca a la vez --ese es el arreglo-- así
    # que la barrera se rompe sola a los 0,3 s y las dos siguen su camino. Sin el candado se
    # cruzan, las dos leen «no hay conversación» y cada una abre la suya.
    #
    # La barrera va DESPUÉS de leer y no antes, y la diferencia es la prueba entera:
    # soltando a los dos hilos ANTES de la lectura, el primero llegaba igualmente a crear la
    # conversación antes de que el segundo leyera, y la prueba pasaba con el fallo puesto
    # --medido--. Puesta aquí, ninguno puede crear nada hasta que los dos hayan leído, que
    # es exactamente la carrera que se quiere reproducir.
    barrera = threading.Barrier(2, timeout=0.3)
    viva_original = persistencia.conversacion_viva

    def viva_sincronizada(conn, telefono, *, ventana_horas=24):
        leido = viva_original(conn, telefono, ventana_horas=ventana_horas)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrera.wait()
        return leido

    monkeypatch.setattr(persistencia, "conversacion_viva", viva_sincronizada)

    asegurar_original = persistencia.asegurar_conversacion

    def asegurar_y_existir(conn, *, telefono, paciente_id=None, canal="whatsapp"):
        id_conv = asegurar_original(conn, telefono=telefono, paciente_id=paciente_id, canal=canal)
        # A partir de aquí la conversación EXISTE, que es lo que haría Neon. Sin esto el
        # doble no podría distinguir el arreglo del fallo.
        base.viva = (id_conv, 0, False, 0)
        return id_conv

    monkeypatch.setattr(persistencia, "asegurar_conversacion", asegurar_y_existir)

    vistas: list[str] = []

    async def responder(entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        ctx.turno.reiniciar()
        ctx.turno_actual += 1
        vistas.append(ctx.id_conversacion)
        # Ceder el bucle: le da a la otra corrida la oportunidad de colarse si el candado
        # no la está reteniendo.
        for _ in range(5):
            await asyncio.sleep(0)
        return _respuesta_simple(ctx)

    monkeypatch.setattr(conversacion, "responder", responder)

    async def correr():
        await asyncio.gather(
            _atender(mensaje_texto("A", wamid="wamid-a")),
            _atender(mensaje_texto("B", wamid="wamid-b")),
        )

    asyncio.run(asyncio.wait_for(correr(), timeout=10))

    assert base.nombres.count("asegurar_conversacion") == 1, (
        "se abrieron dos conversaciones para el mismo número: la mitad del hilo se pierde"
    )
    assert vistas[0] == vistas[1], "los dos turnos corrieron sobre conversaciones distintas"


def test_dos_mensajes_a_la_vez_de_una_conversacion_VIVA_leen_turnos_distintos(monkeypatch):
    """La otra cara del mismo hueco, y la que le cuesta un aviso al doctor.

    Con la lectura fuera del candado, los dos mensajes leían el mismo `turno_actual` y
    armaban la misma clave `conv-x:escalamiento:N`. El turno A escala por dolor, el turno B
    escala por otra cosa, `insertar_escalamiento` descarta el segundo como duplicado, y el
    doctor se entera de UNO SOLO.

    Es palabra por palabra lo que el docstring del módulo dice que pasa *sin* candado.
    """
    limpiar_estado()
    base = BaseFalsa(viva=("conv-x", 5, True, 0)).instalar(monkeypatch)

    tocar_original = persistencia.tocar_conversacion

    def tocar_y_persistir(conn, id_conversacion, *, turno_actual=None):
        tocar_original(conn, id_conversacion, turno_actual=turno_actual)
        if turno_actual is not None:
            # Es lo que hace `tocar_conversacion` de verdad: el turno queda en la base y el
            # siguiente mensaje lo lee de ahí.
            base.viva = (id_conversacion, turno_actual, True, 0)

    monkeypatch.setattr(persistencia, "tocar_conversacion", tocar_y_persistir)

    claves: list[str] = []

    async def responder(entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        ctx.turno.reiniciar()
        ctx.turno_actual += 1
        claves.append(ctx.clave("escalamiento", ctx.turno_actual))
        for _ in range(5):
            await asyncio.sleep(0)
        return _respuesta_simple(ctx)

    monkeypatch.setattr(conversacion, "responder", responder)

    async def correr():
        await asyncio.gather(
            _atender(mensaje_texto("A", wamid="wamid-a")),
            _atender(mensaje_texto("B", wamid="wamid-b")),
        )

    asyncio.run(asyncio.wait_for(correr(), timeout=10))

    assert claves[0] != claves[1], (
        "los dos turnos comparten clave de idempotencia: el segundo escalamiento se "
        "descartaría como duplicado y el doctor no se enteraría"
    )
    assert sorted(claves) == ["conv-x:escalamiento:6", "conv-x:escalamiento:7"]


def test_dos_telefonos_distintos_no_se_bloquean_entre_si(monkeypatch):
    """La otra mitad del candado: que no sea un cuello de botella global.

    Con un candado único, el segundo paciente esperaría a que termine el turno del primero
    --hasta un minuto con el retardo-- y la clínica tendría una cola de un solo carril.

    El `wait_for` no es decoración: si el candado resulta ser global, este `gather` no
    termina nunca y cuelga la suite entera. Preferimos una prueba roja a una suite colgada.
    """
    limpiar_estado()
    BaseFalsa(nueva=lambda telefono: f"conv-{telefono}").instalar(monkeypatch)
    ambas_dentro = asyncio.Event()
    simultaneas = {"max": 0, "ahora": 0}

    async def responder(entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        ctx.turno.reiniciar()
        ctx.turno_actual += 1
        simultaneas["ahora"] += 1
        simultaneas["max"] = max(simultaneas["max"], simultaneas["ahora"])
        if simultaneas["ahora"] == 2:
            ambas_dentro.set()
        await ambas_dentro.wait()
        simultaneas["ahora"] -= 1
        return conversacion.Resultado(
            respuesta=RespuestaDaniela(
                mensaje_al_paciente="ok",
                estado_oportunidad="explorando",
                barrera_detectada="ninguna",
                requiere_escalamiento=False,
                motivo_escalamiento="ninguno",
                fuera_de_alcance=False,
            ),
            turno=ctx.turno_actual,
        )

    monkeypatch.setattr(conversacion, "responder", responder)

    async def correr():
        await asyncio.gather(
            _atender(mensaje_texto("A", wamid="wamid-a", telefono=TELEFONO)),
            _atender(mensaje_texto("B", wamid="wamid-b", telefono=OTRO_TELEFONO)),
        )

    asyncio.run(asyncio.wait_for(correr(), timeout=10))

    assert simultaneas["max"] == 2, "el candado está serializando conversaciones distintas"


# ==========================================================================================
# 8-9 · El retardo humano
# ==========================================================================================


def _reloj_que_avanza(monkeypatch, inicio: float = 1_000.0):
    """Un `time.monotonic` de mentira que solo avanza cuando se le dice.

    Es lo que permite probar un turno de 30 o de 90 segundos sin esperarlos.
    """
    estado = {"ahora": inicio}
    monkeypatch.setattr(time, "monotonic", lambda: estado["ahora"])
    return estado


def test_el_retardo_descuenta_lo_que_tardo_el_turno(monkeypatch):
    """El retardo es «que la respuesta no llegue antes de N segundos», no «esperar N
    segundos MÁS».

    Sumarlos daría respuestas de minuto y medio: el paciente ya se fue.
    """
    estado = _reloj_que_avanza(monkeypatch)
    monkeypatch.setattr(random, "uniform", lambda a, b: 40.0)

    async def tarda_treinta(ctx):
        estado["ahora"] += 30.0

    preparar(monkeypatch, turnos=Turnos(antes=tarda_treinta))
    dormir = DormirFalso()

    atender(mensaje_texto(), dormir=dormir)

    assert dormir.dormidas == [10.0]


def test_el_retardo_nunca_es_negativo(monkeypatch):
    """Un turno más lento que el objetivo se responde ya. Un `sleep` negativo no revienta,
    pero un retardo calculado al revés sí: sería un tiempo muerto encima de un turno que ya
    tardó minuto y medio."""
    estado = _reloj_que_avanza(monkeypatch)
    monkeypatch.setattr(random, "uniform", lambda a, b: 40.0)

    async def tarda_noventa(ctx):
        estado["ahora"] += 90.0

    preparar(monkeypatch, turnos=Turnos(antes=tarda_noventa))
    dormir = DormirFalso()

    atender(mensaje_texto(), dormir=dormir)

    assert dormir.dormidas == [0.0]


# ==========================================================================================
# 10 · El interruptor
# ==========================================================================================


def test_con_el_interruptor_apagado_no_se_envia_nada(monkeypatch):
    """`MAXICARE_DANIELA_RESPONDE=0` tiene que callar a Daniela SIN gastar un token.

    Si el interruptor se comprobara después de `responder`, apagarla costaría lo mismo que
    dejarla encendida y el interruptor no serviría para lo que existe.
    """
    base, turnos = preparar(monkeypatch)
    whatsapp = WhatsAppFalso()

    resultado = atender(
        mensaje_texto(), whatsapp=whatsapp, config=config_falso(daniela_responde=False)
    )

    assert resultado.respondido is False
    assert resultado.motivo == "apagado"
    assert whatsapp.enviados == []
    assert turnos.llamadas == [], "con el interruptor apagado no se llama al modelo"
    assert base.llamadas == [], "ni se toca la base"


# ==========================================================================================
# 11 · El adjunto
# ==========================================================================================


def test_un_mensaje_con_archivo_marca_el_adjunto_y_no_interpreta(monkeypatch):
    """Lo que se le dice al modelo es QUÉ llegó, nunca QUÉ MUESTRA.

    El lector de archivos llega en la entrega 6B; hoy nadie ha visto el contenido. Si este
    camino inventara una lectura clínica, el guardrail no tendría nada que bloquear porque
    la invención vendría de dentro.

    `hubo_adjunto` es el prefiltro que enciende `sin_lectura_clinica`: sin él, ese guardrail
    ni siquiera llama al evaluador y una radiografía pasa sin revisión de salida.
    """
    _, turnos = preparar(monkeypatch)

    atender(
        mensaje_texto(
            tipo="image",
            texto="Esto es lo que me tomaron ayer",
            media_id="media-1",
            mime="image/jpeg",
            nombre_archivo="foto.jpg",
        )
    )

    entrada = turnos.entrada
    assert "imagen" in entrada
    assert "Esto es lo que me tomaron ayer" in entrada
    assert turnos.llamadas[-1]["hubo_adjunto"] is True

    prohibidas = ("caries", "fractura", "muela", "diagnóstico", "infección", "se observa")
    for palabra in prohibidas:
        assert palabra not in entrada.lower(), f"el texto interpreta el archivo: «{palabra}»"


# ==========================================================================================
# 12-13 · Cuando algo falla
# ==========================================================================================


@pytest.mark.parametrize(
    "fallo",
    [
        ErrorDeCanal("WhatsApp rechazó el envío: 400"),
        httpx.ReadTimeout("la Graph API no respondió"),
        KeyError("messages"),
    ],
    ids=["error_de_canal", "timeout_de_httpx", "respuesta_con_otra_forma"],
)
def test_si_enviar_falla_queda_registrado_y_no_revienta(monkeypatch, fallo):
    """WhatsApp no responde --token vencido, número fuera de la ventana de 24 h, o Meta
    tardando de más-- y eso no puede tumbar el turno.

    Los tres casos son reales y solo el primero es un `ErrorDeCanal`: `canales.enviar_texto`
    hace el POST **sin envolver los errores de httpx**, así que un `ReadTimeout` sale crudo, y
    un 200 con otra forma revienta en `r.json()["messages"][0]["id"]` con un `KeyError`. Con
    un `except` estrecho, esa tarde de timeouts se pierde en el BackgroundTask: el paciente
    sin respuesta, `mensajes_entrantes` sin motivo, y la conversación sin tocar --así que a
    las 24 horas se declara muerta a mitad de la charla.
    """
    base, _ = preparar(monkeypatch)
    whatsapp = WhatsAppFalso(falla_con=fallo)

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert resultado.respondido is False
    assert resultado.motivo and type(fallo).__name__ in resultado.motivo
    assert "marcar_fallo_respuesta" in base.nombres
    assert "marcar_respondido" not in base.nombres
    assert "tocar_conversacion" in base.nombres, (
        "sin tocarla, la conversación se declara vieja a las 24 h por un fallo de envío"
    )


def test_si_el_turno_revienta_el_paciente_igual_recibe_algo(monkeypatch):
    """La regla de `fallos.escalamiento`: PASE LO QUE PASE sale un mensaje al paciente.

    `responder` ya traduce lo que lanza el SDK, pero no lo que lanza una tool con un bug ni
    un fallo de red en mitad del turno. El silencio es la única respuesta que no vale.
    """
    preparar(monkeypatch, turnos=Turnos(revienta=RuntimeError("una tool con un bug")))
    whatsapp = WhatsAppFalso()

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert whatsapp.textos == [conversacion.MENSAJE_SEGURO]
    assert resultado.texto_enviado == conversacion.MENSAJE_SEGURO
    assert resultado.motivo and "una tool con un bug" in resultado.motivo


def test_un_turno_reventado_que_SI_respondio_deja_rastro_del_fallo(monkeypatch):
    """Respondido y con fallo interno no son excluyentes, y tratarlos como si lo fueran
    borraba el dato.

    Un turno que explota sale con `MENSAJE_SEGURO`, que ES una respuesta: entraba por
    `marcar_respondido`, y esa función pone `fallo_respuesta = NULL` --con razón, porque un
    reintento que sí sale tiene que borrar el fallo del intento anterior--. Resultado: en
    `mensajes_entrantes` un turno que reventó quedaba EXACTAMENTE IGUAL que uno que fue
    bien, y «¿a cuántos pacientes les contestamos con el mensaje de emergencia?» no se podía
    responder.
    """
    base, _ = preparar(monkeypatch, turnos=Turnos(revienta=RuntimeError("una tool con un bug")))

    atender(mensaje_texto())

    assert "marcar_respondido" in base.nombres, "el paciente sí recibió el mensaje seguro"
    assert "marcar_fallo_respuesta" in base.nombres, (
        "el turno reventó y en la base no queda ni rastro: indistinguible de uno normal"
    )
    # Y en ese orden: al revés, `marcar_respondido` borraría el motivo que se acaba de poner.
    assert base.nombres.index("marcar_respondido") < base.nombres.index("marcar_fallo_respuesta")
    assert "una tool con un bug" in base.argumentos("marcar_fallo_respuesta")[1]


def test_un_turno_normal_no_deja_un_fallo_inventado(monkeypatch):
    """La otra mitad: si `marcar_fallo_respuesta` se llamara siempre, el informe de «a quién
    no le contestamos» daría positivo para todos y dejaría de servir para nada."""
    base, _ = preparar(monkeypatch)

    atender(mensaje_texto())

    assert "marcar_respondido" in base.nombres
    assert "marcar_fallo_respuesta" not in base.nombres


# ==========================================================================================
# 14-15 · El contexto
# ==========================================================================================


def test_el_contexto_lleva_las_credenciales_de_telegram(monkeypatch):
    """Sin ellas, `escalar_a_doctores` construye un `Telegram("", "")` y falla EN SILENCIO:
    el doctor nunca se entera de que había que escalar.

    El chat de pruebas web las deja vacías a propósito --no le hace sonar el teléfono a nadie
    durante una prueba-- y copiar ese contexto tal cual es el error que esta prueba impide.
    """
    _, turnos = preparar(monkeypatch)

    atender(mensaje_texto())

    assert turnos.ctx.telegram_bot_token == "token-telegram"
    assert turnos.ctx.telegram_chat_doctores == "-100999"
    assert turnos.ctx.tema_general == 7


def test_el_contexto_lleva_el_calendario_que_se_le_pasa(monkeypatch):
    """El calendario viaja en el contexto para que la misma tool corra contra el doble en
    pruebas y contra Google en producción. Un `CalendarioDoble()` fijo aquí dejaría a la
    clínica sin ver ni una cita en su calendario, y sin un solo error en el log."""
    _, turnos = preparar(monkeypatch)
    calendario = CalendarioDoble()

    atender(mensaje_texto(), calendario=calendario)

    assert turnos.ctx.calendario is calendario
    assert turnos.ctx.capacidad_por_hora == 3
    assert turnos.ctx.duracion_cita_minutos == 45
    assert turnos.ctx.cierre_relevo_minutos == 120


# ==========================================================================================
# 16 · La sesión persistida
# ==========================================================================================


def test_el_turno_usa_la_sesion_persistida_y_no_la_de_memoria(monkeypatch):
    """La promesa de la fase 7, comprobada por donde se cumple: `atender` tiene que pedirle
    la sesión a `persistencia`, con el id de la conversación y la base de producción.

    Con `SesionEnMemoria` --lo que había-- esta prueba pasa igual si el historial muere al
    reiniciar, porque en un solo proceso no se nota. Por eso lo que se comprueba aquí es la
    FÁBRICA y no el comportamiento: quién construye la sesión es lo que decide si sobrevive.
    """
    pedidas: list[tuple[str, str]] = []

    def fabrica(id_conversacion, *, database_url, esquema=None, limite=None):
        pedidas.append((id_conversacion, database_url))
        return conversacion.SesionEnMemoria(id_conversacion)

    monkeypatch.setattr(atencion.persistencia, "sesion_de_agente", fabrica)

    sesion = atencion._sesion_de("conv-7", "postgresql://u:c@host/db")

    assert pedidas == [("conv-7", "postgresql://u:c@host/db")]
    assert sesion.session_id == "conv-7"


def test_atender_sin_sesion_inyectada_pide_la_persistida(monkeypatch):
    """El cable entero de la fase 7, comprobado por donde de verdad corre: SIN pasarle
    `sesion_de` a `atender` -- que es como lo llama `runtime.py` en producción --, el turno
    tiene que pedirle la sesión a `persistencia.sesion_de_agente` con
    `(id_conversacion, database_url)`.

    Ninguna otra prueba de este archivo recorre este camino: `_atender` inyecta siempre el
    doble en memoria. Por eso esta es la única que cae si alguien cambia
    `fabricar = sesion_de or (lambda id_: _sesion_de(id_, config.database_url))` por
    `fabricar = sesion_de or (lambda id_: conversacion.SesionEnMemoria(id_))` -- la mutación
    que desactiva la persistencia entera y deja el resto de la suite en verde.
    """
    preparar(monkeypatch)
    pedidas: list[tuple[str, str]] = []

    # `limite=-1` y no `None`: es el centinela real de `persistencia.sesion_de_agente`, y un
    # doble que declare el default contrario codifica la semántica contraria a la de
    # producción -- «sin límite» en vez de «usa el de `config`». Hoy no lo mira nadie, pero
    # la deriva de firmas entre un doble y su original ya costó una regresión en esta fase.
    def espia(id_conversacion, *, database_url, esquema=None, limite=-1):
        pedidas.append((id_conversacion, database_url))
        return conversacion.SesionEnMemoria(id_conversacion)

    monkeypatch.setattr(atencion.persistencia, "sesion_de_agente", espia)
    config = config_falso()

    resultado = atender(mensaje_texto(), config=config, sesion_de=None)

    assert resultado.respondido is True
    assert pedidas == [(resultado.id_conversacion, config.database_url)]


def test_una_sesion_inyectada_gana_a_la_de_produccion(monkeypatch):
    """`sesion_de` existe SOLO para que la suite offline pueda correr sin Neon, igual que
    `calendario` y `dormir`. En producción vale `None` y manda la persistida -- pero cuando SÍ
    se inyecta una, tiene que ser esa la que use el turno, y la fábrica de producción
    (`persistencia.sesion_de_agente`) no debe tocarse ni una vez.

    Antes, esta prueba solo miraba `inspect.signature`: pasaba igual con un cuerpo que
    ignorase `sesion_de` por completo. Esta versión comprueba comportamiento.
    """
    base, turnos = preparar(monkeypatch)

    llamadas_a_produccion: list[tuple] = []

    def fabrica_de_produccion(*args, **kwargs):
        llamadas_a_produccion.append((args, kwargs))
        return conversacion.SesionEnMemoria("no-deberia-usarse")

    monkeypatch.setattr(atencion.persistencia, "sesion_de_agente", fabrica_de_produccion)

    inyectada = conversacion.SesionEnMemoria("inyectada")
    resultado = atender(mensaje_texto(), sesion_de=lambda id_conversacion: inyectada)

    assert resultado.respondido is True
    assert turnos.llamadas[-1]["sesion"] is inyectada
    assert llamadas_a_produccion == []


def test_una_conversacion_sin_configuracion_operativa_usa_los_defaults(monkeypatch):
    """Que la tabla `configuracion` no responda no puede dejar mudo al paciente.

    Y la degradación tiene que degradar de verdad. `conectar` abre con `autocommit=False`:
    el error de SQL deja la transacción ABORTADA, y las dos lecturas que vienen después
    dentro del mismo `with` --`conversacion_tomada` y `ligar_mensaje_a_conversacion`--
    revientan con `InFailedSqlTransaction` si nadie hace `rollback()`. Sin él, un
    `statement_timeout` sobre `configuracion` tumba `_leer_estado` entero y TODOS los
    mensajes de la clínica reciben el mensaje de emergencia, sin conversación y sin
    historial, por no poder leer tres enteros que ya tienen default.

    Por eso `ConexionFalsa` imita la transacción abortada: sin eso esta prueba pasaba por la
    razón equivocada.
    """
    base, turnos = preparar(
        monkeypatch, base=BaseFalsa(configuracion=RuntimeError("statement timeout"))
    )
    whatsapp = WhatsAppFalso()

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert resultado.respondido is True
    assert whatsapp.textos and whatsapp.textos[0] != conversacion.MENSAJE_SEGURO, (
        "cayó al camino de «sin base»: la lectura entera se fue al suelo"
    )
    assert base.conexiones[0].rollbacks == 1, "la transacción quedó abortada y nadie la limpió"
    assert "conversacion_tomada" in base.nombres
    assert "ligar_mensaje_a_conversacion" in base.nombres
    assert turnos.ctx.capacidad_por_hora == 2
    assert turnos.ctx.duracion_cita_minutos == 60
    assert turnos.ctx.tema_general == 0


@pytest.mark.parametrize("tipo", ["image", "document", "audio"])
def test_ningun_tipo_de_archivo_se_queda_sin_nombre_en_castellano(monkeypatch, tipo):
    """«Mandó un image» no lo lee nadie, y al modelo le pasa igual: el nombre en castellano
    es lo que le permite decir «recibí tu radiografía» sin saber qué hay dentro."""
    _, turnos = preparar(monkeypatch)

    atender(mensaje_texto(tipo=tipo, texto=None, media_id="media-1"))

    assert ingesta.NOMBRE_HUMANO[tipo] in turnos.entrada
    assert turnos.llamadas[-1]["hubo_adjunto"] is True


# ==========================================================================================
# Lo que `_DatosDelMensaje` conserva, y lo que NO puede conservar
# ==========================================================================================


def test_los_hechos_del_mensaje_sobreviven_al_reinicio_del_turno():
    """`responder` llama a `ctx.turno.reiniciar()` antes de correr, y eso borraba los dos
    campos del prefiltro clínico. Que el paciente haya mandado una radiografía no es algo que
    autorizara una tool: es un hecho del mensaje que ya entró."""
    turno = atencion._DatosDelMensaje(adjunto_del_mensaje=True, sintomas_del_mensaje=True)

    # Antes de reiniciar nada: el invariante ya tiene que ser cierto.
    assert turno.hubo_adjunto is True
    assert turno.menciona_sintomas is True

    turno.reiniciar()

    assert turno.hubo_adjunto is True
    assert turno.menciona_sintomas is True


def test_el_reinicio_sigue_borrando_las_cifras_y_las_horas_autorizadas():
    """La mitad que protege el muro, y la que nadie vigilaba.

    Mutando `reiniciar()` para que conservara también `cifras_autorizadas`, la suite entera
    seguía verde. Eso sería una fuga entre turnos: un precio consultado hace diez mensajes
    volvería a estar autorizado y `sin_cifra_no_documentada` lo dejaría pasar «porque ya lo
    vio». Es exactamente el fallo contra el que existe `DatosDelTurno.reiniciar`, y este
    subtipo es lo único que se interpone entre ese fallo y el paciente.
    """
    turno = atencion._DatosDelMensaje(adjunto_del_mensaje=True)
    turno.cifras_autorizadas = {"1900000"}
    turno.horas_autorizadas = {"09:00"}

    turno.reiniciar()

    assert turno.cifras_autorizadas == set(), "una cifra de otro turno sigue autorizada"
    assert turno.horas_autorizadas == set(), "una hora de otro turno sigue autorizada"
    assert turno.hubo_adjunto is True, "y el hecho del mensaje tiene que seguir ahí"


def test_un_mensaje_que_habla_de_dolor_enciende_el_prefiltro_clinico(monkeypatch):
    """La otra mitad del prefiltro de `sin_lectura_clinica`, y hoy nadie más la enciende.

    El único sitio del repositorio que rellenaba `menciona_sintomas` era un script de la
    fase 4. Sin esa línea en `atencion.py`, el guardrail que impide que Daniela le diga a un
    paciente qué tiene no llega ni a preguntarle al evaluador cuando alguien escribe «me
    duele mucho desde ayer».
    """
    _, turnos = preparar(monkeypatch)

    atender(mensaje_texto("Hola, me duele mucho desde ayer"))

    assert turnos.llamadas[-1]["menciona_sintomas"] is True


def test_un_mensaje_normal_no_enciende_el_prefiltro(monkeypatch):
    """El caso que NO debe disparar: sin él, poner el campo a `True` siempre pasaría la
    prueba de arriba y se pagaría un evaluador por cada «¿tienen parqueadero?»."""
    _, turnos = preparar(monkeypatch)

    atender(mensaje_texto("Hola, ¿tienen parqueadero?"))

    assert turnos.llamadas[-1]["menciona_sintomas"] is False


# ==========================================================================================
# El ruling 5: ninguna conexión abierta mientras corre el modelo
# ==========================================================================================


def test_la_conexion_a_neon_se_cierra_antes_de_llamar_al_modelo(monkeypatch):
    """Una conexión sostenida a lo largo del turno deja la sesión `idle in transaction` los
    ocho o diez segundos que tarda el modelo.

    Ninguna de las lecturas hace `commit()` tras su SELECT, así que el snapshot y la conexión
    del pooler se quedan retenidos **por cada paciente que esté escribiendo a la vez**. Hoy se
    cumple, pero nada lo vigilaba: un refactor que moviera el `with` a envolver el turno
    entero pasaría en verde y la factura de Neon sería el único aviso.
    """
    limpiar_estado()
    base = BaseFalsa(viva=("conv-viva", 1, True, 0)).instalar(monkeypatch)
    turnos = Turnos()

    async def responder(entrada, **extra):
        base.llamadas.append(("modelo",))
        return await turnos(entrada, **extra)

    monkeypatch.setattr(conversacion, "responder", responder)

    atender(mensaje_texto())

    abiertas, abiertas_en_el_modelo = 0, None
    for llamada in base.llamadas:
        if llamada[0] == "conectar":
            abiertas += 1
        elif llamada[0] == "cerrar":
            abiertas -= 1
        elif llamada[0] == "modelo":
            abiertas_en_el_modelo = abiertas

    assert abiertas_en_el_modelo == 0, "hay una conexión a Neon abierta mientras corre el modelo"
    assert base.nombres.count("conectar") == 2, "las lecturas y las escrituras van en dos bloques"
    assert base.nombres.count("cerrar") == 2
    assert all(c.cerrada for c in base.conexiones)


# ==========================================================================================
# El turno se persiste
# ==========================================================================================


def test_el_turno_se_guarda_en_la_conversacion(monkeypatch):
    """`conversaciones.turno_actual` no lo escribía ninguna sentencia del proyecto.

    Como el contexto se construye nuevo por mensaje leyendo el turno de Neon, la columna
    congelada en 0 hacía que todos los turnos de una conversación fueran el turno 1 -- y la
    clave de idempotencia `id_conversacion + turno` dejaba de distinguir un escalamiento
    nuevo de un reintento del anterior. El doctor solo se enteraba del primero.
    """
    base, _ = preparar(monkeypatch, base=BaseFalsa(viva=("conv-viva", 4, True, 0)))

    resultado = atender(mensaje_texto())

    assert resultado.turno == 5, "el turno leído de Neon tiene que avanzar"
    assert base.argumentos("tocar_conversacion") == ("conv-viva", 5)


def test_el_turno_se_guarda_aunque_el_envio_falle(monkeypatch):
    """El turno se gastó igual: el modelo corrió y sus tools ya pudieron escribir. Dejar la
    columna atrás haría que el siguiente mensaje reutilizara la clave de idempotencia de un
    turno que sí ocurrió."""
    base, _ = preparar(monkeypatch, base=BaseFalsa(viva=("conv-viva", 4, True, 0)))
    whatsapp = WhatsAppFalso(falla_con=httpx.ConnectError("sin red"))

    atender(mensaje_texto(), whatsapp=whatsapp)

    assert base.argumentos("tocar_conversacion") == ("conv-viva", 5)


# ==========================================================================================
# El calendario que no arranca
# ==========================================================================================


def calendario_que_no_arranca(config):
    raise ErrorDeCalendario("Google devolvió 503 al comprobar el acceso")


def test_si_el_calendario_no_arranca_daniela_sigue_contestando(monkeypatch):
    """`CalendarioGoogle.__init__` hace una lectura real contra Google --es su comprobación de
    acceso-- y puede fallar al construirse. Sin el `try`, ese fallo salía de `atender` y el
    paciente se quedaba sin respuesta por una dependencia que ni siquiera hace falta para
    contestarle un precio."""
    _, turnos = preparar(monkeypatch)
    monkeypatch.setattr(atencion, "calendario_desde_config", calendario_que_no_arranca)
    whatsapp = WhatsAppFalso()

    resultado = atender(mensaje_texto(), whatsapp=whatsapp, calendario=None)

    assert resultado.respondido is True
    assert whatsapp.textos, "el paciente se quedó sin respuesta por el calendario"
    assert isinstance(turnos.ctx.calendario, CalendarioCaido)


def test_un_calendario_caido_nunca_es_un_calendario_doble(monkeypatch):
    """La decisión que sostiene la prueba anterior.

    Caer a `CalendarioDoble` es el arreglo obvio y es mucho peor que el problema: el doble
    dice que sí a todo, así que `crear_cita` tomaría el cupo, «crearía» el evento en un
    diccionario y Daniela le confirmaría al paciente una cita que no existe en ningún
    calendario. El paciente llega a una clínica donde nadie lo espera.
    """
    _, turnos = preparar(monkeypatch)
    monkeypatch.setattr(atencion, "calendario_desde_config", calendario_que_no_arranca)

    atender(mensaje_texto(), calendario=None)

    assert not isinstance(turnos.ctx.calendario, CalendarioDoble)
    with pytest.raises(ErrorDeCalendario):
        turnos.ctx.calendario.crear_evento(
            inicio=datetime(2026, 9, 14, 9, 0), duracion_minutos=60, titulo="x"
        )


def test_marcar_leido_no_puede_tumbar_el_turno(monkeypatch):
    """`canales.marcar_leido` se traga los `httpx.HTTPError` y nada más. Cualquier otra cosa
    salía de `atender` ANTES de tocar la base: el paciente sin respuesta y sin rastro, por un
    detalle cosmético que ni siquiera es el mensaje."""
    preparar(monkeypatch)
    whatsapp = WhatsAppFalso()

    async def revienta(wamid):
        raise RuntimeError("el token de WhatsApp no sirve para marcar leído")

    whatsapp.marcar_leido = revienta

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert resultado.respondido is True
    assert whatsapp.textos


# ==========================================================================================
# 13 · El búfer -- lo que se escribió de corrido se contesta una vez
# ==========================================================================================
#
# Estas usan tiempo REAL, con ventanas de décimas. Es deliberado: lo que se prueba aquí es
# justamente la carrera entre un mensaje que llega y un temporizador que corre, y un reloj de
# mentira la borraría. La suite entera sigue por debajo de los diez segundos.

VENTANA_CORTA = 0.4
TOPE_CORTO = 2.0


def _en_grupo(**extra):
    """Los argumentos del búfer, para no repetirlos en cada prueba."""
    return dict(ventana=VENTANA_CORTA, tope=TOPE_CORTO, **extra)


def test_dos_mensajes_seguidos_reciben_UNA_sola_respuesta(monkeypatch):
    """El fallo que trajo el búfer, tal como lo vivió la clínica.

    Tres mensajes en 48 segundos y tres respuestas, las dos últimas con siete segundos entre
    ellas. En WhatsApp nadie escribe párrafos: el saludo va aparte de la pregunta.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    async def escena():
        lider = asyncio.create_task(
            _atender(
                mensaje_texto("Quisiera saber qué servicios ofrecen?", wamid="w1"),
                whatsapp=whatsapp,
                **_en_grupo(),
            )
        )
        await asyncio.sleep(0.02)
        assert atencion._buferes, "el líder no llegó a abrir el grupo; la prueba no prueba nada"
        sumado = await _atender(
            mensaje_texto("Ofrecen diseños de sonrisa?", wamid="w2"),
            whatsapp=whatsapp,
            **_en_grupo(),
        )
        return await lider, sumado

    lider, sumado = asyncio.run(escena())

    assert len(turnos.llamadas) == 1, "se corrió más de un turno para una sola idea"
    assert len(whatsapp.enviados) == 1, f"el paciente recibió {len(whatsapp.enviados)} globos"
    assert sumado.agrupado is True
    assert sumado.respondido is False
    # `motivo` vacío a propósito: `runtime.py` registra cualquier motivo como warning, y
    # agrupar no es una incidencia.
    assert sumado.motivo is None
    assert lider.mensajes_agrupados == 2

    entrada = turnos.llamadas[0]["entrada"]
    assert "servicios ofrecen" in entrada
    assert "diseños de sonrisa" in entrada


def test_dos_mensajes_separados_por_una_pausa_reciben_dos_respuestas(monkeypatch):
    """La otra mitad, y sin ella la de arriba no prueba nada.

    Un búfer que se tragara TODO --que agrupara también lo que llega media hora después--
    pasaría la prueba anterior con nota. Lo que hace útil al búfer es que se cierre.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    atender(mensaje_texto("Hola buenas noches", wamid="w1"), whatsapp=whatsapp, **_en_grupo())
    atender(mensaje_texto("¿Cuánto vale una limpieza?", wamid="w2"), whatsapp=whatsapp,
            **_en_grupo())

    assert len(turnos.llamadas) == 2
    assert len(whatsapp.enviados) == 2
    assert atencion._buferes == {}, "quedó un búfer registrado después de contestar"


def test_cada_mensaje_nuevo_reinicia_la_ventana(monkeypatch):
    """Quien escribe tres veces seguidas no espera tres ventanas, pero tampoco se le corta.

    Sin el reinicio, el tercer mensaje llegaría cuando el turno del primero ya arrancó y se
    quedaría fuera del grupo -- que es medio arreglo, y medio arreglo aquí se nota igual.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    async def escena():
        lider = asyncio.create_task(
            _atender(mensaje_texto("uno", wamid="w1"), whatsapp=whatsapp, **_en_grupo())
        )
        await asyncio.sleep(0.02)
        for i in range(2, 8):
            # Huecos cortos, tramo largo: seis mensajes cada 0.1 s abarcan 0.6 s, mas que la
            # ventana de 0.4, pero NINGUN hueco la alcanza. Sin el reinicio, el grupo cerraria
            # a los 0.4 s y los ultimos se quedarian fuera.
            #
            # Los numeros no son estetica: con solo tres mensajes, el hueco tendria que pasar
            # de media ventana y bastaria que un `sleep` de 0.24 s se fuera a 0.4 para romper
            # la prueba. Paso sola y fallo dentro de la suite en el primer intento.
            await asyncio.sleep(0.1)
            await _atender(
                mensaje_texto(f"mas {i}", wamid=f"w{i}"), whatsapp=whatsapp, **_en_grupo()
            )
        return await lider

    lider = asyncio.run(escena())

    assert lider.mensajes_agrupados == 7
    assert len(turnos.llamadas) == 1
    assert len(whatsapp.enviados) == 1


def test_el_tope_corta_a_quien_escribe_sin_parar(monkeypatch):
    """El tope es lo único que impide que la ventana quede abierta para siempre.

    Con la ventana más larga que las pausas del paciente, el silencio no llega nunca: si el
    tope no cerrara el grupo, este turno no saldría jamás y el paciente se quedaría mirando
    el doble check azul.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    async def escena():
        lider = asyncio.create_task(
            _atender(mensaje_texto("uno", wamid="w1"), whatsapp=whatsapp, ventana=5.0, tope=0.3)
        )
        await asyncio.sleep(0.02)
        for i in range(2, 10):
            await asyncio.sleep(0.05)
            await _atender(
                mensaje_texto(f"mas {i}", wamid=f"w{i}"),
                whatsapp=whatsapp,
                ventana=5.0,
                tope=0.3,
            )
        return await lider

    lider = asyncio.run(escena())

    # Con ventana=5 y un mensaje cada 0.05 s, el silencio NUNCA llega. Que este turno haya
    # terminado ya es la prueba, y que no se llevara los nueve mensajes dice que fue el tope.
    assert lider.respondido is True
    assert lider.mensajes_agrupados < 9, "el tope no cortó: el grupo se llevó todo el chorro"


def test_los_dos_mensajes_quedan_marcados_con_la_misma_respuesta(monkeypatch):
    """Una respuesta, dos mensajes contestados, un solo `wamid_respuesta`.

    Marcar solo uno dejaría al otro «sin responder» para siempre, y la barrida que busca a
    quién no le contestamos lo recogería en cada pasada, sin fin.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    async def escena():
        lider = asyncio.create_task(
            _atender(mensaje_texto("uno", wamid="w1"), whatsapp=whatsapp, **_en_grupo())
        )
        await asyncio.sleep(0.02)
        await _atender(mensaje_texto("dos", wamid="w2"), whatsapp=whatsapp, **_en_grupo())
        return await lider

    asyncio.run(escena())

    marcados = [(l[1], l[2]) for l in base.llamadas if l[0] == "marcar_respondido"]
    assert sorted(w for w, _ in marcados) == ["w1", "w2"]
    assert len({respuesta for _, respuesta in marcados}) == 1, "cada uno apunta a otra respuesta"

    ligados = sorted(l[1] for l in base.llamadas if l[0] == "ligar_mensaje_a_conversacion")
    assert ligados == ["w1", "w2"], "un mensaje del grupo se quedó sin conversación"


def test_un_adjunto_en_cualquier_mensaje_del_grupo_marca_el_turno(monkeypatch):
    """Mandar la radiografía y escribir «¿esto qué es?» justo después es UN solo turno.

    Si el adjunto se leyera solo del último mensaje, `sin_lectura_clinica` se quedaría sin
    nada que vigilar precisamente en el turno que sí habla de la imagen.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    imagen = dict(tipo="image", media_id="media-1")

    async def escena(primero, segundo):
        lider = asyncio.create_task(
            _atender(primero, whatsapp=whatsapp, **_en_grupo())
        )
        await asyncio.sleep(0.02)
        await _atender(segundo, whatsapp=whatsapp, **_en_grupo())
        return await lider

    # Los DOS órdenes, y no es por completismo: con la imagen solo en el mensaje que abre el
    # grupo, leer `mensaje.trae_archivo` --el líder-- daría `True` y la prueba pasaría con la
    # lectura rota. Con la imagen solo en el último, la rota sería `mensajes[-1]`. Ninguna
    # lectura de un único mensaje del grupo sobrevive a los dos casos a la vez.
    asyncio.run(escena(
        mensaje_texto(None, wamid="w1", **imagen),
        mensaje_texto("¿esto qué es?", wamid="w2"),
    ))
    asyncio.run(escena(
        mensaje_texto("le mando una foto", wamid="w3"),
        mensaje_texto(None, wamid="w4", **imagen),
    ))

    assert len(turnos.llamadas) == 2, "cada orden tenía que dar exactamente un turno"
    assert [l["hubo_adjunto"] for l in turnos.llamadas] == [True, True]


def test_dos_telefonos_distintos_no_se_agrupan(monkeypatch):
    """El búfer es por número. Juntar a dos pacientes en un turno sería mucho peor que
    contestarle dos veces a uno."""
    base, turnos = preparar(
        monkeypatch, BaseFalsa(viva=None, nueva=lambda tel: f"conv-{tel}")
    )
    whatsapp = WhatsAppFalso()

    async def escena():
        return await asyncio.gather(
            _atender(mensaje_texto("uno", wamid="w1"), whatsapp=whatsapp, **_en_grupo()),
            _atender(
                mensaje_texto("dos", wamid="w2", telefono=OTRO_TELEFONO),
                whatsapp=whatsapp,
                **_en_grupo(),
            ),
        )

    resultados = asyncio.run(escena())

    assert [r.mensajes_agrupados for r in resultados] == [1, 1]
    assert len(turnos.llamadas) == 2
    assert len(whatsapp.enviados) == 2


def test_lo_que_espera_el_bufer_se_descuenta_del_retardo(monkeypatch):
    """El búfer no se suma al retardo: se lo come.

    Encadenarlos sacaría la respuesta del minuto que fija `limites.latencia_maxima`. El
    reloj se cuenta desde el PRIMER mensaje del grupo, así que los segundos que pasó
    esperando ya están pagados.
    """
    base, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    monkeypatch.setattr(random, "uniform", lambda a, b: 10.0)
    dormir = DormirFalso()

    async def escena():
        lider = asyncio.create_task(
            _atender(
                mensaje_texto("uno", wamid="w1"), dormir=dormir, ventana=0.3, tope=TOPE_CORTO
            )
        )
        await asyncio.sleep(0.02)
        await _atender(
            mensaje_texto("dos", wamid="w2"), dormir=dormir, ventana=0.3, tope=TOPE_CORTO
        )
        return await lider

    asyncio.run(escena())

    assert len(dormir.dormidas) == 1
    # Esperó al menos 0.3 s de ventana (más lo que tardó el segundo mensaje en llegar), y
    # todo eso tiene que haber salido de los 10 s del retardo.
    assert dormir.dormidas[0] <= 10.0 - 0.3, (
        f"se pidió dormir {dormir.dormidas[0]:.2f} s sobre un objetivo de 10: la espera del "
        "búfer se está sumando al retardo en vez de descontarse"
    )


def test_una_cancelacion_no_deja_el_bufer_atascado(monkeypatch):
    """El `finally` que saca el búfer, y por qué no es una precaución de más.

    Un búfer que sobreviviera a su turno se tragaría todos los mensajes siguientes de ese
    número: cada uno se sumaría a un grupo que ya no espera a nadie, y ese teléfono no
    volvería a recibir respuesta -- en silencio, y solo ese teléfono.
    """
    preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))

    async def escena():
        tarea = asyncio.create_task(_atender(mensaje_texto(), ventana=5.0, tope=5.0))
        await asyncio.sleep(0.05)
        assert atencion._buferes, "el búfer no llegó a registrarse; la prueba no prueba nada"
        tarea.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarea

    asyncio.run(escena())

    assert atencion._buferes == {}, "el búfer quedó registrado tras cancelarse el turno"


def test_dos_mensajes_a_la_vez_no_abren_dos_grupos(monkeypatch):
    """El bloque del búfer no tiene un solo `await`, y eso es lo único que lo hace atómico.

    Las demás pruebas del búfer separan los dos mensajes con un `sleep`, así que el primero
    ya registró su grupo cuando llega el segundo: ninguna vigila el hueco entre el `get` de
    `_buferes` y el registro. Aquí los dos entran DE VERDAD a la vez. Con un `await` metido
    en medio los dos leen `_buferes` vacío, los dos abren grupo, y el paciente recibe dos
    respuestas a una sola idea -- el fallo que el búfer existe para quitar.
    """
    _, turnos = preparar(monkeypatch, BaseFalsa(viva=("conv-1", 4, True, 0)))
    whatsapp = WhatsAppFalso()

    async def escena():
        return await asyncio.gather(
            _atender(mensaje_texto("hola", wamid="w1"), whatsapp=whatsapp, **_en_grupo()),
            _atender(
                mensaje_texto("una pregunta", wamid="w2"), whatsapp=whatsapp, **_en_grupo()
            ),
        )

    asyncio.run(escena())

    assert len(turnos.llamadas) == 1, "los dos mensajes abrieron su propio turno"
    assert len(whatsapp.enviados) == 1, (
        "dos respuestas a una sola idea: es justo lo que el búfer existe para quitar"
    )


# ==========================================================================================
# 14 · La lectura del archivo
# ==========================================================================================

CLINICO = "reabsorcion radicular en el 46, con lesion periapical de 4 mm"


def _no_clinica(**cambios) -> LecturaNoClinica:
    campos = dict(
        tipo_documento="remision_externa",
        tratamiento="ortodoncia",
        origen="Clinica Dental Norte",
        fecha_documento=None,
        confianza="alta",
    )
    campos.update(cambios)
    return LecturaNoClinica(**campos)


def _tarea(valor, *, tarda: float = 0.0):
    """Una `Task` ya corriendo, como la que devuelve `ingesta.procesar_mensaje`."""

    async def cuerpo():
        if tarda:
            await asyncio.sleep(tarda)
        return valor

    return asyncio.ensure_future(cuerpo())


def mensaje_imagen(**cambios) -> ingesta.MensajeEntrante:
    campos = dict(
        wamid="wamid-foto-1",
        telefono=TELEFONO,
        nombre_perfil="Ana",
        tipo="image",
        texto=None,
        media_id="media-1",
        mime="image/jpeg",
    )
    campos.update(cambios)
    return ingesta.MensajeEntrante(**campos)


def test_con_confianza_alta_daniela_sabe_que_documento_llego(monkeypatch):
    """La entrada del modelo nombra el tratamiento y el origen, y NADA clinico."""
    _, turnos = preparar(monkeypatch)

    async def corrida():
        return await _atender(mensaje_imagen(), lectura=_tarea(_no_clinica()))

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert "ortodoncia" in entrada
    assert "Clinica Dental Norte" in entrada
    assert CLINICO not in entrada
    assert "no puedes verlo" not in entrada, "se quedo con la entrada de antes de 6B"
    assert turnos.llamadas[-1]["hubo_adjunto"] is True, (
        "sin la marca, `sin_lectura_clinica` no vigila justo el turno donde importa"
    )


def test_con_el_tratamiento_sin_identificar_daniela_pregunta_en_vez_de_asumir(monkeypatch):
    """La rama `no_identificado`: lo unico que Daniela puede hacer es preguntar.

    Ojo con lo que esta prueba NO cubre. `confianza` va aqui solo para que la lectura se
    parezca a una de verdad: `atencion` no la lee nunca. La regla de que una confianza por
    debajo de `alta` degrade el tratamiento a `no_identificado` vive en `lectura.repartir`,
    y la prueban las de `tests/test_lectura.py`. Si alguien la rompe alli, esta sigue en
    verde -- por eso el nombre ya no promete vigilarla.
    """
    _, turnos = preparar(monkeypatch)

    async def corrida():
        return await _atender(
            mensaje_imagen(),
            lectura=_tarea(_no_clinica(tratamiento="no_identificado", confianza="baja")),
        )

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert "preguntaselo" in entrada.lower() or "pregúntaselo" in entrada.lower()
    assert "ortodoncia" not in entrada


def test_el_lector_caido_no_calla_a_daniela(monkeypatch):
    """Con la tarea devolviendo None sale la entrada de siempre, y el paciente recibe
    respuesta igual. Un lector caido no puede dejar mudo al sistema."""
    wa = WhatsAppFalso()
    _, turnos = preparar(monkeypatch)

    async def corrida():
        return await _atender(mensaje_imagen(), whatsapp=wa, lectura=_tarea(None))

    atendido = asyncio.run(corrida())

    assert atendido.respondido is True
    assert len(wa.enviados) == 1
    assert "no puedes verlo" in turnos.llamadas[-1]["entrada"]


def test_sin_lectura_ninguna_el_camino_de_hoy_sigue_intacto(monkeypatch):
    """`lectura=None` es lo que pasa con un audio o un sticker: ni se intento leer."""
    _, turnos = preparar(monkeypatch)

    atender(mensaje_imagen())

    assert "no puedes verlo" in turnos.llamadas[-1]["entrada"]


def test_el_lector_lento_no_se_suma_a_la_ventana(monkeypatch):
    """El invariante que el CLAUDE.md lista: el retardo se descuenta, no se suma.

    Con una ventana de 0.4 s y un lector que tarda mucho mas que el margen, el turno sale
    SIN la lectura en vez de esperarla: lo que no llego a tiempo se descarta, no se espera.

    Lo que esta prueba NO vigila, porque el margen va monkeypatcheado a 0.1: el valor de
    produccion de `MARGEN_LECTURA_SEGUNDOS`. Ese lo fija
    `test_el_margen_de_produccion_es_el_que_cabe_en_el_presupuesto`. Y tampoco vigila que
    la recogida siga DESPUES de la ventana; eso es
    `test_la_ventana_le_sirve_de_plazo_al_lector`.
    """
    _, turnos = preparar(monkeypatch)
    monkeypatch.setattr(atencion, "MARGEN_LECTURA_SEGUNDOS", 0.1)

    async def corrida():
        tarea = _tarea(_no_clinica(), tarda=5.0)
        try:
            arranque = time.monotonic()
            atendido = await _atender(
                mensaje_imagen(),
                lectura=tarea,
                ventana=VENTANA_CORTA,
                tope=2.0,
            )
            return atendido, time.monotonic() - arranque
        finally:
            # El `shield` de `_recoger_lecturas` la deja viva a proposito: el lector sigue
            # corriendo para el doctor. Aqui se cancela para que pytest no avise de una
            # `Task` pendiente al cerrar el bucle.
            tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarea

    atendido, tardo = asyncio.run(corrida())

    assert atendido.respondido is True
    assert tardo < 2.0, f"el turno espero al lector: tardo {tardo:.1f} s"
    assert "no puedes verlo" in turnos.llamadas[-1]["entrada"]


def test_el_margen_de_produccion_es_el_que_cabe_en_el_presupuesto():
    """Las dos pruebas que miden el margen lo monkeypatchean, asi que ninguna fija su valor.

    3 s es lo que cabe: el tope del bufer son 45, un turno tarda del orden de 6, y
    `limites.latencia_maxima` es un minuto. Subirlo se come el margen que queda, y encima no
    sirve de nada -- la ventana ya le dio al lector sus 20 segundos en paralelo.
    """
    assert MARGEN_LECTURA_SEGUNDOS == 3.0
    assert atencion.MARGEN_LECTURA_SEGUNDOS == 3.0, (
        "`atencion` no lo importa en su cabecera: el monkeypatch de las pruebas del bufer "
        "no estaria tocando el valor que usa `_recoger_lecturas`"
    )


def test_el_margen_es_del_grupo_entero_y_no_de_cada_archivo(monkeypatch):
    """Dos archivos en un grupo no valen dos margenes.

    Los lectores corrieron TODOS a la vez durante la ventana, asi que a todos les queda lo
    mismo por terminar. Un margen por archivo convertiria las tres paginas de una remision
    --el caso que el bufer existe para agrupar-- en 3 x 3 s de cola detras de un tope de 45,
    fuera del minuto que fija `limites.latencia_maxima`.
    """
    _, turnos = preparar(monkeypatch)
    margen = 0.5
    monkeypatch.setattr(atencion, "MARGEN_LECTURA_SEGUNDOS", margen)

    async def corrida():
        lentas = [_tarea(_no_clinica(), tarda=5.0) for _ in range(2)]
        try:
            arranque = time.monotonic()
            lider = asyncio.ensure_future(
                _atender(
                    mensaje_imagen(),
                    lectura=lentas[0],
                    ventana=VENTANA_CORTA,
                    tope=TOPE_CORTO,
                )
            )
            await asyncio.sleep(0.05)
            await _atender(
                mensaje_imagen(wamid="wamid-foto-2", media_id="media-2"),
                lectura=lentas[1],
                ventana=VENTANA_CORTA,
                tope=TOPE_CORTO,
            )
            await lider
            return time.monotonic() - arranque
        finally:
            for tarea in lentas:
                tarea.cancel()
            for tarea in lentas:
                with contextlib.suppress(asyncio.CancelledError):
                    await tarea

    tardo = asyncio.run(corrida())

    # La ventana cierra a los ~0.45 s (el segundo mensaje la reinicio a los 0.05). Con el
    # plazo compartido detras van 0.5 s; con uno por archivo irian 1.0.
    assert tardo < VENTANA_CORTA + margen * 1.6, (
        f"el turno tardo {tardo:.2f} s: el margen se esta dando por archivo, no al grupo"
    )
    entrada = turnos.llamadas[-1]["entrada"]
    assert entrada.count("no puedes verlo") == 2, (
        "ninguno de los dos lectores llego a tiempo, asi que los dos salen sin lectura"
    )


def test_la_ventana_le_sirve_de_plazo_al_lector(monkeypatch):
    """La otra mitad del invariante: el lector corre EN PARALELO con la ventana.

    Un lector que tarda 0.2 s cabe de sobra en una ventana de 0.4: cuando la ventana cierra
    la lectura ya esta lista y el turno la recoge sin esperar un milisegundo mas. Si alguien
    mueve la recogida DELANTE del bufer, el margen se gasta antes de que el lector arranque
    --0.1 s contra 0.2-- y la lectura se pierde entera aunque hubiera llegado a tiempo. Con
    los numeros de produccion es peor: 3 s de margen contra un lector de 4-8 y una ventana
    de 20 que ya no le sirve de nada.

    Esta prueba no esta en el plan. Se anadio porque la mutacion 1 de su paso 8 --mover la
    recogida delante del bufer-- no hacia caer nada: `test_el_lector_lento_no_se_suma_a_la
    _ventana` solo vigila que el turno no se ALARGUE, y adelantar la recogida no lo alarga,
    solo tira la lectura.
    """
    _, turnos = preparar(monkeypatch)
    monkeypatch.setattr(atencion, "MARGEN_LECTURA_SEGUNDOS", 0.1)

    async def corrida():
        return await _atender(
            mensaje_imagen(),
            lectura=_tarea(_no_clinica(), tarda=0.2),
            ventana=VENTANA_CORTA,
            tope=2.0,
        )

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert "ortodoncia" in entrada, (
        "la lectura cabia en la ventana y se perdio: se esta recogiendo antes de abrirla"
    )


def test_la_lectura_se_ata_al_mensaje_que_traia_el_archivo(monkeypatch):
    """Foto + «esto que es?» son DOS mensajes que el bufer agrupa en un turno.

    La lectura pertenece al primero. Si se atara al ultimo, la entrada del modelo diria que
    el mensaje de TEXTO trae una remision, que es falso y ademas confunde al modelo.
    """
    _, turnos = preparar(monkeypatch)

    async def corrida():
        foto = asyncio.ensure_future(
            _atender(
                mensaje_imagen(),
                lectura=_tarea(_no_clinica()),
                ventana=VENTANA_CORTA,
                tope=2.0,
            )
        )
        await asyncio.sleep(0.05)
        texto = asyncio.ensure_future(
            _atender(
                mensaje_texto("¿esto qué es?", wamid="wamid-texto-2"),
                ventana=VENTANA_CORTA,
                tope=2.0,
            )
        )
        return await asyncio.gather(foto, texto)

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert entrada.count("ortodoncia") == 1, (
        "la lectura se pego a los dos mensajes del grupo, o a ninguno"
    )
    # La linea de la remision va con la foto, no con el texto.
    posicion_lectura = entrada.index("ortodoncia")
    posicion_texto = entrada.index("¿esto qué es?")
    assert posicion_lectura < posicion_texto


# ==========================================================================================
# El relevo (6C) · con la conversación tomada, Daniela no corre
# ==========================================================================================


def test_con_un_doctor_en_relevo_no_se_llama_al_modelo(monkeypatch):
    """No es que se le pida a Daniela que no conteste: es que no se la llama.

    La alternativa --dejarla correr y tirar su respuesta-- gastaría un turno de contexto por
    cada frase que el paciente escriba durante el relevo, y se encontraría ese historial
    entero al retomar, como si hubiera estado hablando ella.
    """
    base, turnos = preparar(
        monkeypatch, base=BaseFalsa(viva=("conv-viva", 4, True, 0), tomada="Dra. Ruiz")
    )
    wa = WhatsAppFalso()

    resultado = atender(mensaje_texto("me sigue doliendo"), whatsapp=wa)

    assert turnos.llamadas == [], "se llamó al modelo con la conversación tomada"
    assert wa.enviados == [], "Daniela le habló por encima del doctor"
    assert resultado.respondido is False
    assert resultado.motivo and "Dra. Ruiz" in resultado.motivo


def test_el_mensaje_del_relevo_no_vuelve_a_manos_de_daniela_media_hora_despues(monkeypatch):
    """`persistencia.mensajes_sin_responder` busca «`respondido_en` NULL Y `fallo_respuesta`
    NULL», que es la firma de «entró y nadie lo procesó». Un mensaje de relevo tiene esa
    misma forma --nadie le contestó por WhatsApp-- así que sin marcarlo, el barrido de
    arranque se lo entregaría a Daniela y le contestaría por encima del doctor.

    El prefijo `relevo:` es lo que permite separarlo de un fallo de verdad con un `LIKE`.
    """
    base, _ = preparar(
        monkeypatch, base=BaseFalsa(viva=("conv-viva", 4, True, 0), tomada="Dra. Ruiz")
    )

    atender(mensaje_texto("me sigue doliendo"))

    _, motivo = base.argumentos("marcar_fallo_respuesta")
    assert motivo.startswith("relevo:")
    assert "marcar_respondido" not in base.nombres


def test_el_relevo_mantiene_viva_la_conversacion(monkeypatch):
    """Sin `tocar_conversacion`, tres horas de charla con el doctor dejarían la conversación
    caducada para `conversacion_viva` y el paciente volvería a ser un primer contacto en
    cuanto Daniela retomara -- sin identidad verificada y sin una palabra de lo hablado.

    Y el turno NO avanza: no hubo turno que contar.
    """
    base, _ = preparar(
        monkeypatch, base=BaseFalsa(viva=("conv-viva", 4, True, 0), tomada="Dra. Ruiz")
    )

    resultado = atender(mensaje_texto("gracias"))

    assert base.argumentos("tocar_conversacion") == ("conv-viva", 4)
    assert resultado.turno == 4


def test_el_boton_llega_al_modelo_con_contexto():
    """«Confirmar» a secas no dice de qué.

    El rótulo de un quick reply llega sin nada alrededor: ni de qué mensaje viene, ni que fue
    un botón y no algo que el paciente escribió. Con el contexto delante, el modelo sabe que
    está respondiendo al recordatorio -- y el evaluador de `uso_indebido`, que recibe
    EXACTAMENTE esta misma cadena, deja de ver un imperativo suelto que parece una orden.
    """
    from maxicare_daniela.ingesta import MensajeEntrante

    entrada = atencion._entrada_para_el_modelo(
        MensajeEntrante(
            wamid="wamid.b",
            telefono="573001234567",
            nombre_perfil=None,
            tipo="button",
            texto="Confirmar",
        )
    )

    assert "Confirmar" in entrada
    assert "pulsó" in entrada or "boton" in entrada.lower() or "botón" in entrada


def test_un_texto_normal_no_se_disfraza_de_boton():
    """Lo de arriba vale para `button` y para nada más: un texto del paciente llega tal cual,
    que es lo que el resto del sistema --y los tres guardrails-- esperan leer."""
    from maxicare_daniela.ingesta import MensajeEntrante

    entrada = atencion._entrada_para_el_modelo(
        MensajeEntrante(
            wamid="wamid.t",
            telefono="573001234567",
            nombre_perfil=None,
            tipo="text",
            texto="Confirmar",
        )
    )

    assert entrada == "Confirmar"
# ==========================================================================================
# El rastro que el turno deja en el informe de «sin resolver»
#
# Es un OBSERVADOR: con la captura encendida o apagada, Daniela contesta exactamente igual.
# Lo que estas pruebas vigilan es lo contrario de lo habitual -- no que escriba, sino que
# escribir no pueda costarle nada al paciente.
# ==========================================================================================


async def _consulto(ctx, tratamiento="ortodoncia", concepto="precio", hubo_dato=False):
    """Lo que `herramientas._consultar_base_conocimiento` deja en el contexto al correr.

    Va por el `antes` de `Turnos`, es decir DESPUÉS de `ctx.turno.reiniciar()`, que es
    cuando ocurre de verdad: durante la corrida del modelo.
    """
    ctx.turno.senales.append(Senal(tratamiento, concepto, hubo_dato=hubo_dato))


def test_un_hueco_de_conocimiento_queda_escrito_con_la_frase_del_paciente(monkeypatch):
    """El cable entero: la tool anota en `ctx.turno`, y `_anotar_resultado` lo vuelca cuando
    el paciente ya tiene su respuesta. Sin esto, `SIN DATO DOCUMENTADO` no se contaba en
    ninguna parte y la clínica no podía saber qué ficha le falta."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0)),
        turnos=Turnos(antes=_consulto),
    )

    atender(mensaje_texto("cuanto me sale la ortodoncia en cuotas"))

    assert base.casos == [
        {
            "huella": "falta_dato:ortodoncia:precio",
            "tipo": "FALTA_DATO",
            "escalo": 0,
            "ejemplo": "cuanto me sale la ortodoncia en cuotas",
            "telefono": TELEFONO,
        }
    ]


def test_el_caso_se_escribe_DESPUES_de_responderle_al_paciente(monkeypatch):
    """El orden es la garantía entera de este módulo: el informe se escribe con el mensaje
    ya enviado, así que ni una consulta suya puede retrasar una respuesta."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0)),
        turnos=Turnos(antes=_consulto),
    )
    whatsapp = WhatsAppFalso()

    atender(mensaje_texto(), whatsapp=whatsapp)

    assert whatsapp.textos, "el paciente tiene que haber recibido su respuesta"
    assert base.nombres.index("tocar_conversacion") < base.nombres.index("registrar_caso")


def test_un_guardrail_que_salto_deja_su_caso_con_el_tratamiento_del_turno(monkeypatch):
    """`Resultado.tripwires` moría dentro del proceso: un guardrail que frenó a Daniela y se
    regeneró bien --el caso MÁS frecuente-- no dejaba rastro. Y la huella lleva el
    tratamiento que se consultó, que es lo que convierte «saltó 4 veces» en «las 4 eran por
    limpieza dental»."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0)),
        turnos=Turnos(
            antes=lambda ctx: _consulto(ctx, "limpieza", "precio", hubo_dato=True),
            tripwires=["sin_cifra_no_documentada"],
        ),
    )

    atender(mensaje_texto())

    assert [(c["huella"], c["tipo"]) for c in base.casos] == [
        ("guardrail:sin_cifra_no_documentada:limpieza", "GUARDRAIL")
    ]


def test_un_turno_que_revento_deja_un_caso_ROTO_con_el_tipo_y_no_con_el_mensaje(monkeypatch):
    """El mensaje de una excepción lleva ids y horas: con él dentro, cada error sería único y
    la tabla no agruparía jamás.

    Y UNA sola tarjeta. El turno reventado NO escala: ese `escalado_por = "dato_faltante"`
    del camino de reventón es un marcador sintético --`conversacion.responder` lanzó, así que
    `al_escalar` no corrió y ningún doctor fue avisado--, y pasárselo al informe abría un
    `humano:dato_faltante` con `escalo=1` además del `ROTO`. Dos daños: dos tarjetas para una
    historia, y la pantalla imprimiendo «se interrumpió al doctor 1 de N veces» sobre una
    interrupción que no existió."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0)),
        turnos=Turnos(revienta=RuntimeError("la tool 7 falló en la conversación abc-123")),
    )

    atender(mensaje_texto("me duele"))

    assert [(c["huella"], c["tipo"], c["escalo"]) for c in base.casos] == [
        ("roto:runtimeerror", "ROTO", 0),
    ]


def test_si_el_envio_falla_el_hueco_de_conocimiento_se_cuenta_igual(monkeypatch):
    """El turno ocurrió entero: lo que falló fue entregarlo. El hueco que Daniela encontró es
    el mismo, y encima hay un `ROTO` por el envío."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0)),
        turnos=Turnos(antes=_consulto),
    )
    whatsapp = WhatsAppFalso(falla_con=httpx.ConnectError("sin red"))

    atender(mensaje_texto("y la ortodoncia?"), whatsapp=whatsapp)

    assert [(c["huella"], c["tipo"]) for c in base.casos] == [
        ("falta_dato:ortodoncia:precio", "FALTA_DATO"),
        ("roto:connecterror", "ROTO"),
    ]


def test_un_mensaje_que_entra_durante_un_relevo_no_ensucia_el_informe(monkeypatch):
    """No negociable 15: ese `fallo_respuesta` empieza por `relevo:` y NO es un fallo. Sin el
    filtro, cada conversación que un doctor toma metería un caso `ROTO` inventado -- y
    justamente en las conversaciones que más se miran."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0), tomada="Dra. Ruiz"),
    )

    atender(mensaje_texto("me sigue doliendo"))

    assert base.casos == []


def test_un_turno_limpio_no_escribe_nada(monkeypatch):
    """Lo normal es que no haya caso. Una fila por turno convertiría el informe en un log."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-viva", 4, True, 0)),
        turnos=Turnos(antes=lambda ctx: _consulto(ctx, "implantes", "precio", hubo_dato=True)),
    )

    atender(mensaje_texto())

    assert base.casos == []


def test_el_ejemplo_guardado_es_lo_QUE_ESCRIBIO_el_paciente_y_no_el_prompt(monkeypatch):
    """Con dos mensajes, `_entrada_del_grupo` antepone una cabecera de instrucción para el
    modelo. Esa cabecera acaba en la pantalla de la clínica y en el informe que lee el
    modelo de la tarea 3: el ejemplo deja de leerse como la pregunta de un paciente."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-1", 4, True, 0)),
        turnos=Turnos(antes=_consulto),
    )
    whatsapp = WhatsAppFalso()

    async def escena():
        lider = asyncio.create_task(
            _atender(
                mensaje_texto("cuanto vale la ortodoncia?", wamid="w1"),
                whatsapp=whatsapp, **_en_grupo(),
            )
        )
        await asyncio.sleep(0.02)
        assert atencion._buferes, "el líder no abrió el grupo; la prueba no prueba nada"
        await _atender(
            mensaje_texto("y se puede en cuotas?", wamid="w2"),
            whatsapp=whatsapp, **_en_grupo(),
        )
        await lider

    asyncio.run(escena())

    assert [c["ejemplo"] for c in base.casos] == [
        "cuanto vale la ortodoncia?\ny se puede en cuotas?"
    ]
    assert "varios mensajes seguidos" not in base.casos[0]["ejemplo"], (
        "el andamiaje que se le arma al modelo no es la frase del paciente"
    )


def test_el_NOMBRE_DEL_ARCHIVO_no_se_guarda_como_frase_del_paciente(monkeypatch):
    """`_entrada_para_el_modelo` mete el nombre del archivo dentro del aviso que le arma al
    modelo. Guardarlo como ejemplo lo saca a una pantalla donde nadie lo pidió, y un nombre
    de archivo puede ser `cedula_1032....jpg`: regla dura 4, no se registran documentos de
    identidad de ningún tipo."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-1", 4, True, 0)),
        turnos=Turnos(antes=_consulto),
    )

    atender(
        mensaje_texto(
            tipo="image",
            texto="esto me lo tomaron ayer, cuanto vale arreglarlo?",
            media_id="media-1",
            mime="image/jpeg",
            nombre_archivo="cedula_1032456789.jpg",
        )
    )

    ejemplo = base.casos[0]["ejemplo"]
    assert ejemplo == "esto me lo tomaron ayer, cuanto vale arreglarlo?"
    assert "cedula" not in ejemplo and ".jpg" not in ejemplo


def test_un_turno_sin_texto_no_deja_ejemplo(monkeypatch):
    """Una radiografía sola no es una frase. El caso se cuenta igual; el ejemplo es `None`,
    y `registrar_caso` guarda `[]`."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-1", 4, True, 0)),
        turnos=Turnos(antes=_consulto),
    )

    atender(mensaje_texto(tipo="image", texto=None, media_id="m-1", mime="image/jpeg"))

    assert base.casos[0]["ejemplo"] is None


def test_un_caso_que_revienta_no_se_lleva_por_delante_a_los_demas(monkeypatch):
    """Sin un `try` por caso, el primero que falla mata el bucle y el turno pierde el resto
    -- un turno con hueco Y guardrail se quedaba sin el guardrail."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(viva=("conv-1", 4, True, 0), caso_revienta=RuntimeError("tabla rota")),
        turnos=Turnos(antes=_consulto, tripwires=["sin_cifra_no_documentada"]),
    )

    atender(mensaje_texto("cuanto vale?"))

    assert [c["tipo"] for c in base.casos] == ["FALTA_DATO", "GUARDRAIL"], (
        "el segundo caso ni se intentó"
    )


def test_un_caso_que_revienta_no_le_quita_la_respuesta_a_nadie(monkeypatch):
    """La razón por la que esto va al final de `_anotar_resultado` y dentro de su `try`. Un
    `tipo` fuera del CHECK, la tabla sin migrar, Neon cayéndose justo ahí: el paciente ya
    tiene su mensaje y el turno ya está contado. Se pierde un caso, y se pierde solo."""
    base, _ = preparar(
        monkeypatch,
        base=BaseFalsa(
            viva=("conv-viva", 4, True, 0), caso_revienta=RuntimeError("tabla sin migrar")
        ),
        turnos=Turnos(antes=_consulto),
    )
    whatsapp = WhatsAppFalso()

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert resultado.respondido is True
    assert whatsapp.textos == ["Claro que sí, con mucho gusto."]
    assert base.argumentos("tocar_conversacion") == ("conv-viva", 5), (
        "el turno se cuenta antes que el caso, así que un caso roto no se lo lleva"
    )


# ==========================================================================================
# El contacto y la señal de la baja
# ==========================================================================================


def test_el_estado_lleva_la_senal_de_la_baja():
    """Sale de la base y nunca del modelo, igual que `telefono_sin_paciente`. Si el modelo
    pudiera ponerla, bastaría con que dijera «no me escriban» para desactivar la
    reactivación de otro."""
    estado = atencion._Estado(
        id_conversacion="c-1",
        turno_actual=0,
        identidad_verificada=False,
        intentos_identificacion=0,
        id_paciente=None,
        nombre_paciente=None,
        telefono_sin_paciente=True,
        tomada_por=None,
        pidio_no_contacto=True,
    )

    assert estado.pidio_no_contacto is True
    assert estado.aviso_visto is False


def test_el_estado_por_defecto_no_tiene_la_senal_de_la_baja():
    """El default es contactable: la baja es algo que el paciente pide."""
    estado = atencion._Estado(
        id_conversacion="c-1",
        turno_actual=0,
        identidad_verificada=False,
        intentos_identificacion=0,
        id_paciente=None,
        nombre_paciente=None,
        telefono_sin_paciente=True,
        tomada_por=None,
    )

    assert estado.pidio_no_contacto is False


def test_el_contexto_recibe_la_senal_de_la_baja():
    """El fallo que esto evita: doce días después de darse de baja, María escribe por una
    muela rota. Para el sistema es una conversación nueva y en blanco, así que sin esta
    señal Daniela cierra como cierra siempre --«¿te escribo en unos días?»-- y le pide
    permiso para algo que ella ya negó expresamente."""
    ctx = contratos.ContextoDaniela(
        id_conversacion="c-1",
        telefono_completo="573001112201",
        database_url="postgres://nada",
        calendario=None,
        pidio_no_contacto=True,
    )

    assert ctx.pidio_no_contacto is True


def test_el_contexto_por_defecto_no_tiene_baja():
    ctx = contratos.ContextoDaniela(
        id_conversacion="c-1",
        telefono_completo="573001112201",
        database_url="postgres://nada",
        calendario=None,
    )

    assert ctx.pidio_no_contacto is False


def test_un_no_contactar_de_la_fila_de_verdad_llega_hasta_el_contexto(monkeypatch):
    """Extremo a extremo, con `_leer_estado` corriendo de verdad -- no un dataclass montado
    a mano. Las pruebas de arriba comprueban que `_Estado` y `ContextoDaniela` ACEPTAN el
    campo; ninguna comprueba que `_leer_estado` lo LEA de `contactos` ni que `atender` lo
    COPIE al contexto. Pasarían en verde con `_leer_estado` sin tocar y con `atender` sin
    pasar la señal -- exactamente lo que esta prueba existe para no dejar pasar."""
    _, turnos = preparar(
        monkeypatch,
        base=BaseFalsa(
            contacto={
                "telefono": TELEFONO,
                "creado_en": FECHA_CONTACTO,
                "actualizado_en": FECHA_CONTACTO,
                "aviso_mostrado_en": None,
                "politica_version": None,
                "no_contactar": True,
                "no_contactar_en": FECHA_CONTACTO,
                "no_contactar_origen": "paciente",
            }
        ),
    )

    atender(mensaje_texto())

    assert turnos.ctx.pidio_no_contacto is True


def test_si_asegurar_contacto_revienta_el_turno_responde_igual_y_calla_lo_comercial(
    monkeypatch,
):
    """`asegurar_contacto` es una escritura nueva y no esencial para el turno: nadie
    consume `pidio_no_contacto` todavía. Dejar que su excepción tumbe `_leer_estado` entero
    cambiaría un turno clínico completo -- Daniela sin contestar -- por una señal de
    consentimiento que hoy no hace nada, justo el empate que el principio del proyecto
    decide a favor de lo clínico.

    Se degrada hacia el lado seguro con `pidio_no_contacto=True`: no ofrecer nada comercial
    nunca es un daño. Y, como `leer_configuracion`, tiene que dejar la transacción limpia
    para las lecturas que siguen dentro del mismo `with`.
    """
    base, turnos = preparar(
        monkeypatch,
        base=BaseFalsa(contacto=RuntimeError("statement timeout")),
    )
    whatsapp = WhatsAppFalso()

    resultado = atender(mensaje_texto(), whatsapp=whatsapp)

    assert resultado.respondido is True
    assert whatsapp.textos and whatsapp.textos[0] != conversacion.MENSAJE_SEGURO, (
        "cayó al camino de «sin base»: la lectura entera se fue al suelo"
    )
    assert base.conexiones[0].rollbacks == 1, "la transacción quedó abortada y nadie la limpió"
    assert "conversacion_tomada" in base.nombres
    assert "ligar_mensaje_a_conversacion" in base.nombres
    assert turnos.ctx.pidio_no_contacto is True


def test_si_asegurar_contacto_revienta_tambien_se_degrada_a_aviso_no_visto(monkeypatch):
    """El otro lado de la misma degradación, a nivel de `_Estado` -- `ContextoDaniela` no
    lleva `aviso_visto` todavía (Tarea 3), así que esto no se puede ver desde `ctx`. Volver
    a enseñar un aviso ya visto es inocuo: por eso el lado seguro aquí es `False`, no `True`.
    """
    base = BaseFalsa(contacto=RuntimeError("statement timeout")).instalar(monkeypatch)

    estado = atencion._leer_estado("postgres://nada", TELEFONO, [])

    assert estado.pidio_no_contacto is True
    assert estado.aviso_visto is False
    assert base.conexiones[0].rollbacks == 1

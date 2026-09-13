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
import random
import time
from datetime import datetime

import httpx
import pytest

from maxicare_daniela import atencion, conversacion, ingesta, persistencia
from maxicare_daniela.calendario import CalendarioCaido, CalendarioDoble, ErrorDeCalendario
from maxicare_daniela.canales import ErrorDeCanal
from maxicare_daniela.config import Config
from maxicare_daniela.contratos import RespuestaDaniela

TELEFONO = "573001112233"
OTRO_TELEFONO = "573009998877"

CONFIGURACION_OPERATIVA = {
    "capacidad_por_hora": 3,
    "duracion_cita_minutos": 45,
    "cierre_relevo_minutos": 120,
    "telegram_topic_general": 7,
}


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

    async def enviar_mensaje(self, texto: str, *, tema_id=None, teclado=None) -> int:
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
    ) -> None:
        self.viva = viva
        self.paciente = paciente
        self.tomada = tomada
        self.configuracion = CONFIGURACION_OPERATIVA if configuracion is None else configuracion
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

        monkeypatch.setattr(persistencia, "conectar", conectar)
        monkeypatch.setattr(persistencia, "conversacion_viva", conversacion_viva)
        monkeypatch.setattr(
            persistencia, "buscar_paciente_por_telefono", buscar_paciente_por_telefono
        )
        monkeypatch.setattr(persistencia, "asegurar_conversacion", asegurar_conversacion)
        monkeypatch.setattr(persistencia, "leer_configuracion", leer_configuracion)
        monkeypatch.setattr(persistencia, "conversacion_tomada", conversacion_tomada)
        monkeypatch.setattr(
            persistencia, "ligar_mensaje_a_conversacion", ligar_mensaje_a_conversacion
        )
        monkeypatch.setattr(persistencia, "tocar_conversacion", tocar_conversacion)
        monkeypatch.setattr(persistencia, "marcar_respondido", marcar_respondido)
        monkeypatch.setattr(persistencia, "marcar_fallo_respuesta", marcar_fallo_respuesta)
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
    ) -> None:
        self.texto = texto
        self.llamadas: list[dict] = []
        self._antes = antes
        self._revienta = revienta
        self._escalado_por = escalado_por

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
    """`_candados` y `_sesiones` son estado de módulo: sobreviven entre pruebas.

    Sin esto, una prueba pasa sola y falla dentro de la suite -- o al revés, que es peor.
    """
    atencion._candados.clear()
    atencion._sesiones.clear()


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
# 16 · La poda
# ==========================================================================================


def test_las_sesiones_viejas_se_podan(monkeypatch):
    """El historial vive en memoria y nadie lo borra: sin poda, `_sesiones` crece con cada
    número que escriba a la clínica y no baja nunca.

    La ventana es la misma de `conversacion_viva`: pasadas 24 horas la conversación ya está
    muerta para la base, así que su historial tampoco sirve para nada.
    """
    preparar(monkeypatch, base=BaseFalsa(viva=("conv-viva", 1, True, 0)))
    vieja = conversacion.SesionEnMemoria("conv-antigua")
    ahora = time.monotonic()
    atencion._sesiones["conv-antigua"] = (
        vieja,
        ahora - (atencion.VENTANA_CONVERSACION_HORAS + 1) * 3600,
    )
    reciente = conversacion.SesionEnMemoria("conv-viva")
    atencion._sesiones["conv-viva"] = (reciente, ahora)

    atender(mensaje_texto())

    assert "conv-antigua" not in atencion._sesiones
    assert atencion._sesiones["conv-viva"][0] is reciente, (
        "una conversación viva no puede perder su historial a mitad de la charla"
    )


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

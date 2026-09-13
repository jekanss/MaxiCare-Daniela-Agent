"""Un turno de Daniela. El único sitio del proyecto donde se llama a `Runner.run`.

------------------------------------------------------------------------------------------
Por qué este módulo existe y no vive dentro de `runtime.py`
------------------------------------------------------------------------------------------

La plantilla del proyecto pone `Runner.run` en `runtime.py`. Aquí se separa, y la razón es
concreta: **hay dos transportes que necesitan exactamente este mismo turno**. El chat de
pruebas de la interfaz web (fase 5) y el webhook de WhatsApp (fase 6). Con la lógica dentro
de `runtime.py` solo hay dos salidas, y las dos son malas: duplicarla, o que `ingesta.py`
importe `runtime` -- que es justo lo que el detector de `tests/test_estructura.py` prohíbe.

La frontera que importa se respeta igual, y de hecho mejor: este módulo no sabe si el
mensaje llegó por HTTP, por WhatsApp o por un cron, y por eso un turno completo --con sus
guardrails, su reintento y su escalamiento-- se puede probar sin levantar un servidor.

Lo que NO está aquí, y es deliberado:

* **El retardo humano** de `config.RETARDO_RESPUESTA_SEGUNDOS`. Es una propiedad del canal
  de WhatsApp --que una respuesta instantánea delate a un bot-- y no del turno. En el chat
  de pruebas, esperar 55 segundos por iteración de prompt haría la pantalla inservible.
* **Cómo se entrega la respuesta.** `responder` devuelve un `Resultado`; quien lo envía por
  WhatsApp, lo pinta en el navegador o lo imprime en una terminal es el que llamó.

------------------------------------------------------------------------------------------
La regla que atraviesa todo, de `fallos.escalamiento`
------------------------------------------------------------------------------------------

    PASE LO QUE PASE sale un mensaje al paciente.
    Nunca un error técnico, nunca silencio, nunca una confirmación falsa.

Por eso `responder` no propaga ninguna excepción del SDK: las traduce. La única que se deja
subir es `UserError`, porque es un defecto de configuración --un `output_type` imposible,
una tool mal declarada-- y taparlo con un mensaje amable lo esconde hasta producción.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agents import (
    Agent,
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
    UserError,
)

from . import persistencia
from .agentes import daniela as agente_daniela
from .config import LIMITE_TURNOS, WORKFLOW_NAME, config_de_corrida
from .contratos import ContextoDaniela, MotivoEscalamiento, RespuestaDaniela

log = logging.getLogger("maxicare.conversacion")

#: Las cuatro de tripwire. La política es la misma para las cuatro --regenerar una vez, y a
#: la segunda mensaje seguro más escalamiento-- así que se atrapan juntas.
TRIPWIRES = (
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
)

# ==========================================================================================
# Los textos que ve el paciente cuando algo falla
# ==========================================================================================
#
# Están aquí, juntos y como constantes, para que se puedan leer de corrido y para que nadie
# los improvise dentro de un `except`. Usan el vocabulario de `fallos.que_pasa_si_no_sabe`:
# Daniela no sabe, lo dice, y pasa a alguien que sí -- sin inventar una causa técnica y sin
# prometer un plazo que nadie se comprometió a cumplir.

MENSAJE_SEGURO = (
    "Prefiero que esto te lo confirme directamente el doctor para no darte un dato "
    "equivocado. Ya le paso tu mensaje y te escribe apenas pueda. 🙏"
)

MENSAJE_LIMITE_TURNOS = (
    "Llevamos un rato dándole vueltas y no quiero enredarte más. Le paso tu caso al doctor "
    "para que lo vea directamente y te contacte."
)

MENSAJE_FALLO_TECNICO = (
    "Se me complicó revisar eso en este momento. Ya le avisé al equipo y te confirmamos "
    "apenas lo tengamos."
)

#: La corrección que se le da al modelo al regenerar. Es explícita a propósito: «vuelve a
#: intentarlo» sin decir qué estuvo mal produce el mismo mensaje y quema el reintento.
CORRECCION = (
    "AVISO DEL SISTEMA: tu respuesta anterior fue bloqueada por un control de seguridad "
    "({motivo}). Vuelve a responder al paciente SIN incluir ninguna cifra, hora, fecha ni "
    "valoración clínica que no te haya devuelto una tool en este mismo turno. Si te falta "
    "el dato, dilo y ofrece confirmarlo; no lo estimes."
)


class SesionEnMemoria:
    """Historial de conversación que vive en el proceso y se pierde al reiniciarlo.

    Implementa el *protocolo* `Session` del SDK -- `get_items`, `add_items`, `pop_item`,
    `clear_session` -- sin heredar de `SessionABC`, que la documentación del propio SDK
    reserva para sus implementaciones internas.

    **Es el doble de las pruebas offline, y de una parte de producción que todavía no se
    cerró.** En el WhatsApp de pacientes reales corre `persistencia.sesion_de_agente`, sobre
    Neon, desde la fase 7 (`atencion.atender`, con su default cableado a esa fábrica). El
    chat web del panel, en cambio, **sigue construyendo esta misma clase** --
    `runtime.py:749` y `runtime.py:954`, sin pasar por `persistencia`-- así que ahí el
    historial todavía se pierde al reiniciar el proceso. Cerrar esa mitad es PENDIENTE de la
    Tarea 6.

    Y sigue siendo, además, el doble de las pruebas offline: existe para que
    `uv run pytest -q` siga corriendo en dos segundos y sin señal -- una suite que exige
    internet es una suite que alguien acaba saltándose.

    Un doble SIN historial no serviría: Daniela no recordaría el mensaje anterior y cada
    turno empezaría de cero, que es justo lo que no se quiere probar.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.session_settings = None
        self._items: list[Any] = []

    async def get_items(self, limit: int | None = None) -> list[Any]:
        # `limit` devuelve los ÚLTIMOS N en orden cronológico, no los primeros. Confundirlo
        # le daría al modelo el principio de la conversación y nunca el final.
        return list(self._items) if limit is None else list(self._items[-limit:])

    async def add_items(self, items: list[Any]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> Any | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


@dataclass
class Resultado:
    """Lo que el transporte necesita saber, y nada más.

    `respuesta` nunca es `None`: si el turno falló, trae uno de los textos de arriba. Es la
    forma de que la regla «pase lo que pase sale un mensaje» no dependa de que quien llame
    se acuerde de comprobar un `None`.
    """

    respuesta: RespuestaDaniela
    turno: int
    #: Qué guardrails saltaron en este turno, por nombre. Vacío es lo normal.
    tripwires: list[str] = field(default_factory=list)
    #: True si hubo que regenerar. Interesa para la métrica, no para el paciente.
    regenerado: bool = False
    #: Por qué se escaló, o `None`. `RespuestaDaniela.requiere_escalamiento` es lo que pide
    #: el modelo; esto es lo que decidió el orquestador, que puede escalar sin que el modelo
    #: lo haya pedido -- por ejemplo cuando se acabaron los turnos.
    escalado_por: MotivoEscalamiento | None = None
    #: La excepción que se tradujo, si hubo. Para el log, nunca para el paciente.
    fallo: str | None = None


def _respuesta_de_emergencia(
    texto: str, *, motivo: MotivoEscalamiento = "dato_faltante"
) -> RespuestaDaniela:
    """Una `RespuestaDaniela` válida construida por el código, no por el modelo.

    `estado_oportunidad='con_barrera'` y `barrera='ninguna'` no son adornos: son los valores
    que dicen «esta conversación está detenida por algo que no es una objeción del paciente»,
    y evitan que un fallo técnico se cuele en las métricas como si fuera una barrera
    comercial que nadie tuvo.
    """
    return RespuestaDaniela(
        mensaje_al_paciente=texto,
        estado_oportunidad="con_barrera",
        barrera_detectada="ninguna",
        requiere_escalamiento=True,
        motivo_escalamiento=motivo,
        fuera_de_alcance=False,
    )


def _nombre_del_tripwire(excepcion: Exception) -> str:
    """El nombre del guardrail que saltó, o el de la excepción si no se puede averiguar.

    El SDK guarda el resultado en `guardrail_result` y el nombre se lee con `.get_name()`
    -- no es un atributo `.name`. Se envuelve en try porque este dato es para un log y para
    una métrica: no vale la pena que un cambio de forma del SDK tumbe un turno.
    """
    try:
        return excepcion.guardrail_result.guardrail.get_name()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- ver docstring
        return type(excepcion).__name__


def _config_de_corrida() -> RunConfig:
    """El `RunConfig` de cada turno de Daniela, con el tracing sin contenido.

    Lo que había aquí era `RunConfig(workflow_name=WORKFLOW_NAME)` a secas, y los defaults
    del SDK 0.22.2 --introspeccionados-- traen `trace_include_sensitive_data=True`. La
    consecuencia no era teórica: desde que se desplegó la fase 6A, cada conversación de
    WhatsApp subía íntegra al dashboard de OpenAI --lo que escribe el paciente, lo que
    responde Daniela, y las entradas y salidas de las nueve tools, que incluyen su nombre,
    su teléfono y sus citas--. Que los traces suben de verdad ya estaba comprobado en este
    proyecto: fue lo que desconcertaba durante el fallo de la `OPENAI_BASE_URL` vacía, cuando
    las trazas llegaban mientras toda llamada al modelo moría.

    `config.TRACE_INCLUDE_SENSITIVE_DATA` existía desde la fase 1 con su porqué escrito
    --`datos.datos_sensibles` tiene contenido-- y no la cableaba nadie más que `lectura.py`,
    que cerró su lado en la fase 6B. Este es el otro lado.

    Va en CÓDIGO y no en una variable de entorno a propósito: `OPENAI_AGENTS_TRACE_INCLUDE_
    SENSITIVE_DATA` apagaría lo mismo, pero una variable de entorno se olvida en el siguiente
    servidor y el fallo vuelve sin que nada avise.

    La construcción vive en `config.config_de_corrida`, que es por donde pasan los TRES
    consumidores de modelo del proyecto. Estaba duplicada, y esa duplicación es justamente
    cómo la misma fuga acabó abierta en dos sitios a la vez.
    """
    return config_de_corrida()


async def responder(
    entrada: str,
    *,
    ctx: ContextoDaniela,
    agente: Agent | None = None,
    sesion: Any = None,
    run_config: RunConfig | None = None,
    max_turns: int = LIMITE_TURNOS,
    al_escalar: Callable[[ContextoDaniela, MotivoEscalamiento, str], Awaitable[None]] | None = None,
) -> Resultado:
    """Corre un turno completo y devuelve siempre algo que se le puede decir al paciente.

    `agente` se puede sustituir --lo usan las pruebas con `ModeloGuionizado`-- pero por
    omisión es el `daniela` de producción, que es lo que exige el entregable de la fase 5:
    el chat del navegador habla con el mismo agente que va a correr en WhatsApp, no con una
    copia parecida.

    `al_escalar` lo aporta el transporte. En WhatsApp avisará a los doctores por Telegram;
    en el chat de pruebas no hay a quién avisar, y que sea `None` es la forma de que una
    prueba no le haga sonar el teléfono a un doctor de verdad.
    """
    agente = agente or agente_daniela
    run_config = run_config or _config_de_corrida()

    # El contrato de `DatosDelTurno`: se vacía ANTES de correr, no después. Si se vaciara al
    # terminar, un turno que reventara a mitad dejaría autorizadas las cifras del anterior.
    ctx.turno.reiniciar()
    ctx.turno_actual += 1

    resultado = Resultado(respuesta=_respuesta_de_emergencia(MENSAJE_FALLO_TECNICO), turno=ctx.turno_actual)

    async def _correr(texto: str) -> RespuestaDaniela:
        corrida = await Runner.run(
            agente,
            texto,
            context=ctx,
            session=sesion,
            max_turns=max_turns,
            run_config=run_config,
        )
        return corrida.final_output

    try:
        resultado.respuesta = await _correr(entrada)

    except TRIPWIRES as e:
        nombre = _nombre_del_tripwire(e)
        resultado.tripwires.append(nombre)
        log.warning("tripwire %s en %s · se regenera una vez", nombre, ctx.id_conversacion)

        # `fallos.excepciones_manejadas`: regenerar UNA vez con la corrección explícita.
        # Nunca dos -- el plan es tajante, y con razón: un guardrail que salta dos veces
        # está diciendo que el modelo no va a corregirse solo, y el tercer intento solo
        # gasta dinero y minutos del paciente.
        try:
            resultado.respuesta = await _correr(CORRECCION.format(motivo=nombre))
            resultado.regenerado = True
        except TRIPWIRES as segunda:
            segundo_nombre = _nombre_del_tripwire(segunda)
            resultado.tripwires.append(segundo_nombre)
            resultado.respuesta = _respuesta_de_emergencia(MENSAJE_SEGURO)
            resultado.escalado_por = "clinico" if "clinic" in segundo_nombre else "dato_faltante"
            resultado.fallo = f"tripwire repetido: {nombre} y luego {segundo_nombre}"
            log.error("segundo tripwire (%s) en %s · se escala", segundo_nombre, ctx.id_conversacion)

    except MaxTurnsExceeded as e:
        # Sin reintento, y el plan explica por qué: si no cerró en quince turnos, más turnos
        # no van a cerrarlo.
        resultado.respuesta = _respuesta_de_emergencia(MENSAJE_LIMITE_TURNOS)
        resultado.escalado_por = "dato_faltante"
        resultado.fallo = f"MaxTurnsExceeded: {e}"
        log.error("límite de turnos en %s", ctx.id_conversacion)

    except ModelBehaviorError as e:
        # Un reintento acotado. Si se repite, el problema es el prompt o un `output_type`
        # demasiado estrecho, no la red -- y entonces sí se escala.
        log.warning("ModelBehaviorError en %s · un reintento", ctx.id_conversacion)
        try:
            resultado.respuesta = await _correr(entrada)
            resultado.regenerado = True
        except AgentsException as segunda:
            resultado.respuesta = _respuesta_de_emergencia(MENSAJE_FALLO_TECNICO)
            resultado.escalado_por = "dato_faltante"
            resultado.fallo = f"ModelBehaviorError repetido: {e} / {segunda}"
            log.exception("ModelBehaviorError repetido en %s", ctx.id_conversacion)

    except UserError:
        # Se deja subir. `UserError` hereda de `AgentsException` (comprobado contra la 0.22.2
        # instalada), así que sin este `except` puesto ANTES la red de seguridad de abajo se
        # lo tragaría. Y es justo lo que no debe pasar: un `output_type` imposible o una tool
        # mal declarada son defectos de configuración, y taparlos con un mensaje amable al
        # paciente los esconde hasta producción.
        raise

    except AgentsException as e:
        # La red de seguridad, nunca el único `except`: registra y escala.
        resultado.respuesta = _respuesta_de_emergencia(MENSAJE_FALLO_TECNICO)
        resultado.escalado_por = "dato_faltante"
        resultado.fallo = f"{type(e).__name__}: {e}"
        log.exception("excepción del SDK en %s", ctx.id_conversacion)

    if resultado.escalado_por is None and resultado.respuesta.requiere_escalamiento:
        motivo = resultado.respuesta.motivo_escalamiento
        resultado.escalado_por = None if motivo == "ninguno" else motivo  # type: ignore[assignment]

    _guardar_estado(ctx, resultado)

    if resultado.escalado_por is not None and al_escalar is not None:
        try:
            await al_escalar(ctx, resultado.escalado_por, resultado.respuesta.mensaje_al_paciente)
        except Exception:  # noqa: BLE001
            # Que no se pueda avisar al doctor no puede impedir que el paciente reciba su
            # mensaje. El escalamiento fallido queda en el log; la respuesta sale igual.
            log.exception("no se pudo escalar %s", ctx.id_conversacion)

    return resultado


def _guardar_estado(ctx: ContextoDaniela, resultado: Resultado) -> None:
    """Persiste el estado de la oportunidad. Un fallo aquí se registra y no detiene nada.

    De esta tabla salen seis de las siete métricas, así que perder una escritura importa --
    pero importa menos que dejar sin respuesta a un paciente por un problema de base de
    datos. Por eso se registra con `exception` y se sigue.
    """
    try:
        with persistencia.conectar(ctx.database_url) as conn:
            persistencia.upsert_estado_oportunidad(
                conn,
                ctx.id_conversacion,
                estado=resultado.respuesta.estado_oportunidad,
                barrera=resultado.respuesta.barrera_detectada,
                fuera_de_alcance=resultado.respuesta.fuera_de_alcance,
                notas=resultado.fallo,
            )
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo guardar el estado de %s", ctx.id_conversacion)


__all__ = [
    "CORRECCION",
    "MENSAJE_LIMITE_TURNOS",
    "MENSAJE_SEGURO",
    "Resultado",
    "SesionEnMemoria",
    "responder",
]

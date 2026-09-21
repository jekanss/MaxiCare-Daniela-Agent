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

from . import consumo, guardrails, persistencia
from .agentes import VERSION_PROMPT, daniela as agente_daniela
from .config import LIMITE_TURNOS, MODELO_DANIELA, WORKFLOW_NAME, config_de_corrida
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

#: Para cuando alguien le encarga a Daniela un trabajo que no es de la clínica: escribir
#: código, traducir, hacerle un ejercicio. `uso_indebido` lo para --y tiene que seguir
#: parándolo, o el sistema se vuelve un ChatGPT gratis pagado por la clínica-- pero **eso no
#: es un ataque ni una duda del doctor**, así que no puede terminar en `MENSAJE_SEGURO`.
#:
#: Aquella frase promete dos cosas que aquí son falsas: que el doctor va a confirmar el dato,
#: y que alguien va a escribir. Medido el 21/09/2026 con «hazme un bucle infinito».
#:
#: NO lo escribe el modelo: llega por el mismo camino que los otros tres, dentro de un
#: `except`, con el turno ya abortado por el tripwire. Por eso es un literal y no una
#: instrucción del prompt -- el modelo nunca llegó a correr.
#: **Sin rayas largas (—), y eso vale para los cuatro mensajes de aquí arriba.** Nadie las
#: escribe en WhatsApp: se teclean con una combinación que no está en el teclado del celular,
#: así que un mensaje que las lleva se lee como generado. Esta frase nació con dos, el
#: 21/09/2026, y las cazó el cliente el mismo día.
MENSAJE_FUERA_DE_ALCANCE = (
    "Con eso no te puedo ayudar, es que yo solo sé de MaxiCare 😅 Pero si necesitas algo de "
    "la clínica (tratamientos, precios, horarios o agendar tu cita), dime y lo vemos."
)

#: La corrección que se le da al modelo al regenerar. Es explícita a propósito: «vuelve a
#: intentarlo» sin decir qué estuvo mal produce el mismo mensaje y quema el reintento.
#: `{motivo}` lo rellena `_motivo_del_tripwire`, y lo que entra ahí es la frase que compuso
#: el guardrail --qué sobró y qué tool la habría verificado--, no su nombre. La diferencia
#: entre las dos cosas es la diferencia entre un reintento que agenda y uno que se disculpa.
CORRECCION = (
    "AVISO DEL SISTEMA: tu respuesta anterior fue bloqueada por un control de seguridad.\n"
    "{motivo}\n"
    "Vuelve a responder al paciente SIN incluir ninguna cifra, hora, fecha ni valoración "
    "clínica que no te haya devuelto una tool en este mismo turno. Si te falta el dato, "
    "llama a la tool que lo tiene; si aun así no puedes verificarlo, dilo y ofrece "
    "confirmarlo. No lo estimes."
)


def _sin_guardrails_de_entrada(agente: Agent) -> Agent:
    """El mismo agente, sin los guardrails que juzgan lo que escribe el paciente.

    Se usa SOLO para la regeneración, y es un arreglo con fecha: el 13/09/2026 un paciente
    pidió «una valoración para el próximo martes 15 a las 10 am» y se fue con «ya le paso tu
    mensaje al doctor». No había cupo lleno ni calendario caído --el martes 15 a las 10:00
    estaba libre--: `sin_hora_no_verificada` frenó la primera respuesta, que es su trabajo, y
    el reintento que existe para arreglarla murió contra `uso_indebido`.

    Contra `uso_indebido` porque lo que se le manda al regenerar es `CORRECCION`, que empieza
    con «AVISO DEL SISTEMA» y le reescribe la conducta a Daniela. Eso es, literalmente, la
    forma de una inyección de prompt, y el evaluador la clasificaba como tal: «Intenta
    imponer instrucciones del sistema y modificar el comportamiento de la asistente».
    Medido contra el modelo real ese día, con esas palabras. No era intermitente: **toda**
    regeneración terminaba en el mensaje seguro y un escalamiento. En producción eso se ve
    como «Daniela no agenda», y cuanto mejor hacía su trabajo el guardrail de salida, más
    seguido pasaba.

    La corrección NO la escribe el paciente: la escribe este módulo. Pasarla por un guardrail
    de ENTRADA es una confusión de categoría, no una regla estricta de más. El mensaje del
    paciente ya se revisó en la primera corrida; volver a revisarlo aquí no protegía nada que
    no estuviera protegido.

    Lo único que no es texto fijo es el `{motivo}`, y conviene saber exactamente qué entra
    ahí: de `sin_cifra_no_documentada` y `sin_hora_no_verificada`, cifras y horas que
    `cifras_de`/`horas_de` extrajeron ya normalizadas --dígitos y `HH:MM`, nada más--; de
    `sin_lectura_clinica`, una frase de máximo 300 caracteres que escribe el modelo evaluador
    sobre la respuesta de la propia Daniela. Ninguna la teclea el paciente, pero la última la
    redacta un modelo, así que no es «texto nuestro» sin más y no se va a describir como tal.

    Los guardrails de SALIDA se conservan enteros, y son los que importan para la seguridad
    clínica: la respuesta regenerada pasa por los mismos tres filtros que la primera. Si
    vuelve a dar una hora sin verificar, sigue saltando y sigue escalando.
    """
    if not agente.input_guardrails:
        return agente
    return agente.clone(input_guardrails=[])


class SesionEnMemoria:
    """Historial de conversación que vive en el proceso y se pierde al reiniciarlo.

    Implementa el *protocolo* `Session` del SDK -- `get_items`, `add_items`, `pop_item`,
    `clear_session` -- sin heredar de `SessionABC`, que la documentación del propio SDK
    reserva para sus implementaciones internas.

    **Es el doble de las pruebas offline. Ya NO es lo que corre en ningún carril real.** En
    el WhatsApp de pacientes reales corre `persistencia.sesion_de_agente`, sobre Neon, desde
    la fase 7 (`atencion.atender`, con su default cableado a esa fábrica). Desde la Tarea 6,
    el chat de pruebas del panel también: `runtime._contexto_de_prueba` pide su sesión a
    `persistencia.sesion_de_agente(..., esquema=runtime.ESQUEMA_PRUEBAS_WEB)`, así que cada
    turno del chat web SÍ queda escrito en `agent_messages` de `pruebas_web`, filas incluidas.

    Lo que NO sobrevive es el REENGANCHE con esas filas: tras reiniciar el proceso,
    `runtime._conversaciones_de_prueba` (el diccionario en memoria) está vacío, así que el
    `id_conversacion` que el navegador todavía recuerda no se reconoce, y
    `persistencia.asegurar_conversacion` -- que SIEMPRE inserta, nunca reutiliza -- le abre
    una fila nueva. Las filas viejas quedan huérfanas en `pruebas_web`, alcanzables solo con
    una consulta manual. Es una decisión, no un defecto: este carril es la pantalla donde la
    clínica prueba a Daniela desde cero, y tiene que poder abrir un «primer contacto» sin
    pedirle a nadie un `/clearstate` antes. En WhatsApp esto no pasa -- ahí
    `conversacion_viva` SÍ reutiliza la conversación de las últimas 24 h.

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
    #: Si `al_escalar` llegó a interrumpir a alguien. `None` es «no se intentó» --no hubo
    #: escalamiento, o el transporte no trae a quién avisar, que es el caso del chat web--.
    #: `False` significa que se intentó y no hubo interrupción: el aviso se calló porque el
    #: doctor ya tenía ese asunto delante sin responder, o Telegram lo rechazó. Lo lee
    #: `atencion` para no contar en la pantalla del cliente una interrupción que no existió.
    doctor_avisado: bool | None = None


def _respuesta_de_emergencia(
    texto: str,
    *,
    motivo: MotivoEscalamiento = "dato_faltante",
    escala: bool = True,
    fuera_de_alcance: bool = False,
) -> RespuestaDaniela:
    """Una `RespuestaDaniela` válida construida por el código, no por el modelo.

    `estado_oportunidad='con_barrera'` y `barrera='ninguna'` no son adornos: son los valores
    que dicen «esta conversación está detenida por algo que no es una objeción del paciente»,
    y evitan que un fallo técnico se cuele en las métricas como si fuera una barrera
    comercial que nadie tuvo.

    **`escala=False` existe porque poner `resultado.escalado_por = None` NO basta**, y eso
    costó una prueba en rojo el 21/09/2026. Al final de `responder` hay un respaldo --«si
    nadie fijó el motivo pero la respuesta pide escalamiento, se escala»-- que existe para
    que un `requiere_escalamiento` del MODELO no se pierda. Como esta fábrica nace con ese
    campo en `True`, ese respaldo volvía a poner el escalamiento que el `except` acababa de
    quitar: el único camino que de verdad lo apaga es no pedirlo desde el principio. El
    motivo cae a `'ninguno'` solo, porque un motivo de escalamiento sin escalamiento es un
    dato que contradice al de al lado.
    """
    return RespuestaDaniela(
        mensaje_al_paciente=texto,
        estado_oportunidad="con_barrera",
        barrera_detectada="ninguna",
        requiere_escalamiento=escala,
        motivo_escalamiento=motivo if escala else "ninguno",
        fuera_de_alcance=fuera_de_alcance,
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


def _motivo_del_tripwire(excepcion: Exception) -> str:
    """Lo que el guardrail dejó DICHO, no cómo se llama.

    `Veredicto` ya lo pedía en su docstring --«sin decirle al modelo QUÉ cifra sobra, el
    segundo intento es tan ciego como el primero»-- y `responder` no lo cumplía: rellenaba el
    `{motivo}` de `CORRECCION` con `_nombre_del_tripwire`, así que al modelo le llegaba
    «sin_hora_no_verificada» y nada más. El texto bueno --«Mencionaste 10:00 sin que ninguna
    tool lo haya verificado en este turno. Llama a `consultar_disponibilidad` y ofrece solo
    lo que devuelva»-- se calculaba, viajaba en `output_info` y se tiraba a la basura.

    Se notaba en el resultado, no en un log: regenerando el turno del 13/09/2026 con el
    modelo real, Daniela contestaba «lo confirmo con la clínica» sin llamar a la tool. No
    escalaba, pero tampoco agendaba, teniendo el martes a las 10:00 libre.

    Mismo `try` amplio que `_nombre_del_tripwire` y por la misma razón: si el SDK cambia de
    forma, se cae al nombre y el turno sigue.
    """
    info = _info_del_tripwire(excepcion)
    # `uso_indebido` manda un dict --motivo + categoría-- desde el 21/09/2026; los otros
    # cinco siguen mandando texto. Se leen las dos formas en vez de migrar los seis: los
    # otros no tienen ninguna categoría que distinguir, y darles una vacía sería inventarles
    # un campo para que este `if` quedara más corto.
    if isinstance(info, dict):
        motivo = str(info.get("motivo") or "").strip()
        if motivo:
            return motivo
    elif isinstance(info, str) and info.strip():
        return info.strip()
    return _nombre_del_tripwire(excepcion)


def _info_del_tripwire(excepcion: Exception):
    """Lo que el guardrail dejó en `output_info`, sea de la forma que sea, o `None`.

    Mismo `try` amplio que sus dos vecinas: si el SDK cambia de forma, esto devuelve `None`,
    el motivo cae al nombre del guardrail y el turno sigue.
    """
    try:
        return excepcion.guardrail_result.output.output_info  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- ver docstring
        return None


def _categoria_del_tripwire(excepcion: Exception) -> str:
    """De qué clase era lo que paró `uso_indebido`. Cadena vacía para los demás guardrails.

    Lo que decide es si se interrumpe a un doctor, así que **el valor por defecto es el que
    escala**: cualquier cosa que no sea exactamente `CATEGORIA_TAREA_AJENA` --un dict sin el
    campo, una cadena, un `None` porque el SDK cambió-- cae del lado de siempre. Ahorrarse un
    escalamiento nunca puede ser el resultado de un dato que no llegó.
    """
    info = _info_del_tripwire(excepcion)
    if isinstance(info, dict):
        return str(info.get("categoria") or "")
    return ""


def _config_de_corrida(ctx: ContextoDaniela) -> RunConfig:
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

    Desde la fase 7 lleva además el `group_id` de la conversación, que es lo que agrupa las
    trazas de un episodio -- las de Daniela, las del lector y las de los evaluadores.
    """
    return config_de_corrida(
        group_id=ctx.id_conversacion,
        canal=ctx.canal,
        version_prompt=VERSION_PROMPT,
    )


def _modelo_de(agente: Agent) -> str:
    """El identificador del modelo que usa ese agente, para poder ponerle precio.

    `Agent.model` admite un string o un objeto `Model`, y las pruebas le pasan un
    `ModeloGuionizado` que no es ninguno de los dos. Un `str()` a secas escribiría el `repr`
    de ese objeto en la columna `modelo` de cada fila; se prefiere el nombre de producción,
    que al menos es un identificador real, y así el consumo de las pruebas no ensucia el
    informe con una categoría inventada.
    """
    modelo = getattr(agente, "model", None)
    return modelo if isinstance(modelo, str) and modelo else MODELO_DANIELA


async def responder(
    entrada: str,
    *,
    ctx: ContextoDaniela,
    agente: Agent | None = None,
    sesion: Any = None,
    run_config: RunConfig | None = None,
    max_turns: int = LIMITE_TURNOS,
    al_escalar: Callable[[ContextoDaniela, MotivoEscalamiento, str], Awaitable[bool | None]] | None = None,
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
    run_config = run_config or _config_de_corrida(ctx)

    # El contrato de `DatosDelTurno`: se vacía ANTES de correr, no después. Si se vaciara al
    # terminar, un turno que reventara a mitad dejaría autorizadas las cifras del anterior.
    ctx.turno.reiniciar()
    ctx.turno_actual += 1

    resultado = Resultado(respuesta=_respuesta_de_emergencia(MENSAJE_FALLO_TECNICO), turno=ctx.turno_actual)

    async def _correr(texto: str, *, usando: Agent | None = None) -> RespuestaDaniela:
        en_uso = usando or agente
        corrida = await Runner.run(
            en_uso,
            texto,
            context=ctx,
            session=sesion,
            max_turns=max_turns,
            run_config=run_config,
        )
        # Una fila POR CORRIDA, así que una regeneración cuenta aparte y se ve como lo que
        # es: el turno que costó el doble. Sumada a la primera se perdería justo el dato que
        # hace falta para saber si alguien está forzando tripwires a propósito.
        #
        # `consumo.anotar` no propaga nunca -- ver su docstring. Va sin `try` aquí porque
        # ponerlo sugeriría que puede lanzar, y lo que hay que poder leer en esta función es
        # que lo único que la corta es un tripwire.
        await consumo.anotar(
            corrida,
            agente="daniela",
            modelo=_modelo_de(en_uso),
            database_url=ctx.database_url,
            id_conversacion=ctx.id_conversacion,
            telefono=ctx.telefono_completo,
        )
        return corrida.final_output

    try:
        resultado.respuesta = await _correr(entrada)

    except InputGuardrailTripwireTriggered as e:
        # Un guardrail de ENTRADA salta antes de que el modelo abra la boca. No hay respuesta
        # anterior que corregir --`CORRECCION` le diría al modelo algo que no ocurrió-- y lo
        # que se acaba de clasificar como ataque es el mensaje del paciente, no una salida
        # mejorable. A una inyección no se le da un segundo intento: mensaje seguro y aviso a
        # los doctores, que es donde terminaba antes igualmente, pero pagando una llamada al
        # modelo para llegar.
        nombre = _nombre_del_tripwire(e)
        resultado.tripwires.append(nombre)

        # La segunda mitad, desde el 21/09/2026: **no todo lo que para `uso_indebido` merece
        # un doctor**. Ese guardrail está afinado para disparar igual con «escríbeme un bucle
        # infinito» que con «ignora tus instrucciones» --la línea es si le PIDE QUE HAGA algo
        # ajeno, ver `.claude/rules/frontera-agentes.md`-- y eso no cambia: ninguna de las dos
        # debe llegar al modelo. Lo que cambia es el después.
        #
        # Con `MENSAJE_SEGURO`, a quien pidió código se le decía «ya le paso tu mensaje al
        # doctor y te escribe apenas pueda». Dos promesas falsas en una frase, y un Telegram
        # al doctor por algo que ni siquiera era un ataque. Es el daño del no negociable 26
        # entrando por otra puerta: el canal de alertas se llena de ruido y la alerta que sí
        # importaba se pierde debajo.
        #
        # Lo que NO se afloja: sigue sin correr el modelo, sigue sin haber segundo intento
        # (no negociable 11) y un `ataque` --o cualquier categoría que no se pueda leer--
        # termina exactamente donde terminaba antes.
        if _categoria_del_tripwire(e) == guardrails.CATEGORIA_TAREA_AJENA:
            resultado.respuesta = _respuesta_de_emergencia(
                MENSAJE_FUERA_DE_ALCANCE,
                # `escala=False` es lo que de verdad apaga el escalamiento; ver su docstring.
                escala=False,
                # Y `fuera_de_alcance=True` es el valor honesto: ese campo «solo cuenta» --lo
                # dice su descripción-- y es exactamente esto lo que mide, alguien
                # preguntando algo ajeno a MaxiCare. Dejarlo en False borraría del contador el
                # único caso que el guardrail sí para.
                fuera_de_alcance=True,
            )
            # Sin `escalado_por` y sin `fallo`, y las dos ausencias son deliberadas: nadie
            # tiene que atender esto, y el turno NO falló --se le contestó lo que había que
            # contestarle--. Marcarlo como fallo lo metería en el informe de «sin resolver»
            # (no negociable 22), que es para lo que Daniela no pudo resolver, no para lo que
            # resolvió diciendo que no. El rastro queda en el `log.warning` de `uso_indebido`,
            # que ya trae la categoría.
            log.info(
                "tripwire de entrada (%s) en %s · fuera de alcance, NO se escala",
                nombre,
                ctx.id_conversacion,
            )
        else:
            resultado.respuesta = _respuesta_de_emergencia(MENSAJE_SEGURO)
            resultado.escalado_por = "dato_faltante"
            resultado.fallo = f"tripwire de entrada: {nombre}"
            log.error(
                "tripwire de entrada (%s) en %s · se escala", nombre, ctx.id_conversacion
            )

    except TRIPWIRES as e:
        nombre = _nombre_del_tripwire(e)
        resultado.tripwires.append(nombre)
        log.warning("tripwire %s en %s · se regenera una vez", nombre, ctx.id_conversacion)

        # `fallos.excepciones_manejadas`: regenerar UNA vez con la corrección explícita.
        # Nunca dos -- el plan es tajante, y con razón: un guardrail que salta dos veces
        # está diciendo que el modelo no va a corregirse solo, y el tercer intento solo
        # gasta dinero y minutos del paciente.
        #
        # `_sin_guardrails_de_entrada` es lo que hace que ese reintento EXISTA de verdad: sin
        # él, la corrección se juzgaba como si la hubiera escrito el paciente y el segundo
        # tripwire estaba garantizado. Ver su docstring.
        try:
            resultado.respuesta = await _correr(
                CORRECCION.format(motivo=_motivo_del_tripwire(e)),
                usando=_sin_guardrails_de_entrada(agente),
            )
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
            # Un `al_escalar` que no devuelva nada --los dobles de las pruebas, y cualquier
            # transporte futuro-- deja esto en `None`, que `atencion` lee como «cuéntalo»:
            # el comportamiento de antes de que este aviso pudiera callarse.
            avisado = await al_escalar(
                ctx, resultado.escalado_por, resultado.respuesta.mensaje_al_paciente
            )
            resultado.doctor_avisado = avisado if isinstance(avisado, bool) else None
        except Exception:  # noqa: BLE001
            # Que no se pueda avisar al doctor no puede impedir que el paciente reciba su
            # mensaje. El escalamiento fallido queda en el log; la respuesta sale igual.
            resultado.doctor_avisado = False
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

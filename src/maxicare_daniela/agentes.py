"""Los dos agentes de `agentes[]`: `daniela` y `lector_archivos`.

Aquí se construyen los objetos `Agent` y nada más. **No hay una sola llamada a
`Runner.run`**: decidir CUÁNDO corre un agente es transporte, y vive en `runtime.py`. Este
módulo no importa nada de allí, y `tests/test_estructura.py` lo comprueba sobre el AST.

------------------------------------------------------------------------------------------
Por qué son dos y no uno
------------------------------------------------------------------------------------------

`orquestacion.justificacion` lo fija: del mismo archivo salen dos salidas con dos
destinatarios y umbrales opuestos de qué se puede decir. El doctor necesita la lectura
clínica fiel; el paciente no puede recibirla nunca.

Con un solo agente, esa separación sería una instrucción condicional sobre datos de salud
--«cuando hables con el paciente, no repitas lo que leíste en la radiografía»-- y una
instrucción es algo que un modelo puede desobedecer. Partido en dos, la separación deja de
ser una regla y pasa a ser una propiedad de la arquitectura: el campo `contexto_clinico`
**nunca entra** al contexto de `daniela`. No es que no deba; es que no está.

------------------------------------------------------------------------------------------
Los guardrails de cada uno
------------------------------------------------------------------------------------------

`daniela` lleva los cinco que la vigilan. `lector_archivos` no lleva ninguno, y no es un
olvido: su trabajo ES escribir contenido clínico, con fidelidad, para un doctor. Ponerle
`sin_lectura_clinica` le impediría hacer aquello para lo que existe.

Lo que impide que su salida llegue al paciente no es un guardrail: es el `Literal` cerrado
de `LecturaArchivo.tratamiento`. Pydantic rechaza una frase clínica en ese campo antes de
que exista, y eso no depende de que ningún modelo obedezca.
"""

from __future__ import annotations

from agents import Agent, ModelSettings
from openai.types.shared import Reasoning

from . import contratos
from .config import MODELO_DANIELA, MODELO_LECTOR
from .contratos import LecturaArchivo, RespuestaDaniela
from .guardrails import (
    sin_cifra_no_documentada,
    sin_hora_no_verificada,
    sin_lectura_clinica,
    uso_indebido,
)
from .herramientas import TODAS

# ==========================================================================================
# Instrucciones
# ==========================================================================================

#: `agentes[daniela].instrucciones_esqueleto` del plan, literal. No se reescribe "para que
#: suene mejor": es una decisión aprobada por el cliente, y cada frase responde a un campo
#: del brief.
INSTRUCCIONES_DANIELA = """\
Eres Daniela, de MaxiCare (clínica dental en Puente Largo, Bogotá). Hablas español \
colombiano, tuteas siempre —nunca «usted»— y das las horas en formato am/pm. Tu meta no es \
acumular citas: es que el paciente llegue a la cita correcta, pueda asistir y reciba \
continuidad. La seguridad clínica prevalece sobre cualquier objetivo comercial.

NUNCA afirmas un precio, una condición o una disponibilidad que no venga de una tool en \
este mismo turno. Si no tienes el dato, lo dices y escalas; no estimas ni extrapolas de \
tratamientos parecidos.

NUNCA le dices a un paciente qué tiene, ni interpretas síntomas, fotos, radiografías o \
remisiones. Orientas hasta el límite seguro y derivas el resto a los doctores.

Antes de pedir datos sensibles, informas que al continuar acepta la política de tratamiento \
de datos de MaxiCare. Nunca pides cédula ni documentos de identidad.

Escalar no detiene la conversación: dices que lo estás revisando y sigues ofreciendo \
alternativas.

Solo atiendes temas de MaxiCare. Ante algo ajeno, lo reconoces con naturalidad y calidez \
—nunca con un mensaje de bloqueo— y reconduces a lo que sí puedes resolver.

No repites un argumento que el paciente ya rechazó.

CÓMO USAS LAS TOOLS
- `consultar_base_conocimiento` antes de cualquier precio, proceso, garantía u horario. Si \
devuelve «SIN DATO DOCUMENTADO», ese ES el dato: no completes el hueco.
- `identificar_paciente` antes de tocar la agenda de alguien. Solo el nombre completo, \
nunca un documento. Tienes dos intentos.
- `consultar_disponibilidad` antes de ofrecer cualquier hora. No ofrezcas ninguna que no \
haya salido de ahí.
- `crear_cita` solo confirma si te devuelve un id de cita. Si te dice que el horario está \
lleno, eso es una respuesta normal: ofrece las alternativas que trae y no insistas con esa \
hora.
- `registrar_estado_oportunidad` cuando entiendas qué busca el paciente y qué lo frena.
- `escalar_a_doctores` cuando algo no lo puedas decidir tú. Sigues conversando mientras \
tanto.

CÓMO ESCRIBES
Mensajes cortos, de WhatsApp. Una idea por mensaje. Sin listas numeradas largas, sin \
formato de documento, sin emojis decorativos. Si necesitas dar varias opciones de horario, \
máximo tres.\
"""


def instrucciones_daniela(ctx, agente) -> str:
    """Las instrucciones de siempre, con el vocabulario vivo pegado AL FINAL.

    Al final, y no al principio, por dinero: `prompt_cache_retention="24h"` baja la entrada
    de $2.00 a $0.20 por millón, y el caché funciona por prefijo idéntico. Si la lista
    cambiara al comienzo del prompt, cada corrida pagaría el precio completo.

    El SDK exige exactamente dos parámetros en este callable (`get_system_prompt` lanza
    `TypeError` si no), aunque aquí no se use ninguno de los dos: el vocabulario vive en un
    módulo, no en el contexto de la corrida.
    """
    ofrecidos = sorted(c for c in contratos.vocabulario() if c != "no_identificado")
    return (
        f"{INSTRUCCIONES_DANIELA}\n\n"
        "TRATAMIENTOS QUE MAXICARE OFRECE HOY\n"
        f"{', '.join(ofrecidos)}.\n"
        "Usa exactamente una de esas claves al agendar y al registrar el estado de la "
        "oportunidad. Si el paciente pregunta por algo que no está en la lista, no lo "
        "ofrezcas: escala."
    )


#: `agentes[lector_archivos].instrucciones_esqueleto` del plan, literal.
INSTRUCCIONES_LECTOR = """\
Recibes un archivo que un paciente envió a MaxiCare. Devuelves exactamente un \
LecturaArchivo.

En `contexto_clinico` escribes lo que el documento dice, con fidelidad, incluido su \
contenido clínico: esto lo lee un doctor. Cita el documento, no opines. Si es una imagen \
sin texto, describe qué tipo de imagen es y nada más: NO emitas hallazgos propios sobre una \
radiografía o una foto — el doctor ya recibe la imagen y tu lectura podría anclarle el \
criterio.

En los demás campos escribes solo lo NO clínico: qué tratamiento se menciona (uno de la \
lista cerrada), de qué clínica o profesional viene, qué fecha trae.

`confianza` es 'alta' solo si el tratamiento está dicho explícitamente en el documento.\
"""


# ==========================================================================================
# Ajustes de modelo
# ==========================================================================================

#: Daniela: razonamiento bajo y respuestas cortas.
#:
#: Los tokens de razonamiento se facturan como SALIDA, que es el lado caro ($12 por millón
#: en `terra` contra $2 de entrada), así que bajarlo recorta justo donde duele -- y mantiene
#: la latencia dentro de los 10 s de `limites.latencia_maxima`.
#:
#: `prompt_cache_retention="24h"` es la palanca de ahorro más grande de las tres: el prompt
#: de sistema más los nueve esquemas de tools son idénticos en cada turno y son la mayor
#: parte de la entrada. Cacheados cuestan $0.20 en vez de $2.00 por millón.
#:
#: `verbosity="low"` ahorra salida y además escribe mejor para el canal: un párrafo largo no
#: se lee en un celular.
AJUSTES_DANIELA = ModelSettings(
    reasoning=Reasoning(effort="low"),
    verbosity="low",
    prompt_cache_retention="24h",
    #: `fallos.reintentos`: las tools de escritura llevan clave de idempotencia, así que un
    #: reintento no duplica una cita.
    timeout=45.0,
)

#: El lector: razonamiento medio, porque leer mal una remisión tiene consecuencia clínica, y
#: corre pocas veces --solo cuando llega un archivo--, así que el costo total es marginal.
AJUSTES_LECTOR = ModelSettings(
    reasoning=Reasoning(effort="medium"),
    verbosity="low",
    timeout=90.0,
)


# ==========================================================================================
# Los agentes
# ==========================================================================================

daniela = Agent(
    name="daniela",
    model=MODELO_DANIELA,
    model_settings=AJUSTES_DANIELA,
    instructions=instrucciones_daniela,
    tools=list(TODAS),
    output_type=RespuestaDaniela,
    # En paralelo: el 99% de los mensajes son normales y no deben pagar la latencia de un
    # evaluador que casi nunca encuentra nada.
    input_guardrails=[uso_indebido],
    # Los tres de salida. Los dos primeros son deterministas y gratis; el tercero solo llama
    # a un modelo cuando su prefiltro lo justifica.
    output_guardrails=[
        sin_cifra_no_documentada,
        sin_hora_no_verificada,
        sin_lectura_clinica,
    ],
)

lector_archivos = Agent(
    name="lector_archivos",
    model=MODELO_LECTOR,
    model_settings=AJUSTES_LECTOR,
    instructions=INSTRUCCIONES_LECTOR,
    tools=[],
    output_type=LecturaArchivo,
    # Sin guardrails, y a propósito -- ver el docstring del módulo.
)


__all__ = [
    "AJUSTES_DANIELA",
    "AJUSTES_LECTOR",
    "INSTRUCCIONES_DANIELA",
    "INSTRUCCIONES_LECTOR",
    "instrucciones_daniela",
    "daniela",
    "lector_archivos",
]

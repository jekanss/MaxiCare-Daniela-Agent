"""Los seis guardrails, sin red y sin agente.

Cada regla se prueba dos veces: en el caso que DEBE disparar y en el que NO debe.

Lo segundo importa tanto como lo primero. Un guardrail que salta sobre un mensaje normal es
un guardrail que alguien acaba desactivando, y entonces deja de proteger también los casos
reales. La medida de un buen freno no es cuánto frena, es que frene exactamente cuando toca.
"""

from __future__ import annotations

import asyncio

import pytest
from agents import RunContextWrapper

from maxicare_daniela import guardrails as g
from maxicare_daniela.calendario import CalendarioDoble
from maxicare_daniela.contratos import ContextoDaniela, RespuestaDaniela


def contexto(**cambios) -> ContextoDaniela:
    base = dict(
        id_conversacion="conv-1",
        telefono_completo="573001112233",
        database_url="postgresql://no-se-usa",
        calendario=CalendarioDoble(),
    )
    base.update(cambios)
    return ContextoDaniela(**base)


# ==========================================================================================
# sin_cifra_no_documentada
# ==========================================================================================


def test_una_cifra_que_ninguna_tool_devolvio_dispara():
    """El fallo que rompe el primer objetivo del brief: un precio inventado."""
    veredicto = g.revisar_cifras("El implante te sale en $1.900.000", autorizadas=set())

    assert veredicto.dispara is True
    assert "1900000" in veredicto.motivo
    assert "consulta" in veredicto.motivo.lower()


def test_la_misma_cifra_no_dispara_si_la_tool_la_devolvio():
    veredicto = g.revisar_cifras("El implante te sale en $1.900.000", autorizadas={"1900000"})

    assert veredicto.dispara is False


@pytest.mark.parametrize(
    "escrito",
    ["$1.900.000", "1.900.000", "$ 1.900.000", "1900000 pesos"],
)
def test_la_misma_cifra_escrita_de_cuatro_formas_se_reconoce_igual(escrito):
    """Sin normalizar, el guardrail saltaría sobre un precio correcto por un punto de más.

    Y un guardrail que salta sobre lo correcto es el que alguien acaba apagando.
    """
    assert g.revisar_cifras(f"cuesta {escrito}", autorizadas={"1900000"}).dispara is False


def test_un_mensaje_normal_con_numeros_no_dispara():
    """Un número de diente o una cantidad de sesiones no son un precio."""
    texto = "Son 2 sesiones y trabajamos sobre el diente 46. ¿Te sirve?"

    assert g.revisar_cifras(texto, autorizadas=set()).dispara is False


def test_el_limite_conocido_queda_documentado_por_una_prueba():
    """Una cifra escrita en letras NO se detecta, y eso está aceptado a conciencia.

    Esta prueba existe para que el límite sea visible: si alguien la ve fallar algún día
    porque el guardrail mejoró, sabrá que puede borrarla. Lo que no puede pasar es que el
    hueco esté y nadie lo sepa.
    """
    texto = "El implante sale en un millón novecientos mil pesos"

    assert g.revisar_cifras(texto, autorizadas=set()).dispara is False


# ==========================================================================================
# sin_hora_no_verificada
# ==========================================================================================


def test_una_hora_que_nadie_verifico_dispara():
    """El fallo más caro para un paciente: presentarse a una hora que nadie le apartó."""
    veredicto = g.revisar_horas("Te espero el martes a las 2 pm", autorizadas=set())

    assert veredicto.dispara is True
    assert "14:00" in veredicto.motivo


def test_la_hora_que_devolvio_disponibilidad_no_dispara():
    veredicto = g.revisar_horas("Te espero a las 2 pm", autorizadas={"14:00"})

    assert veredicto.dispara is False


def test_am_y_pm_se_distinguen():
    """Ofrecer las 9 de la noche cuando la tool dijo las 9 de la mañana es el mismo error."""
    assert g.revisar_horas("a las 9 am", autorizadas={"09:00"}).dispara is False
    assert g.revisar_horas("a las 9 pm", autorizadas={"09:00"}).dispara is True


def test_una_vaguedad_no_es_una_hora_concreta():
    """«Mañana te confirmo» no compromete una hora, así que no tiene nada que verificar."""
    texto = "Mañana te confirmo la disponibilidad, ¿te parece?"

    assert g.revisar_horas(texto, autorizadas=set()).dispara is False


def test_una_cifra_de_precio_no_se_confunde_con_una_hora():
    assert g.revisar_horas("cuesta $1.900.000", autorizadas=set()).dispara is False


# ==========================================================================================
# identidad_antes_de_datos
# ==========================================================================================


def test_sin_identidad_verificada_no_se_toca_la_agenda():
    veredicto = g.revisar_identidad(contexto(identidad_verificada=False))

    assert veredicto.dispara is True
    assert "identificar_paciente" in veredicto.motivo
    assert "NUNCA pidas cédula" in veredicto.motivo


def test_con_identidad_verificada_pasa():
    assert g.revisar_identidad(contexto(identidad_verificada=True)).dispara is False


# ==========================================================================================
# El prefiltro de sin_lectura_clinica
# ==========================================================================================


def test_sin_adjunto_ni_sintomas_no_se_gasta_una_llamada_al_evaluador():
    """Sin prefiltro, este guardrail llamaría a un modelo en cada «¿tienen parqueadero?»."""
    ctx = contexto()

    assert g.vale_la_pena_revisar_lo_clinico(ctx) is False


@pytest.mark.parametrize("campo", ["hubo_adjunto", "menciona_sintomas"])
def test_con_adjunto_o_con_sintomas_si_se_revisa(campo):
    ctx = contexto()
    setattr(ctx.turno, campo, True)

    assert g.vale_la_pena_revisar_lo_clinico(ctx) is True


@pytest.mark.parametrize(
    "mensaje, esperado",
    [
        ("me duele mucho la muela de atrás", True),
        ("tengo la encía inflamada y sangra", True),
        ("¿tienen parqueadero?", False),
        ("quiero cotizar un blanqueamiento", False),
    ],
)
def test_el_prefiltro_de_sintomas_distingue_lo_que_debe(mensaje, esperado):
    assert g.menciona_sintomas(mensaje) is esperado


# ==========================================================================================
# radiografia_sin_recomprimir
# ==========================================================================================


def test_una_foto_de_la_galeria_se_marca_como_recomprimida():
    """Verificado con envíos reales el 2026-09-12: por galería, WhatsApp recomprime."""
    assert g.llego_recomprimida("image") is True


def test_un_documento_pasa_sin_tocar_nada():
    """Un PNG sobrevivió siendo PNG por esta ruta; por la galería habría salido JPEG."""
    assert g.llego_recomprimida("document") is False


def test_lo_que_se_le_pide_al_paciente_explica_como_hacerlo():
    """«Mándala mejor» produce otra foto igual de comprimida y gasta a quien ya colaboró."""
    assert "Documento" in g.PEDIR_COMO_DOCUMENTO
    assert "📎" in g.PEDIR_COMO_DOCUMENTO
    assert "comprime" in g.PEDIR_COMO_DOCUMENTO


# ==========================================================================================
# El turno se vacía
# ==========================================================================================


def test_lo_autorizado_no_sobrevive_al_turno():
    """El caso que esto impide, y que no es hipotético:

    el paciente pregunta por implantes, Daniela consulta y responde bien; tres mensajes
    después pregunta por coronas y repite la cifra de implantes «de memoria». Con el
    conjunto acumulado, el guardrail la dejaría pasar porque la vio antes.
    """
    ctx = contexto()
    ctx.turno.cifras_autorizadas.add("1900000")
    ctx.turno.hubo_adjunto = True

    ctx.turno.reiniciar()

    assert ctx.turno.cifras_autorizadas == set()
    assert ctx.turno.hubo_adjunto is False
    assert g.revisar_cifras("son $1.900.000", ctx.turno.cifras_autorizadas).dispara is True


# ==========================================================================================
# La excepción del evaluador clínico (fase 6B)
# ==========================================================================================
#
# Con `hubo_adjunto = True` el prefiltro deja pasar la revisión SIEMPRE en el turno del
# documento, así que el evaluador opina sobre cada mensaje de ese turno. La excepción de sus
# instrucciones se amplió en 6B: hablar de un tratamiento que viene nombrado en un documento
# que el propio paciente envió tampoco es diagnosticar. Sin eso, «ya me llegó tu remisión
# para ortodoncia» podía autobloquear a Daniela justo en el turno que 6B existe para mejorar.
#
# Un doble no puede juzgar semántica: lo que estas dos pruebas sostienen es el CABLEADO --que
# el mensaje llega íntegro al evaluador y que su veredicto se respeta en los dos sentidos--
# y, la segunda, que al ampliar la excepción nadie borró la regla de disparo.

AVISO_DE_REMISION = "Ya me llegó tu remisión para ortodoncia, el doctor la revisa"
INTERPRETANDO_LA_IMAGEN = "Por la radiografía que mandaste, tienes una caries profunda en el 46"


def _revisar_lo_clinico(monkeypatch, mensaje: str, *, el_evaluador_dispara: bool):
    """Corre `sin_lectura_clinica` sobre un turno con adjunto, con el evaluador doblado.

    Se dobla `_preguntar` --la llamada al modelo-- y no el guardrail: el prefiltro, el
    envoltorio del SDK y el armado del `GuardrailFunctionOutput` son los de producción.
    Mismo patrón que el `sin_evaluadores` de `tests/test_agentes.py`.

    Devuelve `(resultado, visto)`, donde `visto` es lo que le llegó a cada evaluador.
    """
    visto: list[tuple[str, str]] = []

    async def evaluador_doblado(evaluador, texto):
        visto.append((evaluador.name, texto))
        return g.Veredicto(el_evaluador_dispara, "lo dictó la prueba")

    monkeypatch.setattr(g, "_preguntar", evaluador_doblado)

    ctx = contexto()
    ctx.turno.hubo_adjunto = True  # el turno del documento
    salida = RespuestaDaniela(
        mensaje_al_paciente=mensaje,
        estado_oportunidad="explorando",
        barrera_detectada="ninguna",
        requiere_escalamiento=False,
        motivo_escalamiento="ninguno",
        fuera_de_alcance=False,
    )
    resultado = asyncio.run(
        g.sin_lectura_clinica.run(
            context=RunContextWrapper(ctx), agent=None, agent_output=salida
        )
    )
    return resultado, visto


def test_avisar_que_la_remision_llego_no_bloquea_el_turno(monkeypatch):
    """El caso que la excepción nueva protege: decir QUÉ llegó no es decir qué tiene.

    Si esto disparara, Daniela se quedaría muda precisamente en el turno en que el paciente
    acaba de mandar su remisión — y el paciente no sabría ni que llegó.
    """
    resultado, visto = _revisar_lo_clinico(
        monkeypatch, AVISO_DE_REMISION, el_evaluador_dispara=False
    )

    assert resultado.output.tripwire_triggered is False
    assert visto == [("evaluador_lectura_clinica", AVISO_DE_REMISION)], (
        "el mensaje tiene que llegarle al evaluador entero y sin recortar: si el prefiltro "
        "lo hubiera saltado, el guardrail no estaría vigilando este turno"
    )


def test_interpretar_la_radiografia_sigue_disparando(monkeypatch):
    """LA que importa: sin ella, la excepción nueva sería una puerta abierta.

    Dos mitades, y ninguna sobra. La de arriba es el cableado: cuando el evaluador dice que
    sí, el guardrail dispara aunque el mensaje empiece hablando de un documento que el
    propio paciente envió. La de abajo es la instrucción, que es lo que 6B tocó: la
    excepción nueva ampara nombrar un TRATAMIENTO que el documento trae, no cualquier cosa
    que venga en un documento, y la regla que manda disparar al interpretar una imagen
    sigue entera. Un doble no puede juzgar semántica; que nadie borró el disparo al ampliar
    la excepción sí se puede afirmar offline, y es justo el descuido que abriría el hueco.
    """
    resultado, visto = _revisar_lo_clinico(
        monkeypatch, INTERPRETANDO_LA_IMAGEN, el_evaluador_dispara=True
    )

    assert resultado.output.tripwire_triggered is True
    assert visto == [("evaluador_lectura_clinica", INTERPRETANDO_LA_IMAGEN)]

    instrucciones = g._evaluador_clinico.instructions
    assert "interpreta una radiografía o una foto" in instrucciones, (
        "se borró la regla de disparo al ampliar la excepción: el evaluador dejaría pasar "
        "una interpretación de imagen, que es lo más caro que Daniela puede decir"
    )
    assert "nombrado en un documento que el propio paciente envió" in instrucciones
    assert "un tratamiento que el " in instrucciones, (
        "la excepción tiene que seguir acotada a NOMBRAR UN TRATAMIENTO. Redactada como "
        "«lo que venga en un documento no es diagnosticar», ampara también el diagnóstico."
    )


# ==========================================================================================
# La tercera puerta del tracing
#
# `conversacion.py` y `lectura.py` ya pasan un `RunConfig` sin contenido. Los evaluadores de
# guardrail son el tercer consumidor de modelo del proyecto --y el mas facil de olvidar,
# porque nadie piensa en un freno como en algo que habla con OpenAI--. Lo que reciben es
# literalmente el mensaje del paciente (`uso_indebido`) o la respuesta de Daniela antes de
# enviarla (`sin_lectura_clinica`), asi que su traza lleva lo mismo que las otras dos.
# ==========================================================================================


def test_los_evaluadores_no_suben_lo_que_evaluan_a_los_traces(monkeypatch):
    capturado = {}

    class RunnerEspia:
        @staticmethod
        async def run(agente, texto, **kwargs):
            capturado.update(kwargs)
            capturado["texto"] = texto
            raise RuntimeError("corta aqui: lo que importa ya se capturo")

    monkeypatch.setattr(g, "Runner", RunnerEspia)

    # `_preguntar` se traga cualquier fallo del evaluador y devuelve «no dispara»: eso es
    # deliberado --un evaluador caido no puede dejar mudo al sistema-- y aqui viene bien.
    veredicto = asyncio.run(g._preguntar(g._evaluador_uso, "me duele una muela, soy Ana"))

    assert veredicto.dispara is False
    assert capturado["texto"] == "me duele una muela, soy Ana"
    assert capturado.get("run_config") is not None, (
        "el evaluador corre sin RunConfig, asi que el SDK usa sus defaults y sube a los "
        "traces de OpenAI lo que escribio el paciente"
    )
    assert capturado["run_config"].trace_include_sensitive_data is False
    # Los spans siguen: un evaluador que falla mucho tiene que poder verse.
    assert capturado["run_config"].tracing_disabled is False

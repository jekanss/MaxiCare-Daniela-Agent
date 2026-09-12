"""Un modelo guionizado: implementa la interfaz `Model` del SDK sin tocar la red.

Existe para que las pruebas puedan correr `Runner.run` de verdad. La diferencia con probar
las funciones de guardrail directamente no es cosmética: una función que devuelve `True` no
demuestra que el SDK vaya a lanzar el tripwire, que el agente se detenga, ni que el efecto
externo no ocurra. Con esto, el recorrido completo --agente, guardrail, excepción-- se
ejecuta igual que en producción, solo que el modelo dice lo que la prueba le dictó.

No sustituye a `scripts/probar_agentes.py`, que habla con la API real. Lo que este doble
demuestra es el CABLEADO; lo que el script demuestra es el COMPORTAMIENTO. Las dos cosas
hacen falta y ninguna cubre a la otra.
"""

from __future__ import annotations

import json
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage


def _mensaje(texto: str) -> dict[str, Any]:
    """Un turno en el que el modelo simplemente responde."""
    return {
        "id": "msg_doble",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": texto, "annotations": []}],
    }


def _llamada_a_tool(nombre: str, argumentos: dict[str, Any]) -> dict[str, Any]:
    """Un turno en el que el modelo pide ejecutar una tool."""
    return {
        "id": "fc_doble",
        "call_id": f"call_{nombre}",
        "type": "function_call",
        "name": nombre,
        "arguments": json.dumps(argumentos),
        "status": "completed",
    }


class ModeloGuionizado(Model):
    """Devuelve, en orden, los turnos que se le pasaron al construirlo.

    Hereda de `Model` y no se limita a imitar su forma porque `Agent.__post_init__` hace un
    `isinstance(self.model, Model)` explícito: `Model` es una clase abstracta, no un
    Protocol, así que el tipado estructural no basta.

    Cada elemento del guion es una lista de items de salida: usa `responde(...)` y
    `usa_tool(...)` para construirlos sin escribir el JSON a mano.
    """

    def __init__(self, *turnos: list[dict[str, Any]]) -> None:
        self._turnos = list(turnos)
        self.llamadas = 0
        #: Lo que el SDK le pasó en cada llamada. Sirve para comprobar que las tools y las
        #: instrucciones llegaron al modelo, no solo que el Agent las tiene colgadas.
        self.recibido: list[dict[str, Any]] = []

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **extra,
    ) -> ModelResponse:
        self.recibido.append(
            {
                "instrucciones": system_instructions,
                "tools": [getattr(t, "name", None) for t in tools],
                "entrada": input,
            }
        )
        indice = min(self.llamadas, len(self._turnos) - 1)
        self.llamadas += 1
        return ModelResponse(
            output=self._turnos[indice],  # type: ignore[arg-type]
            usage=Usage(),
            response_id=None,
        )

    async def stream_response(self, *args, **kwargs):  # pragma: no cover - no se usa
        raise NotImplementedError("el modelo guionizado no hace streaming")

    def get_retry_advice(self, *args, **kwargs):  # pragma: no cover
        return None

    async def close(self) -> None:  # pragma: no cover
        return None


def responde(objeto: dict[str, Any]) -> list[dict[str, Any]]:
    """Un turno final: el modelo emite el JSON de su `output_type`."""
    return [_mensaje(json.dumps(objeto, ensure_ascii=False))]


def usa_tool(nombre: str, **argumentos: Any) -> list[dict[str, Any]]:
    """Un turno intermedio: el modelo llama una tool."""
    return [_llamada_a_tool(nombre, argumentos)]


def respuesta_daniela(mensaje: str, **cambios: Any) -> dict[str, Any]:
    """Un `RespuestaDaniela` completo con lo mínimo cambiado."""
    base = {
        "mensaje_al_paciente": mensaje,
        "estado_oportunidad": "explorando",
        "barrera_detectada": "ninguna",
        "requiere_escalamiento": False,
        "motivo_escalamiento": "ninguno",
        "fuera_de_alcance": False,
    }
    base.update(cambios)
    return base

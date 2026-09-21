"""Cuánto costó cada corrida del modelo, y avisar cuando el día se pasa de la raya.

Esto existe porque el sistema era **ciego a su propia factura**. Había cuatro consumidores de
modelo --Daniela, el lector de archivos, los evaluadores de guardrail y el analista-- y
ninguno dejaba rastro de lo que gastaba. Para saber cuánto se había ido había que abrir el
dashboard de OpenAI a mano, así que un ataque de coste y un martes con mucha demanda se veían
exactamente igual: no se veían.

Es la primera pieza del perímetro a propósito, antes que cualquier freno. Un límite que no se
puede medir no se puede calibrar, y el número con el que se calibra un freno no puede salir de
una corazonada: con 4 conversaciones al día (21/09/2026) nadie sabe todavía cuál es el uso
normal de esta clínica.

**Nada de lo que hay aquí puede tumbar un turno.** Todas las entradas van envueltas en un
`try` que se lo traga todo, con el mismo criterio que `atencion._anotar_resultado`: si la
contabilidad revienta se pierde una fila, nunca una respuesta a un paciente. Un sistema que
deja de atender porque no pudo apuntar lo que gastó es peor que uno que no apunta.

El dato sale de `RunResult.context_wrapper.usage`, verificado por introspección contra la
0.22.2 instalada (regla dura 2 del proyecto): `requests`, `input_tokens`, `output_tokens` e
`input_tokens_details.cached_tokens`. No se usan `RunHooks` -- harían falta en los cuatro
sitios igual, y además los hooks se disparan por llamada mientras que aquí interesa la corrida
entera.
"""

from __future__ import annotations

import asyncio
import logging

from . import persistencia
from .config import PRECIOS_POR_MILLON

log = logging.getLogger(__name__)


def costo_de(
    modelo: str, *, entrada: int, cacheados: int, salida: int
) -> float:
    """Los dólares que cuesta ese consumo, a los precios de `config.PRECIOS_POR_MILLON`.

    `entrada` es el TOTAL de tokens de entrada y `cacheados` un subconjunto suyo, que es como
    los reporta la Responses API. Así que lo que se cobra a precio lleno es la diferencia:
    sumarlos por separado contaría dos veces el mismo token y daría una factura inflada.

    Un modelo que no esté en la tabla cuesta 0 y **sus tokens se anotan igual**. Perder la
    cifra en dólares es aceptable --se recalcula después con el precio correcto-- pero perder
    el rastro de que hubo consumo no lo es: es justo el caso en que alguien cambió el modelo
    por `.env` y nadie se enteró de que empezó a costar otra cosa.
    """
    precios = PRECIOS_POR_MILLON.get(modelo)
    if precios is None:
        log.warning("no hay precio para el modelo %r; se anota el consumo con costo 0", modelo)
        return 0.0

    por_entrada, por_cacheado, por_salida = precios
    sin_cachear = max(entrada - cacheados, 0)
    return (
        sin_cachear * por_entrada + cacheados * por_cacheado + salida * por_salida
    ) / 1_000_000


def _cifras(corrida) -> tuple[int, int, int, int] | None:
    """`(llamadas, entrada, cacheados, salida)` de una corrida, o `None` si no hay usage.

    Todo con `getattr`: una corrida guionizada de las pruebas no trae `context_wrapper`, y el
    día que el SDK mueva el atributo esto tiene que devolver `None` y callarse, no tumbar el
    turno del paciente por un cambio en la forma de un objeto de instrumentación.
    """
    wrapper = getattr(corrida, "context_wrapper", None)
    uso = getattr(wrapper, "usage", None)
    if uso is None:
        return None

    detalle = getattr(uso, "input_tokens_details", None)
    cacheados = getattr(detalle, "cached_tokens", 0) or 0

    return (
        getattr(uso, "requests", 0) or 0,
        getattr(uso, "input_tokens", 0) or 0,
        int(cacheados),
        getattr(uso, "output_tokens", 0) or 0,
    )


async def anotar(
    corrida,
    *,
    agente: str,
    modelo: str,
    database_url: str,
    id_conversacion: str | None = None,
    telefono: str | None = None,
) -> None:
    """Apunta lo que costó esta corrida. No propaga NUNCA.

    `asyncio.to_thread` porque `persistencia` es síncrona y esto corre dentro del turno: un
    `INSERT` bloqueante aquí le sumaría su latencia a la respuesta del paciente, que es lo que
    el búfer y el descuento del retardo llevan toda la fase 6A protegiendo.
    """
    cifras = _cifras(corrida)
    if cifras is None:
        return

    llamadas, entrada, cacheados, salida = cifras
    if not llamadas and not entrada and not salida:
        # Una corrida que no llamó al modelo --servida entera por el caché de sesión, o
        # cortada antes de empezar-- no es un gasto. Una fila en cero solo ensuciaría el
        # informe y haría que «cuántas corridas hubo» dejara de significar nada.
        return

    try:
        costo = costo_de(modelo, entrada=entrada, cacheados=cacheados, salida=salida)
        await asyncio.to_thread(
            _escribir,
            database_url,
            agente=agente,
            modelo=modelo,
            id_conversacion=id_conversacion,
            telefono=telefono,
            llamadas=llamadas,
            tokens_entrada=entrada,
            tokens_entrada_cacheados=cacheados,
            tokens_salida=salida,
            costo_usd=costo,
        )
    except Exception:  # noqa: BLE001 -- ver el docstring del módulo
        log.exception("no se pudo anotar el consumo de %s; el turno sigue", agente)


def _escribir(database_url: str, **campos) -> None:
    """La parte síncrona. Abre su propia conexión: corre en otro hilo y una conexión de
    `psycopg` no se comparte entre hilos."""
    with persistencia.conectar(database_url) as conn:
        persistencia.anotar_consumo(conn, **campos)


def gasto_y_alerta(database_url: str, *, umbral_usd: float) -> tuple[float, bool] | None:
    """`(gasto_de_hoy, hay_que_avisar)`, o `None` si no se pudo consultar.

    Síncrona a propósito: la llama el vigilante desde un `to_thread`, y ahí las dos consultas
    tienen que ir en la misma conexión para que el `INSERT` de la marca vea el mismo día que
    la suma.

    El orden importa: se mira el gasto ANTES de marcar. Al revés, un día que no llega al
    umbral se habría quemado la marca igual y el aviso de verdad no saldría nunca.
    """
    try:
        with persistencia.conectar(database_url) as conn:
            gasto = persistencia.gasto_del_dia(conn)
            if gasto < umbral_usd:
                return (gasto, False)
            return (gasto, persistencia.toca_avisar_gasto(conn, gasto_usd=gasto))
    except Exception:  # noqa: BLE001
        log.exception("no se pudo revisar el gasto del día")
        return None

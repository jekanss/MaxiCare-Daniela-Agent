"""Cuánto puede pedirle un solo número a Daniela antes de que deje de costar dinero.

El número de WhatsApp de una clínica es público -- tiene que serlo. Eso significa que la firma
HMAC de Meta, que es lo único que protege el webhook, **no distingue a un paciente de alguien
que quiere quemar el saldo de OpenAI**: los dos entran por la puerta principal y los dos vienen
firmados. No hay nada que vulnerar, así que no hay nada que detectar por la vía de «esto es un
ataque». Lo único que se puede hacer es poner un techo.

Antes de esto no había ninguno. Búsqueda sobre todo `src/`: cero coincidencias de `semaphore`,
`rate limit`, `throttle` o `cuota`. Los frenos que existían --el candado por teléfono, el búfer
de 20/45 segundos-- están dimensionados contra el uso normal, no contra un adversario: el
candado serializa a UN número, y N números atacan en paralelo sin ningún tope global.

## Las tres reglas que gobiernan este módulo

1. **Un freno que muerde a un paciente real es peor que el ataque que evita.** Los defaults van
   muy por encima del uso legítimo (ver `config`), y quien se pase recibe una frase y un humano
   avisado, no un portazo.
2. **Si la base no contesta, se DEJA PASAR.** Un freno que tumba turnos cuando Postgres tiene un
   mal minuto convierte una defensa en la caída que pretendía evitar. El fallo abierto está
   elegido, igual que en `guardrails._preguntar`, y por la misma razón.
3. **El aviso sale UNA vez por ventana.** Sin eso, un número que se pasa recibe una frase fija
   por cada mensaje y el doctor un Telegram por cada uno: exactamente la inundación que la
   cuota existe para evitar, servida por la propia defensa. Es el error que el no negociable 26
   ya pagó una vez con los escalamientos, y no se repite.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from . import persistencia
from .config import Config

log = logging.getLogger(__name__)

#: Lo que se le dice a quien se pasó. No menciona cuotas, números ni límites: quien está del
#: otro lado casi siempre es una persona con prisa, no un atacante, y «superaste el límite de
#: mensajes» es la clase de frase que hace que alguien con dolor se vaya a otra clínica.
#:
#: Y dice la verdad: en el mismo turno en que sale esta frase, el doctor recibe el aviso.
FRASE_DE_CUOTA = (
    "Estoy recibiendo muchos mensajes tuyos y prefiero que esto lo vea alguien del equipo. "
    "Ya le avisé al doctor: te escribe en un momento."
)


@dataclass(frozen=True)
class Veredicto:
    """Qué hacer con este mensaje.

    `permitido` y `avisar` son independientes a propósito: en modo observación un mensaje se
    permite Y se avisa, que es justo el estado que sirve para calibrar el umbral con datos
    reales antes de encender el corte.
    """

    permitido: bool
    #: `mensajes` o `archivos`. `None` cuando no se pasó de nada.
    clase: str | None = None
    #: Si a ESTA corrida le toca mandar la frase y el Telegram. Falso cuando ya se avisó
    #: dentro de la ventana.
    avisar: bool = False
    #: Cuántos llevaba. Va en el aviso al doctor: «lleva 47 mensajes en una hora» es
    #: accionable, «se pasó de la cuota» no.
    cuantos: int = 0


def _revisar(database_url: str, telefono: str, *, config: Config) -> Veredicto:
    """La parte síncrona. Una sola conexión para las dos consultas y la marca."""
    with persistencia.conectar(database_url) as conn:
        cuantos = persistencia.mensajes_en_la_ultima_hora(conn, telefono)
        if cuantos <= config.cuota_mensajes_hora:
            return Veredicto(permitido=True)

        # Se consulta la marca SIEMPRE que se pasa, también en modo observación: si no, al
        # apagar el modo observación el primer mensaje que llegara no avisaría --la marca
        # estaría fresca-- o avisaría de más, según el orden. El estado tiene que ser el
        # mismo se corte o no.
        toca = persistencia.toca_avisar_cuota(conn, telefono, "mensajes", ventana_horas=1)

    return Veredicto(
        permitido=config.cuota_modo_observacion,
        clase="mensajes",
        avisar=toca,
        cuantos=cuantos,
    )


async def revisar(telefono: str, *, config: Config) -> Veredicto:
    """Si este número puede seguir gastando modelo. NUNCA lanza.

    Devuelve `permitido=True` ante cualquier fallo -- ver la regla 2 del módulo.
    """
    try:
        return await asyncio.to_thread(_revisar, config.database_url, telefono, config=config)
    except Exception:  # noqa: BLE001 -- regla 2: si la base no contesta, se deja pasar
        log.exception("no se pudo revisar la cuota de %s; se deja pasar", telefono)
        return Veredicto(permitido=True)


def _puede_leer(database_url: str, telefono: str, *, config: Config, tipos) -> bool:
    with persistencia.conectar(database_url) as conn:
        return persistencia.archivos_del_dia(conn, telefono, tipos) < config.cuota_archivos_dia


async def puede_leer_archivos(telefono: str, *, config: Config, tipos) -> bool:
    """Si a este número todavía se le leen los archivos con el modelo.

    **El archivo le llega al doctor pase lo que pase** -- eso no lo apaga nada, es la garantía
    de la fase 2 y el motivo de que esta comprobación viva aquí y no en la entrega. Lo único
    que se apaga es la llamada al modelo caro.

    Es la cuota que más importa de las dos: el lector corre FUERA del búfer y FUERA del candado
    por teléfono, así que N archivos son N llamadas al modelo flagship en paralelo. Es el
    multiplicador de coste más grande que tiene el sistema.
    """
    try:
        return await asyncio.to_thread(
            _puede_leer, config.database_url, telefono, config=config, tipos=tipos
        )
    except Exception:  # noqa: BLE001 -- regla 2
        log.exception("no se pudo revisar la cuota de archivos de %s; se lee igual", telefono)
        return True


def aviso_para_el_doctor(telefono: str, veredicto: Veredicto, *, corto: bool) -> str:
    """El texto del Telegram al General. HTML, como el resto de los avisos.

    `corto` es `cuota_modo_observacion`: en observación el mensaje tiene que decir
    explícitamente que NO se cortó nada, o el doctor leería «se pasó» y daría por hecho que el
    sistema ya lo frenó. Un aviso que se malinterpreta es peor que no avisar.
    """
    cabeza = (
        f"⚠️ El número <code>{telefono}</code> lleva <b>{veredicto.cuantos}</b> mensajes "
        "en una hora."
    )
    if corto:
        return (
            f"{cabeza}\n\nEstá por encima de la cuota, pero el modo observación está "
            "encendido: <b>Daniela le sigue respondiendo con normalidad</b>. Esto es solo "
            "para calibrar el límite."
        )
    return (
        f"{cabeza}\n\nDaniela dejó de responderle y le dijo que alguien del equipo le "
        "escribe. <b>Si es un paciente de verdad, hay que contestarle.</b>"
    )

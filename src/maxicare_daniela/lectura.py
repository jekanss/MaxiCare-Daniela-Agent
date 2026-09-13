"""El muro: qué mitad de un archivo leído va a cada destinatario.

Del mismo archivo salen dos cosas con dos destinatarios y umbrales opuestos de qué se puede
decir:

    contexto_clinico  ─────────►  Telegram, al tema del paciente
    todo lo demás     ─────────►  el contexto de `daniela`

Ese reparto es el muro del diseño. No es un prompt ni un guardrail: es de dónde el código
saca cada cosa. Por eso `repartir` es una función pura y vive sola en la cabecera de este
módulo, donde se puede probar sin levantar nada.

La frontera de este módulo es la misma que la de `ingesta.py`: no importa `runtime.py` ni
ningún framework web.
"""

from __future__ import annotations

import asyncio
import logging

from . import persistencia
from .contratos import LecturaArchivo, LecturaNoClinica

log = logging.getLogger("maxicare.lectura")


def repartir(lectura: LecturaArchivo) -> tuple[str, LecturaNoClinica]:
    """Parte una lectura en (lo que ve el doctor, lo que ve Daniela).

    Por debajo de `confianza == "alta"` el tratamiento se borra. Lo dice el contrato desde
    la fase 1 --«por debajo de 'alta', la capa de ingesta fuerza tratamiento
    ='no_identificado' para que Daniela pregunte en vez de asumir»-- y se hace aquí, en
    código, porque una instrucción puede desobedecerse y una línea de código no.
    """
    seguro = lectura.confianza == "alta"
    no_clinica = LecturaNoClinica(
        tipo_documento=lectura.tipo_documento,
        tratamiento=lectura.tratamiento if seguro else "no_identificado",
        origen=lectura.origen,
        fecha_documento=lectura.fecha_documento,
        confianza=lectura.confianza,
    )
    return lectura.contexto_clinico, no_clinica


# ==========================================================================================
# El tema del paciente
# ==========================================================================================

#: Un candado por teléfono para «buscar o crear el tema».
#:
#: Dos archivos del mismo número nuevo llegando a la vez crearían DOS temas, y el índice
#: único `uq_pacientes_topic` no lo impide: son dos ids distintos, así que el segundo UPDATE
#: pisa al primero y deja un tema huérfano en Telegram al que nadie volverá a escribir.
#:
#: Mismo límite conocido que el candado del turno en `atencion.py`: es de PROCESO. Con más
#: de un worker o más de una réplica deja de proteger y haría falta un `pg_advisory_lock`.
#: El contenedor corre con un solo worker a propósito, y por eso hoy alcanza.
_candados_de_tema: dict[str, asyncio.Lock] = {}


def nombre_del_tema(telefono: str, nombre_perfil: str | None) -> str:
    """«Ana Perez · +573001112233», o solo el teléfono si WhatsApp no mandó perfil.

    Un tema sin nombre es peor que uno feo: el doctor no sabría de quién es el hilo.
    """
    limpio = (nombre_perfil or "").strip()
    return f"{limpio} · +{telefono}" if limpio else f"+{telefono}"


async def asegurar_tema(
    *, telefono: str, nombre_perfil: str | None, database_url: str, telegram
) -> int | None:
    """El tema de ese paciente. Lo crea si no existe. Devuelve `None` si no se pudo.

    Ese `None` no es un error que haya que propagar: significa «manda el archivo al tema
    General, como antes». Degradar es aceptable; perder el archivo no lo es.
    """
    candado = _candados_de_tema.setdefault(telefono, asyncio.Lock())
    async with candado:
        try:
            existente = await asyncio.to_thread(_leer_tema, database_url, telefono)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo consultar el tema de %s; va al General", telefono)
            return None
        if existente:
            return existente

        nombre = nombre_del_tema(telefono, nombre_perfil)
        try:
            tema = await telegram.crear_tema(nombre)
        except Exception:  # noqa: BLE001 -- ancho a propósito: no solo `ErrorDeCanal`
            # (Telegram respondió "ok: false") sino también un fallo de transporte real
            # (timeout, conexión caída). Cualquiera de los dos degrada al General; propagarlo
            # tumbaría la entrega del archivo al doctor, que es la garantía de la fase 2.
            log.exception("Telegram no dejó crear el tema de %s; va al General", telefono)
            return None

        # Cerrarlo es lo que pone el candado, y va antes de guardarlo: si el cierre falla,
        # queremos el id igual --el tema existe-- pero con constancia de que quedó abierto.
        quedo_abierto = False
        try:
            await telegram.cerrar_tema(tema)
        except Exception:  # noqa: BLE001 -- misma razón que arriba: un fallo de transporte
            # al cerrar tampoco puede tumbar la entrega del archivo.
            quedo_abierto = True
            log.error(
                "el tema %s de %s quedó ABIERTO: cualquiera del grupo puede escribirle al "
                "paciente hasta que alguien lo cierre a mano",
                tema,
                telefono,
            )

        try:
            await asyncio.to_thread(
                _guardar_tema, database_url, telefono, nombre_perfil, tema, quedo_abierto
            )
        except Exception:  # noqa: BLE001
            log.exception("el tema %s no quedó guardado; se usa igual en este turno", tema)
        return tema


def _leer_tema(database_url: str, telefono: str) -> int | None:
    with persistencia.conectar(database_url) as conn:
        return persistencia.tema_del_paciente(conn, telefono)


def _guardar_tema(
    database_url: str,
    telefono: str,
    nombre_perfil: str | None,
    tema: int,
    abierto: bool = False,
) -> None:
    with persistencia.conectar(database_url) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo=(nombre_perfil or "").strip() or f"+{telefono}",
            telefono=telefono,
        )
        persistencia.guardar_tema(
            conn, id_paciente=id_paciente, topic_id=tema, abierto=abierto
        )

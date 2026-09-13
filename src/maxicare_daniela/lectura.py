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
import base64
import html
import logging

from agents import Runner

from . import persistencia
from .agentes import lector_archivos
from .canales import ArchivoDescargado
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


# ==========================================================================================
# El lector
# ==========================================================================================

#: Los dos tipos que el lector puede leer de verdad. `audio`, `voice`, `video` y `sticker`
#: siguen con el aviso factual de la fase 2: no se pagan, y no se fingen.
TIPOS_QUE_SE_LEEN = frozenset({"image", "document"})

#: Por encima de esto no se manda al modelo. El doctor recibe el archivo igual --eso no
#: cambia nunca-- y Daniela usa la entrada de siempre.
TOPE_BYTES_LECTOR = 20 * 1024 * 1024


def vale_la_pena_leer(tipo: str, tamano: int) -> bool:
    """Si el archivo es de un tipo legible y no pasa el tope de tamaño."""
    return tipo in TIPOS_QUE_SE_LEEN and tamano <= TOPE_BYTES_LECTOR


def entrada_para_el_lector(archivo: ArchivoDescargado, tipo: str) -> list[dict]:
    """El archivo, con la forma que pide la Responses API.

    Los nombres de campo están verificados por introspección de la 0.22.2 instalada, no de
    memoria: `input_image` lleva `image_url`; `input_file` lleva `filename` y `file_data`.

    `file_data` lleva el data URL completo (`data:<mime>;base64,<...>`), no el base64 a
    secas: confirmado con una llamada real a la API el 12/09/2026 (spec §4.8) -- el base64
    pelado devuelve `400 invalid_value` sobre ese mismo campo.
    """
    datos = base64.b64encode(archivo.contenido).decode("ascii")
    if tipo == "image":
        contenido = {
            "type": "input_image",
            "image_url": f"data:{archivo.mime};base64,{datos}",
            "detail": "auto",
        }
    else:
        contenido = {
            "type": "input_file",
            "filename": archivo.nombre,
            "file_data": f"data:{archivo.mime};base64,{datos}",
        }
    return [{"role": "user", "content": [contenido]}]


async def leer_archivo(
    archivo: ArchivoDescargado, *, tipo: str, correr=None
) -> LecturaArchivo | None:
    """Corre `lector_archivos`. Devuelve `None` si falla, y NUNCA propaga.

    `correr` existe solo para poder probar esto sin red.

    Que devuelva `None` en vez de lanzar no es pereza: quien llama es una tarea de fondo que
    ya entregó el archivo al doctor. Una excepción ahí solo llegaría a un log.
    """
    ejecutar = correr or (lambda entrada: Runner.run(lector_archivos, entrada))
    try:
        corrida = await ejecutar(entrada_para_el_lector(archivo, tipo))
    except Exception:  # noqa: BLE001 -- ver el docstring
        log.exception("el lector no pudo con %s", archivo.nombre)
        return None
    return corrida.final_output


async def leer_y_repartir(
    archivo: ArchivoDescargado, *, tipo: str, telegram, tema_id: int | None, correr=None
) -> LecturaNoClinica | None:
    """Lee, manda lo clínico al tema del paciente y devuelve SOLO la mitad no clínica.

    Aquí es donde el muro se ejerce: `repartir` devuelve dos cosas, una sale por Telegram y
    la otra es el valor de retorno. La clínica no se guarda en ninguna variable que
    sobreviva a esta función.
    """
    leida = await leer_archivo(archivo, tipo=tipo, correr=correr)
    if leida is None:
        try:
            await telegram.enviar_mensaje(
                "⚠️ No se pudo leer este archivo automáticamente.", tema_id=tema_id
            )
        except Exception:  # noqa: BLE001
            log.exception("no se pudo avisar de la lectura fallida")
        return None

    clinico, no_clinica = repartir(leida)
    # Este canal va en HTML (`canales.py` fija `parse_mode: "HTML"` siempre): un `<` o un `&`
    # en `clinico` («canal < 2 mm») lo devuelve Telegram como 400, y ese 400 se traga la
    # lectura entera. No se reutiliza `ingesta._escapar` --misma lógica, `html.escape`-- para
    # no crear un import circular: `ingesta` ya importa `lectura`.
    clinico_seguro = html.escape(clinico, quote=False)
    try:
        await telegram.enviar_mensaje(f"📄 <b>Lectura</b>\n{clinico_seguro}", tema_id=tema_id)
    except Exception:  # noqa: BLE001
        log.exception("la lectura no llegó a Telegram; el archivo sí está")
    return no_clinica

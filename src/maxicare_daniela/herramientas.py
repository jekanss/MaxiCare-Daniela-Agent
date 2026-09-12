"""Las nueve tools de `herramientas[]`: lo que Daniela puede hacer, antes de que exista.

Cada tool es una `@function_tool` (importada como `function_tool` desde `agents` --nunca
`from agents import tool`, que importa un módulo y revienta al decorar) con su
`failure_error_function` propia, verificada contra la 0.22.2 instalada.

Este módulo NO importa `runtime.py` ni ningún framework de transporte, y
`tests/test_estructura.py` lo comprueba sobre el AST. Tampoco importa `calendario.Google` ni
lee variables de entorno: todo lo que viene de fuera --la base, el calendario, las
credenciales de Telegram-- llega dentro de `ContextoDaniela`. Por eso la misma tool corre
contra un doble en una prueba y contra los sistemas reales en producción sin un solo `if`.

------------------------------------------------------------------------------------------
Las dos formas de fallar, y por qué no son la misma
------------------------------------------------------------------------------------------

1. **Fallo previsto** -- el horario está lleno, el paciente no se identificó, no hay dato
   documentado. Es un RESULTADO: la tool devuelve texto explicando qué pasó y qué se puede
   hacer. El modelo lo lee y sigue conversando.

2. **Fallo del sistema** -- Neon no responde, Telegram rechaza. Es una EXCEPCIÓN, y lo que
   el modelo ve lo decide `failure_error_function`. Ese texto no es un mensaje de error:
   es una instrucción sobre qué NO hacer. «No pudiste verificar disponibilidad, NO ofrezcas
   ningún horario» evita que el modelo improvise un horario plausible, que es exactamente
   como un paciente acaba frente a una puerta cerrada.

Confundirlas tiene una consecuencia concreta: si «horario lleno» llegara como excepción, el
modelo reintentaría a ciegas hasta agotar `max_turns`, sin entender nunca que el problema no
era el intento sino el horario.

`crear_cita` es la única con `failure_error_function=None`, y es deliberado: cuando el cupo
ya está tomado y Calendar se cae, la corrida debe MORIR. Si el modelo recibiera un texto
podría decidir confirmarle la cita al paciente de todos modos, y el paciente llegaría a una
clínica donde nadie lo espera.

------------------------------------------------------------------------------------------
Cómo se prueban
------------------------------------------------------------------------------------------

La lógica de cada tool vive en una función normal con prefijo `_`, y la `@function_tool` es
una envoltura de una línea. Un `FunctionTool` ya construido solo se puede invocar con un
JSON serializado; las funciones de abajo se llaman como cualquier otra y se prueban sin
levantar un agente.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from agents import RunContextWrapper, function_tool

from . import contratos, persistencia
from .calendario import ZONA_BOGOTA, ErrorDeCalendario, bloques_del_dia
from .canales import Telegram
from .guardrails import cifras_de, horas_de, identidad_antes_de_datos
from .contratos import (
    Barrera,
    ContextoDaniela,
    EstadoOportunidad,
    SolicitudCancelacion,
    SolicitudCita,
    SolicitudEscalamiento,
    rechazar_documento_de_identidad,
)

log = logging.getLogger("maxicare.herramientas")

#: `ZONA_BOGOTA` se importa de `calendario.py`, donde vive desde que `CalendarioGoogle`
#: también la necesita para traducir lo que devuelve Google. Se sigue reexportando desde
#: aquí --`from .calendario import ZONA_BOGOTA`, arriba-- porque las pruebas y los scripts
#: la venían usando como `herramientas.ZONA_BOGOTA` y no hay motivo para romperlos. Una sola
#: definición: dos copias de un desfase horario son dos cosas que un día divergen.

#: Tope de intentos de identificación por conversación (`herramientas[].valida`). Al
#: tercero no se sigue preguntando: se escala. Insistir convierte una atención en un
#: interrogatorio, y quien no logra identificarse suele ser alguien legítimo con un dato
#: distinto al registrado.
MAX_INTENTOS_IDENTIFICACION = 2


# ==========================================================================================
# Utilidades internas
# ==========================================================================================


def _ahora() -> datetime:
    return datetime.now(ZONA_BOGOTA)


def _a_fecha(valor: str, campo: str) -> datetime:
    """Convierte un texto ISO en un instante con zona horaria.

    Sin zona, Postgres interpretaría el valor en la zona del servidor --que es UTC-- y una
    cita de las 9 de la mañana quedaría guardada a las 4. El modelo escribe horas locales
    de Bogotá porque es lo que habla con el paciente.
    """
    try:
        momento = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{campo} no es una fecha ISO válida: {valor!r}") from e
    return momento if momento.tzinfo else momento.replace(tzinfo=ZONA_BOGOTA)


async def _con_base(ctx: ContextoDaniela, trabajo: Callable[[Any], Any]) -> Any:
    """Corre una función que necesita conexión, fuera del hilo del bucle de eventos.

    `psycopg` es síncrono: llamarlo directamente desde una corrida `async` bloquearía a
    todos los demás pacientes mientras dura la consulta.
    """

    def _ejecutar() -> Any:
        with persistencia.conectar(ctx.database_url) as conn:
            return trabajo(conn)

    return await asyncio.to_thread(_ejecutar)


def _formatear_hora(momento: datetime) -> str:
    dias = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
    local = momento.astimezone(ZONA_BOGOTA)
    return f"{dias[local.weekday()]} {local.day}/{local.month} a las {local:%H:%M}"


def _huecos_libres(
    conn,
    ctx: ContextoDaniela,
    desde: datetime,
    hasta: datetime,
    *,
    maximo: int = 6,
) -> list[datetime]:
    """Los bloques realmente libres: existen, no llegaron al tope, y nadie los bloqueó.

    Los tres filtros son distintos y ninguno sobra. Un bloque puede existir en la rejilla,
    tener cupo en Neon, y aun así estar bloqueado porque un doctor apartó esa hora en su
    calendario para una cirugía. Ofrecerlo sería mandar a alguien a una hora en la que no
    hay nadie.
    """
    rejilla = bloques_del_dia(desde, hasta, duracion_minutos=ctx.duracion_cita_minutos)
    ocupados = persistencia.bloques_ocupados(conn, desde, hasta)
    bloqueos = ctx.calendario.bloqueos(desde, hasta)
    paso = timedelta(minutes=ctx.duracion_cita_minutos)

    libres: list[datetime] = []
    for bloque in rejilla:
        if ocupados.get(bloque, 0) >= ctx.capacidad_por_hora:
            continue
        if any(b.solapa(bloque, bloque + paso) for b in bloqueos):
            continue
        libres.append(bloque)
        if len(libres) >= maximo:
            break
    return libres


def _texto_alternativas(libres: list[datetime]) -> str:
    if not libres:
        return (
            "No quedan bloques libres en esa ventana. Ofrece buscar en otros días o escala "
            "a los doctores si el paciente tiene urgencia."
        )
    return "Bloques libres: " + "; ".join(_formatear_hora(b) for b in libres)


# ==========================================================================================
# Los mensajes de fallo -- lo que el modelo ve cuando un sistema se cae
# ==========================================================================================


def _fallo_conocimiento(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("consultar_base_conocimiento falló: %s", error)
    return (
        "No pudiste consultar la base de conocimiento. NO respondas con información de "
        "memoria y NO estimes ninguna cifra. Dile al paciente que lo confirmas con los "
        "doctores y escala."
    )


def _fallo_disponibilidad(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("consultar_disponibilidad falló: %s", error)
    return (
        "No pudiste verificar disponibilidad. NO ofrezcas ningún horario, ni siquiera uno "
        "que recuerdes de antes. Escala a los doctores y dile al paciente que lo estás "
        "revisando."
    )


def _fallo_identificacion(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("identificar_paciente falló: %s", error)
    return (
        "No pudiste verificar la identidad. NO des ningún dato de citas ni de historia. "
        "Escala a los doctores."
    )


def _fallo_reprogramar(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("reprogramar_cita falló: %s", error)
    return (
        "No se pudo reprogramar. NO le confirmes al paciente ningún horario nuevo: la cita "
        "sigue como estaba. Escala a los doctores."
    )


def _fallo_cancelar(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("cancelar_cita falló: %s", error)
    return (
        "No se pudo completar la cancelación. NO le confirmes al paciente que quedó "
        "cancelada. Escala a los doctores."
    )


def _fallo_estado(ctx: RunContextWrapper[Any], error: Exception) -> str:
    # A diferencia de las demás, esta no cambia lo que el modelo debe decir: el paciente no
    # se entera de que existe esta tabla. Pero se registra, porque de ella salen seis de las
    # siete métricas y un silencio aquí las falsea sin que nadie lo note.
    log.error("registrar_estado_oportunidad falló: %s", error)
    return "No se pudo guardar el estado de la conversación. Continúa la conversación normal."


def _fallo_seguimiento(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("programar_seguimiento falló: %s", error)
    return (
        "No se pudo programar el seguimiento. Escala a los doctores: un recordatorio que no "
        "se programó es una inasistencia que nadie previó."
    )


def _fallo_escalamiento(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("escalar_a_doctores falló: %s", error)
    return (
        "No se pudo avisar a los doctores. Dile al paciente que lo estás revisando y que le "
        "confirmas en breve; no inventes una respuesta al tema que ibas a escalar."
    )


# ==========================================================================================
# 1. consultar_base_conocimiento
# ==========================================================================================


async def _consultar_base_conocimiento(
    ctx: ContextoDaniela, tratamiento: str, pregunta: str
) -> str:
    def trabajo(conn) -> str:
        texto = persistencia.consultar_conocimiento(conn, tratamiento, pregunta)
        if texto.startswith("SIN DATO DOCUMENTADO") and pregunta:
            # El concepto exacto no existía. Antes de declarar que no hay dato, se mira si
            # el tratamiento tiene algo documentado: devolver más información aprobada es
            # seguro; lo que nunca se hace es rellenar el hueco con una estimación.
            texto = persistencia.consultar_conocimiento(conn, tratamiento)
        return texto

    texto = await _con_base(ctx, trabajo)

    # Lo que esta consulta autoriza a decir en este turno. `sin_cifra_no_documentada` exige
    # que toda cifra del mensaje al paciente esté aquí: si Daniela dice un precio que esta
    # tool no devolvió, el tripwire salta. Registrarlo aquí --y no confiar en que el modelo
    # "use lo que le dieron"-- es lo que convierte esa instrucción en una garantía.
    ctx.turno.cifras_autorizadas |= cifras_de(texto)
    return texto


@function_tool(failure_error_function=_fallo_conocimiento)
async def consultar_base_conocimiento(
    wrapper: RunContextWrapper[ContextoDaniela], tratamiento: str, pregunta: str
) -> str:
    """Consulta la información aprobada por MaxiCare sobre un tratamiento.

    Úsala SIEMPRE antes de decir un precio, un tiempo, un proceso o una condición. Si
    devuelve «SIN DATO DOCUMENTADO», ese es el dato: MaxiCare no tiene esa información
    aprobada. No estimes, no extrapoles de un tratamiento parecido, no respondas de memoria.

    Args:
        tratamiento: el tratamiento consultado, por ejemplo 'implantes' o 'cordales'. Usa
            '_general' para sede, horarios, parqueadero y formas de pago.
        pregunta: qué concepto se busca: 'precio', 'proceso', 'garantia', 'duracion'...
    """
    return await _consultar_base_conocimiento(wrapper.context, tratamiento, pregunta)


# ==========================================================================================
# 2. consultar_disponibilidad
# ==========================================================================================


async def _consultar_disponibilidad(ctx: ContextoDaniela, desde: str, hasta: str) -> str:
    inicio = _a_fecha(desde, "desde")
    fin = _a_fecha(hasta, "hasta")
    if fin <= inicio:
        return "La ventana consultada está al revés: 'hasta' tiene que ser posterior a 'desde'."

    def trabajo(conn) -> str:
        libres = _huecos_libres(conn, ctx, inicio, fin)
        return _texto_alternativas(libres)

    texto = await _con_base(ctx, trabajo)
    ctx.turno.horas_autorizadas |= horas_de(texto)
    return texto


@function_tool(failure_error_function=_fallo_disponibilidad)
async def consultar_disponibilidad(
    wrapper: RunContextWrapper[ContextoDaniela], desde: str, hasta: str
) -> str:
    """Devuelve los bloques realmente libres en una ventana de tiempo.

    Cruza tres cosas: los bloques que existen en el horario de atención, los que ya
    alcanzaron el tope de pacientes por hora, y los que los doctores bloquearon a mano en su
    calendario. Nunca ofrezcas un horario que no haya salido de aquí.

    Args:
        desde: inicio de la ventana en ISO, hora de Bogotá. Ej: '2026-09-15T08:00'.
        hasta: fin de la ventana en ISO, hora de Bogotá. Ej: '2026-09-15T18:00'.
    """
    return await _consultar_disponibilidad(wrapper.context, desde, hasta)


# ==========================================================================================
# 3. identificar_paciente
# ==========================================================================================


async def _identificar_paciente(ctx: ContextoDaniela, nombre_completo: str) -> str:
    if ctx.intentos_identificacion >= MAX_INTENTOS_IDENTIFICACION:
        return (
            "Ya se agotaron los dos intentos de identificación de esta conversación. NO "
            "sigas preguntando el nombre y NO pidas ningún documento. Escala a los doctores "
            "para que ellos confirmen quién es."
        )

    # El teléfono viene del contexto, nunca de un argumento: si el modelo pudiera escribirlo,
    # podría escribir uno distinto al de quien está escribiendo.
    rechazar_documento_de_identidad(nombre_completo, "nombre_completo")

    def trabajo(conn) -> tuple[bool, str | None, int | None]:
        registrado = persistencia.buscar_paciente_por_telefono(conn, ctx.telefono_completo)
        if registrado is None:
            persistencia.marcar_intento_identificacion(conn, ctx.id_conversacion, verificada=False)
            return (False, None, None)

        id_paciente, nombre_registrado = registrado
        coincide = _mismo_nombre(nombre_registrado, nombre_completo)
        persistencia.marcar_intento_identificacion(
            conn, ctx.id_conversacion, verificada=coincide
        )
        return (coincide, nombre_registrado, id_paciente)

    coincide, nombre_registrado, id_paciente = await _con_base(ctx, trabajo)
    ctx.intentos_identificacion += 1

    if coincide:
        ctx.identidad_verificada = True
        ctx.id_paciente = id_paciente
        ctx.nombre_paciente = nombre_registrado
        return f"Identidad verificada: {nombre_registrado}. Ya puedes consultar sus citas."

    if nombre_registrado is None:
        return (
            "Ese número no está registrado como paciente. NO es un error: puede ser alguien "
            "escribiendo por primera vez. Puedes agendarle una cita nueva, pero no tienes "
            "historia que consultar."
        )

    restantes = MAX_INTENTOS_IDENTIFICACION - ctx.intentos_identificacion
    if restantes <= 0:
        return (
            "El nombre no coincide con el registrado para ese número y se agotaron los "
            "intentos. NO pidas documento de identidad. Escala a los doctores."
        )
    return (
        "El nombre no coincide con el registrado para ese número. Puedes pedirlo una vez "
        "más, tal como aparece en la historia clínica. NO pidas cédula ni documento."
    )


def _mismo_nombre(registrado: str, ofrecido: str) -> bool:
    """Compara nombres con la tolerancia justa.

    Exigir igualdad exacta rechazaría a «Juan Perez» cuando en la historia dice «Juan Pérez»,
    y ese rechazo gasta uno de los dos intentos de alguien que sí es quien dice ser. Se
    normalizan tildes, mayúsculas y espacios; el orden y el contenido de las palabras no.
    """
    import unicodedata

    def normalizar(texto: str) -> set[str]:
        plano = unicodedata.normalize("NFKD", texto.lower())
        plano = "".join(c for c in plano if not unicodedata.combining(c))
        return {p for p in plano.replace(".", " ").split() if p}

    a, b = normalizar(registrado), normalizar(ofrecido)
    if not a or not b:
        return False
    # Basta con que uno contenga al otro: «Juan Pérez» frente a «Juan Carlos Pérez Gómez»
    # es la misma persona diciendo su nombre completo.
    return a <= b or b <= a


@function_tool(failure_error_function=_fallo_identificacion)
async def identificar_paciente(
    wrapper: RunContextWrapper[ContextoDaniela], nombre_completo: str
) -> str:
    """Verifica quién está escribiendo, contra el registro de la clínica.

    NUNCA pidas cédula, número de documento ni nada parecido: MaxiCare lo prohíbe
    expresamente. Solo el nombre completo. Tienes dos intentos por conversación; después se
    escala a los doctores.

    Args:
        nombre_completo: el nombre que dio el paciente, tal como lo escribió.
    """
    return await _identificar_paciente(wrapper.context, nombre_completo)


# ==========================================================================================
# 4. crear_cita
# ==========================================================================================


class CitaNoConfirmada(RuntimeError):
    """El cupo se tomó y la cita no llegó a existir. La corrida tiene que morir aquí.

    No hereda de nada que el modelo pueda interpretar como resultado: con
    `failure_error_function=None`, el SDK propaga la excepción y el orquestador escala. La
    alternativa --devolverle un texto al modelo-- le dejaría la puerta abierta a confirmar
    igual, y el paciente llegaría a una cita que no existe en el calendario de nadie.
    """


async def _crear_cita(ctx: ContextoDaniela, solicitud: SolicitudCita) -> str:
    inicio = (
        solicitud.inicio
        if solicitud.inicio.tzinfo
        else solicitud.inicio.replace(tzinfo=ZONA_BOGOTA)
    )

    def tomar(conn) -> tuple[tuple[int, int] | None, list[datetime]]:
        cupo = persistencia.tomar_cupo(
            conn,
            inicio=inicio,
            capacidad=ctx.capacidad_por_hora,
            clave_idempotencia=solicitud.clave_idempotencia,
            conversacion_id=ctx.id_conversacion,
        )
        if cupo is not None:
            return (cupo, [])
        # Solo se buscan alternativas si hizo falta: una consulta de más en el camino feliz
        # es latencia que paga cada paciente.
        ventana_fin = inicio + timedelta(hours=8)
        return (None, _huecos_libres(conn, ctx, inicio, ventana_fin))

    cupo, alternativas = await _con_base(ctx, tomar)

    if cupo is None:
        # RESULTADO, no error. El modelo tiene que poder seguir conversando con esto.
        texto = (
            f"Ese horario ({_formatear_hora(inicio)}) ya está lleno: la clínica atiende "
            f"{ctx.capacidad_por_hora} pacientes por hora. NO insistas con esa hora. "
            + _texto_alternativas(alternativas)
        )
        # Las alternativas quedan autorizadas; la hora llena NO, para que el guardrail de
        # horas salte si el modelo insiste con ella pese a lo que acaba de leer.
        ctx.turno.horas_autorizadas |= horas_de(_texto_alternativas(alternativas))
        return texto

    reserva_id, _cupo_num = cupo

    # El cupo ya está apartado. A partir de aquí, cualquier salida que no sea una cita
    # completa tiene que devolverlo.
    try:
        evento_id = await asyncio.to_thread(
            ctx.calendario.crear_evento,
            inicio=inicio,
            duracion_minutos=ctx.duracion_cita_minutos,
            titulo=f"{solicitud.nombre_completo} · {solicitud.tratamiento}",
            descripcion=f"Agendado por Daniela. Conversación {ctx.id_conversacion}.",
        )
    except ErrorDeCalendario as e:
        await asyncio.to_thread(_liberar, ctx, reserva_id)
        log.error("Calendar falló tras tomar el cupo; reserva %s liberada", reserva_id)
        raise CitaNoConfirmada(
            "El cupo se tomó en la base pero el calendario no respondió. La reserva se "
            "liberó y la cita NO existe. No se le puede confirmar nada al paciente."
        ) from e

    def guardar(conn) -> str:
        return persistencia.registrar_cita(
            conn,
            reserva_id=reserva_id,
            conversacion_id=ctx.id_conversacion,
            paciente_id=ctx.id_paciente,
            nombre_completo=solicitud.nombre_completo,
            telefono=ctx.telefono_completo,
            tratamiento=solicitud.tratamiento,
            inicio=inicio,
            duracion_minutos=ctx.duracion_cita_minutos,
            evento_calendar_id=evento_id,
        )

    id_cita = await _con_base(ctx, guardar)
    texto = (
        f"Cita confirmada para {solicitud.nombre_completo}, {_formatear_hora(inicio)}, "
        f"{solicitud.tratamiento}. Id de la cita: {id_cita}."
    )
    ctx.turno.horas_autorizadas |= horas_de(texto)
    return texto


def _liberar(ctx: ContextoDaniela, reserva_id: int) -> None:
    with persistencia.conectar(ctx.database_url) as conn:
        persistencia.liberar_cupo(conn, reserva_id)


@function_tool(
    failure_error_function=None, tool_input_guardrails=[identidad_antes_de_datos]
)
async def crear_cita(
    wrapper: RunContextWrapper[ContextoDaniela], solicitud: SolicitudCita
) -> str:
    """Reserva un bloque para un paciente: toma el cupo y crea el evento en el calendario.

    Si el horario está lleno te lo dice como resultado, con alternativas: no es un error y
    no debes reintentar la misma hora. Solo confirma la cita al paciente si esta tool
    devuelve un id de cita.

    Args:
        solicitud: nombre completo, inicio, tratamiento y clave de idempotencia.
    """
    return await _crear_cita(wrapper.context, solicitud)


# ==========================================================================================
# 5. reprogramar_cita
# ==========================================================================================


async def _reprogramar_cita(ctx: ContextoDaniela, id_cita: str, nuevo_inicio: str) -> str:
    destino = _a_fecha(nuevo_inicio, "nuevo_inicio")

    # La clave se ancla al VALOR DESTINO y no a un delta. Con «mover dos horas», un reintento
    # movería la cita otras dos horas: el mismo intento produciría un resultado distinto cada
    # vez que la red falle. Con el destino, repetirlo es inofensivo.
    clave = ctx.clave("reprogramar", id_cita, destino.isoformat())

    def trabajo(conn) -> tuple[str, dict[str, Any] | None, tuple[int, int] | None, list[datetime]]:
        cita = persistencia.leer_cita(conn, id_cita)
        if cita is None:
            return ("no_existe", None, None, [])
        if ctx.id_paciente is not None and cita["paciente_id"] not in (None, ctx.id_paciente):
            return ("ajena", None, None, [])
        if cita["estado"] == "cancelada":
            return ("cancelada", cita, None, [])

        cupo = persistencia.tomar_cupo(
            conn,
            inicio=destino,
            capacidad=ctx.capacidad_por_hora,
            clave_idempotencia=clave,
            conversacion_id=ctx.id_conversacion,
        )
        if cupo is None:
            return ("lleno", cita, None, _huecos_libres(conn, ctx, destino, destino + timedelta(hours=8)))
        return ("ok", cita, cupo, [])

    estado, cita, cupo, alternativas = await _con_base(ctx, trabajo)

    if estado == "no_existe":
        return f"No existe ninguna cita con id {id_cita}. Verifica el id antes de prometer nada."
    if estado == "ajena":
        return (
            "Esa cita no pertenece al paciente identificado en esta conversación. NO la "
            "muevas y no des ningún dato de ella. Escala a los doctores."
        )
    if estado == "cancelada":
        return "Esa cita ya está cancelada. Si el paciente quiere volver, agenda una nueva."
    if estado == "lleno":
        ctx.turno.horas_autorizadas |= horas_de(_texto_alternativas(alternativas))
        return (
            f"El horario destino ({_formatear_hora(destino)}) está lleno. La cita sigue "
            "donde estaba, sin cambios. " + _texto_alternativas(alternativas)
        )

    assert cita is not None and cupo is not None
    reserva_nueva, _ = cupo
    reserva_vieja = cita["reserva_id"]

    try:
        if cita["evento_calendar_id"]:
            await asyncio.to_thread(
                ctx.calendario.mover_evento, cita["evento_calendar_id"], inicio=destino
            )
    except ErrorDeCalendario:
        # El cupo nuevo ya estaba tomado: devolverlo es lo que impide que un fallo de
        # calendario deje bloqueado un horario que nadie va a usar.
        await asyncio.to_thread(_liberar, ctx, reserva_nueva)
        log.error("Calendar falló reprogramando %s; cupo nuevo liberado", id_cita)
        raise

    def aplicar(conn) -> None:
        persistencia.mover_cita(conn, id_cita, reserva_id=reserva_nueva, inicio=destino)
        if reserva_vieja:
            persistencia.liberar_cupo(conn, reserva_vieja)

    await _con_base(ctx, aplicar)
    texto = f"Cita {id_cita} reprogramada para {_formatear_hora(destino)}."
    ctx.turno.horas_autorizadas |= horas_de(texto)
    return texto


@function_tool(
    failure_error_function=_fallo_reprogramar,
    tool_input_guardrails=[identidad_antes_de_datos],
)
async def reprogramar_cita(
    wrapper: RunContextWrapper[ContextoDaniela], id_cita: str, nuevo_inicio: str
) -> str:
    """Mueve una cita a un horario nuevo, liberando el viejo.

    Si el horario destino está lleno, la cita se queda donde estaba y te devuelve
    alternativas. Nunca le digas al paciente que quedó movida sin que esta tool lo confirme.

    Args:
        id_cita: el id que devolvió `crear_cita`.
        nuevo_inicio: el horario destino en ISO, hora de Bogotá.
    """
    return await _reprogramar_cita(wrapper.context, id_cita, nuevo_inicio)


# ==========================================================================================
# 6. cancelar_cita
# ==========================================================================================


async def _cancelar_cita(ctx: ContextoDaniela, solicitud: SolicitudCancelacion) -> str:
    def leer(conn) -> dict[str, Any] | None:
        return persistencia.leer_cita(conn, solicitud.id_cita)

    cita = await _con_base(ctx, leer)

    if cita is None:
        return f"No existe ninguna cita con id {solicitud.id_cita}."
    if ctx.id_paciente is not None and cita["paciente_id"] not in (None, ctx.id_paciente):
        return (
            "Esa cita no pertenece al paciente identificado en esta conversación. NO la "
            "canceles. Escala a los doctores."
        )
    if cita["estado"] == "cancelada":
        return "Esa cita ya estaba cancelada. No hace falta hacer nada más."

    # El orden es al revés que en `crear_cita`, y a propósito. Al crear, lo peligroso es
    # prometer una cita que no existe. Al cancelar, lo peligroso es lo contrario: dejar el
    # cupo bloqueado. Así que el cupo se libera SIEMPRE, aunque Calendar no responda, y lo
    # que queda pendiente --un evento huérfano en el calendario-- lo limpia un humano.
    calendario_ok = True
    if cita["evento_calendar_id"]:
        try:
            await asyncio.to_thread(
                ctx.calendario.eliminar_evento, cita["evento_calendar_id"]
            )
        except ErrorDeCalendario:
            calendario_ok = False
            log.error("Calendar no respondió cancelando %s; se libera el cupo igual", solicitud.id_cita)

    def aplicar(conn) -> None:
        persistencia.marcar_cita_cancelada(conn, solicitud.id_cita, motivo=solicitud.motivo)
        if cita["reserva_id"]:
            persistencia.liberar_cupo(conn, cita["reserva_id"])

    await _con_base(ctx, aplicar)

    if calendario_ok:
        return f"Cita {solicitud.id_cita} cancelada y el horario quedó libre."
    return (
        f"Cita {solicitud.id_cita} cancelada y el horario quedó libre, pero el evento sigue "
        "en el calendario de los doctores. Escala para que alguien lo borre a mano."
    )


@function_tool(
    failure_error_function=_fallo_cancelar,
    tool_input_guardrails=[identidad_antes_de_datos],
)
async def cancelar_cita(
    wrapper: RunContextWrapper[ContextoDaniela], solicitud: SolicitudCancelacion
) -> str:
    """Cancela una cita y libera su horario.

    Args:
        solicitud: id de la cita, motivo opcional y clave de idempotencia.
    """
    return await _cancelar_cita(wrapper.context, solicitud)


# ==========================================================================================
# 7. registrar_estado_oportunidad
# ==========================================================================================


async def _registrar_estado_oportunidad(
    ctx: ContextoDaniela,
    estado: str,
    barrera: str,
    tratamiento: str | None,
    fuera_de_alcance: bool,
    nota: str | None,
) -> str:
    # Se valida contra la lista viva, ANTES de tocar la base: un tratamiento que la clínica
    # ya no ofrece no debe llegar a escribirse, y la prueba del rechazo no necesita Neon.
    clave = (tratamiento or "").strip().lower()
    if clave not in contratos.vocabulario():
        raise ValueError(
            f"'{tratamiento}' no es un tratamiento que MaxiCare ofrezca. "
            f"Los actuales son: {', '.join(sorted(contratos.vocabulario()))}."
        )

    def trabajo(conn) -> None:
        persistencia.upsert_estado_oportunidad(
            conn,
            ctx.id_conversacion,
            estado=estado,
            barrera=barrera,
            tratamiento=clave,
            fuera_de_alcance=fuera_de_alcance,
            notas=nota,
        )

    await _con_base(ctx, trabajo)
    return "Estado de la conversación guardado."


@function_tool(failure_error_function=_fallo_estado)
async def registrar_estado_oportunidad(
    wrapper: RunContextWrapper[ContextoDaniela],
    estado: EstadoOportunidad,
    barrera: Barrera,
    tratamiento: str,
    fuera_de_alcance: bool,
    nota: str,
) -> str:
    """Guarda dónde está la conversación: qué necesita el paciente y qué lo está frenando.

    La nota es corta y NO clínica: «mandó una radiografía», nunca qué se ve en ella.

    Args:
        estado: en qué punto está el paciente.
        barrera: qué lo está frenando, o 'ninguna'.
        tratamiento: de qué trata la conversación, o 'no_identificado'. Uno de los
            tratamientos que MaxiCare ofrece; la lista va al final de tus instrucciones.
        fuera_de_alcance: si pidió algo que MaxiCare no ofrece.
        nota: una línea de contexto operativo, sin contenido clínico.
    """
    return await _registrar_estado_oportunidad(
        wrapper.context, estado, barrera, tratamiento, fuera_de_alcance, nota or None
    )


# ==========================================================================================
# 8. programar_seguimiento
# ==========================================================================================


async def _programar_seguimiento(
    ctx: ContextoDaniela, tipo: str, fecha_objetivo: str
) -> str:
    objetivo = _a_fecha(fecha_objetivo, "fecha_objetivo")
    if objetivo <= _ahora():
        return "Esa fecha ya pasó. Programa el seguimiento para un momento futuro."

    clave = ctx.clave("seguimiento", tipo, objetivo.isoformat())

    def trabajo(conn) -> tuple[str | None, bool]:
        # Mientras un doctor tiene el relevo, el sistema no programa nada por su cuenta: el
        # doctor podría estar acordando otra fecha en ese mismo momento, y el paciente
        # recibiría un recordatorio que contradice lo que acaba de hablar con él.
        doctor = persistencia.conversacion_tomada(conn, ctx.id_conversacion)
        if doctor:
            return (doctor, False)
        nuevo = persistencia.insertar_seguimiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            tipo=tipo,
            fecha_objetivo=objetivo,
            clave_idempotencia=clave,
        )
        return (None, nuevo)

    doctor, nuevo = await _con_base(ctx, trabajo)

    if doctor:
        return (
            f"No se programó nada: esta conversación la tiene {doctor} en este momento. "
            "Los seguimientos los decide quien está hablando."
        )
    if not nuevo:
        return f"Ese seguimiento ya estaba programado para {_formatear_hora(objetivo)}."
    return f"Seguimiento '{tipo}' programado para {_formatear_hora(objetivo)}."


@function_tool(failure_error_function=_fallo_seguimiento)
async def programar_seguimiento(
    wrapper: RunContextWrapper[ContextoDaniela], tipo: str, fecha_objetivo: str
) -> str:
    """Deja programado un recordatorio o una reactivación para más adelante.

    Args:
        tipo: qué clase de seguimiento, por ejemplo 'recordatorio_cita' o 'reactivacion'.
        fecha_objetivo: cuándo debe salir, en ISO y hora de Bogotá.
    """
    return await _programar_seguimiento(wrapper.context, tipo, fecha_objetivo)


# ==========================================================================================
# 9. escalar_a_doctores
# ==========================================================================================


def _teclado_relevo(id_conversacion: str) -> dict:
    """El botón «Hablar yo con el paciente» que abre el relevo (fase 6)."""
    return {
        "inline_keyboard": [
            [{"text": "Hablar yo con el paciente", "callback_data": f"relevo:{id_conversacion}"}]
        ]
    }


def _aviso_para_doctores(ctx: ContextoDaniela, solicitud: SolicitudEscalamiento) -> str:
    nombre = ctx.nombre_paciente or "paciente sin identificar"
    return (
        f"<b>Escalamiento · {solicitud.motivo}</b>\n"
        f"{nombre} · +{ctx.telefono_completo}\n\n"
        f"{solicitud.resumen_para_doctor}\n\n"
        f"<b>Pregunta:</b> {solicitud.pregunta_concreta}"
    )


async def _escalar_a_doctores(
    ctx: ContextoDaniela, solicitud: SolicitudEscalamiento, *, telegram: Any | None = None
) -> str:
    def registrar(conn) -> int | None:
        return persistencia.insertar_escalamiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            motivo=solicitud.motivo,
            resumen=solicitud.resumen_para_doctor,
            pregunta=solicitud.pregunta_concreta,
            clave_idempotencia=solicitud.clave_idempotencia,
        )

    escalamiento_id = await _con_base(ctx, registrar)

    if escalamiento_id is None:
        # Ya se escaló este turno. No es un fallo: es lo que impide que un reintento de red
        # le mande al doctor la misma alerta tres veces.
        return (
            "Este turno ya estaba escalado; no se volvió a avisar. Dile al paciente que lo "
            "estás revisando con los doctores."
        )

    canal = telegram or Telegram(ctx.telegram_bot_token, ctx.telegram_chat_doctores)

    # SIEMPRE al tema General, nunca al tema del paciente. General es donde los doctores
    # pueden discutir un caso sin que el paciente vea una palabra; el tema del paciente es un
    # canal en vivo hacia su WhatsApp durante el relevo.
    message_id = await canal.enviar_mensaje(
        _aviso_para_doctores(ctx, solicitud),
        tema_id=ctx.tema_general,
        teclado=_teclado_relevo(ctx.id_conversacion),
    )

    def anotar(conn) -> None:
        persistencia.anotar_telegram_en_escalamiento(conn, escalamiento_id, message_id)

    await _con_base(ctx, anotar)
    return (
        "Escalado a los doctores. Sigue conversando con el paciente con normalidad y dile "
        "que estás confirmando ese punto; no dejes la conversación en silencio."
    )


@function_tool(failure_error_function=_fallo_escalamiento)
async def escalar_a_doctores(
    wrapper: RunContextWrapper[ContextoDaniela], solicitud: SolicitudEscalamiento
) -> str:
    """Le pasa a los doctores algo que no puedes decidir tú, sin cortar la conversación.

    Escalar no es abandonar al paciente: sigues hablando con él mientras tanto.

    Args:
        solicitud: motivo, resumen para el doctor, pregunta concreta y clave de idempotencia.
    """
    return await _escalar_a_doctores(wrapper.context, solicitud)


# ==========================================================================================
# El conjunto -- lo que `agentes.py` importará en la fase 4
# ==========================================================================================

#: Las nueve, en el orden de `herramientas[]` del plan.
TODAS = (
    consultar_base_conocimiento,
    consultar_disponibilidad,
    identificar_paciente,
    crear_cita,
    reprogramar_cita,
    cancelar_cita,
    registrar_estado_oportunidad,
    programar_seguimiento,
    escalar_a_doctores,
)

__all__ = [
    "TODAS",
    "CitaNoConfirmada",
    "MAX_INTENTOS_IDENTIFICACION",
    "ZONA_BOGOTA",
    "cancelar_cita",
    "consultar_base_conocimiento",
    "consultar_disponibilidad",
    "crear_cita",
    "escalar_a_doctores",
    "identificar_paciente",
    "programar_seguimiento",
    "registrar_estado_oportunidad",
    "reprogramar_cita",
]

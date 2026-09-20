"""Lo que Daniela puede hacer: las nueve tools de `herramientas[]` y una décima.

La décima, `consultar_citas`, no está en el plan y va al final a propósito: el plan da por
supuesto que el id de una cita viaja en la conversación, y las conversaciones de WhatsApp
mueren a las 24 horas.

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
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Callable, Literal

from agents import RunContextWrapper, function_tool

from . import contratos, persistencia, seguimientos
from .calendario import (
    ZONA_BOGOTA,
    Bloqueo,
    ErrorDeCalendario,
    EventoDelCalendario,
    Jornada,
    bloques_del_dia,
)
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
from .sin_resolver import Senal

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

#: Cuánto se mira hacia adelante para ofrecer alternativas cuando la hora pedida no sirve.
#: Ocho horas es una jornada: alternativas del MISMO día, que es lo que un paciente que ya
#: eligió un día quiere oír. `crear_cita` y `consultar_disponibilidad` usan la misma para no
#: ofrecer dos repertorios distintos según por dónde se entre.
VENTANA_ALTERNATIVAS = timedelta(hours=8)

#: Hasta dónde se busca la hora libre más cercana cuando la pedida cae con la clínica
#: cerrada. Tiene que cruzar el día --y el fin de semana-- o quien escribe un domingo por la
#: noche no recibe nada: tres días cubren el peor caso, que es sábado tarde a lunes mañana.
VENTANA_PROXIMO_HUECO = timedelta(days=3)


# ==========================================================================================
# Utilidades internas
# ==========================================================================================


# Aquí vivía `_ahora()`, que leía el reloj de la máquina. No queda ninguna: el instante lo
# pone `ctx.ahora`, una sola vez por turno. Dos relojes en un módulo son dos relojes que un
# día discrepan, y el que no viaja en el contexto no se puede fijar desde una prueba.


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


async def _bloqueo_que_tapa(ctx: ContextoDaniela, inicio: datetime) -> Bloqueo | None:
    """El bloqueo del doctor que cubre ese bloque, si lo hay. Google manda.

    `consultar_disponibilidad` ya respetaba los bloqueos desde la fase 3, pero las dos tools
    que ESCRIBEN no los miraban: comprobaban la hora pasada y el cupo de Neon, y se iban
    derechas a `crear_evento`. Dos caminos llegaban ahí con una hora que el doctor había
    apartado:

      1. **El modelo agenda sin consultar antes.** El paciente dice «las 3» y Daniela lo
         toma. Nada se lo impedía: `sin_hora_no_verificada` da por buena toda hora que una
         tool confirma, y la confirmación de `crear_cita` se autoriza a sí misma.
      2. **El doctor bloquea DESPUÉS de que Daniela ofreció esa hora.** Entre el «tengo las
         3 libres» y el «sí, esa» del paciente pasan minutos: es una conversación de
         WhatsApp, no una transacción.

    Va ANTES de tocar la base, junto a la comprobación de hora pasada y por el mismo motivo:
    un cupo consumido por una cita que nunca se pudo crear hay que ir a devolverlo.

    Límite conocido: si el doctor bloquea entre dos intentos de la MISMA `crear_cita` (un
    reintento del modelo, segundos), el segundo intento responde «bloqueado» en vez de
    devolver la confirmación que ya existía. La cita, que sí se creó, sigue en pie. Se
    prefiere ese mensaje raro en un caso de segundos a consumir un cupo en todos los demás.
    """
    paso = timedelta(minutes=ctx.duracion_cita_minutos)
    fin = inicio + paso
    bloqueos = await asyncio.to_thread(ctx.calendario.bloqueos, inicio, fin)
    for bloqueo in bloqueos:
        if bloqueo.solapa(inicio, fin):
            return bloqueo
    return None


def _texto_fuera_de_horario(
    ctx: ContextoDaniela, inicio: datetime, cercanos: list[datetime]
) -> str:
    """Lo que el modelo lee cuando pide una hora con la clínica cerrada. RESULTADO, no error.

    Trae las dos cosas que MaxiCare pidió el 13/09/2026: el horario, para que el paciente
    sepa por qué esa hora no puede ser, y horas libres CONCRETAS, para que la conversación
    siga. Antes acababa en «consulta la disponibilidad», y quien escribía a las siete de la
    mañana se quedaba sin ninguna hora que mirar.

    El horario sale de `ctx.jornada` y no de una constante: si la clínica lo cambia, este
    texto cambia con él.

    **Quien llame a esto tiene que autorizar sus horas** (`horas_autorizadas |=
    horas_de(texto)`). No es opcional: «de 8:00 a 17:00» son dos horas concretas, y
    `sin_hora_no_verificada` bloquea el mensaje entero si ninguna tool las devolvió en este
    turno. Sin ese registro, recordarle el horario al paciente le cuesta un «te escribe el
    doctor», que es justo lo contrario de lo que se pidió.
    """
    j = ctx.jornada
    horario = (
        f"Atiende de lunes a viernes de {j.apertura}:00 a {j.cierre}:00 y los sábados de "
        f"{j.apertura}:00 a {j.cierre_sabado}:00"
        + ("." if j.atiende_domingo else ", y los domingos no abre.")
    )
    if cercanos:
        oferta = (
            " Estas sí están libres, y son las más cercanas a lo que pidió: "
            + "; ".join(_formatear_hora(b) for b in cercanos)
            + ". Recuérdale el horario y ofrécele una de estas."
        )
    else:
        oferta = (
            " No encontré ninguna hora libre en los próximos días. Recuérdale el horario y "
            "ofrécele mirar otra fecha."
        )
    return (
        f"Esa hora ({_formatear_hora(inicio)}) está fuera del horario de atención: la "
        f"clínica no atiende en ese momento. {horario} NO agendes ahí.{oferta}"
    )


async def _proximos_huecos(ctx: ContextoDaniela, desde: datetime) -> list[datetime]:
    """Las primeras horas libres a partir de `desde`, cruzando los días que haga falta.

    `VENTANA_ALTERNATIVAS` no sirve aquí: son ocho horas, pensadas para «esa hora está llena,
    toma otra del mismo día». Quien pide las siete de la tarde no tiene nada más ese día --la
    clínica ya cerró-- y ocho horas hacia adelante siguen cayendo de madrugada. Sin cruzar el
    día, el paciente que escribe de noche, que es cuando la gente escribe, no recibiría ni
    una hora.

    Cuesta una consulta a Neon y una llamada a Google en un camino que antes no tocaba
    ninguna de las dos. Es deliberado y es barato: se paga una vez, antes de tomar ningún
    cupo, a cambio de que la conversación no muera en «esa hora no puede ser».
    """

    def trabajo(conn) -> list[datetime]:
        return _huecos_libres(conn, ctx, desde, desde + VENTANA_PROXIMO_HUECO)

    return await _con_base(ctx, trabajo)


def _texto_bloqueado(inicio: datetime, bloqueo: Bloqueo) -> str:
    """Lo que el modelo lee cuando el doctor apartó esa hora. RESULTADO, no excepción.

    No se le dice el título del evento: es la agenda privada del doctor y puede decir
    «cirugía de X» o algo personal. Al paciente le basta con que no está disponible.
    """
    return (
        f"Esa hora ({_formatear_hora(inicio)}) no está disponible: el doctor la tiene "
        "apartada en su calendario. NO la agendes y no insistas con ella. Consulta la "
        "disponibilidad y ofrécele al paciente lo que salga de ahí."
    )


def _huecos_libres(
    conn,
    ctx: ContextoDaniela,
    desde: datetime,
    hasta: datetime,
    *,
    maximo: int = 6,
    repartir: bool = False,
) -> list[datetime]:
    """Los bloques realmente libres: existen, no llegaron al tope, y nadie los bloqueó.

    `repartir` decide QUÉ seis de todos los libres se devuelven, y son dos preguntas
    distintas: «¿qué tienes el miércoles?» pide un panorama del día y lo quiere repartido;
    «esa hora está llena» pide lo más parecido a la hora que el paciente ya eligió, y ahí
    repartir le ofrecería las cinco de la tarde a quien acaba de pedir las nueve.

    Los tres filtros son distintos y ninguno sobra. Un bloque puede existir en la rejilla,
    tener cupo en Neon, y aun así estar bloqueado porque un doctor apartó esa hora en su
    calendario para una cirugía. Ofrecerlo sería mandar a alguien a una hora en la que no
    hay nadie.
    """
    rejilla = bloques_del_dia(
        desde,
        hasta,
        duracion_minutos=ctx.duracion_cita_minutos,
        # Un bloque que ya empezó no es un hueco libre. El filtro va aquí, en el único sitio
        # por el que pasan las tres consultas de agenda, y no en cada una.
        no_antes_de=ctx.ahora,
        # Y un bloque fuera del horario de la clínica tampoco. Sin esto, la ventana que pida
        # el modelo ES la oferta: una consulta del día completo devolvía 00:00, 01:00, 02:00.
        jornada=ctx.jornada,
    )
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
        # Sin reparto se corta en cuanto hay suficientes: son las MÁS CERCANAS a lo pedido,
        # que es lo que quiere quien preguntó por una hora concreta.
        if not repartir and len(libres) >= maximo:
            break
    return _repartidas(libres, maximo) if repartir else libres


def _repartidas(libres: list[datetime], maximo: int) -> list[datetime]:
    """Reduce a `maximo` bloques SIN quedarse con el principio del día.

    Visto en producción el 13/09/2026, con el miércoles 16 entero libre:

        «Sí, tengo disponibilidad el miércoles 16 a las 8:00 am, 9:00 am o 10:00 am.»

    El corte era `libres[:6]` sobre una rejilla ordenada, así que un día completo devolvía
    08:00 a 13:00 y la tarde **no le llegaba al modelo**: no es que la descartara, es que no
    existía para él. Quien solo puede después de almorzar se iba creyendo que no había nada.

    Se conservan siempre el primero y el último, y el resto se reparte a pasos iguales entre
    ellos: la oferta abarca la jornada en vez de amontonarse al principio. Ordenada, porque
    una lista de horas salteadas para que parezca variada es exactamente lo que un paciente
    lee como desorden.
    """
    if len(libres) <= maximo or maximo < 2:
        return libres
    ultimo = len(libres) - 1
    indices = sorted({round(i * ultimo / (maximo - 1)) for i in range(maximo)})
    return [libres[i] for i in indices]


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


def _fallo_consultar_citas(ctx: RunContextWrapper[Any], error: Exception) -> str:
    log.error("consultar_citas falló: %s", error)
    return (
        "No pudiste consultar las citas de este número. NO afirmes que tiene una cita ni "
        "digas ninguna hora de memoria, y NO intentes moverla ni cancelarla a ciegas. "
        "Escala a los doctores y dile al paciente que lo estás revisando."
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


def _fallo_privacidad(ctx: RunContextWrapper[ContextoDaniela], error: Exception) -> str:
    # No negociable 1: nunca confirmarle al paciente algo que no ocurrió. `pedir_baja` y
    # `revocar_baja` hacen su propio commit -- una excepción antes de eso deja la
    # transacción en rollback -- así que un fallo aquí significa que NO quedó anotado, y
    # decirle lo contrario es justo el error que ese no negociable prohíbe.
    # `log.exception` y no `log.error` como sus hermanas de arriba, y la diferencia es
    # deliberada: este es el ÚNICO `_fallo_*` cuya salida le pide a un humano que vaya a
    # anotar la baja a mano, y sin el traceback nadie sabe contra qué --si fue el pooler, el
    # esquema o la fila-- cuando llegue a hacerlo.
    #
    # Comprobado contra el SDK instalado (0.22.2): `failure_error_function` se invoca DENTRO
    # del `except` de `tool.__call__`, así que hay excepción viva y `exception()` adjunta el
    # traceback entero. El `%s` se queda para que la primera línea siga diciendo qué pasó sin
    # tener que bajar a leerlo.
    log.exception("no se pudo registrar la decisión de privacidad: %s", error)
    return (
        "No se pudo registrar todavía. NO le digas al paciente que quedó anotado: no es "
        "cierto. Dile que lo estás resolviendo y escala a los doctores para que alguien lo "
        "anote a mano: esto NO se deja pasar en silencio."
    )


# ==========================================================================================
# 1. consultar_base_conocimiento
# ==========================================================================================


async def _consultar_base_conocimiento(
    ctx: ContextoDaniela, tratamiento: str, pregunta: str
) -> str:
    # Lo que dijo la consulta EXACTA, antes del respaldo por tratamiento. Es una lista y no
    # un `bool` para poder distinguir «no se consultó» de «se consultó y no había»: si
    # `_con_base` no llega a correr `trabajo` --una prueba que lo dobla, un fallo abriendo la
    # conexión-- queda vacía, y la señal cae al criterio de siempre.
    hubo_dato_exacto: list[bool] = []

    def trabajo(conn) -> str:
        texto = persistencia.consultar_conocimiento(conn, tratamiento, pregunta)
        # ESTO, y no el texto de abajo, es lo que alimenta la señal de «sin resolver». El
        # respaldo tapa el hueco para el modelo --a propósito-- pero taparlo también para la
        # medición hacía `FALTA_DATO` inalcanzable: los doce tratamientos de la base tienen
        # fichas, así que un hueco de CONCEPTO («la cuota mensual de ortodoncia») se volvía
        # invisible y lo único que llegaba a producir caso era un tratamiento entero vacío.
        # El ejemplo bandera del informe --«falta el precio de ORTODONCIA, 7 veces, y 5 de
        # los 7 preguntan por la cuota»-- es justo un hueco de concepto.
        hubo_dato_exacto.append(not texto.startswith("SIN DATO DOCUMENTADO"))
        if texto.startswith("SIN DATO DOCUMENTADO") and pregunta:
            # El concepto exacto no existía. Antes de declarar que no hay dato, se mira si
            # el tratamiento tiene algo documentado: devolver más información aprobada es
            # seguro; lo que nunca se hace es rellenar el hueco con una estimación.
            texto = persistencia.consultar_conocimiento(conn, tratamiento)
        return texto

    texto = await _con_base(ctx, trabajo)

    # Se anota SIEMPRE, con dato o sin él. Sin dato produce un caso `FALTA_DATO`; con dato no
    # produce nada, pero deja el tratamiento con el que se enriquece la huella de un guardrail
    # que salte después en este mismo turno. Solo memoria: la escritura ocurre al final, en
    # `atencion._anotar_resultado`, cuando el paciente ya recibió su respuesta.
    #
    # `hubo_dato` sale de la consulta EXACTA. Lo que el modelo ve --`texto`-- no cambia ni un
    # carácter por esto: esta tool sigue siendo un observador y Daniela responde igual con la
    # medición encendida o apagada. Efecto lateral aceptado: también abren caso los conceptos
    # que el modelo se invente. Está bien, y no lleva lista blanca: la ventana de 30 días
    # ordenada por frecuencia los hunde sola, y un modelo que inventa el mismo concepto una y
    # otra vez ES una señal que vale la pena ver.
    ctx.turno.senales.append(
        Senal(
            tratamiento=tratamiento,
            concepto=pregunta,
            hubo_dato=(
                hubo_dato_exacto[0]
                if hubo_dato_exacto
                else not texto.startswith("SIN DATO DOCUMENTADO")
            ),
        )
    )

    # Lo que esta consulta autoriza a decir en este turno. `sin_cifra_no_documentada` exige
    # que toda cifra del mensaje al paciente esté aquí: si Daniela dice un precio que esta
    # tool no devolvió, el tripwire salta. Registrarlo aquí --y no confiar en que el modelo
    # "use lo que le dieron"-- es lo que convierte esa instrucción en una garantía.
    ctx.turno.cifras_autorizadas |= cifras_de(texto)
    # Y las horas, por lo mismo. Faltaba: la fila `_general`/`horario` es la que Daniela
    # RECITA cuando le preguntan a qué hora abren, y «de 8:00 a 17:00» son dos horas
    # concretas. Sin registrarlas, `sin_hora_no_verificada` bloqueaba una respuesta que
    # MaxiCare había aprobado palabra por palabra. La base de conocimiento es contenido
    # aprobado, igual que las cifras: el criterio de confianza es el mismo.
    ctx.turno.horas_autorizadas |= horas_de(texto)
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

    # Una ventana más corta que un bloque produce una rejilla VACÍA, y hasta el 13/09/2026 eso
    # se contaba como «no hay cupo»: exactamente el mismo texto que con la agenda saturada.
    # El paciente pedía «el 16 tipo 10 am» --que invita a pedir 10:00-10:30-- con la agenda
    # entera libre, y se iba creyendo que no había nada. Se estira lo justo para que el bloque
    # que el paciente tiene en la cabeza quepa: preguntó por las 10, se le responde por las 10.
    paso = timedelta(minutes=ctx.duracion_cita_minutos)
    ventana_corta = fin - inicio < paso
    fin_efectivo = inicio + paso if ventana_corta else fin

    def trabajo(conn) -> str:
        # `repartir`: esta es la pregunta «¿qué tienes ese día?», y la respuesta tiene que
        # abarcar la jornada. Ver `_repartidas`.
        libres = _huecos_libres(conn, ctx, inicio, fin_efectivo, repartir=True)
        if libres:
            return _texto_alternativas(libres)
        # «Cerrado» y «lleno» son opuestos y hasta el 13/09/2026 decían lo mismo. Si NINGÚN
        # bloque de la ventana cae dentro de la jornada, no es que no quede cupo: es que la
        # clínica no abre a esa hora, y «no hay cupo» manda al paciente a buscar otro DÍA
        # cuando lo que necesita es otra HORA. La rejilla sin `no_antes_de` es justo la
        # pregunta «¿existe algún bloque aquí?», sin mezclarla con si ya pasó.
        if not bloques_del_dia(
            inicio,
            fin_efectivo,
            duracion_minutos=ctx.duracion_cita_minutos,
            jornada=ctx.jornada,
        ):
            return _texto_fuera_de_horario(
                ctx, inicio, _huecos_libres(conn, ctx, inicio, inicio + VENTANA_PROXIMO_HUECO)
            )
        # Antes de declarar que no hay nada, se mira el resto de la jornada. Es la misma
        # ventana que `crear_cita` usa para sus alternativas: sin esto, la consulta era la
        # única de las dos que dejaba al paciente sin una sola opción concreta.
        cercanos = _huecos_libres(conn, ctx, inicio, inicio + VENTANA_ALTERNATIVAS)
        if not cercanos:
            return _texto_alternativas([])
        return (
            f"En {_formatear_hora(inicio)} no hay cupo. Estos sí están libres el mismo día: "
            + "; ".join(_formatear_hora(b) for b in cercanos)
        )

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

    # Una ficha con el marcador `PENDIENTE` no registra a nadie: se trata igual que no tener
    # ninguna, y a partir de aquí este camino no la distingue.
    #
    # Sin esto, `nombre_registrado` no era `None`, así que ni entraba en la rama de «puede ser
    # alguien escribiendo por primera vez» ni dejaba `telefono_sin_paciente` en `True` -- y
    # ese booleano es el permiso de crear la PRIMERA cita. Producción, 16/09/2026: una
    # paciente nueva dio su nombre, no «coincidió» con el marcador, se le pidió que lo diera
    # «tal como aparece en tu historia clínica» --nunca había venido--, se gastaron los dos
    # intentos y la tool ordenó escalar. Se fue sin cita.
    if nombre_registrado == persistencia.NOMBRE_PENDIENTE:
        nombre_registrado = None

    # Se refresca con lo que acaba de devolver la base, no se deja lo que trajo el turno: si
    # alguien registró a este paciente entre `_leer_estado` y esta llamada, el dato del turno
    # ya es viejo. Es el mismo hecho que decide si puede pedir su primera cita.
    ctx.telefono_sin_paciente = nombre_registrado is None and not coincide

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


def _descripcion_del_evento(ctx: ContextoDaniela, solicitud: SolicitudCita) -> str:
    """Lo que la clínica lee al abrir la cita en su calendario.

    Pedido por MaxiCare el 13/09/2026: «el nombre, el número de teléfono y por qué agendó,
    o sea el servicio que está interesado». Antes decía solo «Agendado por Daniela.
    Conversación <uuid>», y con eso no se puede llamar a nadie para mover una cita ni para
    avisar de algo.

    El teléfono lo pone AQUÍ el código, leyéndolo de `ctx`, y no el modelo: `SolicitudCita`
    no tiene campo de teléfono justamente para que no pueda escribir otro --la hija que
    agenda por su madre--. El número del evento es el número desde el que se escribió.

    El servicio sale de `tratamiento`, que siempre llega; `motivo` es opcional y se suma
    cuando el modelo lo escribió. Por eso el «por qué» no depende de que el modelo llene
    nada: un campo que se puede omitir acaba omitido.
    """
    telefono = ctx.telefono_completo.strip()
    if telefono and not telefono.startswith("+"):
        telefono = f"+{telefono}"

    lineas = [
        f"Teléfono: {telefono}" if telefono else "Teléfono: PENDIENTE",
        f"Servicio: {solicitud.tratamiento}",
    ]
    if solicitud.motivo.strip():
        lineas.append(f"Motivo: {solicitud.motivo.strip()}")
    lineas.append(f"Agendado por Daniela. Conversación {ctx.id_conversacion}.")
    return "\n".join(lineas)


async def _crear_cita(ctx: ContextoDaniela, solicitud: SolicitudCita) -> str:
    inicio = (
        solicitud.inicio
        if solicitud.inicio.tzinfo
        else solicitud.inicio.replace(tzinfo=ZONA_BOGOTA)
    )

    # Antes de tocar la base, y como RESULTADO y no como excepción: el modelo tiene que poder
    # seguir conversando con esto. Un cupo consumido sobre una hora del pasado no lo libera
    # nadie, y la cita sería para un día que ya pasó.
    if inicio < ctx.ahora:
        return (
            f"Esa hora ({_formatear_hora(inicio)}) ya pasó: hoy es "
            f"{_formatear_hora(ctx.ahora)}. NO la agendes. Confirma con el paciente qué "
            "fecha futura quiere y consulta la disponibilidad de nuevo."
        )

    # Fuera del horario de la clínica no se agenda, lo pida quien lo pida. Va antes del
    # bloqueo del doctor por lo mismo que la hora pasada: es local, y `bloqueos()` es una
    # llamada a Google.
    if not ctx.jornada.cabe(inicio, ctx.duracion_cita_minutos):
        texto = _texto_fuera_de_horario(ctx, inicio, await _proximos_huecos(ctx, inicio))
        # Sin esto el texto es correcto y da igual: el horario que Daniela le recuerde al
        # paciente son horas concretas, y `sin_hora_no_verificada` bloquearía el mensaje.
        ctx.turno.horas_autorizadas |= horas_de(texto)
        return texto

    # Google Calendar es la fuente de la disponibilidad, y eso vale también en el momento de
    # escribir. Ver `_bloqueo_que_tapa`: sin esto, una hora que el doctor apartó se podía
    # agendar igual y el evento acababa encima de su cirugía.
    bloqueo = await _bloqueo_que_tapa(ctx, inicio)
    if bloqueo is not None:
        return _texto_bloqueado(inicio, bloqueo)

    # La clave la arma el orquestador, igual que en `reprogramar` y en `seguimiento`, y se
    # ancla al HORARIO pedido -- no al «intento», que es lo que el modelo cree que significa.
    #
    # Con `solicitud.clave_idempotencia`, que rellena el modelo, `tomar_cupo` --que busca por
    # clave SIN filtrar por `inicio` ni por conversación-- hacía tres cosas distintas, todas
    # malas:
    #
    # 1. *Misma clave, otro horario.* El paciente pide las 10:00 y luego «mejor a las 15:00»;
    #    el modelo repite la clave porque para él es el mismo intento. Se devuelve la reserva
    #    de las 10:00 tal cual: las 15:00 NO consumen cupo (con capacidad 2 se venden 3), las
    #    10:00 quedan bloqueadas para nadie, y dos citas cuelgan de una sola reserva.
    # 2. *Claves distintas para el mismo intento.* Un reintento consume un segundo cupo y crea
    #    un segundo evento en Google: un solo paciente agota la hora.
    # 3. *Colisión entre pacientes.* La columna es UNIQUE global y al modelo se le OCULTA el
    #    teléfono a propósito, así que lo natural que puede inventar es algo como
    #    `cita-2026-09-15T10:00-limpieza`. Dos pacientes que piden el mismo bloque generan la
    #    misma cadena, el segundo recibe la reserva del primero, y los dos salen confirmados
    #    sobre un solo cupo.
    #
    # `ctx.clave` lleva el `id_conversacion` delante, que es lo que hace imposible el caso 3,
    # y el `inicio` detrás, que es lo que hace imposible el 1 sin romper el 2.
    clave = ctx.clave("cita", inicio.isoformat())

    def tomar(conn) -> tuple[tuple[int, int] | None, dict[str, Any] | None, list[datetime]]:
        cupo = persistencia.tomar_cupo(
            conn,
            inicio=inicio,
            capacidad=ctx.capacidad_por_hora,
            clave_idempotencia=clave,
            conversacion_id=ctx.id_conversacion,
        )
        if cupo is not None:
            # `tomar_cupo` era idempotente y esta tool no: un acierto de clave devuelve la
            # reserva que ya existía, y el código seguía derecho a `crear_evento` +
            # `registrar_cita`. Dos llamadas con la misma conversación y el mismo horario
            # dejaban UNA reserva, DOS eventos en el calendario del doctor y DOS citas, las
            # dos confirmadas al paciente con ids distintos. Esta consulta es lo que
            # convierte «el cupo ya era tuyo» en «la cita ya era tuya».
            return (cupo, persistencia.cita_viva_de_reserva(conn, cupo[0]), [])
        # Solo se buscan alternativas si hizo falta: una consulta de más en el camino feliz
        # es latencia que paga cada paciente.
        return (None, None, _huecos_libres(conn, ctx, inicio, inicio + VENTANA_ALTERNATIVAS))

    cupo, ya_existente, alternativas = await _con_base(ctx, tomar)

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

    if ya_existente is not None:
        # Esta reserva ya tiene su cita: este intento es un duplicado, no una cita nueva. Se
        # devuelve LA MISMA confirmación, con el id que ya existe, y no se toca el
        # calendario. Decirle al modelo que se creó otra le haría confirmarle al paciente un
        # id distinto para la misma hora, y dejaría un segundo evento en la agenda del
        # doctor sobre un solo cupo.
        #
        # No se libera el cupo: es del paciente, y la cita que cuelga de él es válida.
        texto = (
            f"Cita confirmada para {ya_existente['nombre_completo']}, "
            f"{_formatear_hora(inicio)}, {ya_existente['tratamiento']}. "
            f"Id de la cita: {ya_existente['id']}."
        )
        log.info(
            "crear_cita repetida sobre la reserva %s: se devuelve la cita %s sin crear otra",
            reserva_id,
            ya_existente["id"],
        )
        ctx.turno.horas_autorizadas |= horas_de(texto)
        return texto

    # El cupo ya está apartado. A partir de aquí, cualquier salida que no sea una cita
    # completa tiene que devolverlo.
    try:
        evento_id = await asyncio.to_thread(
            ctx.calendario.crear_evento,
            inicio=inicio,
            duracion_minutos=ctx.duracion_cita_minutos,
            titulo=f"{solicitud.nombre_completo} · {solicitud.tratamiento}",
            descripcion=_descripcion_del_evento(ctx, solicitud),
        )
    except ErrorDeCalendario as e:
        await asyncio.to_thread(_liberar, ctx, reserva_id)
        log.error("Calendar falló tras tomar el cupo; reserva %s liberada", reserva_id)
        raise CitaNoConfirmada(
            "El cupo se tomó en la base pero el calendario no respondió. La reserva se "
            "liberó y la cita NO existe. No se le puede confirmar nada al paciente."
        ) from e

    # CUÁNDO sale el recordatorio se decide AQUÍ, fuera de la transacción, y no dentro de
    # `guardar`. Es una función pura de tres valores que ya se conocen --el horario pedido,
    # `ctx.ahora` y `ctx.jornada`--: ninguno depende de que la cita se haya escrito. Dentro
    # quedan solo las dos ESCRITURAS, y con eso ningún fallo del recordatorio puede tumbar la
    # cita: un error de lógica al calcular el momento revienta antes de que exista nada que
    # deshacer, y lo único que puede fallar del `INSERT` --que la base se caiga-- habría
    # matado igual el `commit` de la propia cita. La seguridad clínica prevalece: perder una
    # cita por un recordatorio es exactamente el intercambio que este proyecto prohíbe.
    #
    # `None` no es un fallo: es «esta cita no lleva recordatorio». La que se agenda para
    # dentro de dos horas no lo lleva porque el paciente acaba de hablar con Daniela.
    cuando_recordar = seguimientos.momento_del_recordatorio(
        inicio_cita=inicio,
        ahora=ctx.ahora,
        jornada=ctx.jornada,
        hora_vispera=ctx.hora_recordatorio_vispera,
        horas_minimas=ctx.horas_minimas_para_recordar,
    )

    def guardar(conn) -> tuple[str, int]:
        # Quien saca su primera cita deja de ser un desconocido, y aquí es donde deja de
        # serlo. Sin esta fila, `identificar_paciente` no tendría nunca contra qué
        # verificarlo --solo mira `pacientes`-- y `reprogramar_cita` y `cancelar_cita`, que
        # sí exigen identidad, se le quedaban cerradas PARA SIEMPRE: pedía mover su propia
        # cita y recibía «te escribe el doctor». `asegurar_paciente` estaba escrito desde la
        # fase 1 y ninguna tool lo llamaba.
        #
        # No pisa el nombre de una ficha que ya exista -- eso lo decide `asegurar_paciente`,
        # y su docstring explica por qué: dos personas en el teléfono de la casa.
        paciente_id = ctx.id_paciente or persistencia.asegurar_paciente(
            conn,
            nombre_completo=solicitud.nombre_completo,
            telefono=ctx.telefono_completo,
        )
        id_cita = persistencia.registrar_cita(
            conn,
            reserva_id=reserva_id,
            conversacion_id=ctx.id_conversacion,
            paciente_id=paciente_id,
            nombre_completo=solicitud.nombre_completo,
            telefono=ctx.telefono_completo,
            tratamiento=solicitud.tratamiento,
            inicio=inicio,
            duracion_minutos=ctx.duracion_cita_minutos,
            evento_calendar_id=evento_id,
            commit=False,
        )
        # El recordatorio lo emite el CÓDIGO, no el modelo. `programar_seguimiento` sigue
        # existiendo para lo que sí es criterio suyo --«llámenme el lunes»--, pero un
        # recordatorio que depende de que se acuerde es un recordatorio que a veces no
        # existe, y nadie se entera de cuál faltó. Es el no-negociable 2 aplicado a otro
        # caso: lo que tiene que ocurrir siempre no lo decide el modelo.
        #
        # Va en la MISMA transacción que la cita --de ahí el `commit=False` de las dos
        # escrituras y el `conn.commit()` de abajo--: separadas, una caída entre ellas deja
        # una cita sin recordatorio y nadie se entera hasta que el paciente no llega. El
        # CUÁNDO ya se calculó arriba, fuera de la transacción, a propósito.
        if cuando_recordar is not None:
            nuevo = persistencia.insertar_seguimiento(
                conn,
                id_conversacion=ctx.id_conversacion,
                tipo="recordatorio_cita",
                fecha_objetivo=cuando_recordar,
                # La clave la arma `ctx.clave`, como las otras cuatro. Nunca el modelo.
                clave_idempotencia=ctx.clave("recordatorio", id_cita),
                cita_id=id_cita,
                commit=False,
            )
            if not nuevo:
                # Aquí `id_cita` es un UUID recién creado, así que esta clave no puede haber
                # existido antes: si el booleano dice que sí, hay algo que no entendemos y
                # tiene que dejar rastro. Se anota y no se interrumpe -- la cita está bien, y
                # tumbarla por el recordatorio es el intercambio que este proyecto prohíbe.
                log.info(
                    "el recordatorio de la cita recién creada %s ya estaba programado; "
                    "no se duplica",
                    id_cita,
                )
        conn.commit()
        return (id_cita, paciente_id)

    id_cita, paciente_id = await _con_base(ctx, guardar)
    # El contexto deja de mentir en el mismo turno. La ficha ya está en la base, así que el
    # `bool(paciente)` de `atencion._leer_estado` lo daría por verificado en el siguiente:
    # dejarlo en False aquí solo haría que el turno en curso creyera otra cosa que la base.
    ctx.id_paciente = paciente_id
    if ctx.nombre_paciente is None:
        ctx.nombre_paciente = solicitud.nombre_completo
    ctx.telefono_sin_paciente = False
    ctx.identidad_verificada = True
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


def _es_ajena(ctx: ContextoDaniela, cita: dict[str, Any]) -> bool:
    """¿Esta cita es de otra persona? La pertenencia se ancla al TELÉFONO.

    Lo que había era:

        if ctx.id_paciente is not None and cita["paciente_id"] not in (None, ctx.id_paciente)

    y tenía dos huecos que nadie podía alcanzar mientras `identidad_antes_de_datos` frenara
    a todo el que no tuviera ficha: con `ctx.id_paciente is None` no comprobaba NADA, y una
    cita con `paciente_id = NULL` la daba por buena para cualquiera. El arreglo que permite
    agendar a un paciente nuevo volvía el segundo hueco alcanzable --esas citas pasaban a ser
    las normales--, así que el candado se ata a lo que de verdad identifica al dueño: el
    número desde el que se escribe, que es el que quedó guardado en la propia cita.

    Que el id de una cita sea un UUID que solo conoce quien lo recibió no es un control de
    acceso; es una contraseña que el propio sistema le enseñó al paciente por WhatsApp.

    Se conserva la pertenencia por `paciente_id`: una persona puede cambiar de número, o
    tener una cita que le abrió la clínica desde otro teléfono, y seguir siendo la dueña.
    """
    if cita.get("telefono") == ctx.telefono_completo:
        return False
    if ctx.id_paciente is not None and cita.get("paciente_id") == ctx.id_paciente:
        return False
    return True


async def _reprogramar_cita(ctx: ContextoDaniela, id_cita: str, nuevo_inicio: str) -> str:
    destino = _a_fecha(nuevo_inicio, "nuevo_inicio")

    # El mismo guardia que `crear_cita`, y aquí duele más: crear en el pasado deja una cita
    # fantasma, pero MOVER al pasado además DESTRUYE una cita buena. `mover_cita` la lleva al
    # día que ya pasó, `liberar_cupo` suelta el cupo que el paciente sí tenía y `mover_evento`
    # arrastra el evento del doctor detrás. El paciente se queda sin la cita que tenía y se lo
    # confirmamos como un cambio normal.
    #
    # Va ANTES del bloqueo del doctor a propósito: es una comparación local y `bloqueos()` es
    # una llamada a Google. Una hora del pasado no merece esa llamada.
    if destino < ctx.ahora:
        return (
            f"Esa hora ({_formatear_hora(destino)}) ya pasó: hoy es "
            f"{_formatear_hora(ctx.ahora)}. NO muevas la cita ahí; sigue donde estaba. "
            "Confirma con el paciente qué fecha futura quiere y consulta la disponibilidad."
        )

    # Mover una cita fuera del horario es el mismo defecto que crearla ahí.
    if not ctx.jornada.cabe(destino, ctx.duracion_cita_minutos):
        texto = _texto_fuera_de_horario(ctx, destino, await _proximos_huecos(ctx, destino))
        ctx.turno.horas_autorizadas |= horas_de(texto)
        return texto

    # Mover una cita a una hora que el doctor apartó es el mismo defecto que crearla ahí, y
    # se comprueba en el mismo sitio: antes de tocar la base. Ver `_bloqueo_que_tapa`.
    bloqueo = await _bloqueo_que_tapa(ctx, destino)
    if bloqueo is not None:
        return _texto_bloqueado(destino, bloqueo)

    # La clave se ancla al VALOR DESTINO y no a un delta. Con «mover dos horas», un reintento
    # movería la cita otras dos horas: el mismo intento produciría un resultado distinto cada
    # vez que la red falle. Con el destino, repetirlo es inofensivo.
    clave = ctx.clave("reprogramar", id_cita, destino.isoformat())

    def trabajo(conn) -> tuple[str, dict[str, Any] | None, tuple[int, int] | None, list[datetime]]:
        cita = persistencia.leer_cita(conn, id_cita)
        if cita is None:
            return ("no_existe", None, None, [])
        if _es_ajena(ctx, cita):
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

    # Fuera de la transacción, por lo mismo que en `crear_cita`: el CUÁNDO es una función
    # pura del destino, de `ctx.ahora` y de la jornada, y nada de eso depende de que la cita
    # se haya movido. Dentro de `aplicar` quedan solo escrituras, y así un error calculando
    # el momento no puede deshacer un movimiento que en Google Calendar YA ocurrió.
    cuando_recordar = seguimientos.momento_del_recordatorio(
        inicio_cita=destino,
        ahora=ctx.ahora,
        jornada=ctx.jornada,
        hora_vispera=ctx.hora_recordatorio_vispera,
        horas_minimas=ctx.horas_minimas_para_recordar,
    )

    # La clave del recordatorio nuevo lleva TRES componentes detrás del id de la cita, y cada
    # uno tapa un caso que los otros dos no:
    #
    # - `id_cita`, para que la cascada sepa de qué cita cuelga.
    # - `destino`, para que dos horas distintas no compartan fila.
    # - `ctx.ahora`, que es lo que separa «el mismo intento» de «otro movimiento». Es fijo
    #   dentro de un turno, así que un reintento de esta misma tool en este mismo turno vuelve
    #   a dar ESTA clave --idempotente, que es justo lo que queremos-- pero un A -> B -> A en
    #   turnos distintos da una clave nueva para el regreso a A. Sin él, al volver a A la
    #   clave acertaría la del primer movimiento, `ON CONFLICT DO NOTHING` descartaría la
    #   inserción, y la cita quedaría movida y sin ningún recordatorio vivo.
    #
    # La arma `ctx.clave`, como las otras cuatro. Nunca el modelo.
    clave_recordatorio = ctx.clave(
        "recordatorio", id_cita, destino.isoformat(), ctx.ahora.isoformat()
    )

    def aplicar(conn) -> None:
        persistencia.mover_cita(
            conn, id_cita, reserva_id=reserva_nueva, inicio=destino, commit=False
        )
        # La cascada, y el ORDEN es parte de ella: primero se programa el nuevo, después se
        # anulan los viejos perdonando el que se acaba de programar. Al revés --anular y
        # luego insertar-- un reintento de la misma reprogramación anularía su propia fila y
        # chocaría al reinsertarla, dejando la cita movida y SIN recordatorio: exactamente el
        # fallo que esta tarea existe para eliminar, reintroducido por la clave.
        #
        # El recordatorio viejo habla de una hora de la que el paciente acaba de salir:
        # mandarlo la víspera lo devuelve a la hora que él mismo pidió cambiar. Se anula, no
        # se borra: el motivo es lo que permite responder «¿por qué este paciente no recibió
        # recordatorio?». Todo dentro de la MISMA transacción que el movimiento de la cita.
        if cuando_recordar is not None:
            nuevo = persistencia.insertar_seguimiento(
                conn,
                id_conversacion=ctx.id_conversacion,
                tipo="recordatorio_cita",
                fecha_objetivo=cuando_recordar,
                clave_idempotencia=clave_recordatorio,
                cita_id=id_cita,
                commit=False,
            )
            if not nuevo:
                # Con `ctx.ahora` dentro de la clave, esto solo puede ser un reintento de esta
                # misma tool en este mismo turno: la fila ya está ahí y sigue viva, porque
                # `excepto_clave` la perdona. Es benigno y esperado -- por eso `info` y no
                # `error`-- pero descartar el booleano en silencio era lo que impedía verlo.
                log.info(
                    "el recordatorio de la cita %s para %s ya estaba programado en este "
                    "turno; no se duplica",
                    id_cita,
                    destino.isoformat(),
                )
        persistencia.anular_seguimientos_de_cita(
            conn,
            id_cita,
            motivo="cita_reprogramada",
            excepto_clave=clave_recordatorio,
            commit=False,
        )
        conn.commit()
        # El cupo viejo va DESPUÉS del commit, en su propia transacción, que es donde
        # estaba antes de que esto tuviera cascada: `mover_cita` confirmaba y `liberar_cupo`
        # venía detrás. Metido dentro, un DELETE que fallara desharía también el movimiento
        # de la cita --que en Google Calendar YA ocurrió-- y dejaría a los dos sistemas
        # contando cosas distintas. Un cupo que se queda sin liberar es un horario perdido;
        # una cita que Neon y Calendar sitúan en horas distintas es un paciente en la puerta.
        #
        # `!= reserva_nueva` no es una defensa contra nada teórico. La clave de `tomar_cupo`
        # --`ctx.clave("reprogramar", id_cita, destino)`-- no lleva componente de turno, así
        # que repetir LA MISMA reprogramación al MISMO destino en un turno posterior devuelve
        # la reserva que ya existía: `reserva_nueva == reserva_vieja`, y esta línea borraba el
        # cupo que la cita está usando. `citas.reserva_id` es `ON DELETE SET NULL`, así que el
        # DELETE no falla, no lanza y no registra nada: la fila queda con `reserva_id = NULL`,
        # la hora vuelve a contarse libre, y con `capacidad_por_hora = 2` se venden tres. Eso
        # es sobreventa de una clínica real, y la seguridad clínica gana ese empate.
        #
        # La causa de fondo --una clave de idempotencia sin componente de turno-- queda por
        # arreglar aparte: tocarla es tocar `ctx.clave` y la contabilidad de cupos entera.
        if reserva_vieja and reserva_vieja != reserva_nueva:
            persistencia.liberar_cupo(conn, reserva_vieja)

    await _con_base(ctx, aplicar)
    # Las DOS horas, y la vieja no es un adorno: confirmar un cambio exige decir de dónde a
    # dónde, y `ctx.turno` se vacía en cada turno, así que la hora anterior --autorizada
    # cuando se agendó-- ya no lo está. Sin nombrarla aquí, `sin_hora_no_verificada` bloqueaba
    # la confirmación de un cambio QUE YA HABÍA OCURRIDO: la cita movida y el paciente
    # recibiendo el mensaje seguro, camino de presentarse a la hora vieja. Sale de `leer_cita`
    # --de la base, en este turno-- así que autorizarla es el mismo criterio de siempre.
    texto = (
        f"Cita {id_cita} reprogramada: estaba en {_formatear_hora(cita['inicio'])} y ahora "
        f"queda en {_formatear_hora(destino)}."
    )
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
    if _es_ajena(ctx, cita):
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
        persistencia.marcar_cita_cancelada(
            conn, solicitud.id_cita, motivo=solicitud.motivo, commit=False
        )
        # La otra mitad de la cascada, y la que más se nota: sin esto, el paciente que
        # canceló recibe la víspera un recordatorio de la cita que acaba de cancelar. Va en
        # la MISMA transacción que la cancelación, porque una caída entre las dos deja un
        # recordatorio vivo apuntando a una cita muerta -- y el despachador lo mandaría.
        persistencia.anular_seguimientos_de_cita(
            conn, solicitud.id_cita, motivo="cita_cancelada", commit=False
        )
        conn.commit()
        # Fuera de la transacción, por lo mismo que en `reprogramar`: la cancelación y sus
        # recordatorios son lo que tiene que ser atómico. Si el cupo no se libera, la
        # clínica pierde un horario; si la cancelación se deshiciera por eso, el paciente
        # que canceló seguiría citado.
        if cita["reserva_id"]:
            persistencia.liberar_cupo(conn, cita["reserva_id"])

    await _con_base(ctx, aplicar)

    # La hora cancelada va en el texto y queda autorizada, por lo mismo que en `reprogramar`:
    # «tu cita del martes a las 9 quedó cancelada» es la frase natural, y sin autorizarla
    # `sin_hora_no_verificada` bloqueaba la confirmación de una cancelación YA APLICADA. El
    # paciente se quedaba creyendo que su cita sigue en pie y el cupo ya estaba libre para
    # otro. Sale de `leer_cita`, no de la memoria del modelo.
    cuando = _formatear_hora(cita["inicio"])
    if calendario_ok:
        texto = f"Cita {solicitud.id_cita} ({cuando}) cancelada y ese horario quedó libre."
    else:
        texto = (
            f"Cita {solicitud.id_cita} ({cuando}) cancelada y ese horario quedó libre, pero "
            "el evento sigue en el calendario de los doctores. Escala para que alguien lo "
            "borre a mano."
        )
    ctx.turno.horas_autorizadas |= horas_de(texto)
    return texto


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
    # La baja comercial, EN CÓDIGO y no solo en el prompt. `seguimientos.decidir` ya la
    # recoge con G0 al despachar, así que sin esto no sale nada -- pero entonces lo único
    # que impide INSERTAR la fila es que el modelo obedezca una instrucción, y en este
    # proyecto lo que tiene que ser cierto lo escribe el código (no negociables 2, 12, 22).
    # Se devuelve texto en vez de lanzar: el modelo tiene que saber por qué no se programó,
    # o lo intentará otra vez con otra fecha.
    #
    # `ctx.pidio_no_contacto` sale de `contactos`, nunca del modelo, y el tipo se mira contra
    # la MISMA lista blanca del despachador: un recordatorio de cita se programa igual, que
    # es justo lo que la baja no puede apagar (no negociable 25).
    if ctx.pidio_no_contacto and tipo not in seguimientos.TIPOS_NO_COMERCIALES:
        return (
            "Este paciente pidió que no le escribieran más, así que no se programó nada "
            "comercial. No se lo ofrezcas ni se lo menciones. El recordatorio de una cita "
            "suya sí se sigue programando: eso no es publicidad."
        )

    objetivo = _a_fecha(fecha_objetivo, "fecha_objetivo")
    # `ctx.ahora` y no `_ahora()`: el instante del turno, que una prueba puede fijar. Es la
    # regla que el propio docstring de `ContextoDaniela.ahora` declara, y esta tool era la
    # única que se salía. En producción los dos valores coinciden; lo que cambia es que el
    # comportamiento deja de depender del reloj de la máquina, y por tanto se puede probar.
    if objetivo <= ctx.ahora:
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
    """Deja programado un seguimiento comercial para más adelante.

    Si el paciente pidió que no le escribieran más, no se programa nada y te lo dice: no
    insistas ni lo intentes con otra fecha. Los recordatorios de una cita NO se piden por
    aquí — los programa el sistema solo al crear o mover la cita.

    Args:
        tipo: qué clase de seguimiento, por ejemplo 'reactivacion'.
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


def _escapar_html(texto: str) -> str:
    """Telegram va en `parse_mode=HTML` y RECHAZA el mensaje entero si el HTML no cierra.

    `quote=False` deja las comillas en paz: dentro de un texto no son HTML, y escaparlas solo
    llenaría el aviso de `&#x27;` donde el doctor espera leer una frase.
    """
    return html.escape(texto, quote=False)


def _aviso_para_doctores(ctx: ContextoDaniela, solicitud: SolicitudEscalamiento) -> str:
    """El texto del escalamiento, con TODO lo ajeno escapado.

    No es cosmético, y el coste no es un aviso feo: es un aviso que no existe. Telegram va en
    `parse_mode=HTML` y RECHAZA el mensaje entero si el HTML no cierra --un paciente llamado
    «Ana <3 Gómez» basta--. `canales.enviar_mensaje` lanza `ErrorDeCanal`,
    `failure_error_function` se lo traga para que el modelo siga conversando, y la fila del
    escalamiento ya quedó escrita en Neon: el aviso de cierre de turno ve la clave quemada y
    se calla. Resultado: dos filas de escalamiento y CERO Telegram. Un paciente con dolor
    «escalado» en una tabla que nadie mira.

    Se escapan el nombre (lo dicta el paciente), el resumen y la pregunta (los escribe el
    modelo a partir de lo que dijo el paciente) y el motivo. Lo único que queda como HTML de
    verdad son las etiquetas que pone esta función.
    """
    nombre = _escapar_html(ctx.nombre_paciente or "paciente sin identificar")
    return (
        f"<b>Escalamiento · {_escapar_html(solicitud.motivo)}</b>\n"
        f"{nombre} · +{ctx.telefono_completo}\n\n"
        f"{_escapar_html(solicitud.resumen_para_doctor)}\n\n"
        f"<b>Pregunta:</b> {_escapar_html(solicitud.pregunta_concreta)}"
    )


async def _escalar_a_doctores(
    ctx: ContextoDaniela, solicitud: SolicitudEscalamiento, *, telegram: Any | None = None
) -> str:
    # La clave la arma el orquestador, no el modelo, y aquí no es una regla de estilo.
    #
    # Hasta ahora esta tool usaba `solicitud.clave_idempotencia`, que es un campo que RELLENA
    # EL MODELO: si escribía cualquier otra cosa --y es libre de hacerlo-- esta clave y la
    # que arma `runtime._avisar_a_doctores` para el mismo turno no coincidían, la
    # deduplicación no deduplicaba nada, y el doctor recibía dos Telegram del mismo
    # escalamiento. A la cuarta alerta repetida deja de mirarlas.
    #
    # `ctx.clave(...)` no se puede inventar: sale del id de la conversación y del turno que
    # lleva `ContextoDaniela`. El campo de la solicitud se conserva porque le hace pensar al
    # modelo en la unicidad de lo que pide, pero ya no decide nada.
    clave = ctx.clave("escalamiento", ctx.turno_actual)

    def registrar(conn) -> int | None:
        return persistencia.insertar_escalamiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            motivo=solicitud.motivo,
            resumen=solicitud.resumen_para_doctor,
            pregunta=solicitud.pregunta_concreta,
            clave_idempotencia=clave,
        )

    escalamiento_id = await _con_base(ctx, registrar)

    if escalamiento_id is None:
        # La clave ya existía. Antes se salía aquí, y eso quemaba la clave al INTENTAR en
        # vez de al CONSEGUIR: si el primer intento escribió la fila y el Telegram NO salió
        # --un 502, un límite de tasa, un HTML que Telegram rechaza-- el doctor se quedaba
        # sin enterarse para siempre, porque nadie volvía a mirar esa fila.
        #
        # Y no bastaba con arreglarlo en `runtime._avisar_a_doctores`: ese solo corre cuando
        # el turno CIERRA escalado. Si el modelo llama a esta tool, el envío falla,
        # `failure_error_function` se traga el error y el modelo termina con
        # `requiere_escalamiento=False` --porque cree que ya avisó-- el aviso de cierre no se
        # llama nunca. Medido: cero telegrams, una fila pendiente, y al paciente se le dijo
        # «ya le estoy avisando al doctor». Por eso el reintento vive también aquí, que es
        # donde nace el problema.
        def pendiente(conn) -> int | None:
            return persistencia.escalamiento_pendiente_de_aviso(conn, clave)

        escalamiento_id = await _con_base(ctx, pendiente)

        if escalamiento_id is None:
            # La fila existe Y tiene su `telegram_message_id`: el doctor ya recibió la
            # alerta. Esto sí es un duplicado, y repetirlo es lo que hace que a la cuarta
            # deje de mirarlas.
            return (
                "Este turno ya estaba escalado; no se volvió a avisar. Dile al paciente que "
                "lo estás revisando con los doctores."
            )

        log.warning(
            "el escalamiento %s se había registrado sin llegar a avisar; se reintenta",
            escalamiento_id,
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

    # Y el hilo del paciente, si no lo tiene. Es la ÚNICA puerta por la que algo que no
    # es un archivo abre un hilo, y el no negociable 14 sigue en pie para lo demás: lo
    # que decide no es el texto, es el escalamiento. Un número equivocado no hace
    # escalar a Daniela; el paciente con dolor, sí. Lo que el paciente escribió mientras
    # no tenía hilo se vuelca ahí -- si no, la única huella suya en todo Telegram es
    # este aviso del General, y el doctor que entra a su expediente lo encuentra vacío.
    # Nunca propaga: `rescatar_hilo` se traga lo suyo.
    # Import diferido: `lectura` importa `agentes`, que importa este módulo. Arriba sería
    # un ciclo; aquí no, porque para cuando esta función corre todo está ya cargado.
    from . import lectura, relevo

    tema = await lectura.rescatar_hilo(
        telefono=ctx.telefono_completo,
        nombre_perfil=ctx.nombre_paciente,
        database_url=ctx.database_url,
        telegram=canal,
    )

    # Y la puerta ahí mismo, que es donde el doctor está mirando. El aviso del General es la
    # garantía; este es el que se ve sin salir del expediente. Ver
    # `relevo.ofrecer_la_puerta_en_el_hilo`: baja el motivo y el botón, nunca el resumen.
    if tema:
        await relevo.ofrecer_la_puerta_en_el_hilo(
            telegram=canal,
            tema=tema,
            telefono=ctx.telefono_completo,
            motivo=solicitud.motivo,
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
# 10. consultar_citas -- la única que NO está en el plan
# ==========================================================================================
#
# Las nueve del plan dan por supuesto que el id de la cita viaja en la conversación, y dentro
# de una conversación es cierto. Pero las conversaciones de WhatsApp mueren a las 24 horas
# (`conversacion_viva`), y `reprogramar_cita` y `cancelar_cita` no tienen otra entrada que ese
# UUID: el paciente que agenda el lunes y escribe el miércoles pedía algo que Daniela no tenía
# forma de encontrar. Se salvaba solo si subía en su chat y copiaba el código a mano.
#
# Es de LECTURA y va anclada al teléfono del contexto, así que no puede devolver la cita de
# otra persona ni aunque el modelo lo intente: no hay ningún argumento que torcer.


#: Cuánto hacia atrás se miran las citas antes de contrastarlas con Calendar. No es un
#: capricho: si el doctor arrastra la cita de ayer a mañana, la fila de Neon sigue diciendo
#: «ayer» y el filtro `inicio >= ahora` la dejaría fuera -- justo la cita que hay que
#: corregir. Dos días cubren un fin de semana y no traen medio historial.
DIAS_HACIA_ATRAS_AL_SINCRONIZAR = 2


@dataclass(frozen=True)
class _SoloLaBase:
    """Lo único que `_con_base` le pide a un contexto.

    El núcleo recibe una `database_url` suelta y no un `ContextoDaniela` --el panel no tiene
    conversación, ni teléfono, ni turno--, pero sigue entrando por `_con_base` a nivel de
    módulo en vez de abrir la conexión por su cuenta. No es ceremonia: `_con_base` es el
    punto que las pruebas doblan con `monkeypatch`, y una copia local con otro nombre dejaría
    a media suite intentando conectarse a Neon de verdad. Pasar por aquí es lo que mantiene
    esa red en pie.
    """

    database_url: str


@dataclass(frozen=True)
class Correccion:
    """Lo que Calendar dijo y Neon no sabía, en datos y sin una sola frase.

    Es la mitad que el panel puede usar. Daniela necesita prosa --se la da `_frase_de`--,
    pero una agenda necesita campos: quién, qué, de cuándo a cuándo. Escribir las dos cosas
    en el mismo sitio era lo que ataba la reconciliación a un solo consumidor.

    `hora_nueva` en None es lo que distingue «la movieron» de «ya no está»: una cancelada no
    tiene destino, y pintarle uno sería pintar una cita en un hueco que la clínica ya soltó.
    """

    cita_id: str
    que_paso: Literal["movida", "cancelada"]
    hora_vieja: datetime
    hora_nueva: datetime | None
    tratamiento: str
    nombre_completo: str


async def reconciliar_con_calendar(
    *,
    database_url: str,
    calendario: Any | None,
    citas: list[dict[str, Any]],
    ahora: datetime,
    jornada: Jornada,
    capacidad_por_hora: int,
    duracion_cita_minutos: int,
    hora_recordatorio_vispera: int,
    horas_minimas_para_recordar: int,
) -> tuple[list[dict[str, Any]], list[Correccion]]:
    """Devuelve esas citas como están HOY en Google Calendar, corrigiendo Neon si hace falta.

    ------------------------------------------------------------------------------------
    Por qué existe (14/09/2026)
    ------------------------------------------------------------------------------------

    El calendario de los doctores no es un espejo de Neon: es donde trabajan. Arrastrar una
    cita con el ratón al día siguiente es el gesto natural, y nadie va a abrir el panel
    después para repetirlo. Hasta hoy eso dejaba la fila de `citas` mintiendo, y Daniela le
    repetía al paciente la hora vieja -- con el paciente presentándose cuando ya no le toca.

    Entre los dos, **manda Calendar**. Es donde está el doctor que va a atender.

    ------------------------------------------------------------------------------------
    Las tres respuestas, y lo que hace cada una
    ------------------------------------------------------------------------------------

        sigue igual        no se toca nada
        está en otra hora  se mueve la fila, se suelta el cupo viejo y se toma el nuevo
        ya no está         se cancela la cita y se suelta el cupo

    **Un fallo no es ninguna de las tres.** `ErrorDeCalendario` --Google caído, un timeout,
    un evento sin bordes legibles-- deja la cita tal como está en Neon y sigue con la
    siguiente. Es la misma regla del no negociable 1 mirada desde el otro lado: ante la duda,
    nunca inventar. Tratar un timeout como «la borraron» cancelaría citas buenas en silencio.

    Y el cupo nuevo puede estar lleno. Se mueve la cita igual: el doctor ya decidió meterla
    ahí, en el calendario que él mira, y negarle la realidad a Neon solo consigue que Daniela
    vuelva a mentir. Queda sin reserva --la columna lo admite-- y con un `log.warning`, que es
    lo que un humano puede ver y corregir.

    ------------------------------------------------------------------------------------
    Y lo segundo que devuelve: las CORRECCIONES (14/09/2026, tarde)
    ------------------------------------------------------------------------------------

    Corregir Neon en silencio no bastaba. El paciente borró su evento a mano, la cita se
    canceló bien, y Daniela contestó «no me aparece una cita futura registrada; ya estoy
    confirmando ese punto con el equipo»: escaló. Lo dejó escrito ella misma en el
    escalamiento --«la consulta actual no muestra citas futuras, AUNQUE EN TURNOS PREVIOS
    aparecía una cita de cordales para el 15/09»--, y las marcas de tiempo cierran el caso:
    la cita se canceló a las 18:43:48 y el aviso al doctor salió a las 18:43:53. Cinco
    segundos. **Le preguntó a un humano algo que su propio turno acababa de resolver.**

    Y escalar ahí era lo correcto: quien ve que el sistema se desdice y no sabe por qué,
    llama a alguien. Lo que estaba mal es que no supiera por qué, teniéndolo delante. Así que
    lo que esta función corrige en la base sale también en el texto, con la hora VIEJA
    dentro: sin ella el modelo ve un hueco en vez de una explicación, y no puede atar lo que
    dijo el turno pasado con lo que lee ahora.

    Solo cuando hay algo que contar. Un «tu cita sigue donde estaba» en cada consulta es
    ruido que el modelo acabaría repitiéndole al paciente.

    ------------------------------------------------------------------------------------
    Por qué es esto y no `_sincronizar_con_calendar` (20/09/2026)
    ------------------------------------------------------------------------------------

    Hasta hoy esta función devolvía las frases ya escritas, así que solo le servía a Daniela:
    la agenda del panel necesita los MISMOS hechos y no puede leer prosa. Aquí queda el
    núcleo --datos: `Correccion`-- y encima `_sincronizar_con_calendar`, que los traduce con
    `_frase_de`. Ni el orden de las operaciones ni las claves de idempotencia cambian: son
    las de la corrección que YA ocurrió en Google.

    `duracion_cita_minutos` no lo usa este cuerpo todavía y viaja igual: es lo que la agenda
    necesita para pintar cuánto dura un bloque, y dos firmas para la misma reconciliación
    serían dos sitios donde recordar pasarlo.
    """
    if not citas:
        return citas, []

    base = _SoloLaBase(database_url=database_url)
    correcciones: list[Correccion] = []
    al_dia: list[dict[str, Any]] = []
    for cita in citas:
        evento_id = cita.get("evento_calendar_id")
        if not evento_id or calendario is None:
            # Una cita sin evento no tiene con qué contrastarse. Las hay: se crean así
            # cuando Calendar falla en mitad de un relevo.
            al_dia.append(cita)
            continue

        try:
            evento = await asyncio.to_thread(calendario.obtener_evento, evento_id)
        except ErrorDeCalendario as e:
            log.warning("no se pudo contrastar la cita %s con Calendar: %s", cita["id"], e)
            al_dia.append(cita)
            continue
        except Exception:  # noqa: BLE001 -- nada de esto puede tumbar una consulta de lectura
            log.exception("fallo inesperado contrastando la cita %s", cita["id"])
            al_dia.append(cita)
            continue

        if evento is None:
            log.info("la cita %s ya no está en Calendar: se cancela", cita["id"])
            await _con_base(base, partial(_cancelar_porque_ya_no_esta, cita))
            correcciones.append(
                Correccion(
                    cita_id=cita["id"],
                    que_paso="cancelada",
                    hora_vieja=cita["inicio"],
                    hora_nueva=None,
                    tratamiento=cita["tratamiento"],
                    nombre_completo=cita["nombre_completo"],
                )
            )
            continue

        if evento.inicio == cita["inicio"]:
            al_dia.append(cita)
            continue

        log.info(
            "la cita %s se movió a mano en Calendar: %s -> %s",
            cita["id"],
            cita["inicio"],
            evento.inicio,
        )
        correcciones.append(
            Correccion(
                cita_id=cita["id"],
                que_paso="movida",
                hora_vieja=cita["inicio"],
                hora_nueva=evento.inicio,
                tratamiento=cita["tratamiento"],
                nombre_completo=cita["nombre_completo"],
            )
        )
        # El CUÁNDO se calcula FUERA de la transacción, igual que en `crear_cita` y en
        # `reprogramar_cita`: es una función pura del destino, de `ahora` y de la jornada,
        # y nada de eso depende de que la fila se haya corregido. Dentro de
        # `_mover_porque_la_movieron` quedan solo escrituras, así que un error calculando el
        # momento no puede deshacer una corrección que en Google Calendar YA ocurrió.
        #
        # Y va envuelto porque esto es un camino de LECTURA: el paciente preguntó por sus
        # citas. Que un fallo del recordatorio le devuelva a Daniela un error en vez de sus
        # citas invierte el orden de importancia del proyecto -- la cita corregida vale más
        # que el recordatorio, siempre.
        try:
            cuando_recordar = seguimientos.momento_del_recordatorio(
                inicio_cita=evento.inicio,
                ahora=ahora,
                jornada=jornada,
                hora_vispera=hora_recordatorio_vispera,
                horas_minimas=horas_minimas_para_recordar,
            )
        except Exception:  # noqa: BLE001 -- ver arriba
            log.exception("fallo calculando el recordatorio de la cita %s", cita["id"])
            cuando_recordar = None

        al_dia.append(
            await _con_base(
                base,
                partial(
                    _mover_porque_la_movieron,
                    cita,
                    evento,
                    cuando_recordar,
                    capacidad_por_hora=capacidad_por_hora,
                    ahora=ahora,
                ),
            )
        )

    return al_dia, correcciones


def _frase_de(correccion: Correccion) -> str:
    """La corrección contada como se la cuenta al modelo. Los dos literales, sin tocar.

    Que estén aquí y no en el núcleo es toda la frontera de este refactor: el panel lee
    `Correccion`, Daniela lee esto. Si alguien mueve una coma, cae
    `test_el_texto_de_la_novedad_no_cambia_al_refactorizar`, que existe para eso.
    """
    if correccion.que_paso == "cancelada":
        return (
            f"La clínica eliminó de su calendario la cita de {correccion.tratamiento} que "
            f"estaba para el {_formatear_hora(correccion.hora_vieja)}, así que acaba de quedar "
            "CANCELADA. Es un hecho confirmado, no es un error del sistema y no hay nada "
            "que verificar: díselo al paciente con naturalidad, discúlpate por el cambio "
            "y ofrécele buscar otro horario."
        )
    return (
        f"La clínica movió en su calendario la cita de {correccion.tratamiento}: estaba para "
        f"el {_formatear_hora(correccion.hora_vieja)} y ahora es el "
        f"{_formatear_hora(correccion.hora_nueva)}. Es un hecho confirmado, no es un error del "
        "sistema: si en un mensaje anterior le dijiste la hora vieja, corrígesela."
    )


async def _sincronizar_con_calendar(
    ctx: ContextoDaniela, citas: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Igual que antes para quien llama: las citas al día y las frases para el modelo.

    La reconciliación entera vive ahora en `reconciliar_con_calendar`; esto es la capa de
    presentación de Daniela y nada más. Lo que el paciente acaba oyendo no cambió ni un
    carácter -- ver `_frase_de`.
    """
    al_dia, correcciones = await reconciliar_con_calendar(
        database_url=ctx.database_url,
        calendario=ctx.calendario,
        citas=citas,
        ahora=ctx.ahora,
        jornada=ctx.jornada,
        capacidad_por_hora=ctx.capacidad_por_hora,
        duracion_cita_minutos=ctx.duracion_cita_minutos,
        hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
        horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
    )
    return al_dia, [_frase_de(c) for c in correcciones]


def _cancelar_porque_ya_no_esta(cita: dict[str, Any], conn) -> None:
    persistencia.marcar_cita_cancelada(
        conn, cita["id"], motivo="borrada del calendario por la clínica"
    )
    if cita["reserva_id"]:
        persistencia.liberar_cupo(conn, cita["reserva_id"])


def _mover_porque_la_movieron(
    cita: dict[str, Any],
    evento: EventoDelCalendario,
    cuando_recordar: datetime | None,
    conn,
    *,
    capacidad_por_hora: int,
    ahora: datetime,
) -> dict[str, Any]:
    """Pone la fila donde dice Calendar, con su recordatorio. Devuelve la cita ya corregida.

    Recibe la capacidad y el instante sueltos, y no un `ContextoDaniela`: quien reconcilia
    puede ser el panel, que no tiene conversación ni turno. Son los dos únicos datos del
    contexto que esta escritura necesitaba.

    **La cascada del recordatorio es la misma que hace `reprogramar_cita`, y tiene que estar
    aquí por una razón concreta: G1 caza el BORRADO de una cita, no su movimiento.** Cuando el
    doctor arrastra la cita en su calendario, el recordatorio viejo sigue vivo, apuntando a la
    cita correcta con la hora equivocada: la fila sigue existiendo y su estado sigue siendo
    válido, así que ninguna de las tres primeras guardas del despachador lo descarta. El
    paciente recibía el recordatorio de una hora que ya nadie tiene apartada.

    El orden es el de `reprogramar_cita` y no es negociable: primero se programa el nuevo y
    después se anulan los viejos PERDONANDO el que se acaba de programar. Al revés, un segundo
    paso por aquí anularía su propia fila y chocaría al reinsertarla (`ON CONFLICT DO
    NOTHING`), dejando la cita corregida y SIN ningún recordatorio vivo.

    Todo en UNA transacción, y `liberar_cupo` DESPUÉS del commit: es donde ya estaba. Metido
    dentro, un DELETE que fallara desharía también la corrección de la cita -- que en Google
    Calendar ya ocurrió -- y dejaría a los dos sistemas contando cosas distintas.
    """
    cupo = persistencia.tomar_cupo(
        conn,
        inicio=evento.inicio,
        capacidad=capacidad_por_hora,
        # La arma el código, como las otras cuatro (no negociable 2). Lleva la hora destino
        # dentro, así que sincronizar dos veces la misma cita no toma dos cupos.
        clave_idempotencia=f"calendar:{cita['id']}:{evento.inicio.isoformat()}",
        conversacion_id=cita["conversacion_id"],
    )
    if cupo is None:
        log.warning(
            "la cita %s se movió a %s, que ya está llena: queda registrada SIN cupo",
            cita["id"],
            evento.inicio,
        )
    reserva_nueva = cupo[0] if cupo else None

    # Los mismos tres componentes que la clave de `reprogramar_cita`, y por los mismos motivos:
    # la cita para que la cascada sepa de qué cuelga, la hora destino para que dos horas no
    # compartan fila, y `ahora` --fijo dentro del turno-- para que un A -> B -> A -> B
    # hecho a mano en Calendar no acierte la clave de un movimiento anterior y se quede sin
    # recordatorio. La arma el código, nunca el modelo.
    clave_recordatorio = (
        f"calendar:recordatorio:{cita['id']}:{evento.inicio.isoformat()}:{ahora.isoformat()}"
    )

    persistencia.mover_cita(
        conn,
        cita["id"],
        reserva_id=reserva_nueva,
        inicio=evento.inicio,
        commit=False,
    )
    if cuando_recordar is not None:
        if not persistencia.insertar_seguimiento(
            conn,
            id_conversacion=cita["conversacion_id"],
            tipo="recordatorio_cita",
            fecha_objetivo=cuando_recordar,
            clave_idempotencia=clave_recordatorio,
            cita_id=cita["id"],
            commit=False,
        ):
            # Solo puede ser un segundo paso por aquí dentro del MISMO turno, y la fila sigue
            # viva porque `excepto_clave` la perdona. Benigno, pero descartar el booleano en
            # silencio era lo que impedía verlo.
            log.info(
                "el recordatorio de la cita %s para %s ya estaba programado en este turno",
                cita["id"],
                evento.inicio.isoformat(),
            )
    persistencia.anular_seguimientos_de_cita(
        conn,
        cita["id"],
        motivo="movida_en_calendar",
        excepto_clave=clave_recordatorio,
        commit=False,
    )
    conn.commit()

    # `and ... != reserva_nueva` no es defensa contra nada teórico: `tomar_cupo` devuelve la
    # reserva que YA existía cuando la clave se repite, y liberar esa es borrarle a la cita el
    # cupo que está usando. `citas.reserva_id` es `ON DELETE SET NULL`, así que el DELETE no
    # falla, no lanza y no registra nada: la hora vuelve a contarse libre y la clínica vende
    # una plaza de más. Ver el mismo guardián en `_reprogramar_cita`.
    if cita["reserva_id"] and cita["reserva_id"] != reserva_nueva:
        persistencia.liberar_cupo(conn, cita["reserva_id"])

    corregida = dict(cita)
    corregida["inicio"] = evento.inicio
    corregida["duracion_minutos"] = evento.duracion_minutos
    corregida["reserva_id"] = cupo[0] if cupo else None
    return corregida


async def _consultar_citas(ctx: ContextoDaniela) -> str:
    def buscar(conn) -> list[dict[str, Any]]:
        return persistencia.citas_activas_de_telefono(
            conn,
            ctx.telefono_completo,
            desde=ctx.ahora - timedelta(days=DIAS_HACIA_ATRAS_AL_SINCRONIZAR),
        )

    # Contrastar ANTES de filtrar el pasado, no después: la cita que el doctor arrastró de
    # ayer a mañana está en el pasado según Neon y en el futuro según Calendar, y es
    # exactamente la que el paciente va a preguntar.
    citas, novedades = await _sincronizar_con_calendar(ctx, await _con_base(ctx, buscar))
    citas = [c for c in citas if c["inicio"] >= ctx.ahora]

    # Lo que acaba de cambiar va DELANTE de la lista. Es lo que explica por qué esto no dice
    # lo mismo que el turno anterior, y sin esa explicación el modelo escala (18:43:48 la
    # cancelación, 18:43:53 el aviso al doctor).
    aviso = "\n".join(novedades) + "\n\n" if novedades else ""
    # Y esas horas quedan autorizadas, igual que la vieja al reprogramar y la cancelada al
    # cancelar (no negociable 13). Es el mismo caso exacto: son horas que una tool acaba de
    # leer de la base y que el paciente TIENE que oír para entender qué pasó. Sin esto,
    # «la cita del 15/09 la eliminó la clínica» dispara `sin_hora_no_verificada` y el
    # escalamiento que estamos quitando entra por la otra puerta.
    ctx.turno.horas_autorizadas |= horas_de(aviso)

    if not citas:
        # RESULTADO, no error, y sin una sola hora dentro salvo las del aviso: lo que el
        # modelo lea aquí es lo único que tiene: cualquier hora que escriba después saldría
        # de su memoria.
        return aviso + (
            "Este número no tiene ninguna cita futura registrada. NO afirmes que tiene una "
            "ni menciones ninguna hora. Si quiere agendar, consulta la disponibilidad."
        )

    lineas = "\n".join(
        f"- {_formatear_hora(cita['inicio'])}, {cita['tratamiento']}, a nombre de "
        f"{cita['nombre_completo']}. Id de la cita: {cita['id']}."
        for cita in citas
    )
    texto = f"{aviso}Citas activas de este número:\n{lineas}"
    # Igual que en `reprogramar` y `cancelar`: la hora que una tool acaba de leer de la base
    # queda autorizada. Sin esto, «tu cita es el martes a las 9» dispara
    # `sin_hora_no_verificada` y el paciente que solo preguntaba cuándo era su cita recibe
    # «te escribe el doctor».
    ctx.turno.horas_autorizadas |= horas_de(texto)
    return texto


@function_tool(
    failure_error_function=_fallo_consultar_citas,
    tool_input_guardrails=[identidad_antes_de_datos],
)
async def consultar_citas(wrapper: RunContextWrapper[ContextoDaniela]) -> str:
    """Busca las citas que este paciente ya tiene, sin pedirle que recuerde ningún código.

    Úsala SIEMPRE que quiera mover o cancelar una cita y no tengas el id a la vista en esta
    misma conversación, y también cuando pregunte cuándo es su cita. Devuelve solo las citas
    futuras que siguen en pie, con el id que `reprogramar_cita` y `cancelar_cita` necesitan.
    """
    return await _consultar_citas(wrapper.context)


# ==========================================================================================
# 11. La baja comercial y su revocación
# ==========================================================================================


async def _registrar_no_contactar(ctx: ContextoDaniela, nota: str | None) -> str:
    def trabajo(conn) -> None:
        persistencia.pedir_baja(
            conn, ctx.telefono_completo, origen="paciente", detalle=nota
        )

    await _con_base(ctx, trabajo)
    return (
        "Anotado: a este número no le vuelve a salir nada comercial, ni ahora ni nunca. "
        "Confírmaselo en una línea y sigue con lo que necesite. No le preguntes por qué, no "
        "le ofrezcas alternativas y no intentes retenerlo. Su cita, si tiene una, le sigue "
        "llegando igual: esto no la toca."
    )


@function_tool(failure_error_function=_fallo_privacidad)
async def registrar_no_contactar(
    wrapper: RunContextWrapper[ContextoDaniela],
    nota: str,
) -> str:
    """Anota que el paciente NO quiere recibir más mensajes nuestros.

    Llámala en cuanto lo pida, aunque lo diga de pasada. Si dudas entre si lo pidió o no,
    llámala igual: dejar de escribirle a quien no lo pidió es una molestia, escribirle a
    quien sí lo pidió es faltarle al respeto.

    NO la llames porque el paciente esté molesto, tenga prisa o no conteste. Solo cuando
    pida que no le escriban.

    Args:
        nota: la frase con la que lo pidió, tal cual. Sin interpretarla.
    """
    return await _registrar_no_contactar(wrapper.context, nota or None)


async def _revocar_no_contactar(ctx: ContextoDaniela, nota: str | None) -> str:
    def trabajo(conn) -> bool:
        return persistencia.revocar_baja(
            conn, ctx.telefono_completo, origen="paciente", detalle=nota
        )

    cambio = await _con_base(ctx, trabajo)
    if not cambio:
        # No negociable 1: a este número nadie le había apagado nada, así que "vuelve a
        # recibir mensajes" sería confirmar un cambio que no ocurrió.
        return "Este número no tenía nada desactivado. Sigue con lo que necesite."
    return "Anotado: vuelve a recibir mensajes nuestros. Confírmaselo en una línea."


@function_tool(failure_error_function=_fallo_privacidad)
async def revocar_no_contactar(
    wrapper: RunContextWrapper[ContextoDaniela],
    nota: str,
) -> str:
    """Vuelve a activar los mensajes a un paciente que los había desactivado.

    SOLO si lo pide él. Que vuelva a escribirte no es pedirlo: alguien que se dio de baja y
    meses después pregunta por una muela rota sigue sin querer publicidad.

    Args:
        nota: la frase con la que lo pidió, tal cual.
    """
    return await _revocar_no_contactar(wrapper.context, nota or None)


# ==========================================================================================
# El conjunto -- lo que `agentes.py` importará en la fase 4
# ==========================================================================================

#: Las nueve del plan, en el orden de `herramientas[]`, más `consultar_citas`, y las dos de
#: la baja comercial al final: ninguna de las tres está en el plan y por eso no se cuelan
#: entre las nueve.
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
    consultar_citas,
    registrar_no_contactar,
    revocar_no_contactar,
)

__all__ = [
    "TODAS",
    "CitaNoConfirmada",
    "MAX_INTENTOS_IDENTIFICACION",
    "ZONA_BOGOTA",
    "cancelar_cita",
    "consultar_base_conocimiento",
    "consultar_citas",
    "consultar_disponibilidad",
    "crear_cita",
    "escalar_a_doctores",
    "identificar_paciente",
    "programar_seguimiento",
    "registrar_estado_oportunidad",
    "registrar_no_contactar",
    "reprogramar_cita",
    "revocar_no_contactar",
]

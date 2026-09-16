"""El despachador de recordatorios: el «otro proceso» de la migración 001.

`seguimientos` es una cola desde la fase 3 y hasta hoy nadie la leía: `enviado_en` no se
escribía en ninguna línea del repositorio. Este módulo es quien la lee.

Deliberadamente NO abre conexiones ni habla con Meta por su cuenta: recibe la conexión y los
canales. Es lo que permite probar las siete guardas en milisegundos y sin señal.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from . import persistencia
from .calendario import Jornada

log = logging.getLogger(__name__)

#: Una cita a menos de esto no lleva recordatorio: el paciente acaba de hablar con Daniela.
#: El valor vivo sale de `configuracion`; este es el respaldo.
HORAS_MINIMAS_POR_DEFECTO = 4

#: La hora a la que salen los recordatorios de las citas del día siguiente.
HORA_VISPERA_POR_DEFECTO = 18

#: Cuánta antelación da la banda corta.
HORAS_ANTES_EN_EL_MISMO_DIA = 2


def _vispera_util(
    dia: datetime, jornada: Jornada, hora_preferida: int, *, recortar_al_cierre: bool
) -> datetime | None:
    """El momento de víspera en o antes de `dia` en que la clínica está abierta.

    Si `dia` está abierto, la hora que se usa depende de para qué se está calculando la
    víspera, y por eso `recortar_al_cierre` no tiene valor por defecto:

    - `recortar_al_cierre=False` (la víspera ordinaria, banda de más de 24 h): `hora_preferida`
      es una hora FIJA elegida a propósito --las 6 p. m., cuando la gente está disponible y
      todavía alcanza a avisar esa noche-- y no se recorta al cierre aunque la clínica cierre
      antes. Recortarla aquí dejaría esa hora fija inalcanzable siempre que
      `hora_preferida > cierre`, que es el caso todos los días de la semana con la
      configuración por defecto.
    - `recortar_al_cierre=True` (el adelanto de madrugada): aquí `hora_preferida` ya no es una
      hora que convenga respetar tal cual --es solo el punto de partida-- y lo que se busca es
      la última hora útil del día, que es el cierre si `hora_preferida` cae después.

    Si `dia` está cerrado --el domingo es el caso normal, y la víspera de un lunes SIEMPRE lo
    es-- retrocede día a día, como mucho una semana, y el primer día abierto que encuentra
    devuelve SU cierre, con recorte o sin él: ya no es la víspera natural, es un día antes
    todavía, y cuanto más tarde salga el recordatorio, más cerca queda de la cita. Si en siete
    días no hay un día abierto, la configuración está rota y devolver algo sería inventarse un
    horario.
    """
    candidato = dia
    es_la_vispera_natural = True
    for _ in range(7):
        cierre = jornada.cierre_de(candidato)
        if cierre is not None:
            if not es_la_vispera_natural:
                hora = cierre
            elif recortar_al_cierre:
                hora = min(hora_preferida, cierre)
            else:
                hora = max(hora_preferida, jornada.apertura)
            if hora >= jornada.apertura:
                return candidato.replace(hour=hora, minute=0, second=0, microsecond=0)
        candidato = (candidato - timedelta(days=1)).replace(hour=12)
        es_la_vispera_natural = False
    return None


def momento_del_recordatorio(
    *,
    inicio_cita: datetime,
    ahora: datetime,
    jornada: Jornada,
    hora_vispera: int = HORA_VISPERA_POR_DEFECTO,
    horas_minimas: int = HORAS_MINIMAS_POR_DEFECTO,
) -> datetime | None:
    """Cuándo debe salir el recordatorio de esa cita, o `None` si no lleva.

    Tres bandas, por antelación con que se agendó:

    - menos de `horas_minimas`: ninguno. El paciente acaba de hablar con Daniela y recordarle
      la cita que acaba de agendar la hace ver desmemoriada.
    - hasta 24 h: dos horas antes, que es tiempo de salir de casa.
    - más de 24 h: la víspera a `hora_vispera`. Hora FIJA y no «24 h antes»: con 24 h exactas,
      la cita de las 7 a. m. dispara su recordatorio a las 7 a. m. del día anterior, hora a la
      que mucha gente no mira el teléfono. Con hora fija salen todos juntos y quien quiera
      cambiar la cita alcanza a avisar esa noche, para que la clínica libere el cupo.

    La excepción de las madrugadas: si «dos horas antes» cae antes de la apertura, el
    recordatorio se ADELANTA a la víspera en vez de retrasarse a la apertura. Retrasarlo lo
    dejaría llegando a la hora de la cita, con el paciente ya en la puerta o ya perdido. Es el
    único caso en que un recordatorio se mueve hacia atrás en el tiempo.
    """
    antelacion = inicio_cita - ahora
    if antelacion < timedelta(hours=horas_minimas):
        return None

    if antelacion <= timedelta(hours=24):
        candidato = inicio_cita - timedelta(hours=HORAS_ANTES_EN_EL_MISMO_DIA)
        if candidato.hour < jornada.apertura:
            candidato = _vispera_util(
                inicio_cita - timedelta(days=1),
                jornada,
                hora_vispera,
                recortar_al_cierre=True,
            )
    else:
        candidato = _vispera_util(
            inicio_cita - timedelta(days=1), jornada, hora_vispera, recortar_al_cierre=False
        )

    if candidato is None or candidato <= ahora:
        return None
    return candidato


#: Cuánto se aplaza un recordatorio que pilló al doctor hablando con el paciente.
MINUTOS_DE_ESPERA_POR_RELEVO = 30

#: A partir de cuánto retraso un recordatorio deja de servir y pasa a estorbar.
HORAS_DE_RETRASO_QUE_LO_INVALIDAN = 2

#: Lo mismo, medido contra la CITA en vez de contra `fecha_objetivo`. Por debajo de esto el
#: recordatorio ya no recuerda nada: quien iba a salir de casa ya salió, y a quien se le había
#: olvidado no le da tiempo.
#:
#: Es menor que `HORAS_ANTES_EN_EL_MISMO_DIA` en minutos (120) a propósito, y el margen no es
#: holgura por gusto -- lo fija un caso real cada uno:
#:
#: - La banda corta programa el recordatorio EXACTAMENTE a dos horas de la cita, y el ciclo
#:   recoge la fila siempre unos segundos DESPUÉS de su `fecha_objetivo`. Medir contra los 120
#:   redondos anularía la banda corta entera, todos los días.
#: - G4 aplaza media hora porque «el doctor puede devolver la conversación en diez minutos y el
#:   recordatorio sigue siendo válido». Sin margen, ese aplazamiento se convertiría en silencio
#:   en una anulación, y un recordatorio a hora y media de la cita todavía sirve para salir de
#:   casa. Mandarlo ayuda al paciente a llegar; anularlo no ayuda a nadie.
MINUTOS_MINIMOS_ANTES_DE_LA_CITA = 75

#: Si el paciente escribió hace menos de esto, ya está hablando con Daniela.
MINUTOS_DE_CONTACTO_RECIENTE = 60

#: Los tipos de seguimiento que NO son comerciales, y que por tanto una baja NO apaga.
#:
#: Es una lista BLANCA a propósito. `seguimientos.tipo` es texto libre que escribe el modelo
#: (`programar_seguimiento`), así que con una lista negra de tipos comerciales, cualquier tipo
#: inventado se colaría directo al envío. Con esta, lo que no está aquí se comprueba contra la
#: baja: falla hacia el lado seguro.
#:
#: Acotar `tipo` con un `Literal` y un CHECK es trabajo del sub-proyecto D, y entonces esto se
#: podrá derivar de esa lista en vez de mantenerse a mano.
#:
#: **El portillo que esta lista tiene abierto, dicho y no escondido:** el tipo lo escribe el
#: modelo, así que un `tipo='recordatorio_cita'` salido de `programar_seguimiento` atraviesa
#: G0 sin mirarla aunque el paciente esté de baja. Hoy no sale nada por ahí --esa tool nunca
#: pone `cita_id`, y el despachador anula con `sin_plantilla` todo lo que llegue sin
#: `cita_inicio`--, así que el portillo está abierto pero no da a ninguna parte. Lo que lo
#: abriría de par en par es el sub-proyecto D, que añade plantillas para los tipos sin cita:
#: ese día, cerrar `tipo` deja de ser una mejora y pasa a ser la condición para que esta
#: guarda siga siendo cierta. Mientras tanto lo estrecha `herramientas._programar_seguimiento`,
#: que comprueba `ctx.pidio_no_contacto` contra esta misma lista antes de insertar, y el
#: docstring de la tool, que ya no le ofrece al modelo `'recordatorio_cita'` como ejemplo.
TIPOS_NO_COMERCIALES = frozenset({"recordatorio_cita"})


@dataclass(frozen=True)
class Decision:
    """Qué hacer con una fila de la cola. `hasta` solo tiene valor si la acción es aplazar."""

    accion: Literal["enviar", "anular", "aplazar"]
    motivo: str
    hasta: datetime | None = None


def decidir(
    fila: dict[str, Any],
    *,
    ahora: datetime,
    jornada: Jornada,
    ultimo_mensaje: datetime | None,
    ya_salio_a_ese_numero: bool = False,
    hora_vispera: int = HORA_VISPERA_POR_DEFECTO,
) -> Decision:
    """Las siete guardas, en orden. Es lo que separa un recordatorio de un buzón de spam.

    G0 va antes que las siete, y decide sobre la baja comercial: un tipo que no está en
    `TIPOS_NO_COMERCIALES` se anula si el contacto pidió no ser contactado. Es el orden que
    pidió MaxiCare por escrito: privacidad -> canal -> criterio -> contacto.

    El orden importa: las tres primeras son sobre la cita y se saltan si el seguimiento no
    cuelga de ninguna; las dos siguientes aplazan en vez de anular, porque su motivo deja de
    ser cierto más tarde; la sexta anula y la séptima aplaza al día siguiente.
    """
    # G0. La baja comercial, antes que las siete. Es el orden que pidió MaxiCare por escrito:
    # privacidad -> canal -> criterio -> contacto.
    #
    # El tipo se mira DENTRO de la condición, y no en un `if` anterior que anule por baja sin
    # más: un recordatorio de cita cruza esta guarda sin que la baja le aplique. Pedir que no
    # te manden publicidad no es renunciar a que te avisen de tu propia cita, y si se mezclan,
    # el que pierde es el paciente que SÍ iba a ir.
    if fila.get("tipo") not in TIPOS_NO_COMERCIALES and fila.get("no_contactar"):
        return Decision("anular", "baja_solicitada")

    cita_estado = fila.get("cita_estado")
    cita_inicio = fila.get("cita_inicio")

    if fila.get("cita_id") is not None:
        # G1. Cancelada o movida: el recordatorio habla de algo que ya no existe. `reprogramada`
        # no basta para anular --la cascada ya anuló el viejo y creó otro-- pero `cancelada` sí,
        # y una cita que desapareció de la fila también.
        if cita_estado is None or cita_estado == "cancelada":
            return Decision("anular", "cita_cambio")

        # G2. Recordar una cita que ya pasó no es tarde: es decirle al paciente que el sistema
        # no sabe lo que pasó.
        if cita_inicio is not None and cita_inicio <= ahora:
            return Decision("anular", "cita_pasada")

        # G3. El proceso estuvo caído. Sin esto, arrancarlo tras un fin de semana manda de golpe
        # todos los recordatorios atrasados.
        if ahora - fila["fecha_objetivo"] > timedelta(hours=HORAS_DE_RETRASO_QUE_LO_INVALIDAN):
            return Decision("anular", "llego_tarde")

        # G3 bis. La MISMA pregunta, medida contra algo que un aplazamiento no puede
        # reescribir. `aplazar_seguimiento` sobrescribe `fecha_objetivo`, así que la cuenta de
        # arriba se pone a cero cada vez que G4 o G5 aplazan: una fila de víspera que a las
        # 18:00 pilla al doctor en relevo encadena G4 -> 18:30 -> 19:00 -> G5 -> la mañana
        # siguiente, y a las 08:00 llega FRESCA según esa cuenta, a una hora de la cita. La
        # garantía que la spec §11 le atribuye a G3 --«el barrido lleva horas caído, no hay
        # avalancha»-- solo valía para la caída dura.
        #
        # La hora de la cita es lo único de esta fila que ningún aplazamiento toca, y por eso
        # es la referencia. El motivo va aparte de `llego_tarde`: son dos preguntas distintas y
        # la clínica tiene que poder distinguirlas al preguntar por qué no salió un
        # recordatorio.
        if cita_inicio is not None and cita_inicio - ahora < timedelta(
            minutes=MINUTOS_MINIMOS_ANTES_DE_LA_CITA
        ):
            return Decision("anular", "cita_inminente")

    # G4. Mientras un doctor tiene el relevo, el sistema no se le atraviesa: podría estar
    # acordando otra fecha en ese mismo momento. Aplaza, NO anula.
    if fila.get("tomada_por"):
        return Decision(
            "aplazar",
            "relevo_activo",
            ahora + timedelta(minutes=MINUTOS_DE_ESPERA_POR_RELEVO),
        )

    # G5. Nada a las tres de la mañana. La jornada sale de `configuracion`, no de una constante
    # nueva: duplicarla deja dos horarios que se contradicen.
    cierre = jornada.cierre_de(ahora)
    if cierre is None:
        return Decision("aplazar", "fuera_de_jornada", _proxima_apertura(ahora, jornada))
    # La ventana de envío llega hasta el cierre, o hasta pasada la hora de víspera si esa cae
    # más tarde: el recordatorio de víspera se programa a `hora_vispera` (18:00 por defecto) a
    # propósito -- Ruling 1 -- y una ventana que acabara en el cierre (17:00) lo aplazaría
    # SIEMPRE a la apertura del día siguiente, que es la mañana de la cita. El `+ 1` es
    # deliberado: el barrido corre a `hora_vispera` en punto (más el retardo del búfer), así
    # que esa hora tiene que quedar DENTRO de la ventana -- con `>= hora_vispera` el propio
    # recordatorio de víspera se aplazaría a sí mismo.
    limite = max(cierre, hora_vispera + 1)
    if ahora.hour < jornada.apertura or ahora.hour >= limite:
        return Decision("aplazar", "fuera_de_jornada", _proxima_apertura(ahora, jornada))

    # G6. Si está hablando con Daniela ahora mismo, recordarle la cita que acaba de agendar la
    # hace ver desmemoriada.
    if (
        ultimo_mensaje is not None
        and ahora - ultimo_mensaje < timedelta(minutes=MINUTOS_DE_CONTACTO_RECIENTE)
    ):
        return Decision("anular", "contacto_reciente")

    # G7. Un número recibe UN recordatorio por ventana de envío. La segunda fila se aplaza a la
    # PRÓXIMA APERTURA de la ventana, que con la ventana ya abierta es la mañana siguiente.
    #
    # No agrupa, y decirlo importa: la plantilla que Meta aprueba tiene cuatro huecos y sitio
    # para UNA cita. Meter dos exigiría otra plantilla, que es otra spec. La versión anterior
    # aplazaba un minuto «para que el agrupador lo recoja», y ese agrupador no existe: en el
    # ciclo siguiente la tanda arranca vacía, G7 da `False` y la segunda fila sale sola con
    # sesenta segundos de diferencia sobre la primera. El paciente recibía las dos plantillas
    # que esta guarda existe para evitar, y la clínica las pagaba las dos.
    #
    # De las dos salidas posibles, anular la segunda fila deja a un paciente sin recordatorio
    # de una cita real: clínicamente peor que cualquier alternativa. Así que se aplaza. Para
    # una segunda cita de esa misma semana, la mañana siguiente sigue llegando a tiempo; donde
    # no llegue, G2 o G3 la anulan y la fila registra por qué. La limitación queda escrita en
    # los datos y no escondida en un silencio.
    if ya_salio_a_ese_numero:
        return Decision("aplazar", "uno_por_numero", _proxima_apertura(ahora, jornada))

    return Decision("enviar", "ok")


def _proxima_apertura(ahora: datetime, jornada: Jornada) -> datetime:
    """El próximo momento en que la clínica está abierta, desde `ahora`.

    Avanza día a día como mucho una semana: si en siete días no abre, la configuración está
    rota y devolver un momento cualquiera sería inventarse un horario. En ese caso devuelve
    mañana a la hora de apertura, y la guarda volverá a aplazarlo -- lo que deja el problema
    visible en la tabla en vez de escondido en un bucle.
    """
    cierre_de_hoy = jornada.cierre_de(ahora)
    if cierre_de_hoy is not None and ahora.hour < jornada.apertura:
        return ahora.replace(hour=jornada.apertura, minute=0, second=0, microsecond=0)

    candidato = ahora
    for _ in range(7):
        candidato = (candidato + timedelta(days=1)).replace(
            hour=jornada.apertura, minute=0, second=0, microsecond=0
        )
        if jornada.cierre_de(candidato) is not None:
            return candidato
    return (ahora + timedelta(days=1)).replace(
        hour=jornada.apertura, minute=0, second=0, microsecond=0
    )


def jornada_zona():
    """La zona de Bogotá, importada tarde para no crear un ciclo con `herramientas`."""
    from .herramientas import ZONA_BOGOTA

    return ZONA_BOGOTA


#: Cuántas veces se reintenta un envío DENTRO del mismo ciclo. No vuelve a la cola: la fila ya
#: está marcada.
INTENTOS_DE_ENVIO = 3

#: Entre un intento y el siguiente. Corto a propósito: el ciclo entero tiene sesenta segundos.
SEGUNDOS_ENTRE_INTENTOS = 2.0


#: Los días como los escribe la plantilla. Van aquí y no se importan de `herramientas`: el
#: formateador de allí (`_formatear_hora`) es privado, es de otro módulo, y sobre todo devuelve
#: «jueves 17/9 a las 09:00» -- fecha Y hora en una sola cadena, que es lo que un hueco de esta
#: plantilla NO puede llevar.
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def _parametros_del_recordatorio(fila: dict[str, Any]) -> list[str]:
    """Los cuatro huecos de la plantilla, en el orden en que Meta los aprobó.

    Cambiar este orden no cambia la plantilla: manda otro dato en otro hueco, y el paciente lee
    una hora donde esperaba su nombre.

    Dos cosas que costaron cinco revisiones y que esta función es la única en poder romper,
    porque es la única de la rama cuyo resultado lee un paciente:

    - **La hora se convierte a Bogotá antes de formatearla.** `citas.inicio` es `TIMESTAMPTZ`
      y `persistencia.conectar` es un `psycopg.connect(url)` pelado: nada fija el `TimeZone`
      de la sesión, así que psycopg devuelve el valor normalizado a UTC y no en el huso con el
      que se calculó. Un `%H:%M` sobre eso le decía al paciente «a las 14:00» sobre una cita
      de las 9:00 -- llegaba cinco horas tarde a una clínica donde ya nadie lo esperaba. Se
      convierte UNA vez, arriba, y los dos huecos salen de esa conversión.
    - **Los huecos 2 y 3 son datos distintos**: el 2 es la fecha y el 3 la hora. Un formateador
      que devuelva las dos juntas deja la plantilla diciendo «su cita el jueves 17/9 a las
      09:00 a las 09:00 para Limpieza».
    """
    inicio = fila["cita_inicio"]
    nombre = (fila.get("nombre_completo") or "").split(" ")[0] or "paciente"
    local = inicio.astimezone(jornada_zona()) if inicio else None
    return [
        nombre,
        f"{_DIAS[local.weekday()]} {local.day}/{local.month}" if local else "PENDIENTE",
        f"{local:%H:%M}" if local else "PENDIENTE",
        fila.get("tratamiento") or "su cita",
    ]


async def despachar(
    *,
    database_url: str,
    whatsapp: Any | None,
    jornada: Jornada,
    plantilla: str,
    idioma: str = "es",
    ahora: datetime | None = None,
    limite: int = 50,
) -> dict[str, int]:
    """Un ciclo del despachador. Devuelve el recuento por acción.

    `ahora` entra como parámetro para que una prueba pueda fijarlo: es la misma regla que
    `ctx.ahora` en las tools, y la razón por la que esto se puede probar sin esperar a las seis
    de la tarde.

    `plantilla` vacía apaga el ENVÍO sin apagar la decisión: las guardas corren, las anulaciones
    y los aplazamientos se escriben, y no sale un solo mensaje. Es lo que permite comprobar en
    producción que decide bien antes de arriesgar un WhatsApp.

    `idioma` viaja junto a la plantilla y sale de `configuracion`, no de aquí: Meta rechaza el
    envío entero (error 132001) si el código no coincide EXACTAMENTE con el de la traducción
    registrada, y una plantilla creada como `es_CO` no acepta `es`. El default es el caso
    probable, no una comprobación.

    Todo el ciclo corre sobre UNA sola conexión, y no una por operación: es quien LLAMA -esta
    función- quien libera lo que `seguimientos_por_despachar` deja tomado con `FOR UPDATE OF s
    SKIP LOCKED`. Pero esa liberación NO espera al final del ciclo: las cinco funciones de
    escritura hacen su propio `commit()` sobre esta misma conexión, y el PRIMER `commit` ya
    suelta el candado del LOTE entero -no solo el de la fila que se acaba de escribir-, porque
    el candado es de la transacción y no de la fila. Desde ese instante, cualquier otra
    instancia puede recoger las filas 2..N de este mismo lote en su propio ciclo. Por eso la
    protección real contra el envío duplicado no es el candado -que ya se soltó- sino que
    `marcar_seguimiento_enviado` solo marca si `enviado_en` seguía en NULL: si otra instancia
    ya se la llevó, aquí no se manda ni se cuenta.

    Y abrirla y cerrarla también van por `to_thread`, como todo lo demás. `psycopg.connect`
    contra Neon es un handshake TLS completo y el cierre hace commit o rollback antes de
    soltar el socket: son cientos de milisegundos BLOQUEANTES cada uno, en el mismo bucle de
    eventos que atiende los webhooks de pacientes reales, cada sesenta segundos. Era la única
    llamada a base de esta función que no lo hacía.
    """
    momento_actual = ahora or datetime.now(jornada_zona())
    recuento = {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 0}
    numeros_de_esta_tanda: set[str] = set()

    conn = await asyncio.to_thread(persistencia.conectar, database_url)
    try:
        configuracion = await asyncio.to_thread(persistencia.leer_configuracion, conn)
        # G5 calcula su ventana de envío hasta `max(cierre, hora_vispera + 1)` (ver `decidir`).
        # Esa hora la cambia la clínica desde el panel sin desplegar nada, así que pasarle la
        # constante de respaldo en vez de leerla dejaría la ventana calculada contra un valor
        # que ya no es el vigente.
        hora_vispera = configuracion.get("hora_recordatorio_vispera", HORA_VISPERA_POR_DEFECTO)

        filas = await asyncio.to_thread(
            persistencia.seguimientos_por_despachar, conn, ahora=momento_actual, limite=limite
        )

        for fila in filas:
            telefono = fila.get("telefono") or ""

            ultimo = await asyncio.to_thread(
                persistencia.ultimo_mensaje_del_paciente, conn, telefono
            )

            decision = decidir(
                fila,
                ahora=momento_actual,
                jornada=jornada,
                ultimo_mensaje=ultimo,
                ya_salio_a_ese_numero=telefono in numeros_de_esta_tanda,
                hora_vispera=hora_vispera,
            )

            if decision.accion == "anular":
                await asyncio.to_thread(
                    persistencia.anular_seguimiento, conn, fila["id"], motivo=decision.motivo
                )
                recuento["anulados"] += 1
                continue

            if decision.accion == "aplazar":
                await asyncio.to_thread(
                    persistencia.aplazar_seguimiento,
                    conn,
                    fila["id"],
                    hasta=decision.hasta or momento_actual,
                )
                recuento["aplazados"] += 1
                continue

            # A partir de aquí `decision.accion == "enviar"`. Un seguimiento sin cita (p. ej.
            # una reactivación) llega hasta aquí porque G1-G3 se saltan sin cita que mirar (ver
            # `decidir`), pero la plantilla que manda este despachador es LA DE RECORDATORIO DE
            # CITA: sin fecha ni hora que meter en sus huecos, mandarla dejaría al paciente
            # leyendo el literal "PENDIENTE" por WhatsApp -la regla dura 3 es para el código,
            # nunca fue permiso para mandarle el marcador a un paciente-. No es una regla de
            # negocio que decida no avisarle: es que HOY no existe una plantilla para este tipo
            # de seguimiento. Se anula -no se pierde, queda visible en la tabla con su motivo-
            # hasta que exista una.
            if fila.get("cita_inicio") is None:
                await asyncio.to_thread(
                    persistencia.anular_seguimiento, conn, fila["id"], motivo="sin_plantilla"
                )
                recuento["anulados"] += 1
                continue

            if telefono:
                # G7 se apoya en que este número YA tiene (o está a punto de tener) un
                # recordatorio en esta tanda. Se anota aquí, antes de mirar si hay plantilla o
                # canal, para que el modo "decide y no manda" (`plantilla == ""`) agrupe igual
                # que agruparía con el canal encendido: si se anotara solo tras un envío que
                # salió bien, dos citas del mismo número decidirían las dos "enviar" con la
                # plantilla apagada, que no es la decisión que se tomaría con la plantilla
                # puesta.
                numeros_de_esta_tanda.add(telefono)

            if not plantilla or whatsapp is None or not telefono:
                log.info(
                    "seguimiento %s: decidido ENVIAR y no se manda (plantilla o canal sin "
                    "configurar). El despachador decide, el canal está apagado, y la fila "
                    "SIGUE pendiente -no se marca- para cuando exista la plantilla.",
                    fila["id"],
                )
                continue

            # MARCAR PRIMERO. Ver `persistencia.marcar_seguimiento_enviado`: no hay transacción
            # que cubra una llamada a Meta, y mandar dos veces es peor que perder uno. El
            # `bool` que devuelve es la protección real contra el duplicado -ver el docstring
            # de esta función-: si ya la marcó otra instancia, aquí no se manda ni se cuenta.
            marcada = await asyncio.to_thread(
                persistencia.marcar_seguimiento_enviado, conn, fila["id"]
            )
            if not marcada:
                log.info(
                    "seguimiento %s: otra instancia ya la marcó enviada entre la lectura y "
                    "este punto; no se manda ni se cuenta aquí.",
                    fila["id"],
                )
                continue

            fallo: str | None = None
            for intento in range(INTENTOS_DE_ENVIO):
                try:
                    await whatsapp.enviar_plantilla(
                        telefono,
                        plantilla=plantilla,
                        parametros=_parametros_del_recordatorio(fila),
                        idioma=idioma,
                    )
                    fallo = None
                    break
                except Exception as e:  # noqa: BLE001 -- el ciclo tiene que seguir con los demás
                    fallo = f"{type(e).__name__}: {e}"
                    if intento + 1 < INTENTOS_DE_ENVIO:
                        await asyncio.sleep(SEGUNDOS_ENTRE_INTENTOS)

            if fallo is not None:
                await asyncio.to_thread(
                    persistencia.anotar_fallo_de_seguimiento, conn, fila["id"], fallo=fallo
                )
                recuento["fallidos"] += 1
                log.error(
                    "seguimiento %s no salió tras %d intentos: %s",
                    fila["id"],
                    INTENTOS_DE_ENVIO,
                    fallo,
                )
                continue

            await asyncio.to_thread(
                persistencia.anotar_recordatorio_en_conversacion,
                conn,
                fila["conversacion_id"],
                tipo=fila["tipo"],
                cuando=momento_actual,
            )
            recuento["enviados"] += 1
    finally:
        await asyncio.to_thread(_cerrar, conn)

    return recuento


def _cerrar(conn: Any) -> None:
    """Cierra la conexión del ciclo, y un fallo cerrándola no se lleva por delante el recuento.

    `close()` deshace lo que quedara sin confirmar, y eso es justo lo correcto: las cinco
    funciones de escritura confirman por su cuenta, así que lo único que puede quedar abierto
    es el `SELECT ... FOR UPDATE` de la lectura -- cuyo candado, precisamente, hay que soltar.
    """
    try:
        conn.close()
    except Exception:  # noqa: BLE001 -- el ciclo ya hizo su trabajo; esto es limpieza
        log.warning("no se pudo cerrar la conexión del ciclo de recordatorios", exc_info=True)

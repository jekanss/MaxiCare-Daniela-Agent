"""El despachador de recordatorios: el «otro proceso» de la migración 001.

`seguimientos` es una cola desde la fase 3 y hasta hoy nadie la leía: `enviado_en` no se
escribía en ninguna línea del repositorio. Este módulo es quien la lee.

Deliberadamente NO abre conexiones ni habla con Meta por su cuenta: recibe la conexión y los
canales. Es lo que permite probar todas sus guardas en milisegundos y sin señal.
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

#: Cuánto se aplaza una reactivación mientras algo la tiene FRENADA -- el interruptor de
#: pánico, la calidad del número, o Daniela apagada. Ver la guarda `freno` en `decidir`.
#:
#: Una hora y no un minuto: las tres señales que pueden levantar el freno se refrescan con el
#: reloj del BARRIDO (`runtime.SEGUNDOS_ENTRE_BARRIDOS_DE_REACTIVACION`, horario), así que
#: volver a preguntar a los sesenta segundos no puede dar una respuesta distinta y lo único
#: que consigue es reescribir `fecha_objetivo` mil veces al día.
MINUTOS_DE_ESPERA_POR_FRENO_DE_REACTIVACION = 60

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

#: La ventana de contacto reciente para una REACTIVACION. Mucho mas ancha que los 60 min del
#: recordatorio a proposito: un recordatorio de cita le sirve a quien escribio hace tres horas,
#: y un «hace unos dias nos escribio» a esa misma persona es lo que hace que conteste «??».
HORAS_DE_CONTACTO_RECIENTE_COMERCIAL = 24

#: Horario propio de la reactivacion, y NO la jornada de la clinica. Si MaxiCare abriera los
#: domingos, colgar de la jornada dejaria salir publicidad en domingo. Un recordatorio de cita
#: en domingo esta bien --la cita es real--; un «sigue interesada?» no.
HORA_APERTURA_COMERCIAL = 9
HORA_CIERRE_COMERCIAL = 19

#: Defaults de las perillas de la 021. Los vivos salen de `configuracion`.
MAX_REACTIVACIONES_12M = 6
MAX_SEGUIMIENTOS_FALLIDOS = 2

#: Techo de espera de una reactivacion que YA se aplazo al menos una vez, medido desde
#: `aplazado_desde` -- la primera vez que no pudo salir, no desde que se creo. Es el mismo
#: problema que motiva G3 bis, dos secciones más abajo, aplicado a lo que no tiene cita: ver
#: R3bis para el caso medido.
#:
#: **Lo que este umbral cubre: el relevo sostenido.** G4 aplaza 30 min cada vez que
#: `tomada_por` sigue puesto, así que un relevo que no se cierra encadena aplazamientos
#: indefinidamente y `aplazado_desde` -fijo desde el primero- es lo único que mide cuánto
#: lleva la fila sin poder salir de verdad. Un relevo sostenido dos días es una operación
#: real de la clínica y no debe anularse; uno sostenido varios días ya no es un caso que
#: convenga dejar vivo indefinidamente. 96 h (4 dias) da margen de sobra al primero sin abrir
#: la puerta al segundo.
#:
#: **Lo que este umbral NO cubre, y no tiene por qué:** una fila que nunca se aplazó -incluida
#: una programada a dos semanas vista, o la de 7 días de la tarea 6- tiene `aplazado_desde` en
#: NULL y ni siquiera entra en esta guarda (ver el `is not None` de más abajo). Esa fila puede
#: ser tan "vieja" como se quiera contra el calendario: lo que importa aquí es si ALGUNA VEZ
#: quedó atascada, no cuánto falta o cuánto pasó desde que se creó. La primera versión de esta
#: guarda medía contra `creado_en` -edad total de la fila- y por eso anulaba en silencio una
#: reactivación de dos semanas que llegaba puntual, con un motivo que decía justo lo
#: contrario de lo que había pasado.
HORAS_DE_ESPERA_QUE_INVALIDAN_UN_APLAZAMIENTO_SOSTENIDO = 96

#: Los tipos de seguimiento que NO son comerciales, y que por tanto una baja NO apaga.
#:
#: Es una lista BLANCA a propósito, y del conjunto EXENTO, no del protegido: falla hacia el
#: lado seguro. La 021 ya cerró el vocabulario que el MODELO puede pedir (`Literal` +
#: `TIPOS_QUE_EL_MODELO_PUEDE_PEDIR`), pero su CHECK es NOT VALID -- no revisa lo que ya
#: estaba en `public` cuando `tipo` todavía era texto libre-- así que una fila vieja con un
#: `tipo` que nadie reconoce sigue siendo alcanzable. Con una lista negra de tipos comerciales,
#: esa fila se colaría directo al envío. Con esta, lo que no está aquí se comprueba contra la
#: baja: un tipo desconocido se trata como comercial, no al revés.
#:
#: `es_reactivacion`, en `decidir`, usa la MISMA polaridad y por la MISMA razón: la primera
#: version usaba `tipo in TIPOS_DE_REACTIVACION` -lista blanca del conjunto GUARDADO, falla
#: ABIERTO- y una fila con un tipo fuera de las tres constantes conocidas atravesaba las cinco
#: guardas de reactivación sin que ninguna se evaluara. Medido por el revisor contra el codigo:
#: `tipo='reactivacion'` (invalido), dos días tarde, freno y tope ya disparados, domingo con la
#: clínica abierta -> `Decision(accion='enviar')`, con las cinco guardas sin evaluar.
#:
#: **El portillo que esta lista tiene abierto, dicho y no escondido:** un `tipo='recordatorio_
#: cita'` salido de `programar_seguimiento` atraviesa G0 sin mirarla aunque el paciente esté de
#: baja. Hoy no sale nada por ahí --esa tool nunca pone `cita_id`, y el despachador anula con
#: `sin_cita` (antes `sin_plantilla`) todo `TIPO_RECORDATORIO` que llegue sin `cita_inicio`--,
#: así que el portillo está abierto pero no da a ninguna parte. Lo estrecha
#: `herramientas._programar_seguimiento`, que comprueba
#: `ctx.pidio_no_contacto` contra esta misma lista antes de insertar, y el docstring de la
#: tool, que no le ofrece al modelo `'recordatorio_cita'` como ejemplo.
TIPOS_NO_COMERCIALES = frozenset({"recordatorio_cita"})

#: Los cuatro tipos que existen. El CHECK `ck_seguimientos_tipo` de la migracion 021 tiene la
#: MISMA lista: son dos caras de un vocabulario, y `test_el_vocabulario_del_codigo_y_el_de_la_
#: migracion_no_se_separan` las mantiene juntas. Sin el cierre, `tipo` es TEXT que escribe el
#: modelo, y un `recordatorio_cita` inventado atraviesa G0 con el paciente de baja.
TIPO_RECORDATORIO = "recordatorio_cita"
TIPO_SIN_AGENDAR = "reactivacion_sin_agendar"
TIPO_CANCELADA = "reactivacion_cancelada"

#: Declarado y SIN disparador: nadie escribe `citas.asistio` hasta que cierre la fase 8. Existe
#: aqui para que encenderlo sea cambiar una constante y no volver a tocar la base.
TIPO_NO_ASISTIO = "reactivacion_no_asistio"

TIPOS_DE_REACTIVACION = frozenset({TIPO_SIN_AGENDAR, TIPO_CANCELADA, TIPO_NO_ASISTIO})

#: Cómo se llama cada tipo cuando lo tiene que leer una PERSONA, no el código.
#:
#: Lo usa `panel.hilo` para la voz del sistema: el doctor abre la conversación y ve al
#: paciente contestando «Sí, me interesa» a un mensaje que no está en ninguna de las tres
#: voces --lo mandó el despachador, no el SDK ni el webhook--, así que hasta el 23/09/2026
#: esa respuesta colgaba de la nada. Es la tercera cara del mismo agujero que ya se tapó dos
#: veces: para Daniela con `persistencia.ultimo_recordatorio`, y para el hilo de Telegram con
#: el volcado del no negociable 29.
#:
#: **Describe el MOTIVO por el que salió, nunca el texto que decía**, y esa es la línea que
#: no se puede cruzar. El cuerpo vive en una plantilla de Meta que este repositorio no
#: guarda y que MaxiCare edita desde otra consola: escribir aquí una aproximación pondría en
#: el hilo, con aspecto de transcripción, una frase que quizá no fue la que leyó el paciente.
#: Regla 3 de CLAUDE.md -- lo que no se sabe no se rellena con algo plausible.
#:
#: Un tipo que no esté aquí sale con su clave cruda en vez de reventar: perder el rótulo
#: bonito de un seguimiento nuevo es barato, y tumbar la pantalla del hilo por no tenerlo
#: es lo contrario de lo que este dict vino a arreglar.
ETIQUETA_DEL_TIPO = {
    TIPO_RECORDATORIO: "Recordatorio de su cita",
    TIPO_SIN_AGENDAR: "Reactivación · preguntó y no agendó",
    TIPO_CANCELADA: "Reactivación · canceló su cita",
    TIPO_NO_ASISTIO: "Reactivación · no asistió",
}
TIPOS_DE_SEGUIMIENTO = TIPOS_DE_REACTIVACION | {TIPO_RECORDATORIO}

#: Los que el BARRIDO puede encolar hoy. `TIPO_NO_ASISTIO` no esta: sin `citas.asistio` no hay
#: forma de saber quien no vino, y encolarlo mandaria «no pudo asistir» a quien si fue.
TIPOS_QUE_EL_BARRIDO_ENCOLA = frozenset({TIPO_SIN_AGENDAR, TIPO_CANCELADA})

#: Los que el MODELO puede pedir por `programar_seguimiento`. Hoy tiene los MISMOS dos valores
#: que `TIPOS_QUE_EL_BARRIDO_ENCOLA`, y es a propósito que sean dos constantes y no una: son
#: dos actores distintos -- el barrido de la fase 8 y la tool que llama el modelo en medio de
#: una conversación -- y coincidir hoy no significa que tengan que moverse juntos mañana. El
#: día que la fase 8 escriba `citas.asistio` y alguien añada `TIPO_NO_ASISTIO` al barrido para
#: encenderlo, tocar solo esa lista no le abre al modelo la puerta de pedir «no pudo asistir»
#: sobre alguien que sí fue -- que es exactamente un reporte. Con una sola constante para los
#: dos, esa apertura pasaría en silencio y ninguna prueba lo notaría.
TIPOS_QUE_EL_MODELO_PUEDE_PEDIR = frozenset({TIPO_SIN_AGENDAR, TIPO_CANCELADA})

#: Los tipos cuya plantilla Meta aprobó SIN una sola variable. `parametros_de` les manda la
#: lista vacía y `canales.enviar_plantilla` omite el componente entero: un `body` con
#: `parameters: []` sobre una plantilla estática es un 132000.
#:
#: **Por qué `reactivacion_sin_agendar` perdió su hueco el 23/09/2026.** Era el nombre de
#: pila, y esa plantilla es la única que le escribe a quien NUNCA agendó: sin cita y sin
#: ficha, la cascada de `_nombre_de_reactivacion` solo podía caer al tercer eslabón, el
#: nombre de perfil de WhatsApp, que es como la persona se llama a sí misma en la app y no
#: como se llama. Medido sobre los 17 números de la base: 3 tenían ficha, 8 un perfil
#: presentable y **6 uno que no se puede saludar** -- `jg390485`, `rodolfopatino015`,
#: `centro`, `canomoraleswilmerandres`, `yualetxis` y `Jean♣️`. Dos de esos ya habían salido:
#: «Hola jg390485» y «Hola Pshico» le llegaron a gente real. Un saludo así es la firma de un
#: envío automatizado, y el desempate de este proyecto es que nadie reporte el número.
#:
#: **Las otras dos NO están aquí, y la diferencia no es la plantilla sino de dónde sale el
#: nombre**: `reactivacion_cancelada` y `reactivacion_no_asistio` le escriben a quien ya
#: agendó alguna vez, así que tiene ficha en `pacientes` con el nombre que dio para su cita
#: --lo escribe `crear_cita`-- y ahí el hueco funciona. Los tres números con ficha de la
#: medición lo confirman: Hancer, Laura, Diego. El recordatorio de cita conserva sus cuatro.
#:
#: **Lista blanca estrecha, y por el mismo motivo que `TIPOS_NO_COMERCIALES` es del conjunto
#: exento**: solo el literal exacto compra la lista vacía. Un `tipo` que no esté aquí sigue
#: yendo por el camino de siempre, que es el que tenía plantilla con hueco.
#:
#: `Jean♣️` enseñó de propina que `_primer_nombre_usable` solo caza el emoji cuando va
#: SEPARADO («🌸 Ana» -> token «🌸»): pegado al nombre viaja entero a Meta. Sigue abierto para
#: las dos plantillas que conservan el hueco.
TIPOS_SIN_HUECOS = frozenset({TIPO_SIN_AGENDAR})

#: Las columnas que R1, R2 y R3bis leen de la fila, y sin las cuales esas tres guardas se
#: apagan SOLAS (revisión final, H6 bis).
#:
#: Las tres se leían con `.get()` y respaldo permisivo -- `0`, `0` y `None`--, así que perder
#: una columna del SELECT de `persistencia.seguimientos_por_despachar` no era un error: era
#: una guarda menos, en silencio y con `pytest -q` entero en verde. Ejecutado contra el código
#: de la rama: la MISMA fila, con el contador reventado (5 sobre un tope de 2), el tope anual
#: reventado (99) y 500 horas atascada, pasaba de `anular/seguimiento_apagado` a `enviar/ok`
#: por no traer estas tres claves. Es, casi palabra por palabra, el primero de los cuatro
#: fallos graves de esta rama -- una guarda que falla ABIERTO-- alcanzable otra vez por una
#: puerta más corta.
#:
#: **`in` y no `is not None`**: `aplazado_desde` en NULL es el estado normal de una fila que
#: nunca se aplazó, así que lo que hay que distinguir es «la columna no vino» de «la columna
#: vino vacía». Un `dict` sabe esa diferencia; un `.get()` no.
COLUMNAS_DE_LAS_GUARDAS_DE_REACTIVACION = (
    "seguimientos_fallidos",
    "reactivaciones_ultimo_ano",
    "aplazado_desde",
)


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
    max_reactivaciones_12m: int = MAX_REACTIVACIONES_12M,
    max_seguimientos_fallidos: int = MAX_SEGUIMIENTOS_FALLIDOS,
    freno_de_reactivacion: str | None = None,
) -> Decision:
    """Las guardas del despachador, en orden. Es lo que separa un recordatorio de un buzón
    de spam.

    G0 va antes que las demás, y decide sobre la baja comercial: un tipo que no está en
    `TIPOS_NO_COMERCIALES` se anula si el contacto pidió no ser contactado. Es el orden que
    pidió MaxiCare por escrito: privacidad -> canal -> criterio -> contacto.

    Las guardas de reactivación (R1-R5, con R3bis colgando de R3 y el FRENO entre R3bis y R4)
    van justo después de G0 y antes del bloque de la cita: todas son exclusivas de un
    seguimiento SIN cita (`es_reactivacion`), y un recordatorio de cita las atraviesa sin
    evaluarlas -- por diseño, no por descuido: el apagado y el tope son frenos COMERCIALES y
    una cita real no es publicidad.

    `freno_de_reactivacion` entra como parámetro y NO se lee de `config`: este módulo no
    importa configuración ni habla con la red, igual que no habla con la base. Quien lo calcula
    es `runtime`, que es quien conoce las tres señales (el interruptor de pánico, la calidad
    del número ante Meta y el interruptor de Daniela). Ver la guarda FRENO.

    Cuentas, para quien lea una paráfrasis de esto en otro sitio: un `recordatorio_cita` pasa
    por NUEVE guardas (G0, G1, G2, G3, G3bis, G4, G5, G6, G7) y una reactivación por TRECE
    (G0, R0, R1, R2, R3, R3bis, FRENO, R4, R5, G4, G5, G6, G7 -- las cuatro del bloque de la
    cita se saltan porque no tiene `cita_id`).

    El orden del resto importa: las tres primeras del bloque de cita son sobre la cita y se
    saltan si el seguimiento no cuelga de ninguna; las dos siguientes aplazan en vez de
    anular, porque su motivo deja de ser cierto más tarde; la sexta anula y la séptima
    aplaza al día siguiente.
    """
    # G0. La baja comercial, antes que las demás. Es el orden que pidió MaxiCare por escrito:
    # privacidad -> canal -> criterio -> contacto.
    #
    # El tipo se mira DENTRO de la condición, y no en un `if` anterior que anule por baja sin
    # más: un recordatorio de cita cruza esta guarda sin que la baja le aplique. Pedir que no
    # te manden publicidad no es renunciar a que te avisen de tu propia cita, y si se mezclan,
    # el que pierde es el paciente que SÍ iba a ir.
    if fila.get("tipo") not in TIPOS_NO_COMERCIALES and fila.get("no_contactar"):
        return Decision("anular", "baja_solicitada")

    # `not in TIPOS_NO_COMERCIALES`, y NO `tipo in TIPOS_DE_REACTIVACION`. Es la MISMA
    # polaridad que G0, dos líneas arriba, y por la misma razón (ver el docstring de
    # `TIPOS_NO_COMERCIALES`): lista blanca del conjunto EXENTO, que falla hacia el lado
    # seguro. La primera versión hacía lista blanca del conjunto GUARDADO -falla ABIERTO- y
    # una fila con un `tipo` fuera de las tres constantes conocidas (alcanzable: el CHECK de
    # la 021 es NOT VALID y no revisa lo que ya estaba en `public`) atravesaba las cinco
    # guardas de reactivación sin que ninguna se evaluara.
    es_reactivacion = fila.get("tipo") not in TIPOS_NO_COMERCIALES

    # R0. La fila trae con qué evaluar R1, R2 y R3bis, o no se manda (revisión final, H6 bis).
    #
    # Va ANTES de las tres guardas que dependen de esas columnas, porque si no la primera que
    # se evalúa ya lo hace a ciegas. Anula en vez de aplazar: una columna que falta no aparece
    # sola con el paso del tiempo -- es un SELECT que cambió-- y aplazar dejaría la fila dando
    # vueltas cada sesenta segundos sin dejar rastro de por qué. Anulada queda el motivo
    # escrito en la tabla, que es la primera pregunta que hace la clínica, y el barrido puede
    # volver a encolar a esa persona cuando el SELECT esté arreglado.
    #
    # Solo para reactivaciones: un `recordatorio_cita` no evalúa R1-R5 y no necesita ninguna
    # de estas columnas. Es la misma frontera de siempre.
    if es_reactivacion:
        faltan = [c for c in COLUMNAS_DE_LAS_GUARDAS_DE_REACTIVACION if c not in fila]
        if faltan:
            return Decision("anular", f"fila_incompleta:{','.join(faltan)}")

    # R1. El freno por persona. Antes que nada de lo demas: si esta apagado, no importa la hora
    # ni el retraso. El apagado es del SISTEMA --«a este numero no le sirve que lo
    # persigamos»-- y no es la baja, que es de la persona y ya la mira G0.
    if es_reactivacion and fila.get("seguimientos_fallidos", 0) >= max_seguimientos_fallidos:
        return Decision("anular", "seguimiento_apagado")

    # R2. El tope por persona y ano (regla 8). No es redundante con R1: el contador vuelve a 0
    # al agendar, asi que quien agenda cada vez lo esquiva siempre. Este es el techo.
    if es_reactivacion and fila.get("reactivaciones_ultimo_ano", 0) >= max_reactivaciones_12m:
        return Decision("anular", "tope_anual")

    # R3. El gemelo de G3 para lo que no tiene cita. G3 vive dentro de `if cita_id is not None`
    # y la reactivacion la atraviesa sin evaluarse: un proceso caido el viernes soltaria el
    # lunes todos los mensajes atrasados de golpe, «hace unos dias» sobre algo de hace una
    # semana. Un pico de mensajes viejos es lo que Meta castiga y lo que hace que la gente
    # reporte. Se anula y no se aplaza: el momento oportuno ya paso, y el barrido lo volvera a
    # encolar si la persona sigue calificando.
    if es_reactivacion and ahora - fila["fecha_objetivo"] > timedelta(
        hours=HORAS_DE_RETRASO_QUE_LO_INVALIDAN
    ):
        return Decision("anular", "llego_tarde")

    # R3bis. El gemelo de G3 bis (más abajo), para lo que no tiene cita. `aplazar_seguimiento`
    # reescribe `fecha_objetivo` en CADA aplazamiento (G4, G5, R4), así que R3 se pone a cero
    # cada vez -- la reactivación no tenía el ancla equivalente a G3 bis porque no tiene
    # `cita_inicio`. Medido a mano: creada el viernes a las 18:30 con el relevo puesto,
    # encadena aplazar (`relevo_activo`, 30 min) -> aplazar (`fuera_de_horario_comercial`) y
    # sale el sábado a las 09:00 con 14,5 h de deriva real que R3 lee como CERO. Con un relevo
    # sostenido varios días la cuenta de R3 sigue en cero indefinidamente: la garantía de R3
    # («el barrido lleva horas caído, no hay avalancha») solo vale para la caída dura.
    #
    # El ancla es `aplazado_desde` (migración 022), NO `creado_en`. La primera versión de esta
    # guarda usaba `creado_en` y medía la EDAD TOTAL de la fila, no cuánto lleva atascada: una
    # reactivación programada a dos semanas vista (`programar_seguimiento` no le pone cota
    # superior a `fecha_objetivo`) es "vieja" desde que se crea y llegaba puntual, y esa
    # versión la anulaba en silencio con un motivo que decía justo lo contrario de lo que
    # había pasado -ejecutado y cazado en la ronda 2 de revisión-. `aplazado_desde` es NULL
    # mientras la fila nunca se ha aplazado -exactamente esos dos casos- y solo se fija la
    # PRIMERA vez que algo la frena (`aplazar_seguimiento`, con `COALESCE`), así que mide lo
    # que la guarda necesita: cuánto lleva sin poder salir, no cuánto lleva existiendo.
    #
    # Motivo propio y no `llego_tarde`: la clínica tiene que poder distinguir «llegó tarde una
    # vez» de «lleva días dando vueltas», igual que `llego_tarde` y `cita_inminente` van
    # separados para el recordatorio de cita.
    aplazado_desde = fila.get("aplazado_desde")
    if (
        es_reactivacion
        and aplazado_desde is not None
        and ahora - aplazado_desde
        > timedelta(hours=HORAS_DE_ESPERA_QUE_INVALIDAN_UN_APLAZAMIENTO_SOSTENIDO)
    ):
        return Decision("anular", "reactivacion_estancada")

    # FRENO. El interruptor de pánico (regla 10), el freno por calidad (regla 11) y el
    # interruptor de Daniela, aplicados donde de verdad se manda y no solo donde se encola.
    #
    # **El agujero que cierra** (revisión final, H2 y H3): los tres frenos vivían SOLO en
    # `barrido.encolar`, que corre una vez por hora. `despachar` corre cada sesenta segundos y
    # no miraba ninguno, así que quien accionaba el freno de emergencia veía salir en el minuto
    # siguiente todo lo que ya estaba en la cola -- hasta el tope diario, más lo aplazado de
    # días anteriores. Y con `MAXICARE_DANIELA_RESPONDE=0` era peor que un mensaje de más:
    # salía un «¿sigue interesada?» que pedía respuesta y quien pulsaba «Sí, me interesa» no
    # recibía NADA, porque `atencion.procesar_mensaje` corta en esa misma bandera. Pedir
    # respuesta y callarse es el disparador de reporte más limpio que existe, y si quien vuelve
    # escribe «me duele», cruza la frontera clínica.
    #
    # **APLAZA, nunca anula y nunca marca.** El freno es transitorio por definición -- la
    # calidad sube, el operador vuelve a encender-- y anular perdería filas que sí calificaban
    # por un motivo que dejará de ser cierto. Marcar sería peor todavía: `marcar_seguimiento_
    # enviado` va ANTES del envío (no negociable 21), así que marcar sin mandar pierde la fila
    # para siempre.
    #
    # **Va DESPUÉS de R1-R3bis y no antes**, y es deliberado: esas cuatro anulan por razones
    # que siguen siendo ciertas con el freno puesto (el contador, el tope anual, el retraso, el
    # atasco), y dejarlas correr es lo que impide que un freno largo acumule un backlog que
    # salga de golpe el día que se levante -- que es justo el pico que R3 existe para evitar.
    # Con R3bis viva, un freno de más de 96 h va matando las filas en vez de apilarlas.
    #
    # **`es_reactivacion` y no `tipo in TIPOS_DE_REACTIVACION`**: misma polaridad que G0 y
    # R1-R5, y por la misma razón -- un `tipo` desconocido cuenta como comercial y se frena.
    # `recordatorio_cita` es lo único que sigue saliendo con el freno puesto, por los tres
    # caminos: ahí está exactamente la frontera del no negociable 25 (la baja es comercial y
    # no apaga el recordatorio de una cita).
    if es_reactivacion and freno_de_reactivacion:
        return Decision(
            "aplazar",
            f"frenada:{freno_de_reactivacion}",
            ahora + timedelta(minutes=MINUTOS_DE_ESPERA_POR_FRENO_DE_REACTIVACION),
        )

    # R4. Horario propio (regla 6). NO cuelga de `jornada`: ver el comentario de
    # HORA_APERTURA_COMERCIAL. Aplaza a la proxima apertura comercial, no a la de la clinica.
    if es_reactivacion:
        fuera_de_hora = (
            ahora.hour < HORA_APERTURA_COMERCIAL or ahora.hour >= HORA_CIERRE_COMERCIAL
        )
        if ahora.weekday() == 6 or fuera_de_hora:
            return Decision(
                "aplazar", "fuera_de_horario_comercial", _proxima_apertura_comercial(ahora)
            )

    # R5. No pisarle la conversacion (regla 7). 24 h en vez de los 60 min de G6.
    if (
        es_reactivacion
        and ultimo_mensaje is not None
        and ahora - ultimo_mensaje < timedelta(hours=HORAS_DE_CONTACTO_RECIENTE_COMERCIAL)
    ):
        return Decision("anular", "hablo_hace_poco")

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


def _proxima_apertura_comercial(ahora: datetime) -> datetime:
    """La siguiente franja 9:00-19:00 que no caiga en domingo.

    Deliberadamente NO mira la `Jornada`: el horario comercial es propio (ver
    HORA_APERTURA_COMERCIAL). Si la clinica cerrara un lunes festivo, un «sigue interesada?»
    ese lunes es inocuo; lo que no es inocuo es un domingo a las siete de la manana.
    """
    candidato = ahora
    if candidato.hour >= HORA_CIERRE_COMERCIAL:
        candidato = (candidato + timedelta(days=1)).replace(
            hour=HORA_APERTURA_COMERCIAL, minute=0, second=0, microsecond=0
        )
    elif candidato.hour < HORA_APERTURA_COMERCIAL:
        candidato = candidato.replace(
            hour=HORA_APERTURA_COMERCIAL, minute=0, second=0, microsecond=0
        )
    while candidato.weekday() == 6:
        candidato = (candidato + timedelta(days=1)).replace(
            hour=HORA_APERTURA_COMERCIAL, minute=0, second=0, microsecond=0
        )
    return candidato


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


def _primer_nombre_usable(texto: str | None) -> str | None:
    """El primer token de `texto` que sirve para un saludo, o `None` si no hay ninguno.

    **Parte ANTES de validar, y no al revés** (defecto de la ronda 2 de revisión: la versión
    anterior validaba la cadena ENTERA -"¿tiene alguna letra en algún lado?"- y solo DESPUÉS
    tomaba el primer token para mandarlo. Con `"🌸 Ana"` la cadena completa tiene una letra -la
    de "Ana"- y pasaba la guarda, pero el token que de verdad viajaba a Meta era `"🌸"`, el
    emoji solo: "Hola 🌸" es peor que "Hola paciente" a ojos del paciente -parece un bot roto-,
    que es exactamente lo que esta guarda existe para impedir. Medido por el revisor: el emoji
    delante del nombre (`💖 Andrea`, `✨ Ana`) es una de las formas más comunes de nombre de
    perfil en WhatsApp, así que no era un caso de esquina. Aquí el token que se valida es el
    MISMO que se manda.

    `.split()` **sin argumento**, y no `.split(" ")`: corta por CUALQUIER espacio en blanco
    -incluidos saltos de línea y tabulaciones- y nunca deja un token vacío. Cierra dos residuos
    más del mismo hallazgo, medidos por el revisor:

    - Un nombre con un salto de línea interno (`"Ana\\nPerez"`) sobrevivía entero, `\\n`
      incluido, porque `.split(" ")` solo corta por el carácter espacio. Meta RECHAZA un
      parámetro de plantilla con saltos de línea, y como la fila se marca ANTES de enviar (no
      negociable 21), se perdería para siempre tras los tres intentos, con aviso de fallo al
      doctor por algo que nunca tuvo que fallar.
    - Un nombre que empieza con un espacio (`" Ana Perez"`, alcanzable porque ni
      `asegurar_paciente` ni `registrar_cita` recortan lo que llega) dejaba `.split(" ")[0]`
      en una cadena VACÍA, y el respaldo `or "paciente"` volvía a colar el literal que este
      hallazgo entero existe para sacar de las reactivaciones -ver `parametros_de`, que ya no
      tiene ese respaldo en su rama de reactivación.

    El criterio de "usable" sigue siendo el mismo, deliberadamente conservador: al menos una
    letra (`str.isalpha()`, que sí reconoce acentos y eñes). No busca la regla perfecta -no
    intenta rescatar "Ana" de "🌸 Ana" probando el segundo token-, solo que el token que se
    manda no sea un emoji, un signo suelto, o vacío.
    """
    if not texto:
        return None
    tokens = texto.split()
    if not tokens:
        return None
    primero = tokens[0]
    return primero if any(caracter.isalpha() for caracter in primero) else None


def _nombre_de_reactivacion(fila: dict[str, Any]) -> str | None:
    """El nombre de pila para el ÚNICO hueco de una reactivación, en cascada de tres fuentes.

    `citas.nombre_completo` -> la ficha de `pacientes` -> el nombre de perfil de WhatsApp
    (`mensajes_entrantes.nombre_perfil`), en ese orden -- el mismo que arma el SELECT de
    `persistencia.seguimientos_por_despachar`. Hallazgo crítico de la ronda 1 de revisión:
    **toda fila de reactivación tiene `cita_id` NULL** (`programar_seguimiento` nunca lo pone,
    y una fila CON `cita_id` la anularía G1), así que el primer eslabón (`nombre_completo`,
    que sale del `LEFT JOIN` a `citas`) siempre estaba en NULL para estas filas y
    `parametros_de` caía a su respaldo `"paciente"` el 100% de las veces -- la firma exacta de
    un mensaje masivo, medida por el revisor contra el SELECT real.

    Un lead que preguntó y no agendó normalmente NO tiene ficha (`crear_cita` es quien la
    registra), así que el segundo eslabón solo resuelve algo para quien ya agendó alguna vez
    (`TIPO_CANCELADA`, `TIPO_NO_ASISTIO`). Para el resto, el único dato que existe es el
    nombre de perfil que la persona misma escribió en WhatsApp -- texto libre, sin garantía
    de forma.

    Cada candidato pasa por `_primer_nombre_usable` -- ronda 2 de revisión: antes esta función
    devolvía la cadena CRUDA de la fuente y `parametros_de` la partía después, validando la
    cadena entera en vez del token que de verdad se manda (ver el docstring de esa función).
    Devuelve `None` si NINGÚN candidato produce un nombre usable: **ante la duda, no se manda.**
    """
    ficha = fila.get("nombre_ficha")
    # El marcador de la regla dura 12: una ficha con ese nombre no cuenta como nombre, cae al
    # siguiente eslabón. `NOMBRE_PENDIENTE` es el literal ASCII "PENDIENTE" -- si se colara
    # aquí, la reactivación saldría diciendo "Hola PENDIENTE", peor que "Hola paciente".
    if ficha == persistencia.NOMBRE_PENDIENTE:
        ficha = None

    for candidato in (fila.get("nombre_completo"), ficha, fila.get("nombre_perfil")):
        nombre = _primer_nombre_usable(candidato)
        if nombre is not None:
            return nombre

    return None


def parametros_de(fila: dict[str, Any]) -> list[str] | None:
    """Los huecos de la plantilla de ESA fila, en el orden en que Meta los aprobó.

    Tres formas distintas, una por plantilla: el recordatorio de cita lleva CUATRO huecos,
    `reactivacion_cancelada` y `reactivacion_no_asistio` llevan UNO --el nombre de pila-- y
    `reactivacion_sin_agendar` no lleva NINGUNO desde el 23/09/2026 (ver `TIPOS_SIN_HUECOS`,
    que explica por qué perdió el suyo). Mandar cuatro a una plantilla de uno no es un detalle
    cosmético --Meta rechaza el envío-- y mandar el tratamiento a una de reactivación sería
    además una filtración: es un dato de salud y una notificación de WhatsApp se lee en la
    pantalla de bloqueo. Marketing lo quitó a propósito el 15/09/2026.

    Devuelve `None` cuando una reactivación CON hueco no tiene NINGÚN nombre usable (ver
    `_nombre_de_reactivacion`): `despachar` lo trata como una fila rota, igual que un
    recordatorio sin `cita_inicio`, y la anula en vez de mandar "Hola paciente". Una lista
    VACÍA no es eso y no se puede confundir con ello: es una plantilla que no pide nada.

    Pública (antes `_parametros_del_recordatorio`, privada): la dobla `scripts/probar_
    plantilla.py`, y quien le cambie la firma rompe ese script en silencio -- `pytest -q` no
    lo corre.
    """
    tipo = fila.get("tipo")
    # I1 (ronda 1 de revisión): la polaridad estaba al revés. La versión anterior mandaba al
    # lado de CUATRO huecos con el tratamiento dentro todo lo que NO fuera una de las tres
    # reactivaciones conocidas -fail OPEN-, así que un `tipo` inválido o uno futuro que el
    # CHECK NOT VALID de la 021 no alcanza a rechazar se filtraba un dato de salud a una
    # notificación de WhatsApp. Ahora el lado por defecto es el ESTRECHO (un hueco, sin
    # tratamiento) y solo el recordatorio de cita -- o un `tipo` ausente, que es como llegan
    # las filas de prueba que no fijan esa clave, p. ej. `scripts/probar_plantilla.py`
    # `_fila_de_ejemplo()` -- usan los cuatro huecos.
    if tipo in TIPOS_NO_COMERCIALES or tipo is None:
        # `.split()` sin argumento y no `.split(" ")` (revisión final, H7). El razonamiento
        # está entero en `_primer_nombre_usable`, y aplica palabra por palabra a esta rama:
        # Meta RECHAZA un parámetro de plantilla con saltos de línea (132007), y como la fila
        # se marca ANTES de enviar (no negociable 21) el rechazo la pierde para siempre tras
        # los tres intentos. `citas.nombre_completo` lo escribe `registrar_cita` con lo que el
        # modelo capturó y SIN recortar, así que `"Ana\nPérez"` es alcanzable -- y lo que se
        # pierde aquí no es un mensaje comercial, es el aviso de una cita real.
        #
        # Aquí SÍ se conserva el respaldo `"paciente"`, al revés que en la rama de
        # reactivación: un recordatorio de cita tiene que salir aunque el nombre sea raro,
        # porque el paciente tiene hora de verdad. Lo que no puede es salir MAL formado.
        primero = ((fila.get("nombre_completo") or "").split() or [""])[0]
        return _parametros_del_recordatorio(fila, primero or "paciente")

    # La plantilla de este tipo no tiene ni una variable, así que no hay nada que rellenar y
    # `canales.enviar_plantilla` omite el componente entero. Va DESPUÉS de la rama del
    # recordatorio y ANTES de la cascada de nombre, que es justo lo que se salta: sin hueco no
    # hay nombre que validar, y la anulación por `sin_nombre` deja de poder alcanzar a este
    # tipo. No es una guarda que se pierda -- es una que se quedó sin objeto.
    if tipo in TIPOS_SIN_HUECOS:
        return []

    # `_nombre_de_reactivacion` ya devuelve el PRIMER TOKEN validado -no la cadena cruda-, así
    # que aquí no se vuelve a partir ni queda un respaldo `"paciente"`: ese literal no puede
    # aparecer en una reactivación (ronda 2 de revisión). Si no hay nombre usable, la fila es
    # una fila rota y `despachar` la anula -- ver `_nombre_de_reactivacion`.
    nombre = _nombre_de_reactivacion(fila)
    return None if nombre is None else [nombre]


def _parametros_del_recordatorio(fila: dict[str, Any], nombre: str) -> list[str]:
    """Los cuatro huecos de la plantilla de recordatorio, en el orden en que Meta los aprobó.

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

    El nombre entra ya calculado (desde `parametros_de`) para que el `split` del nombre
    completo viva en un solo sitio.
    """
    inicio = fila["cita_inicio"]
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
    plantillas: dict[str, str],
    idioma: str = "es",
    ahora: datetime | None = None,
    limite: int = 50,
    freno_de_reactivacion: str | None = None,
) -> dict[str, int]:
    """Un ciclo del despachador. Devuelve el recuento por acción.

    `freno_de_reactivacion` viaja tal cual a `decidir` y no se consulta aquí: ver la guarda
    FRENO de esa función. Con el freno puesto, toda reactivación se APLAZA y los
    `recordatorio_cita` siguen saliendo -- esa es la frontera.

    `ahora` entra como parámetro para que una prueba pueda fijarlo: es la misma regla que
    `ctx.ahora` en las tools, y la razón por la que esto se puede probar sin esperar a las seis
    de la tarde.

    `plantillas` es UNA por tipo (`TIPOS_DE_SEGUIMIENTO`), no una sola: el recordatorio de cita
    lleva cuatro huecos y las tres de reactivación llevan uno, así que no hay una plantilla
    única que sirva para todas. Una entrada vacía o ausente apaga el ENVÍO de ESE tipo sin
    apagar la decisión: las guardas corren, las anulaciones y los aplazamientos se escriben, y
    la fila queda pendiente hasta que exista la plantilla. Es lo que permite comprobar en
    producción que decide bien antes de arriesgar un WhatsApp -- y hoy, con las tres de
    reactivación sin aprobar por Meta, es el estado normal para tres de los cuatro tipos.

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
        max_12m = configuracion.get("max_reactivaciones_12m", MAX_REACTIVACIONES_12M)
        max_fallidos = configuracion.get("max_seguimientos_fallidos", MAX_SEGUIMIENTOS_FALLIDOS)

        filas = await asyncio.to_thread(
            persistencia.seguimientos_por_despachar, conn, ahora=momento_actual, limite=limite
        )

        async def _una_fila(fila: dict[str, Any]) -> None:
            """Todo el trabajo de UNA fila. Sale del bucle para poder tener su propio
            `try/except` (revisión final, H8).

            Antes esto era el cuerpo del `for` y una excepción a mitad de tanda se llevaba por
            delante el lote ENTERO -- incluidos los `recordatorio_cita` de las otras 49 filas,
            que es el lado malo clínico: el paciente no recibe el aviso de una cita real. Y con
            `ORDER BY s.fecha_objetivo`, una fila que reventara de forma determinista estaría
            siempre a la cabeza del lote y bloquearía la cola indefinidamente.

            `return` y no `continue` -- es la misma salida, ahora desde una función.
            """
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
                max_reactivaciones_12m=max_12m,
                max_seguimientos_fallidos=max_fallidos,
                freno_de_reactivacion=freno_de_reactivacion,
            )

            if decision.accion == "anular":
                await asyncio.to_thread(
                    persistencia.anular_seguimiento, conn, fila["id"], motivo=decision.motivo
                )
                recuento["anulados"] += 1
                return

            if decision.accion == "aplazar":
                await asyncio.to_thread(
                    persistencia.aplazar_seguimiento,
                    conn,
                    fila["id"],
                    hasta=decision.hasta or momento_actual,
                )
                recuento["aplazados"] += 1
                return

            # A partir de aquí `decision.accion == "enviar"`. La plantilla de ESTE tipo. Antes
            # había una sola y todo lo que no tuviera `cita_inicio` se anulaba con
            # `sin_plantilla`; esa puerta es la que hoy hace inofensivo el portillo de `tipo`,
            # cerrado ya por la 021 y la tool -- ver `TIPOS_NO_COMERCIALES` y las guardas R1-R5.
            nombre_plantilla = plantillas.get(fila.get("tipo") or "")

            # Fila ROTA #1: cualquier tipo que NO sea una reactivación conocida -el
            # recordatorio de cita, un `tipo` ausente en una fila de prueba, o uno inválido o
            # futuro que se cuele por delante del CHECK NOT VALID de la 021- y que llegue sin
            # `cita_inicio` no tiene con qué rellenar sus huecos de fecha y hora; mandarla
            # dejaría al paciente leyendo el literal "PENDIENTE" por WhatsApp -la regla dura 3
            # es para el código, nunca fue permiso para mandarle el marcador a un paciente-.
            # M3 (ronda 1 de revisión): antes de acotar esto a `TIPO_RECORDATORIO`, ese caso
            # se quedaba pendiente PARA SIEMPRE si nadie migraba el `tipo` a mano -el
            # despachador lo releería cada 60 s sin dejar rastro del motivo-. Ahora cierra
            # fail-closed contra la lista de reactivaciones CONOCIDAS (`TIPOS_DE_REACTIVACION`).
            #
            # Esto NO es la misma polaridad que `es_reactivacion` en `decidir` (ronda 2 de
            # revisión: un comentario anterior lo decía, y era falso). Las dos son
            # conservadoras, pero en direcciones OPUESTAS a propósito, y "unificarlas" rompería
            # una de las dos:
            #   - `decidir` usa `tipo not in TIPOS_NO_COMERCIALES`: un `tipo` DESCONOCIDO SÍ
            #     cuenta como reactivación, para que las guardas comerciales (baja, freno,
            #     tope anual, horario) se le apliquen -- el lado seguro ahí es sospechar de
            #     más, no de menos.
            #   - Aquí se usa `tipo not in TIPOS_DE_REACTIVACION`: un `tipo` DESCONOCIDO NO
            #     cuenta como reactivación reconocida, así que si además le falta `cita_inicio`
            #     se anula fail-closed en vez de tratarlo como una reactivación legítima que
            #     solo espera nombre o plantilla -- el lado seguro aquí es exigir más para
            #     dejarlo vivo, no menos.
            if fila.get("tipo") not in TIPOS_DE_REACTIVACION and fila.get("cita_inicio") is None:
                await asyncio.to_thread(
                    persistencia.anular_seguimiento, conn, fila["id"], motivo="sin_cita"
                )
                recuento["anulados"] += 1
                return

            if telefono:
                # G7 se apoya en que este número YA tiene (o está a punto de tener) un
                # recordatorio en esta tanda. Se anota aquí, antes de mirar si hay plantilla o
                # canal, para que el modo "decide y no manda" (plantilla vacía) agrupe igual
                # que agruparía con el canal encendido: si se anotara solo tras un envío que
                # salió bien, dos citas del mismo número decidirían las dos "enviar" con la
                # plantilla apagada, que no es la decisión que se tomaría con la plantilla
                # puesta.
                numeros_de_esta_tanda.add(telefono)

            # Sin plantilla configurada para ESTE tipo, la fila NO se marca ni se anula: se
            # queda pendiente hasta que Meta apruebe. Marcarla la perdería para siempre, y
            # anularla obligaría al barrido a volver a decidir sobre alguien que ya calificó
            # -- es el estado de comprobación deliberado de las tres plantillas de reactivación
            # mientras ninguna esté aprobada (Ruling C4).
            if not nombre_plantilla or whatsapp is None or not telefono:
                log.info(
                    "seguimiento %s (%s): decidido ENVIAR y no se manda (plantilla o canal "
                    "sin configurar). El despachador decide, el canal está apagado, y la fila "
                    "SIGUE pendiente -no se marca- para cuando exista la plantilla.",
                    fila["id"], fila.get("tipo"),
                )
                return

            # Fila ROTA #2, y el hallazgo CRÍTICO de la ronda 1: una reactivación sin NINGÚN
            # nombre usable para su único hueco (ver `_nombre_de_reactivacion`). Sin esto,
            # `parametros_de` caía a su respaldo `"paciente"` y el 100% de las reactivaciones
            # salían con "Hola paciente" -- la firma de un mensaje masivo, y el desempate de
            # este proyecto es que nadie reporte el número. Se anula, no se deja pendiente: el
            # nombre no va a aparecer solo con el paso del tiempo, y dejarla pendiente
            # acumularía basura que el despachador relee cada 60 s sin ninguna salida posible.
            # El barrido de la parada siguiente puede volver a encolar a esta misma persona si
            # alguna vez deja un nombre usable (agenda, o vuelve a escribirle a Daniela con el
            # perfil puesto).
            #
            # VA DESPUÉS del chequeo de plantilla de arriba, y no antes (ronda 2 de revisión):
            # con la plantilla vacía -el modo de comprobación de hoy- esta fila tiene que
            # quedarse PENDIENTE igual que sus hermanas, no consumirse sola. Comprobarlo antes
            # rompía el invariante de esta fase ("decide, registra y NO TOCA nada mientras no
            # haya plantilla"): una reactivación sin nombre usable se anulaba aunque el canal
            # entero estuviera apagado, y el ensayo -que existe para ver a quién se le habría
            # escrito ANTES de escribirle a nadie- mentía sobre esa fila en particular. Con la
            # plantilla configurada el resultado es el mismo por los dos órdenes: se anula
            # igual, así que el camino real no pierde nada.
            parametros = parametros_de(fila)
            if parametros is None:
                await asyncio.to_thread(
                    persistencia.anular_seguimiento, conn, fila["id"], motivo="sin_nombre"
                )
                recuento["anulados"] += 1
                return

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
                return

            fallo: str | None = None
            for intento in range(INTENTOS_DE_ENVIO):
                try:
                    await whatsapp.enviar_plantilla(
                        telefono,
                        plantilla=nombre_plantilla,
                        parametros=parametros,
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
                return

            await asyncio.to_thread(
                persistencia.anotar_recordatorio_en_conversacion,
                conn,
                fila["conversacion_id"],
                tipo=fila["tipo"],
                cuando=momento_actual,
            )
            recuento["enviados"] += 1

        for fila in filas:
            try:
                await _una_fila(fila)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- H8: una fila no se lleva la tanda entera
                # Se cuenta como `fallidos` y no en una clave nueva, a propósito: `fallidos`
                # ya significa «este seguimiento no salió» y es lo que `runtime` usa para
                # avisar a los doctores. Una fila que revienta es exactamente eso, y si era un
                # `recordatorio_cita` el doctor tiene que enterarse. Avisar de más es el lado
                # barato; callar un recordatorio que no salió, no.
                recuento["fallidos"] += 1
                log.exception(
                    "seguimiento %s (%s) reventó y se salta; la tanda sigue con las demás",
                    fila.get("id"),
                    fila.get("tipo"),
                )
                # Y el `rollback`, que es lo que hace que «la tanda sigue» sea verdad.
                # Sin él, un error de BASE en una fila deja la transacción COMPARTIDA
                # envenenada y la siguiente lectura válida muere con
                # `InFailedSqlTransaction: current transaction is aborted` -- o sea que
                # todas las filas restantes fallan, **recordatorios de cita incluidos**,
                # que es justo lo que este `except` existía para impedir. Medido contra
                # Neon en la re-revisión del arreglo final: el `except` sin `rollback`
                # solo salvaba el caso de Python puro (un `TypeError` en `decidir`), que
                # es el improbable, y no el realista.
                #
                # El `rollback` va envuelto porque un fallo aquí -conexión ya muerta- no
                # puede llevarse la tanda por la puerta de atrás: si no se puede limpiar,
                # las siguientes fallarán una a una y se contarán, que es el estado que
                # este bloque ya sabe manejar.
                try:
                    await asyncio.to_thread(conn.rollback)
                except Exception:  # noqa: BLE001 -- la conexión ya estaba perdida
                    log.exception(
                        "no se pudo limpiar la transacción tras el fallo del seguimiento "
                        "%s; las filas restantes fallarán una a una y se contarán",
                        fila.get("id"),
                    )
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

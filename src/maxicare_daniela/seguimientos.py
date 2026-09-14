"""El despachador de recordatorios: el «otro proceso» de la migración 001.

`seguimientos` es una cola desde la fase 3 y hasta hoy nadie la leía: `enviado_en` no se
escribía en ninguna línea del repositorio. Este módulo es quien la lee.

Deliberadamente NO abre conexiones ni habla con Meta por su cuenta: recibe la conexión y los
canales. Es lo que permite probar las siete guardas en milisegundos y sin señal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

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
            if es_la_vispera_natural:
                hora = min(hora_preferida, cierre) if recortar_al_cierre else max(
                    hora_preferida, jornada.apertura
                )
            else:
                hora = cierre
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

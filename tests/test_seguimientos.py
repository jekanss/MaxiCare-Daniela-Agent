"""El despachador de recordatorios, sin base y sin red.

Las siete guardas y las bandas horarias viven aquí porque son decisiones del código, no de
la base: se prueban en milisegundos y corren siempre. Lo que toca Neon está en
`test_seguimientos_neon.py`, marcado `neon`.

Ningún momento sale del reloj de la máquina: todos entran como parámetro.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada
from maxicare_daniela.herramientas import ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sábado, domingo cerrado


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Septiembre de 2026: el 15 es martes, el 19 sábado, el 20 domingo."""
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA_BOGOTA)


def test_una_cita_a_menos_de_cuatro_horas_no_lleva_recordatorio():
    # Escribe a las 9:00 y agenda para las 11:00 del mismo día: acaba de hablar con Daniela.
    assert (
        s.momento_del_recordatorio(
            inicio_cita=momento(15, 11),
            ahora=momento(15, 9),
            jornada=JORNADA,
        )
        is None
    )


def test_entre_cuatro_y_veinticuatro_horas_sale_dos_horas_antes():
    assert s.momento_del_recordatorio(
        inicio_cita=momento(15, 15),
        ahora=momento(15, 9),
        jornada=JORNADA,
    ) == momento(15, 13)


def test_a_mas_de_un_dia_sale_la_vispera_a_las_seis():
    assert s.momento_del_recordatorio(
        inicio_cita=momento(17, 9),
        ahora=momento(15, 9),
        jornada=JORNADA,
    ) == momento(16, 18)


def test_las_dos_horas_antes_que_caen_de_madrugada_se_adelantan_a_la_vispera():
    # Cita a las 8:00 del miércoles, agendada el martes a las 10:00: 22 h de antelación, cae
    # en la banda de las 2 h, y «2 h antes» son las 6:00 a. m. con la clínica cerrada.
    # Aplazarlo a la apertura lo dejaría llegando a las 8:00, la hora de la cita.
    assert s.momento_del_recordatorio(
        inicio_cita=momento(16, 8),
        ahora=momento(15, 10),
        jornada=JORNADA,
    ) == momento(15, 17)  # la víspera, a la hora de cierre


def test_la_vispera_de_un_lunes_es_el_domingo_y_la_clinica_cierra():
    # Cita el lunes 21 a las 9:00. La víspera es domingo: la clínica no abre, así que el
    # recordatorio se adelanta al sábado a la hora de cierre.
    assert s.momento_del_recordatorio(
        inicio_cita=momento(21, 9),
        ahora=momento(17, 9),
        jornada=JORNADA,
    ) == momento(19, 15)  # sábado, cierre de sábado


def test_un_momento_que_ya_paso_no_se_programa():
    # Si el cálculo cae antes de `ahora`, no hay recordatorio que valga.
    assert (
        s.momento_del_recordatorio(
            inicio_cita=momento(15, 10),
            ahora=momento(15, 9, 30),
            jornada=JORNADA,
        )
        is None
    )


def fila(**cambios) -> dict:
    base = dict(
        id=1,
        conversacion_id="conv-1",
        cita_id="cita-1",
        tipo="recordatorio_cita",
        fecha_objetivo=momento(16, 18),
        intentos=0,
        telefono="573001112233",
        nombre_completo="Ana Gómez",
        cita_inicio=momento(17, 9),
        cita_estado="confirmada",
        tomada_por=None,
    )
    base.update(cambios)
    return base


def test_g1_una_cita_cancelada_no_recibe_recordatorio():
    d = s.decidir(
        fila(cita_estado="cancelada"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_cambio"


def test_g2_una_cita_que_ya_paso_no_recibe_recordatorio():
    d = s.decidir(
        fila(cita_inicio=momento(16, 9)),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_pasada"


def test_g3_un_recordatorio_con_mas_de_dos_horas_de_retraso_se_descarta():
    # El proceso estuvo caído: le tocaba a las 18:00 y son las 21:00.
    d = s.decidir(
        fila(),
        ahora=momento(16, 21),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "llego_tarde"


def test_g4_con_el_relevo_puesto_se_aplaza_y_no_se_anula():
    # El doctor puede devolver la conversación en diez minutos: el recordatorio sigue siendo
    # válido. Anularlo aquí lo perdería para siempre.
    d = s.decidir(
        fila(tomada_por="Dr. Pérez"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "aplazar"
    assert d.hasta == momento(16, 18, 30)


def test_g5_fuera_de_la_jornada_se_aplaza_a_la_apertura():
    d = s.decidir(
        fila(fecha_objetivo=momento(16, 6), cita_inicio=momento(17, 9)),
        ahora=momento(16, 6),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "aplazar"
    assert d.hasta == momento(16, 8)


def test_g6_si_el_paciente_acaba_de_escribir_no_se_le_recuerda_nada():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(16, 17, 40),
    )
    assert d.accion == "anular"
    assert d.motivo == "contacto_reciente"


def test_g7_no_se_manda_un_segundo_mensaje_al_mismo_numero():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
        ya_salio_a_ese_numero=True,
    )
    assert d.accion == "aplazar"
    assert d.motivo == "agrupado"


def test_un_recordatorio_limpio_sale():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(14, 9),
    )
    assert d.accion == "enviar"


def test_un_seguimiento_sin_cita_se_salta_las_tres_primeras_guardas():
    # «Llámenme el lunes»: no cuelga de ninguna cita, así que G1, G2 y G3 no aplican.
    d = s.decidir(
        fila(cita_id=None, cita_inicio=None, cita_estado=None, tipo="reactivacion"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "enviar"

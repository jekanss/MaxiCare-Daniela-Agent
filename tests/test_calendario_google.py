"""Lo que de `CalendarioGoogle` se puede probar sin red, que es justo lo que más importa.

Contra Google no se puede probar nada aquí: no hay credenciales en una corrida de `pytest`
y no debería haberlas. Pero la parte de `CalendarioGoogle` donde vive la política --y donde
un error se paga en citas mal agendadas-- no depende de Google en absoluto: es la
traducción de un evento a un `Bloqueo`, y esa es una función pura sobre diccionarios.

Los diccionarios de este archivo tienen la forma que devuelve `events.list` de la API v3 de
Calendar, con las dos representaciones de tiempo que Google usa de verdad (`dateTime` para
un evento con hora, `date` para uno de día completo, con el `end` exclusivo).

El resto --que `insert` inserte, que `delete` borre-- vive en `scripts/probar_calendario.py`
y solo se puede comprobar contra el calendario real.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

import pytest

from maxicare_daniela.calendario import (
    CLAVE_ORIGEN,
    VALOR_ORIGEN,
    ZONA_BOGOTA,
    Bloqueo,
    CalendarioDoble,
    ErrorDeCalendario,
    a_bloqueo,
    bloques_del_dia,
    calendario_desde_config,
    decodificar_cuenta_de_servicio,
)

LUNES_9 = datetime(2026, 9, 14, 9, 0, tzinfo=ZONA_BOGOTA)


def evento(**cambios) -> dict:
    """Un evento normal de Calendar: una cirugía que el doctor apartó a mano."""
    base = {
        "id": "evt-doctor",
        "status": "confirmed",
        "summary": "Cirugía larga",
        "start": {"dateTime": "2026-09-14T09:00:00-05:00", "timeZone": "America/Bogota"},
        "end": {"dateTime": "2026-09-14T11:00:00-05:00", "timeZone": "America/Bogota"},
    }
    base.update(cambios)
    return base


def cuenta_de_servicio(**cambios) -> str:
    """Una cuenta de servicio con la forma correcta, en base64. Nada de esto es real."""
    datos = {
        "type": "service_account",
        "project_id": "proyecto-de-prueba",
        "private_key_id": "0" * 40,
        "private_key": "-----BEGIN PRIVATE KEY-----\nno-es-una-clave\n-----END PRIVATE KEY-----\n",
        "client_email": "daniela@proyecto-de-prueba.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    datos.update(cambios)
    for clave, valor in list(datos.items()):
        if valor is None:
            del datos[clave]
    return base64.b64encode(json.dumps(datos).encode()).decode()


# ==========================================================================================
# La decisión que sostiene la capacidad de la clínica
# ==========================================================================================


def test_lo_que_aparto_un_doctor_si_bloquea():
    """El caso base, y el motivo de que Calendar participe en la disponibilidad."""
    bloqueo = a_bloqueo(evento())

    assert bloqueo is not None
    assert bloqueo.inicio == LUNES_9
    assert bloqueo.fin == LUNES_9 + timedelta(hours=2)
    assert bloqueo.titulo == "Cirugía larga"


def test_una_cita_que_agendo_daniela_no_es_un_bloqueo():
    """La prueba que sostiene todo el módulo.

    `CalendarioDoble` guarda eventos y bloqueos en dos listas separadas, así que jamás
    devolvía una cita al preguntar por bloqueos. Google los devuelve juntos. Sin este
    filtro, la primera cita de una hora taparía el bloque entero y la clínica atendería un
    paciente por hora en vez de los dos que su capacidad permite.
    """
    cita = evento(
        id="evt-daniela",
        summary="Ana Restrepo · ortodoncia",
        extendedProperties={"private": {CLAVE_ORIGEN: VALOR_ORIGEN}},
    )

    assert a_bloqueo(cita) is None


def test_el_bloque_sigue_libre_despues_de_que_daniela_agende_ahi():
    """El fallo del docstring del módulo, recorrido de punta a punta.

    No comprueba la función aislada: comprueba la consecuencia. Con capacidad 2, después de
    agendar al primer paciente a las 9:00, las 9:00 tienen que seguir ofreciéndose.
    """
    jornada = bloques_del_dia(LUNES_9, LUNES_9 + timedelta(hours=3), duracion_minutos=60)
    assert LUNES_9 in jornada

    eventos_del_dia = [
        evento(id="a", extendedProperties={"private": {CLAVE_ORIGEN: VALOR_ORIGEN}},
               start={"dateTime": "2026-09-14T09:00:00-05:00"},
               end={"dateTime": "2026-09-14T10:00:00-05:00"}),
    ]
    bloqueos = [b for b in (a_bloqueo(e) for e in eventos_del_dia) if b is not None]

    tapados = [h for h in jornada if any(b.solapa(h, h + timedelta(hours=1)) for b in bloqueos)]
    assert tapados == [], "una cita de Daniela tapó su propio bloque: la capacidad se perdió"


def test_si_el_doctor_aparto_esa_misma_hora_el_bloque_si_se_tapa():
    """El falso positivo del test anterior. Sin esta prueba, `a_bloqueo` podría devolver
    `None` siempre y las dos pruebas de arriba seguirían pasando."""
    jornada = bloques_del_dia(LUNES_9, LUNES_9 + timedelta(hours=3), duracion_minutos=60)
    bloqueos = [a_bloqueo(evento())]

    tapados = [h for h in jornada if any(b.solapa(h, h + timedelta(hours=1)) for b in bloqueos)]
    assert tapados == [LUNES_9, LUNES_9 + timedelta(hours=1)]


# ==========================================================================================
# Las otras tres razones para no bloquear
# ==========================================================================================


def test_un_evento_cancelado_no_bloquea():
    assert a_bloqueo(evento(status="cancelled")) is None


def test_un_evento_marcado_libre_no_bloquea():
    """`transparency: "transparent"` es el doctor diciendo «esto no me ocupa». Se le cree."""
    assert a_bloqueo(evento(transparency="transparent")) is None


def test_un_evento_marcado_ocupado_si_bloquea():
    assert a_bloqueo(evento(transparency="opaque")) is not None


# ==========================================================================================
# Día completo: el `end` exclusivo de Google
# ==========================================================================================


def test_un_dia_libre_bloquea_el_dia_entero():
    """Un día libre llega sin hora: `{"date": "..."}`, y el `end` es EXCLUSIVO.

    Si esto se tradujera como dos medianoches «tal cual», o peor, se ignorara por no tener
    `dateTime`, un día libre no bloquearía ni un minuto y Daniela citaría pacientes un día
    en que la clínica está cerrada.
    """
    libre = evento(
        summary="Día libre",
        start={"date": "2026-09-14"},
        end={"date": "2026-09-15"},
    )
    bloqueo = a_bloqueo(libre)

    assert bloqueo is not None
    assert bloqueo.inicio == datetime(2026, 9, 14, 0, 0, tzinfo=ZONA_BOGOTA)
    assert bloqueo.fin == datetime(2026, 9, 15, 0, 0, tzinfo=ZONA_BOGOTA)
    # Lo que de verdad importa: tapa cualquier hora de atención de ese día.
    assert bloqueo.solapa(LUNES_9, LUNES_9 + timedelta(hours=1))


def test_un_dia_libre_no_se_come_el_dia_siguiente():
    """El `end` exclusivo, por su consecuencia: las 9 del martes siguen libres."""
    bloqueo = a_bloqueo(
        evento(start={"date": "2026-09-14"}, end={"date": "2026-09-15"})
    )
    martes_9 = LUNES_9 + timedelta(days=1)

    assert bloqueo is not None
    assert not bloqueo.solapa(martes_9, martes_9 + timedelta(hours=1))


# ==========================================================================================
# Lo que no se entiende, no se inventa
# ==========================================================================================


@pytest.mark.parametrize(
    "roto",
    [
        {"start": {}, "end": {}},
        {"start": {"dateTime": "no-es-una-fecha"}, "end": {"dateTime": "2026-09-14T11:00:00-05:00"}},
        {"start": {"date": "14/09/2026"}, "end": {"date": "2026-09-15"}},
        # Fin antes que inicio: un rango imposible no se "arregla" dándole la vuelta.
        {"start": {"dateTime": "2026-09-14T11:00:00-05:00"},
         "end": {"dateTime": "2026-09-14T09:00:00-05:00"}},
    ],
)
def test_un_evento_sin_rango_utilizable_se_ignora(roto):
    """Se ignora y se deja constancia en el log; no se adivina una duración."""
    assert a_bloqueo(evento(**roto)) is None


def test_una_hora_en_utc_se_entiende():
    """Google puede devolver la hora en UTC con 'Z' si el calendario está en otra zona.

    `datetime.fromisoformat` no entiende la 'Z' antes de Python 3.11 y el proyecto declara
    3.10 como mínimo, así que esto se traduce a mano.
    """
    bloqueo = a_bloqueo(
        evento(start={"dateTime": "2026-09-14T14:00:00Z"},
               end={"dateTime": "2026-09-14T16:00:00Z"})
    )

    assert bloqueo is not None
    # 14:00 UTC son las 9:00 en Bogotá.
    assert bloqueo.inicio == LUNES_9


# ==========================================================================================
# La credencial: cuatro fallos que se parecen y no son el mismo
# ==========================================================================================


def test_una_cuenta_de_servicio_bien_formada_se_lee():
    datos = decodificar_cuenta_de_servicio(cuenta_de_servicio())

    assert datos["type"] == "service_account"
    assert datos["client_email"].endswith(".iam.gserviceaccount.com")


def test_la_variable_vacia_se_distingue():
    with pytest.raises(ErrorDeCalendario, match="vacía"):
        decodificar_cuenta_de_servicio("   ")


def test_el_json_pegado_tal_cual_se_distingue():
    """El fallo más común de todos: pegar el JSON en el .env sin convertirlo.

    El `.env` queda con la variable valiendo `{` y el resto del archivo interpretado como
    variables basura. Ya pasó una vez en este proyecto.
    """
    with pytest.raises(ErrorDeCalendario, match="base64"):
        decodificar_cuenta_de_servicio('{"type": "service_account"}')


def test_un_archivo_de_oauth_de_escritorio_se_distingue():
    """Se autentica una persona, no un servidor. Aquí no hay nadie para abrir un navegador."""
    otro = base64.b64encode(json.dumps({"type": "authorized_user"}).encode()).decode()

    with pytest.raises(ErrorDeCalendario, match="authorized_user"):
        decodificar_cuenta_de_servicio(otro)


def test_una_cuenta_sin_clave_privada_se_distingue():
    with pytest.raises(ErrorDeCalendario, match="private_key"):
        decodificar_cuenta_de_servicio(cuenta_de_servicio(private_key=None))


# ==========================================================================================
# La fábrica
# ==========================================================================================


class ConfigFalsa:
    def __init__(self, sa_b64="", calendar_id=""):
        self.google_sa_b64 = sa_b64
        self.google_calendar_id = calendar_id


def test_sin_credenciales_la_fabrica_devuelve_el_doble_y_no_revienta():
    """Un despliegue sin calendario tiene que ser ruidoso, no mortal.

    Daniela puede seguir contestando preguntas de precios y de tratamientos sin Calendar; lo
    que no puede es caerse entera por una variable. El aviso va al log, y `probar_calendario.py`
    es lo que comprueba el camino real.
    """
    assert isinstance(calendario_desde_config(ConfigFalsa()), CalendarioDoble)
    assert isinstance(calendario_desde_config(ConfigFalsa(sa_b64="x")), CalendarioDoble)
    assert isinstance(calendario_desde_config(ConfigFalsa(calendar_id="x")), CalendarioDoble)


def test_con_las_dos_credenciales_la_fabrica_intenta_google_de_verdad():
    """Con las dos variables presentes NO devuelve el doble en silencio.

    Es la mitad que importa de la prueba anterior: una fábrica que siempre devolviera el
    doble pasaría aquella y dejaría las citas fuera del calendario de los doctores sin que
    nadie se enterara. Con una credencial inventada tiene que fallar al construir.
    """
    with pytest.raises(ErrorDeCalendario):
        calendario_desde_config(ConfigFalsa(sa_b64="no-es-base64-@@@", calendar_id="x@gmail.com"))

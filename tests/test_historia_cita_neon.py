"""La historia de una cita (032) y los antecedentes de un número, contra la base de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon tests/test_historia_cita_neon.py

Lo que se prueba es SQL: que el evento viaje en la transacción del cambio, que la hora vieja
se lea antes de que `mover_cita` la pise, que el CHECK de la causa siga cerrado, y que
`antecedentes_del_telefono` no confunda la conversación en curso con las anteriores. Nada de
eso lo ve una prueba doblada.

Escribe en el esquema `pruebas_historia_cita`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import psycopg
import pytest

from maxicare_daniela import panel, persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_historia_cita"

AHORA = datetime(2026, 10, 26, 11, 0, tzinfo=ZONA_BOGOTA)
TELEFONO = "573004445566"


def _url_de_pruebas() -> str:
    """Sin el pooler y con el `search_path` fijado: ver `test_reactivacion_neon.py`."""
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("falta MAXICARE_DATABASE_URL")
    directa = base.replace("-pooler.", ".")
    separador = "&" if "?" in directa else "?"
    return f"{directa}{separador}options=-csearch_path%3D{ESQUEMA}"


@pytest.fixture(scope="module")
def url() -> str:
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    return _url_de_pruebas()


@pytest.fixture(scope="module")
def esquema(url: str):
    """Crea el esquema, aplica las migraciones, y lo borra al terminar pase lo que pase."""
    base_sin_esquema = url.split("&options=")[0].split("?options=")[0]

    with persistencia.conectar(base_sin_esquema) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()

    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)

    yield url

    with persistencia.conectar(base_sin_esquema) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()


@pytest.fixture(autouse=True)
def _limpio(esquema):
    """`citas` antes que `conversaciones` (sin cascada, migración 001). `eventos_cita` se va
    sola con su `ON DELETE CASCADE`, y `estado_oportunidad` con la suya."""
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seguimientos")
            cur.execute("DELETE FROM citas")
            cur.execute("DELETE FROM conversaciones")
        conn.commit()
    yield


@pytest.fixture
def conn(esquema):
    with persistencia.conectar(esquema) as conexion:
        yield conexion


def _cita(conn, conv: str, *, inicio: datetime, tratamiento: str = "limpieza") -> str:
    return persistencia.registrar_cita(
        conn, reserva_id=None, conversacion_id=conv, paciente_id=None,
        nombre_completo="Marcela Rios", telefono=TELEFONO, tratamiento=tratamiento,
        inicio=inicio, duracion_minutos=60, evento_calendar_id=None,
    )


def _eventos(conn, id_cita: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tipo, inicio, inicio_anterior, causa_cancelacion, asistio
              FROM eventos_cita WHERE cita_id = %s ORDER BY id
            """,
            (id_cita,),
        )
        return cur.fetchall()


# ==========================================================================================
# La bitácora
# ==========================================================================================


def test_la_cita_cuenta_su_historia_entera(conn):
    """Creada -> movida -> confirmada -> cancelada, y la hora vieja SIGUE estando. Es lo que
    `mover_cita` pisaba hasta la 032."""
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    primera = AHORA + timedelta(days=2)
    segunda = AHORA + timedelta(days=3)
    id_cita = _cita(conn, conv, inicio=primera)

    persistencia.mover_cita(conn, id_cita, reserva_id=None, inicio=segunda)
    assert persistencia.marcar_cita_confirmada(conn, id_cita, cuando=AHORA) is True
    persistencia.marcar_cita_cancelada(conn, id_cita, motivo="me salió un viaje", causa="otra_fecha")

    eventos = _eventos(conn, id_cita)
    assert [e[0] for e in eventos] == ["creada", "reprogramada", "confirmada_por_paciente", "cancelada"]
    assert eventos[0][1] == primera
    assert eventos[1][1] == segunda and eventos[1][2] == primera
    assert eventos[3][3] == "otra_fecha"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT motivo_cancelacion, causa_cancelacion FROM citas WHERE id = %s", (id_cita,)
        )
        assert cur.fetchone() == ("me salió un viaje", "otra_fecha")


def test_cancelar_o_confirmar_dos_veces_no_son_dos_eventos(conn):
    """Las dos escrituras son idempotentes y la bitácora tiene que serlo con ellas: un
    reintento de Meta o de la tool no es una segunda cancelación."""
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    id_cita = _cita(conn, conv, inicio=AHORA + timedelta(days=2))

    persistencia.marcar_cita_confirmada(conn, id_cita, cuando=AHORA)
    assert persistencia.marcar_cita_confirmada(conn, id_cita, cuando=AHORA) is False
    persistencia.marcar_cita_cancelada(conn, id_cita, causa="ya_no_quiere")
    persistencia.marcar_cita_cancelada(conn, id_cita, causa="ya_no_quiere")

    assert [e[0] for e in _eventos(conn, id_cita)] == [
        "creada", "confirmada_por_paciente", "cancelada",
    ]


def test_el_evento_se_deshace_con_el_cambio(conn):
    """`commit=False` y luego `rollback`: ni la cancelación ni su evento. Una bitácora que
    contara cancelaciones deshechas contaría citas perdidas que no se perdieron."""
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    id_cita = _cita(conn, conv, inicio=AHORA + timedelta(days=2))

    persistencia.marcar_cita_cancelada(conn, id_cita, causa="salud", commit=False)
    conn.rollback()

    assert [e[0] for e in _eventos(conn, id_cita)] == ["creada"]


def test_la_causa_es_una_lista_cerrada(conn):
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    id_cita = _cita(conn, conv, inicio=AHORA + timedelta(days=2))

    with pytest.raises(psycopg.errors.CheckViolation):
        persistencia.marcar_cita_cancelada(conn, id_cita, causa="porque_si")
    conn.rollback()


def test_reaplicar_las_migraciones_no_rompe_la_032(conn):
    """Se aplican TODAS en cada arranque. Con una fila que usa la causa `clinica` dentro, la
    032 tiene que apartarse sola (`.claude/rules/migraciones.md`)."""
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    id_cita = _cita(conn, conv, inicio=AHORA + timedelta(days=2))
    persistencia.marcar_cita_cancelada(conn, id_cita, causa="clinica")

    persistencia.aplicar_esquema(conn)

    assert [e[0] for e in _eventos(conn, id_cita)] == ["creada", "cancelada"]


# ==========================================================================================
# Los antecedentes
# ==========================================================================================


def test_un_numero_sin_historia_no_tiene_antecedentes(conn):
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    assert (
        persistencia.antecedentes_del_telefono(conn, TELEFONO, id_conversacion=conv, ahora=AHORA)
        is None
    )


def test_la_cita_cancelada_y_su_causa_llegan(conn):
    vieja = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    id_cita = _cita(conn, vieja, inicio=AHORA + timedelta(days=4), tratamiento="cordales")
    persistencia.marcar_cita_cancelada(conn, id_cita, causa="precio")
    actual = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)

    a = persistencia.antecedentes_del_telefono(conn, TELEFONO, id_conversacion=actual, ahora=AHORA)

    assert a["ultima_cita_estado"] == "cancelada"
    assert a["ultima_cita_causa"] == "precio"
    assert a["ultima_cita_tratamiento"] == "cordales"
    assert a["tiene_cita_proxima"] is False


def test_una_cita_futura_viva_no_es_la_ultima_cita_sino_la_proxima(conn):
    """La futura no ha pasado: no es historia. Solo dice que existe, sin hora."""
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    _cita(conn, conv, inicio=AHORA + timedelta(days=4))

    a = persistencia.antecedentes_del_telefono(conn, TELEFONO, id_conversacion=conv, ahora=AHORA)

    assert a["tiene_cita_proxima"] is True
    assert a["ultima_cita_inicio"] is None


def test_la_oportunidad_de_ESTA_conversacion_no_es_un_antecedente(conn):
    """La de ahora ya la ve Daniela en su historial; la anterior es la que se perdía."""
    vieja = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    persistencia.upsert_estado_oportunidad(
        conn, vieja, estado="con_barrera", barrera="precio", tratamiento="implantes"
    )
    actual = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    persistencia.upsert_estado_oportunidad(
        conn, actual, estado="listo_para_agendar", barrera="ninguna", tratamiento="limpieza"
    )

    a = persistencia.antecedentes_del_telefono(conn, TELEFONO, id_conversacion=actual, ahora=AHORA)

    assert (a["oportunidad_estado"], a["oportunidad_barrera"], a["oportunidad_tratamiento"]) == (
        "con_barrera", "precio", "implantes",
    )


def test_una_oportunidad_con_los_defaults_no_dice_nada(conn):
    vieja = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    persistencia.upsert_estado_oportunidad(
        conn, vieja, estado="explorando", barrera="ninguna", tratamiento="no_identificado"
    )
    actual = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)

    assert (
        persistencia.antecedentes_del_telefono(conn, TELEFONO, id_conversacion=actual, ahora=AHORA)
        is None
    )


def test_del_relevo_cruza_la_fecha_y_si_hubo_cita(conn):
    """No negociable 17: hubo relevo y, si la hubo, una cita. Nada de lo que se dijo."""
    vieja = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    assert persistencia.activar_relevo(conn, id_conversacion=vieja, doctor="Dra. Prueba") is None
    _cita(conn, vieja, inicio=AHORA - timedelta(days=1))
    persistencia.cerrar_relevo(conn, vieja, motivo="devuelto_por_doctor")
    actual = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)

    a = persistencia.antecedentes_del_telefono(conn, TELEFONO, id_conversacion=actual, ahora=AHORA)

    assert a["relevo_en"] is not None
    assert a["relevo_con_cita"] is True


# ==========================================================================================
# La línea fija del aviso al doctor (paso 7)
# ==========================================================================================


def test_la_situacion_del_aviso_lee_ESTA_conversacion_y_la_proxima_cita(conn):
    """El estado es el de la conversación que escala --no el de otra del mismo número-- y la
    cita es la próxima viva: ni la pasada ni la cancelada."""
    vieja = persistencia.asegurar_conversacion(
        conn, telefono=TELEFONO, paciente_id=None, canal="whatsapp"
    )
    actual = persistencia.asegurar_conversacion(
        conn, telefono=TELEFONO, paciente_id=None, canal="whatsapp"
    )
    persistencia.upsert_estado_oportunidad(conn, vieja, estado="perdido", barrera="miedo")
    persistencia.upsert_estado_oportunidad(
        conn, actual, estado="con_barrera", barrera="precio", tratamiento="implantes"
    )
    _cita(conn, actual, inicio=AHORA - timedelta(days=2))
    cancelada = _cita(conn, actual, inicio=AHORA + timedelta(days=1))
    persistencia.marcar_cita_cancelada(conn, cancelada, causa="precio")
    proxima = AHORA + timedelta(days=5)
    _cita(conn, actual, inicio=proxima)

    situacion = persistencia.situacion_para_el_aviso(conn, actual, TELEFONO, ahora=AHORA)

    assert situacion["estado"] == "con_barrera"
    assert situacion["barrera"] == "precio"
    assert situacion["tratamiento"] == "implantes"
    assert situacion["proxima_cita"] == proxima


def test_sin_estado_ni_cita_la_situacion_viene_vacia(conn):
    actual = persistencia.asegurar_conversacion(
        conn, telefono=TELEFONO, paciente_id=None, canal="whatsapp"
    )

    situacion = persistencia.situacion_para_el_aviso(conn, actual, TELEFONO, ahora=AHORA)

    assert situacion["estado"] is None and situacion["proxima_cita"] is None


# ==========================================================================================
# La etiqueta de riesgo de la Agenda (paso 10)
# ==========================================================================================


def _recordatorio_enviado(conn, conv: str, id_cita: str) -> None:
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv, tipo="recordatorio_cita",
        fecha_objetivo=AHORA, clave_idempotencia=f"prueba:{id_cita}", cita_id=id_cita,
    )
    with conn.cursor() as cur:
        cur.execute("UPDATE seguimientos SET enviado_en = now() WHERE cita_id = %s", (id_cita,))
    conn.commit()


def test_una_cita_sin_nada_raro_no_lleva_riesgo(conn):
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    cita = _cita(conn, conv, inicio=AHORA + timedelta(days=3))

    assert panel.riesgos_de_citas(conn, [cita]) == {cita: []}


def test_los_tres_motivos_de_riesgo_salen_de_la_base(conn):
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    persistencia.upsert_estado_oportunidad(conn, conv, estado="con_barrera", barrera="distancia")
    faltada = _cita(conn, conv, inicio=AHORA - timedelta(days=20))
    with conn.cursor() as cur:
        cur.execute("UPDATE citas SET asistio = false WHERE id = %s", (faltada,))
    conn.commit()
    cita = _cita(conn, conv, inicio=AHORA + timedelta(days=1))
    _recordatorio_enviado(conn, conv, cita)

    assert panel.riesgos_de_citas(conn, [cita])[cita] == [
        panel.RIESGO_SIN_CONFIRMAR,
        "barrera:distancia",
        panel.RIESGO_ANTECEDENTE,
    ]


def test_confirmar_quita_el_riesgo_y_una_cancelacion_de_la_CLINICA_no_cuenta(conn):
    """La confirmación (botón o escrita) apaga `sin_confirmar`. Y que la clínica cancelara
    una cita anterior no dice nada de si este paciente va a venir."""
    conv = persistencia.asegurar_conversacion(conn, telefono=TELEFONO)
    de_la_clinica = _cita(conn, conv, inicio=AHORA - timedelta(days=10))
    persistencia.marcar_cita_cancelada(conn, de_la_clinica, causa="clinica")
    cita = _cita(conn, conv, inicio=AHORA + timedelta(days=1))
    _recordatorio_enviado(conn, conv, cita)
    persistencia.marcar_cita_confirmada(conn, cita, cuando=AHORA)

    assert panel.riesgos_de_citas(conn, [cita]) == {cita: []}

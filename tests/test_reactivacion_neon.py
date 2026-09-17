"""El contador contra la base de verdad. El SQL es lo que se prueba aquí.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Copia las cuatro fixtures de `tests/test_contacto_neon.py` --esquema propio, migrado y
borrado al terminar-- porque lo que estas pruebas garantizan es de la BASE, no de Python: que
el `WHERE` de `sumar_seguimiento_fallido` vaya por teléfono y no alcance a la cartera entera
es justo lo que una prueba doblada no puede ver.

Escribe en el esquema `pruebas_reactivacion`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_reactivacion"

AHORA = datetime(2026, 9, 16, 11, 0, tzinfo=ZONA_BOGOTA)
TELEFONO = "573001112233"


def _url_de_pruebas() -> str:
    """Sin el pooler --rechaza `options` como parámetro de arranque-- y con el `search_path`
    fijado, para que ninguna consulta de aquí pueda tocar `public` por descuido."""
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
    """Cada prueba arranca sin filas. `citas` y `seguimientos` primero: las FK no dejan al
    revés (cuelgan de `conversaciones`, y `citas` NO tiene `ON DELETE CASCADE` -- migración
    001 -- así que borrar `conversaciones` primero revienta con un error de integridad en
    cuanto una prueba haya sembrado una cita).

    `mensajes_entrantes` no cuelga de nada (no tiene FK a `conversaciones`, ver migración
    004): las pruebas de la tarea 7 la usan para simular "cuándo escribió por última vez" y
    "cuándo dejó de contestar", y sin limpiarla se acumularía entre pruebas y entre corridas.
    """
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM citas")
            cur.execute("DELETE FROM seguimientos")
            cur.execute("DELETE FROM mensajes_entrantes")
            cur.execute("DELETE FROM consentimientos")
            cur.execute("DELETE FROM contactos")
            cur.execute("DELETE FROM conversaciones")
        conn.commit()
    yield


def _mensaje(conn, telefono: str, *, cuando: datetime, wamid: str | None = None) -> None:
    """Siembra un mensaje entrante de verdad. `leads_sin_agendar` lee esta tabla de verdad, y
    no hay helper en `persistencia.py` para escribirla -- solo la escribe `ingesta.py`, con
    el webhook de WhatsApp delante."""
    import uuid

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en)
            VALUES (%s, %s, 'text', 'hola', %s)
            """,
            (wamid or str(uuid.uuid4()), telefono, cuando),
        )
    conn.commit()


@pytest.fixture
def conexion_pruebas(esquema):
    """Una conexión abierta contra el esquema de pruebas, cerrada al terminar."""
    with persistencia.conectar(esquema) as conn:
        yield conn


# ==========================================================================================
# El contador (tarea 5)
# ==========================================================================================


def test_el_contador_sube_y_apaga(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    assert persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO) == 1
    assert persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO) == 2


def test_sumar_seguimiento_fallido_asegura_la_fila_como_sus_hermanas(conexion_pruebas):
    """I3 (ronda de revision sobre la parada D): sin `asegurar_contacto` adentro, esta funcion
    es la unica de sus hermanas (`pedir_baja`, `revocar_baja`) que se pierde en silencio
    cuando la fila todavia no existe -- el `UPDATE ... WHERE telefono = %s` no encuentra nada
    que tocar y el `RETURNING` no devuelve fila.

    En WhatsApp no muerde porque `atencion._leer_estado` ya asegura el contacto antes de
    llamar al modelo. Pero el chat web del panel (`runtime.py`) NO pasa por ahi: sin este
    arreglo, la primera vez que alguien prueba "ya no, gracias" desde el panel, Daniela
    confirma el cierre y el contador se queda en cero -- un freno que se da por ejercitado sin
    haberlo sido. Aqui NO se llama a `asegurar_contacto` primero, a proposito: es justo el
    escenario que el hallazgo describe.
    """
    assert persistencia.leer_contacto(conexion_pruebas, TELEFONO) is None
    assert persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO) == 1
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto is not None, "la fila nunca se creo: la llamada se perdio en silencio"
    assert contacto["seguimientos_fallidos"] == 1


def test_agendar_lo_devuelve_a_cero(conexion_pruebas):
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.reiniciar_seguimientos_fallidos(conexion_pruebas, TELEFONO)
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto["seguimientos_fallidos"] == 0


def test_el_contador_no_alcanza_a_otro_telefono(conexion_pruebas):
    """Mismo riesgo que la redacción de `detalle`: un WHERE flojo apaga a toda la cartera."""
    otro = "573009998877"
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.asegurar_contacto(conexion_pruebas, otro)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    assert persistencia.leer_contacto(conexion_pruebas, otro)["seguimientos_fallidos"] == 0


def test_clearstate_resetea_el_contador_pero_no_la_baja(conexion_pruebas):
    """El contador es del SISTEMA y se va; `no_contactar` es de la persona y se queda.

    Si la baja se fuera, resetear a alguien la devolvería a la lista de contactables sin que
    nadie se entere (no negociable 25).
    """
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.pedir_baja(conexion_pruebas, TELEFONO, origen="paciente")
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.borrar_rastro(conexion_pruebas, TELEFONO)
    contacto = persistencia.leer_contacto(conexion_pruebas, TELEFONO)
    assert contacto["seguimientos_fallidos"] == 0
    assert contacto["no_contactar"] is True


# ==========================================================================================
# La anulación por teléfono (tarea 6)
# ==========================================================================================


def test_cerrar_anula_el_segundo_intento_que_vive_en_otra_conversacion(conexion_pruebas):
    """La razón entera de que vaya por teléfono.

    La serie empieza en la conversación A; a los 7 días esa conversación ya caducó y el
    segundo intento sigue colgando de ella. Si la anulación fuera por `id_conversacion` de la
    conversación VIVA, no lo alcanzaría -- y el paciente que acaba de decir que no recibiría
    el mensaje siete días después.
    """
    conv_a = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv_a, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(days=7), clave_idempotencia="k-2",
    )
    conv_b = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)  # la viva
    # 3 pequeña: la prueba entera depende de que `asegurar_conversacion` NUNCA sea un
    # get-or-create (no negociable 5, el nombre miente). Si alguna vez lo fuera, `conv_b`
    # sería igual a `conv_a` y la prueba degeneraría en silencio a "una sola conversación",
    # perdiendo justo lo que dice medir: que la anulación alcanza una conversación DISTINTA.
    assert conv_b != conv_a, "asegurar_conversacion dejó de insertar siempre una fila nueva"
    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no"
    )
    assert anulados == 1, "el segundo intento de la serie sobrevivió al «no»"


def test_cerrar_no_se_lleva_por_delante_el_recordatorio_de_una_cita(conexion_pruebas):
    """I2 (ronda de revisión sobre la parada D): la única línea de esta parada con
    consecuencia clínica.

    Sin el `AND s.tipo <> 'recordatorio_cita'` del WHERE, un paciente con cita mañana que dice
    «ya no me interesa» sobre una promoción se lleva por delante el recordatorio de SU PROPIA
    cita -- exactamente el intercambio que el no negociable 25 prohíbe («la baja es comercial:
    no apaga el recordatorio de una cita»). `anular_seguimientos_de_cita` no entra aquí: ese
    recordatorio cuelga de una reactivación cualquiera, no de una cita, así que la única
    guarda que lo protege es esta.
    """
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="recordatorio_cita",
        fecha_objetivo=AHORA + timedelta(hours=3), clave_idempotencia="k-recordatorio",
    )
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(days=7), clave_idempotencia="k-reactivacion",
    )

    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no"
    )

    assert anulados == 1, "tenía que anular SOLO la reactivación, no las dos filas"
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT anulado_en FROM seguimientos WHERE tipo = 'recordatorio_cita' "
            "AND clave_idempotencia = 'k-recordatorio'"
        )
        (anulado_en,) = cur.fetchone()
    assert anulado_en is None, "el recordatorio de la cita quedó anulado por un «no» ajeno"


# ==========================================================================================
# Tarea 7: el barrido -- quién entra en la cola
#
# El encargo original traía estas pruebas a medias (`database_url=...` con puntos
# suspensivos literales, comentarios `# (sembrar un lead que califique)`). Se escriben aquí
# de verdad, sembrando filas reales y leyéndolas por la consulta real -- nunca fabricando a
# mano un valor que el SELECT real no podría producir.
# ==========================================================================================


def test_regla_1_no_califica_quien_nunca_escribio(conexion_pruebas):
    """La defensa más fuerte que hay: solo a quien escribió PRIMERO.

    Una fila de `contactos` sin conversación no puede existir hoy --`asegurar_contacto` corre
    dentro de `_leer_estado`, o sea al recibir un mensaje-- pero si algún día alguien importa
    una cartera, esta prueba es lo único que impide que salga a mensajes en frío.
    """
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)  # contacto sin conversación
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_quien_ya_tiene_cita_futura(conexion_pruebas):
    """La condición que comparten los tres tipos: a quien ya tiene hora no se le persigue."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.registrar_cita(
        conexion_pruebas, reserva_id=None, conversacion_id=conv, paciente_id=None,
        nombre_completo="Marcela Rios", telefono=TELEFONO, tratamiento="limpieza",
        inicio=AHORA + timedelta(days=2), duracion_minutos=60, evento_calendar_id=None,
    )
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_antes_de_las_24h(conexion_pruebas):
    """Escribió hace dos horas: todavía está en la conversación, no es un lead perdido."""
    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=2))
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_si_califica_quien_pregunto_ayer_y_no_agendo(conexion_pruebas):
    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    leads = persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50)
    assert [l["telefono"] for l in leads] == [TELEFONO]


def test_no_califica_si_el_ultimo_mensaje_es_muy_viejo(conexion_pruebas):
    """La ventana de 30 días del `WHERE` no es adorno: «hace unos días nos escribió» sobre
    algo de hace más de un mes es falso, y es de las cosas por las que la gente reporta un
    número."""
    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(days=40))
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_quien_esta_de_baja(conexion_pruebas):
    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.pedir_baja(conexion_pruebas, TELEFONO, origen="paciente")
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_quien_ya_esta_en_el_tope_del_contador(conexion_pruebas):
    """R1, por duplicado: el barrido no tiene por qué encolar una fila que
    `seguimientos.decidir` va a anular de todas formas al despachar."""
    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.asegurar_contacto(conexion_pruebas, TELEFONO)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    persistencia.sumar_seguimiento_fallido(conexion_pruebas, TELEFONO)
    leads = persistencia.leads_sin_agendar(
        conexion_pruebas, ahora=AHORA, limite=50, max_seguimientos_fallidos=2,
    )
    assert leads == []


def test_quien_dijo_que_no_no_vuelve_a_entrar_por_esta_misma_consulta(conexion_pruebas):
    """El requisito que el encargo original NO traía, y el más importante de esta parada.

    `cerrar_seguimiento` anula con el motivo exacto `el_paciente_dijo_que_no` cuando el
    paciente dice que no quiere que le insistan sobre ESTA consulta. Las otras guardas de
    `leads_sin_agendar` -en particular la que mira "seguimiento vivo de este tipo", que solo
    filtra por `anulado_en IS NULL`- no distinguen esa anulación de un `llego_tarde`
    cualquiera, y un `llego_tarde` SÍ debe poder volver a ofrecerse. Sin esta guarda extra,
    Daniela le habría dicho al paciente "anotado, no se le vuelve a escribir" y el barrido se
    lo habría vuelto a ofrecer al día siguiente: el fallo exacto por el que se reporta un
    número.

    Bloqueo PERMANENTE y no por ventana: un "no" explícito no caduca con el reloj como
    caducan `llego_tarde` o `fuera_de_horario_comercial`, que son sobre CUÁNDO salió el
    mensaje y no sobre si debía salir. Ante la duda, no se vuelve a escribir.
    """
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=1), clave_idempotencia="k-dijo-que-no",
    )
    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="el_paciente_dijo_que_no",
    )
    assert anulados == 1, "el montaje no anuló la fila que la prueba necesita"

    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_quien_llego_tarde_SI_vuelve_a_entrar_a_diferencia_de_quien_dijo_que_no(
    conexion_pruebas,
):
    """El control de la prueba anterior: una anulación CUALQUIERA que no sea el "no"
    explícito no bloquea, porque el barrido tiene que poder volver a ofrecerlo -- R3 de
    `seguimientos.decidir` cuenta con esto: "el barrido lo volverá a encolar si la persona
    sigue calificando"."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=3), clave_idempotencia="k-llego-tarde",
    )
    anulados = persistencia.anular_reactivaciones_vivas(
        conexion_pruebas, TELEFONO, motivo="llego_tarde",
    )
    assert anulados == 1

    leads = persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50)
    assert [l["telefono"] for l in leads] == [TELEFONO]


def test_no_califica_si_ya_tiene_un_seguimiento_pendiente_de_este_tipo(conexion_pruebas):
    """Sin esta guarda, dos pasadas seguidas del barrido -antes de que el despachador decida
    la primera fila- encolarían una fila nueva encima de la que ya está viva."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA, clave_idempotencia="k-pendiente",
    )
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_no_califica_si_se_le_escribio_hace_menos_de_una_semana(conexion_pruebas):
    """El segundo intento espera `DIAS_ENTRE_INTENTOS_DE_REACTIVACION` (7) días desde el
    ENVÍO, no desde que se creó la fila."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=3), clave_idempotencia="k-enviado-reciente",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=3), "k-enviado-reciente"),
        )
    conexion_pruebas.commit()
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_vuelve_a_calificar_una_semana_despues_del_primer_envio(conexion_pruebas):
    """El segundo intento del "como mucho dos mensajes (24 h y 7 días)" del plan: pasados los
    7 días del primer envío sin respuesta, el barrido lo vuelve a ofrecer."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(days=8))
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=8), clave_idempotencia="k-enviado-viejo",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=8), "k-enviado-viejo"),
        )
    conexion_pruebas.commit()
    leads = persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50)
    assert [l["telefono"] for l in leads] == [TELEFONO]


def test_leads_que_cancelaron_solo_mira_citas_canceladas(conexion_pruebas):
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    id_cita = persistencia.registrar_cita(
        conexion_pruebas, reserva_id=None, conversacion_id=conv, paciente_id=None,
        nombre_completo="Marcela Rios", telefono=TELEFONO, tratamiento="limpieza",
        inicio=AHORA - timedelta(days=2), duracion_minutos=60, evento_calendar_id=None,
    )
    persistencia.marcar_cita_cancelada(conexion_pruebas, id_cita, motivo="paciente")
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE citas SET actualizada_en = %s WHERE id = %s",
            (AHORA - timedelta(hours=30), id_cita),
        )
    conexion_pruebas.commit()
    leads = persistencia.leads_que_cancelaron(conexion_pruebas, ahora=AHORA, limite=50)
    assert [l["telefono"] for l in leads] == [TELEFONO]


def test_leads_que_cancelaron_no_persigue_a_quien_ya_reagendo(conexion_pruebas):
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    id_cita = persistencia.registrar_cita(
        conexion_pruebas, reserva_id=None, conversacion_id=conv, paciente_id=None,
        nombre_completo="Marcela Rios", telefono=TELEFONO, tratamiento="limpieza",
        inicio=AHORA - timedelta(days=2), duracion_minutos=60, evento_calendar_id=None,
    )
    persistencia.marcar_cita_cancelada(conexion_pruebas, id_cita, motivo="paciente")
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE citas SET actualizada_en = %s WHERE id = %s",
            (AHORA - timedelta(hours=30), id_cita),
        )
    conexion_pruebas.commit()
    # Reagendó una cita nueva, futura y confirmada: ya no es un lead que perseguir.
    persistencia.registrar_cita(
        conexion_pruebas, reserva_id=None, conversacion_id=conv, paciente_id=None,
        nombre_completo="Marcela Rios", telefono=TELEFONO, tratamiento="limpieza",
        inicio=AHORA + timedelta(days=3), duracion_minutos=60, evento_calendar_id=None,
    )
    assert persistencia.leads_que_cancelaron(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_contar_enviados_hoy_solo_cuenta_reactivacion(conexion_pruebas):
    """Ruling C2: un recordatorio de cita enviado hoy NO se come el cupo diario del barrido."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="recordatorio_cita",
        fecha_objetivo=AHORA, clave_idempotencia="k-recordatorio-hoy",
    )
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA, clave_idempotencia="k-reactivacion-hoy",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia IN (%s, %s)",
            (AHORA, "k-recordatorio-hoy", "k-reactivacion-hoy"),
        )
    conexion_pruebas.commit()
    assert persistencia.contar_enviados_hoy(conexion_pruebas, ahora=AHORA) == 1


def test_contar_enviados_hoy_no_cuenta_los_de_ayer(conexion_pruebas):
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA, clave_idempotencia="k-de-ayer",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=1), "k-de-ayer"),
        )
    conexion_pruebas.commit()
    assert persistencia.contar_enviados_hoy(conexion_pruebas, ahora=AHORA) == 0


def test_series_por_contabilizar_encuentra_un_envio_viejo_sin_respuesta(conexion_pruebas):
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-viejo-sin-respuesta",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-viejo-sin-respuesta"),
        )
    conexion_pruebas.commit()
    filas = persistencia.series_por_contabilizar(conexion_pruebas, ahora=AHORA)
    assert [f["telefono"] for f in filas] == [TELEFONO]


def test_series_por_contabilizar_no_cuenta_a_quien_SI_contesto(conexion_pruebas):
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-viejo-con-respuesta",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-viejo-con-respuesta"),
        )
    conexion_pruebas.commit()
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(days=5))
    assert persistencia.series_por_contabilizar(conexion_pruebas, ahora=AHORA) == []


def test_no_contabiliza_dos_veces_la_misma_serie(conexion_pruebas):
    """`contabilizado_en` existe justo para esto (Ruling C6): sin la marca, dos pasadas del
    barrido subirían el contador dos veces por el mismo silencio, apagando a alguien que
    solo se quedó callado UNA vez. Y de paso fija el orden de Ruling D5: sumar sube antes de
    marcar, y el contador queda en 1 -no en 2- tras UNA sola serie fallida."""
    from maxicare_daniela import barrido

    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-doble",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-doble"),
        )
    conexion_pruebas.commit()

    primero = barrido._contabilizar_series_cerradas(conexion_pruebas, ahora=AHORA)
    segundo = barrido._contabilizar_series_cerradas(conexion_pruebas, ahora=AHORA)

    assert primero == 1
    assert segundo == 0
    assert persistencia.leer_contacto(conexion_pruebas, TELEFONO)["seguimientos_fallidos"] == 1


def test_el_barrido_no_encola_dos_veces_al_mismo(conexion_pruebas, esquema):
    """La clave de idempotencia la arma el CÓDIGO y lleva el DÍA dentro (Ruling B5). Dos
    pasadas seguidas dejan UNA fila."""
    from maxicare_daniela import barrido

    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))

    calidad = {"quality_rating": "GREEN"}
    primero = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True, calidad=calidad,
    )
    segundo = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True, calidad=calidad,
    )
    assert primero["encolados"] == 1
    assert segundo["encolados"] == 0


def test_el_barrido_de_manana_si_vuelve_a_encolar(conexion_pruebas, esquema):
    """El otro lado de Ruling B5: sin el día dentro de la clave, «anular» sería una puerta de
    una sola dirección. Aquí la fila de HOY se anula por `llego_tarde` --como haría
    `seguimientos.decidir`-- y el barrido de MAÑANA, sobre la misma persona, sí la vuelve a
    encolar."""
    from maxicare_daniela import barrido

    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))

    calidad = {"quality_rating": "GREEN"}
    hoy = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True, calidad=calidad,
    )
    assert hoy["encolados"] == 1
    persistencia.anular_reactivaciones_vivas(conexion_pruebas, TELEFONO, motivo="llego_tarde")

    # Sin mensaje nuevo: el de hace 30 h sigue dentro de la ventana de 24h-30 días también
    # mañana (a esa hora tendría 54 h, y el límite es 30 días), así que sigue sin agendar y
    # el barrido de mañana vuelve a calificarlo.
    manana = barrido.encolar(
        database_url=esquema,
        ahora=AHORA + timedelta(days=1),
        tope_diario=20,
        encendido=True,
        calidad=calidad,
    )
    assert manana["encolados"] == 1


def test_el_interruptor_apaga_el_barrido_entero(conexion_pruebas, esquema):
    """Regla 10. Con `encendido=False` no se encola nada y no se toca la base."""
    from maxicare_daniela import barrido

    persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=False,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 0
    with conexion_pruebas.cursor() as cur:
        cur.execute("SELECT count(*) FROM seguimientos")
        (total,) = cur.fetchone()
    assert total == 0


def test_el_tope_diario_corta(conexion_pruebas, esquema):
    """Regla 9: arranque lento. Meta castiga los picos."""
    from maxicare_daniela import barrido

    for i in range(5):
        telefono = f"57300111220{i}"
        persistencia.asegurar_conversacion(conexion_pruebas, telefono=telefono)
        _mensaje(conexion_pruebas, telefono, cuando=AHORA - timedelta(hours=30))

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=2, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 2

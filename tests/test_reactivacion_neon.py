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


def test_contar_comprometidos_hoy_solo_cuenta_reactivacion(conexion_pruebas):
    """Ruling C2 + M13 (ronda 2 de revisión): un recordatorio de cita enviado hoy NO se come
    el cupo diario del barrido. M13 es grave: `contar_enviados_hoy` (la función vieja) SÍ
    tenía este filtro y el mutante que la reemplazó, `contar_comprometidos_hoy`, quedó
    protegido solo por la memoria de quien la escribió -- sin esta prueba, 41 passed en
    Neon con el filtro quitado, y con recordatorios de cita pendientes contando (siempre
    los hay) el cupo de reactivación sería 0 de forma permanente."""
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
    assert persistencia.contar_comprometidos_hoy(conexion_pruebas, ahora=AHORA) == 1


def test_contar_comprometidos_hoy_no_cuenta_recordatorios_de_cita_pendientes(conexion_pruebas):
    """M13, la mitad que de verdad importa: el filtro tiene que alcanzar también a los
    PENDIENTES, no solo a los enviados -- un recordatorio de cita casi siempre está pendiente
    (se manda la víspera), así que es la rama que se ejercita todo el tiempo en producción."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="recordatorio_cita",
        fecha_objetivo=AHORA, clave_idempotencia="k-recordatorio-pendiente",
    )
    assert persistencia.contar_comprometidos_hoy(conexion_pruebas, ahora=AHORA) == 0


def test_contar_comprometidos_hoy_no_cuenta_lo_ya_enviado_ayer(conexion_pruebas):
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
    assert persistencia.contar_comprometidos_hoy(conexion_pruebas, ahora=AHORA) == 0


def test_contar_comprometidos_hoy_cuenta_lo_pendiente_que_vence_pronto(conexion_pruebas):
    """El lado que cierra el CRÍTICO de la ronda 1: una fila aplazada a unas horas vista
    (como haría R4 al horario comercial) sigue comprometiendo el cupo aunque `enviado_en`
    siga en NULL."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(hours=10), clave_idempotencia="k-aplazada-esta-noche",
    )
    assert persistencia.contar_comprometidos_hoy(conexion_pruebas, ahora=AHORA) == 1


def test_contar_comprometidos_hoy_no_cuenta_lo_programado_muy_a_futuro(conexion_pruebas):
    """Bug 2.1, ronda 2 de revisión: `programar_seguimiento` (la tool que llama el MODELO)
    encola reactivaciones con `fecha_objetivo` arbitraria y SIN cota superior. Sin ventana,
    esas filas -pendientes durante todo su horizonte, porque el despachador solo recoge
    `fecha_objetivo <= ahora`- se cargaban al cupo de CADA día hasta que se resolvían. Medido
    por el revisor: dos filas a +14 días con `tope_diario=2` dejaban `{'encolados': 0}` con
    cinco leads frescos esperando turno."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA + timedelta(days=14), clave_idempotencia="k-programada-lejos",
    )
    assert persistencia.contar_comprometidos_hoy(conexion_pruebas, ahora=AHORA) == 0


def test_bug_2_1_lo_programado_a_futuro_no_apaga_el_barrido_para_los_leads_frescos(
    conexion_pruebas, esquema
):
    """El repro exacto del revisor, contra `barrido.encolar` completo."""
    from maxicare_daniela import barrido

    for i in range(2):
        telefono = f"573008880{i:03d}"
        conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=telefono)
        persistencia.insertar_seguimiento(
            conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
            fecha_objetivo=AHORA + timedelta(days=14), clave_idempotencia=f"k-lejos-{i}",
        )

    for i in range(5):
        telefono = f"573009990{i:03d}"
        persistencia.asegurar_conversacion(conexion_pruebas, telefono=telefono)
        _mensaje(conexion_pruebas, telefono, cuando=AHORA - timedelta(hours=30))

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=2, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 2, (
        f"lo programado a futuro apagó el cupo para los leads frescos: {recuento}"
    )


def test_bug_2_2_contabilizar_corre_aunque_el_cupo_este_a_cero(conexion_pruebas, esquema):
    """Ronda 2 de revisión: `_contabilizar_series_cerradas` va ANTES del `if cupo == 0:
    return`. Con el cupo lleno de pendientes -el estado normal de cada noche, tras el
    arreglo del CRÍTICO-, el `return` temprano dejaba de contabilizar precisamente esas
    horas. Medido por el revisor: con una fila pendiente y `tope_diario=1`, una serie
    vencida hace 10 días devolvía `contabilizados: 0`."""
    from maxicare_daniela import barrido

    # Ocupa el cupo entero con una fila pendiente (no resuelta), simulando la noche.
    conv_ocupa = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv_ocupa, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA, clave_idempotencia="k-ocupa-el-cupo",
    )

    # Una serie vencida hace 10 días, lista para contabilizarse, de OTRO teléfono.
    otro = "573001110099"
    conv_vieja = persistencia.asegurar_conversacion(conexion_pruebas, telefono=otro)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv_vieja, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-vencida-hace-10-dias",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-vencida-hace-10-dias"),
        )
    conexion_pruebas.commit()

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=1, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["contabilizados"] == 1, (
        f"el cupo a cero se llevó por delante el cierre de series vencidas: {recuento}"
    )


def test_M6_leads_sin_agendar_bloqueado_por_una_reactivacion_cancelada_pendiente(
    conexion_pruebas,
):
    """M6, ronda 2 de revisión: mutante superviviente. Revertir la guarda I-1 SOLO en
    `_LEADS_SIN_AGENDAR` dejaba 41 passed en Neon y 992 offline, porque la única prueba de
    I-1 (`test_no_recibe_dos_tipos_de_reactivacion_en_la_misma_ventana`) ejercita el barrido
    completo, y `sin_agendar` corre PRIMERO en `barrido.encolar` -- así que esa prueba solo
    cazaba la guarda del lado de `_LEADS_QUE_CANCELARON`. Aquí se llama a `leads_sin_agendar`
    DIRECTAMENTE sobre un teléfono que YA tiene una `reactivacion_cancelada` pendiente, para
    cazar la guarda del otro lado."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_cancelada",
        fecha_objetivo=AHORA, clave_idempotencia="k-cancelada-en-juego",
    )
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_series_por_contabilizar_encuentra_un_envio_viejo_sin_cita(conexion_pruebas):
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-viejo-sin-cita",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-viejo-sin-cita"),
        )
    conexion_pruebas.commit()
    filas = persistencia.series_por_contabilizar(conexion_pruebas, ahora=AHORA)
    assert [f["telefono"] for f in filas] == [TELEFONO]


def test_series_por_contabilizar_SI_cuenta_a_quien_contesta_pero_no_agenda(conexion_pruebas):
    """I-2, ronda 1 de revisión -- el hallazgo más grave de la parada.

    La versión anterior descartaba la serie si había CUALQUIER mensaje del paciente después
    del envío, así que quien contestaba algo -un «ahora no, gracias» que Daniela no
    interpreta como cierre- nunca acumulaba el contador y el barrido lo seguía invitando
    cada semana para siempre: 6 mensajes en 40 días contra 2 de quien se queda callado,
    medido por el revisor. El criterio correcto es si la serie ACABÓ EN CITA, no si hubo
    RESPUESTA -- la misma vara que ya usa `crear_cita` para resetear el contador.
    """
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-contesta-no-agenda",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-contesta-no-agenda"),
        )
    conexion_pruebas.commit()
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(days=5))  # contestó, no agendó
    filas = persistencia.series_por_contabilizar(conexion_pruebas, ahora=AHORA)
    assert [f["telefono"] for f in filas] == [TELEFONO], "una respuesta sin cita sigue exonerando"


def test_series_por_contabilizar_no_cuenta_a_quien_SI_agendo(conexion_pruebas):
    """El control de la prueba anterior: agendar SÍ exonera -- es la misma vara que
    `crear_cita` usa para resetear `seguimientos_fallidos` a cero.

    Ronda 2 de revisión, higiene: la versión anterior dejaba que `citas.creada_en` cayera en
    el reloj REAL de la corrida (su default es `now()`) y se apoyaba en que ese instante
    fuera posterior a `AHORA - 10 días` -- cierto solo porque `AHORA` está clavada cerca de
    la fecha real del repositorio. Es la misma familia de fallo que ya se ha pagado tres
    veces en este proyecto (ver `.claude/rules/pruebas.md`). Ahora `creada_en` se fija a
    mano, dentro de la ventana de `AHORA`, sin ninguna relación con el reloj de verdad.
    """
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    persistencia.insertar_seguimiento(
        conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
        fecha_objetivo=AHORA - timedelta(days=10), clave_idempotencia="k-si-agendo",
    )
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=10), "k-si-agendo"),
        )
    conexion_pruebas.commit()
    id_cita = persistencia.registrar_cita(
        conexion_pruebas, reserva_id=None, conversacion_id=conv, paciente_id=None,
        nombre_completo="Marcela Rios", telefono=TELEFONO, tratamiento="limpieza",
        inicio=AHORA + timedelta(days=3), duracion_minutos=60, evento_calendar_id=None,
    )
    # La cita se crea DESPUÉS del envío -- fijado a mano, sin depender del reloj real.
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE citas SET creada_en = %s WHERE id = %s",
            (AHORA - timedelta(days=5), id_cita),
        )
    conexion_pruebas.commit()
    assert persistencia.series_por_contabilizar(conexion_pruebas, ahora=AHORA) == []


def test_series_por_contabilizar_cuenta_CADA_envio_por_separado(conexion_pruebas):
    """Ronda 2 de revisión: I-5 se REVIRTIÓ, y esta prueba fija el comportamiento CONTRARIO
    al que fijaba la ronda 1 (que congelaba el defecto medido por el revisor: una serie de
    dos envíos solo contaba una vez, así que subir la perilla de 2 a 3 no daba "3 series"
    sino 3 mensajes con contador en 3 -- ver el docstring de `DIAS_ENTRE_INTENTOS_DE_
    REACTIVACION`). Ahora dos envíos, cada uno vencido y sin cita, cuentan como DOS unidades
    -- determinista, sin `DISTINCT ON`, sin noción de "serie"."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    for clave, dias in (("k-envio-uno", 20), ("k-envio-dos", 8)):
        persistencia.insertar_seguimiento(
            conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
            fecha_objetivo=AHORA - timedelta(days=dias), clave_idempotencia=clave,
        )
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
                (AHORA - timedelta(days=dias), clave),
            )
    conexion_pruebas.commit()

    filas = persistencia.series_por_contabilizar(conexion_pruebas, ahora=AHORA)
    assert len(filas) == 2, f"dos envíos vencidos deberían contar como dos, no {len(filas)}"
    claves = {"k-envio-uno", "k-envio-dos"}
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT clave_idempotencia FROM seguimientos WHERE id = ANY(%s)",
            ([f["id"] for f in filas],),
        )
        marcadas = {fila[0] for fila in cur.fetchall()}
    assert marcadas == claves, "los dos envíos tienen que poder marcarse, no solo el reciente"


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


# ==========================================================================================
# Ronda 1 de revisión sobre la parada E
# ==========================================================================================


def test_CRITICO_el_tope_diario_acota_lo_comprometido_no_solo_lo_enviado(
    conexion_pruebas, esquema
):
    """CRÍTICO, ronda 1 de revisión.

    R4 (`seguimientos.decidir`) APLAZA -no anula- fuera de 9-19h, así que una fila encolada
    de noche se queda PENDIENTE (`enviado_en IS NULL`) con `fecha_objetivo` reescrita a
    mañana. Contar solo `enviado_en` para el cupo diario (`contar_enviados_hoy`, la función
    vieja) deja el cupo "libre" toda la noche: diez pasadas horarias con `tope_diario=20`
    encolaban 200 filas pendientes, que salían TODAS de golpe a las 9:00 del día siguiente.
    Medido por el revisor: 400 leads, tope 20, 280 mensajes reales.

    Aquí se simulan varias pasadas seguidas SIN que nada se despache entre medias -como
    ocurriría de noche, con el despachador vivo pero R4/R5 aplazando todo- y se comprueba
    que el total COMPROMETIDO (enviado + pendiente sin anular) nunca pasa del tope.
    """
    from maxicare_daniela import barrido

    for i in range(50):
        telefono = f"5730055500{i:02d}"
        persistencia.asegurar_conversacion(conexion_pruebas, telefono=telefono)
        _mensaje(conexion_pruebas, telefono, cuando=AHORA - timedelta(hours=30))

    calidad = {"quality_rating": "GREEN"}
    for _ in range(10):  # diez pasadas "horarias" sin que nada se despache entre medias
        barrido.encolar(
            database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True, calidad=calidad,
        )

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM seguimientos WHERE tipo <> 'recordatorio_cita' "
            "AND anulado_en IS NULL"
        )
        (comprometidos,) = cur.fetchone()
    assert comprometidos <= 20, f"el tope diario no acotó: {comprometidos} filas vivas"


def test_no_recibe_dos_tipos_de_reactivacion_en_la_misma_ventana(conexion_pruebas, esquema):
    """I-1, ronda 1 de revisión.

    Quien preguntó y no agendó Y ADEMÁS canceló una cita antigua calificaba por las DOS
    consultas a la vez, y como cada `NOT EXISTS` de "seguimiento en juego" miraba SOLO su
    propio tipo, `barrido.encolar` lo encolaba dos veces en la misma pasada -dos discursos
    distintos ("¿sigues interesada?" y "¿pudiste reagendar tu cita?") en la misma ventana de
    24h-30d. Ahora el "en juego" mira TODOS los tipos de reactivación.
    """
    from maxicare_daniela import barrido

    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
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

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 1, f"encoló los dos tipos en la misma pasada: {recuento}"

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT s.tipo FROM seguimientos s "
            "JOIN conversaciones cv ON cv.id = s.conversacion_id "
            "WHERE cv.telefono = %s",
            (TELEFONO,),
        )
        tipos = [fila[0] for fila in cur.fetchall()]
    assert len(tipos) == 1, f"se encolaron los dos tipos a la vez: {tipos}"


def test_el_barrido_escribe_el_tipo_la_fecha_y_la_clave_correctos(conexion_pruebas, esquema):
    """I-3, ronda 1 de revisión: ninguna prueba miraba lo que el barrido ESCRIBE, solo cuántas
    filas. El revisor intercambió `TIPO_SIN_AGENDAR`<->`TIPO_CANCELADA` en el código y la
    suite entera siguió en verde -30 passed en Neon, 987 offline-. Con esta prueba, ese
    intercambio la rompe."""
    from maxicare_daniela import barrido

    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 1

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT s.tipo, s.fecha_objetivo, s.clave_idempotencia FROM seguimientos s "
            "JOIN conversaciones cv ON cv.id = s.conversacion_id WHERE cv.telefono = %s",
            (TELEFONO,),
        )
        (tipo, fecha_objetivo, clave) = cur.fetchone()

    assert tipo == "reactivacion_sin_agendar"
    assert fecha_objetivo == AHORA
    assert clave == f"{conv}:reactivacion:reactivacion_sin_agendar:{AHORA.date().isoformat()}"


def test_leads_que_cancelaron_escribe_el_tipo_correcto(conexion_pruebas, esquema):
    """La misma prueba que la anterior, del otro lado: `leads_que_cancelaron` tiene que
    escribir `reactivacion_cancelada`, no `reactivacion_sin_agendar`."""
    from maxicare_daniela import barrido

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

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=20, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 1

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT s.tipo FROM seguimientos s "
            "JOIN conversaciones cv ON cv.id = s.conversacion_id WHERE cv.telefono = %s",
            (TELEFONO,),
        )
        (tipo,) = cur.fetchone()
    assert tipo == "reactivacion_cancelada"


def test_leads_que_cancelaron_no_mira_conversaciones_web(conexion_pruebas):
    """C4, cerrado del todo en la ronda 1 de revisión: el filtro `canal = 'whatsapp'` faltaba
    en esta consulta -se apoyaba en que una cita solo nace de una conversación real de
    WhatsApp, cierto hoy pero no protegido por ninguna prueba. Mutar el filtro de la otra
    consulta a `WHERE TRUE` dejaba las pruebas de Neon en verde; esta lo impide."""
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO, canal="web")
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
    assert persistencia.leads_que_cancelaron(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_una_fila_anulada_no_tapa_la_cabeza_de_la_cola(conexion_pruebas, esquema):
    """I-4, ronda 1 de revisión: inanición.

    Con `limite=cupo` en la llamada a la consulta, una fila que ya se encoló HOY y se anuló
    (p. ej. `tope_anual` o `sin_nombre`) no bloquea la reconsulta -el `NOT EXISTS` de "en
    juego" no cuenta lo anulado- pero SIGUE apareciendo en la cabeza del `ORDER BY` y choca
    contra el `ON CONFLICT`: `encolados` se queda en 0 y la persona nº 21 nunca recibe su
    turno en toda la jornada. El arreglo pide MÁS candidatos de los que hacen falta
    (`limite`, no `cupo`) y sigue intentando hasta cubrir el cupo de verdad.
    """
    from maxicare_daniela import barrido

    # Las tres primeras (por `ORDER BY um.cuando DESC`, mensaje más reciente primero) ya
    # tienen HOY una fila anulada -simulando que `seguimientos.decidir` las rechazó nada más
    # despacharlas- y seguirán apareciendo en la cabeza de la consulta.
    for i in range(3):
        telefono = f"573006660{i:03d}"
        conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=telefono)
        _mensaje(conexion_pruebas, telefono, cuando=AHORA - timedelta(hours=25, minutes=i))
        # La MISMA clave que `barrido.encolar` construiría hoy para esta conversación y este
        # tipo -- si no coincide exactamente, el `ON CONFLICT` no choca y la prueba no
        # reproduce nada.
        clave_de_hoy = (
            f"{conv}:reactivacion:reactivacion_sin_agendar:{AHORA.date().isoformat()}"
        )
        persistencia.insertar_seguimiento(
            conexion_pruebas,
            id_conversacion=conv,
            tipo="reactivacion_sin_agendar",
            fecha_objetivo=AHORA,
            clave_idempotencia=clave_de_hoy,
        )
        persistencia.anular_reactivaciones_vivas(conexion_pruebas, telefono, motivo="tope_anual")

    # Dos candidatos genuinamente nuevos, con mensajes más VIEJOS -así que salen DESPUÉS de
    # los tres anteriores en el `ORDER BY um.cuando DESC`, tal como estarían en producción
    # tras varias horas de barrido consumiendo la cabeza de la lista.
    buenos = []
    for i in range(2):
        telefono = f"573007770{i:03d}"
        persistencia.asegurar_conversacion(conexion_pruebas, telefono=telefono)
        _mensaje(conexion_pruebas, telefono, cuando=AHORA - timedelta(hours=26, minutes=i))
        buenos.append(telefono)

    recuento = barrido.encolar(
        database_url=esquema, ahora=AHORA, tope_diario=2, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 2, (
        f"la cabeza de la cola (ya anulada) tapó a los candidatos buenos: {recuento}"
    )

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT cv.telefono FROM seguimientos s "
            "JOIN conversaciones cv ON cv.id = s.conversacion_id "
            "WHERE s.anulado_en IS NULL AND s.tipo = 'reactivacion_sin_agendar'"
        )
        telefonos_encolados = {fila[0] for fila in cur.fetchall()}
    assert telefonos_encolados == set(buenos)


def test_no_ofrece_un_tercer_intento_sin_cerrar_los_dos_anteriores(conexion_pruebas):
    """`INTENTOS_POR_SERIE_DE_REACTIVACION` es fijo (2) y es defensa en profundidad de la
    MISMA cota que R1 (Ronda 2: ya no es un tope independiente -- ver el docstring de
    `DIAS_ENTRE_INTENTOS_DE_REACTIVACION`). Dos envíos ya viejos (fuera de la ventana de
    reintento de 7 días) que TODAVÍA no se contabilizaron -un estado transitorio: en la
    operación normal `_contabilizar_series_cerradas` los cierra antes de que esta consulta
    corra, ver `barrido.encolar`- siguen bloqueando un tercer envío del MISMO tipo aunque
    `contactos.seguimientos_fallidos` (la vía normal de R1) todavía no se haya actualizado.
    """
    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    for i, dias in enumerate([20, 8]):
        clave = f"k-intentos-tope-{i}"
        persistencia.insertar_seguimiento(
            conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
            fecha_objetivo=AHORA - timedelta(days=dias), clave_idempotencia=clave,
        )
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
                (AHORA - timedelta(days=dias), clave),
            )
    conexion_pruebas.commit()
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == []


def test_cerrar_dos_envios_viejos_a_la_vez_agota_el_tope_de_R1(conexion_pruebas):
    """Ronda 2 de revisión: el control de la prueba anterior YA NO es "cerrar abre paso"
    -esa era la promesa rota de la ronda 1, que contaba una unidad por SERIE en vez de por
    ENVÍO-. Con el conteo revertido a "un envío, una unidad", cerrar DOS envíos viejos a la
    vez -el estado transitorio de la prueba anterior- sube el contador en DOS de un tirón, y
    con el default de `max_seguimientos_fallidos` (2) eso agota el tope en la MISMA pasada:
    no queda paso para un tercer intento. Es la operación normal (uno cada vez) la que dosifica
    el contador en pasos de uno -- ver `test_el_calendario_medido_de_las_tres_personas_da_2`.
    """
    from maxicare_daniela import barrido

    conv = persistencia.asegurar_conversacion(conexion_pruebas, telefono=TELEFONO)
    _mensaje(conexion_pruebas, TELEFONO, cuando=AHORA - timedelta(hours=30))
    for i, dias in enumerate([20, 8]):
        clave = f"k-cierra-los-dos-{i}"
        persistencia.insertar_seguimiento(
            conexion_pruebas, id_conversacion=conv, tipo="reactivacion_sin_agendar",
            fecha_objetivo=AHORA - timedelta(days=dias), clave_idempotencia=clave,
        )
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
                (AHORA - timedelta(days=dias), clave),
            )
    conexion_pruebas.commit()

    cerradas = barrido._contabilizar_series_cerradas(conexion_pruebas, ahora=AHORA)
    assert cerradas == 2, "los dos envíos vencidos tenían que cerrarse juntos, no uno"
    assert persistencia.leer_contacto(conexion_pruebas, TELEFONO)["seguimientos_fallidos"] == 2

    leads = persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50)
    assert leads == [], "R1 tenía que quedar agotado tras cerrar los dos de un tirón"


def test_el_calendario_medido_de_las_tres_personas_da_2(conexion_pruebas, esquema):
    """El pedido explícito de la ronda 1 de revisión: simular el ciclo completo (encolar +
    contabilizar, pasada a pasada, marcando el envío como si el despachador lo hubiera hecho
    en el acto) para tres personas representativas, y comprobar que las tres terminan con
    `seguimientos_fallidos == 2` -ni más (I-2: quien contesta sin agendar ya no escapa al
    contador) ni menos (I-1: quien califica por las dos listas no dobla el envío)-, con
    exactamente 2 mensajes enviados cada una.

    - «la que calla»: nunca vuelve a escribir, nunca agenda.
    - «la de las dos listas»: pregunta y no agenda, Y ADEMÁS canceló una cita vieja.
    - «la que contesta»: responde cada vez que le llega un mensaje, pero nunca agenda.

    **Lo que esto NO es, dicho en la ronda 2 de revisión porque el informe de la ronda 1 lo
    daba a entender sin decirlo: no es un extremo a extremo.** `_pasada` marca `enviado_en`
    con un `UPDATE` directo -- `seguimientos.despachar`, `decidir` con sus once guardas y
    `marcar_seguimiento_enviado` NUNCA se ejecutan aquí. El atajo yerra hacia el lado
    GENEROSO: marca como enviado incluso lo que `decidir` habría anulado (por ejemplo, fuera
    de horario comercial). Para el número que este test mide -- "como mucho 2" -- ese sesgo
    es el correcto (si acaso, sobreestima cuántos mensajes saldrían de verdad), así que no
    hace falta corregirlo, pero quien lo lea tiene que saber qué mide y qué no: la cuenta del
    BARRIDO (quién entra y cuántas veces), no el envío real de WhatsApp.

    **Y esta prueba, sola, es insensible a `INTENTOS_POR_SERIE_DE_REACTIVACION`** (ronda 2,
    hallazgo de higiene): en la cadencia normal de arriba nunca coexisten dos envíos sin
    contabilizar del mismo tipo -`_contabilizar_series_cerradas` cierra cada uno antes de que
    llegue el siguiente (bug 2.2 corregido)-, así que ese tope nunca es el que decide. El
    bloque final, con un cuarto teléfono, añade a propósito el estado que sí lo ejercita.
    """
    from maxicare_daniela import barrido

    CALLA = "573005550001"
    DOSLISTAS = "573005550002"
    CONTESTA = "573005550003"

    persistencia.asegurar_conversacion(conexion_pruebas, telefono=CALLA)
    _mensaje(conexion_pruebas, CALLA, cuando=AHORA - timedelta(hours=30))

    conv_dos = persistencia.asegurar_conversacion(conexion_pruebas, telefono=DOSLISTAS)
    _mensaje(conexion_pruebas, DOSLISTAS, cuando=AHORA - timedelta(hours=30))
    id_cita = persistencia.registrar_cita(
        conexion_pruebas, reserva_id=None, conversacion_id=conv_dos, paciente_id=None,
        nombre_completo="Paciente Dos Listas", telefono=DOSLISTAS, tratamiento="limpieza",
        inicio=AHORA - timedelta(days=2), duracion_minutos=60, evento_calendar_id=None,
    )
    persistencia.marcar_cita_cancelada(conexion_pruebas, id_cita, motivo="paciente")
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "UPDATE citas SET actualizada_en = %s WHERE id = %s",
            (AHORA - timedelta(hours=30), id_cita),
        )
    conexion_pruebas.commit()

    persistencia.asegurar_conversacion(conexion_pruebas, telefono=CONTESTA)
    _mensaje(conexion_pruebas, CONTESTA, cuando=AHORA - timedelta(hours=30))

    calidad = {"quality_rating": "GREEN"}

    def _pasada(dia):
        """Encola, y simula que el despachador manda TODO lo que quedó pendiente, en el acto."""
        barrido.encolar(
            database_url=esquema, ahora=dia, tope_diario=100, encendido=True, calidad=calidad,
        )
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "UPDATE seguimientos SET enviado_en = %s "
                " WHERE tipo <> 'recordatorio_cita' AND enviado_en IS NULL "
                "   AND anulado_en IS NULL",
                (dia,),
            )
        conexion_pruebas.commit()

    _pasada(AHORA)                                    # día 0: sale el primer intento de cada una
    _mensaje(conexion_pruebas, CONTESTA, cuando=AHORA + timedelta(days=3))  # contesta, no agenda
    _pasada(AHORA + timedelta(days=7))                # día 7: cierra el 1º, sale el 2º intento
    _pasada(AHORA + timedelta(days=14))               # día 14: cierra el 2º intento

    for telefono in (CALLA, DOSLISTAS, CONTESTA):
        contacto = persistencia.leer_contacto(conexion_pruebas, telefono)
        assert contacto["seguimientos_fallidos"] == 2, (
            f"{telefono}: contador terminó en {contacto['seguimientos_fallidos']}, no en 2"
        )

    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT cv.telefono, count(*) FROM seguimientos s "
            "JOIN conversaciones cv ON cv.id = s.conversacion_id "
            "WHERE s.tipo <> 'recordatorio_cita' AND s.enviado_en IS NOT NULL "
            "GROUP BY cv.telefono"
        )
        enviados = dict(cur.fetchall())
    assert enviados == {CALLA: 2, DOSLISTAS: 2, CONTESTA: 2}, enviados

    # Ninguna de las tres sigue calificando: R1 las apagó a las tres.
    _pasada(AHORA + timedelta(days=21))
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM seguimientos WHERE tipo <> 'recordatorio_cita' "
            "AND enviado_en > %s",
            (AHORA + timedelta(days=14),),
        )
        (nuevos,) = cur.fetchone()
    assert nuevos == 0, "R1 no las apagó: siguió saliendo algo después del día 14"

    # Cuarto caso, añadido a propósito (ver el docstring): un teléfono que llega con DOS
    # envíos viejos ya sin cerrar -el estado que sí ejercita `INTENTOS_POR_SERIE_DE_
    # REACTIVACION`, y que la cadencia normal de arriba no produce nunca-. Con ese tope
    # puesto a 99 (o quitado), esta parte de la prueba deja de fallar y el barrido volvería
    # a ofrecer un tercer intento sin que la serie se hubiera cerrado.
    ATASCADA = "573005550004"
    conv_atascada = persistencia.asegurar_conversacion(conexion_pruebas, telefono=ATASCADA)
    _mensaje(conexion_pruebas, ATASCADA, cuando=AHORA - timedelta(hours=30))
    for i, dias in enumerate([20, 8]):
        clave = f"k-atascada-{i}"
        persistencia.insertar_seguimiento(
            conexion_pruebas, id_conversacion=conv_atascada, tipo="reactivacion_sin_agendar",
            fecha_objetivo=AHORA - timedelta(days=dias), clave_idempotencia=clave,
        )
        with conexion_pruebas.cursor() as cur:
            cur.execute(
                "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
                (AHORA - timedelta(days=dias), clave),
            )
    conexion_pruebas.commit()
    assert persistencia.leads_sin_agendar(conexion_pruebas, ahora=AHORA, limite=50) == [], (
        "INTENTOS_POR_SERIE_DE_REACTIVACION no frenó los dos envíos sin cerrar"
    )

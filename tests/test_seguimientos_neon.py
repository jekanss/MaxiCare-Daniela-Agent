"""La cola de recordatorios contra Neon. Solo con `-m neon`."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.calendario import ZONA_BOGOTA
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_seguimientos"

TEL = "573001112299"
# Un número DISTINTO de `TEL`. El fixture `_limpio` ya borra `contactos` entre pruebas, así
# que reusarlo no contaminaría nada; se mantiene separado porque el nombre dice qué se está
# probando --un teléfono que NUNCA tuvo fila-- y eso se pierde si los dos casos comparten
# número y solo los distingue el orden de las llamadas.
TEL_SIN_CONTACTO = "573001112298"


def _url_de_pruebas() -> str:
    """La URL de Neon, sin pooler y apuntada al esquema de pruebas de este archivo.

    Copiado de `tests/test_tools_neon.py::_url_de_pruebas`: el pooler de Neon rechaza
    `options` como parámetro de arranque, y cada archivo de la suite de Neon usa su propio
    esquema para que un `DROP SCHEMA` de una prueba no le borre las tablas a otra.
    """
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
    """Cada prueba arranca con la cola vacía. Mismo patrón que `test_contacto_neon.py`.

    No es higiene: `seguimientos_por_despachar` trae `limite=50` por defecto, y sin limpiar,
    este esquema acumula una fila vencida por prueba y por corrida. El día que pase de 50,
    las dos pruebas que hacen `next(f for f in filas if ...)` empiezan a dar `StopIteration`
    de forma intermitente --la fila que buscan se queda fuera de la página-- y nadie lo va a
    atribuir a esto.

    `contactos` se limpia también, y su bitácora antes por la FK: la baja que una prueba le
    pone a un número se la encontraría puesta la siguiente. `conversaciones` no se borra
    --media docena de tablas cuelgan de ella-- pero sus dos columnas de recordatorio sí se
    blanquean, que es lo que lee `ultimo_recordatorio` por teléfono: sin eso, la prueba que
    empieza afirmando `ultimo_recordatorio(...) is None` solo pasa mientras siga corriendo
    antes que las que anotan.
    """
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seguimientos")
            cur.execute("DELETE FROM consentimientos")
            cur.execute("DELETE FROM contactos")
            cur.execute(
                "UPDATE conversaciones"
                "   SET ultimo_recordatorio_tipo = NULL, ultimo_recordatorio_en = NULL"
                " WHERE ultimo_recordatorio_tipo IS NOT NULL"
            )
        conn.commit()
    yield


@pytest.fixture
def conexion_pruebas(esquema):
    """Una conexión abierta contra el esquema de pruebas, cerrada al terminar."""
    with persistencia.conectar(esquema) as conn:
        yield conn


@pytest.fixture
def cita_de_prueba(esquema):
    """Una conversación y una cita ya creadas en el esquema de pruebas.

    Copiado del montaje que ya usa `tests/test_tools_neon.py` para crear citas
    (`asegurar_paciente` + `asegurar_conversacion` + `registrar_cita`, directos y sin pasar
    por las tools) -- aquí no hace falta jornada ni disponibilidad, solo una fila de verdad
    de la que colgar un seguimiento.
    """
    telefono = "573000000099"
    with persistencia.conectar(esquema) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo="Paciente De Seguimiento", telefono=telefono
        )
        id_conversacion = persistencia.asegurar_conversacion(
            conn, telefono=telefono, paciente_id=id_paciente
        )
        id_cita = persistencia.registrar_cita(
            conn,
            reserva_id=None,
            conversacion_id=id_conversacion,
            paciente_id=id_paciente,
            nombre_completo="Paciente De Seguimiento",
            telefono=telefono,
            tratamiento="limpieza",
            inicio=datetime(2026, 9, 16, 10, 0, tzinfo=ZONA_BOGOTA),
            duracion_minutos=60,
            evento_calendar_id=None,
        )
    return id_cita, id_conversacion


def test_la_017_deja_las_columnas_y_las_perillas(conexion_pruebas):
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'seguimientos' AND table_schema = current_schema()
            """
        )
        columnas = {fila[0] for fila in cur.fetchall()}

    assert {"cita_id", "anulado_en", "motivo_anulacion", "intentos", "fallo"} <= columnas

    operativa = persistencia.leer_configuracion(conexion_pruebas)
    assert operativa["hora_recordatorio_vispera"] == 18
    assert operativa["horas_minimas_para_recordar"] == 4


def test_lo_que_se_anota_en_la_conversacion_se_vuelve_a_leer(conexion_pruebas, cita_de_prueba):
    """El viaje de ida y vuelta de las dos columnas que la 017 le puso a `conversaciones`.

    `anotar_recordatorio_en_conversacion` escribe y `ultimo_recordatorio` lee, y de esa
    lectura sale el campo del contexto que le dice a Daniela a qué contesta un «sí, confirmo»
    que no tiene antecedente en el historial. Ninguna prueba offline la alcanza --todas doblan
    `persistencia`--, así que un nombre de columna mal escrito no se vería hasta producción, y
    allí se vería como lo peor posible: `atencion._leer_estado` reventando para TODOS los
    pacientes a la vez, con el mensaje de emergencia como única respuesta de la clínica.

    Antes de anotar nada la respuesta es `None`, y ese caso es el normal: la inmensa mayoría
    de las conversaciones nunca recibe un recordatorio.
    """
    _, id_conversacion = cita_de_prueba
    telefono = "573000000099"
    cuando = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)

    assert persistencia.ultimo_recordatorio(conexion_pruebas, telefono) is None

    persistencia.anotar_recordatorio_en_conversacion(
        conexion_pruebas, id_conversacion, tipo="recordatorio_cita", cuando=cuando
    )

    leido = persistencia.ultimo_recordatorio(conexion_pruebas, telefono)
    assert leido is not None
    assert leido[0] == "recordatorio_cita"
    # La columna es `TIMESTAMPTZ`: vuelve en UTC, y lo que tiene que coincidir es el INSTANTE.
    assert leido[1] == cuando


def test_el_recordatorio_se_lee_desde_la_conversacion_SIGUIENTE(
    conexion_pruebas, cita_de_prueba
):
    """El caso mayoritario, y el que la ida y vuelta sobre un mismo id no podía ver.

    El despachador anota en la conversación que CREÓ la cita. `atencion._leer_estado` lee la
    conversación VIVA, y `conversacion_viva` tiene una ventana de 24 h: un recordatorio de
    víspera sale, por definición de su banda, más de 24 h después de esa conversación. Cuando
    el paciente contesta «sí, confirmo» ya está en una conversación NUEVA, así que la lectura
    por `id_conversacion` devolvía `None` y el bloque «YA LE ESCRIBIMOS NOSOTROS» no se emitía
    jamás para la banda mayoritaria.

    Aquí se monta exactamente eso: se anota en la conversación vieja, se abre otra para el
    mismo número --`asegurar_conversacion` SIEMPRE inserta, pese al nombre-- y se comprueba
    que la lectura sigue encontrando el recordatorio.
    """
    _, id_conversacion = cita_de_prueba
    telefono = "573000000099"
    cuando = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)

    persistencia.anotar_recordatorio_en_conversacion(
        conexion_pruebas, id_conversacion, tipo="recordatorio_cita", cuando=cuando
    )

    otra = persistencia.asegurar_conversacion(
        conexion_pruebas, telefono=telefono, paciente_id=None
    )
    assert otra != id_conversacion

    leido = persistencia.ultimo_recordatorio(conexion_pruebas, telefono)
    assert leido is not None, "el recordatorio se perdió al abrirse la conversación siguiente"
    assert leido[0] == "recordatorio_cita"
    assert leido[1] == cuando

    # Y con dos anotados, el que vale es el ÚLTIMO: es lo que hace el `ORDER BY ... DESC`.
    despues = cuando + timedelta(days=1)
    persistencia.anotar_recordatorio_en_conversacion(
        conexion_pruebas, otra, tipo="reactivacion_cancelacion", cuando=despues
    )
    leido = persistencia.ultimo_recordatorio(conexion_pruebas, telefono)
    assert leido == ("reactivacion_cancelacion", despues)


def test_la_anulacion_perdona_la_fila_que_acaba_de_programarse(conexion_pruebas, cita_de_prueba):
    """`excepto_clave` contra Postgres de verdad, que es el único sitio donde se puede ver.

    Las pruebas offline de la cascada doblan `anular_seguimientos_de_cita` con un diccionario,
    así que el `WHERE ... AND clave_idempotencia IS DISTINCT FROM %s` --que se arma con un
    f-string y cambia de forma según venga o no el parámetro-- no lo ejecuta nadie más. Un
    error de sintaxis ahí no se vería hasta que un paciente reprogramara en producción.

    Las dos mitades: la fila perdonada sigue viva, y TODAS las demás de esa cita se anulan.
    """
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)
    perdonada = f"{id_conversacion}:recordatorio:perdonada"
    vieja = f"{id_conversacion}:recordatorio:vieja"

    for clave in (vieja, perdonada):
        persistencia.insertar_seguimiento(
            conexion_pruebas,
            id_conversacion=id_conversacion,
            tipo="recordatorio_cita",
            fecha_objetivo=objetivo,
            clave_idempotencia=clave,
            cita_id=id_cita,
        )

    anulados = persistencia.anular_seguimientos_de_cita(
        conexion_pruebas, id_cita, motivo="cita_reprogramada", excepto_clave=perdonada
    )

    assert anulados == 1, "o se anularon las dos, o no se anuló ninguna"
    # Por SQL directo: `seguimientos_por_despachar` no devuelve `clave_idempotencia`, y lo que
    # hay que distinguir aquí es exactamente CUÁL de las dos filas quedó viva.
    with conexion_pruebas.cursor() as cur:
        cur.execute(
            "SELECT clave_idempotencia FROM seguimientos "
            " WHERE cita_id = %s AND enviado_en IS NULL AND anulado_en IS NULL",
            (id_cita,),
        )
        vivas = {fila[0] for fila in cur.fetchall()}

    assert vivas == {perdonada}


def test_la_cascada_anula_el_recordatorio_de_una_cita_que_se_movio(conexion_pruebas, cita_de_prueba):
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)

    assert persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="recordatorio_cita",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:recordatorio:1",
        cita_id=id_cita,
    )

    anulados = persistencia.anular_seguimientos_de_cita(
        conexion_pruebas, id_cita, motivo="cita_reprogramada"
    )
    assert anulados == 1

    pendientes = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )
    assert [p for p in pendientes if p["cita_id"] == id_cita] == []


def test_un_seguimiento_anulado_no_vuelve_a_la_cola(conexion_pruebas, cita_de_prueba):
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="recordatorio_cita",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:recordatorio:2",
        cita_id=id_cita,
    )
    pendiente = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )[0]

    persistencia.anular_seguimiento(conexion_pruebas, pendiente["id"], motivo="contacto_reciente")

    restantes = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )
    assert pendiente["id"] not in {r["id"] for r in restantes}


def test_aplazar_fija_aplazado_desde_una_sola_vez(conexion_pruebas, cita_de_prueba):
    """El SQL de `aplazar_seguimiento` (migración 022) contra Postgres de verdad -- ninguna
    prueba offline lo ejercita, porque las que tocan `despachar` doblan esta función con un
    diccionario (`tests/test_seguimientos.py`).

    Las dos mitades que R3bis necesita para no repetir el error de la ronda 1: el PRIMER
    aplazamiento pasa `aplazado_desde` de `NULL` a un instante real, y un SEGUNDO
    aplazamiento -simulando otra vuelta de G4 o G5- mueve `fecha_objetivo` otra vez pero deja
    `aplazado_desde` INTACTO. Si el `COALESCE` se cambiara por `now()` a secas, esta prueba
    fallaría en la segunda mitad y R3bis volvería a quedar tan ciega como R3.
    """
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="reactivacion_sin_agendar",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:aplazar:1",
    )
    pendiente = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )[0]
    assert pendiente["aplazado_desde"] is None, "una fila recien creada no se ha aplazado nunca"

    primer_aplazamiento = objetivo + timedelta(minutes=30)
    persistencia.aplazar_seguimiento(conexion_pruebas, pendiente["id"], hasta=primer_aplazamiento)

    tras_el_primero = next(
        f
        for f in persistencia.seguimientos_por_despachar(
            conexion_pruebas, ahora=primer_aplazamiento + timedelta(hours=1)
        )
        if f["id"] == pendiente["id"]
    )
    assert tras_el_primero["fecha_objetivo"] == primer_aplazamiento
    assert tras_el_primero["aplazado_desde"] is not None
    primera_marca = tras_el_primero["aplazado_desde"]

    segundo_aplazamiento = primer_aplazamiento + timedelta(hours=2)
    persistencia.aplazar_seguimiento(conexion_pruebas, pendiente["id"], hasta=segundo_aplazamiento)

    tras_el_segundo = next(
        f
        for f in persistencia.seguimientos_por_despachar(
            conexion_pruebas, ahora=segundo_aplazamiento + timedelta(hours=1)
        )
        if f["id"] == pendiente["id"]
    )
    assert tras_el_segundo["fecha_objetivo"] == segundo_aplazamiento
    assert tras_el_segundo["aplazado_desde"] == primera_marca, (
        "el segundo aplazamiento no puede reescribir `aplazado_desde`"
    )


def test_la_cola_trae_lo_que_el_despachador_necesita_para_decidir(conexion_pruebas, cita_de_prueba):
    id_cita, id_conversacion = cita_de_prueba
    objetivo = datetime(2026, 9, 16, 18, 0, tzinfo=ZONA_BOGOTA)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conversacion,
        tipo="recordatorio_cita",
        fecha_objetivo=objetivo,
        clave_idempotencia=f"{id_conversacion}:recordatorio:3",
        cita_id=id_cita,
    )

    fila = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=objetivo + timedelta(hours=1)
    )[0]

    # Sin estas claves el despachador tendría que hacer una consulta por guarda.
    #
    # `seguimientos_fallidos` y `reactivaciones_ultimo_ano` se sumaron en la ronda 1 de
    # revisión de la tarea 3 (hallazgo 2); `aplazado_desde` llegó en la ronda 2 (R3bis cambió
    # de ancla: `creado_en` medía la edad total de la fila y no cuánto llevaba atascada, y
    # anulaba en silencio una reactivación programada a semanas vista que llegaba puntual).
    # Antes de esos dos hallazgos, borrar cualquiera de estas columnas del SELECT dejaba esta
    # prueba en verde -- las guardas R1/R2/R3bis siguen leyendo `.get(...)` o revientan de
    # otra forma, así que la ausencia no se nota aquí, solo en producción, en silencio.
    assert set(fila) >= {
        "id", "conversacion_id", "cita_id", "tipo", "fecha_objetivo", "intentos",
        "telefono", "nombre_completo", "tratamiento", "cita_inicio", "cita_estado",
        "tomada_por", "seguimientos_fallidos", "reactivaciones_ultimo_ano", "aplazado_desde",
    }


TEL_TOPE_ANUAL = "573001112295"


def test_el_tope_anual_cuenta_reactivaciones_enviadas_y_excluye_el_recordatorio(
    conexion_pruebas,
):
    """La subconsulta de `reactivaciones_ultimo_ano` (hallazgo 2 de la ronda 1 de revisión):
    ninguna prueba offline la ejercita, así que un error de SQL -o una exclusión que se
    caiga- solo revienta o miente aquí. `-m neon` pasando no demuestra que cuenta bien; esta
    prueba sí, porque siembra un caso donde contar mal es observable.

    Dos reactivaciones y UN recordatorio de cita, los tres YA ENVIADOS para el MISMO
    teléfono: si la exclusión de `recordatorio_cita` se cae, R2 (el tope anual) empezaría a
    contar recordatorios como si fueran publicidad, y a alguien con muchas citas legítimas se
    le apagaría la reactivación sin que hubiera recibido ni una.
    """
    id_conv = persistencia.asegurar_conversacion(
        conexion_pruebas, telefono=TEL_TOPE_ANUAL, paciente_id=None, canal="whatsapp"
    )
    hace_un_mes = datetime.now(ZONA_BOGOTA) - timedelta(days=30)

    tipos_enviados = ["reactivacion_sin_agendar", "reactivacion_cancelada", "recordatorio_cita"]
    for i, tipo in enumerate(tipos_enviados):
        persistencia.insertar_seguimiento(
            conexion_pruebas,
            id_conversacion=id_conv,
            tipo=tipo,
            fecha_objetivo=hace_un_mes,
            clave_idempotencia=f"tope-anual-{i}",
        )

    # Hay que marcarlos ENVIADOS de verdad: la subconsulta cuenta sobre `enviado_en IS NOT
    # NULL`, y `seguimientos_por_despachar` es también cómo se consiguen los ids sin duplicar
    # el SQL de insertar_seguimiento a mano.
    pendientes = persistencia.seguimientos_por_despachar(conexion_pruebas, ahora=hace_un_mes)
    ids_sembrados = [f["id"] for f in pendientes if str(f["conversacion_id"]) == id_conv]
    assert len(ids_sembrados) == 3
    for id_seguimiento in ids_sembrados:
        assert persistencia.marcar_seguimiento_enviado(conexion_pruebas, id_seguimiento)

    # Una cuarta fila, PENDIENTE: las tres de arriba ya tienen `enviado_en` y no volverían en
    # el SELECT, así que hace falta una viva para poder leer la cuenta desde aquí.
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conv,
        tipo="reactivacion_sin_agendar",
        fecha_objetivo=datetime.now(ZONA_BOGOTA),
        clave_idempotencia="tope-anual-pendiente",
    )

    filas = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=datetime.now(ZONA_BOGOTA)
    )
    fila = next(f for f in filas if str(f["conversacion_id"]) == id_conv)

    assert fila["reactivaciones_ultimo_ano"] == 2


def test_la_consulta_trae_la_baja_del_contacto(conexion_pruebas):
    """La comprobación entra como una columna del SELECT que ya hace LEFT JOIN para sacar el
    teléfono, y no como una consulta por fila: una tanda son hasta 50."""
    id_conv = persistencia.asegurar_conversacion(
        conexion_pruebas, telefono=TEL, paciente_id=None, canal="whatsapp"
    )
    ayer = datetime.now(ZONA_BOGOTA) - timedelta(days=1)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conv,
        # `reactivacion_sin_agendar` y no `reactivacion` a secas: el CHECK
        # `ck_seguimientos_tipo` de la migración 021 cierra el vocabulario de `tipo`, y un
        # valor fuera de la lista revienta el INSERT. Esta prueba no ejercita el tipo --lo que
        # comprueba es la columna `no_contactar`--, así que cualquier tipo válido sirve.
        tipo="reactivacion_sin_agendar",
        fecha_objetivo=ayer,
        clave_idempotencia="baja-1",
    )
    persistencia.asegurar_contacto(conexion_pruebas, TEL)
    persistencia.pedir_baja(conexion_pruebas, TEL)

    filas = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=datetime.now(ZONA_BOGOTA)
    )
    # Filtrado por `conversacion_id` y no `[0]`: este archivo no limpia `seguimientos` entre
    # pruebas, así que la cola trae también lo que dejaron vivo las pruebas anteriores.
    # `str(...)`: psycopg devuelve la columna UUID como `uuid.UUID`, e `id_conv` es un `str`.
    fila = next(f for f in filas if str(f["conversacion_id"]) == id_conv)

    assert fila["no_contactar"] is True


def test_un_telefono_sin_fila_de_contacto_no_cuenta_como_baja(conexion_pruebas):
    """El LEFT JOIN devuelve NULL, y NULL no es TRUE. El COALESCE lo hace explícito para que
    nadie tenga que acordarse de esto al leer la guarda."""
    id_conv = persistencia.asegurar_conversacion(
        conexion_pruebas, telefono=TEL_SIN_CONTACTO, paciente_id=None, canal="whatsapp"
    )
    ayer = datetime.now(ZONA_BOGOTA) - timedelta(days=1)
    persistencia.insertar_seguimiento(
        conexion_pruebas,
        id_conversacion=id_conv,
        # Mismo motivo que arriba: el CHECK de la 021 exige un tipo del vocabulario cerrado.
        tipo="reactivacion_sin_agendar",
        fecha_objetivo=ayer,
        clave_idempotencia="sin-contacto-1",
    )

    filas = persistencia.seguimientos_por_despachar(
        conexion_pruebas, ahora=datetime.now(ZONA_BOGOTA)
    )
    fila = next(f for f in filas if str(f["conversacion_id"]) == id_conv)

    assert fila["no_contactar"] is False

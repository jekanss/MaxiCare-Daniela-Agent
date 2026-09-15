"""La garantía de `/clearstate`, contra Neon de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Aquí viven las dos preguntas que solo la base puede contestar:

1. **¿El borrado deja al número como uno que nunca ha escrito?** Se contesta comparando
   `atencion._leer_estado` --la única función de la que sale todo lo que Daniela sabe de
   alguien al empezar un turno-- para un número reseteado y para uno virgen. Campo a campo.
2. **¿El orden de los DELETE es el que las claves foráneas permiten?** No se puede razonar
   en una prueba con dobles: `citas`, `reservas` y `mensajes_entrantes` apuntan a
   `conversaciones` SIN cascade, y `conversaciones.paciente_id` apunta a `pacientes` SIN
   cascade. Quien decide si el orden vale es Postgres.

Escriben en el esquema `pruebas_reseteo`, que se crea y se borra aquí. Nunca `public`: ahí
viven los pacientes reales de la clínica, y un borrado mal escrito no se deshace.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from maxicare_daniela import atencion, persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_reseteo"

#: El que se resetea, el que nunca ha escrito y el vecino que NO se puede tocar.
TEL = "573001110001"
TEL_VIRGEN = "573001110002"
TEL_VECINO = "573001110003"


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
def esquema():
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    url = _url_de_pruebas()
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
        # El borrado se COMPRUEBA. Un esquema de pruebas que se queda en la base de la
        # clínica porque el DROP falló en silencio es basura que nadie vuelve a mirar.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (ESQUEMA,),
            )
            assert cur.fetchone() is None, f"el esquema {ESQUEMA} no se borró"


#: Todas las tablas que cuelgan de un teléfono, de la hoja a la raíz.
TABLAS_CON_PACIENTES = (
    "mensajes_entrantes",
    "citas",
    "reservas",
    "escalamientos",
    "seguimientos",
    "notas_archivo",
    "estado_oportunidad",
    "conversaciones",
    "pacientes",
)

#: `temas_telegram` va aparte de la tupla de arriba: no tiene columna `telefono`... la tiene,
#: pero no cuelga de `conversaciones` ni de `pacientes`, así que ningún CASCADE se la lleva.
#: Que `borrar_rastro` la borre a mano es justo lo que hay que comprobar -- una fila que
#: sobreviviera apuntaría a un tema ya borrado en Telegram y el siguiente archivo de ese
#: número moriría con «message thread not found».
TABLA_DEL_HILO = "temas_telegram"

#: Tampoco cuelga de un teléfono: un caso es historia de la clínica y sobrevive al reseteo.
#: Lo único que `/clearstate` le quita son las frases de ese número, dentro de `ejemplos`.
#: Se trunca aquí igual, para que una prueba no herede los casos de la anterior.
TABLA_DE_CASOS = "casos_sin_resolver"


@pytest.fixture(autouse=True)
def limpio(esquema):
    """Deja el esquema vacío antes de cada prueba, con SQL crudo.

    A propósito NO usa `borrar_rastro`: una prueba que se prepara con la misma función que
    verifica no prueba nada -- si la función no borrara, el montaje tampoco limpiaría y el
    fallo se cancelaría solo.
    """
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            f"TRUNCATE {', '.join((*TABLAS_CON_PACIENTES, TABLA_DEL_HILO, TABLA_DE_CASOS))} "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    return esquema


# ==========================================================================================
# Fabricar un número con historia completa
# ==========================================================================================


def _con_historia(url: str, telefono: str, *, wamids: list[str]) -> dict:
    """Deja en la base un número con TODO lo que un paciente real acumula.

    Una tabla por tabla de las nueve que cuelgan de un teléfono. Si el borrado se olvida de
    una, las pruebas de abajo lo ven.
    """
    ahora = datetime.now(timezone.utc)
    id_conversacion = str(uuid.uuid4())
    marca = telefono[-4:]

    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pacientes (nombre_completo, telefono)
            VALUES (%s, %s) RETURNING id
            """,
            (f"Paciente {marca}", telefono),
        )
        id_paciente = cur.fetchone()[0]

        # El hilo de Telegram ya no es una columna de `pacientes`: desde la migración 014
        # vive en `temas_telegram`, atado al teléfono. Sigue teniendo que desaparecer con el
        # reseteo, y eso es lo que comprueba este archivo.
        cur.execute(
            "INSERT INTO temas_telegram (telefono, topic_id, abierto) VALUES (%s, %s, FALSE)",
            (telefono, 4000 + int(marca)),
        )

        cur.execute(
            """
            INSERT INTO conversaciones (id, paciente_id, telefono, canal,
                                        identidad_verificada, intentos_identificacion,
                                        turno_actual)
            VALUES (%s, %s, %s, 'whatsapp', TRUE, 2, 7)
            """,
            (id_conversacion, id_paciente, telefono),
        )

        cur.execute(
            """
            INSERT INTO estado_oportunidad (conversacion_id, estado, barrera, notas)
            VALUES (%s, 'evaluando', 'precio', %s)
            """,
            (id_conversacion, f"quiere ortodoncia, le preocupa el precio ({marca})"),
        )

        cur.execute(
            """
            INSERT INTO notas_archivo (conversacion_id, tipo_documento, tratamiento)
            VALUES (%s, 'radiografia', 'ortodoncia')
            """,
            (id_conversacion,),
        )

        cur.execute(
            """
            INSERT INTO reservas (inicio, cupo_num, conversacion_id, clave_idempotencia)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (ahora + timedelta(days=3), int(marca) % 9 + 1, id_conversacion, f"res-{marca}"),
        )
        id_reserva = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO citas (id, reserva_id, conversacion_id, paciente_id, nombre_completo,
                               telefono, tratamiento, inicio, duracion_minutos,
                               evento_calendar_id, estado)
            VALUES (%s, %s, %s, %s, %s, %s, 'ortodoncia', %s, 60, %s, 'confirmada')
            """,
            (
                str(uuid.uuid4()),
                id_reserva,
                id_conversacion,
                id_paciente,
                f"Paciente {marca}",
                telefono,
                ahora + timedelta(days=3),
                f"evento-{marca}",
            ),
        )

        cur.execute(
            """
            INSERT INTO seguimientos (conversacion_id, tipo, fecha_objetivo,
                                      clave_idempotencia)
            VALUES (%s, 'recordatorio', %s, %s)
            """,
            (id_conversacion, ahora + timedelta(days=2), f"seg-{marca}"),
        )

        cur.execute(
            """
            INSERT INTO escalamientos (conversacion_id, motivo, resumen, pregunta,
                                       clave_idempotencia, telegram_message_id)
            VALUES (%s, 'dolor', %s, '¿lo vemos hoy?', %s, %s)
            """,
            (
                id_conversacion,
                f"Paciente {marca} reporta dolor agudo",
                f"esc-{marca}",
                9000 + int(marca),
            ),
        )

        for i, wamid in enumerate(wamids):
            cur.execute(
                """
                INSERT INTO mensajes_entrantes (wamid, telefono, nombre_perfil, tipo, texto,
                                                conversacion_id, telegram_message_id)
                VALUES (%s, %s, %s, 'text', %s, %s, %s)
                """,
                (wamid, telefono, f"Perfil {marca}", f"mensaje {i}", id_conversacion, 500 + i),
            )
        conn.commit()

    return {"id_paciente": id_paciente, "id_conversacion": id_conversacion}


def _campos_comparables(estado) -> dict:
    """Todo `_Estado` menos `id_conversacion`, que es un UUID nuevo por definición."""
    return {
        "turno_actual": estado.turno_actual,
        "identidad_verificada": estado.identidad_verificada,
        "intentos_identificacion": estado.intentos_identificacion,
        "id_paciente": estado.id_paciente,
        "nombre_paciente": estado.nombre_paciente,
        "tomada_por": estado.tomada_por,
        "operativa": estado.operativa,
    }


def _cuenta(url: str, tabla: str, donde: str, valor) -> int:
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {tabla} WHERE {donde} = %s", (valor,))
        return cur.fetchone()[0]


# ==========================================================================================
# LA GARANTÍA
# ==========================================================================================


@pytest.mark.neon
def test_tras_el_reset_daniela_ve_lo_mismo_que_en_un_primer_contacto(esquema):
    """El entregable de este comando, y la razón por la que existe.

    No compara filas: compara lo ÚNICO que Daniela mira al empezar un turno. Si los ocho
    campos coinciden con los de un número que nunca ha escrito, el turno siguiente es, para
    el modelo, indistinguible de un primer contacto.
    """
    wamids = [f"wamid-hist-{i}" for i in range(3)]
    _con_historia(esquema, TEL, wamids=wamids)

    # El control: antes del reset, Daniela SÍ lo conoce. Sin esta aserción la prueba pasaría
    # aunque el fabricante de historia no hubiera escrito nada.
    antes = atencion._leer_estado(esquema, TEL, [])
    assert antes.identidad_verificada is True
    assert antes.nombre_paciente is not None
    assert antes.turno_actual == 7

    with persistencia.conectar(esquema) as conn:
        persistencia.borrar_rastro(conn, TEL)

    virgen = atencion._leer_estado(esquema, TEL_VIRGEN, [])
    despues = atencion._leer_estado(esquema, TEL, [])

    assert _campos_comparables(despues) == _campos_comparables(virgen)
    # Y la conversación es otra: la sesión del modelo se indexa por este id, así que un id
    # nuevo es una sesión vacía sin tener que limpiar memoria.
    assert despues.id_conversacion != antes.id_conversacion


@pytest.mark.neon
def test_no_queda_una_sola_fila_del_numero(esquema):
    """Tabla por tabla, incluidas las que borra la cascada de `conversaciones`."""
    wamids = [f"wamid-filas-{i}" for i in range(2)]
    creado = _con_historia(esquema, TEL, wamids=wamids)
    conv = creado["id_conversacion"]

    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, TEL)

    assert _cuenta(esquema, "pacientes", "telefono", TEL) == 0
    assert _cuenta(esquema, "conversaciones", "telefono", TEL) == 0
    assert _cuenta(esquema, "mensajes_entrantes", "telefono", TEL) == 0
    assert _cuenta(esquema, "citas", "telefono", TEL) == 0
    assert _cuenta(esquema, "reservas", "conversacion_id", conv) == 0
    # Las que se van por ON DELETE CASCADE. Se comprueban igual: la cascada es una promesa
    # del esquema, y una promesa que nadie comprueba es una suposición.
    assert _cuenta(esquema, "estado_oportunidad", "conversacion_id", conv) == 0
    assert _cuenta(esquema, "notas_archivo", "conversacion_id", conv) == 0
    assert _cuenta(esquema, "seguimientos", "conversacion_id", conv) == 0
    assert _cuenta(esquema, "escalamientos", "conversacion_id", conv) == 0

    assert borradas["pacientes"] == 1
    assert borradas["conversaciones"] == 1
    # El hilo de Telegram. Desde la migración 014 no cuelga de nada, así que si
    # `borrar_rastro` dejara de borrarlo a mano, esta línea es lo único que lo diría.
    assert _cuenta(esquema, "temas_telegram", "telefono", TEL) == 0
    assert borradas["temas_telegram"] == 1
    assert borradas["mensajes_entrantes"] == 2
    assert borradas["citas"] == 1


@pytest.mark.neon
def test_el_vecino_no_se_toca(esquema):
    """La prueba que impide que esto sea un desastre.

    Un `DELETE` sin `WHERE` correcto pasa todas las pruebas de arriba: borrarlo todo también
    deja al número como virgen. Lo que distingue un borrado correcto de uno catastrófico es
    que el de al lado siga intacto.
    """
    _con_historia(esquema, TEL, wamids=["wamid-mio-1"])
    vecino = _con_historia(esquema, TEL_VECINO, wamids=["wamid-vecino-1"])

    with persistencia.conectar(esquema) as conn:
        persistencia.borrar_rastro(conn, TEL)

    assert _cuenta(esquema, "pacientes", "telefono", TEL_VECINO) == 1
    assert _cuenta(esquema, "conversaciones", "telefono", TEL_VECINO) == 1
    assert _cuenta(esquema, "mensajes_entrantes", "telefono", TEL_VECINO) == 1
    assert _cuenta(esquema, "citas", "telefono", TEL_VECINO) == 1
    assert _cuenta(esquema, "escalamientos", "conversacion_id", vecino["id_conversacion"]) == 1

    estado = atencion._leer_estado(esquema, TEL_VECINO, [])
    assert estado.identidad_verificada is True
    assert estado.turno_actual == 7


@pytest.mark.neon
def test_conserva_el_wamid_del_propio_comando(esquema):
    """Si se borrara, un reintento de Meta ejecutaría el comando dos veces.

    La fila se queda, pero con `conversacion_id` en NULL: apuntar a una conversación borrada
    viola la clave foránea, y dejarla apuntando a nada es exactamente lo que es.
    """
    _con_historia(esquema, TEL, wamids=["wamid-viejo"])
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, conversacion_id)
            SELECT %s, %s, 'text', '/clearstate', id FROM conversaciones WHERE telefono = %s
            """,
            ("wamid-comando", TEL, TEL),
        )
        conn.commit()

    with persistencia.conectar(esquema) as conn:
        persistencia.borrar_rastro(conn, TEL, conservar_wamid="wamid-comando")

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT conversacion_id FROM mensajes_entrantes WHERE wamid = %s",
            ("wamid-comando",),
        )
        fila = cur.fetchone()
    assert fila is not None, "se borró la fila que protege contra el reintento de Meta"
    assert fila[0] is None
    assert _cuenta(esquema, "mensajes_entrantes", "wamid", "wamid-viejo") == 0

    # Y la fila conservada no vuelve conocido a nadie: `_leer_estado` no mira esta tabla.
    virgen = atencion._leer_estado(esquema, TEL_VIRGEN, [])
    assert _campos_comparables(atencion._leer_estado(esquema, TEL, [])) == _campos_comparables(
        virgen
    )


@pytest.mark.neon
def test_borrar_un_numero_que_no_existe_no_falla(esquema):
    """Se va a escribir `/clearstate` dos veces seguidas. La segunda no puede reventar."""
    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, "573009998888")
    assert sum(borradas.values()) == 0


@pytest.mark.neon
def test_clearstate_borra_el_historial_del_agente(esquema):
    """Sin esto, `/clearstate` deja de cumplir lo que promete y nadie se entera: el paciente
    vuelve a primer contacto con Daniela recordando lo de antes.

    Es la clase de fallo que solo se ve probándolo, porque la garantía del módulo --que
    `_leer_estado` devuelva los mismos ocho campos-- se sigue cumpliendo igual.
    """
    telefono = "573001112233"

    with persistencia.conectar(esquema) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo="Ana Prueba", telefono=telefono
        )
        id_conversacion = persistencia.asegurar_conversacion(
            conn, telefono=telefono, paciente_id=id_paciente
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_sessions (session_id) VALUES (%s)", (id_conversacion,)
            )
            cur.execute(
                "INSERT INTO agent_messages (session_id, message_data) VALUES (%s, %s)",
                (id_conversacion, '{"role":"user","content":"me llamo Ana"}'),
            )
        conn.commit()

    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, telefono)

    assert borradas["agent_sessions"] == 1

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agent_messages WHERE session_id = %s", (id_conversacion,)
        )
        assert cur.fetchone()[0] == 0, "quedó historial de un número que se reseteó"


@pytest.mark.neon
def test_el_rastro_dice_que_hay_que_borrar_afuera(esquema):
    """Antes de tocar las filas hay que saber qué eliminar en Calendar y en Telegram: una vez
    borrada la fila, el `evento_calendar_id` ya no existe y el evento queda huérfano."""
    wamids = ["wamid-rastro-1"]
    _con_historia(esquema, TEL, wamids=wamids)

    with persistencia.conectar(esquema) as conn:
        rastro = persistencia.rastro_de(conn, TEL)

    assert rastro["eventos"] == ["evento-0001"]
    assert rastro["topic_id"] == 4001
    assert 9001 in rastro["mensajes_telegram"]
    assert 500 in rastro["mensajes_telegram"]


# ==========================================================================================
# El informe de «sin resolver»: la frase se va, el caso se queda
#
# `casos_sin_resolver` es la única tabla que `/clearstate` NO vacía: un caso es historia de
# la clínica --«doce personas preguntaron el precio de la ortodoncia»-- y sigue siendo cierto
# aunque una de esas doce se borre. Lo que se va es la frase textual del paciente, que es lo
# único suyo que hay ahí dentro.
# ==========================================================================================


def _caso_con_ejemplos(url: str, huella: str, ejemplos: list[tuple[str, str]]) -> None:
    """Deja un caso con esas `(frase, telefono)`, una por llamada, como en producción."""
    with persistencia.conectar(url) as conn:
        for texto, telefono in ejemplos:
            persistencia.registrar_caso(
                conn, huella=huella, tipo="FALTA_DATO", ejemplo=texto, telefono=telefono
            )


def _caso(url: str, huella: str) -> dict:
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT contador, ejemplos FROM casos_sin_resolver WHERE huella = %s", (huella,)
        )
        contador, ejemplos = cur.fetchone()
    return {"contador": contador, "ejemplos": json.loads(ejemplos)}


@pytest.mark.neon
def test_clearstate_se_lleva_la_frase_del_numero_y_deja_la_del_vecino(esquema):
    """Es lo único del caso que es del paciente. Si sobreviviera, `/clearstate` prometería un
    borrado completo mientras una frase suya sigue viéndose en la pantalla de la clínica."""
    huella = "falta_dato:ortodoncia:precio"
    _caso_con_ejemplos(
        esquema,
        huella,
        [("cuanto me sale la ortodoncia", TEL), ("y en cuotas?", TEL_VECINO)],
    )

    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, TEL)

    assert borradas["casos_sin_resolver"] == 1
    quedo = _caso(esquema, huella)
    assert [e["texto"] for e in quedo["ejemplos"]] == ["y en cuotas?"]
    assert [e["telefono"] for e in quedo["ejemplos"]] == [TEL_VECINO]


@pytest.mark.neon
def test_el_contador_del_caso_no_baja_al_resetear(esquema):
    """El conteo es historia de la clínica, no dato del paciente. Bajarlo convertiría
    `/clearstate` --un comando de pruebas-- en una forma de falsear el informe."""
    huella = "falta_dato:blanqueamiento:garantia"
    _caso_con_ejemplos(
        esquema, huella, [("la garantia?", TEL), ("cuanto dura?", TEL_VECINO)]
    )
    antes = _caso(esquema, huella)["contador"]

    with persistencia.conectar(esquema) as conn:
        persistencia.borrar_rastro(conn, TEL)

    assert _caso(esquema, huella)["contador"] == antes == 2


@pytest.mark.neon
def test_el_conteo_de_frases_no_lo_infla_un_telefono_que_es_prefijo_de_otro(esquema):
    """Este número lo lee una persona en su WhatsApp, así que tiene que ser el de las filas
    que CAMBIARON. El filtro por `LIKE` contaba las que COINCIDÍAN: `5730011` es prefijo de
    `573001110001`, y el paciente recibía «1 frase tuya» sin que se hubiera borrado ninguna.
    """
    huella = "falta_dato:coronas:precio"
    _caso_con_ejemplos(esquema, huella, [("cuanto vale una corona", TEL)])

    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, TEL[:7])

    assert TEL.startswith(TEL[:7]), "el montaje de la prueba solo vale si es prefijo"
    assert borradas["casos_sin_resolver"] == 0
    assert len(_caso(esquema, huella)["ejemplos"]) == 1, "no se podía tocar nada"


@pytest.mark.neon
def test_un_telefono_escrito_DENTRO_de_la_frase_no_cuenta_como_frase_suya(esquema):
    """El `LIKE` miraba el JSON entero, campo `texto` incluido. Un paciente que escribe un
    número de teléfono en su mensaje hacía que el reseteo de ESE número dijera haber borrado
    una frase que no era suya y que sigue ahí."""
    huella = "falta_dato:_general:sede"
    _caso_con_ejemplos(esquema, huella, [(f"me dijeron que llamara al {TEL}", TEL_VECINO)])

    with persistencia.conectar(esquema) as conn:
        borradas = persistencia.borrar_rastro(conn, TEL)

    assert borradas["casos_sin_resolver"] == 0
    assert len(_caso(esquema, huella)["ejemplos"]) == 1

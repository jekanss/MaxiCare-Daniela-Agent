"""El SQL del relevo (6C), contra Postgres de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Existe por la misma razón que `tests/test_ingesta_neon.py`: la suite offline dobla cada una
de estas funciones con `monkeypatch` --tiene que hacerlo-- y un doble no ejecuta una línea de
SQL. Y aquí hay tres cosas que solo el motor sabe decir:

  · el `WHERE tomada_por IS NULL` que resuelve la carrera de dos doctores. Es la única
    defensa que hay contra que los dos crean tener el hilo, y que funcione no es una promesa
    de Python: es que el UPDATE no encuentre fila;
  · el CHECK cerrado de la migración 003, que tiene que RECHAZAR un motivo inventado --si lo
    aceptara, el estado nuevo se colaría sin pasar por una migración;
  · el `GREATEST(tomada_en, ultimo_mensaje_doctor_en)` del reloj de cierre, que decide si el
    relevo es un cronómetro o un detector de abandono.

Escribe en el esquema `pruebas_relevo`, que se crea y se borra aquí. Nunca `public`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from maxicare_daniela import persistencia, relevo
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_relevo"

TEL = "573001110201"
TEL_VECINO = "573001110202"
TEMA = 4242


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


@pytest.fixture(autouse=True)
def _limpio(esquema):
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            # De la hoja a la raiz: `mensajes_entrantes` y `agent_sessions` apuntan a
            # `conversaciones` (la segunda sin clave foranea, pero su `session_id` ES el id
            # de la conversacion, asi que despues del DELETE no habria a que apuntar).
            cur.execute("DELETE FROM mensajes_entrantes")
            cur.execute("DELETE FROM agent_sessions")
            cur.execute("DELETE FROM citas")
            cur.execute("DELETE FROM reservas")
            cur.execute("DELETE FROM escalamientos")
            cur.execute("DELETE FROM conversaciones")
            cur.execute("DELETE FROM pacientes")
            # `temas_telegram` NO cuelga de ninguna de las anteriores desde la migración 014,
            # así que ningún CASCADE la vacía. Sin esta línea, el hilo de una prueba sobrevive
            # a la siguiente y `relevos_activos` lo encuentra por el `LEFT JOIN`.
            cur.execute("DELETE FROM temas_telegram")
        conn.commit()
    yield


def _conversacion(url: str, telefono: str = TEL, *, con_tema: int | None = None) -> str:
    """Un paciente con su conversación, y opcionalmente con su tema de Telegram."""
    with persistencia.conectar(url) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo="Ana Perez", telefono=telefono
        )
        if con_tema is not None:
            persistencia.guardar_tema(
                conn, telefono=telefono, topic_id=con_tema, abierto=False
            )
        return persistencia.asegurar_conversacion(
            conn, telefono=telefono, paciente_id=id_paciente, canal="whatsapp"
        )


def _atrasar(url: str, id_conversacion: str, *, minutos: int, columna: str) -> None:
    """Envejece una marca de tiempo. Es lo único que permite probar un reloj sin esperarlo."""
    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE conversaciones SET {columna} = now() - make_interval(mins => %s) "
                " WHERE id = %s",
                (minutos, id_conversacion),
            )
        conn.commit()


# ==========================================================================================
# `activar_relevo` — la carrera de dos doctores
# ==========================================================================================


def test_el_primero_que_pulsa_se_la_queda(esquema):
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        assert persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz") is None
        assert persistencia.conversacion_tomada(conn, conv) == "Dra. Ruiz"


def test_el_segundo_no_pisa_al_primero(esquema):
    """El escalamiento suena a la vez en el teléfono de todos los doctores: dos pulsando con
    segundos de diferencia es lo normal. Sin el `WHERE tomada_por IS NULL`, el UPDATE diría
    que sí a los dos y ambos creerían tener el hilo -- con el paciente recibiendo dos
    conversaciones distintas por el mismo WhatsApp.
    """
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")

    with persistencia.conectar(esquema) as conn:
        perdedor = persistencia.activar_relevo(
            conn, id_conversacion=conv, doctor="Dr. Gómez"
        )
        assert perdedor == "Dra. Ruiz"
        assert persistencia.conversacion_tomada(conn, conv) == "Dra. Ruiz"


def test_una_conversacion_que_no_existe_no_revienta(esquema):
    """El `callback_data` puede apuntar a una conversación borrada por `/clearstate`."""
    with persistencia.conectar(esquema) as conn:
        respuesta = persistencia.activar_relevo(
            conn,
            id_conversacion="00000000-0000-0000-0000-000000000000",
            doctor="Dra. Ruiz",
        )
    assert respuesta is not None


def test_activar_limpia_el_cierre_anterior(esquema):
    """El mismo paciente puede volver meses después, y su conversación nueva no puede
    arrastrar el motivo por el que se cerró la de antes."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        persistencia.cerrar_relevo(conn, conv, motivo="tiempo_agotado")
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dr. Gómez")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relevo_motivo_cierre, relevo_cerrado_en FROM conversaciones "
                " WHERE id = %s",
                (conv,),
            )
            motivo, cerrado_en = cur.fetchone()

    assert motivo is None and cerrado_en is None


# ==========================================================================================
# `cerrar_relevo` — la puerta única
# ==========================================================================================


def test_cerrar_devuelve_la_conversacion_y_deja_el_motivo(esquema):
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        assert persistencia.cerrar_relevo(conn, conv, motivo="devuelto_por_doctor") is True
        assert persistencia.conversacion_tomada(conn, conv) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relevo_motivo_cierre FROM conversaciones WHERE id = %s", (conv,)
            )
            assert cur.fetchone()[0] == "devuelto_por_doctor"


def test_cerrar_dos_veces_dice_que_no_la_segunda(esquema):
    """Las salidas se solapan: el doctor pulsa «Listo» en el mismo minuto en que el barrido
    lo da por vencido. El `False` es lo que corta la segunda despedida en el hilo."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        assert persistencia.cerrar_relevo(conn, conv, motivo="tiempo_agotado") is True
        assert persistencia.cerrar_relevo(conn, conv, motivo="tema_perdido") is False


def test_un_motivo_inventado_lo_rechaza_la_base(esquema):
    """El CHECK cerrado de la migración 003 existe para que un estado nuevo pase por una
    migración y no se cuele como un string cualquiera. Si esto dejara de reventar, el CHECK
    se habría perdido en algún `aplicar_esquema` y nadie se enteraría."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")

    with pytest.raises(Exception):
        with persistencia.conectar(esquema) as conn:
            persistencia.cerrar_relevo(conn, conv, motivo="porque_si")


# ==========================================================================================
# `relevo_por_tema` — el camino doctor -> paciente
# ==========================================================================================


def test_del_tema_se_llega_al_telefono(esquema):
    conv = _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        encontrado = persistencia.relevo_por_tema(conn, TEMA)

    assert encontrado == {
        "id_conversacion": conv,
        "telefono": TEL,
        "doctor": "Dra. Ruiz",
        # Lo que decide si el próximo mensaje del doctor es para el paciente, o es de qué es
        # la cita, o es su fecha. `None` es el caso normal: no hay cierre en curso.
        "cierre_pendiente": None,
        # De qué es la cita, ya contestado, mientras se espera la fecha (migración 015).
        "cierre_tratamiento": None,
    }


def test_un_tema_sin_relevo_vivo_no_devuelve_nada(esquema):
    """Es una respuesta legítima y frecuente, no un fallo: alguien escribió en el expediente
    de un paciente sin tener el relevo. Es el hueco del creador del grupo."""
    _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        assert persistencia.relevo_por_tema(conn, TEMA) is None


def test_el_tema_de_otro_paciente_no_devuelve_mi_relevo(esquema):
    conv = _conversacion(esquema, con_tema=TEMA)
    _conversacion(esquema, TEL_VECINO, con_tema=TEMA + 1)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        assert persistencia.relevo_por_tema(conn, TEMA + 1) is None


# ==========================================================================================
# `relevos_activos` — el reloj de cierre
# ==========================================================================================


def test_sin_mensajes_del_doctor_el_reloj_cuenta_desde_la_activacion(esquema):
    conv = _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
    _atrasar(esquema, conv, minutos=90, columna="tomada_en")

    with persistencia.conectar(esquema) as conn:
        (uno,) = persistencia.relevos_activos(conn)

    assert 89 < uno["minutos_callado"] < 92
    assert uno["topic_id"] == TEMA
    assert uno["telefono"] == TEL


def test_un_mensaje_del_doctor_empuja_el_reloj(esquema):
    """El `GREATEST` es la diferencia entre un cronómetro y un detector de abandono. Contando
    desde la activación, un doctor que lleva tres horas hablando se queda cortado a mitad de
    frase -- que es justo cuando una conversación difícil sigue viva."""
    conv = _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
    _atrasar(esquema, conv, minutos=200, columna="tomada_en")

    with persistencia.conectar(esquema) as conn:
        persistencia.tocar_mensaje_doctor(conn, conv)
        (uno,) = persistencia.relevos_activos(conn)

    assert uno["minutos_callado"] < 1, "el reloj no se reinició al escribir el doctor"


def test_una_conversacion_sin_relevo_no_sale_en_el_barrido(esquema):
    _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        assert persistencia.relevos_activos(conn) == []


def test_un_relevo_sin_tema_sale_igual_con_topic_id_nulo(esquema):
    """El `LEFT JOIN` no es cosmético: si fuera un JOIN normal, un relevo cuyo paciente
    perdiera el tema desaparecería del barrido y no se cerraría nunca."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        (uno,) = persistencia.relevos_activos(conn)

    assert uno["topic_id"] is None


# ==========================================================================================
# Las columnas sueltas que sostienen el resto
# ==========================================================================================


def test_el_escalamiento_queda_marcado_por_su_mensaje_de_telegram(esquema):
    """Va por `telegram_message_id` porque es lo único que trae el `callback_query`: el
    doctor pulsó un botón que cuelga de un mensaje concreto."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        escalamiento_id = persistencia.insertar_escalamiento(
            conn,
            id_conversacion=conv,
            motivo="dolor_agudo",
            resumen="le duele",
            pregunta="quien lo ve",
            clave_idempotencia="k-1",
        )
        persistencia.anotar_telegram_en_escalamiento(conn, escalamiento_id, 555)
        persistencia.marcar_relevo_activado(conn, 555)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relevo_activado FROM escalamientos WHERE id = %s", (escalamiento_id,)
            )
            assert cur.fetchone()[0] is True


def test_el_candado_del_tema_se_puede_abrir_y_cerrar_por_telefono(esquema):
    """`guardar_tema` no sirve para esto: pisa el `topic_id`. Lo que cambia al abrir y cerrar
    un relevo es solo el candado."""
    _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.marcar_tema_abierto(conn, TEL, abierto=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT topic_id, abierto FROM temas_telegram WHERE telefono = %s",
                (TEL,),
            )
            assert cur.fetchone() == (TEMA, True)

    with persistencia.conectar(esquema) as conn:
        persistencia.marcar_tema_abierto(conn, TEL, abierto=False)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT abierto FROM temas_telegram WHERE telefono = %s", (TEL,)
            )
            assert cur.fetchone()[0] is False


def test_del_uuid_del_boton_se_llega_al_telefono(esquema):
    """El botón viaja con el uuid y no con el teléfono porque el `callback_data` es visible
    para cualquiera del grupo. Esta consulta es lo que cierra el camino de vuelta."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        assert persistencia.telefono_de_conversacion(conn, conv) == TEL
        assert (
            persistencia.telefono_de_conversacion(
                conn, "00000000-0000-0000-0000-000000000000"
            )
            is None
        )


def test_salud_cuenta_los_relevos_abiertos(esquema):
    """El numero que delata un relevo atascado. Aparte de `relevos_activos` porque `/salud`
    no necesita las cinco columnas de cada fila, solo el total."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        assert persistencia.contar_relevos_abiertos(conn) == 0
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        assert persistencia.contar_relevos_abiertos(conn) == 1
        persistencia.cerrar_relevo(conn, conv, motivo="devuelto_por_doctor")
        assert persistencia.contar_relevos_abiertos(conn) == 0


# ==========================================================================================
# 6D · El hilo por telefono (migracion 014)
# ==========================================================================================


def test_las_columnas_viejas_del_hilo_ya_no_existen(esquema):
    """La 014 las deja caer despues de copiar. Si volvieran, el mismo hecho viviria en dos
    sitios y el bug del mes que viene seria que uno de los dos se queda viejo."""
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'pacientes'
            """
        )
        columnas = {f[0] for f in cur.fetchall()}

    assert "telegram_topic_id" not in columnas
    assert "telegram_topic_abierto" not in columnas


def test_un_numero_sin_ficha_puede_tener_hilo(esquema):
    """LA RAZON DE SER de la 014, y el arreglo del ruido en el General.

    Antes el hilo era una columna de `pacientes`, asi que sin ficha no habia donde colgarlo
    -- y `lectura.asegurar_tema` no podia crear la ficha sin regalarle `identidad_verificada`
    a un desconocido. Resultado medido en produccion: sus radiografias caian en el General y
    sus textos no se archivaban en ninguna parte.
    """
    with persistencia.conectar(esquema) as conn:
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=5150)

        assert persistencia.tema_del_paciente(conn, TEL) == 5150
        # Y sigue sin ser paciente: tener hilo y estar verificado son cosas distintas, que es
        # exactamente lo que permite lo de arriba sin tocar el guardrail de identidad.
        assert persistencia.buscar_paciente_por_telefono(conn, TEL) is None


def test_guardar_el_hilo_dos_veces_no_duplica_la_fila(esquema):
    """Dos archivos del mismo numero pueden llegar casi a la vez, y el candado de
    `lectura._candados_de_tema` es de PROCESO: no protege entre replicas."""
    with persistencia.conectar(esquema) as conn:
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=6001)
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=6002)

        assert persistencia.tema_del_paciente(conn, TEL) == 6002
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM temas_telegram WHERE telefono = %s", (TEL,))
            assert cur.fetchone()[0] == 1


def test_un_hilo_no_puede_ser_de_dos_personas(esquema):
    """`topic_id` es UNIQUE. Si esto dejara de saltar, alguien estaria depositando la
    radiografia de uno en el expediente de otro."""
    with persistencia.conectar(esquema) as conn:
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=7777)

    with pytest.raises(Exception):
        with persistencia.conectar(esquema) as conn:
            persistencia.guardar_tema(conn, telefono=TEL_VECINO, topic_id=7777)


def test_el_relevo_guarda_el_hilo_del_paciente_sin_archivo(esquema):
    """La funcion que el relevo usa para atar el hilo recien creado, sin doblar nada.

    Llamaba a `guardar_tema` con `id_paciente=` --la firma de antes de la 014-- y reventaba
    con `TypeError` en el unico camino que la recorre: el del numero que todavia no tiene
    hilo, o sea el que nunca mando un archivo. `activar` se tragaba la excepcion y deshacia
    el relevo, asi que el boton no funcionaba justo para el paciente nuevo con dolor.

    `tests/test_relevo.py` dobla esta funcion entera --tiene que hacerlo, abre conexion-- y
    por eso alli se comprueba la llamada y aqui el SQL. Las dos hacen falta.
    """
    relevo._guardar_tema_abierto(esquema, TEL, 8899)

    with persistencia.conectar(esquema) as conn:
        assert persistencia.tema_del_paciente(conn, TEL) == 8899
        with conn.cursor() as cur:
            cur.execute("SELECT abierto FROM temas_telegram WHERE telefono = %s", (TEL,))
            # Nace ABIERTO: `createForumTopic` lo deja asi, y si la base dijera lo contrario
            # `cerrar` no sabria que hay un canal en vivo hacia el WhatsApp de esa persona.
            assert cur.fetchone()[0] is True
        # Tener hilo NO es estar verificado: esta fila no crea ninguna ficha.
        assert persistencia.buscar_paciente_por_telefono(conn, TEL) is None


# ==========================================================================================
# 6D · La transcripcion que recibe el doctor al entrar
# ==========================================================================================


def test_la_transcripcion_junta_las_dos_mitades_en_orden(esquema):
    """Lo que dijo el paciente vive en `mensajes_entrantes` y lo que contesto Daniela solo
    existe dentro del historial del SDK. Juntarlas por hora es lo unico que las convierte en
    una conversacion legible en vez de dos listas sueltas."""
    conv = _conversacion(esquema)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en) "
            "VALUES (%s, %s, 'text', %s, %s)",
            ("w-1", TEL, "hola, me duele una muela", base),
        )
        cur.execute(
            "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en) "
            "VALUES (%s, %s, 'text', %s, %s)",
            ("w-2", TEL, "desde ayer", base + timedelta(minutes=2)),
        )
        cur.execute(
            "INSERT INTO agent_sessions (session_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (conv,),
        )
        cur.execute(
            "INSERT INTO agent_messages (session_id, message_data, created_at) "
            "VALUES (%s, %s, %s)",
            (
                conv,
                json.dumps({"role": "assistant", "content": "Cuentame desde cuando"}),
                (base + timedelta(minutes=1)).replace(tzinfo=None),
            ),
        )
        # Una llamada a tool: NO es una frase y no puede acabar en el hilo del paciente.
        cur.execute(
            "INSERT INTO agent_messages (session_id, message_data, created_at) "
            "VALUES (%s, %s, %s)",
            (
                conv,
                json.dumps({"type": "function_call", "name": "consultar_disponibilidad"}),
                (base + timedelta(minutes=1, seconds=30)).replace(tzinfo=None),
            ),
        )
        conn.commit()

    with persistencia.conectar(esquema) as conn:
        lineas = persistencia.transcripcion(conn, TEL)

    assert [(q, t) for q, t, _ in lineas] == [
        ("paciente", "hola, me duele una muela"),
        ("daniela", "Cuentame desde cuando"),
        ("paciente", "desde ayer"),
    ], "la transcripcion no salio en orden, o se colo un item que no es una frase"


def test_la_transcripcion_de_un_numero_nuevo_esta_vacia(esquema):
    with persistencia.conectar(esquema) as conn:
        assert persistencia.transcripcion(conn, TEL) == []


def test_la_transcripcion_corta_por_el_final(esquema):
    """Un hilo de dos meses no cabe en un mensaje de Telegram, y lo que el doctor necesita
    para entrar en contexto es el final, no como empezo todo."""
    base = datetime.now(timezone.utc) - timedelta(hours=5)
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        for i in range(30):
            cur.execute(
                "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en) "
                "VALUES (%s, %s, 'text', %s, %s)",
                (f"w-{i}", TEL, f"mensaje {i}", base + timedelta(minutes=i)),
            )
        conn.commit()

    with persistencia.conectar(esquema) as conn:
        lineas = persistencia.transcripcion(conn, TEL, limite=5)

    assert [t for _, t, _ in lineas] == [f"mensaje {i}" for i in range(25, 30)]


# ==========================================================================================
# 6D · El estado del cierre conversado
# ==========================================================================================


def test_el_dialogo_de_cierre_se_guarda_y_se_lee_por_el_tema(esquema):
    """Vive en la base y no en memoria por una razon concreta: si el proceso reiniciara
    mientras se espera la fecha, un «15/09 14:30» se le apareceria al PACIENTE en su
    WhatsApp, porque `relevar_mensaje` lo reenviaria como un mensaje normal."""
    conv = _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        assert persistencia.relevo_por_tema(conn, TEMA)["cierre_pendiente"] is None

        persistencia.marcar_cierre_pendiente(conn, conv, "esperando_fecha")
        assert persistencia.relevo_por_tema(conn, TEMA)["cierre_pendiente"] == "esperando_fecha"

        persistencia.marcar_cierre_pendiente(conn, conv, None)
        assert persistencia.relevo_por_tema(conn, TEMA)["cierre_pendiente"] is None


def test_un_estado_de_cierre_inventado_lo_rechaza_la_base(esquema):
    """El CHECK cerrado de la 014, por el mismo criterio que el de la 003: un estado nuevo
    pasa por una migracion, no se cuela como un string cualquiera."""
    conv = _conversacion(esquema)

    with pytest.raises(Exception):
        with persistencia.conectar(esquema) as conn:
            persistencia.marcar_cierre_pendiente(conn, conv, "a_ver_que_pasa")


# ==========================================================================================
# 6F/6G · De que es la cita (015) y el hilo que alguien borro
# ==========================================================================================


def test_el_tratamiento_del_cierre_se_guarda_y_se_lee_por_el_tema(esquema):
    """Vive en la base y no en memoria por lo mismo que `cierre_pendiente`: el dato llega en
    la pregunta ANTERIOR a la fecha, y entre las dos puede reiniciarse el proceso. Perderlo
    significaria registrar la cita con el tratamiento de otra, o sin ninguno."""
    conv = _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.activar_relevo(conn, id_conversacion=conv, doctor="Dra. Ruiz")
        assert persistencia.relevo_por_tema(conn, TEMA)["cierre_tratamiento"] is None

        persistencia.guardar_tratamiento_del_cierre(conn, conv, "Control post-operatorio")
        leido = persistencia.relevo_por_tema(conn, TEMA)["cierre_tratamiento"]

    assert leido == "Control post-operatorio"


def test_el_tratamiento_del_cierre_NO_se_valida_contra_el_catalogo(esquema):
    """Decision explicita del cliente (14/09/2026). Es el UNICO sitio del proyecto donde
    `citas.tratamiento` acepta algo que no esta en `tratamientos`: en todos los demas,
    `herramientas._marcar_estado` lo rechaza contra la lista viva.

    La prueba existe para que quien anada esa validacion "por coherencia" sepa que esta
    deshaciendo una decision, no arreglando un descuido.
    """
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        persistencia.guardar_tratamiento_del_cierre(conn, conv, "esto no existe en la tabla")
        with conn.cursor() as cur:
            cur.execute("SELECT cierre_tratamiento FROM conversaciones WHERE id = %s", (conv,))
            assert cur.fetchone()[0] == "esto no existe en la tabla"


def test_el_paso_del_tratamiento_lo_acepta_el_CHECK(esquema):
    """La 015 reemplaza el CHECK de la 014 entero en vez de anadir otro: dos CHECK sobre la
    misma columna se contradicen callados y gana el mas restrictivo -- que aqui seria el
    viejo, o sea que el paso nuevo no entraria nunca."""
    conv = _conversacion(esquema)

    with persistencia.conectar(esquema) as conn:
        for estado in (
            "preguntado",
            "esperando_nombre",  # lo anade la 016, por el mismo motivo
            "esperando_tratamiento",
            "esperando_fecha",
            None,
        ):
            persistencia.marcar_cierre_pendiente(conn, conv, estado)


def test_ponerle_nombre_al_paciente_PENDIENTE(esquema):
    """El numero que escribe por primera vez no tiene ficha, el relevo se la crea como
    PENDIENTE, y sin esto su cita entraba en la agenda como «PENDIENTE - Cordales»."""
    telefono = TEL

    with persistencia.conectar(esquema) as conn:
        persistencia.asegurar_paciente(
            conn, nombre_completo="PENDIENTE", telefono=telefono
        )

        escribio = persistencia.nombrar_si_esta_pendiente(
            conn, telefono=telefono, nombre="Maria Fernanda Rios", pendiente="PENDIENTE"
        )

        assert escribio
        assert persistencia.buscar_paciente_por_telefono(conn, telefono)[1] == (
            "Maria Fernanda Rios"
        )


def test_el_nombre_de_un_paciente_de_VERDAD_no_se_pisa(esquema):
    """La regla que `asegurar_paciente` lleva desde el principio, y esta via no es una
    excepcion: si el numero de la casa lo usan dos personas, pisarlo haria que el historial
    del primero apareciera bajo el nombre del segundo."""
    telefono = TEL

    with persistencia.conectar(esquema) as conn:
        persistencia.asegurar_paciente(
            conn, nombre_completo="Carlos Pena", telefono=telefono
        )

        escribio = persistencia.nombrar_si_esta_pendiente(
            conn, telefono=telefono, nombre="Otro Nombre", pendiente="PENDIENTE"
        )

        assert not escribio
        assert persistencia.buscar_paciente_por_telefono(conn, telefono)[1] == "Carlos Pena"


def test_al_numero_sin_ficha_se_le_crea_una_con_su_nombre(esquema):
    """Lo dispara un doctor que acaba de hablar con esa persona, y con el nombre de verdad.

    Desde el 16/09/2026 es ademas la razon por la que el relevo pudo dejar de abrir la ficha
    en blanco: este camino la CREA cuando no hay ninguna, asi que quitarla de la activacion no
    dejo al doctor sin poder registrar a nadie.
    """
    telefono = TEL

    with persistencia.conectar(esquema) as conn:
        assert persistencia.buscar_paciente_por_telefono(conn, telefono) is None

        assert persistencia.nombrar_si_esta_pendiente(
            conn, telefono=telefono, nombre="Ana Ruiz", pendiente="PENDIENTE"
        )
        assert persistencia.buscar_paciente_por_telefono(conn, telefono)[1] == "Ana Ruiz"


def test_agendar_le_quita_el_marcador_a_una_ficha_que_lo_tenia(esquema):
    """La reparacion de las fichas que el relevo dejo en blanco antes del 16/09/2026.

    `asegurar_paciente` no actualiza nombres, a proposito --dos personas en el telefono de la
    casa--, y esa regla dejaba el marcador puesto PARA SIEMPRE: el unico camino que lo pisaba
    era un relevo que terminara CON cita, y el caso que creo el marcador es justo el del
    doctor que cierra sin agendar.

    Contra Postgres de verdad y no contra un doble, porque lo que hay que comprobar es el
    `ON CONFLICT ... WHERE` de `nombrar_si_esta_pendiente`: un doble diria que si a cualquier
    cosa, incluida la version que pisa el nombre de un paciente real.
    """
    with persistencia.conectar(esquema) as conn:
        # Como quedaba la ficha tras un relevo, hasta hoy.
        marcado = persistencia.asegurar_paciente(
            conn, nombre_completo=persistencia.NOMBRE_PENDIENTE, telefono=TEL
        )
        assert persistencia.buscar_paciente_por_telefono(conn, TEL)[1] == "PENDIENTE"

        # Y ahora el paciente agenda: `_crear_cita` llama justo a esto.
        mismo = persistencia.asegurar_paciente(
            conn, nombre_completo="Sora Patricia Delgado", telefono=TEL
        )

        assert mismo == marcado, "no puede abrir una ficha nueva: sus citas apuntan a la vieja"
        assert persistencia.buscar_paciente_por_telefono(conn, TEL)[1] == (
            "Sora Patricia Delgado"
        )


def test_agendar_NO_le_pisa_el_nombre_a_un_paciente_de_verdad(esquema):
    """El falso positivo de la anterior, y la mitad que no se puede aflojar.

    Sin esta, la reparacion de arriba se podria escribir como un UPDATE a secas y pasaria en
    verde -- y entonces la hija que agenda desde el telefono de la casa le cambiaria el nombre
    a la ficha de su madre, con el historial de la madre debajo.
    """
    with persistencia.conectar(esquema) as conn:
        original = persistencia.asegurar_paciente(
            conn, nombre_completo="Carmen Delgado", telefono=TEL
        )

        mismo = persistencia.asegurar_paciente(
            conn, nombre_completo="Sora Patricia Delgado", telefono=TEL
        )

        assert mismo == original
        assert persistencia.buscar_paciente_por_telefono(conn, TEL)[1] == "Carmen Delgado"


def test_un_estado_de_cierre_inventado_lo_sigue_rechazando_la_base(esquema):
    """Reemplazar el CHECK no puede haberlo dejado abierto."""
    conv = _conversacion(esquema)

    with pytest.raises(Exception):
        with persistencia.conectar(esquema) as conn:
            persistencia.marcar_cierre_pendiente(conn, conv, "a_ver_que_pasa")


def test_olvidar_el_hilo_deja_al_paciente_listo_para_tener_otro(esquema):
    """Se llama cuando se comprueba que el tema YA NO EXISTE en Telegram. Sin esto el sistema
    no se recupera nunca: `temas_telegram` seguiria apuntando a un `topic_id` muerto,
    `asegurar_tema` lo daria por bueno sin crear ninguno, y cada archivo futuro de esa persona
    fallaria al depositarse. Para siempre, y en silencio."""
    with persistencia.conectar(esquema) as conn:
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=4321)
        assert persistencia.tema_del_paciente(conn, TEL) == 4321

        assert persistencia.olvidar_tema(conn, TEL) is True
        assert persistencia.tema_del_paciente(conn, TEL) is None

        # Y el hueco queda libre de verdad: se puede colgar uno nuevo.
        persistencia.guardar_tema(conn, telefono=TEL, topic_id=4322)
        assert persistencia.tema_del_paciente(conn, TEL) == 4322


def test_olvidar_un_hilo_que_no_existe_no_es_un_error(esquema):
    """El barrido puede llegar dos veces al mismo hilo muerto: comprueba, cierra, y en el
    siguiente ciclo otro relevo del mismo numero. Reventar ahi pararia el barrido entero."""
    with persistencia.conectar(esquema) as conn:
        assert persistencia.olvidar_tema(conn, TEL) is False

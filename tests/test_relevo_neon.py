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

import os

import pytest

from maxicare_daniela import persistencia
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
            cur.execute("DELETE FROM escalamientos")
            cur.execute("DELETE FROM conversaciones")
            cur.execute("DELETE FROM pacientes")
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
                conn, id_paciente=id_paciente, topic_id=con_tema, abierto=False
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

    assert encontrado == {"id_conversacion": conv, "telefono": TEL, "doctor": "Dra. Ruiz"}


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
    """`guardar_tema` no sirve para esto: pide el `id_paciente` y pisa el `telegram_topic_id`.
    Lo que cambia al abrir y cerrar un relevo es solo el candado."""
    _conversacion(esquema, con_tema=TEMA)

    with persistencia.conectar(esquema) as conn:
        persistencia.marcar_tema_abierto(conn, TEL, abierto=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT telegram_topic_id, telegram_topic_abierto FROM pacientes "
                " WHERE telefono = %s",
                (TEL,),
            )
            assert cur.fetchone() == (TEMA, True)

    with persistencia.conectar(esquema) as conn:
        persistencia.marcar_tema_abierto(conn, TEL, abierto=False)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT telegram_topic_abierto FROM pacientes WHERE telefono = %s", (TEL,)
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

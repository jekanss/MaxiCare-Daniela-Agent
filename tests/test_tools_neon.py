"""Las tools contra Neon de verdad. Aquí vive el entregable de la fase 3.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Sin esa variable se saltan enteras, y es a propósito: `uv run pytest -q` corre en dos
segundos y sin señal, y eso es parte de por qué se corre. Una suite que exige internet es
una suite que alguien termina saltándose el día que tiene prisa.

------------------------------------------------------------------------------------------
Dónde escriben estas pruebas
------------------------------------------------------------------------------------------

En un esquema aparte llamado `pruebas`, dentro de la misma base de Neon. No es una
precaución de estilo: la tabla `pacientes` de `public` tiene pacientes reales de una clínica.
Un `DELETE` mal escrito en una prueba que corre sobre `public` no se deshace.

El aislamiento es físico, no una convención de nombres: la conexión lleva
`search_path=pruebas`, así que un `INSERT INTO citas` escrito sin pensar entra en el esquema
de pruebas aunque nadie se acuerde de prefijarlo. Al terminar, el esquema se borra entero.

------------------------------------------------------------------------------------------
Qué demuestra la prueba que cierra la fase
------------------------------------------------------------------------------------------

Que el control de capacidad es real y no optimista. `sistemas.lista[Google Calendar].notas`
lo exige, y la única forma de demostrarlo es con un hecho: tres pacientes pidiendo la misma
hora a la vez, y exactamente dos citas al final. La tercera no puede reventar: tiene que
recibir «ese horario está lleno» como respuesta normal, con alternativas.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import herramientas as h
from maxicare_daniela import persistencia
from maxicare_daniela.calendario import CalendarioDoble
from maxicare_daniela.config import cargar_dotenv
from maxicare_daniela.contratos import ContextoDaniela, SolicitudCancelacion, SolicitudCita

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas"
CAPACIDAD = 2


def _url_de_pruebas() -> str:
    """La URL de Neon, sin pooler y apuntada al esquema de pruebas.

    Dos cambios sobre la URL de producción, y los dos hacen falta:

    1. **Se quita el `-pooler` del host.** Neon pone un PgBouncer delante, y PgBouncer
       rechaza `options` como parámetro de arranque con un error explícito:
       «unsupported startup parameter in options: search_path». Además reparte las
       conexiones entre sesiones compartidas, que es justo lo contrario de lo que necesita
       una prueba de concurrencia: aquí hacen falta sesiones de verdad, independientes,
       para que el choque por el mismo cupo sea real y no un artefacto del pool.
    2. **Se añade `options=-csearch_path=pruebas`**, que viaja a libpq y fija el esquema
       para toda la sesión. Así no hay forma de que una consulta de estas pruebas toque
       `public` --donde están los pacientes reales-- por descuido.
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


@pytest.fixture
def contexto_de(esquema):
    """Fabrica un contexto con su paciente y su conversación ya creados en la base."""
    creados: list[str] = []

    def fabricar(telefono: str, nombre: str = "Paciente De Prueba") -> ContextoDaniela:
        with persistencia.conectar(esquema) as conn:
            id_paciente = persistencia.asegurar_paciente(
                conn, nombre_completo=nombre, telefono=telefono
            )
            id_conversacion = persistencia.asegurar_conversacion(
                conn, telefono=telefono, paciente_id=id_paciente
            )
        creados.append(id_conversacion)
        return ContextoDaniela(
            id_conversacion=id_conversacion,
            telefono_completo=telefono,
            database_url=esquema,
            calendario=CalendarioDoble(),
            id_paciente=id_paciente,
            nombre_paciente=nombre,
            identidad_verificada=True,
            capacidad_por_hora=CAPACIDAD,
            duracion_cita_minutos=60,
        )

    return fabricar


def _hora_libre(desplazamiento: int = 0) -> datetime:
    """Una hora futura distinta en cada prueba, para que no se estorben entre sí."""
    base = datetime.now(h.ZONA_BOGOTA).replace(minute=0, second=0, microsecond=0)
    return base + timedelta(days=30, hours=desplazamiento)


# ==========================================================================================
# EL ENTREGABLE DE LA FASE
# ==========================================================================================


def test_tres_llamadas_simultaneas_producen_exactamente_dos_citas(esquema, contexto_de):
    """La prueba que cierra la fase 3.

    Tres pacientes distintos piden la misma hora al mismo tiempo. La clínica atiende dos por
    hora. Al final tiene que haber DOS citas -- ni una más, ni una menos -- y el tercero
    tiene que haber recibido una respuesta útil, no una excepción.

    Lo que esto descarta es el bug que no se ve en una prueba manual: contar las reservas en
    Python y comparar con la capacidad. Entre el conteo y el INSERT cabe otra transacción, y
    los dos pacientes descubren el choque cuando están los dos en la sala de espera.
    """
    inicio = _hora_libre(0)
    contextos = [contexto_de(f"57300000000{i}", f"Paciente Simultaneo {i}") for i in range(3)]

    async def tres_a_la_vez() -> list[str]:
        return await asyncio.gather(
            *[
                h._crear_cita(
                    ctx,
                    SolicitudCita(
                        nombre_completo=ctx.nombre_paciente,
                        inicio=inicio,
                        tratamiento="limpieza",
                        clave_idempotencia=f"{ctx.telefono_completo}:{inicio.isoformat()}",
                    ),
                )
                for ctx in contextos
            ]
        )

    resultados = asyncio.run(tres_a_la_vez())

    confirmadas = [r for r in resultados if "Cita confirmada" in r]
    llenas = [r for r in resultados if "ya está lleno" in r]

    assert len(confirmadas) == 2, f"se confirmaron {len(confirmadas)} citas, no 2: {resultados}"
    assert len(llenas) == 1, "el tercero no recibió el aviso de horario lleno"
    assert "NO insistas" in llenas[0]

    # Y la base dice lo mismo que las respuestas: sin esto, la prueba solo comprobaría
    # que las tools dicen cosas bonitas.
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM citas WHERE inicio = %s", (inicio,))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (inicio,))
        assert cur.fetchone()[0] == 2


def test_el_cupo_aguanta_contension_real_con_hilos(esquema):
    """La misma garantía, empujada más fuerte.

    La prueba de arriba deja que el planificador decida cuándo corre cada llamada, así que
    puede que ni lleguen a solaparse. Esta las hace chocar a propósito: seis hilos esperando
    en una barrera y saliendo todos en el mismo instante contra el mismo horario.

    Si `tomar_cupo` tuviera un `SELECT count(*)` en vez de apoyarse en la restricción UNIQUE,
    aquí saldrían más de dos cupos concedidos.
    """
    inicio = _hora_libre(1)
    hilos = 6
    barrera = threading.Barrier(hilos)
    concedidos: list[tuple[int, int] | None] = [None] * hilos

    def intentar(indice: int) -> None:
        with persistencia.conectar(esquema) as conn:
            barrera.wait()
            concedidos[indice] = persistencia.tomar_cupo(
                conn,
                inicio=inicio,
                capacidad=CAPACIDAD,
                clave_idempotencia=f"contension-{indice}-{inicio.isoformat()}",
            )

    trabajadores = [threading.Thread(target=intentar, args=(i,)) for i in range(hilos)]
    for t in trabajadores:
        t.start()
    for t in trabajadores:
        t.join()

    con_cupo = [c for c in concedidos if c is not None]
    assert len(con_cupo) == CAPACIDAD, f"se concedieron {len(con_cupo)} cupos de {CAPACIDAD}"
    assert len({c[1] for c in con_cupo}) == CAPACIDAD, "dos hilos se llevaron el mismo cupo"


# ==========================================================================================
# Idempotencia
# ==========================================================================================


def test_el_mismo_intento_repetido_no_consume_un_segundo_cupo(esquema, contexto_de):
    """Un reintento de red no puede gastar la mitad de la capacidad de la hora."""
    inicio = _hora_libre(2)
    ctx = contexto_de("573001110001", "Ana Reintento")
    clave = f"{ctx.telefono_completo}:{inicio.isoformat()}"

    with persistencia.conectar(esquema) as conn:
        primero = persistencia.tomar_cupo(
            conn, inicio=inicio, capacidad=CAPACIDAD, clave_idempotencia=clave
        )
        segundo = persistencia.tomar_cupo(
            conn, inicio=inicio, capacidad=CAPACIDAD, clave_idempotencia=clave
        )

    assert primero is not None
    assert primero == segundo, "el reintento se llevó un cupo nuevo"


def test_un_seguimiento_no_se_programa_dos_veces(esquema, contexto_de):
    ctx = contexto_de("573001110002")
    objetivo = _hora_libre(48)
    clave = ctx.clave("seguimiento", "recordatorio_cita", objetivo.isoformat())

    with persistencia.conectar(esquema) as conn:
        nuevo = persistencia.insertar_seguimiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            tipo="recordatorio_cita",
            fecha_objetivo=objetivo,
            clave_idempotencia=clave,
        )
        repetido = persistencia.insertar_seguimiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            tipo="recordatorio_cita",
            fecha_objetivo=objetivo,
            clave_idempotencia=clave,
        )

    assert nuevo is True
    assert repetido is False


# ==========================================================================================
# El ciclo completo de una cita
# ==========================================================================================


def test_una_cita_se_crea_se_mueve_y_se_cancela_liberando_el_cupo(esquema, contexto_de):
    """El recorrido que hace un paciente real, con el cupo vigilado en cada paso."""
    inicio = _hora_libre(3)
    destino = _hora_libre(4)
    ctx = contexto_de("573001110003", "Carlos Ciclo")

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Carlos Ciclo",
                inicio=inicio,
                tratamiento="cordales",
                clave_idempotencia=f"{ctx.telefono_completo}:{inicio.isoformat()}",
            ),
        )
    )
    assert "Cita confirmada" in texto
    id_cita = texto.rsplit("Id de la cita: ", 1)[1].rstrip(".")

    # Reprogramar: el cupo viejo tiene que quedar libre, o la clínica pierde esa hora.
    movida = asyncio.run(h._reprogramar_cita(ctx, id_cita, destino.isoformat()))
    assert "reprogramada" in movida

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (inicio,))
        assert cur.fetchone()[0] == 0, "el horario viejo quedó bloqueado tras reprogramar"
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (destino,))
        assert cur.fetchone()[0] == 1

    # Cancelar.
    cancelada = asyncio.run(
        h._cancelar_cita(
            ctx, SolicitudCancelacion(id_cita=id_cita, motivo="prueba", clave_idempotencia=id_cita)
        )
    )
    assert "cancelada" in cancelada

    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (destino,))
        assert cur.fetchone()[0] == 0, "el cupo no se liberó al cancelar"
        cur.execute("SELECT estado FROM citas WHERE id = %s", (id_cita,))
        assert cur.fetchone()[0] == "cancelada"


def test_si_calendar_no_responde_al_cancelar_el_cupo_se_libera_igual(esquema, contexto_de):
    """Al cancelar, lo peligroso es lo contrario que al crear.

    Al crear, el riesgo es prometer una cita que no existe. Al cancelar, el riesgo es dejar
    un horario bloqueado que nadie va a usar y que ningún paciente podrá reservar. Así que el
    cupo se libera aunque Calendar esté caído, y el evento huérfano lo limpia un humano.
    """
    inicio = _hora_libre(5)
    ctx = contexto_de("573001110004", "Diana Calendario")

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Diana Calendario",
                inicio=inicio,
                tratamiento="limpieza",
                clave_idempotencia=f"{ctx.telefono_completo}:{inicio.isoformat()}",
            ),
        )
    )
    id_cita = texto.rsplit("Id de la cita: ", 1)[1].rstrip(".")

    ctx.calendario.fallar_en.add("eliminar_evento")
    resultado = asyncio.run(
        h._cancelar_cita(
            ctx, SolicitudCancelacion(id_cita=id_cita, clave_idempotencia=id_cita)
        )
    )

    assert "sigue en el calendario" in resultado
    assert "Escala" in resultado
    with persistencia.conectar(esquema) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (inicio,))
        assert cur.fetchone()[0] == 0, "el cupo quedó bloqueado por un fallo de Calendar"


# ==========================================================================================
# Conocimiento y disponibilidad
# ==========================================================================================


def test_un_tratamiento_sin_precio_documentado_no_devuelve_una_cifra(esquema, contexto_de):
    """Endodoncia y prótesis no tienen precio aprobado (sección 2.12 del documento maestro).

    Esta prueba corre contra la base recién sembrada, así que comprueba la regla de punta a
    punta: consulta real, tabla real, y ni un número en la respuesta.
    """
    ctx = contexto_de("573001110005")

    with persistencia.conectar(esquema) as conn:
        persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())

    texto = asyncio.run(h._consultar_base_conocimiento(ctx, "endodoncia", "precio"))

    assert "SIN DATO DOCUMENTADO" in texto or "PENDIENTE DE APROBACIÓN" in texto
    if "SIN DATO DOCUMENTADO" in texto:
        assert "PROHIBIDO estimar" in texto


def test_la_disponibilidad_no_ofrece_un_bloque_lleno(esquema, contexto_de):
    inicio = _hora_libre(6)
    ctx = contexto_de("573001110006")

    with persistencia.conectar(esquema) as conn:
        for i in range(CAPACIDAD):
            persistencia.tomar_cupo(
                conn,
                inicio=inicio,
                capacidad=CAPACIDAD,
                clave_idempotencia=f"lleno-{i}-{inicio.isoformat()}",
            )

    texto = asyncio.run(
        h._consultar_disponibilidad(
            ctx, inicio.isoformat(), (inicio + timedelta(hours=3)).isoformat()
        )
    )

    assert f"{inicio:%H:%M}" not in texto, "ofreció un bloque que ya estaba lleno"


def test_la_disponibilidad_respeta_un_bloqueo_del_doctor(esquema, contexto_de):
    inicio = _hora_libre(7)
    ctx = contexto_de("573001110007")
    ctx.calendario.agregar_bloqueo(inicio, inicio + timedelta(hours=1), "cirugía")

    texto = asyncio.run(
        h._consultar_disponibilidad(
            ctx, inicio.isoformat(), (inicio + timedelta(hours=3)).isoformat()
        )
    )

    assert f"{inicio:%H:%M}" not in texto, "ofreció una hora que el doctor tenía bloqueada"


# ==========================================================================================
# Identificación
# ==========================================================================================


def test_un_numero_registrado_se_identifica_con_su_nombre(esquema, contexto_de):
    ctx = contexto_de("573001110008", "Elena Registrada")
    ctx.identidad_verificada = False
    ctx.id_paciente = None

    texto = asyncio.run(h._identificar_paciente(ctx, "elena registrada"))

    assert "Identidad verificada" in texto
    assert ctx.identidad_verificada is True


def test_tras_dos_nombres_equivocados_se_escala_y_no_se_pide_documento(esquema, contexto_de):
    ctx = contexto_de("573001110009", "Felipe Correcto")
    ctx.identidad_verificada = False
    ctx.id_paciente = None

    asyncio.run(h._identificar_paciente(ctx, "Nombre Equivocado"))
    segundo = asyncio.run(h._identificar_paciente(ctx, "Otro Nombre Distinto"))
    tercero = asyncio.run(h._identificar_paciente(ctx, "Tercer Intento"))

    assert ctx.identidad_verificada is False
    assert "escala" in (segundo + tercero).lower()
    assert "cédula" not in (segundo + tercero).lower()
    assert "documento" not in tercero.lower() or "NO pidas" in tercero

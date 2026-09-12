"""Las nueve tools, sin red y sin base de datos.

Lo que se prueba aquí es lo que se puede probar sin Neon: las reglas que viven en el código
de la tool y los textos que el modelo recibe cuando algo falla. Lo que toca la base --y la
prueba de concurrencia que cierra la fase-- vive en `test_tools_neon.py`, marcado `neon`.

La división no es de comodidad. Estas pruebas corren en milisegundos y sin señal, así que
corren siempre; una suite que necesita internet es una suite que alguien acaba saltándose.

Las tools se llaman por su función interna (`_crear_cita`, `_identificar_paciente`...) y no
por el `FunctionTool` decorado: el objeto decorado solo se puede invocar con un JSON
serializado y un contexto de corrida completo, que es justo el andamiaje que estas pruebas
existen para no necesitar.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import herramientas as h
from maxicare_daniela import persistencia
from maxicare_daniela.calendario import Bloqueo, CalendarioDoble, bloques_del_dia
from maxicare_daniela.contratos import ContextoDaniela, SolicitudCita, SolicitudEscalamiento

INICIO = datetime(2026, 9, 15, 9, 0, tzinfo=h.ZONA_BOGOTA)


def contexto(**cambios) -> ContextoDaniela:
    base = dict(
        id_conversacion="conv-1",
        telefono_completo="573001112233",
        database_url="postgresql://no-se-usa",
        calendario=CalendarioDoble(),
    )
    base.update(cambios)
    return ContextoDaniela(**base)


class BaseFalsa:
    """Sustituye a `_con_base` sin tocar psycopg.

    Cada tool recibe un `conn` que es este objeto; los métodos que la tool llame sobre
    `persistencia` se interceptan con monkeypatch en cada prueba. Así se comprueba el
    ORDEN de las operaciones, que en `crear_cita` es la mitad del diseño.
    """

    def __init__(self) -> None:
        self.llamadas: list[str] = []


# ==========================================================================================
# La regla más importante del sistema: la ausencia de dato es un dato
# ==========================================================================================


def test_sin_dato_documentado_nunca_es_una_cadena_vacia():
    """Si esto devolviera '', el modelo completaría el hueco con un precio plausible."""
    texto = persistencia.formatear_conocimiento([], tratamiento="endodoncia", concepto="precio")

    assert texto.startswith("SIN DATO DOCUMENTADO")
    assert "PROHIBIDO estimar" in texto
    assert texto.strip() != ""


def test_el_mensaje_de_fallo_prohibe_responder_de_memoria():
    texto = h._fallo_conocimiento(None, RuntimeError("Neon caído"))

    assert "NO respondas con información de memoria" in texto
    assert "escala" in texto.lower()


def test_si_falla_la_disponibilidad_se_prohibe_ofrecer_horarios():
    """El fallo más peligroso de todos: un horario inventado manda a alguien a la clínica."""
    texto = h._fallo_disponibilidad(None, RuntimeError("timeout"))

    assert "NO ofrezcas ningún horario" in texto
    assert "que recuerdes de antes" in texto


# ==========================================================================================
# La rejilla de horarios -- lógica pura
# ==========================================================================================


def test_los_bloques_no_se_salen_de_la_ventana():
    bloques = bloques_del_dia(
        datetime(2026, 9, 15, 8, 0), datetime(2026, 9, 15, 11, 30), duracion_minutos=60
    )

    assert len(bloques) == 3  # 8, 9 y 10; el de las 11 no cabe entero
    assert bloques[-1].hour == 10


def test_un_bloqueo_del_doctor_tapa_el_bloque_aunque_haya_cupo():
    """Cupo libre en Neon y hora bloqueada en Calendar son cosas distintas.

    Un doctor que aparta las 9 para una cirugía no crea una reserva: escribe en su
    calendario. Sin este cruce, Daniela ofrecería una hora en la que no hay nadie.
    """
    bloqueo = Bloqueo(
        inicio=datetime(2026, 9, 15, 9, 0, tzinfo=h.ZONA_BOGOTA),
        fin=datetime(2026, 9, 15, 10, 0, tzinfo=h.ZONA_BOGOTA),
        titulo="cirugía",
    )

    assert bloqueo.solapa(INICIO, INICIO + timedelta(hours=1)) is True
    assert bloqueo.solapa(INICIO + timedelta(hours=1), INICIO + timedelta(hours=2)) is False


def test_sin_alternativas_el_texto_no_deja_al_paciente_en_el_aire():
    texto = h._texto_alternativas([])

    assert "otros días" in texto or "otros dias" in texto
    assert "escala" in texto.lower()


# ==========================================================================================
# identificar_paciente
# ==========================================================================================


def test_no_se_pide_un_tercer_intento_de_identificacion():
    """Dos intentos y se escala. Insistir convierte la atención en un interrogatorio."""
    ctx = contexto(intentos_identificacion=h.MAX_INTENTOS_IDENTIFICACION)

    texto = asyncio.run(h._identificar_paciente(ctx, "Juan Pérez"))

    assert "se agotaron" in texto.lower()
    assert "NO pidas ningún documento" in texto
    assert ctx.intentos_identificacion == h.MAX_INTENTOS_IDENTIFICACION  # no lo gastó


def test_un_nombre_con_cedula_se_rechaza_antes_de_tocar_la_base():
    """La prohibición del cliente no se sostiene con una instrucción en el prompt.

    `identificar_paciente` recibe el nombre como argumento suelto, sin modelo de entrada, así
    que ningún validador de Pydantic la cubre: si esta comprobación no estuviera, esta tool
    sería la única puerta abierta por donde una cédula podría entrar al sistema.
    """
    ctx = contexto()

    with pytest.raises(ValueError, match="documento de identidad"):
        asyncio.run(h._identificar_paciente(ctx, "Juan Pérez 1020304050"))


@pytest.mark.parametrize(
    "registrado, ofrecido, esperado",
    [
        ("Juan Pérez", "juan perez", True),          # tildes y mayúsculas no deciden nada
        ("Juan Pérez", "Juan Carlos Pérez Gómez", True),  # dijo su nombre completo
        ("Juan Pérez", "Pedro Ramírez", False),
        ("Juan Pérez", "", False),
    ],
)
def test_la_comparacion_de_nombres_tolera_lo_que_debe(registrado, ofrecido, esperado):
    """Rechazar «Juan Perez» por una tilde gastaría un intento de alguien legítimo."""
    assert h._mismo_nombre(registrado, ofrecido) is esperado


# ==========================================================================================
# crear_cita -- el camino que puede mandar a alguien a una clínica vacía
# ==========================================================================================


def test_horario_lleno_es_un_resultado_y_no_una_excepcion(monkeypatch):
    """Si esto lanzara, el modelo reintentaría la misma hora hasta agotar los turnos."""
    ctx = contexto()

    async def base_falsa(_ctx, trabajo):
        return (None, [INICIO + timedelta(hours=2)])

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=INICIO,
                tratamiento="limpieza",
                clave_idempotencia="573001112233:2026-09-15T09:00",
            ),
        )
    )

    assert "ya está lleno" in texto
    assert "NO insistas" in texto
    assert "Bloques libres" in texto


def test_si_calendar_falla_el_cupo_se_libera_y_la_corrida_muere(monkeypatch):
    """El escenario que justifica que el doble sea hostil.

    Con el cupo ya tomado y Calendar caído, la cita NO existe. Si esto devolviera un texto,
    el modelo podría decidir confirmarla igual y el paciente llegaría a una hora que ningún
    doctor tiene apuntada. Por eso la tool lanza, y por eso lleva
    `failure_error_function=None`.
    """
    calendario = CalendarioDoble()
    calendario.fallar_en.add("crear_evento")
    ctx = contexto(calendario=calendario)

    liberados: list[int] = []

    async def base_falsa(_ctx, trabajo):
        return ((77, 1), [])

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h, "_liberar", lambda _ctx, reserva_id: liberados.append(reserva_id))

    with pytest.raises(h.CitaNoConfirmada):
        asyncio.run(
            h._crear_cita(
                ctx,
                SolicitudCita(
                    nombre_completo="Ana Gómez",
                    inicio=INICIO,
                    tratamiento="limpieza",
                    clave_idempotencia="clave-suficientemente-larga",
                ),
            )
        )

    assert liberados == [77], "el cupo quedó bloqueado para un paciente que nunca tuvo cita"


def test_crear_cita_no_le_da_al_modelo_un_texto_cuando_falla():
    """`failure_error_function=None` no es un olvido: es la decisión del plan.

    El atributo se llama `_failure_error_function` en la 0.22.2 instalada; se lee así y no
    de memoria, porque un `getattr` con default silenciaría un cambio de nombre del SDK y
    esta prueba pasaría sin comprobar nada.
    """
    assert h.crear_cita._failure_error_function is None

    # Las demás sí tienen texto, porque sus fallos no pueden acabar en una cita fantasma.
    assert h.consultar_disponibilidad._failure_error_function is not None
    assert h.escalar_a_doctores._failure_error_function is not None


# ==========================================================================================
# reprogramar_cita -- la clave por valor destino
# ==========================================================================================


def test_la_clave_de_reprogramacion_depende_del_destino_y_no_de_un_delta():
    """Con «mover dos horas», un reintento movería la cita otras dos horas.

    El mismo intento daría un resultado distinto cada vez que la red fallara. Anclada al
    valor destino, repetirla es inofensiva.
    """
    ctx = contexto()
    destino = INICIO + timedelta(hours=2)

    primera = ctx.clave("reprogramar", "cita-1", destino.isoformat())
    segunda = ctx.clave("reprogramar", "cita-1", destino.isoformat())
    otra_hora = ctx.clave("reprogramar", "cita-1", (destino + timedelta(hours=1)).isoformat())

    assert primera == segunda
    assert primera != otra_hora
    assert destino.isoformat() in primera


# ==========================================================================================
# programar_seguimiento
# ==========================================================================================


def test_no_se_programa_nada_sobre_una_conversacion_que_tiene_un_doctor(monkeypatch):
    """El doctor podría estar acordando otra fecha en ese mismo momento."""
    ctx = contexto()

    async def base_falsa(_ctx, trabajo):
        return ("Dr. Martínez", False)

    monkeypatch.setattr(h, "_con_base", base_falsa)

    futuro = (h._ahora() + timedelta(days=2)).isoformat()
    texto = asyncio.run(h._programar_seguimiento(ctx, "recordatorio_cita", futuro))

    assert "No se programó nada" in texto
    assert "Dr. Martínez" in texto


def test_no_se_programa_un_seguimiento_hacia_atras():
    ctx = contexto()
    pasado = (h._ahora() - timedelta(days=1)).isoformat()

    texto = asyncio.run(h._programar_seguimiento(ctx, "reactivacion", pasado))

    assert "ya pasó" in texto


# ==========================================================================================
# escalar_a_doctores
# ==========================================================================================


class TelegramFalso:
    def __init__(self) -> None:
        self.enviados: list[dict] = []

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None) -> int:
        self.enviados.append({"texto": texto, "tema_id": tema_id, "teclado": teclado})
        return 4242


def test_el_escalamiento_va_al_tema_general_y_nunca_al_del_paciente(monkeypatch):
    """El tema del paciente es un canal en vivo hacia su WhatsApp durante el relevo.

    Mandar ahí la discusión interna entre doctores le enseñaría al paciente exactamente lo
    que no debe ver. La separación es física: son dos hilos distintos.
    """
    ctx = contexto(topic_id=99, tema_general=0, nombre_paciente="Ana Gómez")
    telegram = TelegramFalso()

    async def base_falsa(_ctx, trabajo):
        return 5  # id del escalamiento nuevo

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(
        h._escalar_a_doctores(
            ctx,
            SolicitudEscalamiento(
                motivo="clinico",
                resumen_para_doctor="Pregunta por una lesión que ve en su radiografía.",
                pregunta_concreta="¿Se puede responder algo de esto por WhatsApp?",
                clave_idempotencia="conv-1:3",
            ),
            telegram=telegram,
        )
    )

    assert len(telegram.enviados) == 1
    enviado = telegram.enviados[0]
    assert enviado["tema_id"] == 0, "0 significa General; el 99 es el tema del paciente"
    assert enviado["tema_id"] != ctx.topic_id
    assert "Hablar yo con el paciente" in str(enviado["teclado"])


def test_el_mismo_turno_no_escala_dos_veces(monkeypatch):
    """A la cuarta alerta repetida el doctor deja de mirarlas, y ahí muere el escalamiento."""
    ctx = contexto()
    telegram = TelegramFalso()

    async def base_falsa(_ctx, trabajo):
        return None  # la clave ya existía

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(
        h._escalar_a_doctores(
            ctx,
            SolicitudEscalamiento(
                motivo="agenda_llena",
                resumen_para_doctor="No hay cupos esta semana.",
                pregunta_concreta="¿Abrimos un bloque extra?",
                clave_idempotencia="conv-1:3",
            ),
            telegram=telegram,
        )
    )

    assert telegram.enviados == [], "se volvió a avisar sobre un turno ya escalado"
    assert "ya estaba escalado" in texto


def test_el_aviso_al_doctor_no_rompe_el_html_de_telegram():
    """El resumen lo escribe un modelo a partir de lo que dijo un desconocido."""
    ctx = contexto(nombre_paciente="Ana <3 Gómez")
    solicitud = SolicitudEscalamiento(
        motivo="dato_faltante",
        resumen_para_doctor="Preguntó por el precio de algo que no está documentado.",
        pregunta_concreta="¿Cuánto cobramos?",
        clave_idempotencia="conv-1:1",
    )

    aviso = h._aviso_para_doctores(ctx, solicitud)

    assert "<b>" in aviso  # el formato propio sí se conserva
    assert "+573001112233" in aviso


# ==========================================================================================
# El doble del calendario
# ==========================================================================================


def test_borrar_un_evento_que_ya_no_existe_no_es_un_error():
    """Un reintento normal de cancelación no debe acabar escalando a los doctores."""
    calendario = CalendarioDoble()
    evento = calendario.crear_evento(inicio=INICIO, duracion_minutos=60, titulo="x")

    calendario.eliminar_evento(evento)
    calendario.eliminar_evento(evento)  # otra vez: no lanza

    assert evento not in calendario.eventos


def test_el_calendario_de_google_se_niega_a_existir_sin_credenciales():
    """Falla al construirse, no tres pasos después con el cupo del paciente ya tomado."""
    from maxicare_daniela.calendario import CalendarioGoogle

    with pytest.raises(NotImplementedError, match="PENDIENTE"):
        CalendarioGoogle()

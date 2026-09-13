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
import re
from datetime import datetime, timedelta
from typing import get_args

import pytest

from maxicare_daniela import contratos
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


def test_la_rejilla_no_ofrece_bloques_que_ya_pasaron():
    """Una hora que ya pasó no es un hueco libre: es una cita imposible.

    Sin este filtro, un modelo que resuelva mal el año --y hasta hoy no sabía en qué año
    vive-- ofrece el 16 de septiembre del año pasado y nadie lo detiene.
    """
    bloques = bloques_del_dia(
        datetime(2026, 9, 15, 8, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 15, 14, 0, tzinfo=h.ZONA_BOGOTA),
        duracion_minutos=60,
        no_antes_de=datetime(2026, 9, 15, 10, 30, tzinfo=h.ZONA_BOGOTA),
    )

    # Las 10:00 ya empezaron; 8 y 9 quedaron atrás.
    assert [b.hour for b in bloques] == [11, 12, 13]


def test_una_ventana_mas_corta_que_un_bloque_no_se_confunde_con_agenda_llena(monkeypatch):
    """«No cabe una cita en tu franja» y «no hay cupo» son cosas distintas.

    Con la rejilla vacía por ventana corta, la tool decía lo mismo que con la agenda
    saturada. El paciente pedía «el 16 tipo 10 am» con la agenda ENTERAMENTE libre y se iba
    creyendo que no había nada.
    """
    ctx = contexto(ahora=datetime(2026, 9, 16, 7, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(h._consultar_disponibilidad(ctx, "2026-09-16T10:00", "2026-09-16T10:30"))

    assert "No quedan bloques libres" not in texto
    assert "Bloques libres" in texto
    assert "10:00" in texto


def test_la_disponibilidad_no_ofrece_horas_de_hoy_que_ya_pasaron(monkeypatch):
    """Que la rejilla SEPA filtrar el pasado no sirve si la consulta no le pasa la hora.

    Esta prueba existe porque la de la rejilla pasaba con `_huecos_libres` sin cablear: son
    dos cosas distintas y hacían falta las dos.
    """
    ctx = contexto(ahora=datetime(2026, 9, 16, 11, 30, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(h._consultar_disponibilidad(ctx, "2026-09-16T08:00", "2026-09-16T14:00"))

    assert "08:00" not in texto
    assert "09:00" not in texto
    assert "11:00" not in texto  # empezó hace media hora
    assert "12:00" in texto


def test_no_se_agenda_una_cita_en_una_hora_que_ya_paso(monkeypatch):
    """Y se rechaza ANTES de tocar la base: un cupo consumido en el pasado no lo libera nadie."""
    ctx = contexto(ahora=datetime(2026, 9, 15, 12, 0, tzinfo=h.ZONA_BOGOTA))
    toques = []

    async def base_falsa(_ctx, trabajo):
        toques.append("tocó la base")
        return (None, None, [])

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=datetime(2026, 9, 15, 9, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="conv-1:2026-09-15T09:00",
            ),
        )
    )

    assert "ya pasó" in texto
    assert toques == []


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
        # `(cupo, cita_ya_existente, alternativas)`: sin cupo no hay nada que duplicar.
        return (None, None, [INICIO + timedelta(hours=2)])

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


def test_la_clave_del_cupo_la_arma_el_orquestador_y_va_anclada_al_HORARIO(monkeypatch):
    """`crear_cita` era el último sitio del repositorio donde una clave escrita por el modelo
    llegaba a la base, y `tomar_cupo` busca por clave SIN filtrar por `inicio` ni por
    conversación. Eso son tres fallos distintos, todos con el mismo final:

    1. *Misma clave, otro horario.* El paciente pide las 10:00 y luego «mejor a las 15:00».
       El modelo repite la clave --para él es el mismo intento-- y se le devuelve la reserva
       de las 10:00: las 15:00 no consumen cupo (con capacidad 2 se venden 3) y las 10:00
       quedan bloqueadas para nadie. Por eso la clave lleva el `inicio` dentro.
    2. *Colisión entre pacientes.* La columna es UNIQUE global y al modelo se le oculta el
       teléfono a propósito, así que lo natural que puede inventar es
       `cita-2026-09-15T09:00-limpieza`: dos pacientes pidiendo el mismo bloque generan la
       misma cadena y el segundo recibe la reserva del primero. Por eso la clave lleva el
       `id_conversacion` delante.
    3. Y el reintento legítimo del MISMO horario sigue siendo idempotente, que es para lo
       que la clave existía.
    """
    claves: list[str] = []

    def tomar_cupo_falso(_conn, *, inicio, capacidad, clave_idempotencia, conversacion_id=None):
        claves.append(clave_idempotencia)
        return None  # «lleno»: corta el camino antes del calendario y de la base

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "tomar_cupo", tomar_cupo_falso)
    monkeypatch.setattr(h, "_huecos_libres", lambda *a, **k: [])

    def pedir(ctx, inicio):
        asyncio.run(
            h._crear_cita(
                ctx,
                SolicitudCita(
                    nombre_completo="Ana Gómez",
                    inicio=inicio,
                    tratamiento="limpieza",
                    # El modelo repite LA MISMA cadena en los tres casos. Antes eso bastaba
                    # para que las tres reservas se confundieran entre sí.
                    clave_idempotencia="cita-2026-09-15T09:00-limpieza",
                ),
            )
        )

    ana = contexto(id_conversacion="conv-ana")
    beto = contexto(id_conversacion="conv-beto")
    mas_tarde = INICIO + timedelta(hours=6)

    pedir(ana, INICIO)        # el primer intento
    pedir(ana, INICIO)        # el reintento del mismo horario
    pedir(ana, mas_tarde)     # «mejor a las 15:00»
    pedir(beto, INICIO)       # otro paciente, el mismo bloque

    assert "cita-2026-09-15T09:00-limpieza" not in claves, "la clave del modelo llegó a la base"
    assert claves[0] == f"conv-ana:cita:{INICIO.isoformat()}"
    assert claves[0] == claves[1], "el reintento del mismo horario dejó de ser idempotente"
    assert claves[2] != claves[0], "cambiar de hora reusaba la reserva de la hora anterior"
    assert claves[3] != claves[0], "dos pacientes distintos compartían la misma reserva"


def test_el_mismo_intento_no_crea_un_SEGUNDO_evento_en_el_calendario(monkeypatch):
    """`tomar_cupo` era idempotente y la tool no.

    Un acierto de clave devuelve la reserva que YA existía, y el código seguía derecho a
    `crear_evento` + `registrar_cita`. Medido antes del arreglo, llamando dos veces con la
    misma conversación y el mismo horario: una reserva, **dos eventos en el calendario del
    doctor** y dos filas en `citas`, las dos confirmadas al paciente con ids distintos.

    Con la clave anclada al horario esto es mucho menos probable, pero no imposible: el
    modelo puede reintentar la tool dentro del mismo turno.
    """
    calendario = CalendarioDoble()
    ctx = contexto(calendario=calendario)
    liberados: list[int] = []

    # La reserva 77 ya tiene su cita: es lo que devolvería `cita_viva_de_reserva`.
    ya_creada = {
        "id": "cita-original",
        "nombre_completo": "Ana Gómez",
        "tratamiento": "limpieza",
        "evento_calendar_id": "evt-original",
    }

    async def base_falsa(_ctx, trabajo):
        return ((77, 1), ya_creada, [])

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h, "_liberar", lambda _ctx, reserva_id: liberados.append(reserva_id))

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=INICIO,
                tratamiento="limpieza",
                clave_idempotencia="da-igual-lo-que-ponga",
            ),
        )
    )

    assert calendario.eventos == {}, "creó un segundo evento sobre una reserva que ya tenía cita"
    assert "cita-original" in texto, "le daría al paciente un id distinto para la misma hora"
    assert "Cita confirmada" in texto, "el modelo tiene que poder confirmarle igual"
    assert liberados == [], "liberó un cupo que es del paciente y tiene una cita viva"


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
        # Cupo tomado y NINGUNA cita previa en esa reserva: el camino que crea el evento,
        # que es el que esta prueba necesita ver fallar.
        return ((77, 1), None, [])

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
# registrar_estado_oportunidad -- valida contra el vocabulario vivo, no contra el Literal
# ==========================================================================================


def test_registrar_estado_rechaza_lo_que_no_esta_en_el_vocabulario():
    """La validación va ANTES de tocar la base: un tratamiento inválido no debe llegar a
    escribirse, y esta prueba lo comprueba sin necesitar Neon ni un doble de `_con_base`."""
    ctx = contexto()

    with pytest.raises(ValueError, match="no es un tratamiento"):
        asyncio.run(
            h._registrar_estado_oportunidad(
                ctx,
                estado="explorando",
                barrera="ninguna",
                tratamiento="lo_que_sea",
                fuera_de_alcance=False,
                nota=None,
            )
        )


def test_registrar_estado_acepta_un_tratamiento_nuevo_del_vocabulario(monkeypatch):
    """Con el vocabulario recortado por la clínica, un tratamiento que no está en el
    `Literal` original --pero sí en la lista viva-- tiene que pasar igual."""
    ctx = contexto()

    async def base_falsa(_ctx, trabajo):
        return None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    contratos.fijar_vocabulario(["carillas", "implantes"])
    try:
        texto = asyncio.run(
            h._registrar_estado_oportunidad(
                ctx,
                estado="explorando",
                barrera="ninguna",
                tratamiento="carillas",
                fuera_de_alcance=False,
                nota=None,
            )
        )
        assert "guardado" in texto.lower()
    finally:
        contratos.fijar_vocabulario(get_args(contratos.Tratamiento))


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


def test_un_escalamiento_registrado_sin_avisar_se_reintenta_desde_la_TOOL(monkeypatch):
    """El aviso de cierre de turno no basta, y el caso es concreto.

    La tool escribe su fila, Telegram devuelve 502, `failure_error_function` se traga el
    `ErrorDeCanal` para que el modelo siga conversando, y el modelo cierra con
    `requiere_escalamiento=False` --porque cree que ya avisó--. Entonces `responder` no llama
    a `al_escalar`, el reintento de `runtime._avisar_a_doctores` no se alcanza nunca, y
    queda: cero telegrams al doctor, una fila pendiente que nadie va a mirar, y un paciente
    al que se le dijo «ya le estoy avisando al doctor».

    Por eso el reintento tiene que vivir también aquí, que es donde nace el problema.
    """
    ctx = contexto()
    telegram = TelegramFalso()
    anotados: list[tuple[int, int]] = []

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    # La clave ya existe: `insertar_escalamiento` no escribe...
    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "insertar_escalamiento", lambda _c, **kw: None)
    # ...pero esa fila se quedó sin Telegram.
    monkeypatch.setattr(
        h.persistencia, "escalamiento_pendiente_de_aviso", lambda _c, clave: 19
    )
    monkeypatch.setattr(
        h.persistencia,
        "anotar_telegram_en_escalamiento",
        lambda _c, eid, mid: anotados.append((eid, mid)),
    )

    texto = asyncio.run(
        h._escalar_a_doctores(
            ctx,
            SolicitudEscalamiento(
                motivo="clinico",
                resumen_para_doctor="Dice que le duele desde hace tres días.",
                pregunta_concreta="¿Lo citamos hoy mismo?",
                clave_idempotencia="da-igual",
            ),
            telegram=telegram,
        )
    )

    assert len(telegram.enviados) == 1, "el doctor se quedó sin enterarse del escalamiento"
    assert anotados == [(19, 4242)], "el reintento no quedó anotado; se repetiría siempre"
    assert "ya estaba escalado" not in texto


def test_un_escalamiento_YA_avisado_no_se_reintenta_desde_la_tool(monkeypatch):
    """La otra mitad. Sin ella, el arreglo de arriba habría cambiado un aviso perdido por
    un aviso repetido, que es la otra forma de que el doctor deje de mirarlos."""
    ctx = contexto()
    telegram = TelegramFalso()

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "insertar_escalamiento", lambda _c, **kw: None)
    monkeypatch.setattr(
        h.persistencia, "escalamiento_pendiente_de_aviso", lambda _c, clave: None
    )

    texto = asyncio.run(
        h._escalar_a_doctores(
            ctx,
            SolicitudEscalamiento(
                motivo="clinico",
                resumen_para_doctor="Lo mismo de antes.",
                pregunta_concreta="¿Y ahora?",
                clave_idempotencia="da-igual",
            ),
            telegram=telegram,
        )
    )

    assert telegram.enviados == []
    assert "ya estaba escalado" in texto


def test_el_aviso_al_doctor_no_rompe_el_html_de_telegram():
    """El resumen lo escribe un modelo a partir de lo que dijo un desconocido.

    Esta prueba estaba VACUNADA: tenía el nombre bueno, usaba «Ana <3 Gómez» como cebo, y
    luego solo comprobaba `assert "<b>" in aviso`, que es cierto pase lo que pase porque esa
    etiqueta la escribe la propia función. Parecía cobertura y no lo era: el `<3` viajaba sin
    escapar y la prueba seguía en verde.

    Y lo que se pagaba no era un aviso feo. Telegram va en `parse_mode=HTML` y RECHAZA el
    mensaje entero si el HTML no cierra: `canales.enviar_mensaje` lanza `ErrorDeCanal`,
    `failure_error_function` se lo traga, la fila del escalamiento ya está escrita, y el
    aviso de cierre de turno ve la clave quemada y se calla. Dos filas en Neon, CERO Telegram
    al doctor, y un paciente con dolor esperando.
    """
    ctx = contexto(nombre_paciente="Ana <3 Gómez")
    solicitud = SolicitudEscalamiento(
        motivo="dato_faltante",
        resumen_para_doctor="Preguntó por «implante & corona» y por el <precio> de eso.",
        pregunta_concreta="¿Cuánto cobramos?",
        clave_idempotencia="conv-1:1",
    )

    aviso = h._aviso_para_doctores(ctx, solicitud)

    # Lo ajeno va escapado: ni un `<` ni un `&` sueltos del paciente ni del modelo.
    assert "Ana &lt;3 Gómez" in aviso
    assert "&lt;precio&gt;" in aviso
    assert "implante &amp; corona" in aviso
    assert "<3" not in aviso, "el nombre del paciente rompe el HTML del aviso"
    assert "<precio>" not in aviso

    # Y el formato propio sí se conserva: escapar no puede dejar el aviso en texto plano.
    assert aviso.startswith("<b>Escalamiento")
    assert "<b>Pregunta:</b>" in aviso
    assert "+573001112233" in aviso

    # El cinturón: las únicas etiquetas que quedan son las que pone la función.
    assert set(re.findall(r"</?([a-z]+)>", aviso)) == {"b"}


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
    """Falla al construirse, no tres pasos después con el cupo del paciente ya tomado.

    Cuando `CalendarioGoogle` estaba PENDIENTE, esta prueba esperaba `NotImplementedError`.
    La implementación cambió la excepción --ahora es `ErrorDeCalendario`, la misma que
    atrapan las tools-- pero no la propiedad, que es lo que la prueba vigila: sin
    credenciales el objeto no llega a existir. Si algún día alguien hace que el constructor
    tolere una credencial vacía y falle al primer uso, esta prueba tiene que romperse.
    """
    from maxicare_daniela.calendario import CalendarioGoogle, ErrorDeCalendario

    with pytest.raises(ErrorDeCalendario, match="MAXICARE_GOOGLE_CALENDAR_ID"):
        CalendarioGoogle("", "")

    with pytest.raises(ErrorDeCalendario, match="MAXICARE_GOOGLE_SA_B64"):
        CalendarioGoogle("", "agenda@maxicare.example")

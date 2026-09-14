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
from maxicare_daniela.calendario import Bloqueo, CalendarioDoble, Jornada, bloques_del_dia
from maxicare_daniela.contratos import (
    ContextoDaniela,
    SolicitudCancelacion,
    SolicitudCita,
    SolicitudEscalamiento,
)

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

    def commit(self) -> None:
        """La cita y su recordatorio nacen en UNA transacción, así que el `commit` dejó de
        estar dentro de `registrar_cita` y pasó a ser una línea de la tool. Sin este método
        el doble no sirve para probar el camino que de verdad corre en producción."""
        self.llamadas.append("commit")


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


# ==========================================================================================
# La jornada de la clínica: una rejilla sin horario ofrece la madrugada
# ==========================================================================================


def test_la_rejilla_no_sale_del_horario_de_la_clinica():
    """Función pura. L-V de 8 a 17, y el último bloque de 60 min empieza a las 16:00."""
    martes = datetime(2026, 9, 15, 0, 0, tzinfo=h.ZONA_BOGOTA)

    bloques = bloques_del_dia(
        martes,
        martes + timedelta(hours=24),
        duracion_minutos=60,
        jornada=Jornada(),
    )

    assert [b.hour for b in bloques] == [8, 9, 10, 11, 12, 13, 14, 15, 16]


def test_la_jornada_configurada_coincide_con_el_horario_que_daniela_recita():
    """El horario vive en DOS sitios, y esta prueba es el precio de esa decisión.

    Los números de `CONFIGURACION_POR_DEFECTO` filtran la rejilla; la fila `_general`/
    `horario` de la base de conocimiento es lo que Daniela recita cuando le preguntan. Si
    divergen, dice «abrimos hasta las 5» y ofrece hasta las 3, o al revés.

    **Lo que esta prueba NO cubre:** la divergencia que de verdad puede pasar en producción
    es entre la fila de Neon —que la clínica edita desde el panel— y ese mismo texto, y eso
    no se puede comprobar offline. Aquí solo se caza a quien cambie los defaults del código
    sin tocar el documento. Es la mitad del problema, y la otra mitad vive en el panel.
    """
    ruta = persistencia.RUTA_SEMILLA_CONOCIMIENTO
    if not ruta.exists():
        pytest.skip("la base de conocimiento no está en un clone limpio: es gitignored")

    import json

    filas = json.loads(ruta.read_text(encoding="utf-8"))
    horario = next(
        (f for f in filas if f.get("tratamiento") == "_general" and f.get("concepto") == "horario"),
        None,
    )
    assert horario is not None, "no hay fila de horario: Daniela no sabría qué contestar"

    texto = horario["contenido"].lower()
    defecto = persistencia.CONFIGURACION_POR_DEFECTO

    # No se parsea la frase entera --es texto libre y hacerlo sería frágil--: se comprueba
    # que cada número configurado aparezca en ella, en formato de 12 horas, que es como la
    # clínica lo escribe.
    def en_doce_horas(hora: int) -> str:
        return f"{hora - 12 if hora > 12 else hora}:00"

    assert en_doce_horas(defecto["hora_apertura"]) in texto
    assert en_doce_horas(defecto["hora_cierre"]) in texto
    assert en_doce_horas(defecto["hora_cierre_sabado"]) in texto
    assert ("domingo" in texto) == bool(defecto["atiende_domingo"]), (
        "o el texto nombra los domingos y la configuración no los atiende, o al revés"
    )


def test_el_sabado_cierra_antes_y_el_domingo_no_abre():
    """El texto aprobado dice sábados hasta las 3 y no menciona el domingo."""
    sabado = datetime(2026, 9, 19, 0, 0, tzinfo=h.ZONA_BOGOTA)
    domingo = datetime(2026, 9, 20, 0, 0, tzinfo=h.ZONA_BOGOTA)
    dia = timedelta(hours=24)

    del_sabado = bloques_del_dia(sabado, sabado + dia, duracion_minutos=60, jornada=Jornada())
    del_domingo = bloques_del_dia(domingo, domingo + dia, duracion_minutos=60, jornada=Jornada())

    assert [b.hour for b in del_sabado] == [8, 9, 10, 11, 12, 13, 14]
    assert del_domingo == []


def test_la_disponibilidad_del_dia_entero_no_ofrece_la_madrugada(monkeypatch):
    """El fallo real, del 13/09/2026 a las 6:22 p. m., reproducido tal cual.

    El paciente pidió «el próximo martes 15» y «10 am». El modelo llamó a la tool con la
    ventana del día completo --leído de `public.agent_messages`, literal::

        {"desde":"2026-09-15T00:00:00-05:00","hasta":"2026-09-15T23:59:59-05:00"}

    y la rejilla, que no sabía que la clínica tiene horario, devolvió los tres primeros
    bloques de esa ventana: 00:00, 01:00 y 02:00. Daniela se los ofreció como «12:00 am,
    1:00 am o 2:00 am», que es la traducción correcta de unas horas equivocadas.

    Pedir el día entero es razonable cuando el paciente dice «el martes». Lo que no puede
    ser es que el sistema conteste con la madrugada.
    """
    ctx = contexto(ahora=datetime(2026, 9, 13, 18, 22, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(
        h._consultar_disponibilidad(ctx, "2026-09-15T00:00:00", "2026-09-15T23:59:59")
    )

    for madrugada in ("00:00", "01:00", "02:00", "03:00", "04:00", "05:00", "06:00", "07:00"):
        assert madrugada not in texto, f"ofreció las {madrugada}, con la clínica cerrada"
    assert "08:00" in texto, "la primera hora de la jornada sí se ofrece"


def test_no_se_agenda_fuera_del_horario_de_la_clinica(monkeypatch):
    """Ofrecer bien no basta, igual que con los bloqueos: el paciente puede pedir «a las 7».

    Esta prueba afirmaba `tocada == []` --que la base no se tocaba en absoluto-- y eso era un
    proxy de lo que de verdad importa: que no se tome un cupo a las dos de la mañana. Dejó de
    valer cuando este camino pasó a buscar la hora libre más cercana para ofrecérsela al
    paciente, que es una consulta de lectura. Lo que se comprueba ahora es la intención
    directa: `tomar_cupo` no se llama. Leer no es reservar.
    """
    ctx = contexto(ahora=datetime(2026, 9, 13, 18, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(
        persistencia, "tomar_cupo", lambda *a, **kw: pytest.fail("tomó un cupo a las 2 a.m.")
    )

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Jean Chamorro",
                inicio=datetime(2026, 9, 15, 2, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )

    assert "no atiende" in texto


# ==========================================================================================
# Google Calendar es la fuente de la disponibilidad
# ==========================================================================================
#
# Lo que pidió la clínica, en sus palabras: el doctor bloquea de 2 a 5 en SU calendario y
# Daniela deja de ofrecer esas horas; borra el bloqueo y vuelven a estar libres, sin que
# nadie toque nada en el sistema. Las tres pruebas de abajo son esa frase, partida.


def test_un_bloqueo_del_doctor_tapa_esas_horas_en_la_disponibilidad(monkeypatch):
    """El caso literal: 2 p. m. a 5 p. m. apartadas a mano en Google Calendar."""
    calendario = CalendarioDoble()
    calendario.agregar_bloqueo(
        datetime(2026, 9, 16, 14, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 16, 17, 0, tzinfo=h.ZONA_BOGOTA),
        "Cirugía",
    )
    ctx = contexto(
        calendario=calendario, ahora=datetime(2026, 9, 16, 7, 0, tzinfo=h.ZONA_BOGOTA)
    )

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(h._consultar_disponibilidad(ctx, "2026-09-16T13:00", "2026-09-16T18:00"))

    assert "13:00" in texto, "la hora anterior al bloqueo sigue libre"
    # Las 17:00 NO salen, y no es por el bloqueo: la clínica cierra a esa hora, así que un
    # bloque de 60 min que empiece ahí terminaría con todo el mundo fuera. El bloqueo de 2 a
    # 5 y el cierre a las 5 se tocan, y por eso ese día no queda nada después de la una.
    assert "17:00" not in texto
    for tapada in ("14:00", "15:00", "16:00"):
        assert tapada not in texto, f"ofreció {tapada}, que el doctor apartó"


def test_quitar_el_bloqueo_devuelve_esas_horas_sin_tocar_nada_mas(monkeypatch):
    """La otra mitad: el doctor borra el evento y el horario se libera solo.

    No hay caché que invalidar ni estado que sincronizar --`_huecos_libres` le pregunta a
    Calendar en cada consulta--, y esta prueba es lo que impide que alguien meta uno «para
    ahorrar latencia» sin darse cuenta de lo que rompe.
    """
    calendario = CalendarioDoble()
    ctx = contexto(
        calendario=calendario, ahora=datetime(2026, 9, 16, 7, 0, tzinfo=h.ZONA_BOGOTA)
    )

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    consulta = lambda: asyncio.run(
        h._consultar_disponibilidad(ctx, "2026-09-16T13:00", "2026-09-16T18:00")
    )

    calendario.agregar_bloqueo(
        datetime(2026, 9, 16, 14, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 16, 17, 0, tzinfo=h.ZONA_BOGOTA),
        "Cirugía",
    )
    assert "15:00" not in consulta()

    calendario._bloqueos.clear()  # el doctor borra el evento
    assert "15:00" in consulta(), "el horario no volvió a estar disponible"


def test_no_se_agenda_dentro_de_un_bloqueo_del_doctor(monkeypatch):
    """Ofrecer bien no basta: hay que RECHAZAR bien.

    `consultar_disponibilidad` ya respetaba los bloqueos, pero `crear_cita` no los miraba:
    comprobaba la hora pasada y el cupo de Neon, y se iba derecha a `crear_evento`. Dos
    caminos llegaban ahí con una hora bloqueada:

      1. El paciente pide una hora concreta --«las 3»-- y el modelo agenda sin consultar
         antes. Nada se lo impedía: `sin_hora_no_verificada` da por buena toda hora que una
         tool confirma, y la confirmación de `crear_cita` se autoriza a sí misma.
      2. El doctor bloquea DESPUÉS de que Daniela ofreció esa hora y antes de que el
         paciente diga que sí. Es la ventana normal de una conversación por WhatsApp:
         minutos.

    En los dos casos el resultado era una cita encima de la cirugía del doctor, confirmada
    al paciente, y un evento en el calendario donde él ya había dicho que no podía.
    """
    calendario = CalendarioDoble()
    calendario.agregar_bloqueo(
        datetime(2026, 9, 15, 14, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 15, 17, 0, tzinfo=h.ZONA_BOGOTA),
        "Cirugía",
    )
    ctx = contexto(
        calendario=calendario, ahora=datetime(2026, 9, 15, 8, 0, tzinfo=h.ZONA_BOGOTA)
    )
    tocada = []

    async def base_falsa(_ctx, trabajo):
        tocada.append("la base")
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(
        persistencia, "tomar_cupo", lambda conn, **kw: (55, 1)
    )
    monkeypatch.setattr(persistencia, "cita_viva_de_reserva", lambda conn, reserva_id: None)

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=datetime(2026, 9, 15, 15, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )

    assert calendario.eventos == {}, "creó la cita encima del bloqueo del doctor"
    assert "Cita confirmada" not in texto, "se la confirmó al paciente"
    assert tocada == [], "consumió un cupo por una hora que nunca se pudo agendar"


def test_no_se_mueve_una_cita_a_una_hora_bloqueada_por_el_doctor(monkeypatch):
    """Mover una cita encima de la cirugía del doctor es el mismo defecto que crearla ahí.

    Y aquí el camino 2 de `_bloqueo_que_tapa` es todavía más probable: entre que el paciente
    pide cambiar y acepta la hora nueva pasa una conversación entera.
    """
    calendario = CalendarioDoble()
    calendario.agregar_bloqueo(
        datetime(2026, 9, 15, 14, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 15, 17, 0, tzinfo=h.ZONA_BOGOTA),
        "Cirugía",
    )
    ctx = contexto(
        calendario=calendario, ahora=datetime(2026, 9, 15, 8, 0, tzinfo=h.ZONA_BOGOTA)
    )
    tocada = []

    async def base_falsa(_ctx, trabajo):
        tocada.append("la base")
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._reprogramar_cita(ctx, "cita-1", "2026-09-15T15:00"))

    assert "no está disponible" in texto
    assert calendario.eventos == {}, "movió el evento encima del bloqueo"
    assert tocada == [], "tocó la base por una hora que nunca se pudo usar"


def test_no_se_mueve_una_cita_a_una_hora_que_ya_paso(monkeypatch):
    """`crear_cita` lo comprobaba desde el 13/09 y `reprogramar_cita` no, y aquí duele más.

    Crear una cita en el pasado deja una cita fantasma sobre un cupo que nadie libera.
    MOVER una al pasado hace eso **y además destruye una cita buena**: `mover_cita` la lleva
    al día que ya pasó, `liberar_cupo` suelta el cupo bueno que el paciente tenía, y
    `mover_evento` arrastra el evento del calendario del doctor detrás. El paciente se queda
    sin la cita que sí tenía, y Daniela se lo confirma como si fuera un cambio normal.

    Los dos caminos que llegan aquí son los de siempre: el paciente que dice «muévela para
    ayer» y el modelo resolviendo mal una fecha relativa.

    Se comprueba ANTES que el bloqueo del doctor, no después: es una comparación local y el
    bloqueo cuesta una llamada a Google. Una hora del pasado no merece esa llamada.
    """
    calendario = CalendarioDoble()
    ctx = contexto(
        calendario=calendario, ahora=datetime(2026, 9, 15, 12, 0, tzinfo=h.ZONA_BOGOTA)
    )
    tocada = []

    async def base_falsa(_ctx, trabajo):
        tocada.append("la base")
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._reprogramar_cita(ctx, "cita-1", "2026-09-15T09:00"))

    assert "ya pasó" in texto
    assert tocada == [], "tocó la base por una hora que nunca se pudo usar"
    assert calendario.eventos == {}, "arrastró el evento del doctor al pasado"


def test_el_seguimiento_mira_el_reloj_DEL_TURNO_y_no_el_de_la_maquina(monkeypatch):
    """`ctx.ahora` existe para que ninguna tool llame a `datetime.now()` por su cuenta.

    Lo dice su propio docstring en `contratos.py`: viaja en el contexto «por la misma razón
    que `calendario`: una prueba lo fija y el comportamiento deja de depender del reloj de la
    máquina». `programar_seguimiento` usaba `_ahora()` y era la única que se salía.

    En producción los dos valen casi lo mismo, así que esto no es un fallo que se vea: es la
    grieta por la que la política declarada deja de ser verdad. Con el reloj de la máquina
    esta prueba no se puede escribir, y lo que no se puede probar acaba divergiendo.
    """
    ctx = contexto(ahora=datetime(2027, 1, 1, 8, 0, tzinfo=h.ZONA_BOGOTA))
    tocada = []

    async def base_falsa(_ctx, trabajo):
        tocada.append("la base")
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)

    # Futuro para el reloj de la máquina, pasado para el turno.
    texto = asyncio.run(
        h._programar_seguimiento(ctx, "recordatorio_cita", "2026-12-01T09:00")
    )

    assert "ya pasó" in texto
    assert tocada == [], "programó un recordatorio para antes del turno que lo pide"


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


def test_agendar_registra_al_paciente_y_lo_deja_verificado(monkeypatch):
    """Quien saca su primera cita deja de ser un desconocido. En ese mismo instante.

    Sin esto, el arreglo del 13/09/2026 dejaba a medio camino a la persona que acababa de
    agendar: `crear_cita` se le permitía --no tiene ficha, no hay datos que proteger-- pero
    `reprogramar_cita` y `cancelar_cita` seguían exigiendo identidad, y esa identidad no
    podía llegar nunca, porque `identificar_paciente` solo verifica contra filas de
    `pacientes` y nadie creaba la suya. Medido en vivo: pedía moverla y recibía «te escribe
    el doctor»; pedía cancelarla, lo mismo. El mismo callejón sin salida, movido de sitio.

    `asegurar_paciente` llevaba escrito desde la fase 1 --con su docstring sobre no pisar el
    nombre de quien ya existe-- y ninguna tool lo llamaba. Esto es ese cable.

    El efecto secundario importa tanto como el principal: la cita deja de guardarse con
    `paciente_id = NULL`, que era la fila que la comprobación de pertenencia de
    `reprogramar`/`cancelar` trataba como de cualquiera.
    """
    ctx = contexto(identidad_verificada=False, telefono_sin_paciente=True, id_paciente=None)
    registrados: list[tuple[str, str]] = []
    guardadas: list[dict] = []
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ((77, 1), None, [])  # `tomar`: cupo libre y ninguna cita previa
        return trabajo(BaseFalsa())  # `guardar`: corre de verdad contra los dobles

    def asegurar(conn, *, nombre_completo, telefono):
        registrados.append((nombre_completo, telefono))
        return 42

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "asegurar_paciente", asegurar)
    monkeypatch.setattr(
        h.persistencia,
        "registrar_cita",
        lambda conn, **kw: (guardadas.append(kw), "cita-nueva")[1],
    )
    # Lo mismo que en `_descripcion_del_evento`: esta prueba mira la ficha del paciente, no
    # la cola, pero `guardar` corre entero contra los dobles.
    monkeypatch.setattr(h.persistencia, "insertar_seguimiento", lambda conn, **kw: True)

    texto = asyncio.run(
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

    assert "cita-nueva" in texto
    assert registrados == [("Ana Gómez", "573001112233")], "no registró al paciente"
    assert guardadas[0]["paciente_id"] == 42, "la cita quedaría con paciente_id NULL"
    # Y el contexto deja de mentir en el mismo turno: la ficha ya existe en la base, así que
    # `_leer_estado` lo daría por verificado en el siguiente. Dejarlo en False aquí haría que
    # el turno en curso siguiera creyendo que es un desconocido.
    assert ctx.id_paciente == 42
    assert ctx.nombre_paciente == "Ana Gómez"
    assert ctx.telefono_sin_paciente is False
    assert ctx.identidad_verificada is True


def test_la_oferta_de_un_dia_entero_llega_hasta_la_TARDE(monkeypatch):
    """Visto en producción el 13/09/2026:

        «Sí, tengo disponibilidad el miércoles 16 a las 8:00 am, 9:00 am o 10:00 am.»

    Con el día entero libre. `_huecos_libres` cortaba en los seis primeros bloques SEGUIDOS
    --08:00 a 13:00-- así que la tarde no llegaba siquiera al modelo: no es que la
    descartara, es que no existía para él. El paciente que solo puede después de almorzar se
    iba creyendo que no había nada.
    """
    ctx = contexto(ahora=datetime(2026, 9, 16, 6, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(
        h._consultar_disponibilidad(ctx, "2026-09-16T00:00:00", "2026-09-16T23:59:59")
    )

    assert "08:00" in texto, "la primera hora del día tiene que seguir estando"
    de_la_tarde = [hora for hora in ("14:00", "15:00", "16:00") if hora in texto]
    assert de_la_tarde, f"ninguna hora de la tarde en la oferta: {texto}"


def test_pero_las_alternativas_de_una_hora_llena_siguen_siendo_las_MAS_CERCANAS(monkeypatch):
    """El reparto es para «¿qué tienes el miércoles?», no para «esa hora está llena».

    Son preguntas distintas: la primera pide un panorama del día, la segunda pide lo más
    parecido a la hora que el paciente ya eligió. Repartir ahí le ofrecería las cinco de la
    tarde a quien acaba de pedir las nueve de la mañana.
    """
    ctx = contexto(ahora=datetime(2026, 9, 16, 6, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    libres = asyncio.run(
        h._proximos_huecos(ctx, datetime(2026, 9, 16, 8, 0, tzinfo=h.ZONA_BOGOTA))
    )

    assert libres[:3] == [
        datetime(2026, 9, 16, 8, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 16, 9, 0, tzinfo=h.ZONA_BOGOTA),
        datetime(2026, 9, 16, 10, 0, tzinfo=h.ZONA_BOGOTA),
    ], "dejaron de ser las más cercanas"


# ==========================================================================================
# Pedir una hora con la clínica cerrada no es un callejón sin salida
# ==========================================================================================
#
# Pedido por MaxiCare el 13/09/2026: «cuando alguien pregunte por un horario fuera del
# horario de atención, que le recuerde los horarios y le ofrezca un horario cercano y libre».
#
# Lo que lo hacía imposible no era el texto: era `sin_hora_no_verificada`. «Atendemos de 8:00
# a 5:00» son DOS horas concretas, y toda hora del mensaje al paciente tiene que haberla
# devuelto una tool en este mismo turno. Sin autorizarlas, recordarle el horario al paciente
# bloquea el mensaje entero y lo que recibe es «te escribe el doctor».


def _fuera_de_horario(monkeypatch, ctx, cuando: datetime, libres: list[datetime]):
    """Corre `_crear_cita` con una hora fuera de jornada. Devuelve (texto, cupos tomados)."""
    cupos: list[str] = []

    async def base_falsa(_ctx, trabajo):
        cupos.append("tomó cupo")
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia, "bloques_ocupados", lambda conn, desde, hasta: {}
    )
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda *a, **kw: pytest.fail("tomó cupo"))

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=cuando,
                tratamiento="limpieza",
                clave_idempotencia="clave-suficientemente-larga",
            ),
        )
    )
    return texto


def test_fuera_de_horario_recuerda_el_horario_y_ofrece_lo_mas_cercano(monkeypatch):
    """Antes decía «no atiende» y «consulta la disponibilidad», y ahí se acababa: el paciente
    que escribía a las 7 de la mañana se quedaba sin ninguna hora concreta."""
    ctx = contexto(ahora=datetime(2026, 9, 14, 6, 0, tzinfo=h.ZONA_BOGOTA))

    texto = _fuera_de_horario(
        monkeypatch, ctx, datetime(2026, 9, 14, 7, 0, tzinfo=h.ZONA_BOGOTA), []
    )

    assert "8:00" in texto and "17:00" in texto, "no le recordó el horario"
    # Y le da algo concreto: las 8:00 del mismo día son la hora libre más cercana a las 7:00.
    assert "08:00" in texto, "no le ofreció ninguna hora concreta"


def test_el_horario_que_recuerda_queda_AUTORIZADO(monkeypatch):
    """La prueba que sostiene toda la funcionalidad.

    Sin esto el texto es correcto y da igual: `sin_hora_no_verificada` compara contra
    `ctx.turno.horas_autorizadas`, y una hora que ninguna tool devolvió bloquea la respuesta
    entera. Recordarle el horario al paciente le costaría un «te escribe el doctor».
    """
    ctx = contexto(ahora=datetime(2026, 9, 14, 6, 0, tzinfo=h.ZONA_BOGOTA))

    _fuera_de_horario(monkeypatch, ctx, datetime(2026, 9, 14, 7, 0, tzinfo=h.ZONA_BOGOTA), [])

    assert "08:00" in ctx.turno.horas_autorizadas, "no podría decir a qué hora abren"
    assert "17:00" in ctx.turno.horas_autorizadas, "no podría decir a qué hora cierran"


def test_la_noche_ofrece_el_dia_siguiente_y_no_se_queda_muda(monkeypatch):
    """A las 7 de la tarde no queda nada del mismo día: la ventana de alternativas son ocho
    horas y todas caen con la clínica cerrada. Si la búsqueda no cruzara el día, el paciente
    que escribe de noche --que es cuando la gente escribe-- no recibiría ninguna hora."""
    ctx = contexto(ahora=datetime(2026, 9, 14, 18, 0, tzinfo=h.ZONA_BOGOTA))

    texto = _fuera_de_horario(
        monkeypatch, ctx, datetime(2026, 9, 14, 19, 0, tzinfo=h.ZONA_BOGOTA), []
    )

    assert "15/9" in texto, "no ofreció ninguna hora del día siguiente"


def test_la_disponibilidad_distingue_CERRADO_de_LLENO(monkeypatch):
    """Dos situaciones opuestas con el mismo texto llevan al modelo a lo contrario de lo que
    toca: «no hay cupo» manda al paciente a buscar otro DÍA cuando lo que necesita es otra
    HORA del mismo día."""
    ctx = contexto(ahora=datetime(2026, 9, 14, 6, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(
        h._consultar_disponibilidad(ctx, "2026-09-14T19:00:00", "2026-09-14T20:00:00")
    )

    assert "fuera del horario" in texto, f"lo contó como falta de cupo: {texto}"
    assert "no hay cupo" not in texto


def test_reprogramar_fuera_de_horario_tambien_ofrece_algo(monkeypatch):
    """El mismo hueco en la tool de al lado. Es el patrón que ya costó dos arreglos."""
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 6, 0, tzinfo=h.ZONA_BOGOTA), identidad_verificada=True
    )

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})

    texto = asyncio.run(
        h._reprogramar_cita(ctx, "una-cita", "2026-09-14T07:00:00")
    )

    assert "fuera del horario" in texto
    assert "08:00" in texto, "no le ofreció a dónde moverla"
    assert "08:00" in ctx.turno.horas_autorizadas


def test_el_horario_de_la_base_de_conocimiento_tambien_queda_autorizado(monkeypatch):
    """El otro camino por el que llega el horario, y tenía el mismo defecto.

    `consultar_base_conocimiento` registraba `cifras_autorizadas` --para que Daniela pueda
    decir un precio-- y no `horas_autorizadas`. Así que el paciente que pregunta «¿a qué hora
    abren?» recibía la respuesta aprobada por MaxiCare... y el guardrail la bloqueaba por
    contener horas que ninguna tool había verificado.
    """
    ctx = contexto()

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia,
        "consultar_conocimiento",
        lambda conn, tratamiento, pregunta=None: "[HORARIO] Lunes a viernes de 8:00 a 17:00.",
    )

    asyncio.run(h._consultar_base_conocimiento(ctx, "_general", "horario"))

    assert {"08:00", "17:00"} <= ctx.turno.horas_autorizadas


# ==========================================================================================
# Qué le dice el evento del calendario a la clínica
# ==========================================================================================
#
# Pedido por MaxiCare el 13/09/2026: que en el evento vayan «el nombre, el número de
# teléfono y por qué agendó, o sea el servicio que está interesado». Hasta ese día la
# descripción entera era «Agendado por Daniela. Conversación <uuid>»: con eso no se puede
# llamar a nadie si hay que mover una cita o avisar de una urgencia.


def _descripcion_del_evento(monkeypatch, ctx, solicitud) -> str:
    """Corre `_crear_cita` completa contra los dobles y devuelve lo que quedó en Calendar."""
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ((77, 1), None, [])  # cupo libre y ninguna cita previa
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "asegurar_paciente", lambda conn, **kw: 42)
    monkeypatch.setattr(h.persistencia, "registrar_cita", lambda conn, **kw: "cita-nueva")
    # `guardar` programa el recordatorio en la misma transacción desde la tarea 4. Estas
    # pruebas no miran la cola, pero sí corren `guardar` de verdad: sin el doble, la
    # inserción llegaría a `BaseFalsa` buscando un cursor.
    monkeypatch.setattr(h.persistencia, "insertar_seguimiento", lambda conn, **kw: True)

    asyncio.run(h._crear_cita(ctx, solicitud))
    (descripcion,) = ctx.calendario.descripciones.values()
    return descripcion


def test_el_evento_lleva_el_telefono_del_paciente(monkeypatch):
    """Y lo pone el CÓDIGO, desde `ctx.telefono_completo`.

    `SolicitudCita` no tiene campo de teléfono a propósito --lo comprueba
    `test_solicitud_cita_no_tiene_campo_de_telefono`-- justamente para que el modelo no
    pueda escribir otro número: el caso real es la hija agendando por su madre. Que el
    número del evento salga del webhook y no del texto significa que el doctor que marque
    ese número le está marcando a quien escribió.
    """
    ctx = contexto(telefono_completo="573001112233")

    descripcion = _descripcion_del_evento(
        monkeypatch,
        ctx,
        SolicitudCita(
            nombre_completo="Ana Gómez",
            inicio=INICIO,
            tratamiento="limpieza",
            clave_idempotencia="clave-suficientemente-larga",
        ),
    )

    assert "573001112233" in descripcion, "el doctor no tiene a qué número llamar"


def test_el_evento_dice_por_que_agendo(monkeypatch):
    """El motivo, con las palabras del paciente. Es lo que distingue «cordales» de «vengo a
    que me valoren las cordales porque me duele al masticar»."""
    ctx = contexto()

    descripcion = _descripcion_del_evento(
        monkeypatch,
        ctx,
        SolicitudCita(
            nombre_completo="Ana Gómez",
            inicio=INICIO,
            tratamiento="diseno_sonrisa",
            clave_idempotencia="clave-suficientemente-larga",
            motivo="Valoración para diseño de sonrisa; no le gustan sus dientes de adelante.",
        ),
    )

    assert "diseño de sonrisa" in descripcion
    assert "dientes de adelante" in descripcion


def test_sin_motivo_el_evento_igual_dice_el_servicio(monkeypatch):
    """`motivo` es opcional, y por eso no puede ser la única fuente del «por qué».

    El servicio ya viaja en `tratamiento`, que la tool SIEMPRE recibe. Hacerlo obligatorio
    habría movido los veinticinco sitios que construyen una `SolicitudCita` sin comprar
    ninguna garantía que el código no dé ya.
    """
    ctx = contexto()

    descripcion = _descripcion_del_evento(
        monkeypatch,
        ctx,
        SolicitudCita(
            nombre_completo="Ana Gómez",
            inicio=INICIO,
            tratamiento="limpieza",
            clave_idempotencia="clave-suficientemente-larga",
        ),
    )

    assert "limpieza" in descripcion, "el evento no dice a qué viene el paciente"


def test_el_nombre_sigue_en_el_titulo(monkeypatch):
    """Lo que se ve en la vista de mes sin abrir el evento. MaxiCare pidió que «siga»."""
    ctx = contexto()

    _descripcion_del_evento(
        monkeypatch,
        ctx,
        SolicitudCita(
            nombre_completo="Ana Gómez",
            inicio=INICIO,
            tratamiento="limpieza",
            clave_idempotencia="clave-suficientemente-larga",
        ),
    )

    (evento,) = ctx.calendario.eventos.values()
    assert "Ana Gómez" in evento[2]


def test_una_cita_de_otro_telefono_no_se_puede_cancelar(monkeypatch):
    """La pertenencia se comprueba por TELÉFONO, no solo por `paciente_id`.

    La comprobación era `if ctx.id_paciente is not None and cita["paciente_id"] not in
    (None, ctx.id_paciente)`, y tenía dos huecos que hasta el 13/09/2026 no se podían
    alcanzar porque el guardrail frenaba antes a todo el que no tuviera ficha:

    1. Con `ctx.id_paciente is None` no se comprobaba NADA.
    2. Una cita con `paciente_id = NULL` se daba por buena para cualquiera.

    El arreglo de los pacientes nuevos volvía el hueco 2 alcanzable --esas citas pasaban a
    ser las normales-- así que el candado se ata a lo que de verdad identifica al dueño: el
    número desde el que se escribe. El id de una cita es un UUID que solo conoce quien lo
    recibió, pero «difícil de adivinar» no es un control de acceso.
    """
    ctx = contexto(identidad_verificada=True, id_paciente=None)
    ajena = {
        "id": "cita-de-otro",
        "reserva_id": 9,
        "paciente_id": None,
        "telefono": "573009998877",  # NO es el de `contexto()`
        "estado": "confirmada",
        "evento_calendar_id": "evt-1",
        "inicio": INICIO,
    }
    eliminados: list[str] = []

    async def base_falsa(_ctx, trabajo):
        return ajena

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        ctx.calendario, "eliminar_evento", lambda evento_id: eliminados.append(evento_id)
    )

    texto = asyncio.run(
        h._cancelar_cita(ctx, SolicitudCancelacion(
            id_cita="cita-de-otro", motivo="porque sí", clave_idempotencia="cita-de-otro"
        ))
    )

    assert "no pertenece" in texto
    assert "NO la canceles" in texto
    assert eliminados == [], "borró del calendario la cita de otra persona"


def test_la_cita_del_propio_telefono_si_se_puede_cancelar(monkeypatch):
    """El falso positivo de la anterior: sin esta, una comprobación que devolviera «ajena»
    siempre pasaría aquella y dejaría a todo el mundo sin poder cancelar."""
    ctx = contexto(identidad_verificada=True, id_paciente=None)
    propia = {
        "id": "cita-propia",
        "reserva_id": 9,
        "paciente_id": None,
        "telefono": "573001112233",  # el mismo de `contexto()`
        "estado": "confirmada",
        "evento_calendar_id": "evt-1",
        "inicio": INICIO,
    }
    eliminados: list[str] = []
    aplicados: list[str] = []

    async def base_falsa(_ctx, trabajo):
        if not aplicados:
            aplicados.append("leida")
            return propia
        return None

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        ctx.calendario, "eliminar_evento", lambda evento_id: eliminados.append(evento_id)
    )

    texto = asyncio.run(
        h._cancelar_cita(ctx, SolicitudCancelacion(
            id_cita="cita-propia", motivo="porque sí", clave_idempotencia="cita-propia"
        ))
    )

    assert "cancelada" in texto
    assert eliminados == ["evt-1"]


def test_reprogramar_autoriza_LAS_DOS_horas_la_vieja_y_la_nueva(monkeypatch):
    """Confirmar un cambio de hora exige decir las dos, y decir la vieja hacía saltar el freno.

    Medido en vivo el 13/09/2026: la cita se movió de las 10:00 a las 14:00 --la base lo
    confirma-- y el paciente recibió «te escribe el doctor», porque Daniela escribió «antes
    tenías a las 10, ahora a las 2» y solo las 14:00 estaban autorizadas. `ctx.turno` se vacía
    en cada turno, así que las 10:00 que autorizó `crear_cita` ayer ya no valen hoy, y hacen
    bien en no valer.

    El resultado es el peor de los posibles: la cita SE MOVIÓ y al paciente le dijimos que no
    pasó nada. Se presenta a las 10:00 a un cupo que acabamos de liberar.

    La hora vieja no es un recuerdo del modelo: sale de `leer_cita`, de la base, en este mismo
    turno. Autorizarla no debilita nada -- es exactamente el mismo criterio que autoriza la
    nueva.
    """
    ctx = contexto(identidad_verificada=True, id_paciente=None)
    cita = {
        "id": "cita-1",
        "reserva_id": 5,
        "paciente_id": None,
        "telefono": "573001112233",
        "estado": "confirmada",
        "evento_calendar_id": None,
        "inicio": INICIO,  # 15/09 09:00
    }
    destino = INICIO + timedelta(hours=5)  # 14:00
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", cita, (6, 1), [])
        return None  # `aplicar`

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._reprogramar_cita(ctx, "cita-1", destino.isoformat()))

    assert "14:00" in texto
    assert "09:00" in texto, "sin la hora vieja en el texto, el modelo no puede citarla"
    assert {"09:00", "14:00"} <= ctx.turno.horas_autorizadas


def test_cancelar_autoriza_la_hora_que_acaba_de_cancelar(monkeypatch):
    """El mismo fallo y peor consecuencia: la cita queda cancelada y el paciente no se entera.

    «Tu cita del martes a las 2 quedó cancelada» es la frase natural, y esa hora no estaba
    autorizada por nada -- el texto de la tool no la nombraba--, así que el guardrail bloqueaba
    la confirmación de algo que YA había ocurrido. El paciente se presenta a una cita que no
    existe y el cupo ya está libre para otro.
    """
    ctx = contexto(identidad_verificada=True, id_paciente=None)
    cita = {
        "id": "cita-1",
        "reserva_id": 5,
        "paciente_id": None,
        "telefono": "573001112233",
        "estado": "confirmada",
        "evento_calendar_id": None,
        "inicio": INICIO,
    }
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        return cita if len(pasos) == 1 else None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(
        h._cancelar_cita(
            ctx, SolicitudCancelacion(id_cita="cita-1", motivo=None, clave_idempotencia="cita-1")
        )
    )

    assert "09:00" in texto
    assert "09:00" in ctx.turno.horas_autorizadas


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
# consultar_citas -- encontrar la cita sin que el paciente recite el UUID
# ==========================================================================================


def _cita(**cambios) -> dict:
    base = {
        "id": "cita-1",
        "nombre_completo": "Ana Ruiz",
        "telefono": "573001112233",
        "tratamiento": "limpieza",
        "inicio": INICIO,
        "estado": "confirmada",
        # Las tres que hacen falta para contrastar contra Calendar. `evento_calendar_id` va
        # en None a proposito: asi una cita sin evento --las hay-- no llama a Google, y las
        # pruebas que no van de esto no tienen que doblar el calendario.
        "evento_calendar_id": None,
        "reserva_id": None,
        "conversacion_id": "conv-1",
    }
    base.update(cambios)
    return base


def test_sin_citas_no_se_inventa_ninguna(monkeypatch):
    """El texto tiene que servirle al modelo para seguir, no para rellenar el hueco.

    Y no autoriza ninguna hora: si Daniela nombrara una después de esto, saldría de su
    memoria y el guardrail debe frenarla.
    """
    ctx = contexto(identidad_verificada=True)

    async def base_falsa(_ctx, trabajo):
        return []

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._consultar_citas(ctx))

    assert "no tiene" in texto.lower()
    assert not re.search(r"\d{1,2}:\d{2}", texto), "no puede aparecer una hora de la nada"
    assert ctx.turno.horas_autorizadas == set()


def test_una_cita_futura_vuelve_con_su_hora_su_tratamiento_y_su_id(monkeypatch):
    """El id es lo que `reprogramar_cita` y `cancelar_cita` necesitan para existir."""
    ctx = contexto(identidad_verificada=True)

    async def base_falsa(_ctx, trabajo):
        return [_cita(id="c49b580e", tratamiento="ortodoncia")]

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._consultar_citas(ctx))

    assert "09:00" in texto
    assert "ortodoncia" in texto
    assert "Ana Ruiz" in texto
    assert "c49b580e" in texto


def test_consultar_citas_autoriza_las_horas_que_nombra(monkeypatch):
    """Sin esto, «tu cita es el martes a las 9» bloquea la respuesta ENTERA.

    Es el mismo defecto que costó el arreglo del 13/09/2026 en `reprogramar` y `cancelar`:
    una hora que sale de la base en este mismo turno y que nadie autorizó. La consecuencia
    aquí es más tonta y más visible -- el paciente pregunta cuándo es su cita y recibe «te
    escribe el doctor».
    """
    ctx = contexto(identidad_verificada=True)

    async def base_falsa(_ctx, trabajo):
        return [_cita(), _cita(id="cita-2", inicio=INICIO + timedelta(days=1, hours=5))]

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(h._consultar_citas(ctx))

    assert {"09:00", "14:00"} <= ctx.turno.horas_autorizadas


def test_la_busqueda_va_anclada_al_TELEFONO_del_contexto(monkeypatch):
    """El modelo no escribe teléfonos. Nunca.

    Es lo que hace la consulta incapaz por construcción de devolver la cita de otra persona:
    no hay ningún argumento que el modelo pueda torcer.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(days=2))
    visto: dict = {}

    def falsa(conn, telefono, *, desde):
        visto["telefono"] = telefono
        visto["desde"] = desde
        return []

    monkeypatch.setattr(persistencia, "citas_activas_de_telefono", falsa)

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(h._consultar_citas(ctx))

    assert visto["telefono"] == "573001112233"


def test_la_consulta_mira_DOS_DIAS_hacia_atras_antes_de_contrastar(monkeypatch):
    """La consulta cortaba el pasado con `ctx.ahora`, y eso dejaba fuera justo la cita que hay
    que corregir: la que el doctor arrastro de ayer a manana en Google Calendar. Segun Neon
    esta en el pasado; segun Calendar, en el futuro. Ahora el SQL mira dos dias hacia atras y
    el pasado se corta DESPUES de contrastar."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO)
    visto: dict = {}

    def falsa(conn, telefono, *, desde):
        visto["desde"] = desde
        return []

    monkeypatch.setattr(persistencia, "citas_activas_de_telefono", falsa)

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(h._consultar_citas(ctx))

    assert visto["desde"] == ctx.ahora - timedelta(days=h.DIAS_HACIA_ATRAS_AL_SINCRONIZAR)


def test_una_cita_que_SIGUE_en_el_pasado_no_se_ofrece(monkeypatch):
    """Mirar dos dias hacia atras no puede convertirse en ofrecer citas de ayer: una cita
    pasada no se puede mover, y ofrecerla solo sirve para que el modelo proponga un imposible.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO)
    ayer = INICIO - timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (ayer, 60, "limpieza")

    async def base_falsa(_ctx, trabajo):
        return [_cita(inicio=ayer, evento_calendar_id="ev-1")]

    monkeypatch.setattr(h, "_con_base", base_falsa)

    salida = asyncio.run(h._consultar_citas(ctx))

    assert "no tiene ninguna cita futura" in salida


# ==========================================================================================
# Calendar manda: la cita que el doctor movio a mano
#
# «Movi manualmente una cita que estaba en Calendar, la pase para el dia siguiente, pero el
# agente responde con la informacion de antes» -- 14/09/2026.
#
# El calendario de los doctores no es un espejo de Neon: es donde trabajan. Arrastrar una
# cita con el raton es el gesto natural, y nadie va a abrir el panel despues para repetirlo.
# ==========================================================================================


def _sincronizando(monkeypatch, ctx, citas, escrituras):
    """Dobla `_con_base` para que la lectura devuelva `citas` y apunte lo que se escriba."""
    llamadas = {"n": 0}

    async def base_falsa(_ctx, trabajo):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return citas
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia,
        "mover_cita",
        lambda conn, id_cita, *, reserva_id, inicio: escrituras.append(
            ("mover", id_cita, reserva_id, inicio)
        ),
    )
    monkeypatch.setattr(
        persistencia,
        "marcar_cita_cancelada",
        lambda conn, id_cita, *, motivo=None: escrituras.append(("cancelar", id_cita, motivo)),
    )
    monkeypatch.setattr(
        persistencia,
        "liberar_cupo",
        lambda conn, reserva_id: escrituras.append(("liberar", reserva_id)),
    )
    monkeypatch.setattr(
        persistencia,
        "tomar_cupo",
        lambda conn, **kw: (99, 1),
    )


def test_si_la_movieron_en_calendar_Daniela_dice_la_hora_NUEVA(monkeypatch):
    """EL FALLO. Neon decia una hora, Calendar otra, y Daniela recitaba la de Neon: el
    paciente se presenta cuando ya no le toca."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )

    texto = asyncio.run(h._consultar_citas(ctx))

    assert f"{manana.day}/{manana.month}" in texto, texto
    # La hora vieja SI sale, pero solo en el aviso de que la clinica la movio. Lo que no
    # puede pasar es que siga figurando como la cita del paciente: eso es lo que lo mandaria
    # a la clinica el dia que no le toca.
    lista = texto.split("Citas activas de este número:")[1]
    assert f"{INICIO.day}/{INICIO.month} a las" not in lista, "recito la hora vieja"


def test_al_moverla_se_corrige_Neon_y_se_cambia_el_CUPO(monkeypatch):
    """La hora que Daniela dice y la hora que la clinica tiene apartada son el mismo hecho.
    Sin mover el cupo, la hora vieja sigue contando como llena y la nueva como libre -- o sea
    que Daniela podria darle a otro paciente una hora que ya esta ocupada."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )

    asyncio.run(h._consultar_citas(ctx))

    assert ("mover", "cita-1", 99, manana) in escrituras
    assert ("liberar", 7) in escrituras


def test_si_el_horario_destino_esta_lleno_la_cita_se_mueve_IGUAL(monkeypatch):
    """El doctor ya la metio ahi, en el calendario que el mira. Negarle esa realidad a Neon
    solo consigue que Daniela vuelva a decir la hora vieja. Queda sin cupo y con un warning:
    es lo que un humano puede ver y corregir."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: None)

    texto = asyncio.run(h._consultar_citas(ctx))

    assert ("mover", "cita-1", None, manana) in escrituras
    assert f"{manana.day}/{manana.month}" in texto


def test_si_la_borraron_del_calendario_la_cita_se_cancela(monkeypatch):
    """Borrar el evento es como la clinica cancela. Sin esto, Daniela le confirma al paciente
    una cita que ya no existe en ninguna agenda."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-borrado", reserva_id=7)],
        escrituras,
    )

    texto = asyncio.run(h._consultar_citas(ctx))

    assert ("liberar", 7) in escrituras
    assert any(e[0] == "cancelar" for e in escrituras)
    assert "no tiene ninguna cita futura" in texto


def test_si_Calendar_NO_RESPONDE_se_contesta_con_lo_que_dice_Neon(monkeypatch):
    """La regla del no negociable 1 mirada desde el otro lado: ante la duda, nunca inventar.
    Un timeout tratado como «la borraron» cancelaria citas buenas en silencio."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    ctx.calendario.fallar_en.add("obtener_evento")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )

    texto = asyncio.run(h._consultar_citas(ctx))

    assert escrituras == [], "un fallo de Calendar no puede escribir nada"
    assert "09:00" in texto


def test_una_cita_SIN_evento_no_le_pregunta_nada_a_Google(monkeypatch):
    """Las hay: se crean asi cuando Calendar falla en mitad de un relevo. No hay con que
    contrastarlas, y una llamada por cada una seria latencia a cambio de nada."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    ctx.calendario.fallar_en.add("obtener_evento")  # si preguntara, reventaria
    escrituras: list = []
    _sincronizando(monkeypatch, ctx, [_cita(inicio=INICIO)], escrituras)

    texto = asyncio.run(h._consultar_citas(ctx))

    assert "09:00" in texto and escrituras == []


# ------------------------------------------------------------------------------------------
# Y ademas hay que DECIRLE al modelo lo que acaba de pasar
#
# «¿Podria decirme cuando tengo cita de nuevo?» -- «En este momento no me aparece una cita
# futura registrada. Ya estoy confirmando ese punto con el equipo.» El paciente habia borrado
# el evento a mano, asi que la cancelacion era correcta. Lo que no era correcto es el aviso al
# doctor, y el propio modelo dejo escrito por que escalo:
#
#     «La consulta actual no muestra citas futuras, AUNQUE EN TURNOS PREVIOS del mismo chat
#      aparecia una cita de cordales para el 15/09 a las 4:00 pm.»
#     «Confirmar si la cita fue cancelada o si requiere correccion en el sistema.»
#
# Medido en produccion: la cita se cancelo a las 18:43:48.220 y el escalamiento se registro a
# las 18:43:53.933. CINCO SEGUNDOS, el mismo turno. El modelo le pregunto al doctor algo que
# su propio turno acababa de resolver, porque la tool tiro la respuesta a la basura y le
# devolvio el texto generico de «no hay citas».
#
# Escalar ante una contradiccion es lo correcto: quien ve que el sistema se desdice y no sabe
# por que, llama a un humano. El arreglo no es ensenarle a callarse, es quitarle la
# contradiccion.
# ------------------------------------------------------------------------------------------


def test_si_la_borraron_el_texto_DICE_QUE_LA_BORRO_LA_CLINICA(monkeypatch):
    """Sin esto la cita desaparece sin explicacion y el modelo escala -- con razon."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-borrado", reserva_id=7)],
        escrituras,
    )

    texto = asyncio.run(h._consultar_citas(ctx))

    assert "clínica" in texto, texto
    assert "CANCELADA" in texto
    # La hora vieja tiene que aparecer: es la que el paciente oyo el turno pasado, y es lo
    # unico que le permite al modelo atar una cosa con la otra en vez de ver un hueco.
    assert "9:00" in texto
    assert "no es un error" in texto, "hay que descartarle explicitamente el fallo de sistema"


def test_si_la_movieron_el_texto_dice_que_la_movio_LA_CLINICA(monkeypatch):
    """Misma contradiccion, al reves: el turno pasado dijo una hora y ahora dice otra.

    Aqui no llego a escalar en produccion, pero la trampa es identica y depende de la suerte
    del modelo. Decirselo cuesta una linea."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )

    texto = asyncio.run(h._consultar_citas(ctx))

    assert "clínica" in texto and "movió" in texto, texto
    assert "no es un error" in texto


def test_si_NO_cambio_nada_el_texto_no_lleva_ninguna_novedad(monkeypatch):
    """El caso normal es que Calendar y Neon digan lo mismo. Un aviso en cada consulta seria
    ruido que el modelo acabaria repitiendole al paciente: «tu cita sigue donde estaba»."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    ctx.calendario.eventos["ev-1"] = (INICIO, 60, "limpieza")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )

    texto = asyncio.run(h._consultar_citas(ctx))

    assert "clínica" not in texto and "no es un error" not in texto, texto


def test_consultar_citas_exige_identidad_como_las_tres_de_escritura():
    """Lee datos de un paciente, así que cae del lado estricto del guardrail.

    No estorba el caso normal: desde que `crear_cita` registra al paciente, todo teléfono con
    cita tiene ficha, y con ficha la identidad queda verificada sola. Y NO entra en la lista
    blanca de desconocidos: esa sigue teniendo una sola tool.
    """
    from maxicare_daniela import guardrails

    nombres = {g.name for g in h.consultar_citas.tool_input_guardrails or []}

    assert "identidad_antes_de_datos" in nombres
    assert "consultar_citas" not in guardrails._ESCRITURAS_PARA_DESCONOCIDO


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
# El recordatorio lo emite el código, y la cascada lo sigue cuando la cita cambia
# ==========================================================================================


def test_crear_cita_programa_el_recordatorio_sin_que_el_modelo_lo_pida(monkeypatch):
    """El no-negociable 2 aplicado a otro caso: lo que tiene que ocurrir siempre no lo decide
    el modelo.

    Hasta hoy el recordatorio dependía de que el modelo llamara a `programar_seguimiento`, y ni
    el prompt de `agentes.py` ni `crear_cita` la mencionaban. En la práctica la cola estaba
    vacía: la tool existía desde la fase 3 y nadie la llamaba.
    """
    programados: list[dict] = []
    ctx = contexto(ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (55, 1))
    monkeypatch.setattr(persistencia, "cita_viva_de_reserva", lambda conn, reserva_id: None)
    monkeypatch.setattr(persistencia, "asegurar_paciente", lambda conn, **kw: 7)
    monkeypatch.setattr(persistencia, "registrar_cita", lambda conn, **kw: "cita-nueva")

    def _insertar(conn, **kwargs):
        programados.append(kwargs)
        return True

    monkeypatch.setattr(persistencia, "insertar_seguimiento", _insertar)

    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                # Tres días vista: cae en la banda de la víspera.
                inicio=datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )

    assert "Cita confirmada" in texto
    assert len(programados) == 1, "la cita se creó sin recordatorio"
    assert programados[0]["tipo"] == "recordatorio_cita"
    assert programados[0]["cita_id"] == "cita-nueva"
    # La víspera a las 18:00, no «24 horas antes».
    assert programados[0]["fecha_objetivo"] == datetime(
        2026, 9, 16, 18, 0, tzinfo=h.ZONA_BOGOTA
    )
    # La clave la arma `ctx.clave`, nunca el modelo: lleva el id de la conversación delante.
    assert programados[0]["clave_idempotencia"].startswith("conv-1:")
    assert "da-igual" not in programados[0]["clave_idempotencia"]


def test_una_cita_a_dos_horas_no_deja_recordatorio(monkeypatch):
    """El piso de las 4 horas, desde la tool: el paciente acaba de hablar con Daniela."""
    programados: list[dict] = []
    ctx = contexto(ahora=datetime(2026, 9, 15, 9, 0, tzinfo=h.ZONA_BOGOTA))

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (55, 1))
    monkeypatch.setattr(persistencia, "cita_viva_de_reserva", lambda conn, reserva_id: None)
    monkeypatch.setattr(persistencia, "asegurar_paciente", lambda conn, **kw: 7)
    monkeypatch.setattr(persistencia, "registrar_cita", lambda conn, **kw: "cita-nueva")
    monkeypatch.setattr(
        persistencia,
        "insertar_seguimiento",
        lambda conn, **kw: programados.append(kw) or True,
    )

    asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=datetime(2026, 9, 15, 11, 0, tzinfo=h.ZONA_BOGOTA),
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )

    assert programados == []


def test_cancelar_cita_anula_su_recordatorio(monkeypatch):
    """Sin esto, el paciente que canceló recibe la víspera un recordatorio de la cita que
    canceló. Es el fallo que `cita_id` existe para hacer detectable."""
    anulados: list[tuple[str, str]] = []
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA),
        id_paciente=7,
        identidad_verificada=True,
    )

    cita = {
        "id": "cita-1",
        "reserva_id": 55,
        "conversacion_id": "conv-1",
        "paciente_id": 7,
        "nombre_completo": "Ana Gómez",
        "telefono": "573001112233",
        "tratamiento": "limpieza",
        "inicio": datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA),
        "duracion_minutos": 60,
        "evento_calendar_id": None,
        "estado": "confirmada",
    }

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "leer_cita", lambda conn, id_cita: cita)
    monkeypatch.setattr(persistencia, "marcar_cita_cancelada", lambda conn, id_cita, **k: None)
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    monkeypatch.setattr(
        persistencia,
        "anular_seguimientos_de_cita",
        lambda conn, cita_id, **k: (anulados.append((cita_id, k["motivo"])), 1)[1],
    )

    asyncio.run(
        h._cancelar_cita(
            ctx,
            SolicitudCancelacion(
                id_cita="cita-1", motivo="no puedo ir", clave_idempotencia="cita-1"
            ),
        )
    )

    assert anulados == [("cita-1", "cita_cancelada")]


def test_reprogramar_anula_el_recordatorio_viejo_y_deja_uno_nuevo(monkeypatch):
    """La otra mitad de la cascada: mover la cita sin mover su recordatorio le recuerda al
    paciente la hora de la que acaba de salir.

    La clave del recordatorio nuevo lleva el DESTINO dentro. Sin él, la segunda reprogramación
    de la misma cita chocaría con la clave de la primera y `insertar_seguimiento` la
    descartaría en silencio: la cita movida dos veces se quedaría sin recordatorio.
    """
    anulados: list[tuple[str, str]] = []
    programados: list[dict] = []
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA),
        identidad_verificada=True,
    )
    cita = {
        "id": "cita-1",
        "reserva_id": 5,
        "paciente_id": None,
        "telefono": "573001112233",
        "estado": "confirmada",
        "evento_calendar_id": None,
        "inicio": datetime(2026, 9, 16, 9, 0, tzinfo=h.ZONA_BOGOTA),
    }
    destino = datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA)
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", cita, (6, 1), [])
        return trabajo(BaseFalsa())  # `aplicar`: corre de verdad contra los dobles

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "mover_cita", lambda conn, id_cita, **kw: None)
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    monkeypatch.setattr(
        persistencia,
        "anular_seguimientos_de_cita",
        lambda conn, cita_id, **k: (anulados.append((cita_id, k["motivo"])), 1)[1],
    )
    monkeypatch.setattr(
        persistencia,
        "insertar_seguimiento",
        lambda conn, **kw: programados.append(kw) or True,
    )

    asyncio.run(h._reprogramar_cita(ctx, "cita-1", destino.isoformat()))

    assert anulados == [("cita-1", "cita_reprogramada")]
    assert len(programados) == 1, "la cita se movió y se quedó sin recordatorio"
    assert programados[0]["cita_id"] == "cita-1"
    assert programados[0]["fecha_objetivo"] == datetime(
        2026, 9, 16, 18, 0, tzinfo=h.ZONA_BOGOTA
    )
    assert destino.isoformat() in programados[0]["clave_idempotencia"]


# ==========================================================================================
# programar_seguimiento
# ==========================================================================================


def test_no_se_programa_nada_sobre_una_conversacion_que_tiene_un_doctor(monkeypatch):
    """El doctor podría estar acordando otra fecha en ese mismo momento."""
    ctx = contexto()

    async def base_falsa(_ctx, trabajo):
        return ("Dr. Martínez", False)

    monkeypatch.setattr(h, "_con_base", base_falsa)

    futuro = (ctx.ahora + timedelta(days=2)).isoformat()
    texto = asyncio.run(h._programar_seguimiento(ctx, "recordatorio_cita", futuro))

    assert "No se programó nada" in texto
    assert "Dr. Martínez" in texto


def test_no_se_programa_un_seguimiento_hacia_atras():
    ctx = contexto()
    pasado = (ctx.ahora - timedelta(days=1)).isoformat()

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

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
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

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
from maxicare_daniela import seguimientos
from maxicare_daniela.calendario import Bloqueo, CalendarioDoble, Jornada, bloques_del_dia
from maxicare_daniela.contratos import (
    ContextoDaniela,
    SolicitudCancelacion,
    SolicitudCita,
    SolicitudEscalamiento,
)

INICIO = datetime(2026, 9, 15, 9, 0, tzinfo=h.ZONA_BOGOTA)

#: El "ahora" de estas pruebas. Lunes, en hora hábil, y un día ANTES de `INICIO`.
#:
#: Tiene que estar clavado. `ContextoDaniela.ahora` cae al reloj de verdad si nadie se lo
#: da, y con `INICIO` fijo en el 15/09/2026 estas pruebas se pusieron en ROJO solas al dar
#: la medianoche del 16: `_crear_cita` dejó de ver una hora llena y pasó a ver una hora
#: PASADA, así que doce comprobaciones empezaron a medir otra cosa sin que nadie tocara
#: nada. Es la tercera vez que este proyecto tropieza con una fecha que envejece --las otras
#: dos fueron `probar_tools.hora` y el bloque 10 de `probar_agentes`-- y es la primera en la
#: suite offline, que es justamente la red que tenía que avisar.
#:
#: Quien quiera probar el pasado le pasa su propio `ahora`: `base.update(cambios)` va
#: después, así que lo explícito sigue mandando.
AHORA = datetime(2026, 9, 14, 8, 0, tzinfo=h.ZONA_BOGOTA)


def contexto(**cambios) -> ContextoDaniela:
    base = dict(
        id_conversacion="conv-1",
        telefono_completo="573001112233",
        database_url="postgresql://no-se-usa",
        calendario=CalendarioDoble(),
        ahora=AHORA,
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
# Los tres respaldos de la búsqueda: encontrar lo aprobado, sin inventar nada
# ==========================================================================================
#
# El defecto que cierran, medido el 22/09/2026 en producción: «¿qué vale la consulta?» acabó
# en «lo estoy confirmando con el equipo» y en DOS escalamientos `dato_faltante` al doctor,
# con el precio de la valoración aprobado y cargado desde siempre en `_general`/`valoracion`.
# Daniela preguntó `valoracion`/`precio`, que desde la 020 es una clave de tratamiento válida
# y no tiene ni una ficha.
#
# La invariante que estas pruebas protegen NO es «encuentra más cosas»: es que encontrar más
# no puede significar inventar. Por eso la última de la tanda es la de endodoncia.


def _base_falsa_de(fichas: dict[tuple[str, str], str]):
    """Un doble de `persistencia.consultar_conocimiento` sobre un diccionario de fichas.

    Respeta las dos formas de llamarla --con concepto y sin él-- porque la diferencia entre
    «esta ficha no está» y «este tratamiento no tiene ninguna» es lo que decide cuál de los
    respaldos entra.
    """

    def _consultar(conn, tratamiento, concepto=None):
        if concepto:
            filas = [(c, t) for (tr, c), t in fichas.items()
                     if tr == tratamiento and c == concepto]
        else:
            filas = [(c, t) for (tr, c), t in fichas.items() if tr == tratamiento]
        if not filas:
            que = f"«{concepto}» de {tratamiento}" if concepto else f"«{tratamiento}»"
            return persistencia.SIN_DATO.format(que=que)
        return "\n\n".join(f"[{c.upper()}] {t}" for c, t in sorted(filas))

    return _consultar


def _busqueda_falsa_de(fichas: dict[tuple[str, str], str]):
    """Un doble de `persistencia.leer_conocimiento_por_palabra` sobre el mismo diccionario.

    Hace falta uno SEGUNDO, y esa es la trampa de este andamio: el respaldo por palabra no
    pasa por `consultar_conocimiento`, así que doblar solo aquella dejaba a la cascada
    llamando a Neon con el `object()` que estas pruebas usan como conexión. Lo cazó
    `test_la_ficha_entera_sigue_siendo_el_respaldo_cuando_lo_general_no_sabe` con un
    `AttributeError`, que es la forma ruidosa de enterarse; la silenciosa habría sido un
    doble que dijera «no hay» a todo y dejara el respaldo nuevo sin probar.

    Reusa la función REAL de partir en palabras --no la reimplementa-- porque lo que estas
    pruebas fijan es la cascada, no el emparejado: duplicarlo aquí dejaría pasar un cambio de
    criterio en `_palabras_clave` con toda la tanda en verde.
    """

    def _buscar(conn, tratamiento, concepto):
        buscadas = persistencia._palabras_clave(concepto)
        ambitos = {tratamiento, persistencia.TRATAMIENTO_GENERAL}
        return [
            persistencia.FilaConocimiento(tr, c, t, True)
            for (tr, c), t in sorted(fichas.items())
            if tr in ambitos and persistencia._palabras_clave(c) & buscadas
        ]

    return _buscar


VALORACION = (
    "La valoración tiene un valor de $40.000, y ese valor se abona al tratamiento que el "
    "paciente necesite y se realice después."
)


def _preguntar(monkeypatch, fichas, tratamiento, concepto):
    ctx = contexto()

    async def _corre(_ctx, trabajo):
        return trabajo(object())

    monkeypatch.setattr(persistencia, "consultar_conocimiento", _base_falsa_de(fichas))
    monkeypatch.setattr(
        persistencia, "leer_conocimiento_por_palabra", _busqueda_falsa_de(fichas)
    )
    monkeypatch.setattr(h, "_con_base", _corre)
    return ctx, asyncio.run(h._consultar_base_conocimiento(ctx, tratamiento, concepto))


def test_el_precio_de_la_valoracion_se_encuentra_aunque_se_pregunte_por_su_clave(monkeypatch):
    """`valoracion`/`precio` -> `_general`/`valoracion`. El caso medido, tal cual.

    Antes de esto: SIN DATO DOCUMENTADO, «lo confirmo con el equipo» y un escalamiento por
    un dato que MaxiCare tenía escrito.
    """
    ctx, texto = _preguntar(
        monkeypatch,
        {("_general", "valoracion"): VALORACION, ("cordales", "precio"): "Una cordal $300.000."},
        "valoracion",
        "precio",
    )

    assert "$40.000" in texto
    assert "SIN DATO" not in texto
    assert "40000" in ctx.turno.cifras_autorizadas, (
        "sin autorizar la cifra, `sin_cifra_no_documentada` bloquea la respuesta entera y "
        "el paciente recibe «te escribe el doctor»"
    )


def test_el_respaldo_que_contesta_NO_borra_el_hueco_de_la_medicion(monkeypatch):
    """Que el doctor deje de ser interrumpido no significa que el hueco no exista.

    La señal sale de la consulta EXACTA y de ninguna otra, así que el informe de «sin
    resolver» sigue viendo `falta_dato:valoracion:precio` y alguien puede decidir crearle su
    ficha. Taparlo también para la medición es cómo un hueco se vuelve invisible.
    """
    ctx, _ = _preguntar(
        monkeypatch, {("_general", "valoracion"): VALORACION}, "valoracion", "precio"
    )

    assert [(s.tratamiento, s.concepto, s.hubo_dato) for s in ctx.turno.senales] == [
        ("valoracion", "precio", False)
    ]


def test_lo_aprobado_como_general_contesta_la_pregunta_hecha_sobre_un_tratamiento(monkeypatch):
    """`cordales`/`valoracion` -> `_general`/`valoracion`, y no la ficha de cordales.

    Es la regla que pidió MaxiCare el 22/09/2026: los $40.000 de la valoración aplican a
    CUALQUIER tratamiento y se abonan a todos. Un hecho general contesta igual de bien la
    pregunta hecha sobre uno, y por eso gana a volcar la ficha entera del tratamiento --que
    es mucho texto sin la única frase que se preguntó--.
    """
    _, texto = _preguntar(
        monkeypatch,
        {
            ("_general", "valoracion"): VALORACION,
            ("cordales", "precio"): "Una cordal $300.000.",
            ("cordales", "duracion"): "45 minutos.",
        },
        "cordales",
        "valoracion",
    )

    assert "$40.000" in texto
    assert "$300.000" not in texto


def test_la_ficha_entera_sigue_siendo_el_respaldo_cuando_lo_general_no_sabe(monkeypatch):
    """El respaldo que ya existía no se pierde por añadir los otros dos."""
    _, texto = _preguntar(
        monkeypatch,
        {("ortodoncia", "precio"): "Desde $3.500.000.", ("_general", "sede"): "Transversal 57."},
        "ortodoncia",
        "cuota_mensual",
    )

    assert "$3.500.000" in texto
    assert "Transversal" not in texto, "el respaldo por concepto no debe colar lo que no se pidió"


#: Las fichas con las que se mide el respaldo por palabra. Son las claves REALES de la base
#: de MaxiCare, con el contenido cambiado: `medios_pago` y `horario` existen así, y son
#: exactamente los dos nombres que Daniela no acertó el 23/09/2026.
_COMO_LA_BASE_REAL = {
    ("_general", "medios_pago"): "Efectivo, Bre-B, Daviplata o Nequi. Tarjeta con 5% extra.",
    ("_general", "financiacion"): "Solo ortodoncia. Para lo demás NO hay financiación.",
    ("_general", "horario"): "Lunes a viernes de 8:00 a 17:00.",
    ("_general", "politica_precios"): "Informar únicamente las tarifas del catálogo.",
    ("implantes", "precio"): "Fase quirúrgica $1.900.000.",
    ("implantes", "duracion"): "Seis meses entre fase y fase.",
}


@pytest.mark.parametrize(
    "tratamiento, pregunta, esperado",
    [
        # El caso literal del 23/09/2026: Vladimir preguntó por implantes «y si tienen
        # facilidades de pago», y el hueco se anotó como si la clínica no tuviera el dato.
        ("_general", "formas de pago", "MEDIOS_PAGO"),
        ("implantes", "formas de pago", "MEDIOS_PAGO"),
        ("implantes", "facilidades de pago", "MEDIOS_PAGO"),
        # El otro caso del mismo informe, que es un plural y nada más.
        ("_general", "horarios", "HORARIO"),
        # El modelo escribe como se habla; las claves se guardan sin tildes.
        ("_general", "financiación", "FINANCIACION"),
    ],
)
def test_el_nombre_de_la_ficha_ya_no_hay_que_acertarlo_entero(
    monkeypatch, tratamiento, pregunta, esperado
):
    """La búsqueda es `concepto = %s`, y el par lo tiene que ACERTAR el modelo en texto libre.

    Medido en producción el 23/09/2026: «formas de pago» no encuentra `medios_pago` y
    «horarios» no encuentra `horario`. El primero se salvó por la ficha entera de `_general`;
    el mismo caso sobre un tratamiento con fichas propias no se salvaba, porque esa ficha
    entera contesta y corta la cascada antes de mirar en `_general`.
    """
    _, texto = _preguntar(monkeypatch, _COMO_LA_BASE_REAL, tratamiento, pregunta)

    assert f"[{esperado.replace('_', ' ')}]" in texto
    assert "SIN DATO" not in texto


def test_la_ficha_entera_del_tratamiento_ya_no_tapa_lo_que_solo_sabe_general(monkeypatch):
    """El agujero que cerró el respaldo por palabra, en una sola prueba.

    `implantes` TIENE fichas, así que el respaldo de la ficha entera contesta --con precio y
    duración, ninguno sobre pagos-- y cortaba la búsqueda antes de llegar a `_general`. El
    paciente recibía información abundante y ni una palabra de lo que preguntó.
    """
    _, texto = _preguntar(monkeypatch, _COMO_LA_BASE_REAL, "implantes", "formas de pago")

    assert "Bre-B" in texto
    assert "$1.900.000" not in texto, (
        "volvió a contestar con la ficha entera de implantes, que no dice nada de pagos"
    )


def test_una_coincidencia_EXACTA_le_gana_a_una_por_palabra(monkeypatch):
    """El orden de la cascada en una frase: primero todo lo exacto, después lo aproximado.

    Es la regresión que se midió al escribir esto. Con el respaldo por palabra delante de
    `_general`/<tratamiento>, `valoracion`/`precio` lo interceptaba la palabra «precio» y
    devolvía `politica_precios` -- en vez de los $40.000 de `_general`/`valoracion`, que es
    el caso que ese respaldo existe para resolver desde el 22/09/2026.
    """
    _, texto = _preguntar(
        monkeypatch,
        {**_COMO_LA_BASE_REAL, ("_general", "valoracion"): VALORACION},
        "valoracion",
        "precio",
    )

    assert "$40.000" in texto
    assert "POLITICA PRECIOS" not in texto


def test_un_tratamiento_MUDO_no_lo_desmudece_una_palabra_suelta(monkeypatch):
    """La guarda que mantiene callada a la endodoncia, y por qué no es una optimización.

    Un tratamiento sin NI UNA ficha no es uno que se olvidó documentar: es uno sobre el que
    MaxiCare decidió no decir nada (sección 2.12). Sin esta guarda, `endodoncia`/`precio`
    engancha `_general`/`politica_precios` por la palabra «precio». No lleva cifras, pero
    sustituye el literal `SIN DATO DOCUMENTADO` -- que es lo que ORDENA no estimar y escalar.
    Medido contra la base real antes de ponerla.
    """
    _, texto = _preguntar(monkeypatch, _COMO_LA_BASE_REAL, "endodoncia", "precio")

    assert texto.startswith("SIN DATO DOCUMENTADO")
    assert "POLITICA PRECIOS" not in texto


def test_el_respaldo_por_palabra_TAMPOCO_borra_el_hueco_de_la_medicion(monkeypatch):
    """Lo mismo que ya valía para los otros tres: la señal sale de la consulta EXACTA.

    Que Daniela encuentre el dato no significa que la ficha se llame como la gente pregunta.
    El informe tiene que seguir viendo `falta_dato:_general:formas de pago` para que alguien
    pueda decidir renombrarla o darle un alias.
    """
    ctx, texto = _preguntar(monkeypatch, _COMO_LA_BASE_REAL, "_general", "formas de pago")

    assert "Bre-B" in texto
    assert [(s.concepto, s.hubo_dato) for s in ctx.turno.senales] == [
        ("formas de pago", False)
    ]


def test_endodoncia_sigue_MUDA_con_los_tres_respaldos_puestos(monkeypatch):
    """La invariante que no se puede perder al ampliar la búsqueda.

    La sección 2.12 del documento maestro dice que endodoncia y prótesis no tienen precio
    documentado y que no debe publicarse ninguna cifra. Buscar en más sitios no puede acabar
    encontrando algo donde MaxiCare decidió que no hay nada: los tres respaldos devuelven
    filas aprobadas o no devuelven nada.
    """
    _, texto = _preguntar(
        monkeypatch,
        {
            ("_general", "valoracion"): VALORACION,
            ("_general", "politica_precios"): "Informar únicamente las tarifas del catálogo.",
            ("cordales", "precio"): "Una cordal $300.000.",
        },
        "endodoncia",
        "precio",
    )

    assert texto.startswith("SIN DATO DOCUMENTADO")
    assert "PROHIBIDO estimar" in texto
    assert not re.search(r"\d", texto.split("PROHIBIDO")[0].replace("MaxiCare", "")), (
        f"se coló una cifra en la respuesta de un tratamiento sin precio: {texto}"
    )


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

    # Futuro para el reloj de la máquina, pasado para el turno. `reactivacion_sin_agendar` y
    # no `recordatorio_cita`: desde la tarea 2, ese tipo ya no es encolable por esta vía --lo
    # emite el código al crear o mover la cita-- y esta prueba no es sobre el vocabulario.
    texto = asyncio.run(
        h._programar_seguimiento(ctx, "reactivacion_sin_agendar", "2026-12-01T09:00")
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


def test_una_ficha_PENDIENTE_se_trata_como_un_numero_sin_registrar(monkeypatch):
    """El marcador del relevo no es un nombre, y esta tool es donde más caro costaba.

    Con la ficha en `PENDIENTE`, `nombre_registrado` no era `None`, así que
    `telefono_sin_paciente` se quedaba en `False` y el paciente perdía el permiso de crear su
    PRIMERA cita (`_ESCRITURAS_PARA_DESCONOCIDO`). Encima la respuesta le decía al modelo que
    el nombre «no coincide con el registrado», que es lo que hacía a Daniela pedirlo otra vez
    «tal como aparece en tu historia clínica» -- a alguien que nunca ha venido.

    Producción, 16/09/2026: dos intentos gastados y escalamiento, sobre una paciente que solo
    quería una valoración.
    """
    ctx = contexto()

    async def base_falsa(_ctx, trabajo):
        return (False, persistencia.NOMBRE_PENDIENTE, 18)

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._identificar_paciente(ctx, "Sora Patricia Delgado"))

    assert ctx.telefono_sin_paciente is True, "perdió el permiso de su primera cita"
    assert "no está registrado" in texto
    assert "puedes agendarle una cita nueva" in texto.lower()
    # Y lo que NO debe decir: nada de volver a pedir el nombre contra un registro que no
    # existe. Esa frase es la que el paciente leyó como «usted ya está en el sistema».
    assert "historia clínica" not in texto.lower()


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
    monkeypatch.setattr(
        h.persistencia, "reiniciar_seguimientos_fallidos", lambda conn, telefono, **kw: None
    )

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
    # Lo mismo desde la tarea 5: el reset del contador corre dentro de la misma transacción.
    monkeypatch.setattr(
        h.persistencia, "reiniciar_seguimientos_fallidos", lambda conn, telefono, **kw: None
    )

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
    """Dobla `_con_base` para que la lectura devuelva `citas` y apunte lo que se escriba.

    Devuelve la `ColaFalsa` instalada: la corrección de una cita que movieron en Calendar
    arrastra su recordatorio, igual que la arrastra `reprogramar_cita`.
    """
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
        lambda conn, id_cita, *, reserva_id, inicio, commit=True: escrituras.append(
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
    return ColaFalsa().instalar(monkeypatch)


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


def test_al_moverla_en_Calendar_el_recordatorio_se_mueve_CON_ella(monkeypatch):
    """G1 caza el BORRADO de una cita, no su movimiento.

    Cuando el doctor arrastra la cita en su calendario, el recordatorio viejo sigue apuntando
    a una cita que existe y cuyo estado es valido: ninguna de las tres primeras guardas del
    despachador lo descarta, y el paciente recibia la vispera el recordatorio de una hora que
    ya nadie tiene apartada. La cascada es la misma que ya hace `reprogramar_cita`.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)  # miercoles 16/9 a las 9:00
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    cola = _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)],
        escrituras,
    )
    # El recordatorio de la hora vieja, el que dejo `crear_cita`.
    cola.insertar(
        None,
        clave_idempotencia="conv-1:recordatorio:cita-1",
        cita_id="cita-1",
        fecha_objetivo=INICIO - timedelta(days=1),
    )

    asyncio.run(h._consultar_citas(ctx))

    vivos = cola.vivos("cita-1")
    assert len(vivos) == 1, "la cita quedo corregida y con el recordatorio de la hora vieja"
    # La vispera de la hora NUEVA, a las 18:00.
    assert vivos[0]["fecha_objetivo"] == datetime(2026, 9, 15, 18, 0, tzinfo=h.ZONA_BOGOTA)
    assert [f["anulado"] for f in cola.filas if f["clave"] == "conv-1:recordatorio:cita-1"] == [
        "movida_en_calendar"
    ]


def test_sincronizar_dos_veces_en_el_mismo_turno_no_deja_la_cita_sin_recordatorio(monkeypatch):
    """El orden de la cascada --insertar y despues anular perdonando-- mirado desde aqui.

    Al reves, el segundo paso anularia su propia fila y chocaria al reinsertarla
    (`ON CONFLICT DO NOTHING`): cita corregida y CERO recordatorios vivos.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    cita = _cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)
    cola = ColaFalsa().instalar(monkeypatch)

    # Harness propio y no `_sincronizando`: aqui se llama dos veces a la sincronizacion, asi
    # que `_con_base` tiene que EJECUTAR el trabajo siempre, sin alternar con una lectura.
    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia,
        "mover_cita",
        lambda conn, id_cita, *, reserva_id, inicio, commit=True: None,
    )
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (99, 1))

    asyncio.run(h._sincronizar_con_calendar(ctx, [dict(cita)]))
    asyncio.run(h._sincronizar_con_calendar(ctx, [dict(cita)]))

    assert len(cola.vivos("cita-1")) == 1


def test_un_fallo_calculando_el_recordatorio_no_impide_corregir_la_cita(monkeypatch):
    """Esto es un camino de LECTURA: el paciente pregunto por sus citas.

    La cita corregida vale mas que el recordatorio, siempre. Un fallo calculando el momento
    que le devolviera a Daniela un error en vez de sus citas invierte el orden de importancia
    del proyecto -- y ademas dejaria a Neon diciendo una hora y a Calendar otra.
    """
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
    _que_reviente(monkeypatch)

    texto = asyncio.run(h._consultar_citas(ctx))

    assert ("mover", "cita-1", 99, manana) in escrituras
    assert f"{manana.day}/{manana.month}" in texto


def test_al_moverla_no_se_libera_el_cupo_que_la_cita_acaba_de_tomar(monkeypatch):
    """`tomar_cupo` devuelve la reserva que YA existia cuando la clave se repite.

    Liberarla es borrarle a la cita el cupo que esta usando: `citas.reserva_id` es
    `ON DELETE SET NULL`, asi que el DELETE no falla ni lanza, la hora vuelve a contarse libre
    y la clinica vende una plaza de mas.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    _sincronizando(
        monkeypatch,
        ctx,
        [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=99)],
        escrituras,
    )  # el doble de `tomar_cupo` devuelve (99, 1): la MISMA reserva que ya tiene la cita

    asyncio.run(h._consultar_citas(ctx))

    assert ("liberar", 99) not in escrituras, "se libero el cupo que la cita esta usando"


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


# ==========================================================================================
# El nucleo de la reconciliacion: los mismos datos, sin las frases
#
# La agenda del panel necesita esto mismo, y no puede leer prosa. `reconciliar_con_calendar`
# devuelve `Correccion`es; `_sincronizar_con_calendar` las traduce a las frases de Daniela.
# Lo que estas pruebas sostienen es la frontera: los textos no se mueven un caracter, y el
# nucleo sirve para dias que Daniela nunca mira.
# ==========================================================================================


def _reconciliando(monkeypatch, ctx, escrituras):
    """Como `_sincronizando`, pero para quien entra directo a la reconciliacion.

    `_sincronizando` gasta la PRIMERA llamada a `_con_base` devolviendo las citas, porque
    quien llama alli es `_consultar_citas`, que lee antes de contrastar. Aqui no hay lectura
    previa, asi que `_con_base` tiene que EJECUTAR el trabajo desde la primera vez: si no, la
    escritura no llega a ocurrir y una prueba que mira una ausencia pasaria por no haber
    llegado nunca hasta donde tenia que llegar.

    Devuelve la `ColaFalsa` que instalo `_sincronizando`.
    """
    cola = _sincronizando(monkeypatch, ctx, [], escrituras)

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    return cola


def test_el_texto_de_la_novedad_no_cambia_al_refactorizar(monkeypatch):
    """Si alguien toca las frases, esto cae. Son lo que Daniela le dice al paciente.

    Caracter a caracter y con la hora VIEJA dentro (no negociable 20): sin ella el modelo ve
    un hueco en vez de una explicacion y no puede atar lo que dijo el turno pasado con lo que
    lee ahora. Eso fue un escalamiento real cinco segundos despues de la correccion.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)  # miercoles 16/9 a las 9:00
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    escrituras: list = []
    _reconciliando(monkeypatch, ctx, escrituras)

    _, novedades = asyncio.run(
        h._sincronizar_con_calendar(
            ctx, [_cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)]
        )
    )

    assert novedades == [
        "La clínica movió en su calendario la cita de limpieza: estaba para "
        "el martes 15/9 a las 09:00 y ahora es el "
        "miércoles 16/9 a las 09:00. Es un hecho confirmado, no es un error del "
        "sistema: si en un mensaje anterior le dijiste la hora vieja, corrígesela."
    ]


def test_el_texto_de_la_cancelacion_no_cambia_al_refactorizar(monkeypatch):
    """La otra frase, la del evento que la clinica borro. Mismo motivo y mismo caracter."""
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    escrituras: list = []
    _reconciliando(monkeypatch, ctx, escrituras)

    _, novedades = asyncio.run(
        h._sincronizar_con_calendar(
            ctx, [_cita(inicio=INICIO, evento_calendar_id="ev-borrado", reserva_id=7)]
        )
    )

    assert novedades == [
        "La clínica eliminó de su calendario la cita de limpieza que "
        "estaba para el martes 15/9 a las 09:00, así que acaba de quedar "
        "CANCELADA. Es un hecho confirmado, no es un error del sistema y no hay nada "
        "que verificar: díselo al paciente con naturalidad, discúlpate por el cambio "
        "y ofrécele buscar otro horario."
    ]


def test_una_cita_PASADA_no_se_cancela_porque_le_borraran_el_evento(monkeypatch):
    """EL FALLO: el doctor limpia de Calendar los eventos de ayer y la agenda los cancela.

    Una cita cancelada ya no se puede MARCAR, y la marca de asistencia es la unica señal que
    distingue una cita cumplida de una a la que el paciente no llego -- el criterio de exito
    entero del proyecto. El paciente fue, el doctor ordeno su calendario, y el dato se perdio
    sin un error en ningun log.

    Hacia el futuro la cancelacion SI sirve y se queda como esta
    (`test_la_correccion_de_una_cita_borrada_no_lleva_hora_nueva`): borrar el evento es como
    la clinica cancela. Lo que no puede es reescribir el pasado, que ya ocurrio y sobre el que
    Calendar no manda: lo que paso a las 10:00 de ayer no lo decide un evento que hoy no esta,
    lo decide quien marca.

    Y sin `Correccion`, a proposito: no paso nada que contarle a nadie. Una correccion aqui le
    anunciaria al paciente un cambio inexistente y le pintaria a la clinica una alarma roja
    sobre un dia en el que no cambio nada.
    """
    ctx = contexto(identidad_verificada=True)
    ayer = AHORA - timedelta(days=1)
    escrituras: list = []
    cola = _reconciliando(monkeypatch, ctx, escrituras)

    al_dia, correcciones = asyncio.run(
        h.reconciliar_con_calendar(
            database_url=ctx.database_url,
            calendario=ctx.calendario,
            citas=[_cita(inicio=ayer, evento_calendar_id="ev-borrado", reserva_id=7)],
            ahora=AHORA,
            jornada=ctx.jornada,
            capacidad_por_hora=ctx.capacidad_por_hora,
            duracion_cita_minutos=ctx.duracion_cita_minutos,
            hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
            horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
        )
    )

    assert escrituras == [], "una cita que ya ocurrio no se toca"
    assert correcciones == [], "no paso nada que contarle al paciente ni a la clinica"
    assert [c["inicio"] for c in al_dia] == [ayer], "sigue viva, y por tanto marcable"
    assert cola.filas == [], "ni recordatorio de algo que ya paso"


def test_dos_peticiones_de_la_agenda_no_dejan_DOS_recordatorios(monkeypatch):
    """`GET /api/agenda` ESCRIBE, y dos pestañas abiertas del mismo dia son dos peticiones.

    El estado que leen es el mismo --la misma cita, el mismo destino en Calendar-- y lo unico
    que las distingue es el reloj: cada peticion HTTP calcula su propio `ahora`. Con `ahora`
    dentro de la clave de idempotencia eso daba dos claves, dos filas y dos recordatorios
    vivos: el paciente recibe el mismo WhatsApp dos veces.

    La clave cuelga ahora de la RESERVA, que ya es idempotente por diseño: el cupo se toma con
    `calendar:{cita}:{destino}`, sin reloj, asi que las dos peticiones recuperan la MISMA
    reserva y escriben la misma clave. Es el no negociable 2 mirado de cerca -- la clave sale
    del estado, nunca de un instante.

    **Lo que se cuenta son las FILAS, no los vivos, y la diferencia es el fallo entero.** En
    secuencia dos claves distintas dejan un solo vivo, porque la cascada de la segunda anula
    la fila de la primera: mirar los vivos aqui da verde con el fallo dentro. Lo que rompe son
    dos transacciones ENTRELAZADAS, donde ninguna ve la fila de la otra todavia y por tanto
    ninguna la anula -- y entonces las dos quedan vivas. Con una sola clave eso no puede pasar
    en ningun entrelazado: la segunda insercion choca contra `ON CONFLICT DO NOTHING` y la
    unicidad la decide Postgres, no el orden en que llegaron. Es la misma forma de `tomar_cupo`
    y por el mismo motivo.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    cita = _cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)
    cola = ColaFalsa().instalar(monkeypatch)

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia,
        "mover_cita",
        lambda conn, id_cita, *, reserva_id, inicio, commit=True: None,
    )
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    # La misma reserva las dos veces, que es lo que de verdad devuelve `tomar_cupo` ante una
    # clave repetida: mira `reservas` por `clave_idempotencia` ANTES de intentar insertar.
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (99, 1))

    for microsegundos in (0, 137):
        asyncio.run(
            h.reconciliar_con_calendar(
                database_url=ctx.database_url,
                calendario=ctx.calendario,
                citas=[dict(cita)],
                ahora=ctx.ahora + timedelta(microseconds=microsegundos),
                jornada=ctx.jornada,
                capacidad_por_hora=ctx.capacidad_por_hora,
                duracion_cita_minutos=ctx.duracion_cita_minutos,
                hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
                horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
            )
        )

    assert len(cola.filas) == 1, "dos pestañas escriben UNA fila, no dos"
    assert len(cola.vivos("cita-1")) == 1


def test_un_movimiento_NUEVO_al_mismo_destino_si_recupera_su_recordatorio(monkeypatch):
    """La otra mitad, y es la que `ahora` estaba protegiendo: A -> B -> A -> B a mano.

    Al volver a B la clave no puede acertar la de la primera vez: esa fila ya la anulo la
    cascada, y `ON CONFLICT (clave_idempotencia) DO NOTHING` haria que la reinsercion no
    escribiera nada -- cita corregida y CERO recordatorios vivos, que es el fallo silencioso
    que este proyecto paga caro.

    Colgar de la reserva lo cubre solo: al mover la cita fuera de B, `liberar_cupo` borra esa
    reserva, y volver a B toma una NUEVA con un id nuevo. Distinta reserva, distinta clave.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    manana = INICIO + timedelta(days=1)
    ctx.calendario.eventos["ev-1"] = (manana, 60, "limpieza")
    cita = _cita(inicio=INICIO, evento_calendar_id="ev-1", reserva_id=7)
    cola = ColaFalsa().instalar(monkeypatch)

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia,
        "mover_cita",
        lambda conn, id_cita, *, reserva_id, inicio, commit=True: None,
    )
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    reservas = iter([(99, 1), (100, 1)])
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: next(reservas))

    for _ in range(2):
        asyncio.run(
            h.reconciliar_con_calendar(
                database_url=ctx.database_url,
                calendario=ctx.calendario,
                citas=[dict(cita)],
                ahora=ctx.ahora,
                jornada=ctx.jornada,
                capacidad_por_hora=ctx.capacidad_por_hora,
                duracion_cita_minutos=ctx.duracion_cita_minutos,
                hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
                horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
            )
        )

    vivos = cola.vivos("cita-1")
    assert len(vivos) == 1, "una cita no puede quedarse sin ningun recordatorio vivo"
    assert len(cola.filas) == 2, "la reserva nueva escribe una fila nueva"
    assert vivos[0]["clave"].endswith(":100"), "el vivo es el de la reserva nueva"


def test_reconciliar_una_cita_pasada_no_programa_recordatorio(monkeypatch):
    """La agenda reconcilia cualquier dia, tambien uno de la semana pasada.

    Un dia que ya ocurrio no puede dejar programado el recordatorio de algo que ya paso: el
    despachador lo mandaria por una cita que el paciente ya tuvo. Y la ausencia se comprueba
    con el camino delante -- la correccion SI se escribio --, para que el cero recordatorios
    sea una decision y no un reventon a mitad de camino.
    """
    ctx = contexto(identidad_verificada=True)
    la_semana_pasada = AHORA - timedelta(days=7)
    destino = la_semana_pasada + timedelta(hours=2)
    ctx.calendario.eventos["ev-1"] = (destino, 60, "limpieza")
    escrituras: list = []
    cola = _reconciliando(monkeypatch, ctx, escrituras)

    al_dia, correcciones = asyncio.run(
        h.reconciliar_con_calendar(
            database_url=ctx.database_url,
            calendario=ctx.calendario,
            citas=[_cita(inicio=la_semana_pasada, evento_calendar_id="ev-1", reserva_id=7)],
            ahora=AHORA,
            jornada=ctx.jornada,
            capacidad_por_hora=ctx.capacidad_por_hora,
            duracion_cita_minutos=ctx.duracion_cita_minutos,
            hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
            horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
        )
    )

    assert ("mover", "cita-1", 99, destino) in escrituras, "la correccion tiene que ocurrir"
    assert cola.filas == [], "una cita de la semana pasada no lleva recordatorio"
    assert [c.que_paso for c in correcciones] == ["movida"]
    assert [c.hora_vieja for c in correcciones] == [la_semana_pasada]
    assert [c.hora_nueva for c in correcciones] == [destino]
    assert [c["inicio"] for c in al_dia] == [destino]


def test_la_correccion_de_una_cita_borrada_no_lleva_hora_nueva(monkeypatch):
    """`hora_nueva` en None es lo que distingue «la movieron» de «ya no esta».

    El panel la va a serializar tal cual: si una cancelada trajera una hora nueva, la agenda
    pintaria una cita en un hueco que la clinica ya solto.
    """
    ctx = contexto(identidad_verificada=True, ahora=INICIO - timedelta(hours=1))
    escrituras: list = []
    _reconciliando(monkeypatch, ctx, escrituras)

    al_dia, correcciones = asyncio.run(
        h.reconciliar_con_calendar(
            database_url=ctx.database_url,
            calendario=ctx.calendario,
            citas=[_cita(inicio=INICIO, evento_calendar_id="ev-borrado", reserva_id=7)],
            ahora=ctx.ahora,
            jornada=ctx.jornada,
            capacidad_por_hora=ctx.capacidad_por_hora,
            duracion_cita_minutos=ctx.duracion_cita_minutos,
            hora_recordatorio_vispera=ctx.hora_recordatorio_vispera,
            horas_minimas_para_recordar=ctx.horas_minimas_para_recordar,
        )
    )

    assert any(e[0] == "cancelar" for e in escrituras), "la cancelacion tiene que ocurrir"
    assert al_dia == [], "una cita cancelada no vuelve en la agenda"
    assert len(correcciones) == 1
    c = correcciones[0]
    assert (c.cita_id, c.que_paso, c.hora_vieja, c.hora_nueva) == (
        "cita-1",
        "cancelada",
        INICIO,
        None,
    )
    assert (c.tratamiento, c.nombre_completo) == ("limpieza", "Ana Ruiz")


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

    `pidio_no_contacto=True`: es la única prueba del nivel `herramientas` que combina la baja
    con la emisión de un recordatorio, y por eso es la que sostiene la mitad no-comercial del
    no negociable 25 en esta capa. `crear_cita` llama a `persistencia.insertar_seguimiento`
    directo, sin pasar por `ctx.pidio_no_contacto` en ningún punto del camino -- si alguien le
    sumara un `if ctx.pidio_no_contacto: cuando_recordar = None` creyendo que respeta la baja,
    esta prueba es la que lo cazaría.

    I1 (ronda de revisión sobre la parada D): el doble de `reiniciar_seguimientos_fallidos`
    ahora REGISTRA la llamada, y esta prueba comprueba con QUÉ teléfono se hizo -- no solo que
    `crear_cita` no reventó. Con un doble que solo silenciaba el `AttributeError`
    (`lambda conn, telefono, **kw: None`), borrar la línea del reset en `_crear_cita` dejaba
    la suite entera en verde: la ausencia de la llamada era indistinguible de su presencia.
    """
    programados: list[dict] = []
    reinicios: list[str] = []
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA), pidio_no_contacto=True
    )

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

    def _reiniciar(conn, telefono, **kw):
        reinicios.append(telefono)

    monkeypatch.setattr(persistencia, "reiniciar_seguimientos_fallidos", _reiniciar)

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
    # I1: agendar SÍ reinicia el contador, y lo hace con el teléfono de quien agendó -- no con
    # ningún otro. Quitar la línea del reset en `_crear_cita`, o mover el `conn.commit()` por
    # delante de ella, deja esto en rojo.
    assert reinicios == [ctx.telefono_completo]


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
    monkeypatch.setattr(
        persistencia, "reiniciar_seguimientos_fallidos", lambda conn, telefono, **kw: None
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


class ColaFalsa:
    """La cola de `seguimientos` con la semántica que de verdad tiene Postgres.

    Los dobles de una línea no sirven para lo que hay que probar aquí: el fallo vive en la
    interacción entre `ON CONFLICT (clave_idempotencia) DO NOTHING` --que hace que una clave
    repetida NO escriba y devuelva `False`-- y el `UPDATE ... WHERE anulado_en IS NULL` de la
    cascada. Un doble que siempre devuelva `True` esconde justo eso.
    """

    def __init__(self) -> None:
        self.filas: list[dict] = []

    def insertar(self, _conn, **kw) -> bool:
        if any(f["clave"] == kw["clave_idempotencia"] for f in self.filas):
            return False  # ON CONFLICT (clave_idempotencia) DO NOTHING
        self.filas.append(
            {
                "clave": kw["clave_idempotencia"],
                "cita_id": kw["cita_id"],
                "fecha_objetivo": kw["fecha_objetivo"],
                "anulado": None,
            }
        )
        return True

    def anular(self, _conn, cita_id, *, motivo, excepto_clave=None, commit=True) -> int:
        anulados = 0
        for fila in self.filas:
            if fila["cita_id"] != cita_id or fila["anulado"] is not None:
                continue
            if excepto_clave is not None and fila["clave"] == excepto_clave:
                continue
            fila["anulado"] = motivo
            anulados += 1
        return anulados

    def vivos(self, cita_id: str) -> list[dict]:
        return [f for f in self.filas if f["cita_id"] == cita_id and f["anulado"] is None]

    def instalar(self, monkeypatch) -> ColaFalsa:
        monkeypatch.setattr(persistencia, "insertar_seguimiento", self.insertar)
        monkeypatch.setattr(persistencia, "anular_seguimientos_de_cita", self.anular)
        return self


def _reprogramando(monkeypatch, cita: dict):
    """Dobla `_con_base` para una cadena de reprogramaciones sobre la MISMA cita.

    `_con_base` se llama dos veces por reprogramación --`trabajo` y luego `aplicar`--, así que
    el doble alterna. `mover_cita` actualiza la cita compartida, que es lo que hace realista un
    A -> B -> A: el segundo movimiento lee la cita donde la dejó el primero.
    """
    pasos = {"n": 0}

    async def base_falsa(_ctx, trabajo):
        pasos["n"] += 1
        if pasos["n"] % 2 == 1:
            return ("ok", dict(cita), (cita["reserva_id"] + 1, 1), [])
        return trabajo(BaseFalsa())

    def mover(_conn, _id_cita, *, reserva_id, inicio, commit=True):
        cita["reserva_id"] = reserva_id
        cita["inicio"] = inicio
        cita["estado"] = "reprogramada"

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "mover_cita", mover)
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)


def _cita_viva() -> dict:
    return {
        "id": "cita-1",
        "reserva_id": 5,
        "paciente_id": None,
        "telefono": "573001112233",
        "estado": "confirmada",
        "evento_calendar_id": None,
        "inicio": datetime(2026, 9, 16, 9, 0, tzinfo=h.ZONA_BOGOTA),
    }


def test_reintentar_la_misma_reprogramacion_no_deja_la_cita_sin_recordatorio(monkeypatch):
    """El fallo que la clave reintrodujo, y es el peor de los de esta tarea.

    Con la cascada anulando ANTES de insertar, el segundo intento de la misma reprogramación
    anulaba la fila que había creado el primero y después chocaba al reinsertarla
    (`ON CONFLICT DO NOTHING`). La cita quedaba movida, correcta, y con CERO recordatorios
    vivos -- «un recordatorio que a veces no existe, y nadie se entera de cuál faltó», que es
    literalmente lo que esta tarea existe para eliminar.

    Y no es hipotético: `_reprogramar_cita` no tiene el guardián de duplicado que sí tiene
    `_crear_cita`, `tomar_cupo` es idempotente por clave, y el estado tras mover es
    `reprogramada`, no `cancelada`: ninguno de los cuatro `return` tempranos frena un segundo
    intento. El comentario de la clave promete que «repetirlo es inofensivo».
    """
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA), identidad_verificada=True
    )
    cita = _cita_viva()
    cola = ColaFalsa().instalar(monkeypatch)
    _reprogramando(monkeypatch, cita)
    destino = datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA)

    # El recordatorio que dejó `crear_cita`, con su clave sin destino.
    cola.insertar(None, clave_idempotencia="conv-1:recordatorio:cita-1",
                  cita_id="cita-1", fecha_objetivo=cita["inicio"])

    asyncio.run(h._reprogramar_cita(ctx, "cita-1", destino.isoformat()))
    asyncio.run(h._reprogramar_cita(ctx, "cita-1", destino.isoformat()))

    vivos = cola.vivos("cita-1")
    assert len(vivos) == 1, "el reintento dejó la cita movida y sin recordatorio"
    assert vivos[0]["fecha_objetivo"] == datetime(2026, 9, 16, 18, 0, tzinfo=h.ZONA_BOGOTA)
    # El de `crear_cita` sí se anula: habla de la hora de la que el paciente acaba de salir.
    assert [f["anulado"] for f in cola.filas if f["clave"].endswith("cita-1")] == [
        "cita_reprogramada"
    ]


def test_repetir_la_misma_reprogramacion_no_libera_el_cupo_que_la_cita_usa(monkeypatch):
    """Sobreventa de una clinica real, por una clave de idempotencia sin componente de turno.

    `tomar_cupo` empieza mirando si esa clave ya reservo algo, y la de `reprogramar_cita` es
    `conversacion:reprogramar:id_cita:destino`: repetir LA MISMA reprogramacion al MISMO
    destino en un turno posterior devuelve la reserva que ya existia, asi que
    `reserva_nueva == reserva_vieja` y el `liberar_cupo` de despues del commit borraba el cupo
    que la cita esta usando. `citas.reserva_id` es `ON DELETE SET NULL`: el DELETE no falla, no
    lanza y no registra nada. La fila queda con `reserva_id = NULL`, la hora vuelve a contarse
    libre y con `capacidad_por_hora = 2` se venden tres.

    Es un defecto PREEXISTENTE --no lo introduce la cola de recordatorios-- y lo que entra aqui
    es la guarda, no el arreglo de fondo: la clave sin componente de turno sigue pendiente.
    """
    cita = _cita_viva()  # reserva_id = 5
    liberados: list[int] = []
    pasos = {"n": 0}

    async def base_falsa(_ctx, trabajo):
        pasos["n"] += 1
        if pasos["n"] % 2 == 1:
            # El cupo que devuelve `tomar_cupo` es el que la cita YA tiene: es lo que hace la
            # idempotencia por clave cuando la reprogramacion se repite.
            return ("ok", dict(cita), (cita["reserva_id"], 1), [])
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia,
        "mover_cita",
        lambda conn, id_cita, *, reserva_id, inicio, commit=True: None,
    )
    monkeypatch.setattr(
        persistencia, "liberar_cupo", lambda conn, reserva_id: liberados.append(reserva_id)
    )
    ColaFalsa().instalar(monkeypatch)

    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA), identidad_verificada=True
    )
    destino = datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA)

    asyncio.run(h._reprogramar_cita(ctx, "cita-1", destino.isoformat()))

    assert liberados == [], "se libero el cupo que la propia cita esta usando"


def test_mover_de_vuelta_a_una_hora_ya_usada_vuelve_a_dejar_recordatorio(monkeypatch):
    """A -> B -> A, en turnos distintos, y sin ningún reintento de por medio.

    Sin `ctx.ahora` dentro de la clave, el regreso a A acertaba la clave del primer movimiento
    --misma cita, mismo destino-- y `ON CONFLICT DO NOTHING` descartaba la inserción en
    silencio. La cita volvía a su hora original y se quedaba sin recordatorio.

    `ctx.ahora` es fijo dentro de un turno, así que no rompe la idempotencia del reintento
    (la prueba de arriba) y sí distingue dos movimientos de verdad.
    """
    cita = _cita_viva()
    cola = ColaFalsa().instalar(monkeypatch)
    _reprogramando(monkeypatch, cita)
    hora_a = datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA)
    hora_b = datetime(2026, 9, 18, 9, 0, tzinfo=h.ZONA_BOGOTA)

    def turno(minuto: int):
        return contexto(
            ahora=datetime(2026, 9, 14, 9, minuto, tzinfo=h.ZONA_BOGOTA),
            identidad_verificada=True,
        )

    asyncio.run(h._reprogramar_cita(turno(0), "cita-1", hora_a.isoformat()))
    asyncio.run(h._reprogramar_cita(turno(10), "cita-1", hora_b.isoformat()))
    asyncio.run(h._reprogramar_cita(turno(20), "cita-1", hora_a.isoformat()))

    vivos = cola.vivos("cita-1")
    assert len(vivos) == 1, "al volver a la hora original se quedó sin recordatorio"
    # La víspera de A, que es donde tiene que estar el único vivo.
    assert vivos[0]["fecha_objetivo"] == datetime(2026, 9, 16, 18, 0, tzinfo=h.ZONA_BOGOTA)

    # Y una vuelta más, para que la cadena completa A -> B -> A -> B quede cubierta.
    asyncio.run(h._reprogramar_cita(turno(30), "cita-1", hora_b.isoformat()))
    vivos = cola.vivos("cita-1")
    assert len(vivos) == 1
    assert vivos[0]["fecha_objetivo"] == datetime(2026, 9, 17, 18, 0, tzinfo=h.ZONA_BOGOTA)


# ==========================================================================================
# Ningún fallo del recordatorio puede tumbar la cita
# ==========================================================================================
#
# La propiedad que compra calcular el momento FUERA de la transacción. Sin estas dos pruebas,
# devolver la llamada dentro de `guardar` o de `aplicar` deja las cuatro pruebas de la cascada
# en verde y hace desaparecer la propiedad sin ruido.


def _que_reviente(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("un bug calculando el momento del recordatorio")

    monkeypatch.setattr(h.seguimientos, "momento_del_recordatorio", boom)


def test_un_fallo_calculando_el_recordatorio_no_escribe_la_cita(monkeypatch):
    """Si el cálculo revienta, tiene que reventar ANTES de que exista una cita que perder.

    La seguridad clínica prevalece sobre cualquier objetivo comercial: un recordatorio que
    falla es una oportunidad perdida; una cita que se pierde porque le añadimos un
    recordatorio es un paciente que no se atiende. Que `registrar_cita` no llegue a llamarse
    es la propiedad, y sobrevive a que alguien reescriba el cuerpo de `guardar`.
    """
    ctx = contexto(ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA))
    escrituras: list[dict] = []
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ((77, 1), None, [])  # `tomar`: cupo libre y ninguna cita previa
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia, "registrar_cita", lambda conn, **kw: escrituras.append(kw) or "cita-x"
    )
    monkeypatch.setattr(persistencia, "asegurar_paciente", lambda conn, **kw: 7)
    _que_reviente(monkeypatch)

    with pytest.raises(RuntimeError):
        asyncio.run(
            h._crear_cita(
                ctx,
                SolicitudCita(
                    nombre_completo="Ana Gómez",
                    inicio=datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA),
                    tratamiento="limpieza",
                    clave_idempotencia="da-igual",
                ),
            )
        )

    assert escrituras == [], "el fallo del recordatorio se llevó la cita por delante"


def test_un_fallo_calculando_el_recordatorio_no_mueve_la_cita(monkeypatch):
    """La misma propiedad en `reprogramar`, y aquí el daño sería doble: la cita ya se movió en
    Google Calendar, así que deshacer el movimiento en Neon deja a los dos sistemas diciendo
    horas distintas sobre la misma cita."""
    ctx = contexto(
        ahora=datetime(2026, 9, 14, 9, 0, tzinfo=h.ZONA_BOGOTA), identidad_verificada=True
    )
    cita = _cita_viva()
    movidas: list[str] = []
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", cita, (6, 1), [])
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(
        persistencia, "mover_cita", lambda conn, id_cita, **kw: movidas.append(id_cita)
    )
    # La cola entera doblada: así, si alguien devolviera el cálculo dentro de `aplicar`, esta
    # prueba fallaría por la ASERCIÓN --la cita se movió-- y no por un doble que falta.
    monkeypatch.setattr(persistencia, "anular_seguimientos_de_cita", lambda *a, **k: 0)
    monkeypatch.setattr(persistencia, "insertar_seguimiento", lambda conn, **kw: True)
    monkeypatch.setattr(persistencia, "liberar_cupo", lambda conn, reserva_id: None)
    _que_reviente(monkeypatch)

    destino = datetime(2026, 9, 17, 9, 0, tzinfo=h.ZONA_BOGOTA)
    with pytest.raises(RuntimeError):
        asyncio.run(h._reprogramar_cita(ctx, "cita-1", destino.isoformat()))

    assert movidas == [], "el fallo del recordatorio deshizo un movimiento ya hecho en Google"


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
    # `reactivacion_sin_agendar`: `recordatorio_cita` ya no es encolable por esta vía (tarea 2).
    texto = asyncio.run(h._programar_seguimiento(ctx, "reactivacion_sin_agendar", futuro))

    assert "No se programó nada" in texto
    assert "Dr. Martínez" in texto


def test_no_se_programa_un_seguimiento_hacia_atras():
    ctx = contexto()
    pasado = (ctx.ahora - timedelta(days=1)).isoformat()

    # `reactivacion_sin_agendar` y no `reactivacion` a secas: ese tipo nunca existió en el
    # vocabulario cerrado de la tarea 2.
    texto = asyncio.run(h._programar_seguimiento(ctx, "reactivacion_sin_agendar", pasado))

    assert "ya pasó" in texto


def test_con_la_baja_puesta_lo_comercial_NO_LLEGA_A_INSERTARSE(monkeypatch):
    """La guarda va en el código, no solo en el prompt.

    `seguimientos.decidir` ya anula esto con G0 al despachar, así que sin esta comprobación
    tampoco saldría nada. Pero entonces lo único que impide escribir la fila es que el modelo
    obedezca una instrucción, y en este proyecto lo demostrable lo escribe el código (no
    negociables 2, 12, 22). Que `_con_base` reviente si alguien la llama es justamente lo que
    prueba que no se llega a la base.
    """
    ctx = contexto(pidio_no_contacto=True)

    async def base_prohibida(_ctx, trabajo):
        raise AssertionError("no se puede tocar la base con la baja puesta")

    monkeypatch.setattr(h, "_con_base", base_prohibida)

    futuro = (ctx.ahora + timedelta(days=2)).isoformat()
    # `reactivacion_sin_agendar` y no `reactivacion` a secas: ese tipo nunca existió en el
    # vocabulario cerrado de la tarea 2.
    texto = asyncio.run(h._programar_seguimiento(ctx, "reactivacion_sin_agendar", futuro))

    assert "no le escribieran más" in texto
    assert "no se programó nada comercial" in texto


def test_recordatorio_de_cita_no_se_encola_por_esta_via_ni_siquiera_sin_baja(monkeypatch):
    """La tarea 2 sacó `recordatorio_cita` de lo que el modelo puede pedir por esta tool: lo
    emite el CÓDIGO al crear o mover la cita (`crear_cita`/`reprogramar_cita`, con
    `persistencia.insertar_seguimiento` directo), nunca `_programar_seguimiento`. Esta prueba
    reemplaza a la que existía --que esperaba justo lo contrario, que la baja no le aplicaba a
    `recordatorio_cita` PORQUE se podía programar por aquí-- porque esa premisa ya no es
    cierta: ahora se rechaza con baja o sin ella, antes de tocar la base.
    """
    ctx = contexto()

    async def base_prohibida(_ctx, trabajo):
        raise AssertionError("no se puede tocar la base pidiendo un tipo que no se encola")

    monkeypatch.setattr(h, "_con_base", base_prohibida)

    futuro = (ctx.ahora + timedelta(days=2)).isoformat()
    texto = asyncio.run(h._programar_seguimiento(ctx, "recordatorio_cita", futuro))

    assert "no existe" in texto.lower()


def test_la_guarda_de_la_baja_es_incondicional_PORQUE_lo_que_el_modelo_pide_es_siempre_comercial():
    """Reemplaza a `test_la_guarda_de_la_baja_usa_LA_MISMA_lista_blanca_que_el_despachador`,
    que comparaba `_programar_seguimiento` contra `TIPOS_NO_COMERCIALES` -- una comparación
    que dejó de significar nada en cuanto `recordatorio_cita` salió por completo de
    `TIPOS_QUE_EL_MODELO_PUEDE_PEDIR`: desde entonces la comprobación de la baja en la tool
    ya no mira el tipo, es un `if ctx.pidio_no_contacto:` a secas.

    Lo que hace que esa incondicionalidad sea segura -- y lo que esta prueba sostiene -- es
    que las dos constantes no se solapen: si algún día alguien mete en
    `TIPOS_QUE_EL_MODELO_PUEDE_PEDIR` un tipo que también esté en `TIPOS_NO_COMERCIALES`, la
    baja empezaría a bloquear una excepción real (o, al revés, un tipo comercial se colaría
    como exento) sin que ninguna otra prueba lo note.
    """
    assert seguimientos.TIPOS_QUE_EL_MODELO_PUEDE_PEDIR.isdisjoint(
        seguimientos.TIPOS_NO_COMERCIALES
    )


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


def test_el_escalamiento_deja_la_puerta_en_el_hilo_DEL_PACIENTE_pero_no_el_resumen(monkeypatch):
    """La otra mitad de la prueba de arriba, y la que arregla el caso del 17/09/2026.

    El doctor no vive en el General: vive en el hilo del paciente, que es donde ve llegar sus
    mensajes. Hasta hoy el escalamiento solo colgaba la puerta en el General, así que un
    doctor mirando el hilo veía al paciente insistir sin ninguna señal de que Daniela ya había
    pedido ayuda -- y sin nada que pulsar. Medido: borró el hilo y el mensaje del General, y
    dio por hecho que el sistema había dejado de ofrecerle tomar la conversación.

    Lo que va al hilo es **solo la puerta**: motivo y botón. El resumen y la pregunta son la
    discusión interna entre doctores y se quedan en el General, que es de lo que habla
    `test_el_escalamiento_va_al_tema_general_y_nunca_al_del_paciente`. Esa separación no se
    toca; lo que se añade es un aviso operativo sin contenido clínico.

    El botón va por TELÉFONO (`teclado_tomar`) y no por id de conversación: este mensaje se
    queda en el expediente para siempre, y una conversación caduca a las 24 h. Con el id
    dentro, pulsarlo al día siguiente contestaría «esa conversación ya no existe».
    """
    ctx = contexto(topic_id=99, tema_general=0, nombre_paciente="Ana Gómez")
    telegram = TelegramFalso()

    async def base_falsa(_ctx, trabajo):
        return 5

    async def hilo_falso(**_kwargs):
        return 99

    monkeypatch.setattr(h, "_con_base", base_falsa)
    from maxicare_daniela import lectura

    monkeypatch.setattr(lectura, "rescatar_hilo", hilo_falso)

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

    en_el_hilo = [e for e in telegram.enviados if e["tema_id"] == 99]
    assert en_el_hilo, "el hilo del paciente se quedó sin ninguna puerta al relevo"
    aviso = en_el_hilo[0]
    assert "Hablar yo con el paciente" in str(aviso["teclado"])
    assert ctx.telefono_completo in str(aviso["teclado"]), "el botón tiene que ir por teléfono"
    # Y el muro sigue en pie: la discusión interna no baja al expediente del paciente.
    assert "lesión que ve en su radiografía" not in aviso["texto"]
    assert "responder algo de esto por WhatsApp" not in aviso["texto"]


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


# ==========================================================================================
# La baja comercial y su revocación
# ==========================================================================================


def test_registrar_no_contactar_apaga_lo_comercial(monkeypatch):
    """El modelo solo levanta la mano. La fecha, el origen y la versión las arma el código
    desde `ctx`: si el modelo pudiera escribirlas, la bitácora dejaría de ser una prueba."""
    anotado: list[tuple] = []

    async def base_falsa(ctx, trabajo):
        trabajo(BaseFalsa())

    def pedir_baja(conn, telefono, *, origen="paciente", detalle=None):
        anotado.append((telefono, origen, detalle))
        return True

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "pedir_baja", pedir_baja)

    salida = asyncio.run(h._registrar_no_contactar(contexto(), "dijo que no le escriban"))

    assert anotado == [("573001112233", "paciente", "dijo que no le escriban")]
    # El texto que vuelve al modelo tiene que decirle las tres cosas: que quedó anotado, que
    # no insista, y que la cita no se toca.
    assert "no intentes retenerlo" in salida or "no le ofrezcas alternativas" in salida


def test_revocar_no_contactar_la_levanta(monkeypatch):
    anotado: list[tuple] = []

    async def base_falsa(ctx, trabajo):
        return trabajo(BaseFalsa())

    def revocar_baja(conn, telefono, *, origen="paciente", detalle=None):
        anotado.append((telefono, origen, detalle))
        return True

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "revocar_baja", revocar_baja)

    salida = asyncio.run(h._revocar_no_contactar(contexto(), "pidio que le avisen"))

    assert anotado == [("573001112233", "paciente", "pidio que le avisen")]
    assert "vuelve a recibir mensajes" in salida


def test_revocar_no_contactar_a_un_numero_que_nunca_se_dio_de_baja_no_confirma_un_cambio_falso(
    monkeypatch,
):
    """No negociable 1: `revocar_baja` devuelve `False` cuando no había nada que levantar, y
    la tool no puede decirle al paciente que "vuelve a recibir mensajes" si nunca los había
    dejado de recibir -- confirmaría un cambio que no ocurrió."""

    async def base_falsa(ctx, trabajo):
        return trabajo(BaseFalsa())

    def revocar_baja(conn, telefono, *, origen="paciente", detalle=None):
        return False

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(h.persistencia, "revocar_baja", revocar_baja)

    salida = asyncio.run(h._revocar_no_contactar(contexto(), None))

    assert "vuelve a recibir mensajes" not in salida


def test_las_dos_tools_estan_registradas():
    """Una tool que existe y no está en la lista es una tool que el modelo no puede llamar,
    sin un solo error en ningún log."""
    nombres = {t.name for t in h.TODAS}

    assert "registrar_no_contactar" in nombres
    assert "revocar_no_contactar" in nombres


def test_el_modelo_solo_puede_escribir_la_nota_nunca_la_fecha_el_origen_o_la_version():
    """La frontera no es documental, es estructural: sin esta prueba, añadir mañana un
    parámetro `fecha` u `origen` a la tool PÚBLICA no rompería ninguna otra -- las tres
    pruebas de arriba llaman al helper `_nombre` con guion bajo, no al `FunctionTool`
    decorado que es lo único que el modelo puede tocar de verdad."""
    por_nombre = {t.name: t for t in h.TODAS}

    for nombre in ("registrar_no_contactar", "revocar_no_contactar"):
        propiedades = set(por_nombre[nombre].params_json_schema["properties"])
        assert propiedades == {"nota"}

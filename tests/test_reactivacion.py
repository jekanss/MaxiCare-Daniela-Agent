"""Las once reglas anti-reporte, una prueba por regla.

Cada una tiene que FALLAR si alguien afloja su regla. No comprueban el camino feliz:
comprueban que el camino prohibido sigue prohibido.
"""
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada, ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sabado, domingo cerrado


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Septiembre de 2026: el 15 es martes, el 19 sabado, el 20 domingo."""
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA_BOGOTA)


async def _despachar_con(filas, *, whatsapp, plantillas, ahora, monkeypatch=None):
    """Corre un ciclo de `despachar` con la base entera doblada.

    Se dobla `persistencia` y no la base: lo que se prueba es la DECISION y el reparto de
    plantillas, no el SQL. El SQL lo prueban las de `-m neon`.

    OJO: NO dobla `persistencia.contar_enviados_hoy` -- esa funcion no existe hoy. Es del tope
    DIARIO, que es del barrido (parada siguiente de este plan) y no del despachador. Doblar un
    atributo que no existe revienta con `AttributeError` antes de probar nada (Ruling C2).
    """
    import pytest as _pytest
    from maxicare_daniela import persistencia

    mp = monkeypatch or _pytest.MonkeyPatch()

    class _ConexionFalsa:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
        def close(self): pass

    mp.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    mp.setattr(persistencia, "leer_configuracion", lambda conn: {})
    mp.setattr(persistencia, "seguimientos_por_despachar", lambda conn, **k: list(filas))
    mp.setattr(persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None)
    mp.setattr(persistencia, "marcar_seguimiento_enviado", lambda conn, id_seguimiento: True)
    mp.setattr(persistencia, "anular_seguimiento", lambda conn, i, **k: None)
    mp.setattr(persistencia, "aplazar_seguimiento", lambda conn, i, **k: None)
    mp.setattr(persistencia, "anotar_recordatorio_en_conversacion", lambda conn, i, **k: None)
    try:
        return await s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=whatsapp,
            jornada=JORNADA,
            plantillas=plantillas,
            ahora=ahora,
        )
    finally:
        mp.undo()


def test_el_vocabulario_no_deja_inventar_un_tipo():
    assert "inventado" not in s.TIPOS_DE_SEGUIMIENTO


def test_recordatorio_de_cita_no_es_reactivacion():
    """Si se colaran, la baja dejaria de comprobarse sobre ellos (G0 usa lista BLANCA)."""
    assert s.TIPOS_DE_REACTIVACION.isdisjoint(s.TIPOS_NO_COMERCIALES)


def test_toda_reactivacion_es_comercial():
    """Regla 4: la baja tiene que aplicarse a las tres."""
    for tipo in s.TIPOS_DE_REACTIVACION:
        assert tipo not in s.TIPOS_NO_COMERCIALES, tipo


def test_el_vocabulario_del_codigo_y_el_de_la_migracion_no_se_separan():
    """El CHECK de la 021 y esta constante son dos listas del mismo vocabulario.

    Si se separan, el codigo deja pasar un tipo que la base rechaza y el INSERT revienta la
    transaccion del turno entero. Va en las DOS direcciones: que cada tipo del codigo este en
    el SQL, y que el SQL no tenga uno de mas que el codigo no conozca. Con solo la primera
    mitad, un quinto valor colado en el CHECK (a mano, en un despliegue) pasaba desapercibido:
    la base lo aceptaria y el codigo seguiria sin poder pedirlo ni reconocerlo.
    """
    import re
    from pathlib import Path

    sql = Path("migraciones/021_reactivacion.sql").read_text(encoding="utf-8")

    for tipo in s.TIPOS_DE_SEGUIMIENTO:
        assert f"'{tipo}'" in sql, f"{tipo} no esta en el CHECK de la 021"

    match = re.search(r"tipo IN \(([^)]*)\)", sql, re.DOTALL)
    assert match, "no se encontro el bloque `tipo IN (...)` del CHECK en la 021"
    tipos_del_sql = set(re.findall(r"'(\w+)'", match.group(1)))
    assert tipos_del_sql == s.TIPOS_DE_SEGUIMIENTO, (
        "el CHECK de la 021 y `TIPOS_DE_SEGUIMIENTO` tienen valores distintos -- "
        f"sql={tipos_del_sql} codigo={s.TIPOS_DE_SEGUIMIENTO}"
    )


def test_el_literal_de_la_tool_no_se_separa_de_lo_que_el_modelo_puede_pedir():
    """El `Literal` de la firma de `programar_seguimiento` es una TERCERA lista del mismo
    vocabulario, y nada la ataba a `TIPOS_QUE_EL_MODELO_PUEDE_PEDIR`.

    Sin esta prueba: alguien suma un tipo nuevo a la constante y al CHECK de la 021, las
    pruebas de vocabulario quedan en verde, y el `Literal` se queda con el enum viejo -- el
    SDK le sigue enseñando al modelo el schema de antes y el tipo nuevo no se puede pedir
    NUNCA, sin un solo error en ningun log.

    Se compara contra `params_json_schema`, el esquema que el SDK arma a partir del `Literal`
    y que es literalmente lo que el modelo ve -- no contra el codigo fuente del tipo, que
    podria tener el enum correcto y aun asi fallar por como `function_tool` lo serializa.
    """
    from maxicare_daniela import herramientas as h

    enum_del_modelo = set(
        h.programar_seguimiento.params_json_schema["properties"]["tipo"]["enum"]
    )
    assert enum_del_modelo == s.TIPOS_QUE_EL_MODELO_PUEDE_PEDIR


def test_la_tool_rechaza_un_tipo_que_no_existe():
    """Regla 1 y el portillo de G0: el modelo no puede inventarse un tipo."""
    import asyncio

    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto  # reutiliza el contexto clavado

    ctx = contexto()
    respuesta = asyncio.run(_programar_seguimiento(ctx, "publicidad_masiva", "2026-12-01T10:00:00"))
    assert "publicidad_masiva" not in respuesta
    assert "no existe" in respuesta.lower() or "no es un tipo" in respuesta.lower()


def test_la_tool_no_deja_al_modelo_disfrazar_lo_comercial_de_recordatorio():
    """El portillo entero, en una prueba.

    `recordatorio_cita` esta en `TIPOS_NO_COMERCIALES`, asi que G0 no le aplica la baja. Los
    recordatorios los emite el CODIGO al crear o mover una cita; el modelo no tiene por que
    encolar uno, y si puede, tiene una puerta para saltarse la baja.
    """
    import asyncio

    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto

    ctx = contexto(pidio_no_contacto=True)
    respuesta = asyncio.run(
        _programar_seguimiento(ctx, "recordatorio_cita", "2026-12-01T10:00:00")
    )
    assert "programado" not in respuesta.lower()


# ==========================================================================================
# Las cinco guardas que la reactivacion no tenia (tarea 3).
#
# Cuatro guardas (G1, G2, G3, G3bis) viven dentro de `if fila.get("cita_id") is not None:`, y
# un seguimiento de reactivacion no tiene cita: las atraviesa las cuatro sin evaluarse. La que
# importa es G3 (`llego_tarde`): sin su gemelo para lo que no tiene cita, un proceso caido el
# viernes suelta el lunes todos los "hace unos dias nos escribio" de golpe -- el pico exacto
# que Meta castiga y que hace que la gente reporte el numero.
# ==========================================================================================


def fila_de_reactivacion(**cambios) -> dict:
    """Una fila de la cola SIN cita, que es lo que distingue a la reactivacion.

    `aplazado_desde` por defecto es `None`: así nace una reactivación de verdad, y así se
    queda mientras nadie la aplace ni una vez -- incluida una programada a dos semanas vista,
    que puede tener una `fecha_objetivo` lejanísima y aun así no haberse aplazado jamás. Las
    pruebas de R3bis (más abajo) son las únicas que lo pasan explícito, simulando que la fila
    YA se atascó al menos una vez.
    """
    base = dict(
        id=1,
        conversacion_id="conv-1",
        cita_id=None,
        tipo=s.TIPO_SIN_AGENDAR,
        fecha_objetivo=momento(16, 11),
        aplazado_desde=None,
        intentos=0,
        telefono="573001112233",
        nombre_completo="Marcela Rios",
        tratamiento=None,
        cita_inicio=None,
        cita_estado=None,
        tomada_por=None,
        no_contactar=False,
        reactivaciones_ultimo_ano=0,
        seguimientos_fallidos=0,
    )
    base.update(cambios)
    return base


def test_regla_3_una_reactivacion_atrasada_no_sale():
    """G3 para lo que no tiene cita.

    El proceso se cae el viernes y vuelve el lunes. Sin esto salen de golpe todos los
    "hace unos dias nos escribio" con una semana de retraso: el pico que Meta castiga.
    """
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(14, 11)),
        ahora=momento(16, 11),                  # dos dias tarde
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "llego_tarde"


@pytest.mark.parametrize(
    "hora,hasta_esperado",
    [
        (7, momento(16, 9)),    # antes de abrir: se aplaza al mismo dia, a la apertura
        (8, momento(16, 9)),
        (19, momento(17, 9)),   # ya cerro: al dia siguiente
        (22, momento(17, 9)),
    ],
)
def test_regla_6_fuera_del_horario_comercial_no_sale(hora, hasta_esperado):
    """9:00-19:00 y punto. Las 8:00 valen para un recordatorio de cita, no para publicidad.

    `hasta_esperado` cierra el hueco de cobertura de `_proxima_apertura_comercial`
    (hallazgo 3 de la ronda 1 de revision): un helper que devolviera `ahora` tal cual pasaria
    estas mismas aserciones de `accion`/`motivo` y produciria un reaplazamiento infinito -un
    UPDATE por minuto, la fila ni enviada ni anulada, sin una linea en ningun log-.
    """
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(16, hora)),
        ahora=momento(16, hora),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "aplazar", f"a las {hora}:00 salio una reactivacion"
    assert decision.motivo == "fuera_de_horario_comercial"
    assert decision.hasta == hasta_esperado


def test_regla_6_el_domingo_no_sale_aunque_la_clinica_abra():
    """No puede colgar de la jornada de la clinica.

    Un recordatorio de cita en domingo esta bien: la cita es real. Un "sigue interesada?" en
    domingo, no. Si MaxiCare abriera los domingos, la jornada dejaria pasar el segundo.
    """
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(20, 11)),
        ahora=momento(20, 11),                  # domingo
        jornada=Jornada(atiende_domingo=True),  # la clinica ABRE
        ultimo_mensaje=None,
    )
    assert decision.accion == "aplazar"
    assert decision.motivo == "fuera_de_horario_comercial"
    assert decision.hasta == momento(21, 9)  # lunes a la apertura


def test_regla_6_domingo_de_madrugada_tambien_cae_en_lunes():
    """La franja comercial no arranca a medianoche: un domingo a las 7 de la manana se aplaza
    al lunes igual que un domingo a media manana -no a la apertura de HOY, porque hoy es
    domingo-. Verificado a mano contra `_proxima_apertura_comercial` (hallazgo 3)."""
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(20, 7)),
        ahora=momento(20, 7),                   # domingo, antes de las 9
        jornada=Jornada(atiende_domingo=True),  # la clinica ABRE
        ultimo_mensaje=None,
    )
    assert decision.accion == "aplazar"
    assert decision.motivo == "fuera_de_horario_comercial"
    assert decision.hasta == momento(21, 9)


def test_regla_6_el_sabado_en_la_noche_tambien_cae_en_lunes():
    """El aplazamiento cruza el domingo entero: no cae en la apertura del propio sabado (ya
    paso) ni en la del domingo (cerrado), sino en la del lunes. Y el resultado tiene que
    quedar en el FUTURO respecto de `ahora` -si no, es un reaplazamiento infinito."""
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(19, 20)),
        ahora=momento(19, 20),                  # sabado a las 8 pm
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "aplazar"
    assert decision.motivo == "fuera_de_horario_comercial"
    assert decision.hasta == momento(21, 9)
    assert decision.hasta > momento(19, 20), "el aplazamiento tiene que quedar en el futuro"


def test_regla_7_si_hablo_hoy_no_se_le_manda_plantilla():
    """24 h, no los 60 min del recordatorio.

    Mandarle "hace unos dias nos escribio" a quien hablo contigo esta manana es lo que hace
    que la gente conteste "??" y reporte.
    """
    decision = s.decidir(
        fila_de_reactivacion(),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=momento(16, 3),          # 8 h antes
    )
    assert decision.accion == "anular"
    assert decision.motivo == "hablo_hace_poco"


def test_regla_7_el_recordatorio_de_cita_conserva_su_ventana_de_60_min():
    """La ampliacion es solo para reactivacion. Un recordatorio la vispera es util aunque la
    persona haya escrito hace tres horas.

    `fecha_objetivo` va a la MISMA hora que `ahora` (Ruling B1): con el default de
    `fila_de_reactivacion` (11:00) y `ahora` a las 18:00 quedan siete horas de diferencia, y
    G3 -el gemelo de esta guarda para lo que SI tiene cita, dentro del bloque
    `if cita_id is not None`- anula con `llego_tarde` antes de llegar a G6, que es la guarda
    que esta prueba quiere ejercitar. Sin este ajuste la prueba pasaba, pero no por la razon
    que su nombre dice.
    """
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1",
        cita_inicio=momento(17, 9), cita_estado="confirmada",
        fecha_objetivo=momento(16, 18),
    )
    decision = s.decidir(
        fila, ahora=momento(16, 18), jornada=JORNADA, ultimo_mensaje=momento(16, 15)
    )
    assert decision.accion == "enviar"


def test_regla_8_el_tope_anual_para_aunque_el_contador_este_en_cero():
    """El contador se resetea al agendar, asi que alguien que agenda cada vez podria recibir
    muchos en un ano. Este es el techo que el contador no pone."""
    decision = s.decidir(
        fila_de_reactivacion(reactivaciones_ultimo_ano=6),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
        max_reactivaciones_12m=6,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "tope_anual"


def test_regla_5_a_quien_ya_dijo_que_no_dos_veces_no_se_le_persigue():
    decision = s.decidir(
        fila_de_reactivacion(seguimientos_fallidos=2),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
        max_seguimientos_fallidos=2,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "seguimiento_apagado"


def test_el_freno_no_alcanza_al_recordatorio_de_una_cita():
    """Regla 5 vs no negociable: el apagado es comercial. Quien tiene cita recibe su
    recordatorio aunque este apagado y aunque haya pedido la baja.

    Mismo ajuste de `fecha_objetivo` que la prueba anterior, y por el mismo motivo (Ruling
    B1): sin el, G3 -no esta prueba- anula por `llego_tarde` antes de que el freno (R1) o el
    tope (R2) tengan ocasion de aplicarse -o no- a un tipo que no es de reactivacion.
    """
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1", cita_inicio=momento(17, 9),
        cita_estado="confirmada", seguimientos_fallidos=9, no_contactar=True,
        reactivaciones_ultimo_ano=99, fecha_objetivo=momento(16, 18),
    )
    decision = s.decidir(fila, ahora=momento(16, 18), jornada=JORNADA, ultimo_mensaje=None)
    assert decision.accion == "enviar"


# ==========================================================================================
# Ronda 1 de revision sobre la parada B: cinco arreglos.
# ==========================================================================================


def test_un_tipo_fuera_del_vocabulario_no_esquiva_las_guardas_de_reactivacion():
    """Hallazgo 1 de la ronda 1: la polaridad de `es_reactivacion` estaba al reves.

    La primera version hacia `tipo in TIPOS_DE_REACTIVACION` -lista blanca del conjunto
    GUARDADO, falla ABIERTO-. Un `tipo` que el CHECK de la 021 no reconoce -alcanzable: ese
    CHECK es NOT VALID y no revisa las filas viejas de `public`, de cuando `tipo` era texto
    libre- caia fuera de las tres constantes conocidas, `es_reactivacion` daba `False`, y las
    cinco guardas de reactivacion no se evaluaban NUNCA para esa fila.

    Esta es la combinacion que ejecuto el revisor contra el codigo viejo: dos dias tarde, el
    freno disparado, el tope disparado, domingo con la clinica abierta -> `Decision(accion=
    'enviar', motivo='ok')`, con las cinco guardas sin evaluar. Con la polaridad corregida
    (`not in TIPOS_NO_COMERCIALES`, la MISMA que usa G0) esa fila queda protegida: R1 la
    intercepta antes de que las demas ni se miren.
    """
    decision = s.decidir(
        fila_de_reactivacion(
            tipo="reactivacion",  # fuera del vocabulario A PROPOSITO: no es TIPO_SIN_AGENDAR
            fecha_objetivo=momento(18, 11),   # dos dias antes de `ahora`
            seguimientos_fallidos=9,
            reactivaciones_ultimo_ano=99,
        ),
        ahora=momento(20, 11),                  # domingo
        jornada=Jornada(atiende_domingo=True),  # la clinica ABRE
        ultimo_mensaje=None,
    )
    assert decision.accion != "enviar"
    # R1 va primero: con `seguimientos_fallidos=9` es la guarda que dispara.
    assert decision.motivo == "seguimiento_apagado"


# ==========================================================================================
# Ronda 2 de revision sobre la parada B: el ancla de R3bis era la equivocada.
#
# `creado_en` mide la EDAD TOTAL de la fila, no cuanto lleva atascada sin poder salir. Una
# reactivacion programada a dos semanas vista (`programar_seguimiento` no le pone cota
# superior a `fecha_objetivo`) es "vieja" desde que se crea segun `creado_en`, y llegaba
# PUNTUAL -nunca se aplazo ni una vez-. La version con `creado_en` la anulaba en silencio con
# un motivo que decia justo lo contrario de lo que habia pasado. El ancla correcta es
# `aplazado_desde` (migracion 022): NULL mientras la fila nunca se aplazo, fijo desde la
# PRIMERA vez que algo la frena.
# ==========================================================================================


def test_r3bis_una_reactivacion_que_lleva_dias_atascada_no_sale():
    """R3 se queda ciego cuando `fecha_objetivo` se reaplaza.

    `aplazar_seguimiento` reescribe `fecha_objetivo` en cada aplazamiento (G4, G5, R4), asi
    que R3 -que mide contra `fecha_objetivo`- se pone a cero cada vez. Reproducido a mano: una
    reactivacion que se atasca por primera vez el viernes a las 18:30 (relevo puesto) encadena
    aplazar (`relevo_activo`) -> aplazar (`fuera_de_horario_comercial`) y termina saliendo el
    sabado a las 09:00 con 14,5 h de deriva real que R3 ve como CERO. Con un relevo sostenido
    varios dias la cuenta de R3 sigue en cero indefinidamente.

    Aqui se simula el resultado de esa deriva sin reproducir la cadena entera de
    aplazamientos: una fila que se atasco por primera vez hace 5 dias (`aplazado_desde`) cuya
    `fecha_objetivo` quedo "fresca" hace una hora, tras el ultimo aplazamiento.
    """
    decision = s.decidir(
        fila_de_reactivacion(
            aplazado_desde=momento(12, 11),  # se atasco por primera vez hace 5 dias
            fecha_objetivo=momento(17, 10),  # "fresca": reaplazada hace 1 h
        ),
        ahora=momento(17, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "anular"
    assert decision.motivo == "reactivacion_estancada"


def test_r3bis_no_corta_un_fin_de_semana_legitimo():
    """El umbral de R3bis (96 h) tiene que dejar respirar un fin de semana normal de relevo
    sostenido: atascada por primera vez el viernes en la tarde y despachada el lunes a la
    apertura son unas 63 h, menos que el techo. Sin este caso, subir el umbral por error a
    algo mas estricto que un fin de semana pasaria en silencio: ninguna otra prueba lo
    notaria. Es la que impide bajar el umbral de mas."""
    decision = s.decidir(
        fila_de_reactivacion(
            aplazado_desde=momento(18, 18),  # se atasco por primera vez el viernes en la tarde
            fecha_objetivo=momento(21, 9),   # lunes a la apertura
        ),
        ahora=momento(21, 9),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "enviar"


def test_r3bis_no_dispara_si_nunca_se_aplazo_aunque_la_fecha_objetivo_sea_lejana():
    """El caso que `creado_en` cazaba mal (hallazgo de la ronda 2, ejecutado contra el codigo
    viejo): una reactivacion programada a DOS SEMANAS vista -`programar_seguimiento` no le
    pone cota superior a `fecha_objetivo`-, que nunca tuvo que aplazarse ni una vez y llega
    puntual el dia que le tocaba. `aplazado_desde` sigue en `None` -el default de
    `fila_de_reactivacion`- y R3bis ni se evalua: no importa cuanta distancia haya entre
    cuando se creo y su `fecha_objetivo`, porque esta guarda no mira eso."""
    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=momento(16, 11)),
        ahora=momento(16, 11),  # llega EXACTO a su fecha_objetivo: nunca se aplazo
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "enviar"


def test_r3bis_no_dispara_sobre_el_segundo_intento_de_la_tarea_6_a_siete_dias():
    """El caso concreto que el revisor senalo: la tarea 6 de este plan siembra el segundo
    intento de la serie con `fecha_objetivo` a 7 dias vista (168 h). 168 > 96 -el umbral de
    R3bis-, asi que con la version vieja (`creado_en`) esa parada nacia muerta contra esta
    guarda. Con `aplazado_desde` en `None` -nunca se aplazo- la fila llega puntual y sale."""
    creada = momento(9, 11)
    siete_dias_despues = momento(16, 11)
    assert (siete_dias_despues - creada).total_seconds() / 3600 == 168  # el numero del hallazgo

    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=siete_dias_despues),
        ahora=siete_dias_despues,  # llega EXACTO: nunca se aplazo
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "enviar"


def test_el_freno_no_alcanza_al_recordatorio_de_una_cita_ni_tampoco_r3bis():
    """R3bis es una guarda de reactivacion mas: gateada por `es_reactivacion`, igual que
    R1-R5. Un recordatorio de cita con `aplazado_desde` de hace un mes -como si llevara
    atascado todo ese tiempo- sigue sin tocarse."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1", cita_inicio=momento(17, 9),
        cita_estado="confirmada", aplazado_desde=momento(1, 0), fecha_objetivo=momento(16, 18),
    )
    decision = s.decidir(fila, ahora=momento(16, 18), jornada=JORNADA, ultimo_mensaje=None)
    assert decision.accion == "enviar"


# -- Tarea 4: el despacho multiplantilla ----------------------------------------------------
#
# La puerta `sin_plantilla` anulaba TODO lo que llegara a "enviar" sin `cita_inicio`. Esa
# puerta era lo que hacia inofensivo cualquier fallo de las guardas de arriba: mientras no
# hubiera plantilla para una reactivacion, no podia salir nada por WhatsApp aunque las seis
# guardas fallaran todas a la vez. Estas pruebas cubren lo que la sustituye.


def test_la_reactivacion_manda_UN_hueco_y_es_el_nombre_de_pila():
    """Marketing quito el tratamiento a proposito: es un dato de salud y una notificacion se
    lee en la pantalla de bloqueo. Si esta funcion devolviera cuatro huecos, Meta rechaza el
    envio y ademas se filtraria."""
    parametros = s.parametros_de(fila_de_reactivacion(nombre_completo="Marcela Rios Gomez"))
    assert parametros == ["Marcela"]


def test_el_recordatorio_de_cita_sigue_mandando_sus_cuatro_huecos():
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1",
        cita_inicio=momento(17, 9), tratamiento="Limpieza",
    )
    assert s.parametros_de(fila) == ["Marcela", "jueves 17/9", "09:00", "Limpieza"]


def test_sin_plantilla_para_ese_tipo_no_se_manda_y_no_se_marca():
    """Si falta la plantilla de un tipo, esa fila NO se marca como enviada: se queda pendiente
    hasta que la plantilla exista. Marcarla la perderia para siempre."""
    import asyncio

    enviados: list[dict] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    recuento = asyncio.run(
        _despachar_con(
            [fila_de_reactivacion()],
            whatsapp=_WhatsAppFalso(),
            plantillas={s.TIPO_RECORDATORIO: "recordatorio_cita"},  # falta la de reactivacion
            ahora=momento(16, 11),
        )
    )
    assert enviados == []
    assert recuento["enviados"] == 0


def test_cada_tipo_usa_SU_plantilla():
    import asyncio

    enviados: list[dict] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    asyncio.run(
        _despachar_con(
            [fila_de_reactivacion()],
            whatsapp=_WhatsAppFalso(),
            plantillas={
                s.TIPO_RECORDATORIO: "recordatorio_cita",
                s.TIPO_SIN_AGENDAR: "reactivacion_sin_agendar",
            },
            ahora=momento(16, 11),
        )
    )
    assert enviados[0]["plantilla"] == "reactivacion_sin_agendar"
    assert enviados[0]["parametros"] == ["Marcela"]

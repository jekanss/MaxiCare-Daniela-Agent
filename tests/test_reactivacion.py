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


async def _despachar_con(
    filas, *, whatsapp, plantillas, ahora, monkeypatch=None, marcadas=None, anuladas=None,
    freno_de_reactivacion=None, aplazadas=None,
):
    """Corre un ciclo de `despachar` con la base entera doblada.

    Se dobla `persistencia` y no la base: lo que se prueba es la DECISION y el reparto de
    plantillas, no el SQL. El SQL lo prueban las de `-m neon`.

    OJO: NO dobla `persistencia.contar_enviados_hoy` -- esa funcion no existe hoy. Es del tope
    DIARIO, que es del barrido (parada siguiente de este plan) y no del despachador. Doblar un
    atributo que no existe revienta con `AttributeError` antes de probar nada (Ruling C2).

    `marcadas` y `anuladas`, si se pasa una lista, registran cada `id_seguimiento` (y su
    motivo, para `anuladas`) que los dobles de `marcar_seguimiento_enviado` y
    `anular_seguimiento` reciben (I3, ronda 1 de revisión): sin esto los dobles solo
    devolvían `None`/`True` sin dejar rastro, y una prueba que afirma "y no se marca" -o que
    exige un motivo concreto de anulación- no puede fallar por eso -- una aserción sobre una
    AUSENCIA no distingue "se decidió no hacerlo" de "el doble no lo habría contado de todas
    formas" (`.claude/rules/pruebas.md`).
    """
    import pytest as _pytest
    from maxicare_daniela import persistencia

    mp = monkeypatch or _pytest.MonkeyPatch()

    class _ConexionFalsa:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
        def close(self): pass

    def _marcar(conn, id_seguimiento):
        if marcadas is not None:
            marcadas.append(id_seguimiento)
        return True

    def _anular(conn, id_seguimiento, **k):
        if anuladas is not None:
            anuladas.append((id_seguimiento, k.get("motivo")))

    mp.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    mp.setattr(persistencia, "leer_configuracion", lambda conn: {})
    mp.setattr(persistencia, "seguimientos_por_despachar", lambda conn, **k: list(filas))
    mp.setattr(persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None)
    mp.setattr(persistencia, "marcar_seguimiento_enviado", _marcar)
    mp.setattr(persistencia, "anular_seguimiento", _anular)
    def _aplazar(conn, id_seguimiento, **k):
        if aplazadas is not None:
            aplazadas.append((id_seguimiento, k.get("hasta")))

    mp.setattr(persistencia, "aplazar_seguimiento", _aplazar)
    mp.setattr(persistencia, "anotar_recordatorio_en_conversacion", lambda conn, i, **k: None)
    try:
        return await s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=whatsapp,
            jornada=JORNADA,
            plantillas=plantillas,
            ahora=ahora,
            freno_de_reactivacion=freno_de_reactivacion,
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

    # Se busca por NOMBRE y no por número: la migración nació como `021` y al fundir esta
    # rama pasó a `023`, porque `main` ya había ocupado el 021 y el 022 y las dos estaban
    # aplicadas en producción. Un número clavado aquí vuelve a romperse en el siguiente
    # renumerado, y el fallo aparece lejos de su causa. La aserción de que hay EXACTAMENTE
    # una es la mitad que importa: un glob que no casara con nada dejaría esta prueba
    # comprobando el CHECK de un archivo vacío, en verde.
    candidatas = sorted(Path("migraciones").glob("*_reactivacion.sql"))
    assert len(candidatas) == 1, f"se esperaba una sola migración de reactivación: {candidatas}"
    sql = candidatas[0].read_text(encoding="utf-8")

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

    `nombre_completo=None` por defecto, y NO a mano (hallazgo CRÍTICO de la ronda 1 de
    revisión de la tarea 4): esa columna sale de `LEFT JOIN citas`, y **toda fila de
    reactivación tiene `cita_id` NULL** -una fila de reactivación CON `cita_id` la anularía
    G1-, así que el SELECT real NUNCA le da un valor a esta columna en una fila así. La
    versión anterior de esta fábrica la fijaba a `"Marcela Rios"`, un valor que el SELECT no
    puede producir para esta fila, y eso dejaba pasar en verde una prueba
    (`test_la_reactivacion_manda_UN_hueco_y_es_el_nombre_de_pila`) que en producción habría
    fallado: el 100% de las reactivaciones salían con el respaldo "paciente". El nombre para
    el hueco sale ahora de `nombre_perfil` -el nombre de perfil de WhatsApp, la única fuente
    que existe de verdad para un lead que nunca agendó- y `nombre_ficha` queda en `None`
    porque tampoco tiene ficha en `pacientes` -eso solo lo tiene quien ya agendó alguna vez.
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
        nombre_completo=None,
        nombre_ficha=None,
        nombre_perfil="Marcela Rios",
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
    guarda. Con `aplazado_desde` en `None` -nunca se aplazo- la fila llega puntual y sale.

    **Revisión final: esta prueba y la anterior eran la MISMA llamada byte por byte**, y su
    única aserción propia (`== 168`) era aritmética de `datetime`, no del código bajo prueba.
    R3bis es justo la guarda cuyo ancla se fijó mal DOS veces en esta rama, así que estaba
    cubierta por dos pruebas idénticas y por un `assert` que no podía fallar nunca por un
    cambio en `seguimientos.py`.

    Lo que la diferencia ahora es `creado_en`: la fila se creó 168 h antes de su
    `fecha_objetivo`, que es lo que de verdad pasa en la tarea 6. Esa clave **no está en el
    SELECT de `persistencia.seguimientos_por_despachar`** y por eso `decidir` no la mira hoy
    -- está aquí precisamente para que un re-anclaje futuro a `creado_en` (el error de la ronda
    1, cometido dos veces) haga fallar ESTA prueba, en vez de pasar en verde como pasó
    entonces. El valor no es inventado: es el que la columna `creado_en` de la 001 tendría.
    """
    creada = momento(9, 11)
    siete_dias_despues = momento(16, 11)
    assert (siete_dias_despues - creada).total_seconds() / 3600 == 168  # el numero del hallazgo

    decision = s.decidir(
        fila_de_reactivacion(fecha_objetivo=siete_dias_despues, creado_en=creada),
        ahora=siete_dias_despues,  # llega EXACTO: nunca se aplazo
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "enviar", (
        "una fila creada 168 h antes de su fecha_objetivo, que llega puntual y nunca se "
        "aplazó, tiene que salir: si esto falla, R3bis volvió a medir la EDAD de la fila en "
        "vez de cuánto lleva atascada"
    )


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


def test_la_reactivacion_CON_hueco_manda_uno_y_es_el_nombre_de_pila():
    """Marketing quito el tratamiento a proposito: es un dato de salud y una notificacion se
    lee en la pantalla de bloqueo. Si esta funcion devolviera cuatro huecos, Meta rechaza el
    envio y ademas se filtraria.

    Se fija por `nombre_perfil` -y NO por `nombre_completo`, hallazgo CRITICO de la ronda 1 de
    revision-: `nombre_completo` sale de `LEFT JOIN citas`, y toda fila de reactivacion tiene
    `cita_id` NULL, asi que el SELECT real jamas le da un valor. Fijarlo a mano aqui habria
    tapado otra vez el mismo defecto que esta prueba existe para cazar.

    `TIPO_CANCELADA` explicito desde el 23/09/2026: el default de la fabrica es
    `TIPO_SIN_AGENDAR`, y esa plantilla ya no tiene hueco (`TIPOS_SIN_HUECOS`). Estas pruebas
    de la cascada siguen vivas porque las otras dos plantillas SI lo conservan -- y ahi el
    nombre es de fiar, que es justo la razon de que lo conserven.
    """
    parametros = s.parametros_de(
        fila_de_reactivacion(tipo=s.TIPO_CANCELADA, nombre_perfil="Marcela Rios Gomez")
    )
    assert parametros == ["Marcela"]


def test_la_reactivacion_prefiere_la_ficha_de_pacientes_sobre_el_perfil():
    """Quien ya agendo alguna vez -TIPO_CANCELADA, TIPO_NO_ASISTIO- tiene ficha en `pacientes`,
    y esa ficha es mas confiable que el nombre de perfil de WhatsApp: es la que el paciente dio
    al agendar, no un apodo o el nombre de un negocio."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_NO_ASISTIO, nombre_ficha="Ana Perez", nombre_perfil="Anita <3",
    )
    assert s.parametros_de(fila) == ["Ana"]


def test_la_ficha_marcada_NOMBRE_PENDIENTE_no_cuenta_como_nombre():
    """Regla dura 12: esa ficha no es una ficha. Si se usara, la reactivacion diria "Hola
    PENDIENTE" -- peor que el respaldo "paciente" que esta ronda elimino."""
    from maxicare_daniela import persistencia

    fila = fila_de_reactivacion(
        tipo=s.TIPO_CANCELADA,
        nombre_ficha=persistencia.NOMBRE_PENDIENTE,
        nombre_perfil="Marcela",
    )
    assert s.parametros_de(fila) == ["Marcela"]


def test_una_reactivacion_sin_ningun_nombre_usable_no_se_manda():
    """Un nombre de perfil que es solo un emoji -o vacio, o puro simbolo- no tiene ninguna
    letra: `parametros_de` devuelve `None` y `despachar` la anula en vez de mandar "Hola 🌸"
    o "Hola paciente". Ante la duda, no se manda."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil="🌸"
    )
    assert s.parametros_de(fila) is None


def test_un_nombre_de_perfil_vacio_o_solo_espacios_tampoco_es_usable():
    for perfil in (None, "", "   "):
        fila = fila_de_reactivacion(
            tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil=perfil
        )
        assert s.parametros_de(fila) is None, repr(perfil)


def test_un_emoji_delante_del_nombre_no_se_manda_como_si_fuera_el_nombre():
    """El defecto de la ronda 2 de revision: la guarda vieja validaba la cadena ENTERA -"tiene
    alguna letra en algun lado?"- y `"🌸 Ana"` la pasaba porque "Ana" tiene letras, pero el
    token que de verdad viajaba a Meta era el PRIMERO ("🌸" a secas): "Hola 🌸" es peor que
    "Hola paciente", parece un bot roto. Ahora se parte primero y se valida el token que se va
    a mandar, no la cadena completa -- con estos tres "hermanos" del mismo estilo (emoji +
    nombre, la forma mas comun de nombre de perfil en WhatsApp), la fila se anula en vez de
    mandar el emoji solo."""
    for perfil in ("🌸 Ana", "💖 Andrea", "✨ Ana"):
        fila = fila_de_reactivacion(
            tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil=perfil
        )
        assert s.parametros_de(fila) is None, repr(perfil)


def test_un_nombre_que_empieza_con_espacio_no_cae_al_respaldo_paciente():
    """Residuo de la ronda 2: ni `asegurar_paciente` ni `registrar_cita` recortan lo que
    llega, asi que una ficha como " Ana Perez" es alcanzable. Con `.split(" ")[0]` el primer
    token era una cadena vacia y el respaldo `or "paciente"` volvia a colar el literal que
    este hallazgo entero existe para sacar de las reactivaciones. `.split()` sin argumento
    ignora los espacios de sobra y rescata el nombre real -- "paciente" no puede aparecer
    aqui NUNCA, y esta prueba lo deja en firme con una fila que antes lo producia."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_CANCELADA, nombre_ficha=" Ana Perez", nombre_perfil=None
    )
    assert s.parametros_de(fila) == ["Ana"]


def test_un_salto_de_linea_interno_no_sobrevive_al_hueco():
    """Residuo de la ronda 2: Meta RECHAZA un parametro de plantilla con saltos de linea, y
    como la fila se marca ANTES de enviar (no negociable 21) se perderia para siempre tras
    los tres intentos. `.split(" ")[0]` no cortaba por `\\n` ni por `\\t` -solo por el caracter
    espacio literal-, asi que `"Ana\\nPerez"` sobrevivia entero, salto de linea incluido.
    `.split()` sin argumento corta por CUALQUIER espacio en blanco."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil="Ana\nPerez"
    )
    assert s.parametros_de(fila) == ["Ana"]


def test_el_recordatorio_de_cita_sigue_mandando_sus_cuatro_huecos():
    """A diferencia de una reactivacion, un recordatorio de verdad SI trae `nombre_completo`:
    sale de `citas`, que lo declara `NOT NULL`, y por eso este es el unico test del archivo
    que necesita fijarlo a mano -- el default de `fila_de_reactivacion` lo deja en `None` a
    proposito (ver su docstring)."""
    fila = fila_de_reactivacion(
        tipo=s.TIPO_RECORDATORIO, cita_id="cita-1", nombre_completo="Marcela Rios",
        cita_inicio=momento(17, 9), tratamiento="Limpieza",
    )
    assert s.parametros_de(fila) == ["Marcela", "jueves 17/9", "09:00", "Limpieza"]


# -- La plantilla que se quedo sin variables (23/09/2026) ------------------------------------
#
# `reactivacion_sin_agendar` perdio su unico hueco. El porque, con la medicion entera sobre la
# base, esta en `seguimientos.TIPOS_SIN_HUECOS`: aqui solo lo que tiene que seguir siendo
# cierto para que no se rompa por el lado que no se ve.


def test_reactivacion_sin_agendar_no_manda_NINGUN_hueco():
    """La fabrica usa `TIPO_SIN_AGENDAR` por defecto, que es el tipo que el barrido encola de
    verdad. Ni un nombre perfectamente bueno viaja ya: la plantilla no tiene donde ponerlo, y
    un parametro de mas sobre una plantilla estatica es un 132000."""
    assert s.parametros_de(fila_de_reactivacion(nombre_perfil="Marcela Rios")) == []


def test_un_perfil_de_whatsapp_impresentable_ya_no_puede_llegar_a_un_paciente():
    """Los seis casos medidos en la base el 23/09/2026, y no son casos de esquina: son 6 de 17
    numeros. Dos ya habian salido de verdad --«Hola jg390485»-- y eso es lo que costo el
    hueco. `Jean♣️` esta aqui porque ademas enseña que `_primer_nombre_usable` solo caza el
    emoji SEPARADO: pegado al nombre, el token entero viaja a Meta."""
    for perfil in (
        "jg390485", "rodolfopatino015", "centro", "canomoraleswilmerandres",
        "yualetxis", "Jean♣️",
    ):
        assert s.parametros_de(fila_de_reactivacion(nombre_perfil=perfil)) == [], perfil


def test_la_lista_vacia_no_se_puede_confundir_con_la_fila_rota():
    """`despachar` anula con `sin_nombre` cuando `parametros_de` devuelve `None`, y lo
    comprueba con `is None` a proposito: `[]` es falsy, asi que un `if not parametros` en ese
    punto anularia TODAS las filas de esta plantilla -- el canal entero apagado, sin un error
    en ningun log y con la suite en verde. Son dos estados distintos y esta prueba los
    separa."""
    vacia = s.parametros_de(fila_de_reactivacion(nombre_perfil="🌸"))
    rota = s.parametros_de(
        fila_de_reactivacion(tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil="🌸")
    )
    assert vacia == [] and vacia is not None
    assert rota is None


def test_las_otras_dos_plantillas_de_reactivacion_CONSERVAN_su_hueco():
    """La diferencia no es la plantilla, es de donde sale el nombre: quien cancelo o no
    asistio YA agendo alguna vez, asi que tiene ficha en `pacientes` con el nombre que dio
    para su cita --lo escribe `crear_cita`-- y ahi el hueco si es de fiar. Meter estos dos
    tipos en `TIPOS_SIN_HUECOS` haria que Meta rechazara sus envios."""
    for tipo in (s.TIPO_CANCELADA, s.TIPO_NO_ASISTIO):
        fila = fila_de_reactivacion(tipo=tipo, nombre_ficha="Ana Perez")
        assert s.parametros_de(fila) == ["Ana"], tipo


def test_una_sin_agendar_sin_ningun_nombre_YA_NO_se_anula_y_sale():
    """El contraste que cierra el cambio, contra `despachar` entero. Esta misma fila --perfil
    que es solo un emoji-- se anulaba con `sin_nombre` y no salia; ahora sale, porque no hay
    nada que rellenar. Es la unica prueba que demuestra que quitar el hueco no solo cambia los
    parametros: recupera a quien la guarda dejaba fuera."""
    import asyncio

    enviados: list[dict] = []
    anuladas: list[tuple[int, str]] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    recuento = asyncio.run(
        _despachar_con(
            [fila_de_reactivacion(nombre_ficha=None, nombre_perfil="🌸")],
            whatsapp=_WhatsAppFalso(),
            plantillas={s.TIPO_SIN_AGENDAR: "reactivacion_sin_agendar"},
            ahora=momento(16, 11),
            anuladas=anuladas,
        )
    )

    assert anuladas == []
    assert enviados[0]["parametros"] == []
    assert recuento["enviados"] == 1


def test_sin_plantilla_para_ese_tipo_no_se_manda_y_no_se_marca():
    """Si falta la plantilla de un tipo, esa fila NO se marca como enviada: se queda pendiente
    hasta que la plantilla exista. Marcarla la perderia para siempre.

    I3 (ronda 1 de revision): el doble de `marcar_seguimiento_enviado` ahora REGISTRA cada
    llamada (`marcadas`), y esta prueba comprueba la lista vacia -- no solo que no se mando
    nada por WhatsApp. Sin eso, "y no se marca" era una afirmacion que el doble viejo no podia
    dejar en rojo aunque `despachar` marcara la fila: el doble devolvia `True` sin rastro.
    """
    import asyncio

    enviados: list[dict] = []
    marcadas: list[int] = []

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
            marcadas=marcadas,
        )
    )
    assert enviados == []
    assert marcadas == []
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
    # Sin huecos desde el 23/09/2026 (`TIPOS_SIN_HUECOS`): la plantilla que Meta aprobo para
    # este tipo ya no tiene ni una variable, asi que mandar "Marcela" seria un 132000.
    assert enviados[0]["parametros"] == []


def test_una_reactivacion_sin_nombre_usable_se_anula_y_no_se_manda_ni_se_marca():
    """El hallazgo CRITICO de principio a fin, contra `despachar` completo: con la plantilla
    puesta Y sin ningun nombre usable, la fila se ANULA -no se manda "Hola paciente", y no se
    deja pendiente para siempre (acumularia basura que el despachador relee cada 60 s sin
    ninguna salida posible).

    Sobre `TIPO_CANCELADA` desde el 23/09/2026: `sin_nombre` solo puede alcanzar a una
    plantilla que TENGA hueco, y `reactivacion_sin_agendar` perdio el suyo. La guarda no se
    fue -- se quedo con dos tipos en vez de tres."""
    import asyncio

    enviados: list[dict] = []
    anuladas: list[tuple[int, str]] = []
    marcadas: list[int] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    recuento = asyncio.run(
        _despachar_con(
            [
                fila_de_reactivacion(
                    tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil="🌸"
                )
            ],
            whatsapp=_WhatsAppFalso(),
            plantillas={
                s.TIPO_RECORDATORIO: "recordatorio_cita",
                s.TIPO_CANCELADA: "reactivacion_cancelada",
            },
            ahora=momento(16, 11),
            marcadas=marcadas,
            anuladas=anuladas,
        )
    )

    assert enviados == []
    assert marcadas == []
    assert anuladas == [(1, "sin_nombre")]
    assert recuento == {"enviados": 0, "anulados": 1, "aplazados": 0, "fallidos": 0}


def test_en_modo_de_comprobacion_una_reactivacion_sin_nombre_queda_pendiente_como_sus_hermanas():
    """El arreglo de la ronda 2: el chequeo de `sin_nombre` se movio DESPUES del de la
    plantilla. Antes, con el canal apagado -las cuatro plantillas vacias, el modo de
    comprobacion de hoy-, una reactivacion sin nombre usable se ANULABA igual, mientras el
    resto de la cola se quedaba pendiente sin tocar. Eso rompia el invariante de esta fase:
    "decide, registra, y NO TOCA nada mientras no haya plantilla" -el ensayo existe para ver
    a quien se le habria escrito ANTES de escribirle a nadie, y una fila que se consume sola
    durante el ensayo hace que el ensayo mienta sobre esa fila en particular. Ahora se queda
    pendiente igual que sus hermanas."""
    import asyncio

    enviados: list[dict] = []
    anuladas: list[tuple[int, str]] = []
    marcadas: list[int] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    recuento = asyncio.run(
        _despachar_con(
            [
                fila_de_reactivacion(
                    tipo=s.TIPO_CANCELADA, nombre_ficha=None, nombre_perfil="🌸"
                )
            ],
            whatsapp=_WhatsAppFalso(),
            plantillas={},  # el modo de comprobacion de hoy: las cuatro plantillas vacias
            ahora=momento(16, 11),
            marcadas=marcadas,
            anuladas=anuladas,
        )
    )

    assert enviados == []
    assert marcadas == []
    assert anuladas == []
    assert recuento == {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 0}


def test_un_tipo_desconocido_sin_cita_inicio_se_anula_fail_closed():
    """M3 (ronda 1 de revision): antes de acotar la fila ROTA a `TIPO_RECORDATORIO`, un `tipo`
    fuera de las tres reactivaciones conocidas -invalido, o uno futuro que el CHECK NOT VALID
    de la 021 no alcanza a rechazar- y sin `cita_inicio` se quedaba pendiente PARA SIEMPRE: el
    despachador lo releeria cada 60 s sin dejar rastro del motivo. Ahora cierra fail-closed
    contra `TIPOS_DE_REACTIVACION`, la misma polaridad que `es_reactivacion` en `decidir`."""
    import asyncio

    anuladas: list[tuple[int, str]] = []

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            return "wamid.X"

    recuento = asyncio.run(
        _despachar_con(
            [fila_de_reactivacion(tipo="un_tipo_que_no_existe", cita_inicio=None)],
            whatsapp=_WhatsAppFalso(),
            plantillas={s.TIPO_RECORDATORIO: "recordatorio_cita"},
            ahora=momento(16, 11),
            anuladas=anuladas,
        )
    )

    assert anuladas == [(1, "sin_cita")]
    assert recuento == {"enviados": 0, "anulados": 1, "aplazados": 0, "fallidos": 0}


def test_parametros_de_un_tipo_desconocido_cae_al_lado_ESTRECHO_no_al_de_cuatro_huecos():
    """I1 (ronda 1 de revision): la polaridad estaba al reves. La version anterior mandaba
    CUATRO huecos con el tratamiento dentro -un dato de salud- a todo lo que no fuera una de
    las tres reactivaciones CONOCIDAS por su lado explicito, asi que un `tipo` invalido o uno
    futuro caia del lado que SI manda el tratamiento. Ahora el lado por defecto es el
    estrecho: un hueco, sin tratamiento, igual que cualquier reactivacion."""
    fila = fila_de_reactivacion(
        tipo="un_tipo_que_no_existe", nombre_ficha=None, nombre_perfil="Marcela",
        tratamiento="Ortodoncia",
    )
    assert s.parametros_de(fila) == ["Marcela"]


# ==========================================================================================
# Tarea 6: «Ya no, gracias» cierra la serie.
# ==========================================================================================


def test_cerrar_seguimiento_deja_constancia_permanente_y_sube_el_contador():
    """1 pequeña (ronda de revisión sobre la parada D): el motivo tiene que ser el LITERAL
    exacto del código, no cualquier cadena. Es el no negociable 2 aplicado aquí: lo que acaba
    en `seguimientos.motivo_anulacion` lo escribe el CÓDIGO, nunca el modelo -- el modelo solo
    aporta `nota`, que ni siquiera viaja hasta la base.

    **Revisión final (H1): la puerta ya no es `anular_reactivaciones_vivas`.** Esa función sola
    no puede guardar un «no» -- su WHERE exige `enviado_en IS NULL`, y el paciente solo puede
    decir que no DESPUÉS de que el mensaje salió--, así que devolvía 0 siempre y el motivo
    permanente no llegaba nunca a la tabla. La puerta es
    `registrar_negativa_de_reactivacion`, que anula lo vivo Y escribe la lápida. Esta prueba
    afirma sobre ELLA, y que los tipos que recibe son los del vocabulario del barrido y no una
    lista escrita a mano aquí.
    """
    import asyncio
    from unittest.mock import MagicMock

    from maxicare_daniela import herramientas as h
    from tests.test_herramientas import contexto

    llamadas = {}

    def _registrar(conn, telefono, *, id_conversacion, tipos):
        llamadas["registrar"] = (telefono, id_conversacion, tuple(tipos))
        return {"anulados": 0, "lapidas": list(tipos)}

    def _sumar(conn, telefono):
        llamadas["sumar"] = telefono
        return 1

    async def _con_base_falsa(ctx, trabajo):
        # `_con_base` real es `async def` -- un lambda sincrono aqui haria que
        # `await _con_base(...)` reventara con `object no puede usarse en 'await'`
        # sin llegar a probar nada de `_cerrar_seguimiento`.
        return trabajo(MagicMock())

    ctx = contexto()
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(h.persistencia, "registrar_negativa_de_reactivacion", _registrar)
        mp.setattr(h.persistencia, "sumar_seguimiento_fallido", _sumar)
        mp.setattr(h, "_con_base", _con_base_falsa)
        asyncio.run(h._cerrar_seguimiento(ctx, "ya no me interesa"))

    telefono, conversacion, tipos = llamadas["registrar"]
    assert telefono == ctx.telefono_completo
    assert conversacion == ctx.id_conversacion
    assert set(tipos) == set(s.TIPOS_QUE_EL_BARRIDO_ENCOLA), (
        "los tipos sobre los que se escribe la lápida tienen que salir del vocabulario del "
        "barrido: si se escriben a mano, el día que se encienda `reactivacion_no_asistio` el "
        "«no» del paciente no lo cubrirá y nadie se enterará"
    )
    assert llamadas["sumar"] == ctx.telefono_completo


def test_el_no_del_paciente_no_vive_en_el_contador():
    """El corolario del hallazgo H1, y la razón por la que la lápida existe.

    Lo único duradero que `cerrar_seguimiento` escribía era `+1` en
    `contactos.seguimientos_fallidos`, y ese contador NO es un sitio donde pueda vivir un «no»:
    su tope es `max_seguimientos_fallidos`, una perilla EDITABLE de `configuracion`, y
    `crear_cita` lo devuelve a 0. Subir la perilla de 2 a 3 -- una decisión de marketing
    perfectamente razonable-- o que la persona agende una vez borraba el «no» entero.

    Esta prueba fija que la constancia permanente NO depende del contador: `_cerrar_seguimiento`
    llama a la función que escribe en `seguimientos`, y lo hace ANTES de tocar el contador.
    """
    import inspect

    from maxicare_daniela import herramientas as h

    fuente = inspect.getsource(h._cerrar_seguimiento)
    assert "registrar_negativa_de_reactivacion" in fuente
    assert fuente.index("registrar_negativa_de_reactivacion") < fuente.index(
        "sumar_seguimiento_fallido"
    ), "la constancia permanente va antes que el contador, no al revés"


def test_cerrar_seguimiento_NO_marca_la_baja():
    """D2: «ya no me interesa esta consulta» no es «no me escriban nunca mas».

    Marcar la baja aqui quema a un paciente por una frase que no dijo, y es lo unico de los
    dos que no se deshace sin que la persona lo pida.

    Corrige una imprecision del informe de la parada D: la version anterior de esta prueba
    solo inspeccionaba `_cerrar_seguimiento`, y el informe afirmaba -de mas- que tambien
    cubria `anular_reactivaciones_vivas`. Ahora sí cubre las dos, que es lo unico que hace
    cierta la conclusion "ningun camino marca la baja".
    """
    import inspect

    from maxicare_daniela import herramientas as h
    from maxicare_daniela import persistencia

    assert "pedir_baja" not in inspect.getsource(h._cerrar_seguimiento)
    assert "pedir_baja" not in inspect.getsource(persistencia.anular_reactivaciones_vivas)
    # La tercera puerta, desde la revisión final: la que de verdad escribe la constancia.
    assert "pedir_baja" not in inspect.getsource(
        persistencia.registrar_negativa_de_reactivacion
    )
    assert "no_contactar" not in inspect.getsource(
        persistencia.registrar_negativa_de_reactivacion
    ), "la lápida es por consulta; tocar `no_contactar` la convertiría en la baja"


# ==========================================================================================
# Tarea 7.0: el centinela de calidad (regla 11)
# ==========================================================================================


@pytest.mark.parametrize(
    "calidad,debe_encolar",
    [("GREEN", True), ("YELLOW", False), ("RED", False), ("FLAGGED", False),
     ("UNKNOWN", True), ("NA", True), (None, False)],
)
def test_regla_11_la_calidad_del_numero_manda(calidad, debe_encolar):
    """`None` = la consulta fallo, y ahi NO se encola.

    Es la asimetria CONTRARIA a la del no negociable 26: alli, ante la duda se avisa porque
    molestar al doctor de mas es barato. Aqui lo barato es callarse -- el coste de no encolar
    durante una hora son leads, y el de encolar con la calidad en rojo es el numero.

    `UNKNOWN` y `NA` SI encolan: son los valores de un numero sin historial suficiente, no una
    senal de dano. Tratarlos como rojo dejaria el sistema apagado para siempre en una cuenta
    nueva, que es justo cuando mas falta hace.
    """
    from maxicare_daniela import barrido

    assert barrido.se_puede_encolar(calidad) is debe_encolar


def test_el_interruptor_apagado_no_toca_la_base(monkeypatch):
    """Regla 10, Ruling C8: `encendido=False` no llega ni a abrir una conexion."""
    from maxicare_daniela import barrido, persistencia

    def _explota(*a, **k):
        raise AssertionError("encolar() abrio una conexion con encendido=False")

    monkeypatch.setattr(persistencia, "conectar", _explota)
    recuento = barrido.encolar(
        database_url="postgresql://no-se-usa",
        ahora=momento(16, 11),
        tope_diario=20,
        encendido=False,
        calidad={"quality_rating": "GREEN"},
    )
    assert recuento["encolados"] == 0


def test_la_calidad_en_rojo_no_toca_la_base(monkeypatch):
    """Ruling C8: el porton de calidad va DESPUES de `encendido` y ANTES de `conectar` -- con
    la calidad en rojo tampoco hay por que abrir una conexion."""
    from maxicare_daniela import barrido, persistencia

    def _explota(*a, **k):
        raise AssertionError("encolar() abrio una conexion con la calidad en rojo")

    monkeypatch.setattr(persistencia, "conectar", _explota)
    recuento = barrido.encolar(
        database_url="postgresql://no-se-usa",
        ahora=momento(16, 11),
        tope_diario=20,
        encendido=True,
        calidad={"quality_rating": "RED"},
    )
    assert recuento == {"encolados": 0, "contabilizados": 0, "frenado_por_calidad": 1}


def test_la_calidad_desconocida_tampoco_toca_la_base(monkeypatch):
    """`calidad=None` es lo que le llega a `runtime` cuando `WhatsApp.calidad_del_numero`
    devuelve `{}` -- la consulta a Meta fallo. Ante la duda, no se abre ni conexion."""
    from maxicare_daniela import barrido, persistencia

    def _explota(*a, **k):
        raise AssertionError("encolar() abrio una conexion sin poder leer la calidad")

    monkeypatch.setattr(persistencia, "conectar", _explota)
    recuento = barrido.encolar(
        database_url="postgresql://no-se-usa",
        ahora=momento(16, 11),
        tope_diario=20,
        encendido=True,
        calidad=None,
    )
    assert recuento["frenado_por_calidad"] == 1


# ==========================================================================================
# Tarea 8: la tarea de fondo del barrido, con su interruptor
# ==========================================================================================


def test_el_barrido_no_arranca_sin_base(monkeypatch):
    """Mismo patron que `_arrancar_despacho_de_recordatorios`: sin `database_url` no hay
    ninguna base a la que preguntarle nada, y arrancar la tarea igual solo produciria una
    excepcion por ciclo, cada hora, para siempre."""
    import asyncio
    from dataclasses import replace

    from maxicare_daniela import runtime

    async def escenario():
        monkeypatch.setattr(runtime, "config", replace(runtime.config, database_url=""))
        monkeypatch.setattr(runtime, "_tarea_de_barrido_de_reactivacion", None)

        await runtime._arrancar_barrido_de_reactivacion()

        assert runtime._tarea_de_barrido_de_reactivacion is None

    asyncio.run(escenario())


def test_el_barrido_no_arranca_si_esta_apagado(monkeypatch):
    """Regla 10 en el arranque: `config.reactivacion_encendida=False` no crea la tarea, y lo
    dice en el log -- sin esto habria que mirar los logs cada hora para saber si el barrido
    esta corriendo de verdad o solo devolviendo ceros."""
    import asyncio
    from dataclasses import replace

    from maxicare_daniela import runtime

    async def escenario():
        monkeypatch.setattr(
            runtime,
            "config",
            replace(
                runtime.config,
                database_url="postgresql://no-se-usa",
                reactivacion_encendida=False,
            ),
        )
        monkeypatch.setattr(runtime, "_tarea_de_barrido_de_reactivacion", None)

        await runtime._arrancar_barrido_de_reactivacion()

        assert runtime._tarea_de_barrido_de_reactivacion is None

    asyncio.run(escenario())


def test_el_barrido_arranca_igual_sin_plantillas_de_reactivacion(monkeypatch):
    """Encolar sin plantilla es el modo de comprobacion (`seguimientos.despachar`): decide y
    registra, no manda nada. Apagar la TAREA por falta de plantilla apagaria tambien esa
    comprobacion, que es justo lo que hace falta mientras Meta no aprueba las tres."""
    import asyncio
    from dataclasses import replace

    from maxicare_daniela import runtime

    async def escenario():
        monkeypatch.setattr(
            runtime,
            "config",
            replace(
                runtime.config,
                database_url="postgresql://no-se-usa",
                reactivacion_encendida=True,
                plantilla_sin_agendar="",
                plantilla_cancelada="",
                plantilla_no_asistio="",
            ),
        )
        monkeypatch.setattr(runtime, "_tarea_de_barrido_de_reactivacion", None)

        await runtime._arrancar_barrido_de_reactivacion()

        tarea = runtime._tarea_de_barrido_de_reactivacion
        assert tarea is not None, "sin plantillas, el barrido NO arranco"
        assert not tarea.done()
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass

    asyncio.run(escenario())


# ==========================================================================================
# Ronda 1 de revisión sobre la parada E: 7.0.3 y 7.0.6 no tenían NI UNA prueba
# ==========================================================================================


class _RespuestaDeMetaFalsa:
    def __init__(self, cuerpo: dict) -> None:
        self._cuerpo = cuerpo

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._cuerpo


class _ClienteDeMetaFalso:
    """Un `httpx.AsyncClient` de mentira, mismo patrón que `test_relevo.py::_ClienteFalso`."""

    def __init__(self, cuerpo: dict) -> None:
        self._cuerpo = cuerpo
        self.llamadas: list[tuple[str, dict]] = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        self.llamadas.append((url, params or {}))
        return _RespuestaDeMetaFalsa(self._cuerpo)


class _ClienteDeMetaQueExplota:
    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        raise RuntimeError("timeout de verdad, o lo que sea")


def test_calidad_del_numero_devuelve_lo_que_dice_meta(monkeypatch):
    """7.0.3: nadie probaba `calidad_del_numero`. Verificado contra el número de producción
    el 17/09/2026 -- este es el cuerpo real que devolvió Meta."""
    import asyncio

    from maxicare_daniela import canales

    cliente = _ClienteDeMetaFalso(
        {"quality_rating": "GREEN", "messaging_limit_tier": "TIER_250", "status": "CONNECTED"}
    )
    monkeypatch.setattr(canales.httpx, "AsyncClient", cliente)

    resultado = asyncio.run(canales.WhatsApp("token-x", "123456").calidad_del_numero())

    assert resultado == {
        "quality_rating": "GREEN", "messaging_limit_tier": "TIER_250", "status": "CONNECTED",
    }
    url, parametros = cliente.llamadas[0]
    assert url == f"{canales.BASE_GRAPH}/123456"
    assert parametros == {"fields": "quality_rating,messaging_limit_tier,status"}


def test_calidad_del_numero_devuelve_vacio_si_la_llamada_falla(monkeypatch):
    """El contrato que `barrido.se_puede_encolar` necesita: un fallo de red no propaga, y
    `{}` es lo que ese código lee como "no se sabe" -- ante la duda, no se encola."""
    import asyncio

    from maxicare_daniela import canales

    monkeypatch.setattr(canales.httpx, "AsyncClient", _ClienteDeMetaQueExplota())

    resultado = asyncio.run(canales.WhatsApp("token-x", "123456").calidad_del_numero())

    assert resultado == {}


def test_el_aviso_de_calidad_no_se_repite_dentro_del_cooldown(monkeypatch):
    """7.0.6: el aviso al General cuando la calidad frena el barrido, como mucho una vez
    cada `SEGUNDOS_ENTRE_AVISOS_DE_CALIDAD`. Sin esta prueba, nadie comprobaba el cooldown."""
    import asyncio
    from dataclasses import replace

    from maxicare_daniela import runtime

    async def escenario():
        enviados = []

        async def _enviar_falso(texto, *, tema_id):
            enviados.append(texto)

        monkeypatch.setattr(runtime, "_telegram", type("T", (), {"enviar_mensaje": staticmethod(_enviar_falso)})())
        monkeypatch.setattr(
            runtime, "config",
            replace(runtime.config, telegram_bot_token="x", telegram_chat_doctores="-1"),
        )
        monkeypatch.setattr(runtime, "_ultimo_aviso_de_calidad_en", None)

        await runtime._avisar_de_calidad_del_numero({"quality_rating": "RED"})
        await runtime._avisar_de_calidad_del_numero({"quality_rating": "RED"})

        assert len(enviados) == 1, "el segundo aviso, dentro del cooldown, no debia salir"

    asyncio.run(escenario())


def test_el_aviso_sella_el_cooldown_aunque_no_haya_telegram(monkeypatch):
    """El bug real que el revisor encontró: sin Telegram configurado, la rama `log.error`
    salía por `return` ANTES de sellar `_ultimo_aviso_de_calidad_en` -- un ERROR en el log
    por cada ciclo del barrido (cada hora), para siempre, sin que nada lo silenciara. Ahora
    el sello va antes de la rama de Telegram."""
    import asyncio
    from dataclasses import replace

    from maxicare_daniela import runtime

    async def escenario():
        errores = []
        monkeypatch.setattr(runtime.log, "error", lambda *a, **k: errores.append(a))
        monkeypatch.setattr(
            runtime, "config", replace(runtime.config, telegram_bot_token="", telegram_chat_doctores=""),
        )
        monkeypatch.setattr(runtime, "_ultimo_aviso_de_calidad_en", None)

        await runtime._avisar_de_calidad_del_numero({"quality_rating": "RED"})
        await runtime._avisar_de_calidad_del_numero({"quality_rating": "RED"})

        assert len(errores) == 1, "sin sellar el cooldown, cada llamada vuelve a loguear ERROR"

    asyncio.run(escenario())


def test_el_bucle_de_reactivacion_avisa_cuando_la_calidad_frena(monkeypatch):
    """Wiring, 7.0.6: nadie probaba que `_avisar_de_calidad_del_numero` estuviera CABLEADO
    dentro de `_barrer_reactivacion_sin_parar` -- la parte que se rompe callada. Se deja
    correr el bucle UNA vuelta (el `sleep` se dobla para lanzar `CancelledError` en la
    segunda) y se comprueba que, con la calidad en rojo, sale el aviso."""
    import asyncio
    from dataclasses import replace

    from maxicare_daniela import barrido, runtime

    async def escenario():
        llamadas_aviso = []

        async def _calidad_falsa():
            return {"quality_rating": "RED"}

        async def _aviso_falso(calidad):
            llamadas_aviso.append(calidad)

        vueltas = {"n": 0}

        async def _sleep_una_vez(_segundos):
            vueltas["n"] += 1
            if vueltas["n"] > 1:
                raise asyncio.CancelledError()

        monkeypatch.setattr(
            runtime, "config", replace(runtime.config, database_url="postgresql://no-se-usa"),
        )
        monkeypatch.setattr(runtime, "_whatsapp", type("W", (), {"calidad_del_numero": staticmethod(_calidad_falsa)})())
        monkeypatch.setattr(runtime, "_avisar_de_calidad_del_numero", _aviso_falso)
        monkeypatch.setattr(runtime, "_leer_configuracion_operativa", lambda: {})
        monkeypatch.setattr(barrido, "se_puede_encolar", lambda *_: False)
        # OJO al doblar esto (higiene, ronda 2 de revisión): `runtime.asyncio` ES el módulo
        # `asyncio` del intérprete, no una copia -- el mismo patrón, con el mismo aviso, en
        # `tests/test_seguimientos.py::test_los_intentos_agotados_marcan_fallido_y_no_se_
        # pierden`. Hoy no muerde porque `monkeypatch` lo deshace al terminar esta prueba y
        # nada más corre en este bucle de eventos mientras tanto, pero es la clase de parche
        # que sí mordería si algo más usara `asyncio.sleep` de forma concurrente aquí.
        monkeypatch.setattr(runtime.asyncio, "sleep", _sleep_una_vez)

        try:
            await runtime._barrer_reactivacion_sin_parar()
        except asyncio.CancelledError:
            pass

        assert llamadas_aviso == [{"quality_rating": "RED"}], (
            "el aviso de calidad no se cableó dentro del bucle"
        )

    asyncio.run(escenario())


# ==========================================================================================
# Revisión final. Las seis que cierran lo que la rama dejó abierto.
# ==========================================================================================


# -- H6 bis: una guarda que se apaga sola al perder una columna del SELECT -------------------


def test_r0_una_reactivacion_sin_las_columnas_de_sus_guardas_NO_se_manda():
    """La forma exacta del primero de los cuatro fallos graves de esta rama, por otra puerta.

    R1, R2 y R3bis leen `seguimientos_fallidos`, `reactivaciones_ultimo_ano` y
    `aplazado_desde` de la fila. Se leían con `.get()` y respaldo permisivo, así que si el
    SELECT de `persistencia.seguimientos_por_despachar` perdía una de esas columnas, la guarda
    correspondiente se apagaba EN SILENCIO y `pytest -q` seguía entero en verde.

    Medido por el revisor contra el código de la rama: la MISMA fila -- contador 5 sobre un
    tope de 2, tope anual 99 sobre 6, 500 h atascada-- daba `anular/seguimiento_apagado` con
    las tres claves y `enviar/ok` sin ellas. Una fila que tenía TRES razones para no salir
    salía, por perder tres columnas de un SELECT.

    Lo único que lo sostenía era una prueba de `-m neon`. Esto se comprueba offline.
    """
    completa = fila_de_reactivacion(
        seguimientos_fallidos=5,
        reactivaciones_ultimo_ano=99,
        aplazado_desde=momento(1, 11),  # atascada desde hace más de dos semanas
    )
    con_todo = s.decidir(completa, ahora=momento(16, 11), jornada=JORNADA, ultimo_mensaje=None)
    assert con_todo.accion == "anular"

    for columna in s.COLUMNAS_DE_LAS_GUARDAS_DE_REACTIVACION:
        mutilada = {k: v for k, v in completa.items() if k != columna}
        decision = s.decidir(
            mutilada, ahora=momento(16, 11), jornada=JORNADA, ultimo_mensaje=None
        )
        assert decision.accion != "enviar", (
            f"perder la columna '{columna}' del SELECT apaga una guarda y deja SALIR una "
            f"reactivación que no debía salir: {decision}"
        )
        assert decision.motivo.startswith("fila_incompleta"), decision


def test_r0_no_confunde_una_columna_ausente_con_una_columna_en_NULL():
    """`aplazado_desde` en NULL es el estado normal de una fila que nunca se aplazó: si R0 lo
    tratara como "falta la columna", anularía TODA reactivación sana y la función quedaría
    apagada entera sin que ningún log lo dijera."""
    decision = s.decidir(
        fila_de_reactivacion(aplazado_desde=None),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert decision.accion == "enviar"


def test_r0_no_alcanza_a_un_recordatorio_de_cita():
    """La frontera de siempre: un `recordatorio_cita` no evalúa R1-R5 y no necesita ninguna de
    esas columnas, así que una fila de cita sin ellas tiene que salir igual. Si R0 la tocara,
    un cambio en el SELECT dejaría sin aviso a pacientes con cita real."""
    fila = {
        "id": 7,
        "conversacion_id": "conv-1",
        "cita_id": "cita-1",
        "tipo": s.TIPO_RECORDATORIO,
        "fecha_objetivo": momento(16, 11),
        "telefono": "573001112233",
        "nombre_completo": "Ana Gómez",
        "cita_inicio": momento(17, 9),
        "cita_estado": "confirmada",
        "tomada_por": None,
        "no_contactar": False,
    }
    decision = s.decidir(fila, ahora=momento(16, 11), jornada=JORNADA, ultimo_mensaje=None)
    assert decision.accion == "enviar"


# -- Los literales de tipo del SQL, atados al vocabulario ------------------------------------


def test_los_literales_de_tipo_del_sql_no_se_separan_del_vocabulario_del_barrido():
    """El día que la fase 8 encienda `reactivacion_no_asistio` -- «cambiar una constante», dice
    el diseño -- cinco cadenas SQL no se enterarían, y ninguna prueba lo notaría.

    Consecuencia medida sobre el código: la condición 5 de las dos consultas de cartera
    (`s.tipo IN (...)`, el "en juego" que impide dos discursos distintos en la misma ventana)
    no vería el tipo nuevo, y `envios_por_contabilizar` tampoco. Una persona podría recibir un
    `no_asistio` **y** un `sin_agendar` a la vez, con el contador sin subir por ninguno de los
    `no_asistio`. Eso es mandar de MÁS, y el mecanismo de disparo es «alguien hace exactamente
    lo que el diseño le dijo que hiciera».

    **Es una prueba y NO un refactor**, a propósito: la flecha de imports va de
    `seguimientos.py` hacia `persistencia.py` y nunca al revés, así que esos literales no se
    pueden importar sin crear un ciclo. Lo que se puede hacer es que separarlos falle aquí.

    Dos formas distintas de nombrar el tipo, y las dos tienen que seguir el vocabulario:

    - `tipo IN (...)` -- la lista completa. Tiene que ser IGUAL al conjunto, en las dos
      direcciones: ni de menos (el tipo nuevo no se ve) ni de más (un literal que el código no
      reconoce).
    - `tipo = '<uno>'` -- las condiciones 6 y 7, que son POR TIPO a propósito. Cada tipo del
      vocabulario necesita las suyas, y por eso encender un tipo nuevo no es solo tocar la
      constante: hace falta su consulta de cartera.
    """
    import inspect
    import re

    from maxicare_daniela import persistencia as p

    cartera = p._LEADS_SIN_AGENDAR + p._LEADS_QUE_CANCELARON
    con_lista = {
        "_LEADS_SIN_AGENDAR": p._LEADS_SIN_AGENDAR,
        "_LEADS_QUE_CANCELARON": p._LEADS_QUE_CANCELARON,
        "envios_por_contabilizar": inspect.getsource(p.envios_por_contabilizar),
    }

    encontradas = 0
    for nombre, sql in con_lista.items():
        # El `if "'" in lista` descarta la CITA del patrón que hay en el docstring de
        # `envios_por_contabilizar` (`tipo IN (...)`, explicando por qué los literales van a
        # mano). No debilita nada: `tipo IN ()` sin un solo literal no es SQL válido, así que
        # lo que se descarta no puede ser una lista de verdad.
        listas = [l for l in re.findall(r"tipo IN \(([^)]*)\)", sql) if "'" in l]
        assert listas, f"{nombre} ya no tiene ninguna lista `tipo IN (...)`"
        for lista in listas:
            encontradas += 1
            del_sql = set(re.findall(r"'(\w+)'", lista))
            assert del_sql == set(s.TIPOS_QUE_EL_BARRIDO_ENCOLA), (
                f"la lista `tipo IN (...)` de {nombre} se separó del vocabulario del barrido "
                f"-- sql={sorted(del_sql)} codigo={sorted(s.TIPOS_QUE_EL_BARRIDO_ENCOLA)}"
            )
    assert encontradas == 3, f"se esperaban tres listas `tipo IN (...)`, se vieron {encontradas}"

    for tipo in s.TIPOS_QUE_EL_BARRIDO_ENCOLA:
        # La condición 6 (intentos por serie) y la 7 (el «no» del paciente), una cada una.
        assert cartera.count(f"= '{tipo}'") >= 2, (
            f"'{tipo}' está en TIPOS_QUE_EL_BARRIDO_ENCOLA pero las consultas de cartera no "
            "traen sus dos condiciones por tipo (la 6 y la 7): encolarlo lo dejaría sin el "
            "freno de intentos y sin el bloqueo permanente del «no» del paciente"
        )


def test_el_motivo_del_no_del_paciente_es_una_sola_constante():
    """`el_paciente_dijo_que_no` vivía como literal suelto en cuatro sitios. La condición 7
    busca por ese motivo EXACTO: si uno se separa de los otros, el bloqueo permanente deja de
    encontrarse y nada falla."""
    from maxicare_daniela import persistencia as p

    assert p.MOTIVO_NEGATIVA_DEL_PACIENTE == "el_paciente_dijo_que_no"
    for nombre, sql in (
        ("_LEADS_SIN_AGENDAR", p._LEADS_SIN_AGENDAR),
        ("_LEADS_QUE_CANCELARON", p._LEADS_QUE_CANCELARON),
    ):
        assert f"motivo_anulacion = '{p.MOTIVO_NEGATIVA_DEL_PACIENTE}'" in sql, nombre


# -- H2 y H3: el freno llega hasta donde se MANDA, no solo hasta donde se encola --------------


def test_el_freno_aplaza_la_reactivacion_y_nunca_la_anula_ni_la_marca():
    """H2/H3. El interruptor de pánico y el freno por calidad vivían SOLO en `barrido.encolar`,
    que corre una vez por hora. `despachar` corre cada sesenta segundos, es quien de verdad
    manda, y no miraba ninguno: quien accionaba el freno de emergencia veía salir en el minuto
    siguiente todo lo que ya estaba en la cola -- hasta el tope diario, más lo aplazado de días
    anteriores.

    **Aplaza, nunca anula y nunca marca.** Anular perdería filas que sí calificaban por un
    motivo transitorio; marcar sería peor, porque `marcar_seguimiento_enviado` va ANTES del
    envío (no negociable 21) y marcar sin mandar pierde la fila para siempre.
    """
    import asyncio

    marcadas, anuladas, aplazadas = [], [], []
    whatsapp = _WhatsAppQueCuenta()
    recuento = asyncio.run(
        _despachar_con(
            [fila_de_reactivacion(id=1)],
            whatsapp=whatsapp,
            plantillas={s.TIPO_SIN_AGENDAR: "plantilla_real"},
            ahora=momento(16, 11),
            marcadas=marcadas,
            anuladas=anuladas,
            aplazadas=aplazadas,
            freno_de_reactivacion="interruptor_de_panico",
        )
    )
    assert recuento["aplazados"] == 1
    assert recuento["enviados"] == 0
    assert not whatsapp.llamadas, "salió una reactivación con el freno de emergencia puesto"
    assert not marcadas, "una fila frenada se marcó como enviada: se pierde para siempre"
    assert not anuladas, "una fila frenada se anuló: el freno es transitorio, no permanente"
    assert aplazadas and aplazadas[0][0] == 1


def test_el_freno_NO_alcanza_al_recordatorio_de_una_cita():
    """La frontera, y es el no negociable 25: la baja -- y todo freno comercial -- es comercial
    y no apaga el aviso de una cita real. Si el freno alcanzara a `recordatorio_cita`, apagar
    la publicidad dejaría a pacientes con hora sin su recordatorio."""
    import asyncio

    whatsapp = _WhatsAppQueCuenta()
    fila = fila_de_reactivacion(
        id=2,
        tipo=s.TIPO_RECORDATORIO,
        cita_id="cita-1",
        cita_inicio=momento(17, 9),
        cita_estado="confirmada",
        nombre_completo="Ana Gómez",
    )
    recuento = asyncio.run(
        _despachar_con(
            [fila],
            whatsapp=whatsapp,
            plantillas={s.TIPO_RECORDATORIO: "recordatorio"},
            ahora=momento(16, 11),
            freno_de_reactivacion="daniela_apagada",
        )
    )
    assert recuento["enviados"] == 1
    assert len(whatsapp.llamadas) == 1


@pytest.mark.parametrize(
    "apagado,esperado",
    [
        ({"reactivacion_encendida": False}, "interruptor_de_panico"),
        ({"daniela_responde": False}, "daniela_apagada"),
        ({}, "calidad_del_numero"),
    ],
)
def test_runtime_calcula_el_freno_por_las_tres_senales(monkeypatch, apagado, esperado):
    """Las tres señales que tienen que llegar al despachador, y una de ellas es la frontera
    clínica: con `MAXICARE_DANIELA_RESPONDE=0` salía un «¿sigue interesada?» y quien pulsaba
    «Sí, me interesa» no recibía NADA -- `atencion.procesar_mensaje` corta en esa bandera.
    Pedir respuesta y callarse es el disparador de reporte más limpio que existe, y si quien
    vuelve escribe «me duele», cruza la frontera clínica."""
    import dataclasses

    from maxicare_daniela import runtime

    base = dataclasses.replace(
        runtime.config, reactivacion_encendida=True, daniela_responde=True
    )
    monkeypatch.setattr(runtime, "config", dataclasses.replace(base, **apagado))
    # `None` es "todavía nadie preguntó por la calidad", y eso frena -- misma asimetría que
    # `barrido.se_puede_encolar`: ante la duda, no se manda.
    monkeypatch.setattr(runtime, "_ultima_calidad_del_numero", None)
    assert runtime._freno_de_reactivacion() == esperado


def test_runtime_deja_pasar_la_reactivacion_solo_con_la_calidad_confirmada(monkeypatch):
    """El invariante que deja el arreglo: una reactivación solo sale si alguien comprobó la
    calidad del número en la última hora. Un `RED` frena igual que un `None`."""
    import dataclasses

    from maxicare_daniela import runtime

    monkeypatch.setattr(
        runtime,
        "config",
        dataclasses.replace(runtime.config, reactivacion_encendida=True, daniela_responde=True),
    )
    monkeypatch.setattr(runtime, "_ultima_calidad_del_numero", {"quality_rating": "GREEN"})
    assert runtime._freno_de_reactivacion() is None

    monkeypatch.setattr(runtime, "_ultima_calidad_del_numero", {"quality_rating": "RED"})
    assert runtime._freno_de_reactivacion() == "calidad_del_numero"


def test_el_freno_va_despues_de_las_guardas_que_anulan_por_razones_permanentes():
    """El orden importa, y no es cosmético.

    R1, R2, R3 y R3bis anulan por razones que siguen siendo ciertas con el freno puesto (el
    contador, el tope anual, el retraso, el atasco). Dejarlas correr es lo que impide que un
    freno largo apile un backlog que salga de golpe el día que se levante -- el pico exacto que
    R3 existe para evitar. Con R3bis viva, un freno de más de 96 h va matando las filas en vez
    de acumularlas.
    """
    frenada_y_apagada = s.decidir(
        fila_de_reactivacion(seguimientos_fallidos=9),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
        freno_de_reactivacion="interruptor_de_panico",
    )
    assert frenada_y_apagada.accion == "anular"
    assert frenada_y_apagada.motivo == "seguimiento_apagado"

    frenada_y_estancada = s.decidir(
        fila_de_reactivacion(aplazado_desde=momento(1, 11)),
        ahora=momento(16, 11),
        jornada=JORNADA,
        ultimo_mensaje=None,
        freno_de_reactivacion="calidad_del_numero",
    )
    assert frenada_y_estancada.motivo == "reactivacion_estancada"


# -- H7: el nombre del recordatorio también se parte por cualquier espacio -------------------


def test_el_recordatorio_de_cita_no_manda_un_nombre_con_salto_de_linea():
    """H7, frontera clínica. El razonamiento que la ronda 2 escribió para la reactivación
    aplica palabra por palabra aquí: Meta RECHAZA un parámetro de plantilla con saltos de línea
    (132007), y como la fila se marca ANTES de enviar (no negociable 21) el rechazo la pierde
    para siempre tras los tres intentos. Lo que se pierde no es publicidad: es el aviso de una
    cita real.

    `citas.nombre_completo` lo escribe `registrar_cita` con lo que el modelo capturó y SIN
    recortar, así que `"Ana\\nPérez"` es alcanzable. `.split(" ")` no corta por `\\n`.
    """
    parametros = s.parametros_de(
        {
            "tipo": s.TIPO_RECORDATORIO,
            "nombre_completo": "Ana\nPérez",
            "cita_inicio": momento(17, 9),
            "tratamiento": "limpieza",
        }
    )
    assert parametros[0] == "Ana"
    for hueco in parametros:
        assert "\n" not in hueco and "\r" not in hueco and "\t" not in hueco


def test_el_recordatorio_de_cita_con_un_nombre_que_empieza_por_espacio_no_manda_vacio():
    """El segundo residuo del mismo hallazgo: `" Ana Pérez"` dejaba `.split(" ")[0]` en una
    cadena VACÍA. Ni `asegurar_paciente` ni `registrar_cita` recortan lo que llega."""
    parametros = s.parametros_de(
        {
            "tipo": s.TIPO_RECORDATORIO,
            "nombre_completo": "  Ana Pérez",
            "cita_inicio": momento(17, 9),
            "tratamiento": "limpieza",
        }
    )
    assert parametros[0] == "Ana"


def test_el_recordatorio_de_cita_sin_nombre_conserva_su_respaldo():
    """Aquí SÍ se conserva `"paciente"`, al revés que en la rama de reactivación: el paciente
    tiene hora de verdad y el aviso tiene que salir aunque no haya nombre."""
    parametros = s.parametros_de(
        {
            "tipo": s.TIPO_RECORDATORIO,
            "nombre_completo": None,
            "cita_inicio": momento(17, 9),
            "tratamiento": "limpieza",
        }
    )
    assert parametros[0] == "paciente"


# -- H8: una fila que revienta no se lleva la tanda ------------------------------------------


def test_una_fila_que_revienta_no_se_lleva_los_recordatorios_de_las_demas():
    """H8, y el lado malo es clínico. Una excepción dentro del `for fila in filas:` salía de
    `despachar` y se comía el lote ENTERO -- incluidos los `recordatorio_cita` de las otras 49
    filas. Con `ORDER BY s.fecha_objetivo`, una fila que reventara de forma determinista
    estaría siempre a la cabeza y bloquearía la cola indefinidamente."""
    import asyncio

    rota = fila_de_reactivacion(id=1)
    rota["fecha_objetivo"] = "esto no es un datetime"  # revienta en R3, dentro de `decidir`
    buena = fila_de_reactivacion(
        id=2,
        tipo=s.TIPO_RECORDATORIO,
        cita_id="cita-1",
        cita_inicio=momento(17, 9),
        cita_estado="confirmada",
        nombre_completo="Ana Gómez",
        telefono="573009998877",
    )

    whatsapp = _WhatsAppQueCuenta()
    recuento = asyncio.run(
        _despachar_con(
            [rota, buena],
            whatsapp=whatsapp,
            plantillas={s.TIPO_RECORDATORIO: "recordatorio"},
            ahora=momento(16, 11),
        )
    )
    assert recuento["enviados"] == 1, "la fila rota se llevó por delante el recordatorio de cita"
    assert recuento["fallidos"] == 1, "la fila rota tiene que contarse, no desaparecer callada"
    assert len(whatsapp.llamadas) == 1


# -- La cota superior de `programar_seguimiento` ---------------------------------------------


def test_el_modelo_no_puede_programar_un_seguimiento_a_seis_meses():
    """No había ninguna cota superior: `_a_fecha` solo exigía que la fecha fuera futura.

    Dos cosas van mal y ninguna deja rastro: lo que sale a los seis meses dice «hace unos días
    nos escribió» sobre algo que la cartera ya no considera reciente, y `contar_comprometidos_
    hoy` no cuenta una fila a semanas vista, así que no consume cupo hoy y luego aparece fuera
    de todo ritmo. La cota se toma de `persistencia.DIAS_DE_VENTANA_DE_CARTERA` y no se elige
    aquí: es la misma ventana de las dos consultas de cartera.
    """
    import asyncio

    from maxicare_daniela import persistencia as p
    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto

    ctx = contexto()
    lejos = ctx.ahora + timedelta(days=p.DIAS_DE_VENTANA_DE_CARTERA + 1)
    respuesta = asyncio.run(
        _programar_seguimiento(ctx, s.TIPO_SIN_AGENDAR, lejos.isoformat())
    )
    assert "programado" not in respuesta.lower()
    assert str(p.DIAS_DE_VENTANA_DE_CARTERA) in respuesta


def test_el_borde_de_la_cota_de_treinta_dias_sigue_dentro():
    """La cota es `<=`, no `<`: justo a 30 días sí se puede programar. Sin este caso, apretar
    la cota por error a `<` pasaría en silencio y se perdería el día 30 entero."""
    import asyncio

    from maxicare_daniela import persistencia as p
    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto

    programados = {}

    ctx = contexto()

    async def _con_base_falsa(_ctx, trabajo):
        programados["llamado"] = True
        return (None, True)

    justo = ctx.ahora + timedelta(days=p.DIAS_DE_VENTANA_DE_CARTERA)
    with pytest.MonkeyPatch().context() as mp:
        from maxicare_daniela import herramientas as h

        mp.setattr(h, "_con_base", _con_base_falsa)
        respuesta = asyncio.run(
            _programar_seguimiento(ctx, s.TIPO_SIN_AGENDAR, justo.isoformat())
        )
    assert programados.get("llamado"), "la cota se comió el día 30, que sí debe entrar"
    assert "programado" in respuesta.lower()


class _WhatsAppQueCuenta:
    """Un WhatsApp doblado que solo cuenta. No revienta: varias de las pruebas de arriba
    esperan que SÍ salga algo (el recordatorio de cita con el freno puesto)."""

    def __init__(self) -> None:
        self.llamadas: list[dict] = []

    async def enviar_plantilla(self, telefono, *, plantilla, parametros, idioma="es"):
        self.llamadas.append(
            {"telefono": telefono, "plantilla": plantilla, "parametros": parametros}
        )
        return "wamid.doblado"


# ---------------------------------------------------------------------------
# El CABLE del freno de emergencia, no la guarda.
#
# La guarda FRENO de `decidir` estaba sujeta por pruebas; lo que NO lo estaba era la
# unica linea que la alimenta (`freno_de_reactivacion=_freno_de_reactivacion()` en el
# despachador de `runtime`). El revisor del arreglo final borro esa linea y volvio a
# correrlo todo: `pytest -q` entero en verde Y `probar_reactivacion.py` certificando
# "el freno llega hasta donde se MANDA". Con el cable cortado, H2 y H3 vuelven enteros
# -- el interruptor de panico y el freno por calidad dejan de parar los ENVIOS-- y nada
# lo dice. Es la sexta vez en esta rama que un fallo tiene esta forma: el codigo dice una
# cosa y hace otra, en silencio.
#
# Por eso esta prueba NO mira la guarda: mira que el VALOR VIAJA desde `runtime` hasta
# `seguimientos.despachar`.
# ---------------------------------------------------------------------------


def test_el_freno_de_emergencia_VIAJA_de_runtime_al_despachador():
    """Corre un ciclo real de `_despachar_recordatorios_sin_parar` y exige el kwarg."""
    import asyncio

    from maxicare_daniela import runtime as rt

    recibido: dict = {}

    class _Alto(Exception):
        """Corta el `while True` en el segundo sueño, no en el primero."""

    async def _sleep_que_corta(_segundos):
        if recibido.get("despachado"):
            raise _Alto
        return None

    async def _despachar_doblado(**kwargs):
        recibido["despachado"] = True
        recibido["kwargs"] = kwargs
        return {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 0}

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(asyncio, "sleep", _sleep_que_corta)
        mp.setattr(rt.seguimientos, "despachar", _despachar_doblado)
        mp.setattr(rt, "_leer_configuracion_operativa", lambda: {})
        # El freno DICE que hay que frenar; si el cable esta cortado, llega `None`.
        mp.setattr(rt, "_freno_de_reactivacion", lambda: "interruptor_de_panico")
        with pytest.raises(_Alto):
            asyncio.run(rt._despachar_recordatorios_sin_parar())

    assert recibido.get("despachado"), "el ciclo no llego a llamar a `despachar`"
    assert "freno_de_reactivacion" in recibido["kwargs"], (
        "el despachador de `runtime` no le pasa `freno_de_reactivacion` a "
        "`seguimientos.despachar`: el freno de emergencia no llega a donde se MANDA"
    )
    assert recibido["kwargs"]["freno_de_reactivacion"] == "interruptor_de_panico", (
        "el valor no viaja: `despachar` recibe algo distinto de lo que dijo "
        "`_freno_de_reactivacion`"
    )


def test_la_polaridad_del_FRENO_falla_cerrado_con_un_tipo_desconocido():
    """Un `tipo` que nadie reconoce tiene que FRENARSE, no salir.

    El primer fallo grave de esta rama fue una guarda con la polaridad al reves que
    fallaba ABIERTO. El FRENO es la unica de las trece guardas cuya polaridad no sujetaba
    ninguna prueba: mutar `es_reactivacion` (`not in TIPOS_NO_COMERCIALES`) por el literal
    `tipo in TIPOS_DE_REACTIVACION` dejaba la suite entera en verde -- y con esa mutacion
    un `tipo` desconocido sale CON EL FRENO DE EMERGENCIA PUESTO. Es alcanzable: el CHECK
    de la 021 es NOT VALID.

    La frontera, en la misma prueba: `recordatorio_cita` SI sale con el freno puesto. Eso
    es el no negociable 25 -- la baja es comercial y no apaga el aviso de una cita.
    """
    ahora = momento(15, 11)  # martes, 11 de la manana: hora habil, nada mas frena

    def _decidir(tipo):
        return s.decidir(
            {
                "id": 1,
                "tipo": tipo,
                "fecha_objetivo": ahora - timedelta(minutes=5),
                "cita_id": None,
                "seguimientos_fallidos": 0,
                "reactivaciones_ultimo_ano": 0,
                "aplazado_desde": None,
                "no_contactar": False,
            },
            ahora=ahora,
            jornada=JORNADA,
            ultimo_mensaje=None,
            ya_salio_a_ese_numero=False,
            freno_de_reactivacion="interruptor_de_panico",
        )

    for tipo in ("un_tipo_que_nadie_ha_escrito_todavia", "", None):
        decision = _decidir(tipo)
        assert decision.accion == "aplazar", (
            f"un tipo desconocido ({tipo!r}) sale con el freno de emergencia puesto: "
            "la polaridad del FRENO falla ABIERTO"
        )
        assert decision.motivo.startswith("frenada:"), decision.motivo

    # Y la frontera: el recordatorio de una cita real sigue saliendo.
    assert _decidir(s.TIPO_RECORDATORIO).accion != "aplazar" or not _decidir(
        s.TIPO_RECORDATORIO
    ).motivo.startswith("frenada:"), (
        "el freno comercial esta apagando el aviso de una cita real (no negociable 25)"
    )

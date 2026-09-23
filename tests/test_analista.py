"""El analista: el modelo nano que le escribe tres frases a cada caso.

Offline. El modelo se dobla; lo que se prueba es la forma del contrato y la prohibicion
dura, no lo que un modelo real conteste.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maxicare_daniela import analista
from maxicare_daniela.analista import InformeDelCaso, texto_del_caso


def test_el_informe_tiene_tres_campos_con_tope():
    """La brevedad se impone por estructura, no pidiendole al modelo que sea breve."""
    campos = InformeDelCaso.model_fields

    assert set(campos) == {"que_paso", "por_que", "recomiendo"}
    for nombre, campo in campos.items():
        topes = [m for m in campo.metadata if getattr(m, "max_length", None)]
        assert topes, f"{nombre} no tiene tope de longitud y el informe puede divagar"


def test_un_informe_demasiado_largo_lo_rechaza_el_contrato():
    with pytest.raises(Exception):
        InformeDelCaso(que_paso="x" * 5000, por_que="y", recomiendo="z")


def test_el_texto_que_se_le_manda_al_modelo_lleva_los_ejemplos():
    caso = {
        "huella": "falta_dato:ortodoncia:precio", "tipo": "FALTA_DATO",
        "contador": 7, "escalo": 7,
        "primera_vez": "2026-09-15T15:42:00+00:00",
        "ultima_vez": "2026-09-27T14:20:00+00:00",
        "ejemplos": ["cuanto me sale la ortodoncia en cuotas", "brackets por mes?"],
    }

    texto = texto_del_caso(caso)

    assert "ortodoncia" in texto
    assert "7" in texto
    assert "cuanto me sale la ortodoncia en cuotas" in texto


def test_las_instrucciones_prohiben_proponer_cifras():
    """La prohibicion dura del spec 7.3.

    Si el informe pudiera proponer contenido clinico, alguien lo aprobaria de un clic y
    habriamos metido una cifra alucinada a la base de conocimiento por la puerta de atras --
    justo lo que `sin_cifra_no_documentada` existe para impedir.
    """
    from maxicare_daniela.analista import _analista

    instrucciones = _analista.instructions.lower()

    assert "prohibido" in instrucciones
    assert "cifra" in instrucciones or "precio" in instrucciones


# ==========================================================================================
# `analizar_pendientes`: la funcion que hace el trabajo
#
# Se dobla TODO lo que sale del proceso --la conexion, las tres funciones de `persistencia` y
# `Runner.run`--, asi que no gasta un token ni toca la base. Lo que se prueba es el bucle: el
# `continue` que falla abierto, el tope de reintentos, el control de cifras, el `sobre` y el
# cierre de la conexion.
# ==========================================================================================


class _ConexionFalsa:
    def __init__(self) -> None:
        self.cerrada = False

    def close(self) -> None:
        self.cerrada = True


def _informe(que_paso="paso 7 veces", por_que="no hay ficha", recomiendo="crearla"):
    return InformeDelCaso(que_paso=que_paso, por_que=por_que, recomiendo=recomiendo)


def _caso(huella, contador=7):
    return {
        "huella": huella, "tipo": "FALTA_DATO", "contador": contador, "escalo": 0,
        "primera_vez": "2026-09-15T15:42:00+00:00",
        "ultima_vez": "2026-09-15T16:42:00+00:00",
        "ejemplos": ["cuanto vale"],
    }


def _montar(monkeypatch, *, casos, responder):
    """Deja `analizar_pendientes` corriendo entero sin base ni modelo.

    `responder(caso)` devuelve un `InformeDelCaso` o lanza. Devuelve la conexion falsa y la
    lista de lo que se guardo.
    """
    from maxicare_daniela import analista, persistencia

    conn = _ConexionFalsa()
    guardados: list[dict] = []
    pedidos: list[int] = []

    class _RunnerFalso:
        @staticmethod
        async def run(agente, entrada, **kw):
            # Se identifica el caso por la entrada ENTERA y no rascando una linea concreta.
            # Rascaba `huella: `, y el dia que `texto_del_caso` dejo de escribir esa linea
            # --22/09/2026, cuando la huella paso a ir repartida en sus partes-- estas seis
            # pruebas se cayeron a la vez por un cambio que no las afectaba. Comparar la
            # entrada completa no se puede quedar viejo: o es la de ese caso, o no lo es.
            caso = next(c for c in casos if analista.texto_del_caso(c) == entrada)
            return SimpleNamespace(final_output=responder(caso))

    def _sin_informe(c, *, limite, **kw):
        pedidos.append(limite)
        return list(casos)

    monkeypatch.setattr(analista, "_fallos", {})
    monkeypatch.setattr(analista, "Runner", _RunnerFalso)
    monkeypatch.setattr(persistencia, "conectar", lambda url: conn)
    monkeypatch.setattr(persistencia, "casos_sin_informe", _sin_informe)
    monkeypatch.setattr(
        persistencia, "guardar_informe",
        lambda c, *, huella, informe, sobre: guardados.append(
            {"huella": huella, "informe": informe, "sobre": sobre}
        ),
    )
    return conn, guardados, pedidos


def test_analizar_pendientes_escribe_un_informe_por_caso_y_cierra_la_conexion(monkeypatch):
    from maxicare_daniela import analista

    casos = [_caso("falta_dato:ortodoncia:precio", 7), _caso("humano:urgencia", 3)]
    conn, guardados, _ = _montar(monkeypatch, casos=casos, responder=lambda c: _informe())

    escritos = asyncio.run(analista.analizar_pendientes(database_url="postgresql://x"))

    assert escritos == 2
    assert [g["huella"] for g in guardados] == [c["huella"] for c in casos]
    # `sobre` es el contador del caso, no el del vecino: es lo que decide cuando se
    # re-analiza («crecio por cinco»).
    assert [g["sobre"] for g in guardados] == [7, 3]
    assert conn.cerrada, "la conexion del ciclo se cierra siempre"


def test_un_caso_que_falla_no_se_lleva_a_los_demas(monkeypatch):
    """Falla ABIERTO: un caso sin informe se muestra igual y el resto del ciclo sigue."""
    from maxicare_daniela import analista

    casos = [_caso("roto:timeouterror"), _caso("falta_dato:ortodoncia:precio")]

    def _responder(caso):
        if caso["huella"].startswith("roto:"):
            raise RuntimeError("el modelo no contesto")
        return _informe()

    conn, guardados, _ = _montar(monkeypatch, casos=casos, responder=_responder)

    escritos = asyncio.run(analista.analizar_pendientes(database_url="postgresql://x"))

    assert escritos == 1
    assert [g["huella"] for g in guardados] == ["falta_dato:ortodoncia:precio"]
    assert conn.cerrada


def test_un_caso_que_falla_SIEMPRE_deja_de_pedirse_y_no_bloquea_la_cola(monkeypatch):
    """El atasco. `casos_sin_informe` ordena por contador y corta en `limite`: cinco casos
    que fallan de forma reproducible se quedan con los cinco cupos y ningun caso nuevo se
    analiza JAMAS, cada cinco minutos, para siempre. Y como fallan, `escritos` es 0 y el
    `log.info` de `runtime` no imprime nada: el fallo es silencioso justo en la metrica que
    uno miraria."""
    from maxicare_daniela import analista

    # Cinco atascados con el contador mas alto, uno sano detras.
    casos = [_caso(f"roto:atascado{i}", 100 - i) for i in range(5)]
    casos.append(_caso("falta_dato:ortodoncia:precio", 7))

    def _responder(caso):
        if caso["huella"].startswith("roto:"):
            raise RuntimeError("revienta siempre")
        return _informe()

    _montar(monkeypatch, casos=casos, responder=_responder)

    # Los tres primeros ciclos se gastan en los atascados; del cuarto en adelante ya no.
    for _ in range(analista.MAX_FALLOS_SEGUIDOS):
        assert asyncio.run(analista.analizar_pendientes(
            database_url="postgresql://x", limite=5
        )) == 0

    escritos = asyncio.run(analista.analizar_pendientes(database_url="postgresql://x", limite=5))

    assert escritos == 1, "el caso sano tiene que poder pasar cuando los atascados se saltan"


def test_se_piden_mas_casos_de_los_que_caben_para_poder_saltar(monkeypatch):
    """Filtrar sin pedir de mas dejaria el ciclo con menos casos de los que puede analizar."""
    from maxicare_daniela import analista

    casos = [_caso(f"falta_dato:t{i}:precio") for i in range(3)]
    _, _, pedidos = _montar(monkeypatch, casos=casos, responder=lambda c: _informe())

    asyncio.run(analista.analizar_pendientes(database_url="postgresql://x", limite=5))

    assert pedidos == [15]


def test_un_ciclo_nunca_analiza_mas_de_limite(monkeypatch):
    from maxicare_daniela import analista

    casos = [_caso(f"falta_dato:t{i}:precio") for i in range(10)]
    _, guardados, _ = _montar(monkeypatch, casos=casos, responder=lambda c: _informe())

    escritos = asyncio.run(analista.analizar_pendientes(database_url="postgresql://x", limite=2))

    assert escritos == 2 and len(guardados) == 2


def test_un_informe_con_una_cifra_NO_se_guarda(monkeypatch):
    """Control DETERMINISTA, sin una segunda llamada al modelo. Hoy lo unico que impide que
    el nano invente un precio de ortodoncia es el texto de sus instrucciones, y un informe
    con un precio dentro es exactamente lo que alguien aprobaria de un clic hacia la base de
    conocimiento -- justo lo que `sin_cifra_no_documentada` existe para impedir.

    El caso se queda con `informe IS NULL`: la pantalla lo muestra igual, con su contador y
    sus ejemplos, y el ciclo lo reintenta."""
    from maxicare_daniela import analista

    casos = [_caso("falta_dato:ortodoncia:precio")]
    _, guardados, _ = _montar(
        monkeypatch, casos=casos,
        responder=lambda c: _informe(recomiendo="cargar la ficha: son $3.000.000"),
    )

    escritos = asyncio.run(analista.analizar_pendientes(database_url="postgresql://x"))

    assert escritos == 0
    assert guardados == [], "un informe con un precio dentro no entra a la base"


def test_los_contadores_del_informe_NO_son_cifras(monkeypatch):
    """`cifras_de` solo caza cifras con forma de DINERO. Si tambien cazara los contadores, el
    control rechazaria todos los informes legitimos -- que hablan de veces y de dias."""
    from maxicare_daniela import analista

    casos = [_caso("falta_dato:ortodoncia:precio")]
    _, guardados, _ = _montar(
        monkeypatch, casos=casos,
        responder=lambda c: _informe(
            que_paso="12 pacientes preguntaron en 7 dias",
            por_que="se molesto al doctor 4 de 12 veces",
            recomiendo="cargar la ficha de la cuota mensual",
        ),
    )

    escritos = asyncio.run(analista.analizar_pendientes(database_url="postgresql://x"))

    assert escritos == 1 and len(guardados) == 1


def test_un_informe_que_se_guarda_limpia_los_fallos_anteriores(monkeypatch):
    """El contador es de fallos CONSECUTIVOS: dos timeouts seguidos de un exito no pueden
    dejar la huella a un paso del tope para siempre."""
    from maxicare_daniela import analista

    casos = [_caso("falta_dato:ortodoncia:precio")]
    _montar(monkeypatch, casos=casos, responder=lambda c: _informe())
    analista._fallos["falta_dato:ortodoncia:precio"] = analista.MAX_FALLOS_SEGUIDOS - 1

    asyncio.run(analista.analizar_pendientes(database_url="postgresql://x"))

    assert "falta_dato:ortodoncia:precio" not in analista._fallos


def test_MAXICARE_ANALIZAR_SIN_RESOLVER_en_0_no_arranca_la_tarea(monkeypatch):
    """El interruptor de `config.analizar_sin_resolver`, igual que
    `test_el_despachador_arranca_aunque_no_haya_telegram` para los recordatorios: si alguien
    invierte o borra el `if` de `_arrancar_analisis`, la suite offline tiene que caer aquí, no
    quedarse en verde mientras la clinica paga llamadas al modelo que creia apagadas.
    """
    from maxicare_daniela import runtime

    async def escenario():
        monkeypatch.setattr(
            runtime,
            "config",
            replace(
                runtime.config,
                database_url="postgresql://no-se-usa",
                analizar_sin_resolver=False,
            ),
        )
        monkeypatch.setattr(runtime, "_tarea_de_analisis", None)

        await runtime._arrancar_analisis()

        assert runtime._tarea_de_analisis is None, (
            "MAXICARE_ANALIZAR_SIN_RESOLVER=0 y la tarea arrancó igual"
        )

    asyncio.run(escenario())


def test_MAXICARE_ANALIZAR_SIN_RESOLVER_en_1_si_arranca_la_tarea(monkeypatch):
    """La otra dirección: con el interruptor encendido y base configurada, la tarea sí nace."""
    from maxicare_daniela import runtime

    async def escenario():
        monkeypatch.setattr(
            runtime,
            "config",
            replace(
                runtime.config,
                database_url="postgresql://no-se-usa",
                analizar_sin_resolver=True,
            ),
        )
        monkeypatch.setattr(runtime, "_tarea_de_analisis", None)

        await runtime._arrancar_analisis()

        tarea = runtime._tarea_de_analisis
        assert tarea is not None, "el interruptor estaba encendido y la tarea no arrancó"
        assert not tarea.done()
        # Lo primero que hace el bucle es dormir el ciclo entero: cancelarlo aquí no
        # interrumpe ningún análisis a medias y evita dejar una tarea viva entre pruebas.
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass

    asyncio.run(escenario())


# ==========================================================================================
# Lo que el analista VE, que es la mitad del arreglo del 22/09/2026
#
# MaxiCare pidio informes especificos --en que paso se escalo, cual fue la causa, que hacer--
# y el prompt solo es la mitad: el modelo no podia ser especifico porque no recibia con que.
# Estas pruebas fijan lo que ahora si recibe. Ninguna llama al modelo.
# ==========================================================================================


def test_la_entrada_lleva_la_MECANICA_de_la_clase_y_no_solo_su_etiqueta():
    """Sin esto el modelo no tiene forma de saber en que paso se paso la conversacion a una
    persona: esa informacion no esta en la huella, esta en el codigo que produjo el caso."""
    texto = analista.texto_del_caso(_caso("falta_dato:ortodoncia:precio"))
    assert "FALTA_DATO" in texto
    assert "SIN DATO DOCUMENTADO" in texto, "la mecanica de la clase tiene que ir dentro"
    assert analista.MECANICA["FALTA_DATO"] in texto


def test_la_entrada_reparte_la_huella_en_sus_partes():
    """`falta_dato:ortodoncia:precio` cruda produce «falta un dato de ortodoncia». Repartida
    produce «el precio de la ortodoncia», que es lo que se puede accionar."""
    texto = analista.texto_del_caso(_caso("falta_dato:ortodoncia:precio"))
    assert "tratamiento por el que se pregunto: «ortodoncia»" in texto
    assert "dato concreto que no estaba cargado: «precio»" in texto


def test_las_cuatro_clases_de_huella_se_reparten_y_ninguna_se_queda_cruda():
    """El reparto cubre las cuatro que `sin_resolver.py` sabe construir. Una clase nueva cae
    en la rama de respaldo, que dice el identificador entero en vez de callarse."""
    casos = {
        "falta_dato:_general:formas de pago": "un dato general de la clinica",
        "guardrail:uso_indebido:_general": "ningun tratamiento en concreto",
        "roto:TimeoutError": "clase de la averia: TimeoutError",
        "humano:clinico": "sub-motivo por el que se paso a una persona: clinico",
    }
    for huella, esperado in casos.items():
        assert esperado in analista._partes_de_la_huella(huella), huella

    inventada = analista._partes_de_la_huella("clase_que_no_existe:algo")
    assert "clase_que_no_existe:algo" in inventada, "una clase nueva no puede quedar muda"


def test_la_entrada_dice_la_PROPORCION_de_interrupciones_y_no_solo_el_numero():
    """«se interrumpio a un doctor 2 de esas veces» y «todas las veces» son dos hechos de
    negocio distintos, y el segundo es el que decide si urge. Un `escalo: 2` suelto obliga al
    modelo a hacer la cuenta, y la hacia mal."""
    assert "no se interrumpio a ningun doctor" in analista.texto_del_caso(
        _caso("falta_dato:x:y") | {"escalo": 0, "contador": 4}
    )
    assert "todas las veces" in analista.texto_del_caso(
        _caso("falta_dato:x:y") | {"escalo": 4, "contador": 4}
    )
    assert "2 de esas veces" in analista.texto_del_caso(
        _caso("falta_dato:x:y") | {"escalo": 2, "contador": 4}
    )


def test_la_entrada_no_lleva_ni_un_telefono():
    """Los ejemplos llegan ya sin telefono desde `casos_sin_informe`, pero esta entrada viaja
    a OpenAI y conviene que la prueba lo diga: es el mismo criterio que puso `sin_telefonos`.
    """
    caso = _caso("falta_dato:ortodoncia:precio")
    caso["ejemplos"] = ["cuanto vale la ortodoncia"]
    assert "57" not in analista.texto_del_caso(caso).replace("2026", "")


# ==========================================================================================
# Lo que se le PIDE al analista
#
# Son pruebas sobre el texto del prompt, como `test_el_protocolo_de_alarma_sale_de_LA_BASE`.
# No demuestran que el modelo obedezca --eso solo se ve contra la API real-- sino que la
# instruccion sigue escrita. Lo que protegen es que nadie la borre al reordenar el prompt.
# ==========================================================================================


def _instrucciones() -> str:
    return analista._analista.instructions


def test_el_prompt_exige_separar_lo_comprobado_de_la_hipotesis():
    """Es la peticion textual de MaxiCare: «Distingue una causa comprobada de una hipotesis».
    Sin la etiqueta literal, las dos se leen igual y la clinica no sabe de cual fiarse."""
    texto = _instrucciones()
    assert "COMPROBADO" in texto
    assert "Hipotesis:" in texto, "la marca tiene que ser literal, o no se distingue nada"


def test_el_prompt_prohibe_inventarle_una_reaccion_al_paciente():
    """«No atribuyas molestia al paciente ni otras reacciones sin evidencia». El analista ve
    cinco frases sueltas, no la conversacion: cualquier estado de animo que escriba es
    inventado salvo que este en una de esas frases."""
    texto = _instrucciones()
    assert "PROHIBIDO atribuirle al paciente una reaccion" in texto
    assert "molestia" in texto


def test_el_prompt_dice_lo_que_el_analista_NO_ve():
    """Sin esta lista el modelo escribia como si hubiera leido la conversacion entera."""
    assert analista.SIN_EVIDENCIA in _instrucciones()
    assert "si acabo agendando" in analista.SIN_EVIDENCIA


def test_la_recomendacion_tiene_que_ser_una_OPCION_con_su_alternativa():
    """«Las recomendaciones deben presentarse como opciones; no deben modificar
    automaticamente las reglas del negocio». Y la alternativa --dejarlo como esta-- es parte
    del encargo, no un adorno: hay datos que la clinica prefiere que confirme una persona."""
    texto = _instrucciones()
    assert "DEJARLO COMO ESTA" in texto
    assert "No ordenas ni das por hecho" in texto


def test_el_prompt_sigue_prohibiendo_proponer_el_contenido_que_falta():
    """La prohibicion mas vieja del modulo, y la que no se puede perder al reescribirlo: un
    informe con un precio dentro es lo que alguien aprobaria de un clic hacia la base de
    conocimiento."""
    texto = _instrucciones()
    assert "PROHIBIDO, sin excepcion" in texto
    assert "no puedes decir cual es ese precio" in texto


def test_la_recomendacion_cabe_entera_y_el_que_paso_sigue_acotado():
    """Los topes subieron para que quepan las tres partes de la recomendacion, no para dejar
    divagar: `que_paso` sigue siendo corto a proposito."""
    campos = InformeDelCaso.model_fields
    assert campos["recomiendo"].metadata[0].max_length == 700
    assert campos["que_paso"].metadata[0].max_length == 420
    # Y el ejemplo que dio MaxiCare, que es el listón, cabe:
    largo = (
        "Si quieres que Daniela responda estas consultas, anade al sistema las aseguradoras "
        "aceptadas por cada doctor y las condiciones aplicables. Asi podra contestar futuras "
        "preguntas sobre cobertura sin escalar por falta de ese dato. Si prefieres que una "
        "persona confirme siempre esta informacion, manten el comportamiento actual e ignora "
        "esta recomendacion."
    )
    InformeDelCaso(que_paso="x", por_que="y", recomiendo=largo)

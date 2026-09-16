"""El analista: el modelo nano que le escribe tres frases a cada caso.

Offline. El modelo se dobla; lo que se prueba es la forma del contrato y la prohibicion
dura, no lo que un modelo real conteste.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

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
            huella = [l for l in entrada.splitlines() if l.startswith("huella: ")][0][8:]
            caso = next(c for c in casos if c["huella"] == huella)
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

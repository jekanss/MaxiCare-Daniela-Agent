"""Lo que costó cada corrida del modelo: la contabilidad que el sistema no tenía.

Offline y sin base. `persistencia.conectar` y `persistencia.anotar_consumo` van sustituidas
con `monkeypatch`; el `Usage` que se le pasa a `_cifras` es el REAL del SDK --`agents.usage.
Usage`, 0.22.2 instalada-- y no un remedo, porque lo único que este módulo hace con él es
leerle atributos: un doble con la forma que nos convenga certificaría en verde un código que
se rompe contra el objeto de verdad.

Las cuatro cosas que se sostienen aquí, y por qué ninguna es cosmética:

* **Los cacheados son un SUBCONJUNTO de la entrada, no un extra.** Así los reporta la
  Responses API. Sumarlos aparte cuenta dos veces el mismo token y da una factura inflada --y
  el número con el que se calibra un freno no puede salir de una suma mal hecha.
* **Un modelo desconocido cuesta 0 y sus tokens se anotan igual.** Perder la cifra en dólares
  es aceptable --se recalcula con el precio correcto-- y perder el rastro de que hubo consumo
  no lo es: es justo el caso de alguien que cambió el modelo por `.env` y nadie se enteró.
* **Nada de esto puede tumbar un turno.** Si la contabilidad revienta se pierde una fila,
  nunca una respuesta a un paciente. Mismo criterio que `atencion._anotar_resultado`.
* **Una corrida que no gastó nada no deja fila.** Una fila en cero ensucia el informe y hace
  que «cuántas corridas hubo» deje de significar algo.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agents.usage import Usage
from openai.types.responses.response_usage import InputTokensDetails

from maxicare_daniela import consumo, persistencia
from maxicare_daniela.config import PRECIOS_POR_MILLON

#: El modelo de Daniela. Se nombra por su clave real --y se comprueba que siga en la tabla--
#: porque los números de estas pruebas son SUS precios: si alguien renombra la clave, esto
#: tiene que decirlo en vez de caer al camino del modelo desconocido y pasar en verde
#: comprobando que un costo 0 es un costo 0.
MODELO = "gpt-5.6-terra"


def test_el_modelo_con_el_que_se_calculan_estas_pruebas_sigue_teniendo_precio():
    """El cinturón de todo el archivo. Sin esto, renombrar una clave de `PRECIOS_POR_MILLON`
    dejaría media docena de pruebas en verde midiendo la rama del modelo desconocido."""
    assert PRECIOS_POR_MILLON[MODELO] == (2.00, 0.20, 12.00)


# ==========================================================================================
# Dobles
# ==========================================================================================


def corrida(
    *, llamadas: int = 1, entrada: int = 0, cacheados: int = 0, salida: int = 0
):
    """Una corrida con el `Usage` real del SDK colgado donde lo cuelga `Runner.run`."""
    uso = Usage(
        requests=llamadas,
        input_tokens=entrada,
        # `cache_write_tokens` es obligatorio en el modelo de pydantic de la 0.22.2 aunque
        # `Usage()` lo rellene con 0: construirlo sin él revienta con un `ValidationError`.
        # Ese es justo el detalle que un doble hecho a mano no habría reproducido.
        input_tokens_details=InputTokensDetails(
            cached_tokens=cacheados, cache_write_tokens=0
        ),
        output_tokens=salida,
    )
    return SimpleNamespace(context_wrapper=SimpleNamespace(usage=uso))


class ConexionFalsa:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class BaseFalsa:
    """Recoge las filas de `consumo_modelo` que se habrían escrito."""

    def __init__(self, *, revienta: bool = False) -> None:
        self.revienta = revienta
        self.filas: list[dict] = []

    def instalar(self, monkeypatch) -> BaseFalsa:
        def conectar(_url):
            if self.revienta:
                raise RuntimeError("connection to server failed")
            return ConexionFalsa()

        monkeypatch.setattr(persistencia, "conectar", conectar)
        monkeypatch.setattr(
            persistencia, "anotar_consumo", lambda conn, **campos: self.filas.append(campos)
        )
        return self


def anotar(corrida_, **cambios) -> None:
    argumentos = dict(
        agente="daniela",
        modelo=MODELO,
        database_url="postgresql://no-se-usa/na",
        id_conversacion="conv-1",
        telefono="573001112233",
    )
    argumentos.update(cambios)
    asyncio.run(consumo.anotar(corrida_, **argumentos))


# ==========================================================================================
# El precio
# ==========================================================================================


def test_un_millon_de_tokens_de_entrada_sin_cache_cuesta_dos_dolares():
    """El precio de tabla, en la unidad en la que está escrito. Es la calibración del resto:
    si esta falla, ninguna de las de abajo significa nada."""
    assert consumo.costo_de(MODELO, entrada=1_000_000, cacheados=0, salida=0) == pytest.approx(2.00)


def test_un_millon_de_tokens_todos_cacheados_cuesta_la_decima_parte():
    """El proyecto corre con `prompt_cache_retention="24h"`, así que en una conversación larga
    la mayoría de la entrada está cacheada.

    Cobrarla a precio de entrada daría una factura inventada -- diez veces la real en este
    modelo-- y sobre esa cifra se decide un umbral de alerta.
    """
    assert consumo.costo_de(
        MODELO, entrada=1_000_000, cacheados=1_000_000, salida=0
    ) == pytest.approx(0.20)


def test_los_cacheados_son_un_SUBCONJUNTO_de_la_entrada_y_no_un_extra():
    """La prueba que impide que la factura salga inflada.

    La Responses API reporta `input_tokens` como el TOTAL y `cached_tokens` como la parte de
    ese total que vino del caché. Sumarlos por separado --`entrada * precio + cacheados *
    precio_cacheado`-- cuenta dos veces cada token cacheado.

    Con medio millón cacheado de un millón de entrada: lo correcto es 0,5M a $2 más 0,5M a
    $0,20, o sea $1,10. La suma equivocada da $2,10, casi el doble, y crece con el tamaño de
    la conversación: el error es mayor justo en las conversaciones que más importan.
    """
    correcto = consumo.costo_de(MODELO, entrada=1_000_000, cacheados=500_000, salida=0)
    inflado = (1_000_000 * 2.00 + 500_000 * 0.20) / 1_000_000

    assert correcto == pytest.approx(1.10)
    assert correcto != pytest.approx(inflado), "se están sumando aparte: la factura sale inflada"


def test_mas_cacheados_que_entrada_no_da_un_costo_negativo():
    """El `max(entrada - cacheados, 0)`.

    No debería ocurrir nunca --el subconjunto no puede ser mayor que el conjunto-- pero estos
    números salen de un proveedor externo, y un costo negativo no se nota: se RESTA del gasto
    del día y hace que el umbral de alerta no salte nunca. Un fallo que apaga una alarma es
    peor que uno que la dispara de más.
    """
    costo = consumo.costo_de(MODELO, entrada=1_000, cacheados=5_000, salida=0)

    assert costo == pytest.approx(5_000 * 0.20 / 1_000_000)
    assert costo >= 0


def test_la_salida_se_cobra_a_su_propio_precio():
    """Son tres precios y no dos. La salida de `terra` cuesta seis veces su entrada, así que
    confundir las columnas de la tupla no da un error: da una cifra plausible y equivocada."""
    assert consumo.costo_de(
        MODELO, entrada=0, cacheados=0, salida=1_000_000
    ) == pytest.approx(12.00)


def test_un_modelo_desconocido_cuesta_cero():
    """No revienta ni inventa un precio. Ver la prueba de abajo para la otra mitad, que es la
    que de verdad importa."""
    assert consumo.costo_de("gpt-inventado", entrada=1_000_000, cacheados=0, salida=1_000_000) == 0.0


def test_un_modelo_desconocido_ANOTA_SUS_TOKENS_IGUAL(monkeypatch):
    """La mitad que importa del caso desconocido, y la que no es obvia.

    Lo tentador al no tener precio es no escribir nada. Pero este es exactamente el caso de
    alguien que cambió `MAXICARE_MODELO_DANIELA` en el `.env` y nadie se enteró de que el
    sistema empezó a costar otra cosa: si además desaparece del informe, el cambio es
    invisible por partida doble.

    La cifra en dólares se recalcula después con el precio correcto; el rastro de que hubo
    consumo, si no se escribió, no se recupera.
    """
    base = BaseFalsa().instalar(monkeypatch)

    anotar(corrida(entrada=9_000, salida=500), modelo="gpt-inventado")

    assert len(base.filas) == 1
    assert base.filas[0]["tokens_entrada"] == 9_000
    assert base.filas[0]["tokens_salida"] == 500
    assert base.filas[0]["costo_usd"] == 0.0
    assert base.filas[0]["modelo"] == "gpt-inventado"


# ==========================================================================================
# `anotar`: lo que se escribe y lo que no
# ==========================================================================================


def test_una_corrida_normal_escribe_su_fila_con_el_desglose(monkeypatch):
    """El desglose por agente es lo que hace accionable el número: un pico en `lector` y uno
    en `daniela` se arreglan de forma distinta --el primero es alguien mandando archivos, el
    segundo alguien conversando-- y sumados serían indistinguibles."""
    base = BaseFalsa().instalar(monkeypatch)

    anotar(corrida(llamadas=3, entrada=10_000, cacheados=8_000, salida=1_000))

    assert len(base.filas) == 1
    fila = base.filas[0]
    assert fila["agente"] == "daniela"
    assert fila["llamadas"] == 3
    assert fila["tokens_entrada"] == 10_000
    assert fila["tokens_entrada_cacheados"] == 8_000
    assert fila["tokens_salida"] == 1_000
    assert fila["costo_usd"] == pytest.approx(
        (2_000 * 2.00 + 8_000 * 0.20 + 1_000 * 12.00) / 1_000_000
    )


@pytest.mark.parametrize(
    "vacia",
    [
        SimpleNamespace(context_wrapper=SimpleNamespace(usage=Usage())),
        SimpleNamespace(
            context_wrapper=SimpleNamespace(
                usage=Usage(requests=0, input_tokens=0, output_tokens=0)
            )
        ),
    ],
    ids=["usage_por_defecto", "todo_en_cero"],
)
def test_una_corrida_que_no_consumio_nada_no_escribe_fila(monkeypatch, vacia):
    """Una corrida servida entera por el caché de sesión, o cortada antes de empezar, no es
    un gasto.

    Una fila en cero no es inofensiva: el informe cuenta corridas, y unas cuantas en cero
    hacen que «hubo 300 corridas hoy» deje de significar lo que dice. Es el mismo criterio
    que `test_un_turno_limpio_no_escribe_nada` en el informe de casos sin resolver.
    """
    base = BaseFalsa().instalar(monkeypatch)

    anotar(vacia)

    assert base.filas == []


def test_anotar_no_propaga_aunque_la_base_falle(monkeypatch):
    """La regla del módulo entero: si la contabilidad revienta se pierde una fila, nunca una
    respuesta a un paciente.

    `conversacion.py` llama a esto SIN `try` --a propósito, y su comentario lo dice-- así que
    una excepción que saliera de aquí se llevaría por delante el turno. Un sistema que deja de
    atender porque no pudo apuntar lo que gastó es peor que uno que no apunta.
    """
    base = BaseFalsa(revienta=True).instalar(monkeypatch)

    anotar(corrida(entrada=1_000, salida=100))  # no debe lanzar

    assert base.filas == []


def test_el_agente_y_la_conversacion_viajan_tal_cual(monkeypatch):
    """`id_conversacion` y `telefono` son opcionales porque hay consumidores que no tienen
    ninguno de los dos --el analista corre de madrugada sobre el informe entero-- y el resto
    sí: sin ellos, un pico de gasto no se puede atribuir a nadie."""
    base = BaseFalsa().instalar(monkeypatch)

    anotar(
        corrida(entrada=100, salida=10),
        agente="lector",
        id_conversacion=None,
        telefono=None,
    )

    assert base.filas[0]["agente"] == "lector"
    assert base.filas[0]["id_conversacion"] is None
    assert base.filas[0]["telefono"] is None


# ==========================================================================================
# `_cifras`: leer el usage sin depender de su forma
# ==========================================================================================


@pytest.mark.parametrize(
    "sin_usage",
    [
        object(),
        SimpleNamespace(context_wrapper=None),
        SimpleNamespace(context_wrapper=SimpleNamespace(usage=None)),
    ],
    ids=["sin_context_wrapper", "wrapper_vacio", "usage_en_None"],
)
def test_una_corrida_sin_usage_devuelve_None_y_se_calla(sin_usage):
    """Todo con `getattr` a propósito.

    Las corridas guionizadas de las pruebas no traen `context_wrapper`, y el día que el SDK
    mueva el atributo esto tiene que devolver `None` y callarse -- no tumbar el turno de un
    paciente por un cambio en la forma de un objeto de instrumentación. Es la misma decisión
    que el `try` que envuelve la escritura, un nivel más arriba.
    """
    assert consumo._cifras(sin_usage) is None


def test_los_cacheados_salen_de_input_tokens_details():
    """El dato está anidado --`usage.input_tokens_details.cached_tokens`-- y es el único que
    lo está. Leerlo del sitio equivocado devolvería 0 en silencio, que es un número
    perfectamente creíble: la factura saldría diez veces más cara sin que nada fallara."""
    assert consumo._cifras(
        corrida(llamadas=2, entrada=5_000, cacheados=4_000, salida=300)
    ) == (2, 5_000, 4_000, 300)


def test_un_usage_sin_detalle_de_entrada_cuenta_cero_cacheados():
    """Degradar sin perder la corrida. Sin detalle no se sabe cuánto vino del caché, y el lado
    seguro es suponer que nada: sobreestima el costo en vez de esconderlo."""
    uso = SimpleNamespace(
        requests=1, input_tokens=1_000, output_tokens=50, input_tokens_details=None
    )

    assert consumo._cifras(SimpleNamespace(context_wrapper=SimpleNamespace(usage=uso))) == (
        1,
        1_000,
        0,
        50,
    )


# ==========================================================================================
# El vigilante del gasto del día
# ==========================================================================================


class BaseDelGasto:
    def __init__(self, *, gasto: float, toca: bool = True, revienta: bool = False) -> None:
        self.gasto = gasto
        self.toca = toca
        self.revienta = revienta
        self.marcas: list[float] = []

    def instalar(self, monkeypatch) -> BaseDelGasto:
        def conectar(_url):
            if self.revienta:
                raise RuntimeError("connection to server failed")
            return ConexionFalsa()

        def marcar(conn, *, gasto_usd):
            self.marcas.append(gasto_usd)
            return self.toca

        monkeypatch.setattr(persistencia, "conectar", conectar)
        monkeypatch.setattr(persistencia, "gasto_del_dia", lambda conn: self.gasto)
        monkeypatch.setattr(persistencia, "toca_avisar_gasto", marcar)
        return self


def test_por_debajo_del_umbral_no_se_quema_la_marca_del_dia(monkeypatch):
    """El orden importa: se mira el gasto ANTES de marcar.

    Al revés, un día tranquilo se habría quemado la marca igual --el vigilante corre cada
    minuto-- y el aviso de verdad, el del día que sí se pasa, no saldría nunca. Una alarma que
    se desarma sola a las 00:01 es peor que no tenerla.
    """
    base = BaseDelGasto(gasto=1.20).instalar(monkeypatch)

    assert consumo.gasto_y_alerta("postgresql://na", umbral_usd=5.0) == (1.20, False)
    assert base.marcas == [], "se consultó la marca de un día que no llegó al umbral"


def test_al_cruzar_el_umbral_se_avisa_una_vez(monkeypatch):
    """El vigilante corre cada minuto, así que sin la marca el día que se cruce el umbral el
    doctor recibiría 1.440 Telegram idénticos: la propia alarma sería la inundación."""
    base = BaseDelGasto(gasto=7.5).instalar(monkeypatch)

    assert consumo.gasto_y_alerta("postgresql://na", umbral_usd=5.0) == (7.5, True)
    assert base.marcas == [7.5]


def test_si_ya_se_aviso_hoy_se_devuelve_el_gasto_sin_avisar(monkeypatch):
    """El gasto se sigue devolviendo aunque el aviso se calle: quien llama lo registra igual,
    y perder la cifra por haber avisado ya sería perder justo la del día caro."""
    BaseDelGasto(gasto=9.0, toca=False).instalar(monkeypatch)

    assert consumo.gasto_y_alerta("postgresql://na", umbral_usd=5.0) == (9.0, False)


def test_si_la_base_no_contesta_el_vigilante_devuelve_None(monkeypatch):
    """`None` y no `(0.0, False)`: «no se pudo consultar» y «hoy no se gastó nada» son cosas
    distintas, y confundirlas le diría a la clínica que todo va bien justo cuando nadie lo
    sabe. Y no propaga: el vigilante es una tarea de fondo y una excepción la mataría para el
    resto del proceso."""
    BaseDelGasto(gasto=0.0, revienta=True).instalar(monkeypatch)

    assert consumo.gasto_y_alerta("postgresql://na", umbral_usd=5.0) is None

"""Lo único barato que impide que vuelva el fallo que ninguna prueba cazaba.

------------------------------------------------------------------------------------------
Qué pasó, y por qué esto existe
------------------------------------------------------------------------------------------

El 20/09/2026, con la fase 8 entera en verde --`pytest -q`, `-m neon`, `probar_panel.py`, el
endpoint devolviendo los datos correctos y el `PATCH` escribiendo bien--, la pantalla de
Agenda **no dejaba marcar la asistencia**. Las filas de la rejilla tenían una altura FIJA de
96 px y la tarjeta de una cita de 60 minutos --la duración por defecto de esta clínica, o sea
el caso normal-- mide 137 px con sus dos botones: se salía de su hora y la tarjeta de la hora
siguiente se pintaba encima. Los botones «Asistió» y «No asistió» de toda cita seguida de otra
quedaban FÍSICAMENTE tapados. Con citas consecutivas solo la última de la tanda era marcable,
que es justo la acción que la fase 8 entera existe para permitir.

Se encontró abriendo la pantalla con un navegador de verdad, y no había otra forma: el defecto
vivía solo en la geometría del navegador. El arreglo fue dejar crecer la fila de cada hora.

------------------------------------------------------------------------------------------
Lo que esta prueba SÍ hace, y lo que NO
------------------------------------------------------------------------------------------

**NO comprueba la maqueta.** Es una prueba de TEXTO sobre `Agenda.tsx`: no hay motor de
maquetación, así que no puede saber si un botón está tapado. Lo que hace es vigilar las tres
DECISIONES de las que dependía el arreglo, que es lo que alguien puede deshacer sin darse
cuenta de lo que deshace.

**Y no vale un arnés de jsdom**, que es la tentación obvia: sin motor de maquetación,
`getBoundingClientRect` devuelve ceros y una prueba así pasaría en verde sobre la pantalla
rota. Sería peor que no tener nada, porque además daría confianza.

Lo que de verdad cerraría el hueco es un navegador (Playwright). No se añadió porque no está
en las dependencias de este proyecto --ni en `pyproject.toml` ni en `web/package.json`-- y
meter un navegador entero en la cadena de pruebas es una decisión que no toma una prueba. Si
algún día se añade, esta prueba se queda igual: son guardas distintas.

**El único guardián de verdad sigue siendo abrir la pantalla** con dos citas seguidas de 60
minutos y comprobar que los dos botones se pulsan. Está escrito en `web/CLAUDE.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PANTALLA = Path(__file__).resolve().parents[1] / "web" / "src" / "pantallas" / "Agenda.tsx"


@pytest.fixture(scope="module")
def fuente() -> str:
    assert PANTALLA.is_file(), f"no está {PANTALLA}"
    return PANTALLA.read_text(encoding="utf-8")


def test_la_fila_de_cada_hora_puede_crecer(fuente: str) -> None:
    """`minHeight`, nunca `height`: es lo único que impide que una tarjeta se salga.

    Volver a `height: ALTO_HORA` restaura el fallo entero y no rompe ninguna otra prueba.
    """
    assert re.search(r"minHeight:\s*ALTO_HORA", fuente), (
        "la fila de la hora ya no lleva `minHeight: ALTO_HORA`. Si se cambió por una altura "
        "fija, la tarjeta de una cita de 60 minutos (137 px) se sale de su hora y la de la "
        "hora siguiente le tapa los botones de marcar."
    )


def test_no_queda_ninguna_altura_fija_en_la_pantalla(fuente: str) -> None:
    """Ni una sola. La minúscula de `height:` es lo que la distingue de `minHeight:`.

    Se mira el archivo ENTERO y no solo la rejilla, porque el mismo fallo apareció en dos
    sitios: la tarjeta de una cita y la de un bloqueo del doctor (un bloqueo de cuatro horas
    tapaba las citas de después). Las dos se arreglaron igual.
    """
    fijas = re.findall(r"[^a-zA-Z]height:\s*[^;'\"}\n]+", fuente)
    assert not fijas, (
        "apareció una altura fija en Agenda.tsx: "
        f"{fijas}. En esta pantalla la altura la decide el CONTENIDO -- los botones de "
        "marcar miden lo que miden y no se encogen. Usa `minHeight`."
    )


def test_la_linea_del_ahora_se_situa_dentro_de_su_hora(fuente: str) -> None:
    """La dependencia escondida del arreglo, y la que se olvidaría primero.

    La línea vivía fuera de las filas, posicionada desde arriba de la rejilla multiplicando
    por `ALTO_HORA`: una cuenta que da por hecho que todas las filas miden lo mismo. En
    cuanto una crece, esa línea apunta a la hora equivocada -- y solo se ve en el día de HOY,
    así que se descubriría tarde y por casualidad.
    """
    assert re.search(r"\(\(minutoAhora - h\)\s*/\s*60\)\s*\*\s*100", fuente), (
        "la línea del «ahora» ya no se sitúa como un porcentaje de su propia fila."
    )
    assert not re.search(r"top:[^\n]*ALTO_HORA", fuente), (
        "la línea del «ahora» volvió a calcularse multiplicando ALTO_HORA desde arriba de la "
        "rejilla. Eso solo es cierto si todas las filas miden lo mismo, y ya no lo miden."
    )


def test_la_lista_de_sin_marcar_esta_acotada_y_tiene_scroll(fuente: str) -> None:
    """El mismo fallo por la otra puerta, y con la misma consecuencia.

    `panel.citas_sin_marcar` devuelve hasta 50 filas, y ese panel es `shrink-0` dentro de una
    raíz `overflow-hidden`: sin cota, las últimas quedan recortadas **y sin ningún scroll que
    las alcance** -- el de la rejilla es de otro elemento. Invisibles y no marcables.
    """
    lista = re.search(r"verSinMarcar && \(\s*<ul className=\"([^\"]+)\"", fuente)
    assert lista, "no se encontró la lista de «sin marcar»; si se reescribió, revisa esta prueba"
    clases = lista.group(1)
    assert "max-h-" in clases and "overflow-y-auto" in clases, (
        f"la lista de «sin marcar» quedó sin cota o sin scroll propio (clases: '{clases}'). "
        "Con quince pendientes, las últimas no se pueden ni ver ni marcar."
    )

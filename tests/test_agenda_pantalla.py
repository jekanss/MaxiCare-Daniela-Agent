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

from maxicare_daniela import runtime

PANTALLA = Path(__file__).resolve().parents[1] / "web" / "src" / "pantallas" / "Agenda.tsx"
CLIENTE = Path(__file__).resolve().parents[1] / "web" / "src" / "api.ts"


@pytest.fixture(scope="module")
def fuente() -> str:
    assert PANTALLA.is_file(), f"no está {PANTALLA}"
    return PANTALLA.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cliente() -> str:
    assert CLIENTE.is_file(), f"no está {CLIENTE}"
    return CLIENTE.read_text(encoding="utf-8")


def test_la_fila_de_cada_hora_puede_crecer(fuente: str) -> None:
    """`minHeight`, nunca `height`: es lo único que impide que una tarjeta se salga.

    Volver a `height: ALTO_HORA` restaura el fallo entero y no rompe ninguna otra prueba.
    """
    assert re.search(r"minHeight:\s*ALTO_HORA", fuente), (
        "la fila de la hora ya no lleva `minHeight: ALTO_HORA`. Si se cambió por una altura "
        "fija, la tarjeta de una cita de 60 minutos (137 px) se sale de su hora y la de la "
        "hora siguiente le tapa los botones de marcar."
    )


#: Las alturas de Tailwind que esta pantalla sí puede fijar, con su motivo. La lista es
#: corta a propósito: añadir una es una decisión, y quien la añada escribe aquí por qué.
#:
#: - `h-full`, `h-auto`, `h-fit`, `h-min`, `h-max`: no fijan nada, la altura la sigue
#:   decidiendo el contenido o el padre. Son justo lo contrario del fallo.
#: - `h-px` y todo lo que mida menos de `h-4` (1 rem): un punto o una línea de un pelo. La
#:   línea del «ahora» es `h-0.5` y su punto `h-2.5`; ninguno de los dos puede tapar un
#:   botón porque ninguno de los dos contiene nada.
_ALTURAS_DE_TAILWIND_PERMITIDAS = {"h-full", "h-auto", "h-fit", "h-min", "h-max", "h-px"}

#: Por debajo de esto (en la escala de Tailwind, donde 4 = 1 rem = 16 px) una altura fija no
#: es maqueta: es un adorno que no envuelve a nadie.
_ESCALA_INOFENSIVA = 4


def test_no_queda_ninguna_altura_fija_en_la_pantalla(fuente: str) -> None:
    """Ni una sola. La minúscula de `height:` es lo que la distingue de `minHeight:`.

    Se mira el archivo ENTERO y no solo la rejilla, porque el mismo fallo apareció en dos
    sitios: la tarjeta de una cita y la de un bloqueo del doctor (un bloqueo de cuatro horas
    tapaba las citas de después). Las dos se arreglaron igual.

    **Y se miran las DOS formas de escribir una altura, no solo el CSS de `style`.** Esta
    guarda nació escaneando `height:` y nada más, y este archivo usa Tailwind para el layout
    por convención declarada en `web/CLAUDE.md`: un `h-24` o un `h-[96px]` en el `className`
    de la fila reintroduce el fallo ENTERO --los botones de marcar tapados por la tarjeta de
    la hora siguiente-- y la guarda seguía en verde. Era el hueco más barato de cerrar del
    arnés que sustituyó a Playwright, y encima el dialecto normal del archivo, que es lo que
    lo volvía probable.
    """
    fijas = re.findall(r"[^a-zA-Z]height:\s*[^;'\"}\n]+", fuente)
    assert not fijas, (
        "apareció una altura fija en Agenda.tsx: "
        f"{fijas}. En esta pantalla la altura la decide el CONTENIDO -- los botones de "
        "marcar miden lo que miden y no se encogen. Usa `minHeight`."
    )

    # `(?<![\w-])` es lo que impide que `max-h-[40vh]` o `min-h-0` cuenten como alturas
    # fijas: un tope y un mínimo son exactamente lo contrario de este fallo.
    de_tailwind = re.findall(r"(?<![\w-])h-(\[[^\]\s\"']+\]|[\w.]+)", fuente)
    sospechosas = []
    for valor in de_tailwind:
        clase = f"h-{valor}"
        if clase in _ALTURAS_DE_TAILWIND_PERMITIDAS:
            continue
        try:
            if float(valor) < _ESCALA_INOFENSIVA:
                continue
        except ValueError:
            pass  # `h-[96px]`, `h-screen`, `h-dvh`: se decide a mano, no por descuido
        sospechosas.append(clase)

    assert not sospechosas, (
        f"apareció una altura fija de Tailwind en Agenda.tsx: {sospechosas}. Es el mismo "
        "fallo que `height:`, escrito en el dialecto de este archivo: con la fila de la hora "
        "a 96 px, la tarjeta de una cita de 60 minutos mide 137 px, se sale de su hora y la "
        "tarjeta de la hora siguiente le tapa los botones de marcar. Usa `min-h-`, o añade "
        "la clase a `_ALTURAS_DE_TAILWIND_PERMITIDAS` con el motivo escrito."
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


def _motivos_de_python() -> set[str]:
    """Los literales de `motivo_sin_calendario` tal como los decide `runtime.py`.

    Se leen del módulo en vez de escribirse aquí a propósito: añadir un `MOTIVO_*` nuevo tiene
    que hacer fallar la prueba de abajo por sí solo, sin que nadie se acuerde de venir a
    apuntarlo. El barrido por prefijo es ancho --se llevaría cualquier otra constante que
    empiece por `MOTIVO_`--, y eso es correcto: en este archivo esa constante existiría para
    cruzar el borde hacia TypeScript.
    """
    return {
        v for n, v in vars(runtime).items()
        if n.startswith("MOTIVO_") and isinstance(v, str)
    }


def test_los_motivos_sin_calendario_los_decide_python(cliente: str) -> None:
    """Lo único que ata las dos copias del contrato a través del borde de lenguaje.

    `motivo_sin_calendario` decide si la pantalla pinta la franja gris («este día ya es
    antiguo», rutina) o la roja («no se pudo consultar Google Calendar», avería). Los
    literales los decide `runtime.py` y TypeScript los declara una sola vez, en `api.ts`.

    Sin esta prueba, la pareja se separa en silencio y de la peor forma: el servidor manda un
    motivo que el front no conoce, el `else` de la pantalla lo manda a la franja ROJA, y el
    caso rutinario vuelve a dar la alarma diaria que este arreglo existe para quitar. No hay
    error en ningún log, la suite sigue verde y `npm run build` también --TypeScript no puede
    saber qué manda un servidor--.

    Es exactamente el motivo por el que el aviso NO se calcula en el front comparando `dia`
    con hoy: duplicar `DIAS_HACIA_ATRAS_AL_SINCRONIZAR` en TypeScript es otra copia sin nada
    que la ate, y el día que alguien suba la constante a 3 el aviso vuelve a mentir.
    """
    union = re.search(r"export type MotivoSinCalendario\s*=\s*([^\n]+)", cliente)
    assert union, (
        "no está `export type MotivoSinCalendario` en web/src/api.ts. Es el único sitio de "
        "TypeScript donde se pueden escribir esos literales; si se movió, mueve esta prueba."
    )
    en_typescript = set(re.findall(r"'([^']+)'", union.group(1)))
    en_python = _motivos_de_python()
    assert en_typescript == en_python, (
        f"los motivos de `motivo_sin_calendario` ya no coinciden: Python dice {sorted(en_python)} "
        f"y TypeScript dice {sorted(en_typescript)}. Los decide `runtime.py`; `api.ts` los "
        "copia. Un motivo que el front no conozca cae en la franja ROJA, así que el caso "
        "rutinario volvería a dar la alarma que este aviso existe para no dar."
    )
    # Y cada uno con su constante exportada, que es lo que las pantallas consumen.
    for motivo in en_python:
        assert re.search(rf":\s*MotivoSinCalendario\s*=\s*'{re.escape(motivo)}'", cliente), (
            f"«{motivo}» está en el tipo pero no tiene constante exportada en api.ts. Sin "
            "ella, la pantalla que lo necesite escribirá la cadena a mano."
        )


def test_la_pantalla_no_escribe_a_mano_ningun_motivo_sin_calendario(fuente: str) -> None:
    """La otra mitad: `Agenda.tsx` compara contra la constante, nunca contra la cadena.

    Una cadena suelta aquí es una tercera copia, y además una que la prueba de arriba no ve:
    `api.ts` y `runtime.py` podrían seguir de acuerdo mientras la pantalla compara contra un
    literal que ya no manda nadie, con lo que el día viejo saldría en rojo otra vez.
    """
    for motivo in _motivos_de_python():
        assert f"'{motivo}'" not in fuente and f'"{motivo}"' not in fuente, (
            f"Agenda.tsx escribe «{motivo}» a mano. Importa la constante de `@/api` y compara "
            "contra ella: los literales los decide runtime.py y api.ts es su única copia."
        )
    assert "MOTIVO_FUERA_DE_VENTANA" in fuente, (
        "Agenda.tsx dejó de distinguir el día viejo de la avería. Con un solo aviso vuelve la "
        "franja roja «No se pudo consultar Google Calendar» sobre un día que no falló: cinco "
        "de los siete días a los que lleva «Ver ese día» caen fuera de la ventana, así que la "
        "alarma sonaría a diario por nada y dejaría de leerse el día que significa algo."
    )

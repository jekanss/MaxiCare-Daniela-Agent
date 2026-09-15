"""Pruebas de la forma del proyecto, no de su comportamiento.

Dos cosas que tienen que seguir siendo ciertas en cada despliegue, y que nadie va a
verificar leyendo el código por encima:

1. `agentes.py`, `herramientas.py` y `contratos.py` no importan de `runtime.py`. Es la
   frontera entre "qué hace el agente" y "por dónde llega la conversación". Romperla acopla
   cada agente al canal de hoy, y cambiar de canal deja de ser un archivo reescrito para
   convertirse en una reescritura de la lógica de negocio.

2. Los `output_type` del plan son schemas que el SDK acepta de verdad. Un `output_type` que
   el SDK rechaza no falla en una prueba: falla en la primera conversación real.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PAQUETE = Path(__file__).resolve().parents[1] / "src" / "maxicare_daniela"

#: Los módulos que nunca pueden importar transporte. `runtime.py` y `config.py` quedan
#: fuera a propósito: el primero ES el transporte, el segundo lo alimenta.
#:
#: `ingesta.py` y `canales.py` están en la lista aunque hagan IO: `canales.py` habla con
#: WhatsApp y Telegram, e `ingesta.py` decide qué hacer con lo que llega, pero ninguno sabe
#: —ni debe saber— quién los invocó. Esa ignorancia es lo que permite probar el viaje entero
#: de un archivo sin levantar un servidor, y lo que permitiría recibir los webhooks por otra
#: vía sin tocarlos.
MODULOS_SIN_TRANSPORTE = (
    "agentes.py", "herramientas.py", "contratos.py", "ingesta.py", "canales.py",
    # `calendario.py` (fase 3) entra por la misma razón que los demás: Google Calendar es un
    # sistema de `sistemas.lista[]`, no un canal de entrada. Da igual si la petición que
    # disparó la consulta llegó por WhatsApp, por el chat web o por un cron.
    "calendario.py",
    # `guardrails.py` (fase 4) es la regla de negocio más pura que hay en el proyecto: lo
    # que el agente PUEDE hacer. Si necesitara saber por dónde llegó el mensaje para decidir
    # si bloquea, ya no sería una regla, sería una configuración del canal.
    "guardrails.py",
    # `conversacion.py` (fase 5) es el único módulo que llama a `Runner.run`, y aun así entra
    # en esta lista. Es la pieza clave de la frontera, no una excepción a ella: el chat web y
    # el webhook de WhatsApp necesitan EL MISMO turno, con sus guardrails y su reintento. Si
    # este módulo supiera de HTTP, la fase 6 tendría que duplicarlo o hacer que `ingesta.py`
    # importara `runtime` -- las dos cosas que este archivo existe para impedir.
    "conversacion.py",
    # `autenticacion.py` (fase 5) recibe y devuelve cadenas; no sabe que una de ellas viaja
    # en una cookie. Esa ignorancia es lo que permite probar que un token vencido se rechaza
    # sin levantar un servidor ni esperar ocho horas.
    "autenticacion.py",
    # `atencion.py` (fase 6A) es el turno completo de WhatsApp -- conversación, contexto,
    # retardo y envío-- y aun así no sabe que el mensaje llegó por un POST de Meta. Es lo que
    # permite probar el turno entero sin levantar un servidor, y es la razón de que exista
    # como módulo aparte: `runtime.py` solo pone la línea que lo llama. Con el turno dentro
    # del handler, probar «el retardo descuenta lo que tardó el modelo» exigiría un servidor,
    # una firma de Meta y un minuto de espera por caso.
    "atencion.py",
    # `panel.py` (fase 8) recibe una conexión y devuelve diccionarios. No sabe que alguien
    # los va a serializar como JSON detrás de una cookie, y por eso sus pruebas pueden
    # comprobar que la bitácora es atómica sin levantar un servidor.
    "panel.py",
    # `analista.py` (tarea 3 de «sin resolver») corre en su propia tarea de fondo, no en un
    # turno de paciente, y aun así no sabe que existe un reloj ni un `@app.on_event`: recibe
    # un `database_url` y un límite, y devuelve cuántos informes escribió. Eso es lo que
    # permite probar `analizar_pendientes` sin levantar `runtime.py` ni su bucle.
    "analista.py",
)

#: Solo `runtime.py` importa el framework web. Si FastAPI aparece en cualquier otro módulo,
#: la frontera ya se rompió aunque nadie haya importado `runtime` literalmente.
FRAMEWORKS_DE_TRANSPORTE = ("fastapi", "starlette", "uvicorn", "flask", "django")


def _segmentos(nombre: str | None) -> list[str]:
    return nombre.split(".") if nombre else []


def _importa_runtime(ruta: Path) -> bool:
    """True si el archivo importa el módulo `runtime`, comparando SEGMENTOS COMPLETOS del
    nombre de módulo y no subcadenas.

    La diferencia importa: `from typing import runtime_checkable` contiene la subcadena
    'runtime' y no es una violación — y `contratos.py` es justo el módulo que más
    probablemente lo use.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            if any("runtime" in _segmentos(alias.name) for alias in nodo.names):
                return True
        elif isinstance(nodo, ast.ImportFrom):
            if "runtime" in _segmentos(nodo.module):
                return True
            # El nombre importado puede ser `runtime` venga de donde venga:
            # `from maxicare_daniela import runtime` es el import canónico entre módulos
            # hermanos de un layout src/, y es la silueta más probable de violación real.
            if any(alias.name == "runtime" for alias in nodo.names):
                return True
    return False


# ==========================================================================================
# El detector, probado antes de usarlo
# ==========================================================================================

VIOLACIONES = {
    "import_directo": "import runtime\n",
    "import_dotted": "import maxicare_daniela.runtime as rt\n",
    "from_absoluto": "from maxicare_daniela.runtime import enviar\n",
    "from_relativo": "from .runtime import enviar\n",
    "from_paquete_nombrando": "from maxicare_daniela import runtime\n",
    "from_paquete_entre_varios": "from maxicare_daniela import contratos, runtime\n",
    "dentro_de_funcion": "def f():\n    from .runtime import enviar\n    return enviar\n",
    "dentro_de_try": "try:\n    from .runtime import enviar\nexcept ImportError:\n    enviar = None\n",
}

LIMPIOS = {
    "import_normal": "from agents import Agent\n",
    "runtime_checkable": "from typing import Protocol, runtime_checkable\n",
    "prefijo_parecido": "from maxicare_daniela import runtime_helpers\n",
    "modulo_con_prefijo": "import pyruntime_stub\n",
}


@pytest.mark.parametrize("codigo", VIOLACIONES.values(), ids=list(VIOLACIONES))
def test_el_detector_encuentra_las_violaciones(codigo, tmp_path):
    ruta = tmp_path / "m.py"
    ruta.write_text(codigo, encoding="utf-8")
    assert _importa_runtime(ruta) is True


@pytest.mark.parametrize("codigo", LIMPIOS.values(), ids=list(LIMPIOS))
def test_el_detector_no_marca_falsos_positivos(codigo, tmp_path):
    ruta = tmp_path / "m.py"
    ruta.write_text(codigo, encoding="utf-8")
    assert _importa_runtime(ruta) is False


# ==========================================================================================
# La frontera, sobre los archivos reales
# ==========================================================================================


@pytest.mark.parametrize("nombre", MODULOS_SIN_TRANSPORTE)
def test_la_logica_de_negocio_no_importa_transporte(nombre):
    ruta = PAQUETE / nombre
    assert ruta.is_file(), f"falta {nombre} en el paquete"
    assert not _importa_runtime(ruta), (
        f"{nombre} importa de runtime: rompe la separación agentes/transporte del plan"
    )


def test_la_plantilla_esta_completa():
    """Un módulo que no existe no documenta nada; uno vacío con su docstring sí."""
    esperados = {
        "__init__.py",
        "agentes.py",
        "herramientas.py",
        "contratos.py",
        "runtime.py",
        "config.py",
    }
    presentes = {p.name for p in PAQUETE.glob("*.py")}
    assert esperados <= presentes, f"faltan módulos: {esperados - presentes}"


# ==========================================================================================
# Los output_type, contra el SDK de verdad
# ==========================================================================================


@pytest.mark.parametrize("modelo_nombre", ["LecturaArchivo", "RespuestaDaniela"])
def test_los_output_type_son_schemas_que_el_sdk_acepta(modelo_nombre):
    """`AgentOutputSchema` es lo que el SDK construye por dentro cuando se le pasa un
    `output_type` a un `Agent`. Si el modelo no se puede convertir a un JSON Schema válido,
    esto falla aquí y no en la primera conversación real."""
    from agents import AgentOutputSchema

    import maxicare_daniela.contratos as contratos

    modelo = getattr(contratos, modelo_nombre)
    esquema = AgentOutputSchema(modelo)
    generado = esquema.json_schema()

    assert generado["type"] == "object"
    assert set(modelo.model_fields) <= set(generado["properties"])
    assert esquema.is_strict_json_schema() is True


# ==========================================================================================
# La otra mitad de la frontera: el framework web vive en un solo archivo
# ==========================================================================================


def _importa_framework_web(ruta: Path) -> str | None:
    """Devuelve el nombre del framework de transporte importado, o None.

    `_importa_runtime` vigila que nadie importe `runtime.py`, pero eso solo detecta la
    violación indirecta. Un `from fastapi import Request` dentro de `ingesta.py` rompería la
    frontera igual —esa función ya no se podría probar sin un servidor— sin tocar `runtime`.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                raiz = _segmentos(alias.name)[0] if alias.name else ""
                if raiz in FRAMEWORKS_DE_TRANSPORTE:
                    return raiz
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            raiz = _segmentos(nodo.module)[0]
            if raiz in FRAMEWORKS_DE_TRANSPORTE:
                return raiz
    return None


@pytest.mark.parametrize("nombre", MODULOS_SIN_TRANSPORTE)
def test_ningun_modulo_de_logica_importa_un_framework_web(nombre):
    ruta = PAQUETE / nombre
    if not ruta.is_file():
        pytest.skip(f"{nombre} todavía no existe")
    culpable = _importa_framework_web(ruta)
    assert culpable is None, (
        f"{nombre} importa {culpable!r}. El framework web vive solo en runtime.py: "
        "en cualquier otro módulo ata la lógica al canal de hoy y obliga a levantar un "
        "servidor para probarla."
    )


def test_runtime_si_importa_el_framework():
    """El complemento del test anterior. Sin esto, borrar FastAPI de runtime.py dejaría
    todos los demás tests en verde mientras el servidor no existe."""
    assert _importa_framework_web(PAQUETE / "runtime.py") == "fastapi"


def test_el_detector_de_framework_no_confunde_un_nombre_parecido(tmp_path):
    ruta = tmp_path / "m.py"
    ruta.write_text("import fastapi_helpers\nfrom starlette_utils import x\n", encoding="utf-8")
    assert _importa_framework_web(ruta) is None

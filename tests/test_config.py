"""Las variables de entorno que llegan presentes y vacías.

Este módulo existe por un fallo de producción, no por completitud. El `.env` del proyecto
trae seis claves presentes y sin valor, `cargar_dotenv` se niega a exportarlas, y por eso en
local nunca pasó nada. En el VPS las variables las pone el `env_file` de Docker, que sí las
exporta, y dentro del contenedor no hay ningún `.env` que leer: el filtro no llega a correr.

El resultado fue que Daniela contestaba a todos el mensaje seguro mientras `/salud` decía
`configuracion: ok`.
"""

from __future__ import annotations

import os

from openai import OpenAI

from maxicare_daniela import config


def test_una_variable_de_terceros_vacia_se_descarta(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "")

    quitadas = config.descartar_vacias_de_terceros()

    assert "OPENAI_BASE_URL" in quitadas
    assert "OPENAI_BASE_URL" not in os.environ


def test_solo_espacios_tambien_cuenta_como_vacia(monkeypatch):
    monkeypatch.setenv("OPENAI_PROJECT_ID", "   ")

    assert "OPENAI_PROJECT_ID" in config.descartar_vacias_de_terceros()
    assert "OPENAI_PROJECT_ID" not in os.environ


def test_una_variable_con_valor_no_se_toca(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.interno/v1")

    assert config.descartar_vacias_de_terceros() == []
    assert os.environ["OPENAI_BASE_URL"] == "https://proxy.interno/v1"


def test_una_variable_ausente_no_es_un_problema(monkeypatch):
    for nombre in config.VARIABLES_DE_TERCEROS:
        monkeypatch.delenv(nombre, raising=False)

    assert config.descartar_vacias_de_terceros() == []


def test_las_variables_del_proyecto_no_entran_aqui(monkeypatch):
    """La frontera es deliberada y conviene que una prueba la sostenga.

    `MAXICARE_MODELO_DANIELA=` vacía ya la resuelve `_opcional`, que cae al default. Meterla
    también en esta lista sería una segunda forma de arreglar lo mismo, y el día que las dos
    discrepen nadie sabría cuál manda.
    """
    monkeypatch.setenv("MAXICARE_MODELO_DANIELA", "")

    config.descartar_vacias_de_terceros()

    assert os.environ["MAXICARE_MODELO_DANIELA"] == ""
    assert config._opcional("MAXICARE_MODELO_DANIELA", "gpt-5.6-terra") == "gpt-5.6-terra"


def test_el_cliente_de_openai_queda_apuntando_a_openai(monkeypatch):
    """La prueba que de verdad protege: sin ella, quitar un nombre de la lista pasa sola.

    Comprueba las dos mitades sobre el cliente real. Construirlo no abre ninguna conexión,
    así que no hace falta red ni una clave verdadera.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-de-mentira")

    monkeypatch.setenv("OPENAI_BASE_URL", "")
    assert str(OpenAI().base_url) == "", "el fallo que se persigue ya no se reproduce"

    config.descartar_vacias_de_terceros()
    assert str(OpenAI().base_url) == "https://api.openai.com/v1/"


def test_la_url_de_la_politica_arranca_en_pendiente(monkeypatch):
    """Regla dura 3: lo que no se sabe se marca con el literal, nunca con un valor plausible.
    Aquí además apaga el aviso, que es lo que se quiere: es preferible no enseñar un enlace
    que enseñar uno que no vamos a poder sostener."""
    monkeypatch.setenv("MAXICARE_DATABASE_URL", "postgres://nada")
    monkeypatch.delenv("MAXICARE_POLITICA_DATOS_URL", raising=False)

    c = config.Config.desde_entorno()

    assert c.politica_datos_url == "PENDIENTE"
    assert c.politica_datos_version == "politica-2026-09"

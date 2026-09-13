"""El `group_id` y el `trace_metadata` de las tres puertas que hablan con OpenAI.

Nada de aquí toca la red: se construyen `RunConfig` y se miran sus campos.
"""

from __future__ import annotations

from maxicare_daniela import agentes, config


def test_la_version_del_prompt_es_corta_y_estable():
    """Corta porque va en cada trace y nadie lee un sha256 entero; estable porque el mismo
    texto tiene que dar el mismo hash entre procesos --`hash()` de Python no vale: está
    aleatorizado por `PYTHONHASHSEED` y cambiaría en cada arranque."""
    una = config.version_de_prompt("hola")
    otra = config.version_de_prompt("hola")

    assert una == otra
    assert len(una) == 12
    assert una == "b221d9dbb083"  # sha256("hola")[:12], fijo entre procesos


def test_un_prompt_distinto_da_una_version_distinta():
    assert config.version_de_prompt("hola") != config.version_de_prompt("hola ")


def test_la_version_de_daniela_sale_del_texto_estatico():
    assert agentes.VERSION_PROMPT == config.version_de_prompt(agentes.INSTRUCCIONES_DANIELA)


def test_el_vocabulario_de_tratamientos_no_mueve_la_version(monkeypatch):
    """A propósito. El vocabulario se pega AL FINAL del prompt y cambia sin desplegar --la
    clínica añade un tratamiento desde el panel--. Metido en el hash, cada tratamiento nuevo
    parecería un prompt nuevo y la versión dejaría de significar «el prompt cambió».

    Es la misma razón por la que el vocabulario va al final y no al principio: el caché de
    prompt de OpenAI funciona por prefijo idéntico.
    """
    antes = agentes.VERSION_PROMPT
    monkeypatch.setattr(
        agentes.contratos, "vocabulario", lambda: ("ortodoncia", "implantes", "carillas")
    )

    assert agentes.VERSION_PROMPT == antes


def test_la_version_del_lector_es_la_suya():
    """Dos prompts distintos, dos versiones distintas. Compartirlas haría que un cambio en
    el lector pareciera un cambio en Daniela."""
    assert agentes.VERSION_PROMPT_LECTOR != agentes.VERSION_PROMPT
    assert agentes.VERSION_PROMPT_LECTOR == config.version_de_prompt(
        agentes.INSTRUCCIONES_LECTOR
    )

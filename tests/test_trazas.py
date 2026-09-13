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


# ==========================================================================================
# Tarea 10: `config_de_corrida()` con argumentos
# ==========================================================================================


def test_el_group_id_es_la_conversacion_y_no_el_telefono():
    """La distinción importa y está decidida: un teléfono agrupa a una persona para siempre;
    una conversación agrupa un EPISODIO, que es la unidad que alguien va a querer leer
    cuando llegue un reclamo."""
    corrida = config.config_de_corrida(group_id="b3f1c2d4-0000-4000-8000-000000000001")

    assert corrida.group_id == "b3f1c2d4-0000-4000-8000-000000000001"


def test_el_metadata_lleva_canal_y_version_de_prompt():
    corrida = config.config_de_corrida(
        group_id="conv-1", canal="web", version_prompt="abc123def456"
    )

    assert corrida.trace_metadata == {"canal": "web", "version_prompt": "abc123def456"}


def test_sin_version_de_prompt_el_metadata_no_la_inventa():
    """Los evaluadores de guardrail tienen su propio prompt, que no es el de Daniela. Poner
    el de Daniela ahí sería un dato plausible y falso, que es peor que no tenerlo."""
    corrida = config.config_de_corrida(group_id="conv-1", canal="whatsapp")

    assert corrida.trace_metadata == {"canal": "whatsapp"}


def test_sigue_sin_subir_el_contenido_de_la_conversacion():
    """LA PRUEBA QUE NO SE PUEDE RELAJAR. `RunConfig()` nace en la 0.22.2 con
    `trace_include_sensitive_data=True`, y desde la fase 6A hasta el 13/09/2026 cada
    conversación de WhatsApp subió íntegra al dashboard de OpenAI. Añadirle argumentos a
    esta función no puede reabrir esa puerta."""
    corrida = config.config_de_corrida(group_id="conv-1")

    assert corrida.trace_include_sensitive_data is False
    assert corrida.workflow_name == config.WORKFLOW_NAME


def test_sin_argumentos_sigue_funcionando_como_antes():
    """Los tres llamadores se cablean uno a uno; mientras tanto, ninguno se rompe."""
    corrida = config.config_de_corrida()

    assert corrida.group_id is None
    assert corrida.trace_include_sensitive_data is False


def test_el_contexto_sabe_por_que_canal_habla():
    from maxicare_daniela.contratos import ContextoDaniela
    from maxicare_daniela.calendario import CalendarioDoble

    ctx = ContextoDaniela(
        id_conversacion="conv-1",
        telefono_completo="573001112233",
        database_url="postgresql://x/y",
        calendario=CalendarioDoble(),
    )

    assert ctx.canal == "whatsapp", "el default es el canal de producción"

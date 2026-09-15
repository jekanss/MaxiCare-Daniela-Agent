"""El analista: el modelo nano que le escribe tres frases a cada caso.

Offline. El modelo se dobla; lo que se prueba es la forma del contrato y la prohibicion
dura, no lo que un modelo real conteste.
"""

from __future__ import annotations

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

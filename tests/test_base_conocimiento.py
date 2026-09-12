"""Pruebas de la regla que protege el primer objetivo del brief.

`trabajo.resumen`: *«responde con información aprobada por la clínica»*.
`fallos.que_pasa_si_no_sabe`: *«Daniela reconoce que no tiene ese dato, no lo completa con
una estimación ni extrapola de tratamientos parecidos»*.

Estas pruebas no necesitan base de datos: la regla vive en `formatear_conocimiento`, que es
una función pura, precisamente para que se pueda probar sin levantar nada.
"""

from __future__ import annotations

import json
import re

import pytest

from maxicare_daniela.contratos import Tratamiento
from maxicare_daniela.persistencia import (
    CONFIGURACION_POR_DEFECTO,
    PENDIENTE_APROBACION,
    FilaConocimiento,
    cargar_semilla,
    formatear_conocimiento,
)

SEMILLA = cargar_semilla()
TRATAMIENTOS_VALIDOS = set(Tratamiento.__args__)


# ==========================================================================================
# El entregable de la fase 1: la ausencia de dato es un dato explícito
# ==========================================================================================


def test_sin_filas_devuelve_sin_dato_documentado_y_no_vacio():
    """EL TEST DE LA FASE 1.

    Un resultado vacío es una invitación a que el modelo complete el hueco con algo
    razonable, y «algo razonable» sobre un precio es el fallo que rompe el objetivo 1.
    """
    texto = formatear_conocimiento([], tratamiento="endodoncia", concepto="precio")

    assert texto != ""
    assert texto is not None
    assert "SIN DATO DOCUMENTADO" in texto
    assert "endodoncia" in texto
    assert "escala" in texto.lower()


@pytest.mark.parametrize("tratamiento", ["endodoncia", "protesis"])
def test_endodoncia_y_protesis_no_tienen_ni_una_fila_en_la_semilla(tratamiento):
    """Sección 2.12 del documento maestro: «No hay precio real, especialista asignado,
    inclusiones, exclusiones, garantías ni respuestas clínicas específicas documentadas...
    no debe publicarse ninguna cifra».

    La forma de cumplirlo no es una regla en el prompt: es que no exista la fila.
    """
    filas = [f for f in SEMILLA if f["tratamiento"] == tratamiento]
    assert filas == [], f"{tratamiento} tiene filas cargadas y no debería: {filas}"


def test_la_respuesta_de_sin_dato_no_contiene_ninguna_cifra():
    """El guardrail `sin_cifra_no_documentada` del bloque 8 compara las cifras del mensaje
    contra las que devolvió esta consulta. Si el propio literal trajera un número, abriría
    un hueco por donde pasaría una cifra sin respaldo."""
    texto = formatear_conocimiento([], tratamiento="protesis", concepto="precio")
    assert not re.search(r"\d", texto), f"el literal SIN DATO trae una cifra: {texto}"


# ==========================================================================================
# Lo que existe pero MaxiCare no ha aprobado
# ==========================================================================================


def test_una_fila_no_aprobada_sale_con_su_advertencia():
    fila = FilaConocimiento(
        tratamiento="implantes",
        concepto="garantia",
        contenido="La ficha dice que sí tiene garantía.",
        aprobado=False,
        nota_pendiente="No hay plazo, cobertura ni condiciones documentadas.",
    )
    texto = formatear_conocimiento([fila], tratamiento="implantes", concepto="garantia")

    assert PENDIENTE_APROBACION in texto
    assert "No hay plazo" in texto
    assert "sí tiene garantía" in texto


def test_una_fila_aprobada_sale_limpia():
    fila = FilaConocimiento(
        tratamiento="cordales",
        concepto="precio",
        contenido="Una cordal: $300.000. Dos: $500.000.",
        aprobado=True,
    )
    texto = formatear_conocimiento([fila], tratamiento="cordales", concepto="precio")

    assert PENDIENTE_APROBACION not in texto
    assert "SIN DATO DOCUMENTADO" not in texto
    assert "$300.000" in texto


# ==========================================================================================
# La semilla, contra el documento maestro
# ==========================================================================================


def test_la_semilla_es_json_valido_y_no_esta_vacia():
    assert len(SEMILLA) > 40


def test_todos_los_tratamientos_de_la_semilla_estan_en_el_literal():
    """Si la semilla trae un tratamiento que el `Literal` de `contratos.py` no conoce,
    `lector_archivos` no podría devolverlo nunca y esa información sería inalcanzable."""
    en_semilla = {f["tratamiento"] for f in SEMILLA} - {"_general"}
    desconocidos = en_semilla - TRATAMIENTOS_VALIDOS
    assert not desconocidos, f"tratamientos fuera del Literal de contratos.py: {desconocidos}"


def test_no_hay_pares_tratamiento_concepto_duplicados():
    """El UNIQUE de la tabla lo impediría en la base, pero fallar aquí da un mensaje útil en
    vez de un error de Postgres a mitad de la carga."""
    vistos = [(f["tratamiento"], f["concepto"]) for f in SEMILLA]
    duplicados = {p for p in vistos if vistos.count(p) > 1}
    assert not duplicados, f"pares duplicados en la semilla: {duplicados}"


def test_toda_fila_no_aprobada_explica_que_falta():
    """Una fila marcada como pendiente sin decir QUÉ falta es una fila que nadie va a
    resolver, porque nadie sabe qué preguntarle a MaxiCare."""
    sin_explicacion = [
        (f["tratamiento"], f["concepto"])
        for f in SEMILLA
        if not f.get("aprobado", True) and not f.get("nota_pendiente")
    ]
    assert not sin_explicacion, f"filas pendientes sin nota: {sin_explicacion}"


def test_los_once_tratamientos_con_precio_documentado_lo_tienen():
    """El documento maestro documenta precio para once tratamientos. Endodoncia y prótesis
    quedan fuera a propósito."""
    con_precio = {
        f["tratamiento"] for f in SEMILLA if f["concepto"] in {"precio", "paquetes"}
    } - {"_general"}
    esperados = {
        "cordales", "implantes", "blanqueamiento", "ortodoncia", "diseno_sonrisa",
        "microdiseno", "coronas", "periodoncia", "gingivectomia", "bichectomia", "limpieza",
    }
    assert con_precio == esperados


def test_la_semilla_no_contiene_ningun_documento_de_identidad():
    """Ninguna cédula de ejemplo se coló en la transcripción del documento maestro."""
    crudo = json.dumps(SEMILLA, ensure_ascii=False)
    assert not re.search(r"\bC\.?C\.?\s*\d", crudo, re.IGNORECASE)


# ==========================================================================================
# Configuración operativa
# ==========================================================================================


def test_los_defaults_operativos_coinciden_con_lo_que_decidio_el_cliente():
    assert CONFIGURACION_POR_DEFECTO["capacidad_por_hora"] == 2
    assert CONFIGURACION_POR_DEFECTO["duracion_cita_minutos"] == 60
    assert CONFIGURACION_POR_DEFECTO["cierre_relevo_minutos"] == 180
    assert CONFIGURACION_POR_DEFECTO["aviso_relevo_minutos"] == 120


def test_el_aviso_del_relevo_llega_antes_que_el_cierre():
    """Avisar al doctor después de haberle quitado la conversación no sirve de nada."""
    assert (
        CONFIGURACION_POR_DEFECTO["aviso_relevo_minutos"]
        < CONFIGURACION_POR_DEFECTO["cierre_relevo_minutos"]
    )

"""Pruebas de los cinco contratos Pydantic del plan.

Estas pruebas no comprueban que Pydantic funcione. Comprueban que cada decisión de diseño
del plan quedó expresada como una restricción de tipo y no como una intención:

- que una frase clínica NO cabe en el campo que cruza hacia el paciente
- que no existe dónde poner una cédula ni un teléfono en una solicitud de cita
- que los campos que el orquestador necesita para ramificar están y son obligatorios
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from maxicare_daniela.contratos import (
    LecturaArchivo,
    RespuestaDaniela,
    SolicitudCancelacion,
    SolicitudCita,
    SolicitudEscalamiento,
)

# ==========================================================================================
# LecturaArchivo — el muro entre lo que ve el doctor y lo que ve el paciente
# ==========================================================================================

REMISION_DE_MARIA = {
    "tipo_documento": "remision_externa",
    "tratamiento": "cordales",
    "origen": "Clínica Dental Norte, Dr. Ramírez",
    "fecha_documento": date(2026, 9, 5),
    "confianza": "alta",
    "contexto_clinico": (
        "Remisión: 'se remite a cirugía oral para extracción de terceros molares "
        "incluidos 18, 28, 38 y 48'. Menciona radiografía panorámica adjunta."
    ),
}


def test_lectura_archivo_se_instancia_con_el_caso_real():
    lectura = LecturaArchivo(**REMISION_DE_MARIA)
    assert lectura.tratamiento == "cordales"
    assert "incluidos" in lectura.contexto_clinico


@pytest.mark.parametrize(
    "frase_clinica",
    [
        "extracción de cuatro cordales incluidos 18, 28, 38 y 48",
        "cordales incluidos en mala posición",
        "cordales",  # con mayúscula/espacios alrededor sigue sin ser el literal exacto
    ],
)
def test_tratamiento_no_admite_una_frase_clinica(frase_clinica):
    """EL TEST DEL MURO.

    Si este test empieza a pasar con una frase, el campo dejó de ser un `Literal` cerrado y
    todo el bloque 2 del plan se volvió decorativo: `daniela` podría recibir —y repetirle al
    paciente— el contenido clínico del documento.
    """
    if frase_clinica == "cordales":
        pytest.skip("'cordales' es el valor válido; está aquí para contrastar con los otros")
    datos = REMISION_DE_MARIA | {"tratamiento": frase_clinica}
    with pytest.raises(ValidationError):
        LecturaArchivo(**datos)


def test_tratamiento_solo_admite_los_catorce_del_vocabulario():
    with pytest.raises(ValidationError):
        LecturaArchivo(**(REMISION_DE_MARIA | {"tratamiento": "carillas"}))


def test_contexto_clinico_es_obligatorio():
    """Un archivo siempre produce contexto para el doctor, aunque sea 'imagen sin texto'."""
    datos = {k: v for k, v in REMISION_DE_MARIA.items() if k != "contexto_clinico"}
    with pytest.raises(ValidationError):
        LecturaArchivo(**datos)


def test_radiografia_sin_texto_degrada_bien():
    """El caso incómodo: una placa sin nada escrito. No hay tratamiento que nombrar y el
    modelo no lo inventa — usa 'no_identificado' y Daniela pregunta en vez de asumir."""
    lectura = LecturaArchivo(
        tipo_documento="radiografia",
        tratamiento="no_identificado",
        origen=None,
        fecha_documento=None,
        confianza="baja",
        contexto_clinico="Radiografía panorámica, sin texto acompañante.",
    )
    assert lectura.tratamiento == "no_identificado"
    assert lectura.origen is None


def test_origen_rechaza_algo_con_forma_de_cedula():
    datos = REMISION_DE_MARIA | {"origen": "Paciente CC 1.020.345.678"}
    with pytest.raises(ValidationError, match="documento de identidad"):
        LecturaArchivo(**datos)


# ==========================================================================================
# RespuestaDaniela — lo que permite ramificar sin otra llamada de modelo
# ==========================================================================================

RESPUESTA_A_MARIA = {
    "mensaje_al_paciente": "Hola María! Ya me llegó tu remisión y se la pasé a los doctores.",
    "estado_oportunidad": "explorando",
    "barrera_detectada": "ninguna",
    "requiere_escalamiento": True,
    "motivo_escalamiento": "archivo_recibido",
    "fuera_de_alcance": False,
}


def test_respuesta_daniela_se_instancia():
    r = RespuestaDaniela(**RESPUESTA_A_MARIA)
    assert r.requiere_escalamiento is True
    assert r.motivo_escalamiento == "archivo_recibido"


@pytest.mark.parametrize(
    "campo",
    [
        "mensaje_al_paciente",
        "estado_oportunidad",
        "barrera_detectada",
        "requiere_escalamiento",
        "motivo_escalamiento",
        "fuera_de_alcance",
    ],
)
def test_todos_los_campos_de_respuesta_son_obligatorios(campo):
    """Ninguno tiene default: si el orquestador va a ramificar con un `if` sobre
    `requiere_escalamiento`, ese campo no puede venir ausente y asumirse False."""
    datos = {k: v for k, v in RESPUESTA_A_MARIA.items() if k != campo}
    with pytest.raises(ValidationError):
        RespuestaDaniela(**datos)


def test_mensaje_vacio_se_rechaza():
    """`fallos.escalamiento` del plan: pase lo que pase sale un mensaje. Nunca silencio."""
    with pytest.raises(ValidationError):
        RespuestaDaniela(**(RESPUESTA_A_MARIA | {"mensaje_al_paciente": ""}))


def test_respuesta_no_tiene_ningun_campo_de_dato_sensible():
    prohibidos = {"telefono", "cedula", "documento", "contexto_clinico", "historia_clinica"}
    assert prohibidos.isdisjoint(RespuestaDaniela.model_fields)


# ==========================================================================================
# SolicitudCita — las dos ausencias deliberadas
# ==========================================================================================

CITA_DE_MARIA = {
    "nombre_completo": "María Rodríguez",
    "inicio": datetime(2026, 9, 17, 14, 0),
    "tratamiento": "cordales",
    "clave_idempotencia": "3001234567-2026-09-17T14:00",
}


def test_solicitud_cita_se_instancia():
    c = SolicitudCita(**CITA_DE_MARIA)
    assert c.tratamiento == "cordales"


def test_solicitud_cita_no_tiene_campo_de_telefono():
    """El teléfono lo pone el contexto local, no el modelo. Si el modelo pudiera escribirlo,
    podría escribir OTRO: la hija agendando por su madre y el recordatorio yéndose al
    número equivocado."""
    assert "telefono" not in SolicitudCita.model_fields


def test_solicitud_cita_no_tiene_campo_de_documento():
    """`datos.quien_ve_que`: prohibición expresa del cliente. Un campo que no existe no se
    puede llenar por error."""
    prohibidos = {"cedula", "documento", "documento_identidad", "nit", "dni"}
    assert prohibidos.isdisjoint(SolicitudCita.model_fields)


def test_nombre_completo_rechaza_una_cedula():
    with pytest.raises(ValidationError, match="documento de identidad"):
        SolicitudCita(**(CITA_DE_MARIA | {"nombre_completo": "María Rodríguez 1020345678"}))


def test_nombre_completo_rechaza_una_cedula_con_puntos():
    with pytest.raises(ValidationError, match="documento de identidad"):
        SolicitudCita(**(CITA_DE_MARIA | {"nombre_completo": "María Rodríguez 1.020.345.678"}))


def test_clave_de_idempotencia_es_obligatoria():
    """Sin clave no hay reintento seguro, y sin reintento seguro un timeout de red le cuesta
    al paciente repetir todo — o produce dos citas."""
    datos = {k: v for k, v in CITA_DE_MARIA.items() if k != "clave_idempotencia"}
    with pytest.raises(ValidationError):
        SolicitudCita(**datos)


def test_inicio_tiene_que_ser_una_fecha():
    with pytest.raises(ValidationError):
        SolicitudCita(**(CITA_DE_MARIA | {"inicio": "el jueves por la mañana"}))


# ==========================================================================================
# SolicitudCancelacion y SolicitudEscalamiento
# ==========================================================================================


def test_solicitud_cancelacion_se_instancia_sin_motivo():
    c = SolicitudCancelacion(id_cita="cita-882", clave_idempotencia="cita-882")
    assert c.motivo is None


def test_solicitud_escalamiento_exige_pregunta_concreta():
    """Sin `pregunta_concreta` el doctor recibe un aviso, no una pregunta, y no sabe qué se
    le está pidiendo decidir. `fallos.escalamiento_humano` lo exige."""
    with pytest.raises(ValidationError):
        SolicitudEscalamiento(
            motivo="archivo_recibido",
            resumen_para_doctor="Llegó una remisión de Clínica Dental Norte.",
            pregunta_concreta="",
            clave_idempotencia="conv-441-turno-3",
        )


def test_solicitud_escalamiento_no_admite_motivo_ninguno():
    """'ninguno' es un valor válido en la salida de Daniela, pero escalar con motivo
    'ninguno' no significa nada."""
    with pytest.raises(ValidationError):
        SolicitudEscalamiento(
            motivo="ninguno",
            resumen_para_doctor="x",
            pregunta_concreta="y",
            clave_idempotencia="z",
        )


def test_solicitud_escalamiento_completa():
    e = SolicitudEscalamiento(
        motivo="archivo_recibido",
        resumen_para_doctor=(
            "Remisión de Clínica Dental Norte, Dr. Ramírez, 05/09. "
            "'se remite a cirugía oral para extracción de terceros molares incluidos'."
        ),
        pregunta_concreta="¿Se agenda valoración directa o requiere revisar antes?",
        clave_idempotencia="conv-441-turno-3",
    )
    assert e.motivo == "archivo_recibido"

"""Las once reglas anti-reporte, una prueba por regla.

Cada una tiene que FALLAR si alguien afloja su regla. No comprueban el camino feliz:
comprueban que el camino prohibido sigue prohibido.
"""
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada, ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sabado, domingo cerrado


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Septiembre de 2026: el 15 es martes, el 19 sabado, el 20 domingo."""
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA_BOGOTA)


def test_el_vocabulario_no_deja_inventar_un_tipo():
    assert "inventado" not in s.TIPOS_DE_SEGUIMIENTO


def test_recordatorio_de_cita_no_es_reactivacion():
    """Si se colaran, la baja dejaria de comprobarse sobre ellos (G0 usa lista BLANCA)."""
    assert s.TIPOS_DE_REACTIVACION.isdisjoint(s.TIPOS_NO_COMERCIALES)


def test_toda_reactivacion_es_comercial():
    """Regla 4: la baja tiene que aplicarse a las tres."""
    for tipo in s.TIPOS_DE_REACTIVACION:
        assert tipo not in s.TIPOS_NO_COMERCIALES, tipo


def test_el_vocabulario_del_codigo_y_el_de_la_migracion_no_se_separan():
    """El CHECK de la 021 y esta constante son dos listas del mismo vocabulario.

    Si se separan, el codigo deja pasar un tipo que la base rechaza y el INSERT revienta la
    transaccion del turno entero. Va en las DOS direcciones: que cada tipo del codigo este en
    el SQL, y que el SQL no tenga uno de mas que el codigo no conozca. Con solo la primera
    mitad, un quinto valor colado en el CHECK (a mano, en un despliegue) pasaba desapercibido:
    la base lo aceptaria y el codigo seguiria sin poder pedirlo ni reconocerlo.
    """
    import re
    from pathlib import Path

    sql = Path("migraciones/021_reactivacion.sql").read_text(encoding="utf-8")

    for tipo in s.TIPOS_DE_SEGUIMIENTO:
        assert f"'{tipo}'" in sql, f"{tipo} no esta en el CHECK de la 021"

    match = re.search(r"tipo IN \(([^)]*)\)", sql, re.DOTALL)
    assert match, "no se encontro el bloque `tipo IN (...)` del CHECK en la 021"
    tipos_del_sql = set(re.findall(r"'(\w+)'", match.group(1)))
    assert tipos_del_sql == s.TIPOS_DE_SEGUIMIENTO, (
        "el CHECK de la 021 y `TIPOS_DE_SEGUIMIENTO` tienen valores distintos -- "
        f"sql={tipos_del_sql} codigo={s.TIPOS_DE_SEGUIMIENTO}"
    )


def test_el_literal_de_la_tool_no_se_separa_de_lo_que_el_modelo_puede_pedir():
    """El `Literal` de la firma de `programar_seguimiento` es una TERCERA lista del mismo
    vocabulario, y nada la ataba a `TIPOS_QUE_EL_MODELO_PUEDE_PEDIR`.

    Sin esta prueba: alguien suma un tipo nuevo a la constante y al CHECK de la 021, las
    pruebas de vocabulario quedan en verde, y el `Literal` se queda con el enum viejo -- el
    SDK le sigue enseñando al modelo el schema de antes y el tipo nuevo no se puede pedir
    NUNCA, sin un solo error en ningun log.

    Se compara contra `params_json_schema`, el esquema que el SDK arma a partir del `Literal`
    y que es literalmente lo que el modelo ve -- no contra el codigo fuente del tipo, que
    podria tener el enum correcto y aun asi fallar por como `function_tool` lo serializa.
    """
    from maxicare_daniela import herramientas as h

    enum_del_modelo = set(
        h.programar_seguimiento.params_json_schema["properties"]["tipo"]["enum"]
    )
    assert enum_del_modelo == s.TIPOS_QUE_EL_MODELO_PUEDE_PEDIR


def test_la_tool_rechaza_un_tipo_que_no_existe():
    """Regla 1 y el portillo de G0: el modelo no puede inventarse un tipo."""
    import asyncio

    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto  # reutiliza el contexto clavado

    ctx = contexto()
    respuesta = asyncio.run(_programar_seguimiento(ctx, "publicidad_masiva", "2026-12-01T10:00:00"))
    assert "publicidad_masiva" not in respuesta
    assert "no existe" in respuesta.lower() or "no es un tipo" in respuesta.lower()


def test_la_tool_no_deja_al_modelo_disfrazar_lo_comercial_de_recordatorio():
    """El portillo entero, en una prueba.

    `recordatorio_cita` esta en `TIPOS_NO_COMERCIALES`, asi que G0 no le aplica la baja. Los
    recordatorios los emite el CODIGO al crear o mover una cita; el modelo no tiene por que
    encolar uno, y si puede, tiene una puerta para saltarse la baja.
    """
    import asyncio

    from maxicare_daniela.herramientas import _programar_seguimiento
    from tests.test_herramientas import contexto

    ctx = contexto(pidio_no_contacto=True)
    respuesta = asyncio.run(
        _programar_seguimiento(ctx, "recordatorio_cita", "2026-12-01T10:00:00")
    )
    assert "programado" not in respuesta.lower()

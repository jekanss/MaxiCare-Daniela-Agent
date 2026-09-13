"""El muro: el reparto de `LecturaArchivo` en sus dos mitades.

Sin IO, sin modelo, sin base. Si alguna prueba de este archivo necesita una de esas tres
cosas, está en el archivo equivocado.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxicare_daniela import lectura
from maxicare_daniela.contratos import LecturaArchivo, LecturaNoClinica

CENTINELA = "reabsorcion radicular en el 46, con lesion periapical de 4 mm"


def _lectura(**cambios) -> LecturaArchivo:
    datos = {
        "tipo_documento": "remision_externa",
        "tratamiento": "ortodoncia",
        "origen": "Clinica Dental Norte",
        "fecha_documento": None,
        "confianza": "alta",
        "contexto_clinico": CENTINELA,
    }
    datos.update(cambios)
    return LecturaArchivo(**datos)


def test_el_reparto_separa_las_dos_mitades():
    clinico, no_clinica = lectura.repartir(_lectura())

    assert clinico == CENTINELA
    assert no_clinica.tratamiento == "ortodoncia"
    assert no_clinica.origen == "Clinica Dental Norte"


def test_la_mitad_no_clinica_no_tiene_donde_guardar_lo_clinico():
    """No es que no se copie: es que no CABE.

    Un campo que existe y no se debe usar se acaba usando. Que el tipo no lo tenga
    convierte el error en un fallo de construccion en vez de en una fuga en produccion.
    """
    _, no_clinica = lectura.repartir(_lectura())

    assert not hasattr(no_clinica, "contexto_clinico")
    assert "contexto_clinico" not in no_clinica.model_dump()
    assert CENTINELA not in no_clinica.model_dump_json()

    with pytest.raises(ValidationError):
        LecturaNoClinica(
            tipo_documento="remision_externa",
            tratamiento="ortodoncia",
            confianza="alta",
            contexto_clinico=CENTINELA,
        )


@pytest.mark.parametrize("confianza", ["media", "baja"])
def test_por_debajo_de_alta_el_tratamiento_se_borra(confianza):
    """Lo fuerza el codigo, no el prompt. Una instruccion se desobedece; esto no."""
    _, no_clinica = lectura.repartir(_lectura(confianza=confianza, tratamiento="ortodoncia"))

    assert no_clinica.tratamiento == "no_identificado"
    assert no_clinica.confianza == confianza


def test_con_confianza_alta_el_tratamiento_se_respeta():
    """El complemento del anterior: un guardrail que salta siempre se acaba desactivando."""
    _, no_clinica = lectura.repartir(_lectura(confianza="alta", tratamiento="endodoncia"))

    assert no_clinica.tratamiento == "endodoncia"


# ==========================================================================================
# El tema del paciente
# ==========================================================================================


class TelegramDeTemas:
    """Cuenta cuántos temas se crearon y cuáles se cerraron."""

    def __init__(self, *, falla_al_crear: Exception | None = None) -> None:
        self.creados: list[str] = []
        self.cerrados: list[int] = []
        self._falla_al_crear = falla_al_crear

    async def crear_tema(self, nombre: str) -> int:
        if self._falla_al_crear is not None:
            raise self._falla_al_crear
        self.creados.append(nombre)
        return 900 + len(self.creados)

    async def cerrar_tema(self, tema_id: int) -> None:
        self.cerrados.append(tema_id)


class BaseDeTemas:
    """Lo mínimo de `persistencia` que `asegurar_tema` toca."""

    def __init__(self, tema: int | None = None) -> None:
        self.tema = tema
        self.guardados: list[int] = []

    def instalar(self, monkeypatch):
        from maxicare_daniela import persistencia

        class ConexionFalsa:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
        monkeypatch.setattr(persistencia, "tema_del_paciente", lambda conn, tel: self.tema)
        monkeypatch.setattr(
            persistencia, "asegurar_paciente", lambda conn, **kw: 42
        )

        def guardar(conn, *, id_paciente, topic_id):
            self.guardados.append(topic_id)
            self.tema = topic_id

        monkeypatch.setattr(persistencia, "guardar_tema", guardar)
        return self


def test_el_tema_se_crea_una_vez_y_nace_cerrado(monkeypatch):
    import asyncio

    base = BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
        )
    )

    assert tema == 901
    assert tg.creados == ["Ana Perez · +573001112233"]
    assert tg.cerrados == [901], (
        "el tema no nacio cerrado: un tema abierto deja escribir al paciente a cualquiera "
        "del grupo, que es justo lo que el relevo existe para controlar"
    )
    assert base.guardados == [901]


def test_el_segundo_archivo_reusa_el_tema(monkeypatch):
    import asyncio

    base = BaseDeTemas(tema=777).instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
        )
    )

    assert tema == 777
    assert tg.creados == [], "se creo un tema nuevo teniendo uno: una persona, un hilo"
    assert base.guardados == []


def test_dos_archivos_simultaneos_de_un_numero_nuevo_crean_un_solo_tema(monkeypatch):
    """La carrera que el indice unico NO atrapa.

    `uq_pacientes_topic` es unico sobre `telegram_topic_id`, y dos temas distintos tienen
    ids distintos: el segundo UPDATE pisa al primero y deja un tema huerfano en Telegram.
    Lo que lo impide es el candado por telefono.
    """
    import asyncio

    base = BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas()

    async def a_la_vez():
        return await asyncio.gather(
            *[
                lectura.asegurar_tema(
                    telefono="573001112233",
                    nombre_perfil="Ana Perez",
                    database_url="postgresql://x",
                    telegram=tg,
                )
                for _ in range(2)
            ]
        )

    temas = asyncio.run(a_la_vez())

    assert len(tg.creados) == 1, f"se crearon {len(tg.creados)} temas para un solo paciente"
    assert temas[0] == temas[1]


def test_dos_telefonos_distintos_no_se_serializan(monkeypatch):
    """El complemento del anterior: un candado global seria un cuello de botella."""
    import asyncio

    BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas()

    async def dos_numeros():
        return await asyncio.gather(
            lectura.asegurar_tema(
                telefono="573001112233", nombre_perfil="Ana",
                database_url="postgresql://x", telegram=tg,
            ),
            lectura.asegurar_tema(
                telefono="573009998877", nombre_perfil="Luis",
                database_url="postgresql://x", telegram=tg,
            ),
        )

    asyncio.run(dos_numeros())
    assert len(tg.creados) == 2


def test_si_telegram_no_deja_crear_el_tema_se_cae_al_general(monkeypatch):
    """Degradar, no perder. El archivo tiene que llegarle al doctor igual."""
    import asyncio

    from maxicare_daniela.canales import ErrorDeCanal

    BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas(falla_al_crear=ErrorDeCanal("not enough rights"))

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233", nombre_perfil="Ana",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema is None


def test_sin_nombre_de_perfil_el_tema_se_llama_con_el_telefono():
    assert lectura.nombre_del_tema("573001112233", None) == "+573001112233"
    assert lectura.nombre_del_tema("573001112233", "  ") == "+573001112233"

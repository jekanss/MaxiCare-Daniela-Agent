"""El muro: el reparto de `LecturaArchivo` en sus dos mitades.

Sin IO, sin modelo, sin base. Si alguna prueba de este archivo necesita una de esas tres
cosas, está en el archivo equivocado.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxicare_daniela import lectura
from maxicare_daniela.canales import ArchivoDescargado
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

    def __init__(
        self,
        *,
        falla_al_crear: Exception | None = None,
        falla_al_cerrar: Exception | None = None,
    ) -> None:
        self.creados: list[str] = []
        self.cerrados: list[int] = []
        self._falla_al_crear = falla_al_crear
        self._falla_al_cerrar = falla_al_cerrar

    async def crear_tema(self, nombre: str) -> int:
        if self._falla_al_crear is not None:
            raise self._falla_al_crear
        self.creados.append(nombre)
        return 900 + len(self.creados)

    async def cerrar_tema(self, tema_id: int) -> None:
        if self._falla_al_cerrar is not None:
            raise self._falla_al_cerrar
        self.cerrados.append(tema_id)


class BaseDeTemas:
    """Lo mínimo de `persistencia` que `asegurar_tema` toca."""

    def __init__(self, tema: int | None = None) -> None:
        self.tema = tema
        self.guardados: list[int] = []
        self.abiertos: list[bool] = []

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

        def guardar(conn, *, id_paciente, topic_id, abierto=False):
            self.guardados.append(topic_id)
            self.abiertos.append(abierto)
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


def test_si_no_se_puede_cerrar_el_tema_queda_guardado_como_abierto(monkeypatch):
    """Hallazgo 2 de la ronda de arreglo: el log no basta, la base tiene que decir la verdad.

    Si `cerrar_tema` falla, el tema existe en Telegram (no se pierde) pero
    `telegram_topic_abierto` no puede quedar en FALSE: es justo el campo que la fase 6C leerá
    para saber si ese hilo necesita atención.
    """
    import asyncio

    from maxicare_daniela.canales import ErrorDeCanal

    base = BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas(falla_al_cerrar=ErrorDeCanal("not enough rights"))

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233", nombre_perfil="Ana",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema == 901, "el tema existe aunque no se pudo cerrar: no se pierde"
    assert base.guardados == [901]
    assert base.abiertos == [True], "el cierre fallo: la base tiene que decir abierto=True"


def test_un_fallo_de_transporte_al_crear_tambien_cae_al_general(monkeypatch):
    """Hallazgo 3 de la ronda de arreglo.

    No solo `ErrorDeCanal` (Telegram respondió "ok: false") degrada al General: un fallo de
    transporte real -- un timeout, una conexion caida -- tiene que hacer lo mismo. Propagarlo
    tumbaria la entrega del archivo al doctor, que es la garantia de la fase 2.
    """
    import asyncio

    import httpx

    BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas(falla_al_crear=httpx.ConnectError("sin red"))

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233", nombre_perfil="Ana",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema is None


# ==========================================================================================
# El lector
# ==========================================================================================


def _archivo(contenido=b"\x89PNG bytes", mime="image/png", nombre="radio.png"):
    return ArchivoDescargado(contenido=contenido, mime=mime, nombre=nombre)


@pytest.mark.parametrize("tipo", ["image", "document"])
def test_imagen_y_documento_si_se_leen(tipo):
    assert lectura.vale_la_pena_leer(tipo, 1000) is True


@pytest.mark.parametrize("tipo", ["audio", "voice", "video", "sticker", "text"])
def test_lo_demas_no_paga_el_modelo_caro(tipo):
    """`sol` es el modelo caro. Correrlo sobre un sticker es dinero tirado, y sobre una nota
    de voz no funcionaria sin una API de transcripcion que esta fase no incorpora."""
    assert lectura.vale_la_pena_leer(tipo, 1000) is False


def test_un_archivo_enorme_no_va_al_lector():
    assert lectura.vale_la_pena_leer("image", lectura.TOPE_BYTES_LECTOR + 1) is False
    assert lectura.vale_la_pena_leer("image", lectura.TOPE_BYTES_LECTOR) is True


def test_una_imagen_viaja_como_input_image():
    entrada = lectura.entrada_para_el_lector(_archivo(), "image")
    contenido = entrada[0]["content"][0]

    assert contenido["type"] == "input_image"
    assert contenido["image_url"].startswith("data:image/png;base64,")


def test_un_documento_viaja_como_input_file_con_su_nombre():
    """`file_data` lleva el data URL completo, no el base64 pelado.

    Es justo lo que el Paso 1 de esta tarea existía para medir: contra la API real, el
    12/09/2026, el base64 pelado en `file_data` devolvió `400 invalid_value` y el data URL
    completo funcionó. Esta aserción es lo único de la suite que detecta si alguien vuelve
    a mandar el base64 pelado --sin ella, la regresión solo se vería en producción.
    """
    entrada = lectura.entrada_para_el_lector(
        _archivo(contenido=b"%PDF-1.4", mime="application/pdf", nombre="remision.pdf"),
        "document",
    )
    contenido = entrada[0]["content"][0]

    assert contenido["type"] == "input_file"
    assert contenido["filename"] == "remision.pdf"
    assert contenido["file_data"].startswith("data:application/pdf;base64,")


def test_si_el_lector_revienta_devuelve_none_y_no_propaga():
    """El doctor YA tiene el archivo. Un lector caido no puede tumbar nada mas."""
    import asyncio

    async def correr_que_revienta(*a, **kw):
        raise RuntimeError("el modelo no contesto")

    salida = asyncio.run(
        lectura.leer_archivo(_archivo(), tipo="image", correr=correr_que_revienta)
    )
    assert salida is None

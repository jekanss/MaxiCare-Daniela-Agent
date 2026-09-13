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
    """Lo mínimo de `persistencia` que `asegurar_tema` toca.

    `id_paciente=None` es el caso nuevo: un número que NO está registrado como paciente.
    `pacientes_creados` existe para que una prueba pueda afirmar que la ingesta no creó
    ninguno -- se anota en vez de reventar, porque un `raise` aquí lo tragaría el `except`
    de `asegurar_tema` y la prueba pasaría por la razón equivocada.
    """

    def __init__(self, tema: int | None = None, *, id_paciente: int | None = 42) -> None:
        self.tema = tema
        self.id_paciente = id_paciente
        self.guardados: list[int] = []
        self.abiertos: list[bool] = []
        self.id_guardados: list[int] = []
        self.pacientes_creados: list[dict] = []

    def instalar(self, monkeypatch):
        from maxicare_daniela import persistencia

        class ConexionFalsa:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
        monkeypatch.setattr(
            persistencia,
            "buscar_paciente_por_telefono",
            lambda conn, tel: (self.id_paciente, "Ana Perez") if self.id_paciente else None,
        )
        monkeypatch.setattr(persistencia, "tema_del_paciente", lambda conn, tel: self.tema)

        def anotar_creacion(conn, **kw):
            self.pacientes_creados.append(kw)
            return 42

        monkeypatch.setattr(persistencia, "asegurar_paciente", anotar_creacion)

        def guardar(conn, *, id_paciente, topic_id, abierto=False):
            self.guardados.append(topic_id)
            self.abiertos.append(abierto)
            self.id_guardados.append(id_paciente)
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
    assert base.id_guardados == [42], "el tema se colgó de una fila que no es la del paciente"
    assert base.pacientes_creados == [], (
        "la ingesta creó una fila en `pacientes`: eso convierte a un desconocido en paciente "
        "verificado, porque `atencion._leer_estado` deriva la identidad de esa fila"
    )


def test_un_desconocido_no_abre_tema_y_su_archivo_va_al_general(monkeypatch):
    """CRITICO de la revisión final: mandar una foto no puede verificar a nadie.

    `atencion._leer_estado` deriva `identidad_verificada` de la EXISTENCIA de la fila en
    `pacientes`, y `runtime._entregar` corre `procesar_mensaje` ANTES que `atender`. Con
    `asegurar_tema` creando la fila, un número desconocido que mandaba una imagen quedaba
    verificado en ese mismo turno --con el nombre que él mismo puso en su perfil de
    WhatsApp-- y `revisar_identidad` dejaba de protegerlo.

    El tema no se le abre, y el archivo le llega al doctor igual: al General.
    """
    import asyncio

    base = BaseDeTemas(id_paciente=None).instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573009999999",
            nombre_perfil="Nombre Que El Mismo Puso",
            database_url="postgresql://x",
            telegram=tg,
        )
    )

    assert tema is None, "un número sin fila en `pacientes` no tiene hilo propio"
    assert tg.creados == [], (
        "se creó el tema ANTES de comprobar que el paciente existe: eso deja un tema "
        "huérfano en Telegram al que nadie volverá a escribir"
    )
    assert base.pacientes_creados == [], (
        "la ingesta creó la fila del desconocido: esa fila ES la identidad verificada"
    )
    assert base.guardados == []


def test_el_camino_de_la_ingesta_no_nombra_asegurar_paciente():
    """La aserción que impide que esto vuelva por otro sitio.

    No basta con que `asegurar_tema` ya no cree pacientes: la fuga vuelve en cuanto alguien
    escriba la llamada un poco más arriba o un poco más abajo. `asegurar_paciente` tiene un
    solo uso legítimo --las tools, donde el paciente se identifica de verdad-- y ninguno en
    el camino por el que entra un archivo.
    """
    import inspect

    from maxicare_daniela import ingesta

    for modulo in (lectura, ingesta):
        fuente = inspect.getsource(modulo)
        assert "asegurar_paciente" not in fuente, (
            f"`{modulo.__name__}` vuelve a crear pacientes: mandar un archivo no puede "
            "convertir a un desconocido en paciente verificado"
        )


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


def test_el_lector_no_sube_el_contenido_clinico_a_los_traces(monkeypatch):
    """La CUARTA salida del muro, y la unica que sale de la clinica sin que nadie la vea.

    Comprobado contra la 0.22.2 instalada: `RunConfig()` nace con
    `trace_include_sensitive_data=True`. Sin un `run_config` explicito, cada archivo que
    manda un paciente subia a la plataforma de OpenAI el data URL entero de su radiografia Y
    el `LecturaArchivo` completo, con el `contexto_clinico` dentro. `config.py` ya decia por
    que eso importa; lo que faltaba era cablear la constante.
    """
    import asyncio
    from types import SimpleNamespace

    from maxicare_daniela.config import TRACE_INCLUDE_SENSITIVE_DATA, WORKFLOW_NAME

    capturado: dict = {}

    class RunnerFalso:
        @staticmethod
        async def run(agente, entrada, **kw):
            capturado["agente"] = agente
            capturado.update(kw)
            return SimpleNamespace(final_output=_lectura())

    monkeypatch.setattr(lectura, "Runner", RunnerFalso)

    salida = asyncio.run(lectura.leer_archivo(_archivo(), tipo="image"))

    assert salida is not None, "el doble del Runner no llego a correr"
    assert "run_config" in capturado, (
        "`Runner.run` se llamo SIN run_config: el SDK usa sus defaults y sube el contenido "
        "clinico a los traces, que se exportan fuera de la clinica"
    )
    run_config = capturado["run_config"]
    assert run_config.trace_include_sensitive_data is False, (
        "EL MURO SE CAYO POR LA CUARTA SALIDA: el contexto clinico y la radiografia entera "
        "acaban en los traces de OpenAI"
    )
    assert run_config.trace_include_sensitive_data is TRACE_INCLUDE_SENSITIVE_DATA
    assert run_config.workflow_name == WORKFLOW_NAME
    # Y los spans se siguen creando: apagar el tracing entero costaria la latencia, el coste
    # y los errores del lector, que es justo lo que se quiere seguir viendo.
    assert run_config.tracing_disabled is False


def test_si_el_lector_revienta_devuelve_none_y_no_propaga():
    """El doctor YA tiene el archivo. Un lector caido no puede tumbar nada mas."""
    import asyncio

    async def correr_que_revienta(*a, **kw):
        raise RuntimeError("el modelo no contesto")

    salida = asyncio.run(
        lectura.leer_archivo(_archivo(), tipo="image", correr=correr_que_revienta)
    )
    assert salida is None


# ==========================================================================================
# leer_y_repartir: el reparto entero, con el canal de Telegram de por medio
# ==========================================================================================


class TelegramQueCaptura:
    """Que texto llego a `enviar_mensaje` Y A QUE TEMA.

    El `tema_id` se guarda porque tirarlo dejaba un agujero medido: cambiando el destino de
    la lectura al tema General, la suite entera seguia en verde. El contenido clinico de un
    paciente acabaria en el hilo donde miran todos los doctores y nadie se enteraria.
    """

    def __init__(self) -> None:
        #: (texto, tema_id)
        self.mensajes: list[tuple[str, int | None]] = []

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None) -> int:
        self.mensajes.append((texto, tema_id))
        return 1


def test_el_contexto_clinico_se_escapa_antes_de_ir_a_telegram():
    """Hallazgo 2 de la ronda de arreglo.

    `canales.py` manda todo con `parse_mode: "HTML"`. Un `contexto_clinico` con `<` o `&`
    sin escapar --«canal < 2 mm & pieza #46»-- le devuelve a Telegram un 400, y ese 400 lo
    traga el `except` de `leer_y_repartir`: el archivo llega igual, pero la lectura clinica
    se pierde en silencio. Esta prueba vigila que el texto que sale ya vino escapado.
    """
    import asyncio
    from types import SimpleNamespace

    async def correr_con_html(*a, **kw):
        return SimpleNamespace(
            final_output=_lectura(contexto_clinico="canal < 2 mm & pieza #46")
        )

    tg = TelegramQueCaptura()

    no_clinica = asyncio.run(
        lectura.leer_y_repartir(
            _archivo(), tipo="image", telegram=tg, tema_id=901, correr=correr_con_html
        )
    )

    assert no_clinica is not None
    assert len(tg.mensajes) == 1
    texto, tema = tg.mensajes[0]
    assert "canal &lt; 2 mm &amp; pieza #46" in texto, (
        f"el contexto clinico llego sin escapar a un canal HTML: {texto!r}"
    )
    assert tema == 901, (
        "la lectura clinica se mando a un tema que no es el del paciente: en el General la "
        "verian todos los doctores mezclada con la de los demas"
    )


def test_la_lectura_va_al_tema_que_le_dieron_y_no_al_general():
    """El complemento del anterior, sin HTML de por medio: el destino ES la aserción.

    Medido por mutación en la revisión final: con `tema_id=tema_general` en `ingesta.py` las
    393 pruebas seguían en verde, porque ningún doble offline miraba a qué tema iba nada.
    """
    import asyncio
    from types import SimpleNamespace

    async def correr(*a, **kw):
        return SimpleNamespace(final_output=_lectura())

    tg = TelegramQueCaptura()

    asyncio.run(
        lectura.leer_y_repartir(
            _archivo(), tipo="image", telegram=tg, tema_id=777, correr=correr
        )
    )

    assert [tema for _, tema in tg.mensajes] == [777]
    assert CENTINELA in tg.mensajes[0][0]


def test_una_lectura_fallida_avisa_al_tema_del_paciente_tambien():
    """Hasta el «no se pudo leer» es de ese paciente: al General no le dice nada a nadie."""
    import asyncio

    async def correr_que_revienta(*a, **kw):
        raise RuntimeError("el modelo no contesto")

    tg = TelegramQueCaptura()

    salida = asyncio.run(
        lectura.leer_y_repartir(
            _archivo(), tipo="image", telegram=tg, tema_id=777, correr=correr_que_revienta
        )
    )

    assert salida is None
    assert [tema for _, tema in tg.mensajes] == [777]

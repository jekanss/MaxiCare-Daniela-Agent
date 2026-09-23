"""El muro: el reparto de `LecturaArchivo` en sus dos mitades.

Sin IO, sin modelo, sin base. Si alguna prueba de este archivo necesita una de esas tres
cosas, está en el archivo equivocado.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxicare_daniela import agentes, lectura
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

    def __init__(
        self,
        tema: int | None = None,
        *,
        id_paciente: int | None = 42,
        perdido: bool = False,
    ) -> None:
        self.tema = tema
        self.id_paciente = id_paciente
        #: ¿A este número le BORRARON el hilo? (`persistencia.hilo_perdido`, migración 030.)
        #: Es lo que separa «nunca tuvo» de «se lo quitaron», y de eso depende que un mensaje
        #: del paciente pueda o no volver a abrirle uno.
        self.perdido = perdido
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
        # La lápida de la migración 030. `self.perdido` es False en el caso normal --nunca
        # tuvo hilo-- y las pruebas que van del hilo BORRADO lo ponen a mano.
        monkeypatch.setattr(persistencia, "hilo_perdido", lambda conn, tel: self.perdido)

        def anotar_creacion(conn, **kw):
            self.pacientes_creados.append(kw)
            return 42

        monkeypatch.setattr(persistencia, "asegurar_paciente", anotar_creacion)

        def guardar(conn, *, telefono, topic_id, abierto=False):
            self.guardados.append(topic_id)
            self.abiertos.append(abierto)
            self.id_guardados.append(telefono)
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
    assert base.id_guardados == ["573001112233"], "el hilo se ató a otro número"
    assert base.pacientes_creados == [], (
        "la ingesta creó una fila en `pacientes`: eso convierte a un desconocido en paciente "
        "verificado, porque `atencion._leer_estado` deriva la identidad de esa fila"
    )


def test_un_desconocido_SI_abre_tema_pero_NO_queda_verificado(monkeypatch):
    """La conducta cambió con la migración 014; la propiedad de seguridad NO.

    Hasta entonces esta prueba decía lo contrario --«un desconocido no abre tema»-- y tenía
    razón mientras el hilo fuera una columna de `pacientes`: `atencion._leer_estado` deriva
    `identidad_verificada` de la EXISTENCIA de esa fila, y `runtime._entregar` corre
    `procesar_mensaje` ANTES que `atender`, así que abrirle hilo a alguien lo verificaba en
    ese mismo turno, con el nombre que él mismo se puso en su perfil de WhatsApp.

    Lo que cambió no es el criterio: es dónde vive el hilo. Ahora está en `temas_telegram`,
    atado al teléfono, así que **abrir un hilo ya no crea una ficha ni verifica a nadie**, y
    eso es exactamente lo que sigue comprobando la última aserción de aquí.

    El precio de la conducta vieja estaba medido en producción (14/09/2026, primer relevo
    real): las radiografías del lead caían en el General, sus textos no se archivaban en
    ninguna parte y el hilo que le abría el botón nacía vacío.
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

    assert tema == 901, "un lead sin ficha se quedó sin hilo: sus archivos irían al General"
    assert tg.cerrados == [901], "el hilo de un desconocido tiene que nacer cerrado igual"
    assert base.guardados == [901]
    # LA QUE NO SE PUEDE TOCAR. Si esto cae, mandar una foto vuelve a verificar a un
    # desconocido y `identidad_antes_de_datos` deja de protegerlo.
    assert base.pacientes_creados == [], (
        "abrir el hilo creó la fila del desconocido: esa fila ES la identidad verificada"
    )


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

    `temas_telegram.topic_id` es UNIQUE, y dos temas distintos tienen ids distintos: el
    segundo `ON CONFLICT` pisa al primero y deja un tema huerfano en Telegram, al que nadie
    volvera a escribir. Lo que lo impide es el candado por telefono, no la base.
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
    # Desde la fase 7: sin que nadie le pase un group_id, el lector no se inventa uno.
    assert run_config.group_id is None


def test_el_lector_corre_bajo_el_group_id_que_le_dan():
    """Sin esto el trace agrupado tiene un agujero justo en los turnos con archivo, que son
    los más interesantes de leer."""
    corrida = lectura._config_de_corrida("conv-con-radiografia")

    assert corrida.group_id == "conv-con-radiografia"
    assert corrida.trace_metadata["version_prompt"] == agentes.VERSION_PROMPT_LECTOR
    assert corrida.trace_include_sensitive_data is False


def test_sin_conversacion_viva_el_lector_no_se_inventa_un_grupo():
    """El primer mensaje de un paciente nuevo PUEDE traer un archivo, y en ese instante la
    conversación todavía no existe: la crea `atencion._leer_estado`, después de que el lector
    ya arrancó. Ese trace queda fuera del grupo, y queda fuera A PROPÓSITO: inventarle un
    `group_id` que no corresponde a ninguna conversación es peor que no tenerlo.
    """
    corrida = lectura._config_de_corrida(None)

    assert corrida.group_id is None
    assert corrida.trace_include_sensitive_data is False


def test_leer_y_repartir_reenvia_el_group_id_hasta_el_runner(monkeypatch):
    """El cable completo, no solo `_config_de_corrida` en aislamiento.

    `test_el_lector_corre_bajo_el_group_id_que_le_dan` llama a `_config_de_corrida` directo:
    prueba la función, no el cableado. `test_el_lector_no_sube_el_contenido_clinico_a_los_
    traces` sí atraviesa `leer_archivo` de verdad, pero SIN `group_id` (queda `None` porque
    nadie se lo pasa). Ninguna de las dos nota si alguien borra el `group_id=group_id` del
    lambda de `leer_archivo`, el reenvío de `group_id` en `leer_y_repartir`, o si dejan de
    pasarle `correr=None` explícitamente -- las tres formas de romper este cable dejaban
    (antes de esta prueba) la suite entera en verde.

    Por eso aquí NO se pasa `correr`: se dobla `Runner` entero, igual que en
    `test_el_lector_no_sube_el_contenido_clinico_a_los_traces`, para que el lambda por
    defecto de `leer_archivo` sea el que de verdad corra y construya el `run_config`.
    """
    import asyncio
    from types import SimpleNamespace

    capturado: dict = {}

    class RunnerFalso:
        @staticmethod
        async def run(agente, entrada, **kw):
            capturado.update(kw)
            return SimpleNamespace(final_output=_lectura())

    monkeypatch.setattr(lectura, "Runner", RunnerFalso)
    tg = TelegramQueCaptura()

    no_clinica = asyncio.run(
        lectura.leer_y_repartir(
            _archivo(), tipo="image", telegram=tg, tema_id=777, group_id="conv-x",
        )
    )

    assert no_clinica is not None, "el doble del Runner no llego a correr"
    assert "run_config" in capturado
    assert capturado["run_config"].group_id == "conv-x", (
        "el group_id no llego hasta el Runner: se rompio el cable entre leer_y_repartir, "
        "leer_archivo y _config_de_corrida"
    )


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

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
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


# ==========================================================================================
# 6H · La ficha: el doctor lee esto de pie, entre dos pacientes
# ==========================================================================================
#
# El cliente vio en produccion lo que el esqueleto del plan producia: veinte lineas de prosa
# clinica corrida, con lo decisivo --que le piden, que antecedente frena-- enterrado en la
# mitad. La forma la ponen dos sitios a la vez, y tienen que estar de acuerdo:
#
#     INSTRUCCIONES_LECTOR      dice que rotulos escribir
#     ROTULOS_DE_LA_FICHA       dice cuales resaltar
#
# Si se separan, la ficha sale plana y NADA se rompe: por eso hay una prueba que los ata.


FICHA = (
    "Remision externa · Medicina interna · Clinica Dental Norte · 12/09/2026\n"
    "Motivo: fatiga, cefalea y mareo recurrentes hace tres semanas.\n"
    "Hallazgos: estable en la valoracion inicial.\n"
    "Antecedentes: apendicectomia 2012. Sin alergias conocidas.\n"
    "Piden: valoracion por medicina interna y estudios complementarios.\n"
    "Ojo: el documento se declara borrador, sin validez medica."
)


def test_la_ficha_resalta_el_rotulo_y_NO_el_contenido_clinico():
    """Poner en negrita el contenido seria decidir que es importante dentro de lo clinico, y
    eso lo decide el doctor. El codigo solo marca los cinco rotulos, que son suyos."""
    salida = lectura.formatear_para_el_doctor(FICHA)

    for rotulo in lectura.ROTULOS_DE_LA_FICHA:
        assert f"<b>{rotulo}:</b>" in salida, f"el rotulo {rotulo!r} salio sin resaltar"

    # Y lo que sigue al rotulo va tal cual, fuera de la negrita.
    assert "<b>Motivo:</b> fatiga, cefalea y mareo recurrentes hace tres semanas." in salida
    assert "<b>Piden:</b> valoracion por medicina interna" in salida


def test_la_cabecera_va_entera_en_negrita_y_hace_de_titulo():
    """Sustituye al «📄 Lectura» de la 6B: dice que clase de documento es, de quien y de
    cuando, que es infinitamente mas util que la palabra «Lectura» -- y no gasta el renglon
    extra que en un celular empuja lo decisivo fuera de la pantalla."""
    salida = lectura.formatear_para_el_doctor(FICHA)

    assert salida.startswith(
        "<b>Remision externa · Medicina interna · Clinica Dental Norte · 12/09/2026</b>"
    )


def test_el_resaltado_va_DESPUES_de_escapar():
    """El unico orden que funciona. Al reves, `html.escape` convertiria nuestras propias
    etiquetas en texto visible (`&lt;b&gt;Motivo:&lt;/b&gt;`) y el doctor leeria el HTML."""
    salida = lectura.formatear_para_el_doctor("Cabecera\nHallazgos: canal < 2 mm & pieza #46")

    assert "<b>Hallazgos:</b> canal &lt; 2 mm &amp; pieza #46" in salida


def test_el_contenido_clinico_NO_puede_inyectar_HTML():
    """La otra mitad de ese orden, y la razon de que las etiquetas las ponga el CODIGO y no
    el modelo: lo que venga dentro del campo es dato, nunca marcado. Un `<a href>` que pasara
    entero convertiria la ficha de un paciente en un enlace clicable puesto por el documento.
    """
    salida = lectura.formatear_para_el_doctor(
        'Cabecera\nMotivo: <a href="http://ejemplo.invalido">mira esto</a>'
    )

    assert "<a" not in salida
    assert "&lt;a href=" in salida


def test_un_rotulo_que_el_lector_se_invente_no_se_resalta_pero_TAMPOCO_se_pierde():
    """Degradar bien: la linea se lee igual, solo mas plana. Tragarsela seria perder texto
    clinico porque el modelo eligio otra palabra."""
    salida = lectura.formatear_para_el_doctor("Cabecera\nDiagnostico presuntivo: bruxismo")

    assert "<b>Diagnostico presuntivo:</b>" not in salida
    assert "Diagnostico presuntivo: bruxismo" in salida


def test_un_parrafo_corrido_pasa_ENTERO_aunque_el_modelo_ignore_el_formato():
    """El formato es una instruccion, y una instruccion puede desobedecerse. Cuando eso pase
    la ficha sale fea, que es un problema de lectura; tragarse el texto seria clinico."""
    corrido = "Documento identificado como BORRADOR FICTICIO. Remision para valoracion."
    salida = lectura.formatear_para_el_doctor(corrido)

    assert corrido in salida


def test_las_lineas_en_blanco_se_caen():
    """Una ficha de seis lineas separadas por blancos ocupa once y deja de caber."""
    salida = lectura.formatear_para_el_doctor("Cabecera\n\n\nMotivo: dolor\n   \nPiden: cita")

    assert salida == "<b>Cabecera</b>\n<b>Motivo:</b> dolor\n<b>Piden:</b> cita"


def test_el_prompt_del_lector_enumera_los_MISMOS_rotulos_que_resalta_el_codigo():
    """Lo que ata los dos sitios. Renombrar un rotulo en el prompt --«Piden» por
    «Solicitan»-- deja la ficha funcionando y sin resaltar, o sea que el sintoma es que se
    ve fea: nadie lo relacionaria nunca con este cambio.
    """
    for rotulo in lectura.ROTULOS_DE_LA_FICHA:
        assert f"{rotulo}:" in agentes.INSTRUCCIONES_LECTOR, (
            f"el codigo resalta {rotulo!r} pero el prompt ya no se lo pide al lector"
        )


def test_la_lectura_que_sale_a_telegram_va_formateada_y_conserva_el_emoji():
    """La integracion de las dos piezas: `leer_y_repartir` tiene que llamar al formateador,
    no mandar el campo crudo."""
    import asyncio
    from types import SimpleNamespace

    async def correr(*a, **kw):
        return SimpleNamespace(final_output=_lectura(contexto_clinico=FICHA))

    tg = TelegramQueCaptura()

    asyncio.run(
        lectura.leer_y_repartir(
            _archivo(), tipo="image", telegram=tg, tema_id=777, correr=correr
        )
    )

    texto, tema = tg.mensajes[0]
    assert tema == 777
    assert texto.startswith("📄 <b>Remision externa · Medicina interna")
    assert "<b>Piden:</b>" in texto


# ==========================================================================================
# El rescate: un escalamiento SÍ abre el hilo
# ==========================================================================================
#
# Un texto no abre hilo (no negociable 14) y la razón sigue en pie: cada «hola» de un número
# equivocado estrenaría expediente. Pero entre que el número se queda sin hilo --lo borra
# `/clearstate`, o alguien borra el tema-- y que algo se lo vuelva a abrir, sus textos no se
# archivan en ninguna parte.
#
# Medido en producción el 16/09/2026: cuatro mensajes seguidos de un paciente sin hilo
# --«Quiero sacarme una muela», «Duele mucho?»-- quedaron con `telegram_message_id` NULL y
# no llegaron a ningún sitio. Lo único que el doctor vio de esa persona fue el escalamiento
# en el General, cuyo resumen parafrasea lo que había preguntado: desde fuera se lee como si
# los mensajes del paciente hubieran «caído en el General».
#
# El escalamiento es el filtro correcto para abrir el hilo, y no el texto: un número
# equivocado no hace escalar a Daniela, así que no estrena expediente.


class TelegramQueRecibe(TelegramDeTemas):
    """`TelegramDeTemas` más el envío, que es lo que el rescate mide."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.enviados: list[tuple[str, int | None, bool]] = []

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.enviados.append((texto, tema_id, silencioso))
        return 5000 + len(self.enviados)


def _fila(wamid, texto=None, *, dicho=None, tipo="text", cuando=None):
    """Una fila de `_textos_sin_archivar`, con la forma EXACTA que devuelve su SQL.

    Existe para que las pruebas no escriban tuplas de cinco a mano, y sobre todo para que el
    día que la consulta cambie de forma haya UN sitio que corregir. Desde el 22/09/2026 la
    consulta trae también lo que llegó sin texto --un audio, una radiografía--, porque el
    General dejó de ser el destino de reserva y sin esto desaparecerían del expediente.
    """
    return (wamid, texto, dicho, tipo, cuando)


def _sin_pendientes(monkeypatch, pendientes):
    """Dobla las dos consultas que el rescate hace sobre `mensajes_entrantes`."""
    marcados: list[tuple[tuple[str, ...], int]] = []
    monkeypatch.setattr(lectura, "_textos_sin_archivar", lambda url, tel: list(pendientes))
    monkeypatch.setattr(
        lectura,
        "_marcar_archivados",
        lambda url, wamids, mid: marcados.append((tuple(wamids), mid)),
    )
    return marcados


def test_al_escalar_sin_hilo_se_abre_y_se_vuelca_lo_que_el_paciente_habia_escrito(monkeypatch):
    """El caso medido: los cuatro mensajes tienen que acabar en el expediente."""
    import asyncio

    BaseDeTemas(tema=None).instalar(monkeypatch)
    tg = TelegramQueRecibe()
    marcados = _sin_pendientes(monkeypatch, [
        _fila("wamid-1", "Hola buenas noches"),
        _fila("wamid-2", "Quiero sacarme una muela"),
        _fila("wamid-3", "Duele mucho?"),
    ])

    tema = asyncio.run(
        lectura.rescatar_hilo(
            telefono="573001112233", nombre_perfil="Jean",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema == 901, "tiene que haber abierto el hilo"
    assert len(tg.enviados) == 1, "un solo mensaje con todo, no un timbre por frase"
    texto, tema_id, silencioso = tg.enviados[0]
    assert tema_id == 901
    assert silencioso is True, "el hilo es un expediente: se deposita mudo"
    for frase in ("Hola buenas noches", "Quiero sacarme una muela", "Duele mucho?"):
        assert frase in texto
    assert texto.index("Hola buenas noches") < texto.index("Duele mucho?"), "en orden"
    assert marcados == [(("wamid-1", "wamid-2", "wamid-3"), 5001)], (
        "cada mensaje volcado queda marcado, o el siguiente escalamiento lo repetiría"
    )


def test_sin_nada_pendiente_el_rescate_no_escribe_nada(monkeypatch):
    """Lo que hace idempotente al rescate: ya volcado es `telegram_message_id` NO nulo, así
    que la consulta deja de devolverlo y el segundo escalamiento no repite el volcado."""
    import asyncio

    BaseDeTemas(tema=777).instalar(monkeypatch)
    tg = TelegramQueRecibe()
    marcados = _sin_pendientes(monkeypatch, [])

    tema = asyncio.run(
        lectura.rescatar_hilo(
            telefono="573001112233", nombre_perfil="Jean",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema == 777
    assert tg.enviados == []
    assert marcados == []
    assert tg.creados == [], "ya tenía hilo: no se abre otro"


def test_si_el_hilo_no_se_puede_abrir_el_rescate_se_traga_el_fallo(monkeypatch):
    """Nunca propaga. Lo llama un escalamiento, y un escalamiento que revienta por no haber
    podido archivar un «buenas tardes» deja a un paciente sin doctor."""
    import asyncio

    from maxicare_daniela.canales import ErrorDeCanal

    BaseDeTemas(tema=None).instalar(monkeypatch)
    tg = TelegramQueRecibe(falla_al_crear=ErrorDeCanal("Telegram caido"))
    marcados = _sin_pendientes(monkeypatch, [_fila("wamid-1", "Duele mucho?")])

    tema = asyncio.run(
        lectura.rescatar_hilo(
            telefono="573001112233", nombre_perfil="Jean",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema is None
    assert tg.enviados == [], "sin hilo no hay dónde volcar"
    assert marcados == [], "y nada se marca como archivado: sigue pendiente"


def test_el_volcado_escapa_el_html_del_paciente(monkeypatch):
    """`enviar_mensaje` va en `parse_mode=HTML` y RECHAZA el mensaje entero si no cierra.
    Un paciente que escriba «me duele el <3» dejaría el rescate en nada."""
    import asyncio

    BaseDeTemas(tema=None).instalar(monkeypatch)
    tg = TelegramQueRecibe()
    _sin_pendientes(monkeypatch, [_fila("wamid-1", "me duele el <3 & la muela")])

    asyncio.run(
        lectura.rescatar_hilo(
            telefono="573001112233", nombre_perfil="Jean",
            database_url="postgresql://x", telegram=tg,
        )
    )

    texto = tg.enviados[0][0]
    assert "&lt;3" in texto and "&amp;" in texto
    assert "<3" not in texto


# ==========================================================================================
# La lápida (migración 030): quién puede rehacer un hilo borrado
#
# MaxiCare, 22/09/2026: «Si el doctor borró el tema: no lo recrees por la llegada de mensajes
# ni uses el General como destino alternativo. Conserva ese contexto en el sistema para
# recuperarlo cuando corresponda.» Estas cuatro pruebas son esa frase, partida en sus cuatro
# afirmaciones comprobables.
# ==========================================================================================


def test_con_lapida_y_sin_permiso_NO_se_crea_ningun_tema(monkeypatch):
    """Lo que pide `ingesta`: a este número le borraron el hilo, así que no se toca Telegram.

    Sin esto, la foto que el paciente manda diez minutos después de que un doctor cerrara su
    tema le abre uno nuevo, y el gesto del doctor no significa nada.
    """
    import asyncio

    base = BaseDeTemas(tema=None, perdido=True).instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
            rehacer_si_lo_borraron=False,
        )
    )

    assert tema is None
    assert tg.creados == [], "se creó un tema sobre una lápida"
    assert base.guardados == []


def test_con_lapida_pero_CON_permiso_el_escalamiento_SI_lo_rehace(monkeypatch):
    """El «hasta que» de la regla. Si esto se rompiera, borrar un tema dejaría al paciente
    sin expediente para siempre y al doctor sin sitio donde atenderlo."""
    import asyncio

    base = BaseDeTemas(tema=None, perdido=True).instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
        )
    )

    assert tema == 901, "el escalamiento tiene que poder rehacer el hilo"
    assert base.guardados == [901]


def test_SIN_lapida_el_primer_archivo_sigue_abriendo_el_hilo(monkeypatch):
    """El control que impide que esto se convierta en «los archivos ya no abren hilo».

    Un número que NUNCA tuvo hilo no ha perdido nada, así que su primer archivo le estrena
    el expediente igual que siempre --incluso con `rehacer_si_lo_borraron=False`--. La
    distinción entre las dos ausencias es justo lo que la migración 030 vino a dar.
    """
    import asyncio

    base = BaseDeTemas(tema=None, perdido=False).instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
            rehacer_si_lo_borraron=False,
        )
    )

    assert tema == 901
    assert base.guardados == [901]


def test_rescatar_hilo_pide_el_tema_SIN_restricciones(monkeypatch):
    """`rescatar_hilo` corre dentro de un escalamiento, así que la lápida no puede frenarlo.

    Se comprueba el argumento y no el resultado: la prueba de arriba ya fija qué hace
    `asegurar_tema` con el default, y lo que aquí importa es que el escalamiento no herede
    por descuido la restricción que `ingesta` sí pide.
    """
    import asyncio

    BaseDeTemas(tema=None, perdido=True).instalar(monkeypatch)
    _sin_pendientes(monkeypatch, [])
    pedidos: list[dict] = []
    original = lectura.asegurar_tema

    async def espiar(**kw):
        pedidos.append(kw)
        return await original(**kw)

    monkeypatch.setattr(lectura, "asegurar_tema", espiar)

    asyncio.run(
        lectura.rescatar_hilo(
            telefono="573001112233", nombre_perfil="Jean",
            database_url="postgresql://x", telegram=TelegramQueRecibe(),
        )
    )

    assert pedidos, "el rescate no llegó a pedir el hilo"
    assert pedidos[0].get("rehacer_si_lo_borraron", True) is True


# ==========================================================================================
# El volcado, ahora con los archivos dentro
#
# Es la compensación de que el General dejara de ser destino de reserva: lo que llegó sin
# hilo ya no lo ve nadie en el momento, así que el escalamiento tiene que contarlo. Los bytes
# no vuelven -- el enlace de Meta caduca -- y por eso lo que baja es la CONSTANCIA.
# ==========================================================================================


def test_el_volcado_cuenta_los_archivos_que_llegaron_sin_hilo(monkeypatch):
    """Sin esto, el doctor abre el expediente y no hay ni rastro de la radiografía."""
    import asyncio
    from datetime import datetime

    BaseDeTemas(tema=None).instalar(monkeypatch)
    tg = TelegramQueRecibe()
    _sin_pendientes(monkeypatch, [
        _fila("w-1", "Buenas, tengo una duda"),
        _fila("w-2", tipo="image", cuando=datetime(2026, 9, 22, 19, 13)),
        _fila("w-3", dicho="me duele al masticar", tipo="audio"),
    ])

    asyncio.run(
        lectura.rescatar_hilo(
            telefono="573001112233", nombre_perfil="Jean",
            database_url="postgresql://x", telegram=tg,
        )
    )

    texto = tg.enviados[0][0]
    assert "Buenas, tengo una duda" in texto
    assert "19:13" in texto and "una imagen" in texto, (
        f"el archivo no dejó constancia en el volcado: {texto!r}"
    )
    assert "me duele al masticar" in texto, "la transcripción tiene que bajar entera"
    assert texto.index("Buenas") < texto.index("19:13") < texto.index("me duele"), "en orden"


def test_la_transcripcion_baja_MARCADA_y_no_como_si_la_hubiera_escrito_el_paciente():
    """Mismo criterio que la migración 027: `texto` es lo que ESCRIBIÓ y `transcripcion` es
    lo que una máquina entendió que dijo. «El 46» y «el 40» suenan casi igual, y quien lee
    una frase clínica tiene derecho a saber de cuál de las dos se fía."""
    escrito, dicho = lectura.pendientes_legibles([
        _fila("w-1", "me duele el 46"),
        _fila("w-2", dicho="me duele el 46", tipo="audio"),
    ])

    assert escrito == "me duele el 46", "lo escrito baja tal cual"
    assert dicho != escrito and "🎙️" in dicho and "«me duele el 46»" in dicho


def test_un_tipo_de_archivo_desconocido_no_se_queda_mudo_en_el_volcado():
    """Degrada diciendo el tipo crudo. Callarlo sería perder la única señal de que llegó."""
    (linea,) = lectura.pendientes_legibles([_fila("w-1", tipo="contacts")])

    assert "contacts" in linea


def test_el_volcado_escapa_tambien_lo_que_se_DIJO():
    """La transcripción la escribe un modelo a partir de lo que dijo un desconocido, así que
    tiene exactamente el mismo problema de HTML que el texto -- y hasta hoy no pasaba por
    ningún `escape` porque no bajaba al volcado."""
    (linea,) = lectura.pendientes_legibles([_fila("w-1", dicho="menos de 3 < 5 & ya", tipo="audio")])

    assert "&lt;" in linea and "&amp;" in linea
    assert "< 5" not in linea

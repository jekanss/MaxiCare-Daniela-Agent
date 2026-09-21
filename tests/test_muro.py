"""EL ENTREGABLE de la fase 6B, literal del plan-agentes.json:

    «Una prueba manda una remisión, captura TODO lo que entra a `Runner.run(daniela)` y
    comprueba que el contenido clínico no aparece ahí, mientras sí aparece en el mensaje
    enviado a Telegram.»

Si esta prueba se borra o se debilita, la fase deja de estar cerrada.

------------------------------------------------------------------------------------------
Qué corre de verdad aquí, y qué se dobla
------------------------------------------------------------------------------------------

Se doblan **el modelo y la red**, y nada más:

  * `lectura.leer_archivo` — es la llamada al lector. Devuelve la lectura que esta prueba
    dicta, con la centinela dentro de `contexto_clinico`. Parchear aquí y no más arriba es
    deliberado: así `repartir` y el envío a Telegram de `leer_y_repartir` son los de verdad.
  * El modelo de `daniela` — un `ModeloGuionizado`, cuyo `.recibido` ES el «TODO lo que
    entra a `Runner.run(daniela)`» del entregable: instrucciones, tools y entrada.
  * `guardrails._preguntar` — los dos evaluadores son llamadas a un modelo.
  * WhatsApp, Telegram y las funciones de `ingesta` y `persistencia` que tocan Neon.

Lo que NO se dobla, porque es justo lo que se está probando: el reparto
(`lectura.repartir`), el envío de la mitad clínica (`lectura.leer_y_repartir`), la recogida
de la tarea (`atencion._recoger_lecturas`) y la composición de la entrada del modelo
(`atencion._entrada_para_el_modelo`). Una prueba que doblara `leer_y_repartir` no probaría
el muro: probaría el doble.

Síncrona y con `asyncio.run`, como el resto de la suite: no hay `pytest-asyncio`. El camino
entero corre en UN solo bucle de eventos porque la `Task` del lector nace en
`procesar_mensaje` y se recoge dentro de `atender`; partirlo en dos `asyncio.run` dejaría la
tarea huérfana en un bucle ya cerrado.
"""

from __future__ import annotations

import asyncio
import json

from agents import RunConfig

from maxicare_daniela import agentes, atencion, conversacion, ingesta
from maxicare_daniela import guardrails as g
from maxicare_daniela import lectura as lectura_mod
from maxicare_daniela.calendario import CalendarioDoble
from maxicare_daniela.canales import ArchivoDescargado
from maxicare_daniela.config import WORKFLOW_NAME
from maxicare_daniela.contratos import LecturaArchivo, LecturaNoClinica

from .dobles import ModeloGuionizado, responde, respuesta_daniela
from .test_atencion import BaseFalsa, DormirFalso, WhatsAppFalso, config_falso, limpiar_estado

#: La frase que no puede aparecer por accidente. Sin `<`, `>` ni `&` a propósito:
#: `leer_y_repartir` escapa lo clínico con `html.escape` antes de mandarlo a Telegram --ese
#: canal va en HTML-- y un carácter escapado dejaría de encontrarse tal cual en el mensaje.
CENTINELA = "reabsorcion radicular en el 46, con lesion periapical de 4 mm"

TELEFONO = "573001112233"
TEMA_DE_ANA = 901
TEMA_GENERAL = 7

#: Lo que Daniela responde. Sin cifras ni horas: los dos guardrails deterministas de salida
#: son los de producción y una cifra suelta haría saltar `sin_cifra_no_documentada`, con lo
#: que la prueba fallaría por una razón que no tiene nada que ver con el muro.
RESPUESTA = "Ya me llego tu remision, Ana. El doctor la va a revisar y te cuento."


# ==========================================================================================
# Dobles: el archivo, y los dos canales
# ==========================================================================================


class WhatsAppConRemision(WhatsAppFalso):
    """El `WhatsAppFalso` del turno, más la descarga que pide la ingesta.

    Hereda en vez de copiar para que el día que `atencion` le pida algo nuevo a WhatsApp
    esta prueba se entere por el mismo sitio que las demás.
    """

    async def descargar_media(self, media_id, *, nombre_original=None) -> ArchivoDescargado:
        return ArchivoDescargado(
            contenido=b"%PDF-1.4 una remision de verdad no cabe aqui",
            mime="application/pdf",
            nombre=nombre_original or "remision.pdf",
        )


class TelegramDeDoctores:
    """Lo que ven los doctores, y EN QUÉ HILO lo ven.

    `mensajes` guarda `(texto, tema_id)`. El destino no es un detalle: medido por mutación en
    la revisión final, cambiar `tema_id=destino` por `tema_id=tema_general` en `ingesta.py`
    dejaba las 393 pruebas en verde, porque ningún doble offline guardaba el tema. El
    contenido clínico de un paciente se iría al hilo donde miran todos los doctores.

    El pie del archivo se guarda aparte, en `archivos`: si entrara en `mensajes`, la aserción
    de que lo clínico SÍ llegó podría pasar por el pie en vez de por el mensaje de la
    lectura, que es lo que se quiere comprobar.
    """

    def __init__(self) -> None:
        #: (texto, tema_id)
        self.mensajes: list[tuple[str, int | None]] = []
        self.archivos: list[tuple[str, int | None, str]] = []
        self.temas: list[str] = []

    @property
    def textos(self) -> list[str]:
        """Solo el texto, para las aserciones que no miran el destino."""
        return [texto for texto, _ in self.mensajes]

    async def enviar_archivo(self, archivo, *, tipo_whatsapp, pie, tema_id=None, silencioso=False) -> int:
        self.archivos.append((archivo.nombre, tema_id, pie))
        return 10 + len(self.archivos)

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.mensajes.append((texto, tema_id))
        return 20 + len(self.mensajes)

    async def crear_tema(self, nombre: str) -> int:  # pragma: no cover - no debería hacer falta
        self.temas.append(nombre)
        return TEMA_DE_ANA

    async def cerrar_tema(self, tema_id: int) -> None:  # pragma: no cover
        return None


def _remision() -> ingesta.MensajeEntrante:
    """La remisión que el paciente manda: un PDF con un texto normal al lado."""
    return ingesta.MensajeEntrante(
        wamid="wamid-remision-1",
        telefono=TELEFONO,
        nombre_perfil="Ana Perez",
        tipo="document",
        texto="Hola, esta es la remision que me dieron en la otra clinica",
        media_id="media-remision-1",
        mime="application/pdf",
        nombre_archivo="remision.pdf",
    )


def _lo_que_leyo_el_lector() -> LecturaArchivo:
    """La lectura que el modelo habría devuelto: lo clínico y lo administrativo juntos.

    Este objeto es el ÚNICO sitio donde las dos mitades conviven. A partir de aquí el
    código las separa, y esta prueba existe para comprobar que no se vuelven a juntar.
    """
    return LecturaArchivo(
        tipo_documento="remision_externa",
        tratamiento="ortodoncia",
        origen="Clinica Dental Norte",
        fecha_documento=None,
        confianza="alta",
        contexto_clinico=CENTINELA,
    )


# ==========================================================================================
# Montaje
# ==========================================================================================


def _montar(monkeypatch, modelo: ModeloGuionizado) -> BaseFalsa:
    """Deja el proceso sin red y sin base, y a `daniela` hablando con el modelo guionizado.

    Devuelve la `BaseFalsa` porque su `.llamadas` es la otra mitad del muro: ahí queda
    anotada, con sus argumentos, cada llamada que el turno le hace a `persistencia`.
    """
    limpiar_estado()
    # `limpiar_estado` solo vacía el estado de `atencion`. Este candado es de `lectura` y no
    # lo limpia nadie: un `asyncio.Lock` que sobrevive a su bucle de eventos es una bomba de
    # relojería entre pruebas --el día que dos archivos del mismo número entren a la vez,
    # sale un `RuntimeError: is bound to a different event loop` que no se lee como lo que es.
    lectura_mod._candados_de_tema.clear()
    base = BaseFalsa().instalar(monkeypatch)

    # --- Neon, por los dos lados -----------------------------------------------------
    # La ingesta registra el wamid y marca el reenvío; `asegurar_tema` consulta y guarda el
    # tema. Se parchean las funciones que abren conexión, no `asegurar_tema` entera: así el
    # candado por teléfono y la degradación al General siguen siendo los de producción.
    monkeypatch.setattr(ingesta, "_registrar", lambda url, m: True)
    monkeypatch.setattr(ingesta, "_marcar_reenviado", lambda url, w, t, s: None)
    monkeypatch.setattr(ingesta, "_marcar_fallo", lambda url, w, e: None)
    # Ana ya tiene su hilo. Desde la migración 014 eso no depende de que sea paciente: el
    # hilo va por teléfono, y que abrirlo NO verifique a nadie lo prueba `test_lectura.py`.
    monkeypatch.setattr(lectura_mod, "_tema_de", lambda url, tel: TEMA_DE_ANA)
    monkeypatch.setattr(lectura_mod, "_guardar_tema", lambda *a, **kw: None)
    monkeypatch.setattr(conversacion, "_guardar_estado", lambda ctx, resultado: None)

    # --- El lector: lo único que se dobla del camino de la lectura --------------------
    # `**kwargs` y no la firma exacta: al doble le da igual `database_url` o `telefono` --que
    # solo sirven para anotar el consumo-- y clavarlos aquí haría que esta prueba del MURO se
    # cayera cada vez que la instrumentación del lector cambie de parámetros. Lo que este
    # archivo vigila es qué mitad de la lectura sale por dónde, no cómo se le pide.
    async def lector_doblado(archivo, *, tipo, correr=None, group_id=None, **kwargs) -> LecturaArchivo:
        return _lo_que_leyo_el_lector()

    monkeypatch.setattr(lectura_mod, "leer_archivo", lector_doblado)

    # --- Los dos evaluadores: son llamadas a un modelo --------------------------------
    # «No dispara» es lo que hacen en producción cuando el evaluador falla, así que el
    # camino que queda es el mismo. Que el clínico se comporte bien o mal lo prueba
    # `tests/test_guardrails.py`; aquí estorbaría.
    async def no_dispara(evaluador, texto, *, ctx=None):
        return g.Veredicto(False)

    monkeypatch.setattr(g, "_preguntar", no_dispara)

    # --- El modelo de Daniela ---------------------------------------------------------
    # `clone` y no un `Agent` nuevo: el entregable habla de `Runner.run(daniela)`, con sus
    # nueve tools, sus instrucciones de verdad y sus cuatro guardrails enganchados.
    monkeypatch.setattr(conversacion, "agente_daniela", agentes.daniela.clone(model=modelo))

    # --- El tracing, que también es red ------------------------------------------------
    # `conversacion.responder` arma su `RunConfig` cuando no le dan uno, y `atencion` no se
    # lo da: sin esto el SDK intentaría subir el trace y la prueba dejaría de ser offline.
    # No es un doble de `responder` --el de verdad corre entero, guardrails incluidos--,
    # es el mismo `responder` con el trace apagado.
    responder_de_verdad = conversacion.responder

    async def responder_sin_trazas(entrada, **kw):
        kw.setdefault(
            "run_config", RunConfig(workflow_name=WORKFLOW_NAME, tracing_disabled=True)
        )
        return await responder_de_verdad(entrada, **kw)

    monkeypatch.setattr(conversacion, "responder", responder_sin_trazas)
    return base


async def _el_camino_completo(whatsapp, telegram) -> tuple[atencion.Atendido, asyncio.Task]:
    """`procesar_mensaje` → la `Task` del lector → `atender`. El camino de producción.

    Es el mismo par de llamadas que hace `runtime.py` en el webhook, en el mismo orden y
    pasándose lo mismo: `Resultado.lectura` de la primera entra como `lectura=` en la
    segunda.

    Devuelve también la tarea porque su resultado es EL OTRO lado del muro: lo único que el
    lector le entrega a la capa que habla con Daniela.
    """
    mensaje = _remision()
    resultado = await ingesta.procesar_mensaje(
        mensaje,
        whatsapp=whatsapp,
        telegram=telegram,
        database_url="postgresql://no-se-usa/na",
        tema_general=TEMA_GENERAL,
    )
    assert resultado.lectura is not None, "no se arrancó el lector: no hay muro que probar"

    atendido = await atencion.atender(
        mensaje,
        whatsapp=whatsapp,
        telegram=telegram,
        config=config_falso(),
        calendario=CalendarioDoble(),
        dormir=DormirFalso(),
        ventana=0,
        tope=0,
        lectura=resultado.lectura,
        sesion_de=lambda id_: conversacion.SesionEnMemoria(id_),
    )
    return atendido, resultado.lectura


# ==========================================================================================
# EL ENTREGABLE
# ==========================================================================================


def test_el_contenido_clinico_de_una_remision_no_entra_al_contexto_de_daniela(monkeypatch):
    """La prueba del muro, entera.

    Un paciente manda una remisión. El lector la lee. A partir de ahí el documento se parte
    en dos y cada mitad tiene un destinatario distinto:

        contexto_clinico  ──►  Telegram, al tema del paciente   (lo ve el doctor)
        todo lo demás     ──►  el contexto de `daniela`         (lo ve el paciente)

    Lo que se afirma abajo es que esas dos flechas existen Y que van cada una a su sitio.
    """
    modelo = ModeloGuionizado(responde(respuesta_daniela(RESPUESTA)))
    base = _montar(monkeypatch, modelo)

    whatsapp = WhatsAppConRemision()
    tg = TelegramDeDoctores()

    atendido, tarea = asyncio.run(_el_camino_completo(whatsapp, tg))

    # El turno tiene que haber corrido de verdad. Sin esto, un fallo que hiciera a `atender`
    # caer en su mensaje seguro dejaría `recibido` vacío y TODAS las aserciones de abajo
    # pasarían por la razón equivocada: en una lista vacía no aparece nada.
    assert atendido.respondido is True
    assert whatsapp.textos == [RESPUESTA], "Daniela no contestó lo que el guion le dictó"
    assert modelo.recibido, "el modelo de daniela no llegó a correr"

    capturadas = modelo.recibido
    entrada_del_modelo = json.dumps(capturadas, ensure_ascii=False, default=str)

    assert CENTINELA not in entrada_del_modelo, (
        "EL MURO SE CAYO: el contenido clinico del documento llego al contexto de Daniela. "
        "Eso es exactamente el fallo que esta fase existe para impedir."
    )
    assert "reabsorcion" not in entrada_del_modelo.lower()
    assert "periapical" not in entrada_del_modelo.lower()

    enviado_a_telegram = "\n".join(tg.textos)
    assert CENTINELA in enviado_a_telegram, (
        "la otra mitad del muro: el doctor tiene que recibir la lectura completa"
    )

    # Y lo no clinico SI cruzo, o la fase no sirve de nada.
    assert "ortodoncia" in entrada_del_modelo

    # La aserción de arriba es la del plan y se queda tal cual, pero por sí sola es débil:
    # «ortodoncia» también está en la lista de tratamientos que `instrucciones_daniela` le
    # pega al prompt, así que pasaría aunque la lectura no hubiera cruzado nunca. Esta mira
    # SOLO la entrada del turno, que es el único sitio donde la lectura puede haber llegado.
    solo_la_entrada = json.dumps(
        [c["entrada"] for c in capturadas], ensure_ascii=False, default=str
    )
    assert "ortodoncia" in solo_la_entrada, (
        "un muro que ademas bloquea lo que debe pasar es un muro que alguien acabara "
        "quitando: Daniela tiene que saber de que tratamiento es el documento"
    )
    assert "Clinica Dental Norte" in solo_la_entrada, (
        "el origen es administrativo, no clinico: sirve para decir «ya me llego tu remision»"
    )

    # ----------------------------------------------------------------------------------
    # El otro lado del muro: lo que SALE de `leer_y_repartir`
    # ----------------------------------------------------------------------------------
    #
    # Esto no es una aserción de más, es la que sostiene a todas las anteriores. Medido: si
    # `leer_y_repartir` devuelve el `LecturaArchivo` entero --con su `contexto_clinico`--,
    # las cuatro aserciones del entregable siguen pasando, porque
    # `_entrada_para_el_modelo` solo lee `tratamiento` y `origen` y hoy tapa la fuga por
    # casualidad. El día que alguien añada un campo más al prompt, la destapa.
    #
    # El invariante que el docstring de `leer_y_repartir` promete es más fuerte que «no
    # llegó al prompt»: la mitad clínica no sobrevive a esa función. Aquí se comprueba.
    entregado_a_daniela = tarea.result()
    assert isinstance(entregado_a_daniela, LecturaNoClinica), (
        "EL MURO SE CAYO: `leer_y_repartir` devolvio la lectura entera en vez de su mitad "
        f"no clinica (devolvio un {type(entregado_a_daniela).__name__}). Que hoy no llegue "
        "al prompt depende de que nadie toque `_entrada_para_el_modelo`."
    )
    assert CENTINELA not in json.dumps(
        entregado_a_daniela.model_dump(), ensure_ascii=False, default=str
    ), "EL MURO SE CAYO: la mitad que cruza hacia el paciente trae contenido clinico."

    # ----------------------------------------------------------------------------------
    # La tercera salida: Neon
    # ----------------------------------------------------------------------------------
    #
    # La restricción global de la fase dice «`contexto_clinico` no se persiste en ninguna
    # tabla, NUNCA: vive en memoria el tiempo que tarda en salir hacia Telegram». Un
    # `INSERT` no se ve en el prompt ni en el mensaje a Telegram, así que ninguna de las
    # aserciones de arriba lo detectaría. `BaseFalsa.llamadas` guarda cada llamada a
    # `persistencia` CON SUS ARGUMENTOS, y el `repr` alcanza también a un objeto que se
    # pasara entero -- que es como la fuga ocurriría de verdad.
    assert CENTINELA not in str(base.llamadas), (
        "EL MURO SE CAYO: el contenido clinico entro a una escritura en Neon. Lo clinico "
        "vive en memoria el tiempo que tarda en salir hacia Telegram, y ni un segundo mas."
    )


def test_el_archivo_y_su_lectura_van_al_tema_del_paciente(monkeypatch):
    """El destinatario de la mitad clínica no es «Telegram», es el hilo de ESA persona.

    Si la lectura cayera en el General, el contenido clínico de un paciente quedaría en el
    mismo hilo que el de todos los demás. Va con el entregable porque el muro no es solo
    «qué no cruza»: es también a quién llega lo que sí.
    """
    modelo = ModeloGuionizado(responde(respuesta_daniela(RESPUESTA)))
    _montar(monkeypatch, modelo)

    tg = TelegramDeDoctores()
    asyncio.run(_el_camino_completo(WhatsAppConRemision(), tg))

    assert [(nombre, tema) for nombre, tema, _ in tg.archivos] == [
        ("remision.pdf", TEMA_DE_ANA)
    ]
    clinicos = [(texto, tema) for texto, tema in tg.mensajes if CENTINELA in texto]
    assert len(clinicos) == 1, "la lectura clínica se mandó ninguna o más de una vez"
    # Y AL HILO DE ESA PERSONA. Esta es la aserción que faltaba: sin ella, mutar
    # `tema_id=destino` a `tema_id=tema_general` en `ingesta.py` dejaba la suite en verde y
    # la lectura clínica de cada paciente caía en el hilo donde miran todos los doctores.
    assert clinicos[0][1] == TEMA_DE_ANA, (
        f"la lectura clínica fue al tema {clinicos[0][1]} y no al de Ana ({TEMA_DE_ANA}): "
        "el contenido clínico de un paciente quedaría mezclado con el de todos los demás"
    )
    # El aviso al General existe --es donde miran los doctores-- pero no lleva nada clínico.
    # Desde el 13/09/2026 suena UNA vez por tanda y su texto va en plural: avisa de que esa
    # persona mandó archivos, no de cada archivo. Ver NOTA DEL TEXTO SIN TEMA en `ingesta.py`.
    avisos = [(texto, tema) for texto, tema in tg.mensajes if "mandó archivos" in texto]
    assert avisos and CENTINELA not in "\n".join(t for t, _ in avisos)
    assert [tema for _, tema in avisos] == [TEMA_GENERAL], (
        "el aviso tiene que ir al General: es el único sitio donde los doctores miran"
    )

    # El pie viaja pegado al archivo y se compone ANTES de que el lector devuelva nada:
    # dice lo que el archivo es, nunca lo que muestra. Es la garantía de la fase 2 y esta
    # fase no la puede haber aflojado.
    (_, _, pie) = tg.archivos[0]
    assert "Ana Perez" in pie, "el doctor tiene que saber de quién es el archivo"
    assert CENTINELA not in pie and "ortodoncia" not in pie.lower(), (
        "el pie nombra algo que solo se sabe después de leer: alguien esperó al lector y "
        "eso retrasa la entrega del archivo al doctor"
    )

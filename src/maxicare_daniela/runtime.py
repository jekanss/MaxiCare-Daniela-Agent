"""Transporte: lo único que conecta el proyecto con el mundo exterior.

Aquí, y solo aquí, vive el import de un framework web (FastAPI) y —a partir de la fase 4— la
llamada a `Runner.run(...)`. `agentes.py`, `herramientas.py`, `contratos.py` e `ingesta.py`
no importan este módulo; este módulo importa de ellos. Cambiar de canal se hace reescribiendo
este archivo sin tocar un solo agente, tool ni contrato.

------------------------------------------------------------------------------------------
FASE 2 — solo el viaje del archivo
------------------------------------------------------------------------------------------

No hay agente todavía y no es un descuido: `fases[2].justificacion` pone el riesgo primero.
Si un archivo de WhatsApp no se puede recuperar y entregar, el lector de archivos, el muro y
el segundo agente no tienen sentido, y eso no se puede descubrir en la semana ocho.

Lo que sí hay:

    GET  /webhook/whatsapp   verificación del webhook (Meta la pide una vez, al conectar)
    POST /webhook/whatsapp   recepción → descarga → reenvío a Telegram
    GET  /salud              para el VPS y para saber si la configuración quedó completa

Arrancar:  uv run uvicorn maxicare_daniela.runtime:app --host 0.0.0.0 --port 8080

------------------------------------------------------------------------------------------
FASE 6A — y ahora Daniela contesta
------------------------------------------------------------------------------------------

Lo de arriba sigue siendo cierto palabra por palabra: el viaje del archivo no cambió. Lo que
se añade es UNA llamada, `atencion.atender(...)`, DESPUÉS de `ingesta.procesar_mensaje` y en
su propio `try` --ver `_entregar`, donde está explicado por qué ese orden es una garantía y
no una preferencia--. El turno entero vive en `atencion.py`; de este archivo sale la llamada
y las tres cosas que solo el transporte puede aportar: el calendario de producción
(construido una vez al arrancar), el aviso a los doctores (`_avisar_a_doctores`) y el
interruptor `MAXICARE_DANIELA_RESPONDE`, que lo lee `atender`.
"""

from __future__ import annotations

import asyncio
import html
import logging

from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as HTTPExceptionStarlette

from . import atencion, autenticacion, contratos, conversacion, ingesta, panel, persistencia
from .calendario import CalendarioCaido, CalendarioDoble, calendario_desde_config
from .canales import Telegram, WhatsApp
from .config import Config, cargar_dotenv
from .contratos import ContextoDaniela

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s · %(message)s",
)

# httpx registra cada petición con su URL completa en INFO, y el token del bot de Telegram
# VA DENTRO DE LA URL (`api.telegram.org/bot<TOKEN>/sendMessage`). En el VPS eso acabaría
# escrito en journald, donde lo lee cualquiera con acceso al servidor. Los errores de canal
# ya se registran por nuestra cuenta y sin el token, así que no se pierde nada.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("maxicare.runtime")

cargar_dotenv()
config = Config.desde_entorno()

app = FastAPI(title="MaxiCare · Daniela", version="0.5.0", docs_url=None, redoc_url=None)


@app.exception_handler(HTTPExceptionStarlette)
async def _error_en_el_vocabulario_del_frontend(request: Request, exc: HTTPExceptionStarlette):
    """`api.ts` lee `detalle`; FastAPI (y Starlette, para el 404 y el 405 que no pasan por
    una `HTTPException` nuestra) serializan `detail`. Sin esto, ningún mensaje del servidor
    llega a la pantalla: todo cae al texto genérico de `pedir()`."""
    return JSONResponse(
        {"detalle": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


#: Los campos de los cuerpos del panel, con el nombre que usa quien llena el formulario.
#: Sin esta tabla el mensaje diría «clave» o «nota_pendiente», que son nombres de columna.
_NOMBRE_DEL_CAMPO = {
    "clave": "la clave del tratamiento",
    "etiqueta": "el nombre visible del tratamiento",
    "tratamiento": "el tratamiento de la ficha",
    "concepto": "el concepto de la ficha",
    "contenido": "el contenido de la ficha",
    "nota_pendiente": "la nota de qué falta por definir",
    "usuario": "el usuario",
    "contrasena": "la contraseña",
    "mensaje": "el mensaje",
}


def _en_castellano(error: dict) -> str:
    """Un error de Pydantic dicho como se lo diría una persona a otra."""
    campo = next(
        (p for p in reversed(error.get("loc", ())) if isinstance(p, str) and p != "body"), ""
    )
    nombre = _NOMBRE_DEL_CAMPO.get(campo) or (f"«{campo}»" if campo else "uno de los datos")
    ctx = error.get("ctx") or {}
    tipo = error.get("type", "")
    # `str.capitalize()` no sirve: pasa a minúsculas TODO lo demás, y el nombre de un campo
    # desconocido llega entre comillas angulares.
    en_mayuscula = nombre[:1].upper() + nombre[1:]

    if tipo == "missing":
        frase = f"Falta {nombre}."
    elif tipo == "string_too_short" and ctx.get("min_length") == 1:
        frase = f"{en_mayuscula} no puede ir vacío."
    elif tipo == "string_too_short":
        frase = f"{en_mayuscula} necesita al menos {ctx['min_length']} caracteres."
    elif tipo == "string_too_long":
        frase = f"{en_mayuscula} no puede pasar de {ctx['max_length']} caracteres."
    else:
        frase = f"{en_mayuscula} no tiene un valor válido."

    # La clave es el único campo con una forma que hay que explicar, y ya está explicada en
    # `panel.AYUDA_CLAVE`: la misma frase que da el 400 de `validar_clave` cuando el largo
    # sí pasa y lo que falla son las mayúsculas o los espacios. Repetirla aquí a mano haría
    # que el mismo error de la misma persona se explicara de dos maneras según cuál de los
    # dos validadores lo atrapara primero.
    if campo == "clave":
        frase = f"{frase} {panel.AYUDA_CLAVE}"
    return frase


@app.exception_handler(RequestValidationError)
async def _validacion_en_el_vocabulario_del_frontend(
    request: Request, exc: RequestValidationError
):
    """Lo que rechaza Pydantic ANTES de llegar al endpoint, dicho para una persona.

    `RequestValidationError` no es una `HTTPException`, así que el manejador de arriba no la
    cubre: la mitad de los 422 del servidor seguían saliendo con `detail` y con el volcado
    crudo de Pydantic dentro. `pedir()` lee `detalle` y no lo encontraba, de modo que teclear
    una clave de dos letras --que la pantalla deja pulsar, porque solo exige que no esté
    vacía-- daba «No se pudo completar la operación (422)» en vez del mensaje que alguien
    redactó para ese caso exacto. Lo mismo con un contenido de más de 4000 caracteres.

    Se responden dos frases como mucho, no la lista entera: los cuerpos del panel tienen
    cinco campos y quien los llena arregla de uno en uno.
    """
    errores = exc.errors()
    frases = [_en_castellano(e) for e in errores[:2]]
    return JSONResponse({"detalle": " ".join(frases) or "El servidor no entendió la petición."},
                        status_code=422)


_whatsapp = WhatsApp(config.whatsapp_token, config.whatsapp_phone_number_id)
_telegram = Telegram(config.telegram_bot_token, config.telegram_chat_doctores)

#: El tema General del supergrupo. Se lee una vez al arrancar, no en cada mensaje: es
#: configuración operativa que cambia cuando alguien la cambia, no cada segundo.
_tema_general: int | None = None


@app.on_event("startup")
def _cargar_configuracion_operativa() -> None:
    global _tema_general
    try:
        with persistencia.conectar(config.database_url) as conn:
            operativa = persistencia.leer_configuracion(conn)
        _tema_general = int(operativa.get("telegram_topic_general", 1))
        log.info("configuración operativa cargada · tema General = %s", _tema_general)
    except Exception as e:  # noqa: BLE001 — arrancar sin base es peor que arrancar a ciegas
        # Dejarlo en None manda los mensajes al General por omisión, que es exactamente
        # donde deben ir. Un fallo de base no puede impedir que un archivo llegue al doctor.
        _tema_general = None
        log.error("no se pudo leer la configuración operativa (%s); se usa el General", e)


#: El calendario de la clínica, construido UNA vez al arrancar.
#:
#: `CalendarioGoogle.__init__` hace una lectura real contra Google --es su comprobación de
#: acceso-- así que construirlo por mensaje pagaría esa lectura en cada WhatsApp que entre.
#:
#: `None` hasta que corra el startup. Ese `None` no es un agujero: `atencion.atender` lo
#: interpreta como «no me dieron calendario» y construye el suyo con la misma regla de
#: seguridad (`_calendario_por_defecto`), así que lo peor que puede pasar es pagar la lectura
#: contra Google, nunca agendar contra un doble.
_calendario: Any | None = None


@app.on_event("startup")
def _construir_el_calendario() -> None:
    """Lo mismo que `_cargar_configuracion_operativa`: si falla, se arranca igual.

    Que el webhook no arranque deja a los doctores sin recibir las radiografías de sus
    pacientes, y eso es peor que una Daniela que no puede agendar.

    Lo que NO se hace al fallar es caer a `CalendarioDoble`, aunque sea el objeto que ya
    está importado en este archivo para el chat de pruebas. El doble guarda los eventos en
    un diccionario en memoria y dice que sí a todo: con uno aquí, `crear_cita` tomaría el
    cupo en Neon, «crearía» el evento en el vacío y Daniela le confirmaría la cita al
    paciente. **El paciente llegaría a una clínica donde nadie lo espera**, y sin una sola
    línea roja en el log. `CalendarioCaido` lanza `ErrorDeCalendario` en los cuatro métodos,
    que es exactamente lo que las tools de la fase 3 saben manejar: `crear_cita` libera el
    cupo, no confirma nada y la corrida muere para que el orquestador escale a los doctores.
    """
    global _calendario
    try:
        _calendario = calendario_desde_config(config)
        log.info("calendario listo · %s", type(_calendario).__name__)
    except Exception as e:  # noqa: BLE001 -- ver docstring
        _calendario = CalendarioCaido(motivo=str(e))
        log.error(
            "EL CALENDARIO NO ARRANCÓ (%s): Daniela puede conversar, cotizar y responder, "
            "pero NO va a poder agendar, mover ni cancelar citas. Cada intento va a escalar "
            "a los doctores. Revisa MAXICARE_GOOGLE_SA_B64 y que el calendario esté "
            "compartido con la cuenta de servicio.",
            e,
        )


# ==========================================================================================
# Verificación del webhook — Meta la hace una sola vez, al conectar
# ==========================================================================================


#: Las dos rutas del webhook, y las dos son reales.
#:
#: `/whatsapp` es la que la app de Meta ya tenía configurada y suscrita a `messages` desde
#: antes de que existiera este servidor. `/webhook/whatsapp` es la que describe el plan.
#: Servir ambas evita tocar la consola de Meta —donde un cambio de URL obliga a revalidar y
#: puede dejar la clínica sin recibir mensajes mientras tanto— y deja el camino libre para
#: unificarlas cuando convenga, sin prisa y sin ventana de corte.
RUTAS_WEBHOOK = ("/whatsapp", "/webhook/whatsapp")


@app.get(RUTAS_WEBHOOK[0])
@app.get(RUTAS_WEBHOOK[1])
async def verificar(request: Request) -> Response:
    """Meta llama con `hub.challenge` y espera ese mismo número como texto plano.

    Si se devuelve JSON, o el token no coincide, la consola de Meta dice solo «no se pudo
    validar la URL» sin más detalle, así que los logs de abajo son lo único que permite
    saber cuál de las dos cosas pasó.
    """
    parametros = request.query_params
    modo = parametros.get("hub.mode")
    token = parametros.get("hub.verify_token")
    challenge = parametros.get("hub.challenge", "")

    if modo == "subscribe" and token == config.whatsapp_verify_token:
        log.info("webhook verificado por Meta")
        return Response(content=challenge, media_type="text/plain")

    log.warning(
        "verificación rechazada · modo=%r · el token %s",
        modo,
        "no coincide con MAXICARE_WHATSAPP_VERIFY_TOKEN" if token else "no vino en la petición",
    )
    return Response(status_code=403, content="forbidden", media_type="text/plain")


# ==========================================================================================
# Recepción
# ==========================================================================================


@app.post(RUTAS_WEBHOOK[0])
@app.post(RUTAS_WEBHOOK[1])
async def recibir(request: Request, tareas: BackgroundTasks) -> Response:
    """Responde 200 de inmediato y trabaja después.

    Meta espera unos pocos segundos; si no llega el 200, reintenta. Descargar un archivo y
    subirlo a Telegram tarda más que eso, así que hacerlo antes de responder garantizaría
    reintentos — y cada reintento sería otra copia para el doctor.

    El 200 no dice «lo entregué»: dice «lo recibí». Lo que pasó después se consulta en la
    tabla `mensajes_entrantes`, que es donde vive la verdad.
    """
    crudo = await request.body()

    if not ingesta.firma_valida(
        crudo, request.headers.get("x-hub-signature-256"), config.whatsapp_app_secret
    ):
        # La URL es pública. Sin esta puerta, cualquiera que la adivine puede hacer aparecer
        # en el grupo de los doctores un «archivo de paciente» que nadie mandó.
        log.warning("POST con firma inválida desde %s", request.client.host if request.client else "?")
        return Response(status_code=403, content="firma invalida", media_type="text/plain")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        log.warning("POST con cuerpo que no es JSON; se acepta para que Meta no reintente")
        return Response(status_code=200, content="ok", media_type="text/plain")

    mensajes = ingesta.extraer_mensajes(payload)
    if not mensajes:
        # Acuses de entrega y otros eventos que no son mensajes de pacientes. Llegan
        # constantemente y no son un error.
        return Response(status_code=200, content="ok", media_type="text/plain")

    for m in mensajes:
        log.info("entra %s de +%s (%s)", m.wamid, m.telefono, m.tipo)
        tareas.add_task(_entregar, m)

    return Response(status_code=200, content="ok", media_type="text/plain")


def _escapar(texto: str) -> str:
    """Telegram va en `parse_mode=HTML`, y estos textos los escribe un modelo a partir de lo
    que dijo un desconocido: un `<` suelto rompe el mensaje entero, que es justo el que el
    doctor necesita leer. `quote=False` deja las comillas en paz -- dentro de un texto no son
    HTML, y escaparlas solo llenaría el aviso de `&#x27;`."""
    return html.escape(texto, quote=False)


def _registrar_escalamiento(
    ctx: ContextoDaniela, motivo: str, resumen: str, pregunta: str
) -> int | None:
    """Sincrónico a propósito: `persistencia` es psycopg. Siempre dentro de `to_thread`."""
    with persistencia.conectar(ctx.database_url) as conn:
        return persistencia.insertar_escalamiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            motivo=motivo,
            resumen=resumen,
            pregunta=pregunta,
            # La clave la arma el orquestador, nunca el modelo. Y es LA MISMA que construye
            # `herramientas._escalar_a_doctores`, que es lo único que hace que este aviso y
            # el de la tool se reconozcan como el mismo escalamiento.
            clave_idempotencia=ctx.clave("escalamiento", ctx.turno_actual),
        )


def _anotar_telegram(ctx: ContextoDaniela, escalamiento_id: int, message_id: int) -> None:
    with persistencia.conectar(ctx.database_url) as conn:
        persistencia.anotar_telegram_en_escalamiento(conn, escalamiento_id, message_id)


async def _avisar_a_doctores(
    ctx: ContextoDaniela, motivo: str, mensaje_al_paciente: str
) -> None:
    """Le cuenta a los doctores, por Telegram, que este turno escaló.

    Se lo pasa `conversacion.responder` como `al_escalar`, y salta en los dos casos en que
    el turno escala: cuando el modelo lo pide en su respuesta estructurada, y cuando el turno
    se rompió solo (dos tripwires seguidos, límite de turnos, una excepción del SDK). En el
    segundo caso el modelo no llamó a ninguna tool, así que este aviso es lo ÚNICO que separa
    a un paciente atascado de un doctor que no se entera.

    -------------------------------------------------------------------------------------
    Lo que no puede hacer: mandar el aviso dos veces
    -------------------------------------------------------------------------------------

    Si el modelo ya llamó a `escalar_a_doctores` en este mismo turno, el doctor ya tiene su
    Telegram con el resumen que escribió Daniela --que es mejor que este-- y el botón de
    relevo. Repetirlo no es ruido inocente: a la cuarta alerta repetida el doctor deja de
    mirarlas, y ahí es donde muere un sistema de escalamiento.

    La defensa es la clave de idempotencia, no un `if`. `insertar_escalamiento` devuelve
    `None` cuando esa clave ya existe, y ese `None` es la respuesta: ese turno ya escaló, no
    se manda nada. Funciona porque las dos rutas --la tool y esta-- arman exactamente la
    misma clave, `ctx.clave("escalamiento", ctx.turno_actual)`, y porque `turno_actual` se
    persiste en Neon (`persistencia.tocar_conversacion`): con el turno congelado en 0 todos
    los mensajes de una conversación compartirían clave y el doctor se enteraría del primer
    escalamiento y de ninguno más.

    No propaga. `conversacion.responder` ya se traga lo que salga de aquí para que un fallo
    avisando al doctor no deje al paciente sin respuesta, pero un error tragado en silencio
    no se puede depurar: por eso queda en el log con el id de la conversación.
    """
    try:
        nombre = ctx.nombre_paciente or "paciente sin identificar"
        # Esta fila solo llega a existir cuando la clave es nueva, es decir, cuando la tool
        # NO escaló este turno: el resumen puede afirmarlo sin más matices.
        resumen = (
            "El turno escaló sin que el modelo llamara a la tool, así que no hay resumen "
            f"suyo. Esto fue lo que se le respondió al paciente: {mensaje_al_paciente}"
        )
        pregunta = "¿Alguien puede revisar esta conversación y retomarla si hace falta?"

        escalamiento_id = await asyncio.to_thread(
            _registrar_escalamiento, ctx, motivo, resumen, pregunta
        )
        if escalamiento_id is None:
            log.info(
                "el turno %s de %s ya estaba escalado; no se repite el aviso",
                ctx.turno_actual,
                ctx.id_conversacion,
            )
            return

        # El texto lo escribe un modelo a partir de lo que dijo un desconocido, y Telegram
        # va en `parse_mode=HTML`: un `<` sin escapar rompe el mensaje entero, que es el que
        # el doctor necesita leer.
        texto = (
            f"<b>Escalamiento · {_escapar(str(motivo))}</b>\n"
            f"{_escapar(nombre)} · +{ctx.telefono_completo}\n\n"
            "Daniela no pudo resolverlo sola.\n\n"
            f"<b>Lo que se le respondió al paciente:</b>\n"
            f"{_escapar(mensaje_al_paciente)}"
        )
        # SIEMPRE al General: es donde los doctores pueden hablar de un caso sin que el
        # paciente lea una palabra.
        message_id = await _telegram.enviar_mensaje(texto, tema_id=ctx.tema_general or _tema_general)

        await asyncio.to_thread(_anotar_telegram, ctx, escalamiento_id, message_id)
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo avisar a los doctores del escalamiento de %s", ctx.id_conversacion)


async def _entregar(m: ingesta.MensajeEntrante) -> None:
    """Se ejecuta después de haber respondido. Nunca lanza: si lanzara, el error se perdería
    en el log del servidor sin dejar rastro consultable. `procesar_mensaje` ya registra el
    fallo en la fila del mensaje.

    Los dos pasos van en `try` SEPARADOS, y el orden no es una preferencia de estilo: es la
    garantía de la fase 2. `procesar_mensaje` es lo que le hace llegar al doctor la
    radiografía que acaba de mandar el paciente; `atender` es la respuesta de Daniela, que
    llama a un modelo, a Neon y a Google. Con los dos en el mismo `try` --o con Daniela
    primero-- cualquier fallo del turno se llevaría por delante la entrega del archivo, y
    nadie se enteraría hasta que un paciente mandara una radiografía urgente.
    """
    try:
        await ingesta.procesar_mensaje(
            m,
            whatsapp=_whatsapp,
            telegram=_telegram,
            database_url=config.database_url,
            tema_general=_tema_general,
        )
    except Exception:  # noqa: BLE001
        log.exception("fallo inesperado entregando %s", m.wamid)

    try:
        await atencion.atender(
            m,
            whatsapp=_whatsapp,
            telegram=_telegram,
            config=config,
            calendario=_calendario,
            al_escalar=_avisar_a_doctores,
        )
    except Exception:  # noqa: BLE001 -- `atender` promete no propagar; esto lo hace cierto
        # Aquí ya no hay nada que salvar para el paciente, pero el archivo YA llegó al
        # doctor: eso es lo que protege el `try` de arriba.
        log.exception("el turno de Daniela reventó para %s", m.wamid)


# ==========================================================================================
# Salud
# ==========================================================================================


@app.get("/salud")
async def salud() -> dict:
    """Dice qué falta, no solo si está vivo.

    Un `{"ok": true}` que no comprueba nada es peor que no tener endpoint: da confianza sin
    respaldo. Este mira de verdad las tres piezas de las que depende la fase.
    """
    estado: dict = {"servicio": "maxicare-daniela", "fase": 5}

    try:
        with persistencia.conectar(config.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mensajes_entrantes")
            estado["mensajes_recibidos"] = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM mensajes_entrantes "
                "WHERE reenviado_en IS NULL AND fallo IS NOT NULL"
            )
            estado["sin_entregar"] = cur.fetchone()[0]
        estado["base_de_datos"] = "ok"
    except Exception as e:  # noqa: BLE001
        estado["base_de_datos"] = f"FALLA: {e}"

    faltantes = [
        nombre
        for nombre, valor in (
            ("MAXICARE_WHATSAPP_TOKEN", config.whatsapp_token),
            ("MAXICARE_WHATSAPP_PHONE_NUMBER_ID", config.whatsapp_phone_number_id),
            ("MAXICARE_WHATSAPP_VERIFY_TOKEN", config.whatsapp_verify_token),
            ("WHATSAPP_APP_SECRET", config.whatsapp_app_secret),
            ("MAXICARE_TELEGRAM_BOT_TOKEN", config.telegram_bot_token),
            ("MAXICARE_TELEGRAM_CHAT_DOCTORES", config.telegram_chat_doctores),
        )
        if not valor
    ]
    estado["configuracion"] = "ok" if not faltantes else {"faltan": faltantes}
    estado["tema_general"] = _tema_general
    return estado


# ==========================================================================================
# ═══ INTERFAZ WEB (fase 5) ═══
# ==========================================================================================
#
# El cascarón del panel de MaxiCare: ingreso, navegación y el chat de pruebas contra la
# Daniela de producción. El frontend es una aplicación de React que se construye aparte
# (`web/`) y de la que aquí solo se sirven los archivos ya compilados.
#
# ------------------------------------------------------------------------------------------
# El webhook de WhatsApp NO depende de nada de esto
# ------------------------------------------------------------------------------------------
#
# Está en producción recibiendo mensajes de pacientes. Si falta `MAXICARE_SECRETO_SESION`, el
# panel web se apaga con un 503 que dice qué hacer, y el webhook sigue funcionando como si
# nada. Hacer que el proceso no arranque sin esa variable habría sido más «limpio» y habría
# dejado a la clínica sin recibir mensajes por una variable que a WhatsApp no le importa.

RUTA_WEB = Path(__file__).resolve().parents[2] / "web" / "dist"

#: Nombre de la cookie de sesión. `HttpOnly` para que ningún JavaScript pueda leerla
#: --incluido el que inyectaría un XSS-- y `SameSite=Lax` para que no viaje en peticiones que
#: origine otro sitio, que es lo que convierte un enlace malicioso en un CSRF.
COOKIE = "maxicare_sesion"

#: El esquema del carril de pruebas. NO es `pruebas`, que usan `probar_tools.py` y
#: `probar_agentes.py` -- y que ambos BORRAN al terminar. Si compartieran nombre, correr una
#: prueba desde la terminal le vaciaría la conversación a quien estuviera usando el chat web.
ESQUEMA_PRUEBAS_WEB = "pruebas_web"

_secreto_sesion = config.secreto_sesion

#: Las conversaciones vivas del chat de pruebas, en memoria del proceso. Se pierden al
#: reiniciar, y está bien: es un carril de pruebas. El contenedor corre con un solo worker
#: (ver el `CMD` del Dockerfile), así que no hay dos procesos que puedan discrepar.
_conversaciones_de_prueba: dict[str, tuple[ContextoDaniela, conversacion.SesionEnMemoria]] = {}

#: Se prepara una sola vez, la primera vez que alguien abre el chat. Hacerlo al arrancar
#: costaría segundos de despliegue por una pantalla que puede que nadie abra ese día.
_esquema_de_pruebas_listo = False


def _url_de_pruebas() -> str:
    """La conexión directa de Neon con `search_path` fijado al esquema del carril de pruebas.

    Dos detalles que ya costaron una tarde y están en las trampas del `CLAUDE.md`:

    1. **Hay que quitarle el `-pooler.` al host.** PgBouncer rechaza `options` como parámetro
       de arranque (`unsupported startup parameter in options: search_path`).
    2. El aislamiento es FÍSICO, no una convención: con `search_path=pruebas_web`, una
       consulta que diga `INSERT INTO citas` no puede tocar `public.citas` ni queriendo.
    """
    directa = config.database_url.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return f"{directa}{sep}options=-csearch_path%3D{ESQUEMA_PRUEBAS_WEB}"


def _preparar_esquema_de_pruebas() -> str:
    """Crea el esquema si falta y le carga la base de conocimiento. Idempotente.

    La base de conocimiento sale de la MISMA semilla que alimenta `public`, así que los
    precios que cotice Daniela en el chat de pruebas son los de verdad. Esa es la mitad que
    importa del carril separado: se lee lo real, no se escribe sobre lo real.
    """
    global _esquema_de_pruebas_listo
    url = _url_de_pruebas()
    if _esquema_de_pruebas_listo:
        return url

    directa = config.database_url.replace("-pooler.", ".")
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ESQUEMA_PRUEBAS_WEB}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)
        persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())

    _esquema_de_pruebas_listo = True
    log.info("esquema '%s' listo para el chat de pruebas", ESQUEMA_PRUEBAS_WEB)
    return url


# ------------------------------------------------------------------------------------------
# Sesión
# ------------------------------------------------------------------------------------------


def _exigir_panel_habilitado() -> str:
    if not _secreto_sesion:
        raise HTTPException(
            status_code=503,
            detail=(
                "El panel web está apagado: falta MAXICARE_SECRETO_SESION. Genera uno con "
                "el comando que sale en scripts/crear_usuario.py o con secrets.token_urlsafe(48)."
            ),
        )
    try:
        return autenticacion.exigir_secreto(_secreto_sesion)
    except autenticacion.SecretoDebil as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


def usuario_actual(request: Request) -> dict:
    """La dependencia que protege cada ruta del panel. 401 si no hay sesión válida.

    Comprueba la firma Y que el usuario siga activo en la base, en cada petición. Lo segundo
    es lo que hace que quitarle el acceso a alguien tenga efecto inmediato: la firma de su
    cookie sigue siendo válida hasta que venza y no hay forma de anularla -- ver el docstring
    de `autenticacion.py`.
    """
    secreto = _exigir_panel_habilitado()
    galleta = request.cookies.get(COOKIE, "")
    usuario = autenticacion.leer_token(galleta, secreto=secreto) if galleta else None
    if not usuario:
        raise HTTPException(status_code=401, detail="No hay sesión.")

    try:
        with persistencia.conectar(config.database_url) as conn:
            fila = persistencia.buscar_usuario(conn, usuario)
    except Exception as e:  # noqa: BLE001
        log.exception("no se pudo verificar la sesión de %s", usuario)
        raise HTTPException(status_code=503, detail="La base de datos no responde.") from e

    if fila is None or not fila["activo"]:
        # Mismo 401 en los dos casos de cara al cliente; en el log sí se distinguen, porque
        # alguien desactivado intentando entrar es una señal y un usuario borrado no.
        log.warning("sesión rechazada de %r (%s)", usuario, "sin acceso" if fila else "no existe")
        raise HTTPException(status_code=401, detail="Tu sesión ya no es válida.")

    return {"usuario": fila["usuario"], "nombre": fila["nombre"], "rol": fila["rol"]}


class Credenciales(BaseModel):
    usuario: str = Field(min_length=1, max_length=120)
    contrasena: str = Field(min_length=1, max_length=512)


@app.post("/api/entrar")
async def entrar(credenciales: Credenciales, respuesta: Response) -> dict:
    """Verifica y pone la cookie. El mismo mensaje para los tres modos de fallo.

    Usuario inexistente, contraseña equivocada y acceso retirado responden exactamente lo
    mismo. Distinguirlos le confirmaría a quien prueba nombres cuáles existen en la clínica.
    """
    secreto = _exigir_panel_habilitado()
    generico = "Usuario o contraseña incorrectos."

    with persistencia.conectar(config.database_url) as conn:
        fila = persistencia.buscar_usuario(conn, credenciales.usuario)
        valido = bool(
            fila
            and fila["activo"]
            and autenticacion.verificar_contrasena(credenciales.contrasena, fila["hash_contrasena"])
        )
        if not valido:
            log.warning("ingreso fallido para %r", credenciales.usuario[:60])
            raise HTTPException(status_code=401, detail=generico)
        persistencia.marcar_acceso(conn, fila["usuario"])

    respuesta.set_cookie(
        COOKIE,
        autenticacion.firmar_token(fila["usuario"], secreto=secreto),
        max_age=autenticacion.DURACION_SESION_SEGUNDOS,
        httponly=True,
        samesite="lax",
        # En producción va detrás de Traefik, que sirve HTTPS. En local se pone
        # MAXICARE_COOKIE_INSEGURA=1 porque http://localhost no acepta una cookie `Secure`.
        secure=not config.permitir_cookie_insegura,
        path="/",
    )
    log.info("entró %s (%s)", fila["usuario"], fila["rol"])
    return {"usuario": fila["usuario"], "nombre": fila["nombre"], "rol": fila["rol"]}


@app.post("/api/salir")
async def salir(respuesta: Response) -> dict:
    """Borra la cookie del navegador. Es todo lo que se puede hacer, y conviene saberlo: el
    token sigue siendo criptográficamente válido hasta que venza."""
    respuesta.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/sesion")
async def sesion(quien: dict = Depends(usuario_actual)) -> dict:
    """Quién está adentro. La usa el frontend al cargar: la cookie es `HttpOnly`, así que el
    JavaScript no puede mirarla y tiene que preguntar."""
    return quien


# ------------------------------------------------------------------------------------------
# Chat de pruebas -- el entregable de la fase 5
# ------------------------------------------------------------------------------------------


class MensajeDePrueba(BaseModel):
    mensaje: str = Field(min_length=1, max_length=4000)
    conversacion: str | None = None


def _contexto_de_prueba(
    quien: dict, id_conversacion: str | None
) -> tuple[ContextoDaniela, conversacion.SesionEnMemoria]:
    """Recupera la conversación viva, o abre una nueva en el carril de pruebas."""
    if id_conversacion and id_conversacion in _conversaciones_de_prueba:
        return _conversaciones_de_prueba[id_conversacion]

    url = _preparar_esquema_de_pruebas()
    # El «teléfono» lleva el usuario dentro. No colisiona con ningún número real, deja claro
    # en la base de dónde salió la fila, y le da a cada persona de la clínica su propia
    # conversación sin que se pisen entre ellas.
    telefono = f"web-{quien['usuario']}"
    with persistencia.conectar(url) as conn:
        nuevo = persistencia.asegurar_conversacion(conn, telefono=telefono, canal="web")

    ctx = ContextoDaniela(
        id_conversacion=nuevo,
        telefono_completo=telefono,
        database_url=url,
        # Un calendario de mentira, y por la misma razón que el esquema aparte: probar
        # «agéndame el martes» no puede crear un evento en el Google Calendar de la clínica.
        calendario=CalendarioDoble(),
        # Sin identificar, igual que un paciente nuevo en WhatsApp. Es lo que permite probar
        # el flujo de identificación, que es donde más se equivoca un prompt.
        identidad_verificada=False,
        tema_general=_tema_general or 0,
        # Sin credenciales de Telegram: `escalar_a_doctores` no puede avisar a nadie desde
        # aquí. Una prueba no le hace sonar el teléfono a un doctor.
        telegram_bot_token="",
        telegram_chat_doctores="",
    )
    try:
        with persistencia.conectar(url) as conn:
            operativa = persistencia.leer_configuracion(conn)
        ctx.capacidad_por_hora = operativa["capacidad_por_hora"]
        ctx.duracion_cita_minutos = operativa["duracion_cita_minutos"]
        ctx.cierre_relevo_minutos = operativa["cierre_relevo_minutos"]
    except Exception:  # noqa: BLE001 -- los defaults del dataclass son los mismos
        log.warning("chat de pruebas sin configuración operativa; se usan los defaults")

    par = (ctx, conversacion.SesionEnMemoria(nuevo))
    _conversaciones_de_prueba[nuevo] = par
    return par


@app.post("/api/pruebas/chat")
async def chat_de_prueba(entrada: MensajeDePrueba, quien: dict = Depends(usuario_actual)) -> dict:
    """Un turno contra la Daniela de producción.

    Es literalmente el mismo objeto `agentes.daniela` que va a atender WhatsApp: las mismas
    nueve tools, las mismas instrucciones, los mismos seis guardrails. Lo único distinto es
    dónde aterriza lo que escribe.
    """
    ctx, sesion_chat = _contexto_de_prueba(quien, entrada.conversacion)

    resultado = await conversacion.responder(
        entrada.mensaje,
        ctx=ctx,
        sesion=sesion_chat,
        # `al_escalar` se queda en None: en el carril de pruebas no hay a quién avisar. El
        # campo `requiere_escalamiento` de la respuesta sí se devuelve y se pinta en pantalla.
    )
    r = resultado.respuesta
    return {
        "conversacion": ctx.id_conversacion,
        "mensaje": r.mensaje_al_paciente,
        "turno": resultado.turno,
        "estado_oportunidad": r.estado_oportunidad,
        "barrera": r.barrera_detectada,
        "requiere_escalamiento": r.requiere_escalamiento,
        "motivo_escalamiento": r.motivo_escalamiento,
        "fuera_de_alcance": r.fuera_de_alcance,
        "tripwires": resultado.tripwires,
        "regenerado": resultado.regenerado,
    }


class ReinicioDePrueba(BaseModel):
    conversacion: str | None = None


@app.post("/api/pruebas/reiniciar")
async def reiniciar_prueba(entrada: ReinicioDePrueba, quien: dict = Depends(usuario_actual)) -> dict:
    """Olvida la conversación. La fila en `pruebas_web` se queda: no estorba y deja rastro de
    qué se probó."""
    if entrada.conversacion:
        _conversaciones_de_prueba.pop(entrada.conversacion, None)
    return {"ok": True}


# ------------------------------------------------------------------------------------------
# Panel: tratamientos y base de conocimiento
# ------------------------------------------------------------------------------------------


def exigir_rol(*roles: str):
    """Dependencia que restringe una ruta a ciertos roles. Devuelve 403, no 404.

    El botón se esconde en el frontend por comodidad, pero el control vive aquí: un botón
    que desaparece no es un control de acceso.
    """
    def comprobar(quien: dict = Depends(usuario_actual)) -> dict:
        if quien["rol"] not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Hace falta permiso de {' o '.join(roles)} para esto.",
            )
        return quien
    return comprobar


def _refrescar_vocabulario(conn) -> None:
    """Deja el vocabulario del proceso igual a la tabla. Se llama al arrancar y después de
    cada cambio, para que no haga falta reiniciar nada."""
    contratos.fijar_vocabulario(panel.vocabulario_activo(conn))


@app.get("/api/tratamientos")
async def api_tratamientos(quien: dict = Depends(usuario_actual)) -> dict:
    with persistencia.conectar(config.database_url) as conn:
        return {"tratamientos": panel.listar_tratamientos(conn)}


class TratamientoNuevo(BaseModel):
    clave: str = Field(min_length=3, max_length=24)
    etiqueta: str = Field(min_length=1, max_length=120)


@app.post("/api/tratamientos")
async def api_crear_tratamiento(
    cuerpo: TratamientoNuevo, quien: dict = Depends(exigir_rol("admin"))
) -> dict:
    try:
        with persistencia.conectar(config.database_url) as conn:
            creado = panel.crear_tratamiento(
                conn, clave=cuerpo.clave, etiqueta=cuerpo.etiqueta, usuario=quien["usuario"]
            )
            _refrescar_vocabulario(conn)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("%s creó el tratamiento %s", quien["usuario"], creado["clave"])
    return creado


class TratamientoCambio(BaseModel):
    etiqueta: str | None = Field(default=None, max_length=120)
    activo: bool | None = None


@app.patch("/api/tratamientos/{clave}")
async def api_cambiar_tratamiento(
    clave: str, cuerpo: TratamientoCambio, quien: dict = Depends(exigir_rol("admin"))
) -> dict:
    try:
        with persistencia.conectar(config.database_url) as conn:
            cambiado = panel.cambiar_tratamiento(
                conn, clave, etiqueta=cuerpo.etiqueta, activo=cuerpo.activo,
                usuario=quien["usuario"],
            )
            _refrescar_vocabulario(conn)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return cambiado


@app.get("/api/conocimiento")
async def api_conocimiento(quien: dict = Depends(usuario_actual)) -> dict:
    with persistencia.conectar(config.database_url) as conn:
        return {"fichas": panel.listar_conocimiento(conn)}


class Ficha(BaseModel):
    tratamiento: str = Field(min_length=1, max_length=60)
    concepto: str = Field(min_length=1, max_length=60)
    contenido: str = Field(min_length=1, max_length=4000)
    aprobado: bool = True
    nota_pendiente: str | None = Field(default=None, max_length=400)


@app.put("/api/conocimiento")
@app.post("/api/conocimiento")
async def api_guardar_ficha(
    ficha: Ficha, quien: dict = Depends(exigir_rol("admin", "doctor"))
) -> dict:
    try:
        with persistencia.conectar(config.database_url) as conn:
            guardada = panel.guardar_ficha(
                conn, tratamiento=ficha.tratamiento, concepto=ficha.concepto,
                contenido=ficha.contenido, aprobado=ficha.aprobado,
                nota_pendiente=ficha.nota_pendiente, usuario=quien["usuario"],
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("%s editó %s/%s", quien["usuario"], ficha.tratamiento, ficha.concepto)
    return guardada


@app.get("/api/historial")
async def api_historial(quien: dict = Depends(usuario_actual)) -> dict:
    with persistencia.conectar(config.database_url) as conn:
        return {"cambios": panel.historial(conn)}


@app.on_event("startup")
def _cargar_vocabulario_de_tratamientos() -> None:
    """Reemplaza los catorce del `Literal` por los tratamientos activos de la tabla, para que
    Daniela (y esta pantalla) hablen del mismo vocabulario sin reiniciar nada.

    Envuelto en `try/except` a propósito: si Neon no responde al encender, el proceso se
    queda con los catorce del `Literal` y **el webhook de WhatsApp sigue vivo**. Una
    excepción sin capturar aquí tumbaría producción por una tabla que solo usa el panel.
    """
    try:
        with persistencia.conectar(config.database_url) as conn:
            _refrescar_vocabulario(conn)
        log.info("vocabulario de tratamientos cargado desde la base")
    except Exception:  # noqa: BLE001
        log.warning(
            "no se pudo leer la tabla de tratamientos al arrancar; se usan los catorce del "
            "Literal. El webhook sigue funcionando.", exc_info=True
        )


# ------------------------------------------------------------------------------------------
# Los archivos del frontend -- SIEMPRE al final
# ------------------------------------------------------------------------------------------
#
# FastAPI resuelve las rutas en el orden en que se declaran, y la de abajo atrapa cualquier
# ruta. Declararla antes se tragaría `/api/...`, `/salud` y el webhook de WhatsApp -- que
# está en producción. Por eso este bloque cierra el archivo.

if (RUTA_WEB / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=RUTA_WEB / "assets"), name="assets")


@app.get("/{ruta_completa:path}")
async def servir_frontend(ruta_completa: str) -> Response:
    """Devuelve `index.html` para cualquier ruta que no sea de la API.

    La navegación del panel va por el hash (`/#/agenda`), así que el servidor solo tiene que
    entregar una página. Se mantiene el 404 de verdad para lo que cuelgue de `/api`: una
    petición de datos que devuelva HTML produce el peor error posible de depurar --un
    `SyntaxError` de JSON en la consola del navegador, sin ninguna pista de cuál fue la ruta
    mal escrita.
    """
    if ruta_completa.startswith("api/"):
        return JSONResponse({"detalle": "No existe esa ruta."}, status_code=404)

    indice = RUTA_WEB / "index.html"
    if not indice.is_file():
        return JSONResponse(
            {
                "detalle": (
                    "La interfaz web no está compilada. Corre: "
                    "cd web && npm install && npm run build"
                )
            },
            status_code=503,
        )
    # Sin caché: el HTML referencia los bundles con hash en el nombre, así que un index.html
    # cacheado apuntaría a archivos que un despliegue nuevo ya borró.
    return FileResponse(indice, headers={"Cache-Control": "no-store"})

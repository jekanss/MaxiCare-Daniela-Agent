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
import hmac
import html
import logging
import time

from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as HTTPExceptionStarlette

from . import (
    atencion,
    autenticacion,
    contratos,
    conversacion,
    ingesta,
    panel,
    persistencia,
    relevo,
    reseteo,
    seguimientos,
)
from .calendario import CalendarioCaido, CalendarioDoble, Jornada, calendario_desde_config
from .canales import Telegram, WhatsApp
from .config import Config, cargar_dotenv, descartar_vacias_de_terceros
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

# Antes de construir nada que hable con OpenAI. En el contenedor las variables las pone
# el `env_file` de Docker, que exporta las vacías, y una `OPENAI_BASE_URL=` vacía rompe
# toda llamada al modelo con un error que parece de red. Ver la función para el detalle.
_vacias = descartar_vacias_de_terceros()
if _vacias:
    log.warning(
        "descartadas del entorno por venir vacías: %s -- una cadena vacía no es un valor, y el cliente de OpenAI la prefiere sobre su propio default",
        ", ".join(_vacias),
    )

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

#: Las dos perillas del relevo, con los mismos defaults que `persistencia.CONFIGURACION`.
#: Se releen al arrancar como el tema General, y por la misma razón: la clínica las cambia
#: desde el panel de vez en cuando, no cada segundo.
_relevo_minutos: dict[str, int] = {"cierre_relevo_minutos": 180, "aviso_relevo_minutos": 120}


@app.on_event("startup")
def _cargar_configuracion_operativa() -> None:
    global _tema_general
    try:
        with persistencia.conectar(config.database_url) as conn:
            operativa = persistencia.leer_configuracion(conn)
        _tema_general = int(operativa.get("telegram_topic_general", 1))
        for clave in _relevo_minutos:
            _relevo_minutos[clave] = int(operativa.get(clave, _relevo_minutos[clave]))
        log.info(
            "configuración operativa cargada · tema General = %s · relevo: aviso a los %s "
            "min, cierre a los %s min",
            _tema_general,
            _relevo_minutos["aviso_relevo_minutos"],
            _relevo_minutos["cierre_relevo_minutos"],
        )
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

#: Y el del chat de pruebas del panel, que es otro y es de mentira a propósito: probar
#: «agéndame el martes» no puede crear un evento en el calendario donde los doctores miran su
#: día. Es el mismo error de categoría que `public` vs `pruebas_web`.
#:
#: **Uno para todo el proceso**, igual que el de arriba. Se construía uno nuevo en cada
#: conversación y eso dejó de ser inofensivo el 14/09/2026, cuando `consultar_citas` empezó a
#: contrastar contra el calendario: un doble recién nacido no tiene ningún evento, así que
#: toda cita del chat web se leería como «borrada de Calendar» y se cancelaría sola. Lo cazó
#: `scripts/probar_tools.py`, que reproducía el mismo patrón.
_CALENDARIO_WEB = CalendarioDoble()


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
    except Exception as e:  # noqa: BLE001 -- ver docstring
        _calendario = CalendarioCaido(motivo=str(e))
        log.error(
            "EL CALENDARIO NO ARRANCÓ (%s): Daniela puede conversar, cotizar y responder, "
            "pero NO va a poder agendar, mover ni cancelar citas. Cada intento va a escalar "
            "a los doctores. Revisa MAXICARE_GOOGLE_SA_B64 y que el calendario esté "
            "compartido con la cuenta de servicio.",
            e,
        )
        return

    # La OTRA mitad de la puerta, y es la que de verdad podía pasar.
    #
    # `calendario_desde_config` NO LANZA cuando faltan las credenciales: devuelve un
    # `CalendarioDoble()` y lo deja en un `warning`. Eso está bien en una máquina de
    # desarrollo y es letal aquí, porque en este proyecto **una variable presente y vacía no
    # es una variable ausente**: el `.env` trae casi todas las claves escritas y sin valor,
    # así que un `MAXICARE_GOOGLE_SA_B64=` en el VPS --un despliegue a medias, un copiado
    # incompleto-- no da error de arranque: da un doble en producción.
    #
    # Con ese doble, `crear_cita` toma el cupo en Neon, «crea» el evento en un diccionario en
    # memoria, `registrar_cita` guarda un `evento_calendar_id` que no existe en ningún
    # calendario, y Daniela le confirma la cita al paciente. **El paciente llega a una
    # clínica donde nadie lo espera**, y el único rastro es un INFO diciendo que el
    # calendario está listo. Por eso aquí el doble se degrada a caído: lanzar es seguro,
    # fingir no lo es.
    if isinstance(_calendario, CalendarioDoble):
        _calendario = CalendarioCaido(
            motivo="faltan MAXICARE_GOOGLE_SA_B64 o MAXICARE_GOOGLE_CALENDAR_ID"
        )
        log.error(
            "EL CALENDARIO NO ESTÁ CONFIGURADO (falta MAXICARE_GOOGLE_SA_B64 o "
            "MAXICARE_GOOGLE_CALENDAR_ID, o están presentes y VACÍAS). Daniela puede "
            "conversar, cotizar y responder, pero NO va a poder agendar: cada intento "
            "escala a los doctores. Se usa un calendario CAÍDO y nunca uno de mentira, "
            "porque el de mentira le confirmaría al paciente una cita que no existe."
        )
        return

    log.info("calendario listo · %s", type(_calendario).__name__)


#: Los `wamid` cuyo turno está corriendo AHORA en este proceso. Existe para una sola cosa:
#: que apagar el servidor espere a que terminen en vez de matarlos a media frase.
_EN_VUELO: set[str] = set()

#: Cuánto se espera a que drenen. Un turno son 20 s de ventana de búfer + el modelo + el
#: retardo humano; 75 s cubren el caso normal con holgura. Tiene que ser MENOR que el
#: `stop_grace_period` del `docker-compose.yml`, o Docker mata el proceso mientras espera y
#: la espera no habrá servido de nada.
SEGUNDOS_PARA_DRENAR = 75.0


async def _esperar_a_que_drenen(limite: float = SEGUNDOS_PARA_DRENAR) -> int:
    """Espera a que no quede ningún turno en vuelo. Devuelve cuántos se quedaron fuera.

    Esto es el arreglo del 13/09/2026. Un despliegue recreó el contenedor mientras un turno
    estaba a medias y el mensaje del paciente quedó SIN respuesta y SIN fallo: no falló, lo
    mataron, y un proceso muerto no escribe su motivo. Con `stop_grace_period` en su valor
    por defecto --10 segundos-- eso no era mala suerte: pasaba en todo despliegue que pillara
    a alguien escribiendo.

    Se espera con un sondeo y no con un `Event` porque lo que se vigila es un conjunto que
    otras corrutinas modifican: el sondeo no puede perderse un cambio ni quedarse colgado si
    alguien olvida avisar.
    """
    fin = time.monotonic() + limite
    while _EN_VUELO and time.monotonic() < fin:
        await asyncio.sleep(0.25)
    return len(_EN_VUELO)


@app.on_event("shutdown")
async def _drenar_turnos_en_vuelo() -> None:
    """Va ANTES de cerrar los pools: un turno que sigue vivo necesita su conexión a Neon."""
    if not _EN_VUELO:
        return
    pendientes = len(_EN_VUELO)
    log.info("apagando: se esperan %d turno(s) en vuelo", pendientes)
    quedaron = await _esperar_a_que_drenen()
    if quedaron:
        # Se dice en voz alta: estos son exactamente los mensajes que el barrido de arranque
        # tendrá que recoger.
        log.error(
            "apagado con %d turno(s) sin terminar: %s", quedaron, ", ".join(sorted(_EN_VUELO))
        )
    else:
        log.info("apagando: los %d turno(s) terminaron", pendientes)


@app.on_event("shutdown")
async def _cerrar_engines_de_persistencia() -> None:
    """Cierra los pools de `SQLAlchemySession` (fase 7) al apagar el servidor.

    Sin esto, el proceso termina con conexiones de Neon abiertas en el pool de sesiones --
    hasta `TAMANO_POOL_SESIONES + DESBORDO_POOL_SESIONES` por cada `(base, esquema)` que se
    haya usado-- que Neon solo libera por su cuenta cuando la TCP muere, no al instante.
    """
    await persistencia.cerrar_engines()


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
        # `_rastreado` y no `_entregar` a secas: apagar el servidor tiene que esperar a esto.
        tareas.add_task(_rastreado, m)

    return Response(status_code=200, content="ok", media_type="text/plain")


# ==========================================================================================
# El webhook de Telegram — la puerta del relevo (6C)
# ==========================================================================================


#: Ruta del webhook de Telegram. La registra `scripts/configurar_webhook_telegram.py`.
#:
#: **No hay `GET` aquí.** Telegram no valida la URL como hace Meta: se la das con `setWebhook`
#: y empieza a mandar `POST`. Un `GET` en esta ruta cae en `servir_frontend` y devuelve el
#: `index.html` del panel, que es lo correcto: esta dirección no es una página.
RUTA_WEBHOOK_TELEGRAM = "/webhook/telegram"


@app.post(RUTA_WEBHOOK_TELEGRAM)
async def recibir_telegram(request: Request, tareas: BackgroundTasks) -> Response:
    """Los `callback_query` de los botones y lo que los doctores escriben en los temas.

    ------------------------------------------------------------------------------------
    Vacío significa CERRADO, y es la decisión de seguridad de esta fase
    ------------------------------------------------------------------------------------

    Esta URL es pública y es la única puerta del sistema por la que algo de fuera puede hacer
    que el bot **le escriba al WhatsApp de un paciente**. El webhook de Meta se defiende con
    una firma HMAC del cuerpo; Telegram no firma nada: manda una cabecera con un secreto
    compartido que tú mismo elegiste al llamar a `setWebhook`.

    Por eso, sin `MAXICARE_TELEGRAM_WEBHOOK_SECRET` configurado esto devuelve 403 a todo.
    Es al revés de como degrada el resto del proyecto --normalmente se sigue adelante para no
    perder el mensaje de un paciente-- y tiene que ser al revés: aquí lo que se pierde por
    degradar no es un mensaje, es el control de a quién le habla la clínica.

    `compare_digest` y no `==`: comparar secretos con `==` sale antes en el primer byte que
    no coincide, y eso se puede medir.

    Siempre 200 cuando el secreto es bueno, y el trabajo en `BackgroundTasks`. Telegram
    reintenta lo que no conteste rápido, y un reintento de un `callback_query` de relevo es
    otro doctor tomando la conversación.
    """
    esperado = config.telegram_webhook_secret
    if not esperado:
        log.warning(
            "llegó un update de Telegram pero MAXICARE_TELEGRAM_WEBHOOK_SECRET está vacío: "
            "el relevo está apagado. Corre scripts/configurar_webhook_telegram.py"
        )
        return Response(status_code=403, content="forbidden", media_type="text/plain")

    recibido = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(recibido, esperado):
        log.warning(
            "POST a %s con secreto inválido desde %s",
            RUTA_WEBHOOK_TELEGRAM,
            request.client.host if request.client else "?",
        )
        return Response(status_code=403, content="forbidden", media_type="text/plain")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        log.warning("update de Telegram que no es JSON; se acepta para que no reintente")
        return Response(status_code=200, content="ok", media_type="text/plain")

    tareas.add_task(_atender_update_telegram, payload)
    return Response(status_code=200, content="ok", media_type="text/plain")


def _es_nuestro_grupo(chat: dict | None) -> bool:
    """Que el update venga del supergrupo de los doctores y no de otro chat.

    El mismo bot puede estar en más grupos --o alguien puede escribirle por privado-- y sin
    esta comprobación un `callback_data` copiado a mano en cualquier chat activaría un relevo
    de verdad sobre un paciente de verdad. El secreto de la cabecera prueba que el update
    viene de Telegram; esto prueba que viene de DONDE tiene que venir.
    """
    return str((chat or {}).get("id", "")) == str(config.telegram_chat_doctores)


async def _atender_update_telegram(payload: dict) -> None:
    """Reparte el update. Nunca propaga: el 200 ya salió y esto corre en segundo plano."""
    try:
        callback = payload.get("callback_query")
        if callback:
            await _atender_callback(callback)
            return

        # `edited_message` NO se atiende a propósito: editar en Telegram un mensaje que ya
        # salió hacia WhatsApp no puede deshacerlo --Meta no tiene edición-- y reenviar la
        # versión corregida le dejaría al paciente las dos. Lo que el doctor tenga que
        # corregir, lo escribe otra vez.
        mensaje = payload.get("message")
        if mensaje and _es_nuestro_grupo(mensaje.get("chat")):
            # Cerrar el tema a mano ES devolver el control, y es el gesto que sale natural:
            # el doctor termina de hablar y cierra el hilo. Sin esto, el tema quedaba cerrado
            # --o sea, sin canal hacia el paciente-- pero `tomada_por` seguía puesto y Daniela
            # seguía callada hasta que el barrido lo cortara tres horas después. Es
            # exactamente el estado que el no negociable 15 prohíbe: las dos cosas tienen que
            # dejar de ser verdad juntas.
            if "forum_topic_closed" in mensaje:
                await relevo.cerrar_por_tema_cerrado(
                    mensaje,
                    telegram=_telegram,
                    database_url=config.database_url,
                    tema_general=_tema_general or 0,
                )
                return

            await relevo.relevar_mensaje(
                mensaje,
                telegram=_telegram,
                whatsapp=_whatsapp,
                database_url=config.database_url,
                # Para cuando ese mensaje resulta ser la fecha de una cita que el doctor
                # acordó de viva voz. `None` solo si el calendario no arrancó: ahí se le dice
                # y no se registra nada, nunca un `CalendarioDoble` -- no negociable 1.
                calendario=_calendario,
                tema_general=_tema_general or 0,
            )
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo atender un update de Telegram")


async def _quitar_botones(mensaje: dict, id_conversacion: str) -> None:
    """Deja el mensaje sin teclado. Falla en silencio: el trabajo ya se hizo.

    Un botón que sigue puesto después de pulsarlo invita a pulsarlo otra vez, y la segunda
    pulsación de «Sí, quedó agendada» le pediría al doctor una segunda fecha para una cita
    que ya existe.
    """
    if not mensaje.get("message_id"):
        return
    try:
        await _telegram.editar_teclado(mensaje["message_id"], None)
    except Exception:  # noqa: BLE001
        log.warning("los botones de cierre quedaron puestos en %s", id_conversacion)


async def _atender_callback(callback: dict) -> None:
    """Los botones del relevo: tomar la conversación, devolverla, y el cierre conversado."""
    datos = callback.get("data") or ""
    callback_id = callback.get("id") or ""
    mensaje = callback.get("message") or {}

    if not _es_nuestro_grupo(mensaje.get("chat")):
        log.warning("callback_query desde un chat que no es el de los doctores; se ignora")
        await _telegram.responder_callback(callback_id, "No puedo hacer eso desde aquí.")
        return

    doctor = relevo.nombre_de_quien_pulsa(callback.get("from"))
    # Su id de Telegram, para que la bienvenida del hilo sea una MENCIÓN y no un nombre. Es
    # lo que hace que el aviso le suene aunque tenga el grupo silenciado, y tocarlo es lo
    # único que lo lleva al hilo: el botón no puede (`relevo._mencion`).
    doctor_id = (callback.get("from") or {}).get("id")
    general = _tema_general or 0

    if datos.startswith(relevo.PREFIJO_TOMAR):
        await relevo.activar(
            id_conversacion=datos[len(relevo.PREFIJO_TOMAR):],
            doctor=doctor,
            doctor_id=doctor_id,
            callback_id=callback_id,
            # El mensaje del que cuelga el botón: el escalamiento del General, que hay que
            # dejar marcado como atendido para que otro doctor no lo pulse.
            mensaje_id=mensaje.get("message_id"),
            telegram=_telegram,
            database_url=config.database_url,
            cierre_relevo_minutos=_relevo_minutos["cierre_relevo_minutos"],
            tema_general=general,
        )
        return

    if datos.startswith(relevo.PREFIJO_TOMAR_TEL):
        # El botón del aviso de archivos. Viaja con el teléfono porque cuando ese aviso se
        # manda todavía no hay conversación -- ver `relevo.PREFIJO_TOMAR_TEL`.
        telefono = datos[len(relevo.PREFIJO_TOMAR_TEL):]
        try:
            id_conversacion = await asyncio.to_thread(
                relevo._conversacion_de, config.database_url, telefono
            )
        except Exception:  # noqa: BLE001
            log.exception("no se pudo resolver la conversación de +%s", telefono)
            await _telegram.responder_callback(
                callback_id, "No pude abrir esa conversación.", alerta=True
            )
            return
        await relevo.activar(
            id_conversacion=id_conversacion,
            doctor=doctor,
            doctor_id=doctor_id,
            callback_id=callback_id,
            mensaje_id=mensaje.get("message_id"),
            telegram=_telegram,
            database_url=config.database_url,
            cierre_relevo_minutos=_relevo_minutos["cierre_relevo_minutos"],
            tema_general=general,
        )
        return

    if datos.startswith(relevo.PREFIJO_DEVOLVER):
        # Ya NO cierra de golpe: primero pregunta si quedó agendada una cita. El relevo sigue
        # tomado mientras dura esa pregunta, así que Daniela sigue callada.
        id_conversacion = datos[len(relevo.PREFIJO_DEVOLVER):]
        await _telegram.responder_callback(callback_id, "Un momento…")
        if mensaje.get("message_id"):
            # Quitar el botón: pulsar «Listo» dos veces no puede parecer que hace algo.
            try:
                await _telegram.editar_teclado(mensaje["message_id"], None)
            except Exception:  # noqa: BLE001
                log.warning("el botón de devolver quedó puesto en %s", id_conversacion)
        await relevo.iniciar_cierre(
            id_conversacion,
            telegram=_telegram,
            database_url=config.database_url,
            tema_id=mensaje.get("message_thread_id"),
        )
        return

    if datos.startswith(relevo.PREFIJO_NO_AGENDO):
        id_conversacion = datos[len(relevo.PREFIJO_NO_AGENDO):]
        await _telegram.responder_callback(callback_id, "Listo, Daniela retoma.")
        await _quitar_botones(mensaje, id_conversacion)
        await relevo.cerrar(
            id_conversacion,
            motivo="devuelto_por_doctor",
            telegram=_telegram,
            database_url=config.database_url,
            tema_id=mensaje.get("message_thread_id"),
            tema_general=general,
            doctor=doctor,
        )
        return

    if datos.startswith(relevo.PREFIJO_SI_AGENDO):
        id_conversacion = datos[len(relevo.PREFIJO_SI_AGENDO):]
        tema_id = mensaje.get("message_thread_id")
        await _telegram.responder_callback(callback_id, "Dime de qué es.")
        await _quitar_botones(mensaje, id_conversacion)
        if tema_id is None:
            log.warning("«sí agendó» desde fuera de un hilo; se cierra sin cita")
            await relevo.cerrar(
                id_conversacion,
                motivo="devuelto_por_doctor",
                telegram=_telegram,
                database_url=config.database_url,
                tema_general=general,
                doctor=doctor,
            )
            return
        # Los datos primero y la cita al final: así se crea de una vez, con todo, en vez de
        # insertarla y corregirla. Empieza por el que falte -- si el número no tiene nombre,
        # ese; si lo tiene, de qué es. Ver `relevo.pedir_los_datos_de_la_cita`.
        await relevo.pedir_los_datos_de_la_cita(
            id_conversacion,
            telegram=_telegram,
            database_url=config.database_url,
            tema_id=tema_id,
        )
        return

    log.info("callback_query con datos que no reconozco: %r", datos[:64])
    await _telegram.responder_callback(callback_id, "Ese botón ya no hace nada.")


def _escapar(texto: str) -> str:
    """Telegram va en `parse_mode=HTML`, y estos textos los escribe un modelo a partir de lo
    que dijo un desconocido: un `<` suelto rompe el mensaje entero, que es justo el que el
    doctor necesita leer. `quote=False` deja las comillas en paz -- dentro de un texto no son
    HTML, y escaparlas solo llenaría el aviso de `&#x27;`."""
    return html.escape(texto, quote=False)


def _registrar_escalamiento(
    ctx: ContextoDaniela, motivo: str, resumen: str, pregunta: str
) -> int | None:
    """El id del escalamiento que hay que avisar, o `None` si el doctor YA fue avisado.

    Sincrónico a propósito: `persistencia` es psycopg. Siempre dentro de `to_thread`.

    Las dos consultas van en la misma conexión, y la segunda es la que convierte la
    deduplicación en algo honesto: `insertar_escalamiento` devuelve `None` tanto si el aviso
    salió como si solo se intentó. Cuando la fila existe pero se quedó sin
    `telegram_message_id`, no hay nada que duplicar --hay un aviso que falta-- y se devuelve
    su id para mandarlo ahora. **Un aviso que no salió no es un aviso duplicado.**
    """
    # La clave la arma el orquestador, nunca el modelo. Y es LA MISMA que construye
    # `herramientas._escalar_a_doctores`, que es lo único que hace que este aviso y el de la
    # tool se reconozcan como el mismo escalamiento.
    clave = ctx.clave("escalamiento", ctx.turno_actual)
    with persistencia.conectar(ctx.database_url) as conn:
        nuevo = persistencia.insertar_escalamiento(
            conn,
            id_conversacion=ctx.id_conversacion,
            motivo=motivo,
            resumen=resumen,
            pregunta=pregunta,
            clave_idempotencia=clave,
        )
        if nuevo is not None:
            return nuevo
        return persistencia.escalamiento_pendiente_de_aviso(conn, clave)


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

    La defensa es la clave de idempotencia, no un `if`. Funciona porque las dos rutas --la
    tool y esta-- arman exactamente la misma clave, `ctx.clave("escalamiento",
    ctx.turno_actual)`, y porque `turno_actual` se persiste en Neon
    (`persistencia.tocar_conversacion`): con el turno congelado en 0 todos los mensajes de
    una conversación compartirían clave y el doctor se enteraría del primer escalamiento y
    de ninguno más.

    -------------------------------------------------------------------------------------
    Y lo que TAMPOCO puede hacer: callarse porque alguien lo intentó y falló
    -------------------------------------------------------------------------------------

    La primera versión de esto se callaba en cuanto la clave existía, y eso quemaba la clave
    al INTENTAR en vez de al CONSEGUIR. Basta un paciente llamado «Ana <3 Gómez» para verlo:
    la tool escribe su fila, Telegram rechaza el HTML mal cerrado, `failure_error_function`
    se traga el `ErrorDeCanal` para que el modelo siga conversando, y al cerrar el turno esto
    encontraba la clave quemada y no decía nada. Dos filas de escalamiento en Neon y CERO
    Telegram: un paciente con dolor «escalado» en una tabla que nadie mira.

    Por eso `_registrar_escalamiento` distingue los dos casos con
    `persistencia.escalamiento_pendiente_de_aviso`: si la fila existe pero se quedó sin
    `telegram_message_id`, el aviso se manda ahora. Un aviso que no salió no es un aviso
    duplicado.

    No propaga. `conversacion.responder` ya se traga lo que salga de aquí para que un fallo
    avisando al doctor no deje al paciente sin respuesta, pero un error tragado en silencio
    no se puede depurar: por eso queda en el log con el id de la conversación.
    """
    try:
        nombre = ctx.nombre_paciente or "paciente sin identificar"
        # Sin afirmar que el modelo no llamó a la tool: puede haberla llamado y haber fallado
        # antes de escribir su fila, y entonces esta sería la primera. Lo único que se sabe
        # con certeza es que el turno cerró escalado y qué se le dijo al paciente.
        resumen = (
            f"El turno cerró escalado (motivo: {motivo}). Esto fue lo que se le respondió "
            f"al paciente: {mensaje_al_paciente}"
        )
        pregunta = "¿Alguien puede revisar esta conversación y retomarla si hace falta?"

        escalamiento_id = await asyncio.to_thread(
            _registrar_escalamiento, ctx, motivo, resumen, pregunta
        )
        if escalamiento_id is None:
            log.info(
                "el turno %s de %s ya estaba escalado Y avisado; no se repite el aviso",
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
        #
        # `ctx.tema_general` a secas, igual que hace la tool, y NUNCA
        # `ctx.tema_general or _tema_general`: en el General ese valor es `0`, el `or` lo
        # cambiaría por el `_tema_general` del proceso --normalmente `1`-- y Telegram
        # respondería `Bad Request: message thread not found`. El aviso no llegaría. Que `0`
        # signifique «el General» es justo lo que `canales.enviar_mensaje` resuelve con su
        # `if tema_id:`.
        message_id = await _telegram.enviar_mensaje(texto, tema_id=ctx.tema_general)

        await asyncio.to_thread(_anotar_telegram, ctx, escalamiento_id, message_id)
    except Exception:  # noqa: BLE001 -- ver docstring
        log.exception("no se pudo avisar a los doctores del escalamiento de %s", ctx.id_conversacion)


async def _rastreado(m: ingesta.MensajeEntrante) -> None:
    """`_entregar` anotando que este turno está vivo, para que el apagado lo espere.

    El `finally` no es cosmética: si `_entregar` reventara --promete que no, y este envoltorio
    no depende de esa promesa-- un `wamid` colgado en `_EN_VUELO` haría que cada apagado se
    quedara esperando los 75 segundos completos a algo que ya no existe.
    """
    _EN_VUELO.add(m.wamid)
    try:
        await _entregar(m)
    finally:
        _EN_VUELO.discard(m.wamid)


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
    entrega = None
    try:
        entrega = await ingesta.procesar_mensaje(
            m,
            whatsapp=_whatsapp,
            telegram=_telegram,
            database_url=config.database_url,
            tema_general=_tema_general,
        )
    except Exception:  # noqa: BLE001
        log.exception("fallo inesperado entregando %s", m.wamid)

    # Meta reintenta el mismo webhook, y el proyecto ya lo tenía asumido: `procesar_mensaje`
    # deduplica por `wamid` con un `ON CONFLICT DO NOTHING` y devuelve `nuevo=False` cuando
    # reconoce un reintento. Ese dato se estaba tirando, así que el archivo llegaba al doctor
    # una sola vez --bien-- pero Daniela corría el turno entero otra vez: el mismo POST tres
    # veces eran tres respuestas al paciente y tres corridas del modelo pagadas.
    #
    # `entrega is None` significa que `procesar_mensaje` reventó antes de decidir nada, y ahí
    # se sigue: un fallo suyo --Telegram caído, la descarga del archivo-- no puede dejar al
    # paciente sin respuesta. Solo se corta cuando dijo explícitamente que esto es un
    # reintento.
    if entrega is not None and not entrega.nuevo:
        log.info("%s ya estaba atendido: reintento de Meta, Daniela no vuelve a contestar", m.wamid)
        return

    # `/clearstate`: devolver un número de pruebas al estado de primer contacto.
    #
    # Va AQUÍ, después de la deduplicación y antes del turno, por tres razones. (1) Pasada la
    # dedupe, un reintento de Meta no ejecuta el borrado dos veces. (2) `procesar_mensaje` ya
    # corrió, así que el comando queda reenviado a Telegram: un borrado irreversible que deja
    # rastro visible para los doctores es mejor que uno silencioso. (3) `atender` no se toca
    # en absoluto -- ni búfer, ni candado del turno, ni sesión.
    #
    # Con `MAXICARE_TELEFONOS_PRUEBA` vacía --su default-- esta rama no existe para nadie y el
    # texto sigue su camino hasta Daniela como cualquier otro mensaje.
    if reseteo.es_comando(m.texto) and reseteo.autorizado(m.telefono, config.telefonos_prueba):
        await _resetear_numero(m)
        return

    try:
        atendido = await atencion.atender(
            m,
            whatsapp=_whatsapp,
            telegram=_telegram,
            config=config,
            calendario=_calendario,
            al_escalar=_avisar_a_doctores,
            lectura=entrega.lectura if entrega is not None else None,
        )
    except Exception:  # noqa: BLE001 -- `atender` promete no propagar; esto lo hace cierto
        # Aquí ya no hay nada que salvar para el paciente, pero el archivo YA llegó al
        # doctor: eso es lo que protege el `try` de arriba.
        log.exception("el turno de Daniela reventó para %s", m.wamid)
        return

    # El `Atendido` traía el motivo, el turno y el escalamiento, y se descartaba entero. Un
    # turno que respondió con el mensaje de emergencia se veía en el log igual que uno que
    # fue bien, y el único sitio donde quedaba rastro era `mensajes_entrantes` -- que hay que
    # ir a consultar sabiendo ya que pasó algo. Esta línea es la que hace que se vea sin
    # buscarla.
    if atendido.motivo:
        log.warning(
            "turno %s de %s con incidencia (%s): respondido=%s · escalado_por=%s",
            atendido.turno,
            atendido.id_conversacion or m.telefono,
            atendido.motivo,
            atendido.respondido,
            atendido.escalado_por,
        )
    elif atendido.escalado_por:
        log.info(
            "turno %s de %s escalado por %s",
            atendido.turno,
            atendido.id_conversacion or m.telefono,
            atendido.escalado_por,
        )


def _bases_secundarias() -> tuple[str, ...]:
    """El carril de pruebas del panel, si su esquema existe ya.

    Se comprueba en vez de intentarlo y fallar: en una base recién creada el esquema puede no
    existir todavía -- y eso no es un fallo del reseteo, es que no hay nada que borrar ahí.
    Sin esta comprobación, la confirmación le diría al usuario «no pude con: base secundaria»
    en el caso más normal de todos.

    Que el esquema EXISTA no bastaba, y eso costó un hallazgo: existía desde hacía meses pero
    con dos migraciones de retraso, porque su única puesta al día era
    `_preparar_esquema_de_pruebas`, que es perezosa. `borrar_rastro` reventaba ahí con
    `UndefinedColumn`. Desde el 13/09/2026 lo pone al día `scripts/inicializar_base.py`, que
    corre en cada despliegue y verifica las tablas del historial en los dos esquemas.
    """
    try:
        with persistencia.conectar(config.database_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (ESQUEMA_PRUEBAS_WEB,),
            )
            existe = cur.fetchone() is not None
    except Exception:  # noqa: BLE001
        log.warning("no se pudo comprobar si existe el esquema del chat de pruebas")
        return ()
    return (_url_de_pruebas(),) if existe else ()


async def _resetear_numero(m: ingesta.MensajeEntrante) -> None:
    """Atiende `/clearstate`. Nunca lanza: quien pidió el reseteo tiene que recibir algo.

    Un comando destructivo que se queda mudo es peor que uno que falla: el usuario no sabe si
    borró, si no borró o si borró a medias, y lo único que puede hacer es volver a mandarlo.
    """
    try:
        borrado = await reseteo.resetear(
            m.telefono,
            database_url=config.database_url,
            telegram=_telegram,
            calendario=_calendario,
            conservar_wamid=m.wamid,
            bases_extra=await asyncio.to_thread(_bases_secundarias),
        )
    except reseteo.ErrorDeReseteo as exc:
        # El único caso en que se aborta a propósito: Calendar no dejó borrar un evento y
        # borrar las filas lo habría vuelto un cupo fantasma de la clínica.
        log.warning("reseteo de %s abortado: %s", m.telefono, exc)
        await _avisar_del_reseteo(m.telefono, f"No reseteé nada. {exc}", wamid=m.wamid)
        return
    except Exception:  # noqa: BLE001
        log.exception("el reseteo de %s reventó", m.telefono)
        await _avisar_del_reseteo(
            m.telefono,
            "No pude resetear el número: algo falló a mitad. Revisa los logs antes de "
            "volver a intentarlo.",
            wamid=m.wamid,
        )
        return

    log.info("RESETEO de %s: %s", m.telefono, borrado)
    await _avisar_del_reseteo(m.telefono, reseteo.confirmacion(borrado), wamid=m.wamid)


async def _avisar_del_reseteo(telefono: str, texto: str, *, wamid: str | None = None) -> None:
    try:
        respuesta = await _whatsapp.enviar_texto(telefono, texto)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo confirmar el reseteo a %s", telefono)
        return

    # Y se ANOTA. `/clearstate` es el único camino que contesta sin pasar por `atender`, así
    # que era el único que respondía de verdad y dejaba la fila con `respondido_en` NULL: un
    # mensaje contestado que cualquier informe --y desde hoy el barrido de arranque-- leería
    # como perdido. El barrido lo habría reatendido pasándole a Daniela el texto
    # «/clearstate» como si fuera un paciente.
    if wamid is None or respuesta is None:
        return
    try:
        await asyncio.to_thread(_marcar_reseteo_respondido, wamid, respuesta)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo anotar la confirmación del reseteo de %s", wamid, exc_info=True)


def _marcar_reseteo_respondido(wamid: str, wamid_respuesta: str) -> None:
    with persistencia.conectar(config.database_url) as conn:
        persistencia.marcar_respondido(conn, wamid, wamid_respuesta=wamid_respuesta)


# ==========================================================================================
# Salud
# ==========================================================================================


@app.get("/salud")
async def salud() -> dict:
    """Dice qué falta, no solo si está vivo.

    Un `{"ok": true}` que no comprueba nada es peor que no tener endpoint: da confianza sin
    respaldo. Este mira de verdad las tres piezas de las que depende la fase.
    """
    # Se actualiza al cerrar cada fase. Nadie lo comprueba automáticamente, así que se quedó
    # diciendo "6A" durante toda la fase 7 --incluido el despliegue-- y lo único que lo
    # delató fue leer la respuesta de `/salud` a mano.
    estado: dict = {"servicio": "maxicare-daniela", "fase": "7"}

    try:
        with persistencia.conectar(config.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mensajes_entrantes")
            estado["mensajes_recibidos"] = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM mensajes_entrantes "
                "WHERE reenviado_en IS NULL AND fallo IS NOT NULL"
            )
            estado["sin_entregar"] = cur.fetchone()[0]
            # `sin_entregar` mira el viaje HACIA los doctores y solo cuenta lo que dejó un
            # fallo escrito. Un proceso matado a media frase no escribe nada, así que la
            # pérdida del 13/09/2026 --el despliegue que se llevó un turno por delante-- no
            # aparecía en ningún indicador. Esta cuenta es la que mira al paciente: entró y
            # nadie le contestó, sin motivo anotado.
            #
            # DENTRO del `with`, y aquí se pagó el despiste: escrita un nivel a la izquierda
            # corría con la conexión ya cerrada y `/salud` contestaba
            # `base_de_datos: "FALLA: the connection is closed"` -- el indicador de salud
            # mintiendo sobre la salud. Lo caza `test_salud_cuenta_los_mensajes_sin_responder`.
            estado["sin_responder"] = persistencia.contar_sin_responder(conn)
            # Cuántas conversaciones tiene un doctor ahora mismo. Es el número que delata un
            # relevo atascado: mientras esté por encima de cero, hay pacientes a los que
            # Daniela NO está contestando y temas abiertos de par en par. Si no baja en todo
            # un día, el barrido de `_barrer_relevos_sin_parar` dejó de correr.
            estado["relevos_abiertos"] = persistencia.contar_relevos_abiertos(conn)
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

    # QUÉ calendario acabó en `_calendario`, no si la variable está puesta. Sin esta línea, el
    # único rastro de un calendario que no arrancó es un `log.error` del arranque que nadie
    # vuelve a mirar, mientras `/salud` sigue diciendo `configuracion: ok` y Daniela escala a
    # los doctores cada vez que alguien intenta agendar. `CalendarioGoogle` es el bueno;
    # `CalendarioCaido` es «no puede agendar»; un `CalendarioDoble` aquí sería el peor caso de
    # todos --confirmarle al paciente una cita que no existe en ningún calendario-- y por eso
    # `_construir_el_calendario` no deja que ocurra: lo degrada a caído. Que se pueda LEER
    # desde fuera es lo que convierte esa decisión en algo comprobable.
    estado["calendario"] = type(_calendario).__name__

    # Sin esto no hay forma de saber desde fuera si Daniela está callada. Un `0` en el `.env`
    # del VPS y un reinicio la apagan sin dejar rastro en ninguna respuesta de este endpoint.
    estado["daniela_responde"] = config.daniela_responde
    # Encendido o apagado, sin término medio: sin secreto, `/webhook/telegram` responde 403 a
    # todo y el botón «Hablar yo con el paciente» no hace nada. Va aquí y no en
    # `configuracion` porque no tenerlo es un estado legítimo --el sistema entero funciona sin
    # relevo-- pero es invisible: no llega ni la petición, así que no hay error que mirar.
    estado["relevo"] = (
        "activo" if config.telegram_webhook_secret
        else "apagado (falta MAXICARE_TELEGRAM_WEBHOOK_SECRET)"
    )
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
#: Reexportada de `persistencia`, que es donde vive: no es transporte, es un esquema de
#: esta base, y quien lo pone al día son DOS -- este chat, perezosamente, y
#: `scripts/inicializar_base.py`, que es la puerta del despliegue.
ESQUEMA_PRUEBAS_WEB = persistencia.ESQUEMA_PRUEBAS_WEB

_secreto_sesion = config.secreto_sesion

#: El contexto vivo de cada conversación del chat de pruebas. El HISTORIAL ya no está aquí:
#: desde la fase 7 cada turno queda escrito en `agent_messages` del esquema `pruebas_web`,
#: igual que el de WhatsApp en el de `public` (`persistencia.sesion_de_agente`). Lo que queda
#: en memoria es el contexto --calendario doble, credenciales vacías de Telegram,
#: configuración operativa--, barato de reconstruir.
#:
#: Se pierde al reiniciar el proceso, y con él se pierde el REENGANCHE con esas filas -- no
#: las filas en sí. Este diccionario es lo único que traduce un `id_conversacion` conocido a
#: su contexto; vacío tras un reinicio, el id que el navegador todavía recuerda deja de
#: reconocerse, `_contexto_de_prueba` lo trata como conversación nueva y
#: `persistencia.asegurar_conversacion` -- que SIEMPRE inserta -- abre una fila distinta. Las
#: filas del `id_conversacion` viejo quedan huérfanas en `pruebas_web`, sin que nada las
#: vuelva a leer. Es la decisión correcta para este carril, no un descuido: el chat de
#: pruebas es la pantalla donde la clínica prueba a Daniela desde cero, y tiene que poder
#: abrir un primer contacto sin pedir un `/clearstate` antes. El contenedor corre además con
#: un solo worker (ver el `CMD` del Dockerfile), así que no hay dos procesos que puedan
#: discrepar sobre este diccionario.
_conversaciones_de_prueba: dict[str, ContextoDaniela] = {}

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
    return persistencia.url_con_search_path(config.database_url, ESQUEMA_PRUEBAS_WEB)


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

    directa = persistencia.url_directa(config.database_url)
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


def _contexto_de_prueba(quien: dict, id_conversacion: str | None) -> tuple[ContextoDaniela, Any]:
    """Recupera la conversación viva, o abre una nueva en el carril de pruebas.

    El historial ya no viaja con el contexto: se reconstruye en cada llamada con
    `persistencia.sesion_de_agente`, igual que en WhatsApp. Ver el docstring de
    `_conversaciones_de_prueba` sobre por qué el contexto sí se queda en memoria y el
    historial no.

    ------------------------------------------------------------------------------------
    Por qué la sesión va con `config.database_url` (SIN `options=`) y `esquema=` explícito
    ------------------------------------------------------------------------------------

    `_preparar_esquema_de_pruebas()` devuelve una URL con `options=-csearch_path=pruebas_web`
    colgada -- la necesita para que la conexión síncrona de más abajo (`asegurar_conversacion`,
    `leer_configuracion`) aterrice en el esquema correcto sin pasar por SQLAlchemy. Pasarle
    ESA MISMA url a `sesion_de_agente` funcionaría igual -- las filas caerían en `pruebas_web`
    igual -- pero el aislamiento lo estaría dando el `search_path` de la conexión, no
    `schema_translate_map`. Eso ya pasó una vez en esta fase: con `options` colado en la URL,
    `test_las_tablas_de_sesion_no_se_crean_en_public` pasaba con `schema_translate_map` BORRADO
    de `persistencia._engine_de` (ver `tests/test_sesion_neon.py::_sin_options`). El mismo
    riesgo existe aquí, así que se evita de la misma forma: `config.database_url` es la URL de
    producción tal cual (pooler, sin `options`), y `esquema="pruebas_web"` es lo único que
    desvía la escritura de `agent_sessions`/`agent_messages` fuera de `public`. Es el mismo
    patrón que la Tarea 5 fijó para WhatsApp (`atencion._sesion_de`, sin `esquema=` porque ahí
    el destino ES `public`).
    """
    if id_conversacion and id_conversacion in _conversaciones_de_prueba:
        ctx = _conversaciones_de_prueba[id_conversacion]
        return ctx, persistencia.sesion_de_agente(
            id_conversacion, database_url=config.database_url, esquema=ESQUEMA_PRUEBAS_WEB
        )

    url = _preparar_esquema_de_pruebas()
    # El «teléfono» lleva el usuario dentro. No colisiona con ningún número real, deja claro
    # en la base de dónde salió la fila, y le da a cada persona de la clínica su propia
    # conversación sin que se pisen entre ellas.
    telefono = f"web-{quien['usuario']}"
    with persistencia.conectar(url) as conn:
        nuevo = persistencia.asegurar_conversacion(conn, telefono=telefono, canal="web")
        # El mismo hecho que `atencion._leer_estado` saca para WhatsApp. Sin esta lectura, el
        # chat del panel NO reproduciría el carril real: `telefono_sin_paciente` se quedaría
        # en su default `False` y `identidad_antes_de_datos` frenaría una primera cita que en
        # WhatsApp sí pasa. El comentario de abajo dice «igual que un paciente nuevo en
        # WhatsApp», y esto es lo que lo hace cierto.
        sin_ficha = persistencia.buscar_paciente_por_telefono(conn, telefono) is None

    ctx = ContextoDaniela(
        id_conversacion=nuevo,
        telefono_completo=telefono,
        database_url=url,
        # Un calendario de mentira, y por la misma razón que el esquema aparte: probar
        # «agéndame el martes» no puede crear un evento en el Google Calendar de la clínica.
        # UNO SOLO para todo el proceso, no uno por conversación: una clínica tiene un
        # calendario, y desde que `consultar_citas` contrasta contra él (no negociable 20) un
        # doble nuevo por conversación significa «ninguna de tus citas existe ya», o sea que
        # el chat del panel cancelaría cada cita en cuanto alguien preguntara por ella.
        calendario=_CALENDARIO_WEB,
        # Sin identificar, igual que un paciente nuevo en WhatsApp. Es lo que permite probar
        # el flujo de identificación, que es donde más se equivoca un prompt.
        identidad_verificada=False,
        telefono_sin_paciente=sin_ficha,
        tema_general=_tema_general or 0,
        # El chat de pruebas del panel, no WhatsApp: separa en el dashboard de trazas las
        # conversaciones reales de las pruebas de la clínica.
        canal="web",
        # Sin credenciales de Telegram: `escalar_a_doctores` no puede avisar a nadie desde
        # aquí. Una prueba no le hace sonar el teléfono a un doctor.
        telegram_bot_token="",
        telegram_chat_doctores="",
        # Los dos en `None`, y no es un descuido: al chat del panel no le llega ningún
        # recordatorio. El despachador sale por WhatsApp, y aquí no hay número al que salir.
        # Copiar el valor de otra conversación haría que Daniela creyera que a quien escribe
        # desde el panel le salió un recordatorio que nunca existió.
        ultimo_recordatorio_tipo=None,
        ultimo_recordatorio_en=None,
    )
    try:
        with persistencia.conectar(url) as conn:
            operativa = persistencia.leer_configuracion(conn)
        ctx.capacidad_por_hora = operativa["capacidad_por_hora"]
        ctx.duracion_cita_minutos = operativa["duracion_cita_minutos"]
        ctx.cierre_relevo_minutos = operativa["cierre_relevo_minutos"]
        # La jornada también, o el chat de pruebas ofrecería horarios que WhatsApp no ofrece
        # y la pantalla dejaría de servir para probar lo que de verdad pasa.
        ctx.jornada = Jornada(
            apertura=operativa["hora_apertura"],
            cierre=operativa["hora_cierre"],
            cierre_sabado=operativa["hora_cierre_sabado"],
            atiende_domingo=bool(operativa["atiende_domingo"]),
        )
    except Exception:  # noqa: BLE001 -- los defaults del dataclass son los mismos
        log.warning("chat de pruebas sin configuración operativa; se usan los defaults")

    _conversaciones_de_prueba[nuevo] = ctx
    sesion = persistencia.sesion_de_agente(
        nuevo, database_url=config.database_url, esquema=ESQUEMA_PRUEBAS_WEB
    )
    return ctx, sesion


async def _resetear_chat_de_prueba(quien: dict) -> dict:
    """`/clearstate` en el carril web. Devuelve la misma forma que un turno normal.

    NO exige `MAXICARE_TELEFONOS_PRUEBA`, y la diferencia con WhatsApp es deliberada: aquí no
    hay ningún número de teléfono que proteger. El «teléfono» es `web-<usuario>`, una cadena
    que no existe ni puede existir en `public`; la conexión apunta con `search_path` al
    esquema `pruebas_web`, que es un carril de pruebas de punta a punta; y para llegar hasta
    aquí hay que tener sesión abierta en el panel. Lo único que alguien puede borrar es su
    propio carril, que es exactamente para lo que existe el botón de al lado.

    Tampoco se le pasan Telegram ni Calendar, porque en este carril no hay ninguno de los
    dos: el contexto de prueba se construye con `CalendarioDoble` y sin credenciales de
    Telegram, así que no hay eventos reales que eliminar ni temas que borrar.
    """
    telefono = f"web-{quien['usuario']}"
    url = await asyncio.to_thread(_preparar_esquema_de_pruebas)
    borrado = await reseteo.resetear(
        telefono, database_url=url, telegram=None, calendario=None
    )

    # Las conversaciones vivas de ESTE usuario, no todas: dos personas de la clínica pueden
    # estar probando a la vez, y reiniciar la tuya no puede cortarle el hilo a la otra. Solo
    # hace falta olvidar el CONTEXTO -- el historial en `agent_messages`/`agent_sessions` ya
    # lo borró `reseteo.resetear` más arriba, dentro de `persistencia.borrar_rastro` (desde
    # la Tarea 8), así que no queda una fila huérfana que limpiar aquí.
    for id_conversacion, ctx in list(_conversaciones_de_prueba.items()):
        if ctx.telefono_completo == telefono:
            del _conversaciones_de_prueba[id_conversacion]

    log.info("RESETEO del carril web de %s: %s", quien["usuario"], borrado)
    return {
        # `None` es lo que hace que el turno siguiente abra una conversación nueva: el
        # frontend guarda lo que venga aquí y lo manda en la próxima petición.
        "conversacion": None,
        "mensaje": reseteo.confirmacion(borrado),
        "turno": 0,
        "estado_oportunidad": "explorando",
        "barrera": "ninguna",
        "requiere_escalamiento": False,
        "motivo_escalamiento": "",
        "fuera_de_alcance": False,
        "tripwires": [],
        "regenerado": False,
    }


@app.post("/api/pruebas/chat")
async def chat_de_prueba(entrada: MensajeDePrueba, quien: dict = Depends(usuario_actual)) -> dict:
    """Un turno contra la Daniela de producción.

    Es literalmente el mismo objeto `agentes.daniela` que va a atender WhatsApp: las mismas
    nueve tools, las mismas instrucciones, los mismos seis guardrails. Lo único distinto es
    dónde aterriza lo que escribe.
    """
    # `/clearstate` también aquí, y por la misma razón que en WhatsApp: el botón «reiniciar»
    # olvida la conversación pero DEJA las filas, así que el paciente que te inventaste sigue
    # en `pruebas_web` y el turno siguiente te reconoce. Esto sí borra.
    if reseteo.es_comando(entrada.mensaje):
        return await _resetear_chat_de_prueba(quien)

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
    """Olvida el CONTEXTO en memoria. Las filas en `pruebas_web` se quedan -- incluido el
    historial en `agent_messages`, desde la fase 7 -- porque no estorban y dejan rastro de
    qué se probó. No es lo mismo que `/clearstate`: como `_contexto_de_prueba` no reconoce
    ya ese `id_conversacion`, el turno siguiente abre una fila nueva en `conversaciones`
    (`asegurar_conversacion` SIEMPRE inserta) y el historial viejo queda huérfano, con la
    misma fila del paciente por debajo."""
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


async def _recoger_lo_que_quedo_sin_responder() -> int:
    """Atiende los mensajes que el proceso anterior dejó colgados. Devuelve cuántos.

    La red por debajo del drenaje. El drenaje cubre el apagado ordenado --un despliegue-- y
    no puede cubrir lo demás: un `kill -9`, que el VPS se reinicie, que el contenedor se
    quede sin memoria. En esos casos el mensaje queda con `respondido_en` NULL y
    `fallo_respuesta` NULL, y hasta el 13/09/2026 ahí se acababa la historia: el paciente sin
    respuesta y nadie enterado.

    **No pasa por `_entregar`, y es a propósito.** `_entregar` empieza por
    `procesar_mensaje`, que deduplica por `wamid` y devolvería `nuevo=False` --con razón: el
    mensaje YA se registró y YA se le reenvió al doctor-- y se iría sin correr el turno, que
    es justamente lo que falta. Se llama a `atender` directo, que es la mitad que no llegó a
    ocurrir.

    El riesgo asumido, dicho para que nadie lo descubra solo: entre que WhatsApp acepta la
    respuesta y `marcar_respondido` la escribe hay unos milisegundos. Un proceso que muera
    justo ahí hace que el paciente reciba la respuesta dos veces. Se acepta porque la ventana
    es diminuta, porque el silencio es peor que la repetición, y sobre todo porque **las
    claves de idempotencia impiden lo que de verdad importaría**: reintentar no crea una
    segunda cita ni consume un segundo cupo.
    """
    try:
        with persistencia.conectar(config.database_url) as conn:
            colgados = persistencia.mensajes_sin_responder(conn)
    except Exception:  # noqa: BLE001
        log.warning("no se pudo consultar los mensajes sin responder", exc_info=True)
        return 0

    # `/clearstate` no se reatiende JAMÁS, y el cinturón va aparte del tirante. El tirante es
    # que `_avisar_del_reseteo` ahora anota el mensaje como respondido, así que los nuevos ni
    # aparecen aquí; el cinturón es esta línea, para las filas que quedaron sin anotar antes
    # de ese arreglo. Reatender un reseteo no lo repetiría --`atender` no mira el comando--
    # pero le pasaría a Daniela el texto «/clearstate» como si fuera un paciente preguntando.
    colgados = [fila for fila in colgados if not reseteo.es_comando(fila.get("texto"))]

    if not colgados:
        return 0

    log.warning(
        "arranque: %d mensaje(s) se quedaron sin responder y se reintentan: %s",
        len(colgados),
        ", ".join(fila["wamid"] for fila in colgados),
    )
    for fila in colgados:
        mensaje = ingesta.MensajeEntrante(
            wamid=fila["wamid"],
            telefono=fila["telefono"],
            nombre_perfil=fila["nombre_perfil"],
            tipo=fila["tipo"] or "text",
            texto=fila["texto"],
            media_id=fila["media_id"],
            mime=fila["mime"],
        )
        _EN_VUELO.add(mensaje.wamid)
        try:
            await atencion.atender(
                mensaje,
                whatsapp=_whatsapp,
                telegram=_telegram,
                config=config,
                calendario=_calendario,
                al_escalar=_avisar_a_doctores,
                # Sin `lectura`: si el mensaje traía archivo, el lector ya corrió y su nota
                # se entregó al doctor antes de morir el proceso. Rehacerla costaría otra
                # llamada al modelo para un destinatario que ya la tiene.
                lectura=None,
            )
        except Exception:  # noqa: BLE001
            log.exception("el reintento de %s también falló", mensaje.wamid)
        finally:
            _EN_VUELO.discard(mensaje.wamid)
    return len(colgados)


@app.on_event("startup")
async def _recoger_al_arrancar() -> None:
    """Va en una tarea aparte: el barrido llama al modelo una vez por mensaje, y el arranque
    no puede quedarse esperando eso. Mientras corre, `/salud` ya responde y Traefik puede
    empezar a mandar tráfico --que es justo lo que hace falta para que no se pierda el
    siguiente mensaje mientras se recoge el anterior."""
    asyncio.create_task(_recoger_lo_que_quedo_sin_responder())


# ==========================================================================================
# El cierre por tiempo de los relevos
# ==========================================================================================


#: Cada cuánto se mira si algún relevo se quedó solo. Un minuto es de sobra: lo que se mide
#: son horas, y el coste es una consulta a una tabla pequeña filtrada por `tomada_por IS NOT
#: NULL`, que es casi siempre cero filas.
SEGUNDOS_ENTRE_BARRIDOS = 60.0

#: La referencia viva de la tarea. Sin guardarla, el recolector de basura de Python se puede
#: llevar una tarea que nadie mira --`create_task` solo devuelve una referencia débil desde
#: el bucle-- y el cierre por tiempo dejaría de ocurrir sin un solo error en el log.
_tarea_de_barrido: asyncio.Task | None = None


async def _barrer_relevos_sin_parar() -> None:
    """El reloj del relevo. Es lo que impide que un tema se quede abierto para siempre.

    Un relevo que nadie cierra no es un detalle de orden: es un hilo por el que cualquiera
    del grupo le escribe al WhatsApp de un paciente, con Daniela callada al otro lado. Por
    eso esto corre aunque no haya pasado nada más en el servidor.

    **Asume un solo worker**, igual que `_candados` de `atencion.py`. Con varias réplicas
    cada una barrería por su cuenta; el daño estaría acotado --`cerrar_relevo` es idempotente
    y la segunda obtiene `False` sin escribir nada-- pero el aviso previo, que se recuerda en
    memoria, sí saldría repetido.
    """
    while True:
        await asyncio.sleep(SEGUNDOS_ENTRE_BARRIDOS)
        try:
            cerrados = await relevo.barrer(
                telegram=_telegram,
                database_url=config.database_url,
                cierre_minutos=_relevo_minutos["cierre_relevo_minutos"],
                aviso_minutos=_relevo_minutos["aviso_relevo_minutos"],
                tema_general=_tema_general or 0,
            )
            if cerrados:
                log.info("el barrido cerró %d relevo(s) por inactividad", cerrados)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- tiene que seguir vivo mañana
            log.exception("el barrido de relevos falló; se reintenta en el siguiente ciclo")


@app.on_event("startup")
async def _arrancar_barrido_de_relevos() -> None:
    global _tarea_de_barrido
    if not config.telegram_bot_token or not config.telegram_chat_doctores:
        # Sin Telegram no hay relevos que cerrar. Es el caso de las pruebas y el del chat web.
        log.info("sin Telegram configurado: no arranca el barrido de relevos")
        return
    _tarea_de_barrido = asyncio.create_task(_barrer_relevos_sin_parar())


@app.on_event("shutdown")
async def _parar_barrido_de_relevos() -> None:
    """Se cancela y se espera. Sin el `await`, el bucle muere a mitad de un `cerrar()` y
    puede dejar la base cerrada y el tema de Telegram abierto -- el peor de los dos estados
    intermedios, porque Daniela vuelve a hablar con el hilo todavía en vivo."""
    if _tarea_de_barrido is None:
        return
    _tarea_de_barrido.cancel()
    try:
        await _tarea_de_barrido
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


# ==========================================================================================
# El despacho de recordatorios
# ==========================================================================================


def _leer_configuracion_operativa() -> dict[str, int]:
    with persistencia.conectar(config.database_url) as conn:
        return persistencia.leer_configuracion(conn)


#: La referencia viva de la tarea de recordatorios. Igual que `_tarea_de_barrido`: sin
#: guardarla, el recolector de basura se puede llevar una tarea que nadie mira y los
#: recordatorios dejarían de salir sin un solo error en el log.
_tarea_de_recordatorios: asyncio.Task | None = None


async def _despachar_recordatorios_sin_parar() -> None:
    """El reloj de la cola de recordatorios.

    Va en una tarea PROPIA y no dentro de `_barrer_relevos_sin_parar`, aunque el intervalo sea
    el mismo: aquella no arranca sin Telegram --correcto, sin Telegram no hay relevos que
    cerrar-- y los recordatorios no dependen de Telegram para nada. Compartirlas dejaría los
    recordatorios apagados en cualquier despliegue sin grupo de doctores.

    **Asume un solo worker**, igual que el barrido de relevos y que `_candados` de `atencion`.
    Con varias réplicas el daño está acotado por `FOR UPDATE SKIP LOCKED`: cada fila la toma
    una sola.
    """
    while True:
        await asyncio.sleep(SEGUNDOS_ENTRE_BARRIDOS)
        try:
            operativa = await asyncio.to_thread(_leer_configuracion_operativa)
            recuento = await seguimientos.despachar(
                database_url=config.database_url,
                whatsapp=_whatsapp,
                jornada=Jornada(
                    apertura=operativa.get("hora_apertura", 8),
                    cierre=operativa.get("hora_cierre", 17),
                    cierre_sabado=operativa.get("hora_cierre_sabado", 15),
                    atiende_domingo=bool(operativa.get("atiende_domingo", 0)),
                ),
                plantilla=config.plantilla_recordatorio,
            )
            if any(recuento.values()):
                log.info("recordatorios: %s", recuento)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- tiene que seguir vivo mañana
            log.exception("el despacho de recordatorios falló; se reintenta en el ciclo siguiente")


@app.on_event("startup")
async def _arrancar_despacho_de_recordatorios() -> None:
    global _tarea_de_recordatorios
    if not config.database_url:
        log.info("sin base configurada: no arranca el despacho de recordatorios")
        return
    if not config.plantilla_recordatorio:
        log.warning(
            "MAXICARE_PLANTILLA_RECORDATORIO vacía: el despachador decidirá y NO enviará. "
            "Es el modo de comprobación; para enviar de verdad hace falta la plantilla de Meta."
        )
    _tarea_de_recordatorios = asyncio.create_task(_despachar_recordatorios_sin_parar())


@app.on_event("shutdown")
async def _parar_despacho_de_recordatorios() -> None:
    if _tarea_de_recordatorios is None:
        return
    _tarea_de_recordatorios.cancel()
    try:
        await _tarea_de_recordatorios
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


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

"""Configuración del proyecto: lectura de variables de entorno (ver `.env.ejemplo` en la
raíz del proyecto) y las constantes derivadas del plan que `agentes.py`, `herramientas.py`
y `runtime.py` necesitan -- identificador de modelo, `fallos.limite_turnos`,
`observabilidad.tracing` (`workflow_name`).

Lo que va aquí: `os.environ`/`os.getenv(...)` con sus nombres literales y sus defaults
explícitos, sin ningún secreto hardcodeado.

Lo que NO va aquí: nada de transporte ni lógica de negocio. Este módulo no importa de
`agentes.py`, `herramientas.py` ni `runtime.py`, para que la configuración se pueda leer y
probar sin arrastrar el resto del proyecto.

------------------------------------------------------------------------------------------
Dos clases de configuración, y no se mezclan
------------------------------------------------------------------------------------------

1. **Configuración de despliegue** (este módulo): credenciales, URLs, identificadores de
   modelo, constantes que el plan fija. Cambia entre entornos y exige un despliegue.

2. **Configuración operativa** (`persistencia.py`, tabla `configuracion` en Neon): capacidad
   por hora, duración de la cita y tiempo de cierre del relevo. La clínica la ajusta desde
   la interfaz web SIN tocar código ni desplegar. Por eso vive en la base de datos y no
   aquí: si estuviera aquí, cambiar el tope de pacientes por hora sería un despliegue.

Los defaults de esa segunda clase están en `persistencia.CONFIGURACION_POR_DEFECTO`, no en
este archivo, para que haya un solo lugar donde mirarlos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ==========================================================================================
# Constantes que el plan fija y que NO son variables de entorno
# ==========================================================================================

#: `observabilidad.tracing`: `workflow_name` = `meta.nombre_proyecto`, fijo y estable. El
#: default del SDK es 'Agent workflow': con dos proyectos en la misma cuenta los traces se
#: mezclan y no hay forma de separarlos después.
WORKFLOW_NAME = "maxicare-daniela"

#: `fallos.limite_turnos`. El default del SDK es 10 y el peor caso legítimo del flujo ya
#: consume 12 (identificar, consultar precio, consultar agenda, recibir "lleno", intentar
#: otro bloque, recibir "se acaba de llenar", agendar, registrar, programar seguimiento,
#: redactar, más una regeneración por guardrail). Con 10, esa conversación se corta justo
#: cuando Daniela iba a ofrecer la alternativa.
LIMITE_TURNOS = 15

#: `observabilidad.tracing`: obligatorio en False porque `datos.datos_sensibles` tiene
#: contenido y `LecturaArchivo.contexto_clinico` es la excepción declarada en `contratos[]`.
#: En True, el contenido clínico quedaría en los traces, que se exportan fuera.
TRACE_INCLUDE_SENSITIVE_DATA = False

def version_de_prompt(texto: str) -> str:
    """Un identificador corto y estable del texto de un prompt, para el `trace_metadata`.

    Un hash y no un número que alguien suba a mano: se mueve solo cuando el prompt cambia de
    verdad y nadie tiene que acordarse. Doce caracteres porque va en cada trace y nadie lee
    un sha256 entero; es un identificador, no una defensa criptográfica.

    `hashlib` y no el `hash()` de Python, que está aleatorizado por `PYTHONHASHSEED`: daría
    una versión distinta en cada arranque del contenedor y el campo dejaría de servir para
    lo único que sirve, que es agrupar trazas del mismo prompt.
    """
    import hashlib

    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12]


def config_de_corrida(
    *,
    group_id: str | None = None,
    canal: str = "whatsapp",
    version_prompt: str | None = None,
):
    """El `RunConfig` que lleva TODA llamada al modelo de este proyecto.

    Hay tres consumidores de modelo --Daniela en `conversacion.py`, el lector en `lectura.py`
    y los evaluadores de guardrail en `guardrails.py`-- y los tres tienen que pasar por aquí.
    Cada uno construía (o no construía) el suyo, y así fue como el proyecto acabó con la
    misma fuga abierta en dos sitios distintos durante meses: `RunConfig()` nace en la 0.22.2
    con `trace_include_sensitive_data=True`, de modo que omitirlo sube al dashboard de OpenAI
    --que se exporta fuera de la clínica-- lo que escribe el paciente, lo que responde
    Daniela, las entradas y salidas de las nueve tools, y el data URL de cada radiografía.

    Va en código y no en `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` a propósito: una
    variable de entorno se olvida en el siguiente servidor y el fallo vuelve sin avisar.

    Con `False` los spans se siguen creando --latencia, coste, errores, turnos-- y lo único
    que se omite son las entradas y las salidas. Se construye uno POR CORRIDA y no una
    constante de módulo: un `RunConfig` compartido entre corridas concurrentes es estado
    compartido.

    El import va dentro y no arriba por la trampa de las variables vacías: `runtime.py` llama
    a `descartar_vacias_de_terceros()` ANTES de construir nada que hable con OpenAI, y este
    módulo se importa mucho antes que eso.

    `group_id` agrupa las trazas de un mismo EPISODIO. Es el UUID de la conversación, no el
    teléfono: un teléfono agrupa a una persona para siempre; una conversación agrupa lo que
    alguien va a querer leer entero cuando llegue un reclamo. Los TRES consumidores de modelo
    tienen que pasar el mismo, o el trace agrupado tendrá un agujero justo en los turnos con
    archivo, que son los más interesantes de leer.

    `version_prompt` se omite a propósito cuando quien llama no es Daniela ni el lector: los
    evaluadores de guardrail tienen su propio prompt, y poner el de Daniela ahí sería un dato
    plausible y falso.
    """
    from agents import RunConfig

    metadata: dict[str, str] = {"canal": canal}
    if version_prompt is not None:
        metadata["version_prompt"] = version_prompt

    return RunConfig(
        workflow_name=WORKFLOW_NAME,
        trace_include_sensitive_data=TRACE_INCLUDE_SENSITIVE_DATA,
        group_id=group_id,
        trace_metadata=metadata,
    )


#: `persistencia.compactacion`: recorte del historial por cantidad, cero llamadas de modelo.
#: La memoria larga no vive aquí: vive en el estado estructurado de Neon, que no se degrada.
#:
#: EL SDK CUENTA ITEMS, NO MENSAJES. Una llamada a tool y su resultado son dos items, y un
#: turno en que Daniela consulte el conocimiento, mire la agenda y registre el estado gasta
#: seis o siete él solo. Por eso el 40 del plan --justificado como «5 conversaciones
#: completas»-- era falso: podían ser cinco o seis TURNOS.
#:
#: Medido con `scripts/medir_historial.py` el PENDIENTE sobre PENDIENTE conversaciones reales:
#:   items por turno, media: PENDIENTE   peor caso: PENDIENTE
#:   items por conversación, p95: PENDIENTE   máximo: PENDIENTE
#: El número será <peor caso> x 6 turnos, que es una conversación de agendamiento completa
#: --saludo, tratamiento, fecha, disponibilidad, nombre y consentimiento, confirmación--.
#:
#: `None` mientras tanto, y `None` NO es un descuido: es el paso 1 de los tres del spec
#: --persistir sin límite, medir items/turno, fijar el número con el dato al lado--. Sale de
#: `None` cuando `scripts/medir_historial.py` tenga al menos VEINTE turnos reales que contar
#: en `public.agent_messages`, lo que exige desplegar primero. Con menos, el percentil no
#: significa nada y estaríamos sustituyendo una suposición por otra más cara. El `40` de
#: antes era exactamente esa suposición, y por eso se fue.
LIMITE_HISTORIAL_SESION: int | None = None

#: `limites.latencia_maxima`: "nunca instantánea... retardo variable... tope máximo de un
#: minuto". El retardo se sortea dentro de este rango antes de responder.
RETARDO_RESPUESTA_SEGUNDOS = (4, 55)

#: El búfer de mensajes: cuánto silencio espera Daniela antes de dar por cerrado lo que el
#: paciente quería decir.
#:
#: En WhatsApp nadie escribe párrafos. El saludo va en un mensaje, la pregunta en otro, y lo
#: que se le ocurrió después en un tercero. Sin esto cada trozo abre su propio turno y el
#: paciente recibe tres globos seguidos contestando a una sola idea. Medido en la primera
#: conversación real de la clínica: tres mensajes en 48 segundos, dos de ellos separados por
#: cinco, y sus respuestas le llegaron con siete segundos de diferencia.
#:
#: El caso que más va a doler no es ese, sino mandar la radiografía y escribir «¿esto qué
#: es?» justo después: sin agrupar, Daniela contesta la foto por un lado y la pregunta por
#: otro, y ninguna de las dos respuestas sabe de la otra mitad.
VENTANA_SILENCIO_SEGUNDOS = 20

#: Y el tope, porque quien escriba sin parar no puede mantener la ventana abierta para
#: siempre. Se cuenta desde el PRIMER mensaje del grupo, no desde el último.
#:
#: El número sale del presupuesto, no del gusto: `limites.latencia_maxima` es un minuto y un
#: turno tarda del orden de seis segundos. 45 + 6 deja margen; 55 no lo dejaría.
TOPE_BUFER_SEGUNDOS = 45

#: Lo que se le concede al lector DESPUÉS de que la ventana del búfer cerró. Corto a
#: propósito: la ventana ya le dio sus 20 segundos, esto es la cola.
MARGEN_LECTURA_SEGUNDOS = 3.0


# ==========================================================================================
# Configuración de despliegue
# ==========================================================================================


def _requerida(nombre: str) -> str:
    valor = os.environ.get(nombre, "").strip()
    if not valor:
        raise RuntimeError(
            f"falta la variable de entorno {nombre}. Copia `.env.ejemplo` a `.env` y "
            "completa los valores reales; `.env` no se versiona."
        )
    return valor


def _lista(nombre: str) -> tuple[str, ...]:
    """Una variable con varios valores separados por coma, o una tupla vacía.

    Vacía tiene significado propio aquí y no es lo mismo que «sin configurar»: es la que
    deja `/clearstate` sin existir. Ver `telefonos_prueba`.
    """
    crudo = os.environ.get(nombre, "").strip()
    return tuple(parte.strip() for parte in crudo.split(",") if parte.strip())


def _opcional(nombre: str, default: str = "") -> str:
    """El valor de la variable, o el default si no está **o está vacía**.

    Lo segundo importa más de lo que parece. Un `.env` generado a partir de `.env.ejemplo`
    trae las claves presentes y sin valor (`MAXICARE_MODELO_DANIELA=`), así que
    `os.environ.get(nombre, default)` devolvería la cadena vacía y el default nunca se
    usaría. El resultado sería un `Agent(model="")`, que el SDK acepta sin chistar porque no
    valida el identificador, y el fallo aparecería como un error del proveedor en mitad de
    una conversación con un paciente.

    Para los campos cuyo default es `""` esto no cambia nada.
    """
    return os.environ.get(nombre, "").strip() or default


#: Los tres modelos, confirmados el 2026-09-12 contra el catálogo de la cuenta y la
#: documentación de precios de OpenAI. El plan pedía «clase general, identificador a
#: confirmar al construir»; esto es esa confirmación.
#:
#: La familia 5.6 se reparte en tres tiers, y los nombres no lo dicen:
#:
#:     gpt-5.6-sol     flagship   $4.00 / $20.00 por millón
#:     gpt-5.6-terra   mini       $2.00 / $12.00
#:     gpt-5.6-luna    nano       $0.20 /  $1.20
#:
#: `luna` no es «el eficiente»: la documentación dice que corresponde al tier nano. Es
#: barato porque es el más pequeño, y el plan descartó el default económico para el agente
#: que habla con pacientes. Donde sí encaja es en los evaluadores de guardrail, que
#: responden una pregunta cerrada de sí o no.
#:
#: Queda abierto a propósito: `daniela` en `luna` costaría 10× menos. Con nueve tools y
#: esta carga de política es donde la obediencia a instrucciones empieza a fallar, y
#: fallaría eligiendo mal una tool, no dando una respuesta obviamente mala. Eso se mide con
#: la suite de evals de la fase 9 sobre los 50 casos reales de MaxiCare, no se opina.

#: El volumen: contesta cada mensaje.
MODELO_DANIELA = "gpt-5.6-terra"

#: Corre solo cuando llega un archivo, así que su costo total es marginal -- y es el único
#: de los tres que toca contenido clínico.
MODELO_LECTOR = "gpt-5.6-sol"

#: Una pregunta cerrada por llamada. Pagar el tier de Daniela aquí sería 10× por lo mismo.
MODELO_EVALUADOR = "gpt-5.6-luna"


@dataclass(frozen=True)
class Config:
    """Configuración de despliegue ya resuelta.

    Se construye con `Config.desde_entorno()`, que falla limpio y con un mensaje accionable
    si falta una variable obligatoria -- no con un `KeyError` a mitad de una conversación.
    """

    database_url: str
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    #: Con este secreto Meta firma cada webhook. Es lo único que distingue un POST de Meta
    #: de uno de cualquiera que haya adivinado la URL, que es pública.
    whatsapp_app_secret: str
    telegram_bot_token: str
    telegram_chat_doctores: str
    #: La cuenta de servicio entera, en base64 de una sola línea. NO una ruta a un archivo:
    #: así el despliegue no tiene que copiar un JSON al VPS ni acertarle a sus permisos, y
    #: la credencial viaja por el mismo camino que todas las demás. Un `.env` es una línea
    #: por variable, y un JSON pegado tal cual deja la variable valiendo `{`.
    google_sa_b64: str
    #: Cuál de los calendarios. Es un correo: el principal de alguien (`x@gmail.com`) o uno
    #: secundario (`c_...@group.calendar.google.com`). No basta con tenerlo: hay que
    #: compartir ESE calendario con el `client_email` de la cuenta de servicio dándole
    #: «Hacer cambios en los eventos», o la autenticación funciona y el calendario da 404.
    google_calendar_id: str
    modelo_daniela: str
    modelo_lector: str
    modelo_evaluador: str

    #: Con este secreto se firma la cookie de sesión de la interfaz web. Rotarlo cierra
    #: todas las sesiones abiertas de golpe -- ver `autenticacion.py`.
    secreto_sesion: str
    #: `False` en producción (detrás de Traefik, que sirve HTTPS) y `True` en local, donde
    #: `http://localhost` no acepta una cookie marcada `Secure`. El default es el seguro: si
    #: nadie la toca, la cookie solo viaja por HTTPS.
    permitir_cookie_insegura: bool

    #: ¿Daniela contesta por WhatsApp? Activo por defecto, que es lo que pidió el cliente:
    #: la clínica no despliega nada para que su asistente conteste.
    #:
    #: El interruptor existe para lo contrario -- poder callarla en diez segundos sin
    #: desplegar código. Con `MAXICARE_DANIELA_RESPONDE=0` el webhook sigue haciendo
    #: exactamente lo que lleva meses haciendo: registra el mensaje y le reenvía el archivo a
    #: los doctores por Telegram. Lo único que se apaga es la respuesta al paciente.
    #:
    #: Es el freno de mano de esta fase. Un prompt que se porte mal un lunes por la mañana se
    #: corta con una variable de entorno y un reinicio, sin tocar el repositorio y sin dejar a
    #: los doctores sin recibir radiografías -- que es lo que pasaría apagando el servicio.
    daniela_responde: bool

    #: Los números que pueden resetearse a sí mismos con `/clearstate` (ver `reseteo.py`).
    #:
    #: **Vacía por defecto, y eso es la política, no un descuido**: con la tupla vacía el
    #: comando no existe para nadie y el texto `/clearstate` llega a Daniela como cualquier
    #: otro mensaje. Desplegar esto en producción no abre ninguna puerta; hay que listar un
    #: número a propósito para que la puerta exista, y solo para ese número.
    #:
    #: Se compara por dígitos, así que da igual cómo se escriba: `+57 300 123 4567` y
    #: `573001234567` son el mismo número.
    telefonos_prueba: tuple[str, ...] = ()

    @classmethod
    def desde_entorno(cls) -> Config:
        return cls(
            database_url=_requerida("MAXICARE_DATABASE_URL"),
            whatsapp_token=_opcional("MAXICARE_WHATSAPP_TOKEN"),
            whatsapp_phone_number_id=_opcional("MAXICARE_WHATSAPP_PHONE_NUMBER_ID"),
            whatsapp_verify_token=_opcional("MAXICARE_WHATSAPP_VERIFY_TOKEN"),
            # Sin prefijo MAXICARE_ porque es el nombre que le da Meta y así se reconoce al
            # copiarlo de la consola de desarrolladores.
            whatsapp_app_secret=_opcional("WHATSAPP_APP_SECRET"),
            telegram_bot_token=_opcional("MAXICARE_TELEGRAM_BOT_TOKEN"),
            telegram_chat_doctores=_opcional("MAXICARE_TELEGRAM_CHAT_DOCTORES"),
            google_sa_b64=_opcional("MAXICARE_GOOGLE_SA_B64"),
            google_calendar_id=_opcional("MAXICARE_GOOGLE_CALENDAR_ID"),
            modelo_daniela=_opcional("MAXICARE_MODELO_DANIELA", MODELO_DANIELA),
            modelo_lector=_opcional("MAXICARE_MODELO_LECTOR", MODELO_LECTOR),
            modelo_evaluador=_opcional("MAXICARE_MODELO_EVALUADOR", MODELO_EVALUADOR),
            # Opcional aquí y comprobado al arrancar el servidor web, no requerida: los
            # scripts de la fase 3 y 4 y las pruebas construyen un `Config` sin tener ni
            # necesitar un secreto de sesión, y hacerla obligatoria los rompería a todos por
            # una variable que solo le importa a `runtime.py`.
            secreto_sesion=_opcional("MAXICARE_SECRETO_SESION"),
            permitir_cookie_insegura=_opcional("MAXICARE_COOKIE_INSEGURA", "0") == "1",
            # `!= "0"` y no `== "1"`: el default es responder, así que cualquier cosa que no
            # sea un 0 explícito la deja encendida. Al revés, un `.env` con la clave escrita
            # de otra forma la apagaría sin que nadie lo hubiera pedido, y el fallo sería
            # silencioso: pacientes escribiendo y nadie contestando.
            daniela_responde=_opcional("MAXICARE_DANIELA_RESPONDE", "1") != "0",
            telefonos_prueba=_lista("MAXICARE_TELEFONOS_PRUEBA"),
        )


def cargar_dotenv(ruta: str = ".env") -> None:
    """Carga un `.env` sencillo al entorno del proceso si existe.

    Deliberadamente mínimo y sin dependencia externa: lee `CLAVE=valor`, ignora comentarios
    y líneas vacías, y NO pisa una variable que ya venga del entorno real -- en el VPS las
    variables las pone el gestor de procesos, no un archivo.

    ------------------------------------------------------------------------------------
    Una clave sin valor NO se exporta
    ------------------------------------------------------------------------------------

    `CLAVE=` significa «esto todavía no está configurado», no «esto vale la cadena vacía».
    La diferencia parece sutil y no lo es: un `.env` copiado de `.env.ejemplo` trae casi
    todas las claves presentes y vacías, y exportarlas hace que una biblioteca de terceros
    las prefiera sobre su propio default.

    Esto no es hipotético. Con `OPENAI_BASE_URL=` exportado como cadena vacía, el cliente
    de OpenAI la toma como URL base y toda llamada falla con «Request URL is missing an
    'http://' or 'https://' protocol» -- un error que no menciona el `.env` por ninguna
    parte y que cuesta un rato largo rastrear hasta aquí.
    """
    import pathlib

    archivo = pathlib.Path(ruta)
    if not archivo.is_file():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        clave, valor = clave.strip(), valor.strip()
        if clave and valor and clave not in os.environ:
            os.environ[clave] = valor


#: Las variables que una biblioteca de terceros lee directamente de `os.environ`, sin pasar
#: nunca por `Config`. Las nuestras no hacen falta aquí: `_opcional` ya trata la cadena vacía
#: como ausente, y ese es su trabajo.
VARIABLES_DE_TERCEROS = (
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_WEBSOCKET_BASE_URL",
)


def descartar_vacias_de_terceros() -> list[str]:
    """Saca del entorno las variables de terceros que están presentes y vacías.

    `cargar_dotenv` ya se niega a exportar una clave sin valor, y por eso en local esto no
    hace nada. Pero el `.env` no es la única puerta: en el VPS las variables las pone el
    `env_file` de Docker, que **sí** exporta las vacías, una por cada línea `CLAVE=` del
    archivo. El filtro de `cargar_dotenv` ni siquiera llega a correr allí, porque dentro del
    contenedor no hay ningún `.env` que leer.

    Lo que costó descubrirlo, y por qué esto existe: con `OPENAI_BASE_URL=` presente y
    vacía, el cliente de OpenAI la prefiere sobre su propio default y arma `base_url=""`.
    Toda llamada al modelo muere en `APIConnectionError: Connection error.` --un error de
    red, que manda a revisar cortafuegos y DNS-- mientras el resto del sistema funciona
    perfectamente: `/salud` dice `configuracion: ok`, las trazas suben a esa misma API sin
    problema porque no usan ese cliente, y el paciente recibe el mensaje seguro de
    `atencion.py` como si Daniela hubiera decidido no saber la respuesta.

    Devuelve los nombres que quitó, para que quien llame pueda dejarlo en el log: una
    variable que desaparece del entorno sin que nadie lo diga es su propio misterio futuro.
    """
    quitadas = []
    for nombre in VARIABLES_DE_TERCEROS:
        if nombre in os.environ and not os.environ[nombre].strip():
            del os.environ[nombre]
            quitadas.append(nombre)
    return quitadas

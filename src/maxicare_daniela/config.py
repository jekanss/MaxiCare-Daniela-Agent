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

#: `persistencia.compactacion`: recorte del historial por cantidad, cero llamadas de modelo.
#: La memoria larga no vive aquí: vive en el estado estructurado de Neon, que no se degrada.
LIMITE_HISTORIAL_SESION = 40

#: `limites.latencia_maxima`: "nunca instantánea... retardo variable... tope máximo de un
#: minuto". El retardo se sortea dentro de este rango antes de responder.
RETARDO_RESPUESTA_SEGUNDOS = (4, 55)


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

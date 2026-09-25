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

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

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

#: El canal al que Daniela manda las solicitudes de privacidad --borrado, revocación, queja,
#: habeas data--. La Ley 2300 exige que exista uno y que sea ágil.
#:
#: El teléfono vive AQUÍ y no dentro del prompt porque hay dos sitios que tienen que decir
#: exactamente el mismo número: `agentes.INSTRUCCIONES_DANIELA`, que manda a darlo, y
#: `guardrails`, que borra sus dígitos del mensaje antes de contar cifras para que
#: `sin_cifra_no_documentada` no dispare sobre el propio canal de baja. El día que la clínica
#: cambie de número, cambiar solo el prompt devuelve un tripwire intermitente --mensaje
#: seguro al paciente y alerta falsa al doctor, a veces sí y a veces no--, que es el mismo
#: síntoma que ya costó una investigación con el rótulo «Confirmar» (no negociable 23).
#: `tests/test_agentes.py::test_el_telefono_de_privacidad_del_prompt_es_el_que_perdona_el_guardrail`
#: ata los dos.
TELEFONO_PRIVACIDAD = "+57 321 981 2422"
CORREO_PRIVACIDAD = "maxicarecol@gmail.com"

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

    Hay cuatro consumidores de modelo --Daniela en `conversacion.py`, el lector en
    `lectura.py`, los evaluadores de guardrail en `guardrails.py` y el analista de «sin
    resolver» en `analista.py`-- y los cuatro tienen que pasar por aquí.
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
    alguien va a querer leer entero cuando llegue un reclamo. Los TRES consumidores que viven
    dentro del turno de un paciente --Daniela, el lector y los evaluadores-- tienen que pasar
    el mismo, o el trace agrupado tendrá un agujero justo en los turnos con archivo, que son
    los más interesantes de leer. El cuarto, el analista de `analista.py`, no pasa ninguno a
    propósito: corre fuera de todo turno, sobre un caso YA agrupado que junta a muchos
    pacientes, y no hay una conversación suya que leer entera.

    `version_prompt` se omite a propósito cuando quien llama no es Daniela ni el lector: los
    evaluadores de guardrail y el analista tienen su propio prompt, y poner el de Daniela ahí
    sería un dato plausible y falso.
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
#: ==========================================================================================
#: ESTE NÚMERO SON DOS NÚMEROS DISTINTOS, Y HOY SOLO HAY UNO
#: ==========================================================================================
#:
#: 1. **El TOPE DE SEGURIDAD** (lo que vale hoy): existe para que el historial no pueda
#:    crecer hasta reventar la petición. No optimiza nada; solo acota.
#: 2. **El LÍMITE MEDIDO** (la tarea 13b, `PENDIENTE`): sale de `scripts/medir_historial.py`
#:    sobre conversaciones reales y será MÁS PEQUEÑO, porque optimiza coste, no seguridad.
#:
#: Poner el tope no cierra la 13b. Los cuatro números de abajo siguen sin medirse.
#:
#: ------------------------------------------------------------------------------------------
#: Por qué dejó de ser `None`: no era caro, era un paciente atascado para siempre
#: ------------------------------------------------------------------------------------------
#: La ventana de conversación de 24 h es DESLIZANTE --`tocar_conversacion` la renueva en cada
#: turno, incluido el que falla (`atencion.py`)--, así que un paciente que escriba una vez al
#: día conserva el mismo `session_id` indefinidamente y su historial no tiene tope. Cuando esa
#: petición cruza el techo, la API devuelve un `openai.BadRequestError` con
#: `code: context_length_exceeded` -- **comprobado contra la API real, no supuesto**. Y ese
#: error NO es una `AgentsException`: `conversacion.responder` no lo traduce, sube hasta el
#: `except Exception` de `atencion.atender` y el paciente recibe el MENSAJE_SEGURO. En el turno
#: siguiente, otra vez, sin caducidad --el turno fallido también renueva las 24 h-- y sin
#: `/clearstate` si su número no está en `MAXICARE_TELEFONOS_PRUEBA`, que nace vacía.
#:
#: ------------------------------------------------------------------------------------------
#: La aritmética del tope, con lo que se midió el 13/09/2026 (todo verificable)
#: ------------------------------------------------------------------------------------------
#: **Techo por petición: 500.000 tokens.** No es la ventana del modelo, es el límite de la
#: cuenta, y es el que manda porque es el más bajo: la API lo dijo con estas palabras al
#: mandarle una petición de ~600k tokens --«Request too large for gpt-5.6-terra ... on tokens
#: per min (TPM): Limit 500000, Requested 600289»--. La ventana del modelo es MAYOR: a ~600k
#: la validación de contexto no saltó (saltó la de TPM) y a ~1,2M sí
#: (`code: context_length_exceeded`), así que está entre los dos. Se usa el 500.000 porque
#: usar el número grande sería dimensionar contra un techo que esta cuenta no alcanza.
#:
#: **Fracción segura: 10 %.** El TPM es por MINUTO y para TODA la clínica, así que varios
#: pacientes del mismo minuto se lo reparten; y la petición lleva además las instrucciones,
#: los esquemas de las nueve tools, el mensaje del turno y la salida. Con el 10 %, diez turnos
#: simultáneos del mismo minuto siguen cabiendo.
#:
#: **Peor caso por item: 213 tokens.** Medido con el tokenizador del propio `gpt-5.6-terra`
#: (`usage.input_tokens`, no caracteres partidos por cuatro) sobre los 24 items REALES que
#: dejaron dos turnos con tools --incluida una regeneración por guardrail--: 2.229 tokens en
#: total, 92,9 de media, 213 el más grande (un item de `reasoning`).
#:
#:     500.000 tokens de techo  x  0,10 de fracción segura  =  50.000 tokens para historial
#:     50.000 tokens  /  213 tokens por item (peor caso)    =  234 items
#:                                                          -> 230, redondeando a la baja
#:
#: **Contraste, para ver que no recorta nada vivo:** esos mismos dos turnos dejaron 24 items,
#: o sea ~12 por turno. Una conversación de agendamiento completa --seis turnos-- son unos 72
#: items: el tope está 3 veces por encima. Y sin tope harían falta ~5.400 items (~450 turnos
#: en una sola conversación) para cruzar el techo; con él, ese camino no existe.
#:
#: `sin_salidas_huerfanas` es lo que hace que el recorte sea seguro cuando por fin ocurra: el
#: «últimos N» del SDK no sabe de pares `call_id` y puede dejar una salida de tool sin su
#: llamada, que la API rechaza.
#:
#: ------------------------------------------------------------------------------------------
#: EL SDK CUENTA ITEMS, NO MENSAJES
#: ------------------------------------------------------------------------------------------
#: Una llamada a tool y su resultado son dos items, y el `reasoning` es otro. Por eso el 40 del
#: plan --justificado como «5 conversaciones completas»-- era falso: con 12 items por turno
#: medidos, eran tres turnos y medio.
#:
#: ------------------------------------------------------------------------------------------
#: Lo que sigue PENDIENTE: los cuatro números de la 13b
#: ------------------------------------------------------------------------------------------
#: Por medir con `scripts/medir_historial.py`. Los 24 items de arriba son de DOS turnos
#: fabricados para medir tokens, no una muestra de conversaciones reales:
#:   fecha de la medición: PENDIENTE   conversaciones medidas: PENDIENTE
#:   items por turno, media: PENDIENTE   peor caso: PENDIENTE
#:   items por conversación, p95: PENDIENTE   máximo: PENDIENTE
#: El límite medido será <peor caso> x 6 turnos, que es una conversación de agendamiento
#: completa --saludo, tratamiento, fecha, disponibilidad, nombre y consentimiento,
#: confirmación--, y sustituirá a este tope cuando `medir_historial.py` tenga al menos VEINTE
#: turnos repartidos en cinco conversaciones QUE TENGAN FILAS en `public.agent_messages` --no
#: turnos de `conversaciones`, que los hay desde antes de esta fase y no traen un solo item que
#: contar--, lo que exige desplegar primero.
LIMITE_HISTORIAL_SESION: int | None = 230

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

#: Lo mismo para la transcripción de una nota de voz, y es MÁS LARGO a propósito.
#:
#: No es que la transcripción sea lenta --se midieron 0,86 a 1,64 segundos para audios de 10
#: a 17 segundos, así que con los 20 de la ventana por delante siempre habrá terminado--. Es
#: que lo que se pierde al agotarlo es de otra naturaleza: **una lectura que no llega es
#: información de más, y el doctor la recibe igual por su lado; una transcripción que no
#: llega es EL MENSAJE DEL PACIENTE.** Con los 3 segundos del lector, un mal minuto de la API
#: devuelve a Daniela a «ya recibimos tu audio», que es justo lo que esto vino a quitar.
#:
#: Ocho segundos no cuestan nada cuando la tarea ya terminó --que es el caso normal-- y son
#: lo único que separa un hipo de la API de un paciente sin respuesta.
MARGEN_TRANSCRIPCION_SEGUNDOS = 8.0


# ==========================================================================================
# El perímetro: que un desconocido no pueda quemar el saldo
# ==========================================================================================
#
# El número de WhatsApp de una clínica es público -- es un negocio, tiene que serlo. Eso
# significa que la firma HMAC de Meta, que es lo único que protege el webhook, no distingue a
# un paciente de alguien que quiere gastar el saldo de OpenAI: los dos entran por la puerta
# principal y los dos vienen firmados. Todo lo que sigue asume eso.
#
# Los defaults están calibrados contra el volumen REAL del 21/09/2026 --4 conversaciones y 11
# turnos al día, ver `docs/antes-de-produccion.md`-- y van muy por encima del uso legítimo a
# propósito: un freno que muerde a un paciente real es peor que el ataque que evita. Se suben
# o se bajan por `.env` sin desplegar código, que es justo lo que los hace útiles.

#: Mensajes de un mismo teléfono en una hora antes de dejar de llamar al modelo. Una
#: conversación intensa de verdad son 15-20; 40 es el doble largo.
#:
#: Se cuentan sobre `mensajes_entrantes`, que ya existe, y no sobre un contador aparte: una
#: tabla de contadores puede desincronizarse del hecho que cuenta, y la de los hechos no.
CUOTA_MENSAJES_HORA = 40

#: Archivos de un mismo teléfono en un día antes de dejar de LEERLOS. El archivo le sigue
#: llegando al doctor siempre -- eso no lo apaga nada, es la garantía de la fase 2. Lo que se
#: apaga es la llamada al modelo caro.
#:
#: El lector es el único consumidor que corre FUERA del búfer y FUERA del candado por
#: teléfono, así que es el multiplicador de coste más grande del sistema: N archivos son N
#: llamadas al modelo flagship, en paralelo y sin serializar.
#:
#: **30 y no 12, y el número lo fija un caso clínico, no el coste.** La primera versión eran
#: 12, calculados sobre «una tanda de radiografías son 3-6». Está mal contado: **una serie
#: periapical completa son 14-18 placas**, y un paciente que las mande todas habría cruzado
#: la cuota a mitad de la serie. El doctor habría seguido recibiendo los archivos --eso no lo
#: apaga nada-- pero sin la ficha que le escribe el lector, y justo en el caso en que más
#: falta hace: dieciocho placas sin leer son dieciocho imágenes que alguien tiene que abrir
#: una por una.
#:
#: 30 deja pasar esa serie entera con margen y sigue cortando el abuso, que empieza mucho más
#: arriba. Es la regla 1 de `cuotas.py` aplicada: un freno que muerde a un paciente real es
#: peor que el ataque que evita.
CUOTA_ARCHIVOS_DIA = 30

#: Lecturas de archivo simultáneas en todo el proceso. No es una cuota por teléfono: es el
#: techo absoluto de llamadas concurrentes al modelo caro, que además es lo que impide que N
#: archivos grandes estén a la vez en memoria en un contenedor de un solo worker.
LECTORES_CONCURRENTES = 3

#: Notas de voz de un mismo teléfono en un día antes de dejar de TRANSCRIBIRLAS. El audio le
#: sigue llegando al doctor siempre, igual que un archivo: lo que se apaga es la llamada.
#:
#: **Cuenta aparte de `CUOTA_ARCHIVOS_DIA` y no es una duplicación por descuido.** Son dos
#: gastos de órdenes de magnitud distintos --el lector es el modelo flagship con una imagen
#: dentro; esto son céntimos por minuto de audio-- y compartir contador significaría que
#: veinte notas de voz se comen la cuota de la serie periapical que va detrás. Ese caso es
#: justo el que hizo subir la de archivos de 12 a 30.
#:
#: 40 al día: quien manda notas de voz manda MUCHAS, porque es más cómodo que escribir, y
#: mordérselas devuelve al paciente exactamente al agujero que esto vino a tapar.
CUOTA_AUDIOS_DIA = 40

#: Dólares al día a partir de los cuales el General recibe UN aviso. No corta nada: avisa.
#: Cortar por gasto dejaría a los pacientes sin respuesta por una cifra, y esa decisión es de
#: la clínica y no del código.
#:
#: Referencia medida: una conversación de agendamiento completa de 6 turnos cuesta $0,095. El
#: umbral son ~50 conversaciones al día, muy por encima de las 4 de hoy.
ALERTA_GASTO_DIARIO_USD = 5.0

#: Hasta dónde se le pasa al modelo lo que escribió el paciente. Por encima se TRUNCA con una
#: marca visible, nunca se rechaza: rechazar deja al paciente sin respuesta.
#:
#: Existe por un fallo concreto y silencioso. `guardrails._preguntar` atrapa toda excepción y
#: devuelve «no dispara» --decisión correcta: un sistema mudo no protege a nadie-- así que una
#: entrada lo bastante larga para reventar el contexto del evaluador DESACTIVA el guardrail de
#: inyección para ese turno, dejando solo un `log.error`. El tope quita la causa.
#:
#: 8000 es el doble de lo que el panel ya exige a su chat de pruebas (`max_length=4000`). Esa
#: asimetría era el hueco: el carril validado era el interno y el que da a internet, no.
TOPE_ENTRADA_CARACTERES = 8000

#: Megabytes por encima de los cuales un archivo ni se descarga. Se mira el `Content-Length`
#: ANTES de bajar los bytes: hoy el archivo se carga entero en RAM y se re-serializa hacia
#: Telegram, o sea dos copias simultáneas por archivo, sin límite de concurrencia.
#:
#: 55 y no menos, y el número está elegido para NO cambiar nada que hoy funcione: Telegram
#: rechaza por encima de 50 MB, así que un archivo que cruce este tope ya está fallando hoy.
#: El freno contra la inundación es la cuota y el semáforo, no el tamaño -- bajarlo costaría
#: radiografías legítimas, que es exactamente lo que no se puede perder.
TOPE_DESCARGA_MB = 55

#: Precio por millón de tokens, por modelo: (entrada, entrada_cacheada, salida).
#:
#: El cacheado va aparte porque el proyecto corre con `prompt_cache_retention="24h"` y
#: cobrarlo al precio de entrada daría una factura inventada. Un modelo que no esté aquí se
#: anota con costo 0 y sus tokens igual: perder la cifra en dólares es aceptable, perder el
#: rastro de que hubo consumo no lo es.
PRECIOS_POR_MILLON: dict[str, tuple[float, float, float]] = {
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
}


#: Quién recibe el aviso de cada cita nueva, en formato internacional sin `+` -- que es como
#: los quiere la Graph API de Meta.
#:
#: Que estén aquí y no solo en el `.env` es deliberado y tiene precedente en este archivo
#: (`POLITICA_DATOS_URL`): no son un secreto, y son algo que no puede dejar de funcionar
#: porque alguien olvidó una variable al desplegar. Un aviso de cita que no sale no falla en
#: ninguna parte -- simplemente el doctor no se entera, y eso no se nota hasta que un paciente
#: llega a una cita que nadie esperaba.
#:
#: **El nombre miente un poco y se deja así a propósito.** El tercero, Santiago, no es doctor
#: -- es de administración -- y aun así recibe lo mismo. Renombrar la constante a algo como
#: `WHATSAPP_AVISOS_CITA` tocaría `config.py`, `aviso_citas.py` y sus pruebas para no cambiar
#: ni un comportamiento, así que lo que se corrige es el comentario y no el identificador. Lo
#: que sí hay que saber al añadir a alguien: esta lista **solo** alimenta el aviso de cita
#: nueva (`aviso_citas.ajustes_del_entorno`), no los escalamientos ni nada de Telegram, así
#: que entrar aquí no da acceso a nada -- da un WhatsApp por cada cita que agenda Daniela.
WHATSAPP_DOCTORES: tuple[str, ...] = (
    "573106492282",
    "573185790008",
    # Santiago, de administración. Añadido el 23/09/2026 a petición suya.
    "573132103985",
)


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


def _entero(nombre: str, default: int) -> int:
    """Un número entero de una variable, o el default si falta, está vacía o no es un número.

    Lo último es lo que importa. Un `MAXICARE_CUOTA_MENSAJES_HORA=cuarenta` con `int()` a
    secas tumbaría el arranque del servidor entero por una cuota mal escrita, y un webhook
    caído es peor que una cuota en su valor por defecto. Se registra y se sigue: el freno
    queda puesto en el número de fábrica, que es un estado seguro y no un agujero.
    """
    crudo = os.environ.get(nombre, "").strip()
    if not crudo:
        return default
    try:
        return int(crudo)
    except ValueError:
        log.warning("%s no es un entero (%r); se usa el default %s", nombre, crudo, default)
        return default


def _decimal(nombre: str, default: float) -> float:
    """Lo mismo para un umbral con decimales. Ver `_entero`."""
    crudo = os.environ.get(nombre, "").strip()
    if not crudo:
        return default
    try:
        return float(crudo)
    except ValueError:
        log.warning("%s no es un número (%r); se usa el default %s", nombre, crudo, default)
        return default


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

#: El que pasa una nota de voz a texto. **De los cuatro que tiene la cuenta, es el único que
#: sirve**, y eso no se deduce del nombre ni del precio: se midió el 21/09/2026 contra las
#: cinco notas de voz reales que había en `mensajes_entrantes`, todas `audio/ogg; codecs=opus`
#: grabadas con el WhatsApp de un teléfono de verdad.
#:
#:     gpt-transcribe          «¿con quién hablo yo, una doctora o un doctor?...»   correcto
#:     gpt-4o-transcribe       «Con xenáula se una doctora, onde totenui...»        inservible
#:     gpt-4o-mini-transcribe  «Ya conchena una doctora un doctor...»               inservible
#:     whisper-1               correcto, pero se come frases enteras
#:
#: Los dos del medio no es que acierten menos: devuelven algo que PARECE español y no lo es.
#: Eso es peor que no transcribir, porque Daniela contestaría a una frase inventada con toda
#: naturalidad. Quien cambie este identificador vuelve a correr
#: `scripts/probar_transcripcion.py` y mira las frases, no el código de estado.
#:
#: **Factura por DURACIÓN y no por tokens** (`UsageDuration(seconds=10.0)`), así que no tiene
#: sitio en `PRECIOS_POR_MILLON` y su gasto se anota con costo 0 -- ver `consumo.py`.
MODELO_TRANSCRIPTOR = "gpt-transcribe"

#: La política de tratamiento de datos, en constantes de módulo y no solo en los defaults del
#: dataclass, por la misma razón que los modelos: son el valor que `desde_entorno` usa cuando
#: la variable no está, y repetir el literal en los dos sitios es dejar que se separen.
POLITICA_DATOS_URL = "https://drive.google.com/file/d/1IB_XYUfc6Dqd51zBeURemfAVQMVnTy28/view"

#: La copia congelada de esta versión, con su SHA-256, está en `docs/politica/`.
POLITICA_DATOS_VERSION = "politica-v2.0-2026-09"


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
    modelo_transcriptor: str

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

    #: `!= "0"` y no `== "1"`: el default es analizar. Apagarlo NO apaga la captura -- los
    #: casos se siguen agrupando, solo se quedan sin las tres frases.
    analizar_sin_resolver: bool = True

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

    #: Lo que Telegram pone en la cabecera `X-Telegram-Bot-Api-Secret-Token` de cada update.
    #: Es a `/webhook/telegram` lo que `whatsapp_app_secret` es al webhook de Meta, con una
    #: diferencia que decide el comportamiento: **vacío significa CERRADO, no abierto**. Ese
    #: endpoint es la única puerta por la que algo de fuera puede hacer que el bot le escriba
    #: al WhatsApp de un paciente, así que sin secreto responde 403 a todo y el relevo
    #: simplemente no funciona. Lo genera y lo registra
    #: `scripts/configurar_webhook_telegram.py`.
    #:
    #: Va con default --y por eso al final del dataclass, donde los campos con default
    #: tienen que ir-- porque no configurarlo es un estado legítimo: el resto del sistema
    #: funciona entero sin relevo, igual que funcionaba antes de la 6C.
    telegram_webhook_secret: str = ""

    #: El nombre EXACTO de la plantilla aprobada en el Business Manager de Meta. Vacía
    #: --su default-- apaga el envío: el despachador decide igual, anota lo que habría hecho y
    #: no manda nada. Es lo que permite comprobar en producción que decide bien antes de que
    #: mande un solo mensaje. PENDIENTE: el nombre real, que sale de la aprobación de Meta.
    plantilla_recordatorio: str = ""

    #: El código de idioma EXACTO con el que la traducción está registrada en el Business
    #: Manager, **para las CUATRO plantillas** (I2, ronda 1 de revisión: el campo se llamaba
    #: `plantilla_recordatorio_idioma` y `runtime.py` ya lo pasaba a las cuatro, así que el
    #: nombre mentía sobre su propio alcance). No es cosmético y no admite un valor
    #: aproximado: si no coincide al carácter, Meta rechaza el envío entero con el error
    #: 132001 («template name does not exist in the translation») y una plantilla creada como
    #: `es_CO` no acepta `es`. **Las cuatro plantillas tienen que estar registradas en Meta con
    #: este MISMO código.** Si alguna quedara con otro, esa plantilla fallaría al 100% de sus
    #: envíos -y como la fila se marca ANTES de enviar (no negociable 21), cada una se pierde
    #: para siempre y el doctor recibe un aviso de fallo por cada una-, con rastro solo en el
    #: log y en una columna que nadie mira. Hoy no es un fallo vivo: el documento de plantillas
    #: (`docs/plantillas-meta-reactivacion.md`) fija `es` (Spanish) para las tres de
    #: reactivación, igual que la de recordatorio. PENDIENTE: el código real, que sale de la
    #: aprobación de Meta; `es` es el default más probable y por eso mismo no es una
    #: comprobación. La variable de entorno conserva su nombre viejo
    #: (`MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA`) a propósito: ya está en el `.env` del VPS, y
    #: renombrarla la habría dejado sin efecto en el primer despliegue sin que nadie lo notara.
    plantillas_idioma: str = "es"

    #: Las tres de reactivación. Vacías --su default-- dejan su tipo SIN enviar: el despachador
    #: decide igual y la fila se queda pendiente. Es el mismo modo de comprobación que
    #: `plantilla_recordatorio`, y aquí es además el estado normal hasta que Meta apruebe.
    #: Los nombres exactos que hay que pedir están en `docs/plantillas-meta-reactivacion.md`.
    plantilla_sin_agendar: str = ""
    plantilla_cancelada: str = ""
    plantilla_no_asistio: str = ""

    #: El interruptor de pánico de la reactivación (regla 10). **Apaga de verdad, y apaga las
    #: DOS mitades** (el AVISO que decía lo contrario era cierto en la tarea 4 y dejó de serlo
    #: en `5eb1352`, cuando se construyó el barrido; corregido en la revisión final, H4):
    #:
    #: - `barrido.encolar` no encola a nadie nuevo, y con `0` ni siquiera abre una conexión.
    #:   La tarea de fondo tampoco arranca.
    #: - `seguimientos.despachar` APLAZA todo lo comercial que ya estuviera encolado, vía
    #:   `runtime._freno_de_reactivacion` y la guarda FRENO de `seguimientos.decidir`. No lo
    #:   anula ni lo marca: esas filas salen cuando se vuelva a encender.
    #:
    #: Lo que sigue igual con esto en `0`: los recordatorios de cita, la atención y el relevo.
    #: Esa es la frontera del no negociable 25.
    #:
    #: Cambiarlo exige redesplegar (es una variable de entorno, no una perilla de
    #: `configuracion`). `!= "0"` y no `== "1"` porque el default es encendido, como
    #: `daniela_responde`.
    reactivacion_encendida: bool = True

    #: La dirección donde vive la política de tratamiento de datos que el paciente ve en su
    #: primer mensaje. El literal `PENDIENTE` APAGA el aviso: sale el mensaje limpio y no se
    #: registra nada. Dejó de ser el default el 16/09/2026.
    #:
    #: Va aquí y no solo en el `.env` a propósito, y es la única URL del proyecto que lo hace:
    #: no es un secreto --es un documento público-- y sí es algo que hay que poder acreditar.
    #: En el código, el día que cambió queda en `git log`; en una variable del VPS no queda en
    #: ninguna parte, y además se puede olvidar en un despliegue, que aquí significa dejar de
    #: informar sin que nada falle. El `.env` sigue pudiendo pisarla (las pruebas y
    #: `scripts/probar_atencion.py` la blanquean).
    #:
    #: El destino es Drive, decidido por el cliente. El riesgo conocido es que Drive deja
    #: subir una versión nueva sobre el mismo archivo sin que el enlace cambie, y entonces
    #: quien ya aceptó apunta a un texto que no vio. La contramedida es la copia congelada con
    #: su SHA-256 en `docs/politica/`, que es lo que hace verificable el `politica_version` de
    #: cada fila de `consentimientos`. Lo correcto sigue siendo servirlo desde el dominio de
    #: MaxiCare; esto es lo que hay hasta entonces.
    politica_datos_url: str = POLITICA_DATOS_URL

    #: El identificador de la versión vigente de la política. Se congela en cada fila de
    #: `consentimientos`: si la política cambia, hay que poder demostrar cuál vio cada
    #: persona. Cambiarlo NO reenvía el aviso a quien ya lo vio -- eso es una decisión
    #: aparte, y hoy no está construida.
    #:
    #: Nombra la versión que declara el PDF en su portada (2.0), no solo el mes: es lo que
    #: permite pasar de una fila de `consentimientos` al archivo exacto de `docs/politica/`.
    politica_datos_version: str = POLITICA_DATOS_VERSION

    # --------------------------------------------------------------------------------------
    # El perímetro. Los siete llevan default para que un `.env` viejo siga arrancando: un
    # despliegue que olvide una variable no puede significar quedarse SIN freno.
    # --------------------------------------------------------------------------------------

    cuota_mensajes_hora: int = CUOTA_MENSAJES_HORA
    cuota_archivos_dia: int = CUOTA_ARCHIVOS_DIA
    cuota_audios_dia: int = CUOTA_AUDIOS_DIA
    lectores_concurrentes: int = LECTORES_CONCURRENTES
    alerta_gasto_diario_usd: float = ALERTA_GASTO_DIARIO_USD
    tope_entrada_caracteres: int = TOPE_ENTRADA_CARACTERES
    tope_descarga_mb: int = TOPE_DESCARGA_MB

    #: El segundo freno de mano, hermano de `daniela_responde` y deliberadamente INDEPENDIENTE
    #: de él.
    #:
    #: Con `MAXICARE_LEER_ARCHIVOS=0` los archivos siguen llegándole al doctor igual --eso no
    #: lo apaga nada-- y lo único que se corta es la llamada al modelo que los lee. Es el
    #: interruptor para la emergencia económica, porque el lector es el consumidor más caro
    #: del sistema y el único que corre fuera del búfer y fuera del candado por teléfono.
    #:
    #: No cuelga de `daniela_responde` porque son dos emergencias distintas: «Daniela dice
    #: tonterías» y «el lector se está comiendo el saldo». Y porque ese interruptor promete,
    #: por escrito, que el doctor sigue recibiendo sus archivos y sus lecturas como siempre.
    #:
    #: `!= "0"`, como `daniela_responde`: el default es leer, y hace falta un 0 explícito.
    leer_archivos: bool = True

    #: El TERCER freno de mano, y es tercero a propósito: no cuelga de ninguno de los otros
    #: dos.
    #:
    #: Con `MAXICARE_TRANSCRIBIR_AUDIO=0` la nota de voz le llega al doctor igual --como
    #: siempre-- y Daniela vuelve a lo de antes: avisa de que llegó y le pide al paciente que
    #: lo escriba.
    #:
    #: **La tentación era colgarlo de `leer_archivos`**, que ya significa «no pagues modelos
    #: por archivos». Sería el error simétrico del que ese interruptor ya evita. `leer_archivos`
    #: apaga algo que es del DOCTOR y promete por escrito no tocar lo del paciente; esto apaga
    #: algo que es del PACIENTE. Encadenarlos haría que apagar «el lector se está comiendo el
    #: saldo» degrade en silencio la atención de quien manda notas de voz, que es la mitad de
    #: los pacientes de esta clínica. Son tres emergencias distintas y por eso son tres
    #: interruptores.
    #:
    #: Tampoco cuelga de `daniela_responde`, por la misma razón que no cuelga el lector: la
    #: transcripción también baja al hilo del doctor, así que callar a Daniela no puede
    #: quitarle al doctor un texto que ya tenía.
    transcribir_audio: bool = True

    #: A `1`, las cuotas CUENTAN y AVISAN pero no cortan a nadie.
    #:
    #: Existe porque el 21/09/2026 todavía no se sabe cuál es el uso normal: con 4
    #: conversaciones al día, cualquier umbral es una corazonada. Esto permite encender la
    #: medición hoy y el corte cuando haya datos para elegir el número. El default es cortar
    #: --el riesgo está abierto ahora mismo-- pero la puerta de atrás existe y es de una
    #: variable, no de un despliegue.
    cuota_modo_observacion: bool = False

    #: Los WhatsApp de los doctores que reciben el aviso de cada cita nueva. Van en el código
    #: y no solo en el `.env` por el mismo motivo que `politica_datos_url`: no son un secreto
    #: --son los teléfonos de la clínica-- y sí son algo que no puede dejar de funcionar
    #: porque alguien olvidó una variable en un despliegue. El día que cambien, queda en
    #: `git log`. El `.env` los puede pisar.
    whatsapp_doctores: tuple[str, ...] = WHATSAPP_DOCTORES

    #: El nombre EXACTO de la plantilla de Meta que avisa de una cita nueva. Vacía --su
    #: default-- apaga el envío sin apagar nada más: la cita se crea igual y queda el log de
    #: lo que se habría mandado. Mismo patrón que `plantilla_recordatorio`, y por la misma
    #: razón: poder comprobar en producción que decide bien antes de que mande un solo
    #: mensaje. PENDIENTE hasta que Meta la apruebe.
    plantilla_cita_nueva: str = ""

    #: El código de idioma EXACTO con el que la traducción quedó registrada. Si no coincide al
    #: carácter, Meta rechaza el envío entero con el error 132001. Ver
    #: `plantilla_recordatorio_idioma`, que ya pagó esta lección.
    plantilla_cita_nueva_idioma: str = "es"

    #: La plantilla que avisa de un MOVIMIENTO de agenda: el paciente confirmó, no vendrá o
    #: cambió de hora. UNA sola para los tres casos, con el asunto en el primer hueco.
    #:
    #: Tres plantillas se leerían mejor --el doctor sabría qué pasó por el encabezado-- y
    #: costarían tres aprobaciones de Meta en vez de una, con cada retoque de redacción
    #: volviendo a pasar por revisión. Con el asunto como DATO, la redacción vive en Python:
    #: el día que la clínica quiera que diga otra cosa es un commit, no un trámite de 24 h.
    #:
    #: Vacía --su default-- apaga el envío y deja el log de lo que se habría mandado, igual
    #: que `plantilla_cita_nueva`. PENDIENTE hasta que Meta la apruebe.
    plantilla_movimiento_agenda: str = ""

    #: Como `plantilla_cita_nueva_idioma`: si no coincide al carácter con la traducción
    #: registrada, Meta rechaza el envío entero con el 132001.
    plantilla_movimiento_agenda_idioma: str = "es"

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
            telegram_webhook_secret=_opcional("MAXICARE_TELEGRAM_WEBHOOK_SECRET"),
            plantilla_recordatorio=_opcional("MAXICARE_PLANTILLA_RECORDATORIO"),
            # El nombre de la variable de entorno NO cambia (I2): ya está en el `.env` del
            # VPS con este nombre, y gobierna las cuatro plantillas desde antes de este
            # rename -- ver el comentario del campo.
            plantillas_idioma=_opcional(
                "MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA", "es"
            ),
            plantilla_sin_agendar=_opcional("MAXICARE_PLANTILLA_SIN_AGENDAR"),
            plantilla_cancelada=_opcional("MAXICARE_PLANTILLA_CANCELADA"),
            plantilla_no_asistio=_opcional("MAXICARE_PLANTILLA_NO_ASISTIO"),
            reactivacion_encendida=_opcional("MAXICARE_REACTIVACION", "1") != "0",
            politica_datos_url=_opcional("MAXICARE_POLITICA_DATOS_URL", POLITICA_DATOS_URL),
            politica_datos_version=_opcional(
                "MAXICARE_POLITICA_DATOS_VERSION", POLITICA_DATOS_VERSION
            ),
            google_sa_b64=_opcional("MAXICARE_GOOGLE_SA_B64"),
            google_calendar_id=_opcional("MAXICARE_GOOGLE_CALENDAR_ID"),
            modelo_daniela=_opcional("MAXICARE_MODELO_DANIELA", MODELO_DANIELA),
            modelo_lector=_opcional("MAXICARE_MODELO_LECTOR", MODELO_LECTOR),
            modelo_evaluador=_opcional("MAXICARE_MODELO_EVALUADOR", MODELO_EVALUADOR),
            modelo_transcriptor=_opcional(
                "MAXICARE_MODELO_TRANSCRIPTOR", MODELO_TRANSCRIPTOR
            ),
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
            analizar_sin_resolver=_opcional("MAXICARE_ANALIZAR_SIN_RESOLVER", "1") != "0",
            telefonos_prueba=_lista("MAXICARE_TELEFONOS_PRUEBA"),
            # El perímetro. Todas con default, y el default es el freno puesto: un `.env`
            # anterior a esta fase arranca protegido sin tocar nada.
            cuota_mensajes_hora=_entero("MAXICARE_CUOTA_MENSAJES_HORA", CUOTA_MENSAJES_HORA),
            cuota_archivos_dia=_entero("MAXICARE_CUOTA_ARCHIVOS_DIA", CUOTA_ARCHIVOS_DIA),
            cuota_audios_dia=_entero("MAXICARE_CUOTA_AUDIOS_DIA", CUOTA_AUDIOS_DIA),
            lectores_concurrentes=_entero(
                "MAXICARE_LECTORES_CONCURRENTES", LECTORES_CONCURRENTES
            ),
            alerta_gasto_diario_usd=_decimal(
                "MAXICARE_ALERTA_GASTO_DIARIO_USD", ALERTA_GASTO_DIARIO_USD
            ),
            tope_entrada_caracteres=_entero(
                "MAXICARE_TOPE_ENTRADA_CARACTERES", TOPE_ENTRADA_CARACTERES
            ),
            tope_descarga_mb=_entero("MAXICARE_TOPE_DESCARGA_MB", TOPE_DESCARGA_MB),
            # `== "1"` y no `!= "0"`: aquí el default tiene que ser el lado que PROTEGE, y el
            # modo observación es el que no protege. Un `.env` con la clave escrita de otra
            # forma deja las cuotas cortando, que es el fallo seguro.
            cuota_modo_observacion=_opcional("MAXICARE_CUOTA_MODO_OBSERVACION", "0") == "1",
            leer_archivos=_opcional("MAXICARE_LEER_ARCHIVOS", "1") != "0",
            # `!= "0"` como los otros dos frenos: el default es transcribir. Un `.env` con la
            # clave escrita de otra forma deja a los pacientes atendidos, que es el lado
            # barato de equivocarse -- lo caro sería una clínica muda ante las notas de voz
            # sin que nadie lo hubiera pedido.
            transcribir_audio=_opcional("MAXICARE_TRANSCRIBIR_AUDIO", "1") != "0",
            # `or` y no un default en `_lista`: la lista vacía aquí NO tiene significado
            # propio --a diferencia de `telefonos_prueba`, donde vacía es la política-- así
            # que un `.env` sin la clave cae a los números del código.
            whatsapp_doctores=_lista("MAXICARE_WHATSAPP_DOCTORES") or WHATSAPP_DOCTORES,
            plantilla_cita_nueva=_opcional("MAXICARE_PLANTILLA_CITA_NUEVA"),
            plantilla_cita_nueva_idioma=_opcional(
                "MAXICARE_PLANTILLA_CITA_NUEVA_IDIOMA", "es"
            ),
            plantilla_movimiento_agenda=_opcional("MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA"),
            plantilla_movimiento_agenda_idioma=_opcional(
                "MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA_IDIOMA", "es"
            ),
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

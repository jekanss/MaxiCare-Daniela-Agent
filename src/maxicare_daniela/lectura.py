"""El muro: qué mitad de un archivo leído va a cada destinatario.

Del mismo archivo salen dos cosas con dos destinatarios y umbrales opuestos de qué se puede
decir:

    contexto_clinico  ─────────►  Telegram, al tema del paciente
    todo lo demás     ─────────►  el contexto de `daniela`

Ese reparto es el muro del diseño. No es un prompt ni un guardrail: es de dónde el código
saca cada cosa. Por eso `repartir` es una función pura y vive sola en la cabecera de este
módulo, donde se puede probar sin levantar nada.

La frontera de este módulo es la misma que la de `ingesta.py`: no importa `runtime.py` ni
ningún framework web.
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging

from agents import RunConfig, Runner

from . import consumo, persistencia
from .agentes import VERSION_PROMPT_LECTOR, lector_archivos
from .canales import ArchivoDescargado
from .config import (
    LECTORES_CONCURRENTES,
    MODELO_LECTOR,
    TRACE_INCLUDE_SENSITIVE_DATA,
    WORKFLOW_NAME,
    config_de_corrida,
)
from .contratos import LecturaArchivo, LecturaNoClinica

log = logging.getLogger("maxicare.lectura")


def repartir(lectura: LecturaArchivo) -> tuple[str, LecturaNoClinica]:
    """Parte una lectura en (lo que ve el doctor, lo que ve Daniela).

    Por debajo de `confianza == "alta"` el tratamiento se borra. Lo dice el contrato desde
    la fase 1 --«por debajo de 'alta', la capa de ingesta fuerza tratamiento
    ='no_identificado' para que Daniela pregunte en vez de asumir»-- y se hace aquí, en
    código, porque una instrucción puede desobedecerse y una línea de código no.
    """
    seguro = lectura.confianza == "alta"
    no_clinica = LecturaNoClinica(
        tipo_documento=lectura.tipo_documento,
        tratamiento=lectura.tratamiento if seguro else "no_identificado",
        origen=lectura.origen,
        fecha_documento=lectura.fecha_documento,
        confianza=lectura.confianza,
    )
    return lectura.contexto_clinico, no_clinica


# ==========================================================================================
# El tema del paciente
# ==========================================================================================

#: Un candado por teléfono para «buscar o crear el tema».
#:
#: Dos archivos del mismo número nuevo llegando a la vez crearían DOS temas, y el índice
#: único `uq_pacientes_topic` no lo impide: son dos ids distintos, así que el segundo UPDATE
#: pisa al primero y deja un tema huérfano en Telegram al que nadie volverá a escribir.
#:
#: Mismo límite conocido que el candado del turno en `atencion.py`: es de PROCESO. Con más
#: de un worker o más de una réplica deja de proteger y haría falta un `pg_advisory_lock`.
#: El contenedor corre con un solo worker a propósito, y por eso hoy alcanza.
_candados_de_tema: dict[str, asyncio.Lock] = {}

#: Cuánto puede tardar «buscar o crear el tema» antes de que el archivo se mande al General.
#:
#: `ingesta.procesar_mensaje` es quien lo aplica, porque es quien tiene el archivo en la mano.
#: Vive aquí porque es el reloj de ESTA operación: en el primer archivo de un paciente,
#: `asegurar_tema` encadena `crear_tema` y `cerrar_tema`, cada una con `TIMEOUT_NORMAL` (15 s),
#: así que con Telegram lento el archivo del doctor esperaría medio minuto por algo que el
#: propio diseño clasifica como degradable. Y el candado por teléfono lo multiplica: el
#: segundo archivo del mismo número espera detrás del primero.
TOPE_SEGUNDOS_TEMA = 5.0


def nombre_del_tema(telefono: str, nombre_perfil: str | None) -> str:
    """«Ana Perez · +573001112233», o solo el teléfono si WhatsApp no mandó perfil.

    Un tema sin nombre es peor que uno feo: el doctor no sabría de quién es el hilo.
    """
    limpio = (nombre_perfil or "").strip()
    return f"{limpio} · +{telefono}" if limpio else f"+{telefono}"


async def asegurar_tema(
    *, telefono: str, nombre_perfil: str | None, database_url: str, telegram
) -> int | None:
    """El hilo de ese número, creándolo si es el primero. `None` solo si no se pudo.

    Ese `None` no es un error que haya que propagar: significa «manda el archivo al tema
    General, como antes». Degradar es aceptable; perder el archivo no lo es.

    **CUALQUIER número tiene hilo, sea paciente o no.** Hasta la migración 014 no era así, y
    la razón era buena: el hilo era una columna de `pacientes`, y `atencion._leer_estado`
    deriva `identidad_verificada` de la EXISTENCIA de esa fila. Crearla aquí convertía a
    cualquier desconocido que mandara una foto en un paciente verificado —con el nombre que
    él mismo puso en su perfil de WhatsApp de por medio— y en el mismo turno, porque
    `runtime._entregar` corre `procesar_mensaje` antes que `atender`.

    Lo que cambió no es el criterio, es dónde vive el hilo. Ahora está en `temas_telegram`,
    atado al TELÉFONO, así que **abrirle un hilo a alguien ya no le da una identidad** y el
    guardrail sigue exactamente igual de estricto.

    Y el precio de no hacerlo estaba medido en producción (14/09/2026, primer relevo real):
    un lead se quedaba sin hilo, así que sus textos no se archivaban en ninguna parte, sus
    radiografías caían en el General —que es donde los doctores miran TODO— y el hilo que le
    abría el botón del relevo nacía vacío. Justo la persona que más falta hace atender: la
    que escribe por primera vez.

    El `None` que devuelve ya solo significa «Telegram o la base fallaron»: manda el archivo
    al General, como antes. Degradar es aceptable; perder el archivo no lo es.
    """
    candado = _candados_de_tema.setdefault(telefono, asyncio.Lock())
    async with candado:
        try:
            existente = await asyncio.to_thread(_tema_de, database_url, telefono)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo consultar el tema de %s; va al General", telefono)
            return None
        if existente:
            return existente

        nombre = nombre_del_tema(telefono, nombre_perfil)
        try:
            tema = await telegram.crear_tema(nombre)
        except Exception:  # noqa: BLE001 -- ancho a propósito: no solo `ErrorDeCanal`
            # (Telegram respondió "ok: false") sino también un fallo de transporte real
            # (timeout, conexión caída). Cualquiera de los dos degrada al General; propagarlo
            # tumbaría la entrega del archivo al doctor, que es la garantía de la fase 2.
            log.exception("Telegram no dejó crear el tema de %s; va al General", telefono)
            return None

        # Cerrarlo es lo que pone el candado, y va antes de guardarlo: si el cierre falla,
        # queremos el id igual --el tema existe-- pero con constancia de que quedó abierto.
        quedo_abierto = False
        try:
            await telegram.cerrar_tema(tema)
        except Exception:  # noqa: BLE001 -- misma razón que arriba: un fallo de transporte
            # al cerrar tampoco puede tumbar la entrega del archivo.
            quedo_abierto = True
            log.error(
                "el tema %s de %s quedó ABIERTO: cualquiera del grupo puede escribirle al "
                "paciente hasta que alguien lo cierre a mano",
                tema,
                telefono,
            )

        try:
            await asyncio.to_thread(
                _guardar_tema, database_url, telefono, tema, quedo_abierto
            )
        except Exception:  # noqa: BLE001
            log.exception("el tema %s no quedó guardado; se usa igual en este turno", tema)
        return tema


def _tema_de(database_url: str, telefono: str) -> int | None:
    """El hilo de ese número, o `None` si todavía no tiene ninguno.

    Ya no pregunta si es paciente. Desde la migración 014 el hilo vive en `temas_telegram`,
    atado al teléfono, y tener hilo dejó de significar estar verificado -- ver `asegurar_tema`.
    """
    with persistencia.conectar(database_url) as conn:
        return persistencia.tema_del_paciente(conn, telefono)


def _guardar_tema(database_url: str, telefono: str, tema: int, abierto: bool = False) -> None:
    """Ata el hilo al número. Sigue sin crear filas en `pacientes`: ya no hace falta."""
    with persistencia.conectar(database_url) as conn:
        persistencia.guardar_tema(conn, telefono=telefono, topic_id=tema, abierto=abierto)


# ==========================================================================================
# El rescate: lo único que abre un hilo sin que llegue un archivo
# ==========================================================================================

#: Cuánto atrás se miran los textos sin archivar. La conversación caduca a las 24 h, así que
#: más allá no es «lo que este paciente venía diciendo», es arqueología de otro asunto.
HORAS_DE_RESCATE = 24

#: Tope de frases volcadas. Telegram corta un mensaje en 4096 caracteres y lo RECHAZA
#: entero si se pasa: un volcado demasiado largo no se recorta, se pierde.
TOPE_FRASES_RESCATE = 20


async def rescatar_hilo(
    *, telefono: str, nombre_perfil: str | None, database_url: str, telegram
) -> int | None:
    """Abre el hilo de ese número si no lo tiene, y vuelca lo que escribió mientras no había.

    **Es la única puerta por la que algo que no es un archivo abre un hilo**, y el no
    negociable 14 sigue en pie para todo lo demás: un texto suelto no lo abre. Lo que cambia
    es quién decide, y el filtro es el ESCALAMIENTO, no el texto: un número equivocado no
    hace escalar a Daniela, así que no estrena expediente. El paciente con dolor, sí.

    El agujero que tapa, medido en producción el 16/09/2026: entre que un número se queda sin
    hilo --`/clearstate` lo borra, o alguien borra el tema-- y que un archivo se lo vuelva a
    abrir, sus textos no se archivan en ninguna parte. Cuatro mensajes seguidos, «Quiero
    sacarme una muela» y «Duele mucho?» entre ellos, quedaron con `telegram_message_id` NULL
    y no llegaron a ningún sitio. Lo único que el doctor vio de esa persona fue el
    escalamiento en el General, cuyo resumen parafrasea lo que había preguntado: desde fuera
    se lee exactamente como si los mensajes del paciente hubieran caído en el General.

    Idempotente por construcción: lo volcado queda con su `telegram_message_id`, así que la
    consulta deja de devolverlo y el siguiente escalamiento no lo repite.

    **Nunca propaga.** Lo llama un escalamiento, y un escalamiento que revienta por no haber
    podido archivar un «buenas tardes» deja a un paciente esperando a un doctor que no se
    entera. Si algo falla, los textos siguen pendientes y el próximo escalamiento reintenta.
    """
    try:
        tema = await asegurar_tema(
            telefono=telefono,
            nombre_perfil=nombre_perfil,
            database_url=database_url,
            telegram=telegram,
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo abrir el hilo de %s para el rescate", telefono)
        return None
    if not tema:
        return None

    try:
        pendientes = await asyncio.to_thread(_textos_sin_archivar, database_url, telefono)
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer los textos sin archivar de %s", telefono)
        return tema
    if not pendientes:
        return tema

    # Un solo mensaje con todas las frases, no uno por frase: son mudas, pero veinte
    # depósitos seguidos convierten el expediente en un muro por el que hay que bajar.
    cuerpo = "\n".join(f"• {html.escape(texto)}" for _, texto in pendientes)
    aviso = (
        "📝 <b>Lo que escribió antes de que existiera este hilo</b>\n"
        "<i>No se había podido archivar en ninguna parte.</i>\n\n"
        f"{cuerpo}"
    )
    try:
        message_id = await telegram.enviar_mensaje(aviso, tema_id=tema, silencioso=True)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo volcar lo pendiente de %s en su hilo", telefono)
        return tema

    # Marcar va DESPUÉS del envío, como el aviso de la política y al revés que un
    # recordatorio: aquí el riesgo es dejar constancia de un volcado que nunca salió --y
    # perder esas frases para siempre--, no repetirlo. Repetir un volcado es inocuo.
    try:
        await asyncio.to_thread(
            _marcar_archivados, database_url, [w for w, _ in pendientes], message_id
        )
    except Exception:  # noqa: BLE001
        log.exception("el volcado de %s salió pero no quedó marcado; podría repetirse", telefono)
    return tema


def _textos_sin_archivar(database_url: str, telefono: str) -> list[tuple[str, str]]:
    """Los textos de ese número que se procesaron sin llegar a ningún hilo.

    `telegram_message_id IS NULL` junto a `fallo IS NULL` es exactamente la firma que deja
    `ingesta`: «se procesó sin reenviar: es un texto y su número no tiene tema». Un mensaje
    con `fallo` NO entra: ese sí se intentó entregar y se registró como perdido, y volcarlo
    aquí lo borraría del índice por el que se vigila lo que de verdad falló.
    """
    with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT wamid, texto
              FROM mensajes_entrantes
             WHERE telefono = %s
               AND telegram_message_id IS NULL
               AND fallo IS NULL
               AND texto IS NOT NULL
               AND texto <> ''
               AND recibido_en > now() - interval '{HORAS_DE_RESCATE} hours'
             ORDER BY recibido_en
             LIMIT {TOPE_FRASES_RESCATE}
            """,
            (telefono,),
        )
        return [(w, x) for w, x in cur.fetchall()]


def _marcar_archivados(database_url: str, wamids: list[str], message_id: int) -> None:
    """Deja constancia de dónde acabó cada frase volcada. Es lo que hace idempotente al
    rescate: con `telegram_message_id` puesto, `_textos_sin_archivar` deja de devolverla."""
    if not wamids:
        return
    with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE mensajes_entrantes SET telegram_message_id = %s WHERE wamid = ANY(%s)",
            (message_id, list(wamids)),
        )
        conn.commit()


# ==========================================================================================
# El lector
# ==========================================================================================

#: Los dos tipos que el lector puede leer de verdad. `audio`, `voice`, `video` y `sticker`
#: siguen con el aviso factual de la fase 2: no se pagan, y no se fingen.
TIPOS_QUE_SE_LEEN = frozenset({"image", "document"})

#: Por encima de esto no se manda al modelo. El doctor recibe el archivo igual --eso no
#: cambia nunca-- y Daniela usa la entrada de siempre.
TOPE_BYTES_LECTOR = 20 * 1024 * 1024


def vale_la_pena_leer(tipo: str, tamano: int) -> bool:
    """Si el archivo es de un tipo legible y no pasa el tope de tamaño."""
    return tipo in TIPOS_QUE_SE_LEEN and tamano <= TOPE_BYTES_LECTOR


def entrada_para_el_lector(archivo: ArchivoDescargado, tipo: str) -> list[dict]:
    """El archivo, con la forma que pide la Responses API.

    Los nombres de campo están verificados por introspección de la 0.22.2 instalada, no de
    memoria: `input_image` lleva `image_url`; `input_file` lleva `filename` y `file_data`.

    `file_data` lleva el data URL completo (`data:<mime>;base64,<...>`), no el base64 a
    secas: confirmado con una llamada real a la API el 12/09/2026 (spec §4.8) -- el base64
    pelado devuelve `400 invalid_value` sobre ese mismo campo.
    """
    datos = base64.b64encode(archivo.contenido).decode("ascii")
    if tipo == "image":
        contenido = {
            "type": "input_image",
            "image_url": f"data:{archivo.mime};base64,{datos}",
            "detail": "auto",
        }
    else:
        contenido = {
            "type": "input_file",
            "filename": archivo.nombre,
            "file_data": f"data:{archivo.mime};base64,{datos}",
        }
    return [{"role": "user", "content": [contenido]}]


def _config_de_corrida(group_id: str | None) -> RunConfig:
    """La CUARTA salida del muro: los traces, que se exportan fuera de la clínica.

    Comprobado contra la 0.22.2 instalada: `RunConfig()` nace con
    `trace_include_sensitive_data=True` y `tracing_disabled=False`. Sin este `run_config`,
    cada archivo que manda un paciente subía a la plataforma de OpenAI el data URL entero de
    su radiografía Y el `LecturaArchivo` completo, con el `contexto_clinico` dentro.

    `TRACE_INCLUDE_SENSITIVE_DATA` es `False` y `config.py` ya decía por qué: «obligatorio en
    False porque `datos.datos_sensibles` tiene contenido y `LecturaArchivo.contexto_clinico`
    es la excepción declarada en `contratos[]`». Lo que faltaba era cablearla.

    La construcción vive en `config.config_de_corrida`, por donde pasan los TRES consumidores
    de modelo del proyecto. Estaba duplicada aquí, y esa duplicación es cómo la misma fuga
    siguió abierta en `conversacion.py` y en los evaluadores de guardrail mientras este lado
    ya estaba cerrado.

    `group_id` es el `id_conversacion` de quien mandó el archivo, o `None` cuando todavía no
    hay una conversación viva que agrupe esta traza -- ver `ingesta._conversacion_viva`. Se
    acepta el agujero antes que inventar un id que no corresponde a ninguna conversación.
    """
    return config_de_corrida(
        group_id=group_id, canal="whatsapp", version_prompt=VERSION_PROMPT_LECTOR
    )


# ==========================================================================================
# El techo de lecturas simultáneas
# ==========================================================================================
#
# El lector es el consumidor más caro del sistema --modelo flagship, y una imagen o un PDF
# entran como muchos tokens-- y era el único que corría SIN ningún freno: `ingesta` arranca
# una `asyncio.Task` por ARCHIVO, fuera del búfer y fuera del candado por teléfono. Cincuenta
# archivos seguidos eran cincuenta llamadas en paralelo mientras el búfer las agrupaba, muy
# correctamente, en un solo turno de Daniela. El búfer protege a Daniela; al lector no lo
# protegía nada.
#
# No es una cuota por teléfono --esa vive en `atencion`-- sino el techo del proceso entero: es
# lo que impide que N archivos grandes estén a la vez en memoria en un contenedor de un solo
# worker, que es el vector de agotamiento más barato que tiene el sistema.
#
# Perezoso porque el módulo se importa antes de que exista un bucle de eventos, y un
# `Semaphore` creado en el import se ataría al bucle equivocado en las pruebas.
_semaforo: asyncio.Semaphore | None = None


def _permiso_para_leer() -> asyncio.Semaphore:
    global _semaforo
    if _semaforo is None:
        _semaforo = asyncio.Semaphore(LECTORES_CONCURRENTES)
    return _semaforo


async def leer_archivo(
    archivo: ArchivoDescargado,
    *,
    tipo: str,
    correr=None,
    group_id: str | None = None,
    database_url: str | None = None,
    telefono: str | None = None,
) -> LecturaArchivo | None:
    """Corre `lector_archivos`. Devuelve `None` si falla, y NUNCA propaga.

    `correr` existe solo para poder probar esto sin red.

    Que devuelva `None` en vez de lanzar no es pereza: quien llama es una tarea de fondo que
    ya entregó el archivo al doctor. Una excepción ahí solo llegaría a un log.
    """
    ejecutar = correr or (
        lambda entrada: Runner.run(
            lector_archivos, entrada, run_config=_config_de_corrida(group_id)
        )
    )
    try:
        # El `async with` envuelve solo la llamada, no el `entrada_para_el_lector`: ese es el
        # `base64`, que es CPU y no red, y tenerlo dentro alargaría el tiempo que cada lector
        # ocupa una plaza sin estar hablando con nadie.
        entrada = entrada_para_el_lector(archivo, tipo)
        async with _permiso_para_leer():
            corrida = await ejecutar(entrada)
    except Exception:  # noqa: BLE001 -- ver el docstring
        log.exception("el lector no pudo con %s", archivo.nombre)
        return None

    if database_url:
        # `database_url` opcional: las pruebas llaman a esto con un doble en `correr` y sin
        # base, y una lectura no puede exigir Postgres para funcionar. Sin él no se anota y
        # ya está -- la lectura es lo que importa, la contabilidad es instrumentación.
        await consumo.anotar(
            corrida,
            agente="lector",
            modelo=MODELO_LECTOR,
            database_url=database_url,
            id_conversacion=group_id,
            telefono=telefono,
        )
    return corrida.final_output


#: Los rótulos con los que el lector puede abrir una línea de la ficha. Tienen que ser los
#: mismos que enumera `INSTRUCCIONES_LECTOR`: lo que no esté aquí sale sin negrita, que es
#: degradar bien --se lee igual, solo más plano-- pero deja de guiar el ojo.
ROTULOS_DE_LA_FICHA = ("Motivo", "Hallazgos", "Antecedentes", "Piden", "Ojo")


def formatear_para_el_doctor(clinico: str) -> str:
    """Le da forma a la ficha SIN tocar una palabra de lo que dice.

    Dos cosas que solo funcionan en este orden:

    1. **Escapar primero, marcar después.** `canales.py` manda todo con `parse_mode: "HTML"`,
       así que un «canal < 2 mm» sin escapar le devuelve a Telegram un 400 que se traga la
       lectura entera. Y si se marcara antes de escapar, `html.escape` convertiría nuestras
       propias `<b>` en texto visible. Por eso el modelo escribe texto plano y las etiquetas
       las pone el código: el contenido nunca puede inyectar HTML.
    2. **La negrita va SOLO en el rótulo.** Resaltar el contenido clínico sería decidir qué
       es importante dentro de lo clínico, y eso lo decide el doctor.

    El primer renglón es la cabecera --tipo · especialidad · emisor · fecha-- y va entero en
    negrita: es el título de la ficha. Si el modelo se saltara el formato y devolviera un
    párrafo corrido, esto lo deja pasar tal cual, solo con la primera línea resaltada.
    """
    lineas: list[str] = []
    for cruda in html.escape(clinico, quote=False).splitlines():
        linea = cruda.strip()
        if not linea:
            continue
        rotulo, dos_puntos, resto = linea.partition(":")
        if dos_puntos and rotulo in ROTULOS_DE_LA_FICHA:
            lineas.append(f"<b>{rotulo}:</b>{resto}")
        elif not lineas:
            lineas.append(f"<b>{linea}</b>")
        else:
            lineas.append(linea)
    return "\n".join(lineas)


async def leer_y_repartir(
    archivo: ArchivoDescargado,
    *,
    tipo: str,
    telegram,
    tema_id: int | None,
    correr=None,
    group_id: str | None = None,
    silencioso: bool = False,
    database_url: str | None = None,
    telefono: str | None = None,
) -> LecturaNoClinica | None:
    """Lee, manda lo clínico al tema del paciente y devuelve SOLO la mitad no clínica.

    Aquí es donde el muro se ejerce: `repartir` devuelve dos cosas, una sale por Telegram y
    la otra es el valor de retorno. La clínica no se guarda en ninguna variable que
    sobreviva a esta función.

    `silencioso` lo fija quien llama y vale lo mismo que valió para el archivo: la lectura
    acompaña al archivo y suena exactamente donde sonó él. Si notificara por su cuenta,
    haber callado el archivo no habría servido de nada. Ver NOTA DEL SILENCIO en `canales`.
    """
    leida = await leer_archivo(
        archivo,
        tipo=tipo,
        correr=correr,
        group_id=group_id,
        database_url=database_url,
        telefono=telefono,
    )
    if leida is None:
        try:
            await telegram.enviar_mensaje(
                "⚠️ No se pudo leer este archivo automáticamente.",
                tema_id=tema_id,
                silencioso=silencioso,
            )
        except Exception:  # noqa: BLE001
            log.exception("no se pudo avisar de la lectura fallida")
        return None

    clinico, no_clinica = repartir(leida)
    # Escapado y marcado en un solo sitio, y en ese orden: ver `formatear_para_el_doctor`.
    # No se reutiliza `ingesta._escapar` --misma lógica, `html.escape`-- para no crear un
    # import circular: `ingesta` ya importa `lectura`.
    #
    # El «📄 Lectura» de la 6B se fue: la cabecera de la ficha ya dice qué es el documento,
    # mucho mejor que la palabra «Lectura», y en un celular cada renglón de más empuja lo
    # decisivo fuera de la pantalla.
    try:
        await telegram.enviar_mensaje(
            f"📄 {formatear_para_el_doctor(clinico)}",
            tema_id=tema_id,
            silencioso=silencioso,
        )
    except Exception:  # noqa: BLE001
        log.exception("la lectura no llegó a Telegram; el archivo sí está")
    return no_clinica

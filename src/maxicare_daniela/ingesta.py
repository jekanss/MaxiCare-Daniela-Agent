"""Qué se hace con un mensaje que entra por WhatsApp.

Tres operaciones que el plan saca deliberadamente del alcance del agente —descargar el
archivo, reenviarlo a los doctores y registrar que llegó— porque tienen que ocurrir SIEMPRE,
aunque el modelo falle, aunque el guardrail salte, aunque la base de conocimiento no
responda. El enlace de WhatsApp caduca: si se esperara a que un agente decida, para cuando
decida ya no hay archivo.

Por eso no son tools. Una tool la llama el modelo cuando quiere; esto pasa antes de que el
modelo exista en el flujo.

La frontera de este módulo: no importa `runtime.py` ni ningún framework web, así que se
puede probar entero sin levantar un servidor. Lo que sí hace IO vive en `canales.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from . import lectura as lectura_mod
from . import persistencia
# Solo por el teclado del botón. `relevo` importa `lectura`, `persistencia` y `canales`, y
# NUNCA `ingesta`: no hay ciclo, y `tests/test_estructura.py` vigila que siga sin haberlo.
from . import relevo as relevo_mod
from .canales import ErrorDeCanal, HiloInvalido, Telegram, WhatsApp

log = logging.getLogger("maxicare.ingesta")

#: La única referencia FUERTE a las tareas del lector. `asyncio` solo las guarda en un
#: `WeakSet`, así que una tarea que nadie sostiene se la puede llevar el recolector a
#: medias: el turno de Daniela ya terminó --ese es justo el caso en que el `shield` de
#: `atencion._recoger_lecturas` existe-- y el doctor se quedaría sin su lectura clínica en
#: silencio. El `add_done_callback` la saca en cuanto acaba, así que el `set` no crece.
_lectores_vivos: set[asyncio.Task] = set()

#: Lo mismo, para las tareas de `asegurar_tema` que se pasaron del tope y siguen por su
#: cuenta. Van en un `set` propio y no en `_lectores_vivos` porque no son lo mismo: una es la
#: lectura clínica del doctor y esta es el hilo donde caerá su PRÓXIMO archivo.
_temas_en_curso: set[asyncio.Task] = set()

#: Los tipos de WhatsApp que traen un archivo adjunto. `sticker` está incluido porque técnica
#: mente es media aunque nunca sea clínico; excluirlo haría que un sticker se procesara como
#: un texto vacío y se perdiera el registro.
TIPOS_CON_ARCHIVO = frozenset({"image", "document", "audio", "voice", "video", "sticker"})

#: Cada cuánto puede volver a SONAR el aviso de «mandó archivos» en el General.
#:
#: Las mismas 24 horas que `persistencia.conversacion_viva`, y no es una coincidencia que
#: convenga documentar: la tanda de archivos de un paciente ES su conversación. Quien manda
#: la radiografía y a los dos minutos la foto de la encía está contando UNA cosa, y merece
#: un aviso, no dos.
#:
#: No se ata a la fila de `conversaciones` justamente porque en el primer archivo de una
#: conversación nueva esa fila TODAVÍA NO EXISTE: `runtime._entregar` corre
#: `procesar_mensaje` antes que `atencion.atender`, que es quien la crea. Atarlo ahí haría
#: sonar el primero (sin fila que marcar) y otra vez el segundo (fila recién creada, sin
#: marca) — exactamente los dos timbrazos que esto existe para evitar. `mensajes_entrantes`
#: sí está escrita: la escribe `_registrar` en la primera línea de `procesar_mensaje`.
VENTANA_AVISO_ARCHIVO_HORAS = 24

#: Cómo se le nombra a cada tipo delante de un doctor. Un «mandó un image» no lo lee nadie.
NOMBRE_HUMANO = {
    "image": "una imagen",
    "document": "un documento",
    "audio": "un audio",
    "voice": "una nota de voz",
    "video": "un video",
    "sticker": "un sticker",
    "text": "un mensaje",
    "location": "su ubicación",
    "contacts": "un contacto",
}


@dataclass(frozen=True)
class MensajeEntrante:
    """Un mensaje de WhatsApp, ya normalizado.

    No es un contrato Pydantic de `contratos.py` porque no lo produce ni lo consume ningún
    agente: es del transporte. Meterlo allá lo pondría del lado equivocado de la frontera.
    """

    wamid: str
    telefono: str
    nombre_perfil: str | None
    tipo: str
    texto: str | None = None
    media_id: str | None = None
    mime: str | None = None
    nombre_archivo: str | None = None

    @property
    def trae_archivo(self) -> bool:
        return self.tipo in TIPOS_CON_ARCHIVO and bool(self.media_id)


# ==========================================================================================
# Autenticidad del webhook
# ==========================================================================================


def firma_valida(cuerpo: bytes, cabecera: str | None, app_secret: str) -> bool:
    """¿Este POST lo mandó Meta de verdad?

    La URL del webhook es pública: cualquiera que la adivine puede hacerle POST con un
    payload inventado. Sin esta comprobación, un desconocido podría hacer aparecer en el
    grupo de los doctores un «archivo de un paciente» que nunca existió.

    Meta firma el cuerpo crudo con HMAC-SHA256 y el App Secret. Se compara con
    `compare_digest` y no con `==` para no filtrar información por el tiempo de comparación.
    """
    if not app_secret:
        # Sin secreto configurado no se puede verificar, y no se finge que sí: quien llame
        # decide qué hacer. En producción esto es un fallo de configuración.
        return False
    if not cabecera or not cabecera.startswith("sha256="):
        return False
    esperado = hmac.new(app_secret.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecera.removeprefix("sha256="))


# ==========================================================================================
# Parseo — puro, sin red
# ==========================================================================================


def extraer_mensajes(payload: dict) -> list[MensajeEntrante]:
    """Saca los mensajes de un webhook de WhatsApp Cloud API.

    El payload viene anidado en cuatro niveles (`entry[].changes[].value.messages[]`) y trae
    mezclados los mensajes de pacientes con los acuses de entrega de lo que enviamos
    nosotros. Esos acuses van en `value.statuses`, no en `value.messages`, así que leer solo
    `messages` ya los descarta.

    Nunca lanza por un payload raro: un webhook que no se entiende no debe tumbar el
    servidor, porque Meta lo reintentaría en bucle.
    """
    mensajes: list[MensajeEntrante] = []

    for entrada in payload.get("entry") or []:
        for cambio in entrada.get("changes") or []:
            valor = cambio.get("value") or {}

            # El nombre que el paciente puso en su perfil. Es lo único parecido a una
            # identidad que llega gratis, y le ahorra al doctor un «¿quién es este número?».
            perfiles = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in valor.get("contacts") or []
            }

            for m in valor.get("messages") or []:
                wamid = m.get("id")
                telefono = m.get("from")
                tipo = m.get("type")
                if not (wamid and telefono and tipo):
                    continue

                cuerpo = m.get(tipo) if isinstance(m.get(tipo), dict) else {}

                # Pulsar un quick reply de una plantilla NO llega como `text`: Meta manda
                # `type: "button"`, con el rótulo dentro de `button.text` y lo que la
                # plantilla definió en `button.payload`. Se prefiere `text` porque es
                # literalmente lo que el paciente leyó antes de pulsar.
                #
                # Sin esta línea los dos botones de `recordatorio_cita` --«Confirmar» y
                # «Necesito cambiarla»-- no sirven para nada: `texto` queda en None y
                # `atencion._entrada_para_el_modelo` le entrega al modelo «[El paciente envió
                # algo de tipo «button». No trae texto.]», sobre lo que no puede hacer nada.
                # Medido en la primera prueba real de la plantilla, el 15/09/2026: el paciente
                # pulsó «Necesito cambiarla» y Daniela respondió con su saludo de primer
                # contacto. El prompt tiene desde la 017 un bloque que dice que un «sí» del
                # paciente se refiere a la cita del recordatorio -- y ese «sí» no llegaba.
                del_boton = None
                if tipo == "button":
                    del_boton = cuerpo.get("text") or cuerpo.get("payload")

                mensajes.append(
                    MensajeEntrante(
                        wamid=wamid,
                        telefono=telefono,
                        nombre_perfil=perfiles.get(telefono),
                        tipo=tipo,
                        # Un adjunto puede traer texto propio en `caption`: «esta es la
                        # radiografía que me pidieron». Perderlo sería perder el contexto.
                        texto=(
                            (m.get("text") or {}).get("body")
                            or cuerpo.get("caption")
                            or del_boton
                        ),
                        media_id=cuerpo.get("id"),
                        mime=cuerpo.get("mime_type"),
                        nombre_archivo=cuerpo.get("filename"),
                    )
                )
    return mensajes


def _tamano_legible(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _escapar(texto: str) -> str:
    """Telegram en modo HTML rompe el mensaje si el texto trae `<`, `>` o `&`.

    Y el texto viene de un desconocido: un paciente que escriba `<3` no debe poder romper
    el formato del mensaje que ven los doctores.
    """
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def componer_aviso(m: MensajeEntrante, *, tamano: int | None = None) -> str:
    """El texto que ven los doctores. Sin una sola interpretación clínica.

    En esta fase no hay modelo: el pie dice lo que el archivo ES (tipo, peso, nombre), nunca
    lo que el archivo MUESTRA. Esa segunda cosa la produce `lector_archivos` en la fase 6, y
    su destino sigue siendo este mismo tema.
    """
    quien = _escapar(m.nombre_perfil) if m.nombre_perfil else "Sin nombre en el perfil"
    lineas = [f"🦷 <b>{quien}</b> · +{m.telefono}"]

    que = NOMBRE_HUMANO.get(m.tipo, f"algo de tipo «{m.tipo}»")
    if m.trae_archivo:
        detalle = [m.mime or "tipo desconocido"]
        if tamano is not None:
            detalle.append(_tamano_legible(tamano))
        lineas.append(f"Mandó {que} — {' · '.join(detalle)}")
    else:
        lineas.append(f"Mandó {que}")

    if m.texto:
        lineas.append(f"\n💬 <i>{_escapar(m.texto)}</i>")

    return "\n".join(lineas)


# ==========================================================================================
# El viaje del archivo
# ==========================================================================================


@dataclass
class Resultado:
    wamid: str
    nuevo: bool
    reenviado: bool
    fallo: str | None = None
    #: La tarea del lector, si se arrancó. `atencion.atender` la recoge cuando cierra la
    #: ventana del búfer. Es una `Task` y no un valor a propósito: esperarla aquí pondría
    #: una llamada al modelo delante de la entrega del archivo al doctor.
    lectura: asyncio.Task | None = None


def _soltar_tema(tarea: asyncio.Task) -> None:
    """Saca la tarea del `set` y le recoge la excepción, si la hubo.

    Una tarea que se pasó del tope sigue viva sin nadie esperándola: si reventara, su
    excepción se quedaría sin recoger y `asyncio` la sacaría por el destructor, con un
    «Task exception was never retrieved» sin contexto y a destiempo.
    """
    _temas_en_curso.discard(tarea)
    if not tarea.cancelled() and tarea.exception() is not None:
        log.error("la creación del tema falló por detrás: %s", tarea.exception())


async def _tema_o_general(
    m: MensajeEntrante, *, database_url: str, telegram: Telegram
) -> int | None:
    """`asegurar_tema` con un reloj delante. `None` significa «al General».

    El `except` de `asegurar_tema` ya degradaba ante un fallo; lo que faltaba era que el
    RELOJ degradara también. Sin esto, el primer archivo de un paciente encadena `crear_tema`
    y `cerrar_tema` --15 s de timeout cada una-- delante de la entrega al doctor, y el
    candado por teléfono se lo suma al segundo archivo del mismo número. Choca de frente con
    la garantía de la fase 2: nada de lo que se escriba puede retrasar la entrega del archivo.

    El `shield` no es adorno. Sin él, el tope CANCELA la corrutina a mitad de `crear_tema`, y
    si Telegram ya había creado el tema nadie lo guarda: queda un tema huérfano en el grupo y
    el siguiente archivo crea otro. Con él, la operación termina por su cuenta --crea, cierra
    y guarda-- y el hilo queda listo para el próximo archivo de esa persona; lo único que se
    pierde es que ESTE archivo cae en el General. `_temas_en_curso` la sostiene mientras
    tanto, por la misma razón que `_lectores_vivos` sostiene al lector.
    """
    tarea = asyncio.ensure_future(
        lectura_mod.asegurar_tema(
            telefono=m.telefono,
            nombre_perfil=m.nombre_perfil,
            database_url=database_url,
            telegram=telegram,
        )
    )
    _temas_en_curso.add(tarea)
    tarea.add_done_callback(_soltar_tema)
    try:
        return await asyncio.wait_for(
            asyncio.shield(tarea), timeout=lectura_mod.TOPE_SEGUNDOS_TEMA
        )
    except asyncio.TimeoutError:
        log.warning(
            "el tema de %s tardó más de %.0f s: este archivo va al General y el tema se "
            "sigue creando por detrás para el siguiente",
            m.telefono,
            lectura_mod.TOPE_SEGUNDOS_TEMA,
        )
        return None


# ══════════════════════════════════════════════════════════════════════════════════════════
# NOTA DEL TEXTO SIN TEMA — por qué un mensaje puede no llegar a Telegram y no ser un fallo
# ══════════════════════════════════════════════════════════════════════════════════════════
#
# Hasta el 13/09/2026 CADA mensaje entrante se reenviaba al General. Se escribió en la fase 2,
# cuando Daniela NO EXISTÍA y el sistema entero era «recibir y reenviar»; la fase 6A conectó
# al agente y nadie recortó el reenvío. El resultado medido en producción: el doctor recibía
# siete notificaciones por una conversación que Daniela resolvió sola, y el ESCALAMIENTO
# --lo único que le pedía algo-- llegaba enterrado entre las otras seis.
#
# Ahora un texto va al tema de su paciente, en silencio, y NO va a ninguna otra parte:
#
#   tiene tema  -> se deposita ahí, mudo. El hilo queda completo para quien lo abra.
#   no tiene    -> no se manda nada a Telegram. Queda en `mensajes_entrantes` y en el
#                  historial del agente, que es donde de verdad vive la conversación.
#
# Un texto NUNCA crea el tema (`_tema_existente` es una consulta, no `lectura.asegurar_tema`).
# Es la regla que cerró la fase 6: si cada «buenas tardes» abriera un hilo, el grupo sería
# inservible en una semana. El hilo lo abre el primer ARCHIVO del paciente.
#
# ─── Lo que esto le hace a `mensajes_entrantes`, y hay que saberlo ───────────────────────
#
# La migración 004 dejó escrito que «si `telegram_message_id` es NULL y `fallo` también, el
# mensaje entró y nunca llegó a los doctores: es el estado que hay que vigilar». Ese estado
# acaba de dejar de ser anómalo: es lo normal para el texto de un número sin tema. La señal
# de alarma pasa a ser la columna de al lado, que es justo sobre la que ya está construido
# el índice `ix_mensajes_sin_reenviar`:
#
#   reenviado_en NULL  + fallo NULL  -> entró y NADIE lo procesó. Esto sí se vigila.
#   reenviado_en PUESTA + telegram_message_id NULL -> se decidió no reenviarlo. Normal.
#   fallo PUESTO                     -> Telegram lo rechazó.
#
# `_marcar_reenviado` es quien sostiene esa distinción y por eso acepta un id nulo.


async def procesar_mensaje(
    m: MensajeEntrante,
    *,
    whatsapp: WhatsApp,
    telegram: Telegram,
    database_url: str,
    tema_general: int | None = None,
    leer_archivos: bool = True,
) -> Resultado:
    """Recibe → deduplica → descarga → reenvía → registra.

    El orden no es negociable. La deduplicación va primero porque un reintento de Meta no
    debe volver a gastar la descarga; la descarga va inmediatamente después porque el enlace
    caduca; y el registro del reenvío va al final porque solo entonces es cierto.
    """
    # `persistencia` es psycopg síncrono. Llamarlo directo dentro de un handler async
    # bloquearía el bucle de eventos mientras Neon responde. Con el volumen de la clínica
    # apenas se notaría, pero un `to_thread` cuesta una línea.
    nuevo = await asyncio.to_thread(_registrar, database_url, m)
    if not nuevo:
        log.info("wamid %s ya estaba registrado: es un reintento de Meta, se ignora", m.wamid)
        return Resultado(m.wamid, nuevo=False, reenviado=False)

    tarea: asyncio.Task | None = None
    try:
        if m.trae_archivo:
            # Primero los bytes. Todo lo demás puede esperar; esto no.
            archivo = await whatsapp.descargar_media(
                m.media_id, nombre_original=m.nombre_archivo
            )
            tema = await _tema_o_general(m, database_url=database_url, telegram=telegram)
            destino = tema or tema_general
            pie = componer_aviso(m, tamano=archivo.tamano)
            # Durante un relevo el hilo está en vivo y todo lo del paciente suena ahí. Fuera
            # del relevo es un expediente y no suena nunca. Ver `_en_relevo`.
            en_relevo = bool(tema) and await asyncio.to_thread(
                _en_relevo, database_url, m.telefono
            )
            # Silencioso SOLO si cae en el tema del paciente. Si no hay tema, el archivo va
            # al General --es el caso del número que todavía no es paciente-- y ahí tiene
            # que sonar: nadie va a abrir un hilo que no existe para encontrarlo.
            try:
                telegram_id = await telegram.enviar_archivo(
                    archivo,
                    tipo_whatsapp=m.tipo,
                    pie=pie,
                    tema_id=destino,
                    silencioso=bool(tema) and not en_relevo,
                )
            except HiloInvalido as e:
                # Alguien borró el hilo de esta persona. Telegram no lo avisa por ningún
                # evento, y `relevo.barrer` solo lo sondea si está EN RELEVO, así que este
                # rechazo es la única noticia que va a llegar nunca. Se olvida la fila --si
                # no, cada archivo suyo se estrella contra el mismo hilo muerto, para
                # siempre-- y el archivo cae al General.
                #
                # `tema = None` no es cosmético: de ahí cuelgan las tres decisiones de
                # abajo. El lector deposita su lectura donde cayó el archivo, el envío
                # suena --nadie va a abrir un hilo que ya no existe para encontrarlo-- y el
                # aviso «están en su tema» NO se manda, porque ya no es verdad.
                log.warning(
                    "el hilo de %s ya no existe (%s); se olvida y el archivo va al General",
                    m.telefono,
                    e,
                )
                await asyncio.to_thread(_olvidar_tema, database_url, m.telefono)
                tema, destino = None, tema_general
                telegram_id = await telegram.enviar_archivo(
                    archivo,
                    tipo_whatsapp=m.tipo,
                    pie=pie,
                    tema_id=destino,
                    silencioso=False,
                )
            # A partir de aquí el archivo YA está entregado. Nada de lo que sigue —el aviso
            # al General, el arranque del lector— puede convertir esta entrega en un fallo,
            # así que el aviso queda en su propio try/except y el lector arranca ANTES de
            # intentarlo: un aviso que revienta no puede quitarle al doctor la lectura.
            # `leer_archivos` lo calcula `runtime` y vale dos cosas a la vez: el freno de mano
            # (`MAXICARE_DANIELA_RESPONDE`) y la cuota de archivos del número.
            #
            # Lo primero tapaba un agujero que hacía inútil el freno. `procesar_mensaje` corre
            # ANTES que `atencion.atender`, y el chequeo de `daniela_responde` vive dentro de
            # `atender`: con la variable en 0, cada imagen seguía pagando una llamada al
            # modelo caro. El interruptor que existe para callar a Daniela en diez segundos no
            # apagaba el gasto MÁS grande del sistema, y eso no se veía en ningún log porque
            # el lector no responde al paciente -- solo deposita en el tema del doctor.
            #
            # Y da igual cuál de las dos lo apague: **el archivo ya está entregado** cuando se
            # llega aquí. Lo que se salta es la lectura, nunca la entrega. Esa es la garantía
            # de la fase 2 y no la toca ningún freno de este perímetro.
            if leer_archivos and lectura_mod.vale_la_pena_leer(m.tipo, archivo.tamano):
                # El `group_id` del lector es la conversación viva, si la hay -- pero se
                # resuelve DENTRO de la tarea de fondo, nunca antes de crearla:
                # `_conversacion_viva` habla con Neon, y esperarla aquí retrasaría el
                # regreso de `procesar_mensaje`, que es justo la garantía que la fase 2 ya
                # midió y protegió (`tests/test_ingesta.py::test_el_lector_no_retrasa_la_
                # entrega_del_archivo`). Si es el PRIMER mensaje de un paciente nuevo,
                # todavía no existe conversación --la crea `atencion._leer_estado`, después
                # de que esto arranque-- y ese trace queda fuera del grupo. Se acepta:
                # inventarle un id que no corresponde a ninguna conversación sería peor que
                # no tenerlo.
                async def _leer_con_grupo() -> lectura_mod.LecturaNoClinica | None:
                    grupo = await asyncio.to_thread(
                        _conversacion_viva, database_url, m.telefono
                    )
                    return await lectura_mod.leer_y_repartir(
                        archivo,
                        tipo=m.tipo,
                        telegram=telegram,
                        tema_id=destino,
                        group_id=grupo,
                        # Misma regla que el archivo: la lectura acompaña al archivo, así que
                        # suena exactamente donde sonó él. Si notificara aparte, callar el
                        # archivo no habría servido de nada.
                        silencioso=bool(tema) and not en_relevo,
                        # Para anotar lo que costó esta lectura. El lector corre con el modelo
                        # flagship, así que es el consumidor cuyo gasto más hace falta ver por
                        # separado del de Daniela: un pico aquí es alguien mandando archivos y
                        # se arregla con la cuota, no con el búfer.
                        database_url=database_url,
                        telefono=m.telefono,
                    )

                tarea = asyncio.create_task(_leer_con_grupo())
                # Ver `_lectores_vivos`: sin esta referencia fuerte, la tarea puede morir a
                # medias en cuanto el turno de Daniela suelte la suya.
                _lectores_vivos.add(tarea)
                tarea.add_done_callback(_lectores_vivos.discard)
            # `not en_relevo`: durante un relevo el doctor YA tiene el archivo sonándole en
            # el hilo donde está conversando. El aviso al General sería el mismo timbrazo por
            # segunda vez, en el sitio donde menos falta hace.
            if tema and not en_relevo and await asyncio.to_thread(
                _primer_archivo_de_la_tanda, database_url, m.telefono, m.wamid
            ):
                # El archivo no cae en el General, así que el General tiene que enterarse
                # igual: es donde los doctores miran. Degradación, no entrega: si esto falla,
                # el archivo sigue estando donde ya quedó.
                #
                # UNA vez por tanda, no una por archivo. Quien manda la radiografía y a los
                # dos minutos la foto de la encía dispararía dos timbrazos por una sola cosa,
                # y a base de timbrazos que no piden nada el doctor deja de mirar el grupo --
                # que es como se pierde el escalamiento que sí importaba. El plural del texto
                # es deliberado: avisa de la tanda, no del archivo que la abrió.
                try:
                    await telegram.enviar_mensaje(
                        f"📎 {_escapar(m.nombre_perfil or m.telefono)} mandó archivos"
                        f" — están en su tema.",
                        tema_id=tema_general,
                        # El botón va AQUÍ y no solo en el escalamiento. Un escalamiento
                        # ocurre una vez; los archivos siguen llegando, y el doctor que ve
                        # entrar la tercera radiografía de alguien tiene que poder tomar la
                        # conversación sin esperar a que Daniela vuelva a escalar.
                        teclado=relevo_mod.teclado_tomar(m.telefono),
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "el archivo de %s ya está entregado; solo falló el aviso al General",
                        m.wamid,
                    )
            tamano = archivo.tamano
        else:
            # Un texto NO va al General. Ver NOTA DEL TEXTO SIN TEMA, abajo.
            tema = await asyncio.to_thread(_tema_existente, database_url, m.telefono)
            telegram_id = None
            if tema:
                # Mudo, salvo durante un relevo: ahí el doctor está esperando justo esto.
                # `_en_relevo` solo se consulta si hay tema; sin tema no hay dónde sonar y
                # la consulta sería una ida a Neon para no usar el resultado.
                en_relevo = await asyncio.to_thread(_en_relevo, database_url, m.telefono)
                try:
                    telegram_id = await telegram.enviar_mensaje(
                        componer_aviso(m), tema_id=tema, silencioso=not en_relevo
                    )
                except HiloInvalido as e:
                    # El hilo ya no existe. Se olvida para que el PRIMER ARCHIVO que llegue
                    # después abra uno nuevo, y este texto no se archiva en ninguna parte:
                    # un texto nunca abre hilo (no negociable 14) y reencaminarlo al General
                    # sería justo lo que ese no negociable prohíbe.
                    #
                    # No se marca fallo a propósito. El estado al que llega es el del número
                    # sin tema, que es normal y ya tiene su registro: `reenviado_en` puesta
                    # y `telegram_message_id` nulo. Marcarlo llenaría de falsos positivos el
                    # índice por el que se vigila lo que de verdad se perdió.
                    log.warning(
                        "el hilo de %s ya no existe (%s); se olvida y su texto no se archiva",
                        m.telefono,
                        e,
                    )
                    await asyncio.to_thread(_olvidar_tema, database_url, m.telefono)
                    telegram_id = None
            tamano = None

    except ErrorDeCanal as e:
        log.error("no se pudo entregar %s: %s", m.wamid, e)
        await asyncio.to_thread(_marcar_fallo, database_url, m.wamid, str(e))
        return Resultado(m.wamid, nuevo=True, reenviado=False, fallo=str(e))

    await asyncio.to_thread(_marcar_reenviado, database_url, m.wamid, telegram_id, tamano)
    if telegram_id is None:
        log.info("%s se procesó sin reenviar: es un texto y su número no tiene tema", m.wamid)
    else:
        log.info("%s entregado a los doctores (telegram message_id=%s)", m.wamid, telegram_id)
    return Resultado(
        m.wamid, nuevo=True, reenviado=telegram_id is not None, lectura=tarea
    )


# ── Acceso a la base ──────────────────────────────────────────────────────────────────────
# Viven aquí y no en `persistencia.py` porque son de la ingesta, no del dominio clínico:
# `persistencia.py` guarda pacientes, citas y conocimiento; esta tabla guarda el acuse de
# recibo de un canal concreto.


def _registrar(database_url: str, m: MensajeEntrante) -> bool:
    """Inserta el mensaje. Devuelve True si es nuevo, False si Meta lo está reintentando.

    El `ON CONFLICT DO NOTHING` sobre la PRIMARY KEY es lo que decide, no una consulta
    previa: entre un SELECT y un INSERT cabe otro proceso haciendo lo mismo.
    """
    with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mensajes_entrantes
                (wamid, telefono, nombre_perfil, tipo, texto, media_id, mime)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (wamid) DO NOTHING
            RETURNING wamid
            """,
            (m.wamid, m.telefono, m.nombre_perfil, m.tipo, m.texto, m.media_id, m.mime),
        )
        return cur.fetchone() is not None


def _conversacion_viva(database_url: str, telefono: str) -> str | None:
    """El id de la conversación abierta de este número, o `None`. Solo para el `group_id`.

    Se traga cualquier fallo: una traza mal agrupada no puede costarle a un doctor la
    lectura de una radiografía.
    """
    try:
        with persistencia.conectar(database_url) as conn:
            viva = persistencia.conversacion_viva(conn, telefono)
    except Exception:  # noqa: BLE001 -- ver el docstring
        log.warning("no se pudo resolver la conversación de %s para el trace", telefono)
        return None
    return viva[0] if viva else None


def _tema_existente(database_url: str, telefono: str) -> int | None:
    """El tema del paciente, o `None`. Consulta pura: NO lo crea.

    Es deliberado que no sea `lectura.asegurar_tema`, que sí crearía uno: un texto suelto no
    abre hilo. Ver NOTA DEL TEXTO SIN TEMA.

    Si la base no responde, devuelve `None` en vez de propagar: el coste de fallar aquí sería
    perder el turno entero de un paciente por no haber podido archivar un «buenas tardes».
    """
    try:
        with persistencia.conectar(database_url) as conn:
            return persistencia.tema_del_paciente(conn, telefono)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo consultar el tema de %s; su texto no se archiva", telefono)
        return None


def _olvidar_tema(database_url: str, telefono: str) -> None:
    """Borra la fila de `temas_telegram` de ese número. No propaga nunca.

    Se llama cuando Telegram acaba de decir que el hilo no existe. Es la MISMA recuperación
    que `relevo.cerrar` hace con el motivo `tema_perdido`, por la misma razón, pero llega por
    la otra puerta: aquella la dispara el barrido y solo mira relevos vivos
    (`tomada_por IS NOT NULL`), así que el hilo borrado de un paciente que no está en relevo
    no lo miraba nadie.

    Si la base no responde se sigue igual: el mensaje ya está entregado o ya está decidido
    que no se archiva, y perder el turno del paciente por no haber podido borrar una fila
    sería un precio absurdo. Lo único que se pierde es la recuperación, que volverá a
    intentarse con el próximo mensaje de esa persona.
    """
    try:
        with persistencia.conectar(database_url) as conn:
            persistencia.olvidar_tema(conn, telefono)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo olvidar el hilo muerto de %s", telefono)


def _en_relevo(database_url: str, telefono: str) -> bool:
    """¿Tiene un doctor la conversación de este número ahora mismo? (Fase 6C.)

    Decide UNA cosa: si lo que el paciente manda suena en el hilo o entra mudo. Fuera del
    relevo el tema es un expediente y no suena nunca; durante el relevo es una conversación
    en vivo, y un doctor que no oye la respuesta del paciente es un doctor hablando solo.
    Es la excepción que la NOTA DEL SILENCIO de `canales.py` ya dejaba prevista, y la única.

    **Degrada a `False`, o sea a mudo.** Si Neon no responde, lo que no se puede hacer es
    suponer que hay relevo: casi nunca lo hay, y equivocarse hacia el ruido devolvería el
    grupo al estado que el no negociable 14 acaba de arreglar --cada «buenas tardes» de cada
    paciente vibrando en el teléfono de todos--. Al revés, el peor caso es un doctor que
    tiene el hilo abierto delante y ve entrar el mensaje sin que le suene.
    """
    try:
        with persistencia.conectar(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM conversaciones "
                    " WHERE telefono = %s AND tomada_por IS NOT NULL LIMIT 1",
                    (telefono,),
                )
                return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 -- ver docstring
        log.warning("no se pudo saber si +%s está en relevo; entra mudo", telefono)
        return False


def _primer_archivo_de_la_tanda(database_url: str, telefono: str, wamid: str) -> bool:
    """¿Es el primer archivo ENTREGADO de este número en la ventana? Decide si el General
    suena o no.

    `reenviado_en IS NOT NULL` no es un detalle: si el archivo anterior se quedó por el
    camino --Telegram caído, un 429-- el doctor nunca lo vio, así que este no puede heredar
    un aviso que no llegó a existir. Ese mismo filtro excluye de paso la fila de ESTE mensaje,
    que `_registrar` acaba de insertar sin reenviar todavía; el `wamid <>` se queda igual
    para que la consulta siga siendo correcta lea quien la lea.

    Ante un fallo de la base devuelve `True` --avisa--. Un timbrazo de más es ruido; uno de
    menos es una radiografía que nadie mira.
    """
    try:
        with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM mensajes_entrantes
                 WHERE telefono = %s
                   AND wamid <> %s
                   AND tipo = ANY(%s)
                   AND reenviado_en IS NOT NULL
                   AND recibido_en > now() - make_interval(hours => %s)
                 LIMIT 1
                """,
                (telefono, wamid, sorted(TIPOS_CON_ARCHIVO), VENTANA_AVISO_ARCHIVO_HORAS),
            )
            return cur.fetchone() is None
    except Exception:  # noqa: BLE001
        log.exception("no se pudo saber si %s ya tenía archivos; se avisa igual", telefono)
        return True


def _marcar_reenviado(
    database_url: str, wamid: str, telegram_message_id: int | None, tamano: int | None
) -> None:
    with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mensajes_entrantes
               SET telegram_message_id = %s, reenviado_en = %s, bytes_descargados = %s,
                   fallo = NULL
             WHERE wamid = %s
            """,
            (telegram_message_id, datetime.now(timezone.utc), tamano, wamid),
        )


def _marcar_fallo(database_url: str, wamid: str, error: str) -> None:
    with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE mensajes_entrantes SET fallo = %s WHERE wamid = %s",
            (error[:2000], wamid),
        )

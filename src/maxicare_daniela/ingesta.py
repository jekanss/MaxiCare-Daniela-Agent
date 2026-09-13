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
from .canales import ErrorDeCanal, Telegram, WhatsApp

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
                mensajes.append(
                    MensajeEntrante(
                        wamid=wamid,
                        telefono=telefono,
                        nombre_perfil=perfiles.get(telefono),
                        tipo=tipo,
                        # Un adjunto puede traer texto propio en `caption`: «esta es la
                        # radiografía que me pidieron». Perderlo sería perder el contexto.
                        texto=(m.get("text") or {}).get("body") or cuerpo.get("caption"),
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


async def procesar_mensaje(
    m: MensajeEntrante,
    *,
    whatsapp: WhatsApp,
    telegram: Telegram,
    database_url: str,
    tema_general: int | None = None,
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
            telegram_id = await telegram.enviar_archivo(
                archivo, tipo_whatsapp=m.tipo, pie=pie, tema_id=destino
            )
            # A partir de aquí el archivo YA está entregado. Nada de lo que sigue —el aviso
            # al General, el arranque del lector— puede convertir esta entrega en un fallo,
            # así que el aviso queda en su propio try/except y el lector arranca ANTES de
            # intentarlo: un aviso que revienta no puede quitarle al doctor la lectura.
            if lectura_mod.vale_la_pena_leer(m.tipo, archivo.tamano):
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
                    )

                tarea = asyncio.create_task(_leer_con_grupo())
                # Ver `_lectores_vivos`: sin esta referencia fuerte, la tarea puede morir a
                # medias en cuanto el turno de Daniela suelte la suya.
                _lectores_vivos.add(tarea)
                tarea.add_done_callback(_lectores_vivos.discard)
            if tema:
                # El archivo ya no cae en el General, así que el General tiene que enterarse
                # igual: es donde los doctores miran. Degradación, no entrega: si esto falla,
                # el archivo sigue estando donde ya quedó.
                try:
                    await telegram.enviar_mensaje(
                        f"📎 Llegó un archivo de {_escapar(m.nombre_perfil or m.telefono)}"
                        f" — está en su tema.",
                        tema_id=tema_general,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "el archivo de %s ya está entregado; solo falló el aviso al General",
                        m.wamid,
                    )
            tamano = archivo.tamano
        else:
            telegram_id = await telegram.enviar_mensaje(
                componer_aviso(m), tema_id=tema_general
            )
            tamano = None

    except ErrorDeCanal as e:
        log.error("no se pudo entregar %s: %s", m.wamid, e)
        await asyncio.to_thread(_marcar_fallo, database_url, m.wamid, str(e))
        return Resultado(m.wamid, nuevo=True, reenviado=False, fallo=str(e))

    await asyncio.to_thread(_marcar_reenviado, database_url, m.wamid, telegram_id, tamano)
    log.info("%s entregado a los doctores (telegram message_id=%s)", m.wamid, telegram_id)
    return Resultado(m.wamid, nuevo=True, reenviado=True, lectura=tarea)


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


def _marcar_reenviado(
    database_url: str, wamid: str, telegram_message_id: int, tamano: int | None
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

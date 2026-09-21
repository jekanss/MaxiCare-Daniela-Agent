"""El aviso por WhatsApp a los doctores cuando Daniela agenda una cita.

Los doctores ya se enteraban por Telegram, y aun así se enteraban tarde: el grupo lo abren
cuando les suena algo, y WhatsApp lo miran todo el día. Aquí salen los cuatro datos con los
que un doctor decide algo -- quién es, a qué número llamarlo, de qué es la cita y cuándo.

Vive aparte de `herramientas.py` por el mismo motivo que `seguimientos.py`: no abre
conexiones, no decide si la cita se crea y no sabe quién lo llamó. Recibe los cuatro datos y
los manda. Eso es lo que permite comprobar los cinco huecos de la plantilla en milisegundos
y sin señal, que es la única forma de que alguien los compruebe.

**Nada de lo que pase aquí puede tumbar una cita.** Cuando este módulo corre, la cita ya está
en Neon y en Google Calendar y el cupo ya está tomado: a partir de ese punto, un fallo de Meta
es un doctor que no se entera, y perder la cita para no perder el aviso es exactamente el
intercambio que este proyecto prohíbe. De ahí las tres reglas que no se mueven:

- `avisar` **no propaga nunca**, ni el fallo de un destinatario al siguiente;
- el envío va en **su propia tarea**, fuera del camino por el que el paciente recibe su
  confirmación -- el aviso al doctor no puede costarle segundos de espera al paciente;
- con `plantilla_cita_nueva` vacía **se decide igual y no se manda nada**, y queda en el log
  a quién se le habría mandado y con qué. Mismo patrón que `plantilla_recordatorio` en
  `seguimientos.despachar`, y por la misma razón: la plantilla todavía no está aprobada por
  Meta, y esto tiene que poder desplegarse hoy sin mandar un solo mensaje.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .calendario import ZONA_BOGOTA
from .canales import WhatsApp
from .config import WHATSAPP_DOCTORES, Config

log = logging.getLogger(__name__)

#: Los nombres en español, que `strftime` no da sin depender del locale del sistema -- y el
#: locale del VPS no es el de esta máquina.
#:
#: Están copiados y no importados, como los de `seguimientos._DIAS`, porque lo que cambia de
#: un sitio a otro es la FORMA: `herramientas._formatear_hora` devuelve «jueves 17/9 a las
#: 09:00» y `agentes.fecha_en_palabras` devuelve la hora truncada para no romper el caché del
#: prompt. Ninguna de las dos cabe en un hueco de esta plantilla, que necesita la fecha y la
#: hora SEPARADAS y escritas como las lee un doctor.
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


@dataclass(frozen=True)
class CitaNueva:
    """Lo que el doctor necesita saber, y nada más.

    No lleva el id de la cita, ni el de la conversación, ni el motivo que escribió el modelo:
    este mensaje sale por WhatsApp, o sea por un canal que no es el expediente. Lo clínico
    sigue viajando solo a Telegram (ver el MURO en `.claude/rules/frontera-agentes.md`), y
    ningún documento de identidad entra aquí -- la tabla `pacientes` tampoco tiene esa
    columna, y esa ausencia ES la política.
    """

    nombre_paciente: str
    telefono_paciente: str
    tratamiento: str
    inicio: datetime


def _en_bogota(momento: datetime) -> datetime:
    """La hora de la cita en el huso con el que se pactó.

    Es la misma lección que pagó `seguimientos._parametros_del_recordatorio`: `citas.inicio`
    es `TIMESTAMPTZ` y una conexión pelada de psycopg lo devuelve normalizado a UTC, así que
    un `%H:%M` a secas le decía «a las 14:00» a quien tenía la cita a las 9:00. Se convierte
    UNA vez, arriba, y los dos huecos salen de esa conversión.
    """
    if momento.tzinfo is None:
        return momento.replace(tzinfo=ZONA_BOGOTA)
    return momento.astimezone(ZONA_BOGOTA)


def fecha_en_palabras(local: datetime) -> str:
    """«lunes 22 de septiembre». Sin año a propósito: una cita se agenda a semanas vista."""
    return f"{_DIAS[local.weekday()]} {local.day} de {_MESES[local.month - 1]}"


def hora_en_palabras(local: datetime) -> str:
    """«10:00 a. m.», que es como se dicen las horas en Colombia.

    En 24 h y no en 12 el doctor tiene que traducir mentalmente «17:00» delante de un
    paciente, y la clínica ya habla en a. m./p. m. en todas partes: el horario que Daniela
    recita, el que la clínica publica y el que el paciente acaba de leer en su confirmación.
    """
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'a. m.' if local.hour < 12 else 'p. m.'}"


def _o_pendiente(valor: str | None) -> str:
    """Un hueco vacío NO se manda vacío: Meta rechaza el mensaje ENTERO.

    O sea que un nombre en blanco no costaría un renglón, costaría el aviso completo. El
    marcador es el de la regla dura 3 y aquí además es honesto: el destinatario es un doctor,
    y «PENDIENTE» le dice exactamente lo que pasa. A un paciente no se le manda nunca -- ver
    el mismo razonamiento, al revés, en `seguimientos.despachar`.
    """
    limpio = (valor or "").strip()
    return limpio or "PENDIENTE"


def parametros_de(cita: CitaNueva) -> list[str]:
    """Los CINCO huecos, en el orden en que Meta los aprobó.

    Cambiar este orden no cambia la plantilla: manda otro dato en otro hueco, y el doctor lee
    un teléfono donde esperaba la hora. Es la única función de este módulo cuyo resultado lee
    una persona, así que es la única que puede romper el aviso sin romper ninguna prueba.

        {{1}} nombre  ·  {{2}} teléfono  ·  {{3}} tratamiento  ·  {{4}} fecha  ·  {{5}} hora

    La fecha y la hora son huecos DISTINTOS: un formateador que devolviera las dos juntas
    dejaría la plantilla diciendo «el lunes 22 de septiembre a las 10:00 a. m. a las 10:00
    a. m.».
    """
    local = _en_bogota(cita.inicio)
    return [
        _o_pendiente(cita.nombre_paciente),
        _o_pendiente(cita.telefono_paciente),
        _o_pendiente(cita.tratamiento),
        fecha_en_palabras(local),
        hora_en_palabras(local),
    ]


@dataclass(frozen=True)
class Ajustes:
    """A quién se avisa y con qué. `whatsapp` en `None` o `plantilla` vacía apagan el envío."""

    whatsapp: Any | None
    doctores: tuple[str, ...]
    plantilla: str
    idioma: str


def ajustes_del_entorno() -> Ajustes:
    """Los ajustes vivos, leídos por la MISMA puerta que los lee el resto del proyecto.

    Pasa por `Config.desde_entorno()` y no por un `os.environ.get` propio a propósito: dos
    sitios leyendo la misma variable son dos sitios que un día divergen, y el día que alguien
    renombre `MAXICARE_WHATSAPP_TOKEN` esto dejaría de mandar sin que nada fallara. Es el
    mismo defecto que ya costó el teléfono del canal de privacidad.

    Que la lectura no viaje en `ContextoDaniela` -- donde sí viajan las credenciales de
    Telegram -- tiene un motivo y una consecuencia. El motivo: el contexto no lleva las de
    WhatsApp, y quien manda aquí es el código, no el turno. La consecuencia: una prueba
    sustituye esta función entera con `monkeypatch`, que es justo lo que hacen las de
    `tests/test_aviso_citas.py`, en vez de tocar variables de entorno del proceso.

    **Nunca lanza.** `Config.desde_entorno()` exige `MAXICARE_DATABASE_URL` y esto corre
    dentro de una tarea de fondo: un `RuntimeError` ahí no lo vería nadie. Sin ajustes, el
    aviso cae solo en el modo «decide y no manda», que es el lado inocuo de equivocarse.
    """
    try:
        config = Config.desde_entorno()
    except Exception:  # noqa: BLE001 -- ver el docstring: aquí no hay a quién propagarle nada
        log.exception("no se pudo leer la configuración del aviso de cita nueva")
        return Ajustes(whatsapp=None, doctores=WHATSAPP_DOCTORES, plantilla="", idioma="es")

    canal = (
        WhatsApp(config.whatsapp_token, config.whatsapp_phone_number_id)
        if config.whatsapp_token and config.whatsapp_phone_number_id
        else None
    )
    return Ajustes(
        whatsapp=canal,
        doctores=config.whatsapp_doctores,
        plantilla=config.plantilla_cita_nueva,
        idioma=config.plantilla_cita_nueva_idioma,
    )


async def avisar(
    cita: CitaNueva,
    *,
    whatsapp: Any | None,
    doctores: Sequence[str],
    plantilla: str,
    idioma: str = "es",
) -> int:
    """Manda el aviso a cada doctor. Devuelve cuántos salieron, y **no propaga nunca**.

    Va por plantilla y no por `enviar_texto` porque los doctores no le escriben a Daniela:
    están fuera de la ventana de 24 h de Meta siempre, no a veces, y un texto libre ahí lo
    rechaza Meta entero.

    El `try` va DENTRO del bucle, por destinatario. Fuera, el primer número que fallara
    -- uno mal escrito en el `.env`, un doctor que bloqueó el número de la clínica -- se
    llevaría por delante el aviso del otro, que es el que sí habría llegado.
    """
    valores = parametros_de(cita)

    if not plantilla or whatsapp is None or not doctores:
        # La decisión se toma igual y queda escrita. Es lo que permite desplegar esto hoy,
        # con la plantilla todavía sin aprobar, y comprobar en producción que avisa de las
        # citas correctas antes de que salga un solo WhatsApp.
        log.info(
            "aviso de cita nueva DECIDIDO y no enviado (plantilla o canal sin configurar): "
            "destinatarios=%s plantilla=%r parametros=%s",
            list(doctores),
            plantilla,
            valores,
        )
        return 0

    enviados = 0
    for telefono in doctores:
        try:
            await whatsapp.enviar_plantilla(
                telefono, plantilla=plantilla, parametros=valores, idioma=idioma
            )
            enviados += 1
        except Exception:  # noqa: BLE001 -- el otro doctor tiene que enterarse igual
            log.exception("no se pudo avisar de la cita nueva a %s", telefono)

    return enviados


#: La única referencia FUERTE a las tareas del aviso. `asyncio` solo las guarda en un
#: `WeakSet`: una tarea que nadie sostiene se la puede llevar el recolector a medias, y aquí
#: eso sería un aviso que sale por la mitad -- o que no sale-- sin un error en ningún log.
#: Es el mismo `set` que `ingesta._lectores_vivos`, por el mismo motivo. El
#: `add_done_callback` la saca en cuanto acaba, así que no crece.
_avisos_vivos: set[asyncio.Task] = set()


def tareas_en_vuelo() -> tuple[asyncio.Task, ...]:
    """Las tareas de aviso que todavía no han terminado.

    Existe para poder ESPERARLAS: sin esto, la única forma de comprobar desde una prueba que
    el aviso sale de verdad sería leer un `set` privado de otro módulo, y la alternativa
    -- llamar a `avisar` directamente-- dejaría sin probar justo lo que se rompe en silencio,
    que es el paso por `create_task`.
    """
    return tuple(_avisos_vivos)


async def _avisar_con_los_ajustes_vivos(cita: CitaNueva) -> int:
    """El cuerpo de la tarea de fondo, que **no puede lanzar**.

    Nadie va a mirar el resultado de esta tarea: el turno ya siguió su camino y el paciente ya
    tiene su confirmación. Una excepción que se escapara de aquí no la vería nadie hasta que
    el recolector se llevara la tarea, y entonces saldría como un «exception was never
    retrieved» sin la cita ni el teléfono dentro -- o sea, sin nada con lo que ir a mirar qué
    aviso se perdió. `avisar` ya no propaga; esto cubre lo que hay antes de llamarla.
    """
    try:
        ajustes = ajustes_del_entorno()
        return await avisar(
            cita,
            whatsapp=ajustes.whatsapp,
            doctores=ajustes.doctores,
            plantilla=ajustes.plantilla,
            idioma=ajustes.idioma,
        )
    except Exception:  # noqa: BLE001 -- ver el docstring: la cita ya existe y manda ella
        log.exception(
            "la cita de %s quedó creada; solo falló el aviso a los doctores",
            cita.nombre_paciente,
        )
        return 0


def avisar_en_segundo_plano(cita: CitaNueva) -> asyncio.Task | None:
    """Programa el aviso y devuelve al turno de inmediato.

    Lo que se gana es del PACIENTE, no de los doctores: entre `crear_cita` y el mensaje de
    confirmación no hay nada más, así que dos llamadas a la Graph API metidas ahí son dos
    llamadas que el paciente pasa mirando «escribiendo…» por algo que no es suyo. La cita ya
    existe; el aviso puede llegar medio segundo después.

    Devuelve `None` si no hay bucle -- alguien llamándola desde código síncrono--, que es un
    error de programación y no un fallo de envío: se registra y no se lanza, porque quien
    llama a esto acaba de crear una cita buena.
    """
    try:
        tarea = asyncio.create_task(_avisar_con_los_ajustes_vivos(cita))
    except RuntimeError:
        log.exception(
            "no hay bucle de eventos para avisar de la cita de %s", cita.nombre_paciente
        )
        return None

    _avisos_vivos.add(tarea)
    tarea.add_done_callback(_avisos_vivos.discard)
    return tarea

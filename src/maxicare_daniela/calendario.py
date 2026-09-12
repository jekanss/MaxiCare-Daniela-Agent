"""La costura del calendario: una forma, dos implementaciones.

Google Calendar es uno de los sistemas de `sistemas.lista[]`, igual que Neon. Este módulo
define QUÉ tiene que saber hacer un calendario para que las tools de `herramientas.py`
funcionen, sin decidir CUÁL se usa. Esa decisión la toma quien construye el
`ContextoDaniela` -- en pruebas, el doble; en producción, Google.

No importa ningún framework de transporte, y `tests/test_estructura.py` lo verifica.

------------------------------------------------------------------------------------------
Por qué el doble es hostil
------------------------------------------------------------------------------------------

`CalendarioDoble` no existe para que las pruebas pasen: existe para que fallen donde tienen
que fallar. Por eso expone `fallar_en`.

El camino que más importa de toda la fase 3 no es el feliz. Es este:

    1. `crear_cita` toma el cupo en Neon -- el paciente ya tiene el horario apartado
    2. Google Calendar no responde
    3. si nadie libera ese cupo, queda una reserva fantasma bloqueando a otro paciente
    4. si además se le confirma la cita al paciente, el paciente llega a una cita
       que ningún doctor tiene en su calendario

Un doble que siempre dice que sí deja ese camino sin recorrer, y entonces la prueba dice
«OK» sobre el único escenario que puede mandar a alguien a una clínica donde nadie lo
espera. Con `fallar_en={"crear_evento"}` ese camino se recorre en cada corrida.

------------------------------------------------------------------------------------------
Google: la cita de Daniela no es un bloqueo del doctor
------------------------------------------------------------------------------------------

Es la trampa central de `CalendarioGoogle`, y con `CalendarioDoble` era invisible: el doble
guarda los eventos y los bloqueos en dos listas separadas, así que preguntar por bloqueos
jamás devolvía una cita. Google los devuelve en la misma lista.

Sin distinguirlos, la clínica atendería la mitad::

    capacidad_por_hora = 2

    9:00  paciente A  ->  Daniela crea el evento en Calendar
    9:00  paciente B  ->  consultar_disponibilidad pide los bloqueos
                          Calendar devuelve... el evento de A
                          el bloque queda tapado -> las 9:00 desaparecen

El conteo de cupos sale de Neon --Calendar no sabe contar hasta dos-- y de Calendar sale
únicamente lo que los doctores apartaron a mano. Por eso cada evento que crea Daniela va
marcado con `extendedProperties.private.origen = "daniela"`, y `bloqueos()` los descarta.
Un evento sin marca es de los doctores y sí tapa: es el comportamiento correcto, y es lo
que hace que un almuerzo escrito a mano siga sacando esa hora de la oferta.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Colombia no tiene horario de verano, así que un desfase fijo es exacto y no depende de
#: la base de datos de zonas horarias del sistema, que en Windows no viene.
#:
#: Vive aquí y no en `herramientas.py` --que es donde estaba-- porque ahora hacen falta las
#: dos: `herramientas.py` la usa para hablar con Postgres y este módulo para traducir lo que
#: devuelve Google. `herramientas.py` la importa de aquí; la flecha va en esa dirección
#: porque este módulo no importa a aquel.
ZONA_BOGOTA = timezone(timedelta(hours=-5))

#: El nombre de la zona tal como lo escribe Google. No se deriva de `ZONA_BOGOTA`: son dos
#: representaciones de la misma cosa para dos sistemas distintos.
ZONA_GOOGLE = "America/Bogota"


class ErrorDeCalendario(RuntimeError):
    """El calendario no pudo completar la operación.

    Es una excepción propia y no la del cliente HTTP de turno para que `herramientas.py`
    pueda distinguir «el calendario falló» de cualquier otro error sin importar quién esté
    detrás de la costura.
    """


@dataclass(frozen=True)
class Bloqueo:
    """Un rango que los doctores apartaron a mano en su calendario.

    No es una cita de un paciente: es el almuerzo, una cirugía larga, un día libre. Sale de
    Calendar, no de Neon, porque es ahí donde los doctores lo escriben.
    """

    inicio: datetime
    fin: datetime
    titulo: str = ""

    def solapa(self, desde: datetime, hasta: datetime) -> bool:
        """Dos rangos se solapan si cada uno empieza antes de que el otro termine."""
        return self.inicio < hasta and desde < self.fin


@runtime_checkable
class Calendario(Protocol):
    """Lo que las tools necesitan de un calendario. Nada más.

    Deliberadamente pequeño: cada método que se agregue aquí es un método que habrá que
    implementar dos veces y probar dos veces.
    """

    def crear_evento(
        self, *, inicio: datetime, duracion_minutos: int, titulo: str, descripcion: str = ""
    ) -> str:
        """Devuelve el identificador del evento creado. Lanza `ErrorDeCalendario` si falla."""
        ...

    def mover_evento(self, evento_id: str, *, inicio: datetime) -> None: ...

    def eliminar_evento(self, evento_id: str) -> None: ...

    def bloqueos(self, desde: datetime, hasta: datetime) -> list[Bloqueo]:
        """Los rangos que los doctores bloquearon dentro de la ventana pedida."""
        ...


# ==========================================================================================
# El doble
# ==========================================================================================


@dataclass
class CalendarioDoble:
    """Calendario en memoria, con un interruptor para romperlo a voluntad.

    Uso típico en una prueba::

        cal = CalendarioDoble()
        cal.agregar_bloqueo(inicio, fin, "almuerzo")     # los doctores bloquearon algo
        cal.fallar_en.add("crear_evento")                # y ahora Google se cae
    """

    #: Nombres de método que deben lanzar `ErrorDeCalendario` en vez de funcionar.
    fallar_en: set[str] = field(default_factory=set)

    #: evento_id -> (inicio, duracion_minutos, titulo)
    eventos: dict[str, tuple[datetime, int, str]] = field(default_factory=dict)

    _bloqueos: list[Bloqueo] = field(default_factory=list)
    _siguiente: int = 1

    # -- control de la prueba ---------------------------------------------------------

    def agregar_bloqueo(self, inicio: datetime, fin: datetime, titulo: str = "") -> None:
        self._bloqueos.append(Bloqueo(inicio=inicio, fin=fin, titulo=titulo))

    def _comprobar(self, metodo: str) -> None:
        if metodo in self.fallar_en:
            raise ErrorDeCalendario(f"fallo forzado en {metodo} (CalendarioDoble)")

    # -- la forma que exige el Protocol -----------------------------------------------

    def crear_evento(
        self, *, inicio: datetime, duracion_minutos: int, titulo: str, descripcion: str = ""
    ) -> str:
        self._comprobar("crear_evento")
        evento_id = f"doble-{self._siguiente}"
        self._siguiente += 1
        self.eventos[evento_id] = (inicio, duracion_minutos, titulo)
        return evento_id

    def mover_evento(self, evento_id: str, *, inicio: datetime) -> None:
        self._comprobar("mover_evento")
        if evento_id not in self.eventos:
            raise ErrorDeCalendario(f"no existe el evento {evento_id}")
        _, duracion, titulo = self.eventos[evento_id]
        self.eventos[evento_id] = (inicio, duracion, titulo)

    def eliminar_evento(self, evento_id: str) -> None:
        self._comprobar("eliminar_evento")
        # Borrar algo que ya no está es un éxito, no un error: la tool de cancelación puede
        # reintentarse, y la segunda vez el evento ya no existe. Si esto lanzara, un
        # reintento normal escalaría a los doctores sin que nada estuviera mal.
        self.eventos.pop(evento_id, None)

    def bloqueos(self, desde: datetime, hasta: datetime) -> list[Bloqueo]:
        self._comprobar("bloqueos")
        return [b for b in self._bloqueos if b.solapa(desde, hasta)]


# ==========================================================================================
# Google
# ==========================================================================================


#: El permiso que pide la cuenta de servicio. `calendar.events` bastaría para crear, mover y
#: borrar, pero no para la comprobación de acceso del constructor, que lee el calendario en
#: sí. Es el alcance mínimo que cubre las cinco operaciones que este módulo hace.
ALCANCE = ("https://www.googleapis.com/auth/calendar",)

#: La marca que separa «lo agendó Daniela» de «lo apartó un doctor». Vive en
#: `extendedProperties.private`, que Google guarda por aplicación y no le muestra a nadie
#: en la interfaz: el doctor ve un evento normal en su calendario.
CLAVE_ORIGEN = "origen"
VALOR_ORIGEN = "daniela"

#: Borrar algo que ya no está es un éxito. Google dice 404 si el id nunca existió y 410 si
#: el evento ya se borró; los dos significan lo mismo para quien quería que no existiera.
#: Sin esto, un reintento normal de cancelación escalaría a los doctores sin que nada
#: estuviera mal -- el mismo contrato que ya cumple `CalendarioDoble.eliminar_evento`.
YA_NO_EXISTE = frozenset({404, 410})

#: Tope por página de `events.list`. Google no promete devolverlo todo de una vez, así que
#: `bloqueos()` pagina igual. Un bloqueo que se quede en la segunda página es un paciente
#: citado en mitad de una cirugía.
POR_PAGINA = 2500


def decodificar_cuenta_de_servicio(sa_b64: str) -> dict[str, Any]:
    """El base64 de `MAXICARE_GOOGLE_SA_B64` de vuelta a un diccionario, o un error claro.

    Es una función aparte, y pura, porque los cuatro modos de fallo de esta credencial se
    parecen entre sí y no se parecen a nada: un `.env` con el JSON pegado tal cual, un
    base64 truncado al copiarlo, un archivo de credenciales de OAuth de escritorio en vez de
    una cuenta de servicio, o el JSON correcto sin la clave privada. Distinguirlos aquí
    convierte cuatro fallos idénticos de «Google no responde» en cuatro mensajes distintos.
    """
    if not sa_b64.strip():
        raise ErrorDeCalendario(
            "MAXICARE_GOOGLE_SA_B64 está vacía. Es el JSON de la cuenta de servicio "
            "convertido a base64 de una sola línea; ver .env.ejemplo."
        )
    try:
        crudo = base64.b64decode(sa_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ErrorDeCalendario(
            "MAXICARE_GOOGLE_SA_B64 no es base64 válido. El error más común es haber "
            "pegado el JSON tal cual: un .env es una línea por variable, y un JSON de "
            "varias líneas deja la variable valiendo '{'."
        ) from e

    try:
        datos = json.loads(crudo)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ErrorDeCalendario(
            "MAXICARE_GOOGLE_SA_B64 decodifica, pero lo de dentro no es JSON. "
            "Probablemente se copió incompleto."
        ) from e

    if not isinstance(datos, dict):
        raise ErrorDeCalendario("La cuenta de servicio tiene que ser un objeto JSON.")
    if datos.get("type") != "service_account":
        raise ErrorDeCalendario(
            f"El JSON es de tipo {datos.get('type')!r}, no 'service_account'. Un archivo de "
            "credenciales de OAuth de escritorio no sirve: haría falta que una persona "
            "abriera un navegador, y aquí no hay nadie mirando."
        )
    faltantes = [c for c in ("client_email", "private_key", "token_uri") if not datos.get(c)]
    if faltantes:
        raise ErrorDeCalendario(
            f"A la cuenta de servicio le faltan campos: {', '.join(faltantes)}."
        )
    return datos


def _a_instante(borde: dict[str, Any]) -> datetime | None:
    """Un `start` o un `end` de Google convertido a un instante con zona horaria.

    Google usa dos formas distintas y hay que entender las dos:

      * `{"dateTime": "2026-09-15T09:00:00-05:00"}` -- un evento con hora.
      * `{"date": "2026-09-15"}` -- un evento de día completo: el día libre del doctor.

    El día completo NO puede devolverse como medianoche a secas. `end.date` en Google es
    **exclusivo** --un día libre del 15 llega como start=15, end=16--, así que tomar las dos
    fechas a las 00:00 de Bogotá da exactamente el rango correcto. Si esto se ignorara, un
    día libre entero no bloquearía ni un minuto y Daniela citaría pacientes ese día.
    """
    con_hora = borde.get("dateTime")
    if con_hora:
        # `fromisoformat` no entiende la 'Z' de UTC antes de Python 3.11, y el proyecto
        # declara 3.10 como mínimo. Traducirla cuesta una línea.
        texto = con_hora.replace("Z", "+00:00") if con_hora.endswith("Z") else con_hora
        try:
            momento = datetime.fromisoformat(texto)
        except ValueError:
            return None
        return momento if momento.tzinfo else momento.replace(tzinfo=ZONA_BOGOTA)

    solo_fecha = borde.get("date")
    if solo_fecha:
        try:
            dia = datetime.strptime(solo_fecha, "%Y-%m-%d").date()
        except ValueError:
            return None
        return datetime.combine(dia, time.min, tzinfo=ZONA_BOGOTA)

    return None


def a_bloqueo(evento: dict[str, Any]) -> Bloqueo | None:
    """Un evento de Google convertido en `Bloqueo`, o `None` si no debe bloquear nada.

    Toda la política del cruce Calendar↔Neon está aquí, y aquí es donde se puede probar sin
    red. Cuatro razones para devolver `None`, en orden de importancia:

      1. **Lo agendó Daniela.** La capacidad la cuenta Neon. Ver el docstring del módulo:
         sin esto la clínica atendería uno por hora en vez de dos.
      2. **Está cancelado.** `events.list` solo los devuelve si se los pide, pero pedirlos
         nunca y filtrarlos igual cuesta una línea y cierra el caso.
      3. **Está marcado como «libre»** (`transparency: "transparent"`). El doctor dijo
         explícitamente que ese rato no lo ocupa. Respetarlo es respetar lo que escribió.
      4. **No tiene bordes legibles.** Un evento sin inicio o sin fin utilizable no se
         inventa: se ignora y se deja constancia en el log.
    """
    if evento.get("status") == "cancelled":
        return None
    if evento.get("transparency") == "transparent":
        return None

    privadas = evento.get("extendedProperties", {}).get("private", {})
    if privadas.get(CLAVE_ORIGEN) == VALOR_ORIGEN:
        return None

    inicio = _a_instante(evento.get("start") or {})
    fin = _a_instante(evento.get("end") or {})
    if inicio is None or fin is None or fin <= inicio:
        log.warning(
            "Evento de Calendar ignorado por no tener un rango utilizable: id=%s",
            evento.get("id"),
        )
        return None

    return Bloqueo(inicio=inicio, fin=fin, titulo=evento.get("summary") or "")


class CalendarioGoogle:
    """El calendario de verdad. Misma forma que `CalendarioDoble`, mismo contrato.

    **Falla al construirse, no al llamarse**, y ahora eso significa algo: el constructor
    hace una lectura real del calendario. Si la cuenta de servicio existe pero nadie
    compartió el calendario con ella --el paso que casi todo el mundo olvida-- el error
    aparece al arrancar el proceso, con un mensaje que dice a qué correo hay que dárselo.

    La alternativa sería enterarse en mitad de una conversación, en la línea siguiente a
    haber tomado el cupo del paciente en Neon. El cupo se liberaría, sí, pero la cita se
    perdería y alguien tendría que escalarla a mano. Un objeto que se niega a existir es
    barato; uno que existe mal cuesta una cita por cada paciente que llegue mientras dure.
    """

    def __init__(self, sa_b64: str, calendario_id: str) -> None:
        if not calendario_id.strip():
            raise ErrorDeCalendario(
                "MAXICARE_GOOGLE_CALENDAR_ID está vacía. Es el correo del calendario: el "
                "principal de alguien (x@gmail.com) o uno secundario "
                "(c_...@group.calendar.google.com)."
            )

        # Los imports van aquí y no arriba a propósito. El resto del proyecto --las pruebas,
        # los scripts de las fases 1 a 5, el chat web-- importa este módulo por
        # `CalendarioDoble` y `bloques_del_dia`, y ninguno necesita el cliente de Google.
        # Arriba, un entorno sin esas dependencias no podría ni importar `herramientas.py`.
        from google.auth.exceptions import GoogleAuthError
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        self._calendario_id = calendario_id.strip()
        datos = decodificar_cuenta_de_servicio(sa_b64)
        self.correo_cuenta_de_servicio = datos["client_email"]

        try:
            credenciales = service_account.Credentials.from_service_account_info(
                datos, scopes=list(ALCANCE)
            )
            # `static_discovery=True` usa el descriptor que viene dentro del paquete: sin
            # esto, construir el servicio saldría a internet a buscarlo, y un arranque
            # dependería de una segunda red además de la de la API.
            self._servicio = build(
                "calendar", "v3", credentials=credenciales,
                static_discovery=True, cache_discovery=False,
            )
        except (GoogleAuthError, ValueError) as e:
            raise ErrorDeCalendario(
                f"La cuenta de servicio no se pudo cargar: {type(e).__name__}. Suele ser "
                "una private_key alterada al copiarla."
            ) from e

        # La comprobación que justifica todo el párrafo del docstring.
        try:
            self._servicio.calendars().get(calendarId=self._calendario_id).execute()
        except HttpError as e:
            if e.status_code in (403, 404):
                raise ErrorDeCalendario(
                    f"La cuenta de servicio se autenticó bien, pero no puede ver el "
                    f"calendario ({e.status_code}). Falta compartir ESE calendario con "
                    f"{self.correo_cuenta_de_servicio} dándole «Hacer cambios en los "
                    f"eventos» desde Configuración y uso compartido, en Google Calendar."
                ) from e
            raise self._traducir(e, "comprobar el acceso al calendario")
        except OSError as e:
            raise ErrorDeCalendario(
                f"No se pudo llegar a Google Calendar al arrancar: {e}"
            ) from e

    # -- lo que se le dice a `herramientas.py` cuando Google falla ----------------------

    def _traducir(self, error: Exception, operacion: str) -> ErrorDeCalendario:
        """Cualquier fallo de Google, convertido a la única excepción que las tools atrapan.

        No es cosmético. `herramientas.py` hace `except ErrorDeCalendario` y ahí adentro
        libera el cupo que ya tomó en Neon. Una `HttpError` cruda escaparía por encima de
        ese `except`, y la reserva del paciente quedaría apartada para siempre bloqueando a
        otro. Es exactamente la reserva fantasma que la fase 3 existe para impedir, así que
        aquí no se deja pasar nada: se atrapa `Exception`.
        """
        estado = getattr(error, "status_code", None)
        sufijo = f" (HTTP {estado})" if estado else ""
        return ErrorDeCalendario(
            f"Google Calendar falló al {operacion}{sufijo}: {type(error).__name__}"
        )

    # -- la forma que exige el Protocol -------------------------------------------------

    def crear_evento(
        self, *, inicio: datetime, duracion_minutos: int, titulo: str, descripcion: str = ""
    ) -> str:
        momento = inicio if inicio.tzinfo else inicio.replace(tzinfo=ZONA_BOGOTA)
        cuerpo = {
            "summary": titulo,
            "description": descripcion,
            "start": {"dateTime": momento.isoformat(), "timeZone": ZONA_GOOGLE},
            "end": {
                "dateTime": (momento + timedelta(minutes=duracion_minutos)).isoformat(),
                "timeZone": ZONA_GOOGLE,
            },
            # La marca. Sin ella, esta misma cita volvería como bloqueo en la próxima
            # consulta de disponibilidad y taparía el cupo que todavía queda libre.
            "extendedProperties": {"private": {CLAVE_ORIGEN: VALOR_ORIGEN}},
        }
        try:
            creado = (
                self._servicio.events()
                .insert(calendarId=self._calendario_id, body=cuerpo)
                .execute()
            )
        except Exception as e:
            raise self._traducir(e, "crear el evento") from e

        evento_id = creado.get("id")
        if not evento_id:
            # Google respondió 200 sin id. No debería pasar nunca; si pasa, es mejor que la
            # cita no se confirme a que se guarde en Neon con un id vacío que nadie podrá
            # mover ni cancelar después.
            raise ErrorDeCalendario("Google creó el evento pero no devolvió su id.")
        return evento_id

    def mover_evento(self, evento_id: str, *, inicio: datetime) -> None:
        """Cambia la hora conservando la duración que el evento ya tenía.

        La duración se lee del evento en vez de recalcularse desde la configuración: si
        alguien alargó esa cita a mano en Calendar, reprogramarla no debe encogerla de
        vuelta al valor por defecto.
        """
        destino = inicio if inicio.tzinfo else inicio.replace(tzinfo=ZONA_BOGOTA)
        try:
            actual = (
                self._servicio.events()
                .get(calendarId=self._calendario_id, eventId=evento_id)
                .execute()
            )
        except Exception as e:
            raise self._traducir(e, "leer el evento que se iba a mover") from e

        inicio_actual = _a_instante(actual.get("start") or {})
        fin_actual = _a_instante(actual.get("end") or {})
        duracion = (
            fin_actual - inicio_actual
            if inicio_actual and fin_actual and fin_actual > inicio_actual
            else timedelta(minutes=60)
        )

        cuerpo = {
            "start": {"dateTime": destino.isoformat(), "timeZone": ZONA_GOOGLE},
            "end": {"dateTime": (destino + duracion).isoformat(), "timeZone": ZONA_GOOGLE},
        }
        try:
            self._servicio.events().patch(
                calendarId=self._calendario_id, eventId=evento_id, body=cuerpo
            ).execute()
        except Exception as e:
            raise self._traducir(e, "mover el evento") from e

    def eliminar_evento(self, evento_id: str) -> None:
        try:
            self._servicio.events().delete(
                calendarId=self._calendario_id, eventId=evento_id
            ).execute()
        except Exception as e:
            if getattr(e, "status_code", None) in YA_NO_EXISTE:
                # Éxito, no error: lo que se quería es que no exista, y no existe.
                return
            raise self._traducir(e, "eliminar el evento") from e

    def bloqueos(self, desde: datetime, hasta: datetime) -> list[Bloqueo]:
        """Lo que los doctores apartaron a mano dentro de la ventana. Nada más.

        `singleEvents=True` expande las series repetidas en sus ocurrencias reales: sin eso,
        un almuerzo diario llegaría como un solo evento con una regla de repetición que este
        módulo tendría que interpretar, y la interpretaría peor que Google.
        """
        pagina = None
        encontrados: list[Bloqueo] = []
        while True:
            try:
                respuesta = (
                    self._servicio.events()
                    .list(
                        calendarId=self._calendario_id,
                        timeMin=desde.astimezone(ZONA_BOGOTA).isoformat(),
                        timeMax=hasta.astimezone(ZONA_BOGOTA).isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=POR_PAGINA,
                        pageToken=pagina,
                    )
                    .execute()
                )
            except Exception as e:
                raise self._traducir(e, "consultar los bloqueos") from e

            for evento in respuesta.get("items", []):
                bloqueo = a_bloqueo(evento)
                # El filtro de solapamiento se repite aquí aunque `timeMin`/`timeMax` ya lo
                # hagan: un evento de día completo se recorta contra la ventana en Bogotá,
                # no en la zona que el calendario tenga configurada.
                if bloqueo is not None and bloqueo.solapa(desde, hasta):
                    encontrados.append(bloqueo)

            pagina = respuesta.get("nextPageToken")
            if not pagina:
                return encontrados


def calendario_desde_config(config: Any) -> Calendario:
    """El calendario que corresponde según lo que haya configurado: Google, o el doble.

    Existe para que el sitio que construye un `ContextoDaniela` no tenga que decidirlo con
    un `if` propio --habría uno por canal, y divergirían--. Si faltan las credenciales
    devuelve `CalendarioDoble` y lo dice en el log: un despliegue a medias tiene que ser
    ruidoso, pero no puede tumbar a Daniela por una variable del calendario cuando lo demás
    de la conversación funciona.
    """
    if not getattr(config, "google_sa_b64", "") or not getattr(config, "google_calendar_id", ""):
        log.warning(
            "Sin MAXICARE_GOOGLE_SA_B64 o MAXICARE_GOOGLE_CALENDAR_ID: se usa "
            "CalendarioDoble. Las citas NO van a aparecer en el calendario de los doctores."
        )
        return CalendarioDoble()
    return CalendarioGoogle(config.google_sa_b64, config.google_calendar_id)


# ==========================================================================================
# Utilidad compartida
# ==========================================================================================


def bloques_del_dia(
    desde: datetime, hasta: datetime, *, duracion_minutos: int
) -> list[datetime]:
    """Los inicios de bloque entre dos instantes, cada `duracion_minutos`.

    Función pura, sin calendario de por medio: `consultar_disponibilidad` la usa para saber
    qué bloques EXISTEN antes de preguntar cuáles están libres. Separarlas permite probar la
    rejilla sin base de datos ni calendario.
    """
    if duracion_minutos <= 0:
        raise ValueError("la duración de un bloque tiene que ser positiva")

    paso = timedelta(minutes=duracion_minutos)
    bloques: list[datetime] = []
    actual = desde
    while actual + paso <= hasta:
        bloques.append(actual)
        actual += paso
    return bloques

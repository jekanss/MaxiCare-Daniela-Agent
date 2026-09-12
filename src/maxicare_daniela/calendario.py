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
Google: PENDIENTE, y a la vista
------------------------------------------------------------------------------------------

`MAXICARE_GOOGLE_CREDENTIALS_PATH` y `MAXICARE_GOOGLE_CALENDAR_ID` están vacías, así que
`CalendarioGoogle` no se puede ejecutar ni una sola vez para comprobar que funciona.
Escribirla igual dejaría código sin verificar mezclado con código probado, indistinguibles
-- que es lo que prohíbe la regla 3 del `CLAUDE.md`. Así que declara su intención y falla
diciendo exactamente qué falta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


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
# Google -- PENDIENTE
# ==========================================================================================


#: Lo que hay que conseguir de MaxiCare para poder implementar esto.
CREDENCIALES_PENDIENTES = (
    "MAXICARE_GOOGLE_CREDENTIALS_PATH (la cuenta de servicio con permiso sobre el "
    "calendario de la clínica) y MAXICARE_GOOGLE_CALENDAR_ID (cuál de los calendarios)"
)


class CalendarioGoogle:
    """PENDIENTE -- no implementado, y a propósito.

    Cumple la misma forma que `Calendario` para que el día que existan las credenciales se
    conecte sin tocar una sola tool. Hoy no se puede escribir con honestidad: sin poder
    ejecutarla ni una vez, cualquier implementación sería una suposición con apariencia de
    código probado.

    Falla al construirse, no al llamarse. Un objeto que se crea bien y revienta tres pasos
    después --con el cupo del paciente ya tomado en Neon-- es mucho peor que uno que se
    niega a existir.
    """

    def __init__(self, credenciales: str = "", calendario_id: str = "") -> None:
        raise NotImplementedError(
            "CalendarioGoogle está PENDIENTE: falta " + CREDENCIALES_PENDIENTES + ". "
            "Mientras tanto se usa CalendarioDoble. Ver fases[3] de plan-agentes.json."
        )


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

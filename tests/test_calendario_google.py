"""Lo que de `CalendarioGoogle` se puede probar sin red, que es justo lo que más importa.

Contra Google no se puede probar nada aquí: no hay credenciales en una corrida de `pytest`
y no debería haberlas. Pero la parte de `CalendarioGoogle` donde vive la política --y donde
un error se paga en citas mal agendadas-- no depende de Google en absoluto: es la
traducción de un evento a un `Bloqueo`, y esa es una función pura sobre diccionarios.

Los diccionarios de este archivo tienen la forma que devuelve `events.list` de la API v3 de
Calendar, con las dos representaciones de tiempo que Google usa de verdad (`dateTime` para
un evento con hora, `date` para uno de día completo, con el `end` exclusivo).

El resto --que `insert` inserte, que `delete` borre-- vive en `scripts/probar_calendario.py`
y solo se puede comprobar contra el calendario real.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

import pytest

from maxicare_daniela.calendario import (
    CLAVE_ORIGEN,
    VALOR_ORIGEN,
    ZONA_BOGOTA,
    Bloqueo,
    CalendarioCaido,
    CalendarioDoble,
    ErrorDeCalendario,
    a_bloqueo,
    bloques_del_dia,
    calendario_desde_config,
    decodificar_cuenta_de_servicio,
)

LUNES_9 = datetime(2026, 9, 14, 9, 0, tzinfo=ZONA_BOGOTA)


def evento(**cambios) -> dict:
    """Un evento normal de Calendar: una cirugía que el doctor apartó a mano."""
    base = {
        "id": "evt-doctor",
        "status": "confirmed",
        "summary": "Cirugía larga",
        "start": {"dateTime": "2026-09-14T09:00:00-05:00", "timeZone": "America/Bogota"},
        "end": {"dateTime": "2026-09-14T11:00:00-05:00", "timeZone": "America/Bogota"},
    }
    base.update(cambios)
    return base


def cuenta_de_servicio(**cambios) -> str:
    """Una cuenta de servicio con la forma correcta, en base64. Nada de esto es real."""
    datos = {
        "type": "service_account",
        "project_id": "proyecto-de-prueba",
        "private_key_id": "0" * 40,
        "private_key": "-----BEGIN PRIVATE KEY-----\nno-es-una-clave\n-----END PRIVATE KEY-----\n",
        "client_email": "daniela@proyecto-de-prueba.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    datos.update(cambios)
    for clave, valor in list(datos.items()):
        if valor is None:
            del datos[clave]
    return base64.b64encode(json.dumps(datos).encode()).decode()


# ==========================================================================================
# La decisión que sostiene la capacidad de la clínica
# ==========================================================================================


def test_lo_que_aparto_un_doctor_si_bloquea():
    """El caso base, y el motivo de que Calendar participe en la disponibilidad."""
    bloqueo = a_bloqueo(evento())

    assert bloqueo is not None
    assert bloqueo.inicio == LUNES_9
    assert bloqueo.fin == LUNES_9 + timedelta(hours=2)
    assert bloqueo.titulo == "Cirugía larga"


def test_una_cita_que_agendo_daniela_no_es_un_bloqueo():
    """La prueba que sostiene todo el módulo.

    `CalendarioDoble` guarda eventos y bloqueos en dos listas separadas, así que jamás
    devolvía una cita al preguntar por bloqueos. Google los devuelve juntos. Sin este
    filtro, la primera cita de una hora taparía el bloque entero y la clínica atendería un
    paciente por hora en vez de los dos que su capacidad permite.
    """
    cita = evento(
        id="evt-daniela",
        summary="Ana Restrepo · ortodoncia",
        extendedProperties={"private": {CLAVE_ORIGEN: VALOR_ORIGEN}},
    )

    assert a_bloqueo(cita) is None


def test_el_bloque_sigue_libre_despues_de_que_daniela_agende_ahi():
    """El fallo del docstring del módulo, recorrido de punta a punta.

    No comprueba la función aislada: comprueba la consecuencia. Con capacidad 2, después de
    agendar al primer paciente a las 9:00, las 9:00 tienen que seguir ofreciéndose.
    """
    jornada = bloques_del_dia(LUNES_9, LUNES_9 + timedelta(hours=3), duracion_minutos=60)
    assert LUNES_9 in jornada

    eventos_del_dia = [
        evento(id="a", extendedProperties={"private": {CLAVE_ORIGEN: VALOR_ORIGEN}},
               start={"dateTime": "2026-09-14T09:00:00-05:00"},
               end={"dateTime": "2026-09-14T10:00:00-05:00"}),
    ]
    bloqueos = [b for b in (a_bloqueo(e) for e in eventos_del_dia) if b is not None]

    tapados = [h for h in jornada if any(b.solapa(h, h + timedelta(hours=1)) for b in bloqueos)]
    assert tapados == [], "una cita de Daniela tapó su propio bloque: la capacidad se perdió"


def test_si_el_doctor_aparto_esa_misma_hora_el_bloque_si_se_tapa():
    """El falso positivo del test anterior. Sin esta prueba, `a_bloqueo` podría devolver
    `None` siempre y las dos pruebas de arriba seguirían pasando."""
    jornada = bloques_del_dia(LUNES_9, LUNES_9 + timedelta(hours=3), duracion_minutos=60)
    bloqueos = [a_bloqueo(evento())]

    tapados = [h for h in jornada if any(b.solapa(h, h + timedelta(hours=1)) for b in bloqueos)]
    assert tapados == [LUNES_9, LUNES_9 + timedelta(hours=1)]


# ==========================================================================================
# Las otras tres razones para no bloquear
# ==========================================================================================


def test_un_evento_cancelado_no_bloquea():
    assert a_bloqueo(evento(status="cancelled")) is None


def test_un_evento_marcado_libre_no_bloquea():
    """`transparency: "transparent"` es el doctor diciendo «esto no me ocupa». Se le cree."""
    assert a_bloqueo(evento(transparency="transparent")) is None


def test_un_evento_marcado_ocupado_si_bloquea():
    assert a_bloqueo(evento(transparency="opaque")) is not None


# ==========================================================================================
# Día completo: el `end` exclusivo de Google
# ==========================================================================================


def test_un_dia_libre_bloquea_el_dia_entero():
    """Un día libre llega sin hora: `{"date": "..."}`, y el `end` es EXCLUSIVO.

    Si esto se tradujera como dos medianoches «tal cual», o peor, se ignorara por no tener
    `dateTime`, un día libre no bloquearía ni un minuto y Daniela citaría pacientes un día
    en que la clínica está cerrada.
    """
    libre = evento(
        summary="Día libre",
        start={"date": "2026-09-14"},
        end={"date": "2026-09-15"},
    )
    bloqueo = a_bloqueo(libre)

    assert bloqueo is not None
    assert bloqueo.inicio == datetime(2026, 9, 14, 0, 0, tzinfo=ZONA_BOGOTA)
    assert bloqueo.fin == datetime(2026, 9, 15, 0, 0, tzinfo=ZONA_BOGOTA)
    # Lo que de verdad importa: tapa cualquier hora de atención de ese día.
    assert bloqueo.solapa(LUNES_9, LUNES_9 + timedelta(hours=1))


def test_un_dia_libre_no_se_come_el_dia_siguiente():
    """El `end` exclusivo, por su consecuencia: las 9 del martes siguen libres."""
    bloqueo = a_bloqueo(
        evento(start={"date": "2026-09-14"}, end={"date": "2026-09-15"})
    )
    martes_9 = LUNES_9 + timedelta(days=1)

    assert bloqueo is not None
    assert not bloqueo.solapa(martes_9, martes_9 + timedelta(hours=1))


# ==========================================================================================
# Lo que no se entiende, no se inventa
# ==========================================================================================


@pytest.mark.parametrize(
    "roto",
    [
        {"start": {}, "end": {}},
        {"start": {"dateTime": "no-es-una-fecha"}, "end": {"dateTime": "2026-09-14T11:00:00-05:00"}},
        {"start": {"date": "14/09/2026"}, "end": {"date": "2026-09-15"}},
        # Fin antes que inicio: un rango imposible no se "arregla" dándole la vuelta.
        {"start": {"dateTime": "2026-09-14T11:00:00-05:00"},
         "end": {"dateTime": "2026-09-14T09:00:00-05:00"}},
    ],
)
def test_un_evento_sin_rango_utilizable_se_ignora(roto):
    """Se ignora y se deja constancia en el log; no se adivina una duración."""
    assert a_bloqueo(evento(**roto)) is None


def test_una_hora_en_utc_se_entiende():
    """Google puede devolver la hora en UTC con 'Z' si el calendario está en otra zona.

    `datetime.fromisoformat` no entiende la 'Z' antes de Python 3.11 y el proyecto declara
    3.10 como mínimo, así que esto se traduce a mano.
    """
    bloqueo = a_bloqueo(
        evento(start={"dateTime": "2026-09-14T14:00:00Z"},
               end={"dateTime": "2026-09-14T16:00:00Z"})
    )

    assert bloqueo is not None
    # 14:00 UTC son las 9:00 en Bogotá.
    assert bloqueo.inicio == LUNES_9


# ==========================================================================================
# La credencial: cuatro fallos que se parecen y no son el mismo
# ==========================================================================================


def test_una_cuenta_de_servicio_bien_formada_se_lee():
    datos = decodificar_cuenta_de_servicio(cuenta_de_servicio())

    assert datos["type"] == "service_account"
    assert datos["client_email"].endswith(".iam.gserviceaccount.com")


def test_la_variable_vacia_se_distingue():
    with pytest.raises(ErrorDeCalendario, match="vacía"):
        decodificar_cuenta_de_servicio("   ")


def test_el_json_pegado_tal_cual_se_distingue():
    """El fallo más común de todos: pegar el JSON en el .env sin convertirlo.

    El `.env` queda con la variable valiendo `{` y el resto del archivo interpretado como
    variables basura. Ya pasó una vez en este proyecto.
    """
    with pytest.raises(ErrorDeCalendario, match="base64"):
        decodificar_cuenta_de_servicio('{"type": "service_account"}')


def test_un_archivo_de_oauth_de_escritorio_se_distingue():
    """Se autentica una persona, no un servidor. Aquí no hay nadie para abrir un navegador."""
    otro = base64.b64encode(json.dumps({"type": "authorized_user"}).encode()).decode()

    with pytest.raises(ErrorDeCalendario, match="authorized_user"):
        decodificar_cuenta_de_servicio(otro)


def test_una_cuenta_sin_clave_privada_se_distingue():
    with pytest.raises(ErrorDeCalendario, match="private_key"):
        decodificar_cuenta_de_servicio(cuenta_de_servicio(private_key=None))


# ==========================================================================================
# La fábrica
# ==========================================================================================


class ConfigFalsa:
    def __init__(self, sa_b64="", calendar_id=""):
        self.google_sa_b64 = sa_b64
        self.google_calendar_id = calendar_id


def test_sin_credenciales_la_fabrica_devuelve_el_doble_y_no_revienta():
    """Un despliegue sin calendario tiene que ser ruidoso, no mortal.

    Daniela puede seguir contestando preguntas de precios y de tratamientos sin Calendar; lo
    que no puede es caerse entera por una variable. El aviso va al log, y `probar_calendario.py`
    es lo que comprueba el camino real.
    """
    assert isinstance(calendario_desde_config(ConfigFalsa()), CalendarioDoble)
    assert isinstance(calendario_desde_config(ConfigFalsa(sa_b64="x")), CalendarioDoble)
    assert isinstance(calendario_desde_config(ConfigFalsa(calendar_id="x")), CalendarioDoble)


def test_con_las_dos_credenciales_la_fabrica_intenta_google_de_verdad():
    """Con las dos variables presentes NO devuelve el doble en silencio.

    Es la mitad que importa de la prueba anterior: una fábrica que siempre devolviera el
    doble pasaría aquella y dejaría las citas fuera del calendario de los doctores sin que
    nadie se enterara. Con una credencial inventada tiene que fallar al construir.
    """
    with pytest.raises(ErrorDeCalendario):
        calendario_desde_config(ConfigFalsa(sa_b64="no-es-base64-@@@", calendar_id="x@gmail.com"))


# ==========================================================================================
# El calendario que no se pudo construir
# ==========================================================================================


def test_el_calendario_caido_lanza_en_los_cuatro_metodos():
    """Los cuatro, sin excepciones amables.

    La tentación es que `bloqueos()` devuelva `[]` y que `eliminar_evento` pase de largo --los
    dos «no hacen daño»--, y las dos serían mentiras caras: un `[]` significa «los doctores no
    apartaron nada» y pondría el almuerzo en oferta; un borrado fingido deja una cita viva en
    el calendario del doctor mientras Neon la da por cancelada.
    """
    caido = CalendarioCaido(motivo="Google devolvió 503")

    with pytest.raises(ErrorDeCalendario):
        caido.crear_evento(inicio=LUNES_9, duracion_minutos=60, titulo="x")
    with pytest.raises(ErrorDeCalendario):
        caido.mover_evento("ev-1", inicio=LUNES_9)
    with pytest.raises(ErrorDeCalendario):
        caido.eliminar_evento("ev-1")
    with pytest.raises(ErrorDeCalendario):
        caido.bloqueos(LUNES_9, LUNES_9 + timedelta(hours=2))


def test_el_calendario_caido_dice_por_que_se_cayo():
    """El motivo viaja en la excepción. Sin él, el log del turno dice «el calendario falló» y
    alguien tiene que ir a adivinar si es la credencial, el permiso o que Google está caído."""
    with pytest.raises(ErrorDeCalendario) as e:
        CalendarioCaido(motivo="falta compartir el calendario").crear_evento(
            inicio=LUNES_9, duracion_minutos=60, titulo="x"
        )

    assert "falta compartir el calendario" in str(e.value)


def test_el_calendario_caido_no_finge_ser_un_calendario_que_funciona():
    """La razón de que exista, en una aserción.

    Caer a `CalendarioDoble` cuando Google no arranca haría que `crear_cita` tomara el cupo,
    «creara» el evento en un diccionario y le confirmara la cita al paciente: el paciente
    llega a una clínica donde nadie lo espera. `CalendarioCaido` cumple el mismo Protocol
    --las tools lo aceptan igual-- pero se comporta como lo que es.
    """
    from maxicare_daniela.calendario import Calendario

    caido = CalendarioCaido()

    assert isinstance(caido, Calendario), "tiene que cumplir el Protocol o las tools no lo aceptan"
    assert not isinstance(caido, CalendarioDoble)


# ==========================================================================================
# El socket muerto
#
# El cliente de Google se construye UNA vez al arrancar el proceso y httplib2 guarda la
# conexión en keep-alive. Tras unos minutos sin tráfico, Google la cierra por su lado y la
# primera llamada siguiente muere con `BrokenPipeError`.
#
# Y httplib2 0.32.0 NO la cierra al fallar: su `_conn_request` solo reintenta con
# `ENETUNREACH` y `EADDRNOTAVAIL`, y para lo demás hace `raise` sin tocar la conexión. Así
# que el socket muerto se queda en su caché y un reintento a secas volvería a chocar con él.
# Por eso el reintento REHACE el servicio, y por eso estas pruebas cuentan cuántos servicios
# se construyeron.
# ==========================================================================================


class ErrorDeGoogle(Exception):
    """Lo que `googleapiclient` lanza: una excepción con `status_code`.

    `HttpError` de verdad exige un objeto de respuesta de httplib2 para construirse, y lo
    único que este módulo le pregunta es el `status_code` (`getattr(e, "status_code", None)`).
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _Peticion:
    def __init__(self, servicio, nombre):
        self._servicio = servicio
        self._nombre = nombre

    def execute(self):
        return self._servicio.responder(self._nombre)


class _Coleccion:
    def __init__(self, servicio, prefijo):
        self._servicio = servicio
        self._prefijo = prefijo

    def __getattr__(self, metodo):
        def arma(**argumentos):
            self._servicio.argumentos.append((f"{self._prefijo}.{metodo}", argumentos))
            return _Peticion(self._servicio, f"{self._prefijo}.{metodo}")

        return arma


class ServicioFalso:
    """Doble del objeto que devuelve `build(...)`. Reproduce la cadena que usa el módulo:
    `servicio.events().list(**kw).execute()`.

    El `guion` es una lista de resultados en orden: un `dict` se devuelve y una excepción se
    lanza. Agotado el guion devuelve `{}`, que para `events.list` significa «ninguna página
    más».
    """

    def __init__(self, guion=None) -> None:
        self.guion = list(guion or [])
        #: Qué se le pidió, en orden: `events.list`, `events.insert`...
        self.llamadas: list[str] = []
        self.argumentos: list[tuple[str, dict]] = []

    def events(self):
        return _Coleccion(self, "events")

    def calendars(self):
        return _Coleccion(self, "calendars")

    def responder(self, nombre):
        self.llamadas.append(nombre)
        resultado = self.guion.pop(0) if self.guion else {}
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado


def calendario_con_servicios(monkeypatch, *guiones):
    """Un `CalendarioGoogle` de verdad con servicios doblados detrás.

    Un guion por servicio: el primero es el que se construye al arrancar --y atiende además
    la comprobación de acceso del constructor, que se le pone delante aquí-- y los siguientes
    son los que `build` entrega cada vez que el módulo REHACE el servicio.

    `entregados` es lo que mide si se rehizo: con un solo elemento, no se rehizo nunca.
    """
    from maxicare_daniela.calendario import CalendarioGoogle

    servicios = [ServicioFalso(guion) for guion in guiones]
    servicios[0].guion.insert(0, {})  # la lectura real que hace el constructor
    entregados: list[ServicioFalso] = []

    def build_falso(*_a, **_k):
        siguiente = (
            servicios[len(entregados)] if len(entregados) < len(servicios) else ServicioFalso()
        )
        entregados.append(siguiente)
        return siguiente

    monkeypatch.setattr("googleapiclient.discovery.build", build_falso)
    # La clave privada de `cuenta_de_servicio()` no es una clave de verdad --no debe serlo--
    # así que cargarla reventaría antes de llegar a lo que se quiere medir.
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        lambda *_a, **_k: object(),
    )

    calendario = CalendarioGoogle(sa_b64=cuenta_de_servicio(), calendario_id="x@gmail.com")
    return calendario, entregados


def test_un_socket_muerto_al_consultar_bloqueos_se_reintenta_UNA_vez(monkeypatch):
    """El caso Sandy Nariño (+57 319 661 5042), 1/10/2026 17:44 de Bogotá.

    La paciente eligió «este sábado a las dos», `crear_cita` llamó a `_bloqueo_que_tapa`, y
    `bloqueos()` murió con `BrokenPipeError`: el socket llevaba cinco minutos sin usarse --la
    última llamada buena fue a las 17:39:17-- y Google ya lo había cerrado. No había ni un
    reintento en todo el módulo, así que un hipo de red de milisegundos le costó a la
    paciente 15 h 34 min de silencio.
    """
    calendario, entregados = calendario_con_servicios(
        monkeypatch,
        [BrokenPipeError(32, "Broken pipe")],
        [{"items": [evento()]}],
    )

    bloqueos = calendario.bloqueos(LUNES_9, LUNES_9 + timedelta(hours=3))

    assert len(bloqueos) == 1, "el reintento tenía que devolver el bloqueo del doctor"
    assert len(entregados) == 2, "el servicio NO se rehizo: el reintento choca con el mismo socket"


def test_el_reintento_no_es_infinito(monkeypatch):
    """UNA vez, como `relevo.activar` (no negociable 19). Si el segundo intento también cae,
    es que Google no está, y eso ya no es un socket rancio: se traduce y sube."""
    calendario, entregados = calendario_con_servicios(
        monkeypatch,
        [BrokenPipeError(32, "Broken pipe")],
        [BrokenPipeError(32, "Broken pipe")],
    )

    with pytest.raises(ErrorDeCalendario) as e:
        calendario.bloqueos(LUNES_9, LUNES_9 + timedelta(hours=3))

    assert "consultar los bloqueos" in str(e.value)
    assert len(entregados) == 2, "se rehízo más de una vez"


def test_un_fallo_que_NO_es_un_socket_muerto_no_se_reintenta(monkeypatch):
    """Un 500 de Google, una credencial caducada o un timeout no se arreglan rehaciendo el
    socket, y reintentarlos solo gasta el minuto que tiene el turno.

    **El timeout es el que importa de esta lista y por eso está aquí:** no dice que la
    petición no llegara, así que repetir una ESCRITURA tras un timeout puede duplicarla.
    """
    for fallo in (ErrorDeGoogle(500), TimeoutError("se agotó el tiempo"), ValueError("raro")):
        calendario, entregados = calendario_con_servicios(monkeypatch, [fallo])

        with pytest.raises(ErrorDeCalendario):
            calendario.bloqueos(LUNES_9, LUNES_9 + timedelta(hours=3))

        assert len(entregados) == 1, f"{type(fallo).__name__} no tenía que rehacer el servicio"


def test_crear_evento_NO_reintenta_porque_insertar_dos_veces_son_DOS_citas(monkeypatch):
    """La excepción, y es la que decide el empate.

    `events.insert` es la única llamada de este módulo que no es idempotente: la API no
    acepta clave de idempotencia, así que si la petición SÍ llegó a Google y lo que se perdió
    fue la respuesta, el reintento crea un segundo evento. Dos citas en el calendario del
    doctor sobre la misma hora, puestas por el sistema que existe para que eso no pase.

    El resto sí reintenta --un `get` y un `list` son lecturas, un `patch` con cuerpo fijo deja
    el evento igual, y un `delete` repetido es un 404 que este módulo ya cuenta como éxito--.
    Y `crear_evento` no lo necesita: `_bloqueo_que_tapa` acaba de leer el calendario por el
    mismo socket microsegundos antes, así que llega caliente.
    """
    calendario, entregados = calendario_con_servicios(
        monkeypatch,
        [BrokenPipeError(32, "Broken pipe")],
    )

    with pytest.raises(ErrorDeCalendario) as e:
        calendario.crear_evento(inicio=LUNES_9, duracion_minutos=60, titulo="Valoración")

    assert "crear el evento" in str(e.value)
    assert len(entregados) == 1, "se reintentó un insert: puede dejar dos eventos"


def test_las_otras_tres_llamadas_idempotentes_tambien_reintentan(monkeypatch):
    """`mover_evento` (su lectura y su `patch`), `eliminar_evento` y `obtener_evento`.

    Sin esto, el socket rancio se cobraría una reprogramación o una cancelación en vez de una
    cita nueva, y el paciente oiría «no se pudo» por lo mismo que aquí ya no pasa.
    """
    calendario, entregados = calendario_con_servicios(
        monkeypatch,
        [BrokenPipeError(32, "Broken pipe")],
        [
            {
                "start": {"dateTime": "2026-09-14T09:00:00-05:00"},
                "end": {"dateTime": "2026-09-14T10:00:00-05:00"},
            },
            {},  # el patch
        ],
    )
    calendario.mover_evento("evt-1", inicio=LUNES_9 + timedelta(days=1))
    assert len(entregados) == 2

    calendario, entregados = calendario_con_servicios(
        monkeypatch, [BrokenPipeError(32, "Broken pipe")], [{}]
    )
    calendario.eliminar_evento("evt-1")
    assert len(entregados) == 2

    calendario, entregados = calendario_con_servicios(
        monkeypatch,
        [BrokenPipeError(32, "Broken pipe")],
        [
            {
                "start": {"dateTime": "2026-09-14T09:00:00-05:00"},
                "end": {"dateTime": "2026-09-14T10:00:00-05:00"},
            }
        ],
    )
    assert calendario.obtener_evento("evt-1") is not None
    assert len(entregados) == 2


def test_el_reintento_no_se_come_el_404_que_significa_que_ya_no_existe(monkeypatch):
    """La mitad que un reintento mal puesto rompería en silencio.

    `obtener_evento` devuelve `None` SOLO con un 404 o un 410 --«lo borraron»--, y quien
    pregunta cancela la cita al oírlo. `eliminar_evento` cuenta ese mismo 404 como éxito. Si
    el reintento tradujera todo a `ErrorDeCalendario`, las dos conductas desaparecerían y
    `_sincronizar_con_calendar` dejaría de enterarse de que el doctor borró el evento.
    """
    calendario, entregados = calendario_con_servicios(monkeypatch, [ErrorDeGoogle(404)])
    assert calendario.obtener_evento("evt-borrado") is None
    assert len(entregados) == 1

    calendario, _ = calendario_con_servicios(monkeypatch, [ErrorDeGoogle(410)])
    calendario.eliminar_evento("evt-borrado")  # no lanza: lo que se quería es que no exista

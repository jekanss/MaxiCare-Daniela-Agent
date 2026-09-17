"""El despachador de recordatorios, sin base y sin red.

Las guardas y las bandas horarias viven aquí porque son decisiones del código, no de la
base: se prueban en milisegundos y corren siempre. Lo que toca Neon está en
`test_seguimientos_neon.py`, marcado `neon`.

Ningún momento sale del reloj de la máquina: todos entran como parámetro.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada
from maxicare_daniela.herramientas import ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sábado, domingo cerrado


class _ConexionFalsa:
    """Lo mínimo para doblar la conexión del ciclo.

    `despachar` ya no la usa como context manager: abrirla y cerrarla van por `to_thread`
    como todo lo demás, porque `psycopg.connect` contra Neon es un handshake TLS completo y
    bloqueaba el bucle de eventos que atiende los webhooks. `cerrada` deja comprobable que el
    cierre ocurre incluso cuando el ciclo revienta a mitad.
    """

    def __init__(self):
        self.cerrada = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def close(self):
        self.cerrada = True


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Septiembre de 2026: el 15 es martes, el 19 sábado, el 20 domingo."""
    return datetime(2026, 9, dia, hora, minuto, tzinfo=ZONA_BOGOTA)


def test_una_cita_a_menos_de_cuatro_horas_no_lleva_recordatorio():
    # Escribe a las 9:00 y agenda para las 11:00 del mismo día: acaba de hablar con Daniela.
    assert (
        s.momento_del_recordatorio(
            inicio_cita=momento(15, 11),
            ahora=momento(15, 9),
            jornada=JORNADA,
        )
        is None
    )


def test_entre_cuatro_y_veinticuatro_horas_sale_dos_horas_antes():
    assert s.momento_del_recordatorio(
        inicio_cita=momento(15, 15),
        ahora=momento(15, 9),
        jornada=JORNADA,
    ) == momento(15, 13)


def test_a_mas_de_un_dia_sale_la_vispera_a_las_seis():
    assert s.momento_del_recordatorio(
        inicio_cita=momento(17, 9),
        ahora=momento(15, 9),
        jornada=JORNADA,
    ) == momento(16, 18)


def test_las_dos_horas_antes_que_caen_de_madrugada_se_adelantan_a_la_vispera():
    # Cita a las 8:00 del miércoles, agendada el martes a las 10:00: 22 h de antelación, cae
    # en la banda de las 2 h, y «2 h antes» son las 6:00 a. m. con la clínica cerrada.
    # Aplazarlo a la apertura lo dejaría llegando a las 8:00, la hora de la cita.
    assert s.momento_del_recordatorio(
        inicio_cita=momento(16, 8),
        ahora=momento(15, 10),
        jornada=JORNADA,
    ) == momento(15, 17)  # la víspera, a la hora de cierre


def test_la_vispera_de_un_lunes_es_el_domingo_y_la_clinica_cierra():
    # Cita el lunes 21 a las 9:00. La víspera es domingo: la clínica no abre, así que el
    # recordatorio se adelanta al sábado a la hora de cierre.
    assert s.momento_del_recordatorio(
        inicio_cita=momento(21, 9),
        ahora=momento(17, 9),
        jornada=JORNADA,
    ) == momento(19, 15)  # sábado, cierre de sábado


def test_un_momento_que_ya_paso_no_se_programa():
    # Si el cálculo cae antes de `ahora`, no hay recordatorio que valga.
    assert (
        s.momento_del_recordatorio(
            inicio_cita=momento(15, 10),
            ahora=momento(15, 9, 30),
            jornada=JORNADA,
        )
        is None
    )


def fila(**cambios) -> dict:
    base = dict(
        id=1,
        conversacion_id="conv-1",
        cita_id="cita-1",
        tipo="recordatorio_cita",
        fecha_objetivo=momento(16, 18),
        # `creado_en` por defecto es la MISMA fecha que `fecha_objetivo`: por defecto `tipo`
        # es un recordatorio de cita, así que `es_reactivacion` da `False` y R3bis ni se
        # evalúa -- pero las pruebas de este archivo que sí ponen un `tipo` de reactivación
        # (líneas de más abajo) necesitan la clave presente o `fila["creado_en"]` revienta.
        creado_en=momento(16, 18),
        intentos=0,
        telefono="573001112233",
        nombre_completo="Ana Gómez",
        cita_inicio=momento(17, 9),
        cita_estado="confirmada",
        tomada_por=None,
        no_contactar=False,
    )
    base.update(cambios)
    return base


def test_g1_una_cita_cancelada_no_recibe_recordatorio():
    d = s.decidir(
        fila(cita_estado="cancelada"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_cambio"


def test_g2_una_cita_que_ya_paso_no_recibe_recordatorio():
    d = s.decidir(
        fila(cita_inicio=momento(16, 9)),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_pasada"


def test_g3_un_recordatorio_con_mas_de_dos_horas_de_retraso_se_descarta():
    # El proceso estuvo caído: le tocaba a las 18:00 y son las 21:00.
    d = s.decidir(
        fila(),
        ahora=momento(16, 21),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "llego_tarde"


def test_g3_un_aplazamiento_no_le_borra_la_memoria_a_la_guarda_del_retraso():
    """`aplazar_seguimiento` reescribe `fecha_objetivo`, así que la cuenta contra ella se pone
    a cero en cada aplazamiento. Una fila de víspera que a las 18:00 pilla al doctor en relevo
    encadena G4 -> G5 -> la mañana siguiente y llega FRESCA según esa cuenta, a una hora de la
    cita. La cita es lo que ningún aplazamiento puede reescribir, así que se mide contra ella."""
    d = s.decidir(
        # `fecha_objetivo` de hace un instante: la reescribió el último aplazamiento.
        fila(fecha_objetivo=momento(17, 8), cita_inicio=momento(17, 9)),
        ahora=momento(17, 8),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "anular"
    assert d.motivo == "cita_inminente"


def test_la_banda_de_dos_horas_sobrevive_a_la_guarda_de_la_cita_inminente():
    """La guarda de arriba mide contra la cita, y la banda corta se programa EXACTAMENTE a dos
    horas de ella: medirla contra las dos horas redondas anularía la banda entera, todos los
    días, porque el ciclo recoge la fila siempre unos segundos después de su `fecha_objetivo`.
    Es el caso que fija el margen, y por eso se prueba con el ciclo llegando tarde."""
    d = s.decidir(
        fila(fecha_objetivo=momento(16, 13), cita_inicio=momento(16, 15)),
        ahora=momento(16, 13) + timedelta(seconds=55),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "enviar"


def test_un_aplazamiento_de_g4_no_se_come_la_banda_corta():
    """G4 aplaza media hora a propósito --«el doctor puede devolver la conversación en diez
    minutos y el recordatorio sigue siendo válido»--. El margen de la guarda anterior cubre ese
    aplazamiento: un recordatorio a hora y media de la cita todavía sirve para salir de casa, y
    anularlo convertiría en silencio el aplazamiento de G4 en una anulación."""
    d = s.decidir(
        fila(fecha_objetivo=momento(16, 13, 30), cita_inicio=momento(16, 15)),
        ahora=momento(16, 13, 30),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "enviar"


def test_g4_con_el_relevo_puesto_se_aplaza_y_no_se_anula():
    # El doctor puede devolver la conversación en diez minutos: el recordatorio sigue siendo
    # válido. Anularlo aquí lo perdería para siempre.
    d = s.decidir(
        fila(tomada_por="Dr. Pérez"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "aplazar"
    assert d.hasta == momento(16, 18, 30)


def test_g5_fuera_de_la_jornada_se_aplaza_a_la_apertura():
    d = s.decidir(
        fila(fecha_objetivo=momento(16, 6), cita_inicio=momento(17, 9)),
        ahora=momento(16, 6),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "aplazar"
    assert d.hasta == momento(16, 8)


def test_g6_si_el_paciente_acaba_de_escribir_no_se_le_recuerda_nada():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(16, 17, 40),
    )
    assert d.accion == "anular"
    assert d.motivo == "contacto_reciente"


def test_g7_no_se_manda_un_segundo_mensaje_al_mismo_numero():
    """Y se aplaza a la PRÓXIMA APERTURA de la ventana de envío, no un minuto.

    El minuto prometía un agrupador que no existe: en el ciclo siguiente la tanda arranca
    vacía, G7 da `False` y la segunda fila sale sola, con sesenta segundos de diferencia sobre
    la primera. El paciente recibía las dos plantillas que G7 existe para evitar, y la clínica
    las pagaba las dos.
    """
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
        ya_salio_a_ese_numero=True,
    )
    assert d.accion == "aplazar"
    assert d.motivo == "uno_por_numero"
    assert d.hasta == momento(17, 8)  # la apertura del día siguiente, no 18:01


def test_un_recordatorio_limpio_sale():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(14, 9),
    )
    assert d.accion == "enviar"


def test_un_seguimiento_sin_cita_se_salta_las_tres_primeras_guardas():
    """«Llámenme el lunes»: no cuelga de ninguna cita, así que G1, G2 y G3 no aplican.

    El `tipo` es del vocabulario cerrado (`TIPO_SIN_AGENDAR`), a propósito y no por
    casualidad: antes de la ronda 1 de revisión esta prueba usaba `tipo="reactivacion"`
    -fuera del vocabulario- y pasaba por el motivo EQUIVOCADO. `es_reactivacion` tenía la
    polaridad al revés (`tipo in TIPOS_DE_REACTIVACION`, falla ABIERTO), así que un tipo
    inventado hacía que `es_reactivacion` diera `False` y las cinco guardas de reactivación
    (R1-R5) ni se evaluaran: el "enviar" salía de saltarse TODO, no solo G1-G3. Con la
    polaridad corregida (`not in TIPOS_NO_COMERCIALES`) esta fila SÍ pasa por R1-R5 -y sigue
    dando "enviar", porque no tiene nada malo: sin fallidos, sin tope, a tiempo, en horario,
    sin contacto reciente-. El caso adversario que SÍ debe frenar está en
    `tests/test_reactivacion.py::test_un_tipo_fuera_del_vocabulario_no_esquiva_las_guardas_
    de_reactivacion`.
    """
    d = s.decidir(
        fila(cita_id=None, cita_inicio=None, cita_estado=None, tipo=s.TIPO_SIN_AGENDAR),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "enviar"


def test_g0_anula_una_reactivacion_a_quien_pidio_la_baja():
    decision = s.decidir(
        fila(tipo=s.TIPO_SIN_AGENDAR, cita_id=None, cita_inicio=None, no_contactar=True),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "anular"
    assert decision.motivo == "baja_solicitada"


def test_g0_NO_toca_el_recordatorio_de_una_cita():
    """La prueba que más importa de todo el trabajo. Pedir que no te manden publicidad no es
    renunciar a que te avisen de tu propia cita: si se mezclan, el paciente no llega, la
    clínica pierde el cupo, y el sistema habría hecho exactamente lo que se le pidió."""
    decision = s.decidir(
        fila(tipo="recordatorio_cita", no_contactar=True),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "enviar"


def test_un_tipo_inventado_se_trata_como_comercial():
    """La 021 ya cerró el vocabulario que el MODELO puede pedir (`Literal` +
    `TIPOS_QUE_EL_MODELO_PUEDE_PEDIR`), así que esto ya no puede pasar por esa vía. Pero el
    CHECK de esa migración es NOT VALID -no revisa lo que ya estaba en `public` de cuando
    `tipo` era texto libre-, así que una fila VIEJA con un tipo que nadie reconoce sigue
    siendo alcanzable, y por eso este caso se deja con un tipo fuera de vocabulario A
    PROPÓSITO. La lista blanca falla hacia el lado seguro: lo que no está en ella se
    comprueba contra la baja. Una lista negra dejaría pasar cualquier invento directo al
    envío."""
    decision = s.decidir(
        fila(tipo="promo_de_diciembre", cita_id=None, cita_inicio=None, no_contactar=True),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "anular"
    assert decision.motivo == "baja_solicitada"


def test_sin_baja_g0_no_hace_nada():
    decision = s.decidir(
        fila(tipo=s.TIPO_SIN_AGENDAR, cita_id=None, cita_inicio=None, no_contactar=False),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )

    assert decision.accion == "enviar"


def test_los_cuatro_huecos_de_la_plantilla_salen_en_hora_de_bogota(monkeypatch):
    """La única función de esta rama cuyo resultado lee un paciente, llamada de verdad.

    El `cita_inicio` entra **en UTC**, que es como lo devuelve psycopg: `citas.inicio` es
    `TIMESTAMPTZ` y `persistencia.conectar` no fija el `TimeZone` de la sesión, así que la
    fila llega normalizada a UTC y no en el huso con el que se calculó. Mandar
    `f"{inicio:%H:%M}"` sobre ese valor le decía al paciente «a las 14:00» sobre una cita de
    las 9:00: cinco horas tarde a una clínica donde ya nadie lo esperaba.

    Y los huecos 2 y 3 son datos DISTINTOS. Con el formateador de `herramientas`, que devuelve
    «jueves 17/9 a las 09:00», la plantilla quedaba «su cita el jueves 17/9 a las 09:00 a las
    09:00 para Limpieza». La prueba que parecía cubrirlo escribía los cuatro valores a mano y
    documentaba una forma que la función nunca produjo.
    """
    from datetime import timezone

    # Jueves 17/9/2026, 9:00 en Bogotá == 14:00 UTC.
    utc = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    huecos = s._parametros_del_recordatorio(
        {
            "cita_inicio": utc,
            "nombre_completo": "Ana Gómez",
            "tratamiento": "Limpieza",
        }
    )
    assert huecos == ["Ana", "jueves 17/9", "09:00", "Limpieza"]


def test_sin_cita_los_dos_huecos_de_fecha_quedan_en_pendiente():
    """La regla dura 3 es para el código, no un permiso para mandarle el marcador a un
    paciente: `despachar` anula la fila antes de llegar al canal. Esto solo fija que la
    función no se inventa una fecha plausible si la recibe vacía."""
    huecos = s._parametros_del_recordatorio(
        {"cita_inicio": None, "nombre_completo": None, "tratamiento": None}
    )
    assert huecos == ["paciente", "PENDIENTE", "PENDIENTE", "su cita"]


class _RespuestaFalsa:
    status_code = 200

    def json(self):
        return {"messages": [{"id": "wamid.PRUEBA"}]}


def test_la_plantilla_viaja_con_el_tipo_y_los_parametros_que_meta_espera(monkeypatch):
    """Meta rechaza el envío entero si el cuerpo no lleva `type: template` con su `language`.

    Se comprueba la FORMA del cuerpo y no solo que no lance: un cuerpo mal armado devuelve 200
    en algunos casos y el mensaje no llega nunca.
    """
    import asyncio

    from maxicare_daniela import canales

    enviados = {}

    class _ClienteFalso:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            enviados["url"] = url
            enviados["cuerpo"] = json
            return _RespuestaFalsa()

    monkeypatch.setattr(canales.httpx, "AsyncClient", _ClienteFalso)

    wa = canales.WhatsApp("token-falso", "phone-id-falso")
    wamid = asyncio.run(
        wa.enviar_plantilla(
            "573001112233",
            plantilla="recordatorio_cita",
            parametros=["Ana", "miércoles 17/9", "09:00", "Limpieza"],
        )
    )

    assert wamid == "wamid.PRUEBA"
    cuerpo = enviados["cuerpo"]
    assert cuerpo["type"] == "template"
    assert cuerpo["template"]["name"] == "recordatorio_cita"
    assert cuerpo["template"]["language"] == {"code": "es"}
    valores = [p["text"] for p in cuerpo["template"]["components"][0]["parameters"]]
    assert valores == ["Ana", "miércoles 17/9", "09:00", "Limpieza"]


def test_abrir_y_cerrar_la_conexion_no_corren_en_el_bucle_de_eventos(monkeypatch):
    """`psycopg.connect` contra Neon es un handshake TLS completo y el cierre hace commit o
    rollback antes de soltar el socket: cientos de milisegundos BLOQUEANTES cada uno, en el
    mismo bucle que atiende los webhooks de pacientes reales, cada sesenta segundos. Todas las
    demás llamadas a base de `despachar` ya iban por `to_thread`; estas dos no.

    Se comprueba por el HILO, que es lo que importa: que corran en uno distinto del que tiene
    el bucle. Y el cierre se comprueba en el camino de error, que es donde un `try/finally` mal
    puesto deja una conexión de Neon colgada cada minuto hasta agotar el pooler.
    """
    import asyncio
    import threading

    from maxicare_daniela import persistencia, seguimientos as s

    hilo_del_bucle = threading.get_ident()
    hilos: dict[str, int] = {}
    conexion = _ConexionFalsa()

    def _conectar(url):
        hilos["abrir"] = threading.get_ident()
        return conexion

    def _cerrar_espiado():
        hilos["cerrar"] = threading.get_ident()
        conexion.cerrada = True

    conexion.close = _cerrar_espiado
    monkeypatch.setattr(persistencia, "conectar", _conectar)
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})

    def _reventar(conn, **k):
        raise RuntimeError("Neon no respondió")

    monkeypatch.setattr(persistencia, "seguimientos_por_despachar", _reventar)

    async def escenario():
        with pytest.raises(RuntimeError):
            await s.despachar(
                database_url="postgresql://no-se-usa",
                whatsapp=None,
                jornada=JORNADA,
                plantilla="",
                ahora=momento(16, 18),
            )

    asyncio.run(escenario())

    assert conexion.cerrada, "el ciclo reventó y dejó la conexión abierta"
    assert hilos["abrir"] != hilo_del_bucle
    assert hilos["cerrar"] != hilo_del_bucle


def test_el_idioma_de_la_plantilla_llega_hasta_meta(monkeypatch):
    """Meta no busca la traducción más parecida: si el código no coincide al carácter con el
    de la traducción registrada, rechaza el envío entero con el error 132001. Cableado a `es`,
    una plantilla aprobada como `es_CO` sería el 100 % de los recordatorios fallando, con
    rastro solo en el log y en una columna que nadie consulta. Aquí se fija que el valor
    RECORRE el camino entero: de `despachar` al cuerpo que sale a la Graph API."""
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    enviados: list[dict] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia, "marcar_seguimiento_enviado", lambda conn, id_seguimiento: True
    )
    monkeypatch.setattr(
        persistencia,
        "anotar_recordatorio_en_conversacion",
        lambda conn, id_conversacion, **k: None,
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            enviados.append(k)
            return "wamid.X"

    asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            idioma="es_CO",
            ahora=momento(16, 18),
        )
    )

    assert enviados and enviados[0]["idioma"] == "es_CO"


def test_el_despachador_marca_antes_de_enviar(monkeypatch):
    """El orden es la defensa contra el duplicado, y es lo contrario del instinto.

    No hay transacción que cubra una llamada a Meta. Si se envía primero y el proceso muere
    antes del commit, la fila sigue pendiente y el barrido de sesenta segundos después manda el
    mismo recordatorio otra vez, sin que ninguna guarda lo detecte.
    """
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    orden: list[str] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    def _marcar(conn, id_seguimiento):
        orden.append("marcar")
        return True  # nadie más se la llevó: es esta llamada la que la marca

    monkeypatch.setattr(persistencia, "marcar_seguimiento_enviado", _marcar)
    monkeypatch.setattr(
        persistencia,
        "anotar_recordatorio_en_conversacion",
        lambda conn, id_conversacion, **k: None,
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            orden.append("enviar")
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 18),
        )
    )

    assert orden == ["marcar", "enviar"]
    assert recuento["enviados"] == 1


def test_sin_plantilla_configurada_decide_pero_no_manda(monkeypatch):
    """Es lo que permite colgar el despachador en producción y comprobar que decide bien antes
    de que mande un solo mensaje.

    Y la fila NO se marca `enviado_en`: con la plantilla apagada, `despachar` mira si hay
    plantilla ANTES de llamar a `marcar_seguimiento_enviado`, así que la cola no se consume en
    silencio. Si se marcara igual, el modo de prueba se comería el recordatorio para siempre
    -nunca se reintentaría, ni el día en que alguien configure la plantilla-, que es peor que
    los cuatro hallazgos de la revisión juntos. Esta prueba lo deja en firme.
    """
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    mandados: list[str] = []
    marcadas: list[int] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )

    def _marcar(conn, id_seguimiento):
        marcadas.append(id_seguimiento)
        return True

    monkeypatch.setattr(persistencia, "marcar_seguimiento_enviado", _marcar)

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            mandados.append(telefono)
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="",
            ahora=momento(16, 18),
        )
    )

    assert mandados == []
    assert marcadas == []
    assert recuento["enviados"] == 0


def test_el_despachador_lee_la_hora_de_vispera_de_la_configuracion(monkeypatch):
    """G5 necesita la `hora_recordatorio_vispera` REAL, no la constante de respaldo, porque
    la víspera sale a las 18:00 con la clínica cerrada desde las 17:00.

    Sin este cableado, `decidir` recibiría siempre el respaldo (18) así la clínica haya
    cambiado la hora desde el panel, y esta prueba lo demuestra sin tocar `decidir` --a las
    19:00, con el respaldo la ventana de envío ya cerró (`limite = max(17, 19) = 19`) y el
    seguimiento se aplazaría; con la hora configurada (20) la ventana llega hasta las 21 y
    el mismo seguimiento sale.
    """
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(
        persistencia, "leer_configuracion", lambda conn: {"hora_recordatorio_vispera": 20}
    )
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia, "marcar_seguimiento_enviado", lambda conn, id_seguimiento: True
    )
    monkeypatch.setattr(
        persistencia,
        "anotar_recordatorio_en_conversacion",
        lambda conn, id_conversacion, **k: None,
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 19),
        )
    )

    assert recuento == {"enviados": 1, "anulados": 0, "aplazados": 0, "fallidos": 0}


def test_un_seguimiento_sin_cita_se_anula_por_falta_de_plantilla_y_no_se_manda(monkeypatch):
    """Un seguimiento sin cita (p. ej. una reactivación) llega a "enviar" -G1-G3 se saltan sin
    cita que mirar, ver `test_un_seguimiento_sin_cita_se_salta_las_tres_primeras_guardas`- pero
    la plantilla que manda `despachar` es LA DE RECORDATORIO DE CITA. Sin `cita_inicio`,
    mandarla dejaría al paciente leyendo el literal "PENDIENTE" por WhatsApp: no es una regla
    de negocio, es que hoy no existe una plantilla para este tipo. Se anula con un motivo
    propio, no se manda, y el canal ni se toca.
    """
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    anuladas: list[tuple[int, str]] = []
    mandados: list[str] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia,
        "seguimientos_por_despachar",
        lambda conn, **k: [
            fila(cita_id=None, cita_inicio=None, cita_estado=None, tipo=s.TIPO_SIN_AGENDAR)
        ],
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia,
        "anular_seguimiento",
        lambda conn, id_seguimiento, *, motivo: anuladas.append((id_seguimiento, motivo)),
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            mandados.append(telefono)
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 18),
        )
    )

    assert anuladas == [(1, "sin_plantilla")]
    assert mandados == []
    assert recuento == {"enviados": 0, "anulados": 1, "aplazados": 0, "fallidos": 0}


def test_una_fila_que_decidir_anula_no_toca_el_canal(monkeypatch):
    """Cubre la salida `anulados` del bucle -G1, una cita cancelada-, que ninguna prueba
    ejercitaba todavía: hasta ahora solo se probaba `enviados`."""
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    anuladas: list[tuple[int, str]] = []
    mandados: list[str] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia,
        "seguimientos_por_despachar",
        lambda conn, **k: [fila(cita_estado="cancelada")],
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia,
        "anular_seguimiento",
        lambda conn, id_seguimiento, *, motivo: anuladas.append((id_seguimiento, motivo)),
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            mandados.append(telefono)
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 18),
        )
    )

    assert anuladas == [(1, "cita_cambio")]
    assert mandados == []
    assert recuento == {"enviados": 0, "anulados": 1, "aplazados": 0, "fallidos": 0}


def test_una_fila_que_decidir_aplaza_no_toca_el_canal(monkeypatch):
    """Cubre la salida `aplazados` del bucle -G4, el relevo puesto-, con el `hasta` que
    `decidir` calculó de verdad y no uno inventado por la prueba."""
    import asyncio

    from maxicare_daniela import persistencia, seguimientos as s

    aplazadas: list[tuple[int, object]] = []
    mandados: list[str] = []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia,
        "seguimientos_por_despachar",
        lambda conn, **k: [fila(tomada_por="Dr. Pérez")],
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia,
        "aplazar_seguimiento",
        lambda conn, id_seguimiento, *, hasta: aplazadas.append((id_seguimiento, hasta)),
    )

    class _WhatsAppFalso:
        async def enviar_plantilla(self, telefono, **k):
            mandados.append(telefono)
            return "wamid.X"

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppFalso(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 18),
        )
    )

    assert aplazadas == [(1, momento(16, 18, 30))]
    assert mandados == []
    assert recuento == {"enviados": 0, "anulados": 0, "aplazados": 1, "fallidos": 0}


def test_los_intentos_agotados_marcan_fallido_y_no_se_pierden(monkeypatch):
    """Cubre la salida `fallidos` del bucle: el único camino de error de la única ruta que
    llega a un paciente real. El `sleep` entre intentos se dobla para no pagar los segundos
    reales -son `INTENTOS_DE_ENVIO - 1` esperas de `SEGUNDOS_ENTRE_INTENTOS`- y la prueba fija
    el número de intentos: hoy nadie más lo garantiza."""
    import asyncio

    from maxicare_daniela import canales, persistencia, seguimientos as s

    llamadas_al_canal: list[str] = []
    fallos: list[tuple[int, str]] = []

    async def _sin_espera(_segundos):
        return None

    # OJO al doblar esto: `s.asyncio` ES el módulo `asyncio` -no una copia-, así que
    # `lambda _: asyncio.sleep(0)` se llamaría a sí misma para siempre (`asyncio.sleep` ya
    # es la propia lambda cuando corre). Por eso el reemplazo no puede invocar `asyncio.sleep`
    # ni directa ni indirectamente: tiene que ser una corrutina que no espere nada.
    monkeypatch.setattr(s.asyncio, "sleep", _sin_espera)
    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsa())
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: {})
    monkeypatch.setattr(
        persistencia, "seguimientos_por_despachar", lambda conn, **k: [fila()]
    )
    monkeypatch.setattr(
        persistencia, "ultimo_mensaje_del_paciente", lambda conn, telefono: None
    )
    monkeypatch.setattr(
        persistencia, "marcar_seguimiento_enviado", lambda conn, id_seguimiento: True
    )
    monkeypatch.setattr(
        persistencia,
        "anotar_fallo_de_seguimiento",
        lambda conn, id_seguimiento, *, fallo: fallos.append((id_seguimiento, fallo)),
    )

    class _WhatsAppQueSiempreFalla:
        async def enviar_plantilla(self, telefono, **k):
            llamadas_al_canal.append(telefono)
            raise canales.ErrorDeCanal("Meta no contestó")

    recuento = asyncio.run(
        s.despachar(
            database_url="postgresql://no-se-usa",
            whatsapp=_WhatsAppQueSiempreFalla(),
            jornada=JORNADA,
            plantilla="recordatorio_cita",
            ahora=momento(16, 18),
        )
    )

    assert len(llamadas_al_canal) == s.INTENTOS_DE_ENVIO == 3
    assert len(fallos) == 1
    id_seguimiento, fallo = fallos[0]
    assert id_seguimiento == 1
    assert fallo.startswith("ErrorDeCanal")
    assert recuento == {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 1}


def test_el_despachador_arranca_aunque_no_haya_telegram(monkeypatch):
    """El barrido de relevos NO arranca sin Telegram, y es correcto: sin Telegram no hay
    relevos que cerrar. Los recordatorios no dependen de Telegram, así que colgarlos de esa
    misma tarea los dejaría apagados en cualquier despliegue sin grupo de doctores -- sin un
    solo error en el log, que es exactamente como el barrido de relevos estuvo días caído.

    La versión anterior de esta prueba afirmaba `"telegram_bot_token" not in fuente` sobre el
    CÓDIGO del arranque. Probaba una propiedad legítima por un medio que bloqueaba el arreglo
    correcto: el escalamiento del envío fallido tiene que mirar si hay Telegram, y con aquella
    aserción cualquier mención lo rompía. Aquí se comprueba la propiedad de verdad -- que la
    tarea arranca con el token vacío -- en vez de la forma del texto.
    """
    import asyncio

    from maxicare_daniela import runtime

    async def escenario():
        monkeypatch.setattr(
            runtime,
            "config",
            replace(
                runtime.config,
                telegram_bot_token="",
                telegram_chat_doctores="",
                database_url="postgresql://no-se-usa",
            ),
        )
        monkeypatch.setattr(runtime, "_tarea_de_recordatorios", None)

        await runtime._arrancar_despacho_de_recordatorios()

        tarea = runtime._tarea_de_recordatorios
        assert tarea is not None, "sin Telegram, el despacho de recordatorios NO arrancó"
        assert not tarea.done()
        # Lo primero que hace el bucle es dormir el ciclo entero: cancelarlo aquí no
        # interrumpe ningún despacho a medias y evita dejar una tarea viva entre pruebas.
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass

    asyncio.run(escenario())


def test_un_envio_fallido_se_escala_a_los_doctores(monkeypatch):
    """La spec lo pide en §8 y en §11, y con motivo: es la mitad del empate que justificó
    marcar ANTES de enviar. «Un recordatorio perdido es un paciente que quizá no llega, **y la
    clínica se entera**». Escribir la columna `fallo` no es enterarse: nadie la consulta."""
    import asyncio

    from maxicare_daniela import runtime

    avisos: list[str] = []

    class _TelegramFalso:
        async def enviar_mensaje(self, texto, *, tema_id=None, **k):
            avisos.append(texto)
            return 1

    monkeypatch.setattr(
        runtime,
        "config",
        replace(
            runtime.config, telegram_bot_token="token-falso", telegram_chat_doctores="-100123"
        ),
    )
    monkeypatch.setattr(runtime, "_telegram", _TelegramFalso())

    asyncio.run(runtime._avisar_de_recordatorios_fallidos(2))

    assert len(avisos) == 1
    assert "2 recordatorio(s) no salieron" in avisos[0]


def test_el_aviso_de_fallidos_no_tumba_el_ciclo_si_telegram_falla(monkeypatch):
    """Un fallo avisando de un fallo no puede llevarse por delante el ciclo siguiente: el
    despacho de mañana vale más que el aviso de hoy."""
    import asyncio

    from maxicare_daniela import runtime

    class _TelegramRoto:
        async def enviar_mensaje(self, texto, *, tema_id=None, **k):
            raise RuntimeError("Telegram no responde")

    monkeypatch.setattr(
        runtime,
        "config",
        replace(
            runtime.config, telegram_bot_token="token-falso", telegram_chat_doctores="-100123"
        ),
    )
    monkeypatch.setattr(runtime, "_telegram", _TelegramRoto())

    asyncio.run(runtime._avisar_de_recordatorios_fallidos(1))  # no propaga

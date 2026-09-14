"""El despachador de recordatorios, sin base y sin red.

Las siete guardas y las bandas horarias viven aquí porque son decisiones del código, no de
la base: se prueban en milisegundos y corren siempre. Lo que toca Neon está en
`test_seguimientos_neon.py`, marcado `neon`.

Ningún momento sale del reloj de la máquina: todos entran como parámetro.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from maxicare_daniela import seguimientos as s
from maxicare_daniela.calendario import Jornada
from maxicare_daniela.herramientas import ZONA_BOGOTA

JORNADA = Jornada()  # 8-17 entre semana, 15 el sábado, domingo cerrado


class _ConexionFalsa:
    """`persistencia.conectar` se usa como context manager. Esto es lo mínimo para doblarlo."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


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
        intentos=0,
        telefono="573001112233",
        nombre_completo="Ana Gómez",
        cita_inicio=momento(17, 9),
        cita_estado="confirmada",
        tomada_por=None,
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
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
        ya_salio_a_ese_numero=True,
    )
    assert d.accion == "aplazar"
    assert d.motivo == "agrupado"


def test_un_recordatorio_limpio_sale():
    d = s.decidir(
        fila(),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=momento(14, 9),
    )
    assert d.accion == "enviar"


def test_un_seguimiento_sin_cita_se_salta_las_tres_primeras_guardas():
    # «Llámenme el lunes»: no cuelga de ninguna cita, así que G1, G2 y G3 no aplican.
    d = s.decidir(
        fila(cita_id=None, cita_inicio=None, cita_estado=None, tipo="reactivacion"),
        ahora=momento(16, 18),
        jornada=JORNADA,
        ultimo_mensaje=None,
    )
    assert d.accion == "enviar"


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
            fila(cita_id=None, cita_inicio=None, cita_estado=None, tipo="reactivacion")
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


def test_el_despachador_arranca_aunque_no_haya_telegram():
    """El barrido de relevos NO arranca sin Telegram, y es correcto: sin Telegram no hay
    relevos que cerrar. Los recordatorios no dependen de Telegram, así que colgarlos de esa
    misma tarea los dejaría apagados en cualquier despliegue sin grupo de doctores -- sin un
    solo error en el log, que es exactamente como el barrido de relevos estuvo días caído."""
    import inspect

    from maxicare_daniela import runtime

    fuente = inspect.getsource(runtime._arrancar_despacho_de_recordatorios)
    assert "telegram_bot_token" not in fuente

"""El aviso por WhatsApp a los doctores de cada cita nueva.

Lo que se prueba aquí es lo que se rompe en silencio: el ORDEN de los cinco huecos de la
plantilla --que ninguna prueba de integración puede ver, porque Meta acepta cualquier orden--
y las tres garantías de que este aviso no puede costarle una cita a nadie.

No abre conexiones ni habla con Meta: `avisar` recibe el canal, así que un doble de cuatro
líneas basta para comprobar lo mismo que comprobaría un envío real. El único eslabón que estas
pruebas NO ven es que la plantilla exista en el Business Manager con esos cinco huecos en ese
orden; eso lo comprueba `scripts/probar_plantilla.py`, que manda una de verdad.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pytest

from maxicare_daniela import aviso_citas
from maxicare_daniela import herramientas as h
from maxicare_daniela import persistencia
from maxicare_daniela.calendario import CalendarioDoble
from maxicare_daniela.contratos import ContextoDaniela, SolicitudCancelacion, SolicitudCita

#: Clavados, como `tests/test_herramientas.py::AHORA` y por lo mismo: `ContextoDaniela.ahora`
#: cae al reloj de verdad si nadie se lo da, y una fecha que envejece ya ha puesto esta suite
#: en rojo sola tres veces. La cita es el LUNES 21 de septiembre de 2026 a las 10 de la
#: mañana, y el día de la semana es parte de lo que se comprueba: un `weekday()` desplazado
#: en uno manda al doctor a esperar a alguien el día anterior.
AHORA = datetime(2026, 9, 18, 8, 0, tzinfo=h.ZONA_BOGOTA)
INICIO = datetime(2026, 9, 21, 10, 0, tzinfo=h.ZONA_BOGOTA)

DOCTORES = ("573106492282", "573185790008")


def cita(**cambios) -> aviso_citas.CitaNueva:
    base = dict(
        nombre_paciente="Ana Gómez",
        telefono_paciente="573001112233",
        tratamiento="limpieza",
        inicio=INICIO,
    )
    base.update(cambios)
    return aviso_citas.CitaNueva(**base)


class WhatsAppFalso:
    """Guarda lo que se le pidió mandar. `falla_en` son los números a los que revienta."""

    def __init__(self, falla_en: tuple[str, ...] = ()) -> None:
        self.enviados: list[dict] = []
        self._falla_en = falla_en

    async def enviar_plantilla(self, telefono, *, plantilla, parametros, idioma="es"):
        if telefono in self._falla_en:
            raise RuntimeError(f"Meta rechazó el envío a {telefono}")
        self.enviados.append(
            {
                "telefono": telefono,
                "plantilla": plantilla,
                "parametros": parametros,
                "idioma": idioma,
            }
        )
        return "wamid.X"


def enviar(whatsapp, *, plantilla="cita_nueva_doctores", idioma="es", **cambios) -> int:
    return asyncio.run(
        aviso_citas.avisar(
            cita(**cambios),
            whatsapp=whatsapp,
            doctores=DOCTORES,
            plantilla=plantilla,
            idioma=idioma,
        )
    )


# ==========================================================================================
# Los cinco huecos
# ==========================================================================================


def test_los_cinco_parametros_van_en_el_orden_que_meta_aprobo():
    """La única prueba que puede salvar el aviso de ser ilegible.

    Meta no valida qué va en cada hueco: manda lo que se le dé. Con el orden cambiado, el
    doctor lee un teléfono donde esperaba la hora y el mensaje sigue saliendo «bien».
    """
    valores = aviso_citas.parametros_de(cita())

    assert valores == [
        "Ana Gómez",  # {{1}} nombre
        "573001112233",  # {{2}} teléfono
        "limpieza",  # {{3}} tratamiento
        "lunes 21 de septiembre",  # {{4}} fecha
        "10:00 a. m.",  # {{5}} hora
    ]


def test_la_fecha_y_la_hora_son_huecos_DISTINTOS():
    """Un formateador que devolviera las dos juntas --como `herramientas._formatear_hora`--
    dejaría la plantilla diciendo «el lunes 21 a las 10:00 a. m. a las 10:00 a. m.»."""
    _, _, _, fecha, hora = aviso_citas.parametros_de(cita())

    assert ":" not in fecha
    assert "de septiembre" not in hora


@pytest.mark.parametrize(
    ("hora", "esperado"),
    [
        (0, "12:00 a. m."),
        (9, "9:00 a. m."),
        (11, "11:00 a. m."),
        (12, "12:00 p. m."),
        (13, "1:00 p. m."),
        (17, "5:00 p. m."),
    ],
)
def test_la_hora_se_escribe_como_la_dice_la_clinica(hora, esperado):
    """El mediodía y la medianoche son los dos que un `% 12` mal escrito convierte en «0:00»."""
    valores = aviso_citas.parametros_de(cita(inicio=INICIO.replace(hour=hora)))

    assert valores[4] == esperado


def test_la_hora_se_convierte_a_bogota_antes_de_escribirla():
    """La lección que ya pagó el recordatorio: `citas.inicio` es `TIMESTAMPTZ` y psycopg lo
    devuelve normalizado a UTC. Un `%H:%M` sobre eso mandaba al doctor cinco horas tarde."""
    from datetime import timezone

    valores = aviso_citas.parametros_de(cita(inicio=INICIO.astimezone(timezone.utc)))

    assert valores[3] == "lunes 21 de septiembre"
    assert valores[4] == "10:00 a. m."


def test_un_hueco_vacio_sale_como_PENDIENTE_y_nunca_vacio():
    """Meta rechaza el mensaje ENTERO si un parámetro va vacío: un nombre en blanco no
    costaría un renglón, costaría el aviso completo. Y al doctor «PENDIENTE» le dice la
    verdad, que es justo lo que la regla dura 3 pide del código."""
    valores = aviso_citas.parametros_de(cita(nombre_paciente="   ", tratamiento=""))

    assert valores[0] == "PENDIENTE"
    assert valores[2] == "PENDIENTE"
    assert "" not in valores


# ==========================================================================================
# A quién sale, y qué pasa cuando algo falla
# ==========================================================================================


def test_el_aviso_sale_a_LOS_DOS_doctores():
    wa = WhatsAppFalso()

    enviados = enviar(wa)

    assert enviados == 2
    assert [e["telefono"] for e in wa.enviados] == list(DOCTORES)
    assert {e["plantilla"] for e in wa.enviados} == {"cita_nueva_doctores"}


def test_un_fallo_con_el_primer_doctor_no_deja_sin_avisar_al_segundo():
    """El `try` va DENTRO del bucle. Fuera, un número mal escrito en el `.env` se llevaría por
    delante el aviso del otro doctor, que es el que sí habría llegado."""
    wa = WhatsAppFalso(falla_en=(DOCTORES[0],))

    enviados = enviar(wa)

    assert enviados == 1
    assert [e["telefono"] for e in wa.enviados] == [DOCTORES[1]]


def test_si_meta_esta_caida_el_aviso_no_propaga():
    """Cuando esto corre, la cita ya existe. Propagar aquí sería cambiar una cita buena por un
    aviso, que es el intercambio que este proyecto prohíbe."""
    wa = WhatsAppFalso(falla_en=DOCTORES)

    assert enviar(wa) == 0


def test_el_idioma_viaja_tal_cual_hasta_meta():
    """Una plantilla registrada como `es_CO` no acepta `es`: Meta rechaza el envío entero con
    el error 132001. El valor sale de `configuracion`, no de un default de esta función."""
    wa = WhatsAppFalso()

    enviar(wa, idioma="es_CO")

    assert {e["idioma"] for e in wa.enviados} == {"es_CO"}


# ==========================================================================================
# Con la plantilla apagada
# ==========================================================================================


def test_sin_plantilla_no_se_manda_nada_pero_queda_el_log(caplog):
    """Es lo que permite desplegar esto hoy, con la plantilla todavía sin aprobar por Meta:
    la decisión se toma y se registra, y no sale un solo mensaje."""
    wa = WhatsAppFalso()

    with caplog.at_level(logging.INFO, logger="maxicare_daniela.aviso_citas"):
        enviados = enviar(wa, plantilla="")

    assert enviados == 0
    assert wa.enviados == []
    (registro,) = [r for r in caplog.records if "DECIDIDO" in r.getMessage()]
    texto = registro.getMessage()
    # A quién se le habría mandado, y con qué. Sin esto, «no se mandó» y «no se decidió»
    # serían indistinguibles en el log, que es justo lo que este modo existe para separar.
    assert DOCTORES[0] in texto and DOCTORES[1] in texto
    assert "lunes 21 de septiembre" in texto and "10:00 a. m." in texto


def test_sin_canal_tampoco_se_manda_nada():
    """`whatsapp=None` es lo que devuelve `ajustes_del_entorno` cuando faltan las credenciales
    de Meta. No es un fallo: es el mismo modo «decide y no manda»."""
    assert asyncio.run(
        aviso_citas.avisar(
            cita(), whatsapp=None, doctores=DOCTORES, plantilla="cita_nueva_doctores"
        )
    ) == 0


# ==========================================================================================
# De dónde salen los ajustes
# ==========================================================================================


def test_los_ajustes_salen_de_las_variables_que_documenta_el_env_ejemplo(monkeypatch):
    """Pasan por `Config.desde_entorno()` y no por un `os.environ.get` propio: dos sitios
    leyendo la misma variable son dos sitios que un día divergen."""
    monkeypatch.setenv("MAXICARE_DATABASE_URL", "postgresql://no-se-usa")
    monkeypatch.setenv("MAXICARE_WHATSAPP_TOKEN", "token")
    monkeypatch.setenv("MAXICARE_WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("MAXICARE_PLANTILLA_CITA_NUEVA", "cita_nueva_doctores")
    monkeypatch.setenv("MAXICARE_PLANTILLA_CITA_NUEVA_IDIOMA", "es_CO")
    monkeypatch.setenv("MAXICARE_WHATSAPP_DOCTORES", "573000000001,573000000002")

    ajustes = aviso_citas.ajustes_del_entorno()

    assert ajustes.plantilla == "cita_nueva_doctores"
    assert ajustes.idioma == "es_CO"
    assert ajustes.doctores == ("573000000001", "573000000002")
    assert ajustes.whatsapp is not None


def test_sin_credenciales_de_meta_los_ajustes_dejan_el_canal_apagado(monkeypatch):
    """Y no lanzan. Esto corre dentro de una tarea de fondo: una excepción aquí no la vería
    nadie, y el lado inocuo de equivocarse es no mandar."""
    monkeypatch.delenv("MAXICARE_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAXICARE_WHATSAPP_TOKEN", raising=False)
    monkeypatch.delenv("MAXICARE_PLANTILLA_CITA_NUEVA", raising=False)

    ajustes = aviso_citas.ajustes_del_entorno()

    assert ajustes.whatsapp is None
    assert ajustes.plantilla == ""
    # Los números no se pierden aunque no haya nada configurado: viven en `config.py` a
    # propósito, para que un despliegue que olvide una variable no deje a los doctores fuera.
    assert ajustes.doctores


def test_santiago_recibe_el_aviso_con_el_env_vacio(monkeypatch):
    """El caso real: `MAXICARE_WHATSAPP_DOCTORES` está vacía en local Y en el VPS, así que
    quien manda de verdad es la constante de `config.py`.

    No basta con mirar la constante. `desde_entorno` la enlaza con un `or`, y un `or` sobre
    una variable mal leída --una cadena vacía que llegara como `("",)`, por ejemplo-- daría
    una lista de destinatarios que NO es esta sin que la constante hubiera cambiado. Lo que
    se comprueba es lo que llega al final del camino, que es lo que Meta va a recibir.
    """
    monkeypatch.delenv("MAXICARE_WHATSAPP_DOCTORES", raising=False)
    monkeypatch.setenv("MAXICARE_DATABASE_URL", "postgresql://no-se-usa")

    ajustes = aviso_citas.ajustes_del_entorno()

    assert "573132103985" in ajustes.doctores
    # Y los dos que ya estaban siguen estando: añadir a alguien no puede ser quitarle el
    # aviso a otro, que es el fallo silencioso de escribir una tupla nueva en vez de crecerla.
    assert "573106492282" in ajustes.doctores
    assert "573185790008" in ajustes.doctores


# ==========================================================================================
# El viaje entero, desde `crear_cita`
# ==========================================================================================


def contexto(**cambios) -> ContextoDaniela:
    base = dict(
        id_conversacion="conv-1",
        telefono_completo="573001112233",
        database_url="postgresql://no-se-usa",
        calendario=CalendarioDoble(),
        ahora=AHORA,
    )
    base.update(cambios)
    return ContextoDaniela(**base)


class BaseFalsa:
    def commit(self) -> None:
        pass


def crear_cita(monkeypatch, ctx) -> str:
    """Corre `_crear_cita` entera contra los dobles y ESPERA a la tarea del aviso.

    La espera es la mitad que importa: el aviso sale por `asyncio.create_task`, así que sin
    ella el bucle se cierra antes de que la tarea llegue a correr y la prueba pasaría en verde
    tanto si el aviso sale como si no.
    """

    async def base_falsa(_ctx, trabajo):
        return trabajo(BaseFalsa())

    monkeypatch.setattr(h, "_con_base", base_falsa)
    monkeypatch.setattr(persistencia, "bloques_ocupados", lambda conn, desde, hasta: {})
    monkeypatch.setattr(persistencia, "tomar_cupo", lambda conn, **kw: (55, 1))
    monkeypatch.setattr(persistencia, "cita_viva_de_reserva", lambda conn, reserva_id: None)
    monkeypatch.setattr(persistencia, "asegurar_paciente", lambda conn, **kw: 7)
    monkeypatch.setattr(persistencia, "registrar_cita", lambda conn, **kw: "cita-nueva")
    monkeypatch.setattr(persistencia, "insertar_seguimiento", lambda conn, **kw: True)
    # La trajo la reactivación de leads: `crear_cita` pone a cero el contador de
    # seguimientos ignorados, porque quien ignoró dos veces y al final vino demostró lo
    # contrario de lo que ese contador supone. Se dobla como todo lo demás de `persistencia`
    # en este ayudante --y NO ampliando `BaseFalsa` con un `cursor` de mentira-- porque el
    # doble de una conexión que acepta cualquier SQL deja pasar la consulta rota: es
    # exactamente el «verde por el motivo equivocado» de `.claude/rules/pruebas.md`.
    monkeypatch.setattr(
        persistencia, "reiniciar_seguimientos_fallidos", lambda conn, telefono, **kw: None
    )

    async def _todo() -> str:
        texto = await h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Ana Gómez",
                inicio=INICIO,
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
        for tarea in aviso_citas.tareas_en_vuelo():
            await tarea
        return texto

    return asyncio.run(_todo())


def test_agendar_avisa_a_los_doctores_con_los_datos_de_la_cita(monkeypatch):
    """El teléfono sale del CONTEXTO y no de la solicitud: `SolicitudCita` no tiene ese campo
    justamente para que el modelo no pueda escribir otro número --la hija agendando por su
    madre--, y el doctor que marque ese número le marca a quien escribió."""
    wa = WhatsAppFalso()
    monkeypatch.setattr(
        aviso_citas,
        "ajustes_del_entorno",
        lambda: aviso_citas.Ajustes(
            whatsapp=wa, doctores=DOCTORES, plantilla="cita_nueva_doctores", idioma="es"
        ),
    )

    texto = crear_cita(monkeypatch, contexto(telefono_completo="573009998877"))

    assert "Cita confirmada" in texto
    assert len(wa.enviados) == 2
    assert wa.enviados[0]["parametros"] == [
        "Ana Gómez",
        "573009998877",
        "limpieza",
        "lunes 21 de septiembre",
        "10:00 a. m.",
    ]


def test_si_el_aviso_revienta_la_cita_se_crea_igual(monkeypatch):
    """La frontera del proyecto entero: se pierde un aviso, nunca una cita.

    Revienta lo PRIMERO que hace el aviso --leer los ajustes--, que es el único fallo que
    ocurriría dentro del turno y no dentro de la tarea de fondo. Si `_crear_cita` no lo tapara,
    el paciente se quedaría sin la confirmación de una cita que ya está en Neon y en Calendar.
    """

    def revienta():
        raise RuntimeError("el aviso no se pudo ni preparar")

    monkeypatch.setattr(aviso_citas, "ajustes_del_entorno", revienta)

    texto = crear_cita(monkeypatch, contexto())

    assert "Cita confirmada" in texto
    assert "cita-nueva" in texto


# ==========================================================================================
# El viaje entero, desde `cancelar_cita` y `reprogramar_cita`
# ==========================================================================================
#
# El aviso cuelga de las TOOLS y no del botón del recordatorio. Al doctor le cambia la agenda
# igual si el paciente lo pidió escribiendo que si pulsó «No puedo asistir», y colgarlo del
# botón dejaría mudo el camino por el que hoy pasa casi todo.

CITA_EN_LA_BASE = {
    "id": "cita-1",
    "reserva_id": 5,
    "conversacion_id": "conv-1",
    "paciente_id": None,
    "nombre_completo": "Ana Gómez",
    "telefono": "573001112233",
    "tratamiento": "limpieza",
    "inicio": INICIO,
    "duracion_minutos": 60,
    "evento_calendar_id": None,
    "estado": "confirmada",
}


def _movimientos_espiados(monkeypatch) -> list:
    """Se espía `avisar_movimiento_en_segundo_plano` y no el canal: lo que se prueba aquí es
    que la tool DECIDE avisar y con qué datos. Que el envío salga ya lo cubren las pruebas de
    arriba, sin montar media tool para repetirlo."""
    vistos: list = []
    monkeypatch.setattr(
        aviso_citas, "avisar_movimiento_en_segundo_plano", lambda mov: vistos.append(mov)
    )
    return vistos


def _ctx_de_la_cita():
    return contexto(identidad_verificada=True, id_paciente=None)


def test_cancelar_avisa_a_los_doctores_de_que_no_vendra(monkeypatch):
    """La clínica recupera la hora, y alguien tiene que enterarse de que quedó libre."""
    movimientos = _movimientos_espiados(monkeypatch)
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        return CITA_EN_LA_BASE if len(pasos) == 1 else None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(
        h._cancelar_cita(
            _ctx_de_la_cita(),
            SolicitudCancelacion(id_cita="cita-1", motivo=None, clave_idempotencia="cita-1"),
        )
    )

    (mov,) = movimientos
    assert mov.asunto == aviso_citas.ASUNTO_NO_ASISTIRA
    assert mov.nombre_paciente == "Ana Gómez"
    assert mov.telefono_paciente == "573001112233"
    assert mov.tratamiento == "limpieza"
    assert mov.cuando == "lunes 21 de septiembre a las 10:00 a. m."


def test_una_cita_ya_cancelada_no_vuelve_a_avisar(monkeypatch):
    """`_cancelar_cita` sale antes por su propia guarda. Sin esto, dos intentos del modelo
    sobre la misma cita despertarían al doctor dos veces por lo mismo."""
    movimientos = _movimientos_espiados(monkeypatch)

    async def base_falsa(_ctx, _trabajo):
        return {**CITA_EN_LA_BASE, "estado": "cancelada"}

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(
        h._cancelar_cita(
            _ctx_de_la_cita(),
            SolicitudCancelacion(id_cita="cita-1", motivo=None, clave_idempotencia="cita-1"),
        )
    )

    assert movimientos == []


def test_reprogramar_avisa_con_LAS_DOS_horas(monkeypatch):
    """Un cambio de agenda que no diga de dónde a dónde obliga al doctor a ir a buscarlo."""
    movimientos = _movimientos_espiados(monkeypatch)
    destino = INICIO + timedelta(days=1, hours=5)  # martes 22, 3:00 p. m.
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", CITA_EN_LA_BASE, (6, 1), [])
        return None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    asyncio.run(h._reprogramar_cita(_ctx_de_la_cita(), "cita-1", destino.isoformat()))

    (mov,) = movimientos
    assert mov.asunto == aviso_citas.ASUNTO_CAMBIADA
    assert mov.cuando == (
        "antes: lunes 21 de septiembre, 10:00 a. m. · "
        "ahora: martes 22 de septiembre, 3:00 p. m."
    )


def test_una_reprogramacion_que_revienta_no_avisa_de_nada(monkeypatch):
    """Lo que MaxiCare pidió por escrito: si el cambio queda a medias, no se comunica.

    Se cumple por construcción y no por un `if`: el aviso está DESPUÉS de `aplicar`, así que
    una excepción ahí no llega nunca a la línea que avisa. Esta prueba es lo que impide que
    alguien lo suba por encima «para no repetir código».
    """
    movimientos = _movimientos_espiados(monkeypatch)
    destino = INICIO + timedelta(days=1)
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", CITA_EN_LA_BASE, (6, 1), [])
        raise RuntimeError("Neon se cayó a mitad del movimiento")

    monkeypatch.setattr(h, "_con_base", base_falsa)

    with pytest.raises(RuntimeError):
        asyncio.run(h._reprogramar_cita(_ctx_de_la_cita(), "cita-1", destino.isoformat()))

    assert movimientos == []


def test_reprogramar_a_la_MISMA_hora_no_avisa_de_nada(monkeypatch):
    """Sin esto, repetir la misma reprogramación manda un segundo «Cambió su cita» idéntico.

    `_cancelar_cita` ya tiene su guarda (`estado == 'cancelada'` sale antes del aviso) y esta
    no la tenía. La clave de idempotencia de `tomar_cupo` --`ctx.clave("reprogramar", id_cita,
    destino)`-- NO lleva componente de turno, así que repetir el mismo movimiento devuelve la
    misma reserva, `aplicar` termina sin excepción, y el aviso salía otra vez.

    Un aviso duplicado no engaña a nadie --dice la verdad-- pero es ruido en el canal por el
    que la clínica se entera de lo que cambia, y ese canal tiene que poder leerse.
    """
    movimientos = _movimientos_espiados(monkeypatch)
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", CITA_EN_LA_BASE, (6, 1), [])
        return None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    # El destino ES la hora que ya tiene la cita.
    asyncio.run(h._reprogramar_cita(_ctx_de_la_cita(), "cita-1", INICIO.isoformat()))

    assert movimientos == []


def _aviso_que_revienta(monkeypatch):
    def revienta(_mov):
        raise RuntimeError("el aviso no se pudo ni preparar")

    monkeypatch.setattr(aviso_citas, "avisar_movimiento_en_segundo_plano", revienta)


def test_si_el_aviso_revienta_la_cancelacion_se_confirma_igual(monkeypatch):
    """La misma frontera que `test_si_el_aviso_revienta_la_cita_se_crea_igual`, y aquí duele
    más: cuando esto corre, la cita YA está cancelada y el cupo YA está libre. Sin el `try`,
    el paciente recibiría el mensaje de emergencia y se quedaría creyendo que su cita sigue
    en pie, camino de una clínica donde su hora ya es de otro."""
    _aviso_que_revienta(monkeypatch)
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        return CITA_EN_LA_BASE if len(pasos) == 1 else None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(
        h._cancelar_cita(
            _ctx_de_la_cita(),
            SolicitudCancelacion(id_cita="cita-1", motivo=None, clave_idempotencia="cita-1"),
        )
    )

    assert "cancelada" in texto


def test_si_el_aviso_revienta_la_reprogramacion_se_confirma_igual(monkeypatch):
    """Y aquí el paciente acabaría yendo a la hora vieja, que es la cita movida sin avisar."""
    _aviso_que_revienta(monkeypatch)
    destino = INICIO + timedelta(days=1)
    pasos: list[int] = []

    async def base_falsa(_ctx, trabajo):
        pasos.append(1)
        if len(pasos) == 1:
            return ("ok", CITA_EN_LA_BASE, (6, 1), [])
        return None

    monkeypatch.setattr(h, "_con_base", base_falsa)

    texto = asyncio.run(h._reprogramar_cita(_ctx_de_la_cita(), "cita-1", destino.isoformat()))

    assert "reprogramada" in texto


def test_la_tarea_del_aviso_tiene_una_referencia_fuerte(monkeypatch):
    """`asyncio` solo guarda las tareas en un `WeakSet`: una que nadie sostenga se la puede
    llevar el recolector a medias, y eso sería un aviso que no sale sin un error en el log.
    Es el mismo `set` que `ingesta._lectores_vivos`, por el mismo motivo."""
    vistas: list[int] = []

    async def _dentro() -> None:
        monkeypatch.setattr(
            aviso_citas,
            "ajustes_del_entorno",
            lambda: aviso_citas.Ajustes(
                whatsapp=WhatsAppFalso(), doctores=DOCTORES, plantilla="x", idioma="es"
            ),
        )
        tarea = aviso_citas.avisar_en_segundo_plano(cita())
        vistas.append(len(aviso_citas.tareas_en_vuelo()))
        await tarea

    asyncio.run(_dentro())

    assert vistas == [1], "la tarea no quedó sostenida mientras corría"
    # Y el `add_done_callback` la saca al terminar, así que el `set` no crece sin límite.
    assert aviso_citas.tareas_en_vuelo() == ()


# ==========================================================================================
# El movimiento de agenda: confirmó, no vendrá o cambió de hora
# ==========================================================================================
#
# UNA sola plantilla para los tres casos, con el asunto en el primer hueco. Lo que eso compra
# es que la redacción viva en Python: cambiarla es un commit y no un trámite de 24 h con
# Meta. Lo que cuesta es que el ORDEN de los huecos vuelve a ser lo único que puede dejar el
# aviso ilegible sin romper ninguna prueba de integración, así que se comprueba igual que el
# de `CitaNueva`.

OTRO_INICIO = datetime(2026, 9, 22, 15, 0, tzinfo=h.ZONA_BOGOTA)


def movimiento(**cambios) -> "aviso_citas.MovimientoDeAgenda":
    base = dict(
        asunto=aviso_citas.ASUNTO_CONFIRMADA,
        nombre_paciente="Ana Gómez",
        telefono_paciente="573001112233",
        tratamiento="limpieza",
        cuando=aviso_citas.cuando_una_cita(INICIO),
    )
    base.update(cambios)
    return aviso_citas.MovimientoDeAgenda(**base)


def enviar_movimiento(whatsapp, *, plantilla="movimiento_agenda", idioma="es", **cambios) -> int:
    return asyncio.run(
        aviso_citas.avisar_movimiento(
            movimiento(**cambios),
            whatsapp=whatsapp,
            doctores=DOCTORES,
            plantilla=plantilla,
            idioma=idioma,
        )
    )


def test_los_cinco_huecos_del_movimiento_van_en_el_orden_que_meta_aprobo():
    """Con el orden cambiado, el doctor lee un teléfono donde esperaba la hora."""
    assert aviso_citas.parametros_de_movimiento(movimiento()) == [
        "Confirmó su cita",
        "Ana Gómez",
        "573001112233",
        "limpieza",
        "lunes 21 de septiembre a las 10:00 a. m.",
    ]


def test_los_tres_asuntos_dicen_lo_que_paso_sin_leer_el_cuerpo():
    """Son texto para un doctor, no claves: se leen en la primera línea del aviso."""
    assert aviso_citas.ASUNTO_CONFIRMADA == "Confirmó su cita"
    assert aviso_citas.ASUNTO_NO_ASISTIRA == "No podrá asistir"
    assert aviso_citas.ASUNTO_CAMBIADA == "Cambió su cita"


def test_un_cambio_lleva_las_DOS_horas_y_en_un_solo_renglon():
    """Confirmar un cambio exige decir de dónde a dónde, y Meta rechaza el salto de línea."""
    cuando = aviso_citas.cuando_un_cambio(INICIO, OTRO_INICIO)

    assert "lunes 21 de septiembre" in cuando
    assert "martes 22 de septiembre" in cuando
    assert "10:00 a. m." in cuando and "3:00 p. m." in cuando
    assert "\n" not in cuando


def test_ningun_hueco_lleva_saltos_de_linea():
    """Un hueco con un salto de línea no cuesta un renglón: cuesta el mensaje entero.

    El nombre sale de `pacientes`, que lo escribió el modelo desde lo que tecleó el paciente:
    un «Ana\\nGómez» es raro, pero no es imposible, y perdería el aviso entero.
    """
    huecos = aviso_citas.parametros_de_movimiento(
        movimiento(nombre_paciente="Ana\nGómez", tratamiento="limpieza\r\nprofunda")
    )

    for hueco in huecos:
        assert "\n" not in hueco and "\r" not in hueco
    assert huecos[1] == "Ana Gómez"


def test_un_hueco_vacio_del_movimiento_sale_como_PENDIENTE():
    """Meta rechaza el mensaje ENTERO si un parámetro va vacío, no solo ese renglón."""
    huecos = aviso_citas.parametros_de_movimiento(movimiento(nombre_paciente="   "))

    assert huecos[1] == "PENDIENTE"


def test_el_movimiento_sale_a_LOS_DOS_doctores():
    whatsapp = WhatsAppFalso()

    assert enviar_movimiento(whatsapp) == 2
    assert [e["telefono"] for e in whatsapp.enviados] == list(DOCTORES)
    assert {e["plantilla"] for e in whatsapp.enviados} == {"movimiento_agenda"}


def test_un_destinatario_que_falla_no_se_lleva_a_los_otros():
    """El `try` va DENTRO del bucle: un número mal escrito en el .env no calla a los demás."""
    whatsapp = WhatsAppFalso(falla_en=(DOCTORES[0],))

    assert enviar_movimiento(whatsapp) == 1
    assert [e["telefono"] for e in whatsapp.enviados] == [DOCTORES[1]]


def test_sin_plantilla_el_movimiento_se_decide_y_no_se_manda(caplog):
    """Es lo que permite desplegar hoy, con la plantilla todavía sin aprobar."""
    whatsapp = WhatsAppFalso()

    with caplog.at_level(logging.INFO, logger="maxicare_daniela.aviso_citas"):
        assert enviar_movimiento(whatsapp, plantilla="") == 0

    assert whatsapp.enviados == []
    assert "Confirmó su cita" in caplog.text


def test_el_movimiento_no_propaga_si_meta_esta_caida():
    """La cita ya está movida cuando esto corre: un fallo de Meta no puede deshacerla."""
    whatsapp = WhatsAppFalso(falla_en=DOCTORES)

    assert enviar_movimiento(whatsapp) == 0


def test_la_tarea_del_movimiento_tiene_una_referencia_fuerte(monkeypatch):
    """`asyncio` solo guarda las tareas en un `WeakSet`: sin esto, el recolector se la lleva."""
    vistas = []

    async def _dentro():
        monkeypatch.setattr(
            aviso_citas,
            "ajustes_del_movimiento",
            lambda: aviso_citas.Ajustes(
                whatsapp=WhatsAppFalso(), doctores=DOCTORES, plantilla="x", idioma="es"
            ),
        )
        tarea = aviso_citas.avisar_movimiento_en_segundo_plano(movimiento())
        vistas.append(len(aviso_citas.tareas_en_vuelo()))
        await tarea

    asyncio.run(_dentro())

    assert vistas == [1], "la tarea no quedó sostenida mientras corría"
    assert aviso_citas.tareas_en_vuelo() == ()


def test_los_ajustes_del_movimiento_salen_de_su_propia_variable(monkeypatch):
    """No comparte plantilla con el aviso de cita nueva: son dos aprobaciones distintas."""
    monkeypatch.setenv("MAXICARE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("MAXICARE_WHATSAPP_TOKEN", "t")
    monkeypatch.setenv("MAXICARE_WHATSAPP_PHONE_NUMBER_ID", "1")
    monkeypatch.setenv("MAXICARE_PLANTILLA_MOVIMIENTO_AGENDA", "movimiento_agenda")
    monkeypatch.setenv("MAXICARE_PLANTILLA_CITA_NUEVA", "cita_nueva_doctores")

    ajustes = aviso_citas.ajustes_del_movimiento()

    assert ajustes.plantilla == "movimiento_agenda"
    assert ajustes.idioma == "es"

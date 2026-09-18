"""Corre el barrido de reactivación de leads y su despachador contra Neon, y cuenta en claro
qué haría si mandara -- sin mandar nada.

    uv run python scripts/probar_reactivacion.py

Es el entregable de la tarea 9 (fase de reactivación de leads): `barrido.encolar` decide
QUIÉN entra en la cola y `seguimientos.despachar` decide QUÉ HACER con cada fila, y las once
reglas anti-reporte de la spec
(`docs/superpowers/specs/2026-09-17-reactivacion-de-leads-design.md`, sección 3) tienen que
sobrevivir a las dos. No gasta un solo token -- el modelo no interviene en ningún punto de
este camino -- y **no manda nada**: las tres plantillas de reactivación no están aprobadas
por Meta todavía, así que corre con `plantillas={}` en todo el camino y el WhatsApp doblado
revienta si alguien lo usa de verdad, en vez de devolver un éxito silencioso.

Escribe en el esquema `pruebas_reactivacion_script` y lo borra al terminar, pase lo que pase.
Va contra la conexión DIRECTA de Neon, sin el `-pooler.` del host, por el mismo motivo que
`probar_recordatorios.py` y `probar_tools.py`: el pooler rechaza `options` como parámetro de
arranque, y aquí hace falta fijar `search_path`.

Diez de las once reglas quedan comprobadas aquí. La 11 (que el barrido se apague solo si la
calidad del número baja) se marca `POR VERIFICAR`: el mecanismo se prueba en frío contra
`barrido.se_puede_encolar`, pero la consulta REAL a `graph.facebook.com` la verifica
`scripts/probar_plantilla.py --estado` o una comprobación manual -- este script no toca la
red ni gasta nada.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    # `barrido.encolar` avisa por `logging` (nunca por `print`) cuando la calidad frena un
    # ciclo -- las reglas 10 y 11 lo disparan a propósito -- y el handler por omisión de
    # `logging` escribe a `sys.stderr` con el encoding de la consola, no con el de `stdout`.
    # Sin esto, el acento de "número" sale mojibake en cp1252 aunque el resto del script
    # esté limpio.
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os  # noqa: E402

from maxicare_daniela import barrido, persistencia, seguimientos  # noqa: E402
from maxicare_daniela.calendario import Jornada, ZONA_BOGOTA  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402

ESQUEMA = "pruebas_reactivacion_script"

#: Un DSN que no resuelve, a propósito: las reglas 10 y 11 tienen que demostrar que
#: `barrido.encolar` NO ABRE NINGUNA CONEXIÓN cuando el interruptor está apagado o la calidad
#: frena -- si lo intentara, esto reventaría en el acto en vez de devolver ceros.
DSN_INVALIDA = "postgresql://usuario_invalido:clave@host-que-no-existe.invalid:5432/fantasma"

fallos = 0


def marca(ok: bool) -> str:
    global fallos
    if not ok:
        fallos += 1
    return "OK  " if ok else "FALLA"


def marca11(ok: bool) -> str:
    """La regla 11 no se marca `OK`: lo que este script puede comprobar es el mecanismo, no
    la consulta real a Meta (ver el docstring del módulo). Si el mecanismo falla, sigue
    contando como una FALLA de verdad -- lo único que cambia es la etiqueta del éxito."""
    global fallos
    if not ok:
        fallos += 1
        return "FALLA"
    return "POR VERIFICAR"


def _dia_habil_desde(fecha: datetime) -> datetime:
    """Ancla `fecha` al primer día hábil (lunes a viernes) en o después de ella.

    **Esto NO es la misma trampa que `probar_recordatorios.py::bloque_habil` ni
    `probar_tools.py::hora`.** Aquellas cuentan bloques hábiles HACIA ADELANTE DESDE HOY
    (`datetime.now()`) porque reservan un cupo real contra el calendario de verdad, y por eso
    envejecen: el mismo script, corrido un día distinto, cae en un día distinto de la semana.

    Aquí `AHORA` es un reloj SINTÉTICO fijo, nunca comparado contra `datetime.now()` --
    viaja como parámetro a `decidir`, `encolar` y `despachar`, exactamente como `ctx.ahora`
    en las tools -- así que fijarlo una vez no envejece nunca, se corra el día que se corra.
    Se ancla a un día hábil solo para que el camino feliz (regla 6) no choque por accidente
    con el domingo cerrado o el sábado de horario reducido.
    """
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha


#: 2026-03-02 es lunes; el ancla de abajo lo comprueba por su cuenta y no depende de que
#: siga siéndolo si alguien cambia la fecha.
AHORA = _dia_habil_desde(datetime(2026, 3, 2, 11, 0, tzinfo=ZONA_BOGOTA))

# ==========================================================================================
# Los números de prueba de cada caso. Todos empiezan por 5731 -- que no es ningún indicativo
# real de Colombia -- para que sean reconocibles como sintéticos de un vistazo.
# ==========================================================================================

TEL_MARCELA = "573100000001"  # el camino feliz: preguntó y no agendó
TEL_ANDRES = "573100000002"  # el camino feliz: canceló y no volvió a agendar

TEL_R1_FRIO = "573100000101"  # regla 1: solo un contacto, ninguna conversación
TEL_R1_WEB = "573100000102"  # regla 1: escribió, pero por el chat del panel, no WhatsApp

TEL_R2 = "573100000201"  # regla 2: máximo 2 intentos (24 h y 7 días)
TEL_R3 = "573100000301"  # regla 3: el botón de salida cierra el seguimiento para siempre
TEL_R4 = "573100000401"  # regla 4: la baja se respeta y es permanente
TEL_R5 = "573100000501"  # regla 5: el freno del contador

TEL_R9_ENVIADO_HOY = "573100000901"
TEL_R9_PENDIENTE_HOY = "573100000902"
TEL_R9_APLAZADO_LEJOS = "573100000903"
TEL_R9_LEJOS_SIN_APLAZAR = "573100000904"
TEL_R9_RECORDATORIO = "573100000905"
TEL_R9_CANDIDATOS = ["573100000911", "573100000912", "573100000913"]


# ==========================================================================================
# El montaje del esquema. Copiado de `probar_recordatorios.py` y `probar_tools.py`.
# ==========================================================================================


def url_de_pruebas() -> tuple[str, str]:
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        print("ERROR: falta MAXICARE_DATABASE_URL en .env", file=sys.stderr)
        raise SystemExit(1)
    directa = base.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return directa, f"{directa}{sep}options=-csearch_path%3D{ESQUEMA}"


def montar_esquema(directa: str, url: str) -> None:
    print(f"Preparando el esquema de pruebas '{ESQUEMA}' (no se toca 'public')\n")
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)


def limpiar(directa: str) -> None:
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()
    print(f"\nEsquema '{ESQUEMA}' borrado.")


def _mensaje(conn, telefono: str, *, cuando: datetime, nombre_perfil: str | None = None) -> None:
    """Siembra un mensaje entrante de verdad. No hay helper en `persistencia.py` para esto
    -- solo lo escribe `ingesta.py`, con el webhook de WhatsApp delante -- así que se hace a
    mano, igual que en `tests/test_reactivacion_neon.py::_mensaje`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en,
                                             nombre_perfil)
            VALUES (%s, %s, 'text', 'hola', %s, %s)
            """,
            (str(uuid.uuid4()), telefono, cuando, nombre_perfil),
        )
    conn.commit()


class WhatsAppQueRevienta:
    """El WhatsApp doblado de este script. No debe poder mandar nada de verdad: grabar la
    llamada y reventar es mejor que devolver un éxito silencioso, porque un envío real desde
    un script que promete «no manda nada» sería el fallo más caro de todos los que hay aquí.

    Con `plantillas={}` en todo el camino, `seguimientos.despachar` nunca debería siquiera
    intentar llamar a `enviar_plantilla` -- una fila decidida "enviar" se queda pendiente
    (ver su docstring). Que `.llamadas` siga vacío al final es, en sí mismo, una de las
    comprobaciones de este script.
    """

    def __init__(self) -> None:
        self.llamadas: list[dict] = []

    async def enviar_plantilla(
        self, telefono: str, *, plantilla: str, parametros: list[str], idioma: str = "es"
    ) -> str:
        self.llamadas.append(
            {"telefono": telefono, "plantilla": plantilla, "parametros": parametros}
        )
        raise AssertionError(
            f"WhatsAppQueRevienta.enviar_plantilla se llamó de VERDAD para {telefono} con la "
            f"plantilla {plantilla!r}: este script promete no mandar nada."
        )


# ==========================================================================================
# El camino feliz: los dos casos del diseño.
# ==========================================================================================


def demo_camino_feliz(conn, url: str) -> None:
    """Marcela preguntó por WhatsApp y no agendó; Andrés canceló y no volvió a agendar.
    `barrido.encolar` tiene que encontrar a los dos y `seguimientos.despachar` tiene que
    decidir "enviar" sobre los dos sin mandar nada -- las tres plantillas de reactivación
    siguen sin aprobar."""
    persistencia.asegurar_conversacion(conn, telefono=TEL_MARCELA)
    _mensaje(conn, TEL_MARCELA, cuando=AHORA - timedelta(hours=30), nombre_perfil="Marcela Rios")

    conv_andres = persistencia.asegurar_conversacion(conn, telefono=TEL_ANDRES)
    id_cita = persistencia.registrar_cita(
        conn, reserva_id=None, conversacion_id=conv_andres, paciente_id=None,
        nombre_completo="Andres Salazar", telefono=TEL_ANDRES, tratamiento="limpieza",
        inicio=AHORA - timedelta(days=5), duracion_minutos=60, evento_calendar_id=None,
    )
    persistencia.marcar_cita_cancelada(conn, id_cita, motivo="paciente")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE citas SET actualizada_en = %s WHERE id = %s",
            (AHORA - timedelta(hours=30), id_cita),
        )
    conn.commit()

    recuento_encolar = barrido.encolar(
        database_url=url, ahora=AHORA, tope_diario=20, encendido=True,
        calidad={"quality_rating": "GREEN", "messaging_limit_tier": "TIER_250"},
    )
    print(f"barrido.encolar: {recuento_encolar}")

    revienta = WhatsAppQueRevienta()
    recuento_despacho = asyncio.run(
        seguimientos.despachar(
            database_url=url, whatsapp=revienta, jornada=Jornada(), plantillas={}, ahora=AHORA,
        )
    )
    print(f"seguimientos.despachar (sin plantillas -- modo de comprobación): {recuento_despacho}")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cv.telefono, s.tipo, s.enviado_en, s.anulado_en, s.motivo_anulacion
              FROM seguimientos s JOIN conversaciones cv ON cv.id = s.conversacion_id
             WHERE cv.telefono IN (%s, %s)
             ORDER BY cv.telefono
            """,
            (TEL_MARCELA, TEL_ANDRES),
        )
        filas = cur.fetchall()
    for telefono, tipo, enviado_en, anulado_en, motivo in filas:
        if enviado_en:
            estado = "ENVIADO"
        elif anulado_en:
            estado = f"ANULADO ({motivo})"
        else:
            estado = "PENDIENTE (decidido 'enviar'; sin plantilla no sale nada todavía)"
        print(f"  {telefono} · {tipo} · {estado}")

    ok = (
        recuento_encolar["encolados"] == 2
        and recuento_despacho == {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 0}
        and not revienta.llamadas
        and len(filas) == 2
    )
    print(
        f"\n{marca(ok)} camino feliz: dos leads decididos, cero mensajes reales, el WhatsApp "
        "doblado no se llamó ni una vez"
    )


# ==========================================================================================
# Las once reglas anti-reporte, una por una.
# ==========================================================================================


def regla_01(conn) -> None:
    """Solo a quien escribió PRIMERO. Nunca frío, nunca comprado.

    Un contacto sin conversación de WhatsApp -una lista comprada, un número importado a mano-
    no puede calificar nunca: las dos consultas de cartera arrancan de `conversaciones` /
    `citas`, y `_LEADS_SIN_AGENDAR` exige además `canal = 'whatsapp'` (Ruling C4) -- una
    conversación del chat del panel no es un número al que se le pueda mandar una plantilla.
    """
    persistencia.asegurar_contacto(conn, TEL_R1_FRIO)  # una fila en `contactos`, y nada más

    persistencia.asegurar_conversacion(conn, telefono=TEL_R1_WEB, canal="web")
    _mensaje(conn, TEL_R1_WEB, cuando=AHORA - timedelta(hours=30))

    sin_agendar = {l["telefono"] for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200)}
    cancelados = {l["telefono"] for l in persistencia.leads_que_cancelaron(conn, ahora=AHORA, limite=200)}

    ok = (
        TEL_R1_FRIO not in sin_agendar
        and TEL_R1_FRIO not in cancelados
        and TEL_R1_WEB not in sin_agendar
        and TEL_R1_WEB not in cancelados
    )
    print(
        f"{marca(ok)} 1. solo quien escribió primero: un contacto sin conversación y una "
        "conversación de canal 'web' quedan fuera de las dos consultas de cartera"
    )


def regla_02(conn) -> None:
    """Máximo 2 intentos por consulta (24 h y 7 días).

    Un envío de hace 3 días todavía bloquea un tercero (condición 5, "en juego"); a los 8
    días de ESE envío, el barrido lo vuelve a ofrecer. Se comprueba con la MISMA fila, contra
    dos valores de `ahora` distintos -- ninguno de los dos es el reloj real.
    """
    conv = persistencia.asegurar_conversacion(conn, telefono=TEL_R2)
    _mensaje(conn, TEL_R2, cuando=AHORA - timedelta(hours=30))
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv, tipo=seguimientos.TIPO_SIN_AGENDAR,
        fecha_objetivo=AHORA - timedelta(days=3), clave_idempotencia="regla2-envio-1",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA - timedelta(days=3), "regla2-envio-1"),
        )
    conn.commit()

    a_los_3_dias = {l["telefono"] for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200)}
    a_los_8_dias_del_envio = {
        l["telefono"]
        for l in persistencia.leads_sin_agendar(conn, ahora=AHORA + timedelta(days=8), limite=200)
    }

    ok = TEL_R2 not in a_los_3_dias and TEL_R2 in a_los_8_dias_del_envio
    print(
        f"{marca(ok)} 2. máximo 2 intentos (24 h / 7 días): bloqueado a los 3 días del primer "
        "envío, libre otra vez a los 8"
    )


def regla_03(conn) -> None:
    """El botón de salida en cada mensaje cierra ese seguimiento para siempre.

    Recorre la SECUENCIA REAL, y eso es lo que corrige la revisión final: encolar -> marcar
    ENVIADO -> `cerrar_seguimiento` -> el barrido al día siguiente ya no lo encuentra.

    La versión anterior de esta comprobación sembraba a mano una fila PENDIENTE justo antes de
    llamar a la anulación, y en producción esa fila no existe en ese instante: el paciente
    solo puede pulsar «Ya no, gracias» DESPUÉS de que el mensaje salió, y una fila enviada
    tiene `enviado_en NOT NULL`, que es justo lo que `anular_reactivaciones_vivas` excluye.
    Con el estado fabricado, `anulados` valía 1 y todo parecía funcionar; con el estado real
    vale 0 y, antes del arreglo, NADA quedaba escrito -- el barrido lo volvía a encolar 25 h
    después de que Daniela le prometiera lo contrario.

    Lo que hoy sostiene el bloqueo es la LÁPIDA que escribe
    `persistencia.registrar_negativa_de_reactivacion`: una fila que nace anulada con el motivo
    permanente, del tipo que se le mandó, y que la condición 7 de `_LEADS_SIN_AGENDAR` excluye
    PARA SIEMPRE -- sin ventana de tiempo, al revés que las demás anulaciones.
    """
    conv = persistencia.asegurar_conversacion(conn, telefono=TEL_R3)
    _mensaje(conn, TEL_R3, cuando=AHORA - timedelta(hours=30))

    calificaba_antes = TEL_R3 in {
        l["telefono"] for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200)
    }

    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv, tipo=seguimientos.TIPO_SIN_AGENDAR,
        fecha_objetivo=AHORA - timedelta(hours=1), clave_idempotencia="regla3-enviado",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM seguimientos WHERE clave_idempotencia = 'regla3-enviado'")
        (id_fila,) = cur.fetchone()
    # El mensaje SALIÓ. A partir de aquí -y solo a partir de aquí- el paciente puede decir que
    # no: es la única ventana en la que ese botón existe.
    persistencia.marcar_seguimiento_enviado(conn, id_fila)

    resultado = persistencia.registrar_negativa_de_reactivacion(
        conn, TEL_R3, id_conversacion=conv,
        tipos=sorted(seguimientos.TIPOS_QUE_EL_BARRIDO_ENCOLA),
    )

    # Mucho más allá de los 7 días de la regla 2 -- y dentro todavía de los 30 del mensaje
    # original -- para que quede claro que esto NO caduca con el reloj, al revés que un
    # simple "llegó tarde".
    tras_mucho_tiempo = TEL_R3 in {
        l["telefono"]
        for l in persistencia.leads_sin_agendar(conn, ahora=AHORA + timedelta(days=10), limite=200)
    }

    ok = (
        calificaba_antes
        and resultado["lapidas"] == [seguimientos.TIPO_SIN_AGENDAR]
        and not tras_mucho_tiempo
    )
    print(
        f"{marca(ok)} 3. el botón de salida cierra el seguimiento para siempre: calificaba "
        f"antes ({calificaba_antes}); tras el envío REAL el «no» deja "
        f"{resultado['anulados']} anulada(s) y lápida sobre {resultado['lapidas']}; sigue "
        f"fuera 10 días después ({not tras_mucho_tiempo})"
    )


def regla_04(conn) -> None:
    """La baja se respeta, y es permanente -- pero nunca apaga el recordatorio de una cita.

    `pedir_baja` filtra a la persona de las dos consultas de cartera (vía `contactos.
    no_contactar`), y G0 de `seguimientos.decidir` hace lo mismo dentro del despachador --
    salvo para `TIPOS_NO_COMERCIALES`, que es la lista blanca que impide que pedir "no me
    mandes publicidad" se confunda con "no me avises de mi propia cita".
    """
    persistencia.asegurar_conversacion(conn, telefono=TEL_R4)
    _mensaje(conn, TEL_R4, cuando=AHORA - timedelta(hours=30))

    calificaba_antes = TEL_R4 in {
        l["telefono"] for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200)
    }
    cambio = persistencia.pedir_baja(conn, TEL_R4)
    calificaba_despues = TEL_R4 in {
        l["telefono"] for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200)
    }

    fila_reactivacion = {
        "tipo": seguimientos.TIPO_SIN_AGENDAR, "no_contactar": True,
        "seguimientos_fallidos": 0, "reactivaciones_ultimo_ano": 0,
        "fecha_objetivo": AHORA, "aplazado_desde": None,
        "cita_estado": None, "cita_inicio": None, "cita_id": None, "tomada_por": None,
    }
    decision_reactivacion = seguimientos.decidir(fila_reactivacion, ahora=AHORA, jornada=Jornada(),
                                                  ultimo_mensaje=None)

    fila_recordatorio = {
        "tipo": seguimientos.TIPO_RECORDATORIO, "no_contactar": True,
        "fecha_objetivo": AHORA, "aplazado_desde": None, "cita_id": "cita-de-prueba",
        "cita_estado": "confirmada", "cita_inicio": AHORA + timedelta(hours=5),
        "tomada_por": None,
    }
    decision_recordatorio = seguimientos.decidir(fila_recordatorio, ahora=AHORA, jornada=Jornada(),
                                                  ultimo_mensaje=AHORA - timedelta(hours=5))

    ok = (
        calificaba_antes and cambio and not calificaba_despues
        and decision_reactivacion.accion == "anular"
        and decision_reactivacion.motivo == "baja_solicitada"
        and decision_recordatorio.accion == "enviar"
    )
    print(
        f"{marca(ok)} 4. la baja se respeta y es permanente (calificaba: {calificaba_antes} -> "
        f"{calificaba_despues}); G0 anula una reactivación de baja pero NO un recordatorio de "
        "cita del mismo número"
    )


def regla_05(conn) -> None:
    """El freno del contador: dos fallos seguidos y el barrido deja de perseguir a esa
    persona -- hasta que agende, que es lo único que lo resetea (fuera del alcance de este
    script: lo hace `crear_cita`)."""
    persistencia.asegurar_conversacion(conn, telefono=TEL_R5)
    _mensaje(conn, TEL_R5, cuando=AHORA - timedelta(hours=30))

    calificaba_antes = TEL_R5 in {
        l["telefono"] for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200)
    }
    primero = persistencia.sumar_seguimiento_fallido(conn, TEL_R5)
    segundo = persistencia.sumar_seguimiento_fallido(conn, TEL_R5)
    calificaba_despues = TEL_R5 in {
        l["telefono"]
        for l in persistencia.leads_sin_agendar(conn, ahora=AHORA, limite=200, max_seguimientos_fallidos=2)
    }

    fila = {
        "tipo": seguimientos.TIPO_SIN_AGENDAR, "no_contactar": False,
        "seguimientos_fallidos": 2, "reactivaciones_ultimo_ano": 0,
        "fecha_objetivo": AHORA, "aplazado_desde": None,
        "cita_estado": None, "cita_inicio": None, "cita_id": None, "tomada_por": None,
    }
    decision = seguimientos.decidir(
        fila, ahora=AHORA, jornada=Jornada(), ultimo_mensaje=None, max_seguimientos_fallidos=2
    )

    ok = (
        calificaba_antes and primero == 1 and segundo == 2 and not calificaba_despues
        and decision.accion == "anular" and decision.motivo == "seguimiento_apagado"
    )
    print(
        f"{marca(ok)} 5. el freno del contador apaga a los {segundo} fallos: calificaba "
        f"{calificaba_antes} -> {calificaba_despues}, R1 dice {decision.accion}/{decision.motivo}"
    )


def regla_06() -> None:
    """Horario decente: nada antes de las 9:00 ni después de las 19:00, y nada en domingo.
    Pura -- `seguimientos.decidir` no toca la base, así que no hace falta conexión."""
    fila = {
        "tipo": seguimientos.TIPO_SIN_AGENDAR, "no_contactar": False,
        "seguimientos_fallidos": 0, "reactivaciones_ultimo_ano": 0,
        "fecha_objetivo": AHORA, "aplazado_desde": None,
        "cita_estado": None, "cita_inicio": None, "cita_id": None, "tomada_por": None,
    }
    madrugada = AHORA.replace(hour=3)
    decision_madrugada = seguimientos.decidir(fila, ahora=madrugada, jornada=Jornada(), ultimo_mensaje=None)

    domingo = AHORA
    while domingo.weekday() != 6:
        domingo += timedelta(days=1)
    domingo = domingo.replace(hour=11)
    # `fecha_objetivo` tiene que viajar con `domingo` y no quedarse en `AHORA`: si el
    # domingo cae varios días después, `AHORA` de sobra dispara antes R3 ("llegó tarde", más
    # de 2 h de retraso) y la comprobación de horario nunca llega a evaluarse.
    fila_domingo = dict(fila, fecha_objetivo=domingo)
    decision_domingo = seguimientos.decidir(
        fila_domingo, ahora=domingo, jornada=Jornada(), ultimo_mensaje=None
    )

    decision_normal = seguimientos.decidir(fila, ahora=AHORA, jornada=Jornada(), ultimo_mensaje=None)

    ok = (
        decision_madrugada.accion == "aplazar"
        and decision_madrugada.motivo == "fuera_de_horario_comercial"
        and decision_domingo.accion == "aplazar"
        and decision_domingo.motivo == "fuera_de_horario_comercial"
        and decision_normal.accion == "enviar"
    )
    print(
        f"{marca(ok)} 6. horario decente: 3 a. m. -> {decision_madrugada.accion}, domingo -> "
        f"{decision_domingo.accion}, martes 11 a. m. -> {decision_normal.accion}"
    )


def regla_07() -> None:
    """No pisarle la conversación: 24 h, mucho más ancha que los 60 minutos del recordatorio
    de cita (G6). Pura."""
    fila = {
        "tipo": seguimientos.TIPO_SIN_AGENDAR, "no_contactar": False,
        "seguimientos_fallidos": 0, "reactivaciones_ultimo_ano": 0,
        "fecha_objetivo": AHORA, "aplazado_desde": None,
        "cita_estado": None, "cita_inicio": None, "cita_id": None, "tomada_por": None,
    }
    hablo_hace_2h = seguimientos.decidir(
        fila, ahora=AHORA, jornada=Jornada(), ultimo_mensaje=AHORA - timedelta(hours=2)
    )
    hablo_hace_30h = seguimientos.decidir(
        fila, ahora=AHORA, jornada=Jornada(), ultimo_mensaje=AHORA - timedelta(hours=30)
    )
    ok = (
        hablo_hace_2h.accion == "anular" and hablo_hace_2h.motivo == "hablo_hace_poco"
        and hablo_hace_30h.accion == "enviar"
    )
    print(
        f"{marca(ok)} 7. no pisarle la conversación (24 h): hace 2 h -> {hablo_hace_2h.accion}, "
        f"hace 30 h -> {hablo_hace_30h.accion}"
    )


def regla_08() -> None:
    """Tope por persona y año: 6 mensajes de reactivación en 12 meses, pase lo que pase --
    el techo que el contador de la regla 5 no pone, porque ese se resetea al agendar."""
    fila_en_tope = {
        "tipo": seguimientos.TIPO_SIN_AGENDAR, "no_contactar": False,
        "seguimientos_fallidos": 0, "reactivaciones_ultimo_ano": 6,
        "fecha_objetivo": AHORA, "aplazado_desde": None,
        "cita_estado": None, "cita_inicio": None, "cita_id": None, "tomada_por": None,
    }
    fila_bajo_tope = dict(fila_en_tope, reactivaciones_ultimo_ano=5)

    decision_en_tope = seguimientos.decidir(
        fila_en_tope, ahora=AHORA, jornada=Jornada(), ultimo_mensaje=None, max_reactivaciones_12m=6
    )
    decision_bajo_tope = seguimientos.decidir(
        fila_bajo_tope, ahora=AHORA, jornada=Jornada(), ultimo_mensaje=None, max_reactivaciones_12m=6
    )
    ok = (
        decision_en_tope.accion == "anular" and decision_en_tope.motivo == "tope_anual"
        and decision_bajo_tope.accion == "enviar"
    )
    print(
        f"{marca(ok)} 8. tope por persona y año (6/12m): con 6 -> {decision_en_tope.accion}, "
        f"con 5 -> {decision_bajo_tope.accion}"
    )


def regla_09(conn, url: str) -> None:
    """Arranque lento (tope diario bajo) -- y de paso, la red que hoy NO EXISTE: ninguna
    prueba offline cubre `contar_comprometidos_hoy`, y toda su protección vive en `-m neon`.
    Este script corre contra un esquema real sin gastar nada, así que una de sus
    comprobaciones depende de que esa función cuente exactamente bien.
    """
    base = persistencia.contar_comprometidos_hoy(conn, ahora=AHORA)

    # Los tres casos de la Ruling E9 (docstring de `contar_comprometidos_hoy`), uno por uno:
    # enviado hoy, pendiente "de hoy", y pendiente lejano que YA se aplazó una vez cuentan;
    # pendiente lejano que NUNCA se aplazó y un recordatorio de cita, no.
    conv_a = persistencia.asegurar_conversacion(conn, telefono=TEL_R9_ENVIADO_HOY)
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv_a, tipo=seguimientos.TIPO_SIN_AGENDAR,
        fecha_objetivo=AHORA - timedelta(days=1), clave_idempotencia="r9-enviado-hoy",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE seguimientos SET enviado_en = %s WHERE clave_idempotencia = %s",
            (AHORA, "r9-enviado-hoy"),
        )
    conn.commit()

    conv_b = persistencia.asegurar_conversacion(conn, telefono=TEL_R9_PENDIENTE_HOY)
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv_b, tipo=seguimientos.TIPO_CANCELADA,
        fecha_objetivo=AHORA, clave_idempotencia="r9-pendiente-hoy",
    )

    conv_c = persistencia.asegurar_conversacion(conn, telefono=TEL_R9_APLAZADO_LEJOS)
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv_c, tipo=seguimientos.TIPO_SIN_AGENDAR,
        fecha_objetivo=AHORA + timedelta(days=14), clave_idempotencia="r9-aplazado-lejos",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM seguimientos WHERE clave_idempotencia = %s", ("r9-aplazado-lejos",)
        )
        id_c = cur.fetchone()[0]
    persistencia.aplazar_seguimiento(conn, id_c, hasta=AHORA + timedelta(days=14))

    conv_d = persistencia.asegurar_conversacion(conn, telefono=TEL_R9_LEJOS_SIN_APLAZAR)
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv_d, tipo=seguimientos.TIPO_CANCELADA,
        fecha_objetivo=AHORA + timedelta(days=14), clave_idempotencia="r9-lejos-sin-aplazar",
    )

    conv_e = persistencia.asegurar_conversacion(conn, telefono=TEL_R9_RECORDATORIO)
    persistencia.insertar_seguimiento(
        conn, id_conversacion=conv_e, tipo=seguimientos.TIPO_RECORDATORIO,
        fecha_objetivo=AHORA, clave_idempotencia="r9-recordatorio-hoy",
    )

    contados = persistencia.contar_comprometidos_hoy(conn, ahora=AHORA)
    ok_contar = contados == base + 3
    print(
        f"{marca(ok_contar)} 9a. contar_comprometidos_hoy: enviado-hoy + pendiente-hoy + "
        f"aplazado-lejos cuentan, lejos-sin-aplazar y un recordatorio de cita no -- "
        f"{base} -> {contados} (esperado {base + 3})"
    )

    # Tres candidatos más, con mensajes escalonados para que el orden de la consulta
    # (más reciente primero) sea predecible.
    for i, tel in enumerate(TEL_R9_CANDIDATOS):
        persistencia.asegurar_conversacion(conn, telefono=tel)
        _mensaje(conn, tel, cuando=AHORA - timedelta(hours=30 + i))

    tope_diario = contados + 1  # cupo = 1, con tres candidatos calificando
    recuento = barrido.encolar(
        database_url=url, ahora=AHORA, tope_diario=tope_diario, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cv.telefono FROM seguimientos s
              JOIN conversaciones cv ON cv.id = s.conversacion_id
             WHERE cv.telefono = ANY(%s) AND s.anulado_en IS NULL AND s.enviado_en IS NULL
            """,
            (TEL_R9_CANDIDATOS,),
        )
        encolados_de_verdad = {f[0] for f in cur.fetchall()}
    ok_cupo = recuento["encolados"] == 1 and encolados_de_verdad == {TEL_R9_CANDIDATOS[0]}
    print(
        f"{marca(ok_cupo)} 9b. arranque lento: con {contados} ya comprometidos y tope "
        f"{tope_diario} (cupo 1), de {len(TEL_R9_CANDIDATOS)} candidatos que calificaban solo "
        f"entró {encolados_de_verdad or 'ninguno'} -- {recuento}"
    )

    # Segunda pasada, mismo tope: el cupo ya se gastó y nadie más entra aunque los otros dos
    # sigan calificando -- esto es justo lo que impide la avalancha de la ronda 1 de revisión
    # (280 mensajes de golpe a las 9:00, ver `barrido.encolar`).
    recuento2 = barrido.encolar(
        database_url=url, ahora=AHORA, tope_diario=tope_diario, encendido=True,
        calidad={"quality_rating": "GREEN"},
    )
    ok_segunda = recuento2["encolados"] == 0
    print(
        f"{marca(ok_segunda)} 9c. el cupo gastado no se reabre en la pasada siguiente: {recuento2}"
    )


def regla_10() -> None:
    """El interruptor de pánico: `encendido=False` no toca la base -- ni siquiera abre una
    conexión. Se comprueba con un DSN que no resuelve: si `encolar` intentara conectar,
    reventaría aquí mismo en vez de devolver ceros en silencio."""
    try:
        recuento = barrido.encolar(
            database_url=DSN_INVALIDA, ahora=AHORA, tope_diario=20, encendido=False,
            calidad={"quality_rating": "GREEN"},
        )
        ok = recuento == {"encolados": 0, "contabilizados": 0, "frenado_por_calidad": 0}
    except Exception as e:  # noqa: BLE001 -- si esto revienta, la regla 10 está rota
        ok = False
        recuento = f"reventó: {type(e).__name__}: {e}"
    print(
        f"{marca(ok)} 10a. interruptor de pánico (encendido=False, DSN inválido): {recuento} "
        "-- no llegó a abrir ninguna conexión"
    )

    # La SEGUNDA MITAD del interruptor, que hasta la revisión final no existía: el freno vivía
    # solo en `barrido.encolar` -una vez por hora- y el despachador -cada sesenta segundos-
    # no lo miraba, así que quien accionaba el freno de emergencia veía salir todo lo que ya
    # estaba encolado. Aplaza, nunca anula y nunca marca; y el recordatorio de una cita sigue
    # saliendo, que es la frontera del no negociable 25.
    base = {
        "no_contactar": False, "seguimientos_fallidos": 0, "reactivaciones_ultimo_ano": 0,
        "fecha_objetivo": AHORA, "aplazado_desde": None, "cita_estado": None,
        "cita_inicio": None, "cita_id": None, "tomada_por": None,
    }
    motivos = {}
    for senal in ("interruptor_de_panico", "calidad_del_numero", "daniela_apagada"):
        decision = seguimientos.decidir(
            dict(base, tipo=seguimientos.TIPO_SIN_AGENDAR), ahora=AHORA, jornada=Jornada(),
            ultimo_mensaje=None, freno_de_reactivacion=senal,
        )
        motivos[senal] = f"{decision.accion}/{decision.motivo}"
    recordatorio = seguimientos.decidir(
        dict(
            base, tipo=seguimientos.TIPO_RECORDATORIO, cita_id="cita-de-prueba",
            cita_estado="confirmada", cita_inicio=AHORA + timedelta(hours=5),
        ),
        ahora=AHORA, jornada=Jornada(), ultimo_mensaje=None,
        freno_de_reactivacion="interruptor_de_panico",
    )
    ok_freno = (
        all(v.startswith("aplazar/frenada:") for v in motivos.values())
        and recordatorio.accion == "enviar"
    )
    print(
        f"{marca(ok_freno)} 10b. el freno llega hasta donde se MANDA: las tres señales dejan "
        f"la reactivación en {sorted(set(motivos.values()))} (aplazada, ni anulada ni marcada) "
        f"y un recordatorio de cita del mismo número sigue en '{recordatorio.accion}'"
    )


def regla_11() -> None:
    """Que se apague solo si la calidad del número baja. El MECANISMO se puede probar en
    frío contra `barrido.se_puede_encolar` y contra el mismo truco del DSN inválido de la
    regla 10; la CONSULTA REAL a `graph.facebook.com` no la hace este script -- eso lo dice
    `scripts/probar_plantilla.py --estado`, o una comprobación manual. Por eso se marca
    `POR VERIFICAR` y no `OK`."""
    casos = [
        ("GREEN", True), ("UNKNOWN", True), ("NA", True),
        ("YELLOW", False), ("RED", False), ("FLAGGED", False),
    ]
    ok = all(barrido.se_puede_encolar(rating) == esperado for rating, esperado in casos)
    ok = ok and barrido.se_puede_encolar(None) is False

    try:
        recuento_rojo = barrido.encolar(
            database_url=DSN_INVALIDA, ahora=AHORA, tope_diario=20, encendido=True,
            calidad={"quality_rating": "RED"},
        )
        recuento_sin_dato = barrido.encolar(
            database_url=DSN_INVALIDA, ahora=AHORA, tope_diario=20, encendido=True, calidad=None,
        )
        ok = (
            ok
            and recuento_rojo == {"encolados": 0, "contabilizados": 0, "frenado_por_calidad": 1}
            and recuento_sin_dato["frenado_por_calidad"] == 1
        )
    except Exception as e:  # noqa: BLE001
        ok = False

    print(
        f"{marca11(ok)} 11. calidad del número: GREEN/UNKNOWN/NA encolan, YELLOW/RED/FLAGGED y "
        "sin dato frenan sin abrir conexión -- la consulta real a Meta es de "
        "scripts/probar_plantilla.py, no de este script"
    )


def avisos_finales() -> None:
    print("\n" + "=" * 78)
    print("Tres cosas que hay que decir en voz alta, no solo dejar escritas")
    print("=" * 78)
    print(
        "1. EL ENSAYO EN SECO MUESTRA MAS DECISIONES QUE MENSAJES REALES.\n"
        "   Sin plantilla, una fila decidida 'enviar' queda PENDIENTE hasta que R3 la anule\n"
        "   a las 2 horas -- y anular LIBERA CUPO. Así que en un día se pueden ver bastante\n"
        "   más de `tope_diario` decisiones sin que salga ni un mensaje de más: no manda de\n"
        "   más, pero engaña a quien dimensione la campaña con esos números tal cual.\n"
    )
    print(
        "2. NINGUNA PRUEBA -ni esta, ni ninguna- GARANTIZA QUE LA GENTE NO REPORTE EL NÚMERO.\n"
        "   Lo que este script garantiza es que ninguna de las DIEZ reglas comprobables se\n"
        "   puede saltar. El resto lo sostiene el procedimiento: correr el camino entero\n"
        "   contra la API real antes de encender, y arrancar despacio.\n"
    )
    print(
        "3. NADA SALE HASTA QUE META APRUEBE LAS TRES PLANTILLAS DE REACTIVACIÓN.\n"
        "   El sistema queda entero y en modo comprobación: encola, decide, registra, y NO\n"
        "   manda. Encender es rellenar tres variables de plantilla."
    )


def main() -> int:
    directa, url = url_de_pruebas()
    montar_esquema(directa, url)
    try:
        with persistencia.conectar(url) as conn:
            print("=== Camino feliz: Marcela (sin agendar) y Andrés (canceló) ===\n")
            demo_camino_feliz(conn, url)

            print("\n=== Las once reglas anti-reporte ===\n")
            regla_01(conn)
            regla_02(conn)
            regla_03(conn)
            regla_04(conn)
            regla_05(conn)
            regla_06()
            regla_07()
            regla_08()
            regla_09(conn, url)
            regla_10()
            regla_11()

        avisos_finales()
    finally:
        limpiar(directa)
    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLA(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())

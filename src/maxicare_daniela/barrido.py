"""Quién entra en la cola de reactivación.

Va aparte de `seguimientos.py` a propósito: aquel decide QUÉ hacer con una fila que ya está
en la cola, y esto decide QUIÉN entra. Son las dos mitades de la misma función y este es el
ÚNICO módulo que consulta la cartera entera --conversaciones, citas y mensajes de fuera de la
cola-- para decidir quién califica.

**Este módulo no manda nada.** `encolar` solo escribe filas en `seguimientos`; quien decide
qué hacer con cada una y quien de verdad envía es `seguimientos.despachar`, con sus guardas
(G0-G7, R1-R5). Esa separación es lo que permite encender el barrido en producción **sin**
plantillas --las tres de reactivación siguen vacías mientras Meta no las apruebe-- y ver a
quién se le habría escrito antes de escribirle a nadie.

Deliberadamente NO habla con Meta por su cuenta: la calidad del número entra como parámetro
(regla 11), la consulta la hace `runtime`, que es quien tiene el cliente de WhatsApp
construido. Es la misma separación que ya usa `seguimientos.decidir`, que no habla con la
base: así una prueba puede fijar la calidad sin doblar `httpx`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import persistencia, seguimientos

log = logging.getLogger(__name__)

#: Lo que Meta puede decir de un número. `UNKNOWN` y `NA` son de un número sin historial
#: suficiente, no una señal de daño: tratarlos como rojo dejaría el sistema apagado para
#: siempre en una cuenta nueva, que es justo cuando más falta hace.
CALIDADES_QUE_DEJAN_ENCOLAR = frozenset({"GREEN", "UNKNOWN", "NA"})


def se_puede_encolar(quality_rating: str | None) -> bool:
    """`None` --la consulta a Meta falló, o no se hizo-- devuelve `False`.

    Es la asimetría CONTRARIA a la del no negociable 26 (`canales.aviso_sigue_puesto`): allí,
    ante la duda se avisa, porque molestar al doctor de más es barato. Aquí, ante la duda NO
    se encola: el coste de no encolar durante una hora son unos pocos leads que se reintentan
    en el ciclo siguiente; el coste de encolar con la calidad ya en rojo es el número entero
    de WhatsApp de la clínica.
    """
    if not quality_rating:
        return False
    return quality_rating.upper() in CALIDADES_QUE_DEJAN_ENCOLAR


def _contabilizar_envios_vencidos(conn, *, ahora: datetime) -> int:
    """Sube el contador de quien recibió un envío hace más de una semana y no agendó.

    Cada fila de `persistencia.envios_por_contabilizar` es un ENVÍO de reactivación (Ronda 2:
    ya no "una serie" -- ver el docstring de `persistencia.DIAS_ENTRE_INTENTOS_DE_
    REACTIVACION`) que lleva `persistencia.DIAS_ENTRE_INTENTOS_DE_REACTIVACION` días sin
    acabar en una cita (Ronda 1, I-2: la vara es agendar, no contestar): se cuenta
    (`contactos.seguimientos_fallidos += 1`) y se marca (`contabilizado_en`) para que la
    pasada siguiente no la vuelva a contar. Sin la marca, el freno por persona (R1,
    `max_seguimientos_fallidos`) se dispararía solo con el paso del tiempo, sin que la
    persona hiciera nada más.

    Ruling D5: el orden es SUMAR primero (con `commit=False`) y MARCAR después, nunca al
    revés -- ver el docstring corregido de `persistencia.sumar_seguimiento_fallido`.
    `sumar_seguimiento_fallido` llama a `asegurar_contacto`, que hace su propio `commit()`
    incondicional; con el contador todavía sin confirmar, es el `commit()` de
    `marcar_envio_contabilizado` el que confirma las DOS escrituras juntas. Al revés, una
    caída entre las dos deja la fila MARCADA pero el contador SIN SUBIR: el envío fallido
    desaparece sin contarse y sin que nadie pueda volver a intentarlo -- el agujero exacto
    que `contabilizado_en` existe para tapar.
    """
    contadas = 0
    for fila in persistencia.envios_por_contabilizar(conn, ahora=ahora):
        persistencia.sumar_seguimiento_fallido(conn, fila["telefono"], commit=False)
        persistencia.marcar_envio_contabilizado(conn, fila["id"])
        contadas += 1
    return contadas


def encolar(
    *,
    database_url: str,
    ahora: datetime,
    tope_diario: int,
    encendido: bool,
    calidad: dict[str, str] | None,
    max_seguimientos_fallidos: int = seguimientos.MAX_SEGUIMIENTOS_FALLIDOS,
    limite: int = 200,
) -> dict[str, int]:
    """Una pasada del barrido.

    Devuelve `{"encolados": n, "contabilizados": n, "frenado_por_calidad": 0|1}`.

    `calidad` entra como palabra clave OBLIGATORIA y SIN valor por defecto (Ruling C7): un
    default permisivo reabriría el agujero que la regla 11 existe para cerrar. Quien la
    consulta es `runtime`, con `await whatsapp.calidad_del_numero()`, ANTES de llamar aquí --
    esta función es síncrona y no habla con la red, igual que `seguimientos.decidir` no
    habla con la base.

    Dos guardas antes de tocar la base siquiera, en este orden (Ruling C8):

    1. `encendido` (regla 10, el interruptor de pánico). `False` no toca la base: ni
       siquiera abre una conexión.
    2. `calidad` (regla 11). Con la calidad en rojo o desconocida tampoco hay por qué abrir
       una conexión: si el número está en riesgo, ni una consulta de lectura vale la pena.
    """
    recuento = {"encolados": 0, "contabilizados": 0, "frenado_por_calidad": 0}
    if not encendido:
        return recuento

    if not se_puede_encolar(calidad.get("quality_rating") if calidad else None):
        log.warning(
            "la calidad del número es %s: no se encola nada en este ciclo",
            (calidad or {}).get("quality_rating", "desconocida"),
        )
        recuento["frenado_por_calidad"] = 1
        return recuento

    conn = persistencia.conectar(database_url)
    try:
        # Ronda 2 de revisión, bug 2.2: `_contabilizar_envios_vencidos` va ANTES del cupo, no
        # después. Antes del arreglo del CRÍTICO, el cupo solo llegaba a 0 tras envíos REALES
        # -un caso que en la práctica es raro un rato antes de las 9pm-, así que contabilizar
        # corría casi siempre. Con `contar_comprometidos_hoy` (que cuenta lo pendiente), el
        # cupo llega a 0 TODAS las noches y TODOS los domingos -es el estado normal-, y con el
        # `return` de arriba `_contabilizar_envios_vencidos` dejaba de correr precisamente
        # esas horas. Medido por el revisor: con una fila pendiente y `tope_diario=1`, un
        # envío vencido hace 10 días devolvía `contabilizados: 0`. Retrasar el cierre de
        # envíos vencidos no pierde el cierre, pero alimenta el bug 2.1 (más pendientes
        # acumulados, más tiempo) -- contabilizar es trabajo de mantenimiento independiente
        # del cupo. Ronda 3, corrección: NO corre "en cada pasada sin excepción" -- las dos
        # guardas de arriba (`encendido`, `calidad`) siguen yendo ANTES y siguen sin abrir
        # conexión si frenan (Ruling C8: con el número en riesgo, ni una lectura vale la
        # pena). Lo que corrige este orden es que, de las pasadas que SÍ llegan a abrir
        # conexión, ninguna se salte el cierre por falta de cupo.
        recuento["contabilizados"] = _contabilizar_envios_vencidos(conn, ahora=ahora)

        # El tope diario (regla 9). CRÍTICO, ronda 1 de revisión: aquí usaba `contar_
        # enviados_hoy`, que solo ve `enviado_en` y deja INVISIBLE toda fila que R4 aplazó
        # (no anuló) fuera de 9-19h. Con eso, el cupo se veía libre TODA LA NOCHE y cada
        # pasada horaria volvía a encolar el tope entero sobre gente nueva -- 280 mensajes de
        # golpe a las 9:00, medido por el revisor con 400 leads y tope 20. `contar_
        # comprometidos_hoy` cuenta también lo pendiente que es "de hoy" -por fecha o por
        # haberse aplazado ya (Ruling E9, ver su docstring)-, y es lo que de verdad acota el
        # compromiso total sin depender de un número de horas calculado a mano.
        comprometidos = persistencia.contar_comprometidos_hoy(conn, ahora=ahora)
        cupo = max(0, tope_diario - comprometidos)
        if cupo == 0:
            return recuento

        for tipo, consulta in (
            (seguimientos.TIPO_SIN_AGENDAR, persistencia.leads_sin_agendar),
            (seguimientos.TIPO_CANCELADA, persistencia.leads_que_cancelaron),
        ):
            if recuento["encolados"] >= cupo:
                break
            # I-4, ronda 1 de revisión (inanición): se pide `limite` -- el parámetro de la
            # función, pensado para esto y hasta ahora muerto-- y NO `cupo`. Una fila que ya
            # se encoló HOY y luego se anuló (`tope_anual`, `sin_nombre`...) no bloquea la
            # reconsulta -el "en juego" de arriba no cuenta lo anulado- pero SIGUE en la
            # cabeza del `ORDER BY`, y con `limite=cupo` la consulta solo traía esas mismas
            # filas ya condenadas: chocaban contra el `ON CONFLICT`, `encolados` se quedaba
            # en 0, y la persona nº 21 no recibía su turno en TODA la jornada. Pidiendo más
            # candidatos de los que hacen falta y siguiendo hasta cubrir el cupo de verdad,
            # una fila fallida ya no tapa a las que vienen detrás.
            for lead in consulta(
                conn,
                ahora=ahora,
                limite=limite,
                max_seguimientos_fallidos=max_seguimientos_fallidos,
            ):
                if recuento["encolados"] >= cupo:
                    break
                # La clave la arma el CÓDIGO, nunca el modelo (no negociable 2), y lleva el
                # DÍA dentro (Ruling B5): es lo que hace cierta la frase «el barrido lo
                # volverá a encolar» en la que se apoyan R3 y R5 de `seguimientos.decidir`
                # para ANULAR en vez de aplazar. Sin el día, la misma clave chocaría para
                # siempre contra el `ON CONFLICT DO NOTHING` de `insertar_seguimiento`, y
                # anular se convertiría en una puerta de una sola dirección: la persona
                # quedaría fuera de la reactivación aunque siguiera calificando meses después.
                clave = ":".join(
                    [
                        str(lead["conversacion_id"]),
                        "reactivacion",
                        tipo,
                        ahora.date().isoformat(),
                    ]
                )
                # No cuenta como error si `insertar_seguimiento` devuelve `False`: significa
                # que esta persona ya tenía HOY una fila con esta clave (posiblemente
                # anulada, ver I-4) y simplemente se sigue con el siguiente candidato de la
                # lista, sin romper el bucle.
                if persistencia.insertar_seguimiento(
                    conn,
                    id_conversacion=lead["conversacion_id"],
                    tipo=tipo,
                    fecha_objetivo=ahora,
                    clave_idempotencia=clave,
                ):
                    recuento["encolados"] += 1
        return recuento
    finally:
        conn.close()

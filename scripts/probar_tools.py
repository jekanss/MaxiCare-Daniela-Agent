"""Corre las diez tools contra Neon y un calendario de pruebas, y lo cuenta en claro.

    uv run python scripts/probar_tools.py

Es el entregable de la fase 3 en forma legible. Las pruebas de `tests/test_tools_neon.py`
comprueban lo mismo y son la red de seguridad permanente; esto es lo que se le enseña a
alguien para que vea que funciona.

Escribe en un esquema aparte (`pruebas`) dentro de la misma base de Neon, y lo borra al
terminar. No toca `public`, donde viven los pacientes reales: la conexión lleva
`search_path=pruebas`, asi que el aislamiento es fisico y no depende de que nadie se acuerde
de prefijar una tabla.

Va contra la conexion DIRECTA de Neon, no la del pooler: PgBouncer rechaza `options` como
parametro de arranque, y ademas reparte conexiones entre sesiones compartidas -- lo contrario
de lo que necesita una prueba de concurrencia.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import herramientas as h  # noqa: E402
from maxicare_daniela import persistencia  # noqa: E402
from maxicare_daniela.calendario import CalendarioDoble, Jornada  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402
from maxicare_daniela.contratos import (  # noqa: E402
    ContextoDaniela,
    SolicitudCancelacion,
    SolicitudCita,
    SolicitudEscalamiento,
)

ESQUEMA = "pruebas"
CAPACIDAD = 2

fallos = 0


def marca(ok: bool) -> str:
    global fallos
    if not ok:
        fallos += 1
    return "OK  " if ok else "FALLA"


def url_de_pruebas() -> tuple[str, str]:
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        print("ERROR: falta MAXICARE_DATABASE_URL en .env", file=sys.stderr)
        raise SystemExit(1)
    directa = base.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return directa, f"{directa}{sep}options=-csearch_path%3D{ESQUEMA}"


def hora(desplazamiento: int = 0) -> datetime:
    """El bloque hábil número `desplazamiento`, contando desde dentro de 45 días.

    Era `ahora + 45 días + N horas`, y eso dejó de valer el 13/09/2026, cuando la rejilla
    aprendió el horario de la clínica: corriendo a las 7 de la tarde, la base caía a las
    19:00 y las tools rechazaban TODO --seis comprobaciones en rojo, incluida la de
    concurrencia que cierra la fase 3--. Los desplazamientos grandes (`hora(20)`, `hora(72)`)
    caían además en madrugada y en fines de semana.

    Contar bloques hábiles en vez de horas de reloj conserva lo que el script necesita --que
    dos desplazamientos distintos sean horas distintas, y que los consecutivos sean
    contiguos-- y añade lo que ahora hace falta: que todos existan para la clínica. Lo que
    cambia es que `hora(20)` ya no son 20 horas después, sino el bloque hábil 20.
    """
    jornada = Jornada()
    actual = (datetime.now(h.ZONA_BOGOTA) + timedelta(days=45)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    habiles = 0
    while True:
        if jornada.cabe(actual, 60):
            if habiles == desplazamiento:
                return actual
            habiles += 1
        actual += timedelta(hours=1)


def contexto(url: str, telefono: str, nombre: str) -> ContextoDaniela:
    with persistencia.conectar(url) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo=nombre, telefono=telefono
        )
        id_conv = persistencia.asegurar_conversacion(
            conn, telefono=telefono, paciente_id=id_paciente
        )
    return ContextoDaniela(
        id_conversacion=id_conv,
        telefono_completo=telefono,
        database_url=url,
        calendario=CalendarioDoble(),
        id_paciente=id_paciente,
        nombre_paciente=nombre,
        identidad_verificada=True,
        capacidad_por_hora=CAPACIDAD,
        duracion_cita_minutos=60,
    )


class TelegramFalso:
    """No manda nada al grupo real: una prueba no puede hacerle sonar el telefono a un doctor."""

    def __init__(self) -> None:
        self.enviados: list[dict] = []

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.enviados.append({"texto": texto, "tema_id": tema_id, "teclado": teclado})
        return 1


def main() -> int:
    directa, url = url_de_pruebas()

    print("Preparando el esquema de pruebas (no se toca 'public')\n")
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    try:
        with persistencia.conectar(url) as conn:
            persistencia.aplicar_esquema(conn)
            persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())
        return corridas(url)
    finally:
        with persistencia.conectar(directa) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            conn.commit()
        print("\nEsquema de pruebas borrado.")


def corridas(url: str) -> int:
    ctx = contexto(url, "573009990001", "Paciente De Prueba")

    # -- 1. conocimiento -----------------------------------------------------------------
    print("1. consultar_base_conocimiento")
    precio = asyncio.run(h._consultar_base_conocimiento(ctx, "implantes", "precio"))
    print(f"   implantes/precio      -> {marca(len(precio) > 20)} {precio[:60]}...")

    sin_dato = asyncio.run(h._consultar_base_conocimiento(ctx, "ortodoncia_lunar", "precio"))
    ok = sin_dato.startswith("SIN DATO DOCUMENTADO") and "PROHIBIDO estimar" in sin_dato
    print(f"   algo inexistente      -> {marca(ok)} devuelve SIN DATO DOCUMENTADO, no vacio")

    # -- 2. identificacion ---------------------------------------------------------------
    print("\n2. identificar_paciente")
    ctx.identidad_verificada = False
    ctx.id_paciente = None
    texto = asyncio.run(h._identificar_paciente(ctx, "paciente de prueba"))
    print(f"   nombre correcto       -> {marca('verificada' in texto)} {texto[:50]}")

    try:
        asyncio.run(h._identificar_paciente(ctx, "Alguien 1020304050"))
        rechazo = False
    except ValueError:
        rechazo = True
    print(f"   nombre con cedula     -> {marca(rechazo)} rechazado antes de tocar la base")

    # -- 3. disponibilidad ---------------------------------------------------------------
    print("\n3. consultar_disponibilidad")
    desde, hasta = hora(0), hora(6)
    libres = asyncio.run(h._consultar_disponibilidad(ctx, desde.isoformat(), hasta.isoformat()))
    print(f"   ventana de 6 horas    -> {marca('Bloques libres' in libres)} {libres[:70]}...")

    ctx.calendario.agregar_bloqueo(desde, desde + timedelta(hours=1), "cirugia")
    con_bloqueo = asyncio.run(
        h._consultar_disponibilidad(ctx, desde.isoformat(), hasta.isoformat())
    )
    ok = f"{desde:%H:%M}" not in con_bloqueo
    print(f"   con hora bloqueada    -> {marca(ok)} no ofrece la hora que el doctor aparto")

    # Pedir una hora con la clinica cerrada no es quedarse sin cupo, y decirlo igual manda al
    # paciente a buscar otro DIA cuando lo que necesita es otra HORA.
    madrugada = desde.replace(hour=3, minute=0)
    cerrado = asyncio.run(
        h._consultar_disponibilidad(
            ctx, madrugada.isoformat(), (madrugada + timedelta(hours=1)).isoformat()
        )
    )
    print(f"   con la clinica cerrada:")
    print(f"     lo distingue        -> {marca('fuera del horario' in cerrado)} "
          f"no lo cuenta como falta de cupo")
    print(f"     recuerda el horario -> {marca(f'{Jornada().apertura}:00' in cerrado)} "
          f"le dice cuando si atiende")
    print(f"     ofrece alternativa  -> {marca('Estas si' in cerrado or 'Estas sí' in cerrado)} "
          f"y una hora libre concreta")
    # Y esas horas quedan AUTORIZADAS: sin esto el guardrail bloquea el mensaje entero y el
    # paciente recibe «te escribe el doctor» por preguntar a que hora abren.
    autorizado = f"{Jornada().apertura:02d}:00" in ctx.turno.horas_autorizadas
    print(f"     puede decirlo       -> {marca(autorizado)} el horario queda autorizado")

    # -- 4. EL ENTREGABLE ----------------------------------------------------------------
    print("\n4. crear_cita  ***  LA PRUEBA QUE CIERRA LA FASE  ***")
    inicio = hora(10)
    contextos = [
        contexto(url, f"57300999100{i}", f"Paciente Simultaneo {i}") for i in range(3)
    ]

    async def tres_a_la_vez():
        return await asyncio.gather(
            *[
                h._crear_cita(
                    c,
                    SolicitudCita(
                        nombre_completo=c.nombre_paciente,
                        inicio=inicio,
                        tratamiento="limpieza",
                        clave_idempotencia=f"{c.telefono_completo}:{inicio.isoformat()}",
                    ),
                )
                for c in contextos
            ]
        )

    resultados = asyncio.run(tres_a_la_vez())
    confirmadas = sum("Cita confirmada" in r for r in resultados)
    llenas = sum("ya esta lleno" in r or "ya está lleno" in r for r in resultados)

    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM citas WHERE inicio = %s", (inicio,))
        en_base = cur.fetchone()[0]

    print(f"   3 pacientes piden la misma hora, la clinica atiende {CAPACIDAD} por hora")
    print(f"     confirmadas         : {confirmadas}   {marca(confirmadas == 2)}")
    print(f"     avisadas de lleno   : {llenas}   {marca(llenas == 1)}")
    print(f"     citas en la base    : {en_base}   {marca(en_base == 2)}")
    print(f"   -> {marca(confirmadas == 2 and en_base == 2)} "
          f"{en_base} citas de 3 intentos simultaneos")

    # -- 5. contension real con hilos ----------------------------------------------------
    print("\n5. El mismo cupo, con 6 hilos saliendo a la vez")
    inicio2 = hora(11)
    barrera = threading.Barrier(6)
    concedidos: list[object] = [None] * 6

    def intentar(i: int) -> None:
        with persistencia.conectar(url) as conn:
            barrera.wait()
            concedidos[i] = persistencia.tomar_cupo(
                conn,
                inicio=inicio2,
                capacidad=CAPACIDAD,
                clave_idempotencia=f"hilo-{i}-{inicio2.isoformat()}",
            )

    hilos = [threading.Thread(target=intentar, args=(i,)) for i in range(6)]
    for t in hilos:
        t.start()
    for t in hilos:
        t.join()
    con_cupo = [c for c in concedidos if c is not None]
    print(f"   cupos concedidos      -> {marca(len(con_cupo) == CAPACIDAD)} "
          f"{len(con_cupo)} de 6 hilos (el tope es {CAPACIDAD})")

    # -- 6. el ciclo de una cita ---------------------------------------------------------
    print("\n6. reprogramar_cita y cancelar_cita")
    ctx2 = contexto(url, "573009992002", "Carlos Ciclo")
    salida = asyncio.run(
        h._crear_cita(
            ctx2,
            SolicitudCita(
                nombre_completo="Carlos Ciclo",
                inicio=hora(20),
                tratamiento="cordales",
                motivo="Quiere valoracion de las cordales; le molestan al masticar.",
                clave_idempotencia=f"{ctx2.telefono_completo}:{hora(20).isoformat()}",
            ),
        )
    )
    id_cita = salida.rsplit("Id de la cita: ", 1)[1].rstrip(".")

    # Lo que la clinica lee al abrir la cita. El telefono lo pone el codigo desde el
    # contexto, nunca el modelo: `SolicitudCita` no tiene campo de telefono.
    (descripcion,) = ctx2.calendario.descripciones.values()
    lleva_telefono = ctx2.telefono_completo in descripcion
    lleva_servicio = "cordales" in descripcion
    lleva_motivo = "masticar" in descripcion
    print(f"   el evento en Calendar:")
    print(f"     telefono            -> {marca(lleva_telefono)} para poder llamar al paciente")
    print(f"     servicio            -> {marca(lleva_servicio)} a que viene")
    print(f"     motivo              -> {marca(lleva_motivo)} con las palabras del paciente")
    destino = hora(21)
    movida = asyncio.run(h._reprogramar_cita(ctx2, id_cita, destino.isoformat()))
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (hora(20),))
        viejo_libre = cur.fetchone()[0] == 0
    print(f"   reprogramar           -> {marca('reprogramada' in movida)} {movida[:55]}")
    print(f"   el cupo viejo         -> {marca(viejo_libre)} quedo libre")

    ctx2.calendario.fallar_en.add("eliminar_evento")
    cancelada = asyncio.run(
        h._cancelar_cita(ctx2, SolicitudCancelacion(id_cita=id_cita, clave_idempotencia=id_cita))
    )
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (destino,))
        libre = cur.fetchone()[0] == 0
    print(f"   cancelar con Calendar caido:")
    print(f"     el cupo se libera   -> {marca(libre)} igual, aunque Calendar no responda")
    print(f"     se avisa del evento -> {marca('sigue en el calendario' in cancelada)} "
          f"huerfano, para limpiarlo a mano")

    # -- 7. el camino que puede mandar a alguien a una clinica vacia ----------------------
    print("\n7. crear_cita con Calendar caido (el camino peligroso)")
    ctx3 = contexto(url, "573009993003", "Diana Compensacion")
    ctx3.calendario.fallar_en.add("crear_evento")
    inicio3 = hora(30)
    try:
        asyncio.run(
            h._crear_cita(
                ctx3,
                SolicitudCita(
                    nombre_completo="Diana Compensacion",
                    inicio=inicio3,
                    tratamiento="limpieza",
                    clave_idempotencia=f"{ctx3.telefono_completo}:{inicio3.isoformat()}",
                ),
            )
        )
        murio = False
    except h.CitaNoConfirmada:
        murio = True
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reservas WHERE inicio = %s", (inicio3,))
        sin_fantasma = cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM citas WHERE inicio = %s", (inicio3,))
        sin_cita = cur.fetchone()[0] == 0
    print(f"   la corrida muere      -> {marca(murio)} el modelo no puede confirmar nada")
    print(f"   el cupo se libera     -> {marca(sin_fantasma)} no queda reserva fantasma")
    print(f"   no hay cita           -> {marca(sin_cita)} y el paciente no fue citado")

    # -- 8. estado y seguimiento ---------------------------------------------------------
    print("\n8. registrar_estado_oportunidad y programar_seguimiento")
    estado = asyncio.run(
        h._registrar_estado_oportunidad(
            ctx, "listo_para_agendar", "ninguna", "implantes", False, "mando una radiografia"
        )
    )
    print(f"   estado guardado       -> {marca('guardado' in estado)} {estado}")

    objetivo = hora(72)
    primero = asyncio.run(h._programar_seguimiento(ctx, "recordatorio_cita", objetivo.isoformat()))
    repetido = asyncio.run(h._programar_seguimiento(ctx, "recordatorio_cita", objetivo.isoformat()))
    print(f"   seguimiento           -> {marca('programado' in primero)} {primero[:55]}")
    print(f"   el mismo otra vez     -> {marca('ya estaba' in repetido)} no se duplica")

    # -- 9. escalamiento -----------------------------------------------------------------
    print("\n9. escalar_a_doctores")
    telegram = TelegramFalso()
    ctx.topic_id = 99
    ctx.tema_general = 0
    solicitud = SolicitudEscalamiento(
        motivo="clinico",
        resumen_para_doctor="Pregunta por algo que ve en su radiografia.",
        pregunta_concreta="Se puede responder esto por WhatsApp?",
        clave_idempotencia=f"{ctx.id_conversacion}:1",
    )
    asyncio.run(h._escalar_a_doctores(ctx, solicitud, telegram=telegram))
    enviado = telegram.enviados[0] if telegram.enviados else {}
    al_general = enviado.get("tema_id") == 0
    print(f"   destino               -> {marca(al_general)} tema General (0), NO el tema "
          f"del paciente ({ctx.topic_id})")
    print(f"   boton de relevo       -> "
          f"{marca('Hablar yo con el paciente' in str(enviado.get('teclado')))} presente")

    asyncio.run(h._escalar_a_doctores(ctx, solicitud, telegram=telegram))
    print(f"   el mismo turno otra vez -> {marca(len(telegram.enviados) == 1)} "
          f"no se volvio a avisar")

    # -- 10. encontrar la cita cuando la conversacion ya murio ---------------------------
    print("\n10. consultar_citas (la decima, la que no esta en el plan)")
    ctx4 = contexto(url, "573009994004", "Marta Regresa")
    inicio4 = hora(40)
    asyncio.run(
        h._crear_cita(
            ctx4,
            SolicitudCita(
                nombre_completo="Marta Regresa",
                inicio=inicio4,
                tratamiento="blanqueamiento",
                clave_idempotencia=f"{ctx4.telefono_completo}:{inicio4.isoformat()}",
            ),
        )
    )

    # Dos dias despues. Otra conversacion, sin historial y sin el id a la vista: es
    # exactamente lo que le pasa a un paciente real, porque `conversacion_viva` dura 24 h.
    regreso = contexto(url, "573009994004", "Marta Regresa")
    encontrada = asyncio.run(h._consultar_citas(regreso))
    print(f"   conversacion nueva    -> {marca('blanqueamiento' in encontrada)} "
          f"la encuentra sin que el paciente dicte el id")
    autorizada = f"{inicio4:%H:%M}" in regreso.turno.horas_autorizadas
    print(f"   la hora que nombra    -> {marca(autorizada)} queda autorizada, asi que "
          f"puede decirsela al paciente")

    ajeno = contexto(url, "573009995005", "Otro Numero")
    vacio = asyncio.run(h._consultar_citas(ajeno))
    ok_ajeno = "no tiene" in vacio and "blanqueamiento" not in vacio
    print(f"   otro numero           -> {marca(ok_ajeno)} no ve la cita de nadie mas")

    print()
    if fallos:
        print(f"{fallos} comprobacion(es) fallaron.")
        return 1
    print("Las diez tools funcionan. Tres llamadas simultaneas -> exactamente 2 citas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

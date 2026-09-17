"""Corre la cola de recordatorios contra Neon y lo cuenta en claro.

    uv run python scripts/probar_recordatorios.py

Es el entregable de los recordatorios en forma legible. No gasta un solo token: el modelo no
interviene en ningún punto de este camino, que es justamente lo que se quiere demostrar --el
recordatorio lo emite el código.

Escribe en el esquema `pruebas` y lo borra al terminar. Va contra la conexión DIRECTA de Neon,
sin el `-pooler.` del host: la comprobación 6 necesita dos sesiones de verdad, y PgBouncer las
reparte entre sesiones compartidas. Con el pooler, esa comprobación pasa en verde sin probar
nada -- que es peor que no tenerla.
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
from maxicare_daniela import persistencia, seguimientos  # noqa: E402
from maxicare_daniela.calendario import CalendarioDoble, Jornada  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402
from maxicare_daniela.contratos import (  # noqa: E402
    ContextoDaniela,
    SolicitudCancelacion,
    SolicitudCita,
)

ESQUEMA = "pruebas"

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


def montar_esquema(directa: str, url: str) -> None:
    """Copiado de `probar_tools.py`: crea el esquema con la conexión SIN `search_path` --fijar
    `options=-csearch_path=pruebas` antes de que el esquema exista es innecesario arriesgar-- y
    deja `pruebas` listo (esquema + carga de la semilla) para todo lo que sigue."""
    print("Preparando el esquema de pruebas (no se toca 'public')\n")
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)
        persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())


def limpiar(directa: str) -> None:
    """Copiado de `probar_tools.py`: borra el esquema de pruebas, nunca `public`."""
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()
    print("\nEsquema de pruebas borrado.")


def crear_conversacion(conn) -> str:
    """El montaje mínimo de `contexto()` en `probar_tools.py`, sin ficha de paciente: a este
    script le basta una conversación sobre un teléfono de prueba fijo."""
    return persistencia.asegurar_conversacion(conn, telefono="573000000000")


def bloque_habil(jornada: Jornada, desde: datetime, dias_habiles: int) -> datetime:
    """El día número `dias_habiles` en que la clínica abre, contado hacia adelante desde
    `desde` sin contar a `desde` mismo -- fines de semana y domingo cerrado se saltan solos.

    Existe por la misma razón que `hora()` en `probar_tools.py:68-92`, y el precedente es el
    mismo: `desde + timedelta(days=N)` cae en domingo según el día de la semana en que alguien
    corra este script, y ESE día `dos_crear_cita_deja_su_recordatorio` devolvía `""` sin
    ningún `FALLA` -- las comprobaciones 3 y 4 desaparecían de la salida en silencio en vez de
    fallar, porque dependían de un `id_cita` que nunca llegó. Contando días HÁBILES en vez de
    días de calendario, el resultado deja de depender de qué día sea hoy.
    """
    candidato = desde
    habiles = 0
    while habiles < dias_habiles:
        candidato = candidato + timedelta(days=1)
        if jornada.cierre_de(candidato) is not None:
            habiles += 1
    return candidato


def uno_las_columnas_y_las_perillas(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'seguimientos' AND table_schema = current_schema()
            """
        )
        columnas = {f[0] for f in cur.fetchall()}
    esperadas = {"cita_id", "anulado_en", "motivo_anulacion", "intentos", "fallo"}
    operativa = persistencia.leer_configuracion(conn)
    ok = esperadas <= columnas and operativa.get("hora_recordatorio_vispera") == 18
    print(f"{marca(ok)} 1. la 017 dejo las columnas y las perillas")


def dos_crear_cita_deja_su_recordatorio(conn, ctx) -> str:
    inicio = bloque_habil(ctx.jornada, ctx.ahora, 3).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    texto = asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Paciente De Prueba",
                inicio=inicio,
                tratamiento="limpieza",
                clave_idempotencia="da-igual",
            ),
        )
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, cita_id, fecha_objetivo FROM seguimientos WHERE cita_id IS NOT NULL"
        )
        filas = cur.fetchall()
    ok = "Cita confirmada" in texto and len(filas) == 1
    print(f"{marca(ok)} 2. crear_cita dejo su recordatorio, sin que el modelo lo pidiera")
    # `cita_id` es UUID en Postgres y psycopg lo devuelve como `uuid.UUID`, no como `str`: se
    # convierte aquí, una sola vez, para que todo lo que sigue reciba lo mismo que recibiría
    # de la boca del modelo -- que solo conoce el id como texto.
    return str(filas[0][1]) if filas else ""


def tres_reprogramar_mueve_el_recordatorio(conn, ctx, id_cita: str) -> None:
    destino = bloque_habil(ctx.jornada, ctx.ahora, 4).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    asyncio.run(h._reprogramar_cita(ctx, id_cita, destino.isoformat()))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE anulado_en IS NOT NULL),
                   count(*) FILTER (WHERE anulado_en IS NULL)
              FROM seguimientos WHERE cita_id = %s
            """,
            (id_cita,),
        )
        anulados, vivos = cur.fetchone()
    ok = anulados == 1 and vivos == 1
    print(f"{marca(ok)} 3. reprogramar anulo el viejo ({anulados}) y creo uno nuevo ({vivos})")


def cuatro_cancelar_anula_el_recordatorio(conn, ctx, id_cita: str) -> None:
    asyncio.run(
        h._cancelar_cita(
            ctx,
            SolicitudCancelacion(id_cita=id_cita, motivo="prueba", clave_idempotencia=id_cita),
        )
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM seguimientos WHERE cita_id = %s AND anulado_en IS NULL",
            (id_cita,),
        )
        vivos = cur.fetchone()[0]
    ok = vivos == 0
    print(f"{marca(ok)} 4. cancelar dejo {vivos} recordatorios vivos (esperado 0)")


def cinco_el_despachador_decide_sin_enviar(conn, url: str, ctx) -> None:
    """Con `plantilla=""` el despachador corre entero y no manda nada. Es el modo con el que
    se cuelga en producción para ver que decide bien antes de arriesgar un WhatsApp.

    El RECUENTO por sí solo no demuestra nada: cuando `despachar` decide "enviar" y la
    plantilla está vacía, el código ni siquiera incrementa un contador --ver su docstring,
    "SIGUE pendiente -no se marca-, para cuando exista la plantilla"--, así que el recuento
    sale idéntico si no hubiera habido ninguna fila que mirar. Por eso las comprobaciones 2 a
    4 ya dejaron el número de esta cita SIN recordatorio vivo (se anuló al cancelar): esta
    función crea uno nuevo, propio, y lo que prueba de verdad es que sigue EXACTAMENTE igual
    -sin marcar, sin anular- después del ciclo con la plantilla apagada.
    """
    inicio = bloque_habil(ctx.jornada, ctx.ahora, 6).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    asyncio.run(
        h._crear_cita(
            ctx,
            SolicitudCita(
                nombre_completo="Paciente Del Modo Sin Enviar",
                inicio=inicio,
                tratamiento="limpieza",
                clave_idempotencia="prueba-modo-sin-enviar",
            ),
        )
    )
    with conn.cursor() as cur:
        # Filtrado por `c.inicio`, y no por "el único recordatorio vivo que quede": las
        # comprobaciones 2-4 dejan el suyo anulado en el camino feliz, pero si la 4 fallara
        # dejando uno huérfano vivo, un SELECT sin filtro ni ORDER BY podría recoger esa fila
        # ajena y esta comprobación mentiría en vez de fallar con claridad. `inicio` es el
        # mismo valor con el que se creó ESTA cita, en un bloque hábil que ninguna otra
        # comprobación usa.
        cur.execute(
            """
            SELECT s.id, s.fecha_objetivo
              FROM seguimientos s JOIN citas c ON c.id = s.cita_id
             WHERE c.inicio = %s AND s.anulado_en IS NULL AND s.enviado_en IS NULL
            """,
            (inicio,),
        )
        fila = cur.fetchone()
    if fila is None:
        # Mismo principio que el `if id_cita:` de `main()`: si la cita de esta comprobación
        # no dejó recordatorio, se dice con un FALLA y se sigue -- nunca un `TypeError` a
        # medio camino ni una comprobación que desaparece de la salida.
        print(
            f"{marca(False)} 5. el despachador decidio sin enviar: OMITIDA -- "
            "no se creo la cita de prueba"
        )
        return
    id_seguimiento, fecha_objetivo = fila
    # `fecha_objetivo` es TIMESTAMPTZ (migración 001) y psycopg la devuelve normalizada a
    # UTC, no en el huso con el que se calculó (el de `ctx.ahora`). `decidir()` compara
    # `ahora.hour` tal cual -- G5 mira si esa hora cae dentro de la ventana de la jornada--,
    # así que pasarla sin reconvertir corre el ciclo con la hora de reloj EQUIVOCADA: la
    # primera versión de esta comprobación mandaba una `fecha_objetivo` de las 15:00 -05:00
    # como si fueran las 20:00, la ventana la daba por cerrada, y la fila salía "aplazada"
    # en vez de "decidida y retenida por falta de plantilla" -- que es lo que esto demuestra.
    fecha_objetivo = fecha_objetivo.astimezone(ctx.ahora.tzinfo)

    recuento = asyncio.run(
        seguimientos.despachar(
            database_url=url,
            whatsapp=None,
            jornada=ctx.jornada,
            plantilla="",
            ahora=fecha_objetivo,
        )
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT enviado_en, anulado_en FROM seguimientos WHERE id = %s",
            (id_seguimiento,),
        )
        enviado_en, anulado_en = cur.fetchone()

    # Los cuatro contadores en cero es justo lo que deja el camino real: `decidir()` dice
    # "enviar" (todas sus guardas pasaron) y el propio `if not plantilla: ... continue` de
    # `despachar` no toca ninguno -- ver su docstring, "SIGUE pendiente -no se marca-". Un
    # `aplazados` o un `anulados` aquí significaría que la fila NUNCA llegó a esa rama, y
    # esta comprobación estaría demostrando otra cosa sin decirlo.
    ok = (
        recuento == {"enviados": 0, "anulados": 0, "aplazados": 0, "fallidos": 0}
        and enviado_en is None
        and anulado_en is None
    )
    print(
        f"{marca(ok)} 5. el despachador decidio sin enviar: {recuento} "
        f"(la fila sigue pendiente: enviado_en={enviado_en} anulado_en={anulado_en})"
    )


def seis_dos_despachadores_no_toman_la_misma_fila(url: str) -> None:
    """Lo que NINGUNA prueba offline caza, y la razon de que este script exista.

    Dos sesiones piden la cola a la vez. `FOR UPDATE ... SKIP LOCKED` tiene que darle la fila
    a una sola. Con el pooler esto pasa en verde sin probar nada: hace falta la conexion
    directa.
    """
    tomadas: list[list[int]] = []
    barrera = threading.Barrier(2)

    def pedir() -> None:
        with persistencia.conectar(url) as conn:
            conn.autocommit = False
            barrera.wait()
            filas = persistencia.seguimientos_por_despachar(
                conn, ahora=datetime.now(h.ZONA_BOGOTA) + timedelta(days=365)
            )
            tomadas.append([f["id"] for f in filas])
            # Se mantiene la transaccion abierta un instante: sin esto el bloqueo se suelta
            # antes de que la otra sesion llegue a pedir, y la prueba no prueba nada.
            import time

            time.sleep(0.5)
            conn.rollback()

    hilos = [threading.Thread(target=pedir) for _ in range(2)]
    for t in hilos:
        t.start()
    for t in hilos:
        t.join()

    compartidas = set(tomadas[0]) & set(tomadas[1]) if len(tomadas) == 2 else {"sin datos"}
    ok = not compartidas
    print(f"{marca(ok)} 6. dos despachadores tomaron filas distintas (comunes: {compartidas})")


def siete_marcar_dos_veces_no_duplica(conn, id_seguimiento: int) -> None:
    """El único guardián real contra el envío duplicado (ver el docstring de
    `persistencia.marcar_seguimiento_enviado`) y el que ninguna prueba había ejercitado nunca
    contra Postgres de verdad: `AND enviado_en IS NULL` en el `UPDATE`. Offline se dobla con un
    diccionario, y un diccionario no demuestra nada sobre una condición de carrera en SQL."""
    primera = persistencia.marcar_seguimiento_enviado(conn, id_seguimiento)
    segunda = persistencia.marcar_seguimiento_enviado(conn, id_seguimiento)
    ok = primera is True and segunda is False
    print(
        f"{marca(ok)} 7. marcar dos veces la misma fila: primera={primera} segunda={segunda} "
        f"(la segunda tiene que fallar)"
    )


def main() -> int:
    directa, url = url_de_pruebas()
    montar_esquema(directa, url)
    try:
        with persistencia.conectar(url) as conn:
            ctx = ContextoDaniela(
                id_conversacion=crear_conversacion(conn),
                telefono_completo="573000000000",
                database_url=url,
                calendario=CalendarioDoble(),
                # `h.ZONA_BOGOTA`, no la zona de la máquina: la clínica está en Bogotá, el
                # servidor no necesariamente. Este mismo script corre en el VPS (CLAUDE.md),
                # que casi siempre va en UTC -cinco horas por delante-, y con
                # `datetime.now().astimezone()` ese desfase se cuela a la vez en los bloques
                # hábiles, en la ventana de G5 y en la hora de víspera. Es el mismo defecto
                # de "depende de cuándo se corre" con otra variable: dónde.
                ahora=datetime.now(h.ZONA_BOGOTA).replace(
                    hour=9, minute=0, second=0, microsecond=0
                ),
                jornada=Jornada(),
            )
            uno_las_columnas_y_las_perillas(conn)
            id_cita = dos_crear_cita_deja_su_recordatorio(conn, ctx)
            if id_cita:
                tres_reprogramar_mueve_el_recordatorio(conn, ctx, id_cita)
                cuatro_cancelar_anula_el_recordatorio(conn, ctx, id_cita)
            else:
                # Sin cita no hay nada que mover ni que cancelar. Que esto se calle en vez
                # de fallar es justo el defecto que dejaba desaparecer dos comprobaciones de
                # siete sin que la corrida se viera roja: un `FALLA` explícito por cada una,
                # y el código de salida sigue siendo distinto de cero por `fallos`.
                print(f"{marca(False)} 3. reprogramar_cita: OMITIDA -- la 2 no dejo una cita")
                print(f"{marca(False)} 4. cancelar_cita: OMITIDA -- la 2 no dejo una cita")
            cinco_el_despachador_decide_sin_enviar(conn, url, ctx)
        seis_dos_despachadores_no_toman_la_misma_fila(url)

        # -- 7. el único guardián de no-duplicado que corre contra Neon de verdad ------------
        with persistencia.conectar(url) as conn:
            persistencia.insertar_seguimiento(
                conn,
                id_conversacion=ctx.id_conversacion,
                tipo="recordatorio_cita",
                fecha_objetivo=ctx.ahora + timedelta(days=1),
                clave_idempotencia="prueba-marcar-dos-veces",
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM seguimientos WHERE clave_idempotencia = %s",
                    ("prueba-marcar-dos-veces",),
                )
                id_seguimiento = cur.fetchone()[0]
            siete_marcar_dos_veces_no_duplica(conn, id_seguimiento)
    finally:
        limpiar(directa)
    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLA(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    inicio = ctx.ahora + timedelta(days=3)
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
    destino = (ctx.ahora + timedelta(days=4)).replace(hour=10, minute=0)
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


def cinco_el_despachador_decide_sin_enviar(url: str, ctx) -> None:
    """Con `plantilla=""` el despachador corre entero y no manda nada. Es el modo con el que
    se cuelga en produccion para ver que decide bien antes de arriesgar un WhatsApp."""
    recuento = asyncio.run(
        seguimientos.despachar(
            database_url=url,
            whatsapp=None,
            jornada=Jornada(),
            plantilla="",
            ahora=ctx.ahora + timedelta(days=30),
        )
    )
    ok = recuento["enviados"] == 0 and sum(recuento.values()) >= 0
    print(f"{marca(ok)} 5. el despachador decidio sin enviar: {recuento}")


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
                conn, ahora=datetime.now().astimezone() + timedelta(days=365)
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
                ahora=datetime.now().astimezone().replace(
                    hour=9, minute=0, second=0, microsecond=0
                ),
                jornada=Jornada(),
            )
            uno_las_columnas_y_las_perillas(conn)
            id_cita = dos_crear_cita_deja_su_recordatorio(conn, ctx)
            if id_cita:
                tres_reprogramar_mueve_el_recordatorio(conn, ctx, id_cita)
                cuatro_cancelar_anula_el_recordatorio(conn, ctx, id_cita)
        cinco_el_despachador_decide_sin_enviar(url, ctx)
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

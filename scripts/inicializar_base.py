"""Aplica el esquema en Neon y carga la base de conocimiento. Idempotente: se puede correr
todas las veces que haga falta.

    uv run python scripts/inicializar_base.py           # aplica esquema + carga + verifica
    uv run python scripts/inicializar_base.py --solo-verificar

Lee `MAXICARE_DATABASE_URL` de `.env` (que no se versiona) o del entorno del proceso. No
imprime la cadena de conexión ni ningún secreto.

------------------------------------------------------------------------------------------
Pone al día DOS esquemas, no uno
------------------------------------------------------------------------------------------
`public` --la base de la clínica-- y `pruebas_web` --el carril del chat del panel--. El
segundo no es un capricho de simetría: lo creaba solo `runtime._preparar_esquema_de_pruebas`,
que es PEREZOSO --corre la primera vez que alguien abre el chat web-- y nadie lo había
abierto desde que existen las migraciones 009 y 010. Medido el 13/09/2026 contra la base
real: `public` tenía 16 tablas y `pruebas_web` 14 --le faltaban `agent_sessions` y
`agent_messages`--, y a su `mensajes_entrantes` le faltaban cuatro columnas
(`conversacion_id`, `respondido_en`, `wamid_respuesta`, `fallo_respuesta`).

Lo que eso rompía no era el chat: era `/clearstate`, cuyo borrado secundario sobre
`pruebas_web` reventaba con `UndefinedColumn` mientras tres documentos afirmaban que
funcionaba. Que el carril de pruebas esté al día no puede depender de que alguien se acuerde
de abrir una pantalla: depende del despliegue, que es esto.

`--solo-verificar` NO escribe en ninguno de los dos: se limita a decir cuál está atrasado.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# La consola de Windows es cp1252 y revienta al imprimir cualquier cosa fuera de ese
# rango. Este script corre tanto aquí como en el VPS: se fuerza UTF-8 en los dos.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import persistencia  # noqa: E402
from maxicare_daniela.config import Config, cargar_dotenv  # noqa: E402


#: Las dos tablas que trajo la fase 7 (`migraciones/010_sesiones_agente.sql`). Se verifican
#: por nombre en los dos esquemas: sin ellas, el historial del diálogo no se persiste y el
#: turno de cada paciente muere con el proceso -- que es exactamente lo que la fase arregla.
TABLAS_DEL_HISTORIAL = ("agent_sessions", "agent_messages")

#: Las dos que trajo la 019. Se verifican por la misma razón y con el mismo alcance: sin
#: `contactos`, el primer mensaje de cada número revienta al leer el estado y el paciente
#: recibe el mensaje seguro; sin `consentimientos`, el aviso de la política sale y no queda
#: constancia de que salió, que es justo lo que hay que poder acreditar. Y van en los DOS
#: esquemas porque `/clearstate` toca los dos.
TABLAS_DEL_CONSENTIMIENTO = ("contactos", "consentimientos")


def _enmascarar(url: str) -> str:
    """Deja ver a qué host se conectó, nunca las credenciales."""
    try:
        resto = url.split("@", 1)[1]
        return "***@" + resto.split("?", 1)[0]
    except IndexError:
        return "..."


def _tablas_de(conn, esquema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (esquema,),
        )
        return {fila[0] for fila in cur.fetchall()}


def _existe_esquema(conn, esquema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (esquema,)
        )
        return cur.fetchone() is not None


def _poner_al_dia_pruebas_web(database_url: str) -> int:
    """Crea `pruebas_web` si falta, le aplica las migraciones y le carga la semilla.

    Hace lo mismo que `runtime._preparar_esquema_de_pruebas`, y a propósito: ese camino es
    perezoso --espera a que alguien abra el chat-- y este es el del despliegue. El que llegue
    primero deja al otro sin trabajo, porque las diez migraciones son idempotentes
    (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) y la carga es un UPSERT.

    No se importa `runtime` para reusar su función: importarlo levanta FastAPI y construye la
    app entera, y esto es un script de base de datos. Lo que sí se comparte es lo que puede
    derivar -- el nombre del esquema y la forma de la URL -- que viven en `persistencia`.
    """
    directa = persistencia.url_directa(database_url)
    esquema = persistencia.ESQUEMA_PRUEBAS_WEB
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {esquema}")
        conn.commit()

    url = persistencia.url_con_search_path(database_url, esquema)
    with persistencia.conectar(url) as conn:
        aplicadas = persistencia.aplicar_esquema(conn)
        total = persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())
    print(f"  esquema '{esquema}': {len(aplicadas)} migraciones aplicadas, {total} filas "
          f"de conocimiento")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--solo-verificar",
        action="store_true",
        help="No escribe nada: solo comprueba el estado actual de la base.",
    )
    args = parser.parse_args()

    cargar_dotenv()
    try:
        config = Config.desde_entorno()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Conectando a {_enmascarar(config.database_url)}")
    with persistencia.conectar(config.database_url) as conn:
        if not args.solo_verificar:
            aplicadas = persistencia.aplicar_esquema(conn)
            print(f"Esquema aplicado: {', '.join(aplicadas)}")

            semilla = persistencia.cargar_semilla()
            total = persistencia.cargar_base_conocimiento(conn, semilla)
            print(f"Base de conocimiento cargada: {total} filas")

            # El carril de pruebas del panel, que hasta hoy solo se ponia al dia si alguien
            # abria el chat web. Ver el docstring del modulo.
            print()
            print("Carril de pruebas del panel:")
            _poner_al_dia_pruebas_web(config.database_url)

        print()
        print("=" * 78)
        print("VERIFICACIÓN DEL ENTREGABLE DE LA FASE 1")
        print("=" * 78)

        # 1. La ausencia de dato es un dato explícito.
        for tratamiento in ("endodoncia", "protesis"):
            texto = persistencia.consultar_conocimiento(conn, tratamiento, "precio")
            ok = "SIN DATO DOCUMENTADO" in texto
            print(f"\n  consultar_base_conocimiento('{tratamiento}', 'precio')")
            print(f"  -> {'OK  ' if ok else 'FALLA'} {texto[:150]}...")
            if not ok:
                return 1

        # 2. Un dato aprobado sale limpio.
        texto = persistencia.consultar_conocimiento(conn, "cordales", "precio")
        ok = "$300.000" in texto and "SIN DATO" not in texto
        print("\n  consultar_base_conocimiento('cordales', 'precio')")
        print(f"  -> {'OK  ' if ok else 'FALLA'} {texto[:150]}")
        if not ok:
            return 1

        # 3. Un dato que existe pero MaxiCare no ha aprobado sale advertido.
        texto = persistencia.consultar_conocimiento(conn, "implantes", "garantia")
        ok = "PENDIENTE DE APROBACIÓN" in texto
        print("\n  consultar_base_conocimiento('implantes', 'garantia')")
        print(f"  -> {'OK  ' if ok else 'FALLA'} {texto[:150]}...")
        if not ok:
            return 1

        # 4. La configuración operativa quedó en la base, no en el código.
        cfg = persistencia.leer_configuracion(conn)
        print(f"\n  configuración operativa: {cfg}")

        # 5. El control de capacidad es real: el UNIQUE existe de verdad.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_constraint
                WHERE conname = 'uq_reservas_cupo'
                  AND contype = 'u'
                  AND connamespace = 'public'::regnamespace
                """
            )
            # Filtrado por esquema, y no `COUNT(*) == 1` a secas: los esquemas `pruebas` y
            # `pruebas_web` tienen su propia copia de la restricción, así que sin el filtro
            # esto contaba 3 y declaraba FALLA con la base perfectamente sana. Un
            # verificador que grita en rojo cuando todo está bien es uno al que nadie le va
            # a creer el día que tenga razón -- y este vigila el invariante que impide que
            # dos pacientes ocupen el mismo cupo.
            tiene_unique = cur.fetchone()[0] == 1
        print(f"\n  UNIQUE (inicio, cupo_num) en reservas → {'OK' if tiene_unique else 'FALLA'}")
        if not tiene_unique:
            return 1

        # ---------------------------------------------------------------------------
        # 6. Las tablas de la FASE 7, en los DOS esquemas.
        #
        # La verificación del lunes tiene que incluir lo que se despliega el lunes: sin
        # esto, `--solo-verificar` decía OK sobre una base sin la 010 aplicada, que es
        # justo el estado en que el historial no se persiste y el diálogo de cada
        # paciente muere con el proceso.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DEL ENTREGABLE DE LA FASE 7 (el historial persistido)")
        print("=" * 78)

        faltan_en_public = set(TABLAS_DEL_HISTORIAL) - _tablas_de(conn, "public")
        print(f"\n  public: {'OK  ' if not faltan_en_public else 'FALLA'} "
              + (", ".join(TABLAS_DEL_HISTORIAL) if not faltan_en_public
                 else f"faltan {sorted(faltan_en_public)} -- corre este script sin "
                      "--solo-verificar"))
        if faltan_en_public:
            return 1

        esquema_pruebas = persistencia.ESQUEMA_PRUEBAS_WEB
        if not _existe_esquema(conn, esquema_pruebas):
            # Legítimo en una base recién creada: el esquema lo crea este mismo script o
            # la primera apertura del chat. No es un fallo, es que no hay nada que mirar.
            print(f"\n  {esquema_pruebas}: no existe todavía (nada que verificar)")
        else:
            faltan_en_pruebas = set(TABLAS_DEL_HISTORIAL) - _tablas_de(conn, esquema_pruebas)
            al_dia = not faltan_en_pruebas
            print(f"\n  {esquema_pruebas}: {'OK  ' if al_dia else 'FALLA'} "
                  + (", ".join(TABLAS_DEL_HISTORIAL) if al_dia
                     else f"faltan {sorted(faltan_en_pruebas)} -- el carril del chat del "
                          "panel está atrasado y `/clearstate` fallará ahí; corre este "
                          "script sin --solo-verificar"))
            if not al_dia:
                return 1

        # ---------------------------------------------------------------------------
        # 7. Las tablas de la MIGRACIÓN 019 (contacto y consentimiento), en los dos
        #    esquemas. Mismo argumento que el bloque de arriba: la verificación del día
        #    del despliegue tiene que incluir lo que se despliega ese día.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DE LA 019 (contacto y consentimiento)")
        print("=" * 78)

        for esquema in ("public", esquema_pruebas):
            if esquema != "public" and not _existe_esquema(conn, esquema):
                print(f"\n  {esquema}: no existe todavía (nada que verificar)")
                continue
            faltan = set(TABLAS_DEL_CONSENTIMIENTO) - _tablas_de(conn, esquema)
            print(f"\n  {esquema}: {'OK  ' if not faltan else 'FALLA'} "
                  + (", ".join(TABLAS_DEL_CONSENTIMIENTO) if not faltan
                     else f"faltan {sorted(faltan)} -- corre este script sin "
                          "--solo-verificar"))
            if faltan:
                return 1

    print("\n" + "=" * 78)
    print("FASE 1 — segunda mitad: OK  ·  FASE 7 — el historial: OK  ·  019 — contacto y "
          "consentimiento: OK")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

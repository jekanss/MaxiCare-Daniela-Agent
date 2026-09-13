"""Cuanto historial gasta un turno de verdad, para poder fijar `LIMITE_HISTORIAL_SESION`.

    uv run python scripts/medir_historial.py

SOLO LEE. No escribe ni una fila, y mira `public` -- la base real de la clinica -- porque es
el unico sitio donde hay conversaciones reales que contar. Los esquemas de prueba no sirven:
sus turnos son los que alguien invento para una prueba.

Por que esto existe
------------------------------------------------------------------------------------------
El plan original decia `SessionSettings(limit=40)` y lo justificaba como «5 conversaciones
completas». Esa equivalencia es falsa: EL SDK CUENTA ITEMS, NO MENSAJES. Una llamada a tool y
su resultado son dos items, y un turno en que Daniela consulte el conocimiento, mire la
agenda y registre el estado gasta seis o siete el solo. Con el 40, «5 conversaciones» podian
ser cinco o seis TURNOS.

El orden del spec es obligado y este script es el paso 2:

    1. persistir SIN limite  ->  2. medir items/turno  ->  3. fijar el limite
       (fases 1-12)               (esto)                   con el dato al lado

Por que puede no medir nada, y por que eso NO es un fallo
------------------------------------------------------------------------------------------
Hacen falta AL MENOS VEINTE turnos reales. Con menos, el percentil no significa nada y
estariamos sustituyendo una suposicion por otra mas cara -- que es exactamente lo que el
`40` era--. Mientras no los haya, este script lo dice y `config.LIMITE_HISTORIAL_SESION` se
queda en `PENDIENTE`, o sea `None`, o sea el historial entero. Lo que desbloquea la medicion
es desplegar y dejar correr conversaciones reales, no correr esto otra vez.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()

from maxicare_daniela import persistencia  # noqa: E402

#: Por debajo de esto no se mide: se dice que no hay datos. Lo fija el spec de la fase, y la
#: razon es que un p95 sobre cuatro conversaciones es un numero con la misma autoridad que
#: el `40` que este script existe para sustituir.
MINIMO_TURNOS = 20

#: Los turnos que hay que poder recordar: una conversacion de agendamiento completa --saludo,
#: tratamiento, fecha, disponibilidad, nombre y consentimiento, confirmacion--.
TURNOS_A_RECORDAR = 6

#: La consulta del spec, tal cual. `turno_actual > 0` deja fuera las conversaciones que no
#: llegaron a tener un turno: son las que harian una division por cero.
CONSULTA = """
WITH por_sesion AS (
    SELECT session_id, count(*) AS items
      FROM agent_messages
     GROUP BY session_id
),
turnos AS (
    SELECT c.id::text AS session_id, c.turno_actual
      FROM conversaciones c
     WHERE c.turno_actual > 0
)
SELECT
    count(*)                                                      AS conversaciones,
    round(avg(p.items::numeric / t.turno_actual), 1)               AS items_por_turno_medio,
    max(p.items::numeric / t.turno_actual)                         AS items_por_turno_peor,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY p.items)          AS items_p95,
    max(p.items)                                                   AS items_max,
    max(t.turno_actual)                                            AS turnos_max
  FROM por_sesion p
  JOIN turnos t USING (session_id);
"""

#: El censo que decide si hay algo que medir. Va aparte para no tocar la consulta del spec.
CENSO = """
SELECT
    (SELECT count(*) FROM agent_messages)                            AS items,
    (SELECT count(*) FROM agent_sessions)                            AS sesiones,
    (SELECT count(*) FROM conversaciones WHERE turno_actual > 0)     AS conversaciones,
    (SELECT coalesce(sum(turno_actual), 0) FROM conversaciones)      AS turnos
"""


def enmascarar(url: str) -> str:
    """`***@host` -- una cadena de conexion no se imprime entera nunca."""
    if "@" not in url:
        return "***"
    return "***@" + url.split("@", 1)[1].split("?", 1)[0]


def numero(valor, decimales: int = 1) -> str:
    """El valor formateado, o `PENDIENTE` si Postgres devolvio NULL.

    Un `NULL` aqui no es un cero: es «no habia filas que agregar». Imprimir `None` a secas
    --o peor, un 0-- dejaria un numero con pinta de medido en un sitio donde no se midio
    nada, que es justo lo que la regla del `PENDIENTE` existe para impedir.
    """
    if valor is None:
        return "PENDIENTE"
    if isinstance(valor, int):
        return str(valor)
    return f"{float(valor):.{decimales}f}"


def main() -> int:
    url = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not url:
        print("ERROR: falta MAXICARE_DATABASE_URL en .env", file=sys.stderr)
        return 1

    print(f"Base: {enmascarar(url)}")
    # No dice «mira public» a secas porque no seria cierto: mira el esquema al que apunte la
    # URL, y asi es como se puede ensayar el camino CON datos sin inventarse filas en la base
    # de la clinica. En produccion esa URL es `public`, que es lo unico que hay que medir.
    print("Lee SOLO. No escribe nada. Mira el esquema de MAXICARE_DATABASE_URL "
          "(en produccion, 'public').")

    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute(CENSO)
        items, sesiones, conversaciones_con_turno, turnos = cur.fetchone()
        cur.execute(CONSULTA)
        fila = cur.fetchone()

    print("\n" + "=" * 78)
    print("Censo -- que hay ahi para medir")
    print("=" * 78)
    print(f"  filas en agent_messages          : {items}")
    print(f"  filas en agent_sessions          : {sesiones}")
    print(f"  conversaciones con al menos 1 turno: {conversaciones_con_turno}")
    print(f"  turnos reales acumulados         : {turnos}")

    if turnos < MINIMO_TURNOS or items == 0:
        print("\n" + "=" * 78)
        print("SIN DATOS: no hay nada que medir todavia")
        print("=" * 78)
        print(
            f"  Hacen falta al menos {MINIMO_TURNOS} turnos reales en public.agent_messages "
            f"y hay {turnos}."
        )
        if items == 0:
            print(
                "  `agent_messages` esta VACIA: el historial persistido todavia no ha "
                "corrido en produccion."
            )
        print(
            "  Con menos, el percentil no significa nada y el numero que saliera de aqui\n"
            "  seria otra suposicion, mas cara que la que sustituye."
        )
        print(
            f"\n  LIMITE_HISTORIAL_SESION = PENDIENTE (hoy `None`: el historial va entero,\n"
            f"  que es lo correcto mientras no haya medicion).\n"
            f"  Lo desbloquea DESPLEGAR y dejar correr conversaciones reales; despues,\n"
            f"  volver a correr este script."
        )
        return 0

    (
        conversaciones,
        items_por_turno_medio,
        items_por_turno_peor,
        items_p95,
        items_max,
        turnos_max,
    ) = fila

    print("\n" + "=" * 78)
    print("Medido")
    print("=" * 78)
    print(f"  conversaciones                : {numero(conversaciones)}")
    print(f"  items por turno, media        : {numero(items_por_turno_medio)}")
    print(f"  items por turno, peor caso    : {numero(items_por_turno_peor)}")
    print(f"  items por conversacion, p95   : {numero(items_p95)}")
    print(f"  items por conversacion, maximo: {numero(items_max)}")
    print(f"  turnos, maximo                : {numero(turnos_max)}")

    if items_por_turno_peor is None:
        print(
            "\n  El peor caso vino NULL: hay turnos contados pero ninguna sesion que cruce "
            "con ellos.\n  No se recomienda ningun numero."
        )
        return 0

    peor = math.ceil(float(items_por_turno_peor))
    print("\n" + "=" * 78)
    print("Recomendacion, con su aritmetica a la vista")
    print("=" * 78)
    print(f"  items/turno (peor caso medido): {peor}")
    print(f"  turnos que hay que recordar:    {TURNOS_A_RECORDAR}"
          "   <- una conversacion de agendamiento completa")
    print("  ------------------------------------")
    print(f"  LIMITE_HISTORIAL_SESION =      {peor * TURNOS_A_RECORDAR}")
    print(
        "\n  Los CUATRO numeros de arriba --media, peor caso, p95 y maximo-- van al\n"
        "  docstring de `config.LIMITE_HISTORIAL_SESION`, en el sitio de los `PENDIENTE`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

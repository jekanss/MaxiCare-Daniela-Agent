"""Cuanto historial gasta un turno de verdad, para poder fijar `LIMITE_HISTORIAL_SESION`.

    uv run python scripts/medir_historial.py

SOLO LEE. No escribe ni una fila. Mira el esquema al que apunte `MAXICARE_DATABASE_URL`, que
en produccion es `public` -- la base real de la clinica, y el unico sitio donde hay
conversaciones reales que contar; los turnos de un esquema de prueba son los que alguien
invento para una prueba, y no miden nada--. Que sea la URL y no un `public` escrito a fuego
es lo que permite ensayar el camino CON datos contra un esquema desechable sin inventarse
filas en la base de la clinica, que es como se comprobo que la consulta de aqui corre.

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
Hacen falta AL MENOS VEINTE turnos repartidos en CINCO conversaciones, y los dos numeros se
cuentan sobre el conjunto que de verdad se va a medir: conversaciones con filas en
`agent_messages`. NO sobre `sum(conversaciones.turno_actual)`, que es lo que contaba la
primera version de este script y cuenta lo que no es -- las conversaciones anteriores a la
fase 7 no tienen ni un item que medir y empujaban la puerta igual. Ver el comentario del
`CENSO`.

Con menos, el percentil no significa nada y estariamos sustituyendo una suposicion por otra
mas cara -- que es exactamente lo que el `40` era--. Lo que desbloquea la medicion es
desplegar y dejar correr conversaciones reales, no correr esto otra vez.

Mientras tanto, `config.LIMITE_HISTORIAL_SESION` NO vale `None`: vale un TOPE DE SEGURIDAD
(230 items) derivado del techo de tokens de la cuenta, no de ninguna medicion de uso. Los dos
numeros son distintos y no se confunden: el tope solo impide que el historial crezca hasta
reventar la peticion --y con ella al paciente, que se queda recibiendo el mensaje seguro para
siempre--; el limite que mide ESTE script optimiza coste y sera mas pequeno. Que el tope
exista no cierra la 13b.
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
#:
#: Se cuentan los turnos MEDIBLES --los de conversaciones que tienen filas en
#: `agent_messages`--, no los de la tabla `conversaciones`. Ver el comentario del `CENSO`.
MINIMO_TURNOS = 20

#: La otra mitad de la puerta, y no sale del spec: la pone este script. `percentile_cont(0.95)`
#: sobre dos o tres conversaciones es el maximo con otro nombre --interpola entre los valores
#: mas altos y no hay cola que recortar--, asi que veinte turnos repartidos entre dos
#: conversaciones largas seguirian sin dar un p95 con sentido. Cinco es el minimo elegido
#: aqui, no un numero medido; si resulta corto, subirlo es barato y bajarlo es lo caro.
MINIMO_CONVERSACIONES = 5

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
#:
#: Las dos ultimas columnas son la puerta, y salen del MISMO `JOIN` que la consulta de arriba:
#: son las conversaciones y los turnos que de verdad se van a medir. Contar
#: `sum(turno_actual)` de TODAS las conversaciones --que es lo que hacia antes-- cuenta lo que
#: no es: las conversaciones anteriores a la fase 7 no tienen ni un item en `agent_messages`
#: y empujaban la puerta sin aportar nada medible, y una sola conversacion con
#: `turno_actual >= 20` la abria ella sola -- con lo que el p95 saldria sobre una o dos
#: conversaciones, que es exactamente lo que la puerta existe para impedir.
CENSO = """
WITH por_sesion AS (
    SELECT session_id, count(*) AS items
      FROM agent_messages
     GROUP BY session_id
),
turnos AS (
    SELECT c.id::text AS session_id, c.turno_actual
      FROM conversaciones c
     WHERE c.turno_actual > 0
),
medible AS (
    SELECT t.turno_actual
      FROM por_sesion p
      JOIN turnos t USING (session_id)
)
SELECT
    (SELECT count(*) FROM agent_messages)                            AS items,
    (SELECT count(*) FROM agent_sessions)                            AS sesiones,
    (SELECT count(*) FROM conversaciones WHERE turno_actual > 0)     AS conversaciones,
    (SELECT coalesce(sum(turno_actual), 0) FROM conversaciones)      AS turnos_en_la_base,
    (SELECT count(*) FROM medible)                                   AS medibles,
    (SELECT coalesce(sum(turno_actual), 0) FROM medible)             AS turnos_medibles
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
        (
            items,
            sesiones,
            conversaciones_con_turno,
            turnos_en_la_base,
            medibles,
            turnos_medibles,
        ) = cur.fetchone()
        cur.execute(CONSULTA)
        fila = cur.fetchone()

    print("\n" + "=" * 78)
    print("Censo -- que hay ahi para medir")
    print("=" * 78)
    print(f"  filas en agent_messages                  : {items}")
    print(f"  filas en agent_sessions                  : {sesiones}")
    print(f"  conversaciones con al menos 1 turno      : {conversaciones_con_turno}")
    print(f"  turnos en la tabla `conversaciones`      : {turnos_en_la_base}"
          "   <- NO es lo que se mide")
    print(f"  conversaciones MEDIBLES (con items)      : {medibles}"
          "   <- la puerta cuenta estas dos")
    print(f"  turnos MEDIBLES (de esas conversaciones) : {turnos_medibles}")

    if turnos_medibles < MINIMO_TURNOS or medibles < MINIMO_CONVERSACIONES:
        print("\n" + "=" * 78)
        print("SIN DATOS: no hay nada que medir todavia")
        print("=" * 78)
        print(
            f"  Hacen falta al menos {MINIMO_TURNOS} turnos y {MINIMO_CONVERSACIONES} "
            f"conversaciones CON HISTORIAL GUARDADO --o sea, con filas en agent_messages--\n"
            f"  y hay {turnos_medibles} "
            f"{'turno' if turnos_medibles == 1 else 'turnos'} en {medibles} "
            f"{'conversacion' if medibles == 1 else 'conversaciones'}."
        )
        if turnos_en_la_base > turnos_medibles:
            print(
                f"  Los {turnos_en_la_base} turnos de la tabla `conversaciones` NO cuentan "
                f"para esto: {turnos_en_la_base - turnos_medibles} son de conversaciones "
                f"sin un solo item\n  en `agent_messages` -- anteriores a la fase 7, o de "
                f"un carril que no persiste."
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
            "\n  El LIMITE MEDIDO sigue en PENDIENTE. Lo que hay hoy en\n"
            "  config.LIMITE_HISTORIAL_SESION es otra cosa: un TOPE DE SEGURIDAD de 230\n"
            "  items, derivado del techo de tokens de la cuenta, que solo impide que el\n"
            "  historial crezca hasta reventar la peticion. No sale de ninguna medicion de\n"
            "  uso y no cierra esta tarea.\n"
            "  Lo desbloquea DESPLEGAR y dejar correr conversaciones reales; despues,\n"
            "  volver a correr este script."
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

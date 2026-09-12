"""Aplica el esquema en Neon y carga la base de conocimiento. Idempotente: se puede correr
todas las veces que haga falta.

    uv run python scripts/inicializar_base.py           # aplica esquema + carga + verifica
    uv run python scripts/inicializar_base.py --solo-verificar

Lee `MAXICARE_DATABASE_URL` de `.env` (que no se versiona) o del entorno del proceso. No
imprime la cadena de conexión ni ningún secreto.
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


def _enmascarar(url: str) -> str:
    """Deja ver a qué host se conectó, nunca las credenciales."""
    try:
        resto = url.split("@", 1)[1]
        return "***@" + resto.split("?", 1)[0]
    except IndexError:
        return "..."


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
                WHERE conname = 'uq_reservas_cupo' AND contype = 'u'
                """
            )
            tiene_unique = cur.fetchone()[0] == 1
        print(f"\n  UNIQUE (inicio, cupo_num) en reservas → {'OK' if tiene_unique else 'FALLA'}")
        if not tiene_unique:
            return 1

    print("\n" + "=" * 78)
    print("FASE 1 — segunda mitad: OK")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

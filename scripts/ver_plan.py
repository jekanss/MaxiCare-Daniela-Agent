"""Imprime una sola clave de `plan-agentes.json` en vez de leerlo entero.

    uv run python scripts/ver_plan.py                 # que claves hay y cuanto pesa cada una
    uv run python scripts/ver_plan.py fases
    uv run python scripts/ver_plan.py herramientas crear_cita
    uv run python scripts/ver_plan.py fases 4
    uv run python scripts/ver_plan.py --brief usuario_canal

El plan pasa de 21.000 tokens y crece en cada fase, porque ahi se van incrustando los
hallazgos verificados. Leerlo entero para consultar una decision cuesta el contexto de media
sesion; casi siempre lo que se necesita es una clave, a veces un solo elemento.

Esto no es un atajo de comodidad: es la diferencia entre poder consultar el diseño diez veces
en una sesion o dos. Un diseño que sale caro de consultar acaba no consultandose, y entonces
se rediseña por encima de decisiones que ya estaban cerradas -- que es justo lo que el
`CLAUDE.md` pide no hacer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAIZ = Path(__file__).resolve().parents[1]
PLAN = RAIZ / "docs" / "agentes" / "plan-agentes.json"
BRIEF = RAIZ / "docs" / "agentes" / "brief-agentes.json"

#: Las claves de `plan-agentes.json` que son listas de cosas con nombre. Para estas, el
#: segundo argumento filtra por `nombre` (o por `orden`, en `fases`).
LISTAS_CON_NOMBRE = {"agentes", "herramientas", "contratos", "guardrails", "fases"}


def tokens(texto: str) -> int:
    """Estimacion a 4 bytes por token. Es una estimacion y se presenta como tal."""
    return len(texto.encode("utf-8")) // 4


def indice(datos: dict, archivo: Path) -> int:
    """Sin argumentos: que hay dentro y cuanto cuesta cada cosa."""
    entero = json.dumps(datos, ensure_ascii=False)
    print(f"{archivo.name} — ~{tokens(entero):,} tokens estimados en total\n")
    print(f"{'clave':18} {'tokens':>8}  contenido")
    print("-" * 74)
    for clave, valor in datos.items():
        bruto = json.dumps(valor, ensure_ascii=False)
        if isinstance(valor, list):
            nombres = [
                str(e.get("nombre") or e.get("nombre_modelo") or e.get("orden", "?"))
                for e in valor
                if isinstance(e, dict)
            ]
            detalle = f"{len(valor)} elementos: " + ", ".join(nombres)
        elif isinstance(valor, dict):
            detalle = "claves: " + ", ".join(valor)
        else:
            detalle = str(valor)
        print(f"{clave:18} {tokens(bruto):>8}  {detalle[:46]}")
    print()
    print("Pide una clave: uv run python scripts/ver_plan.py fases")
    print("O un elemento : uv run python scripts/ver_plan.py herramientas crear_cita")
    return 0


def main() -> int:
    argumentos = sys.argv[1:]
    archivo = PLAN
    if argumentos and argumentos[0] == "--brief":
        archivo, argumentos = BRIEF, argumentos[1:]

    if not archivo.is_file():
        print(f"ERROR: no existe {archivo}", file=sys.stderr)
        return 1
    datos = json.loads(archivo.read_text(encoding="utf-8"))

    if not argumentos:
        return indice(datos, archivo)

    clave = argumentos[0]
    if clave not in datos:
        print(f"ERROR: '{clave}' no es una clave de {archivo.name}.", file=sys.stderr)
        print(f"       Son: {', '.join(datos)}", file=sys.stderr)
        return 1

    valor = datos[clave]

    # Con un segundo argumento se baja a un solo elemento. Es lo que casi siempre se busca:
    # «que decidimos sobre crear_cita», no «dame las nueve herramientas».
    if len(argumentos) > 1 and clave in LISTAS_CON_NOMBRE and isinstance(valor, list):
        buscado = argumentos[1]
        encontrados = [
            e
            for e in valor
            if str(e.get("nombre", "")) == buscado
            or str(e.get("nombre_modelo", "")) == buscado
            or str(e.get("orden", "")) == buscado
        ]
        if not encontrados:
            disponibles = [
                str(e.get("nombre") or e.get("nombre_modelo") or e.get("orden")) for e in valor
            ]
            print(f"ERROR: no hay '{buscado}' en {clave}.", file=sys.stderr)
            print(f"       Hay: {', '.join(disponibles)}", file=sys.stderr)
            return 1
        valor = encontrados[0] if len(encontrados) == 1 else encontrados

    salida = json.dumps(valor, ensure_ascii=False, indent=1)
    print(salida)
    print(f"\n[{tokens(salida):,} tokens estimados · el archivo entero son "
          f"{tokens(json.dumps(datos, ensure_ascii=False)):,}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

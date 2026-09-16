"""Comprueba que lo que Daniela no pudo resolver acaba en una fila, agrupado.

    uv run python scripts/probar_sin_resolver.py

No gasta un solo token: nunca se instancia un Agent y nunca se llama a OpenAI. Escribe en el
esquema de pruebas y lo borra al terminar; jamas toca `public`.

La comprobacion 4 es la que justifica el script: un tripwire que se regenero bien HOY no deja
absolutamente ningun rastro --`Resultado.tripwires` muere dentro del proceso-- y es la senal
de calidad mas frecuente del sistema. Si alguien vuelve a descartarla, esta linea lo dice.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import persistencia, reseteo, sin_resolver  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402
from maxicare_daniela.sin_resolver import Senal  # noqa: E402

# Propio, no compartido con `probar_tools.py` ni con `probar_recordatorios.py`: los tres
# escriben con `DROP SCHEMA ... CASCADE` al montar y al limpiar, y correrlos a la vez sobre
# el mismo nombre hace que uno le borre el esquema al otro a mitad de corrida --se midio: 22
# "FALLA" de `UndefinedTable` que no eran ninguna regresion, solo dos scripts pisandose.
ESQUEMA = "pruebas_sin_resolver"
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
    print("Preparando el esquema de pruebas (no se toca 'public')\n")
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
    print("\nEsquema de pruebas borrado.")


def fila(conn, huella: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT contador, escalo, ejemplos FROM casos_sin_resolver WHERE huella = %s",
            (huella,),
        )
        f = cur.fetchone()
    return {} if not f else {"contador": f[0], "escalo": f[1], "ejemplos": json.loads(f[2])}


def cuantas(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM casos_sin_resolver")
        return cur.fetchone()[0]


def volcar(conn, *, senales, tripwires, escalado_por, motivo, frase, telefono):
    """Lo mismo que hace `_anotar_resultado`, sin montar un turno entero."""
    for caso in sin_resolver.casos_del_turno(
        senales=senales, tripwires=tripwires, escalado_por=escalado_por,
        motivo=motivo, frase=frase,
    ):
        persistencia.registrar_caso(
            conn, huella=caso.huella, tipo=caso.tipo, escalo=caso.escalo,
            ejemplo=caso.ejemplo, telefono=telefono,
        )


def main() -> int:
    directa, url = url_de_pruebas()
    montar_esquema(directa, url)
    try:
        with persistencia.conectar(url) as conn:
            # 1
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'casos_sin_resolver'",
                    (ESQUEMA,),
                )
                columnas = cur.fetchone()[0]
            print(f"{marca(columnas >= 11)} 1. la 018 dejo la tabla ({columnas} columnas)")

            # 2
            volcar(conn, senales=[Senal("ortodoncia", "precio", hubo_dato=False)],
                   tripwires=[], escalado_por="dato_faltante", motivo=None,
                   frase="cuanto sale la ortodoncia en cuotas", telefono="+573001112233")
            f = fila(conn, "falta_dato:ortodoncia:precio")
            ok = f.get("contador") == 1 and f.get("escalo") == 1
            print(f"{marca(ok)} 2. un SIN DATO DOCUMENTADO deja una fila (escalo={f.get('escalo')})")

            # 3
            for i in range(6):
                volcar(conn, senales=[Senal("ortodoncia", "precio", hubo_dato=False)],
                       tripwires=[], escalado_por=None, motivo=None,
                       frase=f"pregunta {i}", telefono=f"+57300111223{i}")
            f = fila(conn, "falta_dato:ortodoncia:precio")
            ok = f.get("contador") == 7 and len(f.get("ejemplos", [])) == 5
            print(f"{marca(ok)} 3. siete turnos = UNA fila "
                  f"(contador={f.get('contador')}, ejemplos={len(f.get('ejemplos', []))})")

            # 4 -- LA QUE IMPORTA
            volcar(conn, senales=[Senal("profilaxis", "precio", hubo_dato=True)],
                   tripwires=["sin_cifra_no_documentada"], escalado_por=None, motivo=None,
                   frase="cuanto vale la limpieza", telefono="+573004445566")
            f = fila(conn, "guardrail:sin_cifra_no_documentada:profilaxis")
            print(f"{marca(bool(f))} 4. un tripwire REGENERADO deja fila "
                  f"(hoy, sin esto, no dejaba nada)")

            # 5
            antes = cuantas(conn)
            volcar(conn, senales=[], tripwires=[], escalado_por=None,
                   motivo="relevo: la tiene @doctora", frase="hola", telefono="+57300")
            print(f"{marca(cuantas(conn) == antes)} 5. un motivo 'relevo:' NO deja fila")

            # 6
            with conn.cursor() as cur:
                cur.execute("UPDATE casos_sin_resolver SET ultima_vez = now() - "
                            "interval '90 days' WHERE huella = %s",
                            ("guardrail:sin_cifra_no_documentada:profilaxis",))
            conn.commit()
            recientes = [c["huella"] for c in persistencia.casos_recientes(conn, dias=30)]
            ok = ("guardrail:sin_cifra_no_documentada:profilaxis" not in recientes
                  and recientes and recientes[0] == "falta_dato:ortodoncia:precio")
            print(f"{marca(ok)} 6. la ventana hunde lo viejo y ordena por frecuencia "
                  f"({len(recientes)} en la ventana)")

            # 7
            original = persistencia.registrar_caso

            def revienta(*a, **k):
                raise RuntimeError("la base se cayo")

            persistencia.registrar_caso = revienta
            try:
                volcar(conn, senales=[Senal("x", "y", hubo_dato=False)], tripwires=[],
                       escalado_por=None, motivo=None, frase="z", telefono="+57300")
                propago = False
            except RuntimeError:
                propago = True
            finally:
                persistencia.registrar_caso = original
            print(f"{marca(propago)} 7. el fallo SI sale de `volcar`, el doble local de "
                  f"este script (el `try` real de `_anotar_resultado` lo cubre "
                  f"`test_atencion.py::test_un_caso_que_revienta_no_le_quita_la_respuesta_"
                  f"a_nadie`)")

            # 8 -- por el camino REAL de /clearstate: `persistencia.borrar_rastro`, no el
            # `olvidar_ejemplos_de` suelto que la propia funcion dice que /clearstate NO usa.
            f_antes = fila(conn, "falta_dato:ortodoncia:precio")
            borrado = persistencia.borrar_rastro(conn, "+573001112233")
            f_despues = fila(conn, "falta_dato:ortodoncia:precio")
            etiqueta = reseteo.ETIQUETAS_DE_TABLA.get("casos_sin_resolver")
            ok = (
                f_despues["contador"] == f_antes["contador"]
                and not any(e.get("telefono") == "+573001112233"
                            for e in f_despues["ejemplos"])
                # La clave tiene que estar EN el dict que borrar_rastro le devuelve a
                # /clearstate, o el paciente no se entera de que se le borro una frase.
                and "casos_sin_resolver" in borrado
                and borrado["casos_sin_resolver"] >= 1
                # Y esa clave tiene que tener etiqueta legible en ETIQUETAS_DE_TABLA, sin
                # guion bajo: sin ella el paciente recibe "1 en casos_sin_resolver" por
                # WhatsApp, el nombre interno de una tabla de la clinica, no suya.
                and etiqueta is not None
                and "_" not in etiqueta[0]
                and "_" not in etiqueta[1]
            )
            print(f"{marca(ok)} 8. borrar_rastro (el camino real de /clearstate) borra la "
                  f"frase, NO baja el contador, y trae etiqueta legible "
                  f"({f_antes['contador']} -> {f_despues['contador']}, "
                  f"casos_sin_resolver={borrado.get('casos_sin_resolver')}, "
                  f"etiqueta={etiqueta})")
    finally:
        limpiar(directa)

    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLA(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())

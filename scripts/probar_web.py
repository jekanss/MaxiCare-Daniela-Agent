"""El cascaron web de extremo a extremo: ingreso, navegacion y chat contra Daniela.

    uv run python scripts/probar_web.py            # todo menos el chat
    uv run python scripts/probar_web.py --chat     # tambien el chat. GASTA TOKENS.

Es el entregable de la fase 5. Habla con la aplicacion en el mismo proceso --sin abrir un
puerto-- pero contra la Neon de verdad: crea un usuario temporal, entra con el, comprueba
que la puerta cierra, y lo borra pase lo que pase.

Con `--chat` manda un mensaje real al mismo objeto `agentes.daniela` que atiende WhatsApp.
Eso cuesta unos centavos de dolar y es lo unico que demuestra comportamiento y no cableado:
`tests/test_web.py` prueba las rutas con un modelo de mentira.

El chat escribe en el esquema `pruebas_web`, NUNCA en `public`. Ninguna cita de aqui ocupa
un cupo de la clinica ni aparece en la agenda de nadie.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()

from fastapi.testclient import TestClient  # noqa: E402

from maxicare_daniela import autenticacion, persistencia, runtime  # noqa: E402

#: Prefijo `zzz.` para que salga al final de `--listar` y se note de lejos si alguna vez
#: sobrevive a un corte de luz a mitad del script.
TEMPORAL = "zzz.verificacion.temporal"
CLAVE = "clave de verificacion temporal 2026"

fallos = 0


def revisar(etiqueta: str, condicion: bool, detalle: str = "") -> None:
    global fallos
    marca = "OK  " if condicion else "FALLA"
    print(f"  {marca} {etiqueta}" + (f" -- {detalle}" if detalle else ""))
    if not condicion:
        fallos += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Entregable de la fase 5.")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Manda un mensaje real a Daniela. Gasta tokens de verdad.",
    )
    args = parser.parse_args()

    if not runtime.config.secreto_sesion:
        print("FALLA: falta MAXICARE_SECRETO_SESION en .env. El panel esta apagado.")
        print('       Genera uno: python -c "import secrets; print(secrets.token_urlsafe(48))"')
        return 1

    url = runtime.config.database_url

    print("\n1. Esquema y usuario temporal")
    with persistencia.conectar(url) as conn:
        aplicadas = persistencia.aplicar_esquema(conn)
        revisar("006_usuarios.sql aplicada", "006_usuarios.sql" in aplicadas)
        persistencia.crear_usuario(
            conn,
            usuario=TEMPORAL,
            nombre="Verificacion Temporal",
            hash_contrasena=autenticacion.hash_contrasena(CLAVE),
            rol="recepcion",
        )
        fila = persistencia.buscar_usuario(conn, TEMPORAL)
        revisar("el usuario quedo activo", bool(fila and fila["activo"]))
        revisar(
            "el hash no contiene la contrasena",
            CLAVE not in (fila or {}).get("hash_contrasena", ""),
        )

    try:
        cliente = TestClient(runtime.app)

        print("\n2. La puerta cierra")
        revisar("/api/sesion sin cookie da 401", cliente.get("/api/sesion").status_code == 401)
        revisar(
            "/api/pruebas/chat sin cookie da 401",
            cliente.post("/api/pruebas/chat", json={"mensaje": "hola"}).status_code == 401,
        )

        print("\n3. Ingreso")
        malo = cliente.post("/api/entrar", json={"usuario": TEMPORAL, "contrasena": "equivocada"})
        inexistente = cliente.post("/api/entrar", json={"usuario": "no.existe", "contrasena": "x"})
        revisar("contrasena equivocada da 401", malo.status_code == 401)
        revisar("no se distingue de un usuario inexistente", malo.json() == inexistente.json())

        bueno = cliente.post("/api/entrar", json={"usuario": TEMPORAL, "contrasena": CLAVE})
        revisar("credenciales correctas dan 200", bueno.status_code == 200, str(bueno.status_code))
        revisar("la cookie es HttpOnly", "httponly" in bueno.headers.get("set-cookie", "").lower())
        revisar(
            "la cookie es SameSite=Lax", "samesite=lax" in bueno.headers.get("set-cookie", "").lower()
        )

        sesion = cliente.get("/api/sesion")
        revisar("la sesion se reconoce", sesion.status_code == 200)
        revisar("devuelve el usuario correcto", sesion.json().get("usuario") == TEMPORAL)

        print("\n4. Quitar el acceso corta la sesion en curso")
        with persistencia.conectar(url) as conn:
            persistencia.cambiar_acceso(conn, TEMPORAL, activo=False)
        revisar("la misma cookie ya no vale", cliente.get("/api/sesion").status_code == 401)
        with persistencia.conectar(url) as conn:
            persistencia.cambiar_acceso(conn, TEMPORAL, activo=True)

        print("\n5. El frontend compilado")
        pagina = cliente.get("/agenda")
        compilado = pagina.status_code == 200
        revisar(
            "/agenda devuelve la pagina del panel",
            compilado,
            "corre: cd web && npm install && npm run build" if not compilado else "",
        )
        if compilado:
            revisar("el HTML enlaza el bundle", "/assets/" in pagina.text)
        revisar(
            "una ruta de api inexistente devuelve JSON, no HTML",
            cliente.get("/api/no-existe").headers["content-type"].startswith("application/json"),
        )

        print("\n6. El carril de pruebas esta separado")
        de_pruebas = runtime._url_de_pruebas()
        revisar("apunta a pruebas_web", "pruebas_web" in de_pruebas)
        revisar("no es el esquema que borran los otros scripts", runtime.ESQUEMA_PRUEBAS_WEB != "pruebas")
        revisar("usa la conexion directa, sin el pooler", "-pooler." not in de_pruebas)

        if args.chat:
            print("\n7. Chat contra la Daniela de produccion (GASTA TOKENS)")
            cliente.post("/api/entrar", json={"usuario": TEMPORAL, "contrasena": CLAVE})
            r = cliente.post(
                "/api/pruebas/chat",
                json={"mensaje": "Hola, cuanto cuesta un implante dental?", "conversacion": None},
            )
            revisar("la peticion responde 200", r.status_code == 200, str(r.status_code)[:200])
            if r.status_code == 200:
                d = r.json()
                print(f"\n       Daniela: {d['mensaje']}\n")
                revisar("hay mensaje para el paciente", bool(d["mensaje"].strip()))
                revisar("trae el estado de la oportunidad", bool(d["estado_oportunidad"]))
                revisar("trae el identificador de conversacion", bool(d["conversacion"]))
                if d["tripwires"]:
                    print(f"       (saltaron guardrails: {', '.join(d['tripwires'])})")

                # La mitad que de verdad importa del carril separado: que no haya aterrizado
                # nada en `public`.
                with persistencia.conectar(url) as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM public.conversaciones WHERE telefono LIKE 'web-%'"
                    )
                    en_publico = cur.fetchone()[0]
                revisar("no se escribio NADA en public", en_publico == 0, f"{en_publico} filas")
        else:
            print("\n7. Chat -- omitido. Anade --chat para probarlo (gasta tokens).")

    finally:
        with persistencia.conectar(url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM usuarios WHERE usuario = %s", (TEMPORAL,))
            conn.commit()
            quedo = persistencia.buscar_usuario(conn, TEMPORAL)
        revisar("usuario temporal borrado", quedo is None)

    print("\n" + "=" * 78)
    print("FASE 5 -- cascaron web: " + ("OK" if fallos == 0 else f"{fallos} FALLAS"))
    print("=" * 78)
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

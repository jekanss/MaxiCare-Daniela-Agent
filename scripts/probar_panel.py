"""El entregable de la fase 8, primera mitad: el panel de tratamientos de punta a punta.

    uv run python scripts/probar_panel.py            # sin gastar un token
    uv run python scripts/probar_panel.py --chat     # tambien el modelo de verdad. GASTA TOKENS.

Por que el script tiene DOS mitades y no una
------------------------------------------------------------------------------------------
Los endpoints del panel (`PUT /api/conocimiento`, `POST /api/tratamientos`...) escriben
contra `config.database_url`, el esquema `public` -- los datos REALES de la clinica. El chat
de pruebas (`/api/pruebas/chat`) arma su `ContextoDaniela` con
`runtime._preparar_esquema_de_pruebas()`, que apunta al esquema `pruebas_web` -- una copia
propia de la base de conocimiento, cargada de la misma semilla.

Son dos bases distintas. Cambiar un precio por la API y preguntarle de inmediato a Daniela
en el chat de pruebas le haria citar el valor VIEJO -- porque Daniela, en el chat, lee
`pruebas_web`, no `public` -- y de paso habria dejado cambiado un precio real de la clinica
sin nada que lo revirtiera.

Por eso:

    MITAD A -- por HTTP, contra `public`, SIN gastar tokens.
               Prueba el camino panel -> base -> tool: que un cambio por la API llegue a la
               MISMA funcion (`persistencia.consultar_conocimiento`) que usa la tool de
               Daniela, que quede en la bitacora, y que los roles esten cerrados donde deben.

    MITAD B -- el comportamiento real del modelo, contra `pruebas_web`, solo con `--chat`.
               Escribe el precio DIRECTO con `panel.guardar_ficha` sobre una conexion a
               `pruebas_web` y comprueba que Daniela, en el chat de pruebas, lo cotiza.

LA REGLA DURA: `public` queda EXACTAMENTE como estaba. El precio de `implantes` se restaura
en un `finally` pase lo que pase, y la restauracion se comprueba con una asercion al final,
no se da por hecha. El script nunca crea el tratamiento `carillas` en `public` -- solo prueba
que intentarlo con una clave invalida ('Carillas', con mayuscula) da el 400 esperado; a
`carillas` de verdad solo se le da vida en `pruebas_web`, y se borra ahi mismo al terminar.

Y el punto que mas importa del script entero: crear un tratamiento nuevo NO abre el muro.
`LecturaArchivo(tratamiento="carillas")` tiene que seguir lanzando `ValidationError` despues
de que `carillas` ya es un tratamiento activo y cotizable -- ese `Literal` no se toca desde
ninguna pantalla.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from maxicare_daniela import autenticacion, contratos, panel, persistencia, runtime  # noqa: E402

#: Prefijo `zzz.` para que salgan al final de `--listar` y se noten de lejos si alguna vez
#: sobreviven a un corte de luz a mitad del script.
ADMIN = "zzz.panel.admin.temporal"
RECEPCION = "zzz.panel.recepcion.temporal"
CLAVE = "clave de verificacion temporal 2026"

fallos = 0


def revisar(etiqueta: str, condicion: bool, detalle: str = "") -> None:
    global fallos
    marca = "OK  " if condicion else "FALLA"
    print(f"  {marca} {etiqueta}" + (f" -- {detalle}" if detalle else ""))
    if not condicion:
        fallos += 1


def _solo_digitos(texto: str) -> str:
    return "".join(c for c in texto if c.isdigit())


def _precio_de_prueba() -> tuple[str, str]:
    """Un numero de 6 digitos al azar y el texto de ficha que lo contiene.

    Al azar y no una secuencia -- para no toparse por casualidad con un precio real que ya
    este documentado en otro concepto, lo que produciria un falso positivo al buscar el
    numero dentro de lo que responde Daniela.

    El contenido NO lleva ningun aviso de que es un valor de prueba. La primera version de
    este script si lo llevaba ("no es un precio real de la clinica") y Daniela, con toda
    razon, se negaba a citarlo: su instruccion es no inventar ni afirmar un precio que la
    propia ficha dice que no es real. La trazabilidad de que esto fue una prueba no vive en
    el contenido -- vive en `cambios_configuracion`, donde queda el usuario `zzz.*` que lo
    escribio y el valor exacto que puso.
    """
    numero = str(random.randint(100_000, 999_999))
    contenido = f"$ {numero} COP."
    return numero, contenido


def main() -> int:
    parser = argparse.ArgumentParser(description="Entregable de la fase 8 (primera mitad).")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Tambien prueba el chat contra la Daniela de produccion. Gasta tokens de verdad.",
    )
    args = parser.parse_args()

    if not runtime.config.secreto_sesion:
        print("FALLA: falta MAXICARE_SECRETO_SESION en .env. El panel esta apagado.")
        print('       Genera uno: python -c "import secrets; print(secrets.token_urlsafe(48))"')
        return 1

    url_public = runtime.config.database_url

    print("\n0. Estado inicial de public -- lo que hay que dejar exactamente igual")
    with persistencia.conectar(url_public) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tratamientos")
            conteo_tratamientos_antes = cur.fetchone()[0]
        filas_antes = persistencia.leer_conocimiento(conn, "implantes", "precio")
    precio_anterior = filas_antes[0] if filas_antes else None
    revisar("hay una ficha de implantes/precio que respaldar", precio_anterior is not None)

    print("\n1. Dos usuarios temporales: uno admin, uno recepcion")
    with persistencia.conectar(url_public) as conn:
        persistencia.crear_usuario(
            conn, usuario=ADMIN, nombre="Verificacion Panel Admin",
            hash_contrasena=autenticacion.hash_contrasena(CLAVE), rol="admin",
        )
        persistencia.crear_usuario(
            conn, usuario=RECEPCION, nombre="Verificacion Panel Recepcion",
            hash_contrasena=autenticacion.hash_contrasena(CLAVE), rol="recepcion",
        )
        fila_admin = persistencia.buscar_usuario(conn, ADMIN)
        fila_recepcion = persistencia.buscar_usuario(conn, RECEPCION)
    revisar(
        "el admin temporal quedo activo con rol admin",
        bool(fila_admin and fila_admin["activo"] and fila_admin["rol"] == "admin"),
    )
    revisar(
        "el recepcion temporal quedo activo con rol recepcion",
        bool(fila_recepcion and fila_recepcion["activo"] and fila_recepcion["rol"] == "recepcion"),
    )

    precio_pruebas_anterior = None

    try:
        cliente_admin = TestClient(runtime.app)
        cliente_recepcion = TestClient(runtime.app)

        print("\n" + "=" * 78)
        print("MITAD A -- por HTTP, contra public, sin gastar un token")
        print("=" * 78)

        print("\n2. Entra el admin temporal")
        r = cliente_admin.post("/api/entrar", json={"usuario": ADMIN, "contrasena": CLAVE})
        revisar("el admin entra", r.status_code == 200, str(r.status_code))

        print("\n3. PUT /api/conocimiento cambia el precio de implantes a un valor unico")
        numero_nuevo, precio_nuevo = _precio_de_prueba()
        cuerpo_ficha = {
            "tratamiento": "implantes",
            "concepto": "precio",
            "contenido": precio_nuevo,
            "aprobado": precio_anterior.aprobado if precio_anterior else True,
            "nota_pendiente": precio_anterior.nota_pendiente if precio_anterior else None,
        }
        r = cliente_admin.put("/api/conocimiento", json=cuerpo_ficha)
        revisar("PUT /api/conocimiento responde 200", r.status_code == 200, str(r.status_code)[:200])
        revisar(
            "devuelve el contenido nuevo",
            r.status_code == 200 and r.json().get("contenido") == precio_nuevo,
        )

        print("\n4. La MISMA funcion que usa la tool de Daniela ya ve el valor nuevo")
        with persistencia.conectar(url_public) as conn:
            texto_tool = persistencia.consultar_conocimiento(conn, "implantes", "precio")
        revisar(
            "persistencia.consultar_conocimiento devuelve el precio nuevo",
            numero_nuevo in texto_tool,
            texto_tool[:150],
        )

        print("\n5. GET /api/historial trae el cambio con el usuario correcto")
        r = cliente_admin.get("/api/historial")
        revisar("historial responde 200", r.status_code == 200, str(r.status_code))
        cambios = r.json().get("cambios", []) if r.status_code == 200 else []
        ultimo = cambios[0] if cambios else {}
        revisar(
            "el cambio mas reciente es base_conocimiento/implantes/precio",
            ultimo.get("tabla") == "base_conocimiento" and ultimo.get("clave") == "implantes/precio",
            str(ultimo)[:200],
        )
        revisar("el cambio quedo a nombre del admin temporal", ultimo.get("usuario") == ADMIN)
        revisar("el historial trae el valor nuevo", ultimo.get("valor_nuevo") == precio_nuevo)

        print("\n6. Un usuario de rol recepcion recibe 403 al intentar el mismo PUT")
        r = cliente_recepcion.post("/api/entrar", json={"usuario": RECEPCION, "contrasena": CLAVE})
        revisar("recepcion entra", r.status_code == 200, str(r.status_code))
        r = cliente_recepcion.put(
            "/api/conocimiento", json=cuerpo_ficha | {"contenido": precio_nuevo + " (intento no autorizado)"}
        )
        revisar("recepcion recibe 403 al escribir conocimiento", r.status_code == 403, str(r.status_code))

        print("\n7. POST /api/tratamientos con una clave invalida da 400 legible")
        r = cliente_admin.post("/api/tratamientos", json={"clave": "Carillas", "etiqueta": "Carillas"})
        revisar("clave con mayuscula ('Carillas') da 400", r.status_code == 400, str(r.status_code))
        detalle = r.json().get("detalle", "") if r.status_code == 400 else ""
        revisar(
            "el 400 trae un mensaje legible sobre la clave",
            "clave" in detalle.lower() and "carillas" in detalle.lower(),
            detalle[:200],
        )

        if args.chat:
            print("\n" + "=" * 78)
            print("MITAD B -- el modelo de verdad, contra pruebas_web (GASTA TOKENS)")
            print("=" * 78)

            print("\n8. El precio de implantes se escribe DIRECTO en pruebas_web (panel.guardar_ficha)")
            url_pruebas = runtime._preparar_esquema_de_pruebas()
            with persistencia.conectar(url_pruebas) as conn:
                filas_pruebas_antes = persistencia.leer_conocimiento(conn, "implantes", "precio")
                precio_pruebas_anterior = filas_pruebas_antes[0] if filas_pruebas_antes else None
                numero_implantes_chat, precio_implantes_chat = _precio_de_prueba()
                panel.guardar_ficha(
                    conn, tratamiento="implantes", concepto="precio", contenido=precio_implantes_chat,
                    aprobado=True, nota_pendiente=None, usuario=ADMIN,
                )

            print("\n9. Le pregunta a Daniela cuanto cuesta un implante")
            r = cliente_admin.post(
                "/api/pruebas/chat",
                json={"mensaje": "Hola, cuanto cuesta un implante dental?", "conversacion": None},
            )
            revisar("la peticion de implantes responde 200", r.status_code == 200, str(r.status_code)[:200])
            if r.status_code == 200:
                d = r.json()
                print(f"\n       Daniela (implantes): {d['mensaje']}\n")
                revisar(
                    "Daniela cita el precio NUEVO de implantes",
                    numero_implantes_chat in _solo_digitos(d["mensaje"]),
                    d["mensaje"][:300],
                )
                if d["tripwires"]:
                    print(f"       (saltaron guardrails: {', '.join(d['tripwires'])})")

            print("\n10. Se crea 'carillas' en pruebas_web (panel.crear_tratamiento) y se le pone precio")
            with persistencia.conectar(url_pruebas) as conn:
                panel.crear_tratamiento(conn, clave="carillas", etiqueta="Carillas", usuario=ADMIN)
                # `Daniela` lee este vocabulario en cada turno (`agentes.instrucciones_daniela`);
                # sin refrescarlo aqui, no sabria que 'carillas' ya es algo que puede cotizar --
                # es exactamente lo que hace `runtime._refrescar_vocabulario` tras el POST real.
                contratos.fijar_vocabulario(panel.vocabulario_activo(conn))
                numero_carillas_chat, precio_carillas_chat = _precio_de_prueba()
                panel.guardar_ficha(
                    conn, tratamiento="carillas", concepto="precio", contenido=precio_carillas_chat,
                    aprobado=True, nota_pendiente=None, usuario=ADMIN,
                )

            print("\n11. Le pregunta a Daniela cuanto cuestan las carillas")
            r = cliente_admin.post(
                "/api/pruebas/chat",
                json={"mensaje": "Hola, cuanto cuestan las carillas?", "conversacion": None},
            )
            revisar("la peticion de carillas responde 200", r.status_code == 200, str(r.status_code)[:200])
            if r.status_code == 200:
                d = r.json()
                print(f"\n       Daniela (carillas): {d['mensaje']}\n")
                revisar(
                    "Daniela cotiza el tratamiento recien creado (carillas)",
                    numero_carillas_chat in _solo_digitos(d["mensaje"]),
                    d["mensaje"][:300],
                )
                if d["tripwires"]:
                    print(f"       (saltaron guardrails: {', '.join(d['tripwires'])})")
        else:
            print("\nMITAD B -- omitida. Anade --chat para probarla (gasta tokens de verdad).")

        print("\n12. Crear un tratamiento NO abrio el muro: LecturaArchivo sigue rechazando 'carillas'")
        muro_sigue_cerrado = False
        try:
            contratos.LecturaArchivo(
                tipo_documento="otro",
                tratamiento="carillas",
                confianza="alta",
                contexto_clinico="prueba de probar_panel.py -- no es un documento real",
            )
        except ValidationError:
            muro_sigue_cerrado = True
        revisar("LecturaArchivo(tratamiento='carillas') sigue lanzando ValidationError", muro_sigue_cerrado)

    finally:
        print("\n" + "=" * 78)
        print("Limpieza -- public tiene que quedar exactamente como estaba")
        print("=" * 78)

        # 1. Restaurar (o borrar, si no existia) el precio de implantes en `public`.
        try:
            with persistencia.conectar(url_public) as conn:
                if precio_anterior is not None:
                    panel.guardar_ficha(
                        conn, tratamiento="implantes", concepto="precio",
                        contenido=precio_anterior.contenido, aprobado=precio_anterior.aprobado,
                        nota_pendiente=precio_anterior.nota_pendiente, usuario=ADMIN,
                    )
                else:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM base_conocimiento WHERE tratamiento = %s AND concepto = %s",
                            ("implantes", "precio"),
                        )
                    conn.commit()
        except Exception as e:  # noqa: BLE001
            print(f"  (no se pudo restaurar el precio de implantes en public: {e})")

        # 2. Limpiar 'carillas' y el precio de implantes en pruebas_web, si la MITAD B corrio.
        if args.chat:
            try:
                with persistencia.conectar(runtime._url_de_pruebas()) as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM base_conocimiento WHERE tratamiento = %s", ("carillas",))
                        cur.execute("DELETE FROM tratamientos WHERE clave = %s", ("carillas",))
                    conn.commit()
                    if precio_pruebas_anterior is not None:
                        panel.guardar_ficha(
                            conn, tratamiento="implantes", concepto="precio",
                            contenido=precio_pruebas_anterior.contenido,
                            aprobado=precio_pruebas_anterior.aprobado,
                            nota_pendiente=precio_pruebas_anterior.nota_pendiente,
                            usuario=ADMIN,
                        )
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM tratamientos WHERE clave = %s", ("carillas",))
                        quedan = cur.fetchone()[0]
                revisar("no quedo 'carillas' en pruebas_web", quedan == 0)
            except Exception as e:  # noqa: BLE001
                print(f"  (no se pudo limpiar pruebas_web: {e})")

        # 3. El vocabulario vivo del proceso vuelve a reflejar solo lo activo en public.
        try:
            with persistencia.conectar(url_public) as conn:
                contratos.fijar_vocabulario(panel.vocabulario_activo(conn))
        except Exception as e:  # noqa: BLE001
            print(f"  (no se pudo refrescar el vocabulario: {e})")

        # 4. Borrar los dos usuarios temporales.
        try:
            with persistencia.conectar(url_public) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM usuarios WHERE usuario IN (%s, %s)", (ADMIN, RECEPCION))
                conn.commit()
                quedo_admin = persistencia.buscar_usuario(conn, ADMIN)
                quedo_recepcion = persistencia.buscar_usuario(conn, RECEPCION)
            revisar("usuario admin temporal borrado", quedo_admin is None)
            revisar("usuario recepcion temporal borrado", quedo_recepcion is None)
        except Exception as e:  # noqa: BLE001
            revisar("usuarios temporales borrados", False, str(e))

        # 5. LA REGLA DURA, verificada -- no dada por hecha.
        try:
            with persistencia.conectar(url_public) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM tratamientos")
                    conteo_tratamientos_despues = cur.fetchone()[0]
                revisar(
                    "public.tratamientos tiene las mismas filas que al empezar",
                    conteo_tratamientos_despues == conteo_tratamientos_antes,
                    f"antes={conteo_tratamientos_antes} despues={conteo_tratamientos_despues}",
                )
                filas_despues = persistencia.leer_conocimiento(conn, "implantes", "precio")
                precio_despues = filas_despues[0] if filas_despues else None
                if precio_anterior is not None:
                    revisar(
                        "el precio de implantes en public quedo igual que antes de empezar",
                        precio_despues is not None and precio_despues.contenido == precio_anterior.contenido,
                    )
                else:
                    revisar(
                        "no quedo una ficha de implantes/precio que no existia antes",
                        precio_despues is None,
                    )
        except Exception as e:  # noqa: BLE001
            revisar("restauracion de public verificada", False, str(e))

    print("\n" + "=" * 78)
    print("FASE 8 (primera mitad) -- panel de tratamientos: " + ("OK" if fallos == 0 else f"{fallos} FALLAS"))
    print("=" * 78)
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

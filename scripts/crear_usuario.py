"""Crea (o le restablece la contraseña a) un usuario de la interfaz web.

    uv run python scripts/crear_usuario.py
    uv run python scripts/crear_usuario.py --usuario ana.rodriguez --nombre "Dra. A. Rodriguez" --rol admin
    uv run python scripts/crear_usuario.py --listar
    uv run python scripts/crear_usuario.py --quitar-acceso pedro.gomez

Sin argumentos pregunta lo que falte. La contraseña **nunca** se pasa por la línea de
comandos: se escribe con `getpass`, que no la muestra en pantalla y no la deja en el
historial del shell. Un `--contrasena` sería cómodo y quedaría escrito en `.bash_history` y
en la lista de procesos de la máquina.

La migración 006 se aplica sola si hace falta, porque un script que falla con «no existe la
tabla usuarios» y te deja averiguar cuál migración correr no ayuda a nadie.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import persistencia  # noqa: E402
from maxicare_daniela.autenticacion import (  # noqa: E402
    MINIMO_CONTRASENA,
    ContrasenaInvalida,
    hash_contrasena,
)
from maxicare_daniela.config import Config, cargar_dotenv  # noqa: E402

ROLES = ("admin", "doctor", "recepcion")


def _preguntar(etiqueta: str, *, defecto: str = "") -> str:
    sufijo = f" [{defecto}]" if defecto else ""
    respuesta = input(f"{etiqueta}{sufijo}: ").strip()
    return respuesta or defecto


def _pedir_contrasena() -> str:
    """Dos veces, y comparadas. Una sola sería un usuario creado con una contraseña que
    nadie sabe, descubierto en el primer intento de entrar."""
    while True:
        primera = getpass.getpass(f"Contrasena (minimo {MINIMO_CONTRASENA} caracteres): ")
        segunda = getpass.getpass("Repitela: ")
        if primera != segunda:
            print("  -> No coinciden. Otra vez.\n")
            continue
        try:
            return hash_contrasena(primera)
        except ContrasenaInvalida as e:
            print(f"  -> {e}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Usuarios de la interfaz web de MaxiCare.")
    parser.add_argument("--usuario", default="", help="Nombre de ingreso, en minusculas.")
    parser.add_argument("--nombre", default="", help="Como se muestra en pantalla.")
    parser.add_argument("--rol", default="", choices=("", *ROLES))
    parser.add_argument("--listar", action="store_true", help="Muestra los usuarios y sale.")
    parser.add_argument("--quitar-acceso", default="", metavar="USUARIO")
    parser.add_argument("--dar-acceso", default="", metavar="USUARIO")
    args = parser.parse_args()

    cargar_dotenv()
    try:
        config = Config.desde_entorno()
    except RuntimeError as e:
        print(f"FALLA: {e}")
        return 1

    with persistencia.conectar(config.database_url) as conn:
        # Idempotente: vuelve a aplicar las seis migraciones, todas con IF NOT EXISTS.
        persistencia.aplicar_esquema(conn)

        if args.listar:
            usuarios = persistencia.listar_usuarios(conn)
            if not usuarios:
                print("No hay usuarios. Nadie puede entrar al panel todavia.")
                return 0
            print(f"{'usuario':22} {'rol':10} {'estado':9} nombre")
            print("-" * 72)
            for u in usuarios:
                estado = "activo" if u["activo"] else "SIN ACCESO"
                print(f"{u['usuario']:22} {u['rol']:10} {estado:9} {u['nombre']}")
            return 0

        if args.quitar_acceso or args.dar_acceso:
            objetivo = args.quitar_acceso or args.dar_acceso
            dar = bool(args.dar_acceso)
            if persistencia.cambiar_acceso(conn, objetivo, activo=dar):
                print(f"OK · {objetivo} {'puede entrar' if dar else 'YA NO puede entrar'}.")
                if not dar:
                    # Honestidad sobre el límite real del diseño, en el momento en que
                    # importa: quien quita un acceso necesita saber esto, no leerlo después
                    # en un docstring.
                    print(
                        "     Su sesion abierta sigue firmada hasta que venza (8 h como "
                        "maximo), pero cada peticion comprueba esta marca, asi que el corte "
                        "es inmediato."
                    )
                return 0
            print(f"FALLA: no existe el usuario {objetivo!r}.")
            return 1

        usuario = args.usuario or _preguntar("Usuario (ej. ana.rodriguez)")
        if not usuario:
            print("FALLA: hace falta un nombre de usuario.")
            return 1
        usuario = usuario.strip().lower()

        existente = persistencia.buscar_usuario(conn, usuario)
        if existente:
            print(f"\n'{usuario}' ya existe ({existente['nombre']}, {existente['rol']}).")
            print("Continuar le RESTABLECE la contrasena.")
            if _preguntar("Seguir? (s/n)", defecto="n").lower() not in ("s", "si", "y"):
                print("Cancelado. No se escribio nada.")
                return 0

        nombre = args.nombre or _preguntar(
            "Nombre para mostrar", defecto=(existente or {}).get("nombre", "")
        )
        rol = args.rol or _preguntar(
            f"Rol {ROLES}", defecto=(existente or {}).get("rol", "recepcion")
        )
        if rol not in ROLES:
            print(f"FALLA: el rol tiene que ser uno de {ROLES}.")
            return 1

        hash_guardado = _pedir_contrasena()
        id_usuario = persistencia.crear_usuario(
            conn, usuario=usuario, nombre=nombre, hash_contrasena=hash_guardado, rol=rol
        )

    print(f"\nOK · usuario #{id_usuario} '{usuario}' ({rol}) listo.")
    print("     Entra en /entrar de la interfaz web.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

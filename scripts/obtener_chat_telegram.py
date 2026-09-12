"""Averigua el ID del grupo de los doctores en Telegram y comprueba que sirve.

Uso, durante la configuración y cada vez que el grupo cambie:

    uv run python scripts/obtener_chat_telegram.py

Requiere `MAXICARE_TELEGRAM_BOT_TOKEN` en `.env`, que el bot ya esté dentro del grupo, y que
alguien haya escrito al menos un mensaje ahí después de agregarlo. Telegram no expone el ID
de un chat hasta que hay actividad.

No imprime el token. Sí comprueba cuatro cosas que después cuestan caro si están mal:

1. Que el modo privacidad esté DESACTIVADO. Con el modo privacidad activo, el bot solo ve
   los mensajes que empiezan con `/`. El relevo del plan (bloque 9) necesita que Daniela lea
   lo que el doctor escribe en texto normal para reenviárselo literal al paciente: con
   privacidad activa, ese modo no funciona y el fallo es silencioso.

2. Que el chat sea un SUPERGRUPO CON TEMAS (`is_forum`). Todo el diseño del relevo se apoya
   en que cada paciente tenga su propio tema: sin temas no hay dónde separar la discusión
   interna de los doctores del hilo que lee el paciente.

3. Que el bot sea administrador y tenga `can_manage_topics`. Sin ese permiso no puede abrir,
   reabrir ni —lo que importa— CERRAR el tema de un paciente al terminar el relevo.

4. Que ningún doctor sea administrador. Telegram permite escribir en un tema cerrado a quien
   es administrador, así que un doctor con ese rol rompe la única garantía dura del diseño:
   que al cerrar el relevo nadie pueda seguir escribiéndole al paciente.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

API = "https://api.telegram.org/bot{token}/{metodo}"


def _llamar(token: str, metodo: str, **parametros) -> dict:
    url = API.format(token=token, metodo=metodo)
    if parametros:
        url += "?" + urllib.parse.urlencode(parametros)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _revisar_grupo(token: str, chat_id: int, id_del_bot: int) -> bool:
    """Comprueba los puntos 2, 3 y 4 del docstring. Devuelve True si el grupo sirve."""
    sirve = True

    chat = _llamar(token, "getChat", chat_id=chat_id)
    if not chat.get("ok"):
        print(f"  !! Telegram no deja consultar este chat: {chat.get('description')}")
        return False
    chat = chat["result"]

    es_foro = bool(chat.get("is_forum"))
    print(f"  Temas activados (is_forum): {'SI' if es_foro else 'NO'}")
    if not es_foro:
        sirve = False
        print("     Arreglo: en el grupo -> Editar -> activa 'Temas'.")
        print("     Telegram convierte el grupo en supergrupo y el ID CAMBIA; vuelve a")
        print("     correr este script despues para capturar el nuevo.")

    miembro = _llamar(token, "getChatMember", chat_id=chat_id, user_id=id_del_bot)
    miembro = miembro.get("result", {})
    es_admin = miembro.get("status") == "administrator"
    gestiona = bool(miembro.get("can_manage_topics"))
    print(f"  El bot es administrador: {'SI' if es_admin else 'NO'}")
    print(f"  El bot puede gestionar temas: {'SI' if gestiona else 'NO'}")
    if not gestiona:
        sirve = False
        print("     Sin este permiso el bot no puede CERRAR el tema al terminar el relevo,")
        print("     y cerrarlo es lo unico que impide que un doctor le siga escribiendo al")
        print("     paciente cuando ya no debe.")
        print("     Arreglo: Editar -> Administradores -> el bot -> activa 'Gestionar temas'.")

    admins = _llamar(token, "getChatAdministrators", chat_id=chat_id).get("result", [])
    humanos = [a for a in admins if not a["user"].get("is_bot")]
    creadores = [a for a in humanos if a["status"] == "creator"]
    otros = [a for a in humanos if a["status"] != "creator"]

    print(f"  Administradores humanos: {len(humanos)}")
    for a in humanos:
        u = a["user"]
        quien = "@" + u["username"] if u.get("username") else u.get("first_name", "?")
        print(f"     - {quien} ({a['status']})")

    if otros:
        sirve = False
        print()
        print("     !! Hay doctores con rol de administrador.")
        print("        Un administrador SI puede escribir en un tema cerrado, asi que puede")
        print("        escribirle al paciente fuera de un relevo activo sin darse cuenta.")
        print("        Arreglo: Editar -> Administradores -> quitarles el rol. Como miembros")
        print("        normales conservan todo lo que necesitan para trabajar.")
    if creadores:
        print()
        print("     Nota: el creador del grupo no se puede degradar (Telegram no lo permite).")
        print("     Es la unica excepcion y hay que saberla: quien creo el grupo si puede")
        print("     escribir en un tema cerrado. Si eso llega a estorbar, la salida es que")
        print("     el grupo lo cree una cuenta que no atienda pacientes.")

    return sirve


def main() -> int:
    cargar_dotenv()
    token = os.environ.get("MAXICARE_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "ERROR: falta MAXICARE_TELEGRAM_BOT_TOKEN en .env\n"
            "Créalo con @BotFather (/newbot) y pega el token en la línea correspondiente.",
            file=sys.stderr,
        )
        return 1

    # --- quién es el bot, y el chequeo de privacidad ---
    yo = _llamar(token, "getMe")
    if not yo.get("ok"):
        print(f"ERROR: Telegram rechazó el token: {yo.get('description')}", file=sys.stderr)
        return 1
    bot = yo["result"]
    print(f"Bot: @{bot['username']}  ({bot.get('first_name', '')})")

    # `can_read_all_group_messages` es True solo si el modo privacidad está DESACTIVADO.
    lee_todo = bot.get("can_read_all_group_messages", False)
    print(f"Modo privacidad desactivado: {'SI' if lee_todo else 'NO'}")
    if not lee_todo:
        print()
        print("  !! El modo privacidad esta ACTIVO.")
        print("     El bot solo vera los mensajes que empiecen con '/'.")
        print("     El relevo doctor -> paciente NO va a funcionar, y va a fallar en")
        print("     silencio: nadie va a ver un error, simplemente Daniela no reenviara")
        print("     lo que el doctor escriba.")
        print()
        print("     Arreglo: en @BotFather -> /setprivacy -> elige el bot -> Disable.")
        print("     Despues QUITA el bot del grupo y vuelve a agregarlo: el cambio no")
        print("     aplica a los grupos en los que ya estaba.")
        print()

    # --- los chats donde hubo actividad ---
    updates = _llamar(token, "getUpdates")
    if not updates.get("ok"):
        print(f"ERROR: {updates.get('description')}", file=sys.stderr)
        return 1

    chats: dict[int, dict] = {}
    for u in updates["result"]:
        for clave in ("message", "edited_message", "channel_post", "my_chat_member"):
            if clave in u and "chat" in u[clave]:
                c = u[clave]["chat"]
                chats[c["id"]] = c

    if not chats:
        print()
        print("No hay actividad reciente. Haz esto y vuelve a correr el script:")
        print("  1. Agrega el bot al grupo de los doctores.")
        print("  2. Escribe cualquier mensaje en ese grupo.")
        print()
        print("Nota: Telegram solo guarda las actualizaciones unas 24 horas, y si otro")
        print("proceso ya las consumio, no apareceran aqui.")
        return 1

    print()
    print("Chats donde el bot tiene actividad:")
    print()
    print("%-18s %-12s %s" % ("ID", "TIPO", "NOMBRE"))
    print("-" * 60)
    grupos = []
    for cid, c in chats.items():
        tipo = c.get("type", "?")
        nombre = c.get("title") or c.get("username") or c.get("first_name") or ""
        print("%-18s %-12s %s" % (cid, tipo, nombre))
        if tipo in ("group", "supergroup"):
            grupos.append((cid, nombre, tipo))
    print("-" * 60)
    print()

    if not grupos:
        print("  !! Solo hay chats privados, ningun grupo.")
        print("     El plan manda los escalamientos a 'los doctores' en plural y cualquiera")
        print("     de ellos puede tomar la conversacion. Eso pide un grupo.")
        print("     Crea el grupo, agrega el bot, escribe un mensaje y vuelve a correr esto.")
        return 1

    # Un grupo al que se le activaron los temas deja atrás su ID viejo, pero Telegram sigue
    # reportando los dos en `getUpdates`. El supergrupo es siempre el vigente: elegir el
    # primero de la lista devolvería el ID muerto.
    grupos.sort(key=lambda g: g[2] != "supergroup")
    cid, nombre, tipo = grupos[0]

    print(f"Grupo elegido: {nombre}  ({tipo})")
    sirve = _revisar_grupo(token, cid, bot["id"])

    print()
    print("Pon esto en tu .env:")
    print()
    print(f"    MAXICARE_TELEGRAM_CHAT_DOCTORES={cid}")
    print()
    if len(grupos) > 1:
        print(f"    (hay {len(grupos)} grupos con actividad; si el elegido no es el correcto,")
        print("     usa el ID que corresponda de la tabla de arriba)")
        print()

    if not sirve:
        print("FALTA algo de lo de arriba. Corrigelo y vuelve a correr este script.")
        return 1
    print("El grupo cumple todo lo que el diseño del relevo necesita.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

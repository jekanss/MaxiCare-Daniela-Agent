"""Registra (o quita) el webhook de Telegram. Es lo que enciende el relevo de la fase 6C.

    uv run python scripts/configurar_webhook_telegram.py --estado
    uv run python scripts/configurar_webhook_telegram.py --url https://daniela.maxicarecol.com/webhook/telegram
    uv run python scripts/configurar_webhook_telegram.py --quitar

NO gasta tokens: no llama al modelo ni una vez. Solo Telegram.

==========================================================================================
POR QUE HACE FALTA ESTO Y NO BASTA CON DESPLEGAR
==========================================================================================
Meta VALIDA la URL del webhook: le mandas la direccion en su consola, te llama con un
`hub.challenge` y queda suscrita. Telegram no hace nada de eso. Hasta que alguien llame a
`setWebhook`, el bot no manda un solo update y el boton «Hablar yo con el paciente» sigue
siendo decorativo -- sin un error en ningun log, porque no hay nada que fallar.

==========================================================================================
EL SECRETO: VACIO SIGNIFICA CERRADO
==========================================================================================
`/webhook/telegram` es la unica puerta del sistema por la que algo de fuera puede hacer que
el bot le escriba al WhatsApp de un paciente. El webhook de Meta se defiende con una firma
HMAC del cuerpo; Telegram no firma nada: manda una cabecera con un secreto compartido que
tu eliges aqui.

Por eso el servidor responde 403 a todo mientras `MAXICARE_TELEGRAM_WEBHOOK_SECRET` este
vacia. Este script genera uno si no lo hay y te dice que linea pegar en el `.env`.

==========================================================================================
LA TRAMPA: setWebhook Y getUpdates SON EXCLUYENTES
==========================================================================================
Telegram no deja las dos cosas a la vez. En cuanto haya webhook, `scripts/
obtener_chat_telegram.py` dejara de poder leer con `getUpdates` (devuelve el error 409
«Conflict: can't use getUpdates method while webhook is active») y ese script sirve para
averiguar el id del grupo. Si lo necesitas, corre `--quitar`, usalo, y vuelve a poner el
webhook.

Lo que SI sigue funcionando de ese script sin tocar nada: `getChat`, `getChatMember` y
`getChatAdministrators`, que son las comprobaciones de permisos.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

#: Lo unico que este servidor sabe atender. Pedirle a Telegram que mande de todo --ediciones,
#: entradas y salidas del grupo, reacciones-- seria trafico que el webhook descarta una y otra
#: vez, y cada update descartado es una llamada HTTP y una entrada de log.
ACTUALIZACIONES = ["callback_query", "message"]

VARIABLE = "MAXICARE_TELEGRAM_WEBHOOK_SECRET"


def _llamar(token: str, metodo: str, **cuerpo) -> dict:
    datos = json.dumps(cuerpo).encode()
    peticion = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{metodo}",
        data=datos,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        # El token va en la URL. Un error que la incluyera entera dejaria el token del bot
        # escrito en la consola y, con ella, en el historial del terminal.
        return {"ok": False, "description": f"{type(e).__name__}: {e}".replace(token, "***")}


def _estado(token: str) -> int:
    datos = _llamar(token, "getWebhookInfo")
    if not datos.get("ok"):
        print(f"FALLA no se pudo consultar: {datos.get('description')}")
        return 1

    info = datos["result"]
    url = info.get("url") or ""
    print("-" * 78)
    if not url:
        print("  Webhook:            NO HAY. El relevo esta apagado.")
        print("  Los botones de Telegram no hacen nada hasta que lo registres con --url.")
    else:
        print(f"  Webhook:            {url}")
        # `getWebhookInfo` NO dice si hay `secret_token`, y no hay forma de averiguarlo desde
        # aqui. Inventarse un "si" plausible seria peor que no decir nada: la unica prueba
        # de que el secreto coincide es que `last_error_message` NO traiga un 403.
        print("  Secreto:            PENDIENTE (Telegram no lo reporta; ver ULTIMO ERROR)")
        print(f"  Updates permitidos: {info.get('allowed_updates') or 'todos'}")
        print(f"  Pendientes:         {info.get('pending_update_count', 0)}")
    # Lo mas util de todo cuando algo no funciona: Telegram guarda el ultimo error de
    # entrega, y ahi es donde aparece un 403 por secreto mal puesto.
    if info.get("last_error_message"):
        print(f"  ULTIMO ERROR:       {info['last_error_message']}")
        print("                      (un 403 aqui = el secreto del .env no coincide)")
    print("-" * 78)
    return 0


def _poner(token: str, url: str, secreto: str, generado: bool) -> int:
    if not url.startswith("https://"):
        # Telegram solo acepta HTTPS, y con certificado valido. Decirlo aqui ahorra un
        # "Bad Request" que no explica nada.
        print("FALLA la URL tiene que empezar por https:// -- Telegram no acepta http")
        return 1

    datos = _llamar(
        token,
        "setWebhook",
        url=url,
        secret_token=secreto,
        allowed_updates=ACTUALIZACIONES,
        # Sin esto, Telegram entrega de golpe todo lo que se acumulo mientras no habia
        # webhook. En un grupo de doctores eso puede ser un mes de conversacion entrando a la
        # vez por `relevar_mensaje` -- y lo que no tenga relevo vivo respondera en su hilo.
        drop_pending_updates=True,
    )
    if not datos.get("ok"):
        print(f"FALLA Telegram rechazo el webhook: {datos.get('description')}")
        return 1

    print(f"OK   webhook registrado en {url}")
    print(f"     updates: {', '.join(ACTUALIZACIONES)}")
    print()
    if generado:
        print("=" * 78)
        print("PEGA ESTA LINEA EN TU .env Y VUELVE A DESPLEGAR:")
        print()
        print(f"    {VARIABLE}={secreto}")
        print()
        print("Sin ella el servidor responde 403 a TODO lo que mande Telegram, y el boton")
        print("«Hablar yo con el paciente» seguira sin hacer nada.")
        print("=" * 78)
    else:
        print(f"     se uso el {VARIABLE} que ya estaba en el .env")
        print("     si el VPS tiene otro valor, el servidor devolvera 403: comprueba --estado")
    return 0


def _quitar(token: str) -> int:
    datos = _llamar(token, "deleteWebhook")
    if not datos.get("ok"):
        print(f"FALLA no se pudo quitar: {datos.get('description')}")
        return 1
    print("OK   webhook quitado. El relevo queda apagado y `getUpdates` vuelve a funcionar.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", help="URL publica de /webhook/telegram")
    ap.add_argument("--estado", action="store_true", help="solo mira que hay registrado")
    ap.add_argument("--quitar", action="store_true", help="borra el webhook")
    args = ap.parse_args()

    cargar_dotenv()
    token = os.environ.get("MAXICARE_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("FALLA falta MAXICARE_TELEGRAM_BOT_TOKEN")
        return 1

    if args.quitar:
        return _quitar(token)
    if args.estado or not args.url:
        if not args.url and not args.estado:
            print("  (sin --url: solo se muestra el estado. Ver --help)")
            print()
        return _estado(token)

    secreto = os.environ.get(VARIABLE, "").strip()
    generado = not secreto
    if generado:
        # 32 bytes en base64url. Telegram acepta 1-256 caracteres de [A-Za-z0-9_-].
        secreto = secrets.token_urlsafe(32)

    return _poner(token, args.url, secreto, generado)


if __name__ == "__main__":
    raise SystemExit(main())

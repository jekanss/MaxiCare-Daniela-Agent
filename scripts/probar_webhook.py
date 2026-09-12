"""Prueba el webhook de punta a punta contra un servidor ya levantado.

    uv run uvicorn maxicare_daniela.runtime:app --host 127.0.0.1 --port 8099
    uv run python scripts/probar_webhook.py                          # local
    uv run python scripts/probar_webhook.py https://daniela.maxicarecol.com

Hace cinco comprobaciones, que son las que deciden si la fase 2 está en pie:

1. `/salud` responde y dice qué falta.
2. La verificación de Meta devuelve el `hub.challenge` tal cual, en texto plano.
3. Un POST sin firma válida se rechaza con 403. Es la que más importa: la URL del webhook
   es pública, y sin esta puerta cualquiera podría hacer aparecer en el grupo de los
   doctores un «archivo de paciente» que nadie mandó.
4. Un POST firmado llega de verdad al tema General de Telegram. El 200 no basta: la entrega
   ocurre DESPUÉS de responder, así que se comprueba en la tabla.
5. El mismo webhook otra vez no duplica nada.

La 4 escribe de verdad en el grupo de los doctores, con un número y un texto que dicen
claramente que son una prueba.

Usa `httpx` y no `urllib` por una razón concreta: contra el dominio real hay un Cloudflare
delante, y su protección anti-bot rechaza con `error code 1010` el User-Agent por defecto de
`urllib` (`Python-urllib/3.12`). Con `httpx` pasa. No es un problema del servidor: es el
cliente el que hay que elegir bien.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os  # noqa: E402

import httpx  # noqa: E402

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

BASE_POR_DEFECTO = "http://127.0.0.1:8099"
CABECERAS = {"User-Agent": "maxicare-daniela-pruebas/1.0"}

#: La app de Meta apunta a `/whatsapp`; el plan documenta `/webhook/whatsapp`. Las dos se
#: sirven, así que las dos se prueban: si alguien recorta una «porque no se usa», esto lo
#: detecta antes de que la clínica deje de recibir mensajes.
RUTAS = ("/whatsapp", "/webhook/whatsapp")

#: La que Meta usa de verdad. Es donde llegarían los mensajes reales.
RUTA_REAL = RUTAS[0]


def main() -> int:
    cargar_dotenv()
    base = (sys.argv[1] if len(sys.argv) > 1 else BASE_POR_DEFECTO).rstrip("/")
    verify = os.environ.get("MAXICARE_WHATSAPP_VERIFY_TOKEN", "").strip()
    secreto = os.environ.get("WHATSAPP_APP_SECRET", "").strip()

    if not verify or not secreto:
        print("ERROR: faltan MAXICARE_WHATSAPP_VERIFY_TOKEN o WHATSAPP_APP_SECRET en .env",
              file=sys.stderr)
        return 1

    print(f"Probando {base}\n")
    fallos = 0

    with httpx.Client(timeout=30.0, headers=CABECERAS, follow_redirects=True) as c:
        # ── 1. salud ─────────────────────────────────────────────────────────────────────
        try:
            r = c.get(f"{base}/salud")
        except httpx.HTTPError as e:
            print(f"1. GET /salud                       -> FALLA no se pudo conectar: {e}")
            return 1
        ok = r.status_code == 200
        print(f"1. GET /salud                       -> {'OK  ' if ok else 'FALLA'} "
              f"{r.status_code}")
        if not ok:
            print(f"   {r.text[:300]}")
            return 1
        estado = r.json()
        for clave in ("base_de_datos", "configuracion", "tema_general", "mensajes_recibidos"):
            print(f"     {clave:20}: {estado.get(clave)}")
        if estado.get("configuracion") != "ok":
            fallos += 1

        # ── 2. verificación de Meta ──────────────────────────────────────────────────────
        reto = str(int(time.time()))
        r = c.get(f"{base}{RUTA_REAL}", params={
            "hub.mode": "subscribe", "hub.verify_token": verify, "hub.challenge": reto})
        # Meta exige el challenge TAL CUAL, sin comillas ni JSON: si se envuelve, la consola
        # dice solo «no se pudo validar la URL» y no hay forma de saber por qué.
        ok = r.status_code == 200 and r.text == reto
        print(f"\n2. GET verificación de Meta         -> {'OK  ' if ok else 'FALLA'} "
              f"{r.status_code} · devolvió {r.text[:40]!r}, esperaba {reto!r}")
        fallos += not ok

        r = c.get(f"{base}{RUTA_REAL}", params={
            "hub.mode": "subscribe", "hub.verify_token": "token-equivocado",
            "hub.challenge": reto})
        ok = r.status_code == 403
        print(f"   con el token equivocado          -> {'OK  ' if ok else 'FALLA'} "
              f"{r.status_code} (debe ser 403)")
        fallos += not ok

        # ── 3. la puerta: firma ──────────────────────────────────────────────────────────
        cuerpo_falso = json.dumps({"entry": [{"changes": [{"value": {"messages": [{
            "from": "573009999999", "id": "wamid.INTRUSO", "type": "text",
            "text": {"body": "esto no lo mando Meta"}}]}}]}]}).encode()

        r = c.post(f"{base}{RUTA_REAL}", content=cuerpo_falso,
                   headers={"Content-Type": "application/json"})
        ok = r.status_code == 403
        print(f"\n3. POST sin firma                   -> {'OK  ' if ok else 'FALLA'} "
              f"{r.status_code} (debe ser 403)")
        fallos += not ok

        r = c.post(f"{base}{RUTA_REAL}", content=cuerpo_falso, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64})
        ok = r.status_code == 403
        print(f"   POST con firma inventada         -> {'OK  ' if ok else 'FALLA'} "
              f"{r.status_code} (debe ser 403)")
        fallos += not ok

        # ── 4. el viaje completo ─────────────────────────────────────────────────────────
        wamid = f"wamid.PRUEBA.{int(time.time())}"
        real = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "prueba"},
                "contacts": [{"profile": {"name": "PRUEBA TECNICA (no es un paciente)"},
                              "wa_id": "573000000000"}],
                "messages": [{"from": "573000000000", "id": wamid,
                              "timestamp": str(int(time.time())), "type": "text",
                              "text": {"body": "Mensaje de prueba del webhook. Ignorar."}}],
            }}]}],
        }).encode()
        firma = "sha256=" + hmac.new(secreto.encode(), real, hashlib.sha256).hexdigest()

        r = c.post(f"{base}{RUTA_REAL}", content=real, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": firma})
        ok = r.status_code == 200
        print(f"\n4. POST firmado                     -> {'OK  ' if ok else 'FALLA'} "
              f"{r.status_code}")
        fallos += not ok

        print("   esperando la entrega en segundo plano...")
        time.sleep(6)
        despues = c.get(f"{base}/salud").json()

        entro = despues.get("mensajes_recibidos", 0) > estado.get("mensajes_recibidos", 0)
        print(f"     mensajes_recibidos: {estado.get('mensajes_recibidos')} -> "
              f"{despues.get('mensajes_recibidos')}  {'OK' if entro else 'FALLA'}")
        fallos += not entro

        # Un 200 no prueba nada por sí solo: la entrega ocurre DESPUÉS de responder, así que
        # este es el único fallo que importa. Sin contarlo, la prueba diría OK con el archivo
        # tirado en el suelo — que es exactamente lo que pasó la primera vez que se corrió.
        sin_entregar = despues.get("sin_entregar", 0)
        print(f"     sin_entregar      : {sin_entregar}  "
              f"{'OK' if sin_entregar == 0 else 'FALLA — entró pero NO llegó a Telegram'}")
        fallos += sin_entregar != 0

        # ── 5. el reintento de Meta ──────────────────────────────────────────────────────
        c.post(f"{base}{RUTA_REAL}", content=real, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": firma})
        time.sleep(3)
        reintento = c.get(f"{base}/salud").json()
        ok = reintento.get("mensajes_recibidos") == despues.get("mensajes_recibidos")
        print(f"\n5. El MISMO webhook otra vez        -> {'OK  ' if ok else 'FALLA'} "
              f"mensajes_recibidos sigue en {reintento.get('mensajes_recibidos')}")
        print("   (Meta reintenta; sin deduplicación el doctor vería el archivo dos veces)")
        fallos += not ok

        # ── 6. las dos rutas ─────────────────────────────────────────────────────────────
        print("\n6. Las dos rutas del webhook")
        for ruta in RUTAS:
            reto2 = str(int(time.time()))
            r = c.get(f"{base}{ruta}", params={
                "hub.mode": "subscribe", "hub.verify_token": verify, "hub.challenge": reto2})
            ok = r.status_code == 200 and r.text == reto2
            marca = "  <- la que Meta tiene configurada" if ruta == RUTA_REAL else ""
            print(f"   {ruta:22} -> {'OK  ' if ok else 'FALLA'} {r.status_code}{marca}")
            fallos += not ok

    print()
    if fallos:
        print(f"{fallos} comprobación(es) fallaron.")
        return 1
    print("Webhook OK. Revisa el tema General de Telegram: debe estar el mensaje de prueba.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

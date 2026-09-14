"""Manda al grupo REAL de los doctores los cuatro mensajes que cambia el silencio, para
comprobarlos con el celular en la mano.

    uv run python scripts/probar_silencio.py            # solo al tema General
    uv run python scripts/probar_silencio.py --tema 1002   # tambien al tema de un paciente
    uv run python scripts/probar_silencio.py --limpiar    # borra lo que este script mando

NO gasta tokens: no llama al modelo ni una vez. Solo Telegram.

POR QUE EXISTE Y QUE ES LO UNICO QUE PRUEBA
============================================
Ninguna prueba automatica de este repositorio puede demostrar que un celular NO vibro.
`tests/test_ingesta.py` verifica que mandamos `disable_notification: true`; que Telegram lo
respete, y sobre todo que el doctor perciba la diferencia, solo lo dice un telefono.

`scripts/probar_lectura.py` tampoco sirve para esto: su doble de Telegram ni siquiera
registra el `silencioso`, a proposito -- lo que ese script mide es el muro, no el ruido.

Asi que esto es un script de HUMO, no un entregable de fase. Lo que deja escrito es una
respuesta a una sola pregunta: de los cuatro mensajes que llegan, cuantos te suenan.

LO QUE TIENES QUE VER EN EL CELULAR
===================================
    1. ESCALAMIENTO (General)      -> SUENA.  Es lo unico que le pide algo al doctor.
    2. aviso de archivos (General) -> SUENA.  Una vez por tanda, no una por archivo.
    3. texto del paciente (tema)   -> MUDO.   Entra al hilo sin avisar a nadie.
    4. lectura clinica (tema)      -> MUDO.   Acompania al archivo; suena donde sono el.

Si los cuatro suenan, `silencioso=` no esta llegando a Telegram. Si ninguno suena, tienes el
grupo silenciado en tu telefono y esta prueba no mide nada: quitale el mute antes de correrla.

EL TEMA DE UN PACIENTE, SI QUIERES PROBAR LOS DOS MUDOS
========================================================
Sin `--tema`, los mensajes 3 y 4 no se mandan: este script NO crea temas ni toca la base.
Para sacar un id real, del paciente que prefieras:

    SELECT telefono, topic_id FROM temas_telegram;

Escribe en un tema CERRADO sin problema: el bot es administrador, y esa asimetria -- el bot
deposita, el doctor no puede escribir -- es la garantia dura del diseno del relevo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.canales import ErrorDeCanal, Telegram  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402

#: Donde se apunta lo enviado para poder borrarlo despues. Un script que deja basura en el
#: grupo de los doctores no se vuelve a correr.
RASTRO = Path(__file__).resolve().parent / ".silencio_enviado.json"

MARCA = "[PRUEBA DE SILENCIO]"


def _config() -> tuple[str, str, int]:
    cargar_dotenv()
    token = os.environ.get("MAXICARE_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("MAXICARE_TELEGRAM_CHAT_DOCTORES", "").strip()
    if not token or not chat:
        print("FALLA falta MAXICARE_TELEGRAM_BOT_TOKEN o MAXICARE_TELEGRAM_CHAT_DOCTORES")
        sys.exit(1)
    # El General es 0: se le escribe OMITIENDO `message_thread_id`, no mandando 1.
    general = int(os.environ.get("MAXICARE_TELEGRAM_TOPIC_GENERAL", "0") or 0)
    return token, chat, general


async def _enviar(tg: Telegram, tema_de_paciente: int | None) -> list[int]:
    token, chat, general = _config()
    enviados: list[int] = []

    casos = [
        ("1. ESCALAMIENTO", general, False,
         f"{MARCA}\n<b>Escalamiento · dolor agudo</b>\n"
         "Paciente De Prueba · +570000000000\n\n"
         "Esto imita el aviso que manda `escalar_a_doctores`.\n\n"
         "<b>ESTE TIENE QUE SONAR.</b>"),
        ("2. aviso de archivos", general, False,
         f"{MARCA}\n📎 Paciente De Prueba mandó archivos — están en su tema.\n\n"
         "<b>ESTE TIENE QUE SONAR</b> (una vez por tanda, no una por archivo)."),
    ]

    if tema_de_paciente is not None:
        casos += [
            ("3. texto del paciente", tema_de_paciente, True,
             f"{MARCA}\n🦷 <b>Paciente De Prueba</b> · +570000000000\nMandó un mensaje\n\n"
             "💬 <i>me duele desde ayer</i>\n\n"
             "<b>ESTE NO DEBE SONAR.</b>"),
            ("4. lectura clínica", tema_de_paciente, True,
             f"{MARCA}\n📄 <b>Ejemplo · sin contenido clínico real · 14/09/2026</b>\n"
             "<b>Motivo:</b> texto de ejemplo para comprobar el silencio.\n\n"
             "<b>ESTE NO DEBE SONAR.</b>"),
        ]

    for etiqueta, tema, silencioso, texto in casos:
        try:
            mid = await tg.enviar_mensaje(texto, tema_id=tema, silencioso=silencioso)
        except ErrorDeCanal as e:
            print(f"  FALLA {etiqueta}: {e}")
            continue
        enviados.append(mid)
        destino = "General" if not tema else f"tema {tema}"
        esperado = "NO debe sonar" if silencioso else "TIENE que sonar"
        print(f"  OK   {etiqueta:24s} -> {destino:12s} silencioso={str(silencioso):5s}"
              f"  ({esperado})")

    return enviados


async def _limpiar(tg: Telegram) -> None:
    if not RASTRO.exists():
        print("  No hay nada que borrar: no se ha corrido el envio todavia.")
        return
    ids = json.loads(RASTRO.read_text(encoding="utf-8"))
    for mid in ids:
        try:
            await tg.borrar_mensaje(mid)
            print(f"  OK   borrado el mensaje {mid}")
        except ErrorDeCanal as e:
            # Telegram no deja borrar mensajes de mas de 48 h, y el bot necesita
            # `can_delete_messages`. Ninguna de las dos cosas es motivo para abortar.
            print(f"  ---  no se borro {mid}: {e}")
    RASTRO.unlink()
    print("  Rastro borrado.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tema", type=int, default=None,
                    help="message_thread_id del tema de un paciente, para probar los mudos")
    ap.add_argument("--limpiar", action="store_true",
                    help="borra del grupo lo que este script mando")
    args = ap.parse_args()

    token, chat, general = _config()
    tg = Telegram(token, chat)

    print("=" * 78)
    print(f"Grupo de doctores: {chat}   ·   tema General: {general}")
    print("=" * 78)

    if args.limpiar:
        asyncio.run(_limpiar(tg))
        return

    if args.tema is None:
        print("  Sin --tema: solo se prueban los dos que SUENAN (1 y 2).")
        print("  Para probar los mudos, pasa el topic de un paciente. Ver el docstring.")
    print()

    enviados = asyncio.run(_enviar(tg, args.tema))
    RASTRO.write_text(json.dumps(enviados), encoding="utf-8")

    print()
    print("=" * 78)
    print("AHORA MIRA EL CELULAR, no esta pantalla.")
    print("  Suenan 1 y 2." + ("  Mudos 3 y 4." if args.tema is not None else ""))
    print("  Si suenan los cuatro, `silencioso=` no esta llegando a Telegram.")
    print("  Si no suena ninguno, tienes el grupo silenciado: quitale el mute y repite.")
    print()
    print("Cuando termines:  uv run python scripts/probar_silencio.py --limpiar")
    print("=" * 78)


if __name__ == "__main__":
    main()

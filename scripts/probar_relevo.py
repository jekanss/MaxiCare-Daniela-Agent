"""El ciclo del relevo (6C) contra el grupo REAL, en un tema de usar y tirar.

    uv run python scripts/probar_relevo.py
    uv run python scripts/probar_relevo.py --dejar    # no borra el tema, para mirarlo

NO gasta tokens: no llama al modelo ni una vez.
NO toca la base: no crea pacientes, no crea conversaciones, no activa ningun relevo.
NO toca a ningun paciente: todo pasa en un tema nuevo que se borra al terminar.

==========================================================================================
QUE PRUEBA, Y POR QUE NO LO PUEDE PROBAR `pytest`
==========================================================================================
`tests/test_relevo.py` demuestra que el codigo LLAMA a `reopenForumTopic`, a
`editMessageReplyMarkup` y a `setMessageReaction` con los argumentos correctos. Lo que no
puede demostrar es que el bot tenga PERMISO para hacerlo en este grupo concreto -- y esa es
la mitad que se rompe en produccion:

    createForumTopic / closeForumTopic / reopenForumTopic  ->  pide `can_manage_topics`
    deleteForumTopic                                       ->  pide `can_delete_messages`

Un bot al que le falte uno de los dos pasa la suite entera en verde y, el dia que un doctor
pulse el boton, el relevo se activa en la base y el hilo no se abre.

Y prueba una cosa mas que ningun test offline ve: que el WEBHOOK este registrado. Sin el,
Telegram no manda un solo update y el boton no hace nada, sin un error en ningun log.

==========================================================================================
LO QUE NO PRUEBA, Y HAY QUE HACER A MANO
==========================================================================================
El camino doctor -> paciente. Para eso hace falta un doctor escribiendo de verdad en el hilo
de un paciente de verdad en relevo, y eso ya es la prueba manual: escalar una conversacion,
pulsar «Hablar yo con el paciente» y escribir. Este script te deja el terreno comprobado
antes de llegar ahi.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import relevo  # noqa: E402
from maxicare_daniela.canales import ErrorDeCanal, Telegram  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402

CONV_FALSA = "00000000-0000-0000-0000-000000000000"
NOMBRE = "[PRUEBA DE RELEVO] borrar"


class Cuenta:
    def __init__(self) -> None:
        self.ok = 0
        self.fallas = 0

    def paso(self, etiqueta: str, detalle: str = "") -> None:
        self.ok += 1
        print(f"  OK   {etiqueta}{'  ' + detalle if detalle else ''}")

    def falla(self, etiqueta: str, error: object) -> None:
        self.fallas += 1
        print(f"  FALLA {etiqueta}: {error}")


#: Cuanto se le da a Telegram para admitir que borro un tema. Medido: ~3 s. El margen es
#: ancho a proposito -- lo que se prueba es que la sonda ACABA distinguiendo, no que sea veloz.
ESPERA_MAXIMA_BORRADO = 20.0


async def _sondear_hasta_borrado(tg: Telegram, tema: int) -> tuple[str | None, float]:
    """`(ultimo estado visto, segundos que costo)`. Para en cuanto dice 'borrado'."""
    inicio = time.monotonic()
    estado: str | None = None
    while True:
        estado = await tg.estado_del_tema(tema)
        transcurrido = time.monotonic() - inicio
        if estado == "borrado" or transcurrido >= ESPERA_MAXIMA_BORRADO:
            return estado, transcurrido
        await asyncio.sleep(2)


async def _ciclo(tg: Telegram, cuenta: Cuenta, dejar: bool) -> None:
    tema: int | None = None

    # 1. Crear. Aqui es donde se cae un bot sin `can_manage_topics`.
    try:
        tema = await tg.crear_tema(NOMBRE)
        cuenta.paso("crear_tema", f"-> tema {tema}")
    except ErrorDeCanal as e:
        cuenta.falla("crear_tema", e)
        print()
        print("  El bot no puede crear temas. Sin eso no hay relevo posible para un paciente")
        print("  que todavia no tiene hilo. Comprueba `can_manage_topics`:")
        print("      uv run python scripts/obtener_chat_telegram.py")
        return

    try:
        # 2. Cerrarlo y reabrirlo: el candado que sostiene el diseno entero. Un tema cerrado
        #    impide FISICAMENTE escribir a quien no es administrador; el bot, que si lo es,
        #    sigue depositando. El relevo es exactamente reabrir y volver a cerrar.
        try:
            await tg.cerrar_tema(tema)
            cuenta.paso("cerrar_tema", "el candado se pone")
        except ErrorDeCanal as e:
            cuenta.falla("cerrar_tema", e)

        try:
            await tg.reabrir_tema(tema)
            cuenta.paso("reabrir_tema", "el candado se quita")
        except ErrorDeCanal as e:
            cuenta.falla("reabrir_tema", e)

        # 2b. La sonda del tema, contra un tema que EXISTE. La otra mitad --contra uno
        #     borrado-- va en el `finally`, porque hace falta borrarlo primero.
        #
        #     Esto esta aqui por un fallo medido en produccion el 14/09/2026. La primera
        #     version preguntaba con `editForumTopic` sin argumentos, razonando que sin nada
        #     que cambiar no tendria efecto. Cierto, y por eso mismo NO VALIDA EL ID: decia
        #     `ok: true` para un tema ya borrado. La sonda respondia que si a todo, el agujero
        #     que venia a tapar seguia abierto entero, y no habia un solo error en el log ni
        #     una prueba en rojo: las offline doblan a Telegram, asi que ninguna podia verlo.
        #     Solo lo ve una llamada de verdad contra la API. Por eso vive en este script.
        estado = await tg.estado_del_tema(tema)
        if estado == "abierto":
            cuenta.paso("estado_del_tema (vivo)", "-> 'abierto'")
        else:
            cuenta.falla("estado_del_tema (vivo)", f"devolvio {estado!r}, se esperaba 'abierto'")

        # 3. El mensaje de bienvenida, con su boton. Es el que lleva al doctor al hilo:
        #    `answerCallbackQuery(url=...)` hacia un tema del propio supergrupo responde
        #    URL_INVALID, asi que la notificacion de ESTE mensaje es toda la navegacion.
        try:
            mensaje = await tg.enviar_mensaje(
                "Prueba del relevo. Este mensaje deberia SONARTE.\n"
                "Si no suena, el doctor no llega a este hilo cuando le den la conversacion.",
                tema_id=tema,
                teclado=relevo.teclado_devolver(CONV_FALSA),
                silencioso=False,
            )
            cuenta.paso("enviar_mensaje con teclado", f"-> mensaje {mensaje}")
        except ErrorDeCanal as e:
            cuenta.falla("enviar_mensaje con teclado", e)
            mensaje = None

        # 4. Cambiar el teclado sin tocar el texto. Es lo que deja el General ordenado cuando
        #    alguien toma la conversacion.
        if mensaje is not None:
            # Con el id del mensaje: la forma de tres tramos. `t.me/c/<chat>/<n>` es ambiguo
            # en un foro --ese `<n>` es un id de MENSAJE-- y no garantiza aterrizar dentro
            # del hilo, que es toda la queja del 14/09/2026.
            enlace = tg.enlace_al_tema(tema, mensaje)
            try:
                await tg.editar_teclado(
                    mensaje, relevo.teclado_ir_al_hilo(enlace, "Dra. Prueba")
                )
                cuenta.paso("editar_teclado", "el boton se reemplaza por el enlace")
            except ErrorDeCanal as e:
                cuenta.falla("editar_teclado", e)

            # 5. La reaccion: el acuse de que lo que escribio el doctor si salio. No propaga
            #    nunca, asi que aqui solo se puede mirar si aparecio en el grupo.
            await tg.reaccionar(mensaje)
            cuenta.paso("reaccionar", "mira si al mensaje le salio un pulgar")

            print()
            print(f"  El enlace al hilo seria: {enlace}")
            print("  Abrelo: tiene que dejarte DENTRO de este tema, en el mensaje de arriba,")
            print("  no en el General y no en un error.")
            print()
    finally:
        if tema is not None and not dejar:
            try:
                await tg.borrar_tema(tema)
                cuenta.paso("borrar_tema", "el grupo queda como estaba")
            except ErrorDeCanal as e:
                cuenta.falla("borrar_tema", e)
                print(f"       Borra a mano el tema «{NOMBRE}»: el bot no tiene "
                      "`can_delete_messages`.")
            else:
                # LA COMPROBACION QUE FALTABA. Si esto no dice 'borrado', el barrido no se
                # entera nunca de que alguien borro un hilo, y el paciente de ese hilo se
                # queda sin nadie que le conteste: Daniela callada porque el relevo sigue
                # tomado, y el doctor sin sitio donde escribir.
                #
                # CON ESPERA, y no de un tiro. Medido el 14/09/2026: durante unos 3 segundos
                # despues de `deleteForumTopic`, `reopenForumTopic` sigue contestando
                # TOPIC_NOT_MODIFIED --o sea, 'abierto'-- y solo despues pasa a
                # TOPIC_ID_INVALID, ya para siempre. En produccion da igual: el barrido pasa
                # como pronto un minuto despues. Aqui no: sondear al instante hacia fallar
                # esta comprobacion cada vez, y una guarda que falla siempre se acaba
                # ignorando, que es como se pierde la unica que caza el fallo de verdad.
                estado, tardo = await _sondear_hasta_borrado(tg, tema)
                if estado == "borrado":
                    cuenta.paso("estado_del_tema (borrado)", f"-> 'borrado' en {tardo:.0f}s")
                else:
                    cuenta.falla(
                        "estado_del_tema (borrado)",
                        f"devolvio {estado!r} despues de {tardo:.0f}s para un tema borrado. "
                        "La sonda no distingue, y el barrido no cerrara ese relevo NUNCA",
                    )
        elif tema is not None:
            print(f"  ---  el tema {tema} se queda (--dejar). Borralo tu cuando termines.")


def _webhook(token: str, cuenta: Cuenta) -> None:
    """Sin webhook, nada de lo de arriba llega a ocurrir nunca en produccion."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15
        ) as r:
            info = json.loads(r.read().decode()).get("result", {})
    except Exception as e:  # noqa: BLE001
        cuenta.falla("getWebhookInfo", f"{type(e).__name__}")
        return

    if not info.get("url"):
        cuenta.falla("webhook", "NO HAY. Los botones no hacen nada.")
        print("       uv run python scripts/configurar_webhook_telegram.py \\")
        print("            --url https://tu.dominio/webhook/telegram")
        return

    cuenta.paso("webhook registrado", info["url"])
    if info.get("last_error_message"):
        cuenta.falla("ultima entrega", info["last_error_message"])
        print("       Un 403 aqui significa que MAXICARE_TELEGRAM_WEBHOOK_SECRET no coincide")
        print("       entre tu .env y el del VPS.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dejar", action="store_true", help="no borra el tema de prueba")
    args = ap.parse_args()

    cargar_dotenv()
    token = os.environ.get("MAXICARE_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("MAXICARE_TELEGRAM_CHAT_DOCTORES", "").strip()
    if not token or not chat:
        print("FALLA falta MAXICARE_TELEGRAM_BOT_TOKEN o MAXICARE_TELEGRAM_CHAT_DOCTORES")
        return 1

    cuenta = Cuenta()
    print("=" * 78)
    print(f"Grupo de doctores: {chat}")
    print("=" * 78)
    print()
    print("La puerta (sin esto, el boton nunca hace nada):")
    _webhook(token, cuenta)
    print()
    print("El ciclo del tema, en uno de usar y tirar:")
    asyncio.run(_ciclo(Telegram(token, chat), cuenta, args.dejar))

    print()
    print("=" * 78)
    if cuenta.fallas:
        print(f"FALLA {cuenta.fallas} comprobacion(es). El relevo NO esta listo.")
        return 1
    print(f"OK   {cuenta.ok} comprobaciones. El terreno esta listo para la prueba manual:")
    print("     escala una conversacion, pulsa «Hablar yo con el paciente» y escribe en el")
    print("     hilo. Lo que escribas tiene que llegarle al paciente por WhatsApp, literal.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

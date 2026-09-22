"""La transcripcion de notas de voz contra la API REAL, con audios REALES de produccion.

    uv run python scripts/probar_transcripcion.py
    uv run python scripts/probar_transcripcion.py --comparar   # los cuatro modelos, no uno

GASTA, poco: se factura por duracion de audio. Las cinco notas de voz que hay hoy en
produccion suman 69 segundos entre todas.
NO toca la base: solo LEE `mensajes_entrantes` para sacar los `media_id`.
NO le escribe a ningun paciente y NO manda nada a Telegram.

==========================================================================================
QUE PRUEBA, Y POR QUE NO LO PUEDE PROBAR `pytest`
==========================================================================================
`tests/test_transcripcion.py` demuestra que el codigo le manda a la API un nombre acabado en
`.ogg`, el idioma fijo y ningun `prompt`. Lo que no puede demostrar --porque offline la API
va doblada-- es lo unico que de verdad se rompe:

    1. Que la API SIGA ACEPTANDO el ogg/opus que manda WhatsApp. Es el mismo agujero que
       `.claude/rules/perimetro-seguridad.md` ya reconoce para el tope de descarga: lo que
       declara un tercero, offline va doblado.
    2. Que el modelo elegido SIRVA. Y esto no es teorico: de los cuatro que tiene la cuenta,
       tres devuelven algo que PARECE espanol y no lo es. Medido el 21/09/2026 sobre el mismo
       audio:

           gpt-transcribe          «que con quien hablo yo, una doctora o un doctor?»   BIEN
           gpt-4o-transcribe       «Con xenaula se una doctora, onde totenui...»        MAL
           gpt-4o-mini-transcribe  «Ya conchena una doctora un doctor...»               MAL
           whisper-1               bien, pero se come frases enteras

       Un 200 con basura dentro pasa cualquier comprobacion automatica. Por eso este script
       IMPRIME LAS FRASES y no un OK: quien lo corra tiene que leerlas.

==========================================================================================
LO QUE NO PRUEBA, Y HAY QUE HACER A MANO
==========================================================================================
Que Daniela conteste bien a lo que se transcribio. Eso es un turno completo y se prueba
mandandole una nota de voz de verdad por WhatsApp: «cuanto cuesta la limpieza?» tiene que
recibir el precio, no «ya recibimos tu audio».
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import persistencia, transcripcion  # noqa: E402
from maxicare_daniela.canales import WhatsApp  # noqa: E402
from maxicare_daniela.config import (  # noqa: E402
    MODELO_TRANSCRIPTOR,
    Config,
    cargar_dotenv,
    descartar_vacias_de_terceros,
)

#: Los otros tres de la cuenta, solo para `--comparar`. Estan aqui y no en `config.py` porque
#: no son una opcion del sistema: son la evidencia de por que se eligio el que se eligio.
OTROS_MODELOS = ("gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1")


def _notas_de_voz(database_url: str, limite: int) -> list[tuple[str, str, object]]:
    """`(media_id, mime, cuando)` de las notas de voz reales, de la mas nueva a la mas vieja.

    Solo LEE. Se usan audios de verdad y no uno sintetizado a proposito: lo que hay que
    comprobar es que la API acepte lo que graba el WhatsApp de un telefono, no lo que sepa
    generar esta maquina.
    """
    with persistencia.conectar(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT media_id, mime, recibido_en
              FROM mensajes_entrantes
             WHERE tipo = ANY(%(tipos)s) AND media_id IS NOT NULL
             ORDER BY recibido_en DESC
             LIMIT %(limite)s
            """,
            {"tipos": list(transcripcion.TIPOS_QUE_SE_TRANSCRIBEN), "limite": limite},
        )
        return [(m, mime, cuando) for m, mime, cuando in cur.fetchall()]


async def _correr(args: argparse.Namespace) -> int:
    cargar_dotenv()
    # Sin esto, `OPENAI_BASE_URL=` vacia del `.env` se convierte en la URL base del cliente y
    # toda llamada muere con «Request URL is missing an 'http://' or 'https://' protocol».
    # Es la trampa que CLAUDE.md ya documenta, y este script la pisa como cualquier otro.
    descartar_vacias_de_terceros()
    config = Config.desde_entorno()

    filas = _notas_de_voz(config.database_url, args.cuantas)
    if not filas:
        print("No hay ninguna nota de voz en `mensajes_entrantes` con su `media_id`.")
        print("Manda una por WhatsApp al numero de la clinica y vuelve a correr esto.")
        return 1

    whatsapp = WhatsApp(
        config.whatsapp_token,
        config.whatsapp_phone_number_id,
        tope_descarga_mb=config.tope_descarga_mb,
    )
    from openai import AsyncOpenAI

    cliente = AsyncOpenAI()

    modelos = (MODELO_TRANSCRIPTOR, *OTROS_MODELOS) if args.comparar else (MODELO_TRANSCRIPTOR,)
    fallas = 0
    bajadas = 0

    for media_id, mime, cuando in filas:
        print(f"\n=== nota de voz de {cuando}  mime={mime}")
        try:
            archivo = await whatsapp.descargar_media(media_id)
        except Exception as e:  # noqa: BLE001
            # Meta caduca los media al cabo de unos dias. No es un fallo del sistema, asi que
            # no cuenta como tal: se dice y se pasa a la siguiente.
            print(f"  (Meta ya no la sirve: {type(e).__name__}) -- se salta")
            continue

        bajadas += 1
        print(
            f"  bajada: {archivo.tamano} bytes, nombre del proyecto = {archivo.nombre}"
            f"  ->  a la API va como {transcripcion._nombre_para_la_api(archivo)}"
        )

        for modelo in modelos:
            envoltorio = io.BytesIO(archivo.contenido)
            envoltorio.name = transcripcion._nombre_para_la_api(archivo)
            arranque = time.monotonic()
            try:
                r = await cliente.audio.transcriptions.create(
                    model=modelo, file=envoltorio, language=transcripcion.IDIOMA
                )
            except Exception as e:  # noqa: BLE001
                print(f"  FALLA {modelo}: {type(e).__name__}: {str(e)[:200]}")
                if modelo == MODELO_TRANSCRIPTOR:
                    fallas += 1
                continue
            segundos = getattr(getattr(r, "usage", None), "seconds", None)
            marca = "  <-- el que corre en produccion" if modelo == MODELO_TRANSCRIPTOR else ""
            print(f"  OK   {modelo}  {time.monotonic() - arranque:.2f} s  audio={segundos}s{marca}")
            print(f"       {r.text!r}")

    if not bajadas:
        print("\nNinguno de los `media_id` guardados sigue vivo en Meta.")
        print("Manda una nota de voz nueva por WhatsApp y vuelve a correr esto.")
        return 1

    print(
        f"\n{bajadas} audio(s) transcritos con {MODELO_TRANSCRIPTOR}, {fallas} falla(s)."
        "\nLEE LAS FRASES: un 200 con basura dentro pasa cualquier comprobacion automatica."
    )
    return 1 if fallas else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--comparar",
        action="store_true",
        help="corre tambien los otros tres modelos de la cuenta, para ver la diferencia",
    )
    p.add_argument(
        "--cuantas", type=int, default=3, help="cuantas notas de voz recientes probar"
    )
    return asyncio.run(_correr(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

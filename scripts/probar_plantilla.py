"""Comprueba la plantilla de recordatorio de Meta, y manda UNO de verdad si se pide.

    uv run python scripts/probar_plantilla.py --estado          (gratis, no manda nada)
    uv run python scripts/probar_plantilla.py 573196842471      (MANDA un mensaje real)

Es el paso que va entre «Meta aprobó la plantilla» y `bash scripts/desplegar.sh`. No toca la
base de datos en ningún momento: ni lee `seguimientos` ni escribe una fila. Lo único que hace
es armar los cuatro huecos y llamar a Meta.

**Los parámetros salen de `seguimientos.parametros_de`, no de aquí.** Es
deliberado y es lo único que hace que esta prueba valga: escribir los cuatro literales a mano
comprobaría que Meta acepta *una* plantilla, no que acepta la que manda el despachador. Con el
formateador de verdad, un orden de huecos cambiado o la conversión de zona rota se ven en el
WhatsApp que llega al teléfono, que es donde el paciente los vería.

Este script DOBLA la fila que el despachador lee de la base (`cita_inicio`, `nombre_completo`,
`tratamiento`): si esa consulta cambia de nombres de columna, aquí no se entera nadie. Ver
`.claude/rules/scripts-entregables.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from maxicare_daniela import seguimientos  # noqa: E402
from maxicare_daniela.canales import BASE_GRAPH, ErrorDeCanal, WhatsApp  # noqa: E402
from maxicare_daniela.config import Config, cargar_dotenv  # noqa: E402

#: Los errores de Meta que este camino produce de verdad, en castellano y con qué se arregla
#: cada uno. Un `132001` es el caso que costó la aprobación anterior: el código de idioma del
#: `.env` tiene que ser EXACTAMENTE el de la traducción registrada, y `es_CO` no acepta `es`.
_ERRORES = {
    132001: (
        "la plantilla no existe con ESE nombre y ESE idioma. Meta no busca la traducción más\n"
        "      parecida: revisa la columna Language en WhatsApp Manager y pon el mismo código\n"
        "      en MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA (Spanish = 'es'; 'es_CO' es otro)."
    ),
    132000: (
        "el número de huecos que mandamos no coincide con el que Meta aprobó. La plantilla\n"
        "      tiene cuatro variables; `parametros_de` devuelve otra cantidad."
    ),
    132005: "el texto de algún hueco es más largo de lo que Meta admite.",
    132007: "el texto de algún hueco viola el formato de la plantilla (saltos de línea, tabs).",
    132015: "la plantilla está PAUSADA por mala calidad. No se puede mandar hasta que reanude.",
    132016: "la plantilla está DESHABILITADA por Meta. Hay que crear otra.",
    131026: (
        "el número de destino no puede recibir: no tiene WhatsApp, o es un número que esta\n"
        "      cuenta de prueba no tiene en su lista de destinatarios permitidos."
    ),
    131047: "hace más de 24 h del último mensaje. Justo lo que una plantilla debería saltarse.",
    131049: "Meta limitó la entrega de mensajes de marketing a este usuario en este momento.",
    190: "el token de WhatsApp caducó o fue revocado. Hay que generar otro.",
    100: "parámetro inválido; el detalle crudo va justo debajo.",
}


def _fila_de_ejemplo() -> dict[str, Any]:
    """La fila que el despachador leería de la base, para una cita de mañana.

    Mañana y no hoy a propósito: un recordatorio se manda la víspera, y una hora futura deja
    el mensaje diciendo algo que el lector puede comprobar contra su reloj.
    """
    manana = datetime.now(seguimientos.jornada_zona()) + timedelta(days=1)
    return {
        "cita_inicio": manana.replace(hour=9, minute=0, second=0, microsecond=0),
        "nombre_completo": "Jean Chamorro",
        "tratamiento": "Limpieza dental",
    }


async def _waba_ids(token: str) -> list[str]:
    """Los WABA que el token puede tocar, sacados del propio token.

    El proyecto guarda el `phone_number_id` pero no el id de la cuenta de negocio, y listar
    plantillas cuelga de la cuenta y no del número. `debug_token` lo dice sin pedir un secreto
    más: los `granular_scopes` de un token de usuario de sistema traen los ids de destino.
    """
    async with httpx.AsyncClient(timeout=15.0) as cliente:
        r = await cliente.get(
            f"{BASE_GRAPH}/debug_token",
            params={"input_token": token, "access_token": token},
        )
    if r.status_code != 200:
        return []
    datos = r.json().get("data", {})
    ids: list[str] = []
    for permiso in datos.get("granular_scopes", []):
        if permiso.get("scope") in ("whatsapp_business_messaging", "whatsapp_business_management"):
            ids.extend(permiso.get("target_ids", []))
    return list(dict.fromkeys(ids))


async def _mostrar_estado(config: Config) -> int:
    """Lee la plantilla en Meta y la contrasta con el `.env`. No manda ningún mensaje."""
    nombre = config.plantilla_recordatorio
    idioma = config.plantillas_idioma
    print(f"  .env dice: plantilla='{nombre}'  idioma='{idioma}'")
    if not nombre:
        print("  FALLA: MAXICARE_PLANTILLA_RECORDATORIO está vacía. El despachador no mandaría.")
        return 1

    cuentas = await _waba_ids(config.whatsapp_token)
    if not cuentas:
        print("  AVISO: no se pudo derivar el WABA del token; me salto la comprobación previa.")
        print("         El envío real de abajo sigue siendo la prueba definitiva.")
        return 0

    for waba in cuentas:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(
                f"{BASE_GRAPH}/{waba}/message_templates",
                params={"name": nombre, "access_token": config.whatsapp_token},
            )
        if r.status_code != 200:
            continue
        for t in r.json().get("data", []):
            if t.get("name") != nombre:
                continue
            estado = t.get("status", "?")
            lang = t.get("language", "?")
            huecos = 0
            for c in t.get("components", []):
                if c.get("type") == "BODY":
                    texto = c.get("text", "")
                    huecos = len({p for p in texto.split("{{") if p[:1].isdigit()})
            marca = "OK" if estado == "APPROVED" else "FALLA"
            print(f"  {marca}: Meta dice status={estado} language={lang} categoria={t.get('category','?')}")
            print(f"        huecos en el BODY: {huecos}  (el despachador manda 4)")
            if lang != idioma:
                print(
                    f"  FALLA: el idioma del .env ('{idioma}') NO es el de Meta ('{lang}').\n"
                    f"         Meta rechazaría con 132001. Corrige "
                    f"MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA."
                )
                return 1
            return 0 if estado == "APPROVED" else 1

    print(f"  AVISO: no encontré la plantilla '{nombre}' en {len(cuentas)} cuenta(s).")
    print("         Puede ser que el token no tenga permiso de gestión; el envío lo dirá.")
    return 0


async def _enviar(config: Config, telefono: str) -> int:
    parametros = seguimientos.parametros_de(_fila_de_ejemplo())
    # `parametros_de` devuelve `list[str] | None` desde la tarea 4: `None` es la reactivacion
    # sin ningun nombre usable, que el despachador anula con motivo `sin_nombre` en vez de
    # mandar "Hola paciente". Hoy `_fila_de_ejemplo()` no fija `tipo`, asi que cae en la rama
    # del recordatorio y siempre devuelve cuatro -- pero `pytest -q` no corre este fichero, y
    # el dia que alguien le anada `tipo` al ejemplo esto reventaria con un `TypeError` en vez
    # de con un mensaje. Es exactamente la trampa que describe `.claude/rules/scripts-
    # entregables.md`.
    if parametros is None:
        print(
            "  FALLA: `parametros_de` devolvio None para la fila de ejemplo -- es una fila de\n"
            "         reactivacion sin ningun nombre usable, y el despachador la anularia con\n"
            "         motivo 'sin_nombre'. No hay nada que mandar; revisa `_fila_de_ejemplo`."
        )
        return 1
    print("  Los cuatro huecos, como los arma el despachador:")
    for i, valor in enumerate(parametros, start=1):
        print(f"    {{{{{i}}}}} = {valor!r}")
    print(f"  -> mandando '{config.plantilla_recordatorio}' a {telefono} ...")

    whatsapp = WhatsApp(config.whatsapp_token, config.whatsapp_phone_number_id)
    try:
        wamid = await whatsapp.enviar_plantilla(
            telefono,
            plantilla=config.plantilla_recordatorio,
            parametros=parametros,
            idioma=config.plantillas_idioma,
        )
    except ErrorDeCanal as e:
        crudo = str(e)
        print(f"  FALLA: {crudo}")
        for codigo, explicacion in _ERRORES.items():
            if f'"code":{codigo}' in crudo.replace(" ", "") or f"({codigo})" in crudo:
                print(f"  Qué significa el {codigo}: {explicacion}")
                break
        else:
            print("  Código no catalogado; el texto crudo de Meta está arriba.")
        return 1

    print(f"  OK: Meta aceptó el envío. wamid={wamid}")
    print("  Mira el teléfono: el mensaje llega con el header 'Recordatorio de su cita'")
    print("  y los dos botones 'Confirmar' y 'Necesito cambiarla'.")
    print("  AVISO: que Meta lo acepte no es que haya LLEGADO. Confírmalo en el teléfono.")
    return 0


async def principal() -> int:
    parser = argparse.ArgumentParser(description="Comprueba la plantilla de recordatorio.")
    parser.add_argument(
        "telefono",
        nargs="?",
        help="destino en formato internacional sin '+', p. ej. 573196842471",
    )
    parser.add_argument(
        "--estado",
        action="store_true",
        help="solo consulta cómo está la plantilla en Meta; no manda nada.",
    )
    args = parser.parse_args()

    cargar_dotenv()
    config = Config.desde_entorno()

    print("== Estado de la plantilla en Meta ==")
    codigo = await _mostrar_estado(config)
    if args.estado:
        return codigo
    if not args.telefono:
        print("\n  Sin teléfono no mando nada. Pasa uno para probar el envío de verdad.")
        return codigo
    if codigo != 0:
        print("\n  No sigo con el envío: la comprobación de arriba falló.")
        return codigo

    print("\n== Envío real ==")
    return await _enviar(config, args.telefono)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))

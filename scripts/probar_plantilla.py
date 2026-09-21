"""Comprueba las plantillas de Meta, y manda UNA de verdad si se pide.

    uv run python scripts/probar_plantilla.py --estado                        (gratis, LAS CUATRO)
    uv run python scripts/probar_plantilla.py 573196842471                    (MANDA un recordatorio)
    uv run python scripts/probar_plantilla.py 573196842471 --tipo sin_agendar (MANDA una reactivación)

Es el paso que va entre «Meta aprobó la plantilla» y `bash scripts/desplegar.sh`. No toca la
base de datos en ningún momento: ni lee `seguimientos` ni escribe una fila. Lo único que hace
es armar los huecos y llamar a Meta.

Cubre las CUATRO plantillas del proyecto. `--tipo` elige entre `recordatorio` --el default,
cuatro huecos-- y las tres de reactivación (`sin_agendar`, `cancelada`, `no_asistio`), que
llevan UNO. Hasta el 20/09/2026 este script estaba cableado a `config.plantilla_recordatorio`
y las tres de reactivación no tenían forma de probarse: habrían llegado al día del encendido
sin que nadie hubiera visto una sola en un teléfono. Es la trampa de siempre de `scripts/` --
dobla firmas a mano y `pytest -q` no lo corre--, solo que del lado de lo que NO cubría.

**Los parámetros salen de `seguimientos.parametros_de`, no de aquí.** Es
deliberado y es lo único que hace que esta prueba valga: escribir los literales a mano
comprobaría que Meta acepta *una* plantilla, no que acepta la que manda el despachador. Con el
formateador de verdad, un orden de huecos cambiado o la conversión de zona rota se ven en el
WhatsApp que llega al teléfono, que es donde el paciente los vería. Para las reactivaciones
eso incluye la cascada de tres fuentes del nombre (`_nombre_de_reactivacion`), que es
exactamente lo que produjo el «Hola paciente» del 100% de los envíos antes de la revisión.

Este script DOBLA la fila que el despachador lee de la base (`cita_inicio`, `nombre_completo`
y `tratamiento` para el recordatorio; `tipo`, `nombre_ficha` y `nombre_perfil` para las
reactivaciones): si esa consulta cambia de nombres de columna, aquí no se entera nadie. Ver
`.claude/rules/scripts-entregables.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

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
        "      en MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA (Spanish = 'es'; 'es_CO' es otro).\n"
        "      Esa variable gobierna LAS CUATRO plantillas pese a su nombre: la que esté\n"
        "      registrada con otro código falla el 100% de sus envíos."
    ),
    132000: (
        "el número de huecos que mandamos no coincide con el que Meta aprobó. Es el error\n"
        "      que sale al mandarle cuatro parámetros a una plantilla de reactivación, que\n"
        "      lleva UNO: revisa que `--tipo` sea el de la plantilla que estás probando."
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


class _Plantilla(NamedTuple):
    """Lo que hay que saber de una de las cuatro para probarla sin abrir la consola de Meta.

    `huecos` NO se usa para armar el envío --eso es de `seguimientos.parametros_de`, y
    duplicarlo aquí sería justo la trampa que el docstring del módulo dice evitar--. Se usa
    para CONTRASTAR: contra lo que `parametros_de` produce y contra lo que Meta dice que
    aprobó. Tres fuentes que tienen que coincidir, y si no, se dice antes de gastar.
    """

    tipo: str
    atributo: str
    variable: str
    huecos: int
    header: str
    botones: tuple[str, str]


#: Las cuatro, con el header y los botones EXACTOS de `docs/plantillas-meta-reactivacion.md`.
#: Se imprimen al final del envío para que quien mire el teléfono sepa qué tiene que ver: un
#: header que no llega o un botón con otro rótulo es un fallo, y sin esto habría que ir a
#: buscarlo al documento. Los rótulos van CON tildes, que es como están en Meta.
_PLANTILLAS: dict[str, _Plantilla] = {
    "recordatorio": _Plantilla(
        tipo=seguimientos.TIPO_RECORDATORIO,
        atributo="plantilla_recordatorio",
        variable="MAXICARE_PLANTILLA_RECORDATORIO",
        huecos=4,
        header="Recordatorio de su cita",
        botones=("Confirmar", "Necesito cambiarla"),
    ),
    "sin_agendar": _Plantilla(
        tipo=seguimientos.TIPO_SIN_AGENDAR,
        atributo="plantilla_sin_agendar",
        variable="MAXICARE_PLANTILLA_SIN_AGENDAR",
        huecos=1,
        header="Tu consulta en MaxiCare",
        botones=("Sí, me interesa", "Ya no, gracias"),
    ),
    "cancelada": _Plantilla(
        tipo=seguimientos.TIPO_CANCELADA,
        atributo="plantilla_cancelada",
        variable="MAXICARE_PLANTILLA_CANCELADA",
        huecos=1,
        header="Tu cita en MaxiCare",
        botones=("Sí, reagendar", "Ya no, gracias"),
    ),
    "no_asistio": _Plantilla(
        tipo=seguimientos.TIPO_NO_ASISTIO,
        atributo="plantilla_no_asistio",
        variable="MAXICARE_PLANTILLA_NO_ASISTIO",
        huecos=1,
        header="Tu cita en MaxiCare",
        botones=("Sí, reprogramar", "Ya no, gracias"),
    ),
}


def _fila_de_ejemplo(clave: str) -> dict[str, Any]:
    """La fila que el despachador leería de la base, para el tipo que se está probando.

    El recordatorio va con una cita de MAÑANA a propósito: se manda la víspera, y una hora
    futura deja el mensaje diciendo algo que el lector puede comprobar contra su reloj.

    Las tres reactivaciones NO llevan `cita_inicio` ni `tratamiento`, y eso no es un olvido:
    **toda fila de reactivación tiene `cita_id` NULL**, así que el `LEFT JOIN` a `citas` del
    SELECT real no produce ninguna de las dos. Poner aquí un `nombre_completo` --que es lo que
    ese JOIN traería-- haría pasar la prueba por el primer eslabón de la cascada, que en
    producción está vacío el 100% de las veces: exactamente el error que escondió el «Hola
    paciente» hasta la ronda 1 de revisión. Por eso cada tipo entra por el eslabón que le
    corresponde de verdad:

    - `sin_agendar`: un lead que preguntó y no agendó **no tiene ficha** (`crear_cita` es
      quien la registra), así que su único nombre es el de PERFIL de WhatsApp -- texto libre
      que escribió la propia persona, sin garantía de forma.
    - `cancelada` y `no_asistio`: esas personas sí agendaron alguna vez, así que tienen ficha
      y el nombre sale del segundo eslabón (`nombre_ficha`).
    """
    spec = _PLANTILLAS[clave]
    if spec.tipo == seguimientos.TIPO_RECORDATORIO:
        manana = datetime.now(seguimientos.jornada_zona()) + timedelta(days=1)
        return {
            "tipo": spec.tipo,
            "cita_inicio": manana.replace(hour=9, minute=0, second=0, microsecond=0),
            "nombre_completo": "Jean Chamorro",
            "tratamiento": "Limpieza dental",
        }
    if spec.tipo == seguimientos.TIPO_SIN_AGENDAR:
        return {"tipo": spec.tipo, "nombre_perfil": "Jean Chamorro"}
    return {"tipo": spec.tipo, "nombre_ficha": "Jean Chamorro"}


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


def _nombre_de(config: Config, clave: str, override: str | None) -> str:
    """El nombre de plantilla a usar: el de `--plantilla` si lo hay, si no el del `.env`."""
    return override or getattr(config, _PLANTILLAS[clave].atributo)


async def _mostrar_estado(
    config: Config, clave: str, *, exigir: bool = True, override: str | None = None
) -> int:
    """Lee UNA plantilla en Meta y la contrasta con el `.env`. No manda ningún mensaje.

    `exigir=False` es para el recorrido de las cuatro: una plantilla vacía es el MODO DE
    COMPROBACIÓN deliberado --el estado normal de las tres de reactivación mientras Meta no
    aprueba-- y no un fallo del sistema. Con `exigir=True` --el camino del envío-- sí lo es:
    ahí alguien pidió mandar una plantilla concreta y no hay ninguna configurada.
    """
    spec = _PLANTILLAS[clave]
    nombre = _nombre_de(config, clave, override)
    idioma = config.plantillas_idioma
    origen = "--plantilla" if override else ".env dice:"
    print(f"  [{clave}] {origen} plantilla='{nombre}'  idioma='{idioma}'")
    if not nombre:
        if not exigir:
            print(
                f"        {spec.variable} está vacía: modo de comprobación. El despachador\n"
                f"        decide y encola igual, y no manda nada de este tipo."
            )
            return 0
        print(f"  FALLA: {spec.variable} está vacía. El despachador no mandaría.")
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
            print(f"        huecos en el BODY: {huecos}  (el despachador manda {spec.huecos})")
            if lang != idioma:
                print(
                    f"  FALLA: el idioma del .env ('{idioma}') NO es el de Meta ('{lang}').\n"
                    f"         Meta rechazaría con 132001. Corrige "
                    f"MAXICARE_PLANTILLA_RECORDATORIO_IDIOMA."
                )
                return 1
            # El 132000 antes de gastar. Meta cuenta los huecos de su BODY aprobado; nosotros
            # sabemos cuántos manda el despachador para ESTE tipo. Que no cuadren es el
            # rechazo garantizado -- y con la fila marcada ANTES de enviar (no negociable 21),
            # cada envío así se pierde para siempre. Vale más decirlo aquí.
            if huecos != spec.huecos:
                print(
                    f"  FALLA: Meta aprobó {huecos} hueco(s) y el despachador manda "
                    f"{spec.huecos}.\n"
                    f"         Todo envío de este tipo se rechazaría con 132000, y la fila se\n"
                    f"         marca ANTES de enviar: se perdería. Corrige la plantilla en\n"
                    f"         WhatsApp Manager, no el código."
                )
                return 1
            return 0 if estado == "APPROVED" else 1

    print(f"  AVISO: no encontré la plantilla '{nombre}' en {len(cuentas)} cuenta(s).")
    print("         Puede ser que el token no tenga permiso de gestión; el envío lo dirá.")
    return 0


async def _enviar(
    config: Config, clave: str, telefono: str, override: str | None = None
) -> int:
    spec = _PLANTILLAS[clave]
    nombre_plantilla = _nombre_de(config, clave, override)
    parametros = seguimientos.parametros_de(_fila_de_ejemplo(clave))
    # `parametros_de` devuelve `list[str] | None` desde la tarea 4: `None` es la reactivacion
    # sin ningun nombre usable, que el despachador anula con motivo `sin_nombre` en vez de
    # mandar "Hola paciente". Con las filas de ejemplo de hoy no puede pasar --las tres llevan
    # un nombre con letras--, pero `pytest -q` no corre este fichero y quien toque
    # `_fila_de_ejemplo` o la cascada de `_nombre_de_reactivacion` tiene que verlo dicho y no
    # con un `TypeError`. Es la trampa que describe `.claude/rules/scripts-entregables.md`.
    if parametros is None:
        print(
            "  FALLA: `parametros_de` devolvio None para la fila de ejemplo -- es una fila de\n"
            "         reactivacion sin ningun nombre usable, y el despachador la anularia con\n"
            "         motivo 'sin_nombre'. No hay nada que mandar; revisa `_fila_de_ejemplo`."
        )
        return 1
    # El segundo contraste de los tres: lo que el formateador REAL produce contra lo que esta
    # tabla dice que Meta aprobo. El primero --Meta contra la tabla-- ya corrio en
    # `_mostrar_estado`; este caza el caso contrario, que `parametros_de` cambie y Meta no.
    if len(parametros) != spec.huecos:
        print(
            f"  FALLA: `parametros_de` devolvio {len(parametros)} hueco(s) para el tipo\n"
            f"         '{spec.tipo}' y la plantilla lleva {spec.huecos}. Meta rechazaria con\n"
            f"         132000. No mando nada."
        )
        return 1
    print(f"  Los huecos, como los arma el despachador para '{spec.tipo}':")
    for i, valor in enumerate(parametros, start=1):
        print(f"    {{{{{i}}}}} = {valor!r}")
    print(f"  -> mandando '{nombre_plantilla}' a {telefono} ...")

    whatsapp = WhatsApp(config.whatsapp_token, config.whatsapp_phone_number_id)
    try:
        wamid = await whatsapp.enviar_plantilla(
            telefono,
            plantilla=nombre_plantilla,
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
    print(f"  Mira el teléfono. Tiene que llegar con el header '{spec.header}'")
    print(f"  y los dos botones '{spec.botones[0]}' y '{spec.botones[1]}'.")
    print("  Cuatro cosas que SOLO se ven ahí y no en la consola de Meta: que las tildes")
    print("  lleguen bien, que el header aparezca, que los botones traigan ese rótulo exacto,")
    print("  y que el nombre esté en el hueco que le toca.")
    print("  AVISO: que Meta lo acepte no es que haya LLEGADO. Confírmalo en el teléfono.")
    return 0


async def principal() -> int:
    parser = argparse.ArgumentParser(description="Comprueba las plantillas de Meta.")
    parser.add_argument(
        "telefono",
        nargs="?",
        help="destino en formato internacional sin '+', p. ej. 573196842471",
    )
    parser.add_argument(
        "--tipo",
        choices=sorted(_PLANTILLAS),
        default="recordatorio",
        help="cuál de las cuatro plantillas probar. Por defecto, 'recordatorio'.",
    )
    parser.add_argument(
        "--plantilla",
        metavar="NOMBRE",
        help=(
            "usa ESTE nombre de plantilla en vez del que tenga el .env, solo para esta "
            "corrida. Es la forma de probar una plantilla recién aprobada SIN escribirla "
            "en el .env, que es lo que enciende los envíos de verdad."
        ),
    )
    parser.add_argument(
        "--estado",
        action="store_true",
        help="solo consulta cómo están LAS CUATRO en Meta; no manda nada.",
    )
    args = parser.parse_args()

    cargar_dotenv()
    config = Config.desde_entorno()

    # `--estado` recorre las CUATRO y no solo la de `--tipo`: desde que existen las tres de
    # reactivación, la pregunta que alguien se hace antes de desplegar no es «¿cómo está esta?»
    # sino «¿qué va a salir cuando esto arranque?». Una vacía NO es un fallo aquí --es el modo
    # de comprobación-- y por eso va con `exigir=False`.
    if args.estado:
        print("== Estado de las cuatro plantillas en Meta ==")
        peor = 0
        for clave in _PLANTILLAS:
            peor = max(peor, await _mostrar_estado(config, clave, exigir=False))
        configuradas = [c for c in _PLANTILLAS if getattr(config, _PLANTILLAS[c].atributo)]
        print(
            f"\n  Configuradas para ENVIAR: {len(configuradas)} de {len(_PLANTILLAS)}"
            f" -> {', '.join(configuradas) or 'ninguna'}"
        )
        if any(c != "recordatorio" for c in configuradas):
            print(
                "  AVISO: hay reactivaciones configuradas. En un servidor con este código,\n"
                "         esos nombres SON el encendido: no hay una segunda confirmación."
            )
        return peor

    print(f"== Estado de la plantilla '{args.tipo}' en Meta ==")
    if args.plantilla:
        print(
            "  AVISO: --plantilla manda sobre el .env, y SOLO en esta corrida. El .env no se\n"
            "         toca: ponerlo ahí, en un servidor con este código, es el encendido."
        )
    codigo = await _mostrar_estado(config, args.tipo, override=args.plantilla)
    if not args.telefono:
        print("\n  Sin teléfono no mando nada. Pasa uno para probar el envío de verdad.")
        return codigo
    if codigo != 0:
        print("\n  No sigo con el envío: la comprobación de arriba falló.")
        return codigo

    print("\n== Envío real ==")
    return await _enviar(config, args.tipo, args.telefono, args.plantilla)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))

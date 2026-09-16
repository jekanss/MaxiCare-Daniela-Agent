"""`/clearstate`: devolver un numero al estado de primer contacto.

Existe para poder probar a Daniela de verdad. Una conversacion de prueba solo vale si
empieza donde empieza la de un paciente real --sin nombre, sin identidad verificada, sin
historial-- y hasta ahora la unica forma de conseguir eso era estrenar un numero de
telefono.

LA GARANTIA, y es una sola frase: tras el reset, `atencion._leer_estado` devuelve para este
telefono los mismos ocho campos que para un numero que nunca ha escrito. El unico que puede
diferir es `id_conversacion`, que es un UUID nuevo por definicion. No es una aspiracion:
`tests/test_reseteo.py` lo comprueba comparando los dos estados campo a campo, y comprueba
ademas que el texto que recibe el modelo en el turno siguiente es identico al de un primer
contacto.

Por que se puede garantizar: todo lo que Daniela sabe de alguien al empezar un turno sale de
`_leer_estado`, que lee `pacientes`, `conversaciones` y `configuracion` --esta ultima es
global, no del paciente--. El historial del dialogo SI esta en Postgres desde la fase 7:
vive en `agent_sessions` / `agent_messages`, con `session_id = id_conversacion`, y
`persistencia.borrar_rastro` lo borra dentro de la MISMA transaccion que el resto del
rastro. Va antes del `DELETE FROM conversaciones` porque los `session_id` son esos ids: al
reves quedaria historial vivo de una conversacion que ya no existe. Y el campo que manda,
`identidad_verificada`, se calcula como `bool(paciente) or bool(verificada)`: sin fila en
`pacientes` vuelve a `False`.

LO QUE NO BORRA, dicho aqui para que nadie lo descubra despues:

1. **Las trazas de OpenAI que YA se subieron.** Desde la fase 6A hasta el 13/09/2026, cada
   conversacion --mensajes, nombre, argumentos de las tools-- subio integra al dashboard de
   OpenAI, porque ningun `RunConfig` pasaba `trace_include_sensitive_data=False`. La fuga ya
   esta cerrada (`config.config_de_corrida`), pero lo que salio antes sigue alla y este
   comando no lo toca: borra la base, no el dashboard de otra empresa.
2. **Los logs del contenedor**, que llevan el numero en claro y rotan solos.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: El texto exacto que dispara el reseteo.
COMANDO = "/clearstate"


def es_comando(texto: str | None) -> bool:
    """¿Este texto es el comando?

    Exacto y solo exacto, sin prefijos. `startswith` abriria la puerta a que una frase que
    empieza igual --«/clearstate y agendame para el martes»-- borrara la conversacion de
    alguien que no lo pidio, y el borrado no tiene deshacer.
    """
    if not texto:
        return False
    return texto.strip().lower() == COMANDO


def _solo_digitos(telefono: str) -> str:
    return "".join(c for c in telefono if c.isdigit())


def autorizado(telefono: str, permitidos: Sequence[str]) -> bool:
    """¿Este numero puede resetearse?

    Con `permitidos` vacio la respuesta es siempre `False`, y ese es el punto: la variable
    `MAXICARE_TELEFONOS_PRUEBA` nace vacia, de modo que desplegar esto en produccion no abre
    ninguna puerta. Quien quiera resetear un numero tiene que listarlo a proposito.

    La comparacion va por digitos porque las dos cadenas vienen de sitios distintos: en el
    `.env` el numero lo escribe una persona (`+57 300 123-4567`) y en el webhook llega como
    lo manda Meta (`573001234567`). Compararlas tal cual dejaria el comando sin funcionar por
    un `+`, y el fallo seria mudo: el texto se lo tragaria Daniela como un mensaje normal.
    """
    if not permitidos:
        return False
    mio = _solo_digitos(telefono)
    if not mio:
        return False
    return any(_solo_digitos(uno) == mio for uno in permitidos)


@dataclass
class Borrado:
    """Lo que se borro, para poder contarlo en la confirmacion y en las pruebas."""

    #: Filas por tabla. Las tablas que cascadean no aparecen: las borra Postgres.
    filas: dict[str, int] = field(default_factory=dict)
    #: Eventos de Google Calendar efectivamente eliminados.
    eventos: int = 0
    #: Mensajes de Telegram borrados, y si se borro el tema del paciente.
    mensajes_telegram: int = 0
    tema_borrado: bool = False
    #: Lo que se intento y fallo, en texto legible. No aborta el borrado: se informa.
    fallos: list[str] = field(default_factory=list)

    @property
    def total_filas(self) -> int:
        return sum(self.filas.values())


class ErrorDeReseteo(RuntimeError):
    """No se pudo borrar algo de fuera que no se puede dejar huérfano. No se tocó la base."""


async def resetear(
    telefono: str,
    *,
    database_url: str,
    telegram=None,
    calendario=None,
    conservar_wamid: str | None = None,
    bases_extra: Sequence[str] = (),
) -> Borrado:
    """Devuelve un número al estado de primer contacto. Ver el docstring del módulo.

    EL ORDEN ES LO EXTERNO PRIMERO, y no es arbitrario: mientras las filas sigan en la base,
    un borrado que se quedó a medias se puede reintentar. Al revés no -- borrada la fila de
    `citas`, su `evento_calendar_id` no existe en ningún sitio y el evento se queda en el
    calendario de la clínica ocupando un hueco que ya nadie puede cancelar desde el sistema.
    Es el mismo criterio con el que `herramientas._cancelar_cita` llama a Calendar antes de
    tocar Postgres.

    DE AHÍ UNA ASIMETRÍA DELIBERADA ENTRE LOS DOS SISTEMAS DE AFUERA:

    - **Si Google Calendar falla, esto ABORTA y no borra ni una fila.** Un evento huérfano
      ocupa un cupo real de la clínica, y un cupo ocupado por nadie es un paciente de verdad
      que no se puede agendar. El principio del proyecto decide el empate: la seguridad
      clínica prevalece sobre la comodidad de probar. Se arregla Calendar y se reintenta.
    - **Si Telegram falla, se informa y se sigue.** Un tema huérfano en el grupo de los
      doctores es ruido, no daño: no bloquea a nadie ni le miente a ningún paciente.

    `bases_extra` existe por `pruebas_web`, el esquema con las mismas tablas que `public` que
    alimenta el chat del panel y que no se purga nunca. Sin él, un número que alguna vez se
    probó desde la interfaz web seguiría siendo conocido por esa mitad del sistema.

    Ese borrado secundario **exige que `pruebas_web` tenga las migraciones al día**, y esa
    condición no se cumplía sola: hasta el 13/09/2026, el único sitio que lo actualizaba era
    `runtime._preparar_esquema_de_pruebas`, que es perezoso, y el esquema llevaba dos
    migraciones de retraso -- `borrar_rastro` reventaba ahí con `UndefinedColumn` y este
    docstring afirmaba un borrado que no ocurría. Lo pone al día el despliegue
    (`scripts/inicializar_base.py`), que además verifica las dos tablas del historial en los
    dos esquemas. Si aun así fallara, no se pierde nada de `public`: `resetear` lo anota en
    `Borrado.fallos` y sigue, que es la degradación correcta -- el carril de pruebas no puede
    bloquear el reseteo del carril real.
    """
    # Aquí y no arriba: `atencion` arrastra los agentes y el SDK, y `reseteo` se importa
    # también desde sitios que no los necesitan.
    from . import atencion

    borrado = Borrado()

    async with atencion.candado_de(telefono):
        rastro = await asyncio.to_thread(_rastro, database_url, telefono)

        # (1) Google Calendar. Aborta si falla: ver el docstring.
        if calendario is not None and rastro["eventos"]:
            for evento in rastro["eventos"]:
                try:
                    await asyncio.to_thread(calendario.eliminar_evento, evento)
                except Exception as exc:  # noqa: BLE001
                    raise ErrorDeReseteo(
                        f"no se pudo eliminar el evento {evento} de Google Calendar: {exc}. "
                        "No se borró nada de la base; arregla el calendario y repite el "
                        "comando."
                    ) from exc
                borrado.eventos += 1

        # (2) Telegram. Degrada: se informa y se sigue.
        if telegram is not None:
            for mensaje_id in rastro["mensajes_telegram"]:
                try:
                    await telegram.borrar_mensaje(mensaje_id)
                    borrado.mensajes_telegram += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("no se borró el mensaje %s de Telegram: %s", mensaje_id, exc)
                    borrado.fallos.append(f"mensaje {mensaje_id} de Telegram")
            if rastro["topic_id"]:
                try:
                    await telegram.borrar_tema(rastro["topic_id"])
                    borrado.tema_borrado = True
                except Exception as exc:  # noqa: BLE001
                    # Lo más probable es que al bot le falte `can_delete_messages`, que hoy
                    # ningún script comprueba.
                    log.warning("no se borró el tema %s: %s", rastro["topic_id"], exc)
                    borrado.fallos.append(f"tema {rastro['topic_id']} de Telegram")

        # (3) La base. Si esto falla, lo hace entero: `borrar_rastro` va en transacción.
        borrado.filas = await asyncio.to_thread(
            _borrar, database_url, telefono, conservar_wamid
        )
        for otra in bases_extra:
            try:
                extra = await asyncio.to_thread(_borrar, otra, telefono, None)
            except Exception as exc:  # noqa: BLE001
                # Una base secundaria que no responde no puede dejar sin resetear la
                # principal, que es la que decide cómo se comporta Daniela por WhatsApp.
                log.warning("no se pudo limpiar la base secundaria: %s", exc)
                borrado.fallos.append("base secundaria")
                continue
            for tabla, cuantas in extra.items():
                borrado.filas[tabla] = borrado.filas.get(tabla, 0) + cuantas

        # (4) La memoria del proceso, con el candado todavía cogido.
        atencion.olvidar(telefono)

    log.info(
        "reseteo de %s: %s filas, %s eventos, tema=%s",
        telefono,
        borrado.total_filas,
        borrado.eventos,
        borrado.tema_borrado,
    )
    return borrado


def _rastro(database_url: str, telefono: str) -> dict:
    from . import persistencia

    with persistencia.conectar(database_url) as conn:
        return persistencia.rastro_de(conn, telefono)


def _borrar(database_url: str, telefono: str, conservar_wamid: str | None) -> dict[str, int]:
    from . import persistencia

    with persistencia.conectar(database_url) as conn:
        return persistencia.borrar_rastro(conn, telefono, conservar_wamid=conservar_wamid)


#: Cómo se llama cada tabla cuando el conteo sale a un chat de WhatsApp: (singular, plural).
#:
#: Existe porque `confirmacion` componía el texto con las CLAVES del diccionario, que son
#: nombres de tabla. Mientras fueron `citas` o `conversaciones` se leía como español y nadie
#: lo miró; en la fase 7 entró `agent_sessions` --que no es ni español ni nuestro, sino del
#: SDK-- y quien probara por WhatsApp recibía «1 en agent_sessions». Un nombre interno en un
#: chat no es solo feo: invita a creer que el mensaje es un error del sistema.
#:
#: Las claves son EXACTAMENTE las que devuelve `persistencia.borrar_rastro`, y
#: `tests/test_reseteo.py` lo comprueba contra la función de verdad para que una tabla nueva
#: no pueda entrar aquí por la puerta de atrás.
ETIQUETAS_DE_TABLA: dict[str, tuple[str, str]] = {
    "mensajes_entrantes": ("mensaje", "mensajes"),
    "citas": ("cita", "citas"),
    "reservas": ("reserva", "reservas"),
    "agent_sessions": ("historial de conversación", "historiales de conversación"),
    "conversaciones": ("conversación", "conversaciones"),
    "pacientes": ("ficha tuya", "fichas tuyas"),
    # Desde la migración 014 el hilo de Telegram vive en su propia tabla y `borrar_rastro`
    # la cuenta aparte. Sin esta entrada, el paciente recibía «1 en temas_telegram».
    "temas_telegram": ("hilo tuyo en el grupo", "hilos tuyos en el grupo"),
    # La única fila que `borrar_rastro` no borra sino que edita: de `casos_sin_resolver` se
    # va la FRASE, y el caso se queda con su contador intacto. Por eso la etiqueta habla de
    # frases y no de casos: «1 caso sin resolver» le prometería al paciente un borrado que
    # no ocurrió, y de paso le contaría de una tabla que es de la clínica, no suya.
    "casos_sin_resolver": ("frase tuya", "frases tuyas"),
    # Otra fila que `borrar_rastro` edita en vez de borrar (migración 019): el contacto se
    # queda, y lo único que se resetea es el aviso de la política -- lo volverá a ver. El
    # `no_contactar`, si lo tenía puesto, NO se toca ni aparece aquí.
    "contactos_reseteados": ("aviso de política tuyo reiniciado", "avisos de política tuyos reiniciados"),
}


def _en_palabras(tabla: str, cuantas: int) -> str:
    """«12 mensajes», «1 ficha tuya». Una tabla sin etiqueta cae en algo legible y genérico
    en vez de filtrar su nombre: el texto va a un chat, no a un log."""
    singular, plural = ETIQUETAS_DE_TABLA.get(tabla, ("registro", "registros"))
    return f"{cuantas} {singular if cuantas == 1 else plural}"


def confirmacion(borrado: Borrado) -> str:
    """El texto que recibe por WhatsApp quien pidió el reseteo.

    Dice lo que se borró en vez de un «listo» a secas: el comando existe para poder confiar
    en que la siguiente conversación empieza de cero, y esa confianza se apoya en ver el
    conteo. Un reseteo que no borró nada --porque el número ya estaba limpio-- también tiene
    que decirlo, o parecería que falló.

    Lo dice en español, no en nombres de tabla: ver `ETIQUETAS_DE_TABLA`.
    """
    if borrado.total_filas == 0 and borrado.eventos == 0 and not borrado.tema_borrado:
        texto = "Ya no había nada que borrar de este número. Empiezas de cero igual."
    else:
        partes = [_en_palabras(tabla, n) for tabla, n in borrado.filas.items() if n]
        texto = "Listo, borré todo lo tuyo: " + ", ".join(partes) + "."
        if borrado.eventos:
            texto += f" Y {borrado.eventos} cita(s) del calendario."
        if borrado.tema_borrado:
            texto += " Tu hilo de Telegram también."
    if borrado.fallos:
        texto += " No pude con: " + ", ".join(borrado.fallos) + "."
    return texto + " Escríbeme como si no me conocieras."


__all__ = [
    "COMANDO",
    "ETIQUETAS_DE_TABLA",
    "Borrado",
    "ErrorDeReseteo",
    "autorizado",
    "confirmacion",
    "es_comando",
    "resetear",
]

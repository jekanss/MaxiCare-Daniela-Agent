"""El entregable de la fase 7: una conversacion sobrevive a reiniciar el proceso.

    uv run python scripts/probar_persistencia.py            # el cableado. No gasta.
    uv run python scripts/probar_persistencia.py --chat     # el reinicio de verdad. GASTA.

Sin `--chat` comprueba lo que se puede comprobar sin modelo: que las tablas de la 010 estan
aplicadas, que la fabrica devuelve una sesion persistida y no una de memoria, que el
esquema se traduce, y que un ida y vuelta contra Neon guarda y recupera lo que se le puso.

Con `--chat` hace lo que la frase del plan pide de verdad:

    proceso A (interprete propio)  ->  atencion.atender("hola, soy Ana")  ->  MUERE
    ---------------------------------------------------------------------------------
    proceso B (interprete NUEVO, memoria vacia POR CONSTRUCCION)
                                   ->  atencion.atender("como me llamo?")
                                   ->  la respuesta contiene "Ana"

Por que dos interpretes y no un `uvicorn` al que se le manda SIGTERM
------------------------------------------------------------------------------------------
Porque el reinicio hay que hacerlo sobre el carril que el entregable describe, que es el de
WhatsApp, y matar un uvicorn no lo prueba: el chat web PIERDE el reenganche al reiniciar por
diseno documentado y deliberado --ver el docstring de `runtime._conversaciones_de_prueba`--.
Ese diccionario es lo unico que traduce un `id_conversacion` conocido a su contexto; vacio
tras el reinicio, el id deja de reconocerse y `asegurar_conversacion`, que SIEMPRE inserta,
abre una fila distinta. Un reinicio por ahi probaria lo contrario de lo que aqui se afirma, y
ademas arrastraria el login del panel y sus cookies, que no son la fase 7.

Con dos interpretes la garantia es mas fuerte que con un SIGTERM: la memoria del segundo
proceso esta vacia por construccion, no por haberla vaciado. Si Daniela recuerda el nombre,
solo puede haberlo leido de Neon.

La comprobacion que le da el peso: el nombre NO esta en `pacientes`
------------------------------------------------------------------------------------------
Daniela sabe el nombre de un paciente por dos caminos: el estado estructurado --la tabla
`pacientes`, que `_leer_estado` lee en cada turno-- y el historial del dialogo. Si el primero
estuviera lleno, el segundo turno podria acertar con el historial borrado y esta prueba diria
OK sin probar nada de la fase 7. Por eso se comprueba que `pacientes` sigue sin fila para ese
telefono: con esa puerta cerrada, lo unico que puede llevar el nombre de un proceso al
siguiente es `agent_messages`.

Donde escribe
------------------------------------------------------------------------------------------
En el esquema `pruebas_persistencia`, que este script crea al empezar y BORRA al terminar,
comprobando el borrado con una asercion --un `DROP SCHEMA` que no se verifica es una promesa,
no un hecho--. NUNCA en `public`, donde hay pacientes reales de una clinica: la MITAD A
cuenta las filas de `public.agent_messages` antes y despues y exige que no se haya movido
ninguna.

El WhatsApp es falso en las dos modalidades: captura en vez de enviar, y la API de Meta no se
toca. Telegram y Google van vacios a proposito, para que un turno que decidiera escalar no le
haga sonar el telefono a un doctor de verdad, y el calendario es un `CalendarioDoble()`
explicito --el unico caso en que el doble es legitimo, porque aqui no hay cupo real que
confirmarle a nadie--.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import selectors
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Coroutine, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()

from maxicare_daniela import atencion, conversacion, ingesta, persistencia  # noqa: E402
from maxicare_daniela.calendario import CalendarioDoble  # noqa: E402
from maxicare_daniela.config import Config  # noqa: E402

#: Ni `pruebas` (lo borran probar_tools.py y probar_agentes.py), ni `pruebas_atencion`, ni
#: `pruebas_lectura`, ni `pruebas_web`. Uno propio, para que dos entregables puedan correr a
#: la vez sin pisarse.
ESQUEMA = "pruebas_persistencia"

#: Un movil colombiano valido en forma y de mentira de verdad: el `WhatsAppFalso` no envia
#: nada a ningun numero. Tiene que ser EL MISMO en los dos procesos: es por telefono como
#: `conversacion_viva` encuentra la conversacion que el proceso anterior dejo abierta.
TEL_REINICIO = "573009900101"

#: El nombre que el primer proceso dice y el segundo tiene que recordar.
NOMBRE = "Ana"

#: Marca unica por corrida, para que ningun wamid de una corrida choque con el de otra.
MARCA = uuid.uuid4().hex[:8]

_T = TypeVar("_T")

fallos = 0


def revisar(etiqueta: str, condicion: bool, detalle: str = "") -> None:
    global fallos
    marca = "OK  " if condicion else "FALLA"
    print(f"  {marca} {etiqueta}" + (f" -- {detalle}" if detalle else ""))
    if not condicion:
        fallos += 1


def _correr(corutina: Coroutine[Any, Any, _T]) -> _T:
    """`asyncio.run`, salvo en Windows, donde fuerza un `SelectorEventLoop`.

    En Windows `asyncio.run` arranca un `ProactorEventLoop` y el modo async de psycopg lo
    rechaza en el propio `connect()`: `InterfaceError: Psycopg cannot use the
    'ProactorEventLoop' to run in async mode`. En Linux --donde corre el VPS-- el loop por
    defecto ya es el selector y esto no hace nada. Es el mismo envoltorio que
    `tests/test_sesion_neon.py`, y por la misma razon: no se toca la politica global de
    `asyncio`, solo el loop de esta corrida.
    """
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        try:
            return loop.run_until_complete(corutina)
        finally:
            loop.close()
    return asyncio.run(corutina)


# ==========================================================================================
# Conexion
# ==========================================================================================


def urls() -> tuple[str, str]:
    """(conexion directa a la base, conexion directa con `search_path` en el esquema).

    El `-pooler.` se le quita al host porque PgBouncer rechaza `options` como parametro de
    arranque: `unsupported startup parameter in options: search_path`. La primera devuelve la
    base sin fijar esquema --hace falta para el `CREATE SCHEMA`, para el `DROP` y para contar
    lo que hay en `public`--; la segunda es la que usan las comprobaciones y la que se le
    pasa al proceso hijo.
    """
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        print("ERROR: falta MAXICARE_DATABASE_URL en .env", file=sys.stderr)
        raise SystemExit(1)
    directa = base.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return directa, f"{directa}{sep}options=-csearch_path%3D{ESQUEMA}"


def enmascarar(url: str) -> str:
    """`***@host` -- una cadena de conexion no se imprime entera nunca."""
    if "@" not in url:
        return "***"
    return "***@" + url.split("@", 1)[1].split("?", 1)[0]


def sin_secretos(texto: str, url: str) -> str:
    """El mismo texto con la contrasena de la base tachada.

    Existe para la salida del proceso hijo: si el hijo revienta, su traza se imprime para
    poder leerla, y una traza de SQLAlchemy o de psycopg puede arrastrar la cadena de
    conexion entera. La contrasena de la Neon de la clinica no se imprime ni en un fallo.
    """
    clave = urlsplit(url).password
    if not clave:
        return texto
    return texto.replace(clave, "***")


def sin_options(url: str) -> str:
    """La misma URL sin el parametro de consulta `options`, con todo lo demas intacto.

    No vale un `split("?options=")`: la URL de Neon YA trae query (`sslmode`,
    `channel_binding`), asi que `options` entro con `&` y ese corte devolveria la URL entera.
    Hace falta de verdad para la comprobacion A4: si la sesion viajara con `search_path` Y con
    `schema_translate_map` a la vez, A4 pasaria igual con `schema_translate_map` borrado de
    `_engine_de`, porque el `search_path` mandaria la fila al mismo sitio. Es la misma trampa
    que ya costo una revision en `tests/test_sesion_neon.py`.
    """
    partes = urlsplit(url)
    query = [
        (clave, valor)
        for clave, valor in parse_qsl(partes.query, keep_blank_values=True)
        if clave != "options"
    ]
    return urlunsplit(partes._replace(query=urlencode(query)))


def una_fila(url: str, sql: str, parametros: tuple = ()) -> tuple:
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute(sql, parametros)
        fila = cur.fetchone()
    return fila if fila is not None else ()


# ==========================================================================================
# Los dobles -- ninguno habla con nadie de fuera
# ==========================================================================================


class WhatsAppFalso:
    """Captura en vez de enviar. Ningun paciente recibe nada, ni un doble check azul."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []
        self.leidos: list[str] = []

    async def marcar_leido(self, wamid: str) -> None:
        self.leidos.append(wamid)

    async def enviar_texto(self, telefono: str, texto: str) -> str:
        self.enviados.append((telefono, texto))
        return f"wamid.salida.{uuid.uuid4().hex[:12]}"


class TelegramFalso:
    """`atender` no lo usa en el camino normal, pero lo pide la firma. Que sea un doble es lo
    que garantiza que una prueba nunca le suene el telefono a un doctor de verdad."""

    def __init__(self) -> None:
        self.mensajes: list[str] = []

    async def enviar_mensaje(self, texto: str, *, tema_id=None, teclado=None) -> int:
        self.mensajes.append(texto)
        return len(self.mensajes)


class DormirFalso:
    """Se queda con los segundos que le pidieron dormir, sin dormirlos.

    `RETARDO_RESPUESTA_SEGUNDOS` llega a 55: un entregable que los esperara de verdad, dos
    veces, es un entregable que alguien acaba saltandose.
    """

    def __init__(self) -> None:
        self.dormidas: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.dormidas.append(segundos)


# ==========================================================================================
# MITAD A -- el cableado, sin gastar un token
# ==========================================================================================


def _tablas_del_esquema(directa: str) -> set[str]:
    with persistencia.conectar(directa) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (ESQUEMA,),
        )
        return {fila[0] for fila in cur.fetchall()}


def _cuenta(url: str, sql: str, parametros: tuple = ()) -> int:
    fila = una_fila(url, sql, parametros)
    return int(fila[0]) if fila else -1


def mitad_a(directa: str, url: str) -> None:
    print("\n" + "=" * 78)
    print("MITAD A -- el cableado (no gasta un token)")
    print("=" * 78)

    from agents.extensions.memory import SQLAlchemySession

    tablas = _tablas_del_esquema(directa)
    revisar(
        "la 010 dejo `agent_sessions` y `agent_messages` en el esquema de pruebas",
        {"agent_sessions", "agent_messages"} <= tablas,
        f"hay {sorted(tablas & {'agent_sessions', 'agent_messages'})}",
    )

    sesion = persistencia.sesion_de_agente(
        "conv-cableado", database_url=sin_options(url), esquema=ESQUEMA
    )
    revisar(
        "la fabrica devuelve una sesion PERSISTIDA, no una de memoria",
        isinstance(sesion, SQLAlchemySession)
        and not isinstance(sesion, conversacion.SesionEnMemoria),
        type(sesion).__name__,
    )
    revisar(
        "no crea sus tablas: las crea la migracion",
        sesion._create_tables is False,
    )
    revisar(
        "guarda los acentos sin escapar (`ensure_ascii=False`)",
        sesion._ensure_ascii is False,
    )
    opciones = sesion._engine.sync_engine.get_execution_options()
    revisar(
        "el esquema se traduce al compilar (`schema_translate_map`), no por `search_path`",
        opciones.get("schema_translate_map") == {None: ESQUEMA},
        str(opciones.get("schema_translate_map")),
    )

    # ---------------------------------------------------------------------------------
    # A4: el ida y vuelta por `schema_translate_map`, que es el camino de produccion.
    # ---------------------------------------------------------------------------------
    puestos = [
        {"role": "user", "content": "quiero una valoracion de ortodoncia"},
        {"role": "assistant", "content": "Claro, con mucho gusto. ¿Qué dia te sirve?"},
    ]

    async def ida_y_vuelta():
        await sesion.add_items(puestos)
        return await sesion.get_items()

    try:
        vueltos = _correr(ida_y_vuelta())
    finally:
        _correr(persistencia.cerrar_engines())

    revisar(
        "un ida y vuelta contra Neon devuelve lo mismo que se le puso",
        [i.get("content") for i in vueltos] == [i["content"] for i in puestos],
        f"{len(vueltos)} items",
    )
    revisar(
        "los acentos vuelven sin escapar",
        any("¿Qué" in str(i.get("content")) for i in vueltos),
    )

    filas = _cuenta(
        url,
        "SELECT count(*) FROM agent_messages WHERE session_id = %s",
        ("conv-cableado",),
    )
    revisar(
        "las filas cayeron en el esquema de pruebas",
        filas == len(puestos),
        f"{filas} filas en {ESQUEMA}.agent_messages",
    )


def mitad_a_no_toco_public(directa: str, antes: tuple[int, int]) -> None:
    """La segunda mitad de la comprobacion que importa: no basta con que las filas esten en
    `pruebas_persistencia`; tienen que NO estar en `public`, que es la base de la clinica."""
    ahora = (
        _cuenta(directa, "SELECT count(*) FROM public.agent_sessions"),
        _cuenta(directa, "SELECT count(*) FROM public.agent_messages"),
    )
    revisar(
        "no cayo ni una fila en `public.agent_sessions` / `public.agent_messages`",
        ahora == antes,
        f"antes {antes}, ahora {ahora}",
    )


# ==========================================================================================
# MITAD B -- el reinicio de verdad (GASTA TOKENS)
# ==========================================================================================


def _turno_en_proceso_nuevo(url: str, texto: str) -> dict:
    """Un turno completo de WhatsApp en un interprete de Python RECIEN ARRANCADO.

    Se lanza este mismo archivo con `--turno-hijo`, que es un `main` distinto: no comparte ni
    un byte de memoria con el proceso que lo llama. Ahi esta el reinicio -- `atencion._buferes`
    y `atencion._candados` nacen vacios, y la unica forma de que el segundo turno sepa algo
    del primero es leerlo de Neon.

    La URL viaja por el ENTORNO y no por la linea de ordenes: los argumentos de un proceso
    los lee cualquiera en la lista de procesos de la maquina, y ahi va la contrasena de la
    base. `cargar_dotenv` no pisa lo que ya esta en el entorno, asi que el hijo se queda con
    el esquema de pruebas y nunca con el `public` del `.env`.
    """
    entorno = dict(os.environ)
    entorno["MAXICARE_DATABASE_URL"] = url
    entorno["PYTHONIOENCODING"] = "utf-8"

    proceso = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--turno-hijo", "--texto", texto],
        cwd=str(RAIZ),
        env=entorno,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    for linea in (proceso.stdout or "").splitlines():
        if linea.startswith("RESULTADO_JSON "):
            return json.loads(linea[len("RESULTADO_JSON ") :])

    print("  ---- el proceso hijo no devolvio resultado; su salida:")
    for linea in ((proceso.stdout or "") + "\n" + (proceso.stderr or "")).splitlines():
        if linea.strip():
            print("       " + sin_secretos(linea, url))
    return {"error": f"el hijo salio con codigo {proceso.returncode}"}


def _dice_el_nombre(texto: str) -> bool:
    """«Ana» como palabra, no como trozo de otra.

    `"ana" in texto` diria que si con «manana» o «mañana», que es justo la palabra que mas
    aparece en una conversacion de agendamiento. El limite de palabra lo cierra.
    """
    return re.search(rf"\b{NOMBRE}\b", texto, re.IGNORECASE) is not None


def mitad_b(directa: str, url: str) -> None:
    print("\n" + "=" * 78)
    print("MITAD B -- el reinicio de verdad: dos interpretes (GASTA TOKENS)")
    print("=" * 78)

    print(f"  ... proceso A: <<hola, soy {NOMBRE}>>")
    a = _turno_en_proceso_nuevo(url, f"Hola, soy {NOMBRE}. Queria preguntar por ortodoncia.")
    revisar(
        "el proceso A contesto",
        bool(a.get("texto_enviado")) and not a.get("error"),
        str(a.get("error") or a.get("motivo") or "")[:160],
    )
    if not a.get("texto_enviado"):
        return
    print(f"       A (pid {a.get('pid')}) dijo: {a['texto_enviado'][:160]}")

    guardadas = _cuenta(
        url,
        "SELECT count(*) FROM agent_messages WHERE session_id = %s",
        (a.get("id_conversacion"),),
    )
    revisar(
        "el proceso A dejo su historial en Neon antes de morir",
        guardadas > 0,
        f"{guardadas} items en {ESQUEMA}.agent_messages",
    )
    con_el_nombre = _cuenta(
        url,
        "SELECT count(*) FROM agent_messages WHERE session_id = %s "
        "  AND message_data ILIKE %s",
        (a.get("id_conversacion"), f"%{NOMBRE}%"),
    )
    revisar(
        "el nombre esta en el historial guardado",
        con_el_nombre > 0,
        f"{con_el_nombre} items lo mencionan",
    )

    # La puerta que hay que cerrar para que el segundo turno pruebe algo: si el nombre
    # estuviera en `pacientes`, `_leer_estado` se lo daria al modelo en el contexto del turno
    # y Daniela acertaria con el historial borrado.
    pacientes = _cuenta(
        url, "SELECT count(*) FROM pacientes WHERE telefono = %s", (TEL_REINICIO,)
    )
    revisar(
        "el nombre NO esta en `pacientes`: lo unico que lo lleva es el historial",
        pacientes == 0,
        ""
        if pacientes == 0
        else f"{pacientes} filas en pacientes -- el segundo turno puede acertar SIN historial "
        f"y esta comprobacion dejaria de probar la fase 7",
    )

    print(f"  ... proceso B (interprete NUEVO): <<¿como me llamo?>>")
    b = _turno_en_proceso_nuevo(url, "Una cosa, ¿te acuerdas de como me llamo?")
    revisar(
        "el proceso B contesto",
        bool(b.get("texto_enviado")) and not b.get("error"),
        str(b.get("error") or b.get("motivo") or "")[:160],
    )
    if not b.get("texto_enviado"):
        return
    print(f"       B (pid {b.get('pid')}) dijo: {b['texto_enviado'][:300]}")

    revisar(
        "son DOS procesos distintos, no uno reusado",
        a.get("pid") != b.get("pid"),
        f"pid A {a.get('pid')}, pid B {b.get('pid')}",
    )
    revisar(
        "los dos turnos cayeron en la MISMA conversacion",
        a.get("id_conversacion") == b.get("id_conversacion") and b.get("id_conversacion"),
        f"A {a.get('id_conversacion')}, B {b.get('id_conversacion')}",
    )
    # Esto NO prueba la fase 7 y esta dicho aqui a proposito: `turno_actual` sale de
    # `conversaciones`, que es estado estructurado y ya sobrevivia al reinicio antes de esta
    # fase --se comprobo rompiendo la sesion a memoria: esta linea seguia en OK--. Lo que
    # prueba es que el segundo proceso reengancho la MISMA conversacion en vez de abrir una
    # nueva, que es la condicion previa para que el historial pueda encontrarse.
    revisar(
        "el proceso B reengancho la conversacion: para el es el turno 2, no el 1",
        b.get("turno") == 2,
        f"turno {b.get('turno')}",
    )
    revisar(
        f"LA FRASE DEL PLAN: tras reiniciar, Daniela sigue sabiendo que se llama {NOMBRE}",
        _dice_el_nombre(b["texto_enviado"]),
        b["texto_enviado"][:300],
    )


# ==========================================================================================
# El proceso hijo -- un turno y a morir
# ==========================================================================================


def turno_hijo(texto: str) -> int:
    """Un turno de WhatsApp de punta a punta y salir. Lo llama `_turno_en_proceso_nuevo`.

    Esto NO es un modo de uso del script: es la otra mitad del reinicio. Corre `atencion.
    atender` tal cual lo corre produccion --sin espia sobre `conversacion.responder`, con el
    modelo de verdad-- y con la sesion por defecto, que es `persistencia.sesion_de_agente`
    contra la `MAXICARE_DATABASE_URL` del entorno. Lo unico falso es el transporte: WhatsApp
    captura, Telegram captura, el calendario es doble y el retardo no se duerme.
    """
    url = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not url:
        print("ERROR: el proceso hijo no recibio MAXICARE_DATABASE_URL", file=sys.stderr)
        return 1

    cfg = dataclasses.replace(
        Config.desde_entorno(),
        database_url=url,
        telegram_bot_token="",
        telegram_chat_doctores="",
        google_sa_b64="",
        google_calendar_id="",
        daniela_responde=True,
    )

    mensaje = ingesta.MensajeEntrante(
        wamid=f"wamid.entrada.{MARCA}.{uuid.uuid4().hex[:12]}",
        telefono=TEL_REINICIO,
        # El nombre del perfil lo escribe el propio desconocido, y `_leer_estado` no lo
        # trata como identidad. Que sea evidentemente falso importa: si el nombre saliera de
        # aqui, el segundo turno acertaria sin historial.
        nombre_perfil="Perfil Que Escribio El Desconocido",
        tipo="text",
        texto=texto,
        media_id=None,
        mime=None,
        nombre_archivo=None,
    )
    ingesta._registrar(url, mensaje)

    wa = WhatsAppFalso()

    async def correr():
        try:
            return await atencion.atender(
                mensaje,
                whatsapp=wa,
                telegram=TelegramFalso(),
                config=cfg,
                calendario=CalendarioDoble(),
                dormir=DormirFalso(),
                ventana=0,
                tope=0,
            )
        finally:
            await persistencia.cerrar_engines()

    atendido = _correr(correr())

    print(
        "RESULTADO_JSON "
        + json.dumps(
            {
                "pid": os.getpid(),
                "wamid": atendido.wamid,
                "id_conversacion": atendido.id_conversacion,
                "respondido": atendido.respondido,
                "texto_enviado": atendido.texto_enviado,
                "turno": atendido.turno,
                "motivo": atendido.motivo,
                "enviados": len(wa.enviados),
            },
            ensure_ascii=False,
        )
    )
    return 0


# ==========================================================================================
# main
# ==========================================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Entregable de la fase 7: una conversacion sobrevive a reiniciar el proceso. "
            "Escribe en el esquema de pruebas '" + ESQUEMA + "' y lo borra al terminar; "
            "nunca toca 'public'. No le envia nada a ningun paciente."
        )
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help=(
            "Levanta dos procesos de verdad y hace el reinicio contra el modelo. "
            "GASTA TOKENS."
        ),
    )
    parser.add_argument(
        "--turno-hijo",
        action="store_true",
        help="Uso interno: corre UN turno en este interprete y sale. No se llama a mano.",
    )
    parser.add_argument("--texto", default="", help="Uso interno: el texto del turno hijo.")
    args = parser.parse_args()

    if args.turno_hijo:
        return turno_hijo(args.texto)

    directa, url = urls()
    if args.chat and not os.environ.get("OPENAI_API_KEY", "").strip():
        print("ERROR: --chat necesita OPENAI_API_KEY en .env", file=sys.stderr)
        return 1

    print(f"Base: {enmascarar(directa)}")
    print(f"Esquema de pruebas: {ESQUEMA} (se borra al terminar; 'public' no se toca)")
    print(
        "Modo: "
        + (
            "--chat, dos procesos contra el modelo de verdad. GASTA TOKENS."
            if args.chat
            else "sin --chat, solo el cableado. No gasta un token."
        )
    )

    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)
        persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())

    publico_antes = (
        _cuenta(directa, "SELECT count(*) FROM public.agent_sessions"),
        _cuenta(directa, "SELECT count(*) FROM public.agent_messages"),
    )

    empezado = time.monotonic()
    try:
        mitad_a(directa, url)
        if args.chat:
            mitad_b(directa, url)
        else:
            print("\n  (MITAD B omitida: hace falta --chat, que gasta tokens)")
        mitad_a_no_toco_public(directa, publico_antes)
    finally:
        print("\n" + "=" * 78)
        print("Limpieza -- el esquema de pruebas tiene que desaparecer")
        print("=" * 78)
        # La limpieza se COMPRUEBA. Un `DROP SCHEMA` sin verificar es una promesa: si la
        # conexion se cae a mitad, el esquema queda con datos de prueba dentro y el script
        # diria OK igual. Y el fallo pasa por `revisar`, no por un `print`, para que cuente
        # en el resumen y en el codigo de salida.
        try:
            with persistencia.conectar(directa) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
                conn.commit()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM information_schema.schemata "
                        " WHERE schema_name = %s",
                        (ESQUEMA,),
                    )
                    quedan = cur.fetchone()[0]
            revisar(
                f"el esquema '{ESQUEMA}' ya no existe",
                quedan == 0,
                ""
                if quedan == 0
                else f"QUEDO EN PIE con los datos de prueba dentro "
                f"-- borralo a mano: DROP SCHEMA {ESQUEMA} CASCADE",
            )
        except Exception as e:  # noqa: BLE001
            revisar(
                f"el esquema '{ESQUEMA}' ya no existe",
                False,
                f"no se pudo comprobar ni borrar ({e}) -- revisalo a mano: "
                f"DROP SCHEMA {ESQUEMA} CASCADE",
            )

    print("\n" + "=" * 78)
    print(
        "FASE 7 -- una conversacion sobrevive a reiniciar el proceso: "
        + ("OK" if fallos == 0 else f"{fallos} FALLAS")
        + f"  ({time.monotonic() - empezado:.1f}s)"
    )
    print("=" * 78)
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

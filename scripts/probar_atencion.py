"""El entregable de la fase 6A: el turno de WhatsApp de punta a punta, contra Neon.

    uv run python scripts/probar_atencion.py            # sin gastar un token
    uv run python scripts/probar_atencion.py --chat     # el modelo de verdad. GASTA TOKENS.

Que hace y que NO hace
------------------------------------------------------------------------------------------
Corre `atencion.atender` --el turno completo-- contra Neon de verdad, pero con un
`WhatsAppFalso` que CAPTURA en vez de enviar. Ningun paciente recibe nada, ni siquiera un
doble check azul: la API de Meta no se toca en ninguna de las dos modalidades.

Sin `--chat` tampoco se gasta un token: `conversacion.responder` se sustituye por un espia
que fabrica la respuesta. Con `--chat` ese mismo espia DELEGA en el `responder` de verdad
--el modelo contesta, con sus guardrails y sus tools-- y sigue capturando lo mismo, asi que
las comprobaciones valen igual en las dos modalidades. **Con `--chat` esto gasta tokens.**

Dos comprobaciones corren SIEMPRE con la respuesta fabricada, tambien bajo `--chat`, y esta
dicho donde toca: la 4 (serializacion) y la 5 (retardo) miden el transporte, y para medirlo
hace falta controlar cuanto tarda el turno. Un modelo que tarda entre cuatro y doce segundos
no permite afirmar nada sobre un descuento de un segundo.

Donde escribe
------------------------------------------------------------------------------------------
En el esquema `pruebas_atencion`, que este script crea al empezar y BORRA al terminar. Nunca
en `public`, donde hay pacientes reales de una clinica: `atender` hace `INSERT` en
`conversaciones`, `UPDATE` sobre `mensajes_entrantes` y escribe pacientes. El aislamiento es
fisico y no una convencion de nombres --la conexion lleva `options=-csearch_path=...`, asi
que una consulta sin prefijo no puede tocar `public` ni queriendo-- y exige la conexion
DIRECTA de Neon, sin el `-pooler.` del host: PgBouncer rechaza `options` como parametro de
arranque.

El esquema no es `pruebas` a proposito: ese lo borran `probar_tools.py` y `probar_agentes.py`
al terminar, y tampoco es `pruebas_web`, que es el carril del chat de la pantalla. Tres
carriles distintos que no se pisan.

**La limpieza va en un `finally` y se COMPRUEBA con una asercion**: al final se le pregunta a
`information_schema` si el esquema sigue ahi. Un `DROP SCHEMA` que no se verifica es una
promesa, no un hecho.

Por que cada comprobacion esta partida en dos mitades
------------------------------------------------------------------------------------------
Una comprobacion que pasaria igual si la funcion devolviera siempre lo mismo no comprueba
nada. Por eso:

  * la 2 mira que el turno valga 1 despues del primer mensaje Y 2 despues del segundo: un
    `tocar_conversacion` que escribiera siempre `2` --o el bug real que arreglo esta fase,
    que no escribia nada y dejaba la columna en 0-- cae en una de las dos;
  * la 3 mira un numero registrado Y uno que no: un `buscar_paciente_por_telefono` que
    devolviera siempre algo pasaria la primera mitad sola;
  * la 4 mira el mismo telefono Y dos telefonos distintos: un candado global pasaria la
    primera mitad sola;
  * la 5 compara un turno instantaneo contra uno que tarda un segundo, y exige que la
    diferencia entre los dos retardos sea ESE segundo: un retardo que no descontara nada
    daria la misma cifra en los dos casos;
  * la 7 apaga el interruptor Y vuelve a encenderlo con el mismo telefono: un `atender` que
    no respondiera nunca pasaria la primera mitad sola;
  * la 8 compara el texto de un mensaje con archivo contra el de uno de solo texto: buscar
    una frase que estuviera en los dos no probaria nada.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()

from maxicare_daniela import atencion, conversacion, ingesta, persistencia  # noqa: E402
from maxicare_daniela.calendario import CalendarioDoble  # noqa: E402
from maxicare_daniela.config import RETARDO_RESPUESTA_SEGUNDOS, Config  # noqa: E402
from maxicare_daniela.contratos import RespuestaDaniela  # noqa: E402

#: Ni `pruebas` (lo borran probar_tools.py y probar_agentes.py) ni `pruebas_web` (el carril
#: del chat de la pantalla). Uno propio, para que dos entregables puedan correr a la vez.
ESQUEMA = "pruebas_atencion"

#: Telefonos de mentira, uno por comprobacion. Son de mentira de verdad: el prefijo 5730099
#: es un movil colombiano valido en forma, pero el `WhatsAppFalso` no envia nada a ninguno.
TEL_DESCONOCIDO = "573009900001"
TEL_REGISTRADO = "573009900002"
TEL_ANONIMO = "573009900003"
TEL_SERIE = "573009900004"
TEL_PARALELO_A = "573009900005"
TEL_PARALELO_B = "573009900006"
TEL_RETARDO = "573009900007"
TEL_RASTRO = "573009900008"
TEL_APAGADO = "573009900009"
TEL_ADJUNTO = "573009900010"

NOMBRE_REGISTRADO = "Laura Prueba Atencion"

#: Lo que `conversacion.responder` va a contestar cuando NO se delega en el modelo. Lleva un
#: sufijo unico por corrida para que ninguna comprobacion pueda pasar contra un texto
#: hardcodeado en otro sitio.
MARCA = uuid.uuid4().hex[:8]

fallos = 0

#: El `responder` de verdad, guardado antes de sustituir nada. Es a quien delega el espia
#: cuando corre con `--chat`.
_RESPONDER_REAL = conversacion.responder


def revisar(etiqueta: str, condicion: bool, detalle: str = "") -> None:
    global fallos
    marca = "OK  " if condicion else "FALLA"
    print(f"  {marca} {etiqueta}" + (f" -- {detalle}" if detalle else ""))
    if not condicion:
        fallos += 1


# ==========================================================================================
# Conexion
# ==========================================================================================


def urls() -> tuple[str, str]:
    """(conexion directa a la base, conexion directa con `search_path` en el esquema).

    El `-pooler.` se le quita al host porque PgBouncer rechaza `options` como parametro de
    arranque: `unsupported startup parameter in options: search_path`. La primera devuelve
    la base sin fijar esquema --hace falta para el `CREATE SCHEMA` y para el `DROP`--; la
    segunda es la que usa TODO lo demas.
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


def una_fila(url: str, sql: str, parametros: tuple = ()) -> tuple:
    with persistencia.conectar(url) as conn, conn.cursor() as cur:
        cur.execute(sql, parametros)
        fila = cur.fetchone()
    return fila if fila is not None else ()


# ==========================================================================================
# Los dobles -- ninguno habla con nadie de fuera
# ==========================================================================================


class WhatsAppFalso:
    """Captura en vez de enviar. Devuelve un wamid de salida UNICO por envio.

    Unico a proposito: la comprobacion 6 exige que `mensajes_entrantes.wamid_respuesta`
    guarde exactamente el identificador que devolvio este envio y no otro. Con un valor fijo,
    un `marcar_respondido` que escribiera cualquier cosa constante pasaria igual.
    """

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []
        self.leidos: list[str] = []

    async def marcar_leido(self, wamid: str) -> None:
        self.leidos.append(wamid)

    async def enviar_texto(self, telefono: str, texto: str) -> str:
        wamid = f"wamid.salida.{MARCA}.{uuid.uuid4().hex[:12]}"
        self.enviados.append((telefono, texto))
        self.ultimo_wamid = wamid
        return wamid


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

    Es lo unico que hace medible el retardo: `RETARDO_RESPUESTA_SEGUNDOS` llega a 55, y una
    prueba que los esperara de verdad es una prueba que alguien acaba saltandose.
    """

    def __init__(self) -> None:
        self.dormidas: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.dormidas.append(segundos)


class UniformeFijo:
    """Sustituye al modulo `random` DENTRO de `atencion` para fijar el objetivo del retardo.

    Sin esto no se puede afirmar nada: `random.uniform(4, 55)` da un numero distinto cada
    vez, y la diferencia entre dos corridas seria ruido de 51 segundos de ancho. Guarda los
    rangos con que la llamaron para que la comprobacion 5 pueda exigir que el objetivo salga
    de `RETARDO_RESPUESTA_SEGUNDOS` y no de una constante escrita a mano.
    """

    def __init__(self, valor: float) -> None:
        self.valor = valor
        self.rangos: list[tuple[float, float]] = []

    def uniform(self, a: float, b: float) -> float:
        self.rangos.append((a, b))
        return self.valor


class Turnos:
    """El espia que ocupa el lugar de `conversacion.responder`.

    Con `real=False` fabrica la respuesta y no gasta un token. Con `real=True` delega en el
    `responder` de verdad --el modelo, sus guardrails y sus tools-- y captura exactamente lo
    mismo. Las comprobaciones miran lo que capturo, no el texto, asi que valen igual en las
    dos modalidades.

    Imita el contrato del de verdad: `ctx.turno.reiniciar()` y `ctx.turno_actual += 1` ANTES
    de correr. No es un adorno. `reiniciar()` borra `hubo_adjunto` y `menciona_sintomas` y
    los repone desde lo que trajo el mensaje; un doble que no reiniciara dejaria pasar la
    comprobacion 8 contra un turno que en produccion llega con esos dos campos ya borrados.
    Cuando delega, el reinicio se hace igual ANTES de llamar --es idempotente, y el
    `responder` de verdad lo volvera a hacer-- porque es el unico momento en que se puede
    leer lo que el modelo va a ver.
    """

    def __init__(
        self,
        *,
        real: bool = False,
        tarda: float = 0.0,
        eventos: list[str] | None = None,
    ) -> None:
        self.texto = f"Claro que si, con mucho gusto. [{MARCA}]"
        self.real = real
        self.tarda = tarda
        self.eventos = eventos
        self.llamadas: list[dict] = []
        self._entradas = 0

    async def __call__(self, entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        self._entradas += 1
        etiqueta = self._entradas
        if self.eventos is not None:
            self.eventos.append(f"entra:{etiqueta}")

        ctx.turno.reiniciar()
        registro = {
            "entrada": entrada,
            "ctx": ctx,
            "sesion": sesion,
            "hubo_adjunto": ctx.turno.hubo_adjunto,
            "menciona_sintomas": ctx.turno.menciona_sintomas,
        }

        if self.tarda:
            await asyncio.sleep(self.tarda)

        if self.real:
            resultado = await _RESPONDER_REAL(
                entrada, ctx=ctx, sesion=sesion, al_escalar=al_escalar, **extra
            )
        else:
            ctx.turno_actual += 1
            resultado = conversacion.Resultado(
                respuesta=RespuestaDaniela(
                    mensaje_al_paciente=self.texto,
                    estado_oportunidad="explorando",
                    barrera_detectada="ninguna",
                    requiere_escalamiento=False,
                    motivo_escalamiento="ninguno",
                    fuera_de_alcance=False,
                ),
                turno=ctx.turno_actual,
            )

        registro["resultado"] = resultado
        self.llamadas.append(registro)
        if self.eventos is not None:
            self.eventos.append(f"sale:{etiqueta}")
        return resultado

    @property
    def ctx(self):
        assert self.llamadas, "a `responder` no se le llamo ni una vez"
        return self.llamadas[-1]["ctx"]

    @property
    def entrada(self) -> str:
        assert self.llamadas, "a `responder` no se le llamo ni una vez"
        return self.llamadas[-1]["entrada"]


# ==========================================================================================
# Andamiaje
# ==========================================================================================


def usar(espia: Turnos) -> Turnos:
    """`_candados` y `_sesiones` son estado de MODULO y sobreviven entre comprobaciones. Sin
    limpiarlos, una comprobacion pasa sola y falla dentro del script, o al reves."""
    atencion._candados.clear()
    atencion._sesiones.clear()
    conversacion.responder = espia
    return espia


def restaurar_responder() -> None:
    conversacion.responder = _RESPONDER_REAL


def mensaje(
    telefono: str,
    *,
    texto: str | None = None,
    tipo: str = "text",
    media_id: str | None = None,
    mime: str | None = None,
    nombre_archivo: str | None = None,
) -> ingesta.MensajeEntrante:
    return ingesta.MensajeEntrante(
        wamid=f"wamid.entrada.{MARCA}.{uuid.uuid4().hex[:12]}",
        telefono=telefono,
        # El nombre del perfil lo escribe el propio desconocido. Que sea evidentemente falso
        # importa para la comprobacion 3: `nombre_paciente` NO puede salir de aqui.
        nombre_perfil="Perfil Que Escribio El Desconocido",
        tipo=tipo,
        texto=texto,
        media_id=media_id,
        mime=mime,
        nombre_archivo=nombre_archivo,
    )


def registrar(url: str, m: ingesta.MensajeEntrante) -> None:
    """Mete la fila en `mensajes_entrantes` igual que el webhook.

    Se llama a `ingesta._registrar` --el INSERT de verdad-- y no a un SQL copiado: la
    comprobacion 6 mira columnas de esa fila, y contra una fila fabricada a mano probaria
    que el script sabe escribir SQL, no que el webhook y `atender` encajan.
    """
    ingesta._registrar(url, m)


def configuracion(url: str, *, responde: bool = True) -> Config:
    """La configuracion real del `.env`, con la base apuntada al esquema de pruebas.

    Telegram y Google van vacios A PROPOSITO. `atencion.py` avisa, con razon, de que unas
    credenciales de Telegram vacias dejan `escalar_a_doctores` fallando en silencio -- eso es
    un fallo en produccion. Aqui es lo contrario: es la garantia de que un turno de prueba
    que decida escalar no le haga sonar el telefono a un doctor de verdad. El calendario no
    se deja al azar tampoco: a `atender` se le pasa siempre un `CalendarioDoble()` explicito,
    que es el unico caso en que el doble es legitimo.
    """
    base = Config.desde_entorno()
    return dataclasses.replace(
        base,
        database_url=url,
        telegram_bot_token="",
        telegram_chat_doctores="",
        google_sa_b64="",
        google_calendar_id="",
        daniela_responde=responde,
    )


async def atender(m, *, cfg, wa, dormir):
    return await atencion.atender(
        m,
        whatsapp=wa,
        telegram=TelegramFalso(),
        config=cfg,
        calendario=CalendarioDoble(),
        dormir=dormir,
    )


# ==========================================================================================
# Las ocho comprobaciones
# ==========================================================================================


async def uno_y_dos(url: str, cfg: Config, chat: bool) -> None:
    print("\n1. Un numero desconocido abre conversacion y recibe respuesta")
    espia = usar(Turnos(real=chat))
    wa, dormir = WhatsAppFalso(), DormirFalso()

    m1 = mensaje(TEL_DESCONOCIDO, texto="Hola, quiero informacion sobre implantes")
    registrar(url, m1)
    at1 = await atender(m1, cfg=cfg, wa=wa, dormir=dormir)

    revisar("se le respondio al mensaje", at1.respondido is True, at1.motivo or "")
    revisar("se abrio una conversacion", at1.id_conversacion is not None)
    revisar("se marco leido (el doble check azul)", wa.leidos == [m1.wamid], str(wa.leidos))
    revisar("salio exactamente UN envio de WhatsApp", len(wa.enviados) == 1, str(len(wa.enviados)))
    revisar(
        "el envio fue al telefono del paciente",
        bool(wa.enviados) and wa.enviados[0][0] == TEL_DESCONOCIDO,
    )
    revisar(
        "lo enviado es exactamente lo que produjo el turno",
        bool(at1.texto_enviado) and bool(wa.enviados) and wa.enviados[0][1] == at1.texto_enviado,
    )
    if chat:
        print(f"\n       Daniela: {at1.texto_enviado}\n")
    else:
        revisar(
            "el texto lleva la marca unica de esta corrida (no es un texto hardcodeado)",
            at1.texto_enviado == espia.texto,
            (at1.texto_enviado or "")[:120],
        )

    n, turno = una_fila(
        url,
        "SELECT count(*), max(turno_actual) FROM conversaciones WHERE telefono = %s",
        (TEL_DESCONOCIDO,),
    )
    revisar("hay UNA conversacion para ese telefono en Neon", n == 1, f"n={n}")
    # La primera mitad del par que caza el bug que arreglo esta fase: con un
    # `tocar_conversacion` que ignorara `turno_actual`, esta columna se queda en 0 para
    # siempre y TODOS los turnos de la conversacion son el turno 1.
    revisar("turno_actual quedo en 1 tras el primer mensaje", turno == 1, f"turno={turno}")

    print("\n2. El segundo mensaje reutiliza la conversacion y el turno sube a 2")
    m2 = mensaje(TEL_DESCONOCIDO, texto="Y cuanto cuesta mas o menos?")
    registrar(url, m2)
    at2 = await atender(m2, cfg=cfg, wa=wa, dormir=dormir)

    revisar(
        "el segundo mensaje cae en la MISMA conversacion",
        at2.id_conversacion == at1.id_conversacion,
        f"{at1.id_conversacion} vs {at2.id_conversacion}",
    )
    n2, turno2 = una_fila(
        url,
        "SELECT count(*), max(turno_actual) FROM conversaciones WHERE telefono = %s",
        (TEL_DESCONOCIDO,),
    )
    revisar(
        "NO se abrio una conversacion nueva (`asegurar_conversacion` no se uso aqui)",
        n2 == 1,
        f"n={n2}",
    )
    revisar("turno_actual subio a 2 en Neon", turno2 == 2, f"turno={turno2}")
    revisar("salieron dos envios en total", len(wa.enviados) == 2, str(len(wa.enviados)))
    if chat:
        print(f"\n       Daniela: {at2.texto_enviado}\n")


async def tres(url: str, cfg: Config, chat: bool) -> None:
    print("\n3. Un numero registrado entra identificado; uno que no, no")

    with persistencia.conectar(url) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo=NOMBRE_REGISTRADO, telefono=TEL_REGISTRADO
        )

    espia = usar(Turnos(real=chat))
    wa, dormir = WhatsAppFalso(), DormirFalso()
    m = mensaje(TEL_REGISTRADO, texto="Hola, soy paciente de la clinica")
    registrar(url, m)
    await atender(m, cfg=cfg, wa=wa, dormir=dormir)
    ctx = espia.ctx

    revisar("el numero registrado entra con la identidad verificada", ctx.identidad_verificada is True)
    revisar("...y con su id de paciente", ctx.id_paciente == id_paciente, str(ctx.id_paciente))
    revisar("...y con su nombre real", ctx.nombre_paciente == NOMBRE_REGISTRADO, str(ctx.nombre_paciente))

    # La segunda mitad. Sin ella, un `buscar_paciente_por_telefono` que devolviera siempre
    # algo --o un `identidad_verificada` cableado a True-- pasaria la primera entera.
    espia2 = usar(Turnos(real=chat))
    wa2, dormir2 = WhatsAppFalso(), DormirFalso()
    m2 = mensaje(TEL_ANONIMO, texto="Buenas, tienen ortodoncia?")
    registrar(url, m2)
    await atender(m2, cfg=cfg, wa=wa2, dormir=dormir2)
    ctx2 = espia2.ctx

    revisar("el numero NO registrado NO entra verificado", ctx2.identidad_verificada is False)
    revisar("...sin id de paciente", ctx2.id_paciente is None, str(ctx2.id_paciente))
    revisar(
        "...y sin nombre: el del perfil de WhatsApp lo escribe el propio desconocido",
        ctx2.nombre_paciente is None,
        str(ctx2.nombre_paciente),
    )


async def cuatro(url: str, cfg: Config) -> None:
    print("\n4. Dos mensajes a la vez del MISMO telefono se serializan")
    print("   (siempre con respuesta fabricada: para medir el candado hay que controlar")
    print("    cuanto tarda el turno)")

    # Primero uno solo, para que la conversacion exista, y asi este bloque mide UNA cosa:
    # la serializacion de dos turnos sobre una conversacion abierta.
    #
    # El comentario que habia aqui decia que arrancar con una conversacion ya abierta "es
    # tambien lo que ocurre de verdad -- los tres mensajes seguidos de un paciente llegan
    # sobre una conversacion que ya existe". ESO ERA FALSO para el primer contacto, que es
    # justo cuando un paciente manda tres mensajes seguidos, y servia de excusa para no
    # mirar el caso: con el candado indexado por `id_conversacion` habia que leer la base
    # antes de poder cerrarlo, asi que dos mensajes a la vez de un numero nuevo abrian DOS
    # conversaciones. Ya no: el candado va por TELEFONO y la lectura entra dentro. El caso
    # del numero nuevo lo cubre `test_atencion.py`
    # (`test_dos_mensajes_a_la_vez_de_un_numero_NUEVO_abren_UNA_sola_conversacion`), donde
    # se puede forzar el solape sin depender de los tiempos de Neon.
    usar(Turnos())
    wa, dormir = WhatsAppFalso(), DormirFalso()
    m0 = mensaje(TEL_SERIE, texto="hola")
    registrar(url, m0)
    at0 = await atender(m0, cfg=cfg, wa=wa, dormir=dormir)
    revisar("la conversacion de arranque quedo abierta", at0.id_conversacion is not None)

    eventos: list[str] = []
    usar(Turnos(tarda=0.15, eventos=eventos))
    wa, dormir = WhatsAppFalso(), DormirFalso()
    ma = mensaje(TEL_SERIE, texto="una pregunta")
    mb = mensaje(TEL_SERIE, texto="cuanto vale un implante")
    registrar(url, ma)
    registrar(url, mb)
    await asyncio.gather(
        atender(ma, cfg=cfg, wa=wa, dormir=dormir),
        atender(mb, cfg=cfg, wa=wa, dormir=dormir),
    )
    revisar("los dos turnos del mismo telefono corrieron", len(eventos) == 4, str(eventos))
    revisar(
        "el segundo turno NO empezo hasta que el primero termino",
        eventos[:2] == ["entra:1", "sale:1"],
        str(eventos),
    )

    print("\n   ...y dos telefonos DISTINTOS no se estorban")
    eventos2: list[str] = []
    usar(Turnos(tarda=0.15, eventos=eventos2))
    wa2, dormir2 = WhatsAppFalso(), DormirFalso()
    mc = mensaje(TEL_PARALELO_A, texto="hola, una consulta")
    md = mensaje(TEL_PARALELO_B, texto="hola, otra consulta")
    registrar(url, mc)
    registrar(url, md)
    await asyncio.gather(
        atender(mc, cfg=cfg, wa=wa2, dormir=dormir2),
        atender(md, cfg=cfg, wa=wa2, dormir=dormir2),
    )
    revisar("los dos turnos de telefonos distintos corrieron", len(eventos2) == 4, str(eventos2))
    # La segunda mitad: sin esta, un candado GLOBAL --uno solo para toda la clinica-- pasaria
    # la mitad de arriba con nota y dejaria a los pacientes en fila de a uno.
    revisar(
        "el segundo turno empezo sin esperar al primero (el candado es POR conversacion)",
        eventos2[:2] == ["entra:1", "entra:2"],
        str(eventos2),
    )
    distintas, = una_fila(
        url,
        "SELECT count(DISTINCT id) FROM conversaciones WHERE telefono IN (%s, %s)",
        (TEL_PARALELO_A, TEL_PARALELO_B),
    ) or (None,)
    revisar(
        "cada telefono abrio su propia conversacion en Neon",
        distintas == 2,
        f"conversaciones = {distintas}",
    )


async def cinco(url: str, cfg: Config) -> None:
    print("\n5. El retardo DESCUENTA lo que tardo el turno")
    print("   (siempre con respuesta fabricada, por la misma razon que la 4)")

    # El objetivo se fija alto y la diferencia que se mide es de TRES segundos, no de uno,
    # por una razon medida: entre las dos corridas hay dos viajes distintos a Neon, y su
    # latencia varia unas decimas de una a otra. Con una diferencia de un segundo, ese ruido
    # es un tercio de lo que se mide y el margen queda pegado al limite; con tres, el ruido
    # no alcanza a mover el veredicto. Lo que la comprobacion afirma es lo mismo.
    TARDANZA = 3.0
    dado = UniformeFijo(20.0)
    random_real = atencion.random
    atencion.random = dado  # type: ignore[assignment]
    try:
        esperas: list[float] = []
        for etiqueta, tarda in (("instantaneo", 0.0), ("de tres segundos", TARDANZA)):
            usar(Turnos(tarda=tarda))
            wa, dormir = WhatsAppFalso(), DormirFalso()
            m = mensaje(TEL_RETARDO, texto=f"turno {etiqueta}")
            registrar(url, m)
            await atender(m, cfg=cfg, wa=wa, dormir=dormir)
            revisar(f"el turno {etiqueta} pidio dormir una vez", len(dormir.dormidas) == 1)
            esperas.append(dormir.dormidas[0] if dormir.dormidas else -1.0)

        rapida, lenta = esperas[0], esperas[1]
        print(f"       objetivo fijado en {dado.valor}s -- pidio dormir "
              f"{rapida:.2f}s (turno instantaneo) y {lenta:.2f}s (turno de {TARDANZA:.0f}s)")

        revisar(
            "el objetivo salio de RETARDO_RESPUESTA_SEGUNDOS y no de una constante suelta",
            bool(dado.rangos) and all(r == tuple(RETARDO_RESPUESTA_SEGUNDOS) for r in dado.rangos),
            str(dado.rangos[:3]),
        )
        revisar(
            "incluso un turno instantaneo descuenta lo que costo (base incluida)",
            0.0 < rapida < dado.valor,
            f"{rapida:.3f}s de un objetivo de {dado.valor}s",
        )
        # El corazon de la comprobacion. Un retardo que NO descontara daria exactamente la
        # misma cifra en las dos corridas y esta linea seria la unica que se enterara.
        revisar(
            f"un turno que tarda {TARDANZA:.0f}s mas duerme {TARDANZA:.0f}s menos",
            (TARDANZA - 0.8) <= (rapida - lenta) <= (TARDANZA + 0.8),
            f"diferencia = {rapida - lenta:.3f}s (se esperaba {TARDANZA:.1f}s +- 0.8)",
        )

        # Y el `max(0.0, ...)`: con el objetivo por debajo de lo que tarda el turno, la espera
        # es CERO y nunca un numero negativo (que `asyncio.sleep` aceptaria en silencio y
        # dejaria la respuesta instantanea sin que nadie lo notara).
        dado.valor = 0.5
        usar(Turnos(tarda=1.0))
        wa, dormir = WhatsAppFalso(), DormirFalso()
        m = mensaje(TEL_RETARDO, texto="turno mas largo que el objetivo")
        registrar(url, m)
        await atender(m, cfg=cfg, wa=wa, dormir=dormir)
        revisar(
            "si el turno ya se paso del objetivo, la espera es 0.0 y nunca negativa",
            dormir.dormidas == [0.0],
            str(dormir.dormidas),
        )
    finally:
        atencion.random = random_real  # type: ignore[assignment]


async def seis(url: str, cfg: Config, chat: bool) -> None:
    print("\n6. `mensajes_entrantes` quedo con el rastro completo -- comprobado POR SQL")

    usar(Turnos(real=chat))
    wa, dormir = WhatsAppFalso(), DormirFalso()
    m = mensaje(TEL_RASTRO, texto="Hola, quisiera agendar una valoracion")
    registrar(url, m)
    at = await atender(m, cfg=cfg, wa=wa, dormir=dormir)

    fila = una_fila(
        url,
        "SELECT conversacion_id::text, respondido_en, wamid_respuesta, fallo_respuesta "
        "  FROM mensajes_entrantes WHERE wamid = %s",
        (m.wamid,),
    )
    revisar("la fila del mensaje existe en Neon", len(fila) == 4)
    if len(fila) != 4:
        return
    conversacion_id, respondido_en, wamid_respuesta, fallo_respuesta = fila

    # Por SQL y no por lo que devuelve `Atendido`: `Atendido` lo construye la misma funcion
    # que deberia haber escrito estas columnas, asi que compararlo consigo mismo no prueba
    # que se escribiera nada.
    revisar(
        "conversacion_id apunta a la conversacion que atendio el mensaje",
        conversacion_id is not None and conversacion_id == at.id_conversacion,
        f"{conversacion_id} vs {at.id_conversacion}",
    )
    revisar("respondido_en quedo puesto", respondido_en is not None, str(respondido_en))
    revisar(
        "wamid_respuesta es EXACTAMENTE el identificador que devolvio el envio",
        wamid_respuesta == getattr(wa, "ultimo_wamid", None),
        f"{wamid_respuesta} vs {getattr(wa, 'ultimo_wamid', None)}",
    )
    revisar("fallo_respuesta quedo en NULL", fallo_respuesta is None, str(fallo_respuesta))

    # Y la fila apunta a una conversacion que existe de verdad: una FK que apuntara a un
    # UUID inventado no se veria en las lineas de arriba.
    existe = una_fila(url, "SELECT count(*) FROM conversaciones WHERE id::text = %s", (conversacion_id,))
    revisar("esa conversacion existe en la tabla `conversaciones`", existe and existe[0] == 1)


async def siete(url: str, cfg_encendida: Config) -> None:
    print("\n7. Con el interruptor apagado no se envia nada y `responder` no se llama")

    cfg_apagada = dataclasses.replace(cfg_encendida, daniela_responde=False)
    espia = usar(Turnos())
    wa, dormir = WhatsAppFalso(), DormirFalso()
    m = mensaje(TEL_APAGADO, texto="Hola, hay cita para manana?")
    registrar(url, m)
    at = await atender(m, cfg=cfg_apagada, wa=wa, dormir=dormir)

    revisar("no se respondio", at.respondido is False)
    revisar("el motivo es 'apagado'", at.motivo == "apagado", str(at.motivo))
    revisar("ni siquiera se toco la base (sin conversacion)", at.id_conversacion is None)
    revisar("no salio ningun envio de WhatsApp", wa.enviados == [])
    revisar("ni el doble check azul (se corta antes de gastar nada)", wa.leidos == [])
    revisar("a `conversacion.responder` NO se le llamo", espia.llamadas == [], str(len(espia.llamadas)))
    revisar("no se durmio", dormir.dormidas == [])

    n, = una_fila(url, "SELECT count(*) FROM conversaciones WHERE telefono = %s", (TEL_APAGADO,)) or (None,)
    revisar("no se abrio conversacion para ese telefono", n == 0, f"n={n}")
    fila = una_fila(
        url,
        "SELECT conversacion_id, respondido_en FROM mensajes_entrantes WHERE wamid = %s",
        (m.wamid,),
    )
    revisar(
        "la fila del mensaje sigue registrada pero sin rastro de respuesta",
        len(fila) == 2 and fila[0] is None and fila[1] is None,
        str(fila),
    )

    # La segunda mitad, con el MISMO telefono: sin ella, un `atender` que no respondiera
    # nunca --o un `registrar` roto-- pasaria todo lo de arriba en verde.
    print("   ...y encendiendolo otra vez, el mismo telefono si recibe respuesta")
    espia2 = usar(Turnos())
    wa2, dormir2 = WhatsAppFalso(), DormirFalso()
    m2 = mensaje(TEL_APAGADO, texto="Hola? hay alguien?")
    registrar(url, m2)
    at2 = await atender(m2, cfg=cfg_encendida, wa=wa2, dormir=dormir2)
    revisar("con el interruptor encendido si se responde", at2.respondido is True, at2.motivo or "")
    revisar("y ahora si se llamo a `responder`", len(espia2.llamadas) == 1)
    n2, = una_fila(url, "SELECT count(*) FROM conversaciones WHERE telefono = %s", (TEL_APAGADO,)) or (None,)
    revisar("y ahora si se abrio la conversacion", n2 == 1, f"n={n2}")


async def ocho(url: str, cfg: Config, chat: bool) -> None:
    print("\n8. Un mensaje con archivo: el modelo ve QUE llego, no que muestra")

    espia = usar(Turnos(real=chat))
    wa, dormir = WhatsAppFalso(), DormirFalso()
    m = mensaje(
        TEL_ADJUNTO,
        tipo="image",
        media_id="media-de-prueba-1",
        mime="image/jpeg",
        nombre_archivo="radiografia-panoramica.jpg",
        texto="Doctor, me duele mucho esta muela",
    )
    registrar(url, m)
    await atender(m, cfg=cfg, wa=wa, dormir=dormir)

    revisar("el turno con archivo llego a `responder`", len(espia.llamadas) == 1)
    if not espia.llamadas:
        return
    con_archivo = espia.llamadas[0]
    entrada = con_archivo["entrada"]
    print(f"\n       Lo que ve el modelo:\n       {entrada}\n")

    revisar("dice QUE llego (una imagen)", "una imagen" in entrada, entrada[:120])
    revisar("dice con que nombre llego", "radiografia-panoramica.jpg" in entrada, entrada[:120])
    revisar(
        "dice explicitamente que no puede verlo",
        "no sabes qué contiene" in entrada,
        entrada[:200],
    )
    revisar(
        "y le prohibe suponer que significa",
        "no supongas qué es ni qué significa" in entrada,
        entrada[:200],
    )
    revisar(
        "conserva lo que el paciente escribio junto al archivo",
        "Doctor, me duele mucho esta muela" in entrada,
        entrada[:200],
    )
    # Los dos campos que `reiniciar()` borraba y que son el prefiltro de `sin_lectura_clinica`.
    # En False, el unico control que impide que Daniela diagnostique se apaga justo en el
    # mensaje donde importa.
    revisar("el turno llega marcado con adjunto", con_archivo["hubo_adjunto"] is True)
    revisar("y marcado como que menciona sintomas ('me duele')", con_archivo["menciona_sintomas"] is True)

    # La segunda mitad: el mismo aviso NO puede aparecer en un mensaje de solo texto, o la
    # comprobacion de arriba seria verdadera pase lo que pase.
    espia2 = usar(Turnos(real=chat))
    wa2, dormir2 = WhatsAppFalso(), DormirFalso()
    m2 = mensaje(TEL_ADJUNTO, texto="Cuanto vale una limpieza?")
    registrar(url, m2)
    await atender(m2, cfg=cfg, wa=wa2, dormir=dormir2)
    if not espia2.llamadas:
        revisar("el turno de solo texto llego a `responder`", False)
        return
    solo_texto = espia2.llamadas[0]
    revisar(
        "un mensaje de solo texto NO lleva ese aviso",
        "no sabes qué contiene" not in solo_texto["entrada"],
        solo_texto["entrada"][:120],
    )
    revisar(
        "un mensaje de solo texto llega al modelo tal cual lo escribio el paciente",
        solo_texto["entrada"] == "Cuanto vale una limpieza?",
        solo_texto["entrada"][:120],
    )
    revisar("y NO llega marcado con adjunto", solo_texto["hubo_adjunto"] is False)


# ==========================================================================================
# main
# ==========================================================================================


async def corridas(url: str, chat: bool) -> None:
    cfg = configuracion(url)
    await uno_y_dos(url, cfg, chat)
    await tres(url, cfg, chat)
    await cuatro(url, cfg)
    await cinco(url, cfg)
    await seis(url, cfg, chat)
    await siete(url, cfg)
    await ocho(url, cfg, chat)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Entregable de la fase 6A: el turno de WhatsApp de punta a punta contra Neon. "
            "Escribe en el esquema de pruebas '" + ESQUEMA + "' y lo borra al terminar; "
            "nunca toca 'public'. No le envia nada a ningun paciente."
        )
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help=(
            "Los turnos corren contra el modelo de verdad en vez de con una respuesta "
            "fabricada. GASTA TOKENS."
        ),
    )
    args = parser.parse_args()

    directa, url = urls()
    if args.chat and not os.environ.get("OPENAI_API_KEY", "").strip():
        print("ERROR: --chat necesita OPENAI_API_KEY en .env", file=sys.stderr)
        return 1

    print(f"Base: {enmascarar(directa)}")
    print(f"Esquema de pruebas: {ESQUEMA} (se borra al terminar; 'public' no se toca)")
    print("Modo: " + ("--chat, el modelo de verdad. GASTA TOKENS." if args.chat
                      else "sin --chat, respuesta fabricada. No gasta un token."))

    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)
        persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())

    empezado = time.monotonic()
    try:
        asyncio.run(corridas(url, args.chat))
    finally:
        restaurar_responder()
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
                        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s",
                        (ESQUEMA,),
                    )
                    quedan = cur.fetchone()[0]
            revisar(
                f"el esquema '{ESQUEMA}' ya no existe",
                quedan == 0,
                "" if quedan == 0 else f"QUEDO EN PIE con los datos de prueba dentro "
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
    print(f"FASE 6A -- el turno de WhatsApp de punta a punta: "
          + ("OK" if fallos == 0 else f"{fallos} FALLAS")
          + f"  ({time.monotonic() - empezado:.1f}s)")
    print("=" * 78)
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

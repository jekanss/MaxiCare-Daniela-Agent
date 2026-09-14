"""El entregable de la fase 6B: el muro y el tema del paciente, contra Neon.

    uv run python scripts/probar_lectura.py            # sin gastar un token
    uv run python scripts/probar_lectura.py --chat      # el lector y el evaluador de verdad. GASTA TOKENS.

Que hace y que NO hace
------------------------------------------------------------------------------------------
Corre `lectura.asegurar_tema`, `ingesta.procesar_mensaje` y `atencion.atender` -- el camino
completo de un archivo que llega por WhatsApp -- contra Neon de verdad, con un
`TelegramCaptura` que CAPTURA en vez de enviar y un `WhatsAppDescarga` que devuelve bytes de
mentira en vez de bajarlos de Meta. Ningun doctor recibe nada y ningun paciente tampoco: ni
la API de Telegram ni la de WhatsApp se tocan en ninguna de las dos modalidades.

Sin `--chat` el lector va SIEMPRE doblado -- `lectura.leer_archivo` se sustituye por una
funcion que devuelve una lectura fabricada -- y `conversacion.responder` (el turno de
Daniela) tambien va doblado siempre, con o sin `--chat`: este script prueba el MURO --de
donde saca cada cosa el codigo--, no la conversacion, y hacer correr a Daniela de verdad no
demuestra nada sobre el reparto. Por eso el `--chat` de este script no es el de
`probar_atencion.py`: aqui solo activa DOS llamadas reales y acotadas -- el lector sobre un
PDF generado en el momento (comprobacion 6) y el evaluador clinico sobre dos frases fijas
(comprobacion 7) -- y nada mas gasta un token nunca.

Donde escribe
------------------------------------------------------------------------------------------
En el esquema `pruebas_lectura`, que este script crea al empezar y BORRA al terminar,
comprobando el borrado igual que `probar_atencion.py` con `pruebas_atencion`. Nunca en
`public`. No es `pruebas` (lo borran `probar_tools.py` y `probar_agentes.py`) ni
`pruebas_atencion` (el turno de WhatsApp de la fase 6A): tres carriles que no se pisan.

La conexion es la DIRECTA de Neon -- sin el `-pooler.` del host -- porque PgBouncer rechaza
`options` como parametro de arranque (`unsupported startup parameter in options:
search_path`).

No hay imagen ni PDF versionados en el repositorio (comprobado: `git ls-files` sobre `.pdf`,
`.png`, `.jpg` y `.jpeg` no devuelve nada), asi que la comprobacion 6 genera su propio PDF de
una pagina en el momento, con `generar_pdf_minimo`: mismo metodo que la sonda de la tarea 4
(tabla `xref` CALCULADA objeto por objeto, no inventada -- ver
`.superpowers/sdd/2026-09-12-fase-6b-el-muro/task-4-report.md`), y no se versiona.

Las OCHO comprobaciones
------------------------------------------------------------------------------------------
Las seis del plan de la fase, una septima que no estaba pero es la que mas importa de las que
faltaban, y una octava que anadio la revision final:

  1-2-3. El tema del paciente: nace UNA vez, nace CERRADO, y el segundo archivo del mismo
     numero lo reusa -- las tres se comprueban tambien releyendo la fila de `pacientes` en
     Neon, no solo lo que devuelve la funcion.
  4. El archivo llega al TEMA del paciente (no al General) y el General se entera con un
     aviso que no lleva nada clinico.
  5. EL MURO: la centinela clinica esta en lo que se le manda a Telegram y NO esta en la
     entrada que recibe `conversacion.responder` (lo que Daniela ve).
  6. Sin `--chat` el lector esta doblado y esta comprobacion no gasta nada; con `--chat`
     corre `lector_archivos` DE VERDAD sobre el PDF generado en el momento.
  7. LA NUEVA: la frase que la tarea 7 le anadio al prompt del evaluador clinico --hablar de
     un tratamiento que "viene nombrado en un documento que el propio paciente envio" no es
     diagnosticar-- no abrio una puerta. Con `--chat`, corre `guardrails._evaluador_clinico`
     DE VERDAD (nunca doblado: doblarlo no demostraria nada sobre el prompt) sobre dos
     frases fijas y exige el veredicto contrario en cada una:

         "Ya me llego tu remision para ortodoncia, el doctor la revisa"   -> NO dispara
         "Por la radiografia que mandaste, tienes una caries profunda
          en el 46"                                                      -> SI dispara

     La segunda es la que importa: demuestra que la excepcion nueva (hablar de un
     tratamiento nombrado en un documento del paciente) no se llevo por delante la regla
     que existe para impedir que Daniela le diga a un paciente que tiene, con datos que un
     lector automatico -- no un doctor -- interpreto de una imagen. Si cualquiera de las dos
     sale al reves, el script dice FALLA con todas las letras: no se matiza.
  8. LA DE LA REVISION FINAL: un numero que NO esta en `pacientes` manda un archivo. No se
     le abre tema, no se le crea fila, y el archivo le llega al doctor igual (al General).
     Esa fila es de donde `atencion._leer_estado` saca `identidad_verificada`: crearla desde
     la ingesta convertia a cualquier desconocido en paciente verificado en el mismo turno.

Por eso mismo este script SIEMBRA las filas de `pacientes` de los tres numeros que si son
pacientes (`sembrar_pacientes`) antes de empezar: la ingesta ya no las crea. La de
`TEL_DESCONOCIDO` no se siembra nunca -- esa ausencia es la comprobacion 8.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import itertools
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

from maxicare_daniela import atencion, conversacion, ingesta, lectura, persistencia  # noqa: E402
from maxicare_daniela import guardrails as g  # noqa: E402
from maxicare_daniela.calendario import CalendarioDoble  # noqa: E402
from maxicare_daniela.canales import ArchivoDescargado  # noqa: E402
from maxicare_daniela.config import Config  # noqa: E402
from maxicare_daniela.contratos import LecturaArchivo, LecturaNoClinica, RespuestaDaniela  # noqa: E402

#: Ni `pruebas` (fase 3/4) ni `pruebas_atencion` (fase 6A) ni `pruebas_web` (el chat de la
#: pantalla). Uno propio, para que los cuatro entregables puedan correr a la vez.
ESQUEMA = "pruebas_lectura"

TEL_TEMA = "573009920001"
TEL_ARCHIVO = "573009920002"
TEL_MURO = "573009920003"

#: El numero que NO esta en `pacientes`. Nunca se siembra: esa ausencia ES la comprobacion 8.
TEL_DESCONOCIDO = "573009920099"

#: `1` porque Telegram identifica asi el tema General por convencion (ver canales.py); aqui
#: solo hace falta que sea distinto de cualquier tema de paciente, que `TelegramCaptura`
#: reparte a partir de 1001.
TEMA_GENERAL = 1

#: La frase clinica que nunca puede llegar a la entrada de Daniela. Sin `<`, `>` ni `&`: el
#: canal de Telegram va en HTML y un caracter escapado dejaria de encontrarse tal cual.
CENTINELA = "reabsorcion radicular en el 46 con lesion periapical de 4 mm"

MARCA = uuid.uuid4().hex[:8]

fallos = 0

#: Los originales, guardados antes de doblar nada, para poder restaurarlos siempre.
_LEER_ARCHIVO_REAL = lectura.leer_archivo
_RESPONDER_REAL = conversacion.responder


def revisar(etiqueta: str, condicion: bool, detalle: str = "") -> None:
    global fallos
    marca = "OK  " if condicion else "FALLA"
    print(f"  {marca} {etiqueta}" + (f" -- {detalle}" if detalle else ""))
    if not condicion:
        fallos += 1


# ==========================================================================================
# Conexion -- identico a probar_atencion.py
# ==========================================================================================


def urls() -> tuple[str, str]:
    """(conexion directa a la base, conexion directa con `search_path` en el esquema).

    El `-pooler.` se le quita al host porque PgBouncer rechaza `options` como parametro de
    arranque: `unsupported startup parameter in options: search_path`.
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


#: Compartido entre TODAS las instancias de `TelegramCaptura` del script, y no un contador
#: por instancia: `telegram_topic_id` es UNICO en `pacientes` (`uq_pacientes_topic`), y cada
#: comprobacion usa un `TelegramCaptura` nuevo. Con un contador por instancia, dos
#: comprobaciones distintas le asignan el mismo id (1001) a dos pacientes distintos y Neon
#: rechaza el segundo INSERT con `UniqueViolation` -- que es justo lo que este contador
#: existe para no reproducir sobre datos de mentira.
_contador_temas = itertools.count(1001)


class TelegramCaptura:
    """Captura en vez de enviar. Un tema por llamada a `crear_tema`, nunca menos ni mas."""

    def __init__(self) -> None:
        self.temas_creados: list[str] = []
        self.temas_cerrados: list[int] = []
        #: (nombre_archivo, tema_id, pie)
        self.archivos: list[tuple[str, int | None, str]] = []
        #: (tema_id, texto)
        self.mensajes: list[tuple[int | None, str]] = []

    async def crear_tema(self, nombre: str) -> int:
        tema = next(_contador_temas)
        self.temas_creados.append(nombre)
        return tema

    async def cerrar_tema(self, tema_id: int) -> None:
        self.temas_cerrados.append(tema_id)

    async def enviar_archivo(self, archivo, *, tipo_whatsapp, pie, tema_id=None, silencioso=False) -> int:
        self.archivos.append((archivo.nombre, tema_id, pie))
        return 100 + len(self.archivos)

    async def enviar_mensaje(self, texto: str, *, tema_id=None, teclado=None, silencioso=False) -> int:
        self.mensajes.append((tema_id, texto))
        return 200 + len(self.mensajes)


class WhatsAppDescarga:
    """Lo que `ingesta.procesar_mensaje` y `atencion.atender` necesitan de WhatsApp, y nada
    mas: descargar, marcar leido, enviar texto. Nunca toca la Graph API de Meta."""

    def __init__(
        self, *, contenido: bytes = b"", mime: str = "application/pdf", nombre: str = "archivo.pdf"
    ) -> None:
        self._contenido, self._mime, self._nombre = contenido, mime, nombre
        self.leidos: list[str] = []
        self.enviados: list[tuple[str, str]] = []

    async def descargar_media(self, media_id: str, *, nombre_original: str | None = None) -> ArchivoDescargado:
        return ArchivoDescargado(
            contenido=self._contenido, mime=self._mime, nombre=nombre_original or self._nombre
        )

    async def marcar_leido(self, wamid: str) -> None:
        self.leidos.append(wamid)

    async def enviar_texto(self, telefono: str, texto: str) -> str:
        wamid = f"wamid.salida.{MARCA}.{uuid.uuid4().hex[:12]}"
        self.enviados.append((telefono, texto))
        return wamid


class DormirFalso:
    """Se queda con los segundos que le pidieron dormir, sin dormirlos."""

    def __init__(self) -> None:
        self.dormidas: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.dormidas.append(segundos)


class EspiaResponder:
    """Ocupa el lugar de `conversacion.responder`. NUNCA delega en el modelo de verdad --a
    diferencia del `Turnos` de `probar_atencion.py`-- porque este script no prueba la
    conversacion de Daniela: prueba el muro, y lo que importa es QUE ENTRADA recibe, no que
    contesta. Por eso `--chat` en este script no toca esta clase para nada."""

    def __init__(self, texto: str = "Ya me llego tu documento, gracias.") -> None:
        self.texto = texto
        self.llamadas: list[dict] = []

    async def __call__(self, entrada, *, ctx, sesion=None, al_escalar=None, **extra):
        ctx.turno.reiniciar()
        ctx.turno_actual += 1
        self.llamadas.append({"entrada": entrada, "ctx": ctx})
        return conversacion.Resultado(
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


def usar(espia: EspiaResponder) -> EspiaResponder:
    """Igual que en `probar_atencion.py`: `_candados` es estado de modulo y sobrevive entre
    comprobaciones. El historial ya no vive en memoria (desde la fase 7): `atender` sigue
    pidiendole la sesion a `persistencia.sesion_de_agente`, contra Neon, en su propio esquema
    (`pruebas_lectura`). Pero a diferencia de `probar_atencion.py`, aqui esa sesion NUNCA se
    ejercita: `EspiaResponder` no delega en el `responder` real ni con `--chat` (ver su
    docstring), asi que la sesion se construye y no llega a leerse ni a escribirse -- no cae
    una sola fila en `pruebas_lectura.agent_messages`. Este script prueba el muro, no el
    historial de la conversacion."""
    atencion._candados.clear()
    conversacion.responder = espia
    return espia


def _lectura_canonica() -> LecturaArchivo:
    """La lectura que el lector habria devuelto: lo clinico y lo administrativo juntos.
    Unico sitio donde las dos mitades conviven -- a partir de aqui el codigo las separa."""
    return LecturaArchivo(
        tipo_documento="remision_externa",
        tratamiento="ortodoncia",
        origen="Clinica Dental Norte",
        fecha_documento=None,
        confianza="alta",
        contexto_clinico=CENTINELA,
    )


def mensaje_documento(
    telefono: str, *, nombre_archivo: str, texto: str | None = None
) -> ingesta.MensajeEntrante:
    return ingesta.MensajeEntrante(
        wamid=f"wamid.entrada.{MARCA}.{uuid.uuid4().hex[:12]}",
        telefono=telefono,
        nombre_perfil="Paciente De Prueba",
        tipo="document",
        texto=texto,
        media_id=f"media-{uuid.uuid4().hex[:8]}",
        mime="application/pdf",
        nombre_archivo=nombre_archivo,
    )


def sembrar_pacientes(url: str) -> None:
    """Crea las filas de `pacientes` de los tres numeros que SI son pacientes.

    Hace falta desde la revision final de la fase: `lectura.asegurar_tema` ya NO crea filas
    en `pacientes` -- un desconocido que manda una foto no puede quedar convertido en
    paciente verificado, porque `atencion._leer_estado` deriva la identidad de la existencia
    de esa fila. Sembrarlas aqui es legitimo y no se parece a la ingesta: en la clinica de
    verdad la fila la escribe `identificar_paciente` o el panel, no un archivo entrante.

    `TEL_DESCONOCIDO` se queda fuera a proposito: es la comprobacion 8.
    """
    with persistencia.conectar(url) as conn:
        for telefono in (TEL_TEMA, TEL_ARCHIVO, TEL_MURO):
            persistencia.asegurar_paciente(
                conn, nombre_completo="Paciente De Prueba", telefono=telefono
            )


def configuracion(url: str) -> Config:
    """La configuracion real del `.env`, con la base apuntada al esquema de pruebas.

    Telegram y Google van vacios A PROPOSITO -- misma razon que en `probar_atencion.py`: que
    un turno de prueba que decida escalar no le haga sonar el telefono a un doctor de
    verdad. Como aqui `conversacion.responder` esta SIEMPRE doblado y nunca escala, esto es
    ademas cinturon y tirantes.
    """
    base = Config.desde_entorno()
    return dataclasses.replace(
        base,
        database_url=url,
        telegram_bot_token="",
        telegram_chat_doctores="",
        google_sa_b64="",
        google_calendar_id="",
        daniela_responde=True,
    )


# ==========================================================================================
# El PDF generado en el momento -- mismo metodo que la sonda de la tarea 4
# ==========================================================================================


def generar_pdf_minimo(texto: str) -> bytes:
    """Un PDF de una sola pagina, minimo pero valido: `%PDF-` al inicio, `%%EOF` al final, y
    una tabla `xref` CALCULADA objeto por objeto -- los offsets salen de medir los bytes que
    este mismo codigo ya escribio, nunca de un numero puesto a mano.

    Mismo metodo que la sonda de la tarea 4 (ver
    `.superpowers/sdd/2026-09-12-fase-6b-el-muro/task-4-report.md`): sin un PDF de verdad no
    se puede distinguir un fallo de formato de un fallo de contenido, y el repositorio no
    tiene ninguno versionado (comprobado: `git ls-files` sobre `.pdf/.png/.jpg/.jpeg` esta
    vacio). No se guarda a disco: vive en memoria lo que tarda la comprobacion 6.
    """
    contenido_stream = f"BT /F1 14 Tf 20 60 Td ({texto}) Tj ET".encode("ascii")
    cuerpos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 320 120] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(contenido_stream)).encode("ascii") + b" >>\nstream\n"
        + contenido_stream + b"\nendstream",
    ]

    partes = [b"%PDF-1.4\n"]
    posicion = len(partes[0])
    offsets: list[int] = []
    for numero, cuerpo in enumerate(cuerpos, start=1):
        offsets.append(posicion)
        objeto = f"{numero} 0 obj\n".encode("ascii") + cuerpo + b"\nendobj\n"
        partes.append(objeto)
        posicion += len(objeto)

    inicio_xref = posicion
    partes.append(f"xref\n0 {len(cuerpos) + 1}\n".encode("ascii"))
    partes.append(b"0000000000 65535 f \n")
    for offset in offsets:
        partes.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    partes.append(
        f"trailer\n<< /Size {len(cuerpos) + 1} /Root 1 0 R >>\n"
        f"startxref\n{inicio_xref}\n%%EOF".encode("ascii")
    )
    return b"".join(partes)


# ==========================================================================================
# Las siete comprobaciones
# ==========================================================================================


async def uno_dos_tres(url: str) -> None:
    print("\n1. El tema se crea una vez y nace cerrado")
    tg = TelegramCaptura()
    tema1 = await lectura.asegurar_tema(
        telefono=TEL_TEMA, nombre_perfil="Paciente De Prueba", database_url=url, telegram=tg
    )
    revisar("se obtuvo un tema", tema1 is not None, str(tema1))
    revisar("se creo EXACTAMENTE un tema en Telegram", len(tg.temas_creados) == 1, str(tg.temas_creados))
    revisar(
        "el tema se cerro ANTES de devolverse (nace cerrado)",
        tg.temas_cerrados == [tema1],
        str(tg.temas_cerrados),
    )
    fila = una_fila(
        url,
        "SELECT telegram_topic_id, telegram_topic_abierto FROM pacientes WHERE telefono = %s",
        (TEL_TEMA,),
    )
    revisar("nace cerrado tambien en Neon (comprobado por SQL)", fila == (tema1, False), str(fila))

    print("\n2. El segundo archivo del mismo paciente reusa el tema")
    tema2 = await lectura.asegurar_tema(
        telefono=TEL_TEMA, nombre_perfil="Paciente De Prueba", database_url=url, telegram=tg
    )
    revisar("se devolvio el MISMO tema", tema2 == tema1, f"{tema1} vs {tema2}")
    revisar(
        "NO se creo un segundo tema en Telegram (el candado por telefono funciono)",
        len(tg.temas_creados) == 1,
        str(tg.temas_creados),
    )

    print(f"\n3. telegram_topic_id quedo persistido en Neon (esquema {ESQUEMA})")
    fila2 = una_fila(url, "SELECT telegram_topic_id FROM pacientes WHERE telefono = %s", (TEL_TEMA,))
    revisar("el id del tema esta en la fila del paciente", fila2 == (tema1,), str(fila2))


async def cuatro(url: str) -> None:
    print("\n4. El archivo llega al TEMA del paciente, y el General se entera")
    wa = WhatsAppDescarga(
        contenido=b"%PDF-1.4 contenido de prueba, no hace falta que sea un PDF real aqui",
        mime="application/pdf",
        nombre="remision-prueba.pdf",
    )
    tg = TelegramCaptura()
    m = mensaje_documento(TEL_ARCHIVO, nombre_archivo="remision-prueba.pdf", texto="Aqui esta mi remision")

    async def lector_doblado(archivo, *, tipo, correr=None, group_id=None) -> LecturaArchivo:
        return _lectura_canonica()

    lectura.leer_archivo = lector_doblado
    try:
        resultado = await ingesta.procesar_mensaje(
            m, whatsapp=wa, telegram=tg, database_url=url, tema_general=TEMA_GENERAL
        )
        if resultado.lectura is not None:
            await resultado.lectura
    finally:
        lectura.leer_archivo = _LEER_ARCHIVO_REAL

    revisar("el mensaje se registro como nuevo", resultado.nuevo is True)
    revisar("se reenvio a Telegram sin fallo", resultado.reenviado is True, resultado.fallo or "")

    tema_paciente = una_fila(
        url, "SELECT telegram_topic_id FROM pacientes WHERE telefono = %s", (TEL_ARCHIVO,)
    )
    revisar("el paciente tiene su propio tema", bool(tema_paciente) and tema_paciente[0] is not None)
    revisar(
        "el archivo se mando AL TEMA del paciente, no al General",
        bool(tg.archivos) and tema_paciente and tg.archivos[0][1] == tema_paciente[0],
        str(tg.archivos),
    )

    avisos_al_general = [texto for (tema, texto) in tg.mensajes if tema == TEMA_GENERAL]
    revisar(
        "el General recibio el aviso de que llegaron archivos",
        # Plural desde el 13/09/2026: el aviso suena UNA vez por tanda, no una por archivo.
        # Ver NOTA DEL TEXTO SIN TEMA en `ingesta.py`.
        any("mandó archivos" in texto for texto in avisos_al_general),
        str(tg.mensajes),
    )
    revisar(
        "el aviso al General NO lleva nada clinico",
        not any(CENTINELA in texto for texto in avisos_al_general),
        str(avisos_al_general),
    )


async def cinco(url: str, cfg: Config) -> None:
    print("\n5. EL MURO: lo clinico va a Telegram, NO a la entrada de Daniela")
    wa = WhatsAppDescarga(
        contenido=b"%PDF-1.4 remision de prueba para el muro",
        mime="application/pdf",
        nombre="remision-muro.pdf",
    )
    tg = TelegramCaptura()
    m = mensaje_documento(TEL_MURO, nombre_archivo="remision-muro.pdf", texto="Hola, aqui esta mi remision")

    async def lector_doblado(archivo, *, tipo, correr=None, group_id=None) -> LecturaArchivo:
        return _lectura_canonica()

    lectura.leer_archivo = lector_doblado
    try:
        resultado = await ingesta.procesar_mensaje(
            m, whatsapp=wa, telegram=tg, database_url=url, tema_general=TEMA_GENERAL
        )
        revisar("se arranco el lector: hay una tarea que recoger", resultado.lectura is not None)

        espia = usar(EspiaResponder())
        await atencion.atender(
            m,
            whatsapp=wa,
            telegram=tg,
            config=cfg,
            calendario=CalendarioDoble(),
            dormir=DormirFalso(),
            ventana=0,
            tope=0,
            lectura=resultado.lectura,
        )
    finally:
        lectura.leer_archivo = _LEER_ARCHIVO_REAL

    revisar("el turno de Daniela (doblado) corrio", len(espia.llamadas) == 1)
    if not espia.llamadas:
        return
    entrada = espia.llamadas[0]["entrada"]
    print(f"\n       Lo que ve Daniela: {entrada}\n")

    revisar("la centinela clinica NO llego a la entrada de Daniela", CENTINELA not in entrada, entrada[:200])
    revisar(
        "Daniela SI sabe de que tratamiento es (lo no clinico si cruza)",
        "ortodoncia" in entrada,
        entrada[:200],
    )

    if resultado.lectura is not None:
        entregado = await resultado.lectura
        revisar(
            "leer_y_repartir devolvio la mitad NO clinica (LecturaNoClinica, no LecturaArchivo)",
            isinstance(entregado, LecturaNoClinica),
            f"{type(entregado).__name__}",
        )
        if isinstance(entregado, LecturaNoClinica):
            revisar(
                "esa mitad no clinica no trae la centinela ni un campo donde ponerla",
                CENTINELA not in str(entregado.model_dump()),
                str(entregado.model_dump()),
            )

    enviado_a_telegram = "\n".join(texto for (_, texto) in tg.mensajes)
    revisar(
        "la centinela clinica SI llego a Telegram (el doctor la ve)",
        CENTINELA in enviado_a_telegram,
        enviado_a_telegram[:200],
    )


async def seis(chat: bool) -> None:
    print("\n6. Sin --chat el lector va doblado (cero tokens); con --chat corre de verdad")
    if not chat:
        revisar(
            "modo doblado: ninguna comprobacion de este script llamo a la API real del lector",
            lectura.leer_archivo is _LEER_ARCHIVO_REAL,
        )
        print("       (corre con --chat para ver una lectura real, generada sobre un PDF nuevo)")
        return

    pdf = generar_pdf_minimo("Remision para ortodoncia - Clinica Dental Norte")
    print(
        f"       PDF generado en el momento: {len(pdf)} bytes -- "
        f"empieza con {pdf[:5]!r}, termina con {pdf[-6:]!r}"
    )
    revisar("el PDF generado empieza con %PDF-", pdf.startswith(b"%PDF-"))
    revisar("el PDF generado termina con %%EOF", pdf.endswith(b"%%EOF"))
    archivo = ArchivoDescargado(contenido=pdf, mime="application/pdf", nombre="remision-generada.pdf")

    leida = await lectura.leer_archivo(archivo, tipo="document")
    revisar("el lector de verdad devolvio una lectura (no None)", leida is not None)
    if leida is None:
        return

    print(
        f"\n       Lectura real: tipo_documento={leida.tipo_documento!r} "
        f"tratamiento={leida.tratamiento!r} confianza={leida.confianza!r} origen={leida.origen!r}"
    )
    revisar(
        "identifico el tratamiento que el PDF menciona (ortodoncia)",
        leida.tratamiento == "ortodoncia",
        leida.tratamiento,
    )
    revisar(
        "la confianza es alta: el documento lo dice con todas las letras",
        leida.confianza == "alta",
        leida.confianza,
    )


async def siete(chat: bool) -> None:
    print("\n7. La frase nueva del evaluador clinico (tarea 7) no abrio una puerta")
    if not chat:
        print("       (se salta sin --chat: el evaluador es un modelo real y aqui NUNCA se")
        print("        dobla -- doblarlo no demostraria nada sobre el prompt)")
        return

    frase_administrativa = "Ya me llegó tu remisión para ortodoncia, el doctor la revisa"
    frase_clinica = "Por la radiografía que mandaste, tienes una caries profunda en el 46"

    v1 = await g._preguntar(g._evaluador_clinico, frase_administrativa)
    print(f"\n       Frase administrativa: {frase_administrativa!r}")
    print(f"       Veredicto real: dispara={v1.dispara} razon={v1.motivo!r}")
    revisar(
        "la frase administrativa (nombra el tratamiento de un documento del paciente) NO dispara",
        v1.dispara is False,
        f"dispara={v1.dispara} razon={v1.motivo}",
    )

    v2 = await g._preguntar(g._evaluador_clinico, frase_clinica)
    print(f"\n       Frase clinica: {frase_clinica!r}")
    print(f"       Veredicto real: dispara={v2.dispara} razon={v2.motivo!r}")
    revisar(
        "la frase clinica (interpreta un hallazgo sobre una imagen) SI dispara -- la excepcion "
        "nueva no se llevo por delante la regla",
        v2.dispara is True,
        f"dispara={v2.dispara} razon={v2.motivo}",
    )


# ==========================================================================================
# main
# ==========================================================================================


async def ocho(url: str) -> None:
    print("\n8. Un DESCONOCIDO no abre tema, y su archivo llega al General igual")
    wa = WhatsAppDescarga(
        contenido=b"%PDF-1.4 un archivo de un numero que no es paciente",
        mime="application/pdf",
        nombre="foto-desconocido.pdf",
    )
    tg = TelegramCaptura()
    m = mensaje_documento(TEL_DESCONOCIDO, nombre_archivo="foto-desconocido.pdf")

    async def lector_doblado(archivo, *, tipo, correr=None, group_id=None) -> LecturaArchivo:
        return _lectura_canonica()

    lectura.leer_archivo = lector_doblado
    try:
        resultado = await ingesta.procesar_mensaje(
            m, whatsapp=wa, telegram=tg, database_url=url, tema_general=TEMA_GENERAL
        )
        if resultado.lectura is not None:
            await resultado.lectura
    finally:
        lectura.leer_archivo = _LEER_ARCHIVO_REAL

    revisar("el archivo del desconocido se entrego igual", resultado.reenviado is True)
    revisar(
        "fue al General, no a un hilo propio",
        bool(tg.archivos) and tg.archivos[0][1] == TEMA_GENERAL,
        str(tg.archivos),
    )
    revisar(
        "NO se creo ningun tema en Telegram (ni huerfano ni de nadie)",
        tg.temas_creados == [],
        str(tg.temas_creados),
    )
    fila = una_fila(url, "SELECT count(*) FROM pacientes WHERE telefono = %s", (TEL_DESCONOCIDO,))
    revisar(
        "y sobre todo: NO se creo la fila en `pacientes`. Esa fila ES la identidad "
        "verificada -- mandar una foto no puede verificar a nadie",
        fila == (0,),
        str(fila),
    )


async def corridas(url: str, chat: bool) -> None:
    cfg = configuracion(url)
    sembrar_pacientes(url)
    await uno_dos_tres(url)
    await cuatro(url)
    await cinco(url, cfg)
    await seis(chat)
    await siete(chat)
    await ocho(url)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Entregable de la fase 6B: el muro y el tema del paciente contra Neon. Escribe "
            "en el esquema de pruebas '" + ESQUEMA + "' y lo borra al terminar; nunca toca "
            "'public'. No le envia nada a ningun doctor ni a ningun paciente."
        )
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help=(
            "Corre el lector de archivos y el evaluador clinico DE VERDAD, sobre un PDF "
            "generado en el momento y dos frases fijas. GASTA TOKENS (~$0.02)."
        ),
    )
    args = parser.parse_args()

    directa, url = urls()
    if args.chat and not os.environ.get("OPENAI_API_KEY", "").strip():
        print("ERROR: --chat necesita OPENAI_API_KEY en .env", file=sys.stderr)
        return 1

    print(f"Base: {enmascarar(directa)}")
    print(f"Esquema de pruebas: {ESQUEMA} (se borra al terminar; 'public' no se toca)")
    print(
        "Modo: "
        + (
            "--chat, el lector y el evaluador clinico de verdad. GASTA TOKENS."
            if args.chat
            else "sin --chat, todo doblado. No gasta un token."
        )
    )

    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        # Sin `cargar_base_conocimiento`: a diferencia de `probar_atencion.py`, este script
        # nunca corre una tool ni al agente `daniela` de verdad (`EspiaResponder` no delega
        # nunca), asi que no hace falta la base de conocimiento para nada.
        persistencia.aplicar_esquema(conn)

    empezado = time.monotonic()
    try:
        asyncio.run(corridas(url, args.chat))
    finally:
        conversacion.responder = _RESPONDER_REAL
        lectura.leer_archivo = _LEER_ARCHIVO_REAL
        print("\n" + "=" * 78)
        print("Limpieza -- el esquema de pruebas tiene que desaparecer")
        print("=" * 78)
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
                "" if quedan == 0 else f"QUEDO EN PIE con datos de prueba dentro -- "
                                       f"borralo a mano: DROP SCHEMA {ESQUEMA} CASCADE",
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
        "FASE 6B -- el muro y el tema del paciente: "
        + ("OK" if fallos == 0 else f"{fallos} FALLAS")
        + f"  ({time.monotonic() - empezado:.1f}s)"
    )
    print("=" * 78)
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

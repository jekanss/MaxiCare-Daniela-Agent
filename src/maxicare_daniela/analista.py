"""Las tres frases que lleva cada caso del informe.

Corre sobre el tier nano --el mismo que los evaluadores de guardrails-- y sobre el caso YA
AGRUPADO: una llamada por caso, no una por mensaje. Siete pacientes preguntando lo mismo
cuestan UN informe, no siete. Esa agrupacion es el control de costo de verdad; el modelo
barato solo lo remata.

La prohibicion que manda sobre todo lo demas: el informe describe el hueco y recomienda la
accion, JAMAS propone el contenido clinico ni una cifra. Si pudiera, alguien lo aprobaria de
un clic y habriamos metido un precio alucinado a la base de conocimiento por la puerta de
atras -- exactamente lo que `sin_cifra_no_documentada` existe para impedir.

QUE CAMBIO EL 22/09/2026, y por que hacian falta las DOS mitades

MaxiCare leyo los informes que salian y pidio otra cosa: que digan en que paso se paso la
conversacion a una persona, cual fue la causa concreta, que se puede hacer en el negocio, y
que distingan lo comprobado de lo supuesto. Lo que salia era generico --«falta un dato de
ortodoncia», «revisar el caso»-- y la razon no era solo el prompt: **el modelo no tenia con
que ser especifico**. Recibia la etiqueta del tipo y la huella cruda, y en que paso escala
Daniela no esta en ninguna de las dos: esta en el codigo que produjo el caso.

Asi que se tocaron las dos: `MECANICA` y `_partes_de_la_huella` le ponen delante lo que
antes solo sabia el codigo, y las instrucciones le piden lo que MaxiCare pidio. Cambiar solo
el prompt habria conseguido lo peor de los dos mundos: un informe que suena especifico sobre
cosas que el modelo no puede saber.
"""

from __future__ import annotations

import asyncio
import logging

from agents import Agent, Runner
from pydantic import BaseModel, Field

from . import consumo, persistencia, sin_resolver
from .config import MODELO_EVALUADOR, config_de_corrida
from .guardrails import cifras_de

log = logging.getLogger(__name__)

#: Cuantos fallos SEGUIDOS aguanta una huella antes de que el ciclo deje de pedirla.
#:
#: `casos_sin_informe` ordena por contador y corta en `limite`, asi que cinco casos que
#: fallan de forma reproducible --un caso que revienta el contrato, uno que el control de
#: cifras rechaza siempre-- ocupan la seleccion entera y ningun caso nuevo se analiza jamas.
#: Fallar ABIERTO esta bien y no se toca; atascarse no. Tres intentos son quince minutos de
#: reintentos al ciclo de cinco: suficiente para un fallo pasajero de red, poco para un fallo
#: reproducible.
MAX_FALLOS_SEGUIDOS = 3

#: Cuantas huellas fallidas se recuerdan antes de olvidarlas todas. Es una red contra el
#: crecimiento sin fin de un `dict` de modulo, no una politica: olvidar solo significa que el
#: ciclo las vuelve a intentar.
_MAX_HUELLAS_RECORDADAS = 500

#: Fallos consecutivos por huella. En MEMORIA y no en una columna nueva a proposito: no vale
#: una migracion, y que un reinicio lo olvide es la conducta correcta --un despliegue que
#: arregla el fallo tiene que poder reintentar sin que nadie limpie nada--.
_fallos: dict[str, int] = {}


def _anotar_fallo(huella: str) -> None:
    """Un intento fallido mas para esa huella, y el aviso cuando deja de intentarse."""
    _fallos[huella] = _fallos.get(huella, 0) + 1
    if _fallos[huella] == MAX_FALLOS_SEGUIDOS:
        log.error(
            "«sin resolver»: %s fallo %s veces seguidas; el ciclo deja de pedirla hasta el "
            "proximo reinicio. El caso se sigue viendo en la pantalla, sin informe.",
            huella, MAX_FALLOS_SEGUIDOS,
        )
    if len(_fallos) > _MAX_HUELLAS_RECORDADAS:
        log.warning("«sin resolver»: demasiadas huellas fallidas recordadas; se reintentan todas")
        _fallos.clear()


class InformeDelCaso(BaseModel):
    """La unica forma que puede tener un informe.

    Los topes no son decoracion: un informe de tres parrafos no lo abre nadie en una reunion.
    Un modelo con este `output_type` no puede divagar aunque quiera.

    Subieron de 280/280/320 a 420/420/700 el 22/09/2026, y el que subio de verdad es el
    tercero. La recomendacion tiene que decir tres cosas --que hacer, que permitiria resolver,
    y cuando tiene sentido dejarlo como esta-- y en 320 caracteres el modelo se comia la
    tercera, que es justo la que convierte una orden en una opcion. Los otros dos suben poco,
    porque ahi el riesgo es el contrario: un «que paso» largo se lee como relleno.
    """

    que_paso: str = Field(
        max_length=420,
        description="Que intentaba resolver el paciente, en que paso se paso a una persona, "
                    "y que quedo pendiente.",
    )
    por_que: str = Field(
        max_length=420,
        description="La causa concreta, separando lo comprobado de la hipotesis.",
    )
    recomiendo: str = Field(
        max_length=700,
        description="La accion de negocio, que resolveria, y cuando dejarlo como esta. "
                    "Nunca el contenido que falta.",
    )


#: Que significa MECANICAMENTE cada clase de caso.
#:
#: Es lo que permite que el informe diga «en que paso se paso a una persona» sin inventarselo.
#: Sin esto el modelo veia una etiqueta (`FALTA_DATO`) y una huella, y rellenaba el hueco con
#: lo que sonaba razonable: «el paciente se mostro molesto», «la consulta era compleja».
#: Ninguna de las dos cosas consta en ningun sitio. Lo que SI consta es el mecanismo, y es lo
#: que hay escrito aqui.
#:
#: Va en este modulo y no en `sin_resolver.py` a proposito: aquello es puro y describe COMO se
#: arma una huella; esto es texto para un modelo y describe QUE significa para una clinica.
MECANICA: dict[str, str] = {
    "FALTA_DATO": (
        "Daniela consulto la base de conocimiento de la clinica por un (tratamiento, concepto) "
        "concreto y no habia ficha aprobada. La consulta devolvio «SIN DATO DOCUMENTADO», que "
        "es el texto que le ordena no inventarse nada, decirle al paciente que lo confirma con "
        "el equipo, y avisar a los doctores. El paciente se quedo sin esa respuesta en ese "
        "momento."
    ),
    "GUARDRAIL": (
        "Un control automatico freno a Daniela. Los controles miran la respuesta ANTES de "
        "enviarla --que no diga una cifra que nadie aprobo, que no ofrezca una hora que "
        "ninguna consulta verifico, que no interprete una radiografia-- o la entrada antes de "
        "contestarla --que no se use el sistema para encargos ajenos a la clinica--. Cuando "
        "salta, Daniela reescribe la respuesta una vez; si vuelve a saltar, manda un mensaje "
        "prudente y avisa a los doctores."
    ),
    "ROTO": (
        "El turno se corto por una falla tecnica: una excepcion, un limite de la conversacion, "
        "o un envio que no salio. No es una decision del sistema: es una averia. El paciente "
        "recibio un mensaje prudente o no recibio nada."
    ),
    "HUMANO": (
        "Daniela decidio que esto lo tenia que ver una persona y aviso a los doctores. El "
        "sub-motivo va dentro de la huella y cada uno significa algo distinto: `clinico` es "
        "que se topo con el limite de lo que no puede decir sin un diagnostico; "
        "`dato_faltante` es que le falto un dato aprobado; `agenda_llena` es que no habia cupo "
        "que ofrecer; `archivo_recibido` es que llego algo que tiene que mirar un humano; "
        "`excepcion_comercial` es que pidieron algo que se sale de la regla."
    ),
    # El relevo tiene mecanica PROPIA y no es una variedad de lo de arriba, aunque comparta el
    # tipo `HUMANO`. Lo de arriba lo decide Daniela y es automatico; esto lo decide una persona
    # pulsando un boton. Mezclados --como estaban hasta el 23/09/2026-- el modelo leia «el
    # sistema paso la conversacion a una persona» y escribia que la automatizacion se habia
    # quedado corta, incluso cuando lo unico que habia pasado era que un doctor quiso hablar el.
    "HUMANO:relevo": (
        "Un doctor TOMO la conversacion: a partir de ese momento Daniela calla y le escribe el "
        "doctor en persona, por WhatsApp, desde Telegram o desde el panel. Lo decide una "
        "persona pulsando un boton, nunca el sistema.\n"
        "El tercer trozo de la huella dice de donde salio, y la diferencia lo cambia todo:\n"
        "- Un MOTIVO (`dato_faltante`, `clinico`, `agenda_llena`, `archivo_recibido`, "
        "`excepcion_comercial`): Daniela escalo por eso, sono el aviso y el doctor lo pulso. "
        "Ahi la automatizacion SI se quedo corta, y el motivo dice exactamente en que.\n"
        "- `_sin_aviso`: NADIE escalo. Daniela no pidio ayuda; el doctor entro por su cuenta. "
        "Puede ser que quisiera recordarle una cita, adelantarse a algo, o sencillamente "
        "hablar el. **Que un doctor tome una conversacion NO demuestra que la automatizacion "
        "fallara**, y aqui no consta que fallara.\n"
        "En `_sin_aviso` no hay frases guardadas, y eso NO es una laguna: como no hubo aviso, "
        "no hay ningun mensaje del que conste que provocara el relevo, y guardar el ultimo que "
        "el paciente escribiera seria atribuirle una causa que nadie registro."
    ),
}


def _mecanica(caso: dict) -> str:
    """Que significa este caso, con el relevo separado de lo que decide Daniela.

    `MECANICA` se indexa por tipo, y el relevo comparte el tipo `HUMANO` con los cinco motivos
    que emite el modelo sin ser uno de ellos --`MotivoEscalamiento` tiene cinco y `relevo` no
    esta--. Antes que abrir un quinto tipo, que costaria tocar el CHECK de la 018 y las cuatro
    pastillas de la pantalla, la clave lleva el sub-motivo cuando hace falta.
    """
    if caso["tipo"] == "HUMANO" and caso["huella"].startswith("humano:relevo"):
        return MECANICA["HUMANO:relevo"]
    return MECANICA.get(caso["tipo"], "(clase desconocida)")

#: Lo que el analista NO tiene delante, dicho para que no lo suponga.
#:
#: Ve el caso AGREGADO: la huella, los contadores, y hasta cinco frases sueltas de pacientes
#: distintos. No ve la conversacion. Sin esta lista dentro del prompt, el modelo escribia como
#: si la hubiera leido entera.
SIN_EVIDENCIA = (
    "la conversacion completa, lo que Daniela respondio, el tono del paciente, si volvio a "
    "escribir, si acabo agendando, ni si un doctor le contesto despues"
)


_analista = Agent(
    name="analista_sin_resolver",
    model=MODELO_EVALUADOR,
    output_type=InformeDelCaso,
    instructions=(
        "Lees un caso AGRUPADO de cosas que Daniela --la asistente de WhatsApp de una clinica "
        "dental-- no pudo resolver sola, y escribes tres campos para que el equipo de la "
        "clinica decida que ajustar en el negocio.\n\n"

        "Escribes para la clinica, no para un programador: NUNCA nombres de errores tecnicos, "
        "nombres de funciones ni nombres de controles en tu texto.\n\n"

        "== LO QUE VES Y LO QUE NO ==\n"
        "Ves el caso ya agrupado: que clase de caso es, que significa esa clase, de que va en "
        "concreto, cuantas veces paso, cuantas se interrumpio a un doctor, y hasta cinco "
        "frases sueltas de pacientes distintos. NO ves " + SIN_EVIDENCIA + ". Escribe solo "
        "sobre lo que ves.\n"
        "PROHIBIDO atribuirle al paciente una reaccion --molestia, prisa, desconfianza, que se "
        "fue a otra clinica-- que no este escrita literalmente en una de las frases. Si una "
        "frase lo dice, citala; si no, no existe.\n\n"

        "== que_paso ==\n"
        "Tres cosas, en este orden, en dos o tres frases:\n"
        "1. Que intentaba resolver el paciente. Sale de las frases y de la huella. Si las "
        "frases no lo dejan claro, dilo asi: «no se puede determinar que buscaba; las frases "
        "guardadas no lo dicen».\n"
        "2. En que paso se paso a una persona. Eso SI lo sabes: te lo dice la mecanica de la "
        "clase. Dilo en llano.\n"
        "3. Que quedo pendiente para el paciente en ese momento.\n"
        "Identifica el caso por lo que es --el tratamiento y el dato concretos, el sub-motivo "
        "concreto-- y no con una etiqueta generica.\n\n"

        "== por_que ==\n"
        "La causa CONCRETA, y separa dos cosas:\n"
        "- Lo COMPROBADO: lo que se deduce de la mecanica y de la huella. Que la clinica no "
        "tiene cargado ese dato para ese tratamiento, por ejemplo, CONSTA: es exactamente lo "
        "que produjo el caso. Escribelo sin rodeos y sin llamarlo suposicion.\n"
        "- La HIPOTESIS: cualquier cosa que infieras de las frases. Va siempre empezando por "
        "la palabra «Hipotesis:», y solo si aporta algo.\n"
        "Si no puedes determinar la causa, di exactamente que no se puede determinar y que "
        "informacion haria falta para saberlo. Esa es una respuesta valida y util; inventarse "
        "una causa para parecer preciso, no.\n"
        "Pero NO conviertas esa falta en la causa. «Se derivo porque no habia contexto "
        "suficiente», «se paso a una persona porque falta informacion de la conversacion» y "
        "cualquier frase parecida estan PROHIBIDAS: describen lo que tu no ves, no lo que le "
        "paso al paciente. Lo que a ti te falte no le ocurrio a nadie. Si no hay causa "
        "comprobable, una frase basta para decirlo y el resto sobra.\n\n"

        "== recomiendo ==\n"
        "Una accion de negocio concreta, presentada como OPCION, y ATADA a la causa que "
        "acabas de escribir en `por_que`. Si la causa fue que faltaba un dato, la accion es "
        "sobre ese dato. Tres partes:\n"
        "1. Que hacer, en terminos de lo que la clinica controla: cargar un dato, decidir una "
        "politica, ampliar una lista, cambiar un horario. Di QUE dato o QUE decision falta. "
        "Nunca «revisar el caso manualmente» si puedes senalar que informacion falta.\n"
        "2. Que permitiria resolver en futuras conversaciones.\n"
        "3. Cuando tiene sentido DEJARLO COMO ESTA, y dicho en serio: hay cosas que la clinica "
        "prefiere que confirme siempre una persona, y esa es una decision legitima. Cierra con "
        "esa alternativa.\n"
        "No ordenas ni das por hecho que se vaya a hacer: propones. La clinica decide.\n"
        "**Si en `por_que` no pudiste determinar la causa, este campo NO lleva las tres "
        "partes.** Sin causa no hay accion que proponer, y las partes 2 y 3 se vuelven relleno "
        "que suena a consejo. Escribe entonces UNA sola cosa: que haria falta registrar o "
        "mirar para saber por que paso. Un campo corto y honesto vale mas que tres frases que "
        "valen para cualquier caso.\n"
        "PROHIBIDO cerrar recomendando «conservar» lo que ya existe --el aviso al doctor, la "
        "derivacion, el control-- cuando no has senalado antes nada que cambiar: eso no es una "
        "opcion, es describir lo que ya pasa.\n\n"

        "== PROHIBIDO, sin excepcion ==\n"
        "Proponer el contenido que falta. No inventas ni sugieres una cifra, un precio, una "
        "duracion, un diagnostico ni un protocolo. Puedes decir que falta la ficha de un "
        "precio; no puedes decir cual es ese precio. Si no lo sabes --y no lo sabes-- lo "
        "dices.\n\n"

        "== LOS MATICES VALEN ORO ==\n"
        "Si las frases muestran un matiz --preguntan por la cuota mensual y no por el total, "
        "usan una palabra distinta de la que la clinica tiene cargada, preguntan por algo que "
        "la clinica quiza ni ofrece-- ese matiz es lo mas valioso que puedes aportar: dilo en "
        "`recomiendo`, porque cambia QUE hay que cargar.\n"
        "Si el caso parece deliberado --la clinica decidio no dar ese dato por chat, o el "
        "control hizo justo lo que tenia que hacer-- dilo, y en vez de pedir que se arregle, "
        "senala el volumen como dato de negocio. Pero eso vale SOLO cuando hay volumen del que "
        "hablar --el caso paso varias veces-- Y consta que fue deliberado. Un caso de una o "
        "dos veces no es un volumen, y «usa este dato de negocio» ahi no dice nada: es la "
        "frase que se escribe cuando no hay nada que decir, y si no hay nada que decir es "
        "mejor decir eso."
    ),
)


def _con_cifra(informe: InformeDelCaso) -> set[str]:
    """Las cifras de dinero que el informe trae dentro. Vacio es lo normal.

    Control DETERMINISTA, sin una segunda llamada al modelo: hoy lo unico que impide que el
    nano invente un precio de ortodoncia es el texto de sus instrucciones, y un informe con
    un precio dentro es exactamente lo que alguien podria aprobar de un clic hacia la base de
    conocimiento. Es el mismo mecanismo que sostiene `sin_cifra_no_documentada`, reutilizado.

    `cifras_de` solo caza cifras con forma de DINERO, asi que los contadores del informe
    pasan limpios: «doce personas preguntaron por ortodoncia», «12 pacientes en 7 dias» y «se
    molesto al doctor 4 de 12 veces» devuelven todos el conjunto vacio. Solo salta un precio.
    """
    return cifras_de(" ".join(informe.model_dump().values()))


def _partes_de_la_huella(huella: str) -> str:
    """La huella repartida en las dos o tres cosas concretas que lleva dentro.

    La huella es `falta_dato:ortodoncia:precio`, y el modelo la recibia tal cual. Con la
    cadena cruda escribia «falta un dato de ortodoncia»; con las partes separadas y nombradas
    puede escribir «la clinica no tiene cargado el precio de la ortodoncia», que es lo que se
    puede accionar. Su forma la fija `sin_resolver.py` y es fija, asi que esto no adivina
    nada: reparte.
    """
    clase, _, resto = huella.partition(":")
    uno, _, dos = resto.partition(":")
    if clase == "falta_dato":
        cual = "un dato general de la clinica, no de un tratamiento" if uno == "_general" else f"«{uno}»"
        concepto = "la ficha entera" if dos in ("", "_general") else f"«{dos}»"
        return (
            f"  tratamiento por el que se pregunto: {cual}\n"
            f"  dato concreto que no estaba cargado: {concepto}"
        )
    if clase == "guardrail":
        sobre = "ningun tratamiento en concreto" if dos in ("", "_general") else f"«{dos}»"
        return (
            f"  control que freno la respuesta: {uno}\n"
            f"  se estaba hablando de: {sobre}"
        )
    if clase == "roto":
        return f"  clase de la averia: {uno}"
    if clase == "humano" and uno == "relevo":
        # El unico caso de tres trozos, y el trozo nuevo es el que contesta «por que se
        # derivo». Se traduce aqui --y no se le pasa crudo-- porque `_sin_aviso` es una
        # convencion nuestra: el modelo leeria un guion bajo y un motivo que no existe.
        if dos in ("", sin_resolver.SIN_AVISO):
            return (
                "  un doctor tomo la conversacion\n"
                "  quien lo pidio: NADIE. No hubo aviso: Daniela no escalo y el doctor entro "
                "por su cuenta"
            )
        return (
            "  un doctor tomo la conversacion\n"
            f"  aviso que pulso para entrar: Daniela habia escalado por «{dos}»"
        )
    if clase == "humano":
        return f"  sub-motivo por el que se paso a una persona: {uno}"
    return f"  identificador del caso: {huella}"


def texto_del_caso(caso: dict) -> str:
    """Lo que ve el modelo.

    Dejo de ser una ficha tecnica el 22/09/2026. Antes iba la etiqueta del tipo y la huella
    cruda, y con eso el informe solo podia ser generico: no habia forma de que supiera en que
    paso se habia pasado la conversacion a una persona, porque esa informacion no estaba en la
    entrada --esta en el codigo que produjo el caso--. Ahora van tres cosas que faltaban: la
    mecanica de la clase, la huella repartida, y la proporcion de veces que se interrumpio a
    un doctor.

    Sigue siendo corto --la entrada tambien cuesta--: son unas lineas fijas por caso, no una
    conversacion.
    """
    ejemplos = "\n".join(f"  - «{e}»" for e in caso.get("ejemplos") or []) or "  (ninguna)"
    contador = caso["contador"]
    escalo = caso["escalo"]
    if escalo == 0:
        interrupciones = "no se interrumpio a ningun doctor por esto"
    elif escalo == contador:
        interrupciones = "se interrumpio a un doctor todas las veces"
    else:
        interrupciones = f"se interrumpio a un doctor {escalo} de esas veces"

    return (
        f"CLASE DE CASO: {caso['tipo']}\n"
        f"QUE SIGNIFICA ESA CLASE: {_mecanica(caso)}\n\n"
        f"DE QUE VA ESTE CASO EN CONCRETO:\n{_partes_de_la_huella(caso['huella'])}\n\n"
        f"CUANTO: paso {contador} {'vez' if contador == 1 else 'veces'}, y {interrupciones}.\n"
        f"DESDE: {caso['primera_vez']}\n"
        f"HASTA: {caso['ultima_vez']}\n\n"
        "LO QUE ESCRIBIERON LOS PACIENTES (frases sueltas, de personas distintas):\n"
        f"{ejemplos}"
    )


async def analizar_pendientes(*, database_url: str, limite: int = 5) -> int:
    """Le escribe el informe a los casos que no lo tienen. Devuelve cuantos escribio.

    Falla ABIERTO, como `_preguntar` en `guardrails.py`: un caso sin informe se muestra igual
    --con su contador y sus ejemplos-- y se reintenta en el ciclo siguiente. Un analista
    caido no puede dejar la pantalla vacia.

    Pero el reintento esta ACOTADO (`MAX_FALLOS_SEGUIDOS`). Fallar abierto sin tope es
    atascarse: la consulta ordena por contador y corta, asi que cinco casos que fallan de
    forma reproducible se quedan con los cinco cupos y ningun caso nuevo se analiza nunca.

    `persistencia.conectar`/`casos_sin_informe`/`guardar_informe` son psycopg SINCRONO, y esta
    tarea corre en el mismo bucle de eventos que atiende los webhooks de WhatsApp --un solo
    worker, a proposito (`.claude/rules/despliegue.md`)--. Sin `to_thread` cada vuelta congela
    el proceso entero mientras Neon responde. Sigue el mismo patron que
    `seguimientos.despachar`: abrir y cerrar la conexion tambien van por `to_thread`, como todo
    lo demas -- `psycopg.connect` contra Neon es un handshake TLS completo y el cierre hace
    commit o rollback antes de soltar el socket, los dos bloqueantes. `Runner.run` SI es un
    `await` de verdad y no entra en ningun `to_thread`: lo que se manda al hilo es solo lo
    sincrono de psycopg.
    """
    escritos = 0
    conn = await asyncio.to_thread(persistencia.conectar, database_url)
    try:
        # Se piden TRES veces los que caben y se filtran los atascados aqui. Asi una huella
        # que falla siempre deja de ocupar uno de los cinco cupos en vez de bloquearlos: la
        # consulta ordena por contador y sin esto los mismos cinco volvian cada cinco
        # minutos, para siempre, con `escritos` en 0 y el `log.info` de `runtime` mudo --el
        # fallo era silencioso justo en la metrica que uno miraria--.
        candidatos = await asyncio.to_thread(
            persistencia.casos_sin_informe, conn, limite=limite * 3
        )
        casos = [
            c for c in candidatos if _fallos.get(c["huella"], 0) < MAX_FALLOS_SEGUIDOS
        ][:limite]
        for caso in casos:
            try:
                corrida = await Runner.run(
                    _analista,
                    texto_del_caso(caso),
                    max_turns=1,
                    run_config=config_de_corrida(canal="informe"),
                )
                # Sin `id_conversacion` ni `telefono`: un informe agrupa casos de VARIOS
                # pacientes --ese es todo su valor-- así que atarlo a uno solo sería falso.
                # El gasto del analista se mira por agente, que es como se decide si vale lo
                # que cuesta.
                await consumo.anotar(
                    corrida,
                    agente="analista",
                    modelo=MODELO_EVALUADOR,
                    database_url=database_url,
                )
                informe: InformeDelCaso = corrida.final_output
            except Exception as e:  # noqa: BLE001 -- se reintenta en el ciclo siguiente
                log.error("el analista fallo en %s (%s); se reintenta", caso["huella"], e)
                _anotar_fallo(caso["huella"])
                continue

            # El control determinista contra una cifra inventada. No se guarda: el caso se
            # queda con `informe IS NULL`, la pantalla lo muestra igual --con su contador y
            # sus ejemplos-- y el ciclo lo reintenta. Y cuenta como fallo, porque un modelo
            # que insiste en poner un precio ahi insistiria para siempre.
            cifras = _con_cifra(informe)
            if cifras:
                log.error(
                    "el analista propuso una cifra (%s) en %s; el informe NO se guarda",
                    sorted(cifras), caso["huella"],
                )
                _anotar_fallo(caso["huella"])
                continue

            await asyncio.to_thread(
                persistencia.guardar_informe,
                conn, huella=caso["huella"], informe=informe.model_dump(),
                sobre=caso["contador"],
            )
            _fallos.pop(caso["huella"], None)
            escritos += 1
    finally:
        await asyncio.to_thread(_cerrar, conn)

    return escritos


def _cerrar(conn) -> None:
    """Cierra la conexion del ciclo. Un fallo cerrandola no se lleva por delante el recuento
    de informes ya escritos -- cada `guardar_informe` confirma por su cuenta."""
    try:
        conn.close()
    except Exception:  # noqa: BLE001 -- el ciclo ya hizo su trabajo; esto es limpieza
        log.warning("no se pudo cerrar la conexion del analisis de «sin resolver»", exc_info=True)

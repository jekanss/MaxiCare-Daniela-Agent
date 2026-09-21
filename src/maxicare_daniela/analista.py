"""Las tres frases que lleva cada caso del informe.

Corre sobre el tier nano --el mismo que los evaluadores de guardrails-- y sobre el caso YA
AGRUPADO: una llamada por caso, no una por mensaje. Siete pacientes preguntando lo mismo
cuestan UN informe, no siete. Esa agrupacion es el control de costo de verdad; el modelo
barato solo lo remata.

La prohibicion que manda sobre todo lo demas: el informe describe el hueco y recomienda la
accion, JAMAS propone el contenido clinico ni una cifra. Si pudiera, alguien lo aprobaria de
un clic y habriamos metido un precio alucinado a la base de conocimiento por la puerta de
atras -- exactamente lo que `sin_cifra_no_documentada` existe para impedir.
"""

from __future__ import annotations

import asyncio
import logging

from agents import Agent, Runner
from pydantic import BaseModel, Field

from . import consumo, persistencia
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
    """

    que_paso: str = Field(max_length=280, description="Que ocurrio, en numeros y en llano.")
    por_que: str = Field(max_length=280, description="La causa concreta.")
    recomiendo: str = Field(max_length=320, description="La accion. Nunca el contenido.")


_analista = Agent(
    name="analista_sin_resolver",
    model=MODELO_EVALUADOR,
    output_type=InformeDelCaso,
    instructions=(
        "Lees un caso agrupado de cosas que una asistente de una clinica dental no pudo "
        "resolver, y escribes tres frases para que el equipo decida que ajustar.\n\n"
        "Escribes para la clinica, no para un programador: NUNCA nombres de errores tecnicos, "
        "nombres de funciones ni nombres de guardrails en tu texto.\n\n"
        "PROHIBIDO, sin excepcion: proponer el contenido que falta. No inventas ni sugieres "
        "una cifra, un precio, una duracion, un diagnostico ni un protocolo. Puedes decir que "
        "falta la ficha de un precio; no puedes decir cual es ese precio. Si no lo sabes --y "
        "no lo sabes-- lo dices.\n\n"
        "Si los ejemplos muestran un matiz (preguntan por la cuota mensual y no por el total, "
        "usan una palabra distinta a la que la clinica tiene cargada), ese matiz es lo mas "
        "valioso que puedes aportar: dilo en `recomiendo`.\n\n"
        "Si el caso parece deliberado --la clinica decidio no dar ese dato por chat-- dilo, y "
        "en vez de pedir que se arregle, senala el volumen como dato de negocio."
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


def texto_del_caso(caso: dict) -> str:
    """Lo que ve el modelo. Corto a proposito: la entrada tambien cuesta."""
    ejemplos = "\n".join(f"  - {e}" for e in caso.get("ejemplos") or []) or "  (ninguno)"
    return (
        f"tipo: {caso['tipo']}\n"
        f"huella: {caso['huella']}\n"
        f"veces: {caso['contador']}\n"
        f"de esas, se molesto al doctor: {caso['escalo']}\n"
        f"primera vez: {caso['primera_vez']}\n"
        f"ultima vez: {caso['ultima_vez']}\n"
        f"lo que escribieron los pacientes:\n{ejemplos}"
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

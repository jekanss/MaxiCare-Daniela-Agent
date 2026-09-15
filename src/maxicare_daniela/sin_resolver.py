"""Lo que Daniela no pudo resolver, convertido en casos agrupables.

Todo lo de aqui es PURO: ni una conexion, ni una llamada al modelo, ni un reloj. La
escritura vive en `persistencia`, el informe en `analista`, y el volcado en `atencion`.

La pieza central es la HUELLA. Es lo que hace que doce pacientes preguntando el precio de
ortodoncia sean UNA linea con contador 12 y no doce renglones que nadie termina de leer.
La arma el codigo, nunca el modelo: si el modelo pudiera escribirla, dos casos identicos
saldrian con huellas distintas y la agrupacion --que es todo el valor-- se romperia en
silencio, sin un solo error en el log.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Cuantas frases de pacientes guarda un caso. Cinco alcanzan para ver el matiz --si
#: preguntan por el total o por la cuota-- sin convertir la fila en un archivo de mensajes.
#: El recorte a este limite lo hace el SQL de `persistencia.registrar_caso`, no una funcion
#: de este modulo: entre un SELECT y un UPDATE en Python cabe el turno de otro paciente.
MAX_EJEMPLOS = 5

#: Un `fallo_respuesta` que empieza asi NO es un fallo: es un mensaje que entro durante un
#: relevo (no negociable 15). Sin este filtro, cada relevo ensucia el informe.
PREFIJO_RELEVO = "relevo:"

#: Cuando salta un guardrail y en el turno no se consulto ningun tratamiento.
GENERAL = "_general"


@dataclass(frozen=True)
class Senal:
    """Una consulta a la base de conocimiento durante un turno.

    Se anota SIEMPRE, con dato o sin el. Las que tienen dato no producen caso, pero dan el
    tratamiento con el que se enriquece la huella de un guardrail -- que es lo que permite
    que el informe diga «las 4 eran por limpieza dental» y no solo «salto 4 veces».
    """

    tratamiento: str
    concepto: str
    hubo_dato: bool


@dataclass(frozen=True)
class Caso:
    """Un caso listo para escribir. `ejemplo` es `None` cuando no hay frase que guardar."""

    huella: str
    tipo: str
    escalo: int = 0
    ejemplo: str | None = None


# ==========================================================================================
# Las huellas
# ==========================================================================================


def huella_falta_dato(tratamiento: str, concepto: str) -> str:
    return f"falta_dato:{tratamiento}:{concepto or GENERAL}"


def huella_guardrail(nombre: str, tratamiento: str | None) -> str:
    return f"guardrail:{nombre}:{tratamiento or GENERAL}"


def huella_roto(motivo: str) -> str:
    """Solo el TIPO de error, nunca el mensaje.

    Hoy se guarda `f"{type(e).__name__}: {e}"`, y el mensaje lleva ids, horas y fragmentos
    variables. Con el mensaje completo cada error seria unico y la tabla no agruparia jamas:
    tendrias cuatrocientas filas diciendo lo mismo.
    """
    return f"roto:{motivo.split(':', 1)[0].strip()}"


def huella_humano(motivo: str) -> str:
    return f"humano:{motivo}"


# ==========================================================================================
# Los ejemplos
# ==========================================================================================


def sin_telefonos(ejemplos: list[dict]) -> list[str]:
    """Solo las frases. Lo unico que puede cruzar hacia el navegador."""
    return [e["texto"] for e in ejemplos if e.get("texto")]


# ==========================================================================================
# El turno entero, en casos
# ==========================================================================================


def casos_del_turno(
    *,
    senales: list[Senal],
    tripwires: list[str],
    escalado_por: str | None,
    motivo: str | None,
    frase: str | None,
) -> list[Caso]:
    """Todo lo que este turno deja en el informe. Lista vacia es lo normal.

    El orden importa y es este a proposito: el hueco de conocimiento se resuelve PRIMERO,
    porque si lo hay, el escalamiento se cuenta dentro de el (columna `escalo`) en vez de
    abrir un caso propio. Si abriera uno, la misma historia saldria contada dos veces en dos
    tarjetas y el informe perderia justo lo que lo hace legible.
    """
    casos: list[Caso] = []
    huecos = [s for s in senales if not s.hubo_dato]
    hubo_escalamiento = escalado_por is not None and escalado_por != "ninguno"

    # 1. Los huecos de conocimiento. El escalamiento, si lo hubo, se cuelga del primero.
    for i, hueco in enumerate(huecos):
        casos.append(
            Caso(
                huella=huella_falta_dato(hueco.tratamiento, hueco.concepto),
                tipo="FALTA_DATO",
                escalo=1 if (hubo_escalamiento and i == 0) else 0,
                ejemplo=frase,
            )
        )

    # 2. Los guardrails. Se enriquecen con el tratamiento que se consulto en el turno, haya
    #    tenido dato o no: es lo que convierte «salto 4 veces» en «las 4 eran por limpieza
    #    dental», que es la mitad del valor del informe.
    tratamiento = senales[-1].tratamiento if senales else None
    for nombre in tripwires:
        casos.append(Caso(huella=huella_guardrail(nombre, tratamiento), tipo="GUARDRAIL"))

    # 3. El escalamiento sin hueco: urgencia, queja, excepcion comercial.
    if hubo_escalamiento and not huecos:
        casos.append(
            Caso(huella=huella_humano(escalado_por), tipo="HUMANO", escalo=1, ejemplo=frase)
        )

    # 4. Lo que se rompio. `relevo:` se filtra: no es un fallo.
    if motivo and not motivo.startswith(PREFIJO_RELEVO):
        casos.append(Caso(huella=huella_roto(motivo), tipo="ROTO"))

    return casos

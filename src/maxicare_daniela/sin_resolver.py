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

#: Cuanto de la frase de un paciente se guarda como ejemplo.
#:
#: Un caso guarda cinco, y el informe de la tarea 3 se las pasa TODAS al modelo por cada
#: tarjeta que analiza: el tope no protege la fila --`ejemplos` es TEXT-- sino lo que cuesta
#: y lo que se lee. Doscientos ochenta caracteres es lo que ocupa una pregunta de WhatsApp
#: dicha entera («cuanto me sale la ortodoncia y si la puedo pagar en cuotas, y cuanto se
#: demora»); lo que pase de ahi es alguien pegando un texto, y para ver el matiz --si
#: preguntan por el total o por la cuota-- el principio ya alcanza.
MAX_FRASE = 280

#: Un `fallo_respuesta` que empieza asi NO es un fallo: es un mensaje que entro durante un
#: relevo (no negociable 15). Sin este filtro, cada relevo ensucia el informe.
PREFIJO_RELEVO = "relevo:"

#: Ni esto es un fallo APARTE: `conversacion.responder` escribe el mismo hecho en dos
#: sitios, `Resultado.tripwires` y `Resultado.fallo` («tripwire de entrada: X», «tripwire
#: repetido: X y luego Y»). El guardrail ya tiene su tarjeta; sin este filtro, la misma
#: historia salia ademas como `roto:tripwire de entrada`.
PREFIJO_TRIPWIRE = "tripwire"

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


def frase_para_el_informe(textos: list[str | None]) -> str | None:
    """Lo que el PACIENTE escribio en el turno, recortado. `None` si no escribio nada.

    Recibe los textos crudos de los mensajes del grupo y NO la entrada que se le arma al
    modelo. Esa entrada lleva andamiaje --la cabecera de «esto vino en varios mensajes» y,
    si hubo archivo, un aviso con el NOMBRE del archivo dentro--, y todo eso acaba en la
    pantalla de la clinica y en el informe que lee el modelo de la tarea 3. Dos danos: el
    ejemplo deja de leerse como la pregunta de un paciente, y un nombre de archivo aparece
    donde nadie lo pidio. Esta funcion existe para que ese andamiaje no pueda colarse.

    Un turno sin una sola palabra --una radiografia sola-- no deja ejemplo. Guardar algo
    ahi obligaria a inventarselo.
    """
    frase = "\n".join(t.strip() for t in textos if t and t.strip())
    if not frase:
        return None
    return frase if len(frase) <= MAX_FRASE else frase[:MAX_FRASE] + "..."


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

    UNA HISTORIA, UNA TARJETA. Es la regla que decide todo lo de aqui, y no es estetica: la
    tabla promete que doce pacientes preguntando lo mismo sean UNA fila con contador doce.
    Un turno que escribe la misma huella dos veces rompe justo esa promesa, y la rompe en el
    turno que mas importa -- el que fallo. De ahi las cuatro decisiones de esta funcion:

    1. **Las senales repetidas se deduplican.** `ctx.turno.senales` NO se vacia entre la
       corrida original y la regeneracion --tiene que no vaciarse, o el hueco del primer
       intento se perderia--, asi que un turno donde el modelo consulta, salta un guardrail
       y vuelve a consultar lo mismo llega aqui con la senal repetida.
    2. **El hueco de conocimiento se resuelve PRIMERO**, porque si lo hay, el escalamiento
       se cuenta dentro de el (columna `escalo`) en vez de abrir un caso propio: el hueco es
       la causa y el escalamiento el sintoma.
    3. **Si no hubo hueco pero si guardrail, el escalamiento se cuelga del guardrail**, por
       lo mismo. Una inyeccion de prompt dejaba tres tarjetas --GUARDRAIL, ROTO y HUMANO--
       para un solo mensaje.
    4. **Un `motivo` que describe un tripwire no es un `ROTO`.** `conversacion.responder`
       escribe ese hecho en dos sitios a la vez, `tripwires` y `fallo`.
    """
    casos: list[Caso] = []
    hubo_escalamiento = escalado_por is not None and escalado_por != "ninguno"

    # Por HUELLA y no por `(tratamiento, concepto)` a secas: es la huella la que agrupa en la
    # base, y es ella la que normaliza un concepto vacio a `_general`.
    huecos: dict[str, Senal] = {}
    for senal in senales:
        if not senal.hubo_dato:
            huecos.setdefault(huella_falta_dato(senal.tratamiento, senal.concepto), senal)

    # Los guardrails tambien: las dos ramas del doble disparo pueden traer el mismo nombre.
    nombres = list(dict.fromkeys(tripwires))

    # 1. Los huecos de conocimiento. El escalamiento, si lo hubo, se cuelga del primero.
    for i, huella in enumerate(huecos):
        casos.append(
            Caso(
                huella=huella,
                tipo="FALTA_DATO",
                escalo=1 if (hubo_escalamiento and i == 0) else 0,
                ejemplo=frase,
            )
        )

    # 2. Los guardrails. Se enriquecen con el tratamiento que se consulto en el turno, haya
    #    tenido dato o no: es lo que convierte «salto 4 veces» en «las 4 eran por limpieza
    #    dental», que es la mitad del valor del informe. Y si no hubo hueco, el escalamiento
    #    se cuenta aqui en vez de abrir un `HUMANO` que contaria lo mismo.
    #
    #    Con la frase, como los otros tres. Sin ella, el analista recibe un nombre de
    #    tripwire y un contador, y con eso no puede escribir ni «que paso» ni «recomiendo»
    #    -- justo para la senal que hoy se pierde entera, que es por la que existe esta
    #    tabla. Lo que vale del informe es poder notar que cinco de los siete ejemplos
    #    preguntan por la cuota mensual, y eso solo se ve con las frases delante.
    tratamiento = senales[-1].tratamiento if senales else None
    for i, nombre in enumerate(nombres):
        casos.append(
            Caso(
                huella=huella_guardrail(nombre, tratamiento),
                tipo="GUARDRAIL",
                escalo=1 if (hubo_escalamiento and not huecos and i == 0) else 0,
                ejemplo=frase,
            )
        )

    # 3. El escalamiento a secas: urgencia, queja, excepcion comercial. Nada mas lo explica.
    if hubo_escalamiento and not huecos and not nombres:
        casos.append(
            Caso(huella=huella_humano(escalado_por), tipo="HUMANO", escalo=1, ejemplo=frase)
        )

    # 4. Lo que se rompio de verdad. Se filtran los dos motivos que no son un fallo: el del
    #    relevo, y el del tripwire que ya tiene su tarjeta arriba.
    if motivo and not motivo.startswith((PREFIJO_RELEVO, PREFIJO_TRIPWIRE)):
        casos.append(Caso(huella=huella_roto(motivo), tipo="ROTO"))

    return casos

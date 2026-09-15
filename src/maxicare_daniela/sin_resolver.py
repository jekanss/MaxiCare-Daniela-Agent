"""Lo que Daniela no pudo resolver, convertido en casos agrupables.

Todo lo de aqui es PURO: ni una conexion, ni una llamada al modelo, ni un reloj. La
escritura vive en `persistencia`, el informe en `analista`, y el volcado en `atencion`.

Y NADA se importa del paquete en la cabecera, a proposito: `contratos` importa `Senal` de
aqui, asi que un import suyo arriba cerraria el ciclo y el paquete no arrancaria. El unico
import del paquete esta DENTRO de `_sin_documentos`, diferido, y esta explicado alli.


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

#: Lo que queda en el ejemplo donde habia algo con forma de documento de identidad.
OMITIDO = "[omitido]"


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


def _normalizar(valor: str) -> str:
    """Los espacios de sobra fuera, los internos colapsados, todo en minusculas.

    Es lo que hace que la agrupacion sea de verdad. Las dos mitades de una huella de
    `FALTA_DATO` --el tratamiento y el concepto-- las escribe el LLM como texto libre, y la
    busqueda en la base es por igualdad exacta: el unico caso que llega a producir fila es
    justo aquel en que esos valores NO pertenecen a ningun vocabulario. Sin normalizar,
    `Precio`, `precio`, ` precio` y `Precio ` son CUATRO filas de contador uno donde la
    clinica tenia que ver una de contador cuatro, y la tabla se degrada precisamente donde
    mas se usa.

    Se aplica a las cuatro huellas por simetria, aunque `huella_roto` y `huella_humano`
    vengan de vocabularios cerrados: una huella que se normaliza a veces es una huella que
    nadie puede razonar de memoria.
    """
    return " ".join(valor.split()).casefold()


def huella_falta_dato(tratamiento: str, concepto: str) -> str:
    return f"falta_dato:{_normalizar(tratamiento)}:{_normalizar(concepto) or GENERAL}"


def huella_guardrail(nombre: str, tratamiento: str | None) -> str:
    return f"guardrail:{_normalizar(nombre)}:{_normalizar(tratamiento or '') or GENERAL}"


def huella_roto(motivo: str) -> str:
    """Solo el TIPO de error, nunca el mensaje.

    Hoy se guarda `f"{type(e).__name__}: {e}"`, y el mensaje lleva ids, horas y fragmentos
    variables. Con el mensaje completo cada error seria unico y la tabla no agruparia jamas:
    tendrias cuatrocientas filas diciendo lo mismo.
    """
    return f"roto:{_normalizar(motivo.split(':', 1)[0])}"


def huella_humano(motivo: str) -> str:
    return f"humano:{_normalizar(motivo)}"


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

    Y lo que parezca un documento de identidad NO entra. La regla dura 4 del proyecto --«no
    se registran cedulas ni documentos de identidad de ningun tipo», prohibicion expresa del
    cliente-- se aplica aqui porque este texto (a) se guarda en una tabla, (b) se pinta en la
    pantalla del panel para TODOS los roles y (c) viaja a OpenAI en la llamada del analista.
    `atencion` ya invoca esa misma regla para no dejar entrar el nombre de un archivo
    adjunto; el texto del paciente --«mi cedula es 1.020.xxx»-- es el sitio mucho mas probable
    donde aparece una.

    Se REDACTA la parte que parece documento en vez de descartar la frase entera: la frase es
    todo el valor de la tarjeta --es lo que deja ver que cinco de siete preguntan por la cuota
    mensual-- y tirarla completa por un numero convierte un caso util en una fila muda. Que
    el patron se lleve por delante algun precio que el paciente escriba es el precio a pagar,
    y es el mismo que ya paga `identificar_paciente`.
    """
    frase = "\n".join(t.strip() for t in textos if t and t.strip())
    if not frase:
        return None
    frase = _sin_documentos(frase)
    return frase if len(frase) <= MAX_FRASE else frase[:MAX_FRASE] + "..."


def _sin_documentos(frase: str) -> str:
    """El control de forma de la regla dura 4, aplicado al texto del paciente.

    El import va DENTRO a proposito y no arriba: `contratos` importa `Senal` de este modulo
    en su cabecera, asi que un import de `contratos` aqui arriba cerraria el ciclo y el
    paquete no arrancaria. Diferido, el ciclo no existe --cuando esta funcion corre,
    `contratos` lleva rato importado-- y el patron sigue viviendo en UN solo sitio, que es lo
    que impide que dos copias de la misma regla se separen con el tiempo. Es el mismo patron
    que usa `persistencia.registrar_caso` con `MAX_EJEMPLOS`.
    """
    from .contratos import redactar_documento_de_identidad

    return redactar_documento_de_identidad(frase, OMITIDO)


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
    #
    #    Con la frase, por la misma razon que el `GUARDRAIL`: la huella se queda con el TIPO
    #    del error y tira el mensaje --tiene que tirarlo, o nada agruparia jamas--, asi que lo
    #    unico que le queda al desarrollador para saber que estaba pasando cuando reventó es
    #    lo que el paciente habia escrito. Sin ella el analista recibe «lo que escribieron los
    #    pacientes: (ninguno)» justo en la tarjeta que mas contexto necesita.
    if motivo and not motivo.startswith((PREFIJO_RELEVO, PREFIJO_TRIPWIRE)):
        casos.append(Caso(huella=huella_roto(motivo), tipo="ROTO", ejemplo=frase))

    return casos

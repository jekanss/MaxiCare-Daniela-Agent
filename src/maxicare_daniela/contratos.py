"""Modelos Pydantic del proyecto: el `output_type` de cada agente y el modelo de entrada de
cada tool, uno por cada elemento de `contratos[]` en `plan-agentes.json` cuyo
`donde_vive_validacion` sea `"schema"`.

Lo que va aquí: clases `BaseModel` -- forma y validación de datos declarada por tipo y por
`Field`. Solo la validación que el plan asigna explícitamente al schema vive aquí; la que
el plan asigna a un guardrail (`donde_vive_validacion: "guardrail"`, una regla de política
o que exige consultar otro sistema) o al código de la tool (`"codigo_tool"`, una regla de
negocio) no se duplica en este módulo -- vive donde el plan dice que vive, nunca en los dos
lugares a la vez.

Lo que NO va aquí: lógica de negocio, llamadas a sistemas externos, ni nada de transporte.
Este módulo no importa de `runtime.py`; que la respuesta de un agente termine serializada
como JSON en una API HTTP o como texto en un mensaje de WhatsApp es una decisión de
`runtime`, no de estos modelos.

------------------------------------------------------------------------------------------
La decisión de diseño que este módulo hace cumplir
------------------------------------------------------------------------------------------

`LecturaArchivo.tratamiento` es un `Literal` cerrado y no un `str`. Eso no es una
preferencia de tipado: es el mecanismo que separa lo que ve el doctor de lo que ve el
paciente (bloque 2 del plan). Si fuera `str`, `lector_archivos` podría devolver
"extracción de cuatro cordales incluidos 18, 28, 38 y 48" y esa frase llegaría a `daniela`,
que le habla al paciente. Siendo un `Literal`, el único valor que Pydantic acepta es
`'cordales'`: no hay forma de que quepa una frase clínica en ese campo.

`contexto_clinico` es el único campo de texto libre del modelo, y su destino es
exclusivamente Telegram. El reparto lo hace el código de la capa de ingesta, no el modelo.

`SolicitudCita` no tiene campo de teléfono ni de documento de identidad, y las dos
ausencias son decisiones del plan, no descuidos. Ver los comentarios de esa clase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `calendario` no importa `contratos`, así que esto no cierra ningún ciclo. Se importa en vez
# de volver a escribir `timezone(timedelta(hours=-5))` aquí: dos definiciones de la misma
# zona son dos sitios donde arreglar el día que Colombia adopte horario de verano.
from .calendario import ZONA_BOGOTA, Jornada

# `sin_resolver` es lógica pura --ni una conexión, ni un import del paquete-- así que esto
# tampoco cierra ningún ciclo.
from .sin_resolver import Senal

# ---------------------------------------------------------------------------------------
# Vocabularios cerrados
# ---------------------------------------------------------------------------------------

#: Los tratamientos de la base de conocimiento de MaxiCare, más `no_identificado` para
#: cuando un archivo no dice a cuál se refiere. `endodoncia` y `protesis` están aquí porque
#: los pacientes preguntan por ellos, pero la sección 2.12 del documento maestro dice que no
#: tienen precio documentado: `consultar_base_conocimiento` devuelve "SIN DATO DOCUMENTADO"
#: para los dos, y el guardrail `sin_cifra_no_documentada` bloquea cualquier cifra que
#: aparezca en una respuesta sin respaldo de esa tool.
#:
#: Este literal tiene que mantenerse sincronizado con la base de conocimiento que la clínica
#: edita desde la interfaz web. Es el costo declarado de haber elegido un `Literal` sobre un
#: `str`, y su condición de revisión está escrita en `contratos[LecturaArchivo]` del plan.
Tratamiento = Literal[
    "cordales",
    "implantes",
    "blanqueamiento",
    "ortodoncia",
    "diseno_sonrisa",
    "microdiseno",
    "coronas",
    "periodoncia",
    "gingivectomia",
    "bichectomia",
    "limpieza",
    "endodoncia",
    "protesis",
    "no_identificado",
]

# ---------------------------------------------------------------------------------------
# El vocabulario vivo
# ---------------------------------------------------------------------------------------
#
# `Tratamiento` (arriba) hace dos trabajos distintos, y solo uno es de seguridad:
#
#   - En `LecturaArchivo.tratamiento` es EL MURO. Contenido clínico que cruza hacia el
#     paciente. Cerrado, escrito a mano, y no se abre desde ninguna pantalla.
#   - En `SolicitudCita` y en `registrar_estado_oportunidad` es vocabulario de negocio: qué
#     tratamientos ofrece la clínica hoy. Eso cambia sin que cambie nada de seguridad.
#
# Este conjunto es la segunda mitad. Arranca con los catorce del Literal --así las pruebas
# offline y los scripts no necesitan base de datos-- y `runtime.py` lo reemplaza al arrancar
# con los tratamientos activos de la tabla `tratamientos`.
#
# Por qué un módulo global y no una consulta: `contratos.py` NO puede importar la base de
# datos. Rompería las pruebas offline, los scripts, y la frontera que vigila
# `tests/test_estructura.py`.

_VOCABULARIO: frozenset[str] = frozenset(get_args(Tratamiento))


def vocabulario() -> frozenset[str]:
    """Los tratamientos que la clínica ofrece ahora mismo."""
    return _VOCABULARIO


def fijar_vocabulario(claves: Iterable[str]) -> None:
    """Reemplaza el vocabulario. Lo llama `runtime.py` al arrancar y cada vez que la pantalla
    crea o desactiva un tratamiento.

    `no_identificado` se añade siempre: no es un tratamiento que se ofrezca, es el valor que
    usa el sistema cuando no sabe de cuál se trata, y quitarlo rompería el registro de
    oportunidad de cualquier conversación que todavía no tenga claro qué busca el paciente.
    """
    global _VOCABULARIO
    limpias = {c.strip().lower() for c in claves if c and c.strip()}
    _VOCABULARIO = frozenset(limpias | {"no_identificado"})


TipoDocumento = Literal[
    "remision_externa",
    "radiografia",
    "foto_clinica",
    "examen",
    "cotizacion",
    "video",
    "otro",
]

Confianza = Literal["alta", "media", "baja"]

EstadoOportunidad = Literal[
    "explorando",
    "comparando",
    "con_barrera",
    "listo_para_agendar",
    "agendado",
    "post_atencion",
]

Barrera = Literal[
    "precio",
    "miedo",
    "tiempo",
    "desplazamiento",
    "confianza",
    "comparacion",
    "ninguna",
]

#: Las cuatro rutas de `fallos.escalamiento_humano` más `archivo_recibido`.
MotivoEscalamiento = Literal[
    "clinico",
    "excepcion_comercial",
    "agenda_llena",
    "archivo_recibido",
    "dato_faltante",
]

#: El mismo vocabulario que `MotivoEscalamiento`, más el caso "no hay que escalar", que solo
#: tiene sentido en la salida de `daniela` y nunca como entrada de `escalar_a_doctores`.
MotivoEscalamientoODeNinguno = Literal[
    "clinico",
    "excepcion_comercial",
    "agenda_llena",
    "archivo_recibido",
    "dato_faltante",
    "ninguno",
]


# ---------------------------------------------------------------------------------------
# Validación de forma compartida
# ---------------------------------------------------------------------------------------

#: Una corrida de 6 a 12 dígitos seguidos, con o sin puntos de miles. Es la forma de una
#: cédula colombiana, y `datos.quien_ve_que` del brief la prohíbe expresamente: "no se
#: registran cédulas ni documentos de identidad de ningún tipo".
_PARECE_DOCUMENTO = re.compile(r"\b\d{1,3}(?:[.\s]\d{3}){1,3}\b|\b\d{6,12}\b")


def _rechazar_documento_de_identidad(valor: str, campo: str) -> str:
    """Rechaza un valor que contenga algo con forma de documento de identidad.

    Es validación de FORMA, así que vive en el schema y no en un guardrail: no depende del
    estado de ningún sistema ni de quién esté hablando. La prohibición del brief deja de ser
    una regla que alguien debe recordar y pasa a ser un valor que el modelo no puede enviar.
    """
    if _PARECE_DOCUMENTO.search(valor):
        raise ValueError(
            f"{campo} contiene algo con forma de documento de identidad. MaxiCare no "
            "registra cédulas ni documentos de ningún tipo (prohibición expresa del "
            "cliente en datos.quien_ve_que del brief)."
        )
    return valor


#: Mismo control, con nombre público, para el código de las tools. `identificar_paciente`
#: recibe el nombre como argumento suelto --sin modelo de entrada, según el plan-- así que
#: no hay validador de Pydantic que lo cubra; sin esto, la única ruta por donde una cédula
#: podría entrar al sistema sería justamente la tool que pregunta quién eres.
rechazar_documento_de_identidad = _rechazar_documento_de_identidad


def redactar_documento_de_identidad(valor: str, marca: str = "[omitido]") -> str:
    """El mismo control de forma, pero para texto que NO se puede rechazar entero.

    `rechazar_...` sirve cuando el valor es un campo que el modelo envía y que puede
    devolverse con un error. No sirve para el texto que un PACIENTE escribió: ese texto ya
    existe, ya se le respondió, y tirarlo entero por un número dentro perdería la única
    señal que tenía (`sin_resolver.frase_para_el_informe`). Aquí se quita la parte que
    parece documento y se deja el resto.

    El patrón es el mismo y vive en un solo sitio a propósito: dos copias de esta regla se
    separan con el tiempo, y la que se quede corta es la que deja entrar la cédula.
    """
    return _PARECE_DOCUMENTO.sub(marca, valor)


# ---------------------------------------------------------------------------------------
# output_type de los agentes
# ---------------------------------------------------------------------------------------


class LecturaArchivo(BaseModel):
    """`output_type` de `lector_archivos`.

    Del mismo archivo salen dos mitades con dos destinatarios y umbrales opuestos de qué se
    puede decir. El código de la capa de ingesta las reparte:

        contexto_clinico  ────────────►  Telegram (con el archivo)
        todo lo demás     ────────────►  el contexto de `daniela`

    Ese reparto es el muro del diseño. No es un prompt ni un guardrail: es de dónde el
    código saca cada cosa.
    """

    tipo_documento: TipoDocumento = Field(
        description="Qué clase de archivo llegó.",
    )
    tratamiento: Tratamiento = Field(
        description=(
            "El tratamiento que el documento menciona, o 'no_identificado'. Literal cerrado "
            "a propósito: es el campo que cruza hacia el paciente y no puede admitir una "
            "frase clínica."
        ),
    )
    origen: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Clínica o profesional que emite el documento. Dato administrativo, no clínico: "
            "sirve para decir 'ya me llegó tu remisión', no para describir el caso."
        ),
    )
    fecha_documento: date | None = Field(
        default=None,
        description="Fecha del documento, si la trae.",
    )
    confianza: Confianza = Field(
        description=(
            "Qué tan explícito estaba el tratamiento en el documento. 'alta' solo si el "
            "documento lo dice con todas sus letras. Por debajo de 'alta', la capa de "
            "ingesta fuerza tratamiento='no_identificado' para que Daniela pregunte en vez "
            "de asumir."
        ),
    )
    contexto_clinico: str = Field(
        max_length=4000,
        description=(
            "DESTINO EXCLUSIVO: Telegram, para los doctores. Lo que el documento dice, "
            "citado con fidelidad, contenido clínico incluido. FORMATO: una ficha de seis "
            "líneas como mucho — primero 'tipo · especialidad · emisor · fecha' sin rótulo, "
            "y debajo solo las que el documento respalde, de estas: 'Motivo:', "
            "'Hallazgos:', 'Antecedentes:', 'Piden:', 'Ojo:'. Nunca un párrafo corrido, y "
            "nunca una línea para decir que algo no aplica. NO emitir hallazgos propios "
            "sobre una imagen: el doctor ya recibe la imagen y un hallazgo generado por "
            "máquina puede anclarle el criterio. El código nunca pasa este campo a `daniela` "
            "ni lo persiste en Neon."
        ),
    )

    @field_validator("origen")
    @classmethod
    def _origen_sin_documento(cls, v: str | None) -> str | None:
        return _rechazar_documento_de_identidad(v, "origen") if v else v


class LecturaNoClinica(BaseModel):
    """La mitad de `LecturaArchivo` que SÍ puede cruzar hacia el paciente.

    No tiene `contexto_clinico`, y esa ausencia es el muro. No es que el código se acuerde
    de no copiarlo: es que no hay dónde ponerlo. Si alguien añade el campo aquí, las
    pruebas de `tests/test_muro.py` caen.

    `extra="forbid"` es parte de la garantía: sin él, `LecturaNoClinica(**lectura.model_dump())`
    se tragaría el campo clínico en silencio como atributo extra.
    """

    model_config = ConfigDict(extra="forbid")

    tipo_documento: TipoDocumento
    tratamiento: Tratamiento
    origen: str | None = Field(default=None, max_length=200)
    fecha_documento: date | None = None
    confianza: Confianza


class RespuestaDaniela(BaseModel):
    """`output_type` de `daniela`.

    Cinco de los seis campos no son decoración: son lo que permite que el orquestador
    ramifique con un `if` en vez de con otra llamada de modelo, y que seis de las siete
    métricas de `observabilidad.metricas` se calculen con SQL sobre Neon en vez de
    interpretando prosa.

    Ningún campo de `datos.datos_sensibles` aparece aquí.
    """

    mensaje_al_paciente: str = Field(
        min_length=1,
        description="El texto que se envía por WhatsApp o por el chat web. Lo único que el paciente lee.",
    )
    estado_oportunidad: EstadoOportunidad = Field(
        description=(
            "Estado comercial y operativo que pide `trabajo.pasos` del brief. No es una "
            "temperatura de lead: es dónde está esta persona en su proceso."
        ),
    )
    barrera_detectada: Barrera = Field(
        description=(
            "La preocupación concreta detectada, si hay alguna. Permite no repetir un "
            "argumento que el paciente ya rechazó."
        ),
    )
    requiere_escalamiento: bool = Field(
        description="Es el `if` del orquestador que dispara la notificación a Telegram.",
    )
    motivo_escalamiento: MotivoEscalamientoODeNinguno = Field(
        description="Cuál de las rutas de `fallos.escalamiento_humano` aplica, o 'ninguno'.",
    )
    fuera_de_alcance: bool = Field(
        description=(
            "El paciente preguntó algo ajeno a MaxiCare. Mide el criterio de alcance "
            "temático sin necesidad de un guardrail que bloquee: la reconducción suave la "
            "hace el prompt, este campo solo la cuenta."
        ),
    )


# ---------------------------------------------------------------------------------------
# Modelos de entrada de tool
# ---------------------------------------------------------------------------------------


class SolicitudCita(BaseModel):
    """Entrada de `crear_cita` y `reprogramar_cita`.

    Dos ausencias deliberadas, las dos decisiones del plan:

    - **No hay campo de cédula ni de documento.** `datos.quien_ve_que` lo prohíbe
      expresamente. Un campo que no existe no se puede llenar por error.
    - **No hay campo de teléfono.** El teléfono viene del webhook de WhatsApp y vive en el
      contexto local; la tool lo lee de ahí. Si el modelo lo escribiera, podría escribir
      *otro*: el caso real es la hija agendando por su madre y el recordatorio yéndose al
      número equivocado.

    La duración del bloque tampoco está aquí: sale de la configuración operativa que la
    clínica ajusta desde la interfaz web (1 hora por defecto), no de lo que decida el modelo.
    """

    nombre_completo: str = Field(
        min_length=2,
        max_length=200,
        description="Nombre completo del paciente, tal como lo dictó en la conversación.",
    )
    inicio: datetime = Field(
        description=(
            "Inicio del bloque. La duración la pone la configuración operativa, no el modelo."
        ),
    )
    tratamiento: str = Field(
        description=(
            "El tratamiento para el que se agenda. Tiene que ser uno de los que la clínica "
            "ofrece hoy; la lista viva va al final de las instrucciones del agente."
        ),
    )
    motivo: str = Field(
        default="",
        max_length=300,
        description=(
            "Una frase corta de por qué agenda, con las palabras del paciente. La lee el "
            "doctor en su calendario, no el paciente. Descriptiva, NUNCA clínica: 'quiere "
            "valoración para diseño de sonrisa, no le gustan sus dientes de adelante' sí; "
            "un diagnóstico o un tratamiento que nadie ha determinado, no. Si no lo tienes "
            "claro, déjalo vacío: el servicio ya va aparte."
        ),
    )
    clave_idempotencia: str = Field(
        min_length=8,
        max_length=200,
        description=(
            "Un identificador corto de este intento. NO decide nada: la clave real con la que "
            "se reserva el cupo la arma el orquestador a partir de la conversación y del "
            "horario pedido, porque una clave que el modelo pudiera inventar no protegería "
            "nada -- dos pacientes pidiendo el mismo bloque generarían la misma cadena."
        ),
    )

    @field_validator("nombre_completo")
    @classmethod
    def _nombre_sin_documento(cls, v: str) -> str:
        return _rechazar_documento_de_identidad(v, "nombre_completo")

    @field_validator("motivo")
    @classmethod
    def _motivo_sin_documento(cls, v: str) -> str:
        # Texto libre que el modelo escribe y que acaba GUARDADO en el calendario de la
        # clínica. Es justo la forma de la prohibición: «no se registran cédulas ni
        # documentos de identidad de ningún tipo». Que el paciente dicte su cédula en la
        # conversación no puede convertirse en una cédula apuntada en un evento.
        return _rechazar_documento_de_identidad(v, "motivo")

    @field_validator("tratamiento")
    @classmethod
    def _tratamiento_del_vocabulario(cls, v: str) -> str:
        # Se valida contra la lista viva, no contra el Literal: la clínica agrega
        # tratamientos desde la interfaz web sin que nadie despliegue código. El muro sigue
        # siendo el Literal, y vive en `LecturaArchivo`, no aquí.
        clave = v.strip().lower()
        if clave not in vocabulario():
            raise ValueError(
                f"'{v}' no es un tratamiento que MaxiCare ofrezca. "
                f"Los actuales son: {', '.join(sorted(vocabulario()))}."
            )
        return clave


class SolicitudCancelacion(BaseModel):
    """Entrada de `cancelar_cita`.

    Que la cita pertenezca al paciente identificado es una regla de negocio contra el estado
    de Neon: vive en el código de la tool (`donde_vive_validacion: "codigo_tool"`), no aquí.
    """

    id_cita: str = Field(
        min_length=1,
        max_length=200,
        description="Identificador de la cita en Neon.",
    )
    motivo: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "Lo que dijo el paciente, si lo dijo. Alimenta la métrica de recuperaciones."
        ),
    )
    clave_idempotencia: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "id_cita. No decide nada: cancelar es idempotente por sí solo, porque cancelar "
            "dos veces deja la cita cancelada igual que cancelarla una vez."
        ),
    )


class SolicitudEscalamiento(BaseModel):
    """Entrada de `escalar_a_doctores`.

    Los dos campos de texto son obligatorios y no vacíos a propósito:
    `fallos.escalamiento_humano` exige que cada escalamiento lleve contexto suficiente y una
    pregunta concreta. Sin `pregunta_concreta`, el doctor recibe un aviso y no una pregunta,
    y no sabe qué se le está pidiendo decidir.
    """

    motivo: MotivoEscalamiento = Field(
        description="Cuál de las rutas de escalamiento aplica. 'ninguno' no es válido aquí.",
    )
    resumen_para_doctor: str = Field(
        min_length=1,
        max_length=4000,
        description="Contexto suficiente para que el doctor no tenga que empezar de cero.",
    )
    pregunta_concreta: str = Field(
        min_length=1,
        max_length=1000,
        description="Qué se le está pidiendo decidir, en una frase.",
    )
    clave_idempotencia: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Un identificador corto de este escalamiento. NO decide nada: la clave real la "
            "arma el orquestador con id_conversacion + turno_actual. El mismo turno solo "
            "puede escalar una vez, para que el doctor no reciba la misma alerta tres veces "
            "tras un reintento de red."
        ),
    )


# ==========================================================================================
# El contexto local -- lo único de este módulo que NO es un BaseModel
# ==========================================================================================


@dataclass
class DatosDelTurno:
    """Lo que las tools autorizaron a decir EN ESTE TURNO. Se vacía en cada mensaje.

    Es la memoria sobre la que se apoyan `sin_cifra_no_documentada` y
    `sin_hora_no_verificada`: cada tool de lectura anota aquí lo que devolvió, y el
    guardrail exige que toda cifra o toda hora del mensaje al paciente esté en estos
    conjuntos.

    ------------------------------------------------------------------------------------
    Por qué se vacía cada turno y no se acumula
    ------------------------------------------------------------------------------------

    Porque si no, un precio consultado hace diez mensajes seguiría autorizando esa cifra
    hoy. Y el caso que importa es justo ese: el paciente pregunta por implantes, Daniela
    consulta y responde bien; tres mensajes después pregunta por coronas y Daniela repite
    la cifra de implantes «de memoria». Con el conjunto acumulado, el guardrail la deja
    pasar porque la vio antes. Vaciándolo, no hay forma: o la tool la devolvió en este
    turno, o no sale.

    `hubo_adjunto` y `menciona_sintomas` son el prefiltro determinista de
    `sin_lectura_clinica`: sin ellos, ese guardrail llamaría a un modelo evaluador en cada
    mensaje --incluido «¿tienen parqueadero?»-- y duplicaría el costo de la conversación
    para no encontrar nada.
    """

    cifras_autorizadas: set[str] = field(default_factory=set)
    horas_autorizadas: set[str] = field(default_factory=set)
    hubo_adjunto: bool = False
    menciona_sintomas: bool = False
    #: Lo que se consultó a la base de conocimiento en este turno, con dato o sin él. De aquí
    #: salen los casos `FALTA_DATO`, y el tratamiento con el que se enriquece la huella de un
    #: guardrail. Mismo ciclo de vida que `cifras_autorizadas`: se vacía cada turno, porque
    #: un hueco de hace diez mensajes no es un hueco de hoy.
    #:
    #: Es lo único de esta clase que nadie lee DURANTE el turno: se vuelca al final, cuando
    #: el paciente ya tiene su respuesta. Ver `atencion._anotar_resultado`.
    senales: list[Senal] = field(default_factory=list)

    def reiniciar(self) -> None:
        """Lo llama el orquestador al empezar cada turno, antes de `Runner.run`."""
        self.cifras_autorizadas = set()
        self.horas_autorizadas = set()
        self.hubo_adjunto = False
        self.menciona_sintomas = False
        self.senales = []


@dataclass
class ContextoDaniela:
    """Lo que viaja en `Runner.run(context=...)` y llega a tools, guardrails y hooks.

    Es un `dataclass` y no un `BaseModel` por una razón que importa: **esto no se serializa
    nunca al modelo**. El SDK lo pasa tal cual, envuelto en `RunContextWrapper`, y el LLM no
    lo ve. Que sea un `BaseModel` invitaría a mandarlo como `output_type` o a volcarlo en un
    prompt "para dar contexto", y ahí dentro van el teléfono completo y las credenciales.

    Lo que el modelo sí puede ver está en `contexto.visible_al_llm` del plan: el nombre de
    pila, si está identificado o no, y los resultados de las tools. Nada de esto.

    ------------------------------------------------------------------------------------
    Dos ausencias deliberadas
    ------------------------------------------------------------------------------------

    1. **No hay `contexto_clinico`.** La lectura clínica de un archivo viaja a Telegram
       desde la capa de ingesta y muere ahí, antes de que `Runner.run` arranque. Si
       estuviera aquí, una tool podría leerla y ponerla en un mensaje al paciente.
    2. **No hay cédula ni documento de identidad.** Prohibición expresa de MaxiCare. La
       ausencia ES la política, igual que en la tabla `pacientes`.

    `calendario` viaja en el contexto --y no se importa dentro de las tools-- para que la
    misma tool corra contra `CalendarioDoble` en pruebas y contra Google en producción sin
    un solo `if` que distinga los dos casos.
    """

    id_conversacion: str
    telefono_completo: str
    database_url: str
    calendario: Any

    #: Identidad. `id_paciente` solo tiene valor cuando `identidad_verificada` es True.
    id_paciente: int | None = None
    nombre_paciente: str | None = None
    identidad_verificada: bool = False
    intentos_identificacion: int = 0

    #: Comprobado CONTRA LA BASE: este teléfono no tiene fila en `pacientes`. No es lo mismo
    #: que `not identidad_verificada` --ese es «no sabemos quién es»; este es «la clínica no
    #: lo conoce»-- y la diferencia decide si puede pedir su primera cita.
    #:
    #: Lo pone `atencion._leer_estado` con el `buscar_paciente_por_telefono` que ya hacía, y
    #: lo refresca `herramientas._identificar_paciente`. Nunca lo escribe el modelo: si
    #: pudiera, bastaría con que dijera «soy nuevo» para saltarse `identidad_antes_de_datos`.
    telefono_sin_paciente: bool = False

    #: Conversación.
    turno_actual: int = 0

    #: El instante en que arranca este turno, en hora de Bogotá.
    #:
    #: Viaja en el contexto --y no se calcula con `datetime.now()` dentro de cada tool-- por
    #: la misma razón que `calendario`: una prueba lo fija y el comportamiento deja de
    #: depender del reloj de la máquina. De aquí salen las dos cosas que el 13/09/2026
    #: estaban rotas: que Daniela sepa en qué año vive, y que no ofrezca una hora que ya pasó.
    ahora: datetime = field(default_factory=lambda: datetime.now(ZONA_BOGOTA))

    #: Relevo. Con `tomada_por` distinto de None, Daniela calla y no programa nada.
    tomada_por: str | None = None
    topic_id: int | None = None

    #: Configuración operativa leída de Neon, con los defaults de `persistencia` detrás.
    capacidad_por_hora: int = 2
    duracion_cita_minutos: int = 60
    cierre_relevo_minutos: int = 180

    #: Las dos perillas de los recordatorios (migración 017), leídas de `configuracion`.
    #: `crear_cita` y `reprogramar_cita` se las pasan a `seguimientos.momento_del_recordatorio`
    #: en vez de dejar que esa función use sus propios defaults: así la clínica cambia la hora
    #: de la víspera desde el panel y las tools obedecen sin tocar código.
    hora_recordatorio_vispera: int = 18
    horas_minimas_para_recordar: int = 4

    #: El último recordatorio que le salió a este paciente, si lo hubo. Lo mandó el
    #: despachador y NO la conversación, así que el historial del agente no lo contiene: sin
    #: esto, un paciente que responde «sí, confirmo» le está diciendo que sí a algo que Daniela
    #: no sabe que se dijo. Sale de la base y nunca del modelo, igual que
    #: `telefono_sin_paciente`.
    ultimo_recordatorio_tipo: str | None = None
    ultimo_recordatorio_en: datetime | None = None

    #: El horario en que la clínica atiende. Lo que impide ofrecer —y agendar— la madrugada.
    #: Viaja en el contexto por lo mismo que `calendario` y `ahora`: una prueba lo fija. Ver
    #: `calendario.Jornada`, que explica el fallo de producción que lo trajo.
    jornada: Jornada = field(default_factory=Jornada)

    #: Tema General del grupo de Telegram. `0` significa «General, omitiendo
    #: message_thread_id» -- ver la migración 005 y el comentario de `canales.py`.
    tema_general: int = 0

    #: Por dónde llegó este mensaje: `whatsapp` (producción) o `web` (el chat del panel).
    #: Va al `trace_metadata` para poder separar en el dashboard las conversaciones reales de
    #: las pruebas de la clínica, que corren contra el MISMO agente a propósito.
    canal: str = "whatsapp"

    #: Credenciales de Telegram, que `escalar_a_doctores` necesita para avisar. Viven en el
    #: contexto y no en un import de `config` dentro de la tool: así una prueba las
    #: sustituye por un doble sin tocar variables de entorno del proceso.
    telegram_bot_token: str = ""
    telegram_chat_doctores: str = ""

    #: Lo que las tools autorizaron en el turno en curso. Los guardrails de salida lo leen;
    #: el orquestador lo reinicia antes de cada `Runner.run`.
    turno: DatosDelTurno = field(default_factory=DatosDelTurno)

    def clave(self, *partes: object) -> str:
        """Construye una clave de idempotencia anclada a esta conversación.

        Vive aquí, y no en cada tool, porque el plan dice que las claves las arma el
        orquestador: una clave que el modelo pudiera inventar dejaría de proteger nada.
        """
        return ":".join([self.id_conversacion, *(str(p) for p in partes)])

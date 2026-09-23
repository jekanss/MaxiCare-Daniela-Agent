"""Las consultas y escrituras del panel web.

No va en `persistencia.py` porque ese módulo ya tiene 736 líneas y es el que usan las nueve
tools de Daniela; lo del panel es otra responsabilidad y cambia por otras razones.

Como `persistencia.py`, este módulo NO importa `runtime.py` ni un framework web: recibe una
conexión y devuelve diccionarios. Lo vigila `tests/test_estructura.py`.

La regla que atraviesa todo el archivo: **ningún cambio se escribe sin su registro en la
bitácora, y los dos van en la misma transacción.** Editar un precio aquí cambia lo que
Daniela le cotiza a un paciente real al instante, sin despliegue y sin vuelta atrás; la
bitácora es lo único que permite reconstruir qué decía antes y quién lo cambió.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, get_args

from . import persistencia
from .calendario import ZONA_BOGOTA
from .contratos import Tratamiento

#: La misma regla que el CHECK de `migraciones/008_tratamientos.sql`. Está duplicada a
#: propósito: Postgres es la garantía, y esta copia existe solo para dar un mensaje legible
#: antes de que la base rechace la fila con un error que nadie entiende.
CLAVE = re.compile(r"^[a-z][a-z0-9_]{2,23}$")

#: Lo que hay que decirle a una persona cuando su clave no pasa. Vive aquí, junto al regex
#: que la impone, porque hay DOS sitios que rechazan una clave mala: este validador y el
#: `Field(min_length=3, max_length=24)` de `runtime.Ficha`, que corre antes y ni siquiera
#: llega hasta aquí. Si cada uno redacta su propia frase, teclear `ca` da un mensaje y
#: teclear `Carillas` da otro, para el mismo error de la misma persona.
AYUDA_CLAVE = (
    "Usa minúsculas sin espacios ni tildes, de 3 a 24 caracteres; por ejemplo 'carillas' "
    "o 'carillas_esteticas'."
)

#: Los conceptos que hacen útil una ficha. Si faltan, la pantalla lo muestra.
CONCEPTOS_MINIMOS = ("precio", "duracion", "profesional")

#: Los hechos de la clínica --horario, sede, EPS, medios de pago, urgencias-- viven en
#: `base_conocimiento` bajo este tratamiento, que NO es una fila de `tratamientos`: nadie
#: agenda una cita de «_general».
#:
#: El frontend tiene su propia copia (`web/src/pantallas/Tratamientos.tsx`, `const GENERAL`)
#: y la usa para llevarse estas fichas a la pestaña «La clínica». La copia es deliberada
#: --el `.tsx` no puede importar de Python-- y por eso la constante se declara aquí, en el
#: módulo que decide qué se acepta: `guardar_ficha` valida contra la tabla, y sin esta
#: excepción explícita rechazaría las 12 fichas de esa pestaña por no existir el
#: «tratamiento» `_general`. Si algún día cambia el nombre, hay que cambiarlo en los dos.
GENERAL = "_general"


def validar_clave(clave: str) -> str:
    """La clave de un tratamiento, o un error que se puede leer.

    Es lo que termina dentro de `citas.tratamiento` y en los argumentos que ve el modelo,
    así que no admite espacios, mayúsculas ni frases: minúsculas, números y guion bajo, de 3
    a 24 caracteres.

    Solo se recorta el espacio en blanco alrededor -- un descuido de formulario, no un dato
    distinto--. NO se pasa a minúsculas: hacerlo silenciaría exactamente el error que este
    validador existe para atrapar antes de que llegue al CHECK de Postgres, que tampoco
    normaliza. 'Carillas' no es 'carillas': es la clínica escribiendo con el hábito de una
    frase, y ese hábito es la puerta de entrada de un texto clínico.
    """
    limpia = (clave or "").strip()
    if not CLAVE.match(limpia):
        raise ValueError(f"'{clave}' no sirve como clave. {AYUDA_CLAVE}")
    return limpia


def _anotar(cur, *, tabla: str, clave: str, anterior: str | None, nuevo: str, usuario: str) -> None:
    """Escribe la bitácora. Se llama SIEMPRE dentro de la misma transacción que el cambio."""
    cur.execute(
        "INSERT INTO cambios_configuracion (tabla, clave, valor_anterior, valor_nuevo, usuario) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tabla, clave, anterior, nuevo, usuario),
    )


# ------------------------------------------------------------------------------------------
# Tratamientos
# ------------------------------------------------------------------------------------------


def listar_tratamientos(conn) -> list[dict[str, Any]]:
    """Cada tratamiento con lo que la pantalla necesita para decir la verdad sobre él.

    `en_el_muro` se CALCULA del `Literal` real, no se guarda en una columna. Una columna
    sería una segunda fuente de verdad que se desincroniza en silencio el día que alguien
    edite una y no la otra.
    """
    del_muro = set(get_args(Tratamiento))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.clave, t.etiqueta, t.activo, "
            "       coalesce(k.n, 0) AS fichas, "
            "       coalesce(k.conceptos, ARRAY[]::text[]) AS conceptos "
            "  FROM tratamientos t "
            "  LEFT JOIN (SELECT tratamiento, count(*) AS n, "
            "                    array_agg(concepto) AS conceptos "
            "               FROM base_conocimiento GROUP BY tratamiento) k "
            "    ON k.tratamiento = t.clave "
            " ORDER BY t.activo DESC, t.clave"
        )
        filas = cur.fetchall()

    salida = []
    for clave, etiqueta, activo, fichas, conceptos in filas:
        tiene = set(conceptos or [])
        salida.append({
            "clave": clave,
            "etiqueta": etiqueta,
            "activo": activo,
            "en_el_muro": clave in del_muro,
            "fichas": fichas,
            "faltan": [c for c in CONCEPTOS_MINIMOS if c not in tiene],
        })
    return salida


def vocabulario_activo(conn) -> list[str]:
    """Lo que `contratos.fijar_vocabulario` necesita. Solo los activos."""
    with conn.cursor() as cur:
        cur.execute("SELECT clave FROM tratamientos WHERE activo ORDER BY clave")
        return [f[0] for f in cur.fetchall()]


def crear_tratamiento(conn, *, clave: str, etiqueta: str, usuario: str) -> dict[str, Any]:
    """Un tratamiento nuevo, cotizable y agendable desde ya.

    Lo que NO hace, y por eso la pantalla lo avisa: no lo mete en el `Literal` de
    `LecturaArchivo`. Una radiografía sobre este tratamiento se seguirá clasificando como
    `no_identificado` hasta que alguien lo incorpore al muro con un cambio de código.
    """
    limpia = validar_clave(clave)
    nombre = (etiqueta or "").strip()
    if not nombre:
        raise ValueError("El tratamiento necesita un nombre visible.")

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tratamientos WHERE clave = %s", (limpia,))
        if cur.fetchone():
            raise ValueError(f"'{limpia}' ya existe.")
        cur.execute(
            "INSERT INTO tratamientos (clave, etiqueta, creado_por) VALUES (%s, %s, %s)",
            (limpia, nombre, usuario),
        )
        _anotar(cur, tabla="tratamientos", clave=limpia, anterior=None,
                nuevo=f"creado: {nombre}", usuario=usuario)
    conn.commit()
    return {"clave": limpia, "etiqueta": nombre, "activo": True,
            "en_el_muro": limpia in set(get_args(Tratamiento)), "fichas": 0,
            "faltan": list(CONCEPTOS_MINIMOS)}


def cambiar_tratamiento(
    conn, clave: str, *, etiqueta: str | None = None, activo: bool | None = None, usuario: str
) -> dict[str, Any]:
    """Renombra o desactiva. Nunca borra.

    Borrar dejaría las citas históricas apuntando a una clave que ya no existe. Desactivado:
    Daniela no lo ofrece ni lo agenda, y lo que ya se agendó sigue legible.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT etiqueta, activo FROM tratamientos WHERE clave = %s", (clave,))
        fila = cur.fetchone()
        if fila is None:
            raise ValueError(f"'{clave}' no existe.")
        antes_etiqueta, antes_activo = fila

        if etiqueta is not None and etiqueta.strip() and etiqueta.strip() != antes_etiqueta:
            cur.execute(
                "UPDATE tratamientos SET etiqueta = %s WHERE clave = %s",
                (etiqueta.strip(), clave),
            )
            _anotar(cur, tabla="tratamientos", clave=clave, anterior=antes_etiqueta,
                    nuevo=etiqueta.strip(), usuario=usuario)

        if activo is not None and activo != antes_activo:
            cur.execute("UPDATE tratamientos SET activo = %s WHERE clave = %s", (activo, clave))
            _anotar(cur, tabla="tratamientos", clave=clave,
                    anterior="activo" if antes_activo else "inactivo",
                    nuevo="activo" if activo else "inactivo", usuario=usuario)
    conn.commit()
    return next(f for f in listar_tratamientos(conn) if f["clave"] == clave)


# ------------------------------------------------------------------------------------------
# Fichas de conocimiento
# ------------------------------------------------------------------------------------------


def listar_conocimiento(conn) -> list[dict[str, Any]]:
    """Las 73 fichas, crudas. El formateo que ve el modelo vive en
    `persistencia.formatear_conocimiento` y no se duplica aquí."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tratamiento, concepto, contenido, aprobado, nota_pendiente, actualizado_en "
            "FROM base_conocimiento ORDER BY tratamiento, concepto"
        )
        return [
            {"tratamiento": t, "concepto": c, "contenido": k, "aprobado": a,
             "nota_pendiente": n, "actualizado_en": u.isoformat()}
            for t, c, k, a, n, u in cur.fetchall()
        ]


def guardar_ficha(
    conn, *, tratamiento: str, concepto: str, contenido: str, aprobado: bool,
    nota_pendiente: str | None, usuario: str,
) -> dict[str, Any]:
    """Crea o edita una ficha, con su registro en la bitácora, en una sola transacción.

    `aprobado=False` NO esconde la ficha: `formatear_conocimiento` igual se la entrega al
    modelo, precedida de la advertencia de aprobación. El interruptor significa «esto
    todavía no es un compromiso comercial», no «esto no se ve».

    `nota_pendiente` se borra al aprobar. La nota dice qué falta por definir; una ficha
    aprobada ya no tiene nada por definir, y la pantalla esconde el campo al aprobar pero
    sigue mandando lo que hubiera escrito. Guardarlo dejaría un «falta confirmarlo con la
    doctora» invisible, listo para reaparecer el día que alguien desapruebe la ficha meses
    después y se encuentre una advertencia que ya nadie sabe de dónde salió. Se decide aquí
    --no en la pantalla-- porque aquí no se puede esquivar.
    """
    trat = (tratamiento or "").strip().lower()
    conc = (concepto or "").strip().lower()
    texto = (contenido or "").strip()
    if not trat or not conc:
        raise ValueError("Hacen falta el tratamiento y el concepto.")
    if not texto:
        raise ValueError(
            "Una ficha vacía no es lo mismo que una ficha sin datos. Si MaxiCare no tiene "
            "esa información, deja la ficha sin crear: Daniela dirá «SIN DATO DOCUMENTADO» "
            "y escalará, que es el comportamiento correcto."
        )
    if aprobado:
        nota_pendiente = None

    with conn.cursor() as cur:
        # Contra TODAS las filas, no solo las activas: editar la ficha de un tratamiento que
        # la clínica dejó de ofrecer es legítimo --el precio viejo sigue siendo el precio
        # viejo-- y desactivar uno no puede volver sus fichas irreparables.
        #
        # Sin esto, `base_conocimiento.tratamiento` era la única escritura del panel sin
        # control de vocabulario: un TEXT de 60 caracteres, sin clave foránea y sin CHECK,
        # donde cabe una frase clínica entera. La fila resultante no se ve en ninguna
        # pantalla, cuenta en la cabecera y no se puede borrar desde el producto.
        cur.execute(
            "SELECT 1 FROM tratamientos WHERE clave = %s", (trat,)
        )
        if trat != GENERAL and cur.fetchone() is None:
            raise ValueError(
                f"'{trat}' no es un tratamiento de MaxiCare. Una ficha se cuelga de un "
                "tratamiento que ya existe en la tabla; si hace falta uno nuevo, créalo "
                "primero desde «Nuevo tratamiento»."
            )

        cur.execute(
            "SELECT contenido, aprobado FROM base_conocimiento "
            "WHERE tratamiento = %s AND concepto = %s",
            (trat, conc),
        )
        fila = cur.fetchone()
        anterior = fila[0] if fila else None
        antes_aprobado = fila[1] if fila else None

        cur.execute(
            "INSERT INTO base_conocimiento (tratamiento, concepto, contenido, aprobado, "
            "                               nota_pendiente, actualizado_en) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (tratamiento, concepto) DO UPDATE SET "
            "  contenido = EXCLUDED.contenido, aprobado = EXCLUDED.aprobado, "
            "  nota_pendiente = EXCLUDED.nota_pendiente, actualizado_en = now()",
            (trat, conc, texto, aprobado, nota_pendiente),
        )
        # `aprobado` es el único campo del sistema con significado comercial declarado: la
        # pantalla lo escribe como «Es un compromiso comercial» / «Todavía no lo es». Sin
        # este registro, convertir un precio tentativo en un compromiso no dejaba rastro, y
        # el límite de concurrencia que esta rama aceptó --dos ediciones simultáneas, gana
        # la última-- se aceptó *porque* la bitácora guarda el valor anterior. Era cierto
        # del texto y falso de la aprobación.
        #
        # Fila propia, con el mismo vocabulario que el de `activo` en `cambiar_tratamiento`:
        # dos booleanos de la misma pantalla, escritos por el mismo módulo, no pueden
        # registrarse de dos maneras distintas en la misma tabla. Va ANTES que la del
        # contenido para que la del contenido siga siendo la más reciente de las dos: es la
        # que la bitácora enseña arriba y la que responde «qué decía antes».
        if antes_aprobado is not None and aprobado != antes_aprobado:
            _anotar(cur, tabla="base_conocimiento", clave=f"{trat}/{conc}",
                    anterior="aprobado" if antes_aprobado else "sin aprobar",
                    nuevo="aprobado" if aprobado else "sin aprobar", usuario=usuario)

        _anotar(cur, tabla="base_conocimiento", clave=f"{trat}/{conc}",
                anterior=anterior, nuevo=texto, usuario=usuario)
    conn.commit()
    return {"tratamiento": trat, "concepto": conc, "contenido": texto,
            "aprobado": aprobado, "nota_pendiente": nota_pendiente}


def historial(
    conn, limite: int = 100, *, excluir_tablas: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Lo más reciente primero. Sin paginación: cien cambios cubren meses de esta clínica.

    El desempate por `id` no es adorno. Un solo cambio de la pantalla puede escribir DOS
    filas en la misma transacción --contenido y aprobación, o nombre y activo--, y
    `cambiado_en` es `now()`, que en Postgres es el instante de la TRANSACCIÓN: las dos
    filas llevan exactamente la misma marca de tiempo. Ordenando solo por ella, cuál sale
    arriba lo decide el planificador, y la bitácora contaría la historia al revés de vez en
    cuando.

    **`excluir_tablas` no cambia el significado de esta función: sigue siendo «la bitácora
    entera, lo más reciente primero».** Es un recorte que pide QUIEN llama, y por eso su
    default es no recortar nada: el filtro vive en el sitio que sabe para qué pantalla es.
    Hoy lo usa `runtime.api_historial` y el porqué está allí. Las filas excluidas siguen en
    la tabla --la bitácora es la pista de auditoría de quién marcó qué y cuándo, y eso no se
    toca--: lo único que cambia es cuáles se le enseñan a esa pantalla.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tabla, clave, valor_anterior, valor_nuevo, usuario, cambiado_en "
            "FROM cambios_configuracion WHERE NOT (tabla = ANY(%s)) "
            "ORDER BY cambiado_en DESC, id DESC LIMIT %s",
            (list(excluir_tablas), max(1, min(limite, 500))),
        )
        return [
            {"tabla": t, "clave": c, "valor_anterior": a, "valor_nuevo": n,
             "usuario": u, "cambiado_en": f.isoformat()}
            for t, c, a, n, u, f in cur.fetchall()
        ]


# ------------------------------------------------------------------------------------------
# La agenda del día y la marca de asistencia
# ------------------------------------------------------------------------------------------
#
# `citas.asistio` existe desde la migración 001 y hasta la fase 8 no la escribía NADIE. Es la
# única señal que distingue una cita cumplida de una que el paciente no atendió, y sin ella
# la clínica no puede saber si Daniela agenda citas a las que la gente va -- que es el
# criterio de éxito del proyecto, y no el número de citas creadas.


#: Las columnas de una cita tal como las lee la agenda. La lista importa: `evento_calendar_id`
#: y `reserva_id` no se pintan en ninguna pantalla, pero `herramientas.reconciliar_con_calendar`
#: los necesita -- el primero para preguntarle a Google por esa cita, el segundo para soltar
#: el cupo viejo cuando la movieron--. Es la misma forma que devuelve
#: `persistencia.citas_activas_de_telefono`, más `asistio`, que es lo que añade esta fase.
_COLUMNAS_CITA = (
    "id", "conversacion_id", "telefono", "nombre_completo", "tratamiento", "inicio",
    "duracion_minutos", "estado", "asistio", "evento_calendar_id", "reserva_id",
)

_SELECT_CITA = (
    "SELECT id, conversacion_id, telefono, nombre_completo, tratamiento, inicio, "
    "       duracion_minutos, estado, asistio, evento_calendar_id, reserva_id "
    "  FROM citas "
)

#: Los tres estados de la marca, dichos para la bitácora. `_anotar` no admite un `nuevo`
#: nulo --la columna es NOT NULL-- así que desmarcar no puede escribirse como un NULL: sería
#: indistinguible de «no se registró nada». `sin_marcar` es un valor y se lee como tal.
MARCA: dict[bool | None, str] = {True: "asistio", False: "no_asistio", None: "sin_marcar"}


class CitaInexistente(ValueError):
    """Ese id no corresponde a ninguna cita.

    Hereda de `ValueError` para no romper a quien ya captura `ValueError` --y porque lo es--,
    y existe aparte para que `runtime.py` pueda responder 404 en vez de 400 sin leerle el
    mensaje al error. `panel.py` no conoce códigos HTTP y no puede conocerlos: la frontera de
    capas lo prohíbe (`.claude/rules/frontera-agentes.md`).
    """


def _fila_a_cita(fila) -> dict[str, Any]:
    """Los ids en TEXTO, no como `uuid.UUID`.

    Es lo que espera todo lo que ya existe: `Correccion.cita_id` está declarado `str`, las
    claves de idempotencia de `_mover_porque_la_movieron` se arman interpolando `cita['id']`,
    y las escrituras de `persistencia` reciben el id como cadena desde la fase 3.
    """
    cita = dict(zip(_COLUMNAS_CITA, fila))
    cita["id"] = str(cita["id"])
    cita["conversacion_id"] = str(cita["conversacion_id"])
    return cita


def citas_del_dia(conn, *, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
    """Citas vivas de ese rango, con `asistio`, ordenadas por `inicio`.

    **No corta el pasado**, y esa es la diferencia con `persistencia.citas_activas_de_telefono`:
    la agenda existe justo para poder mirar hacia atrás y marcar quién asistió. Una cita
    cancelada tampoco sale -- para la clínica ya no ocupa la hora, y pintarla llenaría el día
    de bloques que nadie va a atender.
    """
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_CITA
            + " WHERE inicio >= %s AND inicio < %s"
              "   AND estado IN ('confirmada', 'reprogramada')"
              " ORDER BY inicio",
            (desde, hasta),
        )
        return [_fila_a_cita(f) for f in cur.fetchall()]


def marcar_asistencia(
    conn, *, cita_id: str, valor: bool | None, usuario: str
) -> dict[str, Any]:
    """Escribe `citas.asistio` y su fila de bitácora, en UNA transacción.

    Devuelve la cita ya actualizada, con la misma forma que una entrada de `citas_del_dia`:
    la pantalla repinta esa fila sin volver a pedir el día entero.

    Tres cosas se rechazan, y ninguna es un capricho:

    - **Un id que no existe** (`CitaInexistente`). Se comprueba antes que nada que la cadena
      sea siquiera un UUID: el id llega desde una URL, y una cadena cualquiera hace que
      Postgres aborte la transacción con `invalid input syntax for type uuid` -- un 500 que
      además deja la conexión inservible para lo que venga detrás.
    - **Una cita cancelada.** No hubo cita: no hay asistencia que registrar, y un «no asistió»
      sobre una cita que la clínica canceló le echa al paciente una falta que no cometió.
    - **Una cita que todavía no ha ocurrido.** Marcar el futuro es adivinar. La columna
      alimenta la única medida real del proyecto --si la gente llega-- y un dato inventado
      ahí la vuelve inútil sin que nadie lo note.

    Marcar dos veces lo mismo no escribe nada: una bitácora que anota cambios que no
    ocurrieron es tan inútil como la que se calla los que sí (el mismo criterio que
    `guardar_ficha` aplica al interruptor de aprobación).
    """
    id_limpio = str(cita_id or "")
    try:
        uuid.UUID(id_limpio)
    except (AttributeError, TypeError, ValueError):
        raise CitaInexistente("esa cita no existe") from None

    with conn.cursor() as cur:
        cur.execute(_SELECT_CITA + " WHERE id = %s", (id_limpio,))
        fila = cur.fetchone()
        if fila is None:
            raise CitaInexistente("esa cita no existe")
        cita = _fila_a_cita(fila)

        if cita["estado"] == "cancelada":
            raise ValueError("esa cita está cancelada: no hay asistencia que marcar")
        if cita["inicio"] > datetime.now(ZONA_BOGOTA):
            raise ValueError("esa cita todavía no ha ocurrido")

        anterior = cita["asistio"]
        if anterior is valor:
            return cita

        cur.execute(
            "UPDATE citas SET asistio = %s, actualizada_en = now() WHERE id = %s",
            (valor, id_limpio),
        )
        _anotar(cur, tabla="citas", clave=id_limpio, anterior=MARCA[anterior],
                nuevo=MARCA[valor], usuario=usuario)
    conn.commit()

    cita["asistio"] = valor
    return cita


def citas_sin_marcar(
    conn, *, desde: datetime, hasta: datetime, limite: int = 50
) -> list[dict[str, Any]]:
    """Las de ese rango cuya hora pasó y siguen con `asistio IS NULL`.

    De la más reciente a la más vieja, y eso decide qué se pierde cuando hay más de `limite`:
    lo que la clínica va a marcar es lo de ayer, no lo del mes pasado. Al revés, el tope
    devolvería las más antiguas y las de ayer no aparecerían nunca.

    `inicio < now()` va además del rango porque el rango puede incluir el día de hoy, donde
    hay citas que todavía no han ocurrido: esas no están «sin marcar», están sin ocurrir.
    """
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_CITA
            + " WHERE inicio >= %s AND inicio < %s AND inicio < now()"
              "   AND asistio IS NULL"
              "   AND estado IN ('confirmada', 'reprogramada')"
              " ORDER BY inicio DESC LIMIT %s",
            (desde, hasta, max(1, min(limite, 200))),
        )
        return [_fila_a_cita(f) for f in cur.fetchall()]


# ------------------------------------------------------------------------------------------
# La portada: los cinco números
# ------------------------------------------------------------------------------------------


#: Cuánto tiene que llevar un mensaje sin respuesta para contarlo como desatendido. Cinco
#: minutos: por debajo de eso lo más probable es que el turno siga vivo dentro de la ventana
#: de silencio del búfer (`atencion._Bufer`), y contarlo sería llamar «sin contestar» a un
#: mensaje que se está contestando ahora mismo.
#:
#: Va interpolado en el SQL y NO como parámetro porque Postgres no acepta un placeholder
#: dentro de un literal `interval`. Es una constante de este archivo, nunca una entrada: si
#: algún día viene de fuera, se pasa como `%s * interval '1 minute'`.
MINUTOS_SIN_CONTESTAR = 5

#: El marcador que deja el sistema cuando no sabe cómo se llama alguien. Vive en
#: `persistencia` y se alia aquí para no repetir el literal en dos módulos.
_NOMBRE_PENDIENTE = persistencia.NOMBRE_PENDIENTE


def resumen_inicio(conn, *, ahora: datetime, dias: int = 30) -> dict[str, Any]:
    """Los cinco números de la portada, más el volumen por día y la agenda de hoy.

    SOLO LECTURA, y esa es la diferencia que más importa con `/api/agenda`: aquella
    reconcilia contra Google Calendar al abrirse --puede mover una cita, soltar un cupo y
    reprogramar un recordatorio-- y se quiso así porque abrir la Agenda es el mejor
    disparador que esa reconciliación tiene. Esta pantalla es la PRIMERA de cada sesión: si
    reconciliara, cada ingreso al panel serían escrituras en Neon y llamadas a la API de
    Google, varias veces al día y por cada persona que entre.

    `ahora` no tiene default a propósito. Un `now()` dentro de la consulta convierte
    cualquier prueba en una que envejece, y en este proyecto eso ya ha amanecido en rojo
    tres veces (`.claude/rules/pruebas.md`). Quien llama decide qué instante es el presente.

    La unidad es el TELÉFONO y no la conversación. Una conversación caduca por inactividad
    de 24 h (`persistencia.conversacion_viva`), así que una negociación de tres días son
    tres filas y una sola persona: contar filas inflaría el denominador de todas las tasas.

    Devuelve las cifras CRUDAS, nunca porcentajes. Quién se divide entre quién lo decide la
    pantalla, y así el numerador y el denominador viajan los dos -- que es lo que permite
    escribir «llegaron 8 de 12» en vez de un 67 % que no dice sobre cuántas citas se calculó.
    """
    desde = ahora - timedelta(days=dias)
    inicio_del_dia = ahora.astimezone(ZONA_BOGOTA).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    with conn.cursor() as cur:
        # 1. Escribieron: personas distintas, no conversaciones.
        cur.execute(
            "SELECT count(DISTINCT telefono) FROM mensajes_entrantes WHERE recibido_en >= %s",
            (desde,),
        )
        escribieron = cur.fetchone()[0]

        # 2. Quedaron con cita: personas distintas con una cita viva creada en el periodo.
        cur.execute(
            "SELECT count(DISTINCT telefono) FROM citas"
            " WHERE creada_en >= %s AND estado <> 'cancelada'",
            (desde,),
        )
        con_cita = cur.fetchone()[0]

        # 3. Llegaron. Solo citas cuya hora YA pasó: una cita de mañana no es una
        #    inasistencia, y contarla haría bajar el número cada vez que Daniela agenda a
        #    alguien. Las tres cifras salen juntas porque `llegaron` sin `marcadas` miente
        #    en cuanto la recepción se retrasa una semana en marcar.
        cur.execute(
            "SELECT count(*) FILTER (WHERE asistio IS TRUE),"
            "       count(*) FILTER (WHERE asistio IS NOT NULL),"
            "       count(*)"
            "  FROM citas"
            " WHERE inicio >= %s AND inicio < %s AND estado <> 'cancelada'",
            (desde, ahora),
        )
        llegaron, marcadas, cumplibles = cur.fetchone()

        # 4. Sin contestar. El `NOT LIKE 'relevo:%%'` saca los mensajes que atendió un doctor
        #    durante un relevo: llevan ese prefijo sin ser un fallo (no negociable 15), y
        #    contarlos convertiría cada relevo en un paciente desatendido. Un
        #    `fallo_respuesta` que NO sea de relevo sí cuenta: al paciente no le llegó nada, y
        #    que el motivo esté anotado no cambia lo que le pasó a él.
        cur.execute(
            "SELECT count(*) FROM mensajes_entrantes"
            " WHERE recibido_en >= %s"
            f"   AND recibido_en < %s - interval '{MINUTOS_SIN_CONTESTAR} minutes'"
            "   AND respondido_en IS NULL"
            "   AND (fallo_respuesta IS NULL OR fallo_respuesta NOT LIKE 'relevo:%%')",
            (desde, ahora),
        )
        sin_contestar = cur.fetchone()[0]

        # 5. El tiempo del doctor. `relevo.cerrar` pone `tomada_por = NULL` pero no toca
        #    `tomada_en`, así que la duración sobrevive al cierre; un relevo todavía abierto
        #    cuenta hasta `ahora`.
        #
        #    SUBCUENTA, y hay que saberlo: `activar_relevo` limpia `relevo_cerrado_en` al
        #    reactivar, así que un segundo relevo en la misma conversación borra el rastro
        #    del primero. Se prefiere un número bajo y honesto a uno inventado.
        cur.execute(
            "SELECT coalesce(sum(EXTRACT(EPOCH FROM"
            "         (coalesce(relevo_cerrado_en, %s) - tomada_en))) / 60, 0)::int,"
            "       count(*)"
            "  FROM conversaciones WHERE tomada_en >= %s",
            (ahora, desde),
        )
        minutos_doctor, conversaciones_con_relevo = cur.fetchone()

        # El volumen por día, en hora de Bogotá. Sin `AT TIME ZONE` el corte del día lo pone
        # el reloj del servidor y las barras se desplazan respecto de lo que vivió la clínica.
        cur.execute(
            "SELECT date(creada_en AT TIME ZONE 'America/Bogota') AS dia, count(*)"
            "  FROM conversaciones"
            " WHERE creada_en >= %s AND canal = 'whatsapp'"
            " GROUP BY 1 ORDER BY 1",
            (desde,),
        )
        volumen = [{"dia": d.isoformat(), "conversaciones": n} for d, n in cur.fetchall()]

    return {
        "desde": desde.isoformat(),
        "dias": dias,
        "escribieron": escribieron,
        "con_cita": con_cita,
        "asistencia": {
            "llegaron": llegaron,
            "marcadas": marcadas,
            "cumplibles": cumplibles,
        },
        "sin_contestar": sin_contestar,
        "relevo": {
            "minutos": minutos_doctor,
            "conversaciones": conversaciones_con_relevo,
        },
        "volumen": volumen,
        "agenda_hoy": citas_del_dia(
            conn, desde=inicio_del_dia, hasta=inicio_del_dia + timedelta(days=1)
        ),
    }


# ==========================================================================================
# La pantalla de Conversaciones
# ==========================================================================================

#: Cuántas horas deja Meta para responderle a alguien con texto libre, contadas desde SU
#: último mensaje. Fuera de esa ventana solo entran plantillas aprobadas, y ninguna de las
#: tres que tiene el proyecto dice «el doctor quiere hablar contigo».
#:
#: No se comprueba contra Meta: es una regla de su plataforma, no un estado que se consulte.
#: Lo que sí se hace es no dejar que el botón «Enviar» falle con un error de la Graph API que
#: nadie sabe leer.
VENTANA_RESPUESTA_HORAS = 24

#: Qué se pinta cuando un mensaje del paciente no tiene texto. Un hueco en el hilo se lee
#: como «no dijo nada», y lo que pasó es que mandó una radiografía.
_MARCA_POR_TIPO = {
    "audio": "(nota de voz)",
    "image": "(imagen)",
    "video": "(video)",
    "document": "(documento)",
    "sticker": "(sticker)",
    "location": "(ubicación)",
}


def _marca(tipo: str | None) -> str:
    return _MARCA_POR_TIPO.get(tipo or "", "(archivo)")


def _nombre_visible(nombre_ficha: str | None, nombre_perfil: str | None) -> str | None:
    """El nombre que se pinta, o `None` para que la pantalla caiga al teléfono.

    **Una ficha cuyo nombre es `PENDIENTE` no cuenta como nombre** (no negociable 12): es el
    marcador que deja el sistema cuando no sabe, y pintarlo donde va un nombre es exactamente
    el error que esa regla existe para evitar. Se cae al nombre del perfil de WhatsApp, que
    es lo que la propia persona escribió de sí misma.
    """
    if nombre_ficha and nombre_ficha != _NOMBRE_PENDIENTE:
        return nombre_ficha
    return nombre_perfil or None


def listar_conversaciones(
    conn, *, ahora: datetime, dias: int = 30, limite: int = 50
) -> list[dict[str, Any]]:
    """Una fila por TELÉFONO, la más reciente primero. SOLO LECTURA.

    La unidad es el teléfono y no la conversación, por lo mismo que en `resumen_inicio`: la
    conversación caduca por inactividad de 24 h, así que una persona que lleva una semana
    negociando son siete filas en `conversaciones` y una sola fila aquí.

    `dias` acota las dos consultas pesadas y vale 30 igual que la portada, a propósito: los
    dos números tienen que poder mirarse a la vez sin contradecirse. Un mensaje sin responder
    de hace tres meses tampoco está «esperando respuesta» -- eso ya es otra cosa.

    `ahora` entra por parámetro, sin default. Un `now()` dentro de la consulta convierte
    cualquier prueba en una que envejece, y aquí eso ya amaneció en rojo tres veces.
    """
    desde = ahora - timedelta(days=dias)
    parametros = {"desde": desde, "ahora": ahora, "limite": limite}

    with conn.cursor() as cur:
        cur.execute(
            "WITH ultimo AS ("
            "    SELECT DISTINCT ON (telefono)"
            "           telefono, texto, tipo, nombre_perfil, recibido_en, transcripcion"
            "      FROM mensajes_entrantes"
            "     WHERE recibido_en >= %(desde)s"
            "     ORDER BY telefono, recibido_en DESC"
            "), esperando AS ("
            "    SELECT telefono, count(*) AS n"
            "      FROM mensajes_entrantes"
            "     WHERE recibido_en >= %(desde)s"
            f"      AND recibido_en < %(ahora)s - interval '{MINUTOS_SIN_CONTESTAR} minutes'"
            "       AND respondido_en IS NULL"
            # Las DOS mitades. `NULL NOT LIKE 'relevo:%%'` no es cierto en SQL: es NULL, y el
            # filtro lo descarta. Escrito solo con la segunda, esta consulta se comería justo
            # el caso peor -- el mensaje que nadie intentó contestar porque el proceso se cayó
            # antes de anotar siquiera el fallo, con las dos columnas en NULL--, que es el
            # motivo de existir del índice de la migración 009.
            "       AND (fallo_respuesta IS NULL OR fallo_respuesta NOT LIKE 'relevo:%%')"
            "     GROUP BY telefono"
            "), viva AS ("
            "    SELECT DISTINCT ON (telefono) telefono, tomada_por, actualizada_en"
            "      FROM conversaciones"
            "     ORDER BY telefono, actualizada_en DESC"
            ") "
            "SELECT u.telefono, p.nombre_completo, u.nombre_perfil, u.texto, u.tipo,"
            "       u.recibido_en, coalesce(e.n, 0), v.tomada_por, v.actualizada_en,"
            "       u.transcripcion"
            "  FROM ultimo u"
            "  LEFT JOIN esperando e ON e.telefono = u.telefono"
            "  LEFT JOIN viva      v ON v.telefono = u.telefono"
            "  LEFT JOIN pacientes p ON p.telefono = u.telefono"
            " ORDER BY GREATEST(u.recibido_en, coalesce(v.actualizada_en, u.recibido_en)) DESC"
            " LIMIT %(limite)s",
            parametros,
        )
        filas = cur.fetchall()

    ventana = ahora - timedelta(hours=VENTANA_RESPUESTA_HORAS)
    lista: list[dict[str, Any]] = []
    for tel, ficha, perfil, texto, tipo, recibido, esperando, tomada, tocada, dicho in filas:
        ultimo_en = max(recibido, tocada) if tocada else recibido
        lista.append(
            {
                "telefono": tel,
                "nombre": _nombre_visible(ficha, perfil),
                # Una nota de voz transcrita se asoma con lo que dijo, no con «(nota de voz)»:
                # la lista existe para decidir a quién abrir, y «me duele mucho» y «¿cuánto
                # cuesta?» llevan a decisiones distintas.
                "vista_previa": (texto or "").strip() or (dicho or "").strip() or _marca(tipo),
                "ultimo_en": ultimo_en.isoformat(),
                "sin_contestar": esperando,
                "tomada_por": tomada,
                # La precedencia importa: una conversación tomada en la que entran mensajes
                # cumple `relevo` y `esperando` a la vez, y lo que hay que decir es que ya hay
                # alguien encima, no que nadie contesta.
                "estado": (
                    "relevo"
                    if tomada
                    else "esperando"
                    if esperando
                    else "activa"
                    if ultimo_en >= ventana
                    else "cerrada"
                ),
            }
        )
    return lista


def hilo(conn, telefono: str, *, limite: int | None = 60) -> list[dict[str, Any]]:
    """Lo que se dijeron los tres, en orden. SOLO LECTURA.

    Modelado sobre `persistencia.transcripcion` --que hace esto mismo para volcarlo en el
    hilo de Telegram-- y con una voz más: la del doctor, que desde la migración 026 sí se
    guarda. Lo frágil, el desempaquetado del formato del SDK, NO se duplica: se llama a
    `persistencia.texto_de_daniela`, para que el día que suba la versión haya un solo sitio
    que arreglar.

    El corte va por el FINAL. Lo que hace falta para entender qué está pasando es lo último
    que se dijeron, no cómo empezó todo hace dos meses.

    **`limite=None` devuelve el hilo ENTERO, y existe para el export.** Un archivo que la
    clínica se descarga para guardar o para enseñárselo a alguien no puede venir recortado
    por un default pensado para una pantalla: sería un recorte que nadie pidió y que nada
    anuncia. La pantalla sigue pidiendo sus 60. `LIMIT NULL` es «sin límite» en Postgres, así
    que el `None` viaja tal cual a las dos consultas y solo hay que cuidar las dos cuentas
    que se hacen en Python con él.
    """
    lineas: list[dict[str, Any]] = []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT texto, tipo, recibido_en, transcripcion FROM mensajes_entrantes"
            " WHERE telefono = %s ORDER BY recibido_en DESC LIMIT %s",
            (telefono, limite),
        )
        for texto, tipo, cuando, transcrito in cur.fetchall():
            escrito = (texto or "").strip()
            dicho = (transcrito or "").strip()
            lineas.append(
                {
                    "quien": "paciente",
                    "autor": None,
                    # Lo ESCRITO manda sobre lo dicho: si un audio trajera caption, esas son
                    # las palabras del paciente y lo demás es lo que una máquina entendió.
                    "texto": escrito or dicho or _marca(tipo),
                    "cuando": cuando,
                    "fallo": None,
                    # La pantalla tiene que poder decir que esto se DIJO y que lo transcribió
                    # una máquina. No es un adorno: «el 46» y «el 40» suenan casi igual, y
                    # quien lee una frase clínica tiene derecho a saber que puede estar mal
                    # oída. Un audio sin transcribir se queda en `(nota de voz)` con `voz` en
                    # falso, que es lo correcto: ahí no hay nada que desconfiar.
                    "voz": bool(dicho) and not escrito,
                }
            )

        # `created_at` es TIMESTAMP **sin zona** --lo fija el SDK, no nosotros: no negociable
        # 10--, y todo lo demás del esquema es TIMESTAMPTZ. Sin el `AT TIME ZONE` las dos
        # mitades no se pueden ordenar juntas y Python revienta con TypeError.
        #
        # Se piden CUATRO veces el límite porque un item del historial NO es un mensaje: una
        # llamada a tool y su resultado son dos filas más que no dicen nada en voz alta. Pedir
        # `limite` filas devolvería un puñado de frases.
        cur.execute(
            "SELECT m.message_data, m.created_at AT TIME ZONE 'UTC'"
            "  FROM agent_messages m"
            " WHERE m.session_id IN ("
            "        SELECT id::text FROM conversaciones WHERE telefono = %s)"
            " ORDER BY m.created_at DESC LIMIT %s",
            (telefono, None if limite is None else limite * 4),
        )
        for crudo, cuando in cur.fetchall():
            dicho = persistencia.texto_de_daniela(crudo)
            if dicho:
                lineas.append(
                    {
                        "quien": "daniela",
                        "autor": None,
                        "texto": dicho,
                        "cuando": cuando,
                        "fallo": None,
                        "voz": False,
                    }
                )

    for fila in persistencia.mensajes_del_doctor(conn, telefono, limite=limite):
        lineas.append(
            {
                "quien": "doctor",
                "autor": fila["autor"],
                "texto": fila["texto"],
                "cuando": fila["cuando"],
                "fallo": fila["fallo"],
                "voz": False,
            }
        )

    lineas.sort(key=lambda l: l["cuando"])
    recorte = lineas if limite is None else lineas[-limite:]
    return [{**linea, "cuando": linea["cuando"].isoformat()} for linea in recorte]


def borrar_conversacion(
    conn, telefono: str, *, usuario: str, ahora: datetime
) -> dict[str, Any]:
    """Borra el hilo de ese número, conserva sus citas, y deja constancia de quién lo hizo.

    Es el único borrado que este panel puede hacer, y no tiene vuelta atrás. Por eso la
    bitácora va en la MISMA transacción que el borrado --la regla de la cabecera de este
    módulo-- y con más razón que en un cambio de precio: allí la fila permite reconstruir qué
    decía antes, y aquí es lo único que va a quedar.

    Lo que se escribe en `valor_anterior` es el recuento, no el contenido. Copiar los mensajes
    a la bitácora convertiría el borrado en un traslado: el paciente que pide que borren lo
    suyo acabaría con su conversación entera guardada en otra tabla, donde nadie la busca y
    nadie la borra.

    Lo que se borra y lo que sobrevive está en `persistencia.borrar_conversacion` y en la
    migración 028. Aquí solo se añade el quién.
    """
    resumen = persistencia.lo_que_se_borraria(conn, telefono, ahora=ahora)
    borradas = persistencia.borrar_conversacion(conn, telefono, commit=False)

    citas = len(resumen["citas_futuras"])
    with conn.cursor() as cur:
        _anotar(
            cur,
            tabla="conversaciones",
            clave=telefono,
            anterior=(
                f"{resumen['mensajes']} mensajes, "
                f"{borradas['conversaciones']} conversaciones, "
                f"{borradas['casos_sin_resolver']} frases en sin-resolver"
            ),
            nuevo=(
                f"BORRADA. Se conservaron {borradas['citas_conservadas']} citas "
                f"({citas} futuras, que quedan SIN recordatorio)."
            ),
            usuario=usuario,
        )
    conn.commit()
    return {**borradas, "mensajes": resumen["mensajes"], "citas_futuras": citas}


# ------------------------------------------------------------------------------------------
# El export
# ------------------------------------------------------------------------------------------

#: Las columnas del CSV, en orden. Es un contrato con quien abra el archivo dentro de un año:
#: añadir una al final es seguro, reordenarlas rompe cualquier plantilla que la clínica haya
#: montado encima.
COLUMNAS_DEL_EXPORT = (
    "telefono", "fecha_hora", "quien", "autor", "texto", "nota_de_voz",
)

#: Punto y coma, y no coma. El archivo existe para abrirse en el Excel de una clínica
#: colombiana, donde el separador de listas del sistema es `;`: con comas, Excel mete las seis
#: columnas en una sola y el export deja de servir para lo único que se pidió. Va junto al BOM
#: de abajo, que es lo que hace que Excel lo lea como UTF-8 y no parta los acentos.
SEPARADOR_CSV = ";"

#: Excel no detecta UTF-8 sin esto: «valoración» se abre como «valoraciÃ³n». Es un carácter
#: invisible al principio del archivo, y las demás herramientas (Sheets, pandas, un editor) lo
#: ignoran o lo absorben solas.
BOM = "﻿"

#: Lo que se le pone a `quien` en el archivo. Las claves son las del hilo; los valores, algo
#: que signifique lo mismo para alguien que abre el CSV en seis meses y no sabe que la agente
#: se llama Daniela ni que `doctor` incluye a recepción.
QUIEN_EN_EL_ARCHIVO = {
    "paciente": "Paciente",
    "daniela": "Daniela (automático)",
    "doctor": "Clínica (persona)",
}


def telefonos_con_hilo(conn) -> list[str]:
    """Todos los números que tienen algo que exportar, ordenados.

    Se juntan las TRES tablas y no solo `conversaciones`: un número puede tener mensajes
    entrantes cuya conversación se borró --quedan con `conversacion_id` en NULL-- y un export
    que no los viera diría que ese paciente nunca escribió.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT telefono FROM conversaciones
            UNION SELECT telefono FROM mensajes_entrantes
            UNION SELECT telefono FROM mensajes_del_doctor
             ORDER BY 1
            """
        )
        return [f[0] for f in cur.fetchall() if f[0]]


def _dentro_del_rango(iso: str, desde: str | None, hasta: str | None) -> bool:
    """¿Cae este instante dentro del rango de días pedido? En hora de Bogotá.

    El rango lo escribe una persona mirando un calendario, así que `hasta` INCLUYE su día
    entero: pedir del 1 al 30 y que el 30 no salga es la clase de recorte que nadie nota
    hasta que falta el mensaje que se buscaba.
    """
    dia = datetime.fromisoformat(iso).astimezone(ZONA_BOGOTA).date().isoformat()
    if desde and dia < desde:
        return False
    if hasta and dia > hasta:
        return False
    return True


def exportar_csv(
    conn,
    *,
    telefono: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
) -> tuple[str, int]:
    """El CSV y cuántas líneas de conversación lleva. SOLO LECTURA.

    Los tres filtros son opcionales y se combinan: sin ninguno sale todo, con `telefono` sale
    una conversación, y con `desde`/`hasta` (fechas `YYYY-MM-DD` de Bogotá) sale el periodo.
    Las tres formas que pidió MaxiCare son la misma función con argumentos distintos, y eso
    es deliberado: con tres endpoints, arreglar el formato en uno y olvidarlo en otro produce
    dos archivos que dicen cosas distintas de los mismos datos.

    **Pide el hilo SIN límite.** `panel.hilo` recorta a 60 para la pantalla, que es lo
    correcto ahí y sería mentira aquí: un archivo que la clínica guarda o enseña no puede
    venir cortado por un default que nada anuncia.

    Las filas van agrupadas por teléfono y, dentro, en orden cronológico. Ordenarlo todo por
    fecha mezclaría a doce pacientes línea a línea, que es ilegible; así cada conversación se
    lee de corrida y el filtro de Excel sigue sirviendo para lo demás.

    El teléfono va COMPLETO dentro del archivo, decidido por MaxiCare el 22/09/2026: quien
    descarga ya los ve todos en la pantalla, y sin el número un export de varias
    conversaciones no sirve para volver a contactar a nadie ni para cruzarlo con la agenda.
    """
    numeros = [telefono] if telefono else telefonos_con_hilo(conn)

    filas: list[list[str]] = []
    for numero in numeros:
        for linea in hilo(conn, numero, limite=None):
            if not _dentro_del_rango(linea["cuando"], desde, hasta):
                continue
            cuando = datetime.fromisoformat(linea["cuando"]).astimezone(ZONA_BOGOTA)
            filas.append([
                numero,
                cuando.strftime("%Y-%m-%d %H:%M"),
                QUIEN_EN_EL_ARCHIVO.get(linea["quien"], linea["quien"]),
                linea["autor"] or "",
                linea["texto"] or "",
                "si" if linea["voz"] else "no",
            ])

    # `QUOTE_ALL` y no el default: un mensaje de WhatsApp lleva saltos de línea, punto y coma
    # y comillas con total normalidad, y una sola celda mal cerrada desplaza todas las
    # columnas de ahí hacia abajo sin que nada avise. `\r\n` es lo que manda el RFC 4180 y lo
    # que Excel espera dentro de una celda multilínea.
    buffer = io.StringIO()
    escritor = csv.writer(
        buffer, delimiter=SEPARADOR_CSV, quoting=csv.QUOTE_ALL, lineterminator="\r\n"
    )
    escritor.writerow(COLUMNAS_DEL_EXPORT)
    escritor.writerows(filas)
    return BOM + buffer.getvalue(), len(filas)


def puede_escribir(conn, telefono: str, *, ahora: datetime) -> dict[str, Any]:
    """Si WhatsApp todavía acepta texto libre hacia ese número, y cuántas horas han pasado.

    Sin esto, el botón «Enviar» del panel fallaría con un error de la Graph API (131047) que
    no le dice nada a quien lo lee, después de haber escrito el mensaje. Es más honesto
    apagar la caja y decir por qué.

    Sin ningún mensaje del paciente NUNCA se puede escribir: la ventana la abre él, y un
    número al que nadie escribió jamás no tiene ventana abierta, no una de 0 horas.
    """
    ultimo = persistencia.ultimo_mensaje_del_paciente(conn, telefono)
    if ultimo is None:
        return {"puede": False, "horas": None}
    horas = (ahora - ultimo).total_seconds() / 3600
    return {"puede": horas < VENTANA_RESPUESTA_HORAS, "horas": round(horas, 1)}


# ==========================================================================================
# La pantalla de Leads
# ==========================================================================================

#: Lo que el sistema escribe en `estado_oportunidad.tratamiento` cuando NO sabe a qué va el
#: paciente. No es un tratamiento y no se pinta como tal: el no negociable 12 lo dice para la
#: agenda («una cita con él diría que nadie sabe a qué va el paciente») y aquí vale igual.
#: `persistencia.py` ya lo filtra en la consulta que arma el contexto de Daniela; esta es la
#: segunda puerta por la que ese valor podría salir a una pantalla, y se cierra en el mismo
#: sitio: traducirlo a `None` para que la pantalla diga «sin definir», que es la verdad.
_TRATAMIENTO_SIN_SABER = "no_identificado"


def listar_leads(
    conn,
    *,
    ahora: datetime,
    dias: int = persistencia.DIAS_DE_VENTANA_DE_CARTERA,
    limite: int = 200,
) -> list[dict[str, Any]]:
    """Una fila por TELÉFONO con su estado comercial. SOLO LECTURA.

    Es la hermana de `listar_conversaciones` y comparte con ella las dos decisiones que
    importan: la unidad es el teléfono --una persona que lleva una semana preguntando son
    siete filas en `conversaciones` y una sola aquí-- y `ahora` entra por parámetro, porque un
    `now()` dentro del SQL convierte cualquier prueba en una que envejece.

    **Y «por teléfono» se aplica hasta el final, no solo al agrupar.** El estado, los
    seguimientos y la cita salen de la PERSONA, nunca de su conversación más reciente: una
    conversación nueva --y se abre una cada vez que la anterior caduca a las 24 h-- no tiene
    fila en `estado_oportunidad` hasta que Daniela conteste un turno, así que colgar el estado
    de ella hacía desaparecer lo que ya se sabía. Lo cazó una prueba, y es la misma decisión
    que toma `persistencia` en la consulta que arma el contexto de Daniela.

    Lo que añade es lo que ninguna pantalla enseñaba todavía: `estado_oportunidad`, que Daniela
    reescribe en CADA turno y que hasta hoy solo se leía a sí misma. El dato existía, con
    quince personas dentro, y no había forma de verlo.

    **La lista arranca en `mensajes_entrantes` y no en `conversaciones`, a propósito.** El
    cartel que esta pantalla sustituye promete «todas las personas que han escrito», y esas dos
    tablas no dicen lo mismo: hay teléfonos con mensajes y sin conversación viva --y son justo
    los que nadie está mirando--, y conversaciones sembradas por un script de pruebas sin un
    solo mensaje detrás, que no son personas.

    Cuatro cosas que esta función NO hace, y cada una tiene su motivo escrito:

    - **No inventa el origen del lead.** El cartel prometía «de qué anuncio llegaron» y ese
      dato no existe en ninguna tabla: el bloque `referral` que Meta manda en los
      click-to-WhatsApp no se lee en la ingesta. El precedente de la fase 8 con el `origen` del
      paciente es explícito --«no se fabrica el dato ni se pone PENDIENTE en una pantalla de
      cara al usuario: sale»-- y aquí se aplica igual.
    - **No rellena el estado que falta.** Sin fila en `estado_oportunidad` el estado sale
      `None`, no `'explorando'`. Esa columna tiene ese DEFAULT para cuando se inserta, y usarlo
      aquí convertiría «nadie llegó a saber qué quería» en «está explorando», que es un hecho
      distinto -- y precisamente el que el filtro principal de la pantalla busca.
    - **No dice a quién va a escribir la reactivación.** Devuelve lo que hay --si hay un
      seguimiento en cola y cuántos salieron ya--, nunca una predicción: `persistencia` advierte
      que sus dos consultas de cartera no son la última palabra sobre a quién se le escribe, que
      lo es `seguimientos.decidir` con sus trece guardas. Una pantalla que pintara «a estos se
      les va a escribir» mentiría.
    - **No agrupa `con_barrera` con las barreras.** Devuelve estado y barrera por separado
      porque `estado='con_barrera'` con `barrera='ninguna'` significa «detenida por algo que NO
      es una objeción del paciente» --una avería--, y mezclarlos enseñaría fallos técnicos como
      clientes dudando. Quien decide cómo se lee eso es la pantalla, que es la que tiene sitio
      para explicarlo.
    """
    desde = ahora - timedelta(days=dias)
    parametros = {
        "desde": desde,
        "ahora": ahora,
        "limite": limite,
        "sin_saber": _TRATAMIENTO_SIN_SABER,
    }

    with conn.cursor() as cur:
        cur.execute(
            # `DISTINCT ON` cuatro veces, y las cuatro por lo mismo: de cada persona interesa
            # UNA fila --su último mensaje, su conversación más reciente, su próxima cita, su
            # seguimiento más cercano-- y sin esto una persona con cuatro conversaciones sale
            # cuatro veces en una pantalla que promete listar personas.
            "WITH ultimo AS ("
            "    SELECT DISTINCT ON (telefono) telefono, nombre_perfil, recibido_en"
            "      FROM mensajes_entrantes"
            "     WHERE recibido_en >= %(desde)s"
            "     ORDER BY telefono, recibido_en DESC"
            "), viva AS ("
            "    SELECT DISTINCT ON (telefono) telefono, id, actualizada_en"
            "      FROM conversaciones"
            "     WHERE canal = 'whatsapp'"
            "     ORDER BY telefono, actualizada_en DESC"
            # La oportunidad va por TELÉFONO y por su propia fecha, NO por la conversación más
            # reciente. La diferencia se midió con una prueba: una conversación nueva --y se
            # abre una cada vez que la anterior caduca a las 24 h-- todavía no tiene fila en
            # `estado_oportunidad` hasta que Daniela conteste un turno, así que colgarla de la
            # más reciente hacía desaparecer el estado que ya se sabía de esa persona.
            # `persistencia` toma la misma decisión por el mismo motivo.
            "), oportunidad AS ("
            # El `NULLIF` va AQUÍ y no más abajo, y no es un capricho de estilo: la columna
            # se une luego con `tratamientos` para sacar su etiqueta, y `no_identificado` ES
            # una fila de esa tabla. Traducido, saldría como un tratamiento con nombre propio
            # -- justo lo contrario de lo que ese valor significa. Cortado en el origen, el
            # `JOIN` no lo encuentra y la pantalla dice «todavía no se sabe qué quiere».
            "    SELECT DISTINCT ON (c.telefono)"
            "           c.telefono, eo.estado, eo.barrera,"
            "           NULLIF(eo.tratamiento, %(sin_saber)s) AS tratamiento,"
            "           eo.fuera_de_alcance, eo.notas, eo.actualizado_en"
            "      FROM estado_oportunidad eo"
            "      JOIN conversaciones c ON c.id = eo.conversacion_id"
            "     ORDER BY c.telefono, eo.actualizado_en DESC"
            "), proxima AS ("
            # `estado <> 'cancelada'` y no `= 'confirmada'`: una cita reprogramada sigue siendo
            # una cita, y contarla como ausente pondría a su dueño en la lista de «a este hay
            # que llamarlo» el día que ya tiene hora.
            "    SELECT DISTINCT ON (telefono) telefono, inicio, tratamiento"
            "      FROM citas"
            "     WHERE estado <> 'cancelada' AND inicio >= %(ahora)s"
            "     ORDER BY telefono, inicio"
            # Los seguimientos también por teléfono, y por lo mismo: `seguimientos` cuelga de
            # la conversación donde se programó, que puede haber caducado. «Cuántos mensajes
            # se le han mandado a esta persona» no es «cuántos van por esta conversación».
            "), programado AS ("
            "    SELECT DISTINCT ON (c.telefono) c.telefono, s.fecha_objetivo"
            "      FROM seguimientos s"
            "      JOIN conversaciones c ON c.id = s.conversacion_id"
            "     WHERE s.enviado_en IS NULL AND s.anulado_en IS NULL"
            "     ORDER BY c.telefono, s.fecha_objetivo"
            "), enviados AS ("
            "    SELECT c.telefono, count(*) AS n"
            "      FROM seguimientos s"
            "      JOIN conversaciones c ON c.id = s.conversacion_id"
            "     WHERE s.enviado_en IS NOT NULL"
            "     GROUP BY c.telefono"
            ") "
            # Los dos tratamientos salen con la ETIQUETA que la clínica edita en la pantalla
            # de Tratamientos, no con la clave: `carillas_esteticas` es un identificador de
            # base de datos y quien mira esto no tiene por qué leer guiones bajos. El
            # `coalesce` cae a la clave porque una clave validada no es una clave VIGENTE --
            # la tabla puede haber perdido ese tratamiento después--, y quedarse sin nada
            # sería peor que enseñarla cruda.
            "SELECT u.telefono, p.nombre_completo, u.nombre_perfil, u.recibido_en,"
            "       v.actualizada_en, o.estado, o.barrera,"
            "       coalesce(t.etiqueta, o.tratamiento), o.fuera_de_alcance,"
            "       o.notas, o.actualizado_en, x.inicio,"
            "       coalesce(tc.etiqueta, x.tratamiento), g.fecha_objetivo,"
            "       coalesce(e.n, 0), coalesce(ct.no_contactar, false), ct.no_contactar_origen"
            "  FROM ultimo u"
            "  LEFT JOIN viva         v  ON v.telefono = u.telefono"
            "  LEFT JOIN oportunidad  o  ON o.telefono = u.telefono"
            "  LEFT JOIN pacientes    p  ON p.telefono = u.telefono"
            "  LEFT JOIN contactos    ct ON ct.telefono = u.telefono"
            "  LEFT JOIN proxima      x  ON x.telefono = u.telefono"
            "  LEFT JOIN programado   g  ON g.telefono = u.telefono"
            "  LEFT JOIN enviados     e  ON e.telefono = u.telefono"
            "  LEFT JOIN tratamientos t  ON t.clave = o.tratamiento"
            "  LEFT JOIN tratamientos tc ON tc.clave = x.tratamiento"
            " ORDER BY GREATEST(u.recibido_en, coalesce(v.actualizada_en, u.recibido_en)) DESC"
            " LIMIT %(limite)s",
            parametros,
        )
        filas = cur.fetchall()

    lista: list[dict[str, Any]] = []
    for fila in filas:
        (
            tel, ficha, perfil, recibido, tocada, estado, barrera, tratamiento,
            fuera, notas, estado_en, cita_inicio, cita_trat, programado_en,
            enviados, baja, baja_origen,
        ) = fila
        ultimo_en = max(recibido, tocada) if tocada else recibido
        lista.append(
            {
                "telefono": tel,
                "nombre": _nombre_visible(ficha, perfil),
                "estado": estado,
                "barrera": barrera,
                # Ya viene como etiqueta legible, y ya viene en `None` si el sistema no sabía:
                # las dos cosas las resuelve el SQL, cada una en el único sitio donde no se
                # pueden pisar entre sí.
                "tratamiento": tratamiento,
                "fuera_de_alcance": bool(fuera),
                # La frase que Daniela dejó escrita sobre esta persona. Es lo único de esta
                # pantalla redactado para un humano, y lo que hace que abrir la conversación
                # sea una decisión y no una lotería.
                "notas": notas,
                "ultimo_en": ultimo_en.isoformat(),
                "estado_en": estado_en.isoformat() if estado_en else None,
                "cita": (
                    {"inicio": cita_inicio.isoformat(), "tratamiento": cita_trat}
                    if cita_inicio
                    else None
                ),
                "programado_en": programado_en.isoformat() if programado_en else None,
                "seguimientos_enviados": enviados,
                "baja": bool(baja),
                "baja_origen": baja_origen,
            }
        )
    return lista

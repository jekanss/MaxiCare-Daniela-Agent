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

import re
from typing import Any, get_args

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


def historial(conn, limite: int = 100) -> list[dict[str, Any]]:
    """Lo más reciente primero. Sin paginación: cien cambios cubren meses de esta clínica.

    El desempate por `id` no es adorno. Un solo cambio de la pantalla puede escribir DOS
    filas en la misma transacción --contenido y aprobación, o nombre y activo--, y
    `cambiado_en` es `now()`, que en Postgres es el instante de la TRANSACCIÓN: las dos
    filas llevan exactamente la misma marca de tiempo. Ordenando solo por ella, cuál sale
    arriba lo decide el planificador, y la bitácora contaría la historia al revés de vez en
    cuando.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tabla, clave, valor_anterior, valor_nuevo, usuario, cambiado_en "
            "FROM cambios_configuracion ORDER BY cambiado_en DESC, id DESC LIMIT %s",
            (max(1, min(limite, 500)),),
        )
        return [
            {"tabla": t, "clave": c, "valor_anterior": a, "valor_nuevo": n,
             "usuario": u, "cambiado_en": f.isoformat()}
            for t, c, a, n, u, f in cur.fetchall()
        ]

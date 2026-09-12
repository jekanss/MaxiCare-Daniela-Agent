"""Acceso a Neon Postgres: conexión, aplicación del esquema y los repositorios que las tools
de `herramientas.py` van a usar.

Por qué existe este módulo y no está todo en `herramientas.py`: una tool es lógica de
negocio, y mezclarle el SQL la vuelve imposible de probar sin una base. Aquí vive el acceso
a datos; allá vive la decisión de qué hacer con ellos.

Este módulo NO importa de `runtime.py` -- no es transporte. Una base de datos es un sistema
de `sistemas.lista[]`, igual que Google Calendar, y da lo mismo si la petición que disparó
la consulta llegó por WhatsApp, por el chat web o por un cron.

------------------------------------------------------------------------------------------
La regla que este módulo hace cumplir
------------------------------------------------------------------------------------------

Cuando no hay dato documentado, la respuesta NO es una cadena vacía ni `None`: es el literal
`SIN DATO DOCUMENTADO` con la instrucción explícita de no estimar y de escalar.

El vacío es una invitación a que el modelo complete el hueco con algo razonable, y "algo
razonable" sobre un precio es exactamente el fallo que rompe el primer objetivo del brief.
La ausencia de dato tiene que ser un dato explícito.

La sección 2.12 del documento maestro dice que endodoncia y prótesis no tienen precio
documentado. La sección 4 lista otros diez puntos pendientes de aprobación de MaxiCare: esos
sí se cargan, pero marcados `aprobado = FALSE`, y se devuelven con una advertencia para que
Daniela no los comunique como compromiso comercial.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

# ==========================================================================================
# Los literales que el resto del sistema busca
# ==========================================================================================

#: Lo que devuelve una consulta sin fila. El guardrail `sin_cifra_no_documentada` del bloque
#: 8 se apoya en que esta respuesta no trae ninguna cifra: cualquier número que aparezca en
#: el mensaje al paciente sin respaldo de una consulta exitosa dispara el tripwire.
SIN_DATO = (
    "SIN DATO DOCUMENTADO. MaxiCare no tiene información aprobada sobre {que}. "
    "PROHIBIDO estimar una cifra, extrapolar de un tratamiento parecido o responder de "
    "memoria. Dile al paciente que lo vas a confirmar con los doctores y escala."
)

#: Lo que se antepone a un dato que existe pero que MaxiCare todavía no aprobó (sección 4
#: del documento maestro).
PENDIENTE_APROBACION = (
    "PENDIENTE DE APROBACIÓN POR MAXICARE — no lo comuniques como compromiso comercial. "
    "Puedes mencionarlo como referencia general aclarando que se confirma en la valoración."
)

#: Defaults de la configuración operativa. El valor vigente vive en la tabla `configuracion`
#: de Neon, que la clínica edita desde la interfaz web; estos son el respaldo para cuando la
#: tabla todavía no existe (arranque en frío) y la documentación de qué significa cada uno.
CONFIGURACION_POR_DEFECTO: dict[str, int] = {
    "capacidad_por_hora": 2,
    "duracion_cita_minutos": 60,
    "cierre_relevo_minutos": 180,
    "aviso_relevo_minutos": 120,
}

RAIZ_PROYECTO = Path(__file__).resolve().parents[2]
RUTA_MIGRACIONES = RAIZ_PROYECTO / "migraciones"
RUTA_SEMILLA_CONOCIMIENTO = RAIZ_PROYECTO / "datos" / "base_conocimiento.json"


# ==========================================================================================
# Lógica pura -- se prueba sin base de datos
# ==========================================================================================


@dataclass(frozen=True)
class FilaConocimiento:
    """Una fila de `base_conocimiento` tal como la devuelve la consulta."""

    tratamiento: str
    concepto: str
    contenido: str
    aprobado: bool
    nota_pendiente: str | None = None


def formatear_conocimiento(
    filas: Sequence[FilaConocimiento], *, tratamiento: str, concepto: str | None = None
) -> str:
    """Convierte el resultado de una consulta en el texto que recibe el modelo.

    Es una función pura a propósito: es la regla más importante del sistema y tiene que
    poder probarse sin levantar una base de datos.

    - Sin filas -> el literal `SIN DATO DOCUMENTADO`, nunca una cadena vacía.
    - Filas no aprobadas -> el contenido, precedido de la advertencia de aprobación.
    - Filas aprobadas -> el contenido tal cual.
    """
    if not filas:
        que = f"«{concepto}» de {tratamiento}" if concepto else f"«{tratamiento}»"
        return SIN_DATO.format(que=que)

    partes: list[str] = []
    for fila in filas:
        encabezado = fila.concepto.replace("_", " ").upper()
        cuerpo = fila.contenido.strip()
        if not fila.aprobado:
            aviso = PENDIENTE_APROBACION
            if fila.nota_pendiente:
                aviso = f"{aviso} Falta por definir: {fila.nota_pendiente}"
            partes.append(f"[{encabezado}] {aviso}\n{cuerpo}")
        else:
            partes.append(f"[{encabezado}] {cuerpo}")
    return "\n\n".join(partes)


def cargar_semilla(ruta: Path | None = None) -> list[dict[str, Any]]:
    """Lee `datos/base_conocimiento.json`, la transcripción del documento maestro."""
    archivo = ruta or RUTA_SEMILLA_CONOCIMIENTO
    datos = json.loads(archivo.read_text(encoding="utf-8"))
    if not isinstance(datos, list):
        raise ValueError(f"{archivo} debe contener una lista de filas")
    return datos


# ==========================================================================================
# Acceso a Neon
# ==========================================================================================


def conectar(database_url: str):
    """Abre una conexión a Postgres. `psycopg` se importa aquí y no arriba para que el resto
    del módulo -- incluida la lógica pura de `formatear_conocimiento` -- se pueda importar y
    probar sin el driver instalado."""
    import psycopg

    return psycopg.connect(database_url)


def aplicar_esquema(conn, ruta: Path | None = None) -> list[str]:
    """Aplica todas las migraciones en orden de nombre. Idempotente: el SQL usa
    `CREATE TABLE IF NOT EXISTS` y `ON CONFLICT DO NOTHING`."""
    carpeta = ruta or RUTA_MIGRACIONES
    aplicadas: list[str] = []
    for archivo in sorted(carpeta.glob("*.sql")):
        with conn.cursor() as cur:
            cur.execute(archivo.read_text(encoding="utf-8"))
        aplicadas.append(archivo.name)
    conn.commit()
    return aplicadas


def cargar_base_conocimiento(conn, filas: Iterable[dict[str, Any]]) -> int:
    """Inserta o actualiza la base de conocimiento. Idempotente por (tratamiento, concepto):
    volver a correrlo con el documento actualizado pisa el contenido viejo en vez de
    duplicarlo."""
    total = 0
    with conn.cursor() as cur:
        for fila in filas:
            cur.execute(
                """
                INSERT INTO base_conocimiento
                    (tratamiento, concepto, contenido, aprobado, nota_pendiente)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tratamiento, concepto) DO UPDATE SET
                    contenido      = EXCLUDED.contenido,
                    aprobado       = EXCLUDED.aprobado,
                    nota_pendiente = EXCLUDED.nota_pendiente,
                    actualizado_en = now()
                """,
                (
                    fila["tratamiento"],
                    fila["concepto"],
                    fila["contenido"],
                    fila.get("aprobado", True),
                    fila.get("nota_pendiente"),
                ),
            )
            total += 1
    conn.commit()
    return total


def leer_conocimiento(
    conn, tratamiento: str, concepto: str | None = None
) -> list[FilaConocimiento]:
    """Devuelve las filas crudas. El formateo -- y la regla de `SIN DATO DOCUMENTADO` -- vive
    en `formatear_conocimiento`, que es pura."""
    sql = (
        "SELECT tratamiento, concepto, contenido, aprobado, nota_pendiente "
        "FROM base_conocimiento WHERE tratamiento = %s"
    )
    params: list[Any] = [tratamiento]
    if concepto:
        sql += " AND concepto = %s"
        params.append(concepto)
    sql += " ORDER BY concepto"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [FilaConocimiento(*fila) for fila in cur.fetchall()]


def consultar_conocimiento(conn, tratamiento: str, concepto: str | None = None) -> str:
    """Lo que la tool `consultar_base_conocimiento` va a envolver en la fase 3."""
    filas = leer_conocimiento(conn, tratamiento, concepto)
    return formatear_conocimiento(filas, tratamiento=tratamiento, concepto=concepto)


def leer_configuracion(conn) -> dict[str, int]:
    """La configuración operativa vigente, con los defaults como respaldo."""
    valores = dict(CONFIGURACION_POR_DEFECTO)
    with conn.cursor() as cur:
        cur.execute("SELECT clave, valor FROM configuracion")
        for clave, valor in cur.fetchall():
            try:
                valores[clave] = int(valor)
            except (TypeError, ValueError):
                continue
    return valores


# ==========================================================================================
# Pacientes y conversaciones
# ==========================================================================================


def buscar_paciente_por_telefono(conn, telefono: str) -> tuple[int, str] | None:
    """`(id, nombre_completo)` del paciente registrado con ese número, o `None`.

    El número de WhatsApp es el identificador débil del que habla `contexto.identidad_
    solicitante`: si ya está en Neon, la identidad queda verificada sin fricción, y ese es
    el caso común.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, nombre_completo FROM pacientes WHERE telefono = %s", (telefono,)
        )
        fila = cur.fetchone()
    return (fila[0], fila[1]) if fila else None


def asegurar_paciente(conn, *, nombre_completo: str, telefono: str) -> int:
    """Devuelve el id del paciente, creándolo si el teléfono no estaba.

    No actualiza el nombre de uno que ya existe: si el número de la casa lo usan dos
    personas, pisarlo haría que el historial del primero apareciera bajo el nombre del
    segundo. Resolver eso es trabajo de un humano, no de un UPDATE silencioso.
    """
    existente = buscar_paciente_por_telefono(conn, telefono)
    if existente:
        return existente[0]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pacientes (nombre_completo, telefono) VALUES (%s, %s)
            ON CONFLICT (telefono) DO NOTHING
            RETURNING id
            """,
            (nombre_completo, telefono),
        )
        fila = cur.fetchone()
        if fila is None:
            # Otra conexión lo insertó entre el SELECT y el INSERT.
            cur.execute("SELECT id FROM pacientes WHERE telefono = %s", (telefono,))
            fila = cur.fetchone()
    conn.commit()
    return fila[0]


def asegurar_conversacion(
    conn, *, telefono: str, paciente_id: int | None = None, canal: str = "whatsapp"
) -> str:
    """Crea una conversación y devuelve su UUID como texto.

    El id lo genera el código, no la base, porque el orquestador lo necesita para construir
    claves de idempotencia antes de haber escrito nada.
    """
    id_conversacion = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversaciones (id, paciente_id, telefono, canal)
            VALUES (%s, %s, %s, %s)
            """,
            (id_conversacion, paciente_id, telefono, canal),
        )
    conn.commit()
    return id_conversacion


def conversacion_tomada(conn, id_conversacion: str) -> str | None:
    """El doctor que tiene el relevo de esa conversación, o `None` si la tiene Daniela.

    `programar_seguimiento` la consulta antes de escribir: mientras un doctor está hablando,
    el sistema no programa recordatorios por su cuenta.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tomada_por FROM conversaciones WHERE id = %s", (id_conversacion,)
        )
        fila = cur.fetchone()
    return fila[0] if fila and fila[0] else None


def marcar_intento_identificacion(conn, id_conversacion: str, *, verificada: bool) -> int:
    """Suma un intento y, si acertó, deja la identidad verificada. Devuelve los intentos."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversaciones
               SET intentos_identificacion = intentos_identificacion + 1,
                   identidad_verificada    = identidad_verificada OR %s,
                   actualizada_en          = now()
             WHERE id = %s
            RETURNING intentos_identificacion
            """,
            (verificada, id_conversacion),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0] if fila else 0


# ==========================================================================================
# Cupos -- el control de capacidad
# ==========================================================================================


def tomar_cupo(
    conn,
    *,
    inicio: datetime,
    capacidad: int,
    clave_idempotencia: str,
    conversacion_id: str | None = None,
) -> tuple[int, int] | None:
    """Aparta uno de los cupos de ese horario. Devuelve `(reserva_id, cupo_num)` o `None`.

    `None` significa «ese horario está lleno», y es una respuesta normal, no un error.

    ------------------------------------------------------------------------------------
    Por qué no hay un `SELECT count(*)` en ninguna parte
    ------------------------------------------------------------------------------------

    La forma intuitiva sería contar cuántas reservas hay y comparar con la capacidad. Está
    mal, y de una manera que no se nota en pruebas manuales: entre el `count` y el `INSERT`
    cabe otra transacción haciendo exactamente lo mismo. Dos pacientes preguntan a la vez,
    los dos ven un cupo libre, los dos reservan, y la clínica descubre el choque cuando
    ambos están en la sala de espera.

    Lo que hace esto es intentar tomar cada cupo por número --1, 2, ... hasta la capacidad--
    y dejar que `uq_reservas_cupo UNIQUE (inicio, cupo_num)` decida. Esa decisión la toma
    Postgres, de forma atómica, y no hay ventana donde colarse. `ON CONFLICT DO NOTHING`
    sin objetivo cubre las DOS restricciones únicas de la tabla (el cupo y la clave), así
    que ninguna colisión llega como excepción y la transacción nunca queda abortada.

    La capacidad viene de `configuracion.capacidad_por_hora`, no de una constante: la
    clínica la cambia desde la interfaz web sin tocar código.
    """
    if capacidad < 1:
        raise ValueError("la capacidad por bloque tiene que ser al menos 1")

    with conn.cursor() as cur:
        # 1. ¿Ya se hizo esta misma reserva? La idempotencia va primero: un reintento del
        #    mismo intento no puede consumir un segundo cupo.
        cur.execute(
            "SELECT id, cupo_num FROM reservas WHERE clave_idempotencia = %s",
            (clave_idempotencia,),
        )
        fila = cur.fetchone()
        if fila:
            return (fila[0], fila[1])

        # 2. Tomar el primer cupo libre.
        for cupo_num in range(1, capacidad + 1):
            cur.execute(
                """
                INSERT INTO reservas (inicio, cupo_num, conversacion_id, clave_idempotencia)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id, cupo_num
                """,
                (inicio, cupo_num, conversacion_id, clave_idempotencia),
            )
            fila = cur.fetchone()
            if fila:
                conn.commit()
                return (fila[0], fila[1])

        # 3. Ningún cupo entró. Puede ser que el horario esté lleno, o que una conexión
        #    gemela con la misma clave se nos haya adelantado; lo segundo se distingue
        #    volviendo a mirar por clave.
        cur.execute(
            "SELECT id, cupo_num FROM reservas WHERE clave_idempotencia = %s",
            (clave_idempotencia,),
        )
        fila = cur.fetchone()

    conn.commit()
    return (fila[0], fila[1]) if fila else None


def liberar_cupo(conn, reserva_id: int) -> None:
    """Devuelve el cupo a la disponibilidad.

    Se llama cuando el cupo se tomó pero la cita no llegó a existir --Calendar falló-- y en
    cada reprogramación, con el horario viejo. Un cupo que nadie libera es un horario que la
    clínica pierde sin que nadie sepa por qué.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reservas WHERE id = %s", (reserva_id,))
    conn.commit()


def bloques_ocupados(conn, desde: datetime, hasta: datetime) -> dict[datetime, int]:
    """Cuántos cupos hay tomados en cada bloque de la ventana."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT inicio, count(*) FROM reservas
             WHERE inicio >= %s AND inicio < %s
             GROUP BY inicio
            """,
            (desde, hasta),
        )
        return {fila[0]: fila[1] for fila in cur.fetchall()}


# ==========================================================================================
# Citas
# ==========================================================================================


def registrar_cita(
    conn,
    *,
    reserva_id: int,
    conversacion_id: str,
    paciente_id: int | None,
    nombre_completo: str,
    telefono: str,
    tratamiento: str,
    inicio: datetime,
    duracion_minutos: int,
    evento_calendar_id: str | None,
) -> str:
    """Escribe la cita ya confirmada en los dos lados y devuelve su id."""
    id_cita = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO citas (id, reserva_id, conversacion_id, paciente_id, nombre_completo,
                               telefono, tratamiento, inicio, duracion_minutos,
                               evento_calendar_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                id_cita, reserva_id, conversacion_id, paciente_id, nombre_completo,
                telefono, tratamiento, inicio, duracion_minutos, evento_calendar_id,
            ),
        )
    conn.commit()
    return id_cita


def leer_cita(conn, id_cita: str) -> dict[str, Any] | None:
    """La cita completa, o `None`. Incluye `paciente_id` para comprobar la pertenencia."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, reserva_id, conversacion_id, paciente_id, nombre_completo, telefono,
                   tratamiento, inicio, duracion_minutos, evento_calendar_id, estado
              FROM citas WHERE id = %s
            """,
            (id_cita,),
        )
        fila = cur.fetchone()
    if not fila:
        return None
    columnas = (
        "id", "reserva_id", "conversacion_id", "paciente_id", "nombre_completo", "telefono",
        "tratamiento", "inicio", "duracion_minutos", "evento_calendar_id", "estado",
    )
    return dict(zip(columnas, fila))


def mover_cita(conn, id_cita: str, *, reserva_id: int, inicio: datetime) -> None:
    """Apunta la cita al cupo nuevo. El cupo viejo lo libera quien llama."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas SET reserva_id = %s, inicio = %s, estado = 'reprogramada',
                             actualizada_en = now()
             WHERE id = %s
            """,
            (reserva_id, inicio, id_cita),
        )
    conn.commit()


def marcar_cita_cancelada(conn, id_cita: str, *, motivo: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE citas SET estado = 'cancelada', motivo_cancelacion = %s,
                             actualizada_en = now()
             WHERE id = %s
            """,
            (motivo, id_cita),
        )
    conn.commit()


# ==========================================================================================
# Estado, seguimientos y escalamientos
# ==========================================================================================


def upsert_estado_oportunidad(
    conn,
    id_conversacion: str,
    *,
    estado: str,
    barrera: str,
    tratamiento: str | None = None,
    fuera_de_alcance: bool = False,
    notas: str | None = None,
) -> None:
    """Guarda el estado comercial de la conversación. De esta tabla salen seis de las siete
    métricas de `observabilidad.metricas`, así que un fallo aquí se registra aunque no
    detenga la conversación."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO estado_oportunidad
                (conversacion_id, estado, barrera, tratamiento, fuera_de_alcance, notas)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (conversacion_id) DO UPDATE SET
                estado           = EXCLUDED.estado,
                barrera          = EXCLUDED.barrera,
                tratamiento      = COALESCE(EXCLUDED.tratamiento, estado_oportunidad.tratamiento),
                fuera_de_alcance = EXCLUDED.fuera_de_alcance,
                notas            = COALESCE(EXCLUDED.notas, estado_oportunidad.notas),
                actualizado_en   = now()
            """,
            (id_conversacion, estado, barrera, tratamiento, fuera_de_alcance, notas),
        )
    conn.commit()


def insertar_seguimiento(
    conn,
    *,
    id_conversacion: str,
    tipo: str,
    fecha_objetivo: datetime,
    clave_idempotencia: str,
) -> bool:
    """`True` si quedó programado ahora, `False` si ya existía. Nunca duplica."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seguimientos
                (conversacion_id, tipo, fecha_objetivo, clave_idempotencia)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (clave_idempotencia) DO NOTHING
            RETURNING id
            """,
            (id_conversacion, tipo, fecha_objetivo, clave_idempotencia),
        )
        nuevo = cur.fetchone() is not None
    conn.commit()
    return nuevo


def insertar_escalamiento(
    conn,
    *,
    id_conversacion: str,
    motivo: str,
    resumen: str,
    pregunta: str,
    clave_idempotencia: str,
) -> int | None:
    """El id del escalamiento si es nuevo; `None` si ese turno ya había escalado.

    `None` no es un error: es la defensa contra mandarle al doctor la misma alerta tres
    veces. A la cuarta deja de mirarlas, y así es como muere un sistema de escalamiento.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO escalamientos
                (conversacion_id, motivo, resumen, pregunta, clave_idempotencia)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (clave_idempotencia) DO NOTHING
            RETURNING id
            """,
            (id_conversacion, motivo, resumen, pregunta, clave_idempotencia),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0] if fila else None


def anotar_telegram_en_escalamiento(conn, escalamiento_id: int, message_id: int) -> None:
    """Guarda el mensaje de Telegram para poder editarle el botón cuando alguien lo toque."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE escalamientos SET telegram_message_id = %s WHERE id = %s",
            (message_id, escalamiento_id),
        )
    conn.commit()


# ==========================================================================================
# Usuarios de la interfaz web (migración 006)
# ==========================================================================================
#
# Este módulo guarda el hash tal como se lo den y lo devuelve tal cual: NO importa
# `autenticacion.py` ni sabe qué es un `scrypt$...`. La comprobación de la contraseña vive
# allá, donde se puede probar sin base de datos, y la de aquí se puede probar sin criptografía.


def buscar_usuario(conn, usuario: str) -> dict[str, Any] | None:
    """El usuario por su nombre de ingreso, activo o no, o `None`.

    Devuelve también los inactivos a propósito. Quien llama necesita distinguir «no existe»
    de «existe pero se le quitó el acceso»: lo primero es un nombre mal escrito, lo segundo
    es alguien que se fue de la clínica y sigue intentando entrar -- y eso merece quedar en
    el log aunque al que lo intenta se le responda lo mismo.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, usuario, nombre, hash_contrasena, rol, activo
            FROM usuarios WHERE usuario = %s
            """,
            (usuario.strip().lower(),),
        )
        fila = cur.fetchone()
    if fila is None:
        return None
    return {
        "id": fila[0],
        "usuario": fila[1],
        "nombre": fila[2],
        "hash_contrasena": fila[3],
        "rol": fila[4],
        "activo": fila[5],
    }


def crear_usuario(
    conn, *, usuario: str, nombre: str, hash_contrasena: str, rol: str = "recepcion"
) -> int:
    """Crea el usuario y devuelve su id. Si el nombre ya existe, ACTUALIZA la contraseña.

    El `ON CONFLICT DO UPDATE` es deliberado y es lo que hace que el script de creación sirva
    también para restablecer una contraseña olvidada, que es la operación que de verdad se va
    a necesitar. Sin él haría falta un segundo script para lo mismo.

    No toca `activo`: restablecerle la contraseña a alguien a quien se le quitó el acceso no
    se lo devuelve. Reactivar es una decisión distinta y se toma aparte.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO usuarios (usuario, nombre, hash_contrasena, rol)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (usuario) DO UPDATE SET
                nombre           = EXCLUDED.nombre,
                hash_contrasena  = EXCLUDED.hash_contrasena,
                rol              = EXCLUDED.rol
            RETURNING id
            """,
            (usuario.strip().lower(), nombre.strip(), hash_contrasena, rol),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila[0]


def listar_usuarios(conn) -> list[dict[str, Any]]:
    """Todos, con los inactivos al final. Sin el hash: no hace falta para mostrar una lista,
    y lo que no sale de la base no se puede filtrar por accidente a una respuesta HTTP."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, usuario, nombre, rol, activo, ultimo_acceso_en
            FROM usuarios ORDER BY activo DESC, nombre
            """
        )
        filas = cur.fetchall()
    return [
        {
            "id": f[0],
            "usuario": f[1],
            "nombre": f[2],
            "rol": f[3],
            "activo": f[4],
            "ultimo_acceso_en": f[5],
        }
        for f in filas
    ]


def cambiar_acceso(conn, usuario: str, *, activo: bool) -> bool:
    """Da o quita el acceso. True si el usuario existía.

    Quitarlo tiene efecto en la siguiente petición que haga esa persona -- ver el comentario
    de la columna `activo` en la migración 006.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usuarios SET activo = %s WHERE usuario = %s",
            (activo, usuario.strip().lower()),
        )
        toco = cur.rowcount
    conn.commit()
    return toco > 0


def marcar_acceso(conn, usuario: str) -> None:
    """Sella la hora del último ingreso. Nunca lanza hacia arriba: que no se pueda escribir
    una marca de tiempo no puede impedirle entrar a nadie."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usuarios SET ultimo_acceso_en = now() WHERE usuario = %s",
            (usuario.strip().lower(),),
        )
    conn.commit()

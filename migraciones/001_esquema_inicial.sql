-- =========================================================================================
-- MaxiCare / Daniela -- esquema inicial
-- Fase 1 del plan (docs/agentes/plan-agentes.json)
--
-- Idempotente: se puede correr varias veces sobre la misma base sin romper nada. Eso no es
-- un lujo, es la condición para poder aplicarlo desde un despliegue automático.
--
-- Las tablas del historial de conversación (`agent_sessions`, `agent_messages`) NO están
-- aquí: las crea `SQLAlchemySession` del SDK con `create_tables=True`.
-- =========================================================================================

-- -----------------------------------------------------------------------------------------
-- Pacientes
--
-- `datos.quien_ve_que` del brief: "PROHIBICIÓN EXPRESA DEL CLIENTE: no se registran cédulas
-- ni documentos de identidad de ningún tipo." No hay columna donde ponerlos. La ausencia es
-- la política, igual que en el modelo Pydantic `SolicitudCita`.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pacientes (
    id              BIGSERIAL PRIMARY KEY,
    nombre_completo TEXT        NOT NULL,
    telefono        TEXT        NOT NULL UNIQUE,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------------------
-- Conversaciones
--
-- `tomada_por` es el `if` del relevo (bloque 3 del plan): cuando tiene valor, Daniela guarda
-- silencio total. `ultimo_mensaje_doctor_en` es el reloj del cierre automático: cuenta desde
-- el último mensaje del doctor, NO desde que tomó la conversación, para que un doctor que
-- está conversando activamente nunca pierda el hilo.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversaciones (
    id                        UUID PRIMARY KEY,
    paciente_id               BIGINT      REFERENCES pacientes(id),
    telefono                  TEXT        NOT NULL,
    canal                     TEXT        NOT NULL DEFAULT 'whatsapp'
                                          CHECK (canal IN ('whatsapp', 'web')),
    identidad_verificada      BOOLEAN     NOT NULL DEFAULT FALSE,
    intentos_identificacion   SMALLINT    NOT NULL DEFAULT 0,
    turno_actual              INTEGER     NOT NULL DEFAULT 0,
    tomada_por                TEXT,
    tomada_en                 TIMESTAMPTZ,
    ultimo_mensaje_doctor_en  TIMESTAMPTZ,
    creada_en                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizada_en            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_conversaciones_telefono ON conversaciones (telefono);
CREATE INDEX IF NOT EXISTS ix_conversaciones_tomadas  ON conversaciones (tomada_por)
    WHERE tomada_por IS NOT NULL;

-- -----------------------------------------------------------------------------------------
-- Estado de la oportunidad -- la memoria larga
--
-- `persistencia.compactacion` del plan: el historial crudo se recorta por cantidad, pero
-- esto no se degrada nunca porque son campos y no prosa. Es lo que permite que Daniela
-- retome a una paciente meses después con seis campos en vez de ocho meses de chat.
-- De esta tabla salen seis de las siete métricas del bloque 10.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS estado_oportunidad (
    conversacion_id  UUID PRIMARY KEY REFERENCES conversaciones(id) ON DELETE CASCADE,
    estado           TEXT        NOT NULL DEFAULT 'explorando',
    barrera          TEXT        NOT NULL DEFAULT 'ninguna',
    tratamiento      TEXT,
    fuera_de_alcance BOOLEAN     NOT NULL DEFAULT FALSE,
    notas            TEXT,
    actualizado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------------------
-- Notas de archivos recibidos
--
-- `datos.datos_sensibles`: el archivo NO se almacena. Solo persiste una nota corta y NO
-- clínica de qué llegó y cuándo. `contexto_clinico` viaja a Telegram y muere ahí: no hay
-- columna donde guardarlo, y esa ausencia también es la política.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notas_archivo (
    id              BIGSERIAL PRIMARY KEY,
    conversacion_id UUID        NOT NULL REFERENCES conversaciones(id) ON DELETE CASCADE,
    tipo_documento  TEXT        NOT NULL,
    tratamiento     TEXT        NOT NULL DEFAULT 'no_identificado',
    origen          TEXT,
    fecha_documento DATE,
    entregado_a_telegram BOOLEAN NOT NULL DEFAULT FALSE,
    recibido_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------------------
-- Reservas -- el control de capacidad REAL, no optimista
--
-- `sistemas.lista[Google Calendar].notas`: "Como el tope es 2 y no 1, dos conversaciones
-- simultáneas pueden legítimamente tomar el mismo horario, pero la tercera no: el control
-- de capacidad tiene que ser real y no optimista."
--
-- Google Calendar no sabe contar hasta 2 ni tiene candados. Esta tabla sí: el UNIQUE sobre
-- (inicio, cupo_num) es atómico. Tres INSERT simultáneos sobre el mismo bloque: dos entran
-- y el tercero rebota, sin candados que mantener.
--
-- `cupo_num` se acota con un CHECK al tope máximo imaginable; el tope REAL vigente vive en
-- la tabla `configuracion` y lo aplica el código antes de insertar. El CHECK es la red de
-- seguridad de último recurso, no la regla de negocio.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reservas (
    id                 BIGSERIAL PRIMARY KEY,
    inicio             TIMESTAMPTZ NOT NULL,
    cupo_num           SMALLINT    NOT NULL CHECK (cupo_num BETWEEN 1 AND 10),
    conversacion_id    UUID        REFERENCES conversaciones(id),
    clave_idempotencia TEXT        NOT NULL UNIQUE,
    creada_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_reservas_cupo UNIQUE (inicio, cupo_num)
);

CREATE INDEX IF NOT EXISTS ix_reservas_inicio ON reservas (inicio);

-- -----------------------------------------------------------------------------------------
-- Citas
--
-- `asistio` es NULL hasta que alguien de la clínica lo marque desde la interfaz web. Es la
-- ÚNICA fuente de ese dato: Daniela no sabe qué pasó dentro del consultorio.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS citas (
    id                 UUID PRIMARY KEY,
    reserva_id         BIGINT      REFERENCES reservas(id) ON DELETE SET NULL,
    conversacion_id    UUID        NOT NULL REFERENCES conversaciones(id),
    paciente_id        BIGINT      REFERENCES pacientes(id),
    nombre_completo    TEXT        NOT NULL,
    telefono           TEXT        NOT NULL,
    tratamiento        TEXT        NOT NULL,
    inicio             TIMESTAMPTZ NOT NULL,
    duracion_minutos   SMALLINT    NOT NULL,
    evento_calendar_id TEXT,
    estado             TEXT        NOT NULL DEFAULT 'confirmada'
                                   CHECK (estado IN ('confirmada', 'reprogramada', 'cancelada')),
    asistio            BOOLEAN,
    motivo_cancelacion TEXT,
    creada_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizada_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_citas_inicio   ON citas (inicio);
CREATE INDEX IF NOT EXISTS ix_citas_paciente ON citas (paciente_id);

-- -----------------------------------------------------------------------------------------
-- Seguimientos -- la cola que lee otro proceso
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seguimientos (
    id                 BIGSERIAL PRIMARY KEY,
    conversacion_id    UUID        NOT NULL REFERENCES conversaciones(id) ON DELETE CASCADE,
    tipo               TEXT        NOT NULL,
    fecha_objetivo     TIMESTAMPTZ NOT NULL,
    clave_idempotencia TEXT        NOT NULL UNIQUE,
    enviado_en         TIMESTAMPTZ,
    creado_en          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_seguimientos_pendientes ON seguimientos (fecha_objetivo)
    WHERE enviado_en IS NULL;

-- -----------------------------------------------------------------------------------------
-- Escalamientos
--
-- `clave_idempotencia` = id_conversacion + turno. Sin ella, un reintento tras un fallo de
-- red le manda al doctor la misma alerta tres veces, y a la cuarta deja de mirarlas -- que
-- es exactamente cómo muere un sistema de escalamiento.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS escalamientos (
    id                 BIGSERIAL PRIMARY KEY,
    conversacion_id    UUID        NOT NULL REFERENCES conversaciones(id) ON DELETE CASCADE,
    motivo             TEXT        NOT NULL,
    resumen            TEXT        NOT NULL,
    pregunta           TEXT        NOT NULL,
    clave_idempotencia TEXT        NOT NULL UNIQUE,
    respondido_en      TIMESTAMPTZ,
    respuesta_doctor   TEXT,
    creado_en          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------------------
-- Base de conocimiento -- editable por la clínica desde la interfaz web
--
-- `aprobado = FALSE` es el mecanismo de la sección 4 del documento maestro: hay 10 puntos
-- pendientes de aprobación de MaxiCare. Se cargan, pero marcados, para que Daniela no los
-- comunique como compromiso comercial hasta que alguien los apruebe.
--
-- Que NO exista fila para un (tratamiento, concepto) es un dato en sí mismo: significa
-- "SIN DATO DOCUMENTADO", y el código lo devuelve como tal en vez de devolver vacío. Un
-- resultado vacío es una invitación a que el modelo complete el hueco con algo razonable,
-- y "algo razonable" sobre un precio es el fallo que rompe el primer objetivo del brief.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS base_conocimiento (
    id             BIGSERIAL PRIMARY KEY,
    tratamiento    TEXT        NOT NULL,
    concepto       TEXT        NOT NULL,
    contenido      TEXT        NOT NULL,
    aprobado       BOOLEAN     NOT NULL DEFAULT TRUE,
    nota_pendiente TEXT,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_base_conocimiento UNIQUE (tratamiento, concepto)
);

-- -----------------------------------------------------------------------------------------
-- Configuración operativa -- las tres perillas de la interfaz web
--
-- Vive en la base y no en el código a propósito: la clínica cambia el tope de pacientes por
-- hora sin que nadie despliegue nada.
-- -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS configuracion (
    clave          TEXT PRIMARY KEY,
    valor          TEXT        NOT NULL,
    descripcion    TEXT        NOT NULL,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('capacidad_por_hora',      '2',
     'Cuántos pacientes caben en el mismo bloque horario.'),
    ('duracion_cita_minutos',   '60',
     'Duración del bloque de cita, para cualquier tratamiento.'),
    ('cierre_relevo_minutos',   '180',
     'Minutos sin mensaje de un doctor tras los cuales la conversación que él tomó vuelve sola a Daniela.'),
    ('aviso_relevo_minutos',    '120',
     'Minutos sin mensaje de un doctor tras los cuales se le recuerda por Telegram.')
ON CONFLICT (clave) DO NOTHING;

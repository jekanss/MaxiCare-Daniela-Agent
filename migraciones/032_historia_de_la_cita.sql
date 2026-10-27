-- =========================================================================================
-- La cita deja de olvidar lo que le pasó
--
-- Dos huecos que encontró la auditoría de las 1.085 conversaciones (26/10/2026), los dos con
-- la misma forma: la fila de `citas` guarda el ÚLTIMO estado y nada más.
--
--   1. `mover_cita` escribe `inicio` encima. Después de reprogramar, la hora que el paciente
--      tenía al principio no está en ningún sitio, y «¿cuántas citas se movieron antes de
--      cancelarse?» no tiene respuesta.
--   2. `motivo_cancelacion` es la frase libre del paciente. Sirve para leerla una a una y no
--      para contarla: «no puedo el martes», «me salió un viaje» y «otro día mejor» son la
--      MISMA causa --la fecha, no el tratamiento-- y ninguna consulta las junta.
--
-- `causa_cancelacion` es la lista cerrada que faltaba, y la escribe `cancelar_cita` desde
-- un `Literal` de `contratos.py`: el modelo elige entre valores fijos, no inventa uno. La
-- frase libre se queda donde estaba. Una no sustituye a la otra: la causa es para contar, la
-- frase es para entender.
--
-- `clinica` no está en el `Literal` y sí en el CHECK a propósito: la escribe el código
-- cuando la clínica borra el evento de su calendario (`herramientas._cancelar_porque_ya_no_
-- esta`), y el modelo no tiene por qué poder elegirla.
--
-- `eventos_cita` es la bitácora: una fila por cada cosa que le pasa a una cita, escrita en
-- la MISMA transacción que el cambio (`persistencia._anotar_evento_cita`). Si el cambio se
-- deshace, su fila también. No se rellena hacia atrás: para las citas anteriores a esta
-- migración no hay forma honesta de reconstruir lo que pasó, y una historia inventada es
-- peor que una que empieza hoy.
--
-- `ON DELETE CASCADE` desde `citas` por la misma razón que `agent_messages` desde
-- `agent_sessions`: `/clearstate` borra las citas del número y su historia tiene que irse
-- con ellas, sin un DELETE más que mantener en `borrar_rastro`. Borrar una conversación
-- desde el panel NO borra citas (no negociable 31), así que tampoco borra esto.
--
-- Los dos CHECK van dentro de su `DO $$ ... IF NOT EXISTS` con `conrelid`, aunque hoy nadie
-- los amplíe: ver `.claude/rules/migraciones.md`, la 021 y lo que costó.
-- =========================================================================================

ALTER TABLE citas ADD COLUMN IF NOT EXISTS causa_cancelacion TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_citas_causa_cancelacion'
           AND conrelid = 'citas'::regclass
    ) THEN
        ALTER TABLE citas
            ADD CONSTRAINT ck_citas_causa_cancelacion CHECK (
                causa_cancelacion IS NULL
                OR causa_cancelacion IN (
                    'otra_fecha',    -- quiere ir, pero no ese día u hora
                    'ya_no_quiere',  -- ya no quiere el tratamiento, o lo resolvió en otro lado
                    'salud',         -- un motivo de salud
                    'precio',        -- el costo
                    'otro',          -- dijo por qué, y no es ninguno de los anteriores
                    'no_lo_dijo',    -- no lo dijo, o no quiso decirlo
                    'clinica'        -- la clínica borró el evento de su calendario
                )
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS eventos_cita (
    id                BIGSERIAL   PRIMARY KEY,
    cita_id           UUID        NOT NULL REFERENCES citas(id) ON DELETE CASCADE,
    tipo              TEXT        NOT NULL,
    -- La hora de la cita DESPUÉS del evento. En 'reprogramada', la nueva.
    inicio            TIMESTAMPTZ,
    -- Solo en 'reprogramada': la hora que `mover_cita` está a punto de pisar.
    inicio_anterior   TIMESTAMPTZ,
    -- Solo en 'cancelada'.
    causa_cancelacion TEXT,
    -- Solo en 'asistencia_marcada'. NULL ahí es «se borró la marca».
    asistio           BOOLEAN,
    ocurrido_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_eventos_cita_tipo'
           AND conrelid = 'eventos_cita'::regclass
    ) THEN
        ALTER TABLE eventos_cita
            ADD CONSTRAINT ck_eventos_cita_tipo CHECK (
                tipo IN (
                    'creada',
                    'reprogramada',
                    'cancelada',
                    'confirmada_por_paciente',
                    'asistencia_marcada'
                )
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_eventos_cita_por_cita ON eventos_cita (cita_id, ocurrido_en);

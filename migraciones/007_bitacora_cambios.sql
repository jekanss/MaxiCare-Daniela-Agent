-- =========================================================================================
-- Bitácora de cambios del panel
--
-- Editar un precio desde la interfaz cambia lo que Daniela le cotiza a un paciente real al
-- instante, sin despliegue. Esta tabla es lo único que permite reconstruir qué decía antes
-- y quién lo cambió.
--
-- SIN clave foránea a `usuarios`, a propósito: si mañana alguien borra un usuario, el
-- registro de lo que cambió tiene que sobrevivir. Una bitácora que se borra con quien la
-- escribió no es una bitácora.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS cambios_configuracion (
    id             BIGSERIAL PRIMARY KEY,
    tabla          TEXT NOT NULL
                   CHECK (tabla IN ('base_conocimiento', 'tratamientos', 'configuracion')),

    -- 'implantes/precio' para una ficha, 'carillas' para un tratamiento.
    clave          TEXT NOT NULL,

    -- NULL significa que la fila no existía: fue una creación, no una edición.
    valor_anterior TEXT,
    valor_nuevo    TEXT NOT NULL,

    usuario        TEXT NOT NULL,
    cambiado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cambios_recientes
    ON cambios_configuracion (cambiado_en DESC);

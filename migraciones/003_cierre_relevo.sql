-- =========================================================================================
-- Por qué terminó cada relevo
--
-- Un relevo termina de tres formas, y las tres salen por el mismo camino a propósito: el
-- doctor lo devuelve, se acaba el tiempo, o el tema deja de existir. Que sea un solo camino
-- de salida es lo que impide que quede un estado en el que Daniela calla y nadie escribe.
--
-- Pero aunque el camino sea uno, el MOTIVO importa, y por dos razones distintas:
--
--   · `tiempo_agotado` que se repite significa que los escalamientos se están mandando a
--     doctores que no están disponibles a esa hora.
--   · `tema_perdido` significa que alguien está borrando temas. Es el único motivo que
--     indica un problema de uso y no de disponibilidad, y sin registrarlo es invisible:
--     Telegram no emite ningún evento al borrar un tema.
--
-- El CHECK cerrado es deliberado, igual que el Literal de `contratos.py`: un motivo nuevo
-- tiene que pasar por una migración, no colarse como un string cualquiera.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS relevo_cerrado_en    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS relevo_motivo_cierre TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_conversaciones_motivo_cierre'
    ) THEN
        ALTER TABLE conversaciones
            ADD CONSTRAINT ck_conversaciones_motivo_cierre CHECK (
                relevo_motivo_cierre IS NULL
                OR relevo_motivo_cierre IN (
                    'devuelto_por_doctor',  -- tocó «Listo, que siga Daniela»
                    'tiempo_agotado',       -- se cumplió `cierre_relevo_minutos`
                    'tema_perdido'          -- borraron el tema, o Telegram ya no deja escribir
                )
            );
    END IF;
END $$;

-- Buscar los relevos que terminaron mal es una consulta frecuente del panel; sin el índice
-- parcial habría que recorrer toda la tabla de conversaciones para encontrar unos pocos.
CREATE INDEX IF NOT EXISTS ix_conversaciones_motivo_cierre
    ON conversaciones (relevo_motivo_cierre, relevo_cerrado_en DESC)
    WHERE relevo_motivo_cierre IS NOT NULL;

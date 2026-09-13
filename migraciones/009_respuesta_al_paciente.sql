-- =========================================================================================
-- Si a un paciente se le respondió
--
-- `mensajes_entrantes` solo registra el viaje hacia Telegram (`telegram_message_id`,
-- `reenviado_en`, `fallo`). No dice nada sobre si Daniela -- o un doctor -- le contestó
-- alguna vez a la persona que escribió. El día que Daniela deje a alguien sin respuesta
-- --y va a pasar-- la pregunta «¿le contestamos a esta persona?» no tenía dónde buscarse.
--
-- `conversacion_id` liga el mensaje a la conversación que lo atendió: sin eso, saber qué
-- se le dijo a un paciente exige adivinar por teléfono y ventana de tiempo. `respondido_en`
-- y `wamid_respuesta` son la prueba de que salió una respuesta real; `fallo_respuesta`
-- registra que se intentó y no se pudo, y a propósito NO toca `respondido_en`: si un fallo
-- marcara respondido, la consulta que importa -- a quién no le contestamos -- devolvería
-- vacío justo cuando hace falta.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

ALTER TABLE mensajes_entrantes
    ADD COLUMN IF NOT EXISTS conversacion_id  UUID REFERENCES conversaciones(id),
    ADD COLUMN IF NOT EXISTS respondido_en    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS wamid_respuesta  TEXT,
    ADD COLUMN IF NOT EXISTS fallo_respuesta  TEXT;

-- «¿A quién no le contestamos?» es la consulta que justifica toda esta migración. El
-- predicado es `respondido_en IS NULL` a secas -- no se le exige además
-- `fallo_respuesta IS NOT NULL` -- porque el caso que más duele es el mensaje que nadie
-- intentó contestar porque el proceso se cayó antes de registrar siquiera el fallo: las dos
-- columnas en NULL. Un predicado más estrecho dejaría ese caso invisible. El precedente de
-- esta misma tabla es igual de ancho: `ix_mensajes_sin_reenviar` solo pide
-- `reenviado_en IS NULL`.
--
-- El `DROP INDEX IF EXISTS` de abajo es necesario porque esta migración ya se aplicó una vez
-- en `public` con el predicado angosto (revisión de la fase 6A); sin borrarlo primero, el
-- `CREATE INDEX IF NOT EXISTS` con el mismo nombre no haría nada y el índice viejo -- con el
-- hueco -- se quedaría vigente.
DROP INDEX IF EXISTS ix_mensajes_sin_responder;
CREATE INDEX IF NOT EXISTS ix_mensajes_sin_responder
    ON mensajes_entrantes (recibido_en DESC)
    WHERE respondido_en IS NULL;

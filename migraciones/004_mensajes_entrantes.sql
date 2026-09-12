-- =========================================================================================
-- Mensajes que entran por WhatsApp
--
-- Meta reintenta un webhook que no responde 200 lo bastante rápido, y reintenta también
-- cuando cree que falló aunque no haya fallado. Sin deduplicación, el mismo archivo le
-- llegaría dos o tres veces a los doctores.
--
-- La clave es el `wamid`: el identificador que WhatsApp le da a cada mensaje. Es único y
-- estable entre reintentos, así que es la clave de idempotencia natural — no hay que
-- inventar ninguna.
--
-- Por qué en Postgres y no en memoria: entre el mensaje original y su reintento el proceso
-- se puede reiniciar (un despliegue, un OOM, el VPS). Un `set()` en memoria no recuerda
-- nada después de eso; una fila sí.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS mensajes_entrantes (
    -- El id de WhatsApp. PRIMARY KEY a propósito: el INSERT que no inserta ES la señal de
    -- «esto ya lo procesé», resuelta por el motor y no por una consulta previa que otro
    -- proceso podría adelantar.
    wamid             TEXT PRIMARY KEY,

    telefono          TEXT        NOT NULL,
    nombre_perfil     TEXT,
    tipo              TEXT        NOT NULL,
    texto             TEXT,

    -- Lo que WhatsApp dice del adjunto. `media_id` es lo que se canjea por el enlace
    -- temporal; no se guarda el enlace porque caduca y guardarlo daría una falsa sensación
    -- de que el archivo se puede recuperar después.
    media_id          TEXT,
    mime              TEXT,
    bytes_descargados INTEGER,

    recibido_en       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- La prueba de que el viaje terminó. Si `telegram_message_id` es NULL y `fallo` también,
    -- el mensaje entró y nunca llegó a los doctores: es el estado que hay que vigilar.
    telegram_message_id INTEGER,
    reenviado_en      TIMESTAMPTZ,
    fallo             TEXT
);

-- «¿Qué entró y no llegó?» es la consulta que importa, y son pocas filas entre muchas.
CREATE INDEX IF NOT EXISTS ix_mensajes_sin_reenviar
    ON mensajes_entrantes (recibido_en DESC)
    WHERE reenviado_en IS NULL;

CREATE INDEX IF NOT EXISTS ix_mensajes_telefono
    ON mensajes_entrantes (telefono, recibido_en DESC);

-- =========================================================================================
-- Lo que el doctor le escribe al paciente
--
-- Hasta hoy no se guardaba en ninguna parte. `relevo.relevar_mensaje` tomaba lo que el doctor
-- escribia en el hilo de Telegram, se lo mandaba al paciente por WhatsApp y ahi terminaba el
-- rastro. Era coherente mientras el hilo de Telegram ERA el registro; deja de serlo en cuanto
-- hay un segundo sitio desde el que mirar la conversacion.
--
-- El hilo de un paciente tiene TRES voces y solo dos estaban guardadas:
--
--     paciente  ->  mensajes_entrantes.texto        (migracion 004)
--     Daniela   ->  agent_messages.message_data     (migracion 010, formato del SDK)
--     doctor    ->  ninguna parte                   <- esto
--
-- Sin esta tabla, la pantalla de Conversaciones pinta un hueco justo en el tramo mejor
-- atendido del dia: aquel en el que un humano se metio. Y el hueco no se nota --no hay error,
-- no hay log--, simplemente parece que nadie hablo.
--
-- Lo que esta tabla NO puede hacer: recuperar los relevos anteriores a ella. Esos tramos
-- quedan vacios para siempre. El hilo empieza a estar completo el dia del despliegue.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS mensajes_del_doctor (
    id              BIGSERIAL   PRIMARY KEY,

    -- Va por TELEFONO, como `temas_telegram` (no negociable 16), y no solo por conversacion.
    -- El hilo que se pinta cruza todas las conversaciones de ese numero --la ventana de 24 h
    -- abre una fila nueva cada dia-- asi que colgarlo solo de `conversacion_id` obligaria a un
    -- JOIN en la consulta que mas se hace.
    telefono        TEXT        NOT NULL,

    -- La trazabilidad de en que relevo se dijo. Admite NULL a proposito: el registro de lo que
    -- se le dijo a un paciente no puede depender de que se pueda resolver una conversacion.
    conversacion_id UUID        REFERENCES conversaciones(id),

    -- El nombre de quien escribio: `conversaciones.tomada_por` si vino de Telegram, el nombre
    -- del usuario del panel si vino de ahi. No es un id: los doctores de Telegram no tienen
    -- fila en `usuarios`, y el panel no sabe el id de Telegram de nadie.
    autor           TEXT        NOT NULL,

    origen          TEXT        NOT NULL CHECK (origen IN ('panel', 'telegram')),

    texto           TEXT        NOT NULL,

    -- El id que WhatsApp le dio al mensaje de salida. NULL significa que NO salio, y entonces
    -- `fallo` dice por que. Es el mismo par que ya usan `mensajes_entrantes.telegram_message_id`
    -- y `fallo` para el viaje de ida.
    wamid           TEXT,

    -- La fila se escribe DESPUES de enviar, nunca antes. Guardar primero dejaria constancia de
    -- un mensaje que el paciente no recibio: el doctor lo daria por entregado y el paciente
    -- seguiria esperando. Al reves, lo peor que pasa es que el doctor lo repita. Es el mismo
    -- criterio del no negociable 24 --falsificar una prueba no-- y el contrario al 21, donde
    -- el riesgo era duplicar un recordatorio.
    enviado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),

    fallo           TEXT
);

-- La consulta que justifica la tabla: «el hilo de este numero, lo ultimo primero».
CREATE INDEX IF NOT EXISTS ix_mensajes_del_doctor_telefono
    ON mensajes_del_doctor (telefono, enviado_en DESC);

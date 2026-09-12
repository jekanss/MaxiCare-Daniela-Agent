-- =========================================================================================
-- Temas de Telegram: un hilo por paciente
--
-- El grupo de los doctores es un supergrupo con temas activados. Cada paciente tiene UN
-- tema, no uno por escalamiento: si la misma persona vuelve meses después, es el mismo hilo
-- con todo su historial, que es lo que pide el objetivo 4 del brief.
--
-- El tema vive mientras dure el relevo y se cierra al terminar. Telegram impide escribir en
-- un tema cerrado a quien no es administrador, y esa es la garantía dura del diseño: el
-- doctor no tiene que acordarse de nada, porque cuando no debe escribirle al paciente,
-- sencillamente no puede.
--
-- Idempotente, como la 001.
-- =========================================================================================

-- El tema se ata al PACIENTE, no a la conversación: una persona puede tener varios episodios
-- a lo largo del tiempo y todos comparten hilo.
ALTER TABLE pacientes
    ADD COLUMN IF NOT EXISTS telegram_topic_id  INTEGER,
    ADD COLUMN IF NOT EXISTS telegram_topic_abierto BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pacientes_topic
    ON pacientes (telegram_topic_id)
    WHERE telegram_topic_id IS NOT NULL;

-- Quién activó el relevo y desde qué mensaje, para poder cerrar el ciclo y auditarlo.
ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS relevo_activado_por  TEXT,
    ADD COLUMN IF NOT EXISTS relevo_activado_en   TIMESTAMPTZ;

-- Los escalamientos nacen en el tema General. Guardar el id del mensaje permite editar el
-- botón cuando alguien ya lo tocó, para que otro doctor no vea un botón que ya no aplica.
ALTER TABLE escalamientos
    ADD COLUMN IF NOT EXISTS telegram_message_id  INTEGER,
    ADD COLUMN IF NOT EXISTS relevo_activado      BOOLEAN NOT NULL DEFAULT FALSE;

-- El id del tema General del supergrupo. Telegram lo trata como thread 1 por convención,
-- pero se guarda como configuración para no depender de esa convención.
INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('telegram_topic_general', '1',
     'Id del tema General del supergrupo de doctores: ahí llegan los escalamientos y ahí discuten entre ellos.')
ON CONFLICT (clave) DO NOTHING;

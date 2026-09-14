-- =========================================================================================
-- La cola de recordatorios deja de ser una lista y pasa a ser una cola de verdad
--
-- `seguimientos` existe desde la 001, que ya la llamaba «la cola que lee otro proceso». Ese
-- proceso no existía: `enviado_en` no se escribía en ninguna línea del repositorio. Lo que
-- faltaba para poder escribirlo no era solo el proceso.
--
-- `cita_id` es lo que le faltaba a la tabla para poder decidir. Colgada solo de
-- `conversacion_id`, una fila no sabe de qué cita habla: el paciente reprograma, el
-- recordatorio sale igual, y le recuerda una cita que ya no existe. Es NULLABLE a propósito
-- --`programar_seguimiento` sigue pudiendo encolar «llámenme el lunes», que no cuelga de
-- ninguna cita-- y un seguimiento sin cita se salta las tres primeras guardas.
--
-- `anulado_en` + `motivo_anulacion` en vez de un DELETE: borrar deja al sistema sin poder
-- responder «¿por qué este paciente no recibió recordatorio?», que es la primera pregunta
-- que hace la clínica cuando alguien no llega.
--
-- `intentos` + `fallo`: la fila se marca ANTES de enviar (no hay transacción que cubra una
-- llamada a Meta), así que un fallo no la devuelve a la cola. Se reintenta dentro del mismo
-- ciclo y, si no sale, queda `enviado_en` puesto Y `fallo` con contenido -- que es la señal
-- a vigilar, igual que `reenviado_en` NULL en `mensajes_entrantes`.
--
-- Las dos columnas de `conversaciones` son para que Daniela sepa a qué dice «sí» un paciente
-- que responde a un recordatorio: el mensaje lo mandó un proceso, no una conversación, así
-- que el historial del agente no lo contiene.
--
-- Las perillas son enteros porque `leer_configuracion` hace `int(valor)` sobre todo lo que
-- encuentra. Un valor no entero se descarta en silencio y manda el default del código.
-- =========================================================================================

ALTER TABLE seguimientos
    ADD COLUMN IF NOT EXISTS cita_id          UUID REFERENCES citas(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS anulado_en       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS motivo_anulacion TEXT,
    ADD COLUMN IF NOT EXISTS intentos         SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS fallo            TEXT;

ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS ultimo_recordatorio_tipo TEXT,
    ADD COLUMN IF NOT EXISTS ultimo_recordatorio_en   TIMESTAMPTZ;

-- El de la 001 no conoce `anulado_en`: un seguimiento anulado seguiría entrando en el
-- barrido para que las guardas lo descartaran otra vez, cada sesenta segundos, para siempre.
CREATE INDEX IF NOT EXISTS ix_seguimientos_por_despachar ON seguimientos (fecha_objetivo)
    WHERE enviado_en IS NULL AND anulado_en IS NULL;

INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('hora_recordatorio_vispera', '18',
     'Hora a la que salen los recordatorios de las citas del dia siguiente. Entero, hora de Bogota.'),
    ('horas_minimas_para_recordar', '4',
     'Antelacion minima para que una cita reciba recordatorio. Por debajo, el paciente acaba de hablar con Daniela.')
ON CONFLICT (clave) DO NOTHING;

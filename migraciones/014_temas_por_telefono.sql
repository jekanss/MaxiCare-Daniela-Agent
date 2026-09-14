-- =========================================================================================
-- El hilo de Telegram deja de colgar de `pacientes`
--
-- La migración 002 ató el tema al PACIENTE, y el razonamiento era bueno: «una persona puede
-- tener varios episodios a lo largo del tiempo y todos comparten hilo». Lo que no vio es que
-- la mayoría de la gente que escribe **todavía no es paciente**, y `pacientes` es justo la
-- tabla de la que `atencion._leer_estado` deriva `identidad_verificada`. De ahí que
-- `lectura.asegurar_tema` se negara a crear la fila --con razón-- y que, por tanto, un lead
-- se quedara sin hilo.
--
-- Medido en produccion el 14/09/2026, en el primer relevo real (+57319…2471):
--
--     · sus 5 mensajes de texto quedaron con `telegram_message_id` NULL: no hay hilo donde
--       archivarlos, asi que no llegaron a ninguna parte;
--     · su radiografia, su segunda imagen y la lectura clinica cayeron en el General, que es
--       el escritorio comun de los doctores y se llena de ruido;
--     · al pulsar «Hablar yo con el paciente», el hilo se creo en ese momento y nacio VACIO,
--       porque sus papeles ya estaban en otro sitio.
--
-- El hecho «este telefono tiene este hilo» no depende de ser paciente. Aqui pasa a vivir
-- solo, atado al TELEFONO, que es la misma clave por la que va la pertenencia de una cita
-- (no negociable 13) y por la que `conversacion_viva` busca.
--
-- LO QUE ESTO **NO** CAMBIA: `identidad_verificada` sigue derivando de la EXISTENCIA de una
-- fila en `pacientes`. Tener hilo y estar verificado dejan de ser lo mismo, que es lo que
-- permite darle hilo a un desconocido sin regalarle una identidad. El guardrail
-- `identidad_antes_de_datos` no se toca.
--
-- SE DEJAN CAER LAS DOS COLUMNAS VIEJAS, y es deliberado: el mismo hecho en dos sitios es
-- como nace el bug del mes que viene. Se copia antes, en esta misma migración. Al aplicarla
-- el 14/09/2026 había UNA fila con tema.
--
-- Idempotente, como todas.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS temas_telegram (
    -- El teléfono y no un id: es lo único que se conoce del primer archivo de un desconocido.
    telefono   TEXT        PRIMARY KEY,
    -- UNIQUE porque un mismo hilo no puede ser el expediente de dos personas. Si esto
    -- saltara, alguien estaría depositando la radiografía de uno en el hilo de otro.
    topic_id   INTEGER     NOT NULL UNIQUE,
    -- ¿Está abierto AHORA en Telegram? Es lo que distingue «expediente» de «canal en vivo
    -- hacia el WhatsApp de una persona». Lo mueve el relevo, en `relevo.cerrar`.
    abierto    BOOLEAN     NOT NULL DEFAULT FALSE,
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Los hilos que ya existen, antes de tirar las columnas. `DO NOTHING` para que volver a
-- correr la migración no reviente.
INSERT INTO temas_telegram (telefono, topic_id, abierto)
SELECT p.telefono, p.telegram_topic_id, p.telegram_topic_abierto
  FROM pacientes p
 WHERE p.telegram_topic_id IS NOT NULL
ON CONFLICT (telefono) DO NOTHING;

-- Se lleva por delante `uq_pacientes_topic`, que colgaba de la columna.
ALTER TABLE pacientes
    DROP COLUMN IF EXISTS telegram_topic_id,
    DROP COLUMN IF EXISTS telegram_topic_abierto;

-- =========================================================================================
-- El cierre del relevo deja de ser un botón y pasa a ser una conversación corta
--
-- Al devolver el control, el bot pregunta si quedó agendada una cita y, si la hubo, pide la
-- fecha. Mientras dura esa pregunta el relevo sigue ABIERTO a propósito --Daniela tiene que
-- seguir callada-- y hace falta saber en qué punto va, porque el siguiente mensaje del
-- doctor en el hilo significa cosas distintas: o es para el paciente, o es la fecha.
--
-- En la base y no en memoria: si el proceso reinicia a mitad de la pregunta, un doctor
-- escribiendo «15/09 10:00» se lo encontraría el PACIENTE en su WhatsApp.
--
-- NULL es el caso normal (no hay cierre en curso). El CHECK va cerrado por el mismo criterio
-- que el de la 003: un estado nuevo pasa por una migración, no se cuela como un string.
-- =========================================================================================

ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS cierre_pendiente TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_conversaciones_cierre_pendiente'
           AND conrelid = 'conversaciones'::regclass
    ) THEN
        ALTER TABLE conversaciones
            ADD CONSTRAINT ck_conversaciones_cierre_pendiente CHECK (
                cierre_pendiente IS NULL
                OR cierre_pendiente IN (
                    'preguntado',      -- se le pregunto si agendo; esperando el boton
                    'esperando_fecha'  -- dijo que si; su proximo mensaje es la fecha
                )
            );
    END IF;
END $$;

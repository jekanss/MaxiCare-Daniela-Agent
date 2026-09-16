-- =========================================================================================
-- La persona del teléfono, y lo que ha decidido sobre sus datos
--
-- Hasta aquí el sistema solo sabía de dos cosas: `conversaciones`, que caduca a las 24 h, y
-- `pacientes`, cuya fila solo nace cuando alguien AGENDA (no negociable 12). Quien preguntó
-- y no agendó no era ninguna de las dos: a las 24 horas dejaba de existir. Es justo el grupo
-- que la reactivación quiere volver a buscar.
--
-- `contactos` es esa tercera cosa. Nace con el primer mensaje que entra, no dice nada sobre
-- identidad --tener fila aquí NO es estar verificado, igual que `temas_telegram` (no
-- negociable 16)-- y de ella cuelga el consentimiento.
--
-- Por qué NO se le abre ficha en `pacientes` a todo el que escribe: en este sistema la
-- existencia de esa fila ES la identidad verificada, y de ahí sale `ctx.telefono_sin_paciente`,
-- el permiso que impide que un desconocido mueva o cancele citas ajenas. Si todo el que
-- saluda tuviera ficha, esa distinción desaparecería.
--
-- Por qué NO se guarda en `conversaciones` leyéndolo por teléfono, que era el candidato
-- natural porque ese patrón ya existe (`ultimo_recordatorio_tipo`): `/clearstate` borra las
-- conversaciones, y se llevaría el `no_contactar` con ellas. Un número dado de baja volvería
-- a ser contactable y nadie se enteraría.
--
-- `consentimientos` es la bitácora, y es lo que se enseña si alguien reclama. Nunca se
-- modifica ni se borra: la FK con ON DELETE RESTRICT lo hace imposible aunque alguien lo
-- intente. Una sola tabla con el estado actual perdería justo lo que la ley pide poder
-- acreditar -- si alguien autoriza, se da de baja y vuelve a autorizar, el estado actual solo
-- recuerda lo último.
--
-- Lo que deliberadamente NO está aquí:
--
--   * `canales_autorizados`. Hoy solo existe WhatsApp. Una columna que siempre dice lo mismo
--     es una columna que miente el día que aparezca un segundo canal y nadie la llene.
--   * un campo de «autorizó marketing». El permiso de reactivación es un aviso con derecho a
--     oponerse (decisión D2 de la spec): lo que se captura es la OPOSICIÓN, no el sí. Dos
--     columnas para un solo hecho son dos columnas que acaban contradiciéndose.
--   * cédula o documento de identidad. Prohibición expresa del cliente; la ausencia ES la
--     política, igual que en `pacientes`.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS contactos (
    telefono            TEXT PRIMARY KEY,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = este número nunca ha visto el aviso de la política.
    aviso_mostrado_en   TIMESTAMPTZ,
    politica_version    TEXT,
    -- La baja COMERCIAL. Nunca apaga el recordatorio de una cita: ver la lista blanca de
    -- `seguimientos.TIPOS_NO_COMERCIALES`.
    no_contactar        BOOLEAN NOT NULL DEFAULT FALSE,
    no_contactar_en     TIMESTAMPTZ,
    no_contactar_origen TEXT
);

CREATE TABLE IF NOT EXISTS consentimientos (
    id               BIGSERIAL PRIMARY KEY,
    -- ON DELETE RESTRICT y no CASCADE: borrar un contacto con bitácora tiene que ser
    -- imposible. Es el punto entero de que esta tabla exista.
    telefono         TEXT        NOT NULL REFERENCES contactos(telefono) ON DELETE RESTRICT,
    evento           TEXT        NOT NULL,
    ocurrido_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Congelada en el momento del evento: si la política cambia, hay que poder demostrar
    -- cuál vio cada persona.
    politica_version TEXT,
    origen           TEXT        NOT NULL,
    detalle          TEXT
);

-- `'consentimientos'::regclass` resuelve por el `search_path`, así que apunta a la tabla del
-- esquema en el que se está aplicando la migración y a ninguna otra. Sin eso, aplicarla en
-- `pruebas` iría a comprobar la restricción de `public` y la lista quedaría abierta ahí. Ver
-- la migración 013.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_consentimientos_evento'
           AND conrelid = 'consentimientos'::regclass
    ) THEN
        ALTER TABLE consentimientos
            ADD CONSTRAINT ck_consentimientos_evento CHECK (
                evento IN (
                    'aviso_mostrado',   -- se le enseñó la política, con su versión
                    'baja_solicitada',  -- pidió que no le escribieran más
                    'baja_revocada',    -- pidió volver a recibir
                    'rastro_borrado'    -- /clearstate sobre este número
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_consentimientos_origen'
           AND conrelid = 'consentimientos'::regclass
    ) THEN
        ALTER TABLE consentimientos
            ADD CONSTRAINT ck_consentimientos_origen CHECK (
                origen IN (
                    'codigo',    -- lo hizo el sistema (el aviso)
                    'paciente',  -- lo pidió la persona
                    'clinica'    -- lo hizo alguien de MaxiCare
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_contactos_no_contactar_origen'
           AND conrelid = 'contactos'::regclass
    ) THEN
        ALTER TABLE contactos
            ADD CONSTRAINT ck_contactos_no_contactar_origen CHECK (
                no_contactar_origen IS NULL
                OR no_contactar_origen IN ('paciente', 'clinica')
            );
    END IF;
END $$;

-- La bitácora se lee siempre por teléfono y en orden inverso: «qué ha decidido esta persona».
CREATE INDEX IF NOT EXISTS ix_consentimientos_telefono
    ON consentimientos (telefono, ocurrido_en DESC);

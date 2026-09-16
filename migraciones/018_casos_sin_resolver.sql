-- =========================================================================================
-- Lo que Daniela no pudo resolver deja de perderse
--
-- Habia cinco senales de que un turno no salio bien y ninguna acababa en un sitio revisable.
-- `SIN DATO DOCUMENTADO` no se contaba en ninguna parte. `Resultado.tripwires` moria dentro
-- del proceso, asi que un guardrail que freno a Daniela y se regenero bien --el caso MAS
-- frecuente-- no dejaba rastro alguno. `mensajes_entrantes.fallo_respuesta` se escribia pero
-- solo se consultaba `IS NULL`: el texto del motivo no lo leia nadie. Y los escalamientos
-- llegaban al tema General de Telegram, que es un chat, y los chats se scrollean.
--
-- `huella` es la columna entera. Es lo que hace que doce pacientes preguntando el precio de
-- ortodoncia sean UNA fila con contador 12 y no doce renglones. La arma el codigo, nunca el
-- modelo: con huellas del modelo, dos casos identicos salen distintos y la agrupacion se
-- rompe sin un solo error en el log. Por eso es UNIQUE: el UPSERT es lo que agrupa.
--
-- `ejemplos` e `informe` son TEXT y no JSONB porque este repositorio no tiene un solo JSONB
-- ni un solo adaptador `Json(...)` de psycopg: el JSON va serializado a mano con
-- `json.dumps(..., ensure_ascii=False)`. JSONB seria mejor, pero nadie va a consultar esta
-- tabla por campo interno, y no vale estrenar un adaptador para eso.
--
-- `ejemplos` guarda el telefono junto a la frase por un unico motivo: sin el, `/clearstate`
-- no puede saber cual de las cinco borrar. Nunca sale por el endpoint.
--
-- `informe_sobre` es el contador en el momento del analisis. Es lo que permite re-analizar
-- solo cuando el caso crecio de verdad, en vez de llamar al modelo cada vez que sube uno.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS casos_sin_resolver (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    huella        TEXT        NOT NULL UNIQUE,
    tipo          TEXT        NOT NULL,
    contador      INTEGER     NOT NULL DEFAULT 1,
    escalo        INTEGER     NOT NULL DEFAULT 0,
    primera_vez   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultima_vez    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- [{"texto": "...", "telefono": "+57..."}, ...]  maximo 5
    ejemplos      TEXT        NOT NULL DEFAULT '[]',
    -- {"que_paso": "...", "por_que": "...", "recomiendo": "..."}
    informe       TEXT,
    informe_en    TIMESTAMPTZ,
    informe_sobre INTEGER
);

-- `'casos_sin_resolver'::regclass` resuelve por el `search_path`, asi que apunta a la tabla
-- del esquema en el que se esta aplicando la migracion y a ninguna otra. Sin eso, aplicarla
-- en `pruebas` iria a comprobar la restriccion de `public`. Ver 013.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_casos_sin_resolver_tipo'
           AND conrelid = 'casos_sin_resolver'::regclass
    ) THEN
        ALTER TABLE casos_sin_resolver
            ADD CONSTRAINT ck_casos_sin_resolver_tipo CHECK (
                tipo IN (
                    'FALTA_DATO',  -- la base de conocimiento no tenia la ficha
                    'GUARDRAIL',   -- salto un tripwire, se regenerara bien o no
                    'ROTO',        -- excepcion, limite de turnos, envio fallido
                    'HUMANO'       -- hubo que molestar al doctor
                )
            );
    END IF;
END $$;

-- La pantalla siempre pide la ventana ordenada por frecuencia. Sin este indice, cada carga
-- es un seq scan sobre toda la historia de la clinica para devolver quince filas.
CREATE INDEX IF NOT EXISTS ix_casos_sin_resolver_ventana
    ON casos_sin_resolver (ultima_vez DESC, contador DESC);

-- El que busca la tarea de fondo: los que todavia no tienen informe.
CREATE INDEX IF NOT EXISTS ix_casos_sin_resolver_sin_informe
    ON casos_sin_resolver (primera_vez) WHERE informe IS NULL;

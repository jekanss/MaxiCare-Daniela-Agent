-- =========================================================================================
-- El historial de la conversación, que hasta ahora moría con el proceso
--
-- Estas DOS tablas no son nuestras: son las que `agents.extensions.memory.SQLAlchemySession`
-- espera encontrar. Las columnas, los tipos y el nombre del índice están copiados de la
-- definición del SDK 0.22.2, introspeccionada, no adivinada.
--
-- POR QUÉ UNA MIGRACIÓN Y NO `create_tables=True`, que el SDK ofrece gratis:
-- el esquema completo de este proyecto se lee en `migraciones/`, `inicializar_base.py` es la
-- puerta única, y el proceso web no ejecuta DDL al arrancar. El riesgo del camino elegido
-- --que una versión futura del SDK cambie su esquema y esta migración quede vieja, en
-- silencio-- lo caza `tests/test_sesion_neon.py`, que hace un ida y vuelta REAL contra estas
-- tablas con `create_tables=False`: si el SDK cambia, esa prueba cae.
--
-- `TIMESTAMP` SIN ZONA, a diferencia del resto de este esquema, que usa `TIMESTAMPTZ`.
-- No es un descuido y no se «mejora»: el SDK declara `TIMESTAMP(timezone=False)` y compara
-- contra `CURRENT_TIMESTAMP`. Una columna mejor que la que el SDK escribe es una
-- incompatibilidad esperando.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS agent_sessions (
    -- El `id_conversacion` de `conversaciones`. NO es una clave foránea a propósito: el SDK
    -- declara esta columna como `String` suelta y `pruebas_web` tiene las dos tablas sin
    -- que sus conversaciones vivan en el mismo sitio. La integridad la sostiene quien
    -- escribe, que es siempre `persistencia.sesion_de_agente`.
    session_id  VARCHAR   PRIMARY KEY,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id           SERIAL    PRIMARY KEY,
    session_id   VARCHAR   NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,

    -- Un item del historial, serializado a JSON por el SDK. Un item NO es un mensaje: una
    -- llamada a tool y su resultado son dos filas. Es la diferencia que hace falsa la
    -- equivalencia «40 items = 5 conversaciones» y por la que el límite se mide en vez de
    -- estimarse (ver `config.LIMITE_HISTORIAL_SESION`).
    message_data TEXT      NOT NULL,

    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- El nombre lo fija el SDK: `idx_{messages_table}_session_time`. Con `messages_table` en su
-- valor por defecto, este. Cambiarlo no rompe nada hoy, pero deja de coincidir con lo que
-- crearía `create_tables=True` en una base nueva, y entonces habría dos índices haciendo lo
-- mismo con nombres distintos.
CREATE INDEX IF NOT EXISTS idx_agent_messages_session_time
    ON agent_messages (session_id, created_at);

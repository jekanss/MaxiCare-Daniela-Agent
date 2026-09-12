-- ==========================================================================================
-- 006 · Usuarios de la interfaz web
--
-- Quién puede entrar al panel de MaxiCare. NO son pacientes: son las personas de la clínica.
--
-- ------------------------------------------------------------------------------------------
-- Aquí NO hay ninguna contraseña, y es la decisión más importante de esta migración
-- ------------------------------------------------------------------------------------------
--
-- Un usuario semilla con contraseña dentro del archivo es una credencial pública: vive en el
-- repositorio, viaja en cada copia del proyecto y sobrevive a que alguien la cambie, porque
-- la migración se vuelve a aplicar. El primer usuario se crea con:
--
--     uv run python scripts/crear_usuario.py
--
-- Si esta tabla queda vacía, nadie entra. Eso es correcto: un panel sin usuarios es un panel
-- cerrado, no un panel roto.
--
-- ------------------------------------------------------------------------------------------
-- Tampoco hay cédula, y por la misma razón que en `pacientes`
-- ------------------------------------------------------------------------------------------
--
-- Prohibición expresa de MaxiCare: no se registran documentos de identidad de ningún tipo.
-- La regla se escribió pensando en pacientes, pero no dice «de pacientes»: dice de ningún
-- tipo. La ausencia de la columna ES la política; no le agregues una.
-- ==========================================================================================

CREATE TABLE IF NOT EXISTS usuarios (
    id              BIGSERIAL PRIMARY KEY,

    -- En minúsculas siempre. Quien escribe `Ana` y quien escribe `ana` son la misma persona,
    -- y descubrir lo contrario a las siete de la mañana con un paciente esperando es el peor
    -- momento posible. `scripts/crear_usuario.py` y `runtime.py` normalizan antes de tocar
    -- esta tabla; el CHECK es el cinturón, por si un día alguien inserta a mano.
    usuario         TEXT        NOT NULL UNIQUE CHECK (usuario = lower(usuario)),

    nombre          TEXT        NOT NULL,

    -- El formato `scrypt$n$r$p$sal$hash` que produce `autenticacion.hash_contrasena`. Los
    -- parámetros viajan dentro para que subir el costo mañana no invalide lo de hoy.
    hash_contrasena TEXT        NOT NULL,

    -- `admin` es el único que ve la sección de Usuarios. Los otros dos existen porque la
    -- clínica los nombró así, no porque hoy hagan algo distinto: las diferencias de permiso
    -- entre `doctor` y `recepcion` llegan con las pantallas de la fase 8. Dejarlos escritos
    -- ahora evita una migración de datos después.
    rol             TEXT        NOT NULL DEFAULT 'recepcion'
                                CHECK (rol IN ('admin', 'doctor', 'recepcion')),

    -- La única revocación inmediata que hay. Un token de sesión firmado no se puede anular
    -- desde el servidor antes de que venza -- ver el docstring de `autenticacion.py` --, así
    -- que el corte real de acceso pasa por aquí: `runtime.py` comprueba esta columna en cada
    -- petición. Cuando alguien deja la clínica, esto es lo que se apaga, y tiene efecto en la
    -- siguiente petición que haga.
    activo          BOOLEAN     NOT NULL DEFAULT TRUE,

    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_acceso_en TIMESTAMPTZ
);

-- Solo los activos, que es a quienes se busca en cada petición. Un índice parcial en una
-- tabla de diez filas no cambia nada hoy; está por consistencia con el resto del esquema.
CREATE INDEX IF NOT EXISTS ix_usuarios_activos ON usuarios (usuario) WHERE activo;

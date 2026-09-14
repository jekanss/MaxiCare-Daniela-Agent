-- =========================================================================================
-- El CHECK de `relevo_motivo_cierre` existía en `public` y en ningún otro esquema
--
-- La 003 creó `ck_conversaciones_motivo_cierre` con el guardián que recomienda la regla de
-- migraciones:
--
--     IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '...')
--
-- y ahí está el defecto. **`pg_constraint.conname` NO es único en toda la base**: es único
-- por tabla. Esa consulta no filtra por tabla ni por esquema, así que en cuanto el
-- constraint existió en `public`, el `IF NOT EXISTS` lo encontró desde CUALQUIER otro
-- esquema y se saltó la creación.
--
-- Consecuencia medida el 14/09/2026, al escribir `tests/test_relevo_neon.py`: en
-- `pruebas_relevo` la columna aceptaba `relevo_motivo_cierre = 'porque_si'` sin rechistar.
-- Lo mismo valía para `pruebas`, `pruebas_ingesta` y para `pruebas_web`, que es el esquema
-- del chat del panel y se crea con `aplicar_esquema` en producción.
--
-- Importa por lo que el CHECK ES. La regla dice que una lista cerrada existe «para que un
-- valor nuevo pase por una migración y no se cuele como un string cualquiera»; con el
-- constraint ausente, ninguna prueba contra Neon podía demostrar que la lista sigue cerrada
-- --la única que lo intenta es `test_un_motivo_inventado_lo_rechaza_la_base`-- y el día que
-- alguien lo borrara de `public` por error, la suite habría seguido verde.
--
-- `'conversaciones'::regclass` es lo que arregla el guardián: resuelve por el `search_path`,
-- así que apunta a la tabla del esquema en el que se está aplicando la migración y a
-- ninguna otra. En `public`, donde el constraint ya existe desde la 003, este bloque no
-- hace nada.
--
-- No se edita la 003: ya está aplicada. Regla 2 de `.claude/rules/migraciones.md`.
-- =========================================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_conversaciones_motivo_cierre'
           AND conrelid = 'conversaciones'::regclass
    ) THEN
        ALTER TABLE conversaciones
            ADD CONSTRAINT ck_conversaciones_motivo_cierre CHECK (
                relevo_motivo_cierre IS NULL
                OR relevo_motivo_cierre IN (
                    'devuelto_por_doctor',  -- tocó «Listo, que siga Daniela»
                    'tiempo_agotado',       -- se cumplió `cierre_relevo_minutos`
                    'tema_perdido'          -- borraron el tema, o Telegram ya no deja escribir
                )
            );
    END IF;
END $$;

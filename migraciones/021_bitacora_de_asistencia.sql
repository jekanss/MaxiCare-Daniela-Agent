-- =========================================================================================
-- La bitácora admite `citas`
--
-- La 007 cerró `cambios_configuracion.tabla` en tres valores --`base_conocimiento`,
-- `tratamientos`, `configuracion`-- porque entonces eso era todo lo que el panel podía
-- escribir. La fase 8 añade una cuarta escritura: `citas.asistio`, la marca de asistencia,
-- que es el primer cambio del panel sobre un dato de un PACIENTE y no sobre la configuración
-- de la clínica.
--
-- Tiene que quedar registrado por lo mismo que los otros tres, y con más razón: «no asistió»
-- es una falta que se le apunta a una persona, y la única manera de corregir una marca mal
-- puesta --o de saber quién la puso-- es esta tabla. Sin este valor en el CHECK, `_anotar`
-- revienta con `CheckViolation` y, como el cambio y su registro van en la misma transacción,
-- se cae también el UPDATE: la columna `asistio` sería inescribible desde el panel.
--
-- La lista sigue siendo cerrada a propósito, igual que `relevo_motivo_cierre` (ver
-- `.claude/rules/migraciones.md`): la quinta tabla que quiera bitácora pasa por otra
-- migración y no se cuela como un string cualquiera.
--
-- No se edita la 007: ya está aplicada. Regla 2 de la regla de migraciones.
--
-- Idempotente por construcción: los dos `DROP ... IF EXISTS` cubren los dos nombres que el
-- constraint puede tener --el que Postgres le puso solo al declararlo dentro del `CREATE
-- TABLE` de la 007, y el explícito que deja esta migración-- así que correrla dos veces
-- termina siempre en el mismo sitio. Y como el nombre se resuelve por el `search_path`,
-- cada esquema (`public`, `pruebas`, `pruebas_web`...) arregla el suyo y no el de al lado,
-- que es el defecto que la 013 tuvo que ir a reparar.
-- =========================================================================================

ALTER TABLE cambios_configuracion
    DROP CONSTRAINT IF EXISTS cambios_configuracion_tabla_check;

ALTER TABLE cambios_configuracion
    DROP CONSTRAINT IF EXISTS ck_cambios_configuracion_tabla;

ALTER TABLE cambios_configuracion
    ADD CONSTRAINT ck_cambios_configuracion_tabla CHECK (
        tabla IN (
            'base_conocimiento',  -- una ficha de conocimiento: 'implantes/precio'
            'tratamientos',       -- el vocabulario: 'carillas'
            'configuracion',      -- las perillas operativas
            'citas'               -- la marca de asistencia (fase 8): el id de la cita
        )
    );

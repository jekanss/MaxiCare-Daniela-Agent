-- =========================================================================================
-- 015 -- De que es la cita que el doctor acordo de viva voz
-- =========================================================================================
--
-- EL DEFECTO QUE CIERRA, medido el 14/09/2026 sobre el codigo de la 6D:
--
--   persistencia.registrar_cita(..., tratamiento="valoracion", ...)
--
-- Fijo, a ciegas, en TODAS las citas que salen de un relevo. Y "valoracion" no es ninguna de
-- las catorce claves de `tratamientos`, asi que cada una de esas citas entraba con una
-- categoria que la clinica no tiene: un informe por tratamiento las contaria aparte, y la
-- regla dura 3 del proyecto dice justo lo contrario -- lo que no se sabe no se rellena con
-- un valor plausible, porque una suposicion razonable no se distingue de un hecho verificado.
--
-- Ahora se le PREGUNTA al doctor, y su respuesta se guarda tal cual la escriba. Decision
-- explicita del cliente el 14/09/2026, despues de plantearle que `citas.tratamiento` SI lo
-- lee Daniela --se lo repite al paciente al comprobar su cita-- y que por eso el resto del
-- sistema lo valida contra la lista viva (`herramientas._marcar_estado` rechaza lo que no
-- este). Esta es la unica via del proyecto que acepta texto libre ahi, y es a proposito:
-- el doctor que acaba de hablar con el paciente sabe de que es la cita mejor que un catalogo.
--
-- La columna existe porque el dato llega ANTES que la fecha y hay que sostenerlo mientras se
-- espera: entre las dos preguntas puede reiniciarse el proceso, y en memoria se perderia.
-- Es el mismo motivo por el que `cierre_pendiente` no vive en memoria.

ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS cierre_tratamiento TEXT;

-- El CHECK de la 014 se queda corto con el paso nuevo. Se reemplaza entero en vez de
-- anadirse otro: dos CHECK sobre la misma columna se contradicen callados, y el que gana es
-- el mas restrictivo -- que aqui seria el viejo, o sea el paso nuevo no entraria nunca.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_conversaciones_cierre_pendiente'
           AND conrelid = 'conversaciones'::regclass
    ) THEN
        ALTER TABLE conversaciones
            DROP CONSTRAINT ck_conversaciones_cierre_pendiente;
    END IF;

    ALTER TABLE conversaciones
        ADD CONSTRAINT ck_conversaciones_cierre_pendiente CHECK (
            cierre_pendiente IS NULL
            OR cierre_pendiente IN (
                'preguntado',            -- se le pregunto si agendo; esperando el boton
                'esperando_tratamiento', -- dijo que si; su proximo mensaje es DE QUE es
                'esperando_fecha'        -- ya dijo de que; su proximo mensaje es la fecha
            )
        );
END $$;

-- =========================================================================================
-- 016 -- Como se llama el paciente de la cita que sale de un relevo
-- =========================================================================================
--
-- EL DEFECTO QUE CIERRA, visto el 14/09/2026 leyendo `relevo._agendar`:
--
--   nombre = paciente[1] if paciente else NOMBRE_PENDIENTE
--   calendario.crear_evento(titulo=f"{nombre} - {tratamiento}", ...)
--
-- El numero que llega por primera vez NO tiene ficha, asi que el relevo se la crea con el
-- nombre `PENDIENTE` (`_ficha_para_el_relevo`: la ficha dice «este numero existe», no «esta
-- persona se llama asi»). Correcto mientras nadie sepa como se llama. Deja de serlo en el
-- cierre: ahi el doctor ACABA de hablar con esa persona y sabe su nombre, y sin preguntarselo
-- la cita entraba en la agenda de la clinica como
--
--   PENDIENTE - Cordales
--
-- El paciente nuevo con dolor agudo es justo el que mas escala, o sea que ese es el caso
-- NORMAL de una cita salida de un relevo, no el raro. Y el cliente pidio que la cita quedara
-- «como si el paciente la hubiera hecho»: una que el paciente hace por WhatsApp lleva su
-- nombre, porque Daniela se lo pregunta antes de agendar.
--
-- Solo se pregunta cuando falta. Con ficha y nombre de verdad, el cierre sigue siendo dos
-- preguntas: de que es la cita, y cuando.
--
-- Y el nombre que conteste NO pisa uno que ya exista. `asegurar_paciente` lleva esa regla
-- desde el principio --«si el numero de la casa lo usan dos personas, pisarlo haria que el
-- historial del primero apareciera bajo el nombre del segundo»-- y esta via no es una
-- excepcion: `nombrar_si_esta_pendiente` solo escribe sobre el marcador.

-- El CHECK de la 015 se queda corto con el paso nuevo. Se reemplaza entero y no se anade
-- otro, por lo mismo que decia la 015: dos CHECK sobre la misma columna se contradicen
-- callados y gana el mas restrictivo, o sea el viejo -- el paso nuevo no entraria nunca.
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
                'esperando_nombre',      -- no hay nombre; su proximo mensaje es COMO SE LLAMA
                'esperando_tratamiento', -- su proximo mensaje es DE QUE es la cita
                'esperando_fecha'        -- ya dijo de que; su proximo mensaje es la fecha
            )
        );
END $$;

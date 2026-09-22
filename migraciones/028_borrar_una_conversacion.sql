-- =========================================================================================
-- 028 -- Una conversacion se puede borrar sin llevarse la cita por delante
-- =========================================================================================
--
-- MaxiCare pidio el 22/09/2026 poder borrar conversaciones desde el panel, y eligio
-- explicitamente que borrar la conversacion NO borre las citas: el paciente deja de tener
-- historial con Daniela, pero su cita sigue en pie y sigue en el calendario del doctor.
--
-- Eso era IMPOSIBLE con el esquema de la 001:
--
--     citas.conversacion_id  UUID  NOT NULL REFERENCES conversaciones(id)   -- 001:126
--
-- NOT NULL y sin ON DELETE. Ni cascadea --lo que se llevaria la cita, que es justo lo que no
-- se quiere-- ni se puede desenganchar. `DELETE FROM conversaciones` falla siempre que ese
-- numero tenga una sola cita. El unico borrado que existia, `/clearstate`, lo esquivaba
-- borrando tambien las citas, que para un reseteo a primer contacto es lo correcto.
--
-- --- Por que DROP NOT NULL y no ON DELETE SET NULL ------------------------------------
--
-- La tentacion es cambiar la clave foranea a `ON DELETE SET NULL` y dejar que la base
-- desenganche sola. Se descarto: la cascada implicita es exactamente lo que hace que un
-- `DELETE FROM conversaciones` escrito en otro sitio --dentro de un anio, por alguien que no
-- leyo esto-- desenganche citas en silencio y sin que nadie lo decida.
--
-- Sin ON DELETE, la restriccion sigue siendo un muro: quien borre una conversacion tiene que
-- desenganchar sus citas A MANO y a proposito, con un UPDATE visible en el codigo
-- (`persistencia.borrar_conversacion`), o la base se lo impide con un error ruidoso. Se
-- afloja lo minimo: que la columna ADMITA el NULL. Quien lo pone sigue siendo una persona.
--
-- Una cita sin conversacion no pierde nada de lo que hace falta para atenderla: la tabla
-- guarda `nombre_completo` y `telefono` por su cuenta desde la 001, y `paciente_id` --que
-- esta fila conserva-- es lo que le permite al paciente mover o cancelar lo suyo.
--
-- --- Lo que esto SI se lleva, y esta decidido ------------------------------------------
--
-- `seguimientos.conversacion_id` es NOT NULL con ON DELETE CASCADE (001:150), asi que al
-- caer la conversacion se van los recordatorios de la cita que se conserva. No se afloja
-- tambien esa columna: un seguimiento sin conversacion es una fila que el despachador no
-- encuentra --hace JOIN con `conversaciones`-- y una fila zombi es peor que ninguna.
--
-- La consecuencia se dice donde se decide: la ventana de confirmacion del panel cuenta las
-- citas futuras y avisa de que su recordatorio se va con la conversacion. Un dato que se
-- pierde en silencio es el defecto; perderlo habiendolo dicho es una decision.
--
-- Igual desaparecen, por sus propias cascadas de la 001: `estado_oportunidad`,
-- `notas_archivo` y `escalamientos`. Todos son del hilo, no del paciente.
-- =========================================================================================

ALTER TABLE citas
    ALTER COLUMN conversacion_id DROP NOT NULL;

COMMENT ON COLUMN citas.conversacion_id IS
    'La conversacion en la que se agendo, o NULL si esa conversacion se borro desde el '
    'panel (028). NULL significa "ya no hay hilo", nunca "no se sabe de quien es": para eso '
    'estan telefono y paciente_id, que esta fila conserva siempre.';

-- =========================================================================================
-- Y borrar la conversacion de un paciente deja rastro de quien lo hizo
-- =========================================================================================
--
-- Misma razon que la 021, que abrio esta lista para la marca de asistencia: es un cambio del
-- panel sobre el dato de un PACIENTE y no sobre la configuracion de la clinica. Con mas
-- razon todavia, porque este no se puede deshacer -- la 021 al menos deja corregir una marca
-- mal puesta, y aqui lo unico que va a quedar es esta fila.
--
-- La lista sigue cerrada a proposito (ver `.claude/rules/migraciones.md`): la sexta tabla
-- que quiera bitacora pasa por otra migracion y no se cuela como un string cualquiera.
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
            'citas',              -- la marca de asistencia (fase 8): el id de la cita
            'conversaciones'      -- el borrado de un hilo (028): el telefono
        )
    );

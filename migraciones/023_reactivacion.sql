-- =========================================================================================
-- 021 · Reactivacion de leads: el vocabulario de `tipo`, y el freno por persona
--
-- Cierra el portillo que `seguimientos.py` documenta desde la 017: `tipo` es TEXT libre, lo
-- escribe el MODELO desde `programar_seguimiento`, y la guarda de la baja (G0) solo se aplica
-- a lo que NO esta en `TIPOS_NO_COMERCIALES`. Un `tipo='recordatorio_cita'` inventado por el
-- modelo atraviesa esa guarda con el paciente de baja. Hoy no da a ninguna parte porque el
-- despachador anula sin `cita_id` con motivo `sin_plantilla`; en cuanto haya plantillas para
-- tipos sin cita, ese portillo se abre de par en par y el resultado es un mensaje comercial a
-- quien pidio que no le escribieran. Eso es un reporte, y un numero reportado se lleva por
-- delante tambien los recordatorios de cita y los escalamientos.
--
-- El CHECK va NOT VALID a proposito: valida lo que entre de ahora en adelante y no mira las
-- filas viejas. Un CHECK normal reventaria el despliegue si en produccion hay un `tipo` que
-- el modelo invento algun dia, y tumbar el arranque entero por una fila historica es peor que
-- dejarla sin validar: esa fila ya no se va a enviar --o esta enviada, o se anula por
-- `sin_plantilla`-- y lo que hay que proteger es lo que viene.
--
-- `reactivacion_no_asistio` entra en la lista aunque NADIE la vaya a encolar: `citas.asistio`
-- sigue sin escribirse (la pantalla de agenda es cascaron, `web/src/pantallas/Agenda.tsx:50`).
-- Que quede declarada y muerta es deliberado: el dia que la fase 8 escriba `asistio`,
-- encenderla es cambiar una constante y no volver a tocar la base.
--
-- El contador de `contactos` es del SISTEMA, no de la persona: dice «a este numero no le sirve
-- que lo persigamos», no «no me escriban mas». Por eso vive aqui y no en `consentimientos`, y
-- por eso `/clearstate` SI lo resetea --al reves que `no_contactar`, que es un derecho y no se
-- toca nunca-.
-- =========================================================================================

-- El vocabulario cerrado. NOT VALID: solo para lo que entre a partir de ahora.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_seguimientos_tipo'
           AND conrelid = 'seguimientos'::regclass
    ) THEN
        ALTER TABLE seguimientos
            ADD CONSTRAINT ck_seguimientos_tipo CHECK (
                tipo IN (
                    'recordatorio_cita',          -- el unico NO comercial
                    'reactivacion_sin_agendar',   -- pregunto y no agendo
                    'reactivacion_cancelada',     -- cancelo y no volvio
                    'reactivacion_no_asistio'     -- declarada; sin disparador hasta la fase 8
                )
            ) NOT VALID;
    END IF;
END $$;

-- El freno por persona. `seguimientos_fallidos` sube cuando una serie termina en un «no» o en
-- silencio, y vuelve a 0 al agendar: alguien que ignoro dos veces y al final vino demostro lo
-- contrario de lo que el contador supone.
ALTER TABLE contactos
    ADD COLUMN IF NOT EXISTS seguimientos_fallidos SMALLINT     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ultimo_seguimiento_en TIMESTAMPTZ;

-- La marca que impide contar dos veces la misma serie. Sin ella, cada pasada del barrido
-- sumaria uno mas por la misma serie cerrada y el freno se dispararia el primer dia.
ALTER TABLE seguimientos
    ADD COLUMN IF NOT EXISTS contabilizado_en TIMESTAMPTZ;

-- El tope anual (regla 8) se cuenta sobre los enviados de reactivacion. Sin este indice, la
-- subconsulta del despachador recorre la tabla entera por cada una de las 50 filas de la tanda.
CREATE INDEX IF NOT EXISTS ix_seguimientos_enviados_de_reactivacion
    ON seguimientos (tipo, enviado_en)
    WHERE enviado_en IS NOT NULL;

-- Series completas que el barrido todavia no ha contabilizado.
CREATE INDEX IF NOT EXISTS ix_seguimientos_sin_contabilizar
    ON seguimientos (enviado_en)
    WHERE enviado_en IS NOT NULL AND contabilizado_en IS NULL;

-- Las tres perillas, en `configuracion` y no en el `.env`: la clinica las cambia desde el panel
-- sin desplegar, igual que la jornada. Son TEXT porque la tabla guarda TEXT y
-- `leer_configuracion` hace `int(valor)`.
INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('tope_diario_reactivacion', '20',
     'Cuantos mensajes de reactivacion como mucho por dia. Arranque lento: Meta castiga los picos.'),
    ('max_reactivaciones_12m', '6',
     'Tope por PERSONA en 12 meses, pase lo que pase. El limite de 2 por consulta protege la consulta, no a la persona.'),
    ('max_seguimientos_fallidos', '2',
     'Cuantas series seguidas sin exito antes de dejar de perseguir a ese numero. Se resetea al agendar.')
ON CONFLICT (clave) DO NOTHING;

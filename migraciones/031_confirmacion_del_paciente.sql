-- =========================================================================================
-- Que el paciente haya confirmado su cita deja de ser invisible
--
-- `citas.estado` arranca en 'confirmada' por DEFAULT desde la 001, así que esa palabra nunca
-- significó que el paciente dijera nada: significa que la cita existe y que no está
-- cancelada. Hasta hoy no había nadie diciéndolo -- los dos quick replies que Meta aprobó en
-- `recordatorio_cita` el 20/09/2026 entraban al turno como texto y se perdían ahí -- y desde
-- que el recordatorio lleva tres botones sí lo hay.
--
-- NULLABLE y sin default a propósito. `NULL` es «no ha dicho nada», que es distinto de «dijo
-- que no» -- eso último ya es `estado = 'cancelada'` con su `motivo_cancelacion`. Un BOOLEAN
-- con default `false` haría esas dos cosas indistinguibles, y son justamente la diferencia
-- entre llamar a un paciente y no llamarlo: al que no ha contestado se le llama, al que
-- canceló no.
--
-- TIMESTAMPTZ y no BOOLEAN por lo mismo que `enviado_en` o `respondido_en`: la hora a la que
-- confirmó es la mitad del dato. «Confirmó ayer a las 18:05» y «confirmó hace diez minutos»
-- valen distinto para quien mira la agenda de mañana.
--
-- **Es también la deduplicación del aviso al doctor.** `marcar_cita_confirmada` actualiza con
-- `WHERE confirmada_por_paciente_en IS NULL` y devuelve el `rowcount`: Meta reintenta los
-- webhooks y un paciente impaciente pulsa el botón dos veces, y sin esa guarda cada toque
-- costaría tres WhatsApps de plantilla a tres personas por la misma cita. Quien decide es
-- Postgres y no un `if` que lea antes de escribir, por lo mismo que en `toca_avisar_cuota`:
-- dos webhooks simultáneos pasarían los dos por esa lectura.
-- =========================================================================================

ALTER TABLE citas ADD COLUMN IF NOT EXISTS confirmada_por_paciente_en TIMESTAMPTZ;

-- Y el índice que necesita la consulta nueva. `cita_del_ultimo_recordatorio` une
-- `seguimientos` con `citas` por `cita_id` para saber de qué cita habla el botón, y
-- `seguimientos` no tenía ni un índice sobre esa columna: los tres que hay (la 017 y los dos
-- de la 023) van por `fecha_objetivo` y por `enviado_en`, que es lo que necesita el
-- despachador. Sin este, cada pulsación de «Confirmar» recorre la tabla entera.
--
-- Hoy no se notaría --la tabla es pequeña-- y por eso se pone ahora: crece una fila por cita
-- y otra por reactivación, así que el día que se note ya será tarde y el síntoma será «los
-- botones tardan», que nadie va a atribuir a esto.
CREATE INDEX IF NOT EXISTS ix_seguimientos_por_cita ON seguimientos (cita_id)
    WHERE cita_id IS NOT NULL;

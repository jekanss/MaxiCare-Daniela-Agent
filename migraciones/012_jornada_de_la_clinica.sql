-- =========================================================================================
-- El horario en que la clínica atiende
--
-- Hasta el 13/09/2026 el sistema no tenía ningún concepto de jornada. La rejilla de
-- `consultar_disponibilidad` salía tal cual de la ventana que el modelo pidiera, así que la
-- ventana ERA la oferta.
--
-- Se vio en producción a las 6:22 p. m. El paciente pidió cita «el próximo martes 15» y el
-- modelo consultó el día completo -- leído de `public.agent_messages`, literal:
--
--     {"desde":"2026-09-15T00:00:00-05:00","hasta":"2026-09-15T23:59:59-05:00"}
--
-- La tool devolvió los tres primeros bloques de esa ventana y Daniela ofreció «12:00 am,
-- 1:00 am o 2:00 am», que es la traducción CORRECTA de 00:00, 01:00 y 02:00. El modelo no
-- inventó nada: el sistema le dio esas horas.
--
-- Los valores son los del documento aprobado por MaxiCare, que ya vivía en la base de
-- conocimiento como TEXTO (`_general` / `horario`): «Lunes a viernes de 8:00 am a 5:00 pm.
-- Sábados de 8:00 am a 3:00 pm.» El domingo cerrado es lo que ese texto dice por omisión, y
-- MaxiCare lo confirmó.
--
-- OJO, y es el precio de tener el horario en dos formatos: estas filas FILTRAN, y la fila de
-- la base de conocimiento es la que Daniela RECITA cuando le preguntan. Quien cambie una
-- tiene que cambiar la otra, o dirá una cosa y ofrecerá otra.
--
-- `atiende_domingo` es 0/1 y no un booleano porque la columna `valor` es TEXT y
-- `leer_configuracion` hace `int(valor)` sobre todo lo que encuentra: un 'false' ahí dentro
-- se descartaría en silencio y el default del código mandaría en su lugar.
--
-- Idempotente, como las anteriores: `ON CONFLICT DO NOTHING` respeta lo que la clínica ya
-- haya cambiado.
-- =========================================================================================

INSERT INTO configuracion (clave, valor, descripcion) VALUES
    ('hora_apertura',      '8',
     'Hora a la que la clínica abre, todos los días que atiende. Entero, hora de Bogotá.'),
    ('hora_cierre',        '17',
     'Hora a la que cierra de lunes a viernes. Una cita tiene que caber ENTERA antes.'),
    ('hora_cierre_sabado', '15',
     'Hora a la que cierra los sábados, que es más temprano que entre semana.'),
    ('atiende_domingo',    '0',
     'Si la clínica abre los domingos. 0 = no. Si se pone en 1, usa la hora de cierre de entre semana.')
ON CONFLICT (clave) DO NOTHING;

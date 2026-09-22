-- =========================================================================================
-- Lo que el paciente DIJO en una nota de voz
--
-- Desde el 21/09/2026 una nota de voz se transcribe y entra al turno como texto del paciente
-- (no negociable 29). El texto se le pasa a Daniela, se le manda al doctor por Telegram... y
-- se tira. `mensajes_entrantes.texto` de un audio se queda en NULL para siempre.
--
-- La consecuencia se ve en la pantalla de Conversaciones: el hilo pinta la cadena literal
-- «(nota de voz)» --la pone `panel._MARCA_POR_TIPO`-- porque es lo unico que tiene. Al 22 de
-- septiembre de 2026 los SEIS audios que ha visto el sistema en su vida estan asi: seis
-- huecos donde un paciente dijo algo que el sistema entendio, contesto y despues olvido.
--
-- Y hay un segundo sitio donde dolia mas, porque ahi el destinatario es un humano decidiendo:
-- `persistencia.transcripcion` --el volcado que recibe un doctor al TOMAR la conversacion--
-- filtra `texto IS NOT NULL AND texto <> ''`, asi que las notas de voz desaparecian enteras
-- de ese resumen. Ni siquiera salian como «(nota de voz)». El doctor entraba a conversar sin
-- saber que el paciente habia hablado.
--
-- POR QUE UNA COLUMNA NUEVA Y NO `texto`:
--
--     texto         -> lo que el paciente ESCRIBIO (en un audio, el caption, casi siempre NULL)
--     transcripcion -> lo que el paciente DIJO, segun una maquina
--
-- Son cosas distintas y la diferencia importa en una clinica: una transcripcion puede
-- equivocarse --«el 46» y «el 40» suenan casi igual-- y quien la lee tiene que saber que esta
-- leyendo una maquina y no a una persona. Escribirla en `texto` haria esa distincion
-- imposible de recuperar, y ademas invertiria el significado de `texto IS NULL`, que hoy es
-- lo que separa un mensaje con palabras de un archivo.
--
-- Lo que esta migracion NO puede hacer: recuperar los seis audios anteriores a ella. Los
-- bytes vivian en memoria durante el turno y ya no existen. Esos seis siguen diciendo
-- «(nota de voz)» para siempre, igual que los relevos anteriores a la 026.
--
-- Idempotente, como las anteriores.
-- =========================================================================================

ALTER TABLE mensajes_entrantes
    ADD COLUMN IF NOT EXISTS transcripcion TEXT;

COMMENT ON COLUMN mensajes_entrantes.transcripcion IS
    'Lo que se entendio de una nota de voz. NULL significa que no se transcribio: puede ser '
    'que no fuera audio, que el transcriptor estuviera apagado, que la cuota cortara o que '
    'no se entendiera nada. Se escribe DESPUES de repartirla, y su fallo no tumba el turno.';

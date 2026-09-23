-- =========================================================================================
-- Un hilo borrado deja LÁPIDA, y por eso no lo resucita el siguiente mensaje del paciente
--
-- Hasta hoy, `persistencia.olvidar_tema` hacía `DELETE FROM temas_telegram`. La recuperación
-- era correcta para lo que se pedía entonces --«que el siguiente archivo le abra un hilo
-- nuevo»-- y borraba a la vez el único dato que distingue dos situaciones que NO son la
-- misma:
--
--     sin fila  ->  este número nunca tuvo hilo. Su primer archivo lo abre.
--     sin fila  ->  a este número le BORRARON el hilo. Su siguiente archivo lo abre otra vez.
--
-- El segundo caso es el que MaxiCare midió el 22/09/2026 sobre +57319…2471 y describió como
-- «cierro el tema y sus mensajes siguen llegando al General»:
--
--     18:21:09  un doctor pulsa «Hablar yo con el paciente»
--     18:21:11  `relevo.activar` no consigue rehacer el hilo -> cierra con `tema_perdido`
--               y la fila de `temas_telegram` desaparece
--     19:13:25  el paciente manda una nota de voz -> sin hilo, `ingesta` la mandaba al
--               General **sonando**, porque el General era el destino de reserva
--
-- Las dos mitades del arreglo son independientes y hacen falta las dos. La otra --que el
-- General deje de ser destino de nada que mande un paciente-- vive en `ingesta.py`. Esta es
-- la que impide que la llegada de un mensaje vuelva a abrir un hilo que un doctor cerró a
-- propósito: quien decide que hay que volver a molestar a un humano es el ESCALAMIENTO, no
-- el paciente escribiendo.
--
-- POR QUÉ UNA COLUMNA Y NO SEGUIR BORRANDO LA FILA
--
-- Porque la ausencia de fila ya significa otra cosa, y significarla bien importa: es lo que
-- hace que el primer archivo de alguien que escribe por primera vez le abra su expediente.
-- Con la lápida, `tema_del_paciente` sigue devolviendo `NULL` --nadie que lea el hilo tiene
-- que enterarse de esto-- y quien necesita la diferencia la pide aparte, con
-- `persistencia.hilo_perdido`.
--
-- `/clearstate` SÍ borra la fila entera, y eso también es correcto: resetear un número a
-- primer contacto es exactamente devolverlo al primer caso de la tabla de arriba.
--
-- Idempotente, como todas. `ADD COLUMN IF NOT EXISTS` no toca las filas que ya están, así
-- que los hilos vivos siguen vivos: `perdido_en` nace en NULL, que es «este hilo está bien».
-- =========================================================================================

ALTER TABLE temas_telegram
    ADD COLUMN IF NOT EXISTS perdido_en TIMESTAMPTZ;

-- El índice que usa la consulta normal. Parcial porque la pregunta que se hace el sistema
-- mil veces al día es «¿tiene hilo USABLE?», y las lápidas no son parte de esa respuesta.
CREATE INDEX IF NOT EXISTS ix_temas_telegram_vivos
    ON temas_telegram (telefono)
    WHERE perdido_en IS NULL;

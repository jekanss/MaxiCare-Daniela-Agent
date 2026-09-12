-- =========================================================================================
-- El tema General se escribe OMITIENDO el thread, no con el thread 1
--
-- La migración 002 guardó `telegram_topic_general = 1` con esta nota: «Telegram lo trata
-- como thread 1 por convención, pero se guarda como configuración para no depender de esa
-- convención». Haber guardado el valor en vez de codificarlo fue lo correcto; la convención
-- resultó ser falsa, y arreglarlo es cambiar una fila.
--
-- Lo comprobado contra la API en la fase 2: Telegram identifica el General como thread 1
-- cuando ENTREGA un mensaje, pero `sendMessage` con `message_thread_id=1` responde
-- `Bad Request: message thread not found`. Al General se escribe sin el campo.
--
-- El 0 significa exactamente eso: «el General, sin message_thread_id». `canales.py` usa
-- `if tema_id:` para que el 0 haga que se omita.
--
-- Por qué importa que este fallo se haya visto ahora: era silencioso en el peor momento.
-- El 200 ya se le devolvió a Meta, WhatsApp da el mensaje por entregado, y el archivo no
-- llegó a nadie. Lo único que lo delataba era la columna `fallo` de `mensajes_entrantes` —
-- razón de más para que esa columna exista.
--
-- Idempotente: fija el valor sin depender de cuál estuviera antes.
-- =========================================================================================

UPDATE configuracion
   SET valor = '0',
       descripcion = 'Tema del supergrupo donde caen los escalamientos. 0 = el tema General '
                     '(se envía omitiendo message_thread_id: Telegram rechaza el thread 1 '
                     'al escribir, aunque lo use al entregar).'
 WHERE clave = 'telegram_topic_general';

INSERT INTO configuracion (clave, valor, descripcion)
SELECT 'telegram_topic_general', '0',
       'Tema del supergrupo donde caen los escalamientos. 0 = el tema General.'
WHERE NOT EXISTS (SELECT 1 FROM configuracion WHERE clave = 'telegram_topic_general');

-- =========================================================================================
-- 023 · La descripcion de `max_seguimientos_fallidos` decia "series" y el codigo cuenta
-- envios
--
-- Ronda 2 de revision sobre el barrido de reactivacion (I-5, revertido). La ronda 1 intento
-- separar "cuantos envios caben dentro de una serie" de "cuantas series tolera una persona",
-- con un contador propio por serie. Verificado por el revisor: ese diseno no contaba series
-- de verdad -una serie normal de dos envios subia el contador DOS veces, no una-, y subir
-- esta perilla de 2 a 3 no daba "tres series" sino tres mensajes sueltos con el contador en
-- 3. La razon de fondo: en este sistema no existe ningun punto donde nazca una "serie" nueva
-- -ninguna tabla, columna ni evento la agrupa-, asi que la unidad real siempre fue el envio.
--
-- Se revirtio en el codigo (`persistencia.series_por_contabilizar` ya no usa DISTINCT ON;
-- cuenta cada envio de reactivacion que no acabo en cita, uno por uno) y esta migracion
-- corrige el ROTULO para que diga lo que la perilla de verdad hace: cuantos ENVIOS de
-- reactivacion tolera una persona antes de dejar de perseguirla, no cuantas series. Una
-- perilla cuya descripcion no coincide con su efecto es exactamente el tipo de cosa con la
-- que este proyecto ya ha tropezado -- ver el no negociable sobre las columnas de la 010, o
-- la migracion 019 sobre `no_contactar` frente al aviso.
--
-- `INTENTOS_POR_SERIE_DE_REACTIVACION` (en `persistencia.py`) sigue existiendo como defensa
-- en profundidad dentro del filtro de cartera, pero NO es la misma cota que esta perilla --
-- ronda 3 de revision, corrigiendo una afirmacion de la ronda 2 que el revisor senalo como
-- falsa. Son dos cotas DISTINTAS que se solapan: esta perilla es POR PERSONA y cuenta lo YA
-- CONTABILIZADO; aquella es POR TIPO y cuenta lo SIN CONTABILIZAR TODAVIA (la guarda contra
-- un envio de mas mientras el barrido no se ha enterado). No tiene fila propia en
-- `configuracion` porque no es una perilla independiente, pero las dos constantes NO tienen
-- que moverse juntas si esta cambia de valor por defecto -- ver el docstring de
-- `INTENTOS_POR_SERIE_DE_REACTIVACION` para las dos diferencias completas.
--
-- Un UPDATE simple es idempotente por construccion: aplicarlo dos veces dejo el mismo texto.
-- =========================================================================================

UPDATE configuracion
   SET descripcion = 'Cuantos mensajes de reactivacion como mucho por persona, antes de '
                      'dejar de perseguirla. Se resetea al agendar.'
 WHERE clave = 'max_seguimientos_fallidos';

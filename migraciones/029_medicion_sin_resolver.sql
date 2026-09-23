-- =========================================================================================
-- La medicion de «sin resolver» empieza de cero
--
-- Los doce casos que habia el 22/09/2026 eran de PRUEBAS: los dejaron las conversaciones con
-- las que se fue armando el sistema, y varios los produjo el propio equipo probando --26
-- relevos, nueve disparos del guardrail de uso indebido--. Mezclados con los reales no se
-- puede decir «doce personas preguntaron por el precio de la valoracion», que es la unica
-- frase para la que existe esta pantalla. MaxiCare pidio borrarlos y empezar a contar.
--
-- POR QUE ESTE DELETE NO SE LLEVA POR DELANTE LOS CASOS DE VERDAD EN CADA ARRANQUE
--
-- `aplicar_esquema` corre TODAS las migraciones en cada arranque del contenedor, asi que un
-- `DELETE FROM casos_sin_resolver` a pelo aqui borraria la medicion entera en el siguiente
-- despliegue, en silencio y sin que nada fallara. Es el modo de fallo de la 021 --ver
-- `.claude/rules/migraciones.md`-- pero peor: aquel abortaba ruidosamente y este no dejaria
-- ni un error.
--
-- La guarda es la MARCA: el borrado ocurre solo si `medicion_sin_resolver_desde` todavia no
-- existe, y lo primero que hace es escribirla. O sea que corre una vez en la vida de cada
-- esquema, y a partir de ahi esta migracion no hace nada. La marca y el borrado van en el
-- MISMO bloque a proposito: si se escribiera la marca sin borrar, o al reves, el siguiente
-- arranque veria un estado que esta migracion no sabe interpretar.
--
-- QUE ES LA MARCA, ademas de la guarda
--
-- Es la fecha desde la que los numeros de esta pantalla significan algo, y se ensena: sin
-- ella, «3 veces» no dice si son tres veces en dos dias o en dos meses. Vive en
-- `configuracion` y no en una tabla propia porque es exactamente lo que esa tabla es --un
-- dato de operacion que el codigo lee y que no cabe en una constante-- y porque
-- `leer_configuracion` se salta sola los valores que no son enteros (su `except ... continue`),
-- asi que una marca de tiempo ahi no rompe a nadie.
--
-- Se guarda en ISO 8601 UTC con la `Z` dentro: es lo que `new Date(...)` del navegador sabe
-- leer sin ambiguedad. `now()::text` daria `2026-09-22 20:15:30.12+00`, que Safari no parsea.
--
-- LO QUE NO SE TOCA: ni una conversacion, ni un mensaje, ni una cita, ni un escalamiento.
-- Solo `casos_sin_resolver`, que es una tabla DERIVADA --se rellena sola a partir de lo que
-- pasa en los turnos-- y por eso se puede vaciar sin perder ningun hecho.
-- =========================================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM configuracion WHERE clave = 'medicion_sin_resolver_desde'
    ) THEN
        DELETE FROM casos_sin_resolver;

        INSERT INTO configuracion (clave, valor, descripcion) VALUES (
            'medicion_sin_resolver_desde',
            to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'Desde cuando cuentan los casos de «sin resolver». Se escribio al borrar los de '
            'prueba; los numeros de esa pantalla se leen contra esta fecha.'
        );
    END IF;
END $$;

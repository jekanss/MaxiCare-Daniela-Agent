-- =========================================================================================
-- Que un desconocido no pueda quemar el saldo, y que se note cuando lo intente
--
-- El sistema estaba blindado en el eje de los datos --ninguna tool puede leer ni tocar lo de
-- otro paciente-- y abierto en el de el dinero. Cualquiera que conozca el numero publico de
-- la clinica podia gastar el saldo de OpenAI sin vulnerar nada: entrando por la puerta
-- principal, con firma HMAC valida de Meta, indistinguible de un paciente real. Y no habia
-- un solo contador que se enterara: para saber cuanto se gasto habia que abrir el dashboard
-- de OpenAI a mano.
--
-- Esta migracion pone las tres piezas que faltaban para poder contar y para poder frenar.
--
-- El indice va PRIMERO y no es un detalle de rendimiento: sin el, contar la cuota de un
-- telefono seria un scan de `mensajes_entrantes` en CADA mensaje que entra. El freno costaria
-- mas que lo que frena, que es la forma mas tonta de convertir una defensa en el ataque.
-- =========================================================================================

-- -----------------------------------------------------------------------------------------
-- 1. El indice que hace barata la cuota
-- -----------------------------------------------------------------------------------------
--
-- `recibido_en DESC` porque todas las consultas de cuota preguntan por lo RECIENTE
-- (`>= now() - interval`), nunca por lo viejo.
CREATE INDEX IF NOT EXISTS mensajes_entrantes_telefono_recibido_idx
    ON mensajes_entrantes (telefono, recibido_en DESC);


-- -----------------------------------------------------------------------------------------
-- 2. `consumo_modelo` -- los ojos
-- -----------------------------------------------------------------------------------------
--
-- Una fila por CORRIDA, no por turno: el desglose por agente es justo lo que hace accionable
-- el numero. Un pico en `lector` y uno en `daniela` se arreglan de forma distinta -- el
-- primero es alguien mandando archivos, el segundo alguien conversando-- y agregados en una
-- sola cifra serian indistinguibles.
--
-- Sin FK a `conversaciones`, a proposito y pese a que `id_conversacion` es su UUID: el lector
-- de archivos arranca dentro de `procesar_mensaje`, ANTES de que `atencion._leer_estado` cree
-- la conversacion. Con FK, la primera lectura de un paciente nuevo reventaria al anotarse, y
-- la contabilidad se caeria justo en el caso que mas interesa medir. Aqui se prefiere una
-- fila huerfana a una excepcion.
--
-- `tokens_entrada_cacheados` va aparte y no restado de `tokens_entrada` porque son dos
-- hechos distintos: cuanto contexto se mando, y cuanto de el se cobro a precio reducido. El
-- proyecto corre con `prompt_cache_retention="24h"`, asi que sumarlos al mismo precio daria
-- una factura inventada -- y restarlos perderia el dato de cuanto contexto viajo de verdad.
--
-- `costo_usd` se calcula en Python y se guarda ya resuelto. Guardar solo tokens obligaria a
-- conocer el precio de un modelo que quiza ya no existe cuando alguien lea la fila: el precio
-- del dia del gasto es parte del hecho, no una conversion que se pueda rehacer despues.
CREATE TABLE IF NOT EXISTS consumo_modelo (
    id                       BIGSERIAL   PRIMARY KEY,

    momento                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- `daniela`, `lector`, `evaluador` o `analista`. Sin CHECK: la lista de consumidores
    -- crece con el proyecto y una migracion por cada uno seria friccion sin beneficio. Es la
    -- excepcion razonada frente a `relevo_motivo_cierre`, donde el valor decide una rama de
    -- codigo; aqui solo agrupa un informe.
    agente                   TEXT        NOT NULL,
    modelo                   TEXT        NOT NULL,

    id_conversacion          UUID,
    telefono                 TEXT,

    llamadas                 INTEGER     NOT NULL DEFAULT 0,
    tokens_entrada           INTEGER     NOT NULL DEFAULT 0,
    tokens_entrada_cacheados INTEGER     NOT NULL DEFAULT 0,
    tokens_salida            INTEGER     NOT NULL DEFAULT 0,

    costo_usd                NUMERIC(12, 6) NOT NULL DEFAULT 0
);

-- La consulta que corre cada minuto es «cuanto llevamos hoy», asi que el indice va por
-- momento y no por telefono.
CREATE INDEX IF NOT EXISTS consumo_modelo_momento_idx
    ON consumo_modelo (momento DESC);


-- -----------------------------------------------------------------------------------------
-- 3. `cuotas_avisadas` -- que el freno no se convierta en el ruido
-- -----------------------------------------------------------------------------------------
--
-- Sin esta tabla, un numero que se pasa de cuota recibe una frase fija por CADA mensaje y el
-- doctor un Telegram por cada uno: exactamente la inundacion que la cuota existe para evitar,
-- servida por la propia defensa. Es el mismo error que el no negociable 26 ya pago una vez
-- con los escalamientos, y la leccion esta escrita: «a la cuarta alerta repetida el doctor
-- deja de mirarlas».
--
-- La PK compuesta permite que un mismo numero tenga viva la cuota de mensajes y la de
-- archivos a la vez sin que una tape a la otra: son dos hechos distintos y el doctor querria
-- saber de los dos.
--
-- El «una vez por ventana» NO se resuelve leyendo y despues escribiendo --dos mensajes
-- simultaneos del mismo numero pasarian los dos por la lectura antes de que ninguno
-- escribiera-- sino con un unico `INSERT ... ON CONFLICT DO UPDATE ... WHERE`, que deja que
-- lo decida Postgres. Mismo criterio que `tomar_cupo` y que la deduplicacion por `wamid`: la
-- unicidad la resuelve el motor, no el orden de llegada.
CREATE TABLE IF NOT EXISTS cuotas_avisadas (
    telefono   TEXT        NOT NULL,
    clase      TEXT        NOT NULL CHECK (clase IN ('mensajes', 'archivos')),
    avisado_en TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (telefono, clase)
);


-- -----------------------------------------------------------------------------------------
-- 4. `alertas_gasto` -- lo mismo, para la factura
-- -----------------------------------------------------------------------------------------
--
-- El aviso de gasto tiene el mismo problema y la misma forma: el vigilante corre cada minuto,
-- y sin una marca el dia que se cruce el umbral el doctor recibe 1.440 Telegram identicos.
--
-- La clave es el DIA y no un contador: el umbral es diario, asi que a medianoche vuelve a
-- avisar solo, sin nadie que tenga que acordarse de reiniciar nada.
CREATE TABLE IF NOT EXISTS alertas_gasto (
    dia        DATE        PRIMARY KEY,
    avisado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    gasto_usd  NUMERIC(12, 6) NOT NULL DEFAULT 0
);

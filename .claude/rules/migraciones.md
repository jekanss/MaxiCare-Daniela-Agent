---
paths:
  - "migraciones/*.sql"
---

# Migraciones

`scripts/inicializar_base.py` aplica **todas** las migraciones en orden de nombre,
cada vez que se corre. De ahí las dos reglas:

1. **Toda migración es idempotente.** `CREATE TABLE IF NOT EXISTS`,
   `ADD COLUMN IF NOT EXISTS`, `ON CONFLICT DO NOTHING`, y para un constraint el
   bloque `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = ...
   AND conrelid = 'la_tabla'::regclass)`.

   **Ese `conrelid` no es opcional.** `pg_constraint.conname` es único por TABLA, no en
   toda la base: sin filtrar, el `IF NOT EXISTS` encuentra el constraint de `public` desde
   cualquier otro esquema y se salta la creación. La 003 lo hizo sin él y el CHECK de
   `relevo_motivo_cierre` no llegó a existir en `pruebas`, `pruebas_ingesta` ni
   `pruebas_web` — o sea que ninguna prueba contra Neon podía demostrar que la lista
   cerrada seguía cerrada. Lo arregla la 013.
2. **Una migración ya aplicada no se edita.** Se añade la siguiente con el número
   que sigue: `004_...sql`. **Con una excepción, y solo una: cuando la migración vieja es la
   que impide que la nueva exista.** Ver abajo.

# AMPLIAR un CHECK: el `DROP` + `ADD` a pelo es una bomba de relojería

Esto costó una hora el 22/09/2026 y habría tumbado el arranque en producción.

La 021 amplió el CHECK de `cambios_configuracion.tabla` de tres valores a cuatro, así:

```sql
ALTER TABLE cambios_configuracion DROP CONSTRAINT IF EXISTS ck_...;
ALTER TABLE cambios_configuracion ADD  CONSTRAINT ck_... CHECK (tabla IN (los cuatro));
```

Correcto, idempotente y verde durante una semana. **Y roto desde el minuto en que la 028
añadió el quinto valor**, porque las migraciones se aplican TODAS en cada arranque y por
orden de nombre:

```
arranque n+1:   ... 021 (vuelve a poner los CUATRO)  ->  028 (pone los cinco)
                     ↑
                     ya hay filas con el quinto valor
                     ERROR: is violated by some row  ->  aplicar_esquema entero abortado
```

`desplegar.sh` corre `inicializar_base.py` **antes** de levantar el contenedor, así que eso
no es un aviso: es un despliegue que no arranca. Lo reprodujo `-m neon` en cuanto una prueba
dejó la primera fila con el valor nuevo.

**La regla, entonces: un `ADD CONSTRAINT` que no vaya dentro de su `DO $$ ... IF NOT EXISTS`
es correcto hoy y falla el día que alguien amplíe esa misma lista.** La guarda no es solo
para no duplicar: es lo que hace que una migración vieja se aparte cuando una nueva manda.

Y por eso la 021 **sí se editó**, que es la excepción a la regla 2 de arriba. No era historia
que reescribir: es un paso que corre en cada arranque y que estaba a punto de tumbar el
siguiente. La alternativa --una migración más que reparase el CHECK después-- deja la 021
armada para la próxima vez. Quien tenga que elegir otra vez: se edita la vieja **solo** para
volverla inofensiva, nunca para cambiar lo que hizo.

# Un DELETE dentro de una migracion corre en CADA arranque

La 029 borra los casos de prueba de «sin resolver» para que la medicion empiece de cero. Un
`DELETE FROM casos_sin_resolver` a pelo ahi habria borrado la medicion ENTERA en el siguiente
despliegue, y en todos los siguientes: las migraciones se aplican todas, siempre.

Es el modo de fallo de la 021 con un filo peor. **Aquel abortaba ruidosamente** --
`CheckViolation`, `inicializar_base.py` con codigo 1, despliegue que no arranca -- y este no
deja nada: la tabla vuelve a cero, la pantalla se ve vacia, y «no ha pasado nada raro este
mes» es una lectura perfectamente creible de una pantalla vacia.

La guarda es un dato que la propia migracion escribe:

```sql
IF NOT EXISTS (SELECT 1 FROM configuracion WHERE clave = 'medicion_sin_resolver_desde') THEN
    DELETE FROM casos_sin_resolver;
    INSERT INTO configuracion (clave, valor, descripcion) VALUES ('medicion_...', now(), ...);
END IF;
```

Las dos sentencias van en el MISMO bloque: escribir la marca sin borrar, o borrar sin
escribirla, deja un estado que la migracion no sabe interpretar en el arranque siguiente.

**La regla: una migracion que BORRA datos necesita una marca que diga que ya corrio, y esa
marca la escribe ella misma en la misma transaccion.** Lo fija
`test_sin_resolver_neon.py::test_reaplicar_las_migraciones_NO_borra_los_casos_que_ya_se_midieron`,
que llama a `aplicar_esquema` dos veces con una fila en medio.

# Lo que el esquema protege

- `reservas` tiene `UNIQUE (inicio, cupo_num)`. Ese constraint **es** el control de
  capacidad (2 pacientes por hora): Neon es la fuente de verdad, Google Calendar es
  el espejo. No lo reemplaces por una comprobación en Python, que no es atómica.
- `pacientes` **no tiene columna de cédula**, por prohibición expresa del cliente.
  La ausencia es la política.
- Los `CHECK` con listas cerradas (`relevo_motivo_cierre`) son deliberados, igual que
  los `Literal` de `contratos.py`: un valor nuevo pasa por una migración, no se cuela
  como un string cualquiera.

# La 010 es distinta: su esquema no lo decidimos nosotros

`010_sesiones_agente.sql` crea `agent_sessions` y `agent_messages`, y **el que fija esas
columnas es el SDK de agentes**, no este proyecto. `SQLAlchemySession` corre con
`create_tables=False` —crear tablas desde el proceso que atiende pacientes es una carrera
esperando a ocurrir— así que el SQL de la migración tiene que coincidir con lo que el SDK
espera, hasta el tipo. Dos consecuencias:

- `created_at` / `updated_at` van **`TIMESTAMP` sin zona**, al revés que el resto del
  esquema, que usa `TIMESTAMPTZ`. No lo "arregles": es lo que el SDK declara.
- Quien suba la versión del SDK tiene que volver a comparar columna por columna. Lo que
  caza que la migración se quede vieja es `tests/test_sesion_neon.py`, que corre contra
  Neon de verdad: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`. Sin esa suite, un
  desajuste solo aparece cuando un paciente escribe.

El esquema `pruebas_web` del chat del panel recibe estas tablas por su cuenta:
`runtime._preparar_esquema_de_pruebas` llama a `aplicar_esquema` la primera vez que alguien
abre el chat, y aplica todas las migraciones con `IF NOT EXISTS`.

# Configuración

Lo que la clínica puede cambiar sin tocar código vive en la tabla `configuracion`,
no en constantes de Python: `capacidad_por_hora`, `duracion_cita_minutos`,
`cierre_relevo_minutos`, `aviso_relevo_minutos`, `telegram_topic_general`.
Los valores por defecto están en `CONFIGURACION_POR_DEFECTO` de `persistencia.py`.

Tras escribir una migración, córrela y verifica:
`uv run python scripts/inicializar_base.py`

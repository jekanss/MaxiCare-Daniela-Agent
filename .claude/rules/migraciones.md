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
   que sigue: `004_...sql`.

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

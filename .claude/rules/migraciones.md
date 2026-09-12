---
paths:
  - "migraciones/*.sql"
---

# Migraciones

`scripts/inicializar_base.py` aplica **todas** las migraciones en orden de nombre,
cada vez que se corre. De ahí las dos reglas:

1. **Toda migración es idempotente.** `CREATE TABLE IF NOT EXISTS`,
   `ADD COLUMN IF NOT EXISTS`, `ON CONFLICT DO NOTHING`, y para un constraint el
   bloque `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = ...)`.
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

# Configuración

Lo que la clínica puede cambiar sin tocar código vive en la tabla `configuracion`,
no en constantes de Python: `capacidad_por_hora`, `duracion_cita_minutos`,
`cierre_relevo_minutos`, `aviso_relevo_minutos`, `telegram_topic_general`.
Los valores por defecto están en `CONFIGURACION_POR_DEFECTO` de `persistencia.py`.

Tras escribir una migración, córrela y verifica:
`uv run python scripts/inicializar_base.py`

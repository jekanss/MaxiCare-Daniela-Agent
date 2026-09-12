---
name: cerrar-fase
description: Cerrar una fase de MaxiCare Daniela — correr su entregable verificable, dejar los hallazgos escritos en plan-agentes.json validando contra el esquema, y actualizar las trampas del CLAUDE.md. Úsala cuando una fase esté terminada o cuando el usuario diga que arranca la siguiente.
---

# Cerrar una fase

Una fase **no se cierra porque se haya escrito mucho código**. Se cierra cuando el hecho
que describe su propio `entregable_verificable` es cierto y se puede mostrar.

Este procedimiento existe porque se repite en cada fase y porque el paso que siempre se
olvida —dejar escrito lo que se aprendió— es el que hace que la siguiente fase no tropiece
con la misma piedra.

## 1 · Leer el entregable, no recordarlo

```bash
uv run python scripts/ver_plan.py fases <orden>
```

El campo `entregable_verificable` dice literalmente qué tiene que ser cierto. Si no puedes
señalar ese hecho, la fase no está cerrada — da igual cuánto se haya avanzado.

## 2 · Correr la prueba que lo demuestra

Cada fase tiene la suya, y están listadas en `CLAUDE.md`. Además, siempre:

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
```

Si algo falla, la fase no se cierra. Repórtalo con la salida real, no con un resumen.

## 3 · Escribir los hallazgos en el plan

Lo que se aprendió construyendo, y que no estaba en el diseño, va a
`docs/agentes/plan-agentes.json`. No como campos nuevos: **`fase` y `guardrail` tienen
`additionalProperties: false`**, así que se dobla dentro de los campos de texto que ya
existen — normalmente `condicion_revision` de la fase, y la `justificacion` de la
herramienta o el guardrail al que afecte.

Escribe un script de un solo uso en el scratchpad con esta forma:

```python
MARCA = "VERIFICADO <AAAA-MM-DD> (fase N)"   # hace el script idempotente

plan = json.loads(PLAN.read_text(encoding="utf-8"))
fase = next(f for f in plan["fases"] if f["orden"] == N)
if MARCA not in fase["condicion_revision"]:
    fase["condicion_revision"] += HALLAZGOS

jsonschema.validate(plan, esquema)          # ANTES de escribir. Si falla, no se toca nada.
PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
```

El esquema está en:
`C:/Users/jeanc/.claude/plugins/cache/openai-agents-sdk-marketplace/openai-agents-sdk/0.1.0/contratos/plan-agentes.schema.json`

**Qué es un hallazgo que vale la pena escribir.** No un resumen de lo que se hizo —eso ya
está en el código—, sino lo que habría hecho falta saber antes de empezar: un
comportamiento de una API que contradice lo que parecía, una trampa del entorno, una
decisión que se tomó y por qué se descartó la otra, o un requisito que esta fase le deja a
otra posterior. Si un hallazgo cambia lo que otra fase tiene que hacer, escríbelo **en esa
otra fase**, no solo en la actual.

## 4 · Actualizar las trampas del `CLAUDE.md`

Si en la fase apareció algo que costó tiempo descubrir y volverá a costarlo, va a
«Trampas de este entorno». Si apareció un comando nuevo que cierra un entregable, va a
«Comandos».

Sé estricto: el `CLAUDE.md` carga en cada sesión y el objetivo son menos de 200 líneas. Un
hallazgo que solo aplica a una zona del árbol va a una regla de `.claude/rules/` con
`paths:`, no aquí. Uno que tiene que sobrevivir a una compactación va aquí, no allá.

## 5 · Informar sin exagerar

Al contarlo:

- **Muestra la salida real** de la prueba que cierra la fase, no una paráfrasis.
- **Distingue «se comprobó» de «no hizo falta comprobarlo».** Si un freno no llegó a saltar
  porque el sistema se portó bien, eso es una buena noticia y **no** es lo mismo que haber
  visto saltar su tripwire. Decir que los seis dispararon cuando tres ni se acercaron es
  justo el informe que hace confiar en un freno sin probarlo.
- **Di qué quedó fuera y por qué.** Si una parte del entregable no se pudo verificar
  —porque falta una credencial, un dato de MaxiCare o un archivo real—, dilo con ese nombre
  y márcalo `PENDIENTE`, nunca con un valor plausible.

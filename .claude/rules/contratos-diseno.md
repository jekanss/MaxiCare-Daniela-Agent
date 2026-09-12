---
paths:
  - "docs/agentes/*.json"
---

# Brief y plan: valídalos antes de darlos por buenos

`brief-agentes.json` y `plan-agentes.json` son contratos con esquema. Una edición no
está terminada hasta que pasa `jsonschema.validate`:

```python
import io, json, jsonschema
BASE = ('C:/Users/jeanc/.claude/plugins/cache/openai-agents-sdk-marketplace/'
        'openai-agents-sdk/0.1.0/contratos/')
doc = json.load(io.open('docs/agentes/plan-agentes.json', encoding='utf-8'))
esquema = json.load(io.open(BASE + 'plan-agentes.schema.json', encoding='utf-8'))
jsonschema.validate(doc, esquema)
```

Los dos esquemas tienen `additionalProperties: false`: un campo inventado los rompe.
Ya pasó una vez — dos campos añadidos «porque hacían falta» que el esquema rechazó.
Si un dato no cabe en ningún campo existente, va dentro del texto de uno que sí
exista, no en una clave nueva.

# Cómo se edita

Con un script en el scratchpad que lee, modifica, **valida** y guarda con
`ensure_ascii=False, indent=2`. No a mano: son 68 KB y un JSON roto se detecta tarde.

Al hacer un `replace` sobre un texto largo, comprueba que el fragmento existía
(`assert viejo in texto`). Un replace que no encuentra nada falla en silencio y deja
el contrato diciendo lo de antes.

# Qué NO se toca sin hablarlo

- Cada decisión lleva `justificacion`, `alternativa_descartada` y
  `condicion_revision`. Los tres son obligatorios y ninguno es relleno: sin una
  alternativa real considerada, no es una decisión, es un informe con forma de decisión.
- El literal `"PENDIENTE"` marca lo que nadie sabe todavía. No lo reemplaces por un
  valor plausible para «dejarlo completo».
- Sigue PENDIENTE, a la espera de MaxiCare: el texto de la política de datos
  (`datos.datos_sensibles`) y la revisión legal (`restricciones.legales`).

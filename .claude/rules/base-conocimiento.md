---
paths:
  - "datos/base_conocimiento.json"
---

# La base de conocimiento

Es la transcripción fiel del documento maestro de MaxiCare. Cada fila es algo que la
clínica dijo. **Nada se agrega aquí por deducción, por analogía con otro tratamiento
ni «para completar».**

# Endodoncia y prótesis no tienen filas, a propósito

El documento maestro (sección 2.12) dice que no hay precio real, especialista
asignado, inclusiones, exclusiones ni garantías documentadas, y que **no debe
publicarse ninguna cifra**.

La forma de cumplirlo no es una regla en el prompt: **es que la fila no exista.**
Sin filas, `formatear_conocimiento` devuelve `SIN DATO DOCUMENTADO`, que le prohíbe
explícitamente al modelo estimar y le manda escalar.

Agregar una fila de precio para cualquiera de los dos rompe el primer objetivo del
proyecto. `tests/test_base_conocimiento.py` falla si aparece.

# Los tres estados de un dato

| Estado | Cómo se escribe | Qué ve el paciente |
|---|---|---|
| Aprobado | `"aprobado": true` | El dato, limpio |
| Existe pero sin aprobar | `"aprobado": false` + `"nota_pendiente"` con **qué** falta | Referencia general, advertida, nunca como compromiso |
| No existe | no hay fila | «SIN DATO DOCUMENTADO» + escalamiento |

Una fila `aprobado: false` **sin** `nota_pendiente` es una fila que nadie va a
resolver, porque nadie sabe qué preguntarle a MaxiCare. El test lo impide.

# Otras invariantes

- Todo `tratamiento` debe existir en el `Literal` de `contratos.py` (o ser
  `_general`). Si no, esa información es inalcanzable: `lector_archivos` no podría
  devolverlo nunca.
- El par `(tratamiento, concepto)` es único.
- Ninguna cédula, ni de ejemplo.

Tras editar: `uv run pytest -q tests/test_base_conocimiento.py` y luego
`uv run python scripts/inicializar_base.py` para recargar.

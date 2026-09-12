---
paths:
  - "src/maxicare_daniela/**/*.py"
---

# La frontera agentes ↔ transporte

`agentes.py`, `herramientas.py` y `contratos.py` **nunca importan `runtime.py`**.
La dirección permitida es la contraria: `runtime.py` importa de los tres.

`runtime.py` es el único módulo que importa un framework de transporte (FastAPI, el
cliente de WhatsApp, Telegram) y el único donde aparece `Runner.run`.

Esto no es estética. Un `import` de FastAPI dentro de `herramientas.py` hace que esa
tool no se pueda probar sin levantar un servidor, y ata la lógica de negocio al canal
de hoy. `tests/test_estructura.py` lo verifica con un detector sobre el AST: si lo
rompes, falla ahí.

# El muro

`LecturaArchivo.tratamiento` es un `Literal` cerrado, **no un `str`**.

Ese tipo es lo que impide físicamente que una frase clínica —«se observa lesión
periapical en el 46»— llegue al agente que le habla al paciente. Con un `str` la
protección sería una instrucción en el prompt, que el modelo puede desobedecer. Con
un `Literal`, Pydantic rechaza el valor antes de que exista.

El contenido clínico viaja por `contexto_clinico`, y **su destino exclusivo es
Telegram**. Nunca se le describe a un paciente el contenido clínico de una imagen,
un video o una remisión.

`tests/test_contratos.py::test_tratamiento_no_admite_una_frase_clinica` es la prueba
que lo sostiene. No la debilites para que pase otra cosa.

# Convenciones del paquete

- `SolicitudCita` **no tiene campo de teléfono**: la tool lo lee del contexto local.
  Si el modelo pudiera escribirlo, podría escribir uno distinto —la hija agendando
  para la mamá— y el recordatorio saldría al número equivocado.
- Toda tool de escritura lleva clave de idempotencia, construida por el orquestador
  antes de llamarla.
- «Horario lleno» es un RESULTADO de la tool con alternativas, nunca un error de
  validación: un error hace que el modelo reintente a ciegas hasta `MaxTurnsExceeded`.

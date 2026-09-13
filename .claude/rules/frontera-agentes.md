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

# `identidad_antes_de_datos`: quién tiene datos que proteger

El guardrail frena `crear_cita`, `reprogramar_cita` y `cancelar_cita` sin identidad
verificada. **Con UNA excepción, y hay que entender por qué es una excepción y no un
agujero:** un teléfono que no tiene fila en `pacientes` SÍ puede crear su primera cita.

El plan justifica el guardrail con `datos.quien_ve_que`: «que el contexto de una cuenta y el
de un paciente no se mezclen cuando alguien escribe por un familiar». Eso presupone una ficha
que proteger. Quien no la tiene no tiene datos que otro pueda ver ni citas que otro pueda
mover, y bloquearlo no protegía a nadie: **dejaba a la clínica sin pacientes nuevos, que es
el objetivo del proyecto.**

Y el sistema se contradecía a sí mismo. `identificar_paciente` le responde al modelo, textual,
«Ese número no está registrado… **Puedes agendarle una cita nueva**», y el guardrail se lo
prohibía. Medido en producción el 13/09/2026, leído del historial del agente en Neon: el
paciente dio su nombre, Daniela llamó a `crear_cita`, el guardrail la frenó, regeneró, la
frenó otra vez, y el paciente recibió «te escribe el doctor». Que el diseño sí lo contemplaba
está en el esquema: `citas.paciente_id` es **NULLABLE** desde la migración 001 y la tabla
guarda `nombre_completo` y `telefono` por su cuenta.

Cuatro cosas sostienen que esto no se convierta en una puerta:

- **`telefono_sin_paciente` no lo dice el modelo.** Lo pone el código tras un
  `buscar_paciente_por_telefono` contra la base —`atencion._leer_estado`, con la lectura que
  ya hacía, y lo refresca `herramientas._identificar_paciente`—. Si el modelo pudiera
  escribirlo, bastaría con que dijera «soy nuevo».
- **`reprogramar_cita` y `cancelar_cita` NO entran**, tenga ficha el número o no: operan
  sobre citas que ya existen, y una cita existente sí puede ser de la persona a la que
  alguien está suplantando.
- **Es una lista blanca (`_ESCRITURAS_PARA_DESCONOCIDO`), no una negra.** La
  `condicion_revision` del plan dice que una cuarta tool de escritura hay que agregarla a
  mano; mientras nadie la agregue, cae del lado estricto. Hay prueba con una tool inventada.
- **Un número CON ficha y sin verificar sigue bloqueado**, que es exactamente el caso que el
  plan quiere frenar.

# Convenciones del paquete

- `SolicitudCita` **no tiene campo de teléfono**: la tool lo lee del contexto local.
  Si el modelo pudiera escribirlo, podría escribir uno distinto —la hija agendando
  para la mamá— y el recordatorio saldría al número equivocado.
- Toda tool de escritura lleva clave de idempotencia, construida por el orquestador
  antes de llamarla.
- «Horario lleno» es un RESULTADO de la tool con alternativas, nunca un error de
  validación: un error hace que el modelo reintente a ciegas hasta `MaxTurnsExceeded`.

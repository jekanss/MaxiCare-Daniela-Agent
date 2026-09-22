---
paths:
  - "datos/base_conocimiento.json"
  - "datos/base_conocimiento.ejemplo.json"
  - "src/maxicare_daniela/herramientas.py"
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

# Dos orígenes, una sola transcripción

Las filas de precio, duración, inclusiones y garantías salen de las secciones 1 a 4 del
documento maestro. Las de `que_es`, `para_quien`, `dolor`, `limite_edad`, `caso_especial`,
`objeciones` y `diferencial` salen de su **sección 6**, que consolida el formato de base de
conocimiento que llenó el equipo médico. La 6.2 dice qué se dejó fuera de ese formato y por
qué: la parte redactada con IA, el benchmark de la competencia y los diferenciales que la
propia clínica marcó como no verificados. Nada de eso se reincorpora sin leer antes esa
sección.

# Los tres estados de un dato

| Estado | Cómo se escribe | Qué ve el paciente |
|---|---|---|
| Aprobado | `"aprobado": true` | El dato, limpio |
| Existe pero sin aprobar | `"aprobado": false` + `"nota_pendiente"` con **qué** falta | Referencia general, advertida, nunca como compromiso |
| No existe | no hay fila | «SIN DATO DOCUMENTADO» + escalamiento |

Una fila `aprobado: false` **sin** `nota_pendiente` es una fila que nadie va a
resolver, porque nadie sabe qué preguntarle a MaxiCare. El test lo impide.

# Cómo se BUSCA: cuatro pasos, y ninguno inventa

Guardar el dato no basta: el modelo tiene que acertar el par `(tratamiento, concepto)` con
el que se guardó, y ese par es texto libre. `herramientas._consultar_base_conocimiento`
prueba cuatro y se queda con el primero que traiga algo aprobado:

| # | Consulta | Para qué |
|---|---|---|
| 1 | `(tratamiento, concepto)` | lo que el modelo pidió |
| 2 | `(_general, concepto)` | el mismo concepto, como hecho general |
| 3 | `(tratamiento, *)` | la ficha entera: el concepto no estaba, pero el tratamiento sí |
| 4 | `(_general, tratamiento)` | el tratamiento no tiene NI UNA ficha y `_general` sabe de él |

El 4 se lee mejor como el tercer intento de los tres respaldos, pero va al final porque su
condición —«ni una ficha»— es exactamente lo que acaba de contestar el 3. Así no cuesta una
consulta extra.

**Lo que hace segura la ampliación es que ninguno de los cuatro inventa**: los cuatro
devuelven filas que MaxiCare aprobó, o el `SIN DATO DOCUMENTADO` de siempre. Endodoncia y
prótesis siguen mudas con los cuatro puestos, y hay una prueba que lo fija
(`test_endodoncia_sigue_MUDA_con_los_tres_respaldos_puestos`). Si algún día un respaldo
empieza a completar huecos en vez de a buscarlos, esa prueba es la que tiene que ponerse en
rojo.

**Por qué el 2 y el 4 existen, medido el 22/09/2026 en producción.** Un paciente preguntó
«¿en qué punto se encuentran y qué vale la consulta?». Daniela contestó la dirección y, del
precio, «lo estoy confirmando con el equipo para darte el dato correcto» — y escaló al
doctor. Dos veces ese día, a las 17:16 y a las 21:04, las dos con motivo `dato_faltante`.

El precio estaba escrito, aprobado y cargado desde siempre, en `_general`/`valoracion`.
Daniela preguntó `valoracion`/`precio`, cuatro veces: desde la 020 `valoracion` ES una clave
de tratamiento —para poder agendar sin diagnóstico— y **no tiene ni una ficha**. El único
respaldo que existía entonces era el 3, que sobre un tratamiento vacío devuelve vacío. Y
`SIN DATO DOCUMENTADO` le ordena por escrito confirmar con los doctores y escalar. Obedeció.

**El precio de la valoración vive en UNA sola fila a propósito.** La alternativa era
copiarlo a `valoracion`/`precio`, y se descartó: los $40.000 aplican a cualquier tratamiento
y se abonan a todos, así que es un hecho general. Copiarlo pondría el mismo precio en dos
filas que la clínica edita por separado desde el panel, y un precio en dos sitios es un
precio que se desincroniza.

**La medición no se toca.** La señal que alimenta el informe de «sin resolver» sale de la
consulta 1 y de ninguna otra, así que `falta_dato:valoracion:precio` se sigue anotando. Que
un respaldo conteste no borra el hueco de vocabulario: solo evita que le cueste una
interrupción al doctor.

# Una clave de tratamiento sin nada que decir no se ve

Es el modo de fallo de arriba, en general: una clave sobre la que Daniela no puede decir
absolutamente nada no rompe nada. Contesta «lo confirmo con el equipo» y escala. Ni un
error, ni un log, ni una prueba en rojo — solo un doctor contestando lo que la clínica ya
había contestado. `valoracion` estuvo así seis días.

Lo vigila el **bloque 11 de `scripts/inicializar_base.py`**, que lista toda clave activa de
`tratamientos` sin una sola ficha propia **ni** una fila `_general`/<clave>. **AVISA, no
falla**, al revés que los diez bloques de encima: aquellos vigilan el esquema —si falta algo,
el sistema está roto— y esto es contenido que la clínica edita desde el panel. Un tratamiento
creado un martes con su ficha pendiente para el miércoles no puede tumbar un despliegue.

Las tres excepciones (`MUDOS_A_PROPOSITO`) son `endodoncia`, `protesis` —sección 2.12— y
`no_identificado`, que no es un tratamiento sino lo que usa el sistema cuando no sabe.

# Otras invariantes

- Todo `tratamiento` debe existir en el `Literal` de `contratos.py` (o ser
  `_general`). Si no, esa información es inalcanzable: `lector_archivos` no podría
  devolverlo nunca.
- El par `(tratamiento, concepto)` es único.
- Ninguna cédula, ni de ejemplo.

Tras editar: `uv run pytest -q tests/test_base_conocimiento.py` y luego
`uv run python scripts/inicializar_base.py` para recargar.

# El archivo real no se versiona

`datos/base_conocimiento.json` y el documento maestro del que sale llevan la direccion de
la sede, los nombres de los odontologos y las tarifas: son datos del cliente, y estan en
`.gitignore` por la misma razon que `.env`. Lo versionado son los ejemplos de al lado, que
conservan las 148 entradas con su `tratamiento` y su `concepto` --el esquema-- y sustituyen
el `contenido`.

**El ejemplo tiene que pasar las pruebas igual que el real.** Es la copia que arranca un
clon, así que toda fila suya con `aprobado: false` necesita su `nota_pendiente`, aunque el
texto sea genérico: sin ella `test_toda_fila_no_aprobada_explica_que_falta` falla en la
primera corrida de un repositorio recién clonado. Estuvo fallando hasta el 16/09/2026.

En un clon nuevo, antes de `inicializar_base.py`:

```bash
cp datos/base_conocimiento.ejemplo.json datos/base_conocimiento.json
```

y despues se sustituye por el real de la clinica.

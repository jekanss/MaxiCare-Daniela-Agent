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

# Cómo se BUSCA: cinco pasos, y ninguno inventa

Guardar el dato no basta: el modelo tiene que acertar el par `(tratamiento, concepto)` con
el que se guardó, y ese par es texto libre. `herramientas._consultar_base_conocimiento`
prueba cinco y se queda con el primero que traiga algo aprobado:

| # | Consulta | Para qué |
|---|---|---|
| 1 | `(tratamiento, concepto)` | lo que el modelo pidió |
| 2 | `(_general, concepto)` | el mismo concepto, como hecho general |
| 3 | `(_general, tratamiento)` | el tratamiento no tiene NI UNA ficha y `_general` sabe de él |
| 4 | por PALABRA, en el tratamiento **y** en `_general` | la ficha existe y se llama de otra forma |
| 5 | `(tratamiento, *)` | la ficha entera: el concepto no estaba, pero el tratamiento sí |

**El orden tiene una regla, no es una lista: primero todo lo EXACTO, después lo aproximado**,
y de lo aproximado, lo más estrecho antes que lo más ancho. El 3 estuvo el último hasta el
23/09/2026 —su condición, «ni una ficha», ya la contestaba el 5, así que no costaba una
consulta— y subió el día que llegó el 4: detrás de una coincidencia parcial,
`valoracion`/`precio` lo interceptaba la palabra «precio» y devolvía `_general`/`politica_precios`
en vez de los $40.000. Es la regresión del 22/09 reabierta por la puerta de al lado, y se
midió antes de subirlo.

**Lo que hace segura la ampliación es que ninguno de los cinco inventa**: los cinco
devuelven filas que MaxiCare aprobó, o el `SIN DATO DOCUMENTADO` de siempre. Endodoncia y
prótesis siguen mudas con los cinco puestos, y lo fijan dos pruebas
(`test_endodoncia_sigue_MUDA_con_los_tres_respaldos_puestos` —el nombre envejeció, el caso
no— y `test_un_tratamiento_MUDO_no_lo_desmudece_una_palabra_suelta`). Si algún día un
respaldo empieza a completar huecos en vez de a buscarlos, esas son las que tienen que
ponerse en rojo.

## El paso 4: por qué existe, y la guarda que lo hace seguro

La búsqueda es `concepto = %s`. Igualdad exacta, sin `ILIKE` y sin normalizar: **«formas de
pago» no encuentra `medios_pago`, y «horarios» no encuentra `horario`.**

Medido en producción el 23/09/2026. Vladimir preguntó por dos implantes «y si tienen
facilidades de pago». Daniela consultó `_general`/«formas de pago», no acertó el nombre, y el
informe de «sin resolver» lo contó como que la clínica no tenía el dato — lo tiene, aprobado,
en `_general`/`medios_pago` y `_general`/`financiacion`. Ese caso se salvó por el paso 5,
porque `_general` tiene quince fichas y entre ellas iba la buena. **El de al lado no se
salvaba**: preguntando `implantes`/«formas de pago», la ficha entera de implantes SÍ existe,
así que contesta —con sus catorce conceptos, ninguno sobre pagos— y corta la cascada antes de
mirar en `_general`. Por eso el 4 va delante del 5.

`persistencia._palabras_clave` normaliza tres cosas, cada una por un caso de la base real:
sin tildes (`financiación` → `financiacion`), sin plural en sus dos formas españolas
(`horarios` → `horario`, `objeciones` → `objecion`) y solo desde tres letras, porque `eps` es
una clave de verdad. **Se mira el CONCEPTO y nunca el contenido**: con el contenido, «pago»
engancharía también el precio de la ortodoncia —que menciona pagos por control— y el respaldo
pasaría de devolver la ficha correcta a devolver media base. Medido sobre las 137 filas
aprobadas: «formas de pago» devuelve UNA, «horarios» devuelve UNA.

**Y la guarda: el paso 4 solo entra si el tratamiento consultado dice ALGO.** No es una
optimización, es lo que mantiene muda a la endodoncia. Un tratamiento sin ni una ficha no es
uno que se olvidó documentar: es uno sobre el que MaxiCare decidió no decir nada (sección
2.12). Sin esa condición, `endodoncia`/`precio` engancha `_general`/`politica_precios` por la
palabra «precio»; no lleva cifras, pero sustituye el literal `SIN DATO DOCUMENTADO`, que es
justo lo que ordena no estimar y escalar. Se midió contra la base real antes de ponerla.

Lo que el paso 4 **no** resuelve es un sinónimo que no comparta ninguna palabra: si la fila se
llamara `condiciones_economicas`, «formas de pago» seguiría sin encontrarla. Para eso harían
falta alias que la clínica escriba desde el panel, y no están hechos.

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

# <Clínica> — Documento maestro de tratamientos, tarifas y políticas

> **Este archivo es un EJEMPLO.** No describe a ninguna clínica real: ni las tarifas, ni los
> nombres, ni la dirección. El documento real de la clínica vive en este mismo directorio
> como `documento-maestro-tratamientos-y-politicas.md` y **no se versiona** — igual que
> `.env`, por la misma razón: contiene datos de un cliente.
>
> Lo que sí se versiona es esta plantilla, porque la **forma** del documento es una decisión
> de diseño y hay que poder leerla sin tener los datos.

**Versión de consolidación:** <fecha>
**Moneda:** <moneda>
**Estado:** los puntos marcados «por confirmar» requieren aprobación de la clínica antes de
comunicarse como compromiso comercial.

---

## Por qué este documento existe, y por qué manda

Es la única fuente de verdad de lo que Daniela puede decir sobre precios, duraciones,
garantías e inclusiones. De aquí sale `datos/base_conocimiento.json`, que es lo que
`scripts/inicializar_base.py` carga en la base y lo que la tool
`consultar_base_conocimiento` lee en cada turno.

La cadena importa: **el agente no puede decir una cifra que no haya salido de aquí.** El
guardrail `sin_cifra_no_documentada` extrae toda cifra de dinero de la respuesta y exige que
esa tool la haya autorizado en ese mismo turno. Un precio que no esté en este documento no
existe para el sistema, y si el modelo lo inventa, la respuesta se bloquea.

De ahí la regla que gobierna cómo se escribe: **cuando una ficha no define algo, se dice
«no documentado», nunca se rellena con lo que parezca razonable.** Una suposición sensata y
un hecho confirmado se leen igual seis meses después, y solo uno de los dos es cierto.

---

## 1. Datos generales y responsables clínicos

- **Sede:** <dirección>. <Cómo llegar>. <Parqueadero: sí / no>.
- **Horario:** <días y horas>.
- **<Nombre del profesional>:** <especialidad>; <procedimientos que realiza>.
- **<Especialidad sin nombre asignado>:** <años de experiencia>; nombre **por confirmar**.

---

## 2. Catálogo de tratamientos

Los precios corresponden a las modalidades descritas, **no a cualquier variante** del
procedimiento. Cuando la ficha no define una inclusión o exclusión, se indica expresamente
«no documentado».

Una sección por tratamiento, todas con la misma tabla. El vocabulario de tratamientos que el
sistema reconoce está cerrado en `contratos.Tratamiento`: añadir uno aquí sin añadirlo allí
no lo hace existir.

### 2.1 <Tratamiento>

| Concepto | Información confirmada en la ficha |
| --- | --- |
| Precios | <cifra por unidad; paquetes si los hay> |
| Profesional | <quién lo realiza> |
| Duración | <minutos por sesión> |
| Incluye | <qué cubre el precio> |
| No incluye | <qué NO cubre: imágenes, medicación, sedación…> |
| Seguimiento/garantía | <controles posteriores, plazo de garantía y qué la invalida> |
| Cuidados descritos | <indicaciones posteriores> |

### 2.2 <Tratamiento> — ficha pendiente

Un tratamiento sin ficha se deja escrito **como pendiente**, con su nombre y nada más. No se
borra de la lista: que falte tiene que verse. Mientras no tenga ficha, Daniela no puede dar
su precio, y eso es correcto.

---

## 3. Políticas comerciales y de atención

- **Política de precios:** <cómo se comunican y en qué casos cambian>.
- **Valoración:** <costo y condiciones>.
- **Medios de pago y financiación:** <cuáles, y con qué condiciones>.
- **EPS y planes de salud:** <relación>.
- **Urgencias:** <cómo se atienden y en qué horario>.
- **Confirmación de cita y recordatorios:** <cuándo, cómo, y qué pasa si no se confirma>.

---

## 4. Pendientes para aprobación de la clínica

La lista de lo que todavía no está confirmado, punto por punto. **Es la sección más
importante del documento** y la que no hay que dejar vacía por comodidad: cada línea de aquí
es algo que el sistema no puede afirmarle a un paciente todavía.

---

## 5. Procedencia

De qué fuentes salió cada cosa —qué manual, qué conversación, qué fecha— para que dentro de
un año se sepa a quién preguntarle cuando un dato deje de cuadrar.

A esos dos se suma el formato de base de conocimiento llenado por la clínica, el formato de base de conocimiento llenado por el equipo médico, que aporta el contenido conversacional consolidado en la **sección 6**: descripciones, indicaciones, manejo del dolor, casos especiales, objeciones y diferenciales. No aporta tarifas. Ante diferencias entre ese formato y las fichas de la sección 2, manda la ficha, y el conflicto queda anotado en la sección 6.2.

## 6. Contenido conversacional por tratamiento

Origen: el formato de base de conocimiento llenado por la clínica, el formato «Base de Conocimiento — Tratamientos» que llenó el equipo médico de MaxiCare. Aporta lo que los dos manuales anteriores no traían: cómo se le explica cada tratamiento a un paciente, qué se le responde cuando pregunta si duele y cómo se contesta cada objeción. **No aporta ni una tarifa nueva**, y ninguna de las entradas derivadas de él contiene un precio.

### 6.1 Qué se incorporó

Siete conceptos por tratamiento, sobre los once que ya tienen ficha de precio:

| Concepto | Qué contiene |
| --- | --- |
| `que_es` | Definición del procedimiento en lenguaje de paciente. |
| `para_quien` | Indicación y problema que resuelve, cerrando siempre en que la candidatura se define en valoración. |
| `dolor` | Qué se siente durante y después. Es la pregunta más frecuente del canal. |
| `limite_edad` | Edad mínima o máxima, cuando la ficha la define. |
| `caso_especial` | Los «¿qué pasa si...?» que el equipo redactó para cada tratamiento. |
| `objeciones` | Objeción del paciente y la respuesta que la clínica aprobó. |
| `diferencial` | Por qué hacerlo en MaxiCare, y materiales o tecnología empleados. |

En `_general` se agregaron `redes` (Instagram), `especialidades` (las cinco que se atienden internamente) y `diferencial` (atención integral, calidad humana y nuevas tecnologías).

Excepciones: **bichectomía no lleva `para_quien`**, porque su fila `indicacion` ya dice lo mismo y el par `(tratamiento, concepto)` es único. Tampoco existen las cuatro entradas de la sección 6.3. Donde el formato dejó el campo vacío, la fila no existe: así `formatear_conocimiento` devuelve «SIN DATO DOCUMENTADO», que es el comportamiento correcto y no un hueco silencioso.

### 6.2 Qué se dejó fuera, deliberadamente

1. **La sección «Ejemplo Formato Lleno».** El propio documento la encabeza con «AQUÍ LES PEGO UN FORMATO COMPLETADO CON IA SOLO COMO A MODO DE EJEMPLO», y está sembrada de marcas «⚠️ COMPLETAR». No es una afirmación de MaxiCare sobre sí misma. Sus cifras contradicen además las fichas reales —sábados hasta las 12:00 m., blanqueamiento de 60–90 minutos, cordales de 30–60 minutos— y en cada conflicto manda la ficha, como fija la sección 5.
2. **El benchmark de precios del mercado Bogotá 2026.** Son rangos de la competencia, no tarifas de MaxiCare. Una fila con esas cifras las volvería citables por el agente y las dejaría autorizadas frente al guardrail de cifras.
3. **Los diferenciales sin respaldo de la sección 4 del formato**: «respuesta en menos de 5 minutos» y «el 90% de los pacientes dicen que fue más fácil de lo esperado». La propia columna contigua del documento los marca como «lo que nos falta saber», es decir, la clínica todavía no los ha sostenido con datos.
4. **Endodoncia y prótesis.** El formato las nombra, pero cada campo suyo dice «COMPLETAR». Siguen sin una sola fila, por la sección 2.12.

### 6.3 Las cuatro entradas que se dejaron fuera

Estas cuatro no entraron a la base. Cada una choca con algo que este documento ya había marcado, y MaxiCare no ha firmado ninguna. Mientras no existan como fila, `consultar_base_conocimiento` devuelve «SIN DATO DOCUMENTADO» para ese concepto y Daniela escala, que es lo que se busca: se prefirió el silencio a una promesa advertida.

| Entrada descartada | Por qué |
| --- | --- |
| `cordales/objeciones` | La respuesta a «es muy caro» ofrece un plan de pago, y la única financiación confirmada es la de ortodoncia (punto 9 de la sección 4). |
| `cordales/caso_especial` | Promete tratar «sin costo adicional» la afectación de un diente vecino y la segunda cita cuando la cirugía se suspende, sin documentar alcance ni condiciones (punto 1). |
| `implantes/caso_especial` | Repite la cobertura por falta de osteointegración «sin costo adicional» que el punto 3 ya había pedido confirmar. |
| `blanqueamiento/objeciones` | Afirma en absoluto que el blanqueamiento profesional «no daña el esmalte ni debilita los dientes», sin condiciones ni excepciones (punto 1). |

Para incorporarlas hace falta que MaxiCare defina alcance y condiciones de cada cobertura; el texto original está en el formato de base de conocimiento, sección del tratamiento correspondiente.

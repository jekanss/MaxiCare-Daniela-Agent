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

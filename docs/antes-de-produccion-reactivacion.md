# Antes de sacar la reactivación de leads a producción

Lista de lo que queda antes de encender. **Nada de esto es código**: el código está
construido, revisado y en verde. Lo que falta son una confirmación legal, tres pruebas con
un teléfono de verdad y unas decisiones de producto que no son mías.

Este archivo existe porque el razonamiento completo vive en
`.superpowers/sdd/2026-09-17-reactivacion-de-leads/progress.md`, **que está en
`.gitignore`**: el día que la rama se funda y se limpie el espacio de trabajo, eso se pierde.
Lo que importa para encender está aquí, en git.

| | |
|---|---|
| Rama | `reactivacion-leads`, fundida en `main` el **21/09/2026** |
| Estado | **③ HECHO: fundida y desplegada, CALLADA.** Falta el ④ |
| Qué manda hoy | **nada** — los tres nombres de plantilla están vacíos en el servidor |
| Pruebas tras fundir | 1148 offline en verde, cero fallos |

**Lo que cambió el 21/09/2026, y hay que leerlo entero antes de tocar el `.env`:**

El código de reactivación **ya corre en producción**. El barrido decide cada hora a quién se
le escribiría y **lo encola**, sin enviar: ese es el modo de comprobación, y es lo que permite
mirar en `seguimientos` a quién habría escrito **antes** de que salga un solo WhatsApp.

**Y NO se acumula una cola que vaya a reventar el día del ④**, que es lo primero que uno teme
al leer lo de arriba. Sin plantilla, una fila decidida `enviar` queda PENDIENTE y **R3 la anula
a las dos horas —y anular LIBERA CUPO—**, así que el estado de observación se recicla solo. Lo
dice en voz alta `scripts/probar_reactivacion.py` al terminar, y tiene la otra cara: **en un
día se ven bastantes más decisiones que `tope_diario_reactivacion`**, sin que salga un mensaje
de más. No manda de más; engaña a quien dimensione la campaña leyendo esos números tal cual.

Hubo que renumerar las tres migraciones —`021`→`023`, `022`→`024`, `023`→`025`— porque `main`
ya había ocupado el 021 y el 022 con la bitácora de asistencia y el perímetro de coste, las
dos ya aplicadas en producción. Es seguro porque `aplicar_esquema` no lleva registro de lo
aplicado: corre todas en cada arranque y todas son idempotentes.

---

## Lo único que de verdad puede encenderlo por accidente

**Los tres nombres de plantilla en el `.env` SON el interruptor. No hay un segundo seguro.**

`MAXICARE_REACTIVACION` vacío significa **ENCENDIDO** (su default es `"1"`); lo que mantiene
el sistema callado hoy es que `MAXICARE_PLANTILLA_SIN_AGENDAR` y sus dos hermanas están
vacías. El día que esos nombres entren al `.env` de un servidor que corra este código, empieza
a mandar en el barrido siguiente.

Por eso el orden de abajo separa ③ de ④, y **hacerlos juntos es encender a ciegas.**

---

## El orden del encendido

### ① Probar cada plantilla contra un teléfono real

```
uv run python scripts/probar_plantilla.py 57XXXXXXXXXX --tipo sin_agendar --plantilla reactivacion_sin_agendar
uv run python scripts/probar_plantilla.py 57XXXXXXXXXX --tipo cancelada   --plantilla reactivacion_cancelada
uv run python scripts/probar_plantilla.py 57XXXXXXXXXX --tipo no_asistio  --plantilla reactivacion_no_asistio
```

`--plantilla` existe justo para esto: **probar sin tocar el `.env`**, que es el interruptor.
Este script **sí gasta** — manda un mensaje de verdad. `--estado` no manda nada.

| Plantilla | Probada con un mensaje real |
|---|---|
| `reactivacion_sin_agendar` | sí, el 20/09/2026 — y encontró el séptimo fallo de la rama |
| `reactivacion_cancelada` | **PENDIENTE** |
| `reactivacion_no_asistio` | **PENDIENTE** |

Las tres están aprobadas por Meta (18/09/2026) y se editaron a tuteo el 20/09. **Editar una
plantilla aprobada la devuelve a revisión**: confirmar que las tres volvieron a `Active` antes
de probar. Los textos vigentes están en [`plantillas-meta-reactivacion.md`](plantillas-meta-reactivacion.md).

### ② Resolver la pregunta legal — ✅ RESUELTO el 21/09/2026: **es SEGUIMIENTO**

**Natalia Peñuela, que redactó la política, confirmó que la reactivación es seguimiento de una
solicitud iniciada por el titular (§6 y §12), no comunicación comercial (§5 y §10).**

Consecuencia directa: **lo construido cumple tal como está y no hay nada que cambiar.** No hace
falta un «sí» expreso antes del primer mensaje de reactivación; basta el aviso con derecho a
oponerse, que es como está implementado.

**Pero esa respuesta viene con una condición que no es decorativa, y hay que leerla al derecho:**
lo que convierte la reactivación en «seguimiento» y no en publicidad es que la persona pueda
oponerse y que esa oposición se respete. O sea que **el botón «Ya no, gracias» es lo que hace
verdadera la respuesta legal**, no un detalle de acabado. Si ese camino no funciona, la base
sobre la que Natalia dijo «seguimiento» deja de existir, y el sistema pasa a ser exactamente lo
que ella descartó. Por eso el ③bis dejó de ser una prueba conveniente y pasó a ser el requisito
del ④.

El memo que se le mandó está en
[`politica/para-marketing-2026-09-16.md`](politica/para-marketing-2026-09-16.md) y la nota de
envío en [`politica/correo-para-natalia-penuela-2026-09-21.md`](politica/correo-para-natalia-penuela-2026-09-21.md).

Lo que queda anotado de la pregunta, para quien tenga que reconstruirla: el PDF v2.0 se
contradice consigo mismo —§6 y §12 contra §5 y §10— y nadie del lado técnico podía resolverlo,
porque las dos lecturas se programan igual de bien y solo una cumple.

Y el dato que fue en el mismo correo, que **sigue vigente y sin resolver**: **Meta obliga a
clasificar las tres plantillas como *Marketing*, mientras la lectura de la política —ahora
confirmada— las trata como seguimiento.** No es contradictorio, son dos marcos etiquetando lo
mismo, pero queda por escrito en la consola de un tercero que MaxiCare no controla.

### ③ Fundir y desplegar **con los nombres de plantilla aún vacíos** — ✅ HECHO el 21/09/2026

Esto despliega el código sin que salga nada. Y abre la única ventana para lo de abajo.

**Estamos dentro de esa ventana desde el 21/09/2026.** Es el estado más barato para mirar y
el único en el que se puede probar el ③bis. No tiene fecha de caducidad: se puede quedar así
indefinidamente, y mientras tanto el barrido sigue enseñando a quién habría escrito.

### ③bis · Probar «Ya no, gracias» con un mensaje real

**Es el camino que protege el número y nunca se ha ejercitado.** `cerrar_seguimiento` no
existe en `main`, así que el botón negativo solo se puede probar **después** de ③ y **antes**
de ④. Si se salta esta ventana, el primer «Ya no, gracias» real lo pulsará un paciente.

### ④ Poner los nombres en el `.env` — 🟡 HECHO A MEDIAS el 21/09/2026: **solo `sin_agendar`**

Las tres cosas del arranque lento, comprobadas ese día antes de encender:

- **Calidad del número contra Meta:** `GREEN` · `TIER_250` · `CONNECTED`. ✅
- **`tope_diario_reactivacion` bajado de 20 a 5** en la tabla `configuracion`, con su fila de
  bitácora. El número puede iniciar 250 conversaciones cada 24 h y conviene quedarse muy por
  debajo: el tier sube solo si la calidad se mantiene, y llenarlo es la forma más rápida de que
  deje de subir. ✅
- **Un solo worker.** `barrido.encolar` no tiene candado: dos réplicas leerían el mismo
  `comprometidos` y cada una encolaría el tope entero. Sigue siendo uno. ✅

**Por qué solo una de las tres, y qué falta para las otras dos:**

`reactivacion_sin_agendar` es la única con el texto correcto, es el grueso del volumen —quien
preguntó y nunca agendó— y es la única probada de punta a punta contra WhatsApp real, con el
botón del «no» incluido.

**`reactivacion_cancelada` y `reactivacion_no_asistio` mezclan tuteo y usted**, y hay que
corregirlas antes de encenderlas:

| Plantilla | Header | Cuerpo |
|---|---|---|
| `reactivacion_cancelada` | «**Su** cita en MaxiCare» | tuteo, correcto |
| `reactivacion_no_asistio` | «**Su** cita en MaxiCare» | «no pudiste asistir a **su** cita» |

Es el mismo defecto que el paso a tuteo del 20/09 existía para arreglar —el paciente lee un
encabezado formal y un segundo después le contesta una Daniela que lo tutea— y que se quedó a
medias porque alcanzó los cuerpos y no los encabezados. **Lo encontró un mensaje real, no una
revisión**: octavo caso del mismo patrón en esta rama.

**Meta rechaza corregirlas hoy:** `error_subcode 2388124`, «solo puedes editar una plantilla
activa una vez cada 24 horas», y se editaron el 20/09. El script de usar y tirar que las
arregla está listo; hay que volver a correrlo pasadas las 24 h, esperar a que Meta las vuelva a
aprobar, probarlas con un mensaje real y entonces descomentar sus dos líneas en el `.env`
(quedaron ahí, comentadas, justo para eso).

---

## Trampa al dimensionar el ensayo en seco

En modo comprobación, una fila decidida «enviar» sin plantilla **queda pendiente hasta que R3
la anula a las 2 h, y anular libera cupo.** Así que el ensayo puede mostrar bastante más de
`tope_diario` **decisiones** al día sin que eso sea lo que saldría con plantilla puesta.

No manda de más — pero engaña a quien dimensione con esos números. **El ensayo en seco enseña
más decisiones que mensajes reales.**

---

## Decisiones de producto que no son mías

Ninguna bloquea el encendido, pero MaxiCare tiene que conocerlas.

1. **`/clearstate` se lleva la lápida del «no»**, por cascada desde `conversaciones`. El
   criterio del no negociable 25 no es el ALCANCE sino la PROPIEDAD: el contador es del
   sistema, pero la lápida es una frase que dijo el paciente. La más delicada de las cinco.
2. **Quien cae en las dos listas nunca recibe `reactivacion_cancelada`**: gasta su presupuesto
   entero en `reactivacion_sin_agendar`. Es la consecuencia correcta del diseño, no un defecto.
3. **Nada le pone cota superior a `fecha_objetivo`.** `_programar_seguimiento` solo rechaza el
   pasado, así que el modelo puede programar una reactivación a seis meses y saldrá puntual
   seis meses después. Acotado por el tope anual (R2) y por R5, pero sin techo en ninguna capa.
4. **`reactivacion_no_asistio` está declarada y sin disparador**: nadie escribe `citas.asistio`.
   La plantilla está aprobada y el código la sabe mandar; el día que la fase 8 escriba esa
   columna, basta añadir el tipo a `TIPOS_QUE_EL_BARRIDO_ENCOLA`.
5. **La categoría en Meta.** Ver ②.

---

## Dos frases del `CLAUDE.md` que este trabajo dejó falsas

No las toqué: el `CLAUDE.md` es del usuario y su redacción es decisión suya. Pero quien las lea
tras una compactación va a creer otra cosa. Son las dos últimas que quedan — las mismas frases
en `.claude/rules/atencion-whatsapp.md`, en `persistencia.py` y en `CLAUDE.md:44` («las doce
tools») ya están corregidas.

| Dónde | Qué dice | Qué pasa hoy |
|---|---|---|
| `CLAUDE.md:169` (no negociable 21) | «ninguna de las **siete** guardas» | son 9 para un recordatorio de cita y 13 para una reactivación; la cifra viva está en el docstring de `seguimientos.decidir` |
| `CLAUDE.md:209` (no negociable 25) | «la bitácora `consentimientos` **no se toca en ningún caso**» | `borrar_rastro` hace `UPDATE … SET detalle = NULL` e inserta un `rastro_borrado` — decisión deliberada del 17/09/2026 por el §13 de la política |

---

## Antes de encender, corre esto

```
uv run python scripts/probar_reactivacion.py
```

No gasta y no manda. Recorre el barrido, el despachador y las once reglas anti-reporte, y
emite sus tres advertencias — la de dimensionar el ensayo, la primera.

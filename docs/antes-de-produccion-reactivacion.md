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
Consecuencia de eso que conviene tener presente al llegar al ④: puede haber cola acumulada,
así que el `tope_diario_reactivacion` bajo del ④ pasa de ser prudencia a ser necesario.

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

### ② Resolver la pregunta legal — es lo único que bloquea de verdad

El párrafo está escrito en [`politica/para-marketing-2026-09-16.md`](politica/para-marketing-2026-09-16.md)
y **sigue sin enviarse a Natalia Peñuela**. Decisión del usuario el 20/09/2026: se manda
**justo antes de salir a producción**, no antes.

Lo que hay que confirmar: el PDF v2.0 se contradice consigo mismo. Los §6 y §12 permiten el
seguimiento de una solicitud iniciada por el titular; los §5 y §10 lo niegan. Nadie del lado
técnico puede resolver eso.

Y un dato que conviene llevar en el mismo correo: **Meta obliga a clasificar las tres
plantillas como *Marketing*, mientras nuestra lectura de la política las trata como
seguimiento.** No es contradictorio —son dos jurisdicciones etiquetando lo mismo— pero es
exactamente la ambigüedad sin resolver, ya puesta por escrito en una consola de Meta.

### ③ Fundir y desplegar **con los nombres de plantilla aún vacíos** — ✅ HECHO el 21/09/2026

Esto despliega el código sin que salga nada. Y abre la única ventana para lo de abajo.

**Estamos dentro de esa ventana desde el 21/09/2026.** Es el estado más barato para mirar y
el único en el que se puede probar el ③bis. No tiene fecha de caducidad: se puede quedar así
indefinidamente, y mientras tanto el barrido sigue enseñando a quién habría escrito.

### ③bis · Probar «Ya no, gracias» con un mensaje real

**Es el camino que protege el número y nunca se ha ejercitado.** `cerrar_seguimiento` no
existe en `main`, así que el botón negativo solo se puede probar **después** de ③ y **antes**
de ④. Si se salta esta ventana, el primer «Ya no, gracias» real lo pulsará un paciente.

### ④ Poner los nombres en el `.env`

Y ese día, las tres cosas del arranque lento:

- **Verificar la calidad del número contra Meta** antes de nada (`quality_rating`).
- **`tope_diario_reactivacion` por debajo de 20.** El número está en `TIER_250` —puede iniciar
  250 conversaciones nuevas cada 24 h— y conviene quedarse muy por debajo: el tier sube solo
  si la calidad se mantiene, y llenarlo es la forma más rápida de que deje de subir.
- **Un solo worker.** `barrido.encolar` no tiene candado: dos réplicas leerían el mismo
  `comprometidos` y cada una encolaría el tope entero.

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

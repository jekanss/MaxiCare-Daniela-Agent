# Reactivación de leads · diseño

17 de septiembre de 2026. Sub-proyecto D del trabajo que abrió marketing el 15/09/2026.
Se apoya en el cimiento que cerró [contacto y consentimiento](2026-09-16-contacto-y-consentimiento-design.md).

## El problema

Alguien escribe a las 22:47 preguntando cuánto vale una limpieza. Daniela le responde, le
ofrece hora, y la persona se duerme. A las 24 h la conversación caduca y **ese lead desaparece
del sistema**. No dijo que no: se le fue.

Le pasa a la clínica varias veces por semana, y es el grupo más barato de convertir que existe:
gente que ya levantó la mano.

## El principio que decide los empates

La regla general del proyecto es que la seguridad clínica prevalece sobre lo comercial. Aquí
hay una segunda, propia de este sub-proyecto:

> **Un número de WhatsApp reportado no es un problema de la reactivación: mata el proyecto
> entero.** Si Meta degrada la calidad del número, se caen también los recordatorios de cita,
> los escalamientos y la atención normal. El daño no se queda en la función que lo causó.

De ahí: **ante la duda, no mandar.** Un lead perdido cuesta una limpieza; un número quemado
cuesta el sistema. Requisito expreso del usuario el 17/09/2026.

## Alcance

**Dentro:**

1. Un barrido periódico que decide a quién escribirle y encola.
2. Que el despachador sepa mandar las plantillas de reactivación, no solo la de cita.
3. Que el botón «Ya no, gracias» (y su equivalente escrito) cierre ese seguimiento.
4. Un contador de rechazos por persona, con su apagado y su reset.
5. Las once reglas anti-reporte de la sección 3, cada una con su prueba.
6. Un interruptor que apaga el barrido sin redesplegar.

**Fuera, a propósito:**

- **La memoria del reencuentro.** Daniela reconoce a quien vuelve (la fila de `contactos` es
  permanente) pero no recuerda de qué hablaron: pasadas 24 h `conversacion_viva` abre una
  conversación nueva con historial en blanco. Es un sub-proyecto aparte y no bloquea éste.
- **La reactivación de «no asistió».** La columna `citas.asistio` existe desde la 001 y
  **nadie la escribe**: la pantalla de agenda tiene los botones dibujados sobre datos
  inventados a mano (`web/src/pantallas/Agenda.tsx:50`) y no hay endpoint detrás. La plantilla
  se pide igual —la aprobación tarda— pero el tipo queda declarado y sin disparador hasta que
  cierre la fase 8. **Que quede declarado y muerto es deliberado**: el día que la fase 8
  escriba `asistio`, encenderlo es cambiar una constante.
- **Importar la cartera histórica.** Los pacientes viejos viven en el WhatsApp del consultorio,
  no en el sistema. Marketing decidió el 15/09/2026 que solo cuentan los nuevos. Si algún día
  se quiere lo otro, es un proyecto aparte y se cotiza aparte.

## 1. Las decisiones, con la alternativa que se descartó

### D1 · El barrido decide, no el modelo

**Decisión:** un proceso periódico recorre la cartera y encola. La tool `programar_seguimiento`
sigue existiendo y Daniela puede usarla dentro de una conversación, pero **no es el camino
principal**.

**Alternativa descartada:** que Daniela decida al final de cada turno si programa un
seguimiento. Se descarta por lo mismo que el recordatorio lo emite el código y no el modelo (no
negociable 21): un olvido del modelo no deja error en ningún log, y aquí el olvido es
silencioso en los dos sentidos —ni se manda ni se sabe que no se mandó—.

### D2 · «Ya no, gracias» cierra ESE seguimiento, no la relación

**Decisión:** el botón negativo cierra el seguimiento en curso y suma uno al contador. **No**
marca `no_contactar`.

**Alternativa descartada:** tratarlo como baja comercial permanente. Se descarta porque «ya no
me interesa esta consulta» no es «no me escriban nunca más», y marcar la baja quema a un
paciente por una frase que no dijo. La baja de verdad ya tiene su camino y funciona.

**Corolario para el texto libre:** ante un «no gracias» ambiguo, **cerrar el seguimiento y NO
marcar la baja**. Es el lado barato de equivocarse: quien quiere la baja lo dice más claro, y
marcar una baja permanente por una frase ambigua es lo único de los dos que no se puede
deshacer sin que la persona vuelva a pedirlo.

### D3 · El contador cuenta el silencio igual que el «no»

**Decisión:** un seguimiento que termina sin respuesta suma lo mismo que uno que termina en un
«no» explícito.

**Alternativa descartada:** contar solo los rechazos explícitos. Se descarta porque quien ignora
dos campañas enteras está diciendo lo mismo que quien contesta «no gracias», solo que sin
escribir —y es además el grupo del que sale un reporte, no del que contesta—.

### D4 · El contador se resetea al agendar

**Decisión:** agendar una cita pone el contador en 0.

**Alternativa descartada:** que el apagado sea permanente. Se descarta porque convierte el freno
en una trampa: alguien que ignoró dos veces y al final agendó demostró justo lo contrario de lo
que el contador supone.

### D5 · Apagado y baja son cosas distintas y no se mezclan

|  | **Apagado** | **Baja** (`no_contactar`) |
|---|---|---|
| Quién decide | el sistema | la persona |
| Significa | «a esta persona no le sirve que la persigamos» | «no me escriban más» |
| Recordatorios de cita | llegan | llegan |
| Puede escribir y agendar | sí | sí |
| Se deshace | agendando | solo si ella lo pide |
| Va a la bitácora legal | **no** | **sí** |

El apagado es cortesía operativa y vive en `contactos`. La baja es un derecho y vive además en
`consentimientos`, que no se puede borrar. **Escribir nunca se apaga**: la baja apaga lo que el
sistema MANDA, jamás lo que la persona escribe.

### D6 · Categoría Marketing en Meta, «seguimiento» en la Ley 1581

No es contradicción: son dos jurisdicciones etiquetando la misma cosa. Meta clasifica por si
hay una transacción detrás (no la hay), la política por si lo abrió el titular (sí). Pedirlas
como Utility solo consigue que Meta las reclasifique o las rechace. El detalle de las tres
plantillas, en `docs/plantillas-meta-reactivacion.md`.

**Lo que sigue abierto y no es de código:** el PDF v2.0 no zanja el permiso —§6 y §12 lo
permiten, §5 y §10 lo contradicen—. La lectura del proyecto es que la reactivación es
*seguimiento de una solicitud que abrió el propio titular*, y esa lectura **sostiene las cinco
condiciones de abajo**. Falta que la confirme quien redactó la política
(`docs/politica/para-marketing-2026-09-16.md`, escrito y sin enviar).

Sigue siendo seguimiento mientras: lo abrió la persona · es corto (2 intentos) · es sobre lo
mismo que preguntó · se acaba solo · tiene salida en cada mensaje. **Se convierte en publicidad
en cuanto el mensaje ofrezca algo que la persona no pidió**, y eso no se rompe en el código: se
rompe cuando alguien redacte el texto de una plantilla nueva.

## 2. Los dos tipos que sí se pueden construir hoy

| Tipo | Quién califica |
|---|---|
| `reactivacion_sin_agendar` | tiene conversación, **sin cita futura**, su último mensaje fue hace ≥ 24 h |
| `reactivacion_cancelada` | canceló una cita hace ≥ 24 h y **no tiene cita futura** |
| ~~`reactivacion_no_asistio`~~ | **declarado y sin disparador**: nadie escribe `citas.asistio` |

«Sin cita futura» es la condición que los tres comparten y la más importante: a quien ya tiene
hora no se le persigue.

## 3. Las once reglas anti-reporte

Cada una es un requisito con su prueba. **Una prueba por regla, que falle si alguien la
afloja** — no una prueba que recorra el camino feliz.

| # | Regla | De dónde sale |
|---|---|---|
| 1 | Solo a quien escribió PRIMERO. Nunca frío, nunca comprado. | la defensa más fuerte que hay |
| 2 | Máximo 2 intentos por consulta (24 h y 7 días) | marketing, 15/09/2026 |
| 3 | Botón de salida en cada mensaje | las tres plantillas |
| 4 | La baja se respeta, y es permanente | migración 019, ya construido |
| 5 | El freno del contador: 2 fallos seguidos y se apaga | D3, D4 |
| 6 | **Horario decente**: nada antes de las 9:00 ni después de las 19:00, y nada en domingo | 17/09/2026 |
| 7 | **No pisarle la conversación**: si habló con Daniela en las últimas 24 h, no sale nada | más ancha que la guarda de 60 min del recordatorio |
| 8 | **Tope por persona y año**: 6 mensajes de reactivación en 12 meses, pase lo que pase | el límite de 2 por consulta protege la consulta, no a la persona |
| 9 | **Arranque lento**: tope diario bajo las primeras semanas | Meta castiga los picos |
| 10 | **Interruptor de pánico**: una variable apaga el barrido sin redesplegar | todo lo demás sigue |
| 11 | **Que se apague solo** si la calidad del número baja | **VERIFICADO** el 17/09/2026 contra el número real |

La 8 no es redundante con la 5: el contador se resetea al agendar (D4), así que alguien que
agenda cada vez podría recibir muchos en un año. El tope anual es el techo que el contador no
pone.

### D7 · La calidad se PREGUNTA, no se espera a que la avisen

La 11 estaba marcada `POR VERIFICAR` y se verificó. El resultado cambió el diseño.

**Decisión: el barrido consulta la calidad del número antes de encolar**, con un `GET` a
`graph.facebook.com/v21.0/{phone_number_id}?fields=quality_rating,messaging_limit_tier,status`.
Comprobado el 17/09/2026 contra el número de producción: devuelve `quality_rating: "GREEN"`,
`messaging_limit_tier: "TIER_250"`, `status: "CONNECTED"`. Es una lectura, no cuesta nada, y no
hace falta suscribir ningún campo en la consola de Meta.

**Alternativa descartada: el webhook `phone_number_quality_update`.** Existe, pero (a) exige
suscribir el campo en la App Dashboard —configuración externa que un despliegue no puede
garantizar—, (b) su payload no está documentado con precisión suficiente para escribir código
contra él sin suponer, y sobre todo (c) **un webhook se puede perder**: si el servidor está
caído cuando Meta lo manda, Meta reintenta y desiste, y el sistema se queda creyendo que todo
va bien justo cuando no va bien. Una consulta que se hace antes de cada barrido no se pierde.

Es el mismo patrón que el proyecto ya usa dos veces: el no negociable 26 —«delante del doctor»
se COMPRUEBA, no se supone— y el 19 —un tema borrado no emite ningún evento, así que se sonda
con `reopenForumTopic`—. **Aquí vuelve a ganar preguntar.**

**Qué hace con la respuesta:**

| `quality_rating` | Qué pasa |
|---|---|
| `GREEN` | encola normal |
| `YELLOW`, `RED`, `FLAGGED` | **no encola nada** y avisa al General por Telegram, una vez por día |
| `UNKNOWN`, `NA` | encola: son los valores de un número sin historial suficiente, no una señal de daño |
| la consulta falla | **no encola**, y lo dice en el log |

La última fila es el principio del sub-proyecto aplicado: ante la duda, no mandar. El coste de
equivocarse por ese lado son leads no contactados durante una hora —el barrido reintenta al
ciclo siguiente—; por el otro lado es seguir mandando con la calidad ya en rojo. Nótese que la
asimetría es la **contraria** a la del no negociable 26, donde ante la duda se AVISA: allí lo
barato era molestar al doctor de más, aquí lo barato es callarse.

**Y un dato que salió de la misma comprobación:** el número está en `TIER_250` — puede iniciar
250 conversaciones nuevas cada 24 h. El tope diario de la regla 9 (20) queda muy por debajo, y
conviene que siga así: el tier sube solo si la calidad se mantiene, y llenarlo es la forma más
rápida de que deje de subir.

## 4. Lo que NO garantiza este diseño

Conviene decirlo antes de construir, no después.

**Ninguna prueba offline puede garantizar que la gente no reporte.** Eso depende del texto y de
la frecuencia, y solo se mide en producción. Este proyecto ya tiene el precedente: los tres
fallos del camino de botones del recordatorio (15/09/2026) **no los cazó ninguna prueba** y no
dejaban error en ningún log.

Lo que sí se puede garantizar con pruebas es que ninguna de las once reglas se pueda saltar. Y
lo que sostiene el resto es el procedimiento: **el camino entero corre contra la API real antes
de encender nada**, y el arranque es lento y vigilado.

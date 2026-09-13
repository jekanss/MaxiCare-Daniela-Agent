# Fase 6A — Daniela contesta por WhatsApp

**Fecha:** 2026-09-12
**Fase del plan:** 6 «Ingesta, el muro y el relevo», primera de tres entregas
**Estado del resto:** 6B (el muro) y 6C (el relevo) quedan fuera de este documento

---

## 1. El problema

Daniela está construida, probada y **desconectada**. Hoy, cuando un paciente escribe:

```
paciente ──WhatsApp──> webhook ──> ingesta ──> Telegram (tema General)
                          │
                          └──> 200 OK, y nada más
```

El paciente no recibe respuesta. `conversacion.responder` —el único sitio del proyecto con
`Runner.run`, con sus seis guardrails y su reintento— existe desde la fase 5 y **solo lo
llama el chat de pruebas web**. Las nueve tools corren contra Neon y contra Google Calendar,
y ningún paciente las ha visto nunca.

Esta entrega es el cable. No construye capacidades nuevas: conecta las que hay.

## 2. Alcance

**Entra:**

- Un turno completo de WhatsApp: mensaje entrante → `conversacion.responder` → respuesta
  enviada al paciente.
- La conversación correcta para ese teléfono, y la identidad sin fricción que el plan
  define en `contexto.identidad_solicitante`.
- El calendario real de Google en el contexto (`calendario_desde_config`, que hoy no la
  llama nadie).
- El retardo humano de `config.RETARDO_RESPUESTA_SEGUNDOS`.
- Serialización por conversación.
- El aviso a los doctores cuando el orquestador escala (`al_escalar`).
- Registro de qué se respondió, para poder auditarlo.
- Un interruptor para callar a Daniela sin desplegar código.

**No entra** (y el código tiene que dejar claro que no entra):

- Interpretar el contenido de un archivo. El `lector_archivos` sigue sin invocarse: es 6B.
- Crear temas de Telegram por paciente: es 6B.
- El relevo, en cualquiera de sus formas: es 6C. `ContextoDaniela.tomada_por` se rellena
  desde `persistencia.conversacion_tomada`, que ya existe, pero nada lo pone en True
  todavía, así que en la práctica siempre será `None`.
- Sesión persistente. Es la fase 7 (`SQLAlchemySession`).
- Lista blanca de teléfonos. **MaxiCare la descartó explícitamente** (12/09/2026): el
  número no es público y lo conoce solo el dueño del proyecto.

## 3. Arquitectura

### 3.1 Un módulo nuevo: `atencion.py`

El turno de WhatsApp **no vive en `runtime.py`**. Vive en `src/maxicare_daniela/atencion.py`,
que entra en `MODULOS_SIN_TRANSPORTE` de `tests/test_estructura.py`.

La razón es la misma por la que `conversacion.py` existe fuera de `runtime.py`: un turno que
supiera de FastAPI no se podría probar sin levantar un servidor, y el día que los mensajes
entren por otra vía habría que reescribirlo. `runtime.py` queda con una sola línea nueva de
verdad: `tareas.add_task(atencion.atender, ...)`.

```
runtime.py            recibe el POST, valida la firma de Meta, responde 200
   │                  (todo esto ya existe y no cambia)
   └──> atencion.py   EL TURNO COMPLETO, sin saber que vino de HTTP
          ├──> ingesta.py          registra y reenvía a Telegram  (ya existe)
          ├──> persistencia.py     conversación, paciente, configuración
          ├──> calendario.py       calendario_desde_config
          ├──> conversacion.py     responder()  (ya existe)
          └──> canales.py          WhatsApp.enviar_texto  (ya existe, sin usar)
```

### 3.2 El flujo, paso por paso

```
1. lock de esta conversación         ──> §4.2
2. conversación viva del teléfono    ──> §4.1
3. identidad por teléfono            ──> §4.3
4. construir ContextoDaniela         ──> §4.4
5. conversacion.responder(...)       ──> ya existe
6. retardo humano                    ──> §4.5
7. whatsapp.enviar_texto             ──> ya existe
8. anotar lo respondido              ──> §4.6
```

El orden de 6 y 7 importa: el retardo va **antes** de enviar, no antes de pensar. Pensar ya
tarda, y el tope es del total.

## 4. Decisiones

### 4.1 Cuándo es «la misma conversación»

**Decisión:** se reutiliza la última conversación de ese teléfono si su `actualizada_en`
está dentro de las últimas **24 horas**. Si no, se abre una nueva.

**Por qué hace falta decidirlo:** `persistencia.asegurar_conversacion` engaña con el
nombre —**siempre INSERTA una fila nueva**, no es un get-or-create—. Usada tal cual en
WhatsApp, cada mensaje abriría una conversación distinta: Daniela no recordaría la frase
anterior, el `turno_actual` sería siempre 1, el estado de oportunidad se fragmentaría en
una fila por mensaje y las claves de idempotencia nunca colisionarían con nada, con lo que
dejarían de proteger.

**Por qué 24 horas:** un silencio de más de un día es un asunto nuevo, y coincide con la
ventana de servicio de WhatsApp. Debajo de eso, retomar la charla es lo natural.

**Alternativa descartada:** una conversación por teléfono para siempre. Cuesta que el estado
de oportunidad de un paciente que volvió a los seis meses arrastre la barrera de la vez
anterior, y que `turno_actual` crezca sin techo.

**Se reabre si:** aparece la sesión persistente de la fase 7, que puede querer otra ventana.

### 4.2 Serialización por conversación

**Decisión:** un `asyncio.Lock` por `id_conversacion`, en un diccionario de módulo.

**Por qué:** es el defecto más probable de toda la fase. Sin él, dos mensajes seguidos del
mismo paciente —algo cotidiano en WhatsApp— lanzan dos `Runner.run` concurrentes sobre el
**mismo objeto `ContextoDaniela`**. `conversacion.responder` hace `ctx.turno.reiniciar()` y
`ctx.turno_actual += 1` al empezar: el segundo turno le borra al primero las cifras y horas
autorizadas, y entonces el guardrail de salida del primero bloquea **por falso positivo**.
El paciente recibe el mensaje genérico de seguridad sin que nada estuviera mal. Además las
dos corridas comparten la clave de idempotencia `id_conversacion + turno_actual`.

**Alternativa descartada:** una cola por teléfono con un worker. Es más robusta y cuesta un
mecanismo entero de encolado para un problema que un lock resuelve con un worker, que es
como corre el contenedor hoy.

**Se reabre si:** el despliegue pasa a varios workers o varias réplicas. Entonces el lock en
memoria deja de valer y hace falta uno en Postgres (`pg_advisory_lock` sobre el uuid).
**Esto se documenta en el código, no solo aquí.**

### 4.3 Identidad

**Decisión:** si `persistencia.buscar_paciente_por_telefono` encuentra al paciente, el
contexto arranca con `identidad_verificada=True`, `id_paciente` y `nombre_paciente`.

**Por qué:** es literalmente lo que el plan ya decidió en `contexto.identidad_solicitante`:
«si ese número ya está en Neon, la identidad queda verificada sin fricción y ese es el caso
común». No es una decisión nueva, es la aplicación de una cerrada.

El caso del familiar que escribe por un tercero lo cubre el flujo que ya existe:
`identificar_paciente` pide nombre y teléfono de registro, con dos intentos.

### 4.4 El contexto

Todos los campos de `ContextoDaniela` se rellenan desde `config` y desde la configuración
operativa de Neon. Dos que el chat de pruebas deja vacíos **a propósito** y que aquí son
obligatorios:

- `telegram_bot_token` y `telegram_chat_doctores`: sin ellos, `escalar_a_doctores` falla en
  silencio y el doctor nunca se entera. El chat web los deja en `""` para no hacerle sonar
  el teléfono a nadie durante una prueba; en WhatsApp son el canal real.
- `calendario`: `calendario_desde_config(config)`, no `CalendarioDoble()`. Es donde el
  trabajo del calendario entra en producción.

`tema_general` sale de la configuración operativa, igual que hoy.

### 4.5 El retardo humano

**Decisión:** se sortea un objetivo dentro de `RETARDO_RESPUESTA_SEGUNDOS` y se espera
**solo lo que falte** para alcanzarlo, midiendo desde antes de `Runner.run`.

```
objetivo = uniform(4, 55)
espera   = max(0, objetivo - lo_que_tardo_el_turno)
```

**Por qué:** `limites.latencia_maxima` del brief dice «tope máximo de un minuto», y eso
incluye lo que tarda el modelo. Sumar 55 segundos encima de un turno de 12 llevaría a 67 y
rompería el límite que el retardo existe para respetar. El retardo no está para hacer
esperar: está para que una respuesta instantánea no delate a un bot.

### 4.6 Registrar lo respondido — migración 009

**Decisión:** `mensajes_entrantes` gana cuatro columnas:

| Columna | Para qué |
|---|---|
| `conversacion_id UUID` | liga el mensaje con su conversación; hoy no hay forma de saber a cuál pertenece |
| `respondido_en TIMESTAMPTZ` | cuándo se le contestó al paciente |
| `wamid_respuesta TEXT` | el id que devuelve `enviar_texto`, para rastrearlo en Meta |
| `fallo_respuesta TEXT` | por qué no se le contestó, si no se le contestó |

**Por qué:** hoy la tabla solo registra el viaje hacia Telegram. Si Daniela deja a un
paciente sin contestar, no queda rastro en ninguna parte: ni en la tabla, ni en la
conversación. El día que pase —y va a pasar— la pregunta «¿le contestamos a esta persona?»
no tendría respuesta.

`ADD COLUMN IF NOT EXISTS`, y **no se edita ninguna migración anterior**.

### 4.7 El interruptor

**Decisión:** `MAXICARE_DANIELA_RESPONDE`, por defecto **activa**. Con `0`, el webhook hace
exactamente lo que hace hoy —registrar y reenviar a Telegram— y no responde nada.

**Por qué por defecto activa:** MaxiCare pidió que responda desde el primer despliegue. El
interruptor no está para restringir sino para **poder apagarla en diez segundos** sin abrir
un editor ni desplegar, que es lo que hace falta a las dos de la mañana. Un sistema que
escribe a personas y no se puede callar rápido es un sistema del que uno no se fía.

### 4.8 Un archivo, mientras 6B no exista

**Decisión:** cuando el mensaje trae archivo, Daniela recibe una línea que dice **qué es**,
nunca qué muestra —«El paciente envió una imagen»— más el pie de foto si lo hay. Y se marca
`ctx.turno.hubo_adjunto = True`.

**Por qué la marca:** es lo que activa el guardrail `sin_lectura_clinica`, que vigila que la
respuesta no describa contenido clínico. Sin marcarlo, el guardrail no correría justo en el
turno donde importa.

El muro no depende de esta decisión: se sostiene por construcción, porque nadie genera un
`LecturaArchivo` todavía. Pero el texto que se le pasa al modelo tiene que respetar la misma
regla que `ingesta.componer_aviso` ya respeta — decir lo que el archivo ES, nunca lo que
MUESTRA— para que cuando 6B llegue, no haya que revisar este camino.

## 5. Límites conocidos, aceptados y documentados

Cada uno lleva su prueba, no un comentario.

1. **El historial vive en memoria.** Si el proceso se reinicia, Daniela olvida la
   conversación. No olvida los datos —paciente, citas, estado de oportunidad están en
   Neon—, olvida el hilo del diálogo. Lo arregla la fase 7 con `SQLAlchemySession`.
2. **El lock es de proceso.** Con varios workers o réplicas deja de proteger. El
   contenedor corre con uno.
3. **El diccionario de sesiones crece.** Se poda por antigüedad; una conversación sin
   actividad en 24 h se descarta de memoria, coherente con §4.1.
4. **Sin el relevo, `tomada_por` siempre es `None`.** El campo se lee de Neon desde ya
   —no cuesta nada y evita tener que volver aquí en 6C— pero nada lo escribe todavía.

## 6. Verificación

**Offline (`uv run pytest -q`), sin red ni base:**

- La conversación se reutiliza dentro de la ventana y se abre nueva fuera de ella.
- Dos mensajes concurrentes de un mismo teléfono se serializan, y de dos teléfonos
  distintos **no** se serializan (o el lock sería un cuello de botella global).
- Un número conocido entra identificado; uno desconocido, no.
- El retardo descuenta lo que tardó el turno, y nunca es negativo.
- Con el interruptor apagado no se envía nada.
- Un mensaje con archivo marca `hubo_adjunto` y el texto que ve el modelo no interpreta.
- Si `enviar_texto` falla, queda escrito en `fallo_respuesta` y no se pierde en un log.

**Entregable (`uv run python scripts/probar_atencion.py`):** el turno completo contra Neon
—esquema de pruebas— con un doble de WhatsApp que captura lo enviado en vez de mandarlo.
Comprueba el camino entero salvo la última milla.

**La última milla la comprueba una persona:** escribirle por WhatsApp al número de MaxiCare
y recibir respuesta. Ningún script puede hacerlo por nosotros, y decir que sí podría sería
mentir sobre lo que está verificado.

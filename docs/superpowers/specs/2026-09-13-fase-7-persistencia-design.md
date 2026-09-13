# Fase 7 — Persistencia del historial y trazas agrupadas

**Fecha:** 2026-09-13
**Estado:** diseño aprobado, sin implementar
**Entregable del plan (`fases[6]`, literal):** «Una conversación sobrevive a reiniciar el
proceso y Daniela la retoma con su contexto. Su trace aparece agrupado por `group_id` y no
contiene una sola línea de texto de la conversación.»

La segunda mitad de esa frase ya está cerrada desde el commit `654694d`: ninguna llamada al
modelo sube el contenido de la conversación a las trazas. Lo que falta es la primera mitad
entera y el `group_id`.

---

## 1. El problema

Hoy el historial del diálogo vive en un diccionario de módulo:

```
atencion._sesiones: dict[str, tuple[SesionEnMemoria, float]]
                         └── clave: id_conversacion
```

Un reinicio del proceso lo borra. No borra los datos —paciente, citas, estado de la
oportunidad y mensajes entrantes están en Neon— pero sí **la conversación**: lo que el
paciente contó, lo que Daniela ya le respondió, y en qué punto del camino iban.

Eso choca de frente con el objetivo 4 del brief, la continuidad longitudinal, y choca
también con algo más inmediato: el relevo, que es el sub-proyecto siguiente, dura tres
horas. La probabilidad de un despliegue dentro de esa ventana no es teórica.

Hay un segundo agujero, menos visible y peor. **El texto que Daniela responde no se guarda
en ninguna columna.** `mensajes_entrantes` tiene el texto del paciente; de la respuesta solo
quedan `wamid_respuesta` y `respondido_en`. Hoy la base permite reconstruir lo que el
paciente escribió y nunca lo que Daniela contestó. La sesión persistida cierra ese agujero
de paso, porque guarda el ida y vuelta completo.

Y un tercero, declarado y nunca cableado: `config.LIMITE_HISTORIAL_SESION = 40` aparece una
sola vez en todo el repositorio, en la línea donde se define.

---

## 2. Alcance

**Entra:**

- La sesión persistente (`SQLAlchemySession`) sobre la Neon que ya existe, con
  `session_id = id_conversacion`.
- La migración `010_sesiones_agente.sql`, que crea las dos tablas del SDK.
- El recorte del historial, con el número fijado **midiendo**, no estimando.
- `/clearstate` borrando también el historial del agente.
- El chat web del panel migrado al mismo mecanismo, en su esquema `pruebas_web`.
- `group_id` y `trace_metadata` en los tres llamadores de `config_de_corrida()`.
- Las pruebas, offline y contra Neon, más el script de fase.

**No entra:**

- **El relevo.** Es el sub-proyecto siguiente. Sus decisiones ya están tomadas (ver §7).
- **La revisión legal y el cifrado.** La decisión provisional del plan se mantiene tal cual.
  El riesgo queda escrito en §6, no resuelto en silencio.
- **Compactación con modelo.** El plan la descartó: pagar una llamada para producir prosa
  que puede perder el dato que importaba, cuando el estado estructurado ya tiene esos datos
  como campos, es pagar dos veces por algo peor.

---

## 3. Arquitectura

### 3.1 Lo que cambia

```
   ┌─ HOY ──────────────────────────┐      ┌─ DESPUÉS ───────────────────────────┐
   │ atencion._sesiones{}            │      │ agent_sessions   (Neon)             │
   │   dict de módulo                │ ───► │ agent_messages   (Neon)             │
   │   muere con el proceso          │      │   una fila por item, JSON           │
   │                                 │      │   session_id = id_conversacion      │
   │ runtime._conversaciones_de_     │      │                                     │
   │   prueba{}  (chat web)          │ ───► │ las mismas tablas en pruebas_web    │
   └─────────────────────────────────┘      └─────────────────────────────────────┘
```

`SesionEnMemoria` **no se borra**: sigue siendo el doble de las pruebas offline, que no
tocan la red. Lo que cambia es que deja de ser lo que corre en producción.

### 3.2 La URL: la trampa que hay que escribir en el código

`SQLAlchemySession.from_url` llama a `create_async_engine`, así que exige un driver
asíncrono. Verificado ejecutando contra el paquete instalado (0.22.2):

```
FAIL  postgresql://…            ModuleNotFoundError: No module named 'psycopg2'
OK    postgresql+psycopg://…    dialect=postgresql  is_async=True
OK    postgresql+asyncpg://…    dialect=postgresql  is_async=True
```

`MAXICARE_DATABASE_URL` es `postgresql://` a secas. Falla **en el constructor**, antes de
tocar la red, y el mensaje habla de `psycopg2`, un paquete que este proyecto no usa: el
rastro apunta al sitio equivocado. Hay que reescribir el esquema de la URL en código, en una
función con nombre y con su prueba.

**Se elige `postgresql+psycopg`** sobre `asyncpg`, aunque los dos funcionan y los dos están
instalados, por una razón concreta: la URL de Neon lleva `sslmode=require` y
`channel_binding=require` como parámetros de consulta. psycopg 3 los entiende porque son
suyos; asyncpg usa otro vocabulario (`ssl=`) y habría que traducirlos a mano. Además psycopg 3
ya es el driver del proyecto: entra un dialecto nuevo, no una librería nueva.

### 3.3 El esquema: por qué no vale el `search_path`

`SQLAlchemySession` no tiene parámetro de schema. Un grep sobre su fuente devuelve cero
líneas con `schema`: crea las tablas sin cualificar y caen donde apunte el `search_path`.

Y el pooler de Neon rechaza `options=-c search_path=...` como parámetro de arranque — ya
está documentado en `CLAUDE.md` y costó una tarde en la fase 3.

La salida es `execution_options={"schema_translate_map": {None: "<esquema>"}}` dentro de los
`engine_kwargs`. SQLAlchemy cualifica las sentencias al compilarlas, no al abrir la conexión,
así que el pooler no tiene nada que rechazar. Comprobado que se aplica también al DDL, no
solo al DML.

Eso mantiene intacta la regla del repositorio: **las pruebas nunca escriben en `public`.**

### 3.4 Las tablas las crea una migración, no el SDK

`create_tables=False`. La `010_sesiones_agente.sql` crea `agent_sessions` y `agent_messages`
con las columnas exactas que el SDK espera:

```
agent_sessions   session_id  VARCHAR PK
                 created_at  TIMESTAMP  NOT NULL  DEFAULT CURRENT_TIMESTAMP
                 updated_at  TIMESTAMP  NOT NULL  DEFAULT CURRENT_TIMESTAMP

agent_messages   id           INTEGER PK autoincrement
                 session_id   VARCHAR NOT NULL  FK -> agent_sessions ON DELETE CASCADE
                 message_data TEXT    NOT NULL
                 created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                 INDEX idx_agent_messages_session_time (session_id, created_at)
```

Ojo con un detalle heredado del SDK: `created_at` y `updated_at` son
`TIMESTAMP WITHOUT TIME ZONE`, no `TIMESTAMPTZ` como el resto de este esquema. Se respeta lo
que el SDK espera en vez de mejorarlo; una columna «mejor» que la que el SDK escribe es una
incompatibilidad esperando.

**Por qué migración y no `create_tables=True`:** el esquema completo se sigue leyendo en
`migraciones/`, `inicializar_base.py` sigue siendo la puerta única, y el proceso web no
ejecuta DDL al arrancar. El riesgo del camino elegido —que una versión futura del SDK cambie
el esquema y la migración quede vieja, en silencio— lo caza la prueba de ida y vuelta de §5.

### 3.5 Qué cambia en cada archivo

| Archivo | Cambio |
|---|---|
| `migraciones/010_sesiones_agente.sql` | nuevo: las dos tablas, idempotente |
| `persistencia.py` | la fábrica de sesiones: reescritura de la URL, engine, esquema |
| `atencion.py` | `_sesiones` deja de ser el almacén; `_sesion_de` pide la persistida |
| `conversacion.py` | `SesionEnMemoria` queda marcada como doble de pruebas |
| `config.py` | `config_de_corrida()` pasa a recibir argumentos; hash del prompt |
| `lectura.py` · `guardrails.py` | propagan el `group_id` a su `config_de_corrida()` |
| `reseteo.py` | borra también las filas del historial del agente |
| `runtime.py` | el chat web usa la sesión persistida en `pruebas_web`; pool dimensionado |

### 3.6 El `group_id`

`group_id` no aparece hoy en una sola línea de `src/`. El plan es explícito: es **el UUID de
la conversación, no el teléfono**. La distinción importa: un teléfono agrupa a una persona
para siempre; una conversación agrupa un episodio, que es la unidad que alguien va a querer
leer cuando llegue un reclamo.

`trace_metadata` lleva el canal (`whatsapp` o `web`) y la versión del prompt.

Los tres llamadores de `config_de_corrida()` —el turno de Daniela, el lector de archivos y
los evaluadores de guardrail— tienen que caer bajo el mismo `group_id`. Si el lector queda
fuera, el trace agrupado tendrá un agujero justo en los turnos con archivo, que son los más
interesantes de leer.

**La versión del prompt** no existe en el código, así que hay que inventar el mecanismo: un
hash corto del texto **estático** de las instrucciones, calculado al importar. Se mueve solo
cuando el prompt cambia de verdad y nadie tiene que acordarse de subir un número. El
vocabulario de tratamientos queda fuera del hash a propósito: se pega al final y cambia sin
desplegar, así que incluirlo haría que cada tratamiento nuevo pareciera un prompt nuevo.

---

## 4. Decisiones

### 4.1 El límite se mide antes de fijarse

El plan dice `SessionSettings(limit=40)` y lo justifica como «≈5 conversaciones completas».
Esa equivalencia es falsa, y conviene dejar escrito por qué en vez de corregir el número y
callar: **el SDK no cuenta mensajes, cuenta items**, una fila por item. Una llamada a tool y
su resultado son dos items. Un turno en que Daniela consulte el conocimiento, mire la agenda
y registre el estado gasta seis o siete items él solo. Cuarenta items no son cinco
conversaciones: pueden ser cinco o seis turnos.

Así que el número se fija **midiendo**, y la constante queda con el dato al lado. Sustituir
una suposición por otra suposición más grande no es un arreglo.

Hay un orden obligado en eso, y conviene escribirlo porque la vía intuitiva no existe: **hoy
no hay nada que medir.** El historial no se guarda, así que no hay items históricos que
contar. La medición solo es posible *después* de persistir. De ahí el orden:

```
   1. persistir SIN límite  ──►  2. medir items/turno      ──►  3. fijar el límite
      (SessionSettings          sobre las filas reales         con el dato al lado
       sin limit)               de agent_messages
```

Entre el paso 1 y el 3 el historial crece sin techo. Es aceptable porque son días de
desarrollo y el volumen actual es bajo, y **no** es aceptable dejarlo así: el paso 3 es parte
de esta fase, no una mejora posterior.

### 4.2 El corte no puede caer entre una tool y su salida

```
   … item 38: function_call         consultar_disponibilidad   ◄── el corte cae AQUÍ
   ──────────────────────────────────────────────────────────  limit
       item 39: function_call_output [tres horarios]
```

Si la ventana corta ahí, lo que se manda al modelo tiene una llamada huérfana, y esa es una
petición que la API rechaza. No aparecería en desarrollo: aparecería en la conversación
número siete de un paciente real.

**No se supone que el SDK lo proteja.** Va como prueba obligatoria, construyendo un historial
que corte exactamente en ese borde. Si resulta que el SDK lo maneja, la prueba lo documenta;
si no, el recorte se hace nuestro y la prueba es la que lo exige.

### 4.3 `/clearstate` tiene que borrar el historial

Hoy resetear un número a primer contacto funciona. Con la sesión persistida dejaría de
funcionar sin que nadie lo note: el paciente volvería a «primer contacto» con Daniela
recordando la conversación anterior. Es la clase de fallo que solo se ve probándolo, así que
se prueba, en los dos esquemas donde `/clearstate` opera.

### 4.4 El chat web usa el mismo mecanismo

Podría quedarse con su diccionario en memoria, que es más simple. No se hace, por la misma
razón que ya está escrita en el plan para no partir a Daniela por canal: **lo que se prueba
tiene que ser lo que corre.** Un chat de pruebas con otra persistencia deja de probar la
persistencia.

### 4.5 Un segundo pool contra Neon, dimensionado a mano

El `AsyncEngine` abre sus propias conexiones, aparte de las de psycopg. Son dos pools contra
la misma base y Neon tiene un techo. Se dimensiona explícitamente en los `engine_kwargs`, en
vez de heredar el default de SQLAlchemy — que nadie eligió para este proyecto.

El número sale de dos datos que hay que mirar antes de escribirlo, no de la intuición: el
techo de conexiones del plan de Neon contratado, y cuántas consume ya el pool de psycopg. La
suma de los dos pools tiene que caber debajo de ese techo con margen, contando que el
despliegue solapa brevemente el contenedor viejo y el nuevo.

---

## 5. Verificación

### Offline (`uv run pytest -q`), sin red ni base

- La reescritura de la URL: `postgresql://` sale como `postgresql+psycopg://`, los
  parámetros de consulta sobreviven intactos, y una URL ya reescrita no se reescribe dos
  veces.
- `config_de_corrida()` con argumentos: fija `group_id`, `trace_metadata` con canal y
  versión de prompt, y **sigue** fijando `trace_include_sensitive_data=False`. Las cuatro
  pruebas que ya protegen eso no se tocan; se les añade el `group_id`.
- El hash del prompt cambia cuando cambia el texto estático y **no** cambia cuando cambia el
  vocabulario de tratamientos.
- El recorte en el borde de la tool call (§4.2), con el historial construido a mano.

### Contra Neon (`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`)

- **Ida y vuelta contra las tablas de la migración**, con `create_tables=False`. Esta es la
  que caza que el SDK haya cambiado su esquema: si la `010_` se queda vieja, esta prueba cae.
- El reinicio simulado: una sesión escribe, se tira, otra con el mismo `session_id` lee, y
  está todo.
- Las tablas se crean en el esquema de pruebas y **no** en `public` — comprobado
  consultando el catálogo, no asumido.
- `/clearstate` borra el historial del agente, en `public` y en `pruebas_web`.

### Entregable ejecutable

`scripts/probar_persistencia.py`, con el patrón de los demás: sin `--chat` no gasta un token.
Con `--chat` hace lo que la frase del plan pide de verdad:

```
  uvicorn arranca
  paciente habla  ─────────────►  «hola, soy Ana»
  ┌────────────────────────────────────────────┐
  │  kill -TERM   ·   arranca otro proceso     │   ◄── el reinicio, de verdad
  └────────────────────────────────────────────┘
  paciente: «¿cómo me llamo?»  ─► Daniela lo sabe
```

Una sesión reconstruida a mano no demuestra eso. El reinicio real, sí.

### El método, que en este repositorio ya está ganado

Cada prueba que importe se comprueba **rompiendo el código a propósito**. Si no cae con el
fallo puesto, la prueba no vale y se rehace. Así se descubrió aquí que dos de los tres
invariantes del búfer no los vigilaba nadie, y que la prueba del muro pasaba con el muro
roto.

---

## 6. Riesgos, escritos y no enterrados

1. **El `PENDIENTE` legal deja de ser teórico.** Hoy el historial muere con el proceso.
   Después de esto, las conversaciones de pacientes quedan escritas y legibles en una
   Postgres administrada fuera del VPS y probablemente fuera de Colombia: una transferencia
   internacional de datos personales sensibles. El plan marca el cifrado como DECISIÓN
   PROVISIONAL precisamente porque `restricciones.legales` está en `PENDIENTE`, y su
   condición de revisión pide cubrir tres cosas: el consentimiento por continuación de la
   conversación, el envío de archivos clínicos a Telegram, y la ubicación de Neon.
   **Esto no bloquea la implementación, y debería resolverse antes del piloto, no después.**
   Si la revisión concluye que el historial no puede ser legible desde el almacén, vuelve
   `EncryptedSession` con un TTL dimensionado a días — descartada hoy porque su TTL por
   defecto es de 600 segundos y los ítems vencidos se saltan en silencio al leer.

2. **Dos pools contra Neon.** Mitigado dimensionándolo a mano (§4.5); queda como cosa a
   mirar si aparecen errores de conexión bajo carga.

3. **Las filas con JSON corrupto se saltan en silencio** dentro del SDK
   (`except json.JSONDecodeError: continue`). Es el mismo vicio por el que el plan descartó
   `EncryptedSession`: perder contexto sin que nada lo reporte. Aquí no es evitable sin
   tocar el SDK. Se acepta y se documenta como límite conocido.

4. **El corte en el borde de la tool call** (§4.2). Mitigado por la prueba obligatoria.

5. **Los acentos salen escapados en el JSON** guardado: el SDK serializa con
   `ensure_ascii=True` por defecto. El ida y vuelta es correcto, pero el texto en la base no
   se lee a ojo. Se pasa `ensure_ascii=False`, que es gratis y evita una tarde perdida
   depurando a ciegas.

---

## 7. Lo que viene después (contexto, no alcance)

El sub-proyecto siguiente es **el relevo**, la mitad que le falta a la fase 6. Sus decisiones
ya están tomadas y se dejan aquí escritas para que no se repitan:

- Se activa con el botón de Telegram (`callback_query`), no con un comando: los doctores no
  recuerdan comandos ni modos.
- Silencio total de Daniela mientras dura.
- El reloj cuenta desde el último mensaje del doctor: 2 h recordatorio, 3 h cierre
  automático, configurable desde el panel.
- **El paciente no ve ninguna transición**, ni al entrar ni al salir.
- **Al retomar, Daniela ve todo el ida y vuelta del relevo**, los mensajes del paciente y los
  del doctor. No rompe el muro: ese texto ya lo recibió el paciente, literal. Y es lo que
  impide que Daniela contradiga lo que el doctor acordó.

Ese último punto es la razón por la que esta fase va primero: sin historial persistido, «ve
todo el ida y vuelta» dura lo que dure el proceso, y un relevo dura tres horas.

Comprobado además contra la API real, y era lo que podía invalidar el relevo entero: el bot
tiene `can_read_all_group_messages = True`, así que sí recibe lo que los doctores escriben en
el grupo sin que lo mencionen. No hay webhook de Telegram registrado todavía.

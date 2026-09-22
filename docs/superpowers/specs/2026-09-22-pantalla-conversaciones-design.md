# La pantalla de Conversaciones: ver el hilo entero y poder meterse

> Diseño aprobado por MaxiCare el 22/09/2026. Implementa la pantalla «Conversaciones» del
> mockup de Claude Design (`MaxiCare Panel.dc.html`), la segunda del panel después de Inicio.

## Contexto

MaxiCare pidió «ver qué está pasando con las conversaciones, en tiempo real». Se le ofrecieron
tres alcances —solo mirar, mirar y abrir Telegram, o mirar y responder desde el panel— y
eligió el tercero, el completo, sabiendo que toca la frontera del relevo.

Hoy, para saber qué le dijo Daniela a un paciente, hay exactamente dos caminos: abrir el
WhatsApp de la clínica, o tomar la conversación por Telegram para que el sistema vuelque la
transcripción en el hilo. El primero no deja rastro de quién miró; el segundo **cambia el
estado del sistema solo para poder leer** —callar a Daniela y abrir un hilo es un precio caro
por una consulta—. Esta pantalla es la tercera vía: leer sin tocar nada.

### Lo que ya existe y no hay que inventar

`persistencia.transcripcion()` (fase 6C) ya resuelve el problema difícil: junta lo que escribió
el paciente con lo que contestó Daniela y lo ordena por hora, desempaquetando dos niveles del
JSON del SDK. Es código que corre en producción desde el primer relevo real. Esta pantalla
hereda esa lógica en vez de reescribirla.

`relevo.cerrar()` ya recibe la cita resuelta y hace todo lo demás. `relevo._conversacion_de()`
ya resuelve «el teléfono X, ¿en qué conversación está?». `whatsapp.enviar_texto()` ya manda y
devuelve el wamid. El panel no construye nada de eso: lo llama.

---

## La unidad es el TELÉFONO, no la conversación

La misma regla que decidió los cinco números de la portada, y por la misma razón: la
conversación muere a las 24 horas y nace otra (`conversacion_viva`). Una lista de
conversaciones mostraría a la misma persona seis veces en una semana.

```
LO QUE SE VE                        LO QUE HAY EN LA BASE
┌────────────────────────┐          conversaciones
│ Laura Medina    09:42  │  ───→      ├── 3f2a… (hoy)
│ +57 310 482 7714       │            ├── 8c1b… (ayer)
│ Hola, me duele mucho…  │            └── d40e… (el martes)
│ [EN RELEVO]         2  │
└────────────────────────┘          …y el hilo son las tres juntas
```

Una fila por persona. El hilo es toda su historia, no la de hoy. `transcripcion` ya funciona
así —consulta por teléfono, cruzando todas las conversaciones de ese número—, así que la
pantalla no fuerza nada: sigue el grano que el código ya tenía.

Consecuencia práctica: las acciones de escritura (tomar, escribir, cerrar) reciben un teléfono
y resuelven la conversación viva por dentro, con `relevo._conversacion_de()`. Si no hay
ninguna, se abre una —que es lo correcto: el doctor está empezando una conversación con esa
persona, y sin fila no hay dónde anotar quién la tiene.

---

## Los cuatro estados

Se calculan con las **mismas reglas que la portada**. No es elegancia: dos pantallas que
cuentan lo mismo de dos maneras distintas producen una reunión sobre cuál miente.

| Estado | Regla | De dónde sale |
|---|---|---|
| `EN RELEVO` | `conversaciones.tomada_por` no es NULL | no negociable 15 |
| `ESPERANDO` | hay entrantes con `respondido_en` NULL, más viejos que `panel.MINUTOS_SIN_CONTESTAR` (5), y `(fallo_respuesta IS NULL OR fallo_respuesta NOT LIKE 'relevo:%')` | idéntica al número 4 de Inicio |
| `ACTIVA` | escribió dentro de las últimas 24 h | `conversacion_viva` |
| `CERRADA` | lo demás | |

El prefijo `relevo:` importa. El no negociable 15 dice que un mensaje que entra durante un
relevo se anota con `fallo_respuesta` empezando por `relevo:` **sin ser un fallo**: es el
doctor hablando, no Daniela callada por error. Contarlo como «esperando» pintaría de rojo
justo las conversaciones mejor atendidas del día.

Y el `IS NULL` de esa condición no es cinturón sobre tirante. En SQL, `NULL NOT LIKE 'relevo:%'`
no es cierto: es NULL, o sea que el filtro lo descarta. Escrito sin la primera mitad, la
consulta se comería justo el caso que más duele —el mensaje que nadie intentó contestar porque
el proceso se cayó antes de anotar siquiera el fallo, con las dos columnas en NULL—, que es el
caso que la migración 009 dejó escrito como el motivo de existir de su índice. `resumen_inicio`
ya lo escribe entero; copiar solo la mitad visible sería una regresión silenciosa.

**Precedencia**: `EN RELEVO` gana a todo. Una conversación que el doctor tiene tomada y en la
que entran mensajes cumple las dos condiciones a la vez, y lo que el panel tiene que decir es
que hay alguien encima, no que nadie contesta.

Los chips de filtro son cuatro: `TODAS`, `ACTIVAS`, `ESPERANDO`, `EN RELEVO`. El mockup traía
tres (`Todas / Activas / Sin resolver`); «Sin resolver» ya es el nombre de otra pantalla del
panel y significa otra cosa —el informe de casos agrupados—, así que reutilizarlo aquí
confundiría dos conceptos distintos delante del mismo usuario.

---

## Las tres voces, y el hueco

El hilo tiene tres voces. Hoy solo dos están guardadas:

```
paciente  →  mensajes_entrantes.texto          ✅ guardado desde la 004
Daniela   →  agent_messages.message_data       ✅ rescatable (JSON del SDK)
doctor    →  NINGUNA PARTE                     ❌ se pierde al enviarse
```

`relevo.relevar_mensaje()` toma lo que el doctor escribe en el hilo de Telegram, se lo manda al
paciente por WhatsApp y **no lo escribe en ningún sitio**. Es coherente con cómo nació el
relevo —el hilo de Telegram ERA el registro— y deja de serlo en cuanto hay un segundo sitio
desde el que mirar.

### Migración 026 · `mensajes_del_doctor`

```sql
CREATE TABLE IF NOT EXISTS mensajes_del_doctor (
    id             BIGSERIAL   PRIMARY KEY,
    telefono       TEXT        NOT NULL,
    conversacion_id UUID       REFERENCES conversaciones(id),
    autor          TEXT        NOT NULL,   -- el nombre de quien escribió
    origen         TEXT        NOT NULL CHECK (origen IN ('panel', 'telegram')),
    texto          TEXT        NOT NULL,
    wamid          TEXT,                   -- el del mensaje de salida; NULL si no salió
    enviado_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    fallo          TEXT
);

CREATE INDEX IF NOT EXISTS ix_mensajes_del_doctor_telefono
    ON mensajes_del_doctor (telefono, enviado_en DESC);
```

Va por **teléfono** y no por conversación, igual que `temas_telegram` (no negociable 16): el
hilo que se pinta cruza conversaciones, así que colgarlo solo de `conversacion_id` obligaría a
un JOIN para la consulta que más se hace. `conversacion_id` se guarda igual —es la trazabilidad
de en qué relevo se dijo— pero admite NULL, porque el registro no puede depender de resolver
una conversación.

### La fila se escribe DESPUÉS de enviar, y se escribe también si falla

Es la misma pregunta que decidieron los no negociables 21 y 24, resuelta mirando qué se rompe
en cada dirección:

| Orden | Si se cae en medio | Coste |
|---|---|---|
| guardar → enviar | fila de un mensaje que el paciente NUNCA recibió | el doctor lo da por entregado; el paciente espera |
| enviar → guardar | mensaje entregado que no aparece en el hilo | el doctor lo repite |

Gana **enviar → guardar**: repetir un mensaje es molesto, creer que contestaste cuando no lo
hiciste deja a un paciente solo. Es el mismo criterio del 24 —«falsificar una prueba, no»—
aplicado al revés del 21, donde el riesgo era duplicar.

Y si `enviar_texto` lanza `ErrorDeCanal`, **la fila se escribe igual**, con `wamid` NULL y el
motivo en `fallo`. En el hilo ese mensaje se pinta marcado, con el texto «no salió». El
precedente exacto es `mensajes_entrantes.fallo` / `fallo_respuesta`: una fila que dice que se
intentó y no se pudo vale más que ninguna fila.

La escritura de la fila va dentro de un `try` que se lo traga todo, con el criterio de
`atencion._anotar_resultado` y del no negociable 22: **si la contabilidad revienta se pierde
una fila, nunca un mensaje al paciente**.

### También se guarda lo que entra por Telegram

`relevo.relevar_mensaje()` escribe su fila con `origen = 'telegram'`. Sin eso, el panel sería
una ventana que solo ve la mitad de lo que ella misma no escribió, y un doctor que trabaja
desde Telegram —que es como se trabaja hoy— dejaría el hilo del panel lleno de agujeros.

> **Lo que no se puede recuperar**: los relevos anteriores a esta migración. Esos tramos quedan
> vacíos para siempre y no hay de dónde sacarlos. El hilo empieza a estar completo el día del
> despliegue, no antes. Conviene que el spec lo diga para que dentro de tres meses nadie lo
> lea como un bug.

### `texto_de_daniela` deja de ser privada

`persistencia._texto_de_daniela` es lo único frágil de todo esto: desempaqueta el formato del
SDK y degrada a `None` ante cualquier forma que no reconozca. El panel la necesita. Se
renombra a `persistencia.texto_de_daniela` —sin guion bajo— en vez de copiarla, porque **dos
implementaciones de ese desempaquetado se separan el día que suba la versión del SDK** y solo
una de las dos se arreglaría. Hoy solo la llama `transcripcion`, así que el renombrado no
rompe a nadie; ningún script de `scripts/` la dobla (comprobado).

---

## Arquitectura

### Los cinco endpoints

```
GET  /api/conversaciones               la lista       los tres roles    NO escribe
GET  /api/conversaciones/{telefono}    el hilo        los tres roles    NO escribe
POST /api/conversaciones/{telefono}/tomar     tomar   admin + doctor
POST /api/conversaciones/{telefono}/mensaje   escribir admin + doctor
POST /api/conversaciones/{telefono}/cerrar    devolver admin + doctor
```

Los dos GET **no escriben nada**, y hay una prueba que lo demuestra ejecutando el SQL de verdad
contra una conexión que delata el verbo de cada sentencia. Es la misma prueba que protege
`/api/inicio`. La única lectura del panel que escribe es `/api/agenda`, y lo hace a propósito
—reconcilia contra Google Calendar—; que esa excepción siga siendo una sola es lo que la
mantiene visible.

**Los permisos van en el servidor.** `exigir_rol("admin", "doctor")` en los tres POST. El botón
se esconde en el frontend por comodidad, pero, como dice el docstring de `exigir_rol`, un botón
que desaparece no es un control de acceso. Es la primera vez que el rol `doctor` hace algo que
`recepcion` no puede: la tabla `usuarios` los separó desde la 006 esperando justo esto.

**Toda la lógica vive en `panel.py`.** `runtime.py` es transporte y nada más; lo exige
`tests/test_estructura.py` y es la regla que mantiene el proyecto testeable sin levantar un
servidor.

### Los archivos

| Archivo | Qué cambia |
|---|---|
| `migraciones/026_mensajes_del_doctor.sql` | nuevo |
| `src/maxicare_daniela/persistencia.py` | `texto_de_daniela` (renombrada), `guardar_mensaje_del_doctor`, `mensajes_del_doctor` |
| `src/maxicare_daniela/panel.py` | `listar_conversaciones`, `hilo`, `puede_escribir` |
| `src/maxicare_daniela/relevo.py` | `relevar_mensaje` guarda su fila |
| `src/maxicare_daniela/runtime.py` | los cinco endpoints, transporte solo |
| `web/src/api.ts` | tipos y llamadas |
| `web/src/componentes/Sidebar.tsx` | sección `conversaciones` entre Inicio y Sin resolver |
| `web/src/pantallas/Conversaciones.tsx` | nueva |
| `web/src/App.tsx` | la ruta |
| `tests/test_panel.py`, `tests/test_relevo.py`, `tests/test_runtime.py` | las pruebas |

### La lista: qué consulta y qué NO

Una fila por teléfono, ordenada por última actividad, tope 50.

La vista previa es **el último mensaje del PACIENTE**, no el último del hilo. Dos razones: es
lo que le dice a quien mira qué está pidiendo esa persona, y sacar el último de Daniela exigiría
parsear el JSON del SDK de cada fila de `agent_messages` para cada teléfono de la lista —caro,
y la lista se refresca cada diez segundos—.

El nombre sale, en este orden: de `pacientes` si hay ficha, de `mensajes_entrantes.nombre_perfil`
si no, y del teléfono si tampoco. **Una ficha cuyo nombre es `persistencia.NOMBRE_PENDIENTE` no
cuenta como nombre** (no negociable 12): se trata como si no hubiera ficha y se cae al perfil o
al número. Pintar el literal `PENDIENTE` donde va un nombre es exactamente el error que esa
regla existe para evitar.

### El hilo: `panel.hilo(conn, telefono, *, limite=60)`

Tres consultas y un `sort`, modelado sobre `persistencia.transcripcion` y reutilizando
`texto_de_daniela`:

```
mensajes_entrantes      →  ('paciente', texto, cuando, None)
agent_messages          →  ('daniela',  texto, cuando, None)
mensajes_del_doctor     →  ('doctor',   texto, cuando, fallo)   + autor
```

El corte va por el FINAL —los últimos 60—, igual que la transcripción del relevo: lo que hace
falta para entender qué pasa es lo último que se dijeron, no cómo empezó todo hace dos meses.

`created_at` de `agent_messages` es `TIMESTAMP` **sin zona** (no negociable 10: lo fija el SDK).
Sin el `AT TIME ZONE 'UTC'` las dos mitades no se pueden ordenar juntas y Python revienta con
`TypeError`. `transcripcion` ya lo hace; el panel lo hereda.

### El refresco: diez segundos, y por qué no cuesta

Neon cobra tiempo de cómputo encendido, no consultas. El backend ya consulta la base **cada 60
segundos, las 24 horas**, desde dos tareas independientes (`_barrer_relevos_sin_parar` y
`_despachar_recordatorios_sin_parar`, ambas con `SEGUNDOS_ENTRE_BARRIDOS = 60.0`), más dos cada
cinco minutos y una cada hora. El cómputo **no se suspende nunca**, se mire el panel o no.

Una pestaña abierta ocho horas a diez segundos añade unos 2.900 `SELECT` por índice a una base
que ya estaba despierta. No enciende nada nuevo.

Dos cosas que sí hace la pantalla para no ser un derroche:

- **Se pausa cuando la pestaña no está visible** (`document.visibilityState`). Un panel olvidado
  en otra ventana toda la noche no consulta ni una vez.
- **El hilo solo se refresca si hay uno abierto.** La lista siempre; el hilo, el seleccionado.

Cinco segundos sería el doble de consultas sin ganar nada —los mensajes de WhatsApp no llegan
cada cinco segundos—. Treinta ahorraría algo que no estaba costando y rompería la sensación de
estar en vivo, que es lo que se pidió.

---

## Tomar, escribir, cerrar

### Tomar es el MISMO relevo, no uno nuevo

El no negociable 15 dice que `tomada_por` puesto significa Daniela callada **y** tema abierto,
y que las dos dejan de ser verdad juntas. Un panel que pusiera `tomada_por` sin abrir el hilo
de Telegram rompería la mitad de esa frase: el doctor que mira Telegram vería la conversación
como si nadie la atendiera, y `relevo.barrer` razonaría sobre un hilo que no existe.

Así que tomar desde el panel abre el hilo igual que el botón:

```
   PANEL ──tomar──┐
                  ├──→ relevo (tomada_por + hilo de Telegram abierto)
TELEGRAM ─botón───┘

   PANEL ──escribe──┐
                    ├──→ WhatsApp al paciente ──→ mensajes_del_doctor
TELEGRAM ──escribe──┘                                    │
                                                  los dos lo ven
```

Un solo relevo con dos ventanas. `autor` es el `nombre` del usuario del panel, que es lo que ya
viaja en la sesión.

### Escribir comprueba la ventana de Meta ANTES

Meta no acepta texto libre fuera de las 24 horas desde el último mensaje del paciente. Sin
comprobarlo, el botón Enviar devolvería un error de la Graph API que nadie sabe leer.

`panel.puede_escribir(conn, telefono, *, ahora)` devuelve si se puede y cuántas horas han
pasado, a partir de `persistencia.ultimo_mensaje_del_paciente()`. El GET del hilo lo incluye, la
caja sale apagada con la explicación, y **el POST lo vuelve a comprobar**: el frontend lo pinta,
el servidor lo decide.

No se ofrece plantilla para reabrir la ventana. Las que hay aprobadas son de recordatorio, de
cita cancelada y de reactivación; ninguna dice «el doctor quiere hablar contigo», y crear una
exige que Meta la apruebe. Queda fuera a propósito y está anotado abajo.

### Cerrar es un formulario, no cinco pasos

Telegram cierra el relevo en cinco turnos porque un chat solo puede preguntar una cosa a la vez:
`cierre_pendiente` recorre `preguntado → esperando_nombre → esperando_tratamiento →
esperando_fecha`. Una pantalla no tiene esa limitación: pregunta las cuatro cosas juntas.

```
¿Hubo cita?   ( ) No      (•) Sí
    Nombre del paciente  [________]     ← solo si no lo sabemos
    ¿De qué es la cita?  [ valoración ▾ ]
    Cuándo               [ 24/09  10:00 ]
                                    [ Devolvérsela a Daniela ]
```

Y ese botón llama a las **dos** funciones que usa Telegram, en el mismo orden: primero
`relevo._agendar(...)` —cupo, Google Calendar, fila— y **solo si esa dice que sí**,
`relevo.cerrar(id, motivo='devuelto_por_doctor', cita=…, doctor=…)`. No hay dos
implementaciones que puedan separarse: hay un formulario que recoge lo que las funciones ya
pedían. `cierre_pendiente` no se toca desde el panel —ese estado es la máquina de Telegram— y
el cierre del panel es directo.

> **Esta frase decía solo `cerrar`, y estaba mal** (corregido el 22/09/2026, al implementarla).
> El `cita=` de `cerrar` **no crea ninguna cita**: su único uso es `_nota_del_relevo`, o sea
> contárselo a Daniela. Cerrar sin pasar por `_agendar` deja al doctor marcando «sí hubo
> cita», a Daniela creyendo que existe y al paciente presentándose en una clínica donde nadie
> lo espera — el no negociable 1 por la puerta de atrás. Si `_agendar` dice que no (la hora se
> llenó, Google no contesta), **el relevo NO se cierra**: llega un 409, el formulario se queda
> puesto y el doctor escribe otra hora, que es exactamente lo que hace el diálogo del hilo.

El desplegable de tratamiento sale de `panel.vocabulario_activo()`, la lista viva. Es la
excepción del no negociable 19 mirada de frente: allí el `tratamiento` del relevo se guarda tal
cual porque lo escribe un doctor a mano en un chat; aquí hay un desplegable, así que no hace
falta la excepción y no se usa.

---

## Estados vacíos

Se cuidan uno por uno, como en Inicio, porque una pantalla en blanco no distingue «no pasa
nada» de «esto está roto».

| Situación | Qué se ve |
|---|---|
| Ninguna conversación | «Todavía no ha escrito nadie.» |
| El filtro no devuelve nada | «Ninguna conversación está esperando respuesta.» (varía con el chip) |
| La búsqueda no encuentra | «Nadie con ese nombre ni ese número.» |
| Ningún hilo seleccionado | El icono y «Selecciona una conversación para ver el historial.» |
| Hilo sin mensajes legibles | «Esta persona solo ha mandado archivos o notas de voz.» |
| Tramo de relevo anterior a la 026 | Nada especial: el hueco no se anuncia. Anunciarlo en cada hilo viejo sería ruido permanente por un problema que se arregla solo con el tiempo. |

---

## Verificación

**Pruebas de Neon** (`-m neon`, esquema `pruebas`), todas con **aserciones de delta**. El
esquema `pruebas` no se borra nunca —`CREATE SCHEMA IF NOT EXISTS`— así que contar filas de
tablas enteras arrastra lo que dejaron otros siete archivos de prueba. Se mide antes y después:

- la lista devuelve una fila por teléfono y el estado correcto en los cuatro casos;
- el hilo devuelve las tres voces ordenadas por hora;
- `guardar_mensaje_del_doctor` deja la fila, con wamid y sin él.

**Pruebas offline**:

- los dos GET no ejecutan más que `SELECT` y no hacen `commit` —con la conexión que delata
  verbos, ejecutando el SQL real y no un doble de la función, para que la prueba siga
  vigilando las consultas que alguien añada mañana;
- `recepcion` recibe 403 en los tres POST;
- fuera de las 24 h, el POST de mensaje no llama a `enviar_texto`; y **la prueba comprueba
  además que el camino se recorrió** —que se consultó el último mensaje del paciente—, porque
  una aserción sobre algo que NO pasa también pasa cuando no se ejecutó nada (no negociable 28);
- un `ErrorDeCanal` al enviar deja fila con `fallo` y sin `wamid`;
- `relevar_mensaje` guarda su fila, y si el guardado revienta el mensaje al paciente sale igual.

**Entregables que ninguna prueba offline caza**: quien toque `relevar_mensaje` corre
`scripts/probar_relevo.py`. Quien toque `panel.py` corre `-m neon` entero (no negociable 8: una
colisión de helper dejó dos pruebas en rojo durante tres commits con la suite offline en verde).

**Fechas**: ninguna prueba clava una fecha sin inyectarla. `panel.listar_conversaciones` y
`panel.puede_escribir` reciben `ahora` por parámetro, como `resumen_inicio`. Una fecha clavada
en una prueba ya envejeció tres veces en este proyecto.

---

## En dos fases

| Fase | Qué entrega | ¿Sirve sola? |
|---|---|---|
| **1 · Ver** | Migración 026, guardado desde Telegram, los dos GET, la pantalla, el refresco | Sí. Es lo que se pidió: ver qué está pasando. |
| **2 · Intervenir** | Tomar, escribir, cerrar | Necesita la 1. |

Un solo spec para las dos porque el hilo de la fase 1 tiene que saber pintar lo que genera la
fase 2 —y porque la tabla se diseña una vez—. Pero la fase 1 se puede desplegar sin la 2.

---

## Lo que queda fuera, y por qué

- **Mandar plantillas para reabrir la ventana de 24 h.** Exige una plantilla nueva aprobada por
  Meta. Cuando exista, el sitio donde entra ya está: el mismo POST que hoy rechaza.
- **Adjuntar archivos desde el panel.** El doctor ya puede mandarlos desde el hilo de Telegram,
  que es donde los recibe. Duplicar la subida por una segunda puerta no resuelve nada nuevo.
- **Buscar dentro del texto de los mensajes.** La búsqueda es por nombre y teléfono, como el
  mockup. Buscar dentro del contenido exige un índice de texto completo sobre datos clínicos;
  es una decisión aparte.
- **Marcar como leído / no leídos.** El mockup trae un contador de no leídos. Aquí no hay a quién
  contárselos: no se registra quién del panel miró qué, y el número que sí significa algo —
  cuántos mensajes están sin responder— ya es el estado `ESPERANDO`. Inventar un «no leído» que
  nadie marca daría un número que crece para siempre.
- **Notificar en el navegador cuando entra un mensaje.** El aviso ya suena en Telegram, que es
  el teléfono que el doctor lleva encima. Un segundo canal de alerta es una decisión de
  operación, no de pantalla.
- **Paginar el hilo hacia atrás.** Se muestran los últimos 60 mensajes. Si hace falta más, es
  otro trabajo.

---

## Lo que este documento NO decide

- **Si MaxiCare quiere que recepción pueda responder.** Hoy queda en `admin + doctor` porque es
  lo que se eligió. Cambiarlo es cambiar una lista de dos literales en `exigir_rol`.
- **Cuánto tiempo se guardan los mensajes del doctor.** La tabla no tiene política de retención.
  Es la misma situación que `mensajes_entrantes`, y merece una decisión conjunta sobre las dos,
  no una sobre la nueva.
- **Qué pasa si dos personas del panel toman la misma conversación a la vez.**
  `persistencia.activar_relevo` ya resuelve la carrera —devuelve quién la tiene— y el panel
  mostrará ese nombre. Lo que no se decide aquí es si debería poder quitársela.

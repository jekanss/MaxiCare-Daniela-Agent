# Contacto y consentimiento · diseño

Fecha: 16/09/2026 · Sub-proyecto **A + B** de la reactivación de leads.

## El problema

El 15/09/2026 el equipo de marketing de MaxiCare respondió las ocho preguntas sobre
seguimiento y reactivación. Siete respuestas son requisitos implementables. La séptima
—«¿tenemos permiso de esos pacientes para escribirles?»— no fue un sí ni un no: fue una
política de tratamiento de datos nueva, una autorización expresa, siete campos a persistir,
reglas para menores y tres leyes colombianas.

Hoy **no existe ni una sola pieza de consentimiento en el sistema**. Verificado:

- `pacientes` tiene cuatro columnas: `id`, `nombre_completo`, `telefono`, `creado_en`
  (`migraciones/001_esquema_inicial.sql:19-24`). Ninguna alteración posterior añadió nada;
  la 014 incluso dropeó las dos que la 002 había puesto.
- No existe `do_not_contact`, ni fecha de aceptación, ni versión de política, ni mecanismo
  de baja en ninguna tabla del proyecto.
- Toda la carga recae sobre una frase del prompt (`agentes.py:83-84`): «Antes de pedir datos
  sensibles, informas que al continuar acepta la política de tratamiento de datos de
  MaxiCare». Es una instrucción al modelo. Nadie registra si se dijo, ni con qué palabras,
  ni comprueba nada.
- El despachador de seguimientos puede escribirle a cualquier número con una cita sin que
  exista forma alguna de que ese número pida que no lo hagan.

## El hueco que comparten el permiso y la memoria

Parecen dos problemas distintos y son el mismo. Los dos necesitan que exista **«la persona
del teléfono X»** como algo permanente, y eso hoy no existe. Solo existen:

- `conversaciones`, que caduca a las 24 h (`atencion.VENTANA_CONVERSACION_HORAS = 24`,
  `atencion.py:97`), y
- `pacientes`, cuya fila **solo nace cuando alguien agenda** (no negociable 12:
  `crear_cita` registra al paciente).

Quien preguntó y no agendó —exactamente el grupo que se quiere reactivar— no es ninguna de
las dos cosas. Es invisible para el sistema en cuanto pasan 24 horas.

Por eso este sub-proyecto construye el cimiento. Sin él, ni el consentimiento ni la memoria
del reencuentro ni el barrido de reactivación tienen dónde apoyarse.

## Alcance

**Dentro:**

1. Una identidad persistente por teléfono (`contactos`), independiente de tener ficha.
2. Una bitácora inmutable de consentimiento (`consentimientos`).
3. El aviso de la política, emitido por el código y registrado.
4. La baja comercial (`no_contactar`), su revocación, y su supervivencia a `/clearstate`.
5. La comprobación de la baja **antes** de las siete guardas del despachador, distinguiendo
   lo comercial de lo transaccional.
6. La señal en el contexto del turno para que Daniela no ofrezca seguimiento a quien lo negó.

**Fuera, a propósito** (cada una con su razón en §10):

- Menores de edad.
- La columna «canales autorizados».
- Botones interactivos de WhatsApp.
- La memoria del reencuentro (sub-proyecto C).
- El barrido, la cadencia y las plantillas de reactivación (sub-proyecto D).

## Las decisiones, con la alternativa que se descartó

Siguiendo el formato del proyecto: cada decisión va con lo que se dejó fuera, para que nadie
la reabra sin saber qué costó.

### D1 · El permiso se pide en dos tiempos, no en la puerta

Marketing pidió literalmente que **antes de continuar con Daniela** el paciente viera el
bloque de autorización completo con un botón de aceptar.

**Se descartó** porque ese muro se lo come todo el que escribe, incluido quien solo quiere
agendar una limpieza. La línea base medida del proyecto es 97 conversaciones → 2 citas, con
cerca de la mitad sin ninguna respuesta; anteponer ~120 palabras de texto legal al saludo
empuja ese número en la dirección contraria. El principio que decide los empates aquí es la
seguridad clínica, no la conversión — pero este caso no enfrenta seguridad contra comercio:
enfrenta comercio contra formalismo.

**Lo que se hace en su lugar:** el aviso breve con enlace acompaña al primer mensaje
(atención), y lo relativo a volver a escribir se trata cuando Daniela decide que hay que
volver a escribir.

### D2 · El permiso de reactivación es un aviso con derecho a oponerse, no un sí expreso

Daniela dice, al despedirse sin cita: «Si quieres te escribo en unos días por si quedan
dudas». María no tiene que hacer nada. Si no se opone, se le escribe.

**Se descartó** capturar un sí explícito con botones. Se llegó a aprobar y se revirtió el
16/09/2026 por decisión del usuario: no quiere botones en estos mensajes.

**Esto se aparta de lo que marketing puso por escrito**, y queda dicho aquí para que nadie lo
descubra en una auditoría: ellos escribieron que «Daniela nunca infiere consentimiento
simplemente porque alguien escribió por WhatsApp» y que «el silencio no equivale a
autorización». El modelo que implementa esta spec es opt-out, no opt-in. **Hay que
confirmárselo a marketing** (§11) antes de que la reactivación salga a producción.

Lo que sí sostiene la decisión: la Ley 2300 exige un mecanismo ágil y sencillo para dejar de
recibir comunicaciones comerciales, y eso es precisamente lo que se construye aquí.

### D3 · El consentimiento de datos de salud es el aviso más la continuación

No hay una segunda pregunta cuando la conversación toca salud. Si a María se le mostró el
aviso y ella siguió contando lo de su muela, eso queda registrado como lo que es: se le
informó y continuó.

**Se descartó** pedir una confirmación expresa al primer asomo de salud (llegó a aprobarse y
se revirtió el 16/09/2026). Razón del usuario: si ya se le avisó y continuó, volver a
preguntarle es tratarla como si no hubiera leído.

**Lo que esto cuesta**, dicho sin adornos: el Decreto 1377 pide consentimiento expreso para
datos sensibles. «Se le avisó y continuó» es una lectura defendible —el propio texto que
redactó marketing dice «puedo suministrar voluntariamente información relacionada con mi
salud»— pero es más débil que un acto afirmativo. Va en el mismo párrafo que se le manda a
marketing.

### D4 · La baja apaga lo comercial y nunca el recordatorio de la cita

Cuando María pide que no le escriban más, se apaga la reactivación y cualquier seguimiento
comercial. **Se mantiene** el recordatorio de su cita y la atención normal si ella escribe.

**Se descartó** la lectura literal (apagarlo todo). Si se apaga el recordatorio, María pide
no recibir publicidad y se queda sin el aviso de su cita del jueves: no llega, la clínica
pierde el cupo y María su tratamiento — y el sistema habría hecho exactamente lo que se le
pidió. Es el principio del proyecto aplicado tal cual: **la seguridad clínica prevalece
sobre cualquier objetivo comercial.**

### D5 · La baja se revoca solo si el paciente lo pide expresamente

Que María vuelva a escribir **no** levanta la baja. Solo la levanta que ella lo pida.

**Se descartó** que la baja fuera definitiva e irrevocable desde WhatsApp (mandarla al canal
de privacidad). Si María está pidiendo activamente que le avisen de una promoción, mandarla a
hacer un trámite por correo es perder a alguien que quiere volver.

### D6 · Tabla propia por teléfono, no ficha de paciente ni columna en conversaciones

**Se descartó abrirle ficha en `pacientes` a todo el que escriba.** En este sistema la
existencia de la fila en `pacientes` **es** la identidad verificada: de ahí sale
`ctx.telefono_sin_paciente`, el permiso que impide que un desconocido mueva o cancele citas
(no negociable 12). Si todo el que saluda tiene ficha, esa distinción desaparece.

**Se descartó guardarlo en `conversaciones` y leerlo por teléfono**, que era el candidato
natural porque ese patrón ya existe en el código (`conversaciones.ultimo_recordatorio_tipo` /
`ultimo_recordatorio_en`, leídas por `persistencia.ultimo_recordatorio` sin ventana). Lo mata
un caso concreto: `/clearstate` borra las conversaciones, y con ellas se llevaría el
`no_contactar`. Un número dado de baja volvería a ser contactable sin que nadie se entere.

### D7 · El aviso lo emite el código, no el modelo

La frase del prompt sirve para que Daniela lo diga; no sirve como prueba. El código pega el
aviso al primer mensaje saliente de un teléfono que nunca lo ha visto, siempre con el mismo
texto y el mismo enlace, y registra qué versión vio.

Es el mismo principio que ya rige las claves de idempotencia (no negociable 2) y la huella de
los casos sin resolver (no negociable 22): **lo que tiene que ser demostrable lo escribe el
código.**

## 1. Modelo de datos · migración 019

Aditiva pura. No altera ni una tabla existente, así que no puede romper nada de lo que ya
funciona.

### 1.1 `contactos` — el estado actual

Una fila por teléfono. Se sobrescribe. Es la que se consulta en cada turno y en cada tanda
del despachador, así que tiene que ser barata.

| Columna | Tipo | Notas |
|---|---|---|
| `telefono` | `TEXT PRIMARY KEY` | la identidad, igual que en `temas_telegram` (no negociable 16) |
| `creado_en` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `actualizado_en` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `aviso_mostrado_en` | `TIMESTAMPTZ` | NULL = nunca vio el aviso |
| `politica_version` | `TEXT` | la versión que vio, p. ej. `politica-2026-09` |
| `no_contactar` | `BOOLEAN NOT NULL DEFAULT FALSE` | la baja comercial |
| `no_contactar_en` | `TIMESTAMPTZ` | |
| `no_contactar_origen` | `TEXT` | `paciente` \| `clinica` |

**No lleva `telefono` con FK a `pacientes`**: son cosas distintas a propósito. Un contacto
puede no tener ficha nunca, y una ficha existe desde antes de esta migración.

### 1.2 `consentimientos` — la bitácora

Una fila por decisión. **Nunca se modifica ni se borra.** Es lo que se enseña si alguien
reclama. El proyecto ya usa este patrón en `bitacora_cambios`.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `telefono` | `TEXT NOT NULL REFERENCES contactos(telefono) ON DELETE RESTRICT` | borrar un contacto con bitácora es imposible |
| `evento` | `TEXT NOT NULL` | CHECK: `aviso_mostrado`, `baja_solicitada`, `baja_revocada`, `rastro_borrado` |
| `ocurrido_en` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `politica_version` | `TEXT` | congelada en el momento del evento |
| `origen` | `TEXT NOT NULL` | CHECK: `codigo`, `paciente`, `clinica` |
| `detalle` | `TEXT` | truncado a 500 caracteres |

Índice por `(telefono, ocurrido_en DESC)`.

El CHECK de `evento` y el de `origen` van **en cada esquema**, como hizo la 013 con el CHECK
del relevo: las pruebas corren en el esquema `pruebas` y el CHECK tiene que existir allí
también.

### 1.3 Por qué dos tablas y no una

Una sola tabla con el estado actual pierde justo lo que la ley pide poder acreditar. Si María
autoriza, se da de baja y vuelve a autorizar, una sola tabla solo recuerda lo último. La de
arriba responde rápido; la de abajo aguanta una revisión.

### 1.4 Lo que NO se crea, y por qué

- **`canales_autorizados`.** Hoy solo existe WhatsApp. Una columna que siempre dice lo mismo
  es una columna que miente el día que aparezca un segundo canal y nadie se acuerde de
  llenarla. Queda escrito: el único canal autorizado es WhatsApp porque es el único que
  existe. Si aparece otro, se añade entonces.
- **`salud_tratada_en`.** Registrar «aquí la conversación tocó salud» exige que el modelo
  detecte qué es salud, y eso falla. `aviso_mostrado_en` más la existencia de mensajes
  posteriores acredita lo mismo sin depender de un juicio del modelo.
- **`marketing_consent` separado de `no_contactar`.** Con D2 (opt-out) el permiso positivo no
  se captura: lo que se captura es la oposición. Dos columnas para un solo hecho son dos
  columnas que se contradicen.

## 2. El aviso de la política

### 2.1 Texto

Fijo, emitido por el código, añadido al primer mensaje saliente de un teléfono cuyo
`aviso_mostrado_en` sea NULL:

```
Al continuar aceptas nuestra política de tratamiento de datos: <URL>
```

Va como línea aparte al final del mensaje de Daniela, no como párrafo propio: es un pie, no
una interrupción.

### 2.2 La URL — **PENDIENTE**

El PDF existe y es público:
`https://drive.google.com/file/d/1IB_XYUfc6Dqd51zBeURemfAVQMVnTy28/view`
(`Politica_Tratamiento_Datos_Personales_MaxiCare_2026.pdf`).

**No se usa ese enlace como destino final**, por tres razones:

1. Drive permite subir una versión nueva sobre el mismo archivo sin que el enlace cambie. Si
   la política se actualiza, todos los que ya aceptaron quedarían apuntando a un texto que no
   es el que vieron — que es exactamente lo que hay que poder acreditar.
2. Un enlace opaco de Drive dentro de un mensaje que pide confianza sobre datos personales
   trabaja en contra de sí mismo.
3. Si alguien mueve o borra el archivo, el enlace muere en silencio y el sistema lo sigue
   mandando.

**Lo que hay que hacer:** servir el PDF desde el dominio propio, en una ruta estable, con una
copia congelada por versión. El VPS y el dominio ya existen. Hasta entonces la constante de
configuración queda con el literal `PENDIENTE` y el aviso **no se emite**: es preferible no
enseñar un enlace que enseñar el que no se va a poder sostener.

### 2.3 Cuándo se marca — al revés que los recordatorios

**El aviso se marca DESPUÉS de que el envío tenga éxito, nunca antes.**

Esto contradice frontalmente la no negociable 21 (un recordatorio se marca **antes** de
enviarse), y alguien lo va a «corregir». La razón de que aquí sea al revés:

- En un recordatorio, el riesgo es **mandarlo dos veces**: marcar después significa que un
  fallo entre envío y marcado repite el mensaje sesenta segundos más tarde.
- En el aviso, el riesgo es **dar por mostrado uno que no salió**: marcar antes significa que
  un fallo de red deja constancia de un aviso que María nunca vio, y esa constancia es
  precisamente la prueba.

Repetir un aviso es inocuo. Falsificar una prueba, no. El criterio se documenta también en el
código, junto a la línea que marca.

## 3. La baja y su revocación

### 3.1 Quién la dispara y quién la escribe

| Momento | Quién lo detecta | Quién escribe la fila |
|---|---|---|
| Primer mensaje del número | el código | el código |
| El paciente pide no ser contactado | Daniela, vía tool | el código |
| El paciente pide volver a recibir | Daniela, vía tool | el código |

Las dos tools nuevas no reciben fecha, ni origen, ni versión: el modelo solo levanta la mano.
La huella la arma el código a partir de `ctx`, como en `ctx.clave(...)` (no negociable 2) y en
la huella de los casos sin resolver (no negociable 22).

### 3.2 Ante la duda, se marca la baja

La instrucción de Daniela es explícita: si un mensaje **podría** ser una petición de no ser
contactado y podría no serlo, se marca. Dejar de escribirle a alguien que no lo pidió es una
molestia; escribirle a quien sí lo pidió es el problema que esta spec existe para evitar.

### 3.3 La revocación

Volver a escribir no levanta la baja. Solo la levanta pedirlo. Cuando el paciente lo pide,
Daniela confirma y la tool lo registra; la bitácora conserva las dos decisiones con sus
fechas, que es lo que hace que el historial sea acreditable.

### 3.4 Sin persuasión

Marketing lo pidió expresamente y va al prompt: cuando alguien pide la baja, Daniela confirma
brevemente y no intenta retenerlo, ni repregunta, ni ofrece alternativas.

Si la petición además incluye borrado de información, revocación de autorización, queja sobre
datos o habeas data, Daniela la identifica como solicitud de privacidad y la dirige al canal
de MaxiCare: `maxicarecol@gmail.com` / `+57 321 981 2422`.

## 4. La comprobación en el despachador

Va **antes** que las siete guardas existentes, que es el orden que pidió marketing
(privacidad → canal → criterio → contacto).

```
   ¿este seguimiento es comercial?
        |
        +-- NO  (recordatorio_cita) --> las 7 guardas de siempre
        |
        +-- SI  --> ¿contactos.no_contactar?
                         |
                         +-- SI --> anular, motivo "baja_solicitada"
                         |
                         +-- NO --> las 7 guardas de siempre
```

### 4.1 Cómo se decide qué es comercial

`seguimientos.tipo` es hoy `TEXT` libre **escrito por el modelo** vía
`programar_seguimiento(tipo: str, ...)` (`herramientas.py:1385`), sin `Literal` que lo acote y
sin CHECK en la tabla.

Acotarlo es trabajo del sub-proyecto D. Aquí se resuelve con una **lista blanca de tipos no
comerciales**, que hoy tiene un solo elemento: `recordatorio_cita`. Todo lo demás se considera
comercial.

Esto falla hacia el lado correcto: si el modelo inventa un tipo, se trata como comercial y se
comprueba la baja. La alternativa —lista negra de tipos comerciales— deja pasar cualquier tipo
inventado directo al envío.

### 4.2 El motivo de anulación

`motivo_anulacion = "baja_solicitada"`. Se suma a los cinco que ya escribe el despachador
(`cita_cambio`, `cita_pasada`, `llego_tarde`, `cita_inminente`, `sin_plantilla`).

## 5. La señal en el contexto del turno

Un campo nuevo en el contexto, que sale de la base y **nunca del modelo**, igual que
`ctx.telefono_sin_paciente` (no negociable 12) y `ctx.entrada_solo_de_botones` (no negociable
23):

- `ctx.pidio_no_contacto: bool`

Cuando está puesto, la instrucción de Daniela es que no ofrezca el seguimiento, no lo insinúe
y no lo mencione. El tema no existe en esa conversación.

**El fallo que esto evita**, y que es el motivo de que el campo exista: doce días después de
darse de baja, María escribe por una muela rota. Para el sistema esa es una conversación nueva
y en blanco. Sin la señal, Daniela cierra como cierra siempre —«¿te escribo en unos días?»— y
le estaría pidiendo permiso para algo que ella ya negó expresamente.

## 6. `/clearstate`

`/clearstate` es irreversible y borra el rastro entero. Con esta migración pasa a tener una
excepción, y la excepción es el punto de toda la spec.

| Se borra | Sobrevive |
|---|---|
| conversaciones, historial del agente, citas, temas, casos | `contactos.no_contactar` y sus dos columnas |
| `contactos.aviso_mostrado_en` y `politica_version` | la tabla `consentimientos` entera |

Resetear a alguien lo devuelve a cero, pero **nunca lo devuelve a la lista de contactables**.
Ese «no» es del paciente, no del sistema.

El borrado registra un evento `rastro_borrado` en la bitácora.

**Decisión del 16/09/2026, contra lo que este párrafo decía antes: el origen es `codigo`, no
`clinica`.** Esta spec y el plan pedían `clinica`, y se cambió al implementarlo. La razón:
`clinica` significa «alguien de MaxiCare tomó esta decisión sobre este paciente», que es lo
que hay que poder distinguir el día que se audite la bitácora. `/clearstate` no es eso: lo
dispara el sistema sobre un número de la lista de pruebas, y anotarlo como `clinica` mete en
la columna que acredita quién decidió un evento que no decidió nadie. **Se descartó** añadir
un cuarto valor al CHECK (`sistema`, `pruebas`): el CHECK vive en cada esquema por la lección
de la 013, así que ampliarlo cuesta una migración en todos ellos para distinguir un caso que
`evento = 'rastro_borrado'` ya distingue por sí solo.

Hay precedente exacto: `/clearstate` borra los ejemplos de casos sin resolver **sin** bajar el
contador, por la misma razón (no negociable 22).

**Orden dentro de `borrar_rastro`:** el reseteo de `contactos` va junto al resto, pero el
`DELETE` de `contactos` **no existe** — la fila se conserva siempre. La bitácora no se toca en
ningún caso; la FK `ON DELETE RESTRICT` lo hace imposible aunque alguien lo intente.

## 7. Qué se toca

| Archivo | Qué |
|---|---|
| `migraciones/019_contacto_y_consentimiento.sql` | nuevo · las dos tablas, índices, CHECK por esquema |
| `persistencia.py` | `asegurar_contacto`, `leer_contacto`, `marcar_aviso_mostrado`, `pedir_baja`, `revocar_baja`, `anotar_consentimiento`; y el reseteo dentro de `borrar_rastro` |
| `atencion.py` | crear el contacto al entrar el mensaje; pegar el aviso al primer saliente; poner `ctx.pidio_no_contacto` |
| `herramientas.py` | dos tools nuevas: registrar la baja y revocarla |
| `seguimientos.py` | la comprobación previa a las siete guardas y el motivo `baja_solicitada` |
| `agentes.py` | las instrucciones de §3.2, §3.4 y §5 |
| `config.py` | URL de la política y versión vigente |
| `contratos.py` | el campo nuevo del contexto |

**El canal de WhatsApp no se toca.** No hay botones nuevos, no hay mensajes interactivos, no
hay nada que pedirle a Meta en este sub-proyecto.

## 8. Pruebas

Contra Neon, esquema `pruebas`, con `-m neon`:

1. La fila de contacto nace sola con el primer mensaje entrante, y es idempotente: dos
   mensajes seguidos no crean dos filas ni fallan.
2. El aviso sale pegado al primer mensaje saliente y **solo** al primero. El segundo mensaje
   de esa misma conversación no lo lleva, y el primero de una conversación nueva del mismo
   teléfono, tampoco.
3. Con la URL en `PENDIENTE`, el aviso no se emite y el mensaje sale limpio.
4. Si el envío falla, `aviso_mostrado_en` sigue NULL (§2.3).
5. La baja sobrevive a `/clearstate`; el aviso no.
6. La bitácora no se puede pisar: un `UPDATE` o un `DELETE` sobre `consentimientos` no forma
   parte de ninguna función de `persistencia.py`, y borrar el contacto padre falla por la FK.
7. **La prueba que más importa:** un número dado de baja no recibe reactivación **y sí recibe
   el recordatorio de su cita**. Las dos mitades en la misma prueba, porque el fallo que
   interesa es que alguien las una.
8. Un tipo de seguimiento inventado se trata como comercial (§4.1).

Offline, con `pytest -q` a secas: la lista blanca de tipos, el texto del aviso y la decisión
de `decidir` con la comprobación nueva, sin tocar la base.

**Scripts que hay que correr además**, porque doblan funciones de `src/` con firmas escritas a
mano y `pytest -q` no los caza: `scripts/probar_recordatorios.py` —dobla el despachador, y la
fila que alimenta `decidir` gana una columna— y `scripts/probar_atencion.py`, el turno de punta
a punta. Los dos son gratis: no gastan tokens.

## 9. Orden de construcción

1. Migración 019 y las funciones de `persistencia.py`, con sus pruebas de Neon. Nada más.
2. El contacto nace en `atencion.py`. Sin aviso todavía.
3. El aviso, con la URL en `PENDIENTE` (§2.2), y su marcado posterior al envío.
4. Las dos tools y las instrucciones de Daniela.
5. La comprobación en el despachador y el motivo nuevo.
6. `ctx.pidio_no_contacto` y la instrucción de no ofrecer.
7. El reseteo en `borrar_rastro`.

Cada paso deja la suite en verde. El 1 y el 2 no cambian ni un mensaje que vea un paciente.

## 10. Lo que esta spec NO resuelve

- **Menores de edad.** Marketing lo pidió por escrito el 15/09/2026 citando la Ley 1581, y el
  16/09/2026 el usuario decidió posponerlo. La razón de fondo: sin poder pedir documento de
  identidad (prohibición expresa del cliente, regla dura 4), lo único que Daniela puede hacer
  es **adivinar** la edad por el texto, y eso falla hacia los dos lados — le corta la atención
  a una adulta que escribe informal, y de forma intermitente, como ya pasó con el rótulo
  «Confirmar» (no negociable 23). **Este punto queda abierto y por escrito.**
- ~~**La URL definitiva de la política** (§2.2). Es un `PENDIENTE` real, no un descuido.~~
  **RESUELTO el 16/09/2026.** El cliente entregó el enlace público de Drive (versión 2.0,
  septiembre de 2026), comprobado accesible sin sesión antes de cablearlo. Vive en
  `config.POLITICA_DATOS_URL` y no en el `.env`, con copia congelada y SHA-256 en
  `docs/politica/`. Lo que **sigue** abierto es el destino ideal —una dirección del dominio de
  MaxiCare con una copia por versión—, porque Drive deja sobrescribir el mismo enlace: es un
  riesgo conocido, mitigado y anotado, no una tarea de código.
- **El permiso como aviso en lugar de sí expreso** (D2, D3). Decidido, implementado y
  pendiente de confirmar con marketing (§11).
- **La memoria del reencuentro** (sub-proyecto C). Daniela sigue tratando como desconocida a
  quien vuelve pasadas 24 h. Esta spec no lo toca.
- **El barrido, la cadencia y las tres plantillas** (sub-proyecto D). Incluido acotar
  `seguimientos.tipo` con `Literal` y CHECK, que aquí se esquiva con la lista blanca de §4.1.
- **El primer toque a las ~20 h dentro de ventana.** Es del sub-proyecto D. Se menciona aquí
  solo porque es la razón de que la plantilla de Meta haya dejado de ser urgente.
- **Que la nota del paciente en la bitácora no se pueda retirar por ninguna ruta.**
  `consentimientos.detalle` guarda la frase del paciente tal cual, escrita por el modelo,
  truncada a 500 caracteres, protegida por `ON DELETE RESTRICT` y por un `/clearstate` que por
  diseño no toca la bitácora: **ninguna ruta de borrado puede retirarla**. En un sistema cuyo
  propósito es el tratamiento de datos, y que dirige las solicitudes de habeas data a un
  correo, eso es una decisión de política de datos que esta spec no examinó y que le
  corresponde a MaxiCare: o se acota la nota a un motivo cerrado, o se permite redactar
  (`detalle = NULL`) en `rastro_borrado` dejando intactos evento, fecha, origen y versión, que
  es lo que la ley pide acreditar. **Pendiente de decisión.**

## 11. El párrafo para marketing

**Reemplazado el 16/09/2026 por `docs/politica/para-marketing-2026-09-16.md`**, que es lo que
hay que enviarles. Este párrafo se escribió antes de que existiera el PDF de la política, y la
política publicada cambió la pregunta: ya no es «esto se aparta de lo que nos mandaron», es
«§6 y §12 lo permiten, §5 y §10 lo contradicen, y hay que decidir cuál manda». Se conserva
abajo como estaba, porque es el estado en que se tomaron las decisiones de esta spec.

Va en lo próximo que se les mande, literal o reescrito, pero no se omite:

> El permiso para volver a escribirle a un paciente quedó implementado como un **aviso con
> derecho a oponerse**: Daniela informa que puede escribirle en unos días, y le escribe salvo
> que la persona diga que no. En cualquier momento puede pedir que no le escriban más, y eso
> se respeta de forma permanente. Lo mismo aplica a los datos de salud: se informa al primer
> contacto y se registra, sin una confirmación adicional.
>
> Esto se aparta de dos frases del documento que nos enviaron —«Daniela nunca infiere
> consentimiento simplemente porque alguien escribió por WhatsApp» y «el silencio no equivale
> a autorización»—. Se hizo así para no anteponer un bloque legal a la primera respuesta de
> cada paciente. **Conviene que lo confirme quien redactó la política antes de que la
> reactivación salga a producción.**
>
> Falta además la dirección definitiva donde va a vivir la política. El PDF de Drive nos sirve
> como origen, pero el enlace que ve el paciente debe estar en el dominio de MaxiCare y
> conservar una copia por versión: si la política se actualiza, hay que poder demostrar cuál
> vio cada persona.

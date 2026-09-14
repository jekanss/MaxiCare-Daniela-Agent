# MaxiCare · Daniela

Agente conversacional de WhatsApp para una clínica dental en Bogotá. Atiende a los
pacientes, responde con el catálogo real de la clínica, agenda contra Google Calendar y
escala a los doctores por Telegram cuando algo se sale de lo que puede resolver sola.

**El principio que decide los empates:**

> La seguridad clínica prevalece sobre cualquier objetivo comercial. El éxito no es
> acumular citas: es que el paciente llegue a la cita correcta.

No es una frase de presentación. Es la regla que resuelve cada decisión de diseño de este
repositorio, y se nota en el código: Daniela prefiere callar a inventar, prefiere escalar a
suponer, y cuando el calendario no arranca queda incapaz de agendar en vez de confirmar
citas que no existen.

---

## Qué hace, de punta a punta

```
  paciente                  Meta                  este servicio                doctores
  ────────                  ────                  ─────────────                ────────
     │                        │                         │                         │
     │──── «hola, cuánto ────►│──── webhook ───────────►│                         │
     │      vale una          │                         │                         │
     │      limpieza?»        │                    ①  reenvía ──────────────────►│
     │                        │                         │                         │
     │                        │                    ②  ventana de 20 s            │
     │                        │                       (junta los mensajes         │
     │──── «y con seguro?» ──►│──── webhook ───────────►│  que llegan de corrido)  │
     │                        │                         │                         │
     │                        │                    ③  un solo turno:             │
     │                        │                       tools → modelo → guardrails │
     │                        │                         │                         │
     │◄─── una respuesta ─────│◄──── envía ─────────────│                         │
     │     (no tres)          │                         │                         │
```

Cuando lo que llega es un archivo —una radiografía, una remisión— el camino se parte en
dos. Eso es **el muro**, y es lo más particular de este sistema:

```
   archivo del paciente
          │
          ├──① se entrega al doctor  ─────────────────────►  Telegram, hilo del paciente
          │    (ANTES de que ningún modelo lo haya visto)
          │
          └──② un agente lector lo lee, en paralelo
                     │
                     ├── contexto_clinico ──────────────────►  Telegram (solo doctores)
                     │
                     └── tipo, tratamiento, origen ─────────►  contexto de Daniela
```

Del mismo archivo salen dos cosas con dos destinatarios y **umbrales opuestos de qué se
puede decir**. El reparto es una función pura que devuelve un tipo al que le **falta** el
campo clínico: una fuga no es un descuido de código, es un error de construcción.

---

## Arquitectura

Una frontera, y todo lo demás se deriva de ella: **la definición de los agentes no sabe
nada del transporte.** Un `Agent` no sabe —y no le hace falta saber— si responde detrás de
un webhook de WhatsApp, de una API HTTP o de un comando de terminal. Esa decisión vive en
un único archivo.

| Módulo | Responsabilidad |
|---|---|
| `agentes.py` | Los dos `Agent`: `daniela` y `lector_archivos`. Sin `Runner.run`. |
| `herramientas.py` | Las nueve tools del plan y `consultar_citas`. Lógica de negocio pura. |
| `contratos.py` | Los modelos Pydantic que cruzan cada frontera. |
| `guardrails.py` | Los frenos: de entrada, de salida y de tool. |
| `conversacion.py` | **El turno**: guardrails, reintento, escalamiento. Sirve igual al chat web. |
| `atencion.py` | El turno **de WhatsApp**: candado, búfer, retardo humano, doble check azul. |
| `ingesta.py` | Recibe → deduplica → descarga → reenvía al doctor → registra. |
| `lectura.py` | El muro: el reparto, el lector, el hilo de cada paciente. |
| `calendario.py` | Google Calendar, con su cuenta de servicio. |
| `persistencia.py` | Neon (PostgreSQL). Pacientes, citas, conversaciones, conocimiento. |
| `canales.py` | WhatsApp y Telegram. El único sitio que habla HTTP hacia afuera. |
| `panel.py` · `autenticacion.py` | El panel web interno de la clínica. |
| `runtime.py` | **El único módulo que importa un framework de transporte.** FastAPI entra por aquí. |

`agentes.py`, `herramientas.py` y `contratos.py` nunca importan `runtime.py`. Hay una
prueba que lo comprueba, porque romper esa regla no es un detalle de estilo: acopla cada
agente al canal de hoy y convierte un cambio de transporte en una reescritura.

### Los dos agentes

- **`daniela`** — habla con el paciente. Diez tools, salida estructurada, cuatro
  guardrails colgados. Razonamiento bajo y verbosidad baja: un párrafo largo no se lee en
  un celular.
- **`lector_archivos`** — lee documentos clínicos para el doctor. Sin tools. **Sin los
  guardrails de Daniela, y es deliberado:** su trabajo es escribir contenido clínico fiel
  para un profesional. Lo que impide que eso llegue al paciente no es un guardrail, es el
  muro.

### Los guardrails

| Guardrail | Qué impide |
|---|---|
| `identidad_antes_de_datos` | tocar la agenda de alguien sin haber verificado quién es |
| `sin_cifra_no_documentada` | decir un precio que ninguna tool autorizó en este turno |
| `sin_hora_no_verificada` | decir cualquier hora concreta que ninguna tool devolvió en ese turno |
| `sin_lectura_clinica` | interpretar una radiografía o un síntoma |
| `uso_indebido` | jailbreak, extracción del prompt, uso como asistente general |

Los dos de cifras y horas comparan **dígitos**, y está documentado en el código: un precio
escrito en letras no se detecta. Se acepta porque el fallo que persiguen —inventar una
cifra— se escribe casi siempre en dígitos, y porque un guardrail que intente entender texto
libre deja de ser determinista, que era justo su valor.

---

## La agenda

**Google Calendar es la fuente oficial de la disponibilidad.** Los doctores gestionan su
tiempo desde su propio calendario y no desde ninguna pantalla de este sistema: si uno
bloquea de 2 a 5 de la tarde, Daniela deja de ofrecer esas horas en la siguiente consulta;
si borra el evento, vuelven a estar libres. No hay nada que sincronizar porque **no hay
caché**, y es deliberado: cada consulta le pregunta a Google.

Una hora se ofrece solo si pasa tres filtros a la vez, y ninguno sobra:

```
   la rejilla de bloques          ┐
   ∩ el horario de la clínica     │   los tres, o no se ofrece
   ∩ el cupo que queda en Neon    │   (y los mismos tres al AGENDAR,
   ∩ los bloqueos del doctor      ┘    no solo al ofrecer)
```

Que los tres valgan también **al escribir** no es redundancia: el paciente puede pedir «las
3» sin preguntar antes qué hay libre, y el doctor puede bloquear esa hora *después* de que
Daniela la ofreciera —en WhatsApp, minutos—. Comprobarlo solo al ofrecer deja el evento
encima de la cirugía de alguien.

El horario de atención vive en la tabla de configuración (`hora_apertura`, `hora_cierre`,
`hora_cierre_sabado`, `atiende_domingo`) y no en el prompt. La diferencia se midió en
producción: sin él, la ventana que el modelo pedía **era** la oferta, y una consulta por «el
próximo martes» devolvía las 00:00, 01:00 y 02:00. Una instrucción del prompt se puede
desobedecer; un filtro no.

Y pedir una hora cerrada no termina la conversación: Daniela recuerda el horario y ofrece
las horas libres más cercanas, buscando hacia adelante los días que haga falta —quien
escribe a las siete de la tarde no tiene nada más ese día—.

Cada cita crea un evento que le dice a la clínica lo que necesita para trabajar:

```
  Jean Carlos Chamorro · cordales
  ─────────────────────────────────────────────
  Teléfono: +57...          ← lo pone el código, nunca el modelo
  Servicio: cordales
  Motivo:   Quiere valoración de las cordales; le molestan al masticar.
```

El teléfono sale del webhook y no de lo que el modelo escriba, por la misma razón por la
que la solicitud de cita no tiene campo de teléfono: el caso real es la hija agendando por
su madre, y un número inventado manda el recordatorio a otra persona.

---

## Las cosas que no se pueden mover

Cada una está en el código con su porqué. Se listan aquí porque todas se aprendieron
rompiéndolas:

1. **Si el calendario no arranca, Daniela queda incapaz de agendar, nunca con un doble que
   diga que sí a todo.** El doble le confirma al paciente una cita que no existe: llega a
   una clínica donde nadie lo espera.
2. **Ninguna clave de idempotencia la escribe el modelo.** Las cuatro las arma el código.
   Con la del modelo, dos pacientes salían confirmados sobre un solo cupo.
3. **El candado del turno va por teléfono, y la lectura del estado va dentro.** Sacarla
   fuera devuelve dos carreras medidas: conversaciones duplicadas y escalamientos que el
   doctor no ve.
4. **Meta reintenta los webhooks, y hay dos deduplicaciones, no una.** Una protege el
   reenvío al doctor; la otra, el turno de Daniela.
5. **El búfer: la ventana va antes del candado, el retardo se descuenta (no se suma), y el
   búfer se saca en un `finally`.** Mover cualquiera de las tres rompe algo en silencio.
6. **Una cita de Daniela no es un bloqueo del doctor.** Sin esa marca, la clínica atiende a
   uno por hora en vez de a dos.
7. **Ninguna tool lee el reloj de la máquina.** El instante lo pone el contexto, una sola
   vez por turno. Dos relojes en el mismo módulo son dos relojes que un día discrepan, y el
   que no viaja en el contexto no se puede fijar desde una prueba.
8. **El archivo llega al doctor antes de que ningún modelo lo haya visto, y sin esperarlo.**
   El lector corre *en paralelo* con la ventana del búfer, nunca delante.
9. **No se registran cédulas ni documentos de identidad de ningún tipo.** La tabla de
   pacientes no tiene esa columna, y esa ausencia **es** la política.
10. **El relevo tiene una sola puerta de salida.** «Daniela callada» y «hilo abierto hacia
   el WhatsApp de alguien» son el mismo hecho escrito en dos sitios: tienen que dejar de ser
   verdad juntas. Cuatro disparadores, un único `cerrar`.
11. **Lo único que cruza del relevo hacia Daniela es que hubo relevo y, si la hubo, la
   cita.** Nunca lo que escribió el doctor, ni literal ni resumido: puede ser clínico, y eso
   entra en el contexto del agente que le habla al paciente. Por eso el cierre *pregunta* en
   vez de resumir — decide un humano.

---

## Stack

Python 3.12 · [uv](https://docs.astral.sh/uv/) · [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
· FastAPI · psycopg 3 · Neon (PostgreSQL) · Google Calendar API · React + Vite + Tailwind
para el panel · Docker + Traefik detrás de un túnel de Cloudflare.

---

## Arrancar

```bash
uv sync                       # dependencias
cp .env.ejemplo .env          # y rellenarlo: Neon, OpenAI, WhatsApp, Telegram, Google
cp datos/base_conocimiento.ejemplo.json datos/base_conocimiento.json
uv run python scripts/inicializar_base.py    # migraciones + carga + verificación
uv run uvicorn maxicare_daniela.runtime:app --reload
```

`inicializar_base.py` es idempotente; con `--solo-verificar` no escribe nada.

**Los datos de la clínica no están en este repositorio.** `datos/base_conocimiento.json` y
el documento maestro del que sale llevan la dirección de la sede, los nombres de los
odontólogos y las tarifas: son del cliente, no del código, y están en `.gitignore` por la
misma razón que `.env`. Lo que sí se versiona son los ejemplos de al lado
(`base_conocimiento.ejemplo.json`, `documento-maestro.ejemplo.md`), que conservan la forma
—las 73 entradas con su tratamiento y su concepto— y sustituyen el contenido.

Eso importa más de lo que parece: **el agente no puede decir una cifra que no haya salido de
ese archivo.** El guardrail `sin_cifra_no_documentada` extrae toda cifra de dinero de la
respuesta y exige que la tool de conocimiento la haya autorizado en ese mismo turno. Con el
ejemplo cargado, el sistema arranca y funciona; simplemente no tiene precios que dar.

### Pruebas

```bash
uv run pytest -q                                    # offline, sin red, ~18 s
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon    # contra la base, en un esquema aparte
```

Las que tocan la base escriben en esquemas de prueba y los borran al terminar,
comprobando el borrado. Nunca tocan el esquema donde viven los pacientes reales.

### Entregables por fase

Cada fase del proyecto cierra con algo que una persona puede correr y mirar. Los marcados
gastan tokens de la API; los demás, ni uno.

| Comando | Qué demuestra | ¿Gasta? |
|---|---|---|
| `scripts/probar_tools.py` | las diez tools contra la base | no |
| `scripts/probar_agentes.py` | los dos agentes y sus guardrails, contra la API real | **sí** |
| `scripts/probar_atencion.py` | el turno de WhatsApp de punta a punta | solo con `--chat` |
| `scripts/probar_lectura.py` | **el muro** y el hilo de cada paciente | solo con `--chat` |
| `scripts/probar_relevo.py` | que el bot PUEDA relevar: permisos, webhook y la sonda del hilo | no |
| `scripts/probar_persistencia.py` | que una conversación sobrevive a reiniciar el proceso | solo con `--chat` |
| `scripts/probar_calendario.py` | Google Calendar; `--diagnosticar` solo lee | no |
| `scripts/probar_panel.py` | el panel interno | solo con `--chat` |

---

## Cómo está construido este repositorio

El diseño se cerró antes de escribir código, decisión por decisión, y cada una guarda **la
alternativa que se descartó y la condición que la reabriría**. Eso vive en
`docs/agentes/`, y se consulta sin leerlo entero:

```bash
uv run python scripts/ver_plan.py fases        # también: agentes, herramientas, guardrails…
```

Las fases se implementaron con especificación → plan → ejecución, y cada una dejó su
rastro en `docs/superpowers/`. Dos hábitos que valieron más de lo que costaron:

- **Cada prueba importante se comprueba rompiendo el código a propósito.** Si la prueba no
  cae con el fallo puesto, la prueba no vale y se rehace. Así se descubrió que dos de los
  tres invariantes del búfer no los vigilaba nadie, y que la prueba del muro pasaba con el
  muro roto.
- **Lo que no se sabe se marca con el literal `PENDIENTE`, nunca con un valor plausible.**
  Una suposición razonable no se distingue de un hecho verificado cuando alguien la lee seis
  meses después.

`CLAUDE.md` y `.claude/rules/` son las notas operativas del repositorio: las trampas del
entorno, los invariantes y el porqué de cada uno.

---

## Estado

El proyecto se construye en diez fases, cada una cerrada por algo que una persona puede
correr y mirar. Ocho están cerradas y dos sin empezar:

| | Fase | |
|---|---|---|
| ✅ | 1 · Contratos y base de conocimiento | |
| ✅ | 2 · WhatsApp y el viaje del archivo | |
| ✅ | 3 · Las nueve tools | tres citas simultáneas sobre un cupo → dos |
| ✅ | 4 · Los dos agentes y sus guardrails | |
| ✅ | 5 · Cascarón web y chat de pruebas | |
| ✅ | 6 · Ingesta, **el muro** y **el relevo** | un doctor toma la conversación y habla él |
| ✅ | 7 · Persistencia y observabilidad | una conversación sobrevive al reinicio |
| ✅ | 8 · Pantallas de operación | |
| ⬜ | 9 · Evals y piloto real | 22 evals antes de atender pacientes |
| ⬜ | 10 · Documento de caso de éxito | depende del piloto |

**El relevo** cerró la fase 6 el 14/09/2026 y está en producción: un doctor pulsa «Hablar yo
con el paciente» en el escalamiento, Telegram le abre el hilo de esa persona, y lo que
escriba ahí le llega al paciente por WhatsApp tal cual —sin firma, sin pasar por ningún
modelo, fotos y audios incluidos—. Daniela se calla mientras dura y vuelve sola.

Lo que lo hace seguro no es el camino feliz, sino que **el hilo abierto es un canal en vivo
hacia el teléfono de alguien**, y eso obliga a que solo haya una forma de cerrarlo:

```
   tomada_por puesto  ==  Daniela callada  Y  hilo abierto en Telegram
                          └── las dos cosas dejan de ser verdad JUNTAS, o no hay cierre
```

Por eso hay cuatro disparadores y **una sola puerta** (`relevo.cerrar`): el botón «Listo»,
el tiempo agotado, cerrar el hilo a mano, y que alguien lo borre. El último no lo avisa
Telegram —no existe el evento— así que hay que preguntárselo, y ese es el caso que más caro
salió (abajo).

### Lo que cerró la agenda, 13/09/2026

El sistema lleva días atendiendo por WhatsApp, y casi todo lo de esta tanda salió de leer
conversaciones reales en vez de imaginarlas:

- **Google Calendar manda también al escribir.** Se respetaba al ofrecer desde la fase 3;
  `crear_cita` y `reprogramar_cita` pasaban de largo por los bloqueos del doctor.
- **La clínica tiene horario, y la rejilla no lo sabía.** Un paciente pidió cita «el próximo
  martes» y Daniela le ofreció «12:00 am, 1:00 am o 2:00 am». El modelo no alucinó: tradujo
  correctamente unas horas que el sistema le dio.
- **Mover una cita al pasado destruía una cita buena.** `crear_cita` comprobaba la hora
  pasada y `reprogramar_cita` no, y ahí el daño es mayor: suelta el cupo que el paciente sí
  tenía y arrastra el evento del doctor detrás.
- **Recordar el horario le costaba la respuesta al paciente.** «Atendemos de 8:00 a 5:00»
  son dos horas concretas, y sin autorizarlas el guardrail bloqueaba el mensaje entero: la
  pregunta más inocente del repertorio acababa en «te escribe el doctor».
- **El evento del calendario no servía para llamar a nadie.** Ahora lleva teléfono, servicio
  y motivo.
- Y una que no se ve en ninguna conversación: **el prompt pagaba el minuto**. El bloque de
  fecha llevaba `HH:MM`, así que cada minuto nuevo invalidaba la caché de las herramientas y
  del historial completo. Truncarlo a la hora no cambia ninguna conducta y, contando tokens
  sobre una conversación de agenda de seis turnos, sale por algo menos de la mitad del coste
  anterior. Es un cálculo, no una medición contra la API: el número real se confirma leyendo
  `usage.cached_tokens` en producción, y eso está pendiente.

**La fase 7** eran dos cosas, y las dos están hechas.

La **persistencia**: el historial del diálogo vivía en un diccionario del proceso, y un
reinicio lo borraba —los datos no: paciente, citas y estado de oportunidad siempre
estuvieron en Neon—. Ahora vive en la base, en `agent_sessions` y `agent_messages`
(migración `010`), con el `session_id` igual al id de la conversación para que `/clearstate`
pueda borrarlo y para que nadie quede atado para siempre a todo lo que dijo alguna vez.
`scripts/probar_persistencia.py --chat` lo demuestra con un reinicio de verdad: un proceso
recibe «hola, soy Ana» y muere; otro proceso, con la memoria vacía por construcción,
responde a «¿cómo me llamo?».

La **observabilidad**: ninguna llamada al modelo sube el contenido de la conversación a las
trazas de OpenAI, las tres puertas pasan por `config.config_de_corrida`, y desde el
13/09/2026 van agrupadas por conversación —el `group_id` es el UUID de `conversaciones`,
nunca el teléfono— con el canal y la versión del prompt como metadatos.

Queda un número por medir, y está marcado `PENDIENTE` a propósito: cuánto historial recordar
(`LIMITE_HISTORIAL_SESION`). El SDK cuenta *items*, no mensajes, y un turno en que Daniela
consulte el conocimiento, mire la agenda y registre el estado gasta seis o siete él solo.
Hasta que haya conversaciones reales que contar, `scripts/medir_historial.py` no tiene sobre
qué correr, y un número inventado hoy no se distinguiría de uno medido.

### Lo que cerró el relevo, 14/09/2026

El relevo se desplegó por la mañana y un doctor lo usó el mismo día. Casi todo lo que sigue
salió de esa tarde, no del diseño:

- **El hilo cuelga del teléfono, no de la ficha.** Colgaba de `pacientes`, y un número que
  escribe por primera vez no tiene fila ahí: sus radiografías caían al canal general, sus
  textos no se archivaban en ninguna parte, y el hilo que le abría el botón nacía vacío —el
  doctor entró y tuvo que empezar preguntando quién era—. La migración 014 lo mueve a su
  propia tabla y deja caer las dos columnas viejas: dos sitios donde vive el mismo hecho es
  exactamente el bug del mes que viene.
- **Un archivo que baja de Telegram no dice qué es.** Su servidor responde
  `application/octet-stream` a todo, y Meta rechaza la subida entera con eso: la foto que el
  doctor mandaba al paciente moría en un 400. El tipo sale de la extensión del `file_path`.
- **Al doctor se le vuelca la conversación al entrar**, con horas, leída de la base. Sin
  modelo: no cuesta nada y no cruza el muro, porque todo eso ya lo vio el paciente.
- **Toda cita salida de un relevo se registraba como `"valoracion"`**, fijo — y
  `"valoracion"` no es ninguna de las catorce claves de la clínica. Ahora se pregunta: de qué
  es, cuándo, y el nombre si el número todavía no tiene. Con eso se toma el cupo, se crea el
  evento en Google Calendar y se escribe la fila, en ese orden, igual que si la hubiera
  agendado el paciente. Sin el nombre, la cita entraba en la agenda como «PENDIENTE ·
  Cordales», que es el caso *normal* de una cita de relevo: el paciente nuevo con dolor es
  justo el que más escala.
- **Borrar un hilo no emite ningún evento** —cerrarlo sí—, así que hay que preguntarle a
  Telegram si sigue vivo. Y ahí estuvo el peor fallo del proyecto: la primera sonda preguntó
  con `editForumTopic` sin argumentos, razonando que sin nada que cambiar no tendría efecto.
  Cierto, y **por eso mismo no valida el id**: devolvía `ok: true` para un hilo borrado media
  hora antes. La sonda decía que sí a todo, el agujero que venía a tapar siguió abierto un
  día entero —relevo tomado, Daniela callada, cuatro mensajes del paciente sin llegar a
  nadie— y no hubo un solo error en el log ni una prueba en rojo: las offline doblan a
  Telegram, así que ninguna podía verlo.

  Se arregló midiendo cuatro candidatas contra el grupo real en dos ejes —¿distingue? ¿deja
  mensajes de servicio?— en vez de eligiendo una por razonamiento. Gana `reopenForumTopic`.
  Y la guarda vive donde puede funcionar: `scripts/probar_relevo.py`, contra la API de
  verdad.

  La lección no es sobre Telegram. Es que **una suite verde sobre dobles no dice nada del
  sistema del que los dobles son copia**, y que razonar sobre una API no es medirla.

- **La lectura clínica es una ficha, no un párrafo.** El doctor la lee en el móvil entre
  paciente y paciente, y le llegaba un muro de veinte líneas de prosa. Ahora son seis como
  mucho, con rótulo (`Motivo:`, `Hallazgos:`, `Antecedentes:`, `Piden:`, `Ojo:`), y **la
  línea que el documento no respalde se omite entera** — nada de «no refiere» ni «sin
  datos»: ocupar una línea con una ausencia es lo que la volvía ilegible. Se resalta solo el
  rótulo, nunca el contenido clínico: decidir qué importa dentro del texto es del doctor.
- **Un botón de Telegram no puede llevarte a ninguna parte.** `answerCallbackQuery` con una
  `url` hacia un tema del propio supergrupo responde `URL_INVALID`. Lo que navega es la
  notificación, así que el aviso del hilo lleva una **mención** al doctor que pulsó —suena
  aunque tenga el grupo silenciado— y el enlace del canal general apunta al mensaje concreto
  dentro del hilo, no al hilo a secas: `t.me/c/<chat>/<n>` es ambiguo en un foro, ese `<n>`
  es un id de mensaje.

# Terminar el panel: fuera Métricas, dentro Configuración y Estado del sistema

Fecha: 25/09/2026 · Estado: diseño aprobado, pendiente de plan de implementación

## Qué se construye y por qué

El panel tiene nueve secciones en el menú y siete construidas. Las tres que faltan llevan a
`web/src/pantallas/Pendiente.tsx`, un cartel que explica qué hará esa pantalla y qué falta
antes. El cartel es honesto, pero dos de esas tres esperas ya caducaron:

- **Configuración** decía esperar «la pantalla»: las perillas ya viven en la base y
  funcionan. Lo que no existe es ni una línea de Python que las escriba — hoy se cambian
  entrando a Neon por SQL a mano. Esa es la mitad que falta, y es real.
- **Estado del sistema** decía esperar la conexión con Google Calendar. Ya está: el
  calendario arranca, y lo que no hay es dónde mirarlo.
- **Métricas** esperaba «un mes de operación real», y se elimina por decisión del usuario.

El resultado es un panel sin ninguna sección muerta.

## Principio que decide los empates de este trabajo

El mismo del proyecto: la seguridad clínica prevalece. Aplicado aquí significa dos cosas
concretas que aparecen varias veces más abajo:

1. **Una pantalla de diagnóstico que no carga es peor que una que carga incompleta.** De ahí
   que las sondas externas vayan detrás de un botón y no en la carga inicial.
2. **Una perilla que se guarda pero no surte efecto es peor que una que no se puede tocar.**
   De ahí el refresco explícito de los globals del proceso tras cada escritura.

---

## Decisión 1 · Eliminar Métricas arrastra a `Pendiente.tsx`

Tras construir Estado y Configuración y borrar Métricas, **ninguna sección del menú lleva ya
al cartel de «todavía no construida»**. Dejar el archivo sería código muerto que la siguiente
persona intenta entender antes de darse cuenta de que nadie lo alcanza.

Se elimina, entonces, el mecanismo entero:

| Archivo | Cambio |
|---|---|
| `web/src/pantallas/Pendiente.tsx` | se borra |
| `web/src/componentes/Sidebar.tsx` | fuera `'metricas'` de `SeccionId`; fuera su entrada de `SECCIONES`; fuera el campo `fase` del tipo `Seccion` y el rótulo `F{fase}` del botón |
| `web/src/App.tsx` | fuera el `import`; la rama final `: ( <PantallaPendiente/> )` pasa a ser las ramas de `estado` y `configuracion` |

El comentario de `SECCIONES` que justifica no esconder las secciones sin construir pierde su
objeto y se sustituye por una línea que diga qué son ahora las secciones: todas construidas.

**Qué le pasa a quien tenga `#/metricas` guardado:** cae en Inicio. `seccionDelHash` ya
devuelve `'inicio'` para cualquier hash que no esté en `TODAS`, así que no hace falta ni una
línea nueva. No se añade una redirección: un marcador a una pantalla que se eliminó a
propósito no merece código permanente.

---

## Decisión 2 · Configuración: las trece perillas operativas

### Qué se edita y qué no

```
 Agenda         capacidad_por_hora · duracion_cita_minutos
                hora_apertura · hora_cierre · hora_cierre_sabado · atiende_domingo
 Relevo         aviso_relevo_minutos · cierre_relevo_minutos
 Recordatorios  hora_recordatorio_vispera · horas_minimas_para_recordar
 Reactivación   tope_diario_reactivacion · max_reactivaciones_12m · max_seguimientos_fallidos
```

**Las dos que quedan fuera, y por qué no es pereza:**

- `telegram_topic_general`. La fija la migración 005 al valor `0`, que significa el General
  del supergrupo. Un dedo torpe aquí manda los escalamientos a un tema que no existe, y el
  fallo es silencioso: Telegram acepta el envío y nadie lee nada. No hay ningún caso de uso
  que pida cambiarla desde una pantalla.
- `medicion_sin_resolver_desde`. Es la única clave no entera de la tabla — `leer_configuracion`
  la descarta en silencio en su `int(valor)` — y reescribirla borraría el sentido de «3 veces»
  en la pantalla de Sin resolver, que cuenta desde esa fecha. La migración 029 la escribe una
  vez y nadie más la toca.

Que no se editen **no significa que no se vean**: las dos aparecen en la pantalla en una
sección de solo lectura al pie, con la explicación de por qué no tienen campo. Esconderlas
haría que alguien buscara en vano la perilla del tema General.

### El camino de una escritura

```
PATCH /api/configuracion          exigir_rol("admin")
  │
  │  cuerpo: {"capacidad_por_hora": 3, "hora_cierre": 18}     ← solo lo que cambia
  │
  ├─ 1. Pydantic valida rangos por campo            → 400 con el campo y el rango
  ├─ 2. panel.guardar_configuracion(conn, cambios, usuario=quien["usuario"])
  │       with conn.cursor() as cur:                ← UNA sola transacción
  │         SELECT clave, valor FROM configuracion WHERE clave = ANY(...)
  │         valida las invariantes CRUZADAS sobre el estado RESULTANTE
  │         por cada clave cuyo valor cambie de verdad:
  │           UPDATE configuracion SET valor = %s, actualizado_en = now()
  │           _anotar(cur, tabla='configuracion', clave, anterior, nuevo, usuario)
  │       conn.commit()
  │
  ├─ 3. _cargar_configuracion_operativa()           ← el refresco; ver más abajo
  └─ 4. devuelve la configuración completa ya guardada
```

Cinco detalles que este camino tiene y un CRUD ingenuo no:

**(a) Una fila de bitácora por perilla, no una por petición.** Es el patrón que ya siguen
`panel.cambiar_tratamiento` y `panel.guardar_ficha`. Una fila por petición haría que el
historial dijera «admin cambió la configuración» y obligara a adivinar qué.

**(b) Si el valor no cambia, no se escribe nada.** Ni `UPDATE` ni fila de bitácora. Abrir la
pantalla y pulsar Guardar sin tocar nada no puede ensuciar el historial con trece filas que
no cambiaron nada. Ya es el comportamiento de las tres rutas de escritura que existen.

**(c) `actualizado_en` se pone a mano.** La columna tiene `DEFAULT now()`, pero un `DEFAULT`
solo actúa en el `INSERT`: un `UPDATE` que no la nombre deja la fecha de la migración.

**(d) `UPDATE`, nunca `INSERT`.** Las trece filas existen desde su migración, y `descripcion`
es `NOT NULL`: un `INSERT` desde la pantalla tendría que inventarse una descripción. Si una
clave no está en la tabla, es un error del despliegue y se devuelve 500, no se crea la fila.

**(e) Las invariantes cruzadas se validan sobre el estado RESULTANTE.** Este es el punto que
un validador por campo no puede cubrir.

### Rangos e invariantes

Por campo (Pydantic, `Field(ge=, le=)`, error 400):

| clave | rango | por qué |
|---|---|---|
| `capacidad_por_hora` | 1–6 | `0` apagaría la agenda entera sin decirlo en ninguna parte |
| `duracion_cita_minutos` | 15–180 | por debajo de 15 no cabe una valoración; 180 es media jornada |
| `hora_apertura` | 0–23 | hora del día |
| `hora_cierre` | 0–23 | hora del día |
| `hora_cierre_sabado` | 0–23 | hora del día |
| `atiende_domingo` | 0 o 1 | **entero, no booleano**: `leer_configuracion` hace `int(valor)` y un `'false'` se descartaría en silencio (lo dice la migración 012) |
| `aviso_relevo_minutos` | 5–1440 | |
| `cierre_relevo_minutos` | 10–1440 | |
| `hora_recordatorio_vispera` | 0–23 | hora del día |
| `horas_minimas_para_recordar` | 1–48 | |
| `tope_diario_reactivacion` | 0–50 | **`0` es válido y significa apagado**: es la forma de frenar la reactivación sin tocar el `.env` |
| `max_reactivaciones_12m` | 0–24 | `0` también apaga |
| `max_seguimientos_fallidos` | 1–10 | |

Cruzadas, en `panel.guardar_configuracion`, con `ValueError` que la ruta traduce a 400:

1. `aviso_relevo_minutos < cierre_relevo_minutos`. Ya es una invariante probada del proyecto
   (`tests/test_base_conocimiento.py`). Si no, se avisa de un relevo que ya se cerró solo.
2. `hora_cierre > hora_apertura`.
3. `hora_cierre_sabado > hora_apertura`.

Las tres se comprueban contra **el valor que va a quedar**, mezclando lo que llega en la
petición con lo que ya hay en la tabla. Subir el aviso a 200 minutos con el cierre en 180 es
válido campo a campo y rompe el relevo; solo se ve mirando los dos juntos.

### El refresco, que es la mitad del trabajo

Las trece perillas no llegan al sistema por el mismo camino, y la diferencia se comprobó
leyendo cada consumidor, no suponiéndola:

```
 ONCE de las trece          cada consumidor RELEE la tabla en su propia pasada:
                              · atencion._leer_estado, en cada turno
                              · el despachador de recordatorios, en cada pasada
                              · el barrido de reactivación, en cada pasada
   ───────────────────────> el valor nuevo entra solo. Nada que hacer.

 aviso_relevo_minutos       runtime._relevo_minutos, un global que SOLO se llena
 cierre_relevo_minutos      en el startup, y del que beben CUATRO sitios: el
                            barrido de relevos, las dos ramas del webhook de
                            Telegram y la ruta de conversaciones del panel.
   ───────────────────────> SIN refresco: guardas 240, la pantalla enseña 240,
                            y el relevo sigue cerrando a los 180 hasta el
                            próximo despliegue. Sin un error en ningún log.
```

Ojo al matiz de `cierre_relevo_minutos`, que está en los dos lados: el turno de Daniela lo
relee de la tabla, y el barrido de fondo lo lee del global. Sin el refresco quedan en
desacuerdo entre ellos, que es peor que quedar los dos viejos.

Por eso la ruta llama a `_cargar_configuracion_operativa()` después del `commit`. Es
exactamente el patrón que ya usa `_refrescar_vocabulario(conn)` tras crear o editar un
tratamiento, con el comentario que lo justifica: «para que no haga falta reiniciar nada».

La función está decorada con `@app.on_event("startup")`, pero es una función normal y se
puede llamar. Relee las dos del relevo y `telegram_topic_general`, que no cambia: releerla
es inocuo.

### El aviso del horario duplicado

`hora_apertura` y `hora_cierre` mandan sobre los cupos que Daniela **ofrece**. Lo que Daniela
**recita** cuando alguien pregunta «¿a qué hora abren?» sale de la ficha `_general/horario`
de la base de conocimiento, que es texto libre que edita un humano.

Son dos sitios y hay que moverlos juntos. El usuario decidió no editar la ficha desde esta
pantalla, así que la pantalla **lo dice donde importa**: cuando el formulario tiene una hora
modificada sin guardar, aparece un aviso con enlace a Tratamientos, sección General, concepto
horario. No es un modal ni un bloqueo: es la frase que evita que Daniela diga «cerramos a las
5» mientras ofrece un cupo a las 6.

### Permisos

`exigir_rol("admin")` para la escritura. Es el mismo nivel que crear un tratamiento o borrar
un hilo, y cambiar `capacidad_por_hora` altera lo que Daniela le promete a un paciente real
en el turno siguiente.

La **lectura** (`GET /api/configuracion`) va con `usuario_actual`: recepción puede necesitar
saber a qué hora cierra la clínica según el sistema. El JSON lleva `es_admin` para que la
pantalla enseñe los campos en solo lectura a quien no puede escribir — pero, como siempre en
este proyecto, el control vive en `exigir_rol` y no en el botón escondido.

---

## Decisión 3 · Estado del sistema: dos rutas, dos costes

### `GET /api/estado` — al abrir la pantalla

Barato: una conexión a Neon y lectura de variables del proceso. Sin una sola llamada externa.

| Bloque | Señal | De dónde sale |
|---|---|---|
| Base | conexión viva | `persistencia.conectar` en un `try` |
| Base | citas futuras sin `evento_calendar_id` | **consulta nueva** |
| Calendario | `CalendarioGoogle` / `CalendarioCaido` | `type(runtime._calendario).__name__` |
| Frenos | `daniela_responde`, `leer_archivos`, `transcribir_audio` | `config.*` |
| Atención | sin responder | `persistencia.contar_sin_responder(conn, tipos_sin_turno=ingesta.TIPOS_QUE_NO_ABREN_TURNO)` |
| Atención | sin entregar al doctor | `reenviado_en IS NULL AND fallo IS NOT NULL` |
| Atención | relevos abiertos | `persistencia.contar_relevos_abiertos(conn)` |
| Cola | recordatorios por despachar | `len(persistencia.seguimientos_por_despachar(conn, ahora, limite=...))` |
| Cola | reactivaciones comprometidas hoy | `persistencia.contar_comprometidos_hoy(conn, ahora=...)` |
| Relevo | webhook de Telegram con secreto | `bool(config.telegram_webhook_secret)` |
| Evaluador | fallos en la ventana | `guardrails.fallos_recientes_de_evaluador()` |
| Solo admin | gasto del día en USD | `persistencia.gasto_del_dia(conn)` |
| Solo admin | variables de entorno que faltan | las seis que ya comprueba `/salud` |

**Las dos últimas solo si `rol == 'admin'`.** Las claves no viajan siquiera en el JSON para
quien no lo sea. El gasto en dólares y qué credenciales faltan son información de negocio y
de seguridad; recepción no las necesita para su trabajo.

**Esta ruta no sustituye a `/salud`.** `/salud` es público porque lo consumen el healthcheck
de Docker y `desplegar.sh`, y por eso nunca revela detalle. Esta exige sesión y por eso sí
puede. Se quedan las dos, y `/salud` no se toca.

**Dos honestidades que la pantalla dice en voz alta:**

- Los fallos del evaluador viven en **memoria del proceso** (`guardrails._fallos_de_evaluador`,
  ventana de 600 s), no en la base: un despliegue los borra. La pantalla los rotula «desde el
  último arranque», no «hoy». Un contador que se reinicia solo y no lo dice es un contador que
  miente.
- **No existe ninguna marca de «última sincronización con Calendar»**, porque la reconciliación
  corre bajo demanda —cuando alguien abre la Agenda o un paciente pregunta por su cita— y no
  guarda la hora. No se inventa una: la pantalla enseña las citas huérfanas, que es el hecho
  medible, y dice con una frase que la sincronización ocurre al abrir la Agenda.

### `POST /api/estado/sondas` — solo al pulsar «Comprobar ahora»

`exigir_rol("admin")`. Dos viajes de red, cada uno en su propio `try`: que Meta no conteste no
puede dejar sin respuesta lo de Telegram.

| Señal | Origen |
|---|---|
| Las cuatro plantillas: cuáles están `APPROVED` | Graph API, `GET {waba}/message_templates` |
| Calidad del número y límite de envío | `canales.WhatsApp.calidad_del_numero()` (ya existe) |
| Webhook de Telegram: url, updates encolados, último error | Telegram, `getWebhookInfo` |

**Por qué detrás de un botón y no en la carga:** si el panel las hiciera al abrir, una caída
de Meta convertiría la pantalla de diagnóstico en una pantalla que no carga. Es el fallo más
tonto posible en la herramienta que existe precisamente para saber qué está roto.

**Es un `POST` aunque no escriba nada en la base**, y es deliberado: hace dos llamadas a
terceros, así que no debe poder dispararse por un prefetch del navegador ni quedar cacheada.
Es el mismo criterio por el que `GET /api/agenda` está documentado como la excepción que sí
escribe: aquí se prefiere el verbo que describe el coste.

**Dos trampas conocidas que la pantalla enseña tal cual:**

- `getWebhookInfo` **no dice si hay `secret_token`**. Así que la pantalla enseña dos hechos
  distintos y no los mezcla: «la URL está puesta» (lo dice Telegram) y «el secreto está
  configurado en este proceso» (lo dice `config`). Sin secreto, `/webhook/telegram` responde
  403 a todo y el botón «Hablar yo con el paciente» no hace nada, sin error en ningún log.
- Consultar `getWebhookInfo` es **solo lectura y no rompe nada**. Lo que rompe el `getUpdates`
  de `obtener_chat_telegram.py` es llamar a `setWebhook`, y esta ruta no lo hace nunca.

---

## Lo que hay que escribir de cero o portar

| Qué | Dónde va | Estado hoy |
|---|---|---|
| `panel.guardar_configuracion(conn, cambios, *, usuario)` | `panel.py` | no existe ninguna escritura de esa tabla |
| `panel.configuracion_editable(conn)` | `panel.py` | lectura con valor, descripción y `actualizado_en` |
| contar citas futuras sin `evento_calendar_id` | `persistencia.py` | cero coincidencias en todo `src/` |
| `canales.WhatsApp.estado_de_plantillas()` | `canales.py` | vive solo en `scripts/probar_plantilla.py` |
| `canales.Telegram.estado_del_webhook()` | `canales.py` | vive solo en `scripts/configurar_webhook_telegram.py` |

**Al portar las dos últimas, los scripts pasan a llamarlas en vez de doblarlas.** Hoy son
firmas escritas a mano que `pytest -q` no corre, y el `CLAUDE.md` marca esa duplicación como
trampa: cambiar la firma de algo que un script dobla lo rompe en silencio con la suite entera
en verde. Reducir la duplicación aquí es parte del trabajo, no un refactor aparte.

## Contratos de las rutas nuevas

Las cuatro se declaran **antes** del catch-all `GET /{ruta_completa:path}` que cierra
`runtime.py`, o se las traga y devuelven el `index.html` — lo que produce el peor error de
depurar: un `SyntaxError` de JSON en la consola del navegador sin pista de la ruta.

```
GET   /api/configuracion          usuario_actual
      -> {valores: {clave: int}, descripciones: {clave: str},
          actualizado_en: {clave: iso}, fijas: [{clave, valor, por_que}], es_admin: bool}

PATCH /api/configuracion          exigir_rol("admin")
      <- {clave: int, ...}  (solo las que cambian)
      -> el mismo cuerpo que el GET, ya guardado
      400 si un rango o una invariante falla, con el campo dentro del mensaje

GET   /api/estado                 usuario_actual
      -> {base: {...}, calendario: {...}, frenos: {...}, atencion: {...},
          cola: {...}, relevo: {...}, evaluador: {...},
          gasto: {...} | ausente,  entorno: {...} | ausente}

POST  /api/estado/sondas          exigir_rol("admin")
      -> {whatsapp: {plantillas: [...], calidad, limite} | {error},
          telegram: {url, pendientes, ultimo_error} | {error},
          secreto_del_webhook: bool}
```

## Verificación

| Qué | Cómo |
|---|---|
| `guardar_configuracion`: escribe, audita, no escribe si no cambia, invariantes | `tests/test_panel.py`, **de Neon**: `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` |
| Validación de rangos, sin base | `tests/` offline, con `uv run pytest -q` |
| Que nada se rompió | `uv run pytest -q` **y** `-m neon` — el no negociable 8 dice que a secas no caza `tests/test_panel.py` |
| Firmas que los scripts doblan | los seis entregables que no gastan tokens |
| El cascarón web | `uv run python scripts/probar_web.py` |
| El panel de punta a punta | `uv run python scripts/probar_panel.py` — **con la vista puesta**: desde que no hay base alterna, ese script siembra en el `public` de producción a propósito, y hoy esa base ya tiene pacientes |

**Se sigue TDD**: la prueba de cada invariante se escribe antes que la validación.

## Fuera de alcance, dicho a sabiendas

- **El borrador de prompts con historial de versiones.** Era la segunda mitad que prometía el
  cartel de Configuración. Es alcance nuevo, no está en `plan-agentes.json`, y editar el
  comportamiento de Daniela sin un paso de prueba obligatorio es la clase de cosa que se
  arregla en producción con un paciente delante. Merece su propio diseño.
- **Editar la ficha `_general/horario`** desde Configuración. Decisión del usuario. Se
  compensa con el aviso descrito arriba.
- **Una marca persistida de última sincronización con Calendar.** Añadirla significa escribir
  en cada reconciliación, que hoy corre dentro de una lectura. No se hace por una pantalla.
- **El chat de pruebas.** Se comprobó durante el diseño y funciona.

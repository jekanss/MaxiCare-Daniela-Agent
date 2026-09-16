# SDD ledger — plan: docs/superpowers/plans/2026-09-15-sin-resolver.md

Spec: `docs/superpowers/specs/2026-09-15-sin-resolver-design.md` (legible, leído).
Rama: `sin-resolver`, creada desde `main` en `28a61a5`.
No hay worktree aparte: un worktree nuevo no tendría `.env` ni `.venv`, y sin `.env` no
corren las pruebas de Neon ni `scripts/`. El repo ya trabaja con ramas en el árbol principal
(24 ramas de fase previas, todas fusionadas a `main`).

## Escaneo previo — pares de tareas que comparten archivo o interfaz

| Par | Qué comparten | Produce vs. consume | Hallazgo |
|---|---|---|---|
| 1↔2 | `tests/test_sin_resolver.py` | T1 lo crea, T2 le AÑADE dos pruebas al final | limpio |
| 1↔2 | `casos_del_turno`, `registrar_caso`, `olvidar_ejemplos_de` | T2 llama `registrar_caso(conn, huella=, tipo=, escalo=, ejemplo=, telefono=)` contra `(conn, *, huella, tipo, escalo=0, ejemplo=None, telefono="")` | limpio |
| 1↔3 | `casos_sin_informe`, `guardar_informe` | T3 llama `casos_sin_informe(conn, limite=)` y `guardar_informe(conn, huella=, informe=, sobre=)`; `texto_del_caso` lee `tipo/huella/contador/escalo/primera_vez/ultima_vez/ejemplos`, las siete que T1 devuelve | limpio |
| 1↔4 | `casos_recientes` | endpoint llama `casos_recientes(conn)`; el tipo `CasoSinResolver` de `api.ts` tiene las ocho claves que T1 devuelve, `informe` incluido | limpio |
| 1↔5 | las cinco de persistencia | mismas firmas | limpio |
| 3↔4 | `src/maxicare_daniela/runtime.py` | T3 añade tarea de fondo + dos `@app.on_event`; T4 añade `GET /api/sin-resolver` antes de la ruta comodín | limpio: regiones disjuntas y T3 va primero. `@app.on_event` es el patrón vivo del archivo (10 usos), no `lifespan` |
| 2↔3, 2↔4, 2↔5, 4↔5 | nada | — | sin superficie común |

## Escaneo previo — coherencia interna de cada tarea

| Tarea | ¿Las pruebas que especifica concuerdan con el código que especifica? | Hallazgo |
|---|---|---|
| 1 | huellas: `falta_dato:{trat}:{concepto}`, `guardrail:{nombre}:{trat\|_general}`, `humano:{motivo}`, `roto:{tipo}` — las nueve pruebas offline concuerdan con `casos_del_turno`. Fixture `esquema` autocontenido (no hay `conftest.py` en el repo); `persistencia.aplicar_esquema` y `config.cargar_dotenv` existen; el marcador `neon` está registrado en `pyproject.toml:39` | **DEFECTO — ver Ruling 1** |
| 2 | el paso 1 obliga a LEER los nombres reales antes de escribir, en vez de inventarlos | limpio |
| 3 | `config_de_corrida(canal=...)` existe (`config.py:71`) y acepta `canal`; `MODELO_EVALUADOR = "gpt-5.6-luna"` existe (`config.py:310`); `persistencia.conectar` existe (`persistencia.py:180`); `analizar_pendientes(database_url=)` concuerda con la llamada de `runtime.py` | limpio |
| 4 | `usuario_actual` devuelve `{"usuario","nombre","rol"}` (`runtime.py:1271`), así que `quien["rol"] == "admin"` es válido; el helper de `api.ts` se llama `pedir<T>` y lanza `SesionCaducada` (`api.ts:29,31`) | limpio |
| 5 | el script dobla `casos_del_turno` y las cinco de persistencia con las mismas firmas | limpio |

### Ruling 1 — `recortar_ejemplos` es código muerto con pruebas que aparentan cubrir producción

**Qué encontré.** La tarea 1 exporta `sin_resolver.recortar_ejemplos(actuales, texto, telefono)`
y le escribe dos pruebas offline (`test_los_ejemplos_se_recortan_a_cinco`,
`test_un_ejemplo_vacio_no_se_guarda`). **Nadie la llama.** El recorte a cinco ejemplos lo hace
`persistencia.registrar_caso` **dentro del SQL** del `ON CONFLICT DO UPDATE`, y el plan
justifica eso a propósito: entre un SELECT y un UPDATE en Python cabe el turno de otro
paciente. Las dos pruebas ejercitan una implementación gemela que no se despliega.

**Ruling: se elimina `recortar_ejemplos` y sus dos pruebas offline.** Se conservan
`MAX_EJEMPLOS` (lo usa el SQL de `registrar_caso`) y `sin_telefonos` (lo usan `casos_recientes`
y `casos_sin_informe`). La regla de los cinco ejemplos y la de «se va el más viejo» quedan
cubiertas por `test_siete_turnos_iguales_dejan_una_fila_con_contador_siete`, que prueba el
código que de verdad corre.

**Por qué.** Una prueba verde sobre una función que nadie invoca es peor que ninguna prueba:
quien mañana cambie `recortar_ejemplos` creerá que cambió el comportamiento de producción y la
suite se lo confirmará. Y las dos implementaciones pueden divergir sin que nada lo cace.

**Qué cuesta si me equivoco.** La regla de los cinco ejemplos deja de tener cobertura en
`uv run pytest -q` a secas y solo se comprueba con `MAXICARE_PRUEBAS_NEON=1`. Quien corra la
suite offline no recibe señal sobre ella. Se revierte añadiendo las dos pruebas de vuelta.

---

## Progreso

### Tarea 1 — «El caso existe y se guarda»

BASE `28a61a5` · implementador `aef721c07fe730c17` (sonnet) · revisor opus.

- `Task 1: implementado — commit 48f3f83, 10/10 offline + 8/8 neon.`
- Observación verificada por el controlador: `tests/test_herramientas.py` tiene **11 fallas
  que ya existen en `main`** (`28a61a5`), ajenas a este plan. Y `main` ya arroja **21
  warnings** en la suite: esta tarea no añadió ninguno. El ⚠️ del revisor sobre los warnings
  queda resuelto: no es un hueco.
- El ⚠️ sobre que el recorte a cinco solo queda cubierto bajo `-m neon` es el coste
  declarado del Ruling 1, no un defecto. Resuelto.
- Desviación del brief que el implementador reportó y el revisor verificó: el SQL del brief
  usaba `json || json`, que no existe en Postgres. Se corrigió casteando a `jsonb` **solo
  para la concatenación**, columna intacta como `TEXT`. Comprobado por grep sobre todo el
  repo: cero columnas `JSONB`, cero adaptadores `Json(...)`. La restricción global se
  respeta.

### Ruling 2 — el borrado de `/clearstate` no va donde el plan dice, y tocar donde va rompe una prueba viva

**Qué encontré** (leyendo el código real antes de despachar la tarea 2). El plan ordena poner
`persistencia.olvidar_ejemplos_de(conn, telefono)` «en el bloque de `/clearstate` de
`atencion.py`, antes del `DELETE FROM conversaciones`». Ese bloque **no existe en
`atencion.py`**: el purgado vive en `persistencia.borrar_rastro` (`persistencia.py:2267`), lo
llama `reseteo.resetear` (`reseteo.py:229`), y va entero **en una transacción con
`rollback`** porque «un borrado a medias es peor que ninguno».

Y hay una trampa que el plan no vio: `borrar_rastro` devuelve un `dict` de conteos, y
`tests/test_reseteo.py:405` saca las tablas **de la función de verdad** y afirma que todas
tienen etiqueta en `reseteo.ETIQUETAS_DE_TABLA`, más que ningún nombre de tabla con guion
bajo salga al chat del paciente. Añadir una clave sin etiqueta rompe esa prueba **y** le
manda «1 en casos_sin_resolver» al paciente por WhatsApp.

**Ruling:**
1. El olvido de las frases va **dentro de `persistencia.borrar_rastro`**, como un
   `cur.execute` más del mismo bloque de cursor, de modo que caiga en la misma transacción
   que todo lo demás. Una llamada aparte desde `atencion.py` sería una segunda transacción
   que puede triunfar mientras la purga falla.
2. El SQL vive en **un solo sitio**: una constante de módulo que usan tanto `borrar_rastro`
   como `olvidar_ejemplos_de`. Duplicarlo literalmente es lo que la rúbrica de revisión
   llama defecto, y aquí además haría que las dos copias pudieran divergir.
3. `olvidar_ejemplos_de` sobrevive como función suelta: la usa el script de la tarea 5.
4. La clave nueva de `borradas` necesita su entrada en `reseteo.ETIQUETAS_DE_TABLA`, en
   español de chat y sin guion bajo — habla de FRASES, no de casos: el contador del caso no
   baja (ese es el punto del spec).
5. El conteo tiene que ser exacto, no inflado. Deja de ser un número de log y pasa a ser un
   número que el paciente lee en su WhatsApp. Esto **asciende** el hallazgo Menor del revisor
   de la tarea 1 sobre `cur.rowcount` con teléfonos que son prefijo de otros.

**Por qué.** La atomicidad de `borrar_rastro` está documentada como no negociable en su
propio docstring, y `/clearstate` es irreversible: no puede quedar a medias.

**Qué cuesta si me equivoco.** Toco una función central del reseteo, que tiene pruebas
offline y de Neon (`test_reseteo.py`, `test_reseteo_neon.py`) y un doble de conexión que
registra SQL. Si el SQL nuevo no le gusta a ese doble, la tarea 2 arrastra arreglos de
`test_reseteo.py`. Se revierte quitando el `cur.execute` y la etiqueta.

### Ruling 3 — la razón que el plan da para el orden del borrado es falsa (sin consecuencia)

El plan dice que el olvido va antes del `DELETE FROM conversaciones` «porque después ya no
hay de dónde saber qué frases eran de este teléfono», copiando el no negociable 9. **Para esta
tabla no es cierto:** el teléfono se guarda dentro del propio JSON de `ejemplos`, no se deriva
de `conversaciones`. El orden dentro de la transacción da igual. **Ruling:** se mantiene la
posición (antes del `DELETE`) por consistencia con el resto del bloque, pero el comentario
que la justifica **no** se copia del plan: diría una falsedad. La razón verdadera es la
transacción única.

**Qué cuesta si me equivoco:** nada funcional. Es un comentario.
- `Task 1: fix round 1/5 (5 atendidos, 0 abiertos — rollback en las cinco funciones + prueba de
  regresion de conexion envenenada; CheckViolation estrecho; TIPOS eliminado; dos docstrings al
  dia; PENDIENTE literal y ORDER BY en olvidar_ejemplos_de; commits 48f3f83..a7bba9b)`
- `Task 1: complete (commits 28a61a5..a7bba9b, review clean)` — 10/10 offline, 9/9 neon.
- Menor diferido al repaso final: `olvidar_ejemplos_de` no tiene prueba que verifique el orden
  de los ejemplos con MAS de uno restante (el unico test deja uno solo, donde el orden es
  indistinguible). El SQL es correcto; falta la red.

### Tarea 2 — «La captura»

BASE `a7bba9b` · implementador `a59ad8ffa708fd987` (opus) · revisor opus.

- `Task 2: implementado — commit db96110, DONE_WITH_CONCERNS. 781 pasan (+21 nuevas), la misma
  linea base de 11 fallas y 21 warnings; 122 neon; los nueve scripts que no gastan, OK.`
- Error de nombre del plan que el implementador corrigio: la clase del turno es
  **`DatosDelTurno`**, no `TurnoEnCurso`. `TurnoEnCurso` no existe en el codigo.
- La revision verifico y descarto las dos preocupaciones del implementador: `borrar_rastro(conn, "")`
  es inalcanzable (los dos llamadores, `runtime.py:989` y `:1457`, nunca pasan vacio), y el
  tamano de la frase no es problema de fila sino de coste del nano y de legibilidad -- se
  arregla junto con el hallazgo de la frase.
- Revision: **Necesita arreglos**. 2 Importantes, 4 Menores.

### Ruling 4 — el volcado se queda dentro del candado del telefono

**Qué encontré.** Le dije al implementador «nada nuevo dentro del candado», parafraseando el
no negociable 3. Eso fue impreciso mío: el no negociable 3 manda que el candado vaya por
TELÉFONO y que `_leer_estado` esté DENTRO — no prohíbe trabajo ahí. Comprobé
`atencion.py:895-905`: **todo `_anotar_resultado` ya corría dentro del candado** antes de este
cambio, con sus `marcar_respondido` y su `tocar_conversacion`.

**Ruling: el volcado se queda donde está.** Sacarlo exigiría reestructurar el flujo de
`responder` para una ganancia marginal: el candado solo serializa contra otro mensaje **del
mismo número**, la lista de casos normalmente viene vacía, y cuando corre el paciente ya tiene
su respuesta enviada. El implementador documentó el hecho en `atencion.py:414-418` en vez de
copiar el comentario falso del brief («nada de esto está dentro del candado»), y ese comentario
se queda.

**Qué cuesta si me equivoco.** Un turno con muchos casos alarga unos milisegundos el candado de
ESE teléfono. Se revierte moviendo el volcado a una tarea posterior al `async with candado`.

### Ruling 5 — una historia, una tarjeta: el tripwire de entrada no abre tres casos

**Qué encontré** (hallazgo del revisor, ampliado por mí). Una inyección que dispara un tripwire
de entrada deja HOY tres filas para un solo hecho: `GUARDRAIL:X`, `ROTO:roto:tripwire de
entrada` (porque `atencion.py:1134` pasa `motivo=fallo` y `fallo` arrastra el texto que
`conversacion.py:370,399` ponen para los tripwires) y `HUMANO:dato_faltante`. Es exactamente lo
que el comentario de `sin_resolver.py:107-110` dice querer evitar.

**Ruling, en dos partes:** (a) un motivo que describe un tripwire **no es un `ROTO`** — el
guardrail ya cuenta esa historia, igual que `relevo:` no es un fallo; (b) si el turno produjo un
caso `GUARDRAIL`, el escalamiento se cuelga de él con `escalo=1` en vez de abrir un `HUMANO`
aparte, que es la misma regla de no duplicar que ya aplica al hueco de conocimiento.

**Qué cuesta si me equivoco.** Un fallo técnico de verdad cuyo motivo empezara por la palabra
«tripwire» dejaría de contarse como `ROTO`. Hoy los únicos textos que empiezan así los escribe
`conversacion.py` para los tripwires, así que el riesgo es que un texto futuro colisione. Se
revierte quitando el predicado.

### Ruling 6 — el caso `GUARDRAIL` guarda la frase del paciente, como los otros tres

**Qué encontré.** El implementador dejó el `GUARDRAIL` sin `ejemplo` al hacer que absorbiera el
escalamiento (Ruling 5), y lo reportó como preocupación: «no cambiar el contrato de un tipo al
pasar, y la frase de una inyección no es un ejemplo útil».

**Ruling: lleva la frase.** El spec `:382` fija la entrada del analista nano en «instrucciones +
huella + 5 ejemplos». Un `GUARDRAIL` con cero ejemplos le deja al nano un nombre de tripwire y
un contador: no puede escribir «qué pasó» ni «recomiendo». Y el `GUARDRAIL` es la señal que hoy
se pierde entera y por la que existe toda la funcionalidad — la peor para dejar muda. El spec
`:195` sostiene lo contrario del argumento del implementador: el valor está en notar que «cinco
de los siete ejemplos preguntan por la cuota mensual».

Su objeción sobre las inyecciones es parcialmente cierta pero no decide: los tripwires
frecuentes (`sin_cifra_no_documentada`, `sin_lectura_clinica`) no son inyecciones y ahí la frase
ES el dato; para una inyección, ver qué intenta la gente le sirve al desarrollador; y la pantalla
vive tras autenticación con la frase ya recortada a 280.

**Qué cuesta si me equivoco.** El texto de un intento de inyección aparece en la pantalla que
MaxiCare también ve. Se revierte quitando `ejemplo=frase` de esa rama.
- `Task 2: fix round 1/5 (6 atendidos, 0 abiertos — dedup por huella de huecos Y de tripwires;
  frase_para_el_informe con los textos reales del grupo y MAX_FRASE=280; PREFIJO_TRIPWIRE filtra
  el ROTO y el escalamiento se cuelga del GUARDRAIL; conteo por elementos en dos sentencias; try
  por caso; y el GUARDRAIL guarda la frase (Ruling 6); commits db96110..b4bfe17)`
- `Task 2: complete (commits a7bba9b..b4bfe17, review clean)` — 797 offline (linea base de 11
  fallas y 21 warnings intacta), 123 neon, +36 pruebas nuevas en la tarea. Los nueve scripts que
  no gastan, verdes.
- El revisor verifico el argumento del implementador de no re-correr los scripts en el segundo
  commit: se sostiene, ningun script de `scripts/` referencia `sin_resolver` ni `casos_del_turno`.
- Menor diferido al repaso final: no hay prueba explicita de que DOS tripwires distintos en un
  turno sigan siendo dos tarjetas. La logica es correcta por inspeccion; falta la red.

### Tarea 3 — «El analista y su tarea de fondo»

BASE `b4bfe17` · implementador `ac788020e79dad8e3` (sonnet) · revisor sonnet.

- `Task 3: implementado — commit c2f36af, 4/4 pruebas nuevas, 801 pasan, import runtime OK.`
- Los warnings suben de 21 a 25. **Verificado por el controlador**: son todos la misma categoria
  preexistente (`on_event is deprecated`), repartida en 13 lineas de `runtime.py`, y la causan
  los dos `@app.on_event` nuevos que el propio brief exige copiar. **Ninguna categoria nueva.**
  No es un hallazgo. (El repo entero usa `on_event` y no `lifespan`: es deliberado.)
- Revision: **Necesita arreglos.** 2 Importantes, 2 Menores.
- El revisor juzgo la preocupacion del implementador sobre la cifra alucinada y la declaro
  **aceptable, no bloqueante**: los tres campos son prosa libre (no cabe `Literal`), la unica
  capa es el prompt con su propia prueba, y este informe lo lee un humano en un panel, no un
  paciente. **Diferido al repaso final**, y hay que decirselo al usuario: una segunda capa
  (guardrail de SALIDA sobre el informe, al estilo de `sin_cifra_no_documentada`) seria defensa
  en profundidad razonable y esta fuera del alcance de este plan.
- Hallazgo del revisor que vale la pena recordar: el brief ordenaba I/O de psycopg **sincrono
  dentro de una corrutina**, con un solo worker sirviendo WhatsApp. `seguimientos.despachar`
  ya lo tenia resuelto con `asyncio.to_thread` hasta el `connect` y el `close`. Defecto del
  plan, no del implementador.
- `Task 3: fix round 1/5 (4 atendidos, 0 abiertos — to_thread en connect/queries/close como
  seguimientos.despachar, con Runner.run fuera del hilo; dos pruebas del interruptor en ambas
  direcciones; analista.py en MODULOS_SIN_TRANSPORTE; la regla del tracing corregida de tres a
  cuatro consumidores; commits c2f36af..5df2004)`
- `Task 3: complete (commits b4bfe17..5df2004, review clean)` — 805 pasan, import runtime OK.

### Tarea 4 — «El endpoint y la pantalla»

BASE `5df2004` · implementador `a06a87ec27a4fa7b3` (sonnet) · revisor sonnet.

- `Task 4: implementado — commit 4abcd8f, 809 pasan, npm run build limpio (tsc --noEmit incluido).`
- Comprobado por el controlador: `web/src/pantallas/SinResolver.tsx` tiene **cero bytes NUL y cero
  caracteres Cc/Cf/Mn**. La trampa del `\uXXXX` de la herramienta `Write` no mordio.
- Revision: **Aprobada**, sin Criticos ni Importantes. El revisor verifico uno por uno los cinco
  riesgos: cero botones y cero `onClick` mutantes; el endpoint en `:1656` contra la comodin en
  `:2022`; el parseo de `huella` aguanta las cuatro formas; el telefono se filtra antes de que
  exista el diccionario; `es_admin` solo del servidor.
- **⚠️ Hueco que NO cierra nadie en esta sesion:** nadie pudo abrir la pantalla en un navegador,
  ni confirmar como se ve a ~400px de ancho. Hay que decirselo al usuario, que si puede.

### Ruling 7 — manda la instrucción del brief, no su código de ejemplo

**Qué encontré** (hallazgo Menor del revisor, ascendido por mí). `SinResolver.tsx` usa utilidades
Tailwind genéricas (`opacity-70`, `text-red-700`, `border`), mientras que las otras cinco
pantallas del panel usan todas el mismo patrón: la constante `SP = "'Space Grotesk', sans-serif"`,
colores hex explícitos y cabecera con fondo blanco separado. El brief **se contradecía**: instruía
leer `Tratamientos.tsx:1-80` «para copiar las clases que usa el proyecto», y el código que él mismo
traía no las usaba. El implementador siguió el código, que era lo razonable.

**Ruling: manda la instrucción.** Se alinea el estilo con el resto del panel, solo el estilo —
nada de estructura, lógica, `<details>`, parseo ni estados borde.

**Por qué.** Esta pantalla la ve MaxiCare al lado de las otras cinco. Una que se siente «de otro
sistema» le resta credibilidad al panel entero, y ese panel es el producto que justifica la
mensualidad del cliente.

**Qué cuesta si me equivoco.** Un archivo de estilo tocado en una tarea ya aprobada, con el
riesgo de introducir un error de tipos que `npm run build` cazaría. Se revierte con `git revert`
del commit de estilo.

## Menores diferidos — para que los triage el repaso final de toda la rama

1. **Tarea 1** — `olvidar_ejemplos_de` / `borrar_rastro` no tienen prueba que verifique el ORDEN
   de los ejemplos cuando queda MAS de uno tras el borrado. El unico test deja uno solo, donde
   el orden es indistinguible. El SQL lleva su `ORDER BY` y es correcto; falta la red.
2. **Tarea 2** — no hay prueba explicita de que DOS tripwires DISTINTOS en un mismo turno sigan
   siendo dos tarjetas. El dedup usa `dict.fromkeys`, asi que por inspeccion es correcto; falta
   la red.
3. **Tarea 3** — la unica defensa contra que el modelo nano invente una cifra, un precio o un
   protocolo es el TEXTO de las instrucciones del agente. No hay guardrail de SALIDA sobre el
   informe, al estilo de `sin_cifra_no_documentada`. Dos revisores lo juzgaron aceptable (los
   tres campos son prosa libre, no cabe un `Literal`, y el informe lo lee un humano en un panel
   y no un paciente). **Es una decision de producto para el usuario**, no un defecto de
   implementacion.
4. **Tarea 4** — ⚠️ **nadie abrio la pantalla en un navegador**, ni comprobo como se ve a ~400px.
   Ni los implementadores ni los revisores ni el controlador tienen esa herramienta en esta
   sesion. `npm run build` (con `tsc --noEmit`) sale limpio y el JSX se trazo a mano contra los
   tres estados borde, pero **la comprobacion visual sigue abierta y solo la puede cerrar el
   usuario**.
5. **Deuda del repo, ajena a este plan:** `main` llega con 11 pruebas rotas en
   `tests/test_herramientas.py` y 25 warnings de `on_event is deprecated`. Fuera de alcance, pero
   hay que decirselo al usuario.
6. **Pendiente del controlador:** el spec y el plan (`docs/superpowers/specs/...` y
   `docs/superpowers/plans/...`) siguen sin commitear. Van al final, cuando la tarea 5 suelte el
   indice. (`docs/cliente/` y `scripts/probar_plantilla.py` son de antes y NO son de este plan.)
- `Task 4: fix round 1/5 (1 atendido, 0 abiertos — SinResolver adopta SP y la paleta hex del
  panel; el revisor comparo los tres archivos lado a lado y coinciden token por token; cero
  cambios de comportamiento, cero botones nuevos, los tres estados borde intactos y el detalle
  tecnico sigue bajo esAdmin; commits 4abcd8f..241fd85)`
- `Task 4: complete (commits 5df2004..241fd85, review clean)` — 809 pasan, `npm run build` limpio.

### Tarea 5 — «El entregable verificable»

BASE `241fd85` · implementador `a80badcfe5648df02` (sonnet) · revisor sonnet.

- `Task 5: implementado — commit e755bd8, script con 8 comprobaciones TODO OK, sin gastar un
  token; suite y neon en linea base; public verificado intacto.`
- Revision: **Necesita arreglos.** 3 Importantes, 2 Menores, todos acotados.

### Ruling 8 — la comprobación 8 llama al camino de verdad, no a su gemela suelta

**Qué encontré** (hallazgo del revisor, ampliado por mí). El script llamaba a
`persistencia.olvidar_ejemplos_de` y anunciaba `OK 8. /clearstate borra la frase...`, pero
`persistencia.py:2619-2622` dice literal que **`/clearstate` NO pasa por ahí**: corre el mismo SQL
desde dentro de `borrar_rastro`, y la función suelta sobrevive solo para mantenimiento. El
entregable afirmaba probar algo que no probaba.

**Ruling: no se renombra el mensaje — se llama a `persistencia.borrar_rastro`, el camino real.**
Y se le añaden dos aserciones que hoy nadie comprueba fuera de `pytest`: que la clave nueva
aparece en el `dict` que devuelve `borrar_rastro`, y que tiene etiqueta en
`reseteo.ETIQUETAS_DE_TABLA` **sin guion bajo**.

**Por qué.** Este script existe precisamente para cazar lo que `pytest -q` no caza, y el sitio de
llamada nuevo dentro de `borrar_rastro` es esa clase de cosa. La segunda aserción cubre la trampa
que casi muerde en la tarea 2: sin etiqueta, el paciente recibe «1 en casos_sin_resolver» por
WhatsApp.

**Qué cuesta si me equivoco.** La comprobación 8 pasa a depender de `reseteo`, que el script no
importaba. Si eso arrastra un import pesado o un ciclo, hay que volver a la version anterior con
el mensaje corregido.
- `Task 5: fix round 1/5 (5 atendidos, 0 abiertos — esquema propio pruebas_sin_resolver; la
  comprobacion 8 sobre borrar_rastro con las dos aserciones nuevas; bullet en
  scripts-entregables.md; comentario de la 7 con su alcance real; el no negociable 22 nombrando
  el dano; commits e755bd8..403102c)`
- `Task 5: complete (commits 241fd85..403102c, review clean)` — script TODO OK en solitario, la
  colision de esquema desaparecio, `public` verificado intacto de nuevo.
- El revisor evaluo y descarto el riesgo mas grave del arreglo: `borrar_rastro` toca NUEVE tablas,
  pero el aislamiento sigue siendo el mismo `search_path` de siempre y no hay ruta nueva hacia
  `public`. Y el import de `reseteo` no arrastra nada que llame al modelo.

## Las cinco tareas, completas

## Repaso final de toda la rama (28a61a5..c6edcff, 12 commits, revisor opus)

**Veredicto: Con arreglos.** Cero Criticos. El revisor verifico camino por camino que las tres
propiedades del diseno se sostienen de verdad: el turno del paciente es identico con esto
encendido o apagado; `tocar_conversacion` y `marcar_fallo_respuesta` hacen `commit()` propio, asi
que el `rollback` de un caso fallido NO puede deshacer una escritura clinica; y la limpieza vive
en SQL, no en la pantalla.

Una oleada UNICA de arreglo despachada (opus) con 5 Importantes + 10 Menores.

### Ruling 9 — `FALTA_DATO` se mide ANTES del respaldo de la tool

**Qué encontró el revisor.** `herramientas.py:429-450` reasigna `texto` con el respaldo por
tratamiento y DESPUES calcula `hubo_dato` sobre el texto reasignado. Como los doce tratamientos
de `datos/base_conocimiento.json` tienen entre 3 y 8 conceptos cada uno, un hueco de CONCEPTO es
invisible: `falta_dato:ortodoncia:cuota_mensual` **no puede existir nunca**. El ejemplo bandera
del spec —«Falta el precio de ORTODONCIA, 7 veces»— es inalcanzable, y el matiz que el spec llama
la mitad del valor del informe («5 de los 7 preguntan por la CUOTA MENSUAL») es justo un hueco de
concepto. **Es un hueco del SPEC, no un error de implementacion:** §3 define `FALTA_DATO` sin
contemplar el respaldo, que es anterior a esta rama.

**Ruling: se mide sobre la PRIMERA consulta, la exacta.** El respaldo no cambia: lo que el modelo
ve sigue idéntico, que es lo que conserva la propiedad de observador.

**Efecto lateral aceptado:** tambien se abriran casos por conceptos que el modelo se invente. La
ventana de 30 dias los hunde sola, y un modelo que inventa el mismo concepto una y otra vez ES
una senal que vale la pena ver. Sin lista blanca.

**Qué cuesta si me equivoco.** Mas filas de las esperadas la primera semana. Se revierte con una
linea. Lo contrario —dejarlo— entrega una funcionalidad cuya senal principal no suena.

### Ruling 10 — `es_admin`: se corrigen los comentarios, no el payload

Tres textos afirman que `es_admin` decide si el navegador recibe el detalle tecnico. No es cierto:
la `huella` viaja a todos los roles y solo la pantalla la esconde. **Ruling: corregir los textos.**
Quitar la `huella` a los no-admin dejaria a la clinica sin titulos, porque `titulo()` la parsea
para componer el nombre legible. Lo que `es_admin` gatea de verdad es la VISUALIZACION cruda, y
eso esta bien. **Qué cuesta si me equivoco:** un usuario de la clinica con sesion puede leer la
huella cruda en el JSON. Riesgo bajo.

### Ruling 11 — la tarjeta `ROTO` también guarda la frase

Misma razon que el Ruling 6 para `GUARDRAIL`: sin ejemplos el analista nano recibe «(ninguno)» y
escribe sobre una huella a secas. Para un `ROTO`, la frase del paciente es justo lo que le dice al
desarrollador que estaba pasando cuando revento, y ya esta calculada y recortada.

### Ruling 12 — control determinista contra una cifra inventada por el nano

Acepto la «mitad barata» que propuso el revisor: pasar los tres campos del informe por
`guardrails.cifras_de` antes de `guardar_informe` y, si aparece una cifra, no guardar. Mismo
mecanismo que sostiene `sin_cifra_no_documentada`, sin una sola llamada al modelo.
**Comprobado por mi que no rompe informes legitimos:** `_CIFRA` solo caza cifras con forma de
DINERO. `«Doce personas preguntaron»` -> vacio; `«12 pacientes en 7 dias»` -> vacio; `«se molesto
al doctor 4 de 12 veces»` -> vacio; `«el precio es $3.000.000»` -> `{'3000000'}`.
**Qué cuesta si me equivoco:** un informe legitimo que mencione una cifra de dinero no se guarda y
se reintenta para siempre — por eso va atado al arreglo del atasco de la cola.

### Ruling 13 — «fase 6D» es un valor plausible y sale

`CLAUDE.md:53` y el plan etiquetan el entregable como fase 6D. El spec §12 deja abierto si esto
entra como fase propia, y **`plan-agentes.json` no tiene ninguna 6D**. La regla dura 3 prohibe
exactamente eso. **Ruling: no se inventa un numero de fase ni se toca `plan-agentes.json`** — la
celda queda sin fase inventada, marcada como pendiente de asignar. **Es una decision del usuario**,
no mia: solo impido que quede escrita una mentira mientras la toma.

## Cierre — verificacion final del controlador

- `Oleada final: 15 atendidos, 0 abiertos (commits c6edcff..856437b). Re-revision opus: listo
  para fusionar.`
- **CORRECCION de mi propia linea base.** Dije a todos los implementadores «11 fallas
  preexistentes». Son **12**. Medi la linea base a las 11:26 corriendo SOLO
  `tests/test_herramientas.py`, y en esa corrida
  `test_reprogramar_autoriza_LAS_DOS_horas_la_vieja_y_la_nueva` paso. A las 14:18 falla 5/5.
  **No es intermitente ni es una regresion: el test lleva una hora fija dentro.** Espera una
  cita a las 14:00 de hoy y afirma que la hora vieja sale en el texto; pasadas las 14:00 el
  codigo responde «esa hora ya paso» --que es correcto-- y el assert cae. Es una **bomba de
  tiempo** en `main`, ajena a este plan.
- Numeros finales, medidos por mi:
  - `main`  → 12 fallas, **753** pasan, 21 warnings
  - rama    → 12 fallas, **827** pasan, 25 warnings  (+74 pruebas, mismas 12 fallas)
  - Neon    → **125 pasan**, limpio. Esto valida contra Postgres de verdad el `WITH ORDINALITY`
    que la re-revision solo pudo comprobar leyendo.
  - `npm run build` verde · `import runtime` OK · `probar_sin_resolver.py` TODO OK.
- **La rama no introduce ni una sola regresion.**

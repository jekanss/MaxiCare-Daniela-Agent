# web/ — la interfaz del panel

React + Vite + Tailwind, aparte del paquete de Python. Sale de un archivo de Figma Make;
`web/src/marca/` y los tokens `--color-sp-*` de `index.css` vienen de allí y **no se
renombran**, o la siguiente pantalla que llegue de Figma deja de encajar.

```
cd web && npm install && npm run build     # deja web/dist, que es lo que sirve runtime.py
cd web && npm run dev                      # :5173 con proxy a :8080 — hacen falta LOS DOS
uv run uvicorn maxicare_daniela.runtime:app --port 8080
```

- **LA TRAMPA QUE MÁS VA A COSTAR: el panel escribe en `public`, el chat de pruebas lee de
  `pruebas_web`.** Son dos bases distintas. Editar un precio en la pantalla y preguntarle a
  Daniela en la pestaña Pruebas **no** sirve para comprobar el cambio: ella cita el valor de
  la semilla y parece un fallo. Ya hizo que el entregable de esa fase se escribiera mal la
  primera vez. Para comprobar el camino completo: `probar_panel.py --chat`, que prueba cada
  mitad por su lado.
- **El chat de pruebas escribe en el esquema `pruebas_web`, nunca en `public`.** No es
  `pruebas`: ese lo BORRAN `probar_tools.py` y `probar_agentes.py` al terminar.
- **En `web/src/pantallas/Tratamientos.tsx`, el `useCallback` de `recargar` tiene
  dependencias vacías a propósito, y `alCaducarSesion` se consume por una `ref`.** Meter esa
  prop en las dependencias —lo que pediría cualquier regla de hooks— deja la pantalla
  releyendo Neon en bucle, porque `App.tsx` la pasa como una flecha nueva en cada render. No
  hay `eslint-plugin-react-hooks` ni arnés de pruebas de frontend que lo atrape: se vería
  como una pantalla lenta y una factura rara.
- **El panel «Últimos cambios» de `Tratamientos.tsx` ya NO enseña las marcas de asistencia**, y
  es a propósito: `runtime.TABLAS_FUERA_DEL_HISTORIAL` deja `citas` fuera de
  `/api/historial`. Esa ventana son las 100 filas más recientes y existe para reconstruir qué
  decía un precio antes y quién lo cambió; con quince citas al día —tres filas por cada
  corrección, porque «Corregir marcación» desmarca primero— serían **todas** marcas de
  asistencia en menos de una semana, y el cambio de precio de la semana pasada dejaría de
  verse en la única pantalla desde la que se puede ver. Las filas siguen en
  `cambios_configuracion`: lo único que se recorta es esta ventana. El motivo largo está en el
  docstring de esa constante.
- **El `Literal` de tratamientos está partido en dos.** `LecturaArchivo` conserva los 14
  escritos a mano —es el muro, y tiene su prueba `test_tratamiento_no_admite_una_frase_clinica`—;
  el vocabulario de negocio vive en la tabla `tratamientos` y lo carga `runtime.py` al
  arrancar. Crear un tratamiento desde la pantalla **no** lo mete en el muro: una
  radiografía suya se clasifica `no_identificado`. Verificado por `probar_panel.py`.
- **`GET /api/agenda` ESCRIBE, y no es un descuido del verbo.** Antes de pintar el día
  reconcilia esas citas contra Google Calendar (no negociable 20): puede **mover** una fila
  de `citas`, **cancelarla**, **soltar su cupo en `reservas` y tomar otro**, y reprogramar su
  recordatorio. Abrir la pantalla de Agenda es hoy el mejor disparador que esa reconciliación
  tiene —el otro es que un paciente pregunte por su cita, y no hay ningún barrido—, así que
  se quiso así. Cuatro consecuencias que hay que tener presentes antes de tocar esta pantalla:
  - **Nada de recargar en bucle.** Un `setInterval` o un `useEffect` mal atado no es una
    pantalla lenta: son escrituras en Neon y llamadas a la API de Google por cada tic.
  - **Lo que corrija se PINTA.** Vuelve en `correcciones`, con la hora vieja dentro. Corregir
    en silencio deja a quien mira viendo una cita saltar de sitio sin explicación — el mismo
    fallo que hizo escalar a Daniela el 14/09/2026, por la otra puerta.
  - **Pero solo hasta `herramientas.DIAS_HACIA_ATRAS_AL_SINCRONIZAR` hacia atrás**
    (`runtime._fuera_de_la_ventana`), y esa cota no es una optimización. Es la misma
    **constante** que usa Daniela pero no exactamente la misma ventana: el núcleo corta por
    INSTANTE y el panel por DÍA, así que en el día frontera el panel es hasta 24 h más ancho
    — siempre del lado permisivo, nunca del restrictivo. Sin ella, abrir en la
    Agenda un día cuyos eventos el doctor ya limpió de su Calendar —orden, no cancelaciones—
    daba esas citas por canceladas en Neon, soltaba sus cupos y dejaba el PATCH respondiendo
    400: **inmarcables para siempre**, las ya marcadas con `asistio = true` sobre una fila
    `cancelada`, sin fila en `cambios_configuracion` y sin un error en ningún log. La acción
    que lo disparaba era MIRAR, y el botón «Ver ese día» de la lista de pendientes lleva
    justo ahí. No contradice el no negociable 20: el camino de Daniela nunca miró más atrás
    de esa misma constante, y el panel lo había ampliado a infinito sin decirlo. El día viejo
    se pinta con lo que dice Neon y `calendario_disponible: false`.
  - **Y `calendario_disponible: false` tiene DOS motivos que no se parecen, así que viaja
    `motivo_sin_calendario` al lado.** `fuera_de_ventana` es rutina —el día es viejo— y la
    pantalla pinta una franja NEUTRA; `no_disponible` es avería —Google no contestó— y pinta
    la ROJA. Antes las dos daban la roja, y como `DIAS_SIN_MARCAR` (7) es más ancho que la
    ventana (2), **cinco de los siete días a los que lleva «Ver ese día» disparaban la alarma
    por un motivo inocuo**: esa franja es la única señal de que se está mirando Neon sin
    contrastar —una fila desfasada pone a un paciente en la hora equivocada—, y una alarma
    diaria por nada deja de leerse el día que significa algo. **Los dos literales los decide
    `runtime.py`** (`MOTIVO_FUERA_DE_VENTANA` / `MOTIVO_CALENDARIO_NO_DISPONIBLE`), el único
    sitio de TypeScript donde se escriben es `web/src/api.ts`, y que las dos copias no se
    separen en silencio lo ata `tests/test_agenda_pantalla.py`. La invariante:
    `motivo_sin_calendario` es `null` si y solo si `calendario_disponible` es `true`; un
    motivo que la pantalla no conozca sale por la franja roja, que es el lado barato de
    equivocarse.
  - **Ese GET que escribe sigue sin candado, y ya NO deja dos recordatorios vivos para la
    misma cita** (arreglado el 21/09/2026). El candado no hizo falta: lo que se quitó fue la
    dependencia del reloj. Era el daño del no negociable 21 entrando por una puerta nueva
    —hasta la fase 8, la reconciliación solo corría desde `atencion.py`, serializada por el
    candado por teléfono—, y el mecanismo entero se conserva aquí porque es lo que hay que
    volver a mirar el día que alguien toque esa clave:
    - **Lo que fallaba:** la clave de idempotencia del recordatorio llevaba
      `ahora.isoformat()` dentro (`herramientas.py`, en `_mover_porque_la_movieron`), y **cada
      petición calcula el suyo con precisión de microsegundos**, así que las dos claves eran
      distintas y el `ON CONFLICT (clave_idempotencia)` no las juntaba. Bajo READ COMMITTED
      los dos INSERT ocurren antes de que ninguno de los dos
      `anular_seguimientos_de_cita(excepto_clave=…)` corra, ninguna transacción ve la fila sin
      confirmar de la otra, y al commit quedaban dos filas vivas para el mismo `cita_id` —el
      único UNIQUE de `seguimientos` es `clave_idempotencia`, no `cita_id`—. Las siete guardas
      del despachador decían que sí a las dos, y el paciente recibía el mismo WhatsApp dos
      veces.
    - **El cupo NO se duplicaba**, y ESO es justo lo que lo arregla: su clave es
      `calendar:{cita_id}:{inicio}`, determinista, y el UNIQUE de `reservas` lo resuelve en
      Postgres. El tercer componente de la clave del recordatorio es ahora **la `reserva_id`**,
      así que dos peticiones simultáneas recuperan la misma fila de `reservas` —`tomar_cupo`
      la busca por clave ANTES de intentar insertar— y escriben la misma clave: la unicidad
      pasa a decidirla Postgres y no el orden de llegada. Y un movimiento de verdad sigue
      teniendo su recordatorio, que es lo que `ahora` protegía: en un A → B → A → B hecho a
      mano, `liberar_cupo` borra la reserva de B al salir, así que volver a B toma una nueva
      con id nuevo. Sin cupo —destino lleno, la cita se mueve igual— se cae al reloj truncado
      al minuto.
    - **`web/src/main.tsx` monta con `<React.StrictMode>`**, así que en `npm run dev` React
      invoca el efecto de montaje dos veces y salen **dos peticiones del mismo día en
      paralelo en cada apertura de la Agenda**. El `ref pedido` de `Agenda.tsx` descarta la
      respuesta sobrante, pero no impide que la petición se ENVÍE ni que el servidor escriba.
      En la build de producción StrictMode no duplica, así que allí hacen falta dos pestañas
      o dos personas en el mismo día dentro de la ventana de una transacción (~50-100 ms) y
      sobre una cita que el doctor acabe de mover: sigue siendo cierto, y raro. Pero «bajo en
      producción, sistemático en desarrollo» no es lo mismo que «bajo».
    - **Lo que se descartó, y por qué:** el `pg_advisory_xact_lock` sobre el id de la cita.
      No basta solo —el segundo paso ya trae su clave calculada desde fuera de la
      transacción, así que insertaría igual— y exige además una relectura del `inicio` dentro
      de `_mover_porque_la_movieron` que lo convierta en un no-op. Es un lock nuevo, sostenido
      sobre I/O de red, en una escritura cuyo orden (cupo → mover → cascada) ya está
      documentado como delicado. Sacar `ahora` de la clave **a secas** tampoco valía: cambiaba
      un fallo ruidoso —recordatorio repetido, inocuo— por uno silencioso —cita sin ningún
      recordatorio—, porque un A → B → A → B acertaría una clave ya anulada y el
      `ON CONFLICT DO NOTHING` no escribiría nada. Colgar de la reserva es lo que da las dos
      mitades a la vez.
    - **Qué lo sostiene:** `test_dos_peticiones_de_la_agenda_no_dejan_DOS_recordatorios` y
      `test_un_movimiento_NUEVO_al_mismo_destino_si_recupera_su_recordatorio`, en
      `tests/test_herramientas.py`. El primero cuenta **filas y no vivos**, a propósito: en
      secuencia la cascada del segundo paso anula la fila del primero y dejaría un solo vivo
      con el fallo dentro, así que una prueba que mire los vivos da verde sobre el bug. Con
      una sola clave el entrelazado deja de importar. Reproducirlo contra Neon de verdad
      exige la conexión DIRECTA, no el pooler.
- **En la rejilla de la Agenda, la fila de cada hora lleva `minHeight` y NUNCA `height`, y el
  rótulo de la hora va DENTRO de la fila.** Las dos cosas sostienen lo mismo, y se pagaron
  caras: con filas de 96 px fijos, la tarjeta de una cita de 60 minutos —la duración por
  defecto de esta clínica, o sea el caso normal— mide 137 px con sus dos botones, se sale de su
  hora y **la tarjeta de la hora siguiente se pinta encima**. Los botones «Asistió» y «No
  asistió» de toda cita seguida de otra quedaban **físicamente tapados**: en una agenda con
  citas consecutivas, solo la última de la tanda era marcable. Verificado el 20/09/2026 con un
  navegador de verdad (`subtree intercepts pointer events`, treinta segundos de reintentos).
  Tres cosas que no se pueden deshacer sin volver a romperlo:
  - **Acotar el `minHeight` de la tarjeta no arregla nada.** Se intentó. La altura real la
    decide el CONTENIDO; ese número solo es un mínimo.
  - **Encoger los botones tampoco es la salida.** Su tamaño es deliberado y está argumentado
    en el archivo: lo pulsa alguien de pie, con prisa, entre un paciente y el siguiente.
  - **La línea del «ahora» se sitúa en un porcentaje de SU fila**, no multiplicando `ALTO_HORA`
    desde arriba de la rejilla. Esa cuenta vieja daba por hecho que todas las filas miden lo
    mismo: en cuanto una crece, apunta a la hora equivocada.
  **Lo vigila `tests/test_agenda_pantalla.py`, y conviene saber hasta dónde llega.** Es una
  prueba de TEXTO sobre este archivo: caza que alguien reponga una altura fija —en el CSS de
  `style` **y** como clase de Tailwind, que es el dialecto normal del archivo: `h-24`,
  `h-[96px]`— o devuelva la línea del «ahora» a la cuenta vieja, y no puede cazar nada más,
  porque no hay motor de maquetación. Los topes y los mínimos (`max-h-`, `min-h-`) no
  cuentan, y las alturas fijas menores de `h-4` tampoco: un punto o una línea de un pelo no
  envuelven a nadie y no pueden tapar un botón. Para fijar otra hay una lista blanca con el
  motivo escrito al lado. **Un arnés de jsdom sería peor que nada**: `getBoundingClientRect` devuelve
  ceros y pasaría en verde sobre la pantalla rota. El guardián de verdad sigue siendo abrir la
  pantalla con dos citas seguidas de 60 minutos y comprobar que los dos botones se pulsan.
- **La lista de «citas sin marcar» va acotada (`max-h-[40vh]`) y con scroll PROPIO.** Es el
  mismo fallo por la otra puerta: el panel es `shrink-0` dentro de una raíz `overflow-hidden` y
  `citas_sin_marcar` devuelve hasta **50** filas, así que sin cota las últimas quedan recortadas
  **y sin ningún scroll que las alcance** —el de la rejilla es de otro elemento—. Invisibles y
  no marcables.
- **La marca de asistencia no funciona sin la migración 021, y falla ENTERA.** La 007 dejó
  `cambios_configuracion.tabla` cerrado en tres valores y `citas` no estaba; como
  `panel.marcar_asistencia` mete el `UPDATE` y su fila de bitácora en la MISMA transacción,
  el `CheckViolation` del INSERT se lleva por delante también el UPDATE: `citas.asistio`
  queda inescribible desde el panel. **Subir este código a un sitio sin la 021 aplicada deja
  la pantalla de Agenda rota en producción.** `scripts/desplegar.sh` aplica las migraciones
  antes de levantar el contenedor, así que el camino normal lo cubre; el que no lo cubre es
  cualquier otro. Para comprobarlo sin escribir nada:
  `uv run python scripts/inicializar_base.py --solo-verificar`, que desde la fase 8 tiene un
  bloque para la 021 y grita `FALLA` en los dos esquemas. Antes decía OK sobre una base en
  ese estado exacto.
- **Dos personas editando la misma ficha: la segunda pisa a la primera.** No hay bloqueo
  optimista, es deliberado. Lo que lo hace aceptable no es que sea improbable, sino que
  `cambios_configuracion` guarda el valor anterior: una edición pisada es recuperable, no
  perdida. Eso vale para el contenido **y para `aprobado`**, que se registra en su propia
  fila. Si algún día se añade un campo editable a la ficha, tiene que anotarse también, o
  esta frase vuelve a ser mentira para ese campo y el límite deja de ser aceptable.

- **`GET /api/inicio` NO reconcilia, y `GET /api/agenda` SÍ. La asimetría es deliberada.**
  Inicio es la PRIMERA pantalla de cada sesión —el hash vacío cae ahí desde el 22/09/2026—,
  así que una sola llamada a Google en ese camino son escrituras en Neon y peticiones a la
  API de Calendar en cada ingreso al panel, varias veces al día y por cada persona de la
  clínica que entre. Quien quiera el día contrastado abre Agenda, que es donde vive esa
  promesa y donde está escrita. Lo vigilan dos pruebas offline de `tests/test_panel.py`:
  `test_api_inicio_no_toca_el_calendario` —que espía `runtime._calendario`, **no**
  `runtime.calendario`, que no existe— y `test_api_inicio_es_de_solo_lectura`, que corre el
  SQL de verdad contra una conexión que apunta el verbo de cada sentencia.

- **La unidad de la portada es el TELÉFONO, nunca la conversación.** Una conversación caduca
  por inactividad de 24 h (`persistencia.conversacion_viva`), así que una negociación de tres
  días son tres filas en `conversaciones` y una sola persona. Contar filas inflaría el
  denominador de todas las tasas y haría parecer a Daniela peor de lo que es. Por eso
  `panel.resumen_inicio` cuenta `DISTINCT telefono` en los dos primeros números.

- **Las cifras llegan CRUDAS y los porcentajes se calculan en la pantalla.** `llegaron` viaja
  con `marcadas` y `cumplibles` al lado, que es lo que permite escribir «8 de 12 marcadas» en
  vez de un 67 % que no dice sobre cuántas citas se calculó. Y los estados vacíos se
  resuelven ANTES que los llenos, en `tarjetas()` de `Inicio.tsx`: con cero citas cumplidas
  la tarjeta dice «sin citas cumplidas todavía» y no `0 %`, que se lee como un fracaso. Una
  barra de progreso sobre un denominador cero es una barra vacía, y una barra vacía dice
  «fallaste», no «todavía no»: por eso `barra` es `null` y no `0` en ese caso.

- **Las pruebas de la portada miden un DELTA, no un total, y eso no es estilo.** El esquema
  `pruebas` no se borra entre pruebas ni entre corridas, y otros seis archivos de Neon
  (`test_tools_neon`, `test_relevo_neon`, `test_seguimientos_neon`, `test_reseteo_neon`,
  `test_reactivacion_neon`, `test_ingesta_neon`) insertan en `mensajes_entrantes`,
  `conversaciones` y `citas` ahí mismo. `resumen_inicio` cuenta la tabla ENTERA, así que un
  `== 1` mediría la basura acumulada de todo el proyecto y amanecería en rojo sin que nadie
  tocara nada.

- **`titulo()` de `SinResolver.tsx` y `hhmm()` de `Agenda.tsx` están exportados para la
  portada.** No se duplican: dos traducciones de la misma huella se separan en silencio y la
  clínica acabaría leyendo dos nombres para el mismo problema, y dos lecturas del mismo ISO
  ya costaron una agenda corrida entera.

- **El estado de carga y el de error viven en `componentes/Estado.tsx`, y el esqueleto lleva
  la FORMA de cada pantalla.** Antes estaban copiados siete veces: cinco «Cargando…», cinco
  bloques rojos y un solo botón de reintentar en toda la aplicación. Un esqueleto con la
  forma del contenido no es adorno frente a un spinner: evita el salto de la página cuando
  los datos llegan, y en la Agenda —donde la espera contra Google Calendar es de 3 a 5
  segundos— es la diferencia entre esperar y creer que se colgó. Dos cosas que no se pueden
  aflojar: **`Fallo` solo lleva `alReintentar` sobre una LECTURA** (sobre una escritura, ese
  botón promete repetir un guardado que no repite: por eso `Tratamientos` tiene dos estados
  de error y no uno), y **un fallo de refresco con datos ya en pantalla no los borra** —el
  hilo de Conversaciones avisa y deja leer lo que hay—.

- **`MensajeDelHilo.voz` es clínico, no decorativo.** Dice que el paciente no escribió eso:
  lo DIJO, y lo pasó a texto una máquina (migración 027). «El 46» y «el 40» suenan casi
  igual, así que quien lee una frase sobre un síntoma tiene derecho a saber de quién se fía.
  Un audio que no se pudo transcribir llega con `texto: '(nota de voz)'` y `voz: false`, que
  es lo correcto: ahí no hay ninguna máquina de la que desconfiar. Y el doctor se distingue
  de Daniela por FORMA —rótulo arriba y barra lateral— y no por tono: los dos lilas que había
  (`#EDE9FE` contra `#F4F1F9`) no se distinguen en la pantalla de un consultorio.

## Del lado de Python, pero solo importa desde aquí

- Sin `MAXICARE_SECRETO_SESION` el panel se apaga con un 503 y **el webhook sigue vivo**.
  Es deliberado: WhatsApp está en producción y no puede caerse por una variable del panel.
- La ruta comodín que sirve `index.html` va **al final** de `runtime.py`. Antes se tragaría
  `/api`, `/salud` y el webhook.

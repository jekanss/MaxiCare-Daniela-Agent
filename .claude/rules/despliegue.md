---
paths:
  - "scripts/desplegar.sh"
  - "Dockerfile"
  - "docker-compose.yml"
  - ".dockerignore"
  - ".env.ejemplo"
  - "src/maxicare_daniela/runtime.py"
  - "src/maxicare_daniela/config.py"
---

# El despliegue

`bash scripts/desplegar.sh` empaqueta, copia, construye la imagen, **aplica las migraciones
antes de levantar** y espera a que el contenedor esté sano. Traefik sigue mandando los
webhooks de Meta al contenedor viejo hasta que el nuevo responde `/salud`, así que un
despliegue fallido no deja a los doctores sin radiografías.

## Las migraciones no son un paso opcional del script, y la 021 es el ejemplo

**El próximo despliegue lleva la migración 021, y sin ella la pantalla de Agenda sale rota.**
La marca de asistencia escribe `citas.asistio` y su fila de bitácora en la MISMA transacción;
hasta la 021, el CHECK de `cambios_configuracion.tabla` —cerrado por la 007— no admitía
`'citas'`, así que el `CheckViolation` del INSERT arrastra también al UPDATE y la columna
queda **inescribible desde el panel**. Al 20/09/2026 la 021 está aplicada en la base alterna
de desarrollo y **no en la principal**.

`desplegar.sh` lo cubre: corre `inicializar_base.py` en un contenedor de un solo uso *antes*
de levantar el servicio, y ese script aplica y luego **verifica** la 021 en `public` y en
`pruebas_web`, saliendo con código 1 si falta. Lo que no cubre es subir el código por
cualquier otro camino. Para mirar sin escribir:

```
uv run python scripts/inicializar_base.py --solo-verificar
```

Y la lección general, que no es de la 021: **una migración que solo añade tablas se puede
verificar por nombre; una que AMPLÍA un CHECK, no.** Una base sin la 021 tiene todas sus
tablas en su sitio y pasaba por sana — el bloque de verificación tuvo que leer la definición
del constraint. La siguiente migración que cambie una restricción en vez de una tabla necesita
su propio bloque, o `--solo-verificar` volverá a decir OK sobre una base rota.

## Un despliegue no puede matar un turno a media frase

**`stop_grace_period: 120s`, `runtime.SEGUNDOS_PARA_DRENAR = 75`, y el orden importa:** el
segundo tiene que ser MENOR que el primero, o Docker mata el proceso mientras espera y la
espera no sirve de nada.

Por qué existen. Docker da **10 segundos** por defecto entre el SIGTERM y el SIGKILL, y un
turno de Daniela necesita más: 20 s de ventana del búfer, más el modelo, más el retardo
humano. Con ese valor, todo despliegue que pillara a alguien escribiendo le mataba el turno.
Pasó el 13/09/2026 y así se veía en `mensajes_entrantes`:

```
02:50:00  RESPONDIDO     '?'
02:47:22  SIN RESPUESTA  'Sabes alguna cosa de unicornios?'
```

`respondido_en` NULL y `fallo_respuesta` NULL. **Un proceso muerto no escribe su motivo**, y
esos dos NULL juntos son la firma exacta de esa muerte: un fallo de verdad deja motivo, una
respuesta que salió deja fecha. No aparecía en ningún indicador —`sin_entregar` mira el
viaje hacia los doctores, que sí había ocurrido— así que la pérdida era silenciosa.

Tres piezas, y cada una cubre lo que la anterior no puede:

1. **El drenaje** (`_EN_VUELO` + `_esperar_a_que_drenen`). Cubre el apagado ordenado. Cada
   turno se anota al empezar y se borra en un `finally`; el `finally` no es cosmética: un
   `wamid` colgado haría que todos los apagados siguientes esperaran los 75 segundos
   completos a algo que ya no existe.
2. **El barrido de arranque** (`_recoger_lo_que_quedo_sin_responder`). Cubre lo que el
   drenaje no puede: un `kill -9`, un reinicio del VPS, un OOM. **No pasa por `_entregar`**,
   y quien lo "simplifique" reutilizándolo deja el arreglo en nada: `_entregar` empieza por
   `procesar_mensaje`, que deduplica por `wamid`, devolvería `nuevo=False` con razón —el
   mensaje ya se registró y ya se le reenvió al doctor— y se iría sin correr el turno, que es
   justo la mitad que falta.
3. **`sin_responder` en `/salud`**. Convierte una pérdida silenciosa en un número. Lleva
   ventana de 24 h para que vuelva a cero solo: un indicador que nunca baja es un indicador
   que nadie mira.

El riesgo asumido del barrido, dicho para que nadie lo descubra solo: entre que WhatsApp
acepta la respuesta y `marcar_respondido` la escribe hay milisegundos, y un proceso que muera
justo ahí hace que el paciente la reciba dos veces. Se acepta porque el silencio es peor, y
sobre todo porque **las claves de idempotencia impiden lo que de verdad importaría**:
reintentar no crea una segunda cita ni consume un segundo cupo.

**`/clearstate` era el único camino que respondía sin pasar por `atender`**, así que
respondía de verdad y dejaba la fila con `respondido_en` NULL: el barrido le habría pasado a
Daniela el texto «/clearstate» como si fuera un paciente preguntando. Ahora
`_avisar_del_reseteo` lo anota, y el barrido además los descarta por si acaso.

- **Un archivo BORRADO no desaparece del VPS solo, y tumbó un despliegue el 26/09/2026.**
  `tar -xzf` sobre un directorio que ya existe añade y sobrescribe, pero **nunca borra** lo
  que el paquete ya no trae. Al fundir las pantallas de Estado y Configuración se eliminó
  `web/src/pantallas/Pendiente.tsx`; el archivo siguió en el servidor desde el despliegue
  anterior, y `tsc --noEmit` lo compiló dentro de Docker contra un tipo que ya no tenía el
  campo `fase`. El build murió en el VPS con `TS2339` sobre un archivo que **en el
  repositorio no existe**, y el `npm run build` local había pasado limpio minutos antes.
  Ahora el script borra `src migraciones datos scripts web` antes de extraer, y valida el
  paquete con `tar -tzf` **antes** de borrar nada: un `scp` truncado no puede dejar el
  servidor sin código. Era la primera vez que el proyecto borraba un archivo de `web/`; en
  `src/` el mismo defecto sería peor, porque un módulo muerto que nadie importa no rompe
  ningún build y se queda ahí para siempre.
- **`desplegar.sh` empaqueta a mano lo que el Dockerfile copia, y ya se desincronizó una
  vez.** El script es de la fase 2; el Dockerfile creció su etapa de Node en la fase 5 y el
  tar nunca creció con él, así que **todo lo construido entre la fase 2 y la 8 se quedó sin
  desplegar** sin que nadie lo supiera: el primer intento moría en `COPY web/ ./` con un
  error sobre checksums que no nombra el script por ninguna parte. Ahora el script deriva la
  lista del propio Dockerfile y aborta antes de subir nada si falta una ruta. Si alguien
  añade un `COPY`, esa comprobación es lo único que lo atrapa.
- **El `.env` que viaja al VPS no es el local tal cual.** Se le quitan las claves vacías
  —ver «Una variable vacía no es una variable ausente», más abajo— y `MAXICARE_COOKIE_INSEGURA`
  se fija en `0`: en local vale `1` porque `http://localhost` rechaza una cookie `Secure`, y
  en el VPS ese mismo `1` publicaría la cookie de sesión del panel sin esa marca.
- **`probar_webhook.py` ya gasta tokens.** Manda un mensaje firmado de verdad, y desde la
  fase 6A eso despierta a Daniela: un turno completo contra el modelo por cada corrida. No
  le escribe a ningún paciente —el número de prueba no existe— pero no es gratis.
- **El `/salud` desplegado es la forma barata de saber qué versión corre.** Publica la fase,
  la clase de calendario y si Daniela responde. Si dice `CalendarioCaido`, Google no arrancó
  y Daniela no puede agendar aunque todo lo demás funcione.
- **Un solo worker, a propósito**, y el `Dockerfile` explica por qué: hay dos estados en
  memoria del proceso (`runtime._tema_general` y `contratos._VOCABULARIO`, que el panel
  reescribe en caliente). Con dos workers, un tratamiento creado desde la pantalla lo
  conocería solo uno de ellos.

## Una variable vacía no es una variable ausente, y hay DOS puertas

El `.env` trae casi todas las claves presentes y **sin valor**. `cargar_dotenv` no exporta las
vacías y `_opcional` cae al default; eso cubre la puerta local. La otra es el `env_file` de
Docker, que **sí** exporta las vacías y ante el cual ese filtro no llega a correr, porque
dentro del contenedor no hay `.env` que leer.

Ya pasó en producción. Con `OPENAI_BASE_URL=` vacía, el cliente de OpenAI la prefiere sobre su
propio default, arma `base_url=""` y **toda** llamada al modelo muere en `APIConnectionError:
Connection error.` **Se lee como un problema de red y no lo es:** las trazas subían a esa misma
API sin problema, `/salud` decía `configuracion: ok`, y el paciente recibía el mensaje seguro
de `atencion.py` como si Daniela hubiera decidido no saber.

Lo cierran dos sitios, y hay que tocar los dos: `runtime.py` con `descartar_vacias_de_terceros()`
al arrancar, y `desplegar.sh`, que no manda ninguna clave vacía al servidor.

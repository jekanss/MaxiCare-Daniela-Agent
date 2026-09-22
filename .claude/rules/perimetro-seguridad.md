# El perímetro: que un desconocido no pueda quemar el saldo

El número de WhatsApp de una clínica es público — tiene que serlo. Eso significa que la firma
HMAC de Meta, que es lo único que protege el webhook, **no distingue a un paciente de alguien
que quiere gastar el saldo de OpenAI**: los dos entran por la puerta principal y los dos vienen
firmados. No hay nada que vulnerar, así que no hay nada que detectar por la vía de «esto es un
ataque». Lo único que se puede hacer es poner un techo y verlo venir.

Antes de esto no había ninguno de los dos. Búsqueda sobre todo `src/`: cero coincidencias de
`semaphore`, `rate limit`, `throttle` o `cuota`. Y cero de `ctx.usage`, `RunHooks` o cualquier
contador de tokens: para saber cuánto se había gastado había que abrir el dashboard de OpenAI a
mano, así que un ataque de coste y un martes con mucha demanda se veían exactamente igual.

## Lo que este perímetro NO toca, y por qué

Tres cosas quedaron fuera a propósito. Si alguien las «completa» sin leer esto, rompe algo que
está sostenido por escrito en otro sitio.

- **`escalar_a_doctores` no lleva tope.** Era la recomendación obvia --un atacante puede hacer
  que Daniela escale en cada turno, y cada escalamiento son tres mensajes de Telegram, uno de
  ellos sonoro y sin deduplicación-- y choca de frente con el **no negociable 26**: «solo
  silencia la RED DE SEGURIDAD, nunca la tool [...] quien mueva esta guarda a un sitio por el
  que pase la tool la cruza». Lo que el modelo escala llamando a la tool sale siempre. Es la
  frontera con la seguridad clínica y el principio que decide los empates la pone del lado de
  que suene de más. **Si algún día hay que acotarlo, no se silencia: se degrada a agregado**
  --«este número lleva N escalamientos en una hora»-- para que la señal siga saliendo.
- **El aviso de cuota no cuelga de `al_escalar`** ni escribe en `escalamientos`. Un límite de
  coste no es un escalamiento clínico, y mezclarlos ensuciaría el informe de «sin resolver» y
  el contador de interrupciones al doctor, que tiene que ser cierto (no negociable 26, última
  parte).
- **`MAXICARE_LEER_ARCHIVOS` es INDEPENDIENTE de `MAXICARE_DANIELA_RESPONDE`.** Ver abajo.

## Las tres reglas del módulo `cuotas.py`

1. **Un freno que muerde a un paciente real es peor que el ataque que evita.** Los defaults van
   muy por encima del uso legítimo, calibrados contra el volumen real del 21/09/2026: 4
   conversaciones y 11 turnos al día.
2. **Si la base no contesta, se DEJA PASAR.** Fallo abierto deliberado, mismo criterio que
   `guardrails._preguntar`: un freno que tumba turnos cuando Postgres tiene un mal minuto
   convierte la defensa en la caída que pretendía evitar.
3. **El aviso sale UNA vez por ventana.** Sin eso, quien se pasa recibe una frase fija por cada
   mensaje y el doctor un Telegram por cada uno — la inundación que la cuota existe para evitar,
   servida por la propia defensa. Lo decide Postgres con un único
   `INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING`, no un `if`: dos mensajes
   simultáneos del mismo número pasarían los dos por una lectura antes de que ninguno escribiera.

## Por qué la cuota vive en `runtime._entregar` y no en `atencion.atender`

Lo enseñaron las pruebas, no el diseño. Metida en `atender`, la consulta de cuota añadía una
conexión a Neon al principio del turno y tumbaba **tres** pruebas de `test_atencion.py`: una
cuenta los bloques de conexión —la invariante de que Neon se cierra ANTES de llamar al modelo—
y otras dos dependen de qué conexión revienta en qué orden, así que la conexión extra
desordenaba los dobles y la que debía fallar ya no era la que fallaba.

Que una defensa nueva desordene los dobles de un camino medido es la señal de que está en el
sitio equivocado, no de que las pruebas sobren. Y el sitio correcto se lee solo: `_entregar` ya
es donde el proyecto decide si un mensaje se atiende — el dedupe de Meta, `/clearstate` — así
que una tercera razón para no atender pertenece a esa lista.

## Los frenos de mano son TRES a propósito, y ninguno cuelga de otro

| Variable | Qué apaga | Qué NO apaga |
|---|---|---|
| `MAXICARE_DANIELA_RESPONDE=0` | la respuesta al paciente | el archivo y la lectura al doctor |
| `MAXICARE_LEER_ARCHIVOS=0` | la llamada al modelo que lee archivos | la entrega del archivo al doctor |
| `MAXICARE_TRANSCRIBIR_AUDIO=0` | la llamada que pasa una nota de voz a texto | la entrega del audio al doctor |

**El tercero llegó el 21/09/2026 con `transcripcion.py`, y la tentación de colgarlo del
segundo era grande**: los dos significan «no pagues un modelo por un archivo». Sería el error
simétrico del que el segundo ya evita. `MAXICARE_LEER_ARCHIVOS` apaga algo que es del DOCTOR
y promete por escrito no tocar lo del paciente; la transcripción es lo contrario —es lo que
hace que a un paciente se le entienda—, así que encadenarlos haría que apagar «el lector se
está comiendo el saldo» degrade en silencio la atención de quien manda notas de voz. Tampoco
cuelga de `MAXICARE_DANIELA_RESPONDE`, por la misma razón que no cuelga el lector: la
transcripción también baja al hilo del doctor, así que callar a Daniela no puede quitarle al
doctor un texto que ya tenía.

Con el tercero en 0 el paciente no se queda sin respuesta: Daniela le pide que lo escriba,
que es una salida que él puede tomar solo.

La tentación es colgar el lector del primero: el lector es el consumidor más caro del sistema y
`procesar_mensaje` corre ANTES que `atender`, donde vive la única comprobación de
`daniela_responde` — o sea que con la variable en 0, cada imagen seguía costando una llamada al
modelo flagship.

**Pero ese interruptor promete por escrito** (`.claude/rules/atencion-whatsapp.md`) que «el
webhook sigue registrando el mensaje y reenviando el archivo a Telegram exactamente como antes,
y lo único que se apaga es la respuesta al paciente». La lectura clínica es del doctor, no del
paciente: atarla ahí le quitaría al doctor justo lo que ese interruptor le promete conservar, y
en el momento en que alguien lo acciona, que es cuando más está mirando el sistema.

Son dos emergencias distintas — «Daniela dice tonterías» y «el lector se está comiendo el
saldo» — y encadenarlas dejaría sin apagar por separado lo que hay que poder apagar por
separado.

## La cuota de audio cuenta APARTE de la de archivos

`cuotas.puede_transcribir` es hermana de `puede_leer_archivos` y usa la misma
`persistencia.archivos_del_dia` —que recibe los `tipos` por parámetro— pero con su propio
techo, `CUOTA_AUDIOS_DIA`. No es duplicación por descuido: son gastos de órdenes de magnitud
distintos —el lector es el modelo flagship con una imagen dentro; una transcripción son
céntimos por minuto de audio— y con un contador compartido, veinte notas de voz se comerían la
cuota de la serie periapical que viene detrás. Ese caso clínico es justo el que hizo subir
`CUOTA_ARCHIVOS_DIA` de 12 a 30.

**Corta en SILENCIO, como la de archivos y al revés que la de mensajes**: sin frase al
paciente y sin Telegram al doctor. La de mensajes avisa porque quien se pasa deja de recibir
respuesta; aquí el paciente sigue recibiéndola —Daniela le pide que lo escriba— así que una
frase de cuota sería asustar a alguien por algo que se arregla escribiendo. Por eso tampoco
hizo falta ampliar el `CHECK` de `cuotas_avisadas.clase`: esa tabla solo la escribe la cuota
de mensajes, y `'archivos'` lleva desde la 022 sin que nadie lo use.

El comentario de `archivos_del_dia` decía que el audio «no llega al modelo». Era cierto hasta
esa fecha y ya no lo es; está corregido donde está.

## El tope de descarga NO baja de lo que hoy funciona

`canales.TOPE_DESCARGA_MB = 55`, y el número está elegido para no perder ni un archivo que hoy
llegue: **Telegram rechaza por encima de 50 MB**, así que un archivo que cruce este tope ya
estaba fallando — más tarde, y después de haber ocupado la RAM. Lo que cambia es dónde falla, no
si falla.

Se mira el `file_size` que Meta ya declara en la metadata del media, **entre las dos llamadas**,
así que rechazar no cuesta ni un byte descargado. Bajarlo costaría radiografías legítimas, que
es exactamente lo que no se puede perder: el freno contra la inundación de archivos es la cuota
y el semáforo, no el tamaño.

## El truncado de la entrada cierra un desarme silencioso

`guardrails._preguntar` atrapa toda excepción y devuelve «no dispara». Es la decisión correcta
—un sistema mudo no protege a nadie— pero convierte **«reventar el contexto del evaluador» en
«apagar el guardrail de inyección»**, en silencio y dejando solo un `log.error`. Y no había tope
de longitud en el carril de WhatsApp: `_Bufer.mensajes` es una lista sin cota y `_entrada_del_grupo`
las une con `"\n".join`, así que 300 mensajes de 4.096 caracteres dentro de la ventana de 45
segundos armaban una sola entrada de más de un megabyte.

`atencion._acotar` **trunca, no rechaza**: quien escribe de más casi siempre es alguien pegando
el informe de otra clínica, no un atacante. Y la marca va dentro de los corchetes de sistema
para que el modelo sepa que falta algo — sin ella contestaría con seguridad sobre un texto que
solo vio a medias.

La asimetría que esto cierra: el chat web del panel ya validaba `max_length=4000`. **El carril
validado era el interno y el que da a internet no.**

Lo que queda vigilando el resto: `guardrails.fallos_recientes_de_evaluador()`, que el vigilante
de `runtime` mira cada cinco minutos. Un PICO de fallos del evaluador es la firma de alguien
probando el desarme; fallos sueltos son el proveedor teniendo un mal rato.

## La contabilidad no puede tumbar un turno

`consumo.anotar` va en un `try` que se lo traga todo, con el mismo criterio que
`atencion._anotar_resultado`: si la contabilidad revienta se pierde una fila, nunca una
respuesta. Un sistema que deja de atender porque no pudo apuntar lo que gastó es peor que uno
que no apunta.

Dos detalles que no son obvios:

- **Los tokens cacheados se guardan APARTE y son un SUBCONJUNTO de la entrada.** El proyecto
  corre con `prompt_cache_retention="24h"`, así que cobrarlos al precio de entrada daría una
  factura inventada — y restarlos perdería el dato de cuánto contexto viajó de verdad.
- **El coste se calcula en Python y se guarda ya resuelto.** Guardar solo tokens obligaría a
  conocer el precio de un modelo que quizá ya no existe cuando alguien lea la fila: el precio
  del día del gasto es parte del hecho.

Una corrida que no consumió nada **no escribe fila**: una fila en cero solo ensuciaría el
informe y haría que «cuántas corridas hubo» dejara de significar nada.

## Lo que la suite offline NO caza

- **Que las tablas de la 022 existan.** `cuotas.revisar` falla abierto, así que sin ellas el
  perímetro no frena a nadie **y no hay un solo error visible**. Lo verifica
  `scripts/inicializar_base.py` (bloque 9), que `desplegar.sh` corre antes de levantar.
- **El tope de descarga contra Meta de verdad.** El `file_size` lo declara la Graph API y
  offline va doblado.
- **Que la API de audio siga aceptando el ogg/opus de WhatsApp, y que el modelo SIRVA.** Mismo
  agujero que el anterior, con un filo más: un `200` con basura dentro pasa cualquier
  comprobación automática, y tres de los cuatro modelos de la cuenta devuelven exactamente
  eso. Lo mira `scripts/probar_transcripcion.py`, que imprime las frases para que las lea una
  persona en vez de contar códigos de estado.
- **El rate limit del borde.** Los middlewares de Traefik viven en las labels del
  `docker-compose.yml`, pero `rateLimit` cuenta por IP de ORIGEN y detrás del túnel de
  Cloudflare todas las peticiones llegan con la IP del proxy. Para que sirva, Traefik tiene que
  arrancar con `forwardedHeaders.trustedIPs` apuntando al túnel, y eso vive en
  `/opt/sinpiloto`, fuera de este repositorio.

## Lo que no puede hacer el código

**El límite de gasto del dashboard de OpenAI.** Es la única defensa que acota el peor caso
absoluto — todo lo de aquí acota el ritmo, no el total — y se pone en la web de OpenAI.

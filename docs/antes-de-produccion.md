# Antes de abrir el grifo

Daniela lleva desplegada desde hace días y **ya atiende**: el webhook responde, las tools
escriben, el panel marca asistencia. Lo que no ha pasado todavía es que le escriba gente. El
21/09/2026 la base de producción tenía **4 conversaciones y 11 turnos**.

Este archivo es la lista de lo que conviene tener resuelto **antes de subir el volumen**, y el
sitio donde dejar lo que se decida de aquí en adelante. Existe por la misma razón que
`antes-de-produccion-reactivacion.md`: los ledgers de `.superpowers/` están en `.gitignore` y
se pierden al limpiar una rama, así que lo que tiene que sobrevivir va versionado en git.

Distíngase de aquel: **ese cubre el encendido de la reactivación de leads** —tres plantillas de
Meta que hoy no pueden mandar nada porque su código no está fundido— y vive en la rama
`reactivacion-leads`. Éste cubre a Daniela atendiendo.

---

## 1. Las evals de la fase 9 — decidido el 21/09/2026: NO bloquean el lanzamiento

El plan (`plan-agentes.json`, fase 9) pide 22 evals antes del piloto, con este argumento:
«un piloto antes de las evals convierte a los pacientes reales en la suite de pruebas».

**Se decidió lanzar sin las 22, a propósito, y conviene saber sobre qué se apoya esa decisión
para poder revisarla:**

- **Lo determinista ya está probado.** 959 pruebas offline en verde, de las cuales 37 en
  `test_guardrails.py` y 52 en `test_agentes.py` — éstas últimas fuerzan el disparo de cada
  tripwire a través del SDK completo con un modelo guionizado. Las 5 evals de guardrail del
  plan están cubiertas en todo lo que una prueba determinista puede cubrir.
- **Lo que NO cubre ninguna de ellas es el modelo real**, que no es determinista. La cicatriz
  de este proyecto: el rótulo «Confirmar» pasó limpio a las 22:04 y disparó una alerta falsa a
  las 22:11, mismo texto y mismo código. Eso es exactamente lo que las evals añaden encima, y
  no es poco — pero es una red, no un requisito para operar.
- **Con 4 conversaciones al día, el piloto es pequeño y observable.** El argumento del plan es
  correcto en el caso general y desproporcionado en este: leer diez conversaciones reales
  enseña más que 22 simulacros escritos adivinando.

**Lo que sí hay que hacer antes de subir volumen, y es poco:**

1. **Las 2 o 3 evals de seguridad clínica.** Es el único riesgo del que no se vuelve: que
   Daniela le describa a un paciente el contenido de una radiografía. El resto de las 22 —las
   5 de camino feliz, las 6 de ruta de fallo, la de fuera de alcance— se escriben *durante* el
   piloto, con los casos que el piloto enseñe, que son mejores que los inventados hoy.
2. **Leer las conversaciones de los primeros días.** Protege más que cualquier eval.
3. **NO redirigir el WhatsApp entero de la clínica de golpe.** Ahí sí, sin red, los pacientes
   son la suite de pruebas. La versión del argumento del plan que sí aplica es ésta.

**Y el hueco que queda abierto, dicho como lo que es:** el guardrail `sin_lectura_clinica`
nunca se ha probado contra material real, porque el **lote de ≥50 radiografías, fotos y
remisiones anonimizadas** que pide `observabilidad.evals` no ha llegado — es una dependencia
externa de MaxiCare, no algo que se pueda construir aquí. Mientras no llegue, ese freno va a
producción probado solo contra casos que inventamos nosotros. No es razón para no lanzar; sí
es razón para que alguien mire qué contestó Daniela los primeros días en que entren imágenes.

Ese mismo lote es, además, lo que decidiría si se puede bajar a la clase económica de modelo:
si lo pasa al 100 %, se cambia y se ahorra ese dinero todos los meses. **Conviene pedirlo ya**,
aunque las evals se escriban después: es lo que más tarda en llegar.

---

## 2. Lo demás que sigue abierto, verificado el 21/09/2026

- **Rotar la contraseña de Neon.** Lleva semanas en la lista y no se ha hecho.
- ~~**A la Neon principal se le están acabando los créditos.**~~ **Cerrado el 21/09/2026: se
  volvió a la principal y `MAXICARE_DATABASE_URL_ALT` se borró del `.env`.** Ya no hay base
  alterna: `MAXICARE_DATABASE_URL` es la única, y desarrollo y producción comparten base.
  Conviene saber lo que eso implica, porque no había que saberlo antes: **`inicializar_base.py`
  y las pruebas `-m neon` escriben ahora en la base que atiende pacientes** —las segundas solo
  en el esquema `pruebas`, que borran al terminar, pero el `public` de esa base ya es el de
  verdad—. La ALT nunca fue un interruptor: **ningún archivo de `src/` la leía jamás**, así que
  «trabajar contra la alterna» era cambiar a mano el valor de `MAXICARE_DATABASE_URL`. Quien
  vuelva a necesitar una base de desarrollo tiene que hacer lo mismo, y ese es el motivo por el
  que no se deja un respaldo silencioso en `config.py`: dejaría que producción arrancara contra
  la base equivocada sin que nadie lo note.
- **El límite MEDIDO del historial sigue en PENDIENTE.** `scripts/medir_historial.py` exige 20
  turnos en 5 conversaciones con historial guardado y hoy hay 11 en 4. Lo desbloquea el propio
  piloto; después, volver a correrlo. No confundir con `config.LIMITE_HISTORIAL_SESION = 230`,
  que es un tope de seguridad y no una medición.
- **El ahorro de caché (~45 %) sigue siendo un cálculo, no una medición.** Se confirma contra
  `usage.cached_tokens` de la API real, y el README debe seguir presentándolo como cálculo
  hasta entonces.
- **`scripts/sembrar_demo_sin_resolver.py` sigue en el repo y en el VPS.** Es de usar y tirar y
  vuelve a sembrar casos falsos en `public` si alguien lo corre.
- **Defecto menor de `scripts/desplegar.sh`**: su último chequeo hace `curl` por HTTP plano a
  Traefik con cabecera `Host` y devuelve **404 en todo despliegue correcto**. Asusta sin
  motivo; por HTTPS responde bien.

---

## 3. Lo que se cerró y ya no está aquí

- **Los tres trabajos diagnosticados de la fase 8** (21/09/2026): la inferencia destructiva
  sobre citas pasadas, los dos recordatorios vivos del `GET /api/agenda`, y la línea que
  faltaba en el `CLAUDE.md` sobre `-m neon` para `tests/test_panel.py`. El mecanismo de los dos
  primeros está en `web/CLAUDE.md`, con lo que se descartó y por qué.

---

## Lo que NO se hace, y no hace falta volver a proponerlo

Decidido por el cliente, no por falta de tiempo:

- **«Valoración» como tratamiento agendable.** Vio el caso —Daniela escaló en vez de agendar—
  y dijo que escalar ahí está bien.
- **La pantalla de las tres perillas** (`capacidad_por_hora`, duración de cita, cierre del
  relevo). Se cambian por SQL. Ojo, quien la construya algún día: las dos del relevo se cachean
  en memoria al arrancar (`runtime.py:215-216`), así que una pantalla que ofreciera las tres
  por igual mentiría en una de las tres.
- **El parte diario de citas por Telegram.** El cliente lo quiere solo por WhatsApp, aun
  sabiendo que eso exige plantilla propia y los números de los doctores. **Las dos cosas que
  faltaban ya existen** desde el 21/09/2026: los números viven en `config.WHATSAPP_DOCTORES` y
  el aviso por WhatsApp de cada cita nueva está construido (ver la sección 4). Un parte DIARIO
  sigue siendo otra plantilla y otro reloj, pero ya no parte de cero.

---

## 4. El perímetro de coste y el aviso de citas — 21/09/2026

Dos trabajos de la rama `seguridad-perimetro`, hechos el mismo día y por el mismo motivo:
**subir el volumen es exactamente lo que vuelve reales los dos riesgos**.

### Lo que hay que hacer a mano antes de que esto sirva

1. **Poner un límite de gasto mensual en el dashboard de OpenAI.** Es la única defensa que
   acota el peor caso ABSOLUTO — todo lo que se construyó acota el ritmo, no el total — y no se
   puede hacer desde el código. Cinco minutos.
2. ~~**Crear la plantilla `cita_nueva_doctores` en el Business Manager de Meta**~~ (categoría
   *Utility*, 5 variables: nombre, teléfono, motivo, fecha, hora). **`MAXICARE_PLANTILLA_CITA_NUEVA`
   ya tiene ese nombre puesto en el `.env` del VPS**, así que el aviso NO está en modo de
   comprobación: manda. El 23/09/2026 se le sumó un tercer destinatario (Santiago, de
   administración) en `config.WHATSAPP_DOCTORES`.

   **Lo que sigue sin comprobar, y conviene saber por qué:** que Meta la tenga APROBADA y la
   entregue. No hay forma de mirarlo desde aquí — listar plantillas cuelga de la cuenta de
   negocio (WABA), y el token de usuario de sistema devuelve sus `granular_scopes` con
   `target_ids` vacío, así que `probar_plantilla.py::_waba_ids` sale con las manos vacías y
   `/me/assigned_whatsapp_business_accounts` responde `data: []`. Las dos salidas son entrar a
   la consola de Meta, o mandar un mensaje de verdad. Y `scripts/probar_plantilla.py` **no
   cubre esta plantilla**: su `_PLANTILLAS` tiene las cuatro de recordatorio y reactivación.
   Mientras tanto, si no estuviera aprobada, el fallo se vería con la primera cita real y solo
   como una línea de `log.exception` en el contenedor.
3. **Desplegar**, que aplica la migración 022. Sin ella el perímetro no frena a nadie **y no
   hay un solo error visible** (no negociable 27).

### La decisión que queda abierta, y conviene tomarla con datos

Las cuotas están calibradas contra 4 conversaciones y 11 turnos al día, que es lo que había
cuando se escribieron. **Son una corazonada informada, no una medición.** Por eso existe
`MAXICARE_CUOTA_MODO_OBSERVACION=1`: cuenta y avisa sin cortar a nadie.

Lo razonable es encenderlo en observación durante los primeros días de volumen real, mirar qué
números aparecen de verdad en `consumo_modelo` y en los avisos del General, y solo entonces
fijar los umbrales y apagar el modo. Arrancar cortando con un número inventado es la forma de
descubrir el límite con un paciente real del otro lado.

### Lo que se decidió NO hacer, con su razón

**Ponerle tope a `escalar_a_doctores`.** Era la recomendación obvia del análisis de amenazas
—un atacante puede hacer que Daniela escale cada turno, y cada escalamiento son tres mensajes
de Telegram, uno sonoro y sin deduplicación— y choca de frente con el no negociable 26: lo que
el modelo escala *llamando a la tool* sale siempre. Ahí está la frontera con la seguridad
clínica, y el principio del proyecto la pone del lado de que suene de más.

Si algún día hay que acotarlo —porque pase de verdad, no porque se pueda imaginar— la forma es
**degradar a agregado y no silenciar**: a partir del tercero en una hora, un único mensaje que
diga cuántos van. La señal sigue saliendo; lo que se corta es la repetición.

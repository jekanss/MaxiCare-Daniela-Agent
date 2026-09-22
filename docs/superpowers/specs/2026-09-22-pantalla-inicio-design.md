# La pantalla de Inicio: cinco números y un sidebar oscuro

Fecha: 2026-09-22 · Estado: aprobado, sin implementar

## Contexto

El panel tiene siete secciones y ninguna portada. Quien entra cae en «Sin resolver», que
es una pantalla de diagnóstico: lo primero que ve el doctor de MaxiCare al abrir su panel
es la lista de lo que su asistente NO pudo hacer. No hay ningún sitio donde el sistema
diga qué sí hizo.

La sección «Métricas» existe en el menú desde el principio, marcada como fase 9, y su
cartel promete «ocho indicadores, ni uno más». Ese texto viene de una especificación de
Figma que no está versionada y usa un vocabulario —«valoración asistida»— que no aparece
ni en el brief ni en el plan. Conviven tres recuentos distintos: ocho más uno en
`brief-agentes.json`, siete en `plan-agentes.json` («una por criterio de éxito, ni una
más») y ocho con otro vocabulario en el cartel.

**Este documento no resuelve la pantalla de Métricas.** Resuelve una anterior: una portada
con los cinco números que contestan, en cinco segundos, si Daniela está funcionando. La
pantalla de Métricas sigue pendiente y es donde irá el análisis con percentiles, series y
comparación formal contra la línea base.

El diseño visual sale de Claude Design, proyecto «Sinpiloto panel redesign»
(`f83871f6-f3a7-4f6d-aa8f-35fafa000dd5`, archivo `MaxiCare Panel.dc.html`). Ese archivo
propone un rediseño del panel entero; aquí se adopta solo la pantalla de Inicio y el
sidebar.

### La línea base, que es lo que le da sentido a cada número

Congelada en `brief-agentes.json → exito`, medida sobre el WhatsApp de la clínica antes de
que Daniela existiera:

| Antes | Valor |
|---|---|
| Conversaciones en un mes | 97 |
| Citas que salieron de ellas | 2 (2 %), todas de gente nueva |
| Conversaciones sin respuesta | 50 % |
| Primera respuesta | de minutos a 3 horas o un día entero |
| Inasistencias | nunca se midió |

## Los cinco números

Ventana: **últimos 30 días**, la misma para los cinco. El mockup mezclaba tres ventanas
—24 horas, semana, 30 días— en cuatro tarjetas contiguas; cuatro cifras juntas que miden
periodos distintos se leen como si midieran el mismo.

Unidad: **el teléfono, no la conversación**. Una conversación caduca por inactividad de
24 h (`persistencia.conversacion_viva`), así que una negociación de tres días son tres
filas en `conversaciones` y una sola persona. Cualquier tasa «por conversación» tiene el
denominador inflado y subestima el trabajo de Daniela.

### 1 · Escribieron

Personas distintas que mandaron algo.

```sql
SELECT count(DISTINCT telefono)
  FROM mensajes_entrantes
 WHERE recibido_en >= %(desde)s;
```

### 2 · Quedaron con cita

Personas distintas con una cita viva creada en el periodo. Se compara contra la línea base
de 2 al mes; el objetivo del brief es 7 de cada 10.

```sql
SELECT count(DISTINCT telefono)
  FROM citas
 WHERE creada_en >= %(desde)s
   AND estado <> 'cancelada';
```

### 3 · Llegaron

**Solo sobre citas cuya hora ya pasó.** Contar una cita de mañana como «no llegó» es
contar un futuro como un fracaso.

```sql
SELECT count(*) FILTER (WHERE asistio IS TRUE)     AS llegaron,
       count(*) FILTER (WHERE asistio IS NOT NULL) AS marcadas,
       count(*)                                     AS cumplibles
  FROM citas
 WHERE inicio >= %(desde)s
   AND inicio < now()
   AND estado <> 'cancelada';
```

**Las tres cifras viajan juntas al navegador y la pantalla pinta la cobertura al lado.**
Si nadie marca la asistencia, este número no baja: se vuelve falso. Una tasa del 100 %
calculada sobre 3 citas de 24 es una mentira con aspecto de dato, y el camino que lleva
ahí es que la recepción se retrase una semana, que es lo normal. `panel.citas_sin_marcar`
ya existe para perseguir esas marcas desde la pantalla de Agenda.

### 4 · Sin contestar

Mensajes que siguen sin respuesta. La línea base es que la mitad de la gente no recibía
ninguna, así que este número tiene que ser cero.

```sql
SELECT count(*)
  FROM mensajes_entrantes
 WHERE recibido_en >= %(desde)s
   AND recibido_en < now() - interval '5 minutes'
   AND respondido_en IS NULL
   AND (fallo_respuesta IS NULL OR fallo_respuesta NOT LIKE 'relevo:%%');
```

Tres cosas que decide ese `WHERE`, cada una por un motivo:

- **El margen de 5 minutos** evita contar como «sin contestar» un mensaje que llegó hace
  diez segundos y está dentro del búfer de silencio. Mismo criterio que
  `persistencia.contar_sin_responder`, que usa `margen_segundos=300`.
- **`fallo_respuesta NOT LIKE 'relevo:%'`** saca los mensajes que atendió un doctor durante
  un relevo. Esos llevan el prefijo `relevo:` sin ser un fallo (no negociable 15), y
  contarlos convertiría cada relevo —que es el sistema funcionando— en un mensaje
  desatendido.
- **Un `fallo_respuesta` que no sea de relevo SÍ cuenta.** El paciente no recibió nada; que
  el motivo esté anotado no cambia lo que le pasó a él.

Límite que hay que declarar en el spec y no olvidar: `marcar_respondido` pone
`fallo_respuesta = NULL` al reintentar con éxito. Esta columna mide **si hay un mensaje sin
contestar ahora**, nunca **cuántos hubo**. No sirve para una serie histórica.

### 5 · Tu tiempo

Minutos que un doctor estuvo dentro de una conversación. Es el criterio de éxito nº 3 del
brief —«los doctores dejan de contestar WhatsApp»— medido en la unidad que el doctor
siente.

```sql
SELECT coalesce(
         sum(EXTRACT(EPOCH FROM (coalesce(relevo_cerrado_en, now()) - tomada_en))) / 60,
         0)::int AS minutos,
       count(*)  AS relevos
  FROM conversaciones
 WHERE tomada_en >= %(desde)s;
```

`relevo.cerrar` pone `tomada_por = NULL` pero **no toca `tomada_en`**
(`persistencia.py:1229`), así que la duración sobrevive al cierre. Un relevo todavía
abierto cuenta hasta `now()`.

Límite: `activar_relevo` limpia `relevo_cerrado_en` al reactivar, así que un segundo
relevo en la misma conversación borra el rastro del primero. Este número **subcuenta**, y
el spec lo dice en vez de fingir precisión.

## Los tres bloques de abajo

Se construyen con lo que ya existe.

| Bloque | Fuente | Nota |
|---|---|---|
| La agenda de hoy | `panel.citas_del_dia` | Sin reconciliar. Ver abajo |
| Qué necesita su atención | `persistencia.casos_recientes` | Los tres casos de mayor `contador`, con enlace a «Sin resolver» |
| Volumen de conversaciones | `conversaciones` agrupadas por día | Rotulado con el primer día que tiene datos |

El gráfico de volumen NO promete catorce días. Rotula el rango real: hoy diría «desde el
16 de septiembre», porque es cuando empieza la historia. Un eje de catorce días con diez
vacíos dibuja una caída que nunca ocurrió.

## Arquitectura

### `GET /api/inicio` no toca Google Calendar

Es la decisión más importante del documento. `GET /api/agenda` reconcilia contra Calendar
al abrirse y puede mover citas, soltar cupos y reprogramar recordatorios: está decidido y
documentado en `web/CLAUDE.md`, y se quiso así porque abrir esa pantalla es el mejor
disparador que la reconciliación tiene.

**Inicio va a ser la primera pantalla de cada sesión.** Si reconciliara, cada login
dispararía escrituras en Neon y llamadas a la API de Google, varias veces al día y por
cada persona que entre. La agenda de hoy en Inicio se lee de Neon tal cual. Quien quiera
la versión contrastada entra a Agenda, que es donde vive esa promesa.

`/api/inicio` es de **solo lectura**, igual que `/api/sin-resolver`.

### Archivos

**Backend** — la lógica en `panel.py`, nunca en `runtime.py`, que es solo transporte
(frontera que vigila `tests/test_estructura.py`):

- `src/maxicare_daniela/panel.py` → `resumen_inicio(conn, *, dias: int = 30) -> dict`.
  Una función, las cinco consultas más los tres bloques. Devuelve las cifras crudas, no
  porcentajes: quién divide entre quién lo decide la pantalla, y así el numerador y el
  denominador viajan los dos.
- `src/maxicare_daniela/runtime.py` → `GET /api/inicio`, insertado junto a
  `/api/sin-resolver` (~línea 2105). Cualquier sitio anterior a la línea 3099, o la ruta
  comodín se lo traga.

**Frontend**:

- `web/src/pantallas/Inicio.tsx` — nueva. Copia el patrón de `SinResolver.tsx`: los tres
  estados con salida temprana (cargando / error / contenido), la `<Cabecera />` repetida en
  los tres, y **la `ref` para `alCaducarSesion`** con `useCallback` de dependencias vacías.
  Meter esa prop en las dependencias deja la pantalla releyendo Neon en bucle, y no hay
  ninguna prueba que lo atrape.
- `web/src/componentes/Sidebar.tsx` — al oscuro (`#0B0912`), iconos SVG en vez de emojis,
  entrada «Inicio» al principio, pie con el logo de Sinpiloto. Añadir `'inicio'` a
  `SeccionId` y a `SECCIONES` con `fase: null`.
- `web/src/App.tsx` — ruta `inicio` y que sea la sección por defecto al entrar.
- `web/src/api.ts` — `leerInicio()`, por `pedir<T>`, devolviendo el objeto entero (trae
  metadatos, como `listarSinResolver`).
- `web/src/index.css` — añadir Sora y JetBrains Mono al import de Google Fonts. Los tokens
  `--color-sp-*` **no se renombran ni se amplían**: la paleta nueva va con hex literales
  inline, que es el dialecto de las pantallas del panel.

Los SVG del logo se descargan del proyecto de Claude Design (`assets/sinpiloto-logo-dark.svg`,
`assets/sinpiloto-mark.svg`) a `web/src/marca/`.

## Estados vacíos

**Se escriben antes que los llenos.** Hoy la base tiene 9 conversaciones, 6 teléfonos,
0 citas y 4 días de historia desde el 16/09. Ese es el estado en que MaxiCare va a ver la
pantalla las próximas semanas, así que es el que tiene que quedar bien.

| Situación | Qué pinta |
|---|---|
| Sin citas todavía | «sin citas cumplidas todavía», nunca `0 %` |
| Citas futuras pero ninguna cumplida | el número de agendadas, y «llegaron» en espera |
| Ninguna marcada | la cobertura en primer plano: «ninguna de 4 marcada» |
| Menos de 7 días de datos | el gráfico rotula su primer día en vez de dibujar un eje de 14 |
| Cero sin contestar | es el valor bueno: se pinta en positivo, no como un vacío |

Un cero de una clínica que lleva seis días funcionando no es un mal resultado, y la
pantalla no puede sugerir que lo sea.

## Lo que queda fuera, y por qué

- **El costo por cita.** Estaba en la propuesta inicial y el cliente lo quitó. `consumo_modelo`
  sigue recogiendo el dato; simplemente no se pinta.
- **Las cuatro tarjetas del mockup** (Conversaciones 284 / Citas 41 / Leads 17 / Sin
  resolver 38). «Leads» y «Sin resolver» bajan a accesos con su contador; «Leads» además
  es una sección que todavía no existe.
- **Los números del mockup, en cualquier forma.** Ni como valores por defecto, ni como
  ejemplo comentado, ni como respaldo cuando la consulta falla. Un `284` que sobreviva en
  el código está a un botón de distancia de parecer real en la pantalla de una clínica. Si
  la consulta falla, la pantalla dice que falló.
- **Un interruptor de datos de demostración.** Se consideró y se descartó: un modo que
  rellena un panel clínico con cifras falsas es algo que no puede encenderse por accidente
  en producción, y hay una sola base de Neon.
- **Sembrar datos de prueba.** `public` es la base que atiende pacientes y su contenido es
  la línea base del documento de caso de éxito (fase 10), que es el activo más valioso del
  proyecto.
- **Las demás pantallas.** Agenda, Tratamientos, Sin resolver y Pruebas se quedan como
  están. El sidebar sí cambia para todas, porque es un solo archivo y es lo primero que se
  ve.

## Verificación

1. `uv run pytest -q` — la suite offline, incluida la nueva de `resumen_inicio` con dobles.
2. `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` — las consultas contra Postgres de
   verdad, en el esquema `pruebas`. **Obligatorio**: `pytest -q` a secas no caza las
   pruebas de panel, y una colisión de helper ya dejó dos en rojo durante tres commits con
   la suite offline entera en verde.
3. `uv run python scripts/probar_panel.py` — el entregable de la fase 8 sigue pasando.
4. `cd web && npm run build` — `tsc --noEmit` incluido.
5. **A ojo, con el panel levantado**: entrar y comprobar que la portada es Inicio, que los
   cinco números salen, que ninguno inventa nada, y que con 0 citas la tarjeta de
   «llegaron» no dice `0 %`.
6. **Contar las peticiones**: abrir Inicio varias veces y comprobar en el log que no
   aparece ninguna llamada a Google Calendar. Es la invariante que sostiene la decisión de
   arriba.

## Lo que este documento NO decide

- El vocabulario «valoración asistida» del cartel de Métricas. Sigue sin saberse si es
  lenguaje de MaxiCare o invención del mockup. Pendiente de preguntárselo al cliente.
- Si las 97 conversaciones de la línea base se contaron como personas o como hilos de
  WhatsApp. Importa antes de publicar cualquier comparación formal, y es una pregunta para
  MaxiCare, no para el código.
- La pantalla de Métricas (fase 9), que es donde irán los percentiles, las series y la
  comparación contra la línea base.

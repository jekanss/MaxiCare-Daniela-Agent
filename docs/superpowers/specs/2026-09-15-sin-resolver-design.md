# Sin resolver — lo que Daniela no pudo, agrupado y explicado

**Fecha:** 2026-09-15
**Estado:** diseño cerrado, sin implementar
**Alcance:** una pantalla de solo lectura, su tabla, su captura y su tarea de fondo.
No es una fase completa del plan: es una funcionalidad acotada que se puede construir
y verificar sola.

---

## 1. El problema

Hoy, cuando Daniela no puede resolver algo, esa información se pierde o se dispersa.

```
  SEÑAL                                   DONDE ACABA HOY

  "no tengo ese dato"                     en ninguna parte.
  consultar_base_conocimiento devuelve    Ni un log. Ni un contador.
  SIN DATO DOCUMENTADO                    [SE PIERDE]

  un guardrail la freno y la              en ninguna parte.
  regeneracion salio bien                 Resultado.tripwires muere
  (el caso MAS frecuente)                 dentro del proceso.
                                          [SE PIERDE]

  el turno se rompio                      mensajes_entrantes.fallo_respuesta
                                          se ESCRIBE, pero solo se consulta
                                          IS NULL. El texto del motivo no lo
                                          lee nadie.  [ESCRITO, CIEGO]

  escalo al doctor                        tabla `escalamientos` + un mensaje
                                          de Telegram al tema General.
                                          respondido_en y respuesta_doctor
                                          son columnas MUERTAS.  [A MEDIAS]

  el doctor relevo                        quien y cuando, nunca POR QUE.
                                          [A MEDIAS]
```

El escalamiento **no puede** hacer de informe, por tres razones concretas:

1. **Es un chat, y los chats se scrollean.** Siete escalamientos por lo mismo llegan
   repartidos en doce días, mezclados con radiografías y urgencias. Nadie ve el patrón.
2. **Nadie sabe cuáles se atendieron.** `escalamientos.respondido_en` y
   `escalamientos.respuesta_doctor` existen desde la migración 001 y nadie las escribe.
3. **No agrupa.** Siete escalamientos son siete filas con siete claves de idempotencia
   distintas. La tabla no tiene forma de decir "esto es lo mismo siete veces".

**Escalamiento y este informe hacen cosas distintas:**

```
  ESCALAMIENTO                        SIN RESOLVER
  ================================    ================================
  "atiende a Camila AHORA"            "7 pacientes en 12 dias"
  urgente                             estructural
  un paciente                         un patron
  resuelve el SINTOMA                 resuelve la CAUSA
  -> Camila queda atendida            -> los proximos 50 no preguntan
```

---

## 2. Qué es y qué no es

**Es un informe vivo de solo lectura.** Una lista corta de problemas repetidos, cada uno
con qué pasó y qué se recomienda hacer.

**No es** una bandeja de tareas. No tiene botones, ni estados, ni cola de trabajo, ni
"marcar como resuelto".

**Quién lo ve:** MaxiCare y el desarrollador, la misma pantalla. El informe está siempre
escrito en idioma de clínica; el detalle técnico va en un desplegable que solo ve `admin`.

**Quién arregla:** el desarrollador. El informe no ofrece atajos para que la clínica
edite nada — ese es el servicio que sostiene la mensualidad.

> Nota honesta: la clínica **ya puede** editar el conocimiento hoy por
> `PUT /api/conocimiento`, abierto a los roles `admin` y `doctor`. Esta funcionalidad no
> quita esa capacidad ni la toca; simplemente no empuja hacia allá. Cerrar esa puerta,
> si alguna vez se quiere, es un cambio de un rol en una línea y otra conversación.

---

## 3. Las cuatro señales que entran

| Tipo | Qué es | De dónde sale |
|---|---|---|
| `FALTA_DATO` | `consultar_base_conocimiento` devolvió `SIN DATO DOCUMENTADO` | **lo único nuevo**: un acumulador en `ctx.turno` |
| `GUARDRAIL` | saltó un tripwire, **incluidos los que se regeneraron bien** | `Resultado.tripwires` — **ya existe y ya se llena** |
| `ROTO` | excepción, `MaxTurnsExceeded`, envío fallido, calendario caído | `motivo` — **ya llega a `_anotar_resultado`** |
| `HUMANO` | escalamiento o relevo | `Resultado.escalado_por` — **ya existe** |

**Tres de las cuatro señales ya viajan solas.** `Resultado` (`conversacion.py:203-223`) ya
transporta `tripwires`, `escalado_por` y `fallo` hasta `atencion.py`; lo que pasa hoy es que
`atencion.py` solo usa tres de esos campos y descarta `tripwires`.

Consecuencia que reduce el riesgo del cambio a casi nada: **`conversacion.py` no se toca.**
Lo único nuevo en el camino del turno es un campo en `TurnoEnCurso` y dos líneas en la tool
de conocimiento.

**Regla de no duplicar:** si en el mismo turno hay un hueco de conocimiento **y** un
escalamiento, el escalamiento se cuenta *dentro* del caso del hueco (columna `escalo`),
no abre un caso propio. Si abriera uno, la misma historia saldría contada dos veces en
dos tarjetas.

Un caso `HUMANO` propio se abre solo cuando el escalamiento **no** viene acompañado de un
hueco de conocimiento en ese turno: una urgencia, una queja por un tratamiento anterior,
una excepción comercial. Y siempre en el caso del relevo abierto a mano por el doctor, que
ocurre fuera de cualquier turno.

---

## 4. Decisiones cerradas

Cada una con la alternativa que se descartó y por qué.

### 4.1 Agrupado por huella, no una fila por incidencia

**Decidido:** doce preguntas iguales son **una** fila con `contador = 12`, `primera_vez`,
`ultima_vez` y hasta cinco ejemplos textuales.

**Descartado:** una fila por incidencia. Con 200 conversaciones al mes el informe tendría
~400 renglones en vez de ~15, y se convierte en un feed que nadie termina de leer.
Además multiplicaría por doce el costo del análisis para decir doce veces lo mismo.

### 4.2 Solo informativo: sin botones ni estados

**Decidido:** la pantalla solo lee. Ningún estado `abierto/resuelto/descartado`.

**Descartado:** bandeja con ciclo de vida y tres botones (`Implementar` /
`No, a propósito` / `Después`). Se descartó por dos razones: el ajuste lo hace el
desarrollador, no el cliente —un botón que carga la ficha desde la pantalla le quita a la
mensualidad su razón de ser—; y porque el problema que los botones resolvían (que el
informe se llene de ruido) se resuelve mejor con la ventana de tiempo (4.3), sin pedirle
disciplina a nadie.

### 4.3 Ventana de 30 días, ordenado por frecuencia: se limpia solo

**Decidido:** la pantalla muestra lo ocurrido en los últimos 30 días, ordenado por cuántas
veces pasó.

Consecuencia: lo que se arregla deja de acumular, sale de la ventana y **se hunde solo**.
Nadie borra ni cierra nada. El informe se mantiene corto por física, no por disciplina.

```
  HOY                                     TRES SEMANAS DESPUES
                                          (tras cargar el precio de ortodoncia)

   7x  Falta el precio de ORTODONCIA       4x  Falta el horario de los sabados
   4x  "limpieza" no la encuentra          3x  "limpieza" no la encuentra
   3x  Falta el horario de los sabados     1x  Falta la duracion de BLANQUEAMIENTO
   2x  Se rompio el calendario
   1x  Falta la duracion de BLANQUEAMIENTO
```

**Descartado:** guardar todo para siempre y filtrar en pantalla. La fila histórica se
conserva igual (la tabla no borra), pero la vista por defecto es la ventana: si se
mostrara todo, el informe crecería sin fondo y el primer mes dejaría de leerse.

Efecto lateral valioso: lo que es *a propósito* —endodoncia y prótesis no llevan precio
por WhatsApp, por decisión de la clínica— aparece siempre. Y eso deja de ser ruido para
convertirse en dato de negocio: *"30 personas al mes preguntan el precio de endodoncia y
no se los damos; ¿la respuesta actual los convierte en valoración o los pierde?"*

### 4.4 La frase del paciente se copia, y `/clearstate` la borra

**Decidido:** el caso guarda hasta cinco frases textuales, copiadas. `/clearstate` las
borra junto con el resto del rastro, **antes** del `DELETE FROM conversaciones`
(no negociable 9). El caso sobrevive: el contador no baja y la huella no cambia.

**Justificación:** el conteo es historia de la clínica, no dato del paciente. *"Doce
personas preguntaron por ortodoncia"* sigue siendo cierto aunque se borre una de esas
conversaciones.

**Cada ejemplo guarda el teléfono junto a la frase**, y ese es el único motivo por el que lo
guarda: sin él, `/clearstate` no tiene forma de saber cuál de las cinco frases borrar.
El teléfono **nunca sale por el endpoint** — se filtra en la capa de lectura, no en la
pantalla, para que ni siquiera viaje al navegador.

**Descartado (a):** guardar solo el id del mensaje y leer la frase por `JOIN`. Más limpio
para el borrado, pero un `JOIN` más en cada carga y el informe se queda mudo si alguna vez
se archivan mensajes viejos.
**Descartado (b):** no guardar frases. Sin la frase no se sabe si preguntaban el precio
total, la cuota mensual o el de los controles — y esa diferencia es justo la ficha que hay
que escribir.

### 4.5 El informe lo escribe un modelo, automáticamente

**Decidido:** cada caso llega con su informe ya escrito. Sin botón de "analizar".

**Descartado (a):** plantilla determinista más un botón de análisis bajo demanda. Un botón
que hay que apretar no se aprieta nunca, y el informe vuelve a ser una lista de errores.
**Descartado (b):** solo plantilla determinista. Cero tokens y cero riesgo, pero nunca
notaría que cinco de los siete ejemplos preguntan por la cuota mensual — que es la mitad
del valor del informe.

---

## 5. Modelo de datos

Migración `018_casos_sin_resolver.sql`.

```sql
CREATE TABLE casos_sin_resolver (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    huella        TEXT NOT NULL UNIQUE,
    tipo          TEXT NOT NULL,          -- FALTA_DATO|GUARDRAIL|ROTO|HUMANO
    contador      INTEGER NOT NULL DEFAULT 1,
    escalo        INTEGER NOT NULL DEFAULT 0,
    primera_vez   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultima_vez    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ejemplos      TEXT NOT NULL DEFAULT '[]',   -- JSON serializado a mano
    informe       TEXT,                          -- JSON serializado a mano
    -- ejemplos: [{"texto": "...", "telefono": "+57..."}, ...]  maximo 5
    -- informe:  {"que_paso": "...", "por_que": "...", "recomiendo": "..."}
    informe_en    TIMESTAMPTZ,
    informe_sobre INTEGER                 -- el contador cuando se analizo
);
```

`informe_sobre` es lo que permite re-analizar solo cuando el caso creció de verdad
(ver 7.4) sin volver a llamar al modelo cada vez que sube el contador.

**`TEXT` y no `JSONB`, a propósito.** No hay un solo `JSONB` en `migraciones/` ni un solo
`Json(...)` de psycopg en `src/`: el JSON que este proyecto guarda va como `TEXT`
serializado con `json.dumps(..., ensure_ascii=False)`. Se sigue ese patrón. `JSONB` sería
técnicamente mejor pero introduciría el primer adaptador de psycopg del repo para una tabla
que nadie va a consultar por campo interno.

**La huella la arma el código, nunca el modelo.** Es determinista y por tipo:

```
  FALTA_DATO   falta_dato:{tratamiento}:{concepto}
               -> falta_dato:ortodoncia:precio

  GUARDRAIL    guardrail:{nombre_guardrail}:{tratamiento|_general}
               -> guardrail:sin_cifra_no_documentada:ortodoncia

  ROTO         roto:{type(e).__name__}
               -> roto:ModelBehaviorError

  HUMANO       humano:{motivo}     (los 5 de MotivoEscalamiento)
               -> humano:excepcion_comercial
```

**Trampa de `ROTO`:** hoy se guarda `f"{type(e).__name__}: {e}"`, y el mensaje lleva ids,
horas y fragmentos variables. Si la huella usara el mensaje completo, **cada error sería
único y no agruparía jamás**. Solo el nombre de la clase.

**Escritura:** un único `INSERT ... ON CONFLICT (huella) DO UPDATE` que incrementa
`contador`, suma `escalo`, pisa `ultima_vez` y hace `append` al array de `ejemplos`
recortándolo a cinco. Atómico: dos turnos simultáneos con la misma huella no pueden crear
dos filas.

---

## 6. Captura

**Principio: el informe jamás puede romper una respuesta a un paciente.** Nada se escribe
en el camino crítico. Todo se acumula en memoria durante el turno y se vuelca una sola vez
al final, en el sitio que ya existe para eso y que ya traga sus propios errores.

```
  UN TURNO DE WHATSAPP  (atencion.py)

  llega mensaje
      |
      v
  [ventana del buffer]  ->  [CANDADO por telefono]
      |
      v
  conversacion.responder()  ->  ctx.turno.reiniciar()   <- senales = []
      |
      |   LO UNICO NUEVO en el camino del turno:
      |   consultar_base_conocimiento anota que consulto
      |      -> ctx.turno.senales.append(Senal(tratamiento, concepto, hubo_dato))
      |
      |   los tripwires y el escalamiento NO se tocan: ya viajan
      |   solos en Resultado.tripwires y Resultado.escalado_por
      |
      v
  RESPUESTA ENVIADA AL PACIENTE     <- el informe no ha tocado NADA todavia
      |
      v
  _anotar_resultado()   atencion.py:359   [ya abre conn, ya traga errores]
      |
      +-- marcar_respondido / marcar_fallo_respuesta      (ya existe)
      +-- tocar_conversacion                              (ya existe)
      +-- sin_resolver.registrar(conn, senales, resultado, frase)   <- LO NUEVO
```

**Por qué `ctx.turno`:** es el patrón que ya usa el proyecto. `TurnoEnCurso`
(`contratos.py:518`) ya acumula `cifras_autorizadas` y `horas_autorizadas` en memoria
durante el turno. Se añade `senales: list[Senal]` y se vacía en `reiniciar()`.

La `Senal` se anota en **toda** consulta a la base de conocimiento, con dato o sin él. Las
que tienen dato no producen caso, pero dan el tratamiento con el que se enriquece la huella
de un `GUARDRAIL` — que es lo que permite que el informe diga *"las 4 eran por limpieza
dental"* en vez de solo *"saltó `sin_cifra_no_documentada` 4 veces"*.

**Verificado:** `reiniciar()` se llama una sola vez por turno (`conversacion.py:345`),
**antes** de la regeneración por tripwire. Lo que se acumule durante el turno sobrevive al
reintento. Ojo con la subclase de `atencion.py:163-194`, que sobreescribe `reiniciar()`
para reponer `hubo_adjunto`: los huecos **no** se reponen, se vacían.

**Por qué `_anotar_resultado`:** ya abre conexión, ya escribe `fallo_respuesta` y
`tocar_conversacion`, y ya tiene un `except Exception` que traga a propósito —para no
reventar un turno al que el paciente ya recibió su respuesta. El informe hereda esa
política: si su escritura falla, se pierde un caso y queda en `log.exception`; nunca se
rompe un turno.

**`HUMANO` también se acumula en `ctx.turno.huecos`, no se escribe dentro de la tool.**
Esto es lo que hace posible la regla de no duplicar de la sección 3: `escalar_a_doctores`
corre *durante* el turno, cuando todavía no se sabe si además hubo un hueco de
conocimiento. Si escribiera ahí mismo, escribiría a ciegas. Anotando en memoria, para
cuando `_anotar_resultado` vuelca todo ya se conoce el turno entero y se puede decidir:

```
  al final del turno, en _anotar_resultado:

     hay FALTA_DATO en huecos ?
        SI  ->  un caso falta_dato:*, con escalo += 1
        NO  ->  un caso humano:{motivo} propio
```

**Única excepción:** el relevo abierto a mano por el doctor (`relevo.activar`) ocurre
fuera de cualquier turno —el doctor aprieta el botón cuando quiere— y no tiene un
`ctx.turno` donde acumularse. Ese sí escribe en su propia transacción, donde ya hay `conn`
y no hay prisa.

---

## 7. El informe

### 7.1 Dónde corre

Una tarea de fondo **propia** en `runtime.py`, hermana de la que despacha recordatorios.
No dentro de la de relevos: esa no arranca sin Telegram, y ese error el proyecto ya lo
cometió una vez (no negociable 21).

### 7.2 Qué produce

Tres campos Pydantic con tope de caracteres. La brevedad se impone por estructura, no
pidiéndole al modelo que sea breve:

```python
class InformeDelCaso(BaseModel):
    que_paso:   str   # que ocurrio, en numeros y en idioma de clinica
    por_que:    str   # la causa concreta
    recomiendo: str   # la accion, nunca el contenido
```

Modelo: `MODELO_EVALUADOR` (`gpt-5.6-luna`, nano, $0.20 / $1.20 por millón), el mismo que
ya corre los evaluadores de guardrails.

### 7.3 Prohibición dura

**El informe describe el hueco y recomienda la acción. Jamás propone el contenido clínico
ni una cifra.**

Puede decir *"hay que crear la ficha (ortodoncia, precio), y los ejemplos sugieren que
preguntan por cuota mensual"*. No puede decir *"el precio de ortodoncia es $3.500.000"*.

Si pudiera, alguien lo aprobaría de un clic y habríamos metido una cifra alucinada a la
base de conocimiento por la puerta de atrás — exactamente lo que `sin_cifra_no_documentada`
existe para impedir.

### 7.4 Topes de gasto

1. Un caso se analiza **una vez**.
2. Se re-analiza solo si `contador >= informe_sobre * 5` **y** pasaron 7 días desde
   `informe_en`.
3. Techo diario de informes. Superado, los casos nuevos quedan sin informe redactado y se
   registra el aviso: un incidente masivo llena el informe, no la factura.
4. `MAXICARE_SIN_RESOLVER_ANALISIS=0` apaga el análisis **sin apagar la captura**.

### 7.5 Costo

```
  POR INFORME (ESTIMADO, no medido)
     entrada   ~600 tokens   instrucciones + huella + 5 ejemplos
     salida    ~120 tokens   tres frases con tope
     costo     ~0,00026 USD

  AL MES,  20 casos nuevos      ~0,005 USD
  AL MES, 200 casos (desastre)  ~0,05  USD

  Un solo turno de Daniela (gpt-5.6-terra, con historial) ~ 0,02 USD
  = 75 informes.
```

El costo no es el factor que decide nada aquí. Los ~600/~120 tokens son **estimación**, no
medición; se cierra leyendo `ctx.usage` sobre casos reales (ver 13).

---

## 8. La pantalla

El sidebar **ya tiene reservada** la sección `bandeja` (fase 6), hoy sirviendo
`Pendiente.tsx`. Se usa esa. La clave del hash no se toca; la etiqueta visible pasa a
**"Sin resolver"**.

Un solo endpoint de lectura: `GET /api/sin-resolver`, para cualquier sesión válida.

```
  +---------------------------------------------------------+
  |  Falta el precio de ORTODONCIA                           |
  |  7 veces  ·  del 15 al 27 de septiembre                  |
  +---------------------------------------------------------+
  |                                                          |
  |  QUE PASO      7 pacientes preguntaron el precio de      |
  |                ortodoncia. Daniela no lo tiene, asi      |
  |                que le pidio el dato al doctor 7 veces.   |
  |                                                          |
  |  POR QUE       Ese precio nunca se cargo al sistema.     |
  |                                                          |
  |  RECOMIENDO    Cargarlo. Ojo: 5 de los 7 preguntaron     |
  |                por la CUOTA MENSUAL, no por el total.    |
  |                                                          |
  |  Las 7 preguntas tal como llegaron  v                    |
  +---------------------------------------------------------+
```

**Lo que ve cada quien:**

```
  LOS DOS                           SOLO admin
  ============================      ==========================
  el informe, en idioma de          "detalle tecnico" desplegable:
  clinica, siempre                    nombre del guardrail
                                      ModelBehaviorError: ...
  "el sistema tuvo una falla y        id de conversacion, turno
   el paciente recibio un
   mensaje de espera"
```

El informe **nunca** muestra `ModelBehaviorError` en la cara del cliente. Un error técnico
crudo en la pantalla de una clínica genera una llamada de soporte que no debería existir.

---

## 9. Lo que NO cambia

| | |
|---|---|
| Latencia para el paciente | **cero** — se escribe después del envío |
| Guardrails, tripwires, reglas | **intactos** — el sistema solo mira |
| Comportamiento de Daniela | **idéntico** con esto encendido o apagado |
| El escalamiento al doctor | **igual** — sigue avisando como hoy |
| Quién escribe una ficha | **un humano**, por el panel, como hoy |

Apagar la tarea de fondo deja el turno de WhatsApp idéntico línea por línea. Lo único que
pasa es que el informe deja de llenarse.

---

## 10. Trampas heredadas

Esto es lo que va al `CLAUDE.md` al cerrar:

1. **`/clearstate` tiene que borrar los ejemplos**, y va antes del
   `DELETE FROM conversaciones` (no negociable 9). El caso vive, el contador no baja.
2. **Se ignora todo `fallo_respuesta` que empiece por `relevo:`** — no es un fallo
   (no negociable 15). Si no, cada relevo ensucia el informe.
3. **El chat web del panel no entra.** Escribe en `pruebas_web`; los experimentos del
   desarrollador no son casos reales.
4. **Nada dentro del candado ni del búfer** (no negociables 3 y 6). Todo se acumula en
   memoria y se vuelca en `_anotar_resultado`.
5. **La huella la arma el código, nunca el modelo.** Si el modelo pudiera escribirla, dos
   casos iguales saldrían con huellas distintas y la agrupación —que es todo el valor— se
   rompe en silencio.
6. **La huella de `ROTO` usa `type(e).__name__`, no el mensaje.** Con el mensaje, cada
   error es único y no agrupa nunca.

---

## 11. Entregable verificable

`scripts/probar_sin_resolver.py` — de punta a punta contra Neon, **sin gastar un token**
(el analista se dobla). Entra en la tabla de entregables del `CLAUDE.md`. Comprueba:

1. Un turno con `SIN DATO DOCUMENTADO` deja exactamente una fila con la huella esperada.
2. Siete turnos iguales dejan **una** fila con `contador = 7` y cinco ejemplos.
3. Un tripwire que se regeneró bien deja fila. *(Hoy no deja nada: es la regresión que
   este script existe para cazar.)*
4. Un `fallo_respuesta` que empieza por `relevo:` **no** deja fila.
5. El chat de `pruebas_web` no escribe en `public`.
6. `/clearstate` borra los ejemplos y **no** baja el contador.

Tests offline (`pytest -q`): cálculo de la huella por tipo, recorte a cinco ejemplos,
orden por ventana, filtro de `relevo:`, y que el `output_type` del informe respete sus
topes.

Test de Neon (`-m neon`): el `ON CONFLICT` bajo concurrencia — dos turnos simultáneos con
la misma huella no crean dos filas.

---

## 12. PENDIENTE

Lo que no se sabe, marcado como tal y no rellenado con un valor plausible:

- **PENDIENTE** — el costo real por informe. Los ~600/~120 tokens de 7.5 son estimación.
  Se cierra leyendo `ctx.usage` sobre al menos 10 informes reales.
- **PENDIENTE** — el techo diario de informes (7.4, punto 3). Depende del volumen real de
  la clínica, que hoy no está medido.
- **PENDIENTE** — la ventana de 30 días. Es la propuesta; se confirma con un mes de datos
  reales viendo si el informe queda legible o se vacía demasiado rápido.
- **PENDIENTE** — los topes de caracteres exactos de los tres campos del informe.
- **PENDIENTE** — el prompt del analista.
- **PENDIENTE** — si esta funcionalidad entra como fase propia del plan o dentro de una
  existente. Se trabaja acotada por ahora.

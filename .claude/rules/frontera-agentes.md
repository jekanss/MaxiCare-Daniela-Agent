---
paths:
  - "src/maxicare_daniela/**/*.py"
---

# La frontera agentes ↔ transporte

`agentes.py`, `herramientas.py` y `contratos.py` **nunca importan `runtime.py`**.
La dirección permitida es la contraria: `runtime.py` importa de los tres.

`runtime.py` es el único módulo que importa un framework de transporte (FastAPI, el
cliente de WhatsApp, Telegram) y el único donde aparece `Runner.run`.

Esto no es estética. Un `import` de FastAPI dentro de `herramientas.py` hace que esa
tool no se pueda probar sin levantar un servidor, y ata la lógica de negocio al canal
de hoy. `tests/test_estructura.py` lo verifica con un detector sobre el AST: si lo
rompes, falla ahí.

# El muro

`LecturaArchivo.tratamiento` es un `Literal` cerrado, **no un `str`**.

Ese tipo es lo que impide físicamente que una frase clínica —«se observa lesión
periapical en el 46»— llegue al agente que le habla al paciente. Con un `str` la
protección sería una instrucción en el prompt, que el modelo puede desobedecer. Con
un `Literal`, Pydantic rechaza el valor antes de que exista.

El contenido clínico viaja por `contexto_clinico`, y **su destino exclusivo es
Telegram**. Nunca se le describe a un paciente el contenido clínico de una imagen,
un video o una remisión.

`tests/test_contratos.py::test_tratamiento_no_admite_una_frase_clinica` es la prueba
que lo sostiene. No la debilites para que pase otra cosa.

# `identidad_antes_de_datos`: quién tiene datos que proteger

El guardrail frena `crear_cita`, `reprogramar_cita` y `cancelar_cita` sin identidad
verificada. **Con UNA excepción, y hay que entender por qué es una excepción y no un
agujero:** un teléfono que no tiene fila en `pacientes` SÍ puede crear su primera cita.

El plan justifica el guardrail con `datos.quien_ve_que`: «que el contexto de una cuenta y el
de un paciente no se mezclen cuando alguien escribe por un familiar». Eso presupone una ficha
que proteger. Quien no la tiene no tiene datos que otro pueda ver ni citas que otro pueda
mover, y bloquearlo no protegía a nadie: **dejaba a la clínica sin pacientes nuevos, que es
el objetivo del proyecto.**

Y el sistema se contradecía a sí mismo. `identificar_paciente` le responde al modelo, textual,
«Ese número no está registrado… **Puedes agendarle una cita nueva**», y el guardrail se lo
prohibía. Medido en producción el 13/09/2026, leído del historial del agente en Neon: el
paciente dio su nombre, Daniela llamó a `crear_cita`, el guardrail la frenó, regeneró, la
frenó otra vez, y el paciente recibió «te escribe el doctor». Que el diseño sí lo contemplaba
está en el esquema: `citas.paciente_id` es **NULLABLE** desde la migración 001 y la tabla
guarda `nombre_completo` y `telefono` por su cuenta.

Cuatro cosas sostienen que esto no se convierta en una puerta:

- **`telefono_sin_paciente` no lo dice el modelo.** Lo pone el código tras un
  `buscar_paciente_por_telefono` contra la base —`atencion._leer_estado`, con la lectura que
  ya hacía, y lo refresca `herramientas._identificar_paciente`—. Si el modelo pudiera
  escribirlo, bastaría con que dijera «soy nuevo».
- **`reprogramar_cita` y `cancelar_cita` NO entran**, tenga ficha el número o no: operan
  sobre citas que ya existen, y una cita existente sí puede ser de la persona a la que
  alguien está suplantando.
- **Es una lista blanca (`_ESCRITURAS_PARA_DESCONOCIDO`), no una negra.** La
  `condicion_revision` del plan dice que una cuarta tool de escritura hay que agregarla a
  mano; mientras nadie la agregue, cae del lado estricto. Hay prueba con una tool inventada.
- **Un número CON ficha y sin verificar sigue bloqueado**, que es exactamente el caso que el
  plan quiere frenar.

Y lo que impide que esa excepción deje a nadie encerrado: **`crear_cita` registra al
paciente** (`asegurar_paciente`, que llevaba escrito desde la fase 1 sin que ninguna tool lo
llamara). Sin eso, quien acababa de agendar no podía mover ni cancelar su propia cita
**jamás**: `identificar_paciente` solo verifica contra filas de `pacientes`, nadie creaba la
suya, y pedía cambiar la hora recibiendo «te escribe el doctor». El mismo callejón sin
salida, movido un paso más allá. Efecto secundario que importa igual: las citas dejan de
guardarse con `paciente_id = NULL`.

# La pertenencia de una cita se ancla al TELÉFONO

`reprogramar_cita` y `cancelar_cita` comprueban de quién es la cita con `_es_ajena`. Lo que
había era `if ctx.id_paciente is not None and cita["paciente_id"] not in (None,
ctx.id_paciente)`, con dos huecos: sin `id_paciente` no comprobaba **nada**, y una cita con
`paciente_id = NULL` valía para cualquiera. Ninguno se podía alcanzar mientras el guardrail
frenara a todo el que no tuviera ficha; permitir la primera cita de un desconocido volvía el
segundo alcanzable, porque esas citas pasaban a ser las normales.

Ahora manda el número desde el que se escribe, que es el que quedó guardado en la cita. **Que
el id de una cita sea un UUID no es un control de acceso:** es una cadena que el propio
sistema le mandó al paciente por WhatsApp. Se conserva la pertenencia por `paciente_id` para
quien cambia de número o tiene una cita que le abrió la clínica.

# Toda hora que la tool confirma tiene que quedar AUTORIZADA, incluida la vieja

`sin_hora_no_verificada` compara contra lo que las tools devolvieron **en este turno**, y
`ctx.turno` se vacía en cada turno. Dos consecuencias que costaron reprogramaciones reales:

- **`reprogramar_cita` devuelve las DOS horas**, la anterior y la nueva. Confirmar un cambio
  exige decir de dónde a dónde, y la hora vieja ya no está autorizada por el turno en que se
  agendó. La hora vieja sale de `leer_cita` —de la base, en este turno—, así que autorizarla
  es el mismo criterio de siempre, no una excepción.
- **`cancelar_cita` devuelve la hora que cancela**, por lo mismo: «tu cita del martes a las 2
  quedó cancelada» es la frase natural.

En los dos casos el fallo era el peor posible, porque **la escritura ya había ocurrido**: la
cita movida o cancelada en Neon y en Calendar, y el paciente recibiendo el mensaje seguro.
Se presentaba a la hora vieja, a un cupo que el sistema acababa de liberar.

**Y el guardrail tenía su propio bug de lectura: «2:00 pm» valía 02:00.** La primera
alternativa de `_HORA` casaba `2:00` y dejaba el `pm` fuera del match, así que la rama de 12
horas no lo veía nunca. Solo fallaba con minutos —«2 pm» siempre estuvo bien— y bastaba para
bloquear cualquier confirmación de una cita de tarde escrita como habla la gente. Es
literalmente la `condicion_revision` que el plan le puso a ese guardrail: «si bloquea
mensajes legítimos hay que afinar la extracción, no quitar el guardrail».

# `consultar_citas`: la décima tool, y por qué no está en el plan

Las nueve del plan dan por supuesto que el id de una cita viaja en la conversación. Dentro de
una conversación es cierto; el problema es que **las conversaciones de WhatsApp mueren a las
24 horas** (`conversacion_viva`), y `reprogramar_cita` y `cancelar_cita` son las dos únicas
tools cuya entrada obligatoria —el UUID— no puede salir de ninguna otra. El paciente que
agendaba el lunes y escribía el miércoles pedía algo que Daniela no tenía forma de encontrar:
se salvaba solo si subía en su chat y copiaba el código a mano.

Tres decisiones que hay que respetar si alguien la toca:

- **No recibe ningún argumento.** El teléfono sale de `ctx.telefono_completo`. Eso es lo que
  la hace incapaz *por construcción* de devolver la cita de otra persona: no hay nada que el
  modelo pueda torcer. Es la misma regla que deja a `SolicitudCita` sin campo de teléfono.
- **Lleva `identidad_antes_de_datos` aunque solo lea**, y **no** entra en
  `_ESCRITURAS_PARA_DESCONOCIDO` —esa lista blanca sigue teniendo una sola tool—. No estorba
  el caso normal: desde que `crear_cita` registra al paciente, todo teléfono con cita tiene
  ficha, y con ficha `atencion._leer_estado` da la identidad por verificada sola. Lo único
  que deja fuera son las citas anteriores a ese arreglo, que tampoco se pueden mover.
- **Autoriza las horas que nombra**, como `reprogramar` y `cancelar`. Sin eso, el paciente
  que solo pregunta cuándo es su cita recibe «te escribe el doctor».

Filtra por teléfono y no por `paciente_id` —el criterio más estrecho de los dos— y corta el
pasado con `ctx.ahora`: una cita de ayer no se puede mover, y ofrecerla solo sirve para que
el modelo proponga algo imposible.

# El límite clínico: qué va en el prompt y qué va en la base

MaxiCare pidió el 13/09/2026 acotar hasta dónde llega Daniela en lo clínico, después de leer
una conversación de prueba. El esqueleto del plan lo resolvía en una frase —«NUNCA le dices a
un paciente qué tiene»— y esa frase no cubre las tres preguntas con las que la gente llega de
verdad, porque **ninguna de las tres pide un diagnóstico**:

- «Uno me dijo periodontitis y otro que con una limpieza quedaba bien, ¿quién tiene razón?»
- «¿Ustedes creen que de pronto se puede salvar?»
- «Si toca sacarla, ¿cuánto vale?»

Pide arbitrar, pronosticar y ponerle precio a un tratamiento que nadie le ha indicado. Ningún
guardrail las frena: no hay cifra sin respaldo, no hay hora sin verificar, y
`sin_lectura_clinica` no dispara porque técnicamente no le está diciendo qué tiene.

**El reparto, y es la línea que no hay que cruzar:**

| | Dónde vive | Por qué |
|---|---|---|
| La conducta —qué reconoce, qué no decide, cómo responde— | el prompt | es política, no medicina |
| El criterio clínico —qué síntoma es una alarma, qué se hace con él | la base de conocimiento | es medicina, y solo MaxiCare la firma |

Por eso el prompt manda a consultar `_general` / `urgencias` y a aplicar **exactamente** lo
que devuelva, y no enumera ni una señal por su cuenta. Copiar esa lista al prompt sería meter
criterio clínico sin aprobar por la puerta de atrás, y quedaría fuera del alcance de la
pantalla desde la que la clínica corrige lo que dice.
`test_el_protocolo_de_alarma_sale_de_LA_BASE_y_no_del_prompt` lo sostiene.

## Los dos defectos que solo se vieron contra el modelo real

Las pruebas offline comprueban que la instrucción sigue escrita. No pueden ver esto, y
`scripts/probar_agentes.py` bloque 11 existe para eso:

- **Un estado de alarma que se enciende y no se apaga.** La primera redacción decía «mientras
  eso siga abierto sueltas el guion comercial» y no decía cómo se cierra. El paciente escribió
  «me sangran bastante las encías», Daniela activó el protocolo en el turno 1 y siguió en él
  los cinco siguientes: cinco escalamientos, cinco «lo estoy revisando con prioridad», cero
  horarios ofrecidos, incluso después de que el paciente dijera «no, nada de eso». Y encima
  contradecía el protocolo aprobado, que para esas señales dice **buscar el cupo más
  cercano** — ahí agendar es parte de la respuesta, no lo que se suspende.
- **Un límite clínico no es un escalamiento.** Daniela escalaba cada vez que topaba con el
  límite, y en una conversación así el límite aparece en casi todos los turnos. Tres alertas
  de Telegram por un solo paciente es como se pierde la que sí importaba. La respuesta a un
  límite clínico ya la tiene: es la valoración.

Los dos se arreglaron en el prompt y los dos tienen prueba. Ninguno era visible en verde.

## Las señales de alarma NO comparten una sola conducta

MaxiCare amplió la fila el 13/09/2026 y la respuesta no fue una lista más larga: fue **dos
conductas distintas**, y la diferencia importa.

| Señal | Qué hace Daniela |
|---|---|
| dolor severo · sangrado activo · fiebre · inflamación en la cara | cupo más cercano + avisar al equipo |
| **dificultad para respirar o para tragar** | **NO ofrecer cita** · urgencias médicas + avisar al equipo |

Por eso el prompt dice «qué pasa con la cita **lo decide el protocolo y no tú**» y no «agendar
no se suspende», que fue la primera redacción. Esa frase, dicha a secas, manda a la clínica a
alguien que tiene que ir a un hospital: es el único punto de todo este ajuste donde una
palabra de más tiene consecuencia clínica directa.
`test_la_senal_de_alarma_se_comprueba_una_vez_y_tiene_salida` lo fija.

Y es la demostración de por qué el reparto de arriba es el correcto: MaxiCare cambió el
criterio clínico **sin que cambiara una línea del prompt sobre qué señales existen**. Solo
hubo que quitarle al prompt una afirmación que se había adelantado al protocolo.

## La valoración ya tiene tarifa

Estuvo `aprobado: false` hasta el 13/09/2026 —el documento maestro no traía tarifa universal,
y la fila mezclaba eso con «en periodoncia se reporta entre $80.000 y $150.000»—, así que
Daniela no podía cerrar ninguna conversación con el precio de la cita que estaba ofreciendo.
MaxiCare fijó **$50.000, abonables a cualquier tratamiento posterior**, lo que además responde
la segunda mitad de la nota vieja: quien solo asiste a consulta paga esos $50.000.

# Convenciones del paquete

- `SolicitudCita` **no tiene campo de teléfono**: la tool lo lee del contexto local.
  Si el modelo pudiera escribirlo, podría escribir uno distinto —la hija agendando
  para la mamá— y el recordatorio saldría al número equivocado.
- Toda tool de escritura lleva clave de idempotencia, construida por el orquestador
  antes de llamarla.
- «Horario lleno» es un RESULTADO de la tool con alternativas, nunca un error de
  validación: un error hace que el modelo reintente a ciegas hasta `MaxTurnsExceeded`.

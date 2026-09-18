# Las tres plantillas de reactivacion, para pedirle a Meta

Para quien las crea en WhatsApp Manager. 17 de septiembre de 2026.

Cada campo de aqui esta puesto a proposito. **Cambiar una plantilla ya aprobada exige otra
aprobacion de Meta**, asi que no se improvisa ninguno: lo que se apruebe mal se espera dos
veces.

Lo que el codigo manda es **solo el componente `body`**, con los huecos posicionales
(`{{1}}`, `{{2}}`...) en el orden del texto aprobado. Un quick reply lleva su carga util
dentro de la propia plantilla, asi que el emisor no rellena nada de los botones
(`canales.WhatsApp.enviar_plantilla`).

---

## Las seis trampas que ya se pagaron con `recordatorio_cita`

No hace falta volver a descubrirlas.

1. **El texto NO puede terminar en una variable.** Meta lo rechaza, y el punto final no le
   basta. Por eso las tres de abajo cierran con una frase.
2. **El Header existe a la fuerza en cuanto lo tocas.** Si escribes algo en el Header y luego
   lo borras, Meta rechaza el envio con «component of type HEADER is missing expected
   field(s) (text)». O se deja con texto FIJO, o no se toca nunca. Aqui va con texto fijo.
3. **«Quick reply» no aparece con ese nombre en la consola: se llama *Custom*.**
4. **El idioma es Spanish, que es el codigo `es`** -- no una variante regional. Una plantilla
   creada como `es_CO` y llamada con `es` devuelve el error 132001 y no se manda nada.
5. **El *message validity period* por defecto son 10 minutos.** Con eso, un telefono apagado
   pierde el mensaje para siempre: el sistema marca ANTES de enviar y no reintenta. Subelo a
   lo mas alto que deje la consola.
6. **El nombre va en minusculas, con guiones bajos y sin tildes.** Meta no acepta otra cosa.

---

## Por que estas tres son categoria **Marketing** y no Utility

`recordatorio_cita` paso como *Utility* porque detras hay una cita real agendada: es una
notificacion sobre una transaccion que existe.

Un «hace unos dias nos escribio, ¿sigue interesado?» no tiene ninguna transaccion detras.
Meta lo clasifica como **Marketing**, y pedirlo como Utility solo consigue que lo reclasifique
o lo rechace -- y una vuelta mas de espera.

Esto **no contradice** la lectura de la politica de datos, que trata la reactivacion como
*seguimiento* de una solicitud que abrio el propio paciente (§6 y §12). Son dos etiquetas de
dos jurisdicciones distintas sobre la misma cosa: «seguimiento» para la Ley 1581, «marketing»
para Meta. Conviene saberlo antes de ver la categoria en la consola y sorprenderse.

Consecuencia practica: **un mensaje de categoria Marketing cuesta mas que uno Utility.**

---

## Lo que comparten las tres

| Campo | Valor |
|---|---|
| Categoria | **Marketing** |
| Idioma | **Spanish** (codigo `es`) |
| Variables en el body | **una sola**: `{{1}}` = primer nombre del paciente |
| Footer | vacio |
| Botones | dos, tipo *Custom* (quick reply) |
| Message validity period | lo mas alto que permita la consola |

**Por que una sola variable, y por que NO lleva el tratamiento:** lo decidio marketing el
15/09/2026, y con razon. El nombre del tratamiento es un dato de salud, y una notificacion de
WhatsApp se lee en la pantalla de bloqueo, a la vista de cualquiera que tenga el telefono
delante. «Ortodoncia» o «endodoncia» ahi es una filtracion. Por eso los textos dicen «su
consulta» y «su cita», nunca de que.

---

## 1 · `reactivacion_sin_agendar`

La mas importante de las tres: es el grueso del volumen. Alguien pregunto, no dijo que no, y
se le fue.

| | |
|---|---|
| **Nombre** | `reactivacion_sin_agendar` |
| **Categoria** | Marketing |
| **Idioma** | Spanish (`es`) |
| **Header** | Texto fijo: `Su consulta en MaxiCare` |
| **Body** | `Hola {{1}}, hace unos dias nos escribio a MaxiCare y quedo pendiente agendar su cita. Si aun le interesa, con gusto le ayudamos a encontrar un horario.` |
| **Ejemplo de `{{1}}`** | `Maria` |
| **Footer** | vacio |
| **Boton 1** (Custom) | `Si, me interesa` |
| **Boton 2** (Custom) | `Ya no, gracias` |

## 2 · `reactivacion_cancelada`

Agendo, cancelo, y no volvio a pedir fecha.

| | |
|---|---|
| **Nombre** | `reactivacion_cancelada` |
| **Categoria** | Marketing |
| **Idioma** | Spanish (`es`) |
| **Header** | Texto fijo: `Su cita en MaxiCare` |
| **Body** | `Hola {{1}}, vimos que cancelo su cita en MaxiCare y no ha vuelto a agendar. Si lo desea, le ayudamos a buscar una nueva fecha.` |
| **Ejemplo de `{{1}}`** | `Maria` |
| **Footer** | vacio |
| **Boton 1** (Custom) | `Si, reagendar` |
| **Boton 2** (Custom) | `Ya no, gracias` |

## 3 · `reactivacion_no_asistio`

Tenia cita y no llego.

| | |
|---|---|
| **Nombre** | `reactivacion_no_asistio` |
| **Categoria** | Marketing |
| **Idioma** | Spanish (`es`) |
| **Header** | Texto fijo: `Su cita en MaxiCare` |
| **Body** | `Hola {{1}}, notamos que no pudo asistir a su cita en MaxiCare. Si desea reprogramarla, con gusto le buscamos otro horario.` |
| **Ejemplo de `{{1}}`** | `Maria` |
| **Footer** | vacio |
| **Boton 1** (Custom) | `Si, reprogramar` |
| **Boton 2** (Custom) | `Ya no, gracias` |

---

## Sobre los acentos

Los textos de arriba van **sin tildes a proposito en este archivo**, para que la consola de
Windows pueda imprimirlo. **Al pegarlos en Meta hay que escribirlos con sus tildes**:
«dias» → «dias» con tilde en la i, «cancelo» → «canceló», «Si,» → «Sí,», «Maria» → «María».
Meta acepta tildes sin ningun problema -- `recordatorio_cita` las lleva.

## Que significa cada boton

- **El boton afirmativo** abre la conversacion: el paciente entra por la puerta normal de
  WhatsApp y Daniela sigue desde ahi, con su agenda delante.
- **`Ya no, gracias`** cierra **ese seguimiento y solo ese**. La persona sigue siendo
  contactable en el futuro. Decidido asi el 17/09/2026: «ya no me interesa esta consulta» no
  es «no me escriban nunca mas», y tratarlo como baja total quema a un paciente por una frase
  que no dijo. Quien quiera la baja completa se la puede pedir a Daniela, y eso ya funciona.

Un aviso que ya costo caro y que aqui **ya esta resuelto**: el rotulo de un quick reply llega
en `button.text`, no en `text.body`, y el evaluador de uso indebido llego a leer «Confirmar»
como una inyeccion. Los dos fallos se arreglaron el 15/09/2026 (no negociable 23). El camino
de los botones esta probado de punta a punta en produccion; estas tres no vuelven a pagarlo.

---

## Cuando Meta las apruebe

1. Avisar con los tres nombres exactos tal como quedaron.
2. Van al `.env` **y al del VPS**, no solo al local.
3. **No hace falta ampliar nada de codigo: el despachador ya sabe mandar las cuatro.**
   `seguimientos.despachar` elige la plantilla por TIPO (`plantillas[fila["tipo"]]`) y
   `parametros_de` arma un hueco para las de reactivacion y cuatro para la de recordatorio.
   Un tipo sin plantilla configurada no se pierde ni se anula: la fila se queda PENDIENTE
   hasta que exista, que es el modo de comprobacion de hoy. El motivo `sin_plantilla` ya no
   existe; lo que queda es `sin_cita`, y es otra cosa -- una fila que no es reactivacion y
   llega sin `cita_inicio`, o sea sin nada con que rellenar sus huecos de fecha y hora.
   (Este punto decia lo contrario hasta la revision final: era cierto antes de `823fd0f` y
   el riesgo de dejarlo era que alguien "lo ampliara" otra vez sobre algo ya hecho.)
4. `scripts/probar_plantilla.py` manda UNA de verdad y traduce los errores de Meta. Un rechazo
   de Meta no se cobra.

## Que NO hacer

- **No pedirlas como Utility** para que salgan mas baratas. Meta las reclasifica o las
  rechaza, y se pierde la vuelta.
- **No meterle el tratamiento a la plantilla**, por mas que ayude a la conversion. Es un dato
  de salud en una pantalla de bloqueo.
- **No anadir un tercer intento ni una cuarta plantilla** sin volver a la conversacion de la
  politica: dos intentos y un limite corto es justo lo que sostiene que esto sea *seguimiento*
  y no publicidad.
- **No dejar el Header a medias.** Ver la trampa 2.

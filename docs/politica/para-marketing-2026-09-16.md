# Cómo quedó el permiso para volver a escribirle a un paciente

Para marketing y para quien redactó la Política de Tratamiento de Datos (versión 2.0,
septiembre de 2026). 16 de septiembre de 2026.

Esto no pide una decisión técnica. Pide que alguien confirme una lectura de la política antes
de que la reactivación salga a producción.

> **Estado al 21/09/2026: ES AHORA.** El código de reactivación se fundió y se desplegó ese
> día, **callado**: los tres nombres de plantilla están vacíos en el servidor y con eso no
> sale ni un mensaje. Ponerlos es una línea, y es lo que esta confirmación bloquea. La
> decisión de MaxiCare era mandar este memo justo antes de salir a producción, y el momento
> llegó. El resto de lo que falta para encender está en
> [`../antes-de-produccion-reactivacion.md`](../antes-de-produccion-reactivacion.md).

## Qué quedó construido

Daniela ya distingue tres cosas que antes eran una sola:

1. **El aviso de la política.** En el primer mensaje que recibe cualquier número nuevo,
   Daniela añade una línea al final de su respuesta: «Al continuar aceptas nuestra política de
   tratamiento de datos», con el enlace. Sale **una sola vez en la vida de ese número** —no
   una vez al día ni una por conversación— y queda registrado con la fecha y la versión exacta
   del documento que esa persona vio. Lo escribe el sistema palabra por palabra, siempre igual;
   no lo redacta Daniela, justamente para que sirva como prueba.

2. **La baja.** Si alguien pide que no le escriban más, aunque lo diga de pasada, queda
   anotado de forma permanente y el sistema deja de programarle cualquier mensaje comercial.
   Se puede revocar si la persona vuelve a pedirlo. Cada uno de esos movimientos queda en una
   bitácora que no se puede borrar ni modificar: es lo que se enseña si alguien reclama.

3. **La cita, que no es lo comercial.** El recordatorio de una cita agendada **sigue
   llegando** aunque la persona esté dada de baja. Son cosas distintas y el sistema las
   separa por dentro, no por criterio de nadie: la baja apaga lo comercial y no puede apagar
   un recordatorio.

## ✅ RESPONDIDA el 21/09/2026: es SEGUIMIENTO

**Natalia Peñuela confirmó la primera lectura: la reactivación es seguimiento de una solicitud
iniciada por el titular (§6 y §12), no comunicación comercial (§5 y §10).** Lo construido
cumple tal como está; no hace falta un «sí» expreso antes del primer mensaje.

**Lo que esa respuesta exige a cambio, y no es retórica:** un seguimiento se distingue de la
publicidad en que la persona puede oponerse y esa oposición se respeta. El derecho a oponerse
es lo que sostiene la respuesta. Por eso el camino de la baja —el botón «Ya no, gracias» y
todo lo que cuelga de él— dejó de ser una comodidad y pasó a ser **la condición de la que
depende que esta confirmación siga siendo cierta**.

Lo de abajo se conserva tal como se envió, porque es lo que se preguntó y sobre lo que se
respondió.

## La pregunta que hay que confirmar

El permiso para **recontactar** a alguien que preguntó y no agendó quedó implementado como un
**aviso con derecho a oponerse**: se informa, y se le escribe salvo que diga que no. No se le
pide un «sí» expreso.

Se hizo así para no anteponer un bloque legal a la primera respuesta de cada paciente: quien
escribe a una clínica a las once de la noche preguntando cuánto vale una limpieza abandona la
conversación si lo primero que recibe es una casilla de autorización.

En la política publicada eso encaja con dos puntos, y choca de frente con un tercero:

| Encaja con | Dice |
|---|---|
| §6 | «MaxiCare diferencia el seguimiento relacionado con una solicitud iniciada por el titular de las comunicaciones promocionales generales.» La exigencia de habilitación previa que viene después está escrita sobre las **comunicaciones comerciales o publicitarias**, no sobre ese seguimiento. |
| §12 | «Las reglas de reactivación de Daniela operan de forma separada y con límites propios.» |

| Choca con | Dice |
|---|---|
| §5 | Entre las finalidades: «recontactar al titular respecto de una solicitud u oportunidad que haya quedado abierta, **cuando exista autorización** y resulte procedente». |
| §10 | «El silencio o la falta de acción no se interpretarán como autorización.» |

Las dos lecturas son defendibles y la diferencia no es de matiz:

- **Si la reactivación es «seguimiento de una solicitud que el propio titular abrió»**, lo
  construido es correcto tal como está y no hay nada que cambiar.
- **Si la reactivación es «comunicación comercial»**, entonces hace falta un sí expreso antes
  del primer mensaje de reactivación, y hay que construirlo: hoy no existe.

**Quien redactó la política es quien tiene que decidir cuál de las dos es.** No es una decisión
que pueda tomar quien programa, porque las dos se implementan igual de bien y solo una cumple.

**Ojo, que esta frase cambió el 21/09/2026 y decía lo contrario.** Hasta ese día decía «no hay
riesgo: la reactivación todavía no está construida». Ya lo está, y está desplegada. Lo que
impide que salga un solo mensaje no es que falte código: es que los tres nombres de plantilla
están vacíos en el servidor, y rellenarlos es una línea. Por eso esta confirmación pasó de ser
un pendiente a ser lo único que falta.

Lo que sí está construido, funcionando y sin depender de esta respuesta es **la baja**, que es
la mitad que protege al paciente.

## El enlace de la política

Queda apuntando al PDF en Google Drive, que es lo que se entregó. Funciona: se comprobó que
abre sin necesidad de iniciar sesión.

Hay un riesgo que conviene conocer aunque hoy no moleste. **Drive permite subir una versión
nueva encima del mismo archivo sin que el enlace cambie.** El día que eso pase, todas las
personas que aceptaron la versión anterior quedarían apuntando a un texto que nunca leyeron, y
eso es exactamente lo que hay que poder acreditar si alguien reclama.

Como paño de agua tibia, el sistema guarda su propia copia congelada del PDF tal como estaba
hoy, con una huella digital que permite comprobar si el documento cambió. Sirve, pero es una
defensa nuestra, no de MaxiCare.

**Lo que resolvería el problema de raíz:** servir la política desde una dirección del dominio
de MaxiCare, con una copia por versión que nunca se sobrescriba (por ejemplo
`maxicarecol.com/politica/2026-09.pdf`, y el año que viene una `2027-xx.pdf` al lado, no
encima). Cuando exista, es un cambio de una línea.

## Un punto pendiente, y este sí es de MaxiCare

Cuando alguien pide la baja, el sistema guarda **la frase con la que la pidió, tal cual**, en
la bitácora que no se puede borrar. Sirve para acreditar qué pidió exactamente y cuándo.

El problema es que esa bitácora está protegida contra cualquier borrado, a propósito, y por
diseño **no existe hoy ninguna forma de retirar esa frase** —ni siquiera para la propia
clínica—. La política publicada, en cambio, reconoce en §13 el derecho del titular a
«solicitar la supresión de datos».

Hay dos salidas, y las dos son baratas:

- guardar solo un motivo de una lista cerrada en vez de la frase libre; o
- permitir borrar la frase dejando intactos el hecho, la fecha, el origen y la versión, que es
  lo que la ley pide poder acreditar.

Lo que no conviene es dejarlo como está sin haberlo decidido a la vista.

## Lo que no se hizo, y por qué

**Menores de edad.** Se pidió por escrito el 15/09/2026 citando la Ley 1581, y se pospuso a
conciencia. La razón: MaxiCare prohibió expresamente registrar cédulas o documentos de
identidad, así que lo único que Daniela podría hacer es **adivinar** la edad por cómo escribe
alguien. Eso falla hacia los dos lados —le corta la atención a una adulta que escribe informal—
y falla de forma intermitente, que es la peor clase de fallo: en una demostración funciona.

La política ya lo contempla en §11 como una conducta («cuando se identifique que quien
interactúa es menor, pedir la intervención de su representante»). Construirlo requiere decidir
antes **cómo se identifica**, y esa decisión no se ha tomado.

# La cita que Daniela no podía crear, y el General inundado

Origen: la conversación de Sora Patricia Delgado (+573196842471), 16/09/2026 15:44–15:50.
Diagnóstico completo en el hilo; aquí solo lo que hay que hacer.

## Lo que la evidencia dice

Cero guardrails. Cuatro escalamientos: turnos 2 y 5 por la tool, turnos 3 y 4 por la red de
seguridad de fin de turno. La ficha quedó `None` (el arreglo de la ficha en blanco funcionó).

El quinto escalamiento lo explica todo, en palabras del propio modelo:

> «Solicitó revisión mañana a las 9:00 am; el bloque está libre. No hay tratamiento
> determinado y la agenda requiere una clave de tratamiento para reservar.»

Tenía nombre, cupo y permiso. Le faltaba una clave con la que agendar.

## Arreglo A — `valoracion` entra al catálogo

`migraciones/020_valoracion.sql`, con el estilo de la 008: `INSERT ... ON CONFLICT DO NOTHING`.
`inicializar_base.py` la aplica a `public` y a `pruebas_web` sin tocar el script.

Cierra el cabo suelto que dejó abierto la **015**, cuyo encabezado dice que `"valoracion"`
«no es ninguna de las catorce claves de `tratamientos`». Entonces se resolvió preguntándole al
doctor en texto libre. Ahora la clave existe, y el texto libre del relevo sigue igual: son dos
vías distintas para dos momentos distintos.

También entra en el `Literal` de `contratos.Tratamiento`. Las dos mitades importan:

- La tabla manda en `SolicitudCita` y `registrar_estado_oportunidad` (vocabulario vivo).
- El `Literal` es el arranque de `_VOCABULARIO` antes de que `runtime.fijar_vocabulario` lea
  la base — o sea las pruebas offline, los scripts y cualquier arranque en el que la consulta
  falle. Sin él, `probar_agentes.py` correría con un vocabulario distinto al de producción.

Su propio docstring pide esa sincronía. Coste aceptado y declarado: `LecturaArchivo` podrá
clasificar un documento como `valoracion`. Para una remisión que pide valoración es correcto;
para una radiografía el lector seguirá teniendo `no_identificado`.

## Arreglo B — el prompt tiene que mandarla a agendar, no a escalar

**Esto NO es lo que se aprobó como «opción 1», y el cambio es deliberado.**

Lo aprobado era meter `no_identificado` en la lista de agendables. Con `valoracion` en el
catálogo eso deja a Daniela con dos claves para la misma situación, y la peor de las dos es la
que el sistema usa cuando *no sabe*. Se le da una sola: `valoracion`. `no_identificado` sigue
fuera de la lista ofrecida, que es donde lo puso `agentes.py:347` a propósito.

El defecto real no era solo la clave que faltaba. Era esta colisión en el prompt:

- «Si el paciente pregunta por algo que no está en la lista, no lo ofrezcas: **escala**.»
- «Un límite clínico no es un escalamiento… **para eso está la valoración**, y esa respuesta
  ya la tienes.»

La segunda frase nombra la valoración como la salida, y la primera la manda a escalar porque
no había clave con la que darla. Daniela obedeció la que podía cumplir.

Cambia: agendar una valoración cuando el tratamiento no está determinado es la respuesta
normal, no un escalamiento. Se escala cuando falta un DATO, no cuando falta un diagnóstico —
que es justo para lo que sirve la cita.

## Arreglo C — un escalamiento por asunto, y que no dependa del modelo

Causa raíz en `conversacion.py:441-449`:

```python
if resultado.escalado_por is None and resultado.respuesta.requiere_escalamiento:
    ...
if resultado.escalado_por is not None and al_escalar is not None:
    await al_escalar(...)
```

`requiere_escalamiento` se **lee como un flanco** («avisa ahora») y el modelo lo **emite como
un estado** («esto sigue necesitando a un humano»). Mientras el asunto siga abierto lo deja en
`true`, y cada turno se convierte en un Telegram nuevo al General, porque la clave de
idempotencia es por turno — y eso es correcto y no se toca (`escalamiento:{turno}`; con la
clave congelada el doctor se enteraría del primer escalamiento y de ninguno más).

El prompt ya dice «Escalas una vez por asunto, no una vez por mensaje» (`agentes.py:170`) y no
bastó. Mismo precedente que el no negociable 25: la guarda va en el código, no en la obediencia
del modelo.

`persistencia.escalamiento_vivo_con_motivo(conn, id_conversacion, motivo) -> bool` — cierto
cuando ya hay uno **entregado** (`telegram_message_id IS NOT NULL`) y **sin responder**
(`respondido_en IS NULL`) con ese mismo motivo. `_registrar_escalamiento` lo consulta y
devuelve `None`.

Las tres condiciones, cada una cerrando un agujero distinto:

- **Entregado** — nunca por la fila sola. Es la regla que ya sostiene
  `escalamiento_pendiente_de_aviso`: un aviso que no salió no es un aviso duplicado, y quemar
  la clave al INTENTAR deja al paciente «escalado» en una tabla que nadie mira.
- **Sin responder** — si el doctor ya contestó, el asunto se cerró y lo que venga es nuevo.
- **Mismo motivo** — un `dato_faltante` detrás de un `clinico` es otro asunto y pasa. En el
  caso medido, esto habría borrado los mensajes 620 y 622 y conservado el 611 y el 624.

No toca a la tool. `escalar_a_doctores` escribe su fila y manda su propio Telegram con un
resumen de verdad; `_avisar_a_doctores` corre después, encuentra la clave usada y ya registra
«ya estaba escalado Y avisado». Lo único que esto silencia es la red de seguridad repitiendo
un aviso que el doctor tiene delante sin responder.

## Archivos

| # | Archivo | Qué |
|---|---|---|
| A | `migraciones/020_valoracion.sql` | nuevo |
| A | `src/maxicare_daniela/contratos.py` | `valoracion` en el `Literal` |
| B | `src/maxicare_daniela/agentes.py` | la regla de la valoración |
| C | `src/maxicare_daniela/persistencia.py` | `escalamiento_vivo_con_motivo` |
| C | `src/maxicare_daniela/runtime.py` | `_registrar_escalamiento` la consulta |
| — | `tests/` | A: vocabulario · C: cuatro casos |
| — | `CLAUDE.md`, `.claude/rules/` | lo que hay que recordar |

## Pruebas de C, y por qué cuatro

1. Dos turnos seguidos con el mismo motivo → **un** Telegram.
2. Motivo distinto en el segundo → **dos**.
3. El primero ya respondido → el segundo pasa.
4. El primero con `telegram_message_id` NULL → el segundo pasa (falso positivo de la 1: sin
   este caso, alguien «simplifica» la consulta quitando el `IS NOT NULL` y la suite sigue
   verde mientras el agujero de «se quemó al intentar» vuelve a abrirse).

## Lo que apareció por el camino, y no estaba en este plan

Tres cosas. Ninguna se buscó; las tres bloqueaban poder afirmar que lo de arriba funciona.

**1. La suite offline llevaba doce horas en rojo, sola.** `uv run pytest -q` sobre `main`
limpio daba 12 fallos en `test_herramientas.py` desde la medianoche: `INICIO` apuntaba al
15/09 y `ContextoDaniela.ahora` cae al reloj real si nadie se lo da, así que una hora
«ocupada» pasó a ser una hora «pasada» y doce pruebas empezaron a medir otra cosa. Tercera
vez en el proyecto. Se clava `AHORA` en `tests/test_herramientas.py`. Sin esto no había
línea base verde contra la que comparar nada.

**2. Dos pruebas pasaban por el motivo equivocado.** `test_un_turno_que_ya_escalo_no_manda_
un_segundo_telegram` y `test_un_escalamiento_YA_avisado_no_se_reintenta` afirmaban «no sale
Telegram» y lo conseguían porque `ConexionFalsa` no tiene `cursor`: la consulta real lanzaba
`AttributeError`, `_avisar_a_doctores` se lo tragaba, y no salía nada. La deduplicación que
decían probar no se ejecutaba nunca. Estaba así desde antes de esta rama; salió porque el
arreglo C añade una consulta más al mismo camino.

**3. Callar un aviso hacía mentir a la pantalla del cliente.** `SinResolver.tsx` imprime
«Se interrumpió al doctor N de M veces». Con el arreglo C, tres turnos escalados producen un
solo Telegram — pero `escalamiento_real` seguía contando tres. Es la misma mentira que
`atencion.py` ya evita en el camino del reventón, entrando por otra puerta. Se cierra
haciendo que `_avisar_a_doctores` devuelva si de verdad interrumpió a alguien y que eso
viaje en `Resultado.doctor_avisado`. De paso queda tapado un caso que ya existía: un Telegram
que Telegram rechaza tampoco es una interrupción.

## Verificación

- `uv run pytest -q`
- `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`
- `uv run python scripts/inicializar_base.py` (aplica la 020 a los dos esquemas)
- `uv run python scripts/probar_tools.py` — dobla firmas a mano y `pytest` no lo corre
- `scripts/probar_agentes.py` **gasta**: solo si el usuario lo pide

---
paths:
  - "tests/**/*.py"
---

# Las pruebas de este proyecto

## Las dos suites, y por qué están separadas

```
uv run pytest -q                                  offline · ~2 s · SIEMPRE verde
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon   contra Neon · ~50 s · bajo demanda
```

Una prueba que necesita internet es una prueba que alguien acaba saltándose el día que
tiene prisa. Por eso **lo que toca la base lleva `@pytest.mark.neon`** y se salta sin la
variable de entorno. No le quites el marcador a una prueba para «que corra siempre».

Las de Neon escriben en el esquema `pruebas`, nunca en `public`, donde hay pacientes
reales. El aislamiento es físico: la conexión lleva `search_path=pruebas`. Y usa la
conexión **directa** —sin `-pooler.` en el host—, porque el pooler de Neon rechaza
`options` como parámetro de arranque y además comparte sesiones, lo que falsearía
cualquier medición de concurrencia.

## La suite de Neon es el punto ciego, y ya cobró

Como solo corre bajo demanda, un cambio puede dejarla en rojo y nadie enterarse. Pasó el
13/09/2026: la rejilla aprendió el horario de la clínica y `_hora_libre(n)` de
`test_tools_neon.py` —que era `ahora + 30 días + N horas`— empezó a devolver horas de
madrugada. **Seis pruebas en rojo, ninguna hablando de horarios**, y vivieron así varias
horas mientras `uv run pytest -q` seguía verde. El mismo defecto se había arreglado ya en
`scripts/probar_tools.py::hora` el mismo día; aquí no, porque nadie volvió a correr `-m neon`.

**Quien toque la rejilla, la jornada o el reloj corre también `-m neon`.** Tarda tres
minutos y medio. Los dos helpers cuentan ahora **bloques hábiles**, no horas de reloj: un
`_hora_libre(20)` no son veinte horas después.

## Una fecha clavada envejece, y a la tercera le tocó a la suite offline

El 16/09/2026, sin que nadie tocara nada, `uv run pytest -q` amaneció con **doce pruebas de
`test_herramientas.py` en rojo**. `INICIO` estaba clavado en el 15/09/2026 y
`ContextoDaniela.ahora` cae al reloj de verdad si nadie se lo da: a medianoche, una hora que
las pruebas usaban como «hora futura ocupada» pasó a ser una hora PASADA, y `_crear_cita`
empezó a contestar otra cosa. Nadie había roto nada. Doce comprobaciones dejaron de medir lo
que decían medir, en silencio, por el calendario.

Es la **tercera** vez en este proyecto: antes fueron `scripts/probar_tools.py::hora` y el
bloque 10 de `scripts/probar_agentes.py`, las dos el 13/09/2026. Las dos primeras se
arreglaron contando bloques hábiles. Esta se arregla al revés, y es importante entender por
qué: un script de entregable corre CONTRA LA AGENDA REAL y necesita una hora que de verdad
exista mañana, así que cuenta hacia delante desde hoy. Una prueba offline no habla con nadie
y lo que necesita es lo contrario: que el tiempo no se mueva. Por eso `tests/test_herramientas.py`
ahora fija `AHORA` además de `INICIO`.

**La regla: si una prueba offline compara contra el presente, el presente se clava.** Un
`datetime.now()` --propio o heredado de un default-- dentro de la suite rápida es una prueba
con fecha de caducidad, y la caducidad llega un día cualquiera a las 00:00, lejos del commit
que la plantó.

## Verde por el motivo equivocado

Dos pruebas de `test_webhook_responde.py` afirmaban «el doctor NO recibe un segundo Telegram»
y lo conseguían así: `ConexionFalsa` no tiene `cursor`, la consulta de verdad lanzaba
`AttributeError`, `_avisar_a_doctores` se lo tragaba --promete no propagar-- y no salía
ningún Telegram. La aserción pasaba. La deduplicación que decían probar no se ejecutaba
NUNCA.

Salió a la luz al añadir una consulta más al mismo camino, que es como suelen salir: el
doble se quedó corto, y el fallo se disfrazó de éxito porque la prueba solo miraba una
ausencia.

**Una aserción sobre algo que NO ocurre no distingue «se decidió no hacerlo» de «reventó
antes de intentarlo».** Cuando lo que se prueba es una ausencia, hay que comprobar además que
el camino llegó hasta donde tenía que llegar --que la fila se escribió, que la consulta se
consultó-- o doblar lo suficiente para que un error no pueda pasar por decisión. Verde por el
motivo equivocado es peor que rojo: el rojo se arregla.

## Una tool se prueba por su función interna

Cada tool vive en dos piezas: la lógica en `_nombre_de_la_tool(...)` y encima una
`@function_tool` de una línea. **Llama a la función con guion bajo.** Un `FunctionTool` ya
construido solo se puede invocar con un JSON serializado y un contexto de corrida completo,
que es justo el andamiaje que estas pruebas existen para no necesitar.

## Un doble de modelo hereda de `Model`

`agents.models.interface.Model` es una clase **abstracta**, no un Protocol: `Agent.__post_init__`
hace `isinstance(model, Model)` explícito, así que imitar su forma no basta.
`tests/dobles.py` ya tiene `ModeloGuionizado`; úsalo en vez de escribir otro.

Correr con él pasa por el SDK completo, que es lo que demuestra que un tripwire **detiene la
corrida** — no solo que una función devolvió `True`. Añade
`RunConfig(tracing_disabled=True)` o el SDK intentará subir el trace y la prueba dejará de
ser offline.

## Una prueba no le cambia el entorno a las demás

`pytest` **importa todos los módulos de prueba antes de ejecutar ninguno**. Un
`os.environ[...] = ...` o un `setdefault` en el cuerpo de un módulo queda fijado para toda
la sesión, y el módulo que lo puso pasa en verde mientras rompe a otro que corre después.

Pasó de verdad: `test_web.py` fijaba una `MAXICARE_DATABASE_URL` falsa para no tocar la base
real, y dejó las once pruebas de `test_tools_neon.py` intentando conectarse a
`localhost/nada`. Solo se vio al correr `-m neon`, que es la suite que casi nunca se corre.

Para no tocar un sistema externo, **sustituye la función que lo alcanza** con `monkeypatch`
—que pytest deshace solo al terminar cada prueba— en vez de manipular el entorno del proceso.

## Prueba también el caso que NO debe disparar

Todo guardrail necesita sus dos pruebas: la del caso que debe frenar y la del mensaje
normal que debe pasar.

Un guardrail que salta cuando no toca es un guardrail que alguien acaba desactivando, y
entonces deja de proteger también los casos reales. La medida de un buen freno no es cuánto
frena.

## Los límites conocidos se prueban, no se callan

Si una regla tiene un hueco aceptado a conciencia, escribe la prueba que lo documenta (ver
`test_el_limite_conocido_queda_documentado_por_una_prueba`). Un hueco con prueba es una
decisión; un hueco sin prueba es una sorpresa esperando a alguien.

## No debilites una prueba para que pase otra cosa

`test_tratamiento_no_admite_una_frase_clinica` sostiene el muro entre lo que ve el doctor y
lo que ve el paciente. Si empieza a estorbar, el problema está en el código nuevo.

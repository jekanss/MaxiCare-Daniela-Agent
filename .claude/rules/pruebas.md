---
paths:
  - "tests/**/*.py"
---

# Las pruebas de este proyecto

## Las dos suites, y por qué están separadas

```
uv run pytest -q                                  offline · ~45 s · SIEMPRE verde
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon   contra Neon · ~17 min · bajo demanda
```

**Esos 17 minutos son reales y en su mayoría irreducibles**, así que no se corre la suite
entera «por si acaso»: se corre EL ARCHIVO que cubre lo que tocaste, y la suite completa una
sola vez, antes de desplegar. Lo medido el 22/09/2026 contra Neon desde Bogotá:

| | |
|---|---|
| Abrir una conexión | 627 ms |
| Una consulta cualquiera | **87 ms** |
| `aplicar_esquema` (reaplica las 28 migraciones) | 2.609 ms |

Los 87 ms por consulta son la distancia física a `us-east-2`, y una prueba que hace veinte
consultas se va sola a 1,7 s esperando. No hay truco: 248 pruebas contra una base al otro
lado del continente son un cuarto de hora. **Lo único que se puede hacer es no pagarlo de
más**, y de ahí la regla de la sección siguiente.

## El esquema se monta UNA vez por archivo, nunca por prueba

Toda fixture que llame a `aplicar_esquema` va con `scope="module"`. Colgarla de la fixture
de cada prueba son 2,6 s tirados por prueba reaplicando migraciones que ya estaban
aplicadas: `tests/test_panel.py` lo hacía así --el único de los diez archivos de Neon-- y
con sus 42 pruebas eso era **la mitad del tiempo del archivo**. Arreglado el 22/09/2026:
de 190 s a 106 s, sin quitar ni una prueba.

Lo que NO se sube a `module` es la CONEXIÓN. Ahorraría otros 0,6 s por prueba y cuesta el
aislamiento: varias pruebas provocan un `CheckViolation` a propósito y se recuperan con
`rollback`, y una transacción abortada compartida envenenaría a las que vengan detrás.

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

**Quien toque la rejilla, la jornada o el reloj corre también `-m neon`.** Los dos helpers
cuentan ahora **bloques hábiles**, no horas de reloj: un `_hora_libre(20)` no son veinte
horas después.

## El esquema `pruebas` NO se vacía entre corridas, y eso acumula

La trampa que costó una tarde el 22/09/2026. Tres pruebas de `test_panel.py` sembraban su
cita pidiendo cupo **a la misma hora fija**, y como el esquema sobrevive a la corrida, cada
pasada dejaba tres reservas más ahí. `reservas.cupo_num` está acotado por la 001 con
`CHECK (cupo_num BETWEEN 1 AND 10)`, así que a la cuarta pasada: `CheckViolation`.

Lo que lo volvió difícil de ver son dos cosas:

- **Parecía intermitente y no lo era.** Fallaba según cuántas veces se hubiera corrido
  antes, que es un estado que no se ve en ningún sitio.
- **Pasaba al correr el archivo solo.** No por aislamiento: porque `test_tools_neon.py`
  comparte este esquema y entra con un `DROP SCHEMA pruebas CASCADE`, así que la suite
  completa dejaba el contador a cero para la siguiente. Eso mandó el diagnóstico hacia
  «interferencia entre archivos», que era exactamente al revés.

Y el primer arreglo fue el equivocado: subir la `capacidad` que se le pasa a `tomar_cupo`.
No servía de nada, porque el tope que se alcanzaba era el del CHECK y no el de la
configuración. **Un tope que salta a la cuarta corrida no se sube: se deja de llenar.**

La regla: **una prueba que siembra en una hora, una clave o un identificador FIJO está
acumulando.** O se los inventa únicos por corrida, o limpia lo suyo antes de sembrar. El
helper hace las dos: un contador le da una hora propia a cada prueba dentro de la corrida, y
un `DELETE` la devuelve limpia entre corridas.

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

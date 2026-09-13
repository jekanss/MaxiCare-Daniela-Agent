# `/clearstate` — resetear un número a primer contacto

**Objetivo:** que un número de pruebas pueda escribir `/clearstate` por WhatsApp y quedar,
para Daniela, exactamente como un número que nunca ha escrito.

**La garantía, en una frase:** `atencion._leer_estado` devuelve, tras el reset, los mismos
ocho campos que para un número virgen — salvo `id_conversacion`, que es un UUID nuevo por
definición — y el texto que recibe el modelo en el turno siguiente es idéntico al de un
primer contacto. Las dos cosas son pruebas, no afirmaciones.

## Por qué es demostrable

Todo lo que Daniela sabe de alguien al empezar un turno sale de `_leer_estado`
(`atencion.py:241`), que lee `pacientes`, `conversaciones` y `configuracion` (global). El
historial del diálogo NO está en Postgres: vive en `conversacion.SesionEnMemoria`, indexada
por `id_conversacion` (`atencion.py:128`). Conversación borrada, id nuevo, sesión vacía: la
memoria no hay que limpiarla, deja de ser alcanzable.

El campo que manda es `identidad_verificada = bool(paciente) or bool(verificada)`
(`atencion.py:297`). Sin fila en `pacientes`, vuelve a `False`.

## Alcance decidido con el usuario

- Borra todo: base, evento de Google Calendar, tema de Telegram.
- Solo para números listados en `MAXICARE_TELEFONOS_PRUEBA`. Vacía o ausente = el comando
  no existe y el texto va a Daniela como cualquier otro.
- Borra en `public` **y** en `pruebas_web` (`runtime.py:654`), que es una copia completa de
  las 12 tablas que alimenta el chat del panel y que no se purga nunca.
- Borra también los mensajes del bot en Telegram cuyo `message_id` conocemos
  (`escalamientos.telegram_message_id`, `mensajes_entrantes.telegram_message_id`).

## Lo que NO cubre — declarado, no descubierto después

1. **Las trazas de OpenAI.** `conversacion.py:222` arma el `RunConfig` sin
   `trace_include_sensitive_data=False`. La conversación entera está en el dashboard de
   OpenAI. Daniela no la lee, así que no afecta al comportamiento; el rastro existe. Sigue
   aplazado a la fase 7.
   **ACTUALIZACIÓN 13/09/2026:** la fuga se cerró ese mismo día, en los tres consumidores de
   modelo, con `config.config_de_corrida`. Lo que este comando sigue sin borrar es lo que ya
   se subió entre la fase 6A y esa fecha: eso vive en el dashboard de OpenAI, no en la base.
2. **Los logs de Docker del VPS**, con el número en claro, rotación ~50 MB.

## Archivos

| Archivo | Cambio |
|---|---|
| `src/maxicare_daniela/reseteo.py` | NUEVO. `es_comando`, `autorizado`, `Borrado`, `resetear` |
| `src/maxicare_daniela/persistencia.py` | `rastro_de(conn, telefono)`, `borrar_rastro(conn, telefono, conservar_wamid)` |
| `src/maxicare_daniela/canales.py` | `Telegram.borrar_tema`, `Telegram.borrar_mensaje` |
| `src/maxicare_daniela/config.py` | `telefonos_prueba: tuple[str, ...]` |
| `.env.ejemplo` | `MAXICARE_TELEFONOS_PRUEBA=` (vacía) |
| `src/maxicare_daniela/runtime.py` | la interceptación en `_entregar`, tras la dedupe |
| `tests/test_reseteo.py` | NUEVO. Offline + la prueba de la garantía |
| `tests/test_reseteo_neon.py` | NUEVO. Contra Neon, esquema `pruebas`, orden de los DELETE |

## El orden del borrado, y por qué

```
(1) Calendar:  eliminar_evento por cada cita con evento_calendar_id
(2) Telegram:  borrar mensajes conocidos, luego el tema del paciente
(3) base:      mensajes_entrantes -> citas -> reservas -> conversaciones
               (cascada: estado_oportunidad, notas_archivo, seguimientos, escalamientos)
               -> pacientes
(4) memoria:   sacar el bufer del telefono, el candado y el candado de tema
(5) WhatsApp:  confirmacion con el conteo por tabla
```

Lo externo va primero a propósito: si Calendar falla a mitad, las filas siguen ahí y se
puede reintentar. Al revés, un evento sin fila es un hueco ocupado que nadie puede cancelar
desde el sistema. Es el mismo criterio de `herramientas._cancelar_cita:703`.

Las FK obligan ese orden: `citas`, `reservas` y `mensajes_entrantes` apuntan a
`conversaciones` SIN cascade, y `conversaciones.paciente_id` apunta a `pacientes` SIN
cascade. Borrar en otro orden no es peor estilo: la base lo rechaza.

## El mismo comando en el chat web del panel

`runtime.chat_de_prueba` lo intercepta igual, y ahí **no se exige la lista blanca**. La
diferencia es deliberada: el «teléfono» de ese carril es `web-<usuario>`, una cadena que no
existe ni puede existir en `public`; la conexión apunta con `search_path` a `pruebas_web`; y
para llegar hace falta sesión abierta en el panel. Cada persona borra su propio carril.

Tampoco se le pasan Telegram ni Calendar, porque el contexto de prueba se construye con
`CalendarioDoble` y sin credenciales de Telegram: no hay eventos reales ni temas que borrar.

El endpoint devuelve `conversacion: null`, que es lo que hace que el turno siguiente abra una
conversación nueva en vez de pedir un id recién borrado. `web/src/api.ts` pasa a declarar ese
campo como `string | null`.

**`/api/pruebas/reiniciar` no cambia.** Olvida la conversación en memoria y deja las filas, a
propósito, para que quede rastro de qué se probó. Por eso el botón «reiniciar» no devuelve a
primer contacto: el paciente inventado sigue en la tabla y el turno siguiente lo reconoce.

## Dos detalles que cambian el resultado

1. **Se conserva la fila de `mensajes_entrantes` del propio `/clearstate`**, con
   `conversacion_id = NULL`. Si se borra, un reintento de Meta ejecuta el comando dos veces.
   No hace conocido a nadie: `_leer_estado` no lee esa tabla.
2. **Se coge el candado por teléfono** de `atencion._candado_de` antes de borrar. Sin él,
   una ventana de búfer abierta escribe sobre una conversación que ya no existe.

## Verificación

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
```

No hay `scripts/probar_reseteo.py` y es deliberado: lo que un script así demostraría —el
ciclo completo con Telegram y WhatsApp falsos— ya lo demuestran `test_reseteo_cable.py`
(offline, el cableado) y `test_reseteo_neon.py` (contra la base real, la garantía). Un
entregable que repite lo que la suite ya cubre no añade confianza, solo otro archivo que
mantener. Lo único que ningún script puede comprobar sigue siendo lo mismo que en la fase
6B: que el tema desaparezca de verdad del Telegram real. Eso lo mira una persona.

**Cada prueba se comprobó rompiendo el código a propósito**, y las cuatro mutaciones
cayeron donde debían:

| Mutación | Cae |
|---|---|
| No borrar `pacientes` | la garantía + 2 más |
| `DELETE FROM pacientes` sin `WHERE` | `test_el_vecino_no_se_toca` |
| Que Calendar no aborte el borrado | `test_si_calendar_falla_no_se_borra_ni_una_fila` |
| Quitar la comprobación de autorización | las 2 pruebas de la lista blanca |
| Quitar la interceptación del chat web | `test_el_comando_en_el_chat_web_no_llega_al_modelo` |

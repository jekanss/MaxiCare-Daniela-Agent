# Fase 6A — Daniela contesta por WhatsApp · Plan de implementación

> **Para quien ejecute esto:** usar superpowers:subagent-driven-development. Los pasos van
> con casilla (`- [ ]`).

**Objetivo:** que un mensaje de WhatsApp llegue a `conversacion.responder` y su respuesta
vuelva al paciente, con el calendario real de Google en el contexto.

**Arquitectura:** un módulo nuevo `atencion.py` que contiene el turno completo y no sabe
que la petición vino por HTTP; `runtime.py` solo lo invoca en segundo plano.

**Stack:** Python 3.12 + uv · `openai-agents` 0.22.2 · `psycopg[binary]` · FastAPI ·
pytest 9.1.1 (**sin pytest-asyncio**: las pruebas son síncronas y llaman `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-09-12-fase-6a-daniela-contesta-design.md`

## Restricciones globales

Copiadas del spec y del `CLAUDE.md`. Cada tarea las hereda.

1. **`atencion.py` no importa `runtime.py` ni ningún framework de transporte.** Va añadido
   a `MODULOS_SIN_TRANSPORTE` en `tests/test_estructura.py`, que lo verifica sobre el AST.
2. **No se edita ninguna migración existente.** La nueva es `009_`, con
   `ADD COLUMN IF NOT EXISTS`, idempotente.
3. **Las pruebas que tocan Neon llevan `@pytest.mark.neon`** y usan el esquema `pruebas`.
   Las demás corren sin red en milisegundos.
4. **No se registran cédulas ni documentos de identidad.** Ninguna columna nueva los admite.
5. Los scripts de `scripts/` empiezan con
   `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y usan marcadores ASCII
   (`OK` / `FALLA` / `->`).
6. **No se debilita ninguna prueba existente para que pase algo nuevo.** En particular
   `test_tratamiento_no_admite_una_frase_clinica`.
7. `uv run pytest -q` tiene que quedar verde al terminar cada tarea. Hoy: **282 pruebas**.

---

## Tarea 1 · Migración 009 y las funciones de persistencia

**Archivos:**
- Crear: `migraciones/009_respuesta_al_paciente.sql`
- Modificar: `src/maxicare_daniela/persistencia.py`
- Modificar: `tests/test_tools_neon.py` (añadir las pruebas al final)

**Produce** (lo consume la Tarea 2):

```python
def conversacion_viva(
    conn, telefono: str, *, ventana_horas: int = 24
) -> tuple[str, int, bool, int] | None:
    """La conversación reciente de ese teléfono: (id, turno_actual, identidad_verificada,
    intentos_identificacion). `None` si no hay ninguna dentro de la ventana."""

def tocar_conversacion(conn, id_conversacion: str) -> None:
    """Pone `actualizada_en = now()`. Sin esto la ventana de 24 h se mide contra la
    creación y una charla larga se partiría en dos a mitad."""

def ligar_mensaje_a_conversacion(conn, wamid: str, id_conversacion: str) -> None:

def marcar_respondido(conn, wamid: str, *, wamid_respuesta: str) -> None:

def marcar_fallo_respuesta(conn, wamid: str, *, motivo: str) -> None:
```

- [ ] **Paso 1: escribir las pruebas, que deben fallar**

En `tests/test_tools_neon.py` (marcador `neon` ya está a nivel de módulo):

- `test_una_conversacion_reciente_del_mismo_telefono_se_reutiliza` — crear conversación con
  `asegurar_conversacion`, y `conversacion_viva` devuelve ese mismo id.
- `test_una_conversacion_vieja_no_se_reutiliza` — poner `actualizada_en` a 30 horas atrás
  con un UPDATE directo; `conversacion_viva` devuelve `None`.
- `test_conversacion_viva_devuelve_la_mas_reciente` — dos conversaciones del mismo teléfono;
  devuelve la de `actualizada_en` mayor. **Ordenar por `actualizada_en DESC, id DESC`**:
  `now()` en Postgres es la hora de la TRANSACCIÓN, así que dos filas creadas en la misma
  transacción comparten el instante exacto y sin el desempate el resultado es arbitrario.
  Esto ya mordió en la fase 8.
- `test_conversacion_viva_no_cruza_telefonos` — un teléfono no ve la conversación de otro.
- `test_tocar_conversacion_adelanta_la_ventana` — envejecer a 30 h, `tocar_conversacion`, y
  entonces `conversacion_viva` sí la encuentra.
- `test_la_respuesta_al_paciente_queda_registrada` — insertar en `mensajes_entrantes`,
  `ligar_mensaje_a_conversacion` + `marcar_respondido`, y comprobar por SQL que
  `conversacion_id`, `respondido_en` y `wamid_respuesta` quedaron.
- `test_un_fallo_al_responder_queda_registrado` — `marcar_fallo_respuesta` deja el motivo y
  **`respondido_en` sigue en NULL**. Si un fallo marcara respondido, la consulta «¿a quién
  no le contestamos?» devolvería vacío justo cuando importa.

- [ ] **Paso 2: correrlas y verlas fallar**

`MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` → error de atributo inexistente.

- [ ] **Paso 3: escribir la migración**

`migraciones/009_respuesta_al_paciente.sql`, con cabecera explicando POR QUÉ (hoy la tabla
solo registra el viaje hacia Telegram; si Daniela deja a alguien sin contestar no queda
rastro). Cuatro `ALTER TABLE mensajes_entrantes ADD COLUMN IF NOT EXISTS`:
`conversacion_id UUID REFERENCES conversaciones(id)`, `respondido_en TIMESTAMPTZ`,
`wamid_respuesta TEXT`, `fallo_respuesta TEXT`.

Más un índice parcial que haga barata la pregunta que justifica la migración:

```sql
CREATE INDEX IF NOT EXISTS ix_mensajes_sin_responder
    ON mensajes_entrantes (recibido_en DESC)
    WHERE respondido_en IS NULL AND fallo_respuesta IS NOT NULL;
```

- [ ] **Paso 4: implementar las cinco funciones** en `persistencia.py`, junto a
      `asegurar_conversacion`. Docstrings en el estilo del módulo: explican la decisión, no
      repiten la firma.

- [ ] **Paso 5: aplicar y verificar**

```bash
uv run python scripts/inicializar_base.py
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run pytest -q
```

- [ ] **Paso 6: commit** — `feat: mensajes_entrantes registra si al paciente se le respondio`

---

## Tarea 2 · `atencion.py`, el turno de WhatsApp

**Archivos:**
- Crear: `src/maxicare_daniela/atencion.py`
- Crear: `tests/test_atencion.py`
- Modificar: `tests/test_estructura.py` (añadir `"atencion.py"` a `MODULOS_SIN_TRANSPORTE`
  con su comentario de por qué, como tienen todos los demás)

**Consume:** las cinco funciones de la Tarea 1.

**Produce** (lo consume la Tarea 3):

```python
@dataclass(frozen=True)
class Atendido:
    """Lo que pasó con un mensaje. Se devuelve en vez de registrarse solo en el log para
    que el script entregable pueda comprobarlo sin leer journald."""
    wamid: str
    id_conversacion: str | None
    respondido: bool
    texto_enviado: str | None = None
    motivo: str | None = None      # por qué no se respondió
    turno: int = 0
    escalado_por: str | None = None

async def atender(
    mensaje: ingesta.MensajeEntrante,
    *,
    whatsapp: WhatsApp,
    telegram: Telegram,
    config: Config,
    calendario: Any | None = None,
    dormir: Callable[[float], Awaitable[None]] | None = None,
) -> Atendido:
```

`calendario` y `dormir` existen **solo para las pruebas**: por defecto son
`calendario_desde_config(config)` y `asyncio.sleep`. Una prueba que esperase 55 segundos de
verdad es una prueba que alguien acaba saltándose.

**Constantes del módulo:**

```python
VENTANA_CONVERSACION_HORAS = 24
TEXTO_ARCHIVO = "El paciente envió {que}."   # QUÉ es, nunca qué muestra
```

**Estado del módulo** (con su comentario sobre el límite de un solo worker):

```python
_candados: dict[str, asyncio.Lock] = {}
_sesiones: dict[str, tuple[SesionEnMemoria, float]] = {}   # (sesión, último uso)
```

- [ ] **Paso 1: escribir las pruebas, que deben fallar**

`tests/test_atencion.py`, offline, sin red ni base. Todo lo externo se sustituye con dobles:
un `WhatsAppFalso` que acumula lo enviado, un `TelegramFalso`, y **`monkeypatch` sobre las
funciones de `persistencia` y sobre `conversacion.responder`** — nunca tocando
`os.environ`, porque pytest importa todos los módulos antes de ejecutar ninguno y una
variable fijada en un módulo se le queda a los demás (ya rompió `test_tools_neon.py` una vez).

Las pruebas, con lo que cada una vigila:

1. `test_un_mensaje_de_texto_recibe_respuesta` — el camino feliz: `enviado` trae el
   `mensaje_al_paciente`, y `Atendido.respondido` es True.
2. `test_la_conversacion_reciente_se_reutiliza` — `conversacion_viva` devuelve un id;
   `asegurar_conversacion` **no** se llama.
3. `test_sin_conversacion_reciente_se_abre_una` — al revés.
4. `test_un_numero_conocido_entra_identificado` — con `buscar_paciente_por_telefono`
   devolviendo `(7, "Ana Restrepo")`, el contexto que recibe `responder` trae
   `identidad_verificada=True`, `id_paciente=7` y `nombre_paciente="Ana Restrepo"`.
5. `test_un_numero_desconocido_no_entra_identificado` — el falso positivo del anterior. Sin
   esta prueba, un `buscar_paciente_por_telefono` que devolviera siempre algo pasaría la 4.
6. `test_dos_mensajes_del_mismo_telefono_se_serializan` — con un `responder` falso que
   registra entrada y salida, comprobar que **no se solapan**: la secuencia tiene que ser
   `entra A, sale A, entra B, sale B`. Lanzarlos con `asyncio.gather`.
7. `test_dos_telefonos_distintos_no_se_bloquean_entre_si` — la mitad que impide que el
   candado se convierta en un cuello de botella global. Con un `responder` que espera a un
   `asyncio.Event`, las dos corridas tienen que estar dentro a la vez.
8. `test_el_retardo_descuenta_lo_que_tardo_el_turno` — un `responder` falso que "tarda" 30 s
   (avanzando un reloj inyectado) y un `dormir` que captura el valor: lo dormido tiene que
   ser `objetivo - 30`, nunca 30 + objetivo.
9. `test_el_retardo_nunca_es_negativo` — un turno que tardó 90 s: se duerme 0, no un número
   negativo (`asyncio.sleep` con negativo no lanza, pero pedirlo es decir que no se pensó).
10. `test_con_el_interruptor_apagado_no_se_envia_nada` — `Atendido.respondido is False`,
    `motivo` lo dice, y `whatsapp.enviados == []`.
11. `test_un_mensaje_con_archivo_marca_el_adjunto_y_no_interpreta` — el texto que recibe
    `responder` contiene el tipo («imagen») y el pie de foto si lo hay, y `ctx.turno.hubo_adjunto`
    es True. **Aserción explícita de que NO aparece ninguna palabra de contenido clínico.**
12. `test_si_enviar_falla_queda_registrado_y_no_revienta` — `enviar_texto` lanza
    `ErrorDeCanal`; `atender` devuelve `respondido=False` con motivo, llama a
    `marcar_fallo_respuesta`, y **no propaga**.
13. `test_si_el_turno_revienta_el_paciente_igual_recibe_algo` — `responder` lanza una
    excepción inesperada: sale un mensaje de cortesía al paciente. Es la regla de
    `fallos.escalamiento`: pase lo que pase sale un mensaje.
14. `test_el_contexto_lleva_las_credenciales_de_telegram` — con `""` en el contexto,
    `escalar_a_doctores` falla en silencio y el doctor nunca se entera. El chat web las deja
    vacías a propósito; aquí no puede.
15. `test_el_contexto_lleva_el_calendario_que_se_le_pasa` — que `calendario` llegue al
    contexto, para que en producción sea el de Google y no un doble.
16. `test_las_sesiones_viejas_se_podan` — tras superar la ventana, la sesión se descarta.

- [ ] **Paso 2: correrlas y verlas fallar** — `uv run pytest -q tests/test_atencion.py`.

- [ ] **Paso 3: implementar `atencion.py`**

Docstring de módulo que explique, en el estilo del proyecto: por qué el turno no vive en
`runtime.py`, qué es el candado y por qué, y los cuatro límites conocidos del §5 del spec.

Orden del cuerpo de `atender`:

1. Si el interruptor está apagado → `Atendido(respondido=False, motivo="apagado")`.
2. `momento_inicio = time.monotonic()` **antes de todo**, que es lo que hace honesto el
   descuento del retardo.
3. Con `persistencia.conectar`: `conversacion_viva` → si no hay, `asegurar_paciente` no;
   `asegurar_conversacion`. Luego `buscar_paciente_por_telefono`, `leer_configuracion`,
   `conversacion_tomada`, `ligar_mensaje_a_conversacion`. **Todo en una sola conexión**, y
   dentro de `asyncio.to_thread`: psycopg es síncrono y bloquearía a los demás pacientes.
4. Tomar el candado de esa conversación.
5. Construir `ContextoDaniela` con todo lo del §4.4 del spec.
6. Componer la entrada: el texto, o `TEXTO_ARCHIVO` + el pie si trae archivo.
7. `conversacion.responder(...)` con `al_escalar` (Tarea 3 le pasa el real; por defecto
   `None`).
8. Retardo: `espera = max(0.0, objetivo - (time.monotonic() - momento_inicio))`.
9. `whatsapp.enviar_texto` → `marcar_respondido`, o `marcar_fallo_respuesta`.
10. `tocar_conversacion`.

`marcar_leido` se llama **antes** del retardo, al principio: el doble check azul mientras
Daniela "escribe" es lo que hace creíble la espera.

- [ ] **Paso 4: verificar** — `uv run pytest -q`, todo verde incluido `test_estructura.py`.

- [ ] **Paso 5: commit** — `feat: atencion.py, el turno de WhatsApp fuera del transporte`

---

## Tarea 3 · Cablear el webhook

**Archivos:**
- Modificar: `src/maxicare_daniela/runtime.py`
- Modificar: `src/maxicare_daniela/config.py` (el interruptor)
- Modificar: `.env.ejemplo`
- Modificar: `tests/test_web.py` (o crear `tests/test_webhook_responde.py`)

- [ ] **Paso 1: el interruptor en `config.py`**

Campo `daniela_responde: bool`, leído de `MAXICARE_DANIELA_RESPONDE`, **por defecto True**:
`_opcional("MAXICARE_DANIELA_RESPONDE", "1") != "0"`. Comentario explicando que el default
activo es lo que pidió MaxiCare y que el interruptor existe para poder callarla sin
desplegar. Documentarlo en `.env.ejemplo`.

- [ ] **Paso 2: la prueba, que debe fallar**

Con `TestClient`, un POST firmado con un mensaje de texto tiene que acabar llamando a
`atencion.atender` (sustituido con `monkeypatch`). Comprobar también que **sigue
llamándose `ingesta.procesar_mensaje`**: el archivo tiene que llegar al doctor pase lo que
pase con el modelo, que es la garantía de la fase 2 y no puede romperse aquí.

- [ ] **Paso 3: cablear**

En `_entregar`, después de `ingesta.procesar_mensaje`, llamar a `atencion.atender` con
`_whatsapp`, `_telegram`, `config`, el calendario global y `al_escalar=_avisar_a_doctores`.

Dos cosas que no se pueden hacer mal:

- **El orden.** `procesar_mensaje` primero, siempre, y en su propio `try`. Si el turno de
  Daniela revienta, el archivo ya llegó a Telegram.
- **`_entregar` no puede propagar nunca.** Ya lo documenta su docstring.

Construir el calendario **una vez al arrancar**, en un handler de startup, dentro de
`try/except`: `CalendarioGoogle` hace una lectura real al construirse y un fallo no puede
impedir que el webhook arranque —WhatsApp está en producción—. Si falla, `CalendarioDoble`
y un `log.error` bien visible.

`_avisar_a_doctores(ctx, motivo, mensaje)`: escribe al tema General por Telegram,
reutilizando `persistencia.insertar_escalamiento` con la clave
`ctx.clave("escalamiento", ctx.turno_actual)` **para no duplicar** el aviso que la tool
`escalar_a_doctores` ya pudo haber mandado en ese mismo turno.

- [ ] **Paso 4: verificar** — `uv run pytest -q`.

- [ ] **Paso 5: commit** — `feat: el webhook de WhatsApp le pasa el mensaje a Daniela`

---

## Tarea 4 · El entregable y la documentación

**Archivos:**
- Crear: `scripts/probar_atencion.py`
- Modificar: `CLAUDE.md`

- [ ] **Paso 1: el script**

Contra Neon —esquema de pruebas, **nunca `public`**— con un `WhatsAppFalso` que captura en
vez de enviar. No gasta tokens salvo con `--chat`, que corre el turno de verdad contra el
modelo. Imprime `OK` / `FALLA` por línea y devuelve el código de salida.

Comprueba, cada uno con su línea:

1. Un mensaje de un número desconocido abre conversación y recibe respuesta.
2. El segundo mensaje **reutiliza** la conversación y `turno_actual` sube a 2.
3. Un número que sí está en `pacientes` entra identificado.
4. Dos mensajes a la vez del mismo teléfono se serializan.
5. El retardo se descontó (comprobando lo que se pidió dormir, no durmiendo).
6. `mensajes_entrantes` quedó con `conversacion_id`, `respondido_en` y `wamid_respuesta`.
7. Con el interruptor apagado no se envía nada.
8. Limpieza del esquema de pruebas en un `finally` **verificado con una aserción**, nunca
   dado por hecho.

- [ ] **Paso 2: `CLAUDE.md`**

El comando nuevo, y una sección corta con lo que va a costar caro si se olvida:

- El candado es de proceso: con más de un worker deja de proteger.
- El historial vive en memoria; un reinicio borra el hilo, no los datos.
- El interruptor y cómo usarlo.
- `asegurar_conversacion` **siempre inserta**; para WhatsApp se usa `conversacion_viva`.

- [ ] **Paso 3: correr el entregable y commit** —
      `feat: probar_atencion.py, el turno de WhatsApp de punta a punta`

---

## Lo que cierra la fase 6A

`uv run pytest -q` verde, `probar_atencion.py` en OK, y **una persona escribiéndole al
número de MaxiCare por WhatsApp y recibiendo respuesta**. Lo último no lo puede hacer
ningún script, y decir lo contrario sería mentir sobre lo que está verificado.

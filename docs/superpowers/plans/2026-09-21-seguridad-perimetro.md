# Seguridad del perímetro — que un desconocido no pueda quemar el saldo

Rama `seguridad-perimetro`, desde `main` en `281b6d4`.
Línea base ANTES de tocar nada: **959 pasadas, 165 saltadas, 0 fallos** (`uv run pytest -q`).
Ese número es el contrato: al terminar tienen que seguir las 959, más las nuevas.

## El problema, en una frase

El sistema está blindado en el eje de los datos y abierto en el eje del dinero: cualquiera
que conozca el número público de la clínica puede gastar el saldo de OpenAI sin vulnerar
nada, entrando con firma HMAC válida de Meta, y **hoy no hay un solo contador que se entere**.

## Las tres restricciones

1. **No alterar el comportamiento de Daniela.** Los umbrales van tan por encima del uso real
   que ningún paciente los toca. Donde hay que elegir, se trunca en vez de rechazar y se
   degrada en vez de silenciar.
2. **La seguridad clínica sigue mandando.** Ningún freno puede hacer que un archivo clínico
   no le llegue al doctor, ni que un escalamiento real se pierda. Los frenos son sobre el
   GASTO, no sobre la atención.
3. **Todo freno se mide antes de cortar.** Cada límite escribe su propia huella, para que
   subirlo o bajarlo sea un dato y no una corazonada.

## Decisiones tomadas (por el usuario, 21/09/2026)

- **Al pasarse de cuota:** frase fija al paciente + UN aviso al doctor por ventana. Nunca uno
  por mensaje: eso sería el mismo vector de inundación que se está cerrando.
- **Se toca el despliegue:** `docker-compose.yml` con `mem_limit` y middlewares de Traefik.
- **Rama aparte**, para revisar el diff entero antes de fundir.

## Decisiones de diseño (mías, documentadas para poder discutirlas)

- **La cuota se cuenta sobre `mensajes_entrantes`, que ya existe**, no sobre una tabla
  paralela. Una tabla de contadores puede desincronizarse del hecho que cuenta; la tabla de
  los hechos, no. Cuesta un índice, que la 022 crea.
- **El tope de descarga NO baja de lo que hoy funciona.** Telegram rechaza >50 MB, así que un
  corte en 55 MB por `Content-Length` protege la RAM sin perder ni un archivo que hoy llegue.
  El freno real contra la inundación de archivos es la cuota + el semáforo, no el tamaño.
- **La entrada larga se TRUNCA, no se rechaza.** Rechazar deja al paciente sin respuesta;
  truncar deja a Daniela respondiendo y evita que el evaluador reviente por contexto, que es
  la causa real del fallo abierto.
- **El gasto se lee de `RunResult.context_wrapper.usage`**, no con `RunHooks`. Verificado por
  introspección en la 0.22.2 instalada: `input_tokens`, `output_tokens`, `requests` e
  `input_tokens_details.cached_tokens`. Los cacheados se guardan aparte o el coste sale falso.

## Umbrales iniciales

Calibrados contra el volumen real del 21/09/2026 (4 conversaciones, 11 turnos al día). Todos
por `.env`, todos con el valor por defecto muy por encima del uso legítimo.

| Variable | Default | Por qué ese número |
|---|---|---|
| `MAXICARE_CUOTA_MENSAJES_HORA` | 40 | Una conversación intensa son ~15-20 mensajes |
| `MAXICARE_CUOTA_ARCHIVOS_DIA` | 12 | Una tanda de radiografías son 3-6 |
| `MAXICARE_ALERTA_GASTO_DIARIO_USD` | 5.0 | ~50 conversaciones completas al precio medido |
| `MAXICARE_LECTORES_CONCURRENTES` | 3 | El lector es el modelo caro; 3 en vuelo es holgado |
| `MAXICARE_TOPE_ENTRADA_CARACTERES` | 8000 | El doble de lo que el panel ya exige (4000) |
| `MAXICARE_TOPE_DESCARGA_MB` | 55 | Por encima del límite de Telegram: no pierde nada |
| `MAXICARE_CUOTA_MODO_OBSERVACION` | 0 | A 1, cuenta y alerta pero no corta |

`MAXICARE_CUOTA_MODO_OBSERVACION` existe porque con 4 conversaciones/día todavía no se sabe
cuál es el uso normal. Permite encender la medición hoy y el corte cuando haya datos.

---

## Tanda A — Cimientos

- **`migraciones/022_cuotas_y_consumo.sql`**
  - `CREATE INDEX ... ON mensajes_entrantes (telefono, recibido_en DESC)` — sin él, cada
    mensaje entrante haría un scan de la tabla para contar la cuota.
  - `consumo_modelo`: una fila por corrida. `id_conversacion`, `telefono`, `agente`, `modelo`,
    `llamadas`, `tokens_entrada`, `tokens_entrada_cacheados`, `tokens_salida`, `momento`.
    Sin FK a `conversaciones`: el lector corre antes de que exista la conversación.
  - `cuotas_avisadas`: `telefono`, `clase` (mensajes|archivos), `avisado_en`. Es lo que hace
    que el aviso al doctor y la frase al paciente salgan UNA vez por ventana.
- **`config.py`**: las siete constantes, sus siete variables de entorno, y los precios por
  modelo para poder convertir tokens en dólares.
- **`.env.ejemplo`**: las siete, comentadas.

## Tanda B — Telemetría de gasto (medida 1)

- **`persistencia.py`**: `anotar_consumo(...)` y `gasto_del_dia(...)`.
- **`conversacion.py`**, **`lectura.py`**, **`guardrails.py`**, **`analista.py`**: los cuatro
  consumidores anotan su usage después de `Runner.run`. Va en un `try` que traga, como
  `_anotar_resultado`: si la contabilidad revienta se pierde una fila, nunca un turno.
- **`runtime.py`**: tarea propia que compara el gasto del día contra el umbral y manda UN
  Telegram al General cuando lo cruza. Propia, no colgada de la de relevos, que no arranca
  sin Telegram — misma razón que el no negociable 21.

## Tanda C — Cuota por teléfono (medida 2)

- **`persistencia.py`**: `mensajes_en_la_ultima_hora(...)`, `archivos_del_dia(...)`,
  `marcar_cuota_avisada(...)` con `ON CONFLICT` para el "una vez por ventana".
- **`atencion.py`**: el chequeo va justo después de `daniela_responde` y ANTES de
  `marcar_leido` y del búfer. Al pasarse: frase fija + aviso, o silencio si ya se avisó.
  Devuelve `Atendido(motivo="cuota")`.

## Tanda D — El carril del lector (medida 3)

- **`canales.py`**: `descargar_media` mira `Content-Length` antes de bajar los bytes.
- **`lectura.py`**: `asyncio.Semaphore` global sobre las lecturas concurrentes.
- **`ingesta.py`**: `procesar_mensaje` acepta `leer_archivos: bool = True`; la cuota de
  archivos y `daniela_responde` lo apagan. **El archivo sigue llegando al doctor siempre** —
  lo que se apaga es la llamada al modelo, nunca la entrega.
- **`runtime.py`**: `_entregar` le pasa `config.daniela_responde`. Cierra el agujero de que
  el freno de mano no apagaba el gasto más caro.

## Tanda E — La superficie regalada (medidas 4, 5, 6)

- **`runtime.py` `/salud`**: `{"ok": true}` público; el detalle tras `Depends(usuario_actual)`.
  Se deja de devolver el texto crudo de la excepción de base y la lista de secretos que faltan.
- **`runtime.py` `/api/entrar`**: contador de intentos por IP y por usuario, y `scrypt`
  señuelo cuando el usuario no existe — hoy el cortocircuito del `and` delata qué usuarios
  existen por temporización, lo que anula el mensaje genérico escrito a propósito.
- **`docker-compose.yml`**: `mem_limit`, `pids_limit`, y middlewares de Traefik `buffering` +
  `rateLimit`.

## Tanda F — Inyección y ruido (medidas 7, 8, 9, 10)

- **`atencion.py`**: truncado de la entrada al modelo con marca visible.
- **`guardrails.py`**: el fallo abierto se conserva —un sistema mudo no protege a nadie— pero
  deja de ser silencioso: se cuenta y se alerta. Un pico de fallos del evaluador es la firma
  de alguien probando el desarme.
- **`herramientas.py`**: tope por ventana en `escalar_a_doctores`, **degradando a agregado, no
  silenciando**. A partir del tercero en una hora, un único mensaje que diga cuántos van.
  Aquí está la frontera con la seguridad clínica: se toca el RUIDO, nunca la señal.
- **`herramientas.py`**: `consultar_disponibilidad` acota la ventana a 90 días.

---

## Verificación (obligatoria antes de decir que está hecho)

1. `uv run pytest -q` → 959 + las nuevas, 0 fallos.
2. `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon` → la 022 y el panel.
3. Los seis scripts que no gastan, **porque este trabajo toca firmas que los scripts doblan
   a mano y `pytest` no los corre**: `probar_tools.py`, `probar_relevo.py`,
   `probar_recordatorios.py`, `probar_sin_resolver.py`, `probar_calendario.py`, `probar_web.py`.
4. `uv run python scripts/inicializar_base.py --solo-verificar`.

## Lo que NO puedo hacer yo

- **El hard limit de gasto en el dashboard de OpenAI.** Es la única defensa que acota el peor
  caso absoluto y va en la web de OpenAI, no en el código.
- **El rate limit del borde en Traefik** más allá de las labels: Traefik vive en
  `/opt/sinpiloto`, fuera de este repositorio.

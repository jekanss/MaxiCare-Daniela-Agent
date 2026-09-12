# Pantalla de Tratamientos — diseño

**Fecha:** 2026-09-12
**Estado:** aprobado por el cliente, pendiente de plan de implementación
**Precede a:** fase 6 (ingesta, el muro y el relevo)

---

## 1. Por qué esta pantalla antes de la fase 6

El orden del plan pone las pantallas de operación en la fase 8, después de que Daniela
conteste en WhatsApp. Esta se adelanta por una razón que no es técnica: **convierte un
bloqueo nuestro en trabajo paralelo del cliente.**

Los precios de endodoncia y prótesis, y los nombres de las especialistas de ortodoncia y
periodoncia, llevan semanas marcados `PENDIENTE`. Pedirlos por mensaje no ha funcionado.
Con esta pantalla MaxiCare los carga mientras se construye la fase 6.

Es la única pantalla del menú que no espera a ninguna fase: `base_conocimiento` tiene 73
filas reales y `consultar_base_conocimiento` ya las lee en vivo.

### Estado medido de la base (2026-09-12)

```
base_conocimiento     73      (14 sin aprobar)
configuracion          5
mensajes_entrantes     8
usuarios               1
pacientes / conversaciones / estado_oportunidad / citas / reservas /
seguimientos / escalamientos / notas_archivo        0
```

Daniela todavía no ha corrido sobre un mensaje real de un paciente. Por eso el resto de
pantallas del menú —Bandeja, Leads, Agenda, Métricas— quedan fuera: se construirían
contra tablas vacías, y una pantalla sin datos no se distingue de una rota.

### La foto de las fichas

```
                 precio  duracion  profesional  garantia      incluye       no_incluye
bichectomia        OK      OK          OK       SIN APROBAR      OK             --
blanqueamiento     OK      OK          OK           --       SIN APROBAR       OK
cordales           OK      OK          OK           --           OK            OK
coronas            OK      OK          OK       SIN APROBAR      --            --
diseno_sonrisa     OK      OK          OK       SIN APROBAR      OK            --
gingivectomia      OK      OK          OK       SIN APROBAR      --            --
implantes          OK      OK          OK       SIN APROBAR      OK            OK
limpieza           OK      OK      SIN APROBAR      --           --            --
microdiseno        OK      OK          OK       SIN APROBAR      --            --
ortodoncia         OK      OK          OK           OK           OK            OK
periodoncia        OK      OK          OK           --           --            --
endodoncia      ← CERO FILAS
protesis        ← CERO FILAS
```

Solo ortodoncia está completa. Hacer esto evidente de un vistazo es el trabajo principal
de la pantalla.

---

## 2. Alcance

**Dentro:**

- Editar las 73 fichas: contenido, estado de aprobación y nota de pendiente.
- Crear fichas nuevas (pares tratamiento + concepto), incluidas las de endodoncia y prótesis.
- **Crear y desactivar tratamientos** desde la pantalla, sin tocar código.
- Sección aparte para los 12 hechos de clínica que hoy viven bajo `_general`.
- Bitácora de todo cambio: quién, cuándo, valor anterior y nuevo.
- Control por rol.

**Fuera, y se dice en la pantalla, no se esconde:**

- Las cuatro perillas de operación, Usuarios del panel y Estado del sistema — fase 8,
  después de la fase 6.
- Borrar fichas o tratamientos. Ver §8.
- Los borradores del prompt de Daniela con historial de versiones: es alcance que
  `plan-agentes.json` no tiene, y no se absorbe de contrabando.

---

## 3. La decisión central: partir el `Literal` en dos

`Tratamiento` es hoy un `Literal` cerrado de 14 valores en `contratos.py`, y se usa en
exactamente dos contratos. **Hace dos trabajos distintos, y solo uno es de seguridad.**

| Uso | Qué es | Qué pasa con un tratamiento nuevo |
|---|---|---|
| `LecturaArchivo.tratamiento` (línea 181) | **El muro.** Contenido clínico que cruza hacia el paciente. | Cae en `no_identificado`. Correcto. |
| `SolicitudCita.tratamiento` (línea 298) | Vocabulario de negocio. | Hoy falla la validación. |
| `registrar_estado_oportunidad` | Vocabulario de negocio. | Hoy falla la validación. |
| `RespuestaDaniela.mensaje` | Texto libre. | Ya funciona: Daniela puede hablar de él. |
| `consultar_base_conocimiento(tratamiento: str)` | Ya es `str`. | Ya funciona: devuelve `SIN DATO DOCUMENTADO`. |

**El muro no se mueve.** `LecturaArchivo` conserva el `Literal` escrito a mano y
`test_tratamiento_no_admite_una_frase_clinica` no se toca. Contenido clínico que cruza
hacia el paciente no se abre desde una pantalla, nunca.

**El vocabulario sí.** Los otros dos pasan a validar contra la lista viva.

### Condición de reapertura que se acaba de disparar

La fase 1 dejó escrito: *«si el `Literal` de tratamientos resulta insuficiente al cargar la
base de conocimiento real»*. No se disparó por un dato que faltara —los 14 cubren los 12
que MaxiCare ofrece más endodoncia y prótesis— sino porque **el cliente tiene que poder
crecer sin nosotros**, que es literalmente el argumento de la fase 8: *la interfaz web es
la autonomía de MaxiCare respecto de quien construye*.

Esto se anota en `plan-agentes.json` al cerrar. No se absorbe en silencio.

---

## 4. Datos

### Migración 007 — la bitácora

```sql
CREATE TABLE IF NOT EXISTS cambios_configuracion (
    id             BIGSERIAL PRIMARY KEY,
    tabla          TEXT NOT NULL
                   CHECK (tabla IN ('base_conocimiento','tratamientos','configuracion')),
    clave          TEXT NOT NULL,   -- 'implantes/precio' o 'carillas'
    valor_anterior TEXT,            -- NULL = no existía
    valor_nuevo    TEXT NOT NULL,
    usuario        TEXT NOT NULL,
    cambiado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Sin clave foránea a `usuarios`, a propósito.** Si mañana alguien borra un usuario, el
registro de lo que cambió tiene que sobrevivir. Una bitácora que se borra con quien la
escribió no es una bitácora.

El cambio y su registro van **en la misma transacción**. Si falla el `INSERT` de la
bitácora, el precio no se cambia.

### Migración 008 — los tratamientos

```sql
CREATE TABLE IF NOT EXISTS tratamientos (
    clave      TEXT PRIMARY KEY CHECK (clave ~ '^[a-z][a-z0-9_]{2,23}$'),
    etiqueta   TEXT NOT NULL,
    activo     BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    creado_por TEXT
);
```

Sembrada con los 14 del `Literal` actual, todos activos.

**La `clave` la restringe Postgres, no solo Python.** Es el string que termina dentro de
`citas.tratamiento` y en los argumentos que ve el modelo: minúsculas, sin espacios, 3 a 24
caracteres. Una frase clínica no pasa el CHECK.

**La `etiqueta`** («Carillas estéticas») es para las personas del panel. No llega al modelo.

**No hay columna `en_el_muro`.** La tentación era marcar ahí cuáles están en el `Literal`
de `LecturaArchivo`; sería una segunda fuente de verdad que se desincroniza en silencio el
día que alguien edite una y no la otra. La API lo calcula con `get_args(Tratamiento)` en
cada consulta.

---

## 5. El vocabulario vivo, sin reiniciar el proceso

Esto es lo que separa «lo puede meter el cliente» de «lo mete el cliente y espera».

**`contratos.py` no puede importar la base de datos.** Rompería las pruebas offline, los
scripts y la frontera de módulos que vigila `tests/test_estructura.py`. Entonces:

```python
# contratos.py
_VOCABULARIO: frozenset[str] = frozenset(get_args(Tratamiento))

def fijar_vocabulario(claves: Iterable[str]) -> None: ...
def vocabulario() -> frozenset[str]: ...
```

Arranca con los 14 del `Literal`, así que offline todo sigue igual. `runtime.py` lo
reemplaza al encender con los tratamientos activos de la tabla, y lo vuelve a fijar cada
vez que la pantalla crea o desactiva uno.

Dos firmas cambian de `Tratamiento` a `str` con un `field_validator` contra `vocabulario()`:

- `SolicitudCita.tratamiento`, en `contratos.py`.
- el argumento `tratamiento` de `registrar_estado_oportunidad`, en `herramientas.py`.

**El costo honesto de ese cambio:** el esquema JSON que ve el modelo para `crear_cita` deja
de traer la enumeración de valores y pasa a ser un string con su descripción. Se compensa
en §6. Si el modelo inventa una clave, el validador la rechaza y el SDK le devuelve el
error para que reintente — el efecto externo no ocurre.

---

## 6. Cómo se entera el modelo

`instructions` de `Agent` acepta un callable de **exactamente 2 parámetros**
`(run_context, agent)`, síncrono o asíncrono. Verificado por introspección contra la 0.22.2
instalada, no de memoria.

Las instrucciones de Daniela pasan a ser ese callable, y la lista de tratamientos activos
se añade **al final del texto, nunca al principio**.

**Por qué al final:** `prompt_cache_retention="24h"` es una decisión de costo del bloque de
modelos —baja la entrada de $2.00 a $0.20 por millón, y el prompt de sistema con los nueve
esquemas de tools es la mayor parte de esa entrada—. El caché funciona por prefijo idéntico.
Una lista que cambia al principio lo invalidaría entero en cada corrida.

---

## 7. Módulo, endpoints y roles

### `src/maxicare_daniela/panel.py` — nuevo, sin framework

Entra en `MODULOS_SIN_TRANSPORTE` de `tests/test_estructura.py`, como los otros ocho. No
va en `persistencia.py`, que ya tiene 736 líneas.

```
listar_tratamientos    crear_tratamiento     cambiar_tratamiento
listar_conocimiento    guardar_ficha         historial
vocabulario_activo
```

### Endpoints — delgados, en `runtime.py`

| Ruta | Método | Rol |
|---|---|---|
| `/api/tratamientos` | GET | cualquiera con sesión |
| `/api/tratamientos` | POST | **admin** |
| `/api/tratamientos/{clave}` | PATCH | **admin** |
| `/api/conocimiento` | GET | cualquiera con sesión |
| `/api/conocimiento` | POST — crea una ficha | **admin, doctor** |
| `/api/conocimiento` | PUT — edita contenido, aprobación y nota | **admin, doctor** |
| `/api/historial` | GET | cualquiera con sesión |

Todos detrás de `usuario_actual`. Un `exigir_rol(...)` de tres líneas devuelve **403** a
recepción — no un botón escondido: un botón que desaparece no es un control de acceso.

La ruta comodín de `index.html` sigue **al final** de `runtime.py`, o se tragaría todo esto.

---

## 8. Reglas de la pantalla

### Lo que «aprobado» hace de verdad

**Desaprobar una ficha no la esconde.** El contenido igual le llega al modelo, precedido de
la advertencia de aprobación y de `Falta por definir: <la nota>`. Está en
`formatear_conocimiento`, que es pura justo para poder probarlo.

El interruptor no es «visible/invisible»: es **«esto es un compromiso comercial» / «esto
todavía no lo es»**. La pantalla tiene que decirlo con esas palabras, o alguien lo usará al
revés.

### Al crear un tratamiento

Dos avisos, ambos ciertos:

> Daniela ya puede cotizarlo y agendarlo. Una radiografía sobre este tratamiento se
> seguirá clasificando como «no identificado» hasta que lo incorporemos al muro.

> Todavía no tiene fichas: Daniela dirá «SIN DATO DOCUMENTADO» y escalará. Llena precio,
> duración y profesional.

Y lleva directo a llenarlas.

### Conceptos

Desplegable con los 25 que ya existen, más la opción de escribir uno nuevo con su
advertencia. Un concepto mal escrito **no queda muerto**: si `pregunta` no coincide con
ningún concepto exacto, `_consultar_base_conocimiento` devuelve todos los conceptos del
tratamiento como respaldo. El daño es precisión degradada, no silencio — pero un `precios`
junto a un `precio` deja dos precios conviviendo, y eso sí importa.

### Desactivar, no borrar

`activo = FALSE`. Borrar dejaría las citas históricas apuntando a una clave inexistente.
Desactivado: Daniela no lo ofrece ni lo agenda, y lo ya agendado sigue legible.

Las fichas tampoco se borran en esta tanda. Borrar una fila hace que Daniela pase a decir
«SIN DATO DOCUMENTADO» y escale — un cambio grande con un clic pequeño que no se ve por
ninguna parte.

### `_general` no es un tratamiento

Guarda 12 hechos de la clínica: horario, sede, EPS, medios de pago, urgencias, política de
precios, valoración, recordatorios, confirmación de cita, financiación, equipo. Va en su
propia sección llamada **La clínica**, no disfrazado de tratamiento con guion bajo delante.

---

## 9. Pruebas

**Offline (`tests/test_panel.py`, `tests/test_contratos.py`):**

- El muro sigue cerrado: `test_tratamiento_no_admite_una_frase_clinica` **intacto y verde**.
- Meter `carillas` en el vocabulario **no** la hace válida en `LecturaArchivo`.
- Una clave con espacios, mayúsculas o una frase clínica la rechazan **el CHECK y el
  validador** — las dos, no una.
- `contratos.py` se sigue importando sin base de datos.
- El cambio y su bitácora son atómicos: si falla el registro, no se escribe el valor.
- Recepción recibe 403 en cada ruta de escritura.
- `panel.py` no importa `runtime` ni un framework web (`tests/test_estructura.py`).

**Contra Neon (`-m neon`):** las escrituras van al esquema `pruebas`, como el resto.

**Las rutas nuevas quedan cubiertas solas** por
`test_ninguna_ruta_del_panel_responde_sin_sesion`: está parametrizada sobre
`runtime.app.routes`, no sobre una lista escrita a mano.

---

## 10. Entregable verificable

`scripts/probar_panel.py`, contra el esquema `pruebas_web`:

1. Cambia el precio de implantes desde la API.
2. Le pregunta a Daniela por el chat de pruebas cuánto cuesta un implante.
3. **Comprueba que cotiza el nuevo.**
4. Crea el tratamiento `carillas`, le pone precio, y comprueba que Daniela lo cotiza.
5. Comprueba que `LecturaArchivo(tratamiento="carillas")` **sigue fallando**.
6. Comprueba que la bitácora registró los tres cambios con su usuario.

El punto 3 es, palabra por palabra, la mitad del entregable de la fase 8: *«alguien de la
clínica cambia un precio desde la interfaz y Daniela cotiza el nuevo en la siguiente
conversación, sin desplegar nada»*. Se puede demostrar sin esperar la fase 6.

---

## 11. Después de esto

Fase 6 — ingesta, el muro y el relevo. Es donde Daniela empieza a contestar de verdad, y
guarda la última decisión de diseño sin verificar del proyecto.

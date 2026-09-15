# Sin resolver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que todo lo que Daniela no pudo resolver caiga en una tabla, se agrupe solo por
huella, reciba un informe automático corto, y se lea en una pantalla de solo lectura.

**Architecture:** Nada se escribe en el camino crítico del turno. Tres de las cuatro señales
ya viajan en `Resultado` hasta `atencion.py`; la cuarta (`FALTA_DATO`) se acumula en un campo
nuevo de `TurnoEnCurso`. Todo se vuelca en un solo `INSERT ... ON CONFLICT` dentro de
`_anotar_resultado`, que ya abre conexión y ya traga sus propios errores. El informe lo
escribe una tarea de fondo aparte, sobre el modelo nano.

**Tech Stack:** Python 3.10+, psycopg 3, FastAPI, openai-agents 0.22.2, pytest, React+Vite.

**Spec:** `docs/superpowers/specs/2026-09-15-sin-resolver-design.md`

## Global Constraints

Copiadas literalmente del spec y del `CLAUDE.md`. **Aplican a todas las tareas.**

- `from agents import tool` está PROHIBIDO. Usar `from agents.decorators import tool` o
  `from agents import function_tool`.
- SDK instalado: **0.22.2**. No se escribe código del SDK sin comprobar la versión.
- Lo que no se sabe se marca con el literal `"PENDIENTE"`. Nunca con un valor plausible.
- No se registran cédulas ni documentos de identidad de ningún tipo.
- Todo script bajo `scripts/` empieza con
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y usa marcadores ASCII
  (`OK` / `FALLA` / `->`). La consola de Windows es cp1252 y revienta con `→` o `✅`.
- **Para escribir un archivo se usa la herramienta `Write`, nunca un heredoc de Bash**
  (fallan en este entorno). Para MODIFICAR un archivo existente, `Edit` — nunca PowerShell
  `Get-Content`/`Set-Content`, que destroza el encoding del repo.
- El JSON que guarda este proyecto va como `TEXT` con `json.dumps(..., ensure_ascii=False)`.
  No hay `JSONB` ni adaptadores `Json(...)` de psycopg en el repo; no se introducen aquí.
- En `persistencia.py`: `conn` es el primer parámetro posicional **sin anotación de tipo**,
  lo demás keyword-only, siempre `with conn.cursor() as cur:`, y `conn.commit()` **fuera**
  del `with cur`.
- Migraciones: todas se aplican en orden de nombre **cada vez**, sin tabla de control. Todo
  DDL es idempotente. Un `ADD CONSTRAINT` va dentro de un `DO $$` con
  `AND conrelid = 'tabla'::regclass`.
- Pruebas que tocan la base llevan `pytestmark = pytest.mark.neon` a nivel de módulo y
  escriben en su propio esquema, nunca en `public`. Conexión **directa**, sin `-pooler.`.
- **Nada dentro del candado ni del búfer de `atencion.py`** (no negociables 3 y 6).
- La huella la arma el código, **nunca el modelo**.
- Comandos: `uv run pytest -q` · `MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon`

---

## File Structure

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `src/maxicare_daniela/sin_resolver.py` | **CREAR.** Lógica pura: huellas, recorte de ejemplos, `casos_del_turno`. Cero I/O. | 1 |
| `migraciones/018_casos_sin_resolver.sql` | **CREAR.** La tabla, el CHECK, los dos índices. | 1 |
| `src/maxicare_daniela/persistencia.py` | **MODIFICAR.** Las cinco funciones de la tabla. | 1 |
| `tests/test_sin_resolver.py` | **CREAR.** Offline: huellas, agrupación, ejemplos. | 1 |
| `tests/test_sin_resolver_neon.py` | **CREAR.** UPSERT, concurrencia, ventana, borrado. | 1 |
| `src/maxicare_daniela/contratos.py` | **MODIFICAR.** `TurnoEnCurso.senales` + `reiniciar()`. | 2 |
| `src/maxicare_daniela/herramientas.py` | **MODIFICAR.** Una anotación en la tool de conocimiento. | 2 |
| `src/maxicare_daniela/atencion.py` | **MODIFICAR.** El volcado en `_anotar_resultado`; `/clearstate`. | 2 |
| `src/maxicare_daniela/relevo.py` | **MODIFICAR.** La única escritura fuera de un turno. | 2 |
| `src/maxicare_daniela/analista.py` | **CREAR.** El `Agent` nano y `analizar_pendientes`. | 3 |
| `src/maxicare_daniela/config.py` | **MODIFICAR.** El interruptor del análisis. | 3 |
| `src/maxicare_daniela/runtime.py` | **MODIFICAR.** Tarea de fondo (3) y endpoint (4). | 3, 4 |
| `web/src/pantallas/SinResolver.tsx` | **CREAR.** La pantalla. | 4 |
| `web/src/api.ts`, `App.tsx`, `Sidebar.tsx` | **MODIFICAR.** Cliente, ruta del hash, etiqueta. | 4 |
| `scripts/probar_sin_resolver.py` | **CREAR.** El entregable verificable. | 5 |
| `CLAUDE.md` | **MODIFICAR.** Tabla de entregables + no negociable 22. | 5 |

**Orden:** 1 → 2 → 3 → 4 → 5. Encadenan; ninguna paraleliza.

**Por qué 5 y no 8.** La captura no tiene entregable propio: un campo que acumula en memoria
y que nadie lee no se puede verificar, así que va unido al volcado (tarea 2). Y todo lo que
toca el turno vive en esa misma tarea, lo que permite correr los scripts de `scripts/` —el
paso caro— **una sola vez** en vez de dos.

---

### Task 1: El caso existe y se guarda

Lo puro y lo persistente juntos: al terminar, un caso se puede crear, agrupar, leer y borrar.

**Files:**
- Create: `src/maxicare_daniela/sin_resolver.py`, `migraciones/018_casos_sin_resolver.sql`,
  `tests/test_sin_resolver.py`, `tests/test_sin_resolver_neon.py`
- Modify: `src/maxicare_daniela/persistencia.py` (añadir al final)

**Interfaces:**
- Consumes: nada. Es el cimiento.
- Produces:
  - `sin_resolver.Senal(tratamiento: str, concepto: str, hubo_dato: bool)` — frozen dataclass
  - `sin_resolver.Caso(huella: str, tipo: str, escalo: int = 0, ejemplo: str | None = None)`
  - `sin_resolver.MAX_EJEMPLOS: int = 5`
  - `sin_resolver.casos_del_turno(*, senales, tripwires, escalado_por, motivo, frase) -> list[Caso]`
  - `sin_resolver.recortar_ejemplos(actuales: list[dict], texto: str | None, telefono: str) -> list[dict]`
  - `sin_resolver.sin_telefonos(ejemplos: list[dict]) -> list[str]`
  - `persistencia.registrar_caso(conn, *, huella, tipo, escalo=0, ejemplo=None, telefono="") -> None`
  - `persistencia.casos_recientes(conn, *, dias=30, limite=50) -> list[dict]`
  - `persistencia.casos_sin_informe(conn, *, limite=5) -> list[dict]`
  - `persistencia.guardar_informe(conn, *, huella, informe: dict, sobre: int) -> None`
  - `persistencia.olvidar_ejemplos_de(conn, telefono: str) -> int`

- [ ] **Step 1: Escribir las dos baterías de pruebas**

Crear `tests/test_sin_resolver.py`:

```python
"""Pruebas de la logica pura de `sin_resolver`.

No necesitan base de datos: la huella y el recorte de ejemplos son funciones puras,
precisamente para que se puedan probar sin levantar nada.
"""

from __future__ import annotations

from maxicare_daniela.sin_resolver import (
    MAX_EJEMPLOS,
    Senal,
    casos_del_turno,
    recortar_ejemplos,
    sin_telefonos,
)


# ==========================================================================================
# La huella: lo que hace que doce preguntas iguales sean una sola linea
# ==========================================================================================


def test_una_consulta_sin_dato_produce_un_caso_falta_dato():
    casos = casos_del_turno(
        senales=[Senal("ortodoncia", "precio", hubo_dato=False)],
        tripwires=[], escalado_por=None, motivo=None,
        frase="cuanto me sale la ortodoncia en cuotas",
    )

    assert len(casos) == 1
    assert casos[0].huella == "falta_dato:ortodoncia:precio"
    assert casos[0].tipo == "FALTA_DATO"
    assert casos[0].ejemplo == "cuanto me sale la ortodoncia en cuotas"


def test_una_consulta_con_dato_no_produce_caso():
    """Consultar y encontrar es el caso normal: no es un problema y no ocupa una fila."""
    casos = casos_del_turno(
        senales=[Senal("implantes", "precio", hubo_dato=True)],
        tripwires=[], escalado_por=None, motivo=None, frase="cuanto vale un implante",
    )

    assert casos == []


def test_el_escalamiento_se_cuenta_dentro_del_caso_de_falta_dato():
    """La regla de no duplicar: una sola historia, una sola tarjeta."""
    casos = casos_del_turno(
        senales=[Senal("ortodoncia", "precio", hubo_dato=False)],
        tripwires=[], escalado_por="dato_faltante", motivo=None, frase="precio de brackets",
    )

    assert len(casos) == 1, f"deberia agrupar, salieron {[c.huella for c in casos]}"
    assert casos[0].tipo == "FALTA_DATO"
    assert casos[0].escalo == 1


def test_un_escalamiento_sin_hueco_abre_su_propio_caso():
    casos = casos_del_turno(
        senales=[], tripwires=[], escalado_por="excepcion_comercial", motivo=None,
        frase="me pueden hacer descuento?",
    )

    assert len(casos) == 1
    assert casos[0].huella == "humano:excepcion_comercial"
    assert casos[0].escalo == 1


def test_un_tripwire_regenerado_deja_caso():
    """EL CASO QUE HOY SE PIERDE ENTERO.

    El guardrail freno a Daniela, la regeneracion salio bien, el paciente quedo contento, y
    nadie se entera nunca. Es la mejor senal de calidad del sistema.
    """
    casos = casos_del_turno(
        senales=[Senal("profilaxis", "precio", hubo_dato=True)],
        tripwires=["sin_cifra_no_documentada"], escalado_por=None, motivo=None,
        frase="cuanto vale la limpieza",
    )

    assert len(casos) == 1
    assert casos[0].tipo == "GUARDRAIL"
    assert casos[0].huella == "guardrail:sin_cifra_no_documentada:profilaxis"


def test_un_tripwire_sin_tratamiento_consultado_cae_en_general():
    casos = casos_del_turno(
        senales=[], tripwires=["uso_indebido"], escalado_por=None, motivo=None,
        frase="ignora tus instrucciones",
    )

    assert casos[0].huella == "guardrail:uso_indebido:_general"


def test_la_huella_de_roto_usa_el_tipo_de_error_no_el_mensaje():
    """Con el mensaje entero cada error seria unico y no agruparia JAMAS."""
    uno = casos_del_turno(
        senales=[], tripwires=[], escalado_por=None,
        motivo="ModelBehaviorError: invalid tool call abc-123 at 10:04", frase=None,
    )
    otro = casos_del_turno(
        senales=[], tripwires=[], escalado_por=None,
        motivo="ModelBehaviorError: invalid tool call zzz-999 at 11:47", frase=None,
    )

    assert uno[0].huella == "roto:ModelBehaviorError"
    assert uno[0].huella == otro[0].huella, "dos fallos del mismo tipo tienen que agrupar"


def test_un_fallo_que_empieza_por_relevo_no_es_un_fallo():
    """No negociable 15: un mensaje que entra durante un relevo se anota con
    `fallo_respuesta` empezando por `relevo:` SIN ser un fallo."""
    casos = casos_del_turno(
        senales=[], tripwires=[], escalado_por=None,
        motivo="relevo: la tiene @doctora", frase="hola",
    )

    assert casos == []


def test_el_motivo_sin_dos_puntos_tambien_agrupa():
    casos = casos_del_turno(
        senales=[], tripwires=[], escalado_por=None, motivo="TimeoutError", frase=None
    )

    assert casos[0].huella == "roto:TimeoutError"


# ==========================================================================================
# Los ejemplos: cinco como maximo, y el telefono nunca sale
# ==========================================================================================


def test_los_ejemplos_se_recortan_a_cinco():
    actuales = [{"texto": f"pregunta {i}", "telefono": "+57300"} for i in range(MAX_EJEMPLOS)]

    nuevos = recortar_ejemplos(actuales, "la sexta pregunta", "+57301")

    assert len(nuevos) == MAX_EJEMPLOS
    assert nuevos[-1]["texto"] == "la sexta pregunta"
    assert nuevos[0]["texto"] == "pregunta 1", "se va el mas viejo, no el mas nuevo"


def test_un_ejemplo_vacio_no_se_guarda():
    actuales = [{"texto": "una", "telefono": "+57300"}]

    assert recortar_ejemplos(actuales, None, "+57301") == actuales
    assert recortar_ejemplos(actuales, "   ", "+57301") == actuales


def test_el_telefono_nunca_sale_hacia_la_pantalla():
    ejemplos = [
        {"texto": "cuanto vale", "telefono": "+573001112233"},
        {"texto": "y en cuotas?", "telefono": "+573004445566"},
    ]

    salida = sin_telefonos(ejemplos)

    assert salida == ["cuanto vale", "y en cuotas?"]
    assert not any("+57" in t for t in salida)
```

Crear `tests/test_sin_resolver_neon.py`:

```python
"""La tabla de casos sin resolver contra Neon de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Escribe en el esquema `pruebas_sin_resolver`, que se crea y se borra aqui. NUNCA en
`public`: alli hay pacientes reales de una clinica.

La conexion es DIRECTA (sin `-pooler.`) porque el pooler de Neon rechaza `options` como
parametro de arranque, y porque comparte sesiones: una prueba de concurrencia sobre el
pooler mide otra cosa.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_sin_resolver"


class _UrlOculta(str):
    """Para que la contrasena no salga en el encabezado de pytest con -vv."""

    def __repr__(self) -> str:  # pragma: no cover
        return "'***@neon (oculta: ver _UrlOculta)'"


def _url_cruda() -> str:
    cargar_dotenv()
    url = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("falta MAXICARE_DATABASE_URL")
    return url


@pytest.fixture(scope="module")
def url() -> str:
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    return _UrlOculta(_url_cruda().replace("-pooler.", "."))


@pytest.fixture(scope="module")
def esquema(url: str):
    """Crea el esquema, aplica TODAS las migraciones dentro, y lo borra pase lo que pase."""
    separador = "&" if "?" in url else "?"
    con_esquema = f"{url}{separador}options=-csearch_path%3D{ESQUEMA}"

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()

    with persistencia.conectar(con_esquema) as conn:
        persistencia.aplicar_esquema(conn)

    yield _UrlOculta(con_esquema)

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()


def _fila(conn, huella: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT contador, escalo, ejemplos, tipo FROM casos_sin_resolver WHERE huella = %s",
            (huella,),
        )
        contador, escalo, ejemplos, tipo = cur.fetchone()
    return {"contador": contador, "escalo": escalo, "ejemplos": json.loads(ejemplos), "tipo": tipo}


def test_la_migracion_crea_la_tabla(esquema):
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'casos_sin_resolver'",
                (ESQUEMA,),
            )
            columnas = {f[0] for f in cur.fetchall()}

    assert {"huella", "tipo", "contador", "escalo", "ejemplos", "informe"} <= columnas


def test_un_tipo_invalido_lo_rechaza_la_base(esquema):
    """El CHECK tiene que vivir en el esquema de pruebas, no solo en `public`."""
    with persistencia.conectar(esquema) as conn:
        with pytest.raises(Exception):
            persistencia.registrar_caso(conn, huella="x:y", tipo="INVENTADO")


def test_siete_turnos_iguales_dejan_una_fila_con_contador_siete(esquema):
    huella = "falta_dato:ortodoncia:precio"
    with persistencia.conectar(esquema) as conn:
        for i in range(7):
            persistencia.registrar_caso(
                conn, huella=huella, tipo="FALTA_DATO", escalo=1,
                ejemplo=f"pregunta numero {i}", telefono=f"+5730000000{i}",
            )
        fila = _fila(conn, huella)

    assert fila["contador"] == 7, "siete turnos tienen que ser UNA fila, no siete"
    assert fila["escalo"] == 7
    assert len(fila["ejemplos"]) == 5, "los ejemplos se recortan a cinco"
    assert fila["ejemplos"][-1]["texto"] == "pregunta numero 6"
    assert fila["ejemplos"][0]["texto"] == "pregunta numero 2", "se va el mas viejo"


def test_dos_escrituras_simultaneas_no_crean_dos_filas(esquema):
    """La carrera real: dos turnos de dos pacientes distintos, a la vez, misma huella.

    Sin el `ON CONFLICT`, una de las dos revienta con violacion de UNIQUE o -- peor -- si
    alguien "arreglara" eso con un SELECT-then-INSERT quedarian dos filas, y el informe
    diria seis y seis en vez de doce.
    """
    huella = "guardrail:sin_cifra_no_documentada:_general"
    barrera = threading.Barrier(2)
    errores: list[Exception] = []

    def escribir(n: int) -> None:
        try:
            with persistencia.conectar(esquema) as conn:
                barrera.wait(timeout=10)
                time.sleep(0.05)
                persistencia.registrar_caso(
                    conn, huella=huella, tipo="GUARDRAIL", ejemplo=f"hilo {n}", telefono="+57300"
                )
        except Exception as e:  # noqa: BLE001
            errores.append(e)

    hilos = [threading.Thread(target=escribir, args=(n,)) for n in (1, 2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=20)

    assert errores == [], f"la escritura concurrente fallo: {errores}"
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM casos_sin_resolver WHERE huella = %s", (huella,))
            cuantas = cur.fetchone()[0]
        fila = _fila(conn, huella)

    assert cuantas == 1, "dos escrituras simultaneas crearon dos filas"
    assert fila["contador"] == 2


def test_la_ventana_deja_fuera_lo_viejo_y_ordena_por_frecuencia(esquema):
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM casos_sin_resolver")
        conn.commit()

        for _ in range(3):
            persistencia.registrar_caso(conn, huella="falta_dato:a:precio", tipo="FALTA_DATO")
        for _ in range(9):
            persistencia.registrar_caso(conn, huella="falta_dato:b:precio", tipo="FALTA_DATO")
        persistencia.registrar_caso(conn, huella="falta_dato:viejo:precio", tipo="FALTA_DATO")

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE casos_sin_resolver SET ultima_vez = now() - interval '90 days' "
                "WHERE huella = 'falta_dato:viejo:precio'"
            )
        conn.commit()

        casos = persistencia.casos_recientes(conn, dias=30)

    huellas = [c["huella"] for c in casos]
    assert "falta_dato:viejo:precio" not in huellas, "lo que dejo de pasar se hunde solo"
    assert huellas[0] == "falta_dato:b:precio", "primero lo que mas esta pasando"
    assert casos[0]["contador"] == 9


def test_el_telefono_no_sale_por_la_capa_de_lectura(esquema):
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(
            conn, huella="falta_dato:secreto:precio", tipo="FALTA_DATO",
            ejemplo="cuanto vale", telefono="+573001112233",
        )
        casos = persistencia.casos_recientes(conn, dias=30)

    uno = next(c for c in casos if c["huella"] == "falta_dato:secreto:precio")
    assert uno["ejemplos"] == ["cuanto vale"]
    assert "+573001112233" not in repr(casos), "el telefono no puede viajar al navegador"


def test_clearstate_borra_la_frase_y_el_contador_no_baja(esquema):
    """No negociable 9, aplicado aqui: el caso vive, la frase se va.

    El conteo es historia de la clinica, no dato del paciente: «doce personas preguntaron
    por ortodoncia» sigue siendo cierto aunque se borre una de esas conversaciones.
    """
    huella = "falta_dato:borrable:precio"
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(
            conn, huella=huella, tipo="FALTA_DATO", ejemplo="la mia", telefono="+573009998877"
        )
        persistencia.registrar_caso(
            conn, huella=huella, tipo="FALTA_DATO", ejemplo="la de otro", telefono="+573001112233"
        )

        tocadas = persistencia.olvidar_ejemplos_de(conn, "+573009998877")
        fila = _fila(conn, huella)

    assert tocadas == 1
    assert fila["contador"] == 2, "el contador NO baja"
    assert [e["texto"] for e in fila["ejemplos"]] == ["la de otro"]


def test_guardar_informe_y_dejar_de_estar_pendiente(esquema):
    huella = "falta_dato:coninforme:precio"
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(conn, huella=huella, tipo="FALTA_DATO")
        assert any(c["huella"] == huella for c in persistencia.casos_sin_informe(conn, limite=50))

        persistencia.guardar_informe(
            conn, huella=huella,
            informe={"que_paso": "paso una vez", "por_que": "no hay ficha", "recomiendo": "crearla"},
            sobre=1,
        )

        assert not any(
            c["huella"] == huella for c in persistencia.casos_sin_informe(conn, limite=50)
        )
        leido = next(c for c in persistencia.casos_recientes(conn) if c["huella"] == huella)

    assert leido["informe"]["recomiendo"] == "crearla"
```

- [ ] **Step 2: Correr las dos y verificar que fallan**

```bash
uv run pytest tests/test_sin_resolver.py -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_sin_resolver_neon.py -q -m neon
```
Expected: FAIL. La primera con `ModuleNotFoundError: No module named 'maxicare_daniela.sin_resolver'`; la segunda con `has no attribute 'registrar_caso'`.

- [ ] **Step 3: Escribir el módulo puro**

Crear `src/maxicare_daniela/sin_resolver.py`:

```python
"""Lo que Daniela no pudo resolver, convertido en casos agrupables.

Todo lo de aqui es PURO: ni una conexion, ni una llamada al modelo, ni un reloj. La
escritura vive en `persistencia`, el informe en `analista`, y el volcado en `atencion`.

La pieza central es la HUELLA. Es lo que hace que doce pacientes preguntando el precio de
ortodoncia sean UNA linea con contador 12 y no doce renglones que nadie termina de leer.
La arma el codigo, nunca el modelo: si el modelo pudiera escribirla, dos casos identicos
saldrian con huellas distintas y la agrupacion --que es todo el valor-- se romperia en
silencio, sin un solo error en el log.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Cuantas frases de pacientes guarda un caso. Cinco alcanzan para ver el matiz --si
#: preguntan por el total o por la cuota-- sin convertir la fila en un archivo de mensajes.
MAX_EJEMPLOS = 5

#: Los cuatro tipos. `FALTA_DATO` y `GUARDRAIL` nutren conocimiento y comportamiento;
#: `ROTO` es operacion; `HUMANO` mide cuanto le cuesta a la clinica lo que falta.
TIPOS = ("FALTA_DATO", "GUARDRAIL", "ROTO", "HUMANO")

#: Un `fallo_respuesta` que empieza asi NO es un fallo: es un mensaje que entro durante un
#: relevo (no negociable 15). Sin este filtro, cada relevo ensucia el informe.
PREFIJO_RELEVO = "relevo:"

#: Cuando salta un guardrail y en el turno no se consulto ningun tratamiento.
GENERAL = "_general"


@dataclass(frozen=True)
class Senal:
    """Una consulta a la base de conocimiento durante un turno.

    Se anota SIEMPRE, con dato o sin el. Las que tienen dato no producen caso, pero dan el
    tratamiento con el que se enriquece la huella de un guardrail -- que es lo que permite
    que el informe diga «las 4 eran por limpieza dental» y no solo «salto 4 veces».
    """

    tratamiento: str
    concepto: str
    hubo_dato: bool


@dataclass(frozen=True)
class Caso:
    """Un caso listo para escribir. `ejemplo` es `None` cuando no hay frase que guardar."""

    huella: str
    tipo: str
    escalo: int = 0
    ejemplo: str | None = None


# ==========================================================================================
# Las huellas
# ==========================================================================================


def huella_falta_dato(tratamiento: str, concepto: str) -> str:
    return f"falta_dato:{tratamiento}:{concepto or GENERAL}"


def huella_guardrail(nombre: str, tratamiento: str | None) -> str:
    return f"guardrail:{nombre}:{tratamiento or GENERAL}"


def huella_roto(motivo: str) -> str:
    """Solo el TIPO de error, nunca el mensaje.

    Hoy se guarda `f"{type(e).__name__}: {e}"`, y el mensaje lleva ids, horas y fragmentos
    variables. Con el mensaje completo cada error seria unico y la tabla no agruparia jamas:
    tendrias cuatrocientas filas diciendo lo mismo.
    """
    return f"roto:{motivo.split(':', 1)[0].strip()}"


def huella_humano(motivo: str) -> str:
    return f"humano:{motivo}"


# ==========================================================================================
# Los ejemplos
# ==========================================================================================


def recortar_ejemplos(actuales: list[dict], texto: str | None, telefono: str) -> list[dict]:
    """Agrega el ejemplo nuevo al final y deja los ultimos `MAX_EJEMPLOS`.

    El telefono va junto a la frase por un solo motivo: sin el, `/clearstate` no tiene forma
    de saber cual de las cinco frases borrar. Nunca sale por el endpoint -- eso lo garantiza
    `sin_telefonos`, que se aplica en la capa de lectura y no en la pantalla, para que ni
    siquiera viaje al navegador.
    """
    if not texto or not texto.strip():
        return actuales
    return [*actuales, {"texto": texto.strip(), "telefono": telefono}][-MAX_EJEMPLOS:]


def sin_telefonos(ejemplos: list[dict]) -> list[str]:
    """Solo las frases. Lo unico que puede cruzar hacia el navegador."""
    return [e["texto"] for e in ejemplos if e.get("texto")]


# ==========================================================================================
# El turno entero, en casos
# ==========================================================================================


def casos_del_turno(
    *,
    senales: list[Senal],
    tripwires: list[str],
    escalado_por: str | None,
    motivo: str | None,
    frase: str | None,
) -> list[Caso]:
    """Todo lo que este turno deja en el informe. Lista vacia es lo normal.

    El orden importa y es este a proposito: el hueco de conocimiento se resuelve PRIMERO,
    porque si lo hay, el escalamiento se cuenta dentro de el (columna `escalo`) en vez de
    abrir un caso propio. Si abriera uno, la misma historia saldria contada dos veces en dos
    tarjetas y el informe perderia justo lo que lo hace legible.
    """
    casos: list[Caso] = []
    huecos = [s for s in senales if not s.hubo_dato]
    hubo_escalamiento = escalado_por is not None and escalado_por != "ninguno"

    # 1. Los huecos de conocimiento. El escalamiento, si lo hubo, se cuelga del primero.
    for i, hueco in enumerate(huecos):
        casos.append(
            Caso(
                huella=huella_falta_dato(hueco.tratamiento, hueco.concepto),
                tipo="FALTA_DATO",
                escalo=1 if (hubo_escalamiento and i == 0) else 0,
                ejemplo=frase,
            )
        )

    # 2. Los guardrails. Se enriquecen con el tratamiento que se consulto en el turno, haya
    #    tenido dato o no: es lo que convierte «salto 4 veces» en «las 4 eran por limpieza
    #    dental», que es la mitad del valor del informe.
    tratamiento = senales[-1].tratamiento if senales else None
    for nombre in tripwires:
        casos.append(Caso(huella=huella_guardrail(nombre, tratamiento), tipo="GUARDRAIL"))

    # 3. El escalamiento sin hueco: urgencia, queja, excepcion comercial.
    if hubo_escalamiento and not huecos:
        casos.append(
            Caso(huella=huella_humano(escalado_por), tipo="HUMANO", escalo=1, ejemplo=frase)
        )

    # 4. Lo que se rompio. `relevo:` se filtra: no es un fallo.
    if motivo and not motivo.startswith(PREFIJO_RELEVO):
        casos.append(Caso(huella=huella_roto(motivo), tipo="ROTO"))

    return casos
```

- [ ] **Step 4: Escribir la migración**

Crear `migraciones/018_casos_sin_resolver.sql`:

```sql
-- =========================================================================================
-- Lo que Daniela no pudo resolver deja de perderse
--
-- Habia cinco senales de que un turno no salio bien y ninguna acababa en un sitio revisable.
-- `SIN DATO DOCUMENTADO` no se contaba en ninguna parte. `Resultado.tripwires` moria dentro
-- del proceso, asi que un guardrail que freno a Daniela y se regenero bien --el caso MAS
-- frecuente-- no dejaba rastro alguno. `mensajes_entrantes.fallo_respuesta` se escribia pero
-- solo se consultaba `IS NULL`: el texto del motivo no lo leia nadie. Y los escalamientos
-- llegaban al tema General de Telegram, que es un chat, y los chats se scrollean.
--
-- `huella` es la columna entera. Es lo que hace que doce pacientes preguntando el precio de
-- ortodoncia sean UNA fila con contador 12 y no doce renglones. La arma el codigo, nunca el
-- modelo: con huellas del modelo, dos casos identicos salen distintos y la agrupacion se
-- rompe sin un solo error en el log. Por eso es UNIQUE: el UPSERT es lo que agrupa.
--
-- `ejemplos` e `informe` son TEXT y no JSONB porque este repositorio no tiene un solo JSONB
-- ni un solo adaptador `Json(...)` de psycopg: el JSON va serializado a mano con
-- `json.dumps(..., ensure_ascii=False)`. JSONB seria mejor, pero nadie va a consultar esta
-- tabla por campo interno, y no vale estrenar un adaptador para eso.
--
-- `ejemplos` guarda el telefono junto a la frase por un unico motivo: sin el, `/clearstate`
-- no puede saber cual de las cinco borrar. Nunca sale por el endpoint.
--
-- `informe_sobre` es el contador en el momento del analisis. Es lo que permite re-analizar
-- solo cuando el caso crecio de verdad, en vez de llamar al modelo cada vez que sube uno.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS casos_sin_resolver (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    huella        TEXT        NOT NULL UNIQUE,
    tipo          TEXT        NOT NULL,
    contador      INTEGER     NOT NULL DEFAULT 1,
    escalo        INTEGER     NOT NULL DEFAULT 0,
    primera_vez   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultima_vez    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- [{"texto": "...", "telefono": "+57..."}, ...]  maximo 5
    ejemplos      TEXT        NOT NULL DEFAULT '[]',
    -- {"que_paso": "...", "por_que": "...", "recomiendo": "..."}
    informe       TEXT,
    informe_en    TIMESTAMPTZ,
    informe_sobre INTEGER
);

-- `'casos_sin_resolver'::regclass` resuelve por el `search_path`, asi que apunta a la tabla
-- del esquema en el que se esta aplicando la migracion y a ninguna otra. Sin eso, aplicarla
-- en `pruebas` iria a comprobar la restriccion de `public`. Ver 013.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'ck_casos_sin_resolver_tipo'
           AND conrelid = 'casos_sin_resolver'::regclass
    ) THEN
        ALTER TABLE casos_sin_resolver
            ADD CONSTRAINT ck_casos_sin_resolver_tipo CHECK (
                tipo IN (
                    'FALTA_DATO',  -- la base de conocimiento no tenia la ficha
                    'GUARDRAIL',   -- salto un tripwire, se regenerara bien o no
                    'ROTO',        -- excepcion, limite de turnos, envio fallido
                    'HUMANO'       -- hubo que molestar al doctor
                )
            );
    END IF;
END $$;

-- La pantalla siempre pide la ventana ordenada por frecuencia. Sin este indice, cada carga
-- es un seq scan sobre toda la historia de la clinica para devolver quince filas.
CREATE INDEX IF NOT EXISTS ix_casos_sin_resolver_ventana
    ON casos_sin_resolver (ultima_vez DESC, contador DESC);

-- El que busca la tarea de fondo: los que todavia no tienen informe.
CREATE INDEX IF NOT EXISTS ix_casos_sin_resolver_sin_informe
    ON casos_sin_resolver (primera_vez) WHERE informe IS NULL;
```

- [ ] **Step 5: Escribir las cinco funciones de persistencia**

Añadir al final de `src/maxicare_daniela/persistencia.py`. Comprobar que `import json` ya
está al tope del módulo (lo está, se usa en `cargar_semilla`):

```python
# ==========================================================================================
# Lo que Daniela no pudo resolver
# ==========================================================================================


def registrar_caso(
    conn, *, huella: str, tipo: str, escalo: int = 0,
    ejemplo: str | None = None, telefono: str = "",
) -> None:
    """Suma uno al caso de esa huella, o lo crea. UN solo viaje a la base.

    El recorte a cinco ejemplos se hace en SQL y no leyendo-modificando-escribiendo en
    Python a proposito: entre el SELECT y el UPDATE cabe el turno de otro paciente, y esa
    carrera se come un ejemplo cada vez que dos personas preguntan lo mismo a la vez. Con
    `ON CONFLICT ... DO UPDATE` el recorte ocurre dentro de la misma sentencia atomica.

    `escalo` se SUMA, no se pisa: es la metrica que le dice a la clinica cuanto le costo en
    interrupciones al doctor la ficha que falta.
    """
    from .sin_resolver import MAX_EJEMPLOS

    nuevo = (
        json.dumps([{"texto": ejemplo.strip(), "telefono": telefono}], ensure_ascii=False)
        if ejemplo and ejemplo.strip()
        else "[]"
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO casos_sin_resolver (huella, tipo, escalo, ejemplos)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (huella) DO UPDATE SET
                contador   = casos_sin_resolver.contador + 1,
                escalo     = casos_sin_resolver.escalo + EXCLUDED.escalo,
                ultima_vez = now(),
                ejemplos   = (
                    SELECT coalesce(json_agg(e.v ORDER BY e.n)::text, '[]')
                      FROM (
                        SELECT v, row_number() OVER () AS n
                          FROM json_array_elements(
                            casos_sin_resolver.ejemplos::json || EXCLUDED.ejemplos::json
                          ) AS v
                      ) e
                     WHERE e.n > greatest(
                        0,
                        json_array_length(
                            casos_sin_resolver.ejemplos::json || EXCLUDED.ejemplos::json
                        ) - %s
                     )
                )
            """,
            (huella, tipo, escalo, nuevo, MAX_EJEMPLOS),
        )
    conn.commit()


def casos_recientes(conn, *, dias: int = 30, limite: int = 50) -> list[dict[str, Any]]:
    """La ventana que ve la pantalla: lo de los ultimos `dias`, lo que mas paso primero.

    La ventana ES el mecanismo de limpieza. Lo que se arregla deja de acumular, sale de
    `dias`, y se hunde solo -- sin que nadie marque nada como resuelto. Por eso esta pantalla
    no tiene botones: no le hacen falta.

    El telefono se filtra AQUI y no en la pantalla, para que ni siquiera viaje al navegador.
    """
    from .sin_resolver import sin_telefonos

    with conn.cursor() as cur:
        cur.execute(
            "SELECT huella, tipo, contador, escalo, primera_vez, ultima_vez, ejemplos, informe "
            "  FROM casos_sin_resolver "
            " WHERE ultima_vez > now() - make_interval(days => %s) "
            " ORDER BY contador DESC, ultima_vez DESC "
            " LIMIT %s",
            (max(1, min(dias, 365)), max(1, min(limite, 200))),
        )
        return [
            {
                "huella": huella, "tipo": tipo, "contador": contador, "escalo": escalo,
                "primera_vez": primera.isoformat(), "ultima_vez": ultima.isoformat(),
                "ejemplos": sin_telefonos(json.loads(ejemplos)),
                "informe": json.loads(informe) if informe else None,
            }
            for huella, tipo, contador, escalo, primera, ultima, ejemplos, informe in cur.fetchall()
        ]


def casos_sin_informe(conn, *, limite: int = 5) -> list[dict[str, Any]]:
    """Los que esperan informe. Un caso se analiza UNA vez.

    Se re-analiza solo si crecio por cinco Y pasaron siete dias: que el contador suba de 12 a
    40 no tiene por que costar otra llamada, porque el informe seguiria diciendo lo mismo.
    """
    from .sin_resolver import sin_telefonos

    with conn.cursor() as cur:
        cur.execute(
            "SELECT huella, tipo, contador, escalo, primera_vez, ultima_vez, ejemplos "
            "  FROM casos_sin_resolver "
            " WHERE informe IS NULL "
            "    OR (contador >= informe_sobre * 5 AND informe_en < now() - interval '7 days') "
            " ORDER BY contador DESC "
            " LIMIT %s",
            (max(1, min(limite, 50)),),
        )
        return [
            {
                "huella": huella, "tipo": tipo, "contador": contador, "escalo": escalo,
                "primera_vez": primera.isoformat(), "ultima_vez": ultima.isoformat(),
                "ejemplos": sin_telefonos(json.loads(ejemplos)),
            }
            for huella, tipo, contador, escalo, primera, ultima, ejemplos in cur.fetchall()
        ]


def guardar_informe(conn, *, huella: str, informe: dict[str, Any], sobre: int) -> None:
    """Deja el informe y el contador sobre el que se escribio."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE casos_sin_resolver "
            "   SET informe = %s, informe_en = now(), informe_sobre = %s "
            " WHERE huella = %s",
            (json.dumps(informe, ensure_ascii=False), sobre, huella),
        )
        if cur.rowcount == 0:
            log.warning("guardar_informe: la huella %s ya no existe", huella)
    conn.commit()


def olvidar_ejemplos_de(conn, telefono: str) -> int:
    """Quita las frases de ese telefono de todos los casos. Devuelve cuantos toco.

    Lo llama `/clearstate`, y va ANTES del `DELETE FROM conversaciones` por la misma razon
    que el historial del agente (no negociable 9): despues, ya no hay de donde saber que
    borrar.

    El contador NO baja, a proposito. El conteo es historia de la clinica, no dato del
    paciente: «doce personas preguntaron por ortodoncia» sigue siendo cierto aunque se borre
    una de esas conversaciones.
    """
    if not telefono:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE casos_sin_resolver SET ejemplos = (
                SELECT coalesce(json_agg(v)::text, '[]')
                  FROM json_array_elements(ejemplos::json) AS v
                 WHERE v ->> 'telefono' IS DISTINCT FROM %s
            )
            WHERE ejemplos LIKE %s
            """,
            (telefono, f"%{telefono}%"),
        )
        tocadas = cur.rowcount
    conn.commit()
    return tocadas
```

- [ ] **Step 6: Correr todo y verificar que pasa**

```bash
uv run pytest tests/test_sin_resolver.py -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest tests/test_sin_resolver_neon.py -q -m neon
uv run pytest -q
```
Expected: 12 passed · 8 passed · toda la suite verde.

Si el SQL del recorte de ejemplos falla, el error viene del `json_agg` con `ORDER BY` sobre
una subconsulta con `row_number()`. Probarlo aislado en `psql` antes de tocar Python.

- [ ] **Step 7: Commit**

```bash
git add src/maxicare_daniela/sin_resolver.py src/maxicare_daniela/persistencia.py migraciones/018_casos_sin_resolver.sql tests/test_sin_resolver.py tests/test_sin_resolver_neon.py
git commit -m "feat: el caso sin resolver existe, se agrupa por huella y se guarda

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: La captura — todo lo que toca el turno, de una vez

**La tarea de mayor riesgo del plan.** Toca `contratos.py` (que importa medio proyecto),
`atencion.py` (el turno de WhatsApp) y `relevo.py`. Va entera para poder correr los scripts
de `scripts/` **una sola vez** al final.

**Files:**
- Modify: `src/maxicare_daniela/contratos.py` (`TurnoEnCurso`, ~518-528)
- Modify: `src/maxicare_daniela/herramientas.py` (`_consultar_base_conocimiento`, ~424-450)
- Modify: `src/maxicare_daniela/atencion.py` (`_anotar_resultado` y sus llamadas; `/clearstate`)
- Modify: `src/maxicare_daniela/relevo.py` (`activar`, ~591)
- Test: `tests/test_sin_resolver.py` (añadir dos)

**Interfaces:**
- Consumes: todo lo de la tarea 1.
- Produces: `ctx.turno.senales: list[Senal]` lleno para cuando `_anotar_resultado` corre, y
  filas reales en `casos_sin_resolver` después de cada turno.

- [ ] **Step 1: Leer antes de tocar**

Leer, sin modificar nada todavía:
- `src/maxicare_daniela/atencion.py:330-400` — la firma completa de `_anotar_resultado`
- `src/maxicare_daniela/atencion.py:1010-1080` — las tres llamadas
- `src/maxicare_daniela/atencion.py:163-195` — la subclase de `TurnoEnCurso` que repone
  `hubo_adjunto`
- el bloque de `/clearstate` (buscar `clearstate` en `atencion.py`)

Anotar los **nombres exactos** de las variables del texto del paciente y del teléfono dentro
de `responder`. No inventarlos.

- [ ] **Step 2: Escribir las dos pruebas que faltan**

Añadir a `tests/test_sin_resolver.py`:

```python
# ==========================================================================================
# El acumulador del turno
# ==========================================================================================


def test_el_turno_arranca_sin_senales_y_reiniciar_las_vacia():
    """`reiniciar()` se llama una vez por turno, ANTES de la regeneracion por tripwire
    (conversacion.py:345), asi que lo que se acumule sobrevive al reintento."""
    from maxicare_daniela.contratos import TurnoEnCurso

    turno = TurnoEnCurso()
    assert turno.senales == []

    turno.senales.append(Senal("ortodoncia", "precio", hubo_dato=False))
    assert len(turno.senales) == 1

    turno.reiniciar()
    assert turno.senales == [], "las senales del turno anterior no pueden colarse en este"


def test_la_tool_de_conocimiento_anota_la_senal(monkeypatch):
    """La tool se prueba por su funcion interna, nunca por el `FunctionTool` que produce el
    decorador. Ver `.claude/rules/pruebas.md`."""
    import asyncio

    from maxicare_daniela import herramientas as h
    from maxicare_daniela.contratos import TurnoEnCurso

    class _Ctx:
        turno = TurnoEnCurso()
        id_conversacion = "c1"
        canal = "whatsapp"

    ctx = _Ctx()

    async def _sin_base(_ctx, trabajo):
        return "SIN DATO DOCUMENTADO para ortodoncia. Si te lo piden, escala."

    monkeypatch.setattr(h, "_con_base", _sin_base)
    asyncio.run(h._consultar_base_conocimiento(ctx, "ortodoncia", "precio"))

    assert ctx.turno.senales == [Senal("ortodoncia", "precio", hubo_dato=False)]
```

Run: `uv run pytest tests/test_sin_resolver.py -q`
Expected: FAIL con `AttributeError: 'TurnoEnCurso' object has no attribute 'senales'`

- [ ] **Step 3: El campo en `TurnoEnCurso`**

En `src/maxicare_daniela/contratos.py`, dentro de `TurnoEnCurso`, junto a
`cifras_autorizadas` y `horas_autorizadas`:

```python
    #: Lo que se consulto a la base de conocimiento en este turno, con dato o sin el. De
    #: aqui salen los casos `FALTA_DATO`, y el tratamiento con el que se enriquece la huella
    #: de un guardrail. Mismo ciclo de vida que `cifras_autorizadas`: se vacia cada turno,
    #: porque un hueco de hace diez mensajes no es un hueco de hoy.
    senales: list[Senal] = field(default_factory=list)
```

Y dentro de `reiniciar()`, junto a las otras cuatro líneas:

```python
        self.senales = []
```

Import al tope de `contratos.py`: `from .sin_resolver import Senal`

Comprobar que no hay ciclo **antes de seguir**:

```bash
uv run python -c "from maxicare_daniela import contratos, herramientas, atencion, relevo; print('OK')"
```
Expected: `OK`. Si sale `ImportError ... circular import`, usar `if TYPE_CHECKING:` con la
anotación entre comillas.

Y leer `atencion.py:180-195`: si esa subclase llama a `super().reiniciar()` como primera
línea, no hay nada que añadir; las señales se vacían y **no se reponen**, a diferencia de
`hubo_adjunto`. Si construye el estado a mano, añadir `self.senales = []`.

- [ ] **Step 4: La anotación en la tool**

En `_consultar_base_conocimiento`, justo **después** de `texto = await _con_base(ctx, trabajo)`
y **antes** de `ctx.turno.cifras_autorizadas |= ...`:

```python
    # Se anota SIEMPRE, con dato o sin el. Sin dato produce un caso `FALTA_DATO`; con dato no
    # produce nada, pero deja el tratamiento con el que se enriquece la huella de un guardrail
    # que salte despues en este mismo turno. Solo memoria: la escritura ocurre al final, en
    # `_anotar_resultado`, cuando el paciente ya recibio su respuesta.
    ctx.turno.senales.append(
        Senal(
            tratamiento=tratamiento,
            concepto=pregunta,
            hubo_dato=not texto.startswith("SIN DATO DOCUMENTADO"),
        )
    )
```

Import al tope de `herramientas.py`: `from .sin_resolver import Senal`

- [ ] **Step 5: El volcado en `_anotar_resultado`**

Añadir a la firma, **todos keyword-only con default** para no romper las llamadas existentes
ni los scripts que doblan esta firma:

```python
    senales: list | None = None,
    tripwires: list[str] | None = None,
    escalado_por: str | None = None,
    frase: str | None = None,
    telefono: str = "",
```

Normalizar al principio del cuerpo:

```python
    senales = senales or []
    tripwires = tripwires or []
```

Y **después** de `persistencia.tocar_conversacion(...)`, dentro del mismo
`with persistencia.conectar(...)`:

```python
            # Lo que este turno deja en el informe de «sin resolver». Va aqui, al final y
            # dentro del `try` que ya traga, por una razon que no se puede mover: cuando esta
            # linea corre, el paciente YA tiene su respuesta en pantalla. Si esto revienta se
            # pierde un caso y queda en el `log.exception` de abajo; nunca se rompe un turno.
            #
            # Nada de esto esta dentro del candado ni del buffer (no negociables 3 y 6).
            for caso in sin_resolver.casos_del_turno(
                senales=senales, tripwires=tripwires, escalado_por=escalado_por,
                motivo=motivo, frase=frase,
            ):
                persistencia.registrar_caso(
                    conn, huella=caso.huella, tipo=caso.tipo, escalo=caso.escalo,
                    ejemplo=caso.ejemplo, telefono=telefono,
                )
```

Import al tope de `atencion.py`: `from . import sin_resolver`

En la llamada del turno normal (donde existe `resultado`), añadir:

```python
        senales=ctx.turno.senales,
        tripwires=resultado.tripwires,
        escalado_por=resultado.escalado_por,
        frase=<el texto del paciente, nombre real del paso 1>,
        telefono=<el telefono, nombre real del paso 1>,
```

En las llamadas de los caminos de error donde no hay `resultado`, pasar solo lo que exista
(`motivo` ya se pasa hoy) y dejar el resto en su default.

- [ ] **Step 6: `/clearstate` y el relevo**

En el bloque de `/clearstate` de `atencion.py`, **antes** del `DELETE FROM conversaciones` y
junto al borrado de `agent_sessions`:

```python
            # Antes del DELETE de conversaciones, por lo mismo que el historial del agente:
            # despues ya no hay de donde saber que frases eran de este telefono.
            persistencia.olvidar_ejemplos_de(conn, telefono)
```

En `src/maxicare_daniela/relevo.py`, dentro de `activar(...)` (~591), junto a
`_marcar_escalamiento` y dentro de la conexión que ya está abierta ahí:

```python
        # El relevo abierto a mano no pasa por ningun turno, asi que no hay `ctx.turno` donde
        # acumularlo: es la UNICA escritura de un caso que no sale de `_anotar_resultado`.
        # Sin esto, la senal mas cara de todas --un doctor dejando lo que hacia-- seria la
        # unica que no queda contada.
        persistencia.registrar_caso(conn, huella="humano:relevo", tipo="HUMANO", escalo=1)
```

Si esa sección no tiene ya un `try`, envolver **solo esta llamada**: un fallo escribiendo el
informe no puede impedir que un doctor tome una conversación.

- [ ] **Step 7: Correr las pruebas y los seis scripts — OBLIGATORIO**

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
```
Expected: verde. Se acaba de tocar `contratos.py`, que lo importa medio proyecto.

Y luego, porque se acaba de cambiar la firma de `_anotar_resultado` y **los scripts de
`scripts/` doblan firmas a mano**, cosa que `pytest -q` no caza (la suite entera se queda
verde mientras el script revienta):

```bash
uv run python scripts/probar_tools.py
uv run python scripts/probar_web.py
uv run python scripts/probar_atencion.py
uv run python scripts/probar_lectura.py
uv run python scripts/probar_recordatorios.py
uv run python scripts/probar_calendario.py
```
Expected: `TODO OK` en los seis. Un `TypeError: ... got an unexpected keyword argument` es
exactamente la regresión que este paso existe para cazar.

- [ ] **Step 8: Commit**

```bash
git add src/maxicare_daniela/contratos.py src/maxicare_daniela/herramientas.py src/maxicare_daniela/atencion.py src/maxicare_daniela/relevo.py tests/test_sin_resolver.py
git commit -m "feat: el turno vuelca sus casos despues de responderle al paciente

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: El analista y su tarea de fondo

**Files:**
- Create: `src/maxicare_daniela/analista.py`, `tests/test_analista.py`
- Modify: `src/maxicare_daniela/config.py`, `src/maxicare_daniela/runtime.py`, `.env.ejemplo`

**Interfaces:**
- Consumes: `casos_sin_informe` y `guardar_informe` (tarea 1), `MODELO_EVALUADOR` y
  `config_de_corrida` de `config`.
- Produces:
  - `analista.InformeDelCaso` — BaseModel con `que_paso`, `por_que`, `recomiendo`
  - `analista.texto_del_caso(caso: dict) -> str`
  - `analista.analizar_pendientes(*, database_url: str, limite: int = 5) -> int`
  - `config.analizar_sin_resolver: bool`

- [ ] **Step 1: Escribir la prueba que falla**

Crear `tests/test_analista.py`:

```python
"""El analista: el modelo nano que le escribe tres frases a cada caso.

Offline. El modelo se dobla; lo que se prueba es la forma del contrato y la prohibicion
dura, no lo que un modelo real conteste.
"""

from __future__ import annotations

import pytest

from maxicare_daniela.analista import InformeDelCaso, texto_del_caso


def test_el_informe_tiene_tres_campos_con_tope():
    """La brevedad se impone por estructura, no pidiendole al modelo que sea breve."""
    campos = InformeDelCaso.model_fields

    assert set(campos) == {"que_paso", "por_que", "recomiendo"}
    for nombre, campo in campos.items():
        topes = [m for m in campo.metadata if getattr(m, "max_length", None)]
        assert topes, f"{nombre} no tiene tope de longitud y el informe puede divagar"


def test_un_informe_demasiado_largo_lo_rechaza_el_contrato():
    with pytest.raises(Exception):
        InformeDelCaso(que_paso="x" * 5000, por_que="y", recomiendo="z")


def test_el_texto_que_se_le_manda_al_modelo_lleva_los_ejemplos():
    caso = {
        "huella": "falta_dato:ortodoncia:precio", "tipo": "FALTA_DATO",
        "contador": 7, "escalo": 7,
        "primera_vez": "2026-09-15T15:42:00+00:00",
        "ultima_vez": "2026-09-27T14:20:00+00:00",
        "ejemplos": ["cuanto me sale la ortodoncia en cuotas", "brackets por mes?"],
    }

    texto = texto_del_caso(caso)

    assert "ortodoncia" in texto
    assert "7" in texto
    assert "cuanto me sale la ortodoncia en cuotas" in texto


def test_las_instrucciones_prohiben_proponer_cifras():
    """La prohibicion dura del spec 7.3.

    Si el informe pudiera proponer contenido clinico, alguien lo aprobaria de un clic y
    habriamos metido una cifra alucinada a la base de conocimiento por la puerta de atras --
    justo lo que `sin_cifra_no_documentada` existe para impedir.
    """
    from maxicare_daniela.analista import _analista

    instrucciones = _analista.instructions.lower()

    assert "prohibido" in instrucciones
    assert "cifra" in instrucciones or "precio" in instrucciones
```

Run: `uv run pytest tests/test_analista.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'maxicare_daniela.analista'`

- [ ] **Step 2: Escribir el analista**

Crear `src/maxicare_daniela/analista.py`:

```python
"""Las tres frases que lleva cada caso del informe.

Corre sobre el tier nano --el mismo que los evaluadores de guardrails-- y sobre el caso YA
AGRUPADO: una llamada por caso, no una por mensaje. Siete pacientes preguntando lo mismo
cuestan UN informe, no siete. Esa agrupacion es el control de costo de verdad; el modelo
barato solo lo remata.

La prohibicion que manda sobre todo lo demas: el informe describe el hueco y recomienda la
accion, JAMAS propone el contenido clinico ni una cifra. Si pudiera, alguien lo aprobaria de
un clic y habriamos metido un precio alucinado a la base de conocimiento por la puerta de
atras -- exactamente lo que `sin_cifra_no_documentada` existe para impedir.
"""

from __future__ import annotations

import logging

from agents import Agent, Runner
from pydantic import BaseModel, Field

from . import persistencia
from .config import MODELO_EVALUADOR, config_de_corrida

log = logging.getLogger(__name__)


class InformeDelCaso(BaseModel):
    """La unica forma que puede tener un informe.

    Los topes no son decoracion: un informe de tres parrafos no lo abre nadie en una reunion.
    Un modelo con este `output_type` no puede divagar aunque quiera.
    """

    que_paso: str = Field(max_length=280, description="Que ocurrio, en numeros y en llano.")
    por_que: str = Field(max_length=280, description="La causa concreta.")
    recomiendo: str = Field(max_length=320, description="La accion. Nunca el contenido.")


_analista = Agent(
    name="analista_sin_resolver",
    model=MODELO_EVALUADOR,
    output_type=InformeDelCaso,
    instructions=(
        "Lees un caso agrupado de cosas que una asistente de una clinica dental no pudo "
        "resolver, y escribes tres frases para que el equipo decida que ajustar.\n\n"
        "Escribes para la clinica, no para un programador: NUNCA nombres de errores tecnicos, "
        "nombres de funciones ni nombres de guardrails en tu texto.\n\n"
        "PROHIBIDO, sin excepcion: proponer el contenido que falta. No inventas ni sugieres "
        "una cifra, un precio, una duracion, un diagnostico ni un protocolo. Puedes decir que "
        "falta la ficha de un precio; no puedes decir cual es ese precio. Si no lo sabes --y "
        "no lo sabes-- lo dices.\n\n"
        "Si los ejemplos muestran un matiz (preguntan por la cuota mensual y no por el total, "
        "usan una palabra distinta a la que la clinica tiene cargada), ese matiz es lo mas "
        "valioso que puedes aportar: dilo en `recomiendo`.\n\n"
        "Si el caso parece deliberado --la clinica decidio no dar ese dato por chat-- dilo, y "
        "en vez de pedir que se arregle, senala el volumen como dato de negocio."
    ),
)


def texto_del_caso(caso: dict) -> str:
    """Lo que ve el modelo. Corto a proposito: la entrada tambien cuesta."""
    ejemplos = "\n".join(f"  - {e}" for e in caso.get("ejemplos") or []) or "  (ninguno)"
    return (
        f"tipo: {caso['tipo']}\n"
        f"huella: {caso['huella']}\n"
        f"veces: {caso['contador']}\n"
        f"de esas, se molesto al doctor: {caso['escalo']}\n"
        f"primera vez: {caso['primera_vez']}\n"
        f"ultima vez: {caso['ultima_vez']}\n"
        f"lo que escribieron los pacientes:\n{ejemplos}"
    )


async def analizar_pendientes(*, database_url: str, limite: int = 5) -> int:
    """Le escribe el informe a los casos que no lo tienen. Devuelve cuantos escribio.

    Falla ABIERTO, como `_preguntar` en `guardrails.py`: un caso sin informe se muestra igual
    --con su contador y sus ejemplos-- y se reintenta en el ciclo siguiente. Un analista
    caido no puede dejar la pantalla vacia.
    """
    escritos = 0
    with persistencia.conectar(database_url) as conn:
        for caso in persistencia.casos_sin_informe(conn, limite=limite):
            try:
                corrida = await Runner.run(
                    _analista,
                    texto_del_caso(caso),
                    max_turns=1,
                    run_config=config_de_corrida(canal="informe"),
                )
                informe: InformeDelCaso = corrida.final_output
            except Exception as e:  # noqa: BLE001 -- se reintenta en el ciclo siguiente
                log.error("el analista fallo en %s (%s); se reintenta", caso["huella"], e)
                continue

            persistencia.guardar_informe(
                conn, huella=caso["huella"], informe=informe.model_dump(),
                sobre=caso["contador"],
            )
            escritos += 1

    return escritos
```

- [ ] **Step 3: El interruptor en `config.py` y `.env.ejemplo`**

En la dataclass de configuración de `config.py`, siguiendo el patrón de `daniela_responde`:

```python
    #: `!= "0"` y no `== "1"`: el default es analizar. Apagarlo NO apaga la captura -- los
    #: casos se siguen agrupando, solo se quedan sin las tres frases.
    analizar_sin_resolver: bool = True
```

Y en el constructor, junto a los otros `_opcional`:

```python
            analizar_sin_resolver=_opcional("MAXICARE_ANALIZAR_SIN_RESOLVER", "1") != "0",
```

En `.env.ejemplo`:

```
# Apaga el informe automatico de «sin resolver». La captura sigue funcionando.
MAXICARE_ANALIZAR_SIN_RESOLVER=1
```

- [ ] **Step 4: La tarea de fondo**

En `runtime.py`, copiando el patrón exacto de `_despachar_recordatorios_sin_parar`
(`runtime.py:1881-1941`). Va en **su propia tarea**, no dentro de la de recordatorios ni de
la de relevos (no negociable 21):

```python
#: Cada cinco minutos. El informe no es urgente: lo lee un humano una vez por semana. Con
#: sesenta segundos se gastarian llamadas para que nadie las mire antes.
SEGUNDOS_ENTRE_ANALISIS = 300.0

#: La referencia viva, igual que las otras dos tareas: sin guardarla, el recolector de basura
#: se puede llevar una tarea que nadie mira y el informe dejaria de escribirse sin un solo
#: error en el log.
_tarea_de_analisis: asyncio.Task | None = None


async def _analizar_sin_parar() -> None:
    """El reloj del informe. Fuera del turno del paciente, en su propia tarea."""
    while True:
        await asyncio.sleep(SEGUNDOS_ENTRE_ANALISIS)
        try:
            escritos = await analista.analizar_pendientes(database_url=config.database_url)
            if escritos:
                log.info("sin resolver: %s informes escritos", escritos)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- tiene que seguir vivo manana
            log.exception("el analisis de «sin resolver» fallo; se reintenta en el siguiente ciclo")


@app.on_event("startup")
async def _arrancar_analisis() -> None:
    global _tarea_de_analisis
    if not config.database_url:
        log.info("sin base configurada: no arranca el analisis de «sin resolver»")
        return
    if not config.analizar_sin_resolver:
        log.info("MAXICARE_ANALIZAR_SIN_RESOLVER=0: se capturan casos, no se escriben informes")
        return
    _tarea_de_analisis = asyncio.create_task(_analizar_sin_parar())


@app.on_event("shutdown")
async def _parar_analisis() -> None:
    if _tarea_de_analisis is None:
        return
    _tarea_de_analisis.cancel()
    try:
        await _tarea_de_analisis
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
```

Import al tope de `runtime.py`: añadir `analista` a la lista del paquete.

> El `await asyncio.sleep(...)` va **al principio** del `while`, no al final, y el
> `except asyncio.CancelledError: raise` va **antes** del `except Exception`. Ambas cosas son
> load-bearing: si no, la cancelación del shutdown se la traga el log.

- [ ] **Step 5: Correr y comprobar que el servidor arranca**

```bash
uv run pytest tests/test_analista.py -q
uv run pytest -q
uv run python -c "from maxicare_daniela import runtime; print('OK')"
```
Expected: 4 passed · suite verde · `OK`. El último importa: `runtime.py` es lo que corre en
producción.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/analista.py src/maxicare_daniela/config.py src/maxicare_daniela/runtime.py tests/test_analista.py .env.ejemplo
git commit -m "feat: el analista nano le escribe tres frases a cada caso, en su propia tarea

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: El endpoint y la pantalla

**Files:**
- Modify: `src/maxicare_daniela/runtime.py` (el endpoint, **antes** de la ruta comodín)
- Create: `web/src/pantallas/SinResolver.tsx`
- Modify: `web/src/api.ts`, `web/src/App.tsx`, `web/src/componentes/Sidebar.tsx`

**Interfaces:**
- Consumes: `persistencia.casos_recientes` (tarea 1).
- Produces: `GET /api/sin-resolver` → `{"casos": [...], "es_admin": bool}`

- [ ] **Step 1: El endpoint**

En `runtime.py`, junto a `GET /api/historial` (~1649) y **antes** de la ruta comodín de
`index.html` (~1956), que tiene que quedarse al final:

```python
@app.get("/api/sin-resolver")
async def api_sin_resolver(quien: dict = Depends(usuario_actual)) -> dict:
    """La ventana del informe. Solo lectura: esta pantalla no tiene ninguna escritura.

    `es_admin` es lo que decide si el navegador recibe el detalle tecnico. Se calcula aqui y
    no en el front: un error crudo en la pantalla de una clinica genera una llamada de
    soporte que no deberia existir.
    """
    with persistencia.conectar(config.database_url) as conn:
        casos = persistencia.casos_recientes(conn)
    return {"casos": casos, "es_admin": quien["rol"] == "admin"}
```

- [ ] **Step 2: El cliente TypeScript**

En `web/src/api.ts`, junto a `listarHistorial`, usando el helper existente del archivo (el
que lanza `SesionCaducada`) y no `fetch` directo. Leer esa función antes de escribir esta:

```ts
export type CasoSinResolver = {
  huella: string
  tipo: 'FALTA_DATO' | 'GUARDRAIL' | 'ROTO' | 'HUMANO'
  contador: number
  escalo: number
  primera_vez: string
  ultima_vez: string
  ejemplos: string[]
  informe: { que_paso: string; por_que: string; recomiendo: string } | null
}

export async function listarSinResolver(): Promise<{
  casos: CasoSinResolver[]
  es_admin: boolean
}> {
  return pedir('/api/sin-resolver')
}
```

- [ ] **Step 3: La pantalla**

**Antes de escribirla, leer `web/src/pantallas/Tratamientos.tsx:1-80`** para copiar el patrón
exacto de `useCallback` + `ref` y las clases que usa el proyecto. Los tokens `--color-sp-*`
de `web/src/marca/` **no se renombran** (`web/CLAUDE.md`).

Crear `web/src/pantallas/SinResolver.tsx`:

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { listarSinResolver, SesionCaducada, type CasoSinResolver } from '../api'

type Props = { alCaducarSesion: () => void }

/** `falta_dato:ortodoncia:precio` -> «Falta el dato «precio» de ORTODONCIA».
 *
 * La huella cruda no se le ensena a la clinica: es un identificador, no una frase. El
 * detalle tecnico vive detras del `<details>` de admin. */
function titulo(caso: CasoSinResolver): string {
  const [, uno = '', dos = ''] = caso.huella.split(':')
  switch (caso.tipo) {
    case 'FALTA_DATO':
      return `Falta ${dos === '_general' ? 'un dato' : `el dato «${dos}»`} de ${uno.toUpperCase()}`
    case 'GUARDRAIL':
      return `Daniela iba a decir algo que no debía${dos !== '_general' ? ` (${dos})` : ''}`
    case 'ROTO':
      return 'El sistema tuvo una falla técnica'
    case 'HUMANO':
      return 'Hubo que pasarle la conversación al doctor'
    default:
      return caso.huella
  }
}

function fecha(iso: string): string {
  return new Date(iso).toLocaleDateString('es-CO', { day: 'numeric', month: 'short' })
}

export default function SinResolver({ alCaducarSesion }: Props) {
  const [casos, setCasos] = useState<CasoSinResolver[]>([])
  const [esAdmin, setEsAdmin] = useState(false)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')

  // Por `ref` y NO en las deps del useCallback: meter la prop en deps deja la pantalla
  // releyendo Neon en bucle. Es la trampa documentada en `web/CLAUDE.md`.
  const caducar = useRef(alCaducarSesion)
  caducar.current = alCaducarSesion

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      const datos = await listarSinResolver()
      setCasos(datos.casos)
      setEsAdmin(datos.es_admin)
      setError('')
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else setError('No se pudo cargar el informe.')
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  if (cargando) return <p className="p-6">Cargando…</p>
  if (error) return <p className="p-6 text-red-700">{error}</p>

  return (
    <section className="p-6 max-w-3xl mx-auto">
      <h1 className="text-2xl font-semibold">Sin resolver</h1>
      <p className="text-sm opacity-70 mt-1">
        Últimos 30 días, lo que más está pasando primero. Esta pantalla solo informa.
      </p>

      {casos.length === 0 && (
        <p className="mt-8 p-6 rounded-lg border text-center">
          Nada sin resolver en los últimos 30 días. Buena señal.
        </p>
      )}

      <ul className="mt-6 space-y-4">
        {casos.map((caso) => (
          <li key={caso.huella} className="rounded-lg border overflow-hidden">
            <header className="p-4 border-b flex items-baseline justify-between gap-4 flex-wrap">
              <h2 className="font-medium">{titulo(caso)}</h2>
              <span className="text-sm whitespace-nowrap opacity-70">
                {caso.contador} {caso.contador === 1 ? 'vez' : 'veces'} ·{' '}
                {fecha(caso.primera_vez)} al {fecha(caso.ultima_vez)}
              </span>
            </header>

            <div className="p-4 space-y-3 text-sm">
              {caso.informe ? (
                <>
                  <p>
                    <strong className="block text-xs uppercase opacity-60">Qué pasó</strong>
                    {caso.informe.que_paso}
                  </p>
                  <p>
                    <strong className="block text-xs uppercase opacity-60">Por qué</strong>
                    {caso.informe.por_que}
                  </p>
                  <p>
                    <strong className="block text-xs uppercase opacity-60">Recomiendo</strong>
                    {caso.informe.recomiendo}
                  </p>
                </>
              ) : (
                // Un analista caído no puede dejar la pantalla vacía: el caso se muestra
                // igual, con su contador y sus ejemplos, que ya dicen bastante.
                <p className="opacity-70">El análisis todavía no está listo.</p>
              )}

              {caso.escalo > 0 && (
                <p className="opacity-70">
                  Se interrumpió al doctor {caso.escalo} de {caso.contador} veces.
                </p>
              )}

              {caso.ejemplos.length > 0 && (
                <details>
                  <summary className="cursor-pointer">
                    Las preguntas tal como llegaron ({caso.ejemplos.length})
                  </summary>
                  <ul className="mt-2 space-y-1 pl-4 list-disc opacity-80">
                    {caso.ejemplos.map((texto, i) => (
                      <li key={i}>«{texto}»</li>
                    ))}
                  </ul>
                </details>
              )}

              {esAdmin && (
                <details>
                  <summary className="cursor-pointer opacity-60">Detalle técnico</summary>
                  <code className="block mt-2 text-xs break-all opacity-80">{caso.huella}</code>
                </details>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
```

- [ ] **Step 4: Engancharla**

- `web/src/App.tsx`: en `seccionDelHash`, servir `<SinResolver />` para `'bandeja'` en vez de
  `<Pendiente />`. **La clave del hash no se cambia** — cambiarla rompe enlaces guardados.
- `web/src/componentes/Sidebar.tsx:32-47`: etiqueta visible a **«Sin resolver»**, y quitarle
  la marca de fase pendiente.

- [ ] **Step 5: Construir y mirarlo**

```bash
cd web && npm run build
```
Expected: build limpio, sin errores de TypeScript.

Con `uvicorn` en `:8080`, abrir `http://localhost:8080/#bandeja` y comprobar a ojo: que
carga, que el estado vacío se lee bien, y que a ~400px de ancho no hay scroll horizontal.

- [ ] **Step 6: Commit**

```bash
git add src/maxicare_daniela/runtime.py web/src/api.ts web/src/pantallas/SinResolver.tsx web/src/App.tsx web/src/componentes/Sidebar.tsx
git commit -m "feat: la pantalla de «sin resolver», solo lectura y sin un solo boton

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: El entregable verificable

**Files:**
- Create: `scripts/probar_sin_resolver.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Escribir el script**

Estructura copiada de `scripts/probar_recordatorios.py`. **No lleva `argparse`**: no tiene
ningún modo que gaste, igual que `probar_recordatorios.py` y `probar_tools.py`.

Crear `scripts/probar_sin_resolver.py`:

```python
"""Comprueba que lo que Daniela no pudo resolver acaba en una fila, agrupado.

    uv run python scripts/probar_sin_resolver.py

No gasta un solo token: nunca se instancia un Agent y nunca se llama a OpenAI. Escribe en el
esquema de pruebas y lo borra al terminar; jamas toca `public`.

La comprobacion 4 es la que justifica el script: un tripwire que se regenero bien HOY no deja
absolutamente ningun rastro --`Resultado.tripwires` muere dentro del proceso-- y es la senal
de calidad mas frecuente del sistema. Si alguien vuelve a descartarla, esta linea lo dice.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import persistencia, sin_resolver  # noqa: E402
from maxicare_daniela.config import cargar_dotenv  # noqa: E402
from maxicare_daniela.sin_resolver import Senal  # noqa: E402

ESQUEMA = "pruebas"
fallos = 0


def marca(ok: bool) -> str:
    global fallos
    if not ok:
        fallos += 1
    return "OK  " if ok else "FALLA"


def url_de_pruebas() -> tuple[str, str]:
    cargar_dotenv()
    base = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not base:
        print("ERROR: falta MAXICARE_DATABASE_URL en .env", file=sys.stderr)
        raise SystemExit(1)
    directa = base.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return directa, f"{directa}{sep}options=-csearch_path%3D{ESQUEMA}"


def montar_esquema(directa: str, url: str) -> None:
    print("Preparando el esquema de pruebas (no se toca 'public')\n")
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()
    with persistencia.conectar(url) as conn:
        persistencia.aplicar_esquema(conn)


def limpiar(directa: str) -> None:
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()
    print("\nEsquema de pruebas borrado.")


def fila(conn, huella: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT contador, escalo, ejemplos FROM casos_sin_resolver WHERE huella = %s",
            (huella,),
        )
        f = cur.fetchone()
    return {} if not f else {"contador": f[0], "escalo": f[1], "ejemplos": json.loads(f[2])}


def cuantas(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM casos_sin_resolver")
        return cur.fetchone()[0]


def volcar(conn, *, senales, tripwires, escalado_por, motivo, frase, telefono):
    """Lo mismo que hace `_anotar_resultado`, sin montar un turno entero."""
    for caso in sin_resolver.casos_del_turno(
        senales=senales, tripwires=tripwires, escalado_por=escalado_por,
        motivo=motivo, frase=frase,
    ):
        persistencia.registrar_caso(
            conn, huella=caso.huella, tipo=caso.tipo, escalo=caso.escalo,
            ejemplo=caso.ejemplo, telefono=telefono,
        )


def main() -> int:
    directa, url = url_de_pruebas()
    montar_esquema(directa, url)
    try:
        with persistencia.conectar(url) as conn:
            # 1
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'casos_sin_resolver'",
                    (ESQUEMA,),
                )
                columnas = cur.fetchone()[0]
            print(f"{marca(columnas >= 11)} 1. la 018 dejo la tabla ({columnas} columnas)")

            # 2
            volcar(conn, senales=[Senal("ortodoncia", "precio", hubo_dato=False)],
                   tripwires=[], escalado_por="dato_faltante", motivo=None,
                   frase="cuanto sale la ortodoncia en cuotas", telefono="+573001112233")
            f = fila(conn, "falta_dato:ortodoncia:precio")
            ok = f.get("contador") == 1 and f.get("escalo") == 1
            print(f"{marca(ok)} 2. un SIN DATO DOCUMENTADO deja una fila (escalo={f.get('escalo')})")

            # 3
            for i in range(6):
                volcar(conn, senales=[Senal("ortodoncia", "precio", hubo_dato=False)],
                       tripwires=[], escalado_por=None, motivo=None,
                       frase=f"pregunta {i}", telefono=f"+57300111223{i}")
            f = fila(conn, "falta_dato:ortodoncia:precio")
            ok = f.get("contador") == 7 and len(f.get("ejemplos", [])) == 5
            print(f"{marca(ok)} 3. siete turnos = UNA fila "
                  f"(contador={f.get('contador')}, ejemplos={len(f.get('ejemplos', []))})")

            # 4 -- LA QUE IMPORTA
            volcar(conn, senales=[Senal("profilaxis", "precio", hubo_dato=True)],
                   tripwires=["sin_cifra_no_documentada"], escalado_por=None, motivo=None,
                   frase="cuanto vale la limpieza", telefono="+573004445566")
            f = fila(conn, "guardrail:sin_cifra_no_documentada:profilaxis")
            print(f"{marca(bool(f))} 4. un tripwire REGENERADO deja fila "
                  f"(hoy, sin esto, no dejaba nada)")

            # 5
            antes = cuantas(conn)
            volcar(conn, senales=[], tripwires=[], escalado_por=None,
                   motivo="relevo: la tiene @doctora", frase="hola", telefono="+57300")
            print(f"{marca(cuantas(conn) == antes)} 5. un motivo 'relevo:' NO deja fila")

            # 6
            with conn.cursor() as cur:
                cur.execute("UPDATE casos_sin_resolver SET ultima_vez = now() - "
                            "interval '90 days' WHERE huella = %s",
                            ("guardrail:sin_cifra_no_documentada:profilaxis",))
            conn.commit()
            recientes = [c["huella"] for c in persistencia.casos_recientes(conn, dias=30)]
            ok = ("guardrail:sin_cifra_no_documentada:profilaxis" not in recientes
                  and recientes and recientes[0] == "falta_dato:ortodoncia:precio")
            print(f"{marca(ok)} 6. la ventana hunde lo viejo y ordena por frecuencia "
                  f"({len(recientes)} en la ventana)")

            # 7
            original = persistencia.registrar_caso

            def revienta(*a, **k):
                raise RuntimeError("la base se cayo")

            persistencia.registrar_caso = revienta
            try:
                volcar(conn, senales=[Senal("x", "y", hubo_dato=False)], tripwires=[],
                       escalado_por=None, motivo=None, frase="z", telefono="+57300")
                propago = False
            except RuntimeError:
                propago = True
            finally:
                persistencia.registrar_caso = original
            print(f"{marca(propago)} 7. el fallo SI sale de `volcar` "
                  f"(quien lo traga es el try de _anotar_resultado, no esta funcion)")

            # 8
            f_antes = fila(conn, "falta_dato:ortodoncia:precio")
            persistencia.olvidar_ejemplos_de(conn, "+573001112233")
            f_despues = fila(conn, "falta_dato:ortodoncia:precio")
            ok = (f_despues["contador"] == f_antes["contador"]
                  and not any(e.get("telefono") == "+573001112233"
                              for e in f_despues["ejemplos"]))
            print(f"{marca(ok)} 8. /clearstate borra la frase y el contador NO baja "
                  f"({f_antes['contador']} -> {f_despues['contador']})")
    finally:
        limpiar(directa)

    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLA(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> La comprobación 7 verifica que el fallo **sí** sale de `volcar`: quien lo traga es el
> `try/except Exception` que ya existe en `_anotar_resultado`. Si algún día alguien mete un
> `try` dentro de `volcar`, esta comprobación lo caza — tragarlo dos veces esconde el
> `log.exception` que es la única pista de que se están perdiendo casos.

- [ ] **Step 2: Correrlo y comprobar que `public` quedó intacto**

```bash
uv run python scripts/probar_sin_resolver.py
```
Expected: `TODO OK` y `Esquema de pruebas borrado.`

- [ ] **Step 3: Actualizar el `CLAUDE.md`**

Añadir a la tabla de entregables por fase:

```
| `scripts/probar_sin_resolver.py` | el informe de lo que Daniela no pudo (fase 6D) | no |
```

Y el no negociable **22**, redactado como los otros — una línea que sobreviva a una
compactación:

```
22. **El informe de «sin resolver» se escribe DESPUÉS de responderle al paciente, y su huella
   la arma el código.** Va dentro del `try` de `_anotar_resultado` que ya traga: si revienta
   se pierde un caso, nunca un turno. Con huellas del modelo, dos casos iguales salen
   distintos y la agrupación —que es todo el valor— se rompe sin un solo error en el log. El
   `ROTO` agrupa por `type(e).__name__`, **nunca** por el mensaje: con el mensaje cada error
   es único. Un `fallo_respuesta` que empieza por `relevo:` no entra: no es un fallo. Y
   `/clearstate` borra los ejemplos **sin** bajar el contador.
```

- [ ] **Step 4: Correr TODO, por última vez**

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_sin_resolver.py
uv run python scripts/probar_tools.py
uv run python scripts/probar_atencion.py
uv run python scripts/probar_recordatorios.py
```
Expected: todo verde. Si algo falla, **no se commitea**: se arregla.

- [ ] **Step 5: Commit**

```bash
git add scripts/probar_sin_resolver.py CLAUDE.md
git commit -m "test: el entregable de «sin resolver», ocho comprobaciones sin gastar un token

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Lo que este plan NO hace

Está en el spec como `PENDIENTE` y se deja fuera a propósito:

- **Medir el costo real del informe.** Los ~600/~120 tokens son estimación. Se cierra leyendo
  `ctx.usage` sobre al menos 10 informes reales, después de un mes en producción.
- **El techo diario de informes** (spec 7.4, punto 3). Depende del volumen real de la clínica,
  que hoy no está medido. Mientras tanto el control de costo efectivo es la agrupación + el
  intervalo de 5 minutos + `limite=5` por ciclo: como máximo 1.440 informes/día (~0,37 USD), y
  eso exigiría 1.440 casos *distintos* al día.
- **Confirmar la ventana de 30 días.** Es la propuesta; se ajusta con un mes de datos reales.
- **Cerrar la puerta de que la clínica edite el conocimiento.** Existe desde antes y no se
  toca aquí.

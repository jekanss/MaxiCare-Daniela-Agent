# Fase 6B — El muro · Plan de implementación

> **Para trabajadores agénticos:** SUB-SKILL OBLIGATORIA: usa
> superpowers:subagent-driven-development (recomendada) o superpowers:executing-plans para
> implementar este plan tarea por tarea. Los pasos usan casillas (`- [ ]`) para el
> seguimiento.

**Objetivo:** que `lector_archivos` corra sobre los archivos que llegan por WhatsApp, que su
mitad clínica llegue solo a los doctores y su mitad no clínica solo a Daniela, y que cada
paciente tenga su hilo de Telegram.

**Arquitectura:** un módulo nuevo, `lectura.py`, con el reparto como función pura. El
archivo se entrega al doctor antes de que el modelo lo vea; el lector corre como tarea
aparte, en paralelo con la ventana de 20 s del búfer, y `atencion.py` recoge lo que esté
listo cuando la ventana cierra.

**Stack:** Python 3.12 · uv · `openai-agents` 0.22.2 · psycopg · httpx · pytest (SIN
pytest-asyncio: las pruebas son síncronas y llaman `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-09-12-fase-6b-el-muro-design.md` — léelo entero antes
de la primera tarea. Este plan argumenta desde él.

## Restricciones globales

Valen para TODAS las tareas. Cada una las hereda sin repetirlas.

- **El principio que decide los empates:** la seguridad clínica prevalece sobre cualquier
  objetivo comercial. El éxito no es acumular citas: es que el paciente llegue a la cita
  correcta.
- **`contexto_clinico` no se persiste en ninguna tabla, nunca.** Vive en memoria el tiempo
  que tarda en salir hacia Telegram.
- **Nada de lo que se escriba puede retrasar la entrega del archivo al doctor.** Es la
  garantía de la fase 2.
- **No se registran cédulas ni documentos de identidad de ningún tipo.** La tabla
  `pacientes` no tiene esa columna y esa ausencia ES la política.
- **`from agents import tool` está prohibido.** Usa `from agents import function_tool`.
- **Los heredoc de Bash fallan en este entorno.** Para escribir un archivo usa la
  herramienta Write, nunca `cat > archivo <<'EOF'`.
- **La consola de Windows es cp1252.** Todo script bajo `scripts/` empieza con
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` y usa marcadores ASCII
  (`OK` / `FALLA` / `->`).
- **Las pruebas escriben en `pruebas`, `pruebas_web` o `pruebas_atencion`, NUNCA en
  `public`**, donde viven los pacientes reales de la clínica.
- Comentarios y docstrings en español, como todo el repositorio.
- Tras cada tarea: `uv run pytest -q` en verde antes de commitear.

## Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `src/maxicare_daniela/lectura.py` | **NUEVO** · el reparto, el lector, el tema del paciente |
| `src/maxicare_daniela/contratos.py` | `LecturaNoClinica`: la mitad que sí cruza |
| `src/maxicare_daniela/canales.py` | `Telegram.crear_tema`, `Telegram.cerrar_tema` |
| `src/maxicare_daniela/persistencia.py` | leer y guardar `pacientes.telegram_topic_id` |
| `src/maxicare_daniela/ingesta.py` | destino del archivo, aviso al General, arranca el lector |
| `src/maxicare_daniela/atencion.py` | recoge la lectura al cerrar la ventana y la usa |
| `src/maxicare_daniela/runtime.py` | pasa la tarea del lector de `procesar_mensaje` a `atender` |
| `src/maxicare_daniela/guardrails.py` | una frase en `_evaluador_clinico` |
| `tests/test_lectura.py` | **NUEVO** · el reparto, el tema, el lector |
| `tests/test_muro.py` | **NUEVO** · EL ENTREGABLE de la fase |
| `scripts/probar_lectura.py` | **NUEVO** · entregable ejecutable |

**Sin migración.** `migraciones/002` ya trae `pacientes.telegram_topic_id`,
`pacientes.telegram_topic_abierto` y el índice único `uq_pacientes_topic`.

---

## Tarea 1: El contrato y el reparto

Es el muro en su forma más pequeña y probable: una función pura, sin IO, sin modelo, sin
base. Todo lo demás de la fase se apoya aquí.

**Archivos:**
- Modificar: `src/maxicare_daniela/contratos.py` (añadir tras `class LecturaArchivo`)
- Crear: `src/maxicare_daniela/lectura.py`
- Crear: `tests/test_lectura.py`

**Interfaces:**
- Consume: `LecturaArchivo`, `TipoDocumento`, `Tratamiento`, `Confianza` de `contratos.py`.
- Produce:
  - `contratos.LecturaNoClinica` — modelo Pydantic SIN campo `contexto_clinico`.
  - `lectura.repartir(lectura: LecturaArchivo) -> tuple[str, LecturaNoClinica]` — devuelve
    `(contexto_clinico, mitad_no_clinica)`.

- [ ] **Paso 1: Escribir las pruebas que fallan**

En `tests/test_lectura.py`:

```python
"""El muro: el reparto de `LecturaArchivo` en sus dos mitades.

Sin IO, sin modelo, sin base. Si alguna prueba de este archivo necesita una de esas tres
cosas, está en el archivo equivocado.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxicare_daniela import lectura
from maxicare_daniela.contratos import LecturaArchivo, LecturaNoClinica

CENTINELA = "reabsorcion radicular en el 46, con lesion periapical de 4 mm"


def _lectura(**cambios) -> LecturaArchivo:
    datos = {
        "tipo_documento": "remision",
        "tratamiento": "ortodoncia",
        "origen": "Clinica Dental Norte",
        "fecha_documento": None,
        "confianza": "alta",
        "contexto_clinico": CENTINELA,
    }
    datos.update(cambios)
    return LecturaArchivo(**datos)


def test_el_reparto_separa_las_dos_mitades():
    clinico, no_clinica = lectura.repartir(_lectura())

    assert clinico == CENTINELA
    assert no_clinica.tratamiento == "ortodoncia"
    assert no_clinica.origen == "Clinica Dental Norte"


def test_la_mitad_no_clinica_no_tiene_donde_guardar_lo_clinico():
    """No es que no se copie: es que no CABE.

    Un campo que existe y no se debe usar se acaba usando. Que el tipo no lo tenga
    convierte el error en un fallo de construccion en vez de en una fuga en produccion.
    """
    _, no_clinica = lectura.repartir(_lectura())

    assert not hasattr(no_clinica, "contexto_clinico")
    assert "contexto_clinico" not in no_clinica.model_dump()
    assert CENTINELA not in no_clinica.model_dump_json()

    with pytest.raises(ValidationError):
        LecturaNoClinica(
            tipo_documento="remision",
            tratamiento="ortodoncia",
            confianza="alta",
            contexto_clinico=CENTINELA,
        )


@pytest.mark.parametrize("confianza", ["media", "baja"])
def test_por_debajo_de_alta_el_tratamiento_se_borra(confianza):
    """Lo fuerza el codigo, no el prompt. Una instruccion se desobedece; esto no."""
    _, no_clinica = lectura.repartir(_lectura(confianza=confianza, tratamiento="ortodoncia"))

    assert no_clinica.tratamiento == "no_identificado"
    assert no_clinica.confianza == confianza


def test_con_confianza_alta_el_tratamiento_se_respeta():
    """El complemento del anterior: un guardrail que salta siempre se acaba desactivando."""
    _, no_clinica = lectura.repartir(_lectura(confianza="alta", tratamiento="endodoncia"))

    assert no_clinica.tratamiento == "endodoncia"
```

- [ ] **Paso 2: Correr las pruebas y verlas fallar**

Ejecuta: `uv run pytest tests/test_lectura.py -q`
Esperado: FALLA con `ModuleNotFoundError: No module named 'maxicare_daniela.lectura'`.

- [ ] **Paso 3: Añadir `LecturaNoClinica` a `contratos.py`**

Justo **después** de `class LecturaArchivo`, para que se lean juntas:

```python
class LecturaNoClinica(BaseModel):
    """La mitad de `LecturaArchivo` que SÍ puede cruzar hacia el paciente.

    No tiene `contexto_clinico`, y esa ausencia es el muro. No es que el código se acuerde
    de no copiarlo: es que no hay dónde ponerlo. Si alguien añade el campo aquí, las
    pruebas de `tests/test_muro.py` caen.

    `extra="forbid"` es parte de la garantía: sin él, `LecturaNoClinica(**lectura.model_dump())`
    se tragaría el campo clínico en silencio como atributo extra.
    """

    model_config = ConfigDict(extra="forbid")

    tipo_documento: TipoDocumento
    tratamiento: Tratamiento
    origen: str | None = Field(default=None, max_length=200)
    fecha_documento: date | None = None
    confianza: Confianza
```

Comprueba que `ConfigDict` está importado de `pydantic` en la cabecera del archivo; si no,
añádelo al import que ya existe.

- [ ] **Paso 4: Escribir `lectura.py` con el reparto y nada más**

```python
"""El muro: qué mitad de un archivo leído va a cada destinatario.

Del mismo archivo salen dos cosas con dos destinatarios y umbrales opuestos de qué se puede
decir:

    contexto_clinico  ─────────►  Telegram, al tema del paciente
    todo lo demás     ─────────►  el contexto de `daniela`

Ese reparto es el muro del diseño. No es un prompt ni un guardrail: es de dónde el código
saca cada cosa. Por eso `repartir` es una función pura y vive sola en la cabecera de este
módulo, donde se puede probar sin levantar nada.

La frontera de este módulo es la misma que la de `ingesta.py`: no importa `runtime.py` ni
ningún framework web.
"""

from __future__ import annotations

import logging

from .contratos import LecturaArchivo, LecturaNoClinica

log = logging.getLogger("maxicare.lectura")


def repartir(lectura: LecturaArchivo) -> tuple[str, LecturaNoClinica]:
    """Parte una lectura en (lo que ve el doctor, lo que ve Daniela).

    Por debajo de `confianza == "alta"` el tratamiento se borra. Lo dice el contrato desde
    la fase 1 --«por debajo de 'alta', la capa de ingesta fuerza tratamiento
    ='no_identificado' para que Daniela pregunte en vez de asumir»-- y se hace aquí, en
    código, porque una instrucción puede desobedecerse y una línea de código no.
    """
    seguro = lectura.confianza == "alta"
    no_clinica = LecturaNoClinica(
        tipo_documento=lectura.tipo_documento,
        tratamiento=lectura.tratamiento if seguro else "no_identificado",
        origen=lectura.origen,
        fecha_documento=lectura.fecha_documento,
        confianza=lectura.confianza,
    )
    return lectura.contexto_clinico, no_clinica
```

- [ ] **Paso 5: Correr las pruebas y verlas pasar**

Ejecuta: `uv run pytest tests/test_lectura.py -q`
Esperado: PASAN las 5.

- [ ] **Paso 6: Comprobar por mutación que el muro se cae si se rompe**

Cambia temporalmente `LecturaNoClinica.model_config` a `ConfigDict(extra="allow")` y añade
`contexto_clinico: str | None = None`. Corre `uv run pytest tests/test_lectura.py -q`:
**tiene que fallar** `test_la_mitad_no_clinica_no_tiene_donde_guardar_lo_clinico`. Deshaz el
cambio. Si la prueba pasó con la mutación puesta, la prueba no vale y hay que rehacerla
antes de seguir.

- [ ] **Paso 7: Commit**

```bash
git add src/maxicare_daniela/contratos.py src/maxicare_daniela/lectura.py tests/test_lectura.py
git commit -m "feat: el muro, como un reparto que no se puede saltar"
```

---

## Tarea 2: Crear y cerrar temas en Telegram

**Archivos:**
- Modificar: `src/maxicare_daniela/canales.py` (dentro de `class Telegram`, tras `enviar_archivo`)
- Modificar: `tests/test_ingesta.py` (al final, junto a las pruebas de `Telegram` que ya hay)

**Interfaces:**
- Produce:
  - `Telegram.crear_tema(nombre: str) -> int` — devuelve el `message_thread_id`.
  - `Telegram.cerrar_tema(tema_id: int) -> None`.

- [ ] **Paso 1: Escribir las pruebas que fallan**

Al final de `tests/test_ingesta.py`. Usa el mismo patrón de `httpx.MockTransport` que ya
usan las dos pruebas de `message_thread_id` de ese archivo:

```python
def test_crear_tema_pide_el_nombre_y_devuelve_el_id():
    import asyncio
    import httpx

    from maxicare_daniela.canales import Telegram

    llamadas: list[tuple[str, dict]] = []

    def capturar(request: httpx.Request) -> httpx.Response:
        llamadas.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_thread_id": 91, "name": "x"}}
        )

    transporte = httpx.MockTransport(capturar)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        tema = asyncio.run(Telegram("t", "-100123").crear_tema("Ana Perez · +573001112233"))
    finally:
        httpx.AsyncClient = original

    assert tema == 91
    assert llamadas[0][0].endswith("/createForumTopic")
    assert llamadas[0][1]["name"] == "Ana Perez · +573001112233"


def test_cerrar_tema_manda_el_thread_id():
    import asyncio
    import httpx

    from maxicare_daniela.canales import Telegram

    llamadas: list[tuple[str, dict]] = []

    def capturar(request: httpx.Request) -> httpx.Response:
        llamadas.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    transporte = httpx.MockTransport(capturar)
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        asyncio.run(Telegram("t", "-100123").cerrar_tema(91))
    finally:
        httpx.AsyncClient = original

    assert llamadas[0][0].endswith("/closeForumTopic")
    assert llamadas[0][1]["message_thread_id"] == 91


def test_un_tema_que_telegram_rechaza_no_pasa_por_bueno():
    """Sin esto, un `ok: false` devolveria un KeyError sin nombre y el archivo se perderia
    buscando un tema que no existe."""
    import asyncio
    import httpx

    from maxicare_daniela.canales import ErrorDeCanal, Telegram

    transporte = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"ok": False, "description": "not enough rights to manage topics"}
        )
    )
    original = httpx.AsyncClient

    class ClienteFalso(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    httpx.AsyncClient = ClienteFalso
    try:
        with pytest.raises(ErrorDeCanal, match="not enough rights"):
            asyncio.run(Telegram("t", "-100123").crear_tema("Ana"))
    finally:
        httpx.AsyncClient = original
```

Comprueba que `pytest` y `json` están importados en la cabecera de `tests/test_ingesta.py`;
si falta alguno, añádelo.

- [ ] **Paso 2: Correr las pruebas y verlas fallar**

Ejecuta: `uv run pytest tests/test_ingesta.py -q -k tema`
Esperado: FALLA con `AttributeError: 'Telegram' object has no attribute 'crear_tema'`.

- [ ] **Paso 3: Implementar los dos métodos**

Dentro de `class Telegram`, después de `enviar_archivo`:

```python
    async def crear_tema(self, nombre: str) -> int:
        """Abre un tema en el supergrupo y devuelve su `message_thread_id`.

        Telegram corta el nombre en 128 caracteres y rechaza la llamada entera si se pasa,
        así que se recorta aquí: un tema con el nombre corto es mejor que ningún tema.
        """
        cuerpo = {"chat_id": self._chat_id, "name": nombre[:128]}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("createForumTopic"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no creó el tema: {datos.get('description')}")
        return datos["result"]["message_thread_id"]

    async def cerrar_tema(self, tema_id: int) -> None:
        """Cierra un tema. Un tema cerrado impide FÍSICAMENTE escribir a quien no es
        administrador del grupo, mientras el bot, que sí lo es, sigue pudiendo depositar.

        Esa asimetría es la garantía dura del diseño: el doctor no tiene que acordarse de
        nada, porque cuando no debe escribirle al paciente, sencillamente no puede.
        """
        cuerpo = {"chat_id": self._chat_id, "message_thread_id": tema_id}
        async with httpx.AsyncClient(timeout=TIMEOUT_NORMAL) as cliente:
            r = await cliente.post(self._url("closeForumTopic"), json=cuerpo)
        datos = r.json()
        if not datos.get("ok"):
            raise ErrorDeCanal(f"Telegram no cerró el tema: {datos.get('description')}")
```

- [ ] **Paso 4: Correr las pruebas y verlas pasar**

Ejecuta: `uv run pytest tests/test_ingesta.py -q` → todas en verde.

- [ ] **Paso 5: Commit**

```bash
git add src/maxicare_daniela/canales.py tests/test_ingesta.py
git commit -m "feat: canales sabe crear y cerrar temas de Telegram"
```

---

## Tarea 3: El tema del paciente, con su candado

**Archivos:**
- Modificar: `src/maxicare_daniela/persistencia.py` (junto a `asegurar_paciente`, línea ~248)
- Modificar: `src/maxicare_daniela/lectura.py`
- Modificar: `tests/test_lectura.py`

**Interfaces:**
- Consume: `persistencia.asegurar_paciente(conn, *, nombre_completo: str, telefono: str) -> int`,
  `persistencia.conectar(database_url)`, `Telegram.crear_tema`, `Telegram.cerrar_tema`.
- Produce:
  - `persistencia.tema_del_paciente(conn, telefono: str) -> int | None`
  - `persistencia.guardar_tema(conn, *, id_paciente: int, topic_id: int) -> None`
  - `lectura.nombre_del_tema(telefono: str, nombre_perfil: str | None) -> str`
  - `lectura.asegurar_tema(*, telefono, nombre_perfil, database_url, telegram) -> int | None`
    — devuelve `None` si no se pudo crear, y ese `None` significa «usa el General».

- [ ] **Paso 1: Escribir las pruebas que fallan**

Añade a `tests/test_lectura.py`:

```python
# ==========================================================================================
# El tema del paciente
# ==========================================================================================


class TelegramDeTemas:
    """Cuenta cuántos temas se crearon y cuáles se cerraron."""

    def __init__(self, *, falla_al_crear: Exception | None = None) -> None:
        self.creados: list[str] = []
        self.cerrados: list[int] = []
        self._falla_al_crear = falla_al_crear

    async def crear_tema(self, nombre: str) -> int:
        if self._falla_al_crear is not None:
            raise self._falla_al_crear
        self.creados.append(nombre)
        return 900 + len(self.creados)

    async def cerrar_tema(self, tema_id: int) -> None:
        self.cerrados.append(tema_id)


class BaseDeTemas:
    """Lo mínimo de `persistencia` que `asegurar_tema` toca."""

    def __init__(self, tema: int | None = None) -> None:
        self.tema = tema
        self.guardados: list[int] = []

    def instalar(self, monkeypatch):
        from maxicare_daniela import persistencia

        class ConexionFalsa:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
        monkeypatch.setattr(persistencia, "tema_del_paciente", lambda conn, tel: self.tema)
        monkeypatch.setattr(
            persistencia, "asegurar_paciente", lambda conn, **kw: 42
        )

        def guardar(conn, *, id_paciente, topic_id):
            self.guardados.append(topic_id)
            self.tema = topic_id

        monkeypatch.setattr(persistencia, "guardar_tema", guardar)
        return self


def test_el_tema_se_crea_una_vez_y_nace_cerrado(monkeypatch):
    import asyncio

    base = BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
        )
    )

    assert tema == 901
    assert tg.creados == ["Ana Perez · +573001112233"]
    assert tg.cerrados == [901], (
        "el tema no nacio cerrado: un tema abierto deja escribir al paciente a cualquiera "
        "del grupo, que es justo lo que el relevo existe para controlar"
    )
    assert base.guardados == [901]


def test_el_segundo_archivo_reusa_el_tema(monkeypatch):
    import asyncio

    base = BaseDeTemas(tema=777).instalar(monkeypatch)
    tg = TelegramDeTemas()

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233",
            nombre_perfil="Ana Perez",
            database_url="postgresql://x",
            telegram=tg,
        )
    )

    assert tema == 777
    assert tg.creados == [], "se creo un tema nuevo teniendo uno: una persona, un hilo"
    assert base.guardados == []


def test_dos_archivos_simultaneos_de_un_numero_nuevo_crean_un_solo_tema(monkeypatch):
    """La carrera que el indice unico NO atrapa.

    `uq_pacientes_topic` es unico sobre `telegram_topic_id`, y dos temas distintos tienen
    ids distintos: el segundo UPDATE pisa al primero y deja un tema huerfano en Telegram.
    Lo que lo impide es el candado por telefono.
    """
    import asyncio

    base = BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas()

    async def a_la_vez():
        return await asyncio.gather(
            *[
                lectura.asegurar_tema(
                    telefono="573001112233",
                    nombre_perfil="Ana Perez",
                    database_url="postgresql://x",
                    telegram=tg,
                )
                for _ in range(2)
            ]
        )

    temas = asyncio.run(a_la_vez())

    assert len(tg.creados) == 1, f"se crearon {len(tg.creados)} temas para un solo paciente"
    assert temas[0] == temas[1]


def test_dos_telefonos_distintos_no_se_serializan(monkeypatch):
    """El complemento del anterior: un candado global seria un cuello de botella."""
    import asyncio

    BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas()

    async def dos_numeros():
        return await asyncio.gather(
            lectura.asegurar_tema(
                telefono="573001112233", nombre_perfil="Ana",
                database_url="postgresql://x", telegram=tg,
            ),
            lectura.asegurar_tema(
                telefono="573009998877", nombre_perfil="Luis",
                database_url="postgresql://x", telegram=tg,
            ),
        )

    asyncio.run(dos_numeros())
    assert len(tg.creados) == 2


def test_si_telegram_no_deja_crear_el_tema_se_cae_al_general(monkeypatch):
    """Degradar, no perder. El archivo tiene que llegarle al doctor igual."""
    import asyncio

    from maxicare_daniela.canales import ErrorDeCanal

    BaseDeTemas().instalar(monkeypatch)
    tg = TelegramDeTemas(falla_al_crear=ErrorDeCanal("not enough rights"))

    tema = asyncio.run(
        lectura.asegurar_tema(
            telefono="573001112233", nombre_perfil="Ana",
            database_url="postgresql://x", telegram=tg,
        )
    )

    assert tema is None


def test_sin_nombre_de_perfil_el_tema_se_llama_con_el_telefono():
    assert lectura.nombre_del_tema("573001112233", None) == "+573001112233"
    assert lectura.nombre_del_tema("573001112233", "  ") == "+573001112233"
```

- [ ] **Paso 2: Correr las pruebas y verlas fallar**

Ejecuta: `uv run pytest tests/test_lectura.py -q`
Esperado: FALLA con `AttributeError: module 'maxicare_daniela.lectura' has no attribute 'asegurar_tema'`.

- [ ] **Paso 3: Añadir las dos funciones a `persistencia.py`**

Junto a `asegurar_paciente`:

```python
def tema_del_paciente(conn, telefono: str) -> int | None:
    """El tema de Telegram de ese número, o `None` si todavía no tiene.

    El tema se ata al PACIENTE, no a la conversación: una persona puede tener varios
    episodios a lo largo del tiempo y todos comparten hilo. Lo dice la migración 002.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_topic_id FROM pacientes WHERE telefono = %s",
            (telefono,),
        )
        fila = cur.fetchone()
    return fila[0] if fila else None


def guardar_tema(conn, *, id_paciente: int, topic_id: int) -> None:
    """Ata el tema al paciente. `telegram_topic_abierto` queda en FALSE a propósito: el
    tema nace cerrado y solo el relevo (6C) lo abre."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pacientes
               SET telegram_topic_id = %s, telegram_topic_abierto = FALSE
             WHERE id = %s
            """,
            (topic_id, id_paciente),
        )
```

Añade los dos nombres a `__all__` si ese módulo lo tiene.

- [ ] **Paso 4: Implementar el candado y `asegurar_tema` en `lectura.py`**

```python
import asyncio

from . import persistencia
from .canales import ErrorDeCanal

#: Un candado por teléfono para «buscar o crear el tema».
#:
#: Dos archivos del mismo número nuevo llegando a la vez crearían DOS temas, y el índice
#: único `uq_pacientes_topic` no lo impide: son dos ids distintos, así que el segundo UPDATE
#: pisa al primero y deja un tema huérfano en Telegram al que nadie volverá a escribir.
#:
#: Mismo límite conocido que el candado del turno en `atencion.py`: es de PROCESO. Con más
#: de un worker o más de una réplica deja de proteger y haría falta un `pg_advisory_lock`.
#: El contenedor corre con un solo worker a propósito, y por eso hoy alcanza.
_candados_de_tema: dict[str, asyncio.Lock] = {}


def nombre_del_tema(telefono: str, nombre_perfil: str | None) -> str:
    """«Ana Perez · +573001112233», o solo el teléfono si WhatsApp no mandó perfil.

    Un tema sin nombre es peor que uno feo: el doctor no sabría de quién es el hilo.
    """
    limpio = (nombre_perfil or "").strip()
    return f"{limpio} · +{telefono}" if limpio else f"+{telefono}"


async def asegurar_tema(
    *, telefono: str, nombre_perfil: str | None, database_url: str, telegram
) -> int | None:
    """El tema de ese paciente. Lo crea si no existe. Devuelve `None` si no se pudo.

    Ese `None` no es un error que haya que propagar: significa «manda el archivo al tema
    General, como antes». Degradar es aceptable; perder el archivo no lo es.
    """
    candado = _candados_de_tema.setdefault(telefono, asyncio.Lock())
    async with candado:
        try:
            existente = await asyncio.to_thread(_leer_tema, database_url, telefono)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo consultar el tema de %s; va al General", telefono)
            return None
        if existente:
            return existente

        nombre = nombre_del_tema(telefono, nombre_perfil)
        try:
            tema = await telegram.crear_tema(nombre)
        except ErrorDeCanal:
            log.exception("Telegram no dejó crear el tema de %s; va al General", telefono)
            return None

        # Cerrarlo es lo que pone el candado, y va antes de guardarlo: si el cierre falla,
        # queremos el id igual --el tema existe-- pero con constancia de que quedó abierto.
        try:
            await telegram.cerrar_tema(tema)
        except ErrorDeCanal:
            log.error(
                "el tema %s de %s quedó ABIERTO: cualquiera del grupo puede escribirle al "
                "paciente hasta que alguien lo cierre a mano",
                tema,
                telefono,
            )

        try:
            await asyncio.to_thread(
                _guardar_tema, database_url, telefono, nombre_perfil, tema
            )
        except Exception:  # noqa: BLE001
            log.exception("el tema %s no quedó guardado; se usa igual en este turno", tema)
        return tema


def _leer_tema(database_url: str, telefono: str) -> int | None:
    with persistencia.conectar(database_url) as conn:
        return persistencia.tema_del_paciente(conn, telefono)


def _guardar_tema(
    database_url: str, telefono: str, nombre_perfil: str | None, tema: int
) -> None:
    with persistencia.conectar(database_url) as conn:
        id_paciente = persistencia.asegurar_paciente(
            conn, nombre_completo=(nombre_perfil or "").strip() or f"+{telefono}",
            telefono=telefono,
        )
        persistencia.guardar_tema(conn, id_paciente=id_paciente, topic_id=tema)
```

- [ ] **Paso 5: Correr las pruebas y verlas pasar**

Ejecuta: `uv run pytest tests/test_lectura.py -q` → las 11 en verde.

- [ ] **Paso 6: Comprobar por mutación que el candado hace falta**

Sustituye `async with candado:` por `if True:`. Corre
`uv run pytest tests/test_lectura.py -q -k simultaneos`: **tiene que fallar** con dos temas
creados. Deshaz el cambio.

- [ ] **Paso 7: Commit**

```bash
git add src/maxicare_daniela/persistencia.py src/maxicare_daniela/lectura.py tests/test_lectura.py
git commit -m "feat: cada paciente tiene su tema, y nace cerrado"
```

---

## Tarea 4: Correr el lector sobre los bytes

**Archivos:**
- Modificar: `src/maxicare_daniela/lectura.py`
- Modificar: `tests/test_lectura.py`

**Interfaces:**
- Consume: `agentes.lector_archivos`, `canales.ArchivoDescargado` (campos: `contenido: bytes`,
  `mime: str`, `nombre: str`, propiedad `tamano: int`), `agents.Runner`.
- Produce:
  - `lectura.TIPOS_QUE_SE_LEEN: frozenset[str]` — `{"image", "document"}`
  - `lectura.TOPE_BYTES_LECTOR: int` — `20 * 1024 * 1024`
  - `lectura.vale_la_pena_leer(tipo: str, tamano: int) -> bool`
  - `lectura.entrada_para_el_lector(archivo: ArchivoDescargado, tipo: str) -> list[dict]`
  - `lectura.leer_archivo(archivo, *, tipo, correr=None) -> LecturaArchivo | None`

- [ ] **Paso 1: Resolver el PENDIENTE del spec §4.8 ANTES de escribir nada**

El spec deja un `PENDIENTE` explícito: los nombres de campo están verificados por
introspección, pero **no** si `file_data` espera el data URL completo
(`data:application/pdf;base64,...`) o el base64 a secas.

Resuélvelo con una llamada real. Cuesta unos centavos y es la única forma honesta:

```bash
uv run python -c "
import asyncio, base64
from agents import Runner
from maxicare_daniela.agentes import lector_archivos

pdf = base64.b64encode(open('docs/conocimiento/documento-maestro-tratamientos-y-politicas.md','rb').read()[:2000]).decode()
for etiqueta, valor in [('data-url', f'data:application/pdf;base64,{pdf}'), ('base64-pelado', pdf)]:
    try:
        r = asyncio.run(Runner.run(lector_archivos, [{'role':'user','content':[
            {'type':'input_file','filename':'p.pdf','file_data': valor}]}]))
        print(etiqueta, '-> OK')
    except Exception as e:
        print(etiqueta, '-> FALLA:', type(e).__name__, str(e)[:160])
"
```

**Anota el resultado en el spec**, en §4.8, sustituyendo el bloque `PENDIENTE` por lo que
salga. Si las dos formas funcionan, usa el data URL y dilo. No sigas con una suposición: una
suposición razonable no se distingue de un hecho verificado.

- [ ] **Paso 2: Escribir las pruebas que fallan**

Añade a `tests/test_lectura.py`:

```python
# ==========================================================================================
# El lector
# ==========================================================================================


from maxicare_daniela.canales import ArchivoDescargado


def _archivo(contenido=b"\x89PNG bytes", mime="image/png", nombre="radio.png"):
    return ArchivoDescargado(contenido=contenido, mime=mime, nombre=nombre)


@pytest.mark.parametrize("tipo", ["image", "document"])
def test_imagen_y_documento_si_se_leen(tipo):
    assert lectura.vale_la_pena_leer(tipo, 1000) is True


@pytest.mark.parametrize("tipo", ["audio", "voice", "video", "sticker", "text"])
def test_lo_demas_no_paga_el_modelo_caro(tipo):
    """`sol` es el modelo caro. Correrlo sobre un sticker es dinero tirado, y sobre una nota
    de voz no funcionaria sin una API de transcripcion que esta fase no incorpora."""
    assert lectura.vale_la_pena_leer(tipo, 1000) is False


def test_un_archivo_enorme_no_va_al_lector():
    assert lectura.vale_la_pena_leer("image", lectura.TOPE_BYTES_LECTOR + 1) is False
    assert lectura.vale_la_pena_leer("image", lectura.TOPE_BYTES_LECTOR) is True


def test_una_imagen_viaja_como_input_image():
    entrada = lectura.entrada_para_el_lector(_archivo(), "image")
    contenido = entrada[0]["content"][0]

    assert contenido["type"] == "input_image"
    assert contenido["image_url"].startswith("data:image/png;base64,")


def test_un_documento_viaja_como_input_file_con_su_nombre():
    entrada = lectura.entrada_para_el_lector(
        _archivo(contenido=b"%PDF-1.4", mime="application/pdf", nombre="remision.pdf"),
        "document",
    )
    contenido = entrada[0]["content"][0]

    assert contenido["type"] == "input_file"
    assert contenido["filename"] == "remision.pdf"


def test_si_el_lector_revienta_devuelve_none_y_no_propaga():
    """El doctor YA tiene el archivo. Un lector caido no puede tumbar nada mas."""
    import asyncio

    async def correr_que_revienta(*a, **kw):
        raise RuntimeError("el modelo no contesto")

    salida = asyncio.run(
        lectura.leer_archivo(_archivo(), tipo="image", correr=correr_que_revienta)
    )
    assert salida is None
```

- [ ] **Paso 3: Correr las pruebas y verlas fallar**

Ejecuta: `uv run pytest tests/test_lectura.py -q`
Esperado: FALLA con `AttributeError: ... has no attribute 'vale_la_pena_leer'`.

- [ ] **Paso 4: Implementar**

En `lectura.py`. **Usa el formato de `file_data` que resolviste en el Paso 1**, no el que
aparece aquí si resultó ser el otro:

```python
import base64

from agents import Runner

from .agentes import lector_archivos
from .canales import ArchivoDescargado

#: Los dos tipos que el lector puede leer de verdad. `audio`, `voice`, `video` y `sticker`
#: siguen con el aviso factual de la fase 2: no se pagan, y no se fingen.
TIPOS_QUE_SE_LEEN = frozenset({"image", "document"})

#: Por encima de esto no se manda al modelo. El doctor recibe el archivo igual --eso no
#: cambia nunca-- y Daniela usa la entrada de siempre.
TOPE_BYTES_LECTOR = 20 * 1024 * 1024


def vale_la_pena_leer(tipo: str, tamano: int) -> bool:
    return tipo in TIPOS_QUE_SE_LEEN and tamano <= TOPE_BYTES_LECTOR


def entrada_para_el_lector(archivo: ArchivoDescargado, tipo: str) -> list[dict]:
    """El archivo, con la forma que pide la Responses API.

    Los nombres de campo están verificados por introspección de la 0.22.2 instalada, no de
    memoria: `input_image` lleva `image_url`; `input_file` lleva `filename` y `file_data`.
    """
    datos = base64.b64encode(archivo.contenido).decode("ascii")
    if tipo == "image":
        contenido = {
            "type": "input_image",
            "image_url": f"data:{archivo.mime};base64,{datos}",
            "detail": "auto",
        }
    else:
        contenido = {
            "type": "input_file",
            "filename": archivo.nombre,
            "file_data": f"data:{archivo.mime};base64,{datos}",
        }
    return [{"role": "user", "content": [contenido]}]


async def leer_archivo(
    archivo: ArchivoDescargado, *, tipo: str, correr=None
) -> LecturaArchivo | None:
    """Corre `lector_archivos`. Devuelve `None` si falla, y NUNCA propaga.

    `correr` existe solo para poder probar esto sin red.

    Que devuelva `None` en vez de lanzar no es pereza: quien llama es una tarea de fondo que
    ya entregó el archivo al doctor. Una excepción ahí solo llegaría a un log.
    """
    ejecutar = correr or (lambda entrada: Runner.run(lector_archivos, entrada))
    try:
        corrida = await ejecutar(entrada_para_el_lector(archivo, tipo))
    except Exception:  # noqa: BLE001 -- ver el docstring
        log.exception("el lector no pudo con %s", archivo.nombre)
        return None
    return corrida.final_output
```

- [ ] **Paso 5: Correr las pruebas y verlas pasar**

Ejecuta: `uv run pytest tests/test_lectura.py -q` → todas en verde.

- [ ] **Paso 6: Commit**

```bash
git add src/maxicare_daniela/lectura.py tests/test_lectura.py docs/superpowers/specs/2026-09-12-fase-6b-el-muro-design.md
git commit -m "feat: el lector corre sobre los bytes, y su fallo no tumba nada"
```

---

## Tarea 5: La ingesta manda el archivo al tema y arranca el lector

**Archivos:**
- Modificar: `src/maxicare_daniela/lectura.py` (añadir `leer_y_repartir`)
- Modificar: `src/maxicare_daniela/ingesta.py` (`Resultado` y `procesar_mensaje`)
- Modificar: `tests/test_ingesta.py`

**Interfaces:**
- Consume: todo lo de las tareas 1-4.
- Produce:
  - `lectura.leer_y_repartir(archivo, *, tipo, telegram, tema_id, correr=None) -> LecturaNoClinica | None`
    — corre el lector, manda `contexto_clinico` a Telegram y devuelve solo la mitad no clínica.
  - `ingesta.Resultado` gana `lectura: asyncio.Task | None = None`.
  - `ingesta.procesar_mensaje` gana el parámetro `database_url` (ya lo tiene) y usa
    `lectura.asegurar_tema`.

- [ ] **Paso 1: Escribir las pruebas que fallan**

Añade a `tests/test_ingesta.py`. Reutiliza los dobles que ese archivo ya tenga para
`WhatsApp`; si no hay uno con `descargar_media`, escríbelo siguiendo el patrón de
`WhatsAppFalso` en `tests/test_atencion.py`.

```python
# ==========================================================================================
# El destino del archivo (fase 6B)
# ==========================================================================================

TEMA_GENERAL = 0
TEMA_DE_ANA = 901


class TelegramConTemas:
    """Registra a qué tema fue cada cosa. Es lo único que estas pruebas miden."""

    def __init__(self) -> None:
        self.archivos: list[tuple[str, int | None]] = []
        self.mensajes: list[tuple[str, int | None]] = []

    async def enviar_archivo(self, archivo, *, tipo_whatsapp, pie, tema_id=None) -> int:
        self.archivos.append((archivo.nombre, tema_id))
        return 10 + len(self.archivos)

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None) -> int:
        self.mensajes.append((texto, tema_id))
        return 20 + len(self.mensajes)


class WhatsAppConArchivo:
    def __init__(self, *, tamano: int = 2048) -> None:
        self.tamano = tamano

    async def descargar_media(self, media_id, *, nombre_original=None):
        from maxicare_daniela.canales import ArchivoDescargado

        return ArchivoDescargado(
            contenido=b"x" * self.tamano, mime="image/jpeg", nombre="radio.jpg"
        )


def _sin_base(monkeypatch):
    """`procesar_mensaje` registra en Neon; aquí eso no es lo que se mide."""
    from maxicare_daniela import ingesta as mod

    monkeypatch.setattr(mod, "_registrar", lambda url, m: True)
    monkeypatch.setattr(mod, "_marcar_reenviado", lambda url, w, t, s: None)
    monkeypatch.setattr(mod, "_marcar_fallo", lambda url, w, e: None)


def _mensaje_con_foto(**cambios):
    from maxicare_daniela.ingesta import MensajeEntrante

    campos = dict(
        wamid="wamid-foto-1",
        telefono="573001112233",
        nombre_perfil="Ana Perez",
        tipo="image",
        media_id="media-1",
        mime="image/jpeg",
    )
    campos.update(cambios)
    return MensajeEntrante(**campos)


def test_el_archivo_va_al_tema_del_paciente_y_el_aviso_al_general(monkeypatch):
    """El cambio visible de 6B: el archivo deja de caer en el General.

    Al General va un aviso, porque es donde los doctores miran; el archivo se deposita en
    el hilo de esa persona, que es el que conserva su historial.
    """
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(
        lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA)
    )
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert tg.archivos == [("radio.jpg", TEMA_DE_ANA)]
    assert len(tg.mensajes) == 1
    texto, tema = tg.mensajes[0]
    assert tema == TEMA_GENERAL
    assert "Ana Perez" in texto


def test_un_texto_suelto_sigue_yendo_al_general(monkeypatch):
    """Lo único que se muda al tema del paciente es el ARCHIVO."""
    import asyncio

    from maxicare_daniela import ingesta
    from maxicare_daniela.ingesta import MensajeEntrante

    _sin_base(monkeypatch)
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            MensajeEntrante(
                wamid="w-1", telefono="573001112233", nombre_perfil="Ana",
                tipo="text", texto="Hola",
            ),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert tg.archivos == []
    assert tg.mensajes[0][1] == TEMA_GENERAL


def test_el_lector_no_retrasa_la_entrega_del_archivo(monkeypatch):
    """La garantía de la fase 2, como aserción y no como comentario.

    Con un lector que tarda un segundo, `procesar_mensaje` tiene que haber vuelto --y el
    archivo estar ya en Telegram-- mucho antes de que el lector termine. Si alguien pone un
    `await` delante de la entrega, esta prueba cae.
    """
    import asyncio
    import time

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))

    async def lector_lento(*a, **kw):
        await asyncio.sleep(1.0)
        return None

    monkeypatch.setattr(lectura, "leer_y_repartir", lector_lento)
    tg = TelegramConTemas()

    async def corrida():
        arranque = time.monotonic()
        resultado = await ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
        tardo = time.monotonic() - arranque
        if resultado.lectura is not None:
            resultado.lectura.cancel()
        return resultado, tardo

    resultado, tardo = asyncio.run(corrida())

    assert resultado.reenviado is True
    assert tg.archivos, "el archivo no llego a Telegram"
    assert tardo < 0.3, f"la entrega del archivo espero al lector: tardo {tardo:.2f} s"
    assert resultado.lectura is not None, "no se arranco el lector"


def test_si_no_hay_tema_el_archivo_cae_al_general(monkeypatch):
    """Degradar, no perder. Un fallo de Telegram al crear el tema no puede dejar al doctor
    sin la radiografia."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(None))
    monkeypatch.setattr(lectura, "leer_y_repartir", _devuelve_async(None))
    tg = TelegramConTemas()

    asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert tg.archivos == [("radio.jpg", TEMA_GENERAL)]
    assert tg.mensajes == [], "sin tema propio no hay nada que avisar: el archivo YA esta ahi"


@pytest.mark.parametrize("tipo,mime", [("audio", "audio/ogg"), ("sticker", "image/webp")])
def test_lo_que_no_se_lee_no_arranca_el_lector(monkeypatch, tipo, mime):
    """`sol` es el modelo caro y no se paga por un sticker."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _revienta_si_se_llama)

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(tipo=tipo, mime=mime),
            whatsapp=WhatsAppConArchivo(),
            telegram=TelegramConTemas(),
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert resultado.lectura is None


def test_un_archivo_enorme_no_arranca_el_lector(monkeypatch):
    """Llega al doctor igual; lo unico que no ocurre es la lectura."""
    import asyncio

    from maxicare_daniela import ingesta, lectura

    _sin_base(monkeypatch)
    monkeypatch.setattr(lectura, "asegurar_tema", _devuelve_async(TEMA_DE_ANA))
    monkeypatch.setattr(lectura, "leer_y_repartir", _revienta_si_se_llama)
    tg = TelegramConTemas()

    resultado = asyncio.run(
        ingesta.procesar_mensaje(
            _mensaje_con_foto(),
            whatsapp=WhatsAppConArchivo(tamano=lectura.TOPE_BYTES_LECTOR + 1),
            telegram=tg,
            database_url="postgresql://x",
            tema_general=TEMA_GENERAL,
        )
    )

    assert resultado.lectura is None
    assert tg.archivos, "el archivo grande tiene que llegar al doctor igual"
```

Dos ayudantes que estas pruebas usan; ponlos junto a los dobles:

```python
def _devuelve_async(valor):
    async def _fn(*a, **kw):
        return valor

    return _fn


async def _revienta_si_se_llama(*a, **kw):
    raise AssertionError("se arranco el lector cuando no debia: eso es dinero tirado")
```

- [ ] **Paso 2: Correr las pruebas y verlas fallar**

Ejecuta: `uv run pytest tests/test_ingesta.py -q`

- [ ] **Paso 3: Añadir `leer_y_repartir` a `lectura.py`**

```python
async def leer_y_repartir(
    archivo: ArchivoDescargado, *, tipo: str, telegram, tema_id: int | None, correr=None
) -> LecturaNoClinica | None:
    """Lee, manda lo clínico al tema del paciente y devuelve SOLO la mitad no clínica.

    Aquí es donde el muro se ejerce: `repartir` devuelve dos cosas, una sale por Telegram y
    la otra es el valor de retorno. La clínica no se guarda en ninguna variable que
    sobreviva a esta función.
    """
    leida = await leer_archivo(archivo, tipo=tipo, correr=correr)
    if leida is None:
        try:
            await telegram.enviar_mensaje(
                "⚠️ No se pudo leer este archivo automáticamente.", tema_id=tema_id
            )
        except Exception:  # noqa: BLE001
            log.exception("no se pudo avisar de la lectura fallida")
        return None

    clinico, no_clinica = repartir(leida)
    try:
        await telegram.enviar_mensaje(f"📄 <b>Lectura</b>\n{clinico}", tema_id=tema_id)
    except Exception:  # noqa: BLE001
        log.exception("la lectura no llegó a Telegram; el archivo sí está")
    return no_clinica
```

- [ ] **Paso 4: Cambiar `Resultado` y `procesar_mensaje` en `ingesta.py`**

`Resultado` gana un campo:

```python
    #: La tarea del lector, si se arrancó. `atencion.atender` la recoge cuando cierra la
    #: ventana del búfer. Es una `Task` y no un valor a propósito: esperarla aquí pondría
    #: una llamada al modelo delante de la entrega del archivo al doctor.
    lectura: asyncio.Task | None = None
```

Dentro de `procesar_mensaje`, en la rama `if m.trae_archivo:`, **después** de la descarga y
**antes** del envío:

```python
            tema = await lectura_mod.asegurar_tema(
                telefono=m.telefono,
                nombre_perfil=m.nombre_perfil,
                database_url=database_url,
                telegram=telegram,
            )
            destino = tema or tema_general
            pie = componer_aviso(m, tamano=archivo.tamano)
            telegram_id = await telegram.enviar_archivo(
                archivo, tipo_whatsapp=m.tipo, pie=pie, tema_id=destino
            )
            if tema:
                # El archivo ya no cae en el General, así que el General tiene que enterarse
                # igual: es donde los doctores miran.
                await telegram.enviar_mensaje(
                    f"📎 Llegó un archivo de {_escapar(m.nombre_perfil or m.telefono)}"
                    f" — está en su tema.",
                    tema_id=tema_general,
                )
            if vale_la_pena_leer(m.tipo, archivo.tamano):
                tarea = asyncio.create_task(
                    lectura_mod.leer_y_repartir(
                        archivo, tipo=m.tipo, telegram=telegram, tema_id=destino
                    )
                )
            tamano = archivo.tamano
```

Importa el módulo como `from . import lectura as lectura_mod` para no chocar con el
parámetro `lectura` del `Resultado`, y `from .lectura import vale_la_pena_leer`. Devuelve
`Resultado(..., lectura=tarea)` en el retorno de éxito, con `tarea = None` inicializada
antes del `try`.

- [ ] **Paso 5: Correr las pruebas y verlas pasar**

Ejecuta: `uv run pytest tests/test_ingesta.py -q` y luego `uv run pytest -q` entera.

- [ ] **Paso 6: Commit**

```bash
git add src/maxicare_daniela/lectura.py src/maxicare_daniela/ingesta.py tests/test_ingesta.py
git commit -m "feat: el archivo entra al tema del paciente y el lector arranca detras"
```

---

## Tarea 6: Daniela recoge la lectura al cerrar la ventana

Es la tarea con más riesgo de la fase: toca el búfer, y el `CLAUDE.md` lista sus tres
invariantes como no negociables.

**Archivos:**
- Modificar: `src/maxicare_daniela/atencion.py`
- Modificar: `src/maxicare_daniela/runtime.py` (`_entregar`, ~línea 531)
- Modificar: `tests/test_atencion.py`

**Interfaces:**
- Consume: `ingesta.Resultado.lectura`, `contratos.LecturaNoClinica`.
- Produce: `atencion.atender(..., lectura: asyncio.Task | None = None)`.

- [ ] **Paso 1: Escribir las pruebas que fallan**

En `tests/test_atencion.py`, sección nueva «14 · La lectura del archivo»:

```python
# ==========================================================================================
# 14 · La lectura del archivo
# ==========================================================================================

CLINICO = "reabsorcion radicular en el 46, con lesion periapical de 4 mm"


def _no_clinica(**cambios) -> LecturaNoClinica:
    campos = dict(
        tipo_documento="remision",
        tratamiento="ortodoncia",
        origen="Clinica Dental Norte",
        fecha_documento=None,
        confianza="alta",
    )
    campos.update(cambios)
    return LecturaNoClinica(**campos)


def _tarea(valor, *, tarda: float = 0.0):
    """Una `Task` ya corriendo, como la que devuelve `ingesta.procesar_mensaje`."""

    async def cuerpo():
        if tarda:
            await asyncio.sleep(tarda)
        return valor

    return asyncio.ensure_future(cuerpo())


def mensaje_imagen(**cambios) -> ingesta.MensajeEntrante:
    campos = dict(
        wamid="wamid-foto-1",
        telefono=TELEFONO,
        nombre_perfil="Ana",
        tipo="image",
        texto=None,
        media_id="media-1",
        mime="image/jpeg",
    )
    campos.update(cambios)
    return ingesta.MensajeEntrante(**campos)


def test_con_confianza_alta_daniela_sabe_que_documento_llego(monkeypatch):
    """La entrada del modelo nombra el tratamiento y el origen, y NADA clinico."""
    _, turnos = preparar(monkeypatch)

    async def corrida():
        return await _atender(mensaje_imagen(), lectura=_tarea(_no_clinica()))

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert "ortodoncia" in entrada
    assert "Clinica Dental Norte" in entrada
    assert CLINICO not in entrada
    assert "no puedes verlo" not in entrada, "se quedo con la entrada de antes de 6B"
    assert turnos.llamadas[-1]["hubo_adjunto"] is True, (
        "sin la marca, `sin_lectura_clinica` no vigila justo el turno donde importa"
    )


def test_por_debajo_de_alta_daniela_pregunta_en_vez_de_asumir(monkeypatch):
    _, turnos = preparar(monkeypatch)

    async def corrida():
        return await _atender(
            mensaje_imagen(),
            lectura=_tarea(_no_clinica(tratamiento="no_identificado", confianza="baja")),
        )

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert "preguntaselo" in entrada.lower() or "pregúntaselo" in entrada.lower()
    assert "ortodoncia" not in entrada


def test_el_lector_caido_no_calla_a_daniela(monkeypatch):
    """Con la tarea devolviendo None sale la entrada de siempre, y el paciente recibe
    respuesta igual. Un lector caido no puede dejar mudo al sistema."""
    wa = WhatsAppFalso()
    _, turnos = preparar(monkeypatch)

    async def corrida():
        return await _atender(mensaje_imagen(), whatsapp=wa, lectura=_tarea(None))

    atendido = asyncio.run(corrida())

    assert atendido.respondido is True
    assert len(wa.enviados) == 1
    assert "no puedes verlo" in turnos.llamadas[-1]["entrada"]


def test_sin_lectura_ninguna_el_camino_de_hoy_sigue_intacto(monkeypatch):
    """`lectura=None` es lo que pasa con un audio o un sticker: ni se intento leer."""
    _, turnos = preparar(monkeypatch)

    atender(mensaje_imagen())

    assert "no puedes verlo" in turnos.llamadas[-1]["entrada"]


def test_el_lector_lento_no_se_suma_a_la_ventana(monkeypatch):
    """El invariante que el CLAUDE.md lista: el retardo se descuenta, no se suma.

    Con una ventana de 0.4 s y un lector que tarda mucho mas que el margen, el turno sale
    SIN la lectura en vez de esperarla. Si alguien encadena el lector delante del bufer,
    esta prueba cae -- y tambien cae si alguien sube `MARGEN_LECTURA_SEGUNDOS` pensando que
    «asi da tiempo».
    """
    _, turnos = preparar(monkeypatch)
    monkeypatch.setattr(atencion, "MARGEN_LECTURA_SEGUNDOS", 0.1)

    async def corrida():
        arranque = time.monotonic()
        atendido = await _atender(
            mensaje_imagen(),
            lectura=_tarea(_no_clinica(), tarda=5.0),
            ventana=VENTANA_CORTA,
            tope=2.0,
        )
        return atendido, time.monotonic() - arranque

    atendido, tardo = asyncio.run(corrida())

    assert atendido.respondido is True
    assert tardo < 2.0, f"el turno espero al lector: tardo {tardo:.1f} s"
    assert "no puedes verlo" in turnos.llamadas[-1]["entrada"]


def test_la_lectura_se_ata_al_mensaje_que_traia_el_archivo(monkeypatch):
    """Foto + «esto que es?» son DOS mensajes que el bufer agrupa en un turno.

    La lectura pertenece al primero. Si se atara al ultimo, la entrada del modelo diria que
    el mensaje de TEXTO trae una remision, que es falso y ademas confunde al modelo.
    """
    _, turnos = preparar(monkeypatch)

    async def corrida():
        foto = asyncio.ensure_future(
            _atender(
                mensaje_imagen(),
                lectura=_tarea(_no_clinica()),
                ventana=VENTANA_CORTA,
                tope=2.0,
            )
        )
        await asyncio.sleep(0.05)
        texto = asyncio.ensure_future(
            _atender(
                mensaje_texto("¿esto qué es?", wamid="wamid-texto-2"),
                ventana=VENTANA_CORTA,
                tope=2.0,
            )
        )
        return await asyncio.gather(foto, texto)

    asyncio.run(corrida())

    entrada = turnos.llamadas[-1]["entrada"]
    assert entrada.count("ortodoncia") == 1, (
        "la lectura se pego a los dos mensajes del grupo, o a ninguno"
    )
    # La linea de la remision va con la foto, no con el texto.
    posicion_lectura = entrada.index("ortodoncia")
    posicion_texto = entrada.index("¿esto qué es?")
    assert posicion_lectura < posicion_texto
```

Añade a la cabecera del archivo los imports que falten: `time`, y
`from maxicare_daniela.contratos import LecturaNoClinica`. `VENTANA_CORTA` y
`mensaje_texto` ya existen en ese archivo.

**Comprueba que `limpiar_estado()` limpia también las tareas colgadas**: si una prueba deja
una `Task` sin esperar, pytest avisa con `Task was destroyed but it is pending`. Las tres
pruebas con `_tarea(..., tarda=...)` dejan una viva a propósito; si el aviso ensucia la
salida, cancélalas en un `finally`.

- [ ] **Paso 2: Correr y ver fallar**

Ejecuta: `uv run pytest tests/test_atencion.py -q -k lectura`

- [ ] **Paso 3: El búfer guarda las lecturas**

`_Bufer` gana un campo:

```python
    #: La tarea del lector de cada mensaje que traía archivo, por `wamid`. No todas las
    #: entradas del grupo tienen una: un texto suelto no tiene nada que leer.
    lecturas: dict[str, asyncio.Task]
```

En el bloque de bookkeeping —**sin añadir un solo `await`**, que es lo que lo mantiene
atómico— tanto la rama del que se suma como la del que abre registran su tarea:

```python
        if lectura is not None:
            esperando.lecturas[mensaje.wamid] = lectura
```

y para el que abre el grupo:

```python
    bufer = _Bufer(
        [mensaje],
        primero=ahora,
        ultimo=ahora,
        despierta=asyncio.Event(),
        lecturas={mensaje.wamid: lectura} if lectura is not None else {},
    )
```

- [ ] **Paso 4: Recoger las lecturas DESPUÉS de la ventana**

Justo después del `finally` que saca el búfer, y **antes** del candado del turno:

```python
    # El lector corrió EN PARALELO con la ventana, no delante. Aquí solo se recoge lo que ya
    # esté listo: lo que quede se descarta.
    #
    # Encadenarlo delante sumaría sus 4-8 s a los 20 de la ventana y, en el peor caso,
    # Daniela habría contestado ya el texto que vino junto a la foto sin saber que había una
    # foto. Eso está medido: el 12/09, un documento recibido a las 19:03:28 se entregó
    # después de un texto recibido a las 19:03:29.
    leidas = await _recoger_lecturas(bufer.lecturas)
```

y la función:

```python
async def _recoger_lecturas(
    tareas: dict[str, asyncio.Task], margen: float = MARGEN_LECTURA_SEGUNDOS
) -> dict[str, LecturaNoClinica]:
    """Lo que el lector alcanzó a producir. Lo que no, no llegó.

    `margen` es corto a propósito: la ventana ya le dio al lector sus 20 segundos. Esto es
    la cola, no la espera.
    """
    listas: dict[str, LecturaNoClinica] = {}
    for wamid, tarea in tareas.items():
        try:
            # `shield` para que el timeout NO cancele la tarea: el lector sigue corriendo y
            # su mensaje a Telegram llega igual, tarde pero llega. Cancelarla dejaría al
            # doctor sin la lectura solo porque Daniela ya no la necesitaba.
            valor = await asyncio.wait_for(asyncio.shield(tarea), timeout=margen)
        except Exception:  # noqa: BLE001 -- TimeoutError incluido; ninguna puede tumbar el turno
            log.info("la lectura de %s no llegó a tiempo; el turno sale sin ella", wamid)
            continue
        if valor is not None:
            listas[wamid] = valor
    return listas
```

Añade a `config.py`, junto a `TOPE_BUFER_SEGUNDOS`:

```python
#: Lo que se le concede al lector DESPUÉS de que la ventana del búfer cerró. Corto a
#: propósito: la ventana ya le dio sus 20 segundos, esto es la cola.
MARGEN_LECTURA_SEGUNDOS = 3.0
```

- [ ] **Paso 5: Usar la lectura en la entrada del modelo**

`_entrada_para_el_modelo(mensaje, lectura=None)` y `_entrada_del_grupo(mensajes, leidas)`.
En la rama `if mensaje.trae_archivo:`, cuando hay lectura:

```python
        if lectura is not None:
            aviso = f"[El paciente acaba de enviar {que}. Un lector automático lo revisó y "
            aviso += "el doctor ya lo tiene. "
            if lectura.tratamiento != "no_identificado":
                aviso += f"Es sobre {lectura.tratamiento.replace('_', ' ')}. "
            else:
                aviso += (
                    "No se pudo identificar para qué tratamiento es: pregúntaselo. "
                )
            if lectura.origen:
                aviso += f"Viene de «{lectura.origen}». "
            aviso += (
                "NO sabes qué muestra el archivo y no debes suponerlo: eso lo ve el "
                "doctor.]"
            )
```

Mantén la rama de hoy intacta para cuando `lectura is None`.

- [ ] **Paso 6: Pasar la tarea desde `runtime._entregar`**

```python
        atendido = await atencion.atender(
            m,
            whatsapp=_whatsapp,
            telegram=_telegram,
            config=config,
            calendario=_calendario,
            al_escalar=_avisar_a_doctores,
            lectura=entrega.lectura if entrega is not None else None,
        )
```

- [ ] **Paso 7: Correr TODA la suite**

Ejecuta: `uv run pytest -q`
Esperado: las 343 de antes más las nuevas, todas en verde. **Si alguna de las nueve pruebas
del búfer falla, para**: es señal de que se movió un invariante.

- [ ] **Paso 8: Mutación de los tres invariantes del búfer**

Una por una, comprobando que cae alguna prueba y deshaciendo después:

1. Mover `leidas = await _recoger_lecturas(...)` a **antes** del bloque del búfer → tiene que
   caer `test_el_lector_lento_no_se_suma_a_la_ventana`.
2. Meter un `await asyncio.sleep(0)` dentro del bloque de bookkeeping → tiene que caer
   alguna de las pruebas de concurrencia del búfer.
3. Quitar el `finally` que saca el búfer → tiene que caer la prueba que ya existe para eso.

- [ ] **Paso 9: Commit**

```bash
git add src/maxicare_daniela/atencion.py src/maxicare_daniela/runtime.py src/maxicare_daniela/config.py tests/test_atencion.py
git commit -m "feat: Daniela reconoce el documento que el paciente mando"
```

---

## Tarea 7: EL ENTREGABLE — la prueba del muro, y la frase del evaluador

**Archivos:**
- Crear: `tests/test_muro.py`
- Modificar: `src/maxicare_daniela/guardrails.py` (`_evaluador_clinico`, ~línea 235)
- Modificar: `tests/test_guardrails.py`

**Interfaces:** consume todo lo anterior. No produce nada nuevo.

- [ ] **Paso 1: Escribir la prueba del entregable**

`tests/test_muro.py`:

```python
"""EL ENTREGABLE de la fase 6B, literal del plan-agentes.json:

    «Una prueba manda una remisión, captura TODO lo que entra a `Runner.run(daniela)` y
    comprueba que el contenido clínico no aparece ahí, mientras sí aparece en el mensaje
    enviado a Telegram.»

Si esta prueba se borra o se debilita, la fase deja de estar cerrada.
"""
```

La prueba:

1. Define `CENTINELA = "reabsorcion radicular en el 46, con lesion periapical de 4 mm"` —
   una frase que **no puede** aparecer por accidente.
2. Monta un `ModeloGuionizado` (de `tests/dobles.py`) para `daniela` que **captura toda su
   entrada** en una lista.
3. Monta un doble del lector que devuelve un `LecturaArchivo` con esa centinela en
   `contexto_clinico` y `tratamiento="ortodoncia"`, `confianza="alta"`.
4. Corre el camino completo: `ingesta.procesar_mensaje` y luego `atencion.atender` con la
   tarea que devolvió.
5. Afirma, en este orden:

```python
    entrada_del_modelo = json.dumps(capturadas, ensure_ascii=False, default=str)

    assert CENTINELA not in entrada_del_modelo, (
        "EL MURO SE CAYO: el contenido clinico del documento llego al contexto de Daniela. "
        "Eso es exactamente el fallo que esta fase existe para impedir."
    )
    assert "reabsorcion" not in entrada_del_modelo.lower()
    assert "periapical" not in entrada_del_modelo.lower()

    enviado_a_telegram = "\n".join(tg.mensajes)
    assert CENTINELA in enviado_a_telegram, (
        "la otra mitad del muro: el doctor tiene que recibir la lectura completa"
    )

    # Y lo no clinico SI cruzo, o la fase no sirve de nada.
    assert "ortodoncia" in entrada_del_modelo
```

Las dos últimas aserciones importan tanto como las dos primeras: un muro que además bloquea
lo que debe pasar es un muro que alguien acabará quitando.

- [ ] **Paso 2: Correr y ver pasar**

Ejecuta: `uv run pytest tests/test_muro.py -q`

- [ ] **Paso 3: Mutar el muro y ver caer la prueba**

En `lectura.leer_y_repartir`, devuelve `leida` (el `LecturaArchivo` entero) en vez de
`no_clinica`. **`test_muro.py` tiene que fallar** nombrando el muro. Deshaz el cambio.
Si no falló, la prueba no vale.

- [ ] **Paso 4: Ampliar la excepción del evaluador clínico**

En `guardrails.py`, `_evaluador_clinico`, la línea que hoy dice:

```
"información, o dice que un doctor lo va a revisar. Hablar de un tratamiento que el "
"paciente ya mencionó no es diagnosticar."
```

pasa a:

```
"información, o dice que un doctor lo va a revisar. Hablar de un tratamiento que el "
"paciente ya mencionó --o que viene nombrado en un documento que el propio paciente "
"envió-- no es diagnosticar."
```

**Por qué:** con `hubo_adjunto = True` el evaluador corre siempre en ese turno. «Ya me llegó
tu remisión para ortodoncia» cae en una zona que la excepción de hoy no cubre, y Daniela
podría autobloquearse justo en el turno que 6B existe para mejorar. Es el único punto de la
fase que toca las instrucciones de un agente, y es un evaluador, no Daniela.

- [ ] **Paso 5: Prueba de que la excepción no abrió un hueco**

En `tests/test_guardrails.py`, comprueba con el evaluador doblado que:
- «Ya me llegó tu remisión para ortodoncia, el doctor la revisa» → **no** dispara.
- «Por la radiografía que mandaste, tienes una caries profunda en el 46» → **sí** dispara.

La segunda es la que importa: sin ella, la excepción nueva sería una puerta abierta.

- [ ] **Paso 6: Commit**

```bash
git add tests/test_muro.py src/maxicare_daniela/guardrails.py tests/test_guardrails.py
git commit -m "test: el muro, con la prueba que cierra el entregable de la fase 6"
```

---

## Tarea 8: `scripts/probar_lectura.py`, el entregable ejecutable

**Archivos:**
- Crear: `scripts/probar_lectura.py`
- Modificar: `CLAUDE.md` (la tabla de entregables por fase)
- Modificar: `.claude/rules/atencion-whatsapp.md` (una sección sobre el muro)

- [ ] **Paso 1: Escribir el script**

Sigue el patrón exacto de `scripts/probar_atencion.py`: cabecera con
`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`, marcadores ASCII
(`OK` / `FALLA` / `->`), esquema de pruebas propio que se crea y se borra **comprobando el
borrado**, y un doble de Telegram que captura en vez de enviar.

Comprobaciones, numeradas como en `probar_atencion.py`:

1. El tema se crea una vez y nace cerrado.
2. El segundo archivo del mismo paciente reusa el tema.
3. `telegram_topic_id` quedó persistido en Neon (esquema `pruebas_lectura`).
4. El archivo llegó al tema del paciente y el aviso al General.
5. **EL MURO:** la centinela está en lo enviado a Telegram y no en la entrada de Daniela.
6. Sin `--chat`, el lector va doblado y **no se gasta un solo token**. Con `--chat`, corre
   `lector_archivos` de verdad sobre una imagen de ejemplo del repositorio.

- [ ] **Paso 2: Correrlo sin `--chat`**

Ejecuta: `uv run python scripts/probar_lectura.py`
Esperado: las 6 comprobaciones en `OK`, el esquema borrado, cero tokens.

- [ ] **Paso 3: Correrlo con `--chat`**

Ejecuta: `uv run python scripts/probar_lectura.py --chat`
Esperado: lo mismo, más una lectura real impresa. **Gasta tokens** (~$0.02).

- [ ] **Paso 4: Documentar**

En `CLAUDE.md`, añade la fila a la tabla de entregables:

```
| `scripts/probar_lectura.py` | el muro y el tema del paciente (fase 6B) | solo con `--chat` |
```

En `.claude/rules/atencion-whatsapp.md`, una sección «El muro» con las tres cosas que no se
pueden mover: el reparto es un tipo sin el campo, el archivo nunca espera al modelo, y el
lector corre en paralelo con la ventana.

- [ ] **Paso 5: Suite completa y commit**

```bash
uv run pytest -q
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon
uv run python scripts/probar_atencion.py
git add scripts/probar_lectura.py CLAUDE.md .claude/rules/atencion-whatsapp.md
git commit -m "feat: probar_lectura.py, el muro de punta a punta"
```

Las tres se corren porque la tarea 6 tocó el búfer, y el `CLAUDE.md` avisa de que
`pytest -q` a secas no caza una regresión en `tocar_conversacion`.

---

## Verificación final de la fase

```bash
uv run pytest -q                                    # offline
MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon    # contra Neon
uv run python scripts/probar_lectura.py             # el entregable, sin gastar
uv run python scripts/probar_atencion.py            # el turno sigue entero
```

**La fase NO está cerrada hasta que una persona mire un Telegram real** y vea el tema del
paciente con el archivo dentro y la lectura debajo. Ningún script puede hacerlo, y decir que
sí sería mentir sobre lo que está verificado.

**Antes de desplegar, avisar a la clínica** (spec §4.9): hasta hoy los archivos caían en el
tema General y a partir de este despliegue caen en el hilo de cada paciente, con un aviso en
el General. Un doctor que no lo sepa creerá que dejaron de llegar radiografías. Esto no es
una tarea de código y por eso no tiene número, pero sin ello el despliegue genera un
incidente que no es técnico.

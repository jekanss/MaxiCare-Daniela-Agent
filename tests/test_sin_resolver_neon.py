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

import psycopg
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
    """El CHECK tiene que vivir en el esquema de pruebas, no solo en `public`.

    `CheckViolation` y no `Exception` a secas: un `Exception` ancho pasaria igual si
    `registrar_caso` reventara con un `TypeError` por un cambio de firma, sin que el CHECK
    tuviera nada que ver.
    """
    with persistencia.conectar(esquema) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            persistencia.registrar_caso(conn, huella="x:y", tipo="INVENTADO")


def test_un_registro_que_falla_no_envenena_la_conexion_del_llamador(esquema):
    """La regresion que importa de verdad.

    `registrar_caso` es instrumentacion: se llama desde una conexion que ya trae escrituras
    clinicas encima (`atencion._anotar_resultado`, `relevo.activar`, tarea 2). Si el fallo de
    un caso deja la transaccion de esa conexion abortada, toda sentencia posterior sobre ELLA
    --incluida una escritura clinica que no tiene nada que ver con este caso-- muere con
    «current transaction is aborted», aunque el `INSERT` fallido no importara para nada.

    Sin el `rollback` dentro de `registrar_caso`, este intento de escribir un caso VALIDO en
    la misma conexion, justo despues del invalido, revienta igual que el primero.
    """
    with persistencia.conectar(esquema) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            persistencia.registrar_caso(conn, huella="x:y", tipo="INVENTADO")

        # La MISMA conexion, sin abrir otra, tiene que poder seguir escribiendo.
        persistencia.registrar_caso(
            conn, huella="falta_dato:sobrevive:precio", tipo="FALTA_DATO"
        )
        fila = _fila(conn, "falta_dato:sobrevive:precio")

    assert fila["contador"] == 1


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

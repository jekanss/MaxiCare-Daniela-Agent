"""Las pruebas del panel: el vocabulario de tratamientos y la edición de fichas.

Las que tocan Neon llevan `@pytest.mark.neon` y escriben en el esquema `pruebas`.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from maxicare_daniela import contratos, panel, persistencia, runtime
from maxicare_daniela.config import cargar_dotenv

CORE = ("precio", "duracion", "profesional")


def _url_de_pruebas() -> str:
    """La conexión DIRECTA con `search_path=pruebas`. El pooler de Neon rechaza `options`
    como parámetro de arranque, así que hay que quitarle el `-pooler.` al host."""
    url = os.environ.get("MAXICARE_DATABASE_URL", "")
    directa = url.replace("-pooler.", ".")
    sep = "&" if "?" in directa else "?"
    return f"{directa}{sep}options=-csearch_path%3Dpruebas"


@pytest.fixture
def conn():
    # `@pytest.mark.neon` por sí solo no salta nada: en este proyecto el corte real de
    # `uv run pytest -q` lo hace cada fixture, revisando la variable a mano (igual que
    # `test_tools_neon.py::url`). Sin este chequeo, la suite offline intentaría conectarse
    # a Neon de verdad en cada corrida.
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    # Tampoco hay conftest.py en la raíz que cargue el .env todavía (ver `test_tools_neon.py`,
    # que hace lo mismo dentro de su propia fixture): se carga aquí, no en el cuerpo del
    # módulo, para no fijarle el entorno a las demás pruebas antes de que corran.
    cargar_dotenv()
    with psycopg.connect(_url_de_pruebas()) as c:
        with c.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS pruebas")
        c.commit()
        persistencia.aplicar_esquema(c)
        yield c


@pytest.mark.neon
def test_las_migraciones_nuevas_se_aplican(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tratamientos")
        assert cur.fetchone()[0] >= 14
        cur.execute("SELECT to_regclass('pruebas.cambios_configuracion')")
        assert cur.fetchone()[0] is not None


@pytest.mark.neon
def test_postgres_rechaza_una_clave_que_no_es_una_clave(conn):
    """El CHECK, no solo el validador de Python. Una frase clínica no entra ni por SQL."""
    malas = [
        "Carillas",                       # mayúscula
        "carillas esteticas",             # espacio
        "absceso-periapical",             # guion
        "ca",                             # muy corta
        "reconstruccion_de_hemiarcada_superior_izquierda",  # muy larga
    ]
    for clave in malas:
        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO tratamientos (clave, etiqueta) VALUES (%s, %s)", (clave, "x")
            )
        conn.rollback()


def test_validar_clave_rechaza_lo_que_no_es_una_clave():
    """Offline: la misma regla que el CHECK de Postgres, para poder dar un mensaje decente
    antes de llegar a la base."""
    for mala in ["Carillas", "carillas esteticas", "absceso-periapical", "ca", "1carillas"]:
        with pytest.raises(ValueError):
            panel.validar_clave(mala)
    assert panel.validar_clave("  Carillas  ".strip().lower()) == "carillas"
    assert panel.validar_clave("carillas_esteticas") == "carillas_esteticas"


@pytest.mark.neon
def test_listar_tratamientos_dice_cuales_estan_en_el_muro(conn):
    # Corrección 1 sobre el brief: el esquema `pruebas` persiste entre corridas. Sin el
    # try/finally, la segunda vez que alguien corra esta prueba `crear_tratamiento` lanzaría
    # «ya existe» sobre un tratamiento que nadie quiso dejar ahí.
    try:
        filas = {f["clave"]: f for f in panel.listar_tratamientos(conn)}
        assert filas["implantes"]["en_el_muro"] is True
        panel.crear_tratamiento(conn, clave="carillas", etiqueta="Carillas", usuario="prueba")
        filas = {f["clave"]: f for f in panel.listar_tratamientos(conn)}
        assert filas["carillas"]["en_el_muro"] is False
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM cambios_configuracion WHERE tabla = 'tratamientos' "
                "AND clave = 'carillas'"
            )
            cur.execute("DELETE FROM tratamientos WHERE clave = 'carillas'")
        conn.commit()


@pytest.mark.neon
def test_listar_tratamientos_cuenta_lo_que_falta(conn):
    # Corrección 3 sobre el brief: en el esquema `pruebas` `base_conocimiento` está VACÍA
    # -las migraciones se aplican pero la semilla no se carga-, así que los 14 tratamientos
    # salen con `fichas=0` y `faltan` con los tres conceptos. Eso pasaría igual si `faltan`
    # devolviera siempre los tres, para todos: no probaría nada. Se prueban las dos caras,
    # como pide `.claude/rules/pruebas.md`: el tratamiento CON ficha ya no debe listar ese
    # concepto como faltante, y uno SIN ficha sí. `guardar_ficha` es idempotente (ON CONFLICT
    # DO UPDATE), así que no hace falta deshacerla al terminar.
    panel.guardar_ficha(
        conn, tratamiento="endodoncia", concepto="precio",
        contenido="$1.200.000 conducto simple", aprobado=True,
        nota_pendiente=None, usuario="prueba",
    )
    filas = {f["clave"]: f for f in panel.listar_tratamientos(conn)}

    assert filas["endodoncia"]["fichas"] == 1
    assert "precio" not in filas["endodoncia"]["faltan"]
    assert set(filas["endodoncia"]["faltan"]) == {"duracion", "profesional"}

    assert filas["protesis"]["fichas"] == 0
    assert set(filas["protesis"]["faltan"]) >= set(CORE)


@pytest.mark.neon
def test_guardar_ficha_y_su_bitacora_son_una_sola_transaccion(conn):
    panel.guardar_ficha(
        conn, tratamiento="implantes", concepto="precio",
        contenido="$1.900.000 la fase quirurgica", aprobado=True,
        nota_pendiente=None, usuario="dra.prueba",
    )
    registros = panel.historial(conn, limite=5)
    assert registros[0]["clave"] == "implantes/precio"
    assert registros[0]["usuario"] == "dra.prueba"
    assert registros[0]["valor_nuevo"].startswith("$1.900.000")


@pytest.mark.neon
def test_crear_un_tratamiento_que_ya_existe_no_lo_pisa(conn):
    with pytest.raises(ValueError, match="ya existe"):
        panel.crear_tratamiento(conn, clave="implantes", etiqueta="Otro", usuario="prueba")


@pytest.mark.neon
def test_desactivar_lo_saca_del_vocabulario_pero_no_de_la_tabla(conn):
    # Corrección 2 sobre el brief: la reactivación va en `finally`. Si una aserción falla
    # antes de llegar a ella, `bichectomia` se queda desactivado y contamina toda prueba
    # posterior que dependa del vocabulario completo de los catorce.
    try:
        panel.cambiar_tratamiento(conn, "bichectomia", activo=False, usuario="prueba")
        assert "bichectomia" not in panel.vocabulario_activo(conn)
        assert any(f["clave"] == "bichectomia" for f in panel.listar_tratamientos(conn))
    finally:
        panel.cambiar_tratamiento(conn, "bichectomia", activo=True, usuario="prueba")


# ==========================================================================================
# Endpoints y control de rol (Tarea 6) -- offline, con un doble de `usuario_actual` por
# `dependency_overrides`: no hace falta base de datos para probar quién puede tocar qué.
# ==========================================================================================


def _como(rol: str):
    return lambda: {"usuario": f"x.{rol}", "nombre": "Prueba", "rol": rol}


def test_recepcion_no_puede_editar_una_ficha():
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        c = TestClient(runtime.app)
        r = c.put("/api/conocimiento", json={
            "tratamiento": "implantes", "concepto": "precio",
            "contenido": "$1", "aprobado": True, "nota_pendiente": None,
        })
        assert r.status_code == 403
        assert "detalle" in r.json()
    finally:
        runtime.app.dependency_overrides.clear()


def test_solo_admin_crea_un_tratamiento():
    for rol, esperado in [("doctor", 403), ("recepcion", 403)]:
        runtime.app.dependency_overrides[runtime.usuario_actual] = _como(rol)
        try:
            c = TestClient(runtime.app)
            r = c.post("/api/tratamientos", json={"clave": "carillas", "etiqueta": "Carillas"})
            assert r.status_code == esperado, f"{rol} obtuvo {r.status_code}"
        finally:
            runtime.app.dependency_overrides.clear()


def test_un_error_del_servidor_llega_al_usuario_como_detalle():
    """`api.ts` lee `cuerpo?.detalle`, pero `HTTPException` serializa `detail`. Sin este
    manejador, ningún mensaje del servidor llega a la pantalla y todo se ve como un error
    genérico."""
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        c = TestClient(runtime.app)
        cuerpo = c.post("/api/tratamientos", json={"clave": "x", "etiqueta": "y"}).json()
        assert cuerpo.get("detalle")
        assert "admin" in cuerpo["detalle"].lower() or "permiso" in cuerpo["detalle"].lower()
    finally:
        runtime.app.dependency_overrides.clear()

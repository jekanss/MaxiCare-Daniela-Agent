"""Las pruebas del panel: el vocabulario de tratamientos y la edición de fichas.

Las que tocan Neon llevan `@pytest.mark.neon` y escriben en el esquema `pruebas`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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
    # concepto como faltante, y uno SIN ficha sí.
    #
    # El `finally` NO es higiene: es lo que protege el entregable de la fase 1.
    # `test_tools_neon.py::test_un_tratamiento_sin_precio_documentado_no_devuelve_una_cifra`
    # afirma que `endodoncia/precio` devuelve «SIN DATO DOCUMENTADO» --la frase que impide
    # que el modelo rellene el hueco con un precio plausible--, y corre contra ESTE mismo
    # esquema. Dejar la ficha aquí hacía que esa prueba pasara solo porque el otro archivo
    # entra con un `DROP SCHEMA ... CASCADE`: bastaba correr las dos suites por separado, o
    # reordenarlas, para que la garantía clínica dejara de estar probada. Se borra a mano y
    # no con otro `DROP SCHEMA`, que sería lento y pelearía con `test_tools_neon`.
    try:
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
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM base_conocimiento WHERE tratamiento = 'endodoncia' "
                "AND concepto = 'precio'"
            )
            cur.execute(
                "DELETE FROM cambios_configuracion WHERE tabla = 'base_conocimiento' "
                "AND clave = 'endodoncia/precio'"
            )
        conn.commit()


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
def test_la_bitacora_registra_que_algo_se_volvio_un_compromiso_comercial(conn):
    """`aprobado` es el único campo con significado comercial declarado, y no se registraba.

    El límite de concurrencia de esta pantalla --dos ediciones a la vez, gana la última-- se
    aceptó *porque* la bitácora guarda el valor anterior. Eso era cierto del texto y falso
    de la aprobación: una edición pisada que convirtiera un precio tentativo en un
    compromiso comercial no se podía deshacer ni averiguar.
    """
    try:
        panel.guardar_ficha(
            conn, tratamiento="implantes", concepto="garantia",
            contenido="Un ano sobre la corona", aprobado=False,
            nota_pendiente="Falta confirmarlo con la doctora", usuario="dra.prueba",
        )
        panel.guardar_ficha(
            conn, tratamiento="implantes", concepto="garantia",
            contenido="Un ano sobre la corona", aprobado=True,
            nota_pendiente=None, usuario="dra.prueba",
        )
        registros = panel.historial(conn, limite=10)
        aprobacion = [
            r for r in registros
            if r["clave"] == "implantes/garantia" and r["valor_nuevo"] == "aprobado"
        ]
        assert aprobacion, "aprobar una ficha no dejó rastro en la bitácora"
        assert aprobacion[0]["valor_anterior"] == "sin aprobar"
        assert aprobacion[0]["usuario"] == "dra.prueba"

        # La otra cara: guardar sin tocar el interruptor no inventa una fila de aprobación.
        # Una bitácora que anota cambios que no ocurrieron es tan inútil como la que se
        # calla los que sí.
        antes = len(panel.historial(conn, limite=100))
        panel.guardar_ficha(
            conn, tratamiento="implantes", concepto="garantia",
            contenido="Un ano sobre la corona, contado desde la instalacion",
            aprobado=True, nota_pendiente=None, usuario="dra.prueba",
        )
        despues = panel.historial(conn, limite=100)
        assert len(despues) == antes + 1
        assert despues[0]["valor_nuevo"].startswith("Un ano sobre la corona, contado")
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM base_conocimiento WHERE tratamiento = 'implantes' "
                "AND concepto = 'garantia'"
            )
            cur.execute(
                "DELETE FROM cambios_configuracion WHERE tabla = 'base_conocimiento' "
                "AND clave = 'implantes/garantia'"
            )
        conn.commit()


@pytest.mark.neon
def test_aprobar_una_ficha_borra_la_nota_de_lo_que_faltaba(conn):
    """La pantalla esconde el campo al aprobar, pero escondido no es vaciado: seguía
    enviando lo que hubiera dentro. Quedaba una ficha aprobada con un «falta por definir»
    invisible, listo para reaparecer el día que alguien la desapruebe meses después."""
    try:
        guardada = panel.guardar_ficha(
            conn, tratamiento="implantes", concepto="garantia",
            contenido="Un ano sobre la corona", aprobado=True,
            nota_pendiente="Falta confirmarlo con la doctora", usuario="dra.prueba",
        )
        assert guardada["nota_pendiente"] is None
        fichas = {(f["tratamiento"], f["concepto"]): f for f in panel.listar_conocimiento(conn)}
        assert fichas[("implantes", "garantia")]["nota_pendiente"] is None

        # Y la otra cara: sin aprobar, la nota es justo lo que hay que conservar.
        panel.guardar_ficha(
            conn, tratamiento="implantes", concepto="garantia",
            contenido="Un ano sobre la corona", aprobado=False,
            nota_pendiente="Falta confirmarlo con la doctora", usuario="dra.prueba",
        )
        fichas = {(f["tratamiento"], f["concepto"]): f for f in panel.listar_conocimiento(conn)}
        assert fichas[("implantes", "garantia")]["nota_pendiente"] == (
            "Falta confirmarlo con la doctora"
        )
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM base_conocimiento WHERE tratamiento = 'implantes' "
                "AND concepto = 'garantia'"
            )
            cur.execute(
                "DELETE FROM cambios_configuracion WHERE tabla = 'base_conocimiento' "
                "AND clave = 'implantes/garantia'"
            )
        conn.commit()


@pytest.mark.neon
def test_una_ficha_no_puede_colgar_de_un_tratamiento_que_no_existe(conn):
    """`base_conocimiento.tratamiento` es un TEXT sin clave foránea: era la única escritura
    del panel sin control de vocabulario, y ahí cabe una frase clínica entera. La fila que
    resultaba no se ve en ninguna pantalla y no se puede borrar desde el producto."""
    with pytest.raises(ValueError, match="no es un tratamiento"):
        panel.guardar_ficha(
            conn, tratamiento="se observa lesion periapical en el 46", concepto="precio",
            contenido="$1", aprobado=True, nota_pendiente=None, usuario="prueba",
        )
    conn.rollback()


@pytest.mark.neon
def test_lo_que_si_admite_una_ficha_general_y_un_tratamiento_desactivado(conn):
    """Las dos caras del filtro de arriba, y las dos que romperían el producto si faltaran.

    `_general` --los hechos de la clínica, la pestaña «La clínica» y sus 12 fichas-- NO es
    una fila de `tratamientos`, y un tratamiento desactivado sigue teniendo un precio viejo
    que alguien puede necesitar corregir.
    """
    try:
        panel.guardar_ficha(
            conn, tratamiento=panel.GENERAL, concepto="horario",
            contenido="Lunes a viernes de 8 a 6", aprobado=True,
            nota_pendiente=None, usuario="prueba",
        )
        panel.cambiar_tratamiento(conn, "bichectomia", activo=False, usuario="prueba")
        assert "bichectomia" not in panel.vocabulario_activo(conn)
        panel.guardar_ficha(
            conn, tratamiento="bichectomia", concepto="precio",
            contenido="$2.800.000", aprobado=True, nota_pendiente=None, usuario="prueba",
        )
    finally:
        panel.cambiar_tratamiento(conn, "bichectomia", activo=True, usuario="prueba")
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM base_conocimiento WHERE (tratamiento = %s AND concepto = "
                "'horario') OR (tratamiento = 'bichectomia' AND concepto = 'precio')",
                (panel.GENERAL,),
            )
            cur.execute(
                "DELETE FROM cambios_configuracion WHERE tabla = 'base_conocimiento' "
                "AND clave IN (%s, 'bichectomia/precio')",
                (f"{panel.GENERAL}/horario",),
            )
        conn.commit()


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


def test_lo_que_rechaza_pydantic_tambien_se_puede_leer():
    """`RequestValidationError` NO es una `HTTPException`, así que el manejador que traduce
    `detail` a `detalle` no la cubría: la mitad de los errores del servidor --todos los 422--
    seguían saliendo con el volcado crudo de Pydantic y `pedir()` los enseñaba como «No se
    pudo completar la operación (422)».

    Son alcanzables desde la pantalla: «Nuevo tratamiento» se habilita con que la clave no
    esté vacía, y `sugerirClave` recorta lo que sugiere, no lo que la persona escribe.
    """
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        c = TestClient(runtime.app)

        r = c.post("/api/tratamientos", json={"clave": "ca", "etiqueta": "Carillas"})
        assert r.status_code == 422
        cuerpo = r.json()
        assert "detail" not in cuerpo, "el frontend lee `detalle`, no `detail`"
        assert "string_too_short" not in cuerpo["detalle"], "eso es el volcado de Pydantic"
        assert "clave" in cuerpo["detalle"].lower()
        # La misma ayuda que da `validar_clave` cuando el que falla es el regex y no el
        # largo. Un solo error, una sola explicación, la atrape quien la atrape.
        assert panel.AYUDA_CLAVE in cuerpo["detalle"]

        r = c.put("/api/conocimiento", json={
            "tratamiento": "implantes", "concepto": "precio",
            "contenido": "", "aprobado": True, "nota_pendiente": None,
        })
        assert r.status_code == 422
        assert "vacío" in r.json()["detalle"]

        r = c.put("/api/conocimiento", json={
            "tratamiento": "implantes", "concepto": "precio",
            "contenido": "x" * 4001, "aprobado": True, "nota_pendiente": None,
        })
        assert r.status_code == 422
        assert "4000" in r.json()["detalle"]

        r = c.post("/api/tratamientos", json={"etiqueta": "Carillas"})
        assert r.status_code == 422
        assert r.json()["detalle"].lower().startswith("falta")
    finally:
        runtime.app.dependency_overrides.clear()


def test_el_general_del_frontend_y_el_del_servidor_son_el_mismo():
    """`_general` no es una fila de `tratamientos`: es donde viven los hechos de la clínica.

    La pantalla tiene su propia copia de la constante --un `.tsx` no puede importar de
    Python-- y `panel.guardar_ficha` la necesita para no rechazar las 12 fichas de la
    pestaña «La clínica». Son dos literales que se tienen que mover juntos, y esta prueba es
    lo único que lo nota si un día solo se mueve uno.
    """
    pantalla = (
        Path(__file__).resolve().parents[1] / "web" / "src" / "pantallas" / "Tratamientos.tsx"
    ).read_text(encoding="utf-8")
    encontrado = re.search(r"const GENERAL = '([^']+)'", pantalla)
    assert encontrado, "la pantalla ya no declara `const GENERAL`"
    assert encontrado.group(1) == panel.GENERAL

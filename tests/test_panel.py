"""Las pruebas del panel: el vocabulario de tratamientos y la edición de fichas.

Las que tocan Neon llevan `@pytest.mark.neon` y escriben en el esquema `pruebas`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from maxicare_daniela import atencion, contratos, herramientas, panel, persistencia, runtime
from maxicare_daniela import calendario as calendario_real
from maxicare_daniela.calendario import (
    ZONA_BOGOTA,
    Bloqueo,
    CalendarioCaido,
    CalendarioDoble,
    ErrorDeCalendario,
)
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


# ==========================================================================================
# «Sin resolver» -- GET /api/sin-resolver
# ==========================================================================================
#
# Offline: doblan `persistencia.conectar` y `persistencia.casos_recientes`, así que no tocan
# Neon. Que la ruta EXIGE sesión ya lo cubre, sin necesidad de repetirlo aquí,
# `test_ninguna_ruta_del_panel_responde_sin_sesion` de `test_web.py` -- recorre todas las
# rutas de `/api/` leyéndolas de la propia aplicación, y esta cae dentro sola.


class _ConexionFalsaSinResolver:
    """Sirve para el `with persistencia.conectar(...)` del endpoint y nada más."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_CASO = {
    "huella": "falta_dato:ortodoncia:precio",
    "tipo": "FALTA_DATO",
    "contador": 3,
    "escalo": 1,
    "primera_vez": "2026-09-01T10:00:00+00:00",
    "ultima_vez": "2026-09-10T10:00:00+00:00",
    "ejemplos": ["¿Cuánto vale la ortodoncia?"],
    "informe": {"que_paso": "x", "por_que": "y", "recomiendo": "z"},
}


def test_sin_resolver_responde_con_la_forma_pactada(monkeypatch):
    """`{"casos": [...], "es_admin": bool}`, con las ocho claves de cada caso intactas -- ni
    una de más (el teléfono no viaja) ni una de menos."""
    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
    monkeypatch.setattr(persistencia, "casos_recientes", lambda conn: [dict(_CASO)])
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        r = TestClient(runtime.app).get("/api/sin-resolver")
        assert r.status_code == 200
        cuerpo = r.json()
        assert set(cuerpo) == {"casos", "es_admin"}
        assert len(cuerpo["casos"]) == 1
        assert set(cuerpo["casos"][0]) == set(_CASO)
        assert cuerpo["casos"][0] == _CASO
    finally:
        runtime.app.dependency_overrides.clear()


def test_sin_resolver_sin_casos_no_revienta(monkeypatch):
    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
    monkeypatch.setattr(persistencia, "casos_recientes", lambda conn: [])
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        r = TestClient(runtime.app).get("/api/sin-resolver")
        assert r.status_code == 200
        assert r.json() == {"casos": [], "es_admin": True}
    finally:
        runtime.app.dependency_overrides.clear()


def test_es_admin_sale_de_quien_pregunta_no_del_caso():
    """`es_admin` lo calcula el servidor a partir del rol de la sesión -- nunca del front.

    La `huella` viaja a todos los roles --la pantalla la necesita para componer el título
    legible-- y lo que `es_admin` decide es si se MUESTRA cruda: un no-admin no puede quedar
    viendo el detalle técnico por un descuido en la pantalla."""
    for rol, esperado in [("admin", True), ("doctor", False), ("recepcion", False)]:
        runtime.app.dependency_overrides[runtime.usuario_actual] = _como(rol)
        try:
            with (
                pytest.MonkeyPatch.context() as mp,
            ):
                mp.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
                mp.setattr(persistencia, "casos_recientes", lambda conn: [])
                r = TestClient(runtime.app).get("/api/sin-resolver")
            assert r.status_code == 200, f"{rol} obtuvo {r.status_code}"
            assert r.json()["es_admin"] is esperado, f"{rol} -> es_admin debía ser {esperado}"
        finally:
            runtime.app.dependency_overrides.clear()


# ==========================================================================================
# La agenda del día y la marca de asistencia (fase 8, tarea 2)
# ==========================================================================================
#
# Las fechas de este bloque son RELATIVAS a propósito y solo las usan las pruebas de Neon.
# Una hora clavada aquí sería una hora que un día cae del lado equivocado de `now()` --el
# mismo defecto que ya dejó doce pruebas en rojo el 16/09/2026-- y lo que estas pruebas
# necesitan es justo lo contrario que las offline: que «ayer» siga siendo ayer siempre.
# Las offline de más abajo sí clavan el presente, y por eso piden el día por la URL.


def _a_las_nueve(dias: int) -> datetime:
    """Las 9:00 de Bogotá, `dias` días desde hoy. Nunca cruza la frontera de `now()`."""
    return (datetime.now(ZONA_BOGOTA) + timedelta(days=dias)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


AYER_A_LAS_NUEVE = _a_las_nueve(-1)
MANANA_A_LAS_NUEVE = _a_las_nueve(1)


def _sembrar_cita(
    conn, *, inicio: datetime, estado: str = "confirmada", asistio: bool | None = None,
    telefono: str = "573009998877",
) -> str:
    """Una cita mínima en el esquema `pruebas`, sin reserva ni evento de Calendar.

    No pasa por `persistencia.registrar_cita` porque esa exige un `reserva_id` y aquí no
    hace falta ninguno: `citas.reserva_id` es nullable y lo que se prueba es el SQL del
    panel, no el reparto de cupos.
    """
    conversacion = persistencia.asegurar_conversacion(conn, telefono=telefono)
    id_cita = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO citas (id, conversacion_id, nombre_completo, telefono, tratamiento, "
            "                   inicio, duracion_minutos, estado, asistio) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (id_cita, conversacion, "Paciente De Prueba", telefono, "valoracion", inicio,
             60, estado, asistio),
        )
    conn.commit()
    return id_cita


def _limpiar_citas(conn, telefono: str = "573009998877") -> None:
    """El esquema `pruebas` persiste entre corridas: lo que siembra una prueba lo borra ella.

    Sin esto, `citas_sin_marcar` iría acumulando las citas pasadas de todas las corridas
    anteriores y las aserciones de conteo dejarían de significar nada.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM cambios_configuracion WHERE tabla = 'citas' AND clave IN "
            "(SELECT id::text FROM citas WHERE telefono = %s)",
            (telefono,),
        )
        cur.execute("DELETE FROM citas WHERE telefono = %s", (telefono,))
        cur.execute("DELETE FROM conversaciones WHERE telefono = %s", (telefono,))
    conn.commit()


def _asistio(conn, cita_id: str) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT asistio FROM citas WHERE id = %s", (cita_id,))
        return cur.fetchone()[0]


@pytest.mark.neon
def test_la_bitacora_admite_citas_y_sigue_siendo_una_lista_cerrada(conn):
    """La 021 abrió el CHECK de `cambios_configuracion.tabla` a `citas`, y a nada más.

    La 007 lo cerró en tres valores y la marca de asistencia es el cuarto. Sin él, `_anotar`
    revienta con `CheckViolation` y --como el cambio y su registro van en la misma
    transacción-- se cae con ella el `UPDATE`: `citas.asistio` quedaría inescribible desde
    el panel. La otra cara, que es la que hace que el CHECK siga sirviendo de algo: una
    tabla inventada la sigue rechazando Postgres.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cambios_configuracion (tabla, clave, valor_nuevo, usuario) "
            "VALUES ('citas', 'x', 'asistio', 'prueba')"
        )
    conn.rollback()

    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO cambios_configuracion (tabla, clave, valor_nuevo, usuario) "
            "VALUES ('pacientes', 'x', 'y', 'prueba')"
        )
    conn.rollback()


@pytest.mark.neon
def test_marcar_asistencia_escribe_la_columna_y_su_bitacora(conn):
    try:
        cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
        panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="recepcion")
        assert _asistio(conn, cita) is True
        fila = panel.historial(conn, limite=1)[0]
        assert (fila["tabla"], fila["clave"], fila["valor_nuevo"]) == ("citas", cita, "asistio")
        assert fila["usuario"] == "recepcion"
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_las_marcas_de_asistencia_no_inundan_la_bitacora_de_la_otra_pantalla(conn):
    """La marca escribe en la MISMA tabla que alimenta «Últimos cambios» de Tratamientos.

    Esa pantalla existe --lo dice su propio texto-- para reconstruir qué decía un precio
    antes y quién lo cambió, y `historial` devuelve las 100 filas más recientes. Con quince
    citas al día, esas 100 son todas marcas de asistencia en menos de una semana y el cambio
    de precio deja de verse en la única pantalla desde la que se puede ver. Con ello se cae
    la renuncia escrita de `web/CLAUDE.md`: una edición pisada es recuperable solo mientras
    se pueda encontrar.

    Las dos mitades, y las dos importan: la fila SE ESCRIBE --es la pista de auditoría-- y
    el recorte lo pide quien llama, no la función.
    """
    try:
        panel.guardar_ficha(
            conn, tratamiento="implantes", concepto="precio",
            contenido="$1.900.000 la fase quirurgica", aprobado=True,
            nota_pendiente=None, usuario="dra.prueba",
        )
        cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
        panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="recepcion")

        completa = panel.historial(conn, limite=1)
        assert (completa[0]["tabla"], completa[0]["clave"]) == ("citas", cita), (
            "la bitácora dejó de guardar quién marcó qué: eso es la pista de auditoría"
        )

        recortada = panel.historial(
            conn, limite=100, excluir_tablas=runtime.TABLAS_FUERA_DEL_HISTORIAL
        )
        assert all(f["tabla"] != "citas" for f in recortada)
        # Y el recorte es de UNA tabla, no de la bitácora: el cambio de precio sigue ahí.
        assert recortada[0]["clave"] == "implantes/precio"
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_corregir_una_marca_deja_el_valor_anterior_en_la_bitacora(conn):
    try:
        cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
        panel.marcar_asistencia(conn, cita_id=cita, valor=False, usuario="recepcion")
        panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="admin")
        fila = panel.historial(conn, limite=1)[0]
        assert (fila["valor_anterior"], fila["valor_nuevo"]) == ("no_asistio", "asistio")
        assert fila["usuario"] == "admin"
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_desmarcar_es_un_valor_y_no_un_campo_ausente(conn):
    """`null` vuelve la cita a «sin marcar», y eso también se registra.

    Quien se equivoca al marcar tiene que poder dejar la cita como estaba; sin este camino,
    un clic mal dado convertía un dato desconocido en un «no asistió» permanente.
    """
    try:
        cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
        panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="recepcion")
        panel.marcar_asistencia(conn, cita_id=cita, valor=None, usuario="recepcion")
        assert _asistio(conn, cita) is None
        fila = panel.historial(conn, limite=1)[0]
        assert (fila["valor_anterior"], fila["valor_nuevo"]) == ("asistio", "sin_marcar")
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_una_cita_que_todavia_no_ha_ocurrido_no_se_puede_marcar(conn):
    try:
        cita = _sembrar_cita(conn, inicio=MANANA_A_LAS_NUEVE)
        with pytest.raises(ValueError, match="todavía no"):
            panel.marcar_asistencia(conn, cita_id=cita, valor=False, usuario="recepcion")
        # La otra mitad: no basta con que lance. Si además hubiera escrito, el error sería
        # decorativo -- ver «verde por el motivo equivocado» en `.claude/rules/pruebas.md`.
        assert _asistio(conn, cita) is None
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_una_cita_cancelada_no_tiene_asistencia_que_marcar(conn):
    try:
        cita = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE, estado="cancelada")
        with pytest.raises(ValueError, match="cancelada"):
            panel.marcar_asistencia(conn, cita_id=cita, valor=True, usuario="recepcion")
        assert _asistio(conn, cita) is None
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_marcar_una_cita_que_no_existe_no_revienta_la_conexion(conn):
    """Un UUID que no está, y una cadena que ni siquiera es un UUID.

    La segunda importa: el id llega por la URL, y sin la comprobación previa Postgres
    responde `invalid input syntax for type uuid`, que sale como un 500 y deja la
    transacción abortada para todo lo que venga después en la misma conexión.
    """
    for inventado in (str(uuid.uuid4()), "no-soy-un-uuid"):
        with pytest.raises(panel.CitaInexistente, match="no existe"):
            panel.marcar_asistencia(conn, cita_id=inventado, valor=True, usuario="recepcion")
    # La conexión sigue sirviendo: si la de arriba hubiera llegado a Postgres con basura,
    # esto lanzaría `InFailedSqlTransaction`.
    assert panel.vocabulario_activo(conn)


@pytest.mark.neon
def test_el_dia_trae_las_citas_vivas_en_orden_y_no_las_canceladas(conn):
    try:
        dia = _a_las_nueve(30)
        desde = dia.replace(hour=0)
        hasta = desde + timedelta(days=1)

        tarde = _sembrar_cita(conn, inicio=dia.replace(hour=15))
        manana = _sembrar_cita(conn, inicio=dia.replace(hour=9))
        cancelada = _sembrar_cita(conn, inicio=dia.replace(hour=11), estado="cancelada")
        otro_dia = _sembrar_cita(conn, inicio=dia.replace(hour=9) + timedelta(days=1))

        filas = panel.citas_del_dia(conn, desde=desde, hasta=hasta)
        ids = [f["id"] for f in filas]

        assert ids == [manana, tarde], "el orden es por `inicio`, y solo las de ese día"
        assert cancelada not in ids
        assert otro_dia not in ids
        assert filas[0]["asistio"] is None
        assert filas[0]["tratamiento"] == "valoracion"
        assert filas[0]["telefono"] == "573009998877"
    finally:
        _limpiar_citas(conn)


@pytest.mark.neon
def test_sin_marcar_no_devuelve_las_ya_marcadas(conn):
    try:
        desde = AYER_A_LAS_NUEVE.replace(hour=0) - timedelta(days=6)
        hasta = AYER_A_LAS_NUEVE.replace(hour=0) + timedelta(days=1)

        pendiente = _sembrar_cita(conn, inicio=AYER_A_LAS_NUEVE)
        marcada = _sembrar_cita(conn, inicio=_a_las_nueve(-2))
        futura = _sembrar_cita(conn, inicio=MANANA_A_LAS_NUEVE)
        panel.marcar_asistencia(conn, cita_id=marcada, valor=True, usuario="recepcion")

        ids = [f["id"] for f in panel.citas_sin_marcar(conn, desde=desde, hasta=hasta)]

        # Las dos caras: la que sigue sin marcar está, la que ya se marcó no. Sin la
        # primera, un SELECT que no devolviera nunca nada también pasaría.
        assert pendiente in ids
        assert marcada not in ids
        # Y una cita que todavía no ha ocurrido no puede estar «sin marcar»: no hay nada
        # que marcar. Está fuera del rango además, que es el segundo cinturón.
        assert futura not in ids
    finally:
        _limpiar_citas(conn)


# ------------------------------------------------------------------------------------------
# Los dos endpoints -- offline, con el presente clavado y sin tocar Neon
# ------------------------------------------------------------------------------------------


#: El presente clavado de este bloque, y `_DIA` sale de él en vez de al revés. Lo pone
#: `_sin_base` sobre `runtime._ahora_en_bogota`; el porqué está en su docstring.
_AHORA = datetime(2026, 9, 20, 12, 0, tzinfo=ZONA_BOGOTA)
_DIA = _AHORA.date().isoformat()

#: Un día que sigue dentro de la ventana de reconciliación, y otro que ya no. Se derivan de
#: la constante de `herramientas`, no de un número escrito a mano: el día que alguien la suba
#: a 3 o la baje a 1, estas dos pruebas siguen midiendo la frontera y no un número viejo.
_DIA_DENTRO = _AHORA.date() - timedelta(days=herramientas.DIAS_HACIA_ATRAS_AL_SINCRONIZAR)
_DIA_FUERA = _AHORA.date() - timedelta(days=herramientas.DIAS_HACIA_ATRAS_AL_SINCRONIZAR + 1)


def _ese_dia_a_las_nueve(dia: date) -> datetime:
    """Las 09:00 de ese día en Bogotá, para que la cita sembrada caiga DENTRO del día que se
    pide: si cayera fuera, el filtro de `api_agenda` la descartaría y la prueba mediría el
    filtro en vez de la cota.

    El nombre largo es deliberado: `_a_las_nueve` ya existe arriba y recibe un DESPLAZAMIENTO
    en días contra el reloj de verdad (`:538`), que es lo que necesitan las pruebas de Neon.
    Llamar igual a las dos las hace intercambiables a la vista y no lo son --y como esas
    viven bajo `-m neon`, la colisión pasaba entera por `uv run pytest -q`--.
    """
    return datetime(dia.year, dia.month, dia.day, 9, 0, tzinfo=ZONA_BOGOTA)

_CITA_DE_AGENDA = {
    "id": "11111111-2222-3333-4444-555555555555",
    "conversacion_id": "66666666-7777-8888-9999-000000000000",
    "telefono": "573001112233",
    "nombre_completo": "Paciente De Prueba",
    "tratamiento": "valoracion",
    "inicio": datetime(2026, 9, 20, 9, 0, tzinfo=ZONA_BOGOTA),
    "duracion_minutos": 60,
    "estado": "confirmada",
    "asistio": None,
    # Con evento de Calendar, y no `None`, a propósito. `reconciliar_con_calendar` descarta
    # en su primera línea toda cita sin `evento_calendar_id` --no hay contra qué
    # contrastarla-- así que una cita sin él nunca llega al camino de cancelación: cualquier
    # prueba que afirme «no se canceló nada» pasaría con y sin la guarda que lo impide, que
    # es el «verde por el motivo equivocado» de `.claude/rules/pruebas.md`.
    "evento_calendar_id": "evt_de_prueba_0001",
    "reserva_id": None,
}


class _CalendarioDeLaAgenda:
    """Un calendario que no es ni `CalendarioCaido` ni `CalendarioDoble`, y por eso SÍ pasa.

    Existe para poder ejercitar la rama «hay calendario de verdad» sin hablar con Google.
    `_calendario_de_la_agenda` decide por `isinstance`, así que una clase cualquiera basta.
    """

    def __init__(self, bloqueos=()):
        self._bloqueos = list(bloqueos)
        self.preguntaron_por = []

    def bloqueos(self, desde, hasta):
        self.preguntaron_por.append((desde, hasta))
        return list(self._bloqueos)


def _sin_base(monkeypatch, *, citas=None, sin_marcar=None):
    """Deja el endpoint de la agenda sin base de datos. El calendario lo pone cada prueba.

    **Y clava el presente**, que desde la cota de `_fuera_de_la_ventana` es tan necesario
    como los dobles de la base. El endpoint compara `_DIA` contra hoy: con el reloj suelto,
    tres días después de escribir esto ese mismo `_DIA` cae fuera de la ventana y las pruebas
    de más abajo dejarían de ejercitar la rama que creen ejercitar --sin fallar, que es lo
    peor--. Es la trampa de `.claude/rules/pruebas.md`, que ya se cobró tres veces: las
    pruebas offline clavan el presente, los scripts cuentan hacia delante.
    """
    monkeypatch.setattr(runtime, "_ahora_en_bogota", lambda: _AHORA)
    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
    monkeypatch.setattr(
        persistencia, "leer_configuracion", lambda conn: dict(persistencia.CONFIGURACION_POR_DEFECTO)
    )
    monkeypatch.setattr(
        panel, "citas_del_dia",
        lambda conn, **kw: [dict(c) for c in (citas if citas is not None else [_CITA_DE_AGENDA])],
    )
    monkeypatch.setattr(
        panel, "citas_sin_marcar", lambda conn, **kw: [dict(c) for c in (sin_marcar or [])]
    )


def _nadie_reconcilia(monkeypatch):
    """Hace que reconciliar sea un fallo de la prueba, no un no-op silencioso.

    Sin esto, quitar `CalendarioCaido` del `isinstance` de `_calendario_de_la_agenda` no
    rompía nada: la reconciliación corría contra el calendario caído, se tragaba cada
    `ErrorDeCalendario`, y el `except` de `bloqueos` acababa poniendo `calendario_disponible`
    en `False` igual. Mismo cuerpo de respuesta, guarda muerta y suite en verde.
    """
    async def jamas(**kw):
        raise AssertionError("no se puede reconciliar sin un calendario de verdad")

    monkeypatch.setattr(herramientas, "reconciliar_con_calendar", jamas)


def test_sin_calendario_la_agenda_responde_igual(monkeypatch):
    """Google caído no deja a la clínica sin ver su día. Lo dice, y sigue.

    `calendario_disponible: False` es lo que la pantalla necesita para no pintar una agenda
    sin bloqueos como si fuera una agenda libre. Y no se reconcilia NADA: contrastar contra
    un calendario que lanza en cada llamada solo sirve para tragarse errores.

    **Y el motivo tiene que ser `no_disponible`, no el otro.** Es la mitad que la pantalla usa
    para decidir el TONO: esta es la avería --lo que se ve puede estar desfasado y eso pone a
    un paciente en la hora equivocada-- y va en rojo. Si alguien la etiquetara como el día
    viejo, la única alarma que tiene esta pantalla se pintaría como rutina gris.
    """
    _sin_base(monkeypatch)
    _nadie_reconcilia(monkeypatch)
    monkeypatch.setattr(runtime, "_calendario", CalendarioCaido(motivo="sin credenciales"))
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        r = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}")
        assert r.status_code == 200
        cuerpo = r.json()
        assert set(cuerpo) == {
            "dia", "citas", "bloqueos", "correcciones", "sin_marcar", "calendario_disponible",
            "motivo_sin_calendario",
        }
        assert cuerpo["calendario_disponible"] is False
        assert cuerpo["motivo_sin_calendario"] == runtime.MOTIVO_CALENDARIO_NO_DISPONIBLE
        assert cuerpo["dia"] == _DIA
        assert cuerpo["bloqueos"] == [] and cuerpo["correcciones"] == []
        # Y las citas salen igual: sin calendario no hay reconciliación, pero el día sí está.
        assert len(cuerpo["citas"]) == 1
        cita = cuerpo["citas"][0]
        assert set(cita) >= {
            "id", "telefono", "nombre_completo", "tratamiento", "inicio", "duracion_minutos",
            "estado", "asistio",
        }
        assert cita["inicio"].startswith("2026-09-20T09:00:00")
        assert cita["asistio"] is None
    finally:
        runtime.app.dependency_overrides.clear()


def test_la_agenda_no_construye_un_calendario_por_peticion(monkeypatch):
    """Reutiliza el del proceso. `CalendarioGoogle.__init__` habla con Google DE VERDAD.

    Construir uno por petición congela el bucle de eventos durante ese viaje: con un paciente
    escribiendo a la vez, se retrasan su respuesta, la ventana del búfer de `atencion.py` y
    el webhook de Telegram. Y encima duplicaría la degradación doble -> caído que
    `_construir_el_calendario` ya hace una sola vez al arrancar.

    **Lo que vigila son las tres puertas por las que se construye uno**, y no el cuerpo de la
    respuesta. Esta prueba doblaba `atencion._calendario_por_defecto` --que la agenda dejó de
    llamar en `7dddfdf`-- y afirmaba `calendario_disponible is False` sobre `_calendario =
    None`. Con esa pareja, reintroducir la construcción por petición hacía dos cosas, y
    ninguna era fallar por el motivo correcto:

    - **En un entorno SIN credenciales, la prueba quedaba en verde.**
      `calendario_desde_config` devuelve ahí un `CalendarioDoble` que
      `_calendario_de_la_agenda` rechaza igual, así que la respuesta sale idéntica. Ese es el
      entorno de cualquiera que clone el repositorio.
    - **En uno CON credenciales --esta máquina--, la suite offline se ponía a hablar con
      Google de verdad.** Lo que la delataba era `_nadie_reconcilia`, o sea otra guarda, y
      después de un viaje de red que una prueba offline no debe hacer nunca.

    En los dos casos la prueba no vigilaba lo que su nombre promete. Ahora sí: las tres
    puertas revientan si alguien las llama, y la aserción del cuerpo se queda como control de
    que el camino llegó hasta el final en vez de morirse antes (`.claude/rules/pruebas.md`,
    «verde por el motivo equivocado»).
    """
    _sin_base(monkeypatch)
    _nadie_reconcilia(monkeypatch)

    def revienta(*args, **kw):
        raise AssertionError("la agenda no puede construir un calendario en cada petición")

    monkeypatch.setattr(runtime, "calendario_desde_config", revienta)
    monkeypatch.setattr(calendario_real, "CalendarioGoogle", revienta)
    monkeypatch.setattr(atencion, "_calendario_por_defecto", revienta)
    monkeypatch.setattr(runtime, "_calendario", None)  # el arranque todavía no corrió
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        r = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}")
        assert r.status_code == 200
        assert r.json()["calendario_disponible"] is False
    finally:
        runtime.app.dependency_overrides.clear()


def test_un_calendario_doble_no_cuenta_como_calendario(monkeypatch):
    """El no negociable 1, mirado desde la agenda.

    Un `CalendarioDoble` está VACÍO y dice que sí a todo. Pasárselo a la reconciliación es
    decirle que ninguna cita del día existe ya en Calendar: las cancelaría TODAS, soltaría
    sus cupos y la pantalla diría que el calendario está disponible.

    La reconciliación corre aquí DE VERDAD --no está doblada-- sobre una cita que sí tiene
    `evento_calendar_id`, así que sin la guarda el camino llega hasta
    `persistencia.marcar_cita_cancelada`. Eso es lo que la prueba vigila: no que la cita
    siga en la lista, sino que nadie haya intentado cancelarla.
    """
    _sin_base(monkeypatch)
    cancelaciones: list[str] = []
    monkeypatch.setattr(
        persistencia, "marcar_cita_cancelada",
        lambda conn, id_cita, motivo=None: cancelaciones.append(str(id_cita)),
    )
    monkeypatch.setattr(runtime, "_calendario", CalendarioDoble())
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        r = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}")
        assert r.status_code == 200
        assert r.json()["calendario_disponible"] is False
        assert cancelaciones == [], "el doble vacío estuvo a punto de cancelar el día entero"
        assert len(r.json()["citas"]) == 1, "la cita del día sigue ahí, sin cancelar"
    finally:
        runtime.app.dependency_overrides.clear()


def test_con_calendario_las_correcciones_y_los_bloqueos_salen_enteros(monkeypatch):
    """La rama que SÍ tiene calendario, que es la que la pantalla consume de verdad.

    Las dos formas más sensibles del contrato --`Correccion` y `Bloqueo`-- solo se
    ejercitaban vacías, así que un renombre de campo en cualquiera de las dos salía como un
    500 en la clínica con la suite entera en verde.

    Van las dos clases de corrección: `movida` lleva `hora_nueva`, `cancelada` la lleva en
    `null`, y eso es lo ÚNICO que las distingue al pintarlas.
    """
    _sin_base(monkeypatch)

    bloqueo = Bloqueo(
        inicio=datetime(2026, 9, 20, 12, 0, tzinfo=ZONA_BOGOTA),
        fin=datetime(2026, 9, 20, 13, 0, tzinfo=ZONA_BOGOTA),
        titulo="Almuerzo",
    )
    calendario = _CalendarioDeLaAgenda([bloqueo])
    monkeypatch.setattr(runtime, "_calendario", calendario)

    movida = herramientas.Correccion(
        cita_id="aaaaaaaa-0000-0000-0000-000000000001",
        que_paso="movida",
        hora_vieja=datetime(2026, 9, 20, 9, 0, tzinfo=ZONA_BOGOTA),
        hora_nueva=datetime(2026, 9, 20, 16, 0, tzinfo=ZONA_BOGOTA),
        tratamiento="ortodoncia",
        nombre_completo="Ana Ruiz",
    )
    cancelada = herramientas.Correccion(
        cita_id="aaaaaaaa-0000-0000-0000-000000000002",
        que_paso="cancelada",
        hora_vieja=datetime(2026, 9, 20, 10, 0, tzinfo=ZONA_BOGOTA),
        hora_nueva=None,
        tratamiento="valoracion",
        # Una cita que abrió el relevo para un número sin ficha. Llega hasta aquí.
        nombre_completo=persistencia.NOMBRE_PENDIENTE,
    )

    async def reconciliar(**kw):
        return kw["citas"], [movida, cancelada]

    monkeypatch.setattr(herramientas, "reconciliar_con_calendar", reconciliar)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        cuerpo = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}").json()

        assert cuerpo["calendario_disponible"] is True
        assert cuerpo["bloqueos"] == [{
            "inicio": "2026-09-20T12:00:00-05:00",
            "fin": "2026-09-20T13:00:00-05:00",
            "titulo": "Almuerzo",
        }]
        # Y se le preguntó por el día que se pidió, no por otro.
        assert calendario.preguntaron_por[0][0].isoformat() == "2026-09-20T00:00:00-05:00"

        una, dos = cuerpo["correcciones"]
        assert set(una) == {
            "cita_id", "que_paso", "hora_vieja", "hora_nueva", "tratamiento", "nombre_completo"
        }
        assert una["que_paso"] == "movida"
        assert una["hora_vieja"] == "2026-09-20T09:00:00-05:00"
        assert una["hora_nueva"] == "2026-09-20T16:00:00-05:00"
        assert una["nombre_completo"] == "Ana Ruiz"

        assert dos["que_paso"] == "cancelada"
        assert dos["hora_nueva"] is None
        assert dos["nombre_completo"] == runtime.SIN_NOMBRE
    finally:
        runtime.app.dependency_overrides.clear()


def test_si_los_bloqueos_fallan_el_motivo_dice_que_es_una_averia(monkeypatch):
    """La tercera puerta hasta `calendario_disponible: False`, y la única sin prueba propia.

    La reconciliación funcionó y los bloqueos no: el día se pinta sin las franjas que el
    doctor tiene apartadas, así que enseña como libres horas que no lo están. Eso es una
    AVERÍA --la misma mentira del `CalendarioDoble` por la otra puerta-- y tiene que salir por
    la franja roja, no por la gris de «este día ya es antiguo».

    Sin esta prueba, olvidar el motivo en ese `except` deja la respuesta diciendo
    `calendario_disponible: False` con `motivo_sin_calendario: null`: rompe la invariante
    «`null` si y solo si disponible» sin que nada falle, y la pantalla se queda a merced de su
    propio `else`.
    """
    class _SinBloqueos(_CalendarioDeLaAgenda):
        def bloqueos(self, desde, hasta):
            raise ErrorDeCalendario("Google no contestó")

    _sin_base(monkeypatch)
    monkeypatch.setattr(runtime, "_calendario", _SinBloqueos())

    async def reconciliar(**kw):
        return kw["citas"], []

    monkeypatch.setattr(herramientas, "reconciliar_con_calendar", reconciliar)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        cuerpo = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}").json()
        assert cuerpo["calendario_disponible"] is False
        assert cuerpo["motivo_sin_calendario"] == runtime.MOTIVO_CALENDARIO_NO_DISPONIBLE
        # Y el día se sigue pintando: lo que se pierde es el contraste, no la agenda.
        assert len(cuerpo["citas"]) == 1 and cuerpo["bloqueos"] == []
    finally:
        runtime.app.dependency_overrides.clear()


def test_una_cita_que_se_fueron_a_otro_dia_sale_de_la_lista_del_dia(monkeypatch):
    """Solo queda en `correcciones`, con `hora_nueva` diciendo a dónde se fue.

    Pintarla en un día que ya no es el suyo sería repetir el error que la reconciliación
    existe para arreglar: enseñar una hora que la clínica ya cambió.
    """
    _sin_base(monkeypatch)
    monkeypatch.setattr(runtime, "_calendario", _CalendarioDeLaAgenda())

    async def reconciliar(**kw):
        mudada = dict(kw["citas"][0])
        mudada["inicio"] = datetime(2026, 9, 21, 9, 0, tzinfo=ZONA_BOGOTA)
        return [mudada], [herramientas.Correccion(
            cita_id=mudada["id"], que_paso="movida",
            hora_vieja=datetime(2026, 9, 20, 9, 0, tzinfo=ZONA_BOGOTA),
            hora_nueva=mudada["inicio"], tratamiento="valoracion",
            nombre_completo="Paciente De Prueba",
        )]

    monkeypatch.setattr(herramientas, "reconciliar_con_calendar", reconciliar)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        cuerpo = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}").json()
        assert cuerpo["citas"] == []
        assert len(cuerpo["correcciones"]) == 1
        assert cuerpo["correcciones"][0]["hora_nueva"].startswith("2026-09-21")
    finally:
        runtime.app.dependency_overrides.clear()


def test_un_dia_dentro_de_la_ventana_si_se_reconcilia(monkeypatch):
    """La mitad que la cota NO puede romper: el día de anteayer sigue contrastándose.

    Es el borde exacto de `herramientas.DIAS_HACIA_ATRAS_AL_SINCRONIZAR` --el último día que
    entra-- y está aquí para que nadie cierre la ventana de más. Sin esta prueba, un `<=` por
    un `<` dejaría a la agenda sin reconciliar el día que Daniela sí reconcilia, y el panel
    contaría una historia distinta de la que le cuenta al paciente.
    """
    cita = dict(_CITA_DE_AGENDA, inicio=_ese_dia_a_las_nueve(_DIA_DENTRO))
    _sin_base(monkeypatch, citas=[cita])
    monkeypatch.setattr(runtime, "_calendario", _CalendarioDeLaAgenda())

    reconciliados: list[str] = []

    async def reconciliar(**kw):
        reconciliados.append(kw["ahora"].isoformat())
        return kw["citas"], []

    monkeypatch.setattr(herramientas, "reconciliar_con_calendar", reconciliar)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        r = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA_DENTRO.isoformat()}")
        assert r.status_code == 200
        cuerpo = r.json()
        assert reconciliados, "el día de anteayer dejó de contrastarse contra Calendar"
        assert cuerpo["calendario_disponible"] is True
        # La otra mitad de la invariante: `null` si y solo si el día SÍ se contrastó. Sin
        # esto, un motivo puesto de más pintaría un aviso sobre un día que no tiene nada.
        assert cuerpo["motivo_sin_calendario"] is None
        assert len(cuerpo["citas"]) == 1
    finally:
        runtime.app.dependency_overrides.clear()


def test_un_dia_mas_viejo_que_la_ventana_no_se_reconcilia_ni_toca_la_base(monkeypatch):
    """La cota, mirada desde el daño que evita.

    Mirar un día viejo NO puede escribir en Neon. Sin esta cota, abrir en la Agenda un día
    cuyos eventos el doctor ya limpió de su Calendar cancelaba esas citas, soltaba sus cupos
    y las dejaba **inmarcables para siempre**: el PATCH responde 400 sobre una cita
    `cancelada`, y con ella se va la métrica de asistencia, que es el entregable de la fase.
    No quedaba ni fila en `cambios_configuracion` ni error en ningún log.

    Los tres dobles son la prueba entera, y ninguno es adorno:
    `reconciliar_con_calendar` revienta --es lo ÚNICO que escribe por este camino--,
    `_calendario_de_la_agenda` revienta --el día viejo no llega ni a pedir calendario, que es
    lo que lo vuelve instantáneo-- y `marcar_cita_cancelada` revienta por si algún día
    apareciera un tercer camino hasta la cancelación.

    **Y el motivo tiene que ser `fuera_de_ventana`, que es lo que le permite a la pantalla no
    mentir.** Con el booleano a secas, este día --el caso NORMAL: `DIAS_SIN_MARCAR` es 7 y la
    ventana 2, así que cinco de los siete días a los que lleva «Ver ese día» caen aquí-- salía
    con la franja roja «No se pudo consultar Google Calendar», que es falso: no falló nada. Y
    el daño no era el rótulo, era gastar a diario la ÚNICA señal que avisa de que se está
    mirando Neon sin contrastar, que es la que sí tiene consecuencia clínica.
    """
    def revienta(*args, **kw):
        raise AssertionError("un día fuera de la ventana no puede llegar hasta aquí")

    cita = dict(_CITA_DE_AGENDA, inicio=_ese_dia_a_las_nueve(_DIA_FUERA))
    _sin_base(monkeypatch, citas=[cita])
    _nadie_reconcilia(monkeypatch)
    monkeypatch.setattr(runtime, "_calendario_de_la_agenda", revienta)
    monkeypatch.setattr(persistencia, "marcar_cita_cancelada", revienta)
    # Y con un calendario de VERDAD puesto, para que lo que corte sea la cota y no la falta
    # de calendario: sin esta línea la prueba pasaría igual con la cota borrada.
    monkeypatch.setattr(runtime, "_calendario", _CalendarioDeLaAgenda())
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        r = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA_FUERA.isoformat()}")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["calendario_disponible"] is False
        assert cuerpo["motivo_sin_calendario"] == runtime.MOTIVO_FUERA_DE_VENTANA
        assert cuerpo["correcciones"] == [] and cuerpo["bloqueos"] == []
        # El día se pinta igual: lo que se pierde es el contraste, no la agenda.
        assert len(cuerpo["citas"]) == 1
        assert cuerpo["citas"][0]["asistio"] is None
    finally:
        runtime.app.dependency_overrides.clear()


def test_el_literal_pendiente_no_llega_nunca_a_la_pantalla(monkeypatch):
    """`relevo.crear_cita_del_relevo` escribe `PENDIENTE` en `citas.nombre_completo`.

    Pasa cuando el doctor agenda desde el hilo de Telegram para un número que todavía no
    tiene ficha. La agenda mostraría, tal cual, un bloque de las 9:00 a nombre de
    «PENDIENTE» --que se lee como el nombre de una persona-- y la pantalla lo pinta crudo.
    La regla de esta fase: lo que no se sabe no se pinta. El teléfono sí viaja, así que la
    clínica sigue sabiendo a quién llamar.
    """
    del_relevo = dict(_CITA_DE_AGENDA, nombre_completo=persistencia.NOMBRE_PENDIENTE)
    sin_nada = dict(_CITA_DE_AGENDA, id="99999999-0000-0000-0000-000000000000",
                    nombre_completo="   ")
    _sin_base(monkeypatch, citas=[del_relevo], sin_marcar=[sin_nada])
    _nadie_reconcilia(monkeypatch)
    monkeypatch.setattr(runtime, "_calendario", None)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        cuerpo = TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}").json()
        assert cuerpo["citas"][0]["nombre_completo"] == runtime.SIN_NOMBRE
        assert cuerpo["citas"][0]["telefono"] == "573001112233"
        # Y la misma regla en `sin_marcar`, que es la otra lista de la misma pantalla.
        assert cuerpo["sin_marcar"][0]["nombre_completo"] == runtime.SIN_NOMBRE
        assert "PENDIENTE" not in TestClient(runtime.app).get(f"/api/agenda?dia={_DIA}").text
    finally:
        runtime.app.dependency_overrides.clear()


def test_un_dia_que_no_es_un_dia_no_llega_a_la_base(monkeypatch):
    """Un `dia` ilegible se rechaza antes de abrir una conexión, no con un 500 de psycopg.

    Los dos dobles, y ninguno es adorno: el `dia` se valida antes de tocar Neon **y** antes
    de tocar Google. Esta prueba tenía aquí un doble de `atencion._calendario_por_defecto`,
    que la agenda dejó de llamar cuando pasó a reutilizar el calendario del proceso: seguía
    en verde sin vigilar nada. El que de verdad cubre esa mitad hoy es
    `runtime._calendario_de_la_agenda`, que es lo que el endpoint llama.
    """
    def revienta(*args, **kw):
        raise AssertionError("no se debía llegar hasta aquí")

    monkeypatch.setattr(persistencia, "conectar", revienta)
    monkeypatch.setattr(runtime, "_calendario_de_la_agenda", revienta)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        r = TestClient(runtime.app).get("/api/agenda?dia=el-martes")
        assert r.status_code == 422
        assert "AAAA-MM-DD" in r.json()["detalle"]
    finally:
        runtime.app.dependency_overrides.clear()


def _marca_falsa(monkeypatch) -> list[dict]:
    """Doble de `panel.marcar_asistencia` que anota con qué lo llamaron."""
    llamadas: list[dict] = []

    def falsa(conn, *, cita_id, valor, usuario):
        llamadas.append({"cita_id": cita_id, "valor": valor, "usuario": usuario})
        return dict(_CITA_DE_AGENDA, asistio=valor)

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
    monkeypatch.setattr(panel, "marcar_asistencia", falsa)
    return llamadas


def test_recepcion_puede_marcar_asistencia(monkeypatch):
    llamadas = _marca_falsa(monkeypatch)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        c = TestClient(runtime.app)
        r = c.patch(f"/api/agenda/citas/{_CITA_DE_AGENDA['id']}", json={"asistio": True})
        assert r.status_code == 200
        # Devuelve la cita YA actualizada, con la forma de una entrada de `citas_del_dia`:
        # la pantalla repinta esa fila sin recargar el día entero.
        assert r.json()["asistio"] is True
        assert r.json()["id"] == _CITA_DE_AGENDA["id"]
        assert r.json()["inicio"].startswith("2026-09-20T09:00:00")
        assert llamadas == [{
            "cita_id": _CITA_DE_AGENDA["id"], "valor": True, "usuario": "x.recepcion",
        }]
    finally:
        runtime.app.dependency_overrides.clear()


def test_null_es_desmarcar_y_no_un_campo_ausente(monkeypatch):
    """`asistio: null` tiene que llegar como `None`, y omitir el campo tiene que ser un 422.

    Son dos cosas distintas y el modelo las distingue a propósito: `Field(...)` lo declara
    obligatorio y anulable. Con un default, un cuerpo mal formado desmarcaría la cita.
    """
    llamadas = _marca_falsa(monkeypatch)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        c = TestClient(runtime.app)
        r = c.patch(f"/api/agenda/citas/{_CITA_DE_AGENDA['id']}", json={"asistio": None})
        assert r.status_code == 200
        assert r.json()["asistio"] is None
        assert llamadas[-1]["valor"] is None

        r = c.patch(f"/api/agenda/citas/{_CITA_DE_AGENDA['id']}", json={})
        assert r.status_code == 422
        assert "marca de asistencia" in r.json()["detalle"]
        assert len(llamadas) == 1, "un cuerpo sin el campo no puede llegar a la base"
    finally:
        runtime.app.dependency_overrides.clear()


def test_un_rol_inventado_no_puede_marcar(monkeypatch):
    """Los tres roles del panel pueden marcar; cualquier otro, no. El botón se esconde en
    la pantalla por comodidad, pero el control vive en `exigir_rol`."""
    llamadas = _marca_falsa(monkeypatch)
    for rol, esperado in [("admin", 200), ("doctor", 200), ("recepcion", 200),
                          ("mercadeo", 403)]:
        runtime.app.dependency_overrides[runtime.usuario_actual] = _como(rol)
        try:
            r = TestClient(runtime.app).patch(
                f"/api/agenda/citas/{_CITA_DE_AGENDA['id']}", json={"asistio": False}
            )
            assert r.status_code == esperado, f"{rol} obtuvo {r.status_code}"
        finally:
            runtime.app.dependency_overrides.clear()
    assert len(llamadas) == 3, "el rol inventado no llegó a la base"


def test_una_cita_que_no_existe_da_404_y_una_ya_pasada_da_400(monkeypatch):
    """Los dos errores del PATCH, que NO son el mismo y la pantalla los cuenta distinto.

    `CitaInexistente` hereda de `ValueError` a propósito: `panel.py` no conoce códigos HTTP
    --no puede, la frontera lo prohíbe-- y esto es lo que le deja a `runtime.py` distinguir
    «ese id no está» de «ese id está y no se puede marcar» sin leerle el mensaje al error.
    """
    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        c = TestClient(runtime.app)

        def no_existe(conn, **kw):
            raise panel.CitaInexistente("esa cita no existe")

        monkeypatch.setattr(panel, "marcar_asistencia", no_existe)
        r = c.patch(f"/api/agenda/citas/{_CITA_DE_AGENDA['id']}", json={"asistio": True})
        assert r.status_code == 404
        assert "no existe" in r.json()["detalle"]

        def todavia_no(conn, **kw):
            raise ValueError("esa cita todavía no ha ocurrido")

        monkeypatch.setattr(panel, "marcar_asistencia", todavia_no)
        r = c.patch(f"/api/agenda/citas/{_CITA_DE_AGENDA['id']}", json={"asistio": True})
        assert r.status_code == 400
        assert "todavía no" in r.json()["detalle"]
    finally:
        runtime.app.dependency_overrides.clear()


def test_el_historial_del_panel_no_le_mezcla_las_marcas_de_asistencia(monkeypatch):
    """El cableado, que es la mitad que la prueba de Neon no puede ver.

    `panel.historial` sabe recortar y sigue sin recortar por su cuenta: quien decide es este
    endpoint, porque es el que sabe a qué pantalla alimenta. Si alguien le quita el
    argumento, la bitácora de precios vuelve a llenarse de marcas de asistencia sin que se
    caiga ninguna prueba de la base.
    """
    pedido: dict[str, tuple[str, ...]] = {}

    def falsa(conn, limite=100, *, excluir_tablas=()):
        pedido["excluir"] = tuple(excluir_tablas)
        return []

    monkeypatch.setattr(persistencia, "conectar", lambda url: _ConexionFalsaSinResolver())
    monkeypatch.setattr(panel, "historial", falsa)
    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        r = TestClient(runtime.app).get("/api/historial")
        assert r.status_code == 200
        assert "citas" in pedido["excluir"], (
            "«Últimos cambios» volvió a pedir la bitácora entera: quince citas al día la "
            "llenan de marcas de asistencia en menos de una semana"
        )
    finally:
        runtime.app.dependency_overrides.clear()


# ==========================================================================================
# La portada: los cinco números
#
# Todas miden un DELTA --antes de insertar y después-- y nunca un total absoluto. El esquema
# `pruebas` no se borra entre pruebas ni entre corridas, y otros seis archivos de Neon
# (`test_tools_neon`, `test_relevo_neon`, `test_seguimientos_neon`, `test_reseteo_neon`,
# `test_reactivacion_neon`, `test_ingesta_neon`) insertan en `mensajes_entrantes`,
# `conversaciones` y `citas` ahí mismo. `resumen_inicio` cuenta la tabla ENTERA, así que un
# `== 1` mediría la basura acumulada de todo el proyecto y amanecería en rojo sin que nadie
# tocara nada. El delta solo mide lo que esta prueba escribió.
# ==========================================================================================

#: El presente clavado de estas tres. Las filas se insertan RELATIVAS a él, así que ni la
#: ventana de 30 días ni el corte del pasado dependen del reloj de verdad: es la regla de
#: `.claude/rules/pruebas.md`, y la razón por la que esta fecha no envejece.
_AHORA_PORTADA = datetime(2026, 9, 22, 15, 0, tzinfo=ZONA_BOGOTA)


@pytest.mark.neon
def test_resumen_inicio_cuenta_personas_y_no_conversaciones(conn):
    """Tres mensajes del MISMO teléfono son UNA persona.

    Una conversación caduca por inactividad de 24 h (`persistencia.conversacion_viva`), así
    que una negociación de tres días son tres filas en `conversaciones` y una sola persona.
    Contar filas inflaría el denominador de todas las tasas y haría parecer a Daniela peor
    de lo que es.
    """
    antes = panel.resumen_inicio(conn, ahora=_AHORA_PORTADA)

    telefono = f"57300{uuid.uuid4().hex[:7]}"
    with conn.cursor() as cur:
        conv = uuid.uuid4()
        cur.execute(
            "INSERT INTO conversaciones (id, telefono, canal, creada_en)"
            " VALUES (%s,%s,'whatsapp',%s)",
            (conv, telefono, _AHORA_PORTADA - timedelta(days=2)),
        )
        for i in range(3):
            cur.execute(
                "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en,"
                " conversacion_id, respondido_en)"
                " VALUES (%s,%s,'text','hola',%s,%s,%s)",
                (
                    f"wamid-{uuid.uuid4().hex}",
                    telefono,
                    _AHORA_PORTADA - timedelta(days=i),
                    conv,
                    _AHORA_PORTADA - timedelta(days=i),
                ),
            )
    conn.commit()

    despues = panel.resumen_inicio(conn, ahora=_AHORA_PORTADA)

    assert despues["escribieron"] - antes["escribieron"] == 1, "tres mensajes, una persona"
    assert despues["sin_contestar"] == antes["sin_contestar"], "los tres tienen respuesta"


@pytest.mark.neon
def test_resumen_inicio_solo_mira_citas_cuya_hora_ya_paso(conn):
    """Una cita de mañana no es una inasistencia: es una cita de mañana.

    Contarla en el denominador de «llegaron» convertiría el futuro en un fracaso y haría
    bajar el número cada vez que Daniela agenda a alguien, que es justo lo contrario de lo
    que la pantalla quiere decir.
    """
    antes = panel.resumen_inicio(conn, ahora=_AHORA_PORTADA)

    telefono = f"57301{uuid.uuid4().hex[:7]}"
    with conn.cursor() as cur:
        conv = uuid.uuid4()
        cur.execute(
            "INSERT INTO conversaciones (id, telefono, canal, creada_en)"
            " VALUES (%s,%s,'whatsapp',%s)",
            (conv, telefono, _AHORA_PORTADA - timedelta(days=3)),
        )
        for cuando, asistio in (
            (_AHORA_PORTADA - timedelta(days=2), True),   # vino
            (_AHORA_PORTADA - timedelta(days=1), None),   # pasó y nadie marcó
            (_AHORA_PORTADA + timedelta(days=1), None),   # mañana: no cuenta
        ):
            cur.execute(
                "INSERT INTO citas (id, conversacion_id, nombre_completo, telefono,"
                " tratamiento, inicio, duracion_minutos, estado, asistio, creada_en)"
                " VALUES (%s,%s,'Prueba',%s,'valoracion',%s,60,'confirmada',%s,%s)",
                (
                    uuid.uuid4(), conv, telefono, cuando, asistio,
                    _AHORA_PORTADA - timedelta(days=3),
                ),
            )
    conn.commit()

    despues = panel.resumen_inicio(conn, ahora=_AHORA_PORTADA)

    assert despues["con_cita"] - antes["con_cita"] == 1, "tres citas del mismo teléfono, una persona"

    delta = {
        clave: despues["asistencia"][clave] - antes["asistencia"][clave]
        for clave in ("llegaron", "marcadas", "cumplibles")
    }
    assert delta == {"llegaron": 1, "marcadas": 1, "cumplibles": 2}, (
        "la de mañana no entra en ninguna de las tres: no ha ocurrido"
    )


@pytest.mark.neon
def test_resumen_inicio_no_cuenta_como_sin_contestar_lo_que_atendio_un_doctor(conn):
    """Un mensaje que llegó durante un relevo lleva `fallo_respuesta` con prefijo `relevo:`
    sin ser un fallo (no negociable 15). Contarlo convertiría cada relevo --que es el
    sistema funcionando-- en un paciente desatendido.
    """
    antes = panel.resumen_inicio(conn, ahora=_AHORA_PORTADA)

    telefono = f"57302{uuid.uuid4().hex[:7]}"
    with conn.cursor() as cur:
        conv = uuid.uuid4()
        cur.execute(
            "INSERT INTO conversaciones (id, telefono, canal, creada_en, tomada_en,"
            " relevo_cerrado_en) VALUES (%s,%s,'whatsapp',%s,%s,%s)",
            (
                conv, telefono,
                _AHORA_PORTADA - timedelta(days=1),
                _AHORA_PORTADA - timedelta(minutes=30),
                _AHORA_PORTADA - timedelta(minutes=10),
            ),
        )
        for fallo in ("relevo: lo atiende el doctor", None):
            cur.execute(
                "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, recibido_en,"
                " conversacion_id, fallo_respuesta)"
                " VALUES (%s,%s,'text','hola',%s,%s,%s)",
                (
                    f"wamid-{uuid.uuid4().hex}", telefono,
                    _AHORA_PORTADA - timedelta(hours=1), conv, fallo,
                ),
            )
    conn.commit()

    despues = panel.resumen_inicio(conn, ahora=_AHORA_PORTADA)

    assert despues["sin_contestar"] - antes["sin_contestar"] == 1, "solo el que NO fue de relevo"
    assert despues["relevo"]["minutos"] - antes["relevo"]["minutos"] == 20
    assert despues["relevo"]["conversaciones"] - antes["relevo"]["conversaciones"] == 1


# ------------------------------------------------------------------------------------------
# `/api/inicio` -- offline. Estas dos SÍ las corre `uv run pytest -q`.
# ------------------------------------------------------------------------------------------


class _CursorQueDelata:
    """Devuelve filas de ceros con la forma que espera cada consulta y apunta el VERBO de
    cada sentencia. Las respuestas se sirven en el orden en que `resumen_inicio` pregunta."""

    def __init__(self, verbos: list[str], respuestas=None, sentencias=None) -> None:
        self._verbos = verbos
        self._sentencias = sentencias if sentencias is not None else []
        # El default es la forma que pide `resumen_inicio`. Las rutas de Conversaciones
        # preguntan otras cosas --y una de ellas, `ultimo_mensaje_del_paciente`, se come el
        # `(0,)` y revienta al restarle una fecha--, así que pueden traer las suyas.
        self._respuestas = (
            list(respuestas)
            if respuestas is not None
            else [(0,), (0,), (0, 0, 0), (0,), (0, 0)]
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._verbos.append(sql.strip().split()[0].upper())
        self._sentencias.append(" ".join(sql.split()).upper())

    def fetchone(self):
        return self._respuestas.pop(0) if self._respuestas else (0,)

    def fetchall(self):
        return []


class _ConexionDeSoloLectura:
    """Una conexión que apunta todo lo que se le pide. No dobla `resumen_inicio`: el SQL de
    verdad corre encima de ella, que es lo que hace que esta prueba vigile algo."""

    def __init__(self, respuestas=None) -> None:
        self.verbos: list[str] = []
        self.sentencias: list[str] = []
        self.commits = 0
        self._respuestas = respuestas

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _CursorQueDelata(self.verbos, self._respuestas, self.sentencias)

    def commit(self):
        self.commits += 1


def test_api_inicio_no_toca_el_calendario(monkeypatch):
    """La invariante que sostiene toda la decisión: esta pantalla es la PRIMERA de cada
    sesión, así que un solo `calendario.*` en este camino son llamadas a Google en cada
    ingreso al panel, varias veces al día y por cada persona que entre.

    Entra CON sesión, por `dependency_overrides`, y eso no es un detalle: comprobar el 401
    de un anónimo dejaría el cuerpo del endpoint sin ejecutar y la prueba pasaría sin
    ejercer nada. Verde por el motivo equivocado es el fallo que este proyecto ya pagó.
    """
    llamadas: list[str] = []

    class CalendarioEspia:
        def __getattr__(self, nombre):
            llamadas.append(nombre)
            raise AssertionError(f"/api/inicio llamó al calendario: {nombre}")

    # El objetivo es `runtime._calendario`, la instancia global que se construye al arrancar.
    # `runtime.calendario` NO existe: el módulo hace `from .calendario import (...)` y solo
    # trae nombres sueltos, así que un espía puesto ahí con `raising=False` no vigilaría nada
    # y esta prueba pasaría vacía.
    monkeypatch.setattr(runtime, "_calendario", CalendarioEspia())
    monkeypatch.setattr(
        runtime.panel,
        "resumen_inicio",
        lambda conn, **kw: {
            "escribieron": 0,
            "con_cita": 0,
            "asistencia": {"llegaron": 0, "marcadas": 0, "cumplibles": 0},
            "sin_contestar": 0,
            "relevo": {"minutos": 0, "conversaciones": 0},
            "volumen": [],
            "agenda_hoy": [],
        },
    )
    monkeypatch.setattr(runtime.persistencia, "casos_recientes", lambda conn: [])
    monkeypatch.setattr(
        runtime.persistencia, "conectar", lambda url: _ConexionFalsaSinResolver()
    )

    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("doctor")
    try:
        r = TestClient(runtime.app).get("/api/inicio")
        assert r.status_code == 200, r.text
        assert r.json()["usuario"] == "Prueba", "el saludo sale del servidor, no del front"
    finally:
        runtime.app.dependency_overrides.clear()

    assert llamadas == [], "la portada no reconcilia contra Google: eso es de /api/agenda"


def test_api_inicio_es_de_solo_lectura(monkeypatch):
    """Ni un INSERT, ni un UPDATE, ni un `commit`. `GET /api/agenda` escribe a propósito y
    está documentado; este no, y la diferencia tiene que quedar vigilada.

    `resumen_inicio` NO se dobla aquí: el SQL de verdad se ejecuta contra una conexión que
    apunta el verbo de cada sentencia, así que la prueba cubre también lo que haga la
    función el día que alguien le añada una consulta.
    """
    conexion = _ConexionDeSoloLectura()
    monkeypatch.setattr(runtime.persistencia, "conectar", lambda url: conexion)
    monkeypatch.setattr(runtime.persistencia, "casos_recientes", lambda conn: [])

    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        r = TestClient(runtime.app).get("/api/inicio")
        assert r.status_code == 200, r.text
    finally:
        runtime.app.dependency_overrides.clear()

    assert len(conexion.verbos) >= 6, (
        "el camino no llegó a consultar: una prueba sobre lo que NO pasa tiene que "
        "comprobar además que el camino corrió"
    )
    assert set(conexion.verbos) == {"SELECT"}, f"la portada escribió: {conexion.verbos}"
    assert conexion.commits == 0


# ==========================================================================================
# La pantalla de Conversaciones
# ==========================================================================================

#: El presente de estas pruebas. Clavado y pasado por parámetro, nunca `now()`: en este
#: proyecto una fecha suelta ya amaneció en rojo tres veces.
_AHORA_CONVERSACIONES = datetime(2026, 9, 22, 15, 0, tzinfo=ZONA_BOGOTA)


def _telefono_nuevo() -> str:
    """Un número que no usa nadie más.

    El esquema `pruebas` NO se borra entre corridas --`CREATE SCHEMA IF NOT EXISTS`-- y otros
    siete archivos escriben en estas mismas tablas. Un teléfono fijo haría que la segunda
    corrida viera los mensajes de la primera.
    """
    return f"5730{uuid.uuid4().int % 10**8:08d}"


def _entra(conn, telefono, *, texto, cuando, respondido=None, fallo=None, tipo="text"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mensajes_entrantes"
            " (wamid, telefono, tipo, texto, recibido_en, respondido_en, fallo_respuesta)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (f"wamid-{uuid.uuid4()}", telefono, tipo, texto, cuando, respondido, fallo),
        )
    conn.commit()


def _conversacion(conn, telefono, *, tomada_por=None, actualizada=None):
    ident = str(uuid.uuid4())
    cuando = actualizada or _AHORA_CONVERSACIONES
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversaciones"
            " (id, telefono, canal, tomada_por, tomada_en, creada_en, actualizada_en)"
            " VALUES (%s, %s, 'whatsapp', %s, %s, %s, %s)",
            (ident, telefono, tomada_por, cuando if tomada_por else None, cuando, cuando),
        )
    conn.commit()
    return ident


@pytest.mark.neon
def test_la_lista_da_una_fila_por_telefono(conn):
    """La unidad es el TELÉFONO. Tres mensajes de la misma persona son una fila, no tres.

    Es la misma regla que decidió los cinco números de la portada: la conversación caduca a
    las 24 h y nace otra, así que listar conversaciones mostraría a la misma persona una vez
    por día que escribiera.
    """
    tel = _telefono_nuevo()
    ahora = _AHORA_CONVERSACIONES
    for i, minutos in enumerate((90, 60, 30)):
        _entra(
            conn,
            tel,
            texto=f"mensaje {i}",
            cuando=ahora - timedelta(minutes=minutos),
            respondido=ahora,
        )

    mias = [
        c
        for c in panel.listar_conversaciones(conn, ahora=ahora, limite=500)
        if c["telefono"] == tel
    ]

    assert len(mias) == 1, "tres mensajes del mismo número dieron más de una fila"
    assert mias[0]["vista_previa"] == "mensaje 2", "la vista previa no es el último mensaje"
    assert mias[0]["estado"] == "activa"
    assert mias[0]["sin_contestar"] == 0


@pytest.mark.neon
def test_una_conversacion_tomada_no_se_pinta_como_desatendida(conn):
    """`relevo` gana a `esperando`, y el contador sigue ahí.

    Una conversación que el doctor tiene tomada y en la que entran mensajes cumple las dos
    condiciones a la vez. Lo que la pantalla tiene que decir es que ya hay alguien encima:
    pintarla de rojo mandaría a otra persona a atender lo que ya está atendido.
    """
    tel = _telefono_nuevo()
    ahora = _AHORA_CONVERSACIONES
    _entra(conn, tel, texto="me sigue doliendo", cuando=ahora - timedelta(hours=1))

    def _mia():
        return next(
            c
            for c in panel.listar_conversaciones(conn, ahora=ahora, limite=500)
            if c["telefono"] == tel
        )

    antes = _mia()
    assert antes["estado"] == "esperando", "un mensaje de hace una hora sin responder"
    assert antes["sin_contestar"] == 1

    _conversacion(conn, tel, tomada_por="Dra. Ruiz", actualizada=ahora)

    despues = _mia()
    assert despues["estado"] == "relevo", "la conversación tomada se pintó como desatendida"
    assert despues["tomada_por"] == "Dra. Ruiz"
    assert despues["sin_contestar"] == 1, "el contador desapareció al tomarla"


@pytest.mark.neon
def test_los_mensajes_de_un_relevo_no_cuentan_como_sin_contestar(conn):
    """No negociable 15: un mensaje que entra durante un relevo lleva `fallo_respuesta`
    empezando por `relevo:` SIN ser un fallo. Es el doctor hablando, no Daniela callada.

    Y el que tiene las DOS columnas en NULL --nadie intentó contestarlo porque el proceso se
    cayó antes de anotar siquiera el fallo-- sí cuenta: es el caso que más duele y el motivo
    de existir del índice de la migración 009. Con `fallo_respuesta NOT LIKE` a secas, SQL lo
    descartaría en silencio, porque `NULL NOT LIKE 'x'` no es falso: es NULL.
    """
    tel = _telefono_nuevo()
    ahora = _AHORA_CONVERSACIONES
    _entra(
        conn,
        tel,
        texto="lo atendió el doctor",
        cuando=ahora - timedelta(hours=2),
        fallo="relevo: la tiene Dra. Ruiz",
    )
    _entra(conn, tel, texto="este nadie lo miró", cuando=ahora - timedelta(hours=1))

    mia = next(
        c
        for c in panel.listar_conversaciones(conn, ahora=ahora, limite=500)
        if c["telefono"] == tel
    )

    assert mia["sin_contestar"] == 1, (
        "o se contó el mensaje del relevo, o se perdió el que tiene las dos columnas en NULL"
    )


@pytest.mark.neon
def test_el_hilo_junta_las_tres_voces_en_orden(conn):
    """Paciente, Daniela y doctor, ordenados por hora. La tercera voz existe desde la 026.

    Antes de ella el tramo del relevo salía vacío y parecía que nadie había hablado.
    """
    from datetime import timezone

    tel = _telefono_nuevo()
    ahora = _AHORA_CONVERSACIONES
    ident = _conversacion(conn, tel, actualizada=ahora)

    _entra(conn, tel, texto="hola, me duele una muela", cuando=ahora - timedelta(minutes=30))

    item = json.dumps(
        {
            "role": "assistant",
            "content": json.dumps({"mensaje_al_paciente": "Lo siento. ¿Desde cuándo?"}),
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_sessions (session_id) VALUES (%s)"
            " ON CONFLICT (session_id) DO NOTHING",
            (ident,),
        )
        cur.execute(
            "INSERT INTO agent_messages (session_id, message_data, created_at)"
            " VALUES (%s, %s, %s)",
            (ident, item, (ahora - timedelta(minutes=29)).astimezone(timezone.utc).replace(tzinfo=None)),
        )
    conn.commit()

    persistencia.guardar_mensaje_del_doctor(
        conn,
        telefono=tel,
        conversacion_id=ident,
        autor="Dra. Ruiz",
        origen="telegram",
        texto="Ven mañana a las 8.",
        wamid="wamid.1",
    )
    # `enviado_en` lo pone la base con `now()`. Es lo correcto en producción --la fila se
    # escribe justo después de enviar-- y un problema aquí: el presente de esta prueba está
    # CLAVADO, y el reloj de verdad puede ir por delante o por detrás de él. Sin recolocar la
    # fila, el orden del hilo depende de la hora a la que se corra la suite.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mensajes_del_doctor SET enviado_en = %s WHERE telefono = %s",
            (ahora - timedelta(minutes=28), tel),
        )
    conn.commit()

    lineas = panel.hilo(conn, tel)

    assert [l["quien"] for l in lineas] == ["paciente", "daniela", "doctor"], (
        f"el hilo no junta las tres voces en orden: {[(l['quien'], l['texto']) for l in lineas]}"
    )
    assert lineas[1]["texto"] == "Lo siento. ¿Desde cuándo?", (
        "se volcó el JSON de RespuestaDaniela en vez de la frase"
    )
    assert lineas[2]["autor"] == "Dra. Ruiz"


@pytest.mark.neon
def test_fuera_de_la_ventana_de_meta_no_se_puede_escribir(conn):
    """WhatsApp no acepta texto libre pasadas 24 h desde el último mensaje del paciente.

    Y a un número que nunca escribió NO se le puede escribir: la ventana la abre él, así que
    «sin mensajes» no es una ventana de cero horas, es ninguna ventana.
    """
    ahora = _AHORA_CONVERSACIONES

    mudo = _telefono_nuevo()
    assert panel.puede_escribir(conn, mudo, ahora=ahora) == {"puede": False, "horas": None}

    reciente = _telefono_nuevo()
    _entra(conn, reciente, texto="hola", cuando=ahora - timedelta(hours=2))
    dentro = panel.puede_escribir(conn, reciente, ahora=ahora)
    assert dentro["puede"] is True
    assert dentro["horas"] == 2.0

    viejo = _telefono_nuevo()
    _entra(conn, viejo, texto="hola", cuando=ahora - timedelta(hours=25))
    fuera = panel.puede_escribir(conn, viejo, ahora=ahora)
    assert fuera["puede"] is False, "se habría dejado mandar un texto que Meta rechaza"
    assert fuera["horas"] == 25.0, "sin las horas, la pantalla no puede explicar por qué"


def test_las_dos_rutas_de_conversaciones_son_de_solo_lectura(monkeypatch):
    """Ni un INSERT, ni un UPDATE, ni un `commit`, en la ruta que más se pide del panel.

    La pantalla se refresca sola cada diez segundos mientras esté a la vista: una escritura
    escondida aquí son miles de escrituras al día contra la base que atiende pacientes.

    `listar_conversaciones` y `hilo` NO se doblan: el SQL de verdad corre contra una conexión
    que apunta el verbo de cada sentencia, así que esto vigila también las consultas que
    alguien añada mañana.
    """
    conexion = _ConexionDeSoloLectura(respuestas=[(None,)] * 8)
    monkeypatch.setattr(runtime.persistencia, "conectar", lambda url: conexion)

    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("recepcion")
    try:
        cliente = TestClient(runtime.app)
        lista = cliente.get("/api/conversaciones")
        assert lista.status_code == 200, lista.text
        detalle = cliente.get("/api/conversaciones/573001110101")
        assert detalle.status_code == 200, detalle.text
    finally:
        runtime.app.dependency_overrides.clear()

    assert len(conexion.verbos) >= 5, (
        "el camino no llegó a consultar: una prueba sobre lo que NO pasa tiene que "
        "comprobar además que el camino corrió"
    )
    # `WITH` es la CTE de la lista, y sigue siendo lectura. Pero aceptar el verbo a
    # ciegas dejaría pasar un `WITH ... INSERT`, que también empieza por WITH: por eso
    # se mira además la sentencia entera.
    assert set(conexion.verbos) <= {"SELECT", "WITH"}, f"verbos: {conexion.verbos}"
    # Con límites de palabra y no subcadenas: `created_at` --una columna que fija el SDK--
    # contiene "CREATE", y sin `\b` esta prueba fallaría por leer una fecha.
    escrituras = r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b"
    culpables = [s for s in conexion.sentencias if re.search(escrituras, s)]
    assert culpables == [], f"la pantalla escribió: {culpables}"
    assert conexion.commits == 0
    assert lista.json()["puede_escribir"] is False, "recepción no toma ni escribe"


def test_un_telefono_que_no_es_un_telefono_no_llega_a_la_base(monkeypatch):
    """Un 404 temprano. Las consultas van parametrizadas, así que esto no es sobre inyección:
    es sobre lo que acaba en los logs de acceso."""
    conexion = _ConexionDeSoloLectura(respuestas=[(None,)] * 8)
    monkeypatch.setattr(runtime.persistencia, "conectar", lambda url: conexion)

    runtime.app.dependency_overrides[runtime.usuario_actual] = _como("admin")
    try:
        r = TestClient(runtime.app).get("/api/conversaciones/no-es-un-telefono")
    finally:
        runtime.app.dependency_overrides.clear()

    assert r.status_code == 404
    assert conexion.verbos == [], "se consultó la base con un teléfono que no lo era"

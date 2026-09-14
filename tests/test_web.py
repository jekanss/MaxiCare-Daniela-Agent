"""El cascarón web: quién puede entrar y, sobre todo, quién NO.

Offline. `TestClient` habla con la aplicación en el mismo proceso, sin abrir un puerto, y
toda la persistencia va contra dobles: estas pruebas no tocan Neon ni el modelo.

------------------------------------------------------------------------------------------
La prueba que justifica el archivo entero
------------------------------------------------------------------------------------------

`test_ninguna_ruta_del_panel_responde_sin_sesion` recorre **todas** las rutas del panel,
sacadas de la propia aplicación y no de una lista escrita a mano. Una ruta nueva que alguien
agregue sin la dependencia `usuario_actual` aparece sola en esa lista y falla aquí, que es
exactamente lo que no puede descubrirse en producción: un panel con conversaciones de
pacientes abierto a cualquiera que sepa la URL.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

# `runtime.py` construye su `Config` al importarse y `MAXICARE_DATABASE_URL` es obligatoria.
# Se carga el `.env` primero y solo se inventa un valor si de verdad no hay ninguno --por
# ejemplo, en un clon recién bajado.
#
# El orden importa, y costó una corrida entera descubrirlo: pytest IMPORTA todos los módulos
# de prueba antes de ejecutar ninguno, así que un `setdefault` con una URL falsa puesto aquí
# se queda fijado para toda la sesión y deja a `test_tools_neon.py` intentando conectarse a
# `localhost/nada`. Una prueba no puede cambiarle el entorno a las demás.
#
# Da igual cuál gane: todas las pruebas de este archivo sustituyen `persistencia.conectar`
# por un doble, así que ninguna abre una conexión de verdad.
from maxicare_daniela.config import cargar_dotenv  # noqa: E402

cargar_dotenv()
os.environ.setdefault("MAXICARE_DATABASE_URL", "postgresql://prueba:prueba@localhost/nada")

from fastapi.testclient import TestClient  # noqa: E402

from maxicare_daniela import autenticacion, contratos, conversacion, persistencia, runtime  # noqa: E402

SECRETO = "s" * autenticacion.MINIMO_SECRETO
CLAVE = "una contrasena larga"

USUARIO = {
    "id": 1,
    "usuario": "ana.rodriguez",
    "nombre": "Dra. A. Rodríguez",
    "hash_contrasena": autenticacion.hash_contrasena(CLAVE),
    "rol": "admin",
    "activo": True,
}


class ConexionFalsa:
    """Sirve para el `with persistencia.conectar(...)` de `runtime.py` y nada más."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setattr(runtime, "_secreto_sesion", SECRETO)
    monkeypatch.setattr(persistencia, "conectar", lambda url: ConexionFalsa())
    monkeypatch.setattr(persistencia, "buscar_usuario", lambda conn, usuario: dict(USUARIO))
    monkeypatch.setattr(persistencia, "marcar_acceso", lambda conn, usuario: None)
    return TestClient(runtime.app)


def rutas_del_panel() -> list[tuple[str, str]]:
    """Las rutas protegidas, leídas de la aplicación y no de una lista a mano.

    Se excluyen las tres que tienen que seguir siendo públicas: el webhook de WhatsApp (lo
    firma Meta), `/salud` (lo consulta el healthcheck del contenedor) y `/api/entrar` (pedir
    sesión para poder iniciar sesión no tendría sentido).
    """
    publicas = {"/whatsapp", "/webhook/whatsapp", "/salud", "/api/entrar", "/api/salir"}
    encontradas = []
    for r in runtime.app.routes:
        ruta = getattr(r, "path", "")
        if not ruta.startswith("/api/") or ruta in publicas:
            continue
        for metodo in sorted(getattr(r, "methods", set()) - {"HEAD", "OPTIONS"}):
            encontradas.append((metodo, ruta))
    return encontradas


# ==========================================================================================
# La puerta
# ==========================================================================================


@pytest.mark.parametrize("metodo,ruta", rutas_del_panel(), ids=lambda v: str(v))
def test_ninguna_ruta_del_panel_responde_sin_sesion(cliente, metodo, ruta):
    r = cliente.request(metodo, ruta, json={})
    assert r.status_code == 401, f"{metodo} {ruta} contestó {r.status_code} sin sesión"


def test_hay_rutas_del_panel_que_comprobar():
    """El cinturón de la prueba de arriba.

    Si `rutas_del_panel()` devolviera una lista vacía --por un cambio de nombre de las rutas,
    por ejemplo-- la prueba parametrizada pasaría sin comprobar absolutamente nada, en verde
    y sin decir una palabra. Esto lo impide."""
    assert len(rutas_del_panel()) >= 3


def test_el_webhook_de_whatsapp_sigue_siendo_publico(cliente):
    """Está en producción. Meta no manda cookies: lo que autentica ese POST es la firma
    `x-hub-signature-256`, y ponerle sesión lo dejaría sin recibir mensajes."""
    r = cliente.get("/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no"})
    assert r.status_code == 403, "responde la lógica del webhook, no la de la sesión"


def test_salud_sigue_siendo_publico(cliente):
    """El `HEALTHCHECK` del contenedor lo consulta sin cookie. Si pidiera sesión, Docker
    marcaría el contenedor como enfermo para siempre."""
    assert cliente.get("/salud").status_code == 200


# ==========================================================================================
# Ingreso
# ==========================================================================================


def test_entrar_con_las_credenciales_correctas_pone_la_cookie(cliente):
    r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": CLAVE})

    assert r.status_code == 200
    assert r.json()["nombre"] == "Dra. A. Rodríguez"
    assert runtime.COOKIE in r.cookies


def test_la_cookie_es_httponly_y_samesite_lax(cliente):
    """`HttpOnly` es lo que impide que un XSS se lleve la sesión; `SameSite=Lax`, lo que
    impide que un enlace de otro sitio dispare una acción con tu sesión puesta."""
    r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": CLAVE})
    cabecera = r.headers["set-cookie"].lower()

    assert "httponly" in cabecera
    assert "samesite=lax" in cabecera


def test_la_contrasena_equivocada_no_entra(cliente):
    r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "otra cosa"})

    assert r.status_code == 401
    assert runtime.COOKIE not in r.cookies


def test_un_usuario_desactivado_no_entra(cliente, monkeypatch):
    """La revocación que sí es inmediata. Un token firmado no se puede anular, pero esto se
    comprueba en cada petición -- ver `test_el_limite_conocido_un_token_no_se_puede_revocar`
    en `test_autenticacion.py`."""
    monkeypatch.setattr(
        persistencia, "buscar_usuario", lambda conn, usuario: {**USUARIO, "activo": False}
    )

    r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": CLAVE})
    assert r.status_code == 401


def test_los_tres_fallos_de_ingreso_dicen_lo_mismo(cliente, monkeypatch):
    """Usuario inexistente, contraseña equivocada y acceso retirado: el mismo texto.

    Distinguirlos le confirmaría a quien prueba nombres cuáles existen en la clínica."""
    respuestas = []

    for buscar in (
        lambda conn, usuario: None,
        lambda conn, usuario: dict(USUARIO),
        lambda conn, usuario: {**USUARIO, "activo": False},
    ):
        monkeypatch.setattr(persistencia, "buscar_usuario", buscar)
        respuestas.append(
            cliente.post("/api/entrar", json={"usuario": "x", "contrasena": "mal"}).json()
        )

    assert len({str(r) for r in respuestas}) == 1, f"se distinguen entre sí: {respuestas}"


def test_una_sesion_valida_se_reconoce(cliente):
    cliente.cookies.set(runtime.COOKIE, autenticacion.firmar_token("ana.rodriguez", secreto=SECRETO))

    r = cliente.get("/api/sesion")

    assert r.status_code == 200
    assert r.json() == {"usuario": "ana.rodriguez", "nombre": "Dra. A. Rodríguez", "rol": "admin"}


def test_una_cookie_firmada_con_otro_secreto_no_sirve(cliente):
    cliente.cookies.set(runtime.COOKIE, autenticacion.firmar_token("ana.rodriguez", secreto="z" * 40))

    assert cliente.get("/api/sesion").status_code == 401


def test_al_usuario_desactivado_se_le_corta_la_sesion_en_curso(cliente, monkeypatch):
    """El caso que de verdad importa: alguien se fue de la clínica con la sesión abierta.

    Su cookie sigue firmada y sigue sin vencer. Lo que lo deja fuera es la comprobación de
    `activo` en cada petición."""
    cliente.cookies.set(runtime.COOKIE, autenticacion.firmar_token("ana.rodriguez", secreto=SECRETO))
    assert cliente.get("/api/sesion").status_code == 200

    monkeypatch.setattr(
        persistencia, "buscar_usuario", lambda conn, usuario: {**USUARIO, "activo": False}
    )
    assert cliente.get("/api/sesion").status_code == 401


# ==========================================================================================
# Sin secreto, el panel se apaga y el webhook sigue vivo
# ==========================================================================================


def test_sin_secreto_el_panel_da_503_pero_el_webhook_sigue_funcionando(monkeypatch):
    """La decisión que protege producción.

    El webhook de WhatsApp está recibiendo mensajes de pacientes. Hacer que el proceso no
    arranque por una variable que solo le importa al panel dejaría a la clínica incomunicada
    por una pantalla que ese día puede que nadie abra."""
    monkeypatch.setattr(runtime, "_secreto_sesion", "")
    cliente = TestClient(runtime.app)

    panel = cliente.get("/api/sesion")
    assert panel.status_code == 503
    # `detalle`, no `detail`: la Tarea 6 añadió el manejador que traduce toda `HTTPException`
    # a la clave que lee `api.ts`. Antes de ese manejador este 503 no llegaba legible a la
    # pantalla -- era exactamente el bug que la Tarea 6 existe para cerrar.
    assert "MAXICARE_SECRETO_SESION" in panel.json()["detalle"]

    assert cliente.get("/salud").status_code == 200
    assert cliente.get("/whatsapp", params={"hub.mode": "subscribe"}).status_code == 403


# ==========================================================================================
# El chat de pruebas
# ==========================================================================================


def test_el_chat_usa_el_agente_de_produccion_y_el_carril_de_pruebas(cliente, monkeypatch):
    """Las dos mitades del entregable de la fase 5, en una sola prueba.

    1. Que sea EL MISMO `agentes.daniela`: `responder` recibe `agente=None`, y su default es
       el de producción. Si alguien clonara el agente «para el chat», esto lo dice.
    2. Que la URL de la base apunte al esquema del carril de pruebas y NUNCA a `public`.
    """
    visto: dict = {}

    async def falso_responder(entrada, *, ctx, agente=None, sesion=None, **extra):
        visto["entrada"] = entrada
        visto["agente"] = agente
        visto["url"] = ctx.database_url
        visto["identidad"] = ctx.identidad_verificada
        visto["telegram"] = ctx.telegram_bot_token
        return conversacion.Resultado(
            respuesta=conversacion._respuesta_de_emergencia("Hola, soy Daniela."), turno=1
        )

    monkeypatch.setattr(runtime, "_preparar_esquema_de_pruebas", lambda: "postgresql://x/y?options=-csearch_path%3Dpruebas_web")
    monkeypatch.setattr(persistencia, "asegurar_conversacion", lambda conn, **kw: "conv-web-1")
    # El chat web pregunta si ese "telefono" tiene ficha, igual que `_leer_estado` en
    # WhatsApp: es lo que decide si un paciente nuevo puede pedir su primera cita.
    monkeypatch.setattr(persistencia, "buscar_paciente_por_telefono", lambda conn, tel: None)
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: persistencia.CONFIGURACION_POR_DEFECTO)
    # Desde la Tarea 6, `_contexto_de_prueba` también pide una sesión persistida a
    # `persistencia.sesion_de_agente`. Sin este doble, la llamada es real: construye un
    # `AsyncEngine` de SQLAlchemy con la URL de Neon de `config.database_url` (perezoso, no
    # abre conexión, pero queda vivo y sin disponer en `persistencia._engines`) y contradice
    # la promesa de la cabecera del archivo de que ninguna prueba de aquí abre una conexión
    # de verdad.
    monkeypatch.setattr(
        persistencia, "sesion_de_agente",
        lambda id_conversacion, *, database_url, esquema=None, limite=None: conversacion.SesionEnMemoria(id_conversacion),
    )
    monkeypatch.setattr(conversacion, "responder", falso_responder)
    cliente.cookies.set(runtime.COOKIE, autenticacion.firmar_token("ana.rodriguez", secreto=SECRETO))

    r = cliente.post("/api/pruebas/chat", json={"mensaje": "hola", "conversacion": None})

    assert r.status_code == 200
    assert r.json()["mensaje"] == "Hola, soy Daniela."
    assert visto["agente"] is None, "debe usar el agente por defecto, que es el de producción"
    assert "pruebas_web" in visto["url"]
    assert "public" not in visto["url"]
    assert visto["identidad"] is False, "un paciente nuevo llega sin identificar, también aquí"
    assert visto["telegram"] == "", "una prueba no le hace sonar el teléfono a un doctor"


# ==========================================================================================
# Tarea 6 -- el chat de pruebas persiste en Neon, con el mismo mecanismo que WhatsApp
# ==========================================================================================


class _ConexionDeMentira:
    """El mismo doble que `ConexionFalsa`, con nombre propio para las pruebas de esta
    sección: sirve para el `with persistencia.conectar(...)` de `_contexto_de_prueba` sin
    abrir nada de verdad."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _conexion_de_mentira(url):
    return _ConexionDeMentira()


def test_el_chat_de_pruebas_persiste_en_su_propio_esquema(monkeypatch):
    """El chat del panel usa la MISMA sesión persistida que WhatsApp, apuntada a
    `pruebas_web`. Con un diccionario en memoria dejaría de probar lo que existe para
    probar, y el esquema de pruebas es lo único que impide que una cita de mentira ocupe un
    cupo real de la clínica."""
    pedidas: list[dict] = []

    def fabrica(id_conversacion, *, database_url, esquema=None, limite=None):
        pedidas.append({"id": id_conversacion, "esquema": esquema, "database_url": database_url})
        return conversacion.SesionEnMemoria(id_conversacion)

    monkeypatch.setattr(runtime.persistencia, "sesion_de_agente", fabrica)
    monkeypatch.setattr(runtime, "_preparar_esquema_de_pruebas", lambda: "postgresql://x/y")
    monkeypatch.setattr(
        runtime.persistencia, "asegurar_conversacion", lambda *a, **k: "conv-web-1"
    )
    monkeypatch.setattr(
        runtime.persistencia, "buscar_paciente_por_telefono", lambda conn, tel: None
    )
    monkeypatch.setattr(runtime.persistencia, "conectar", _conexion_de_mentira)
    monkeypatch.setattr(runtime.persistencia, "leer_configuracion", lambda conn: {
        "capacidad_por_hora": 2, "duracion_cita_minutos": 60, "cierre_relevo_minutos": 180
    })
    runtime._conversaciones_de_prueba.clear()

    try:
        runtime._contexto_de_prueba({"usuario": "ana"}, None)

        assert [{"id": p["id"], "esquema": p["esquema"]} for p in pedidas] == [
            {"id": "conv-web-1", "esquema": runtime.ESQUEMA_PRUEBAS_WEB}
        ]
    finally:
        runtime._conversaciones_de_prueba.clear()


def test_la_llamada_a_sesion_de_agente_usa_config_database_url_no_la_url_de_pruebas(monkeypatch):
    """El aviso de la revisión de las Tareas 3 y 4: una URL con `options=-csearch_path=`
    colada aquí haría que el aislamiento lo diera el `search_path` de la conexión y no
    `schema_translate_map`, igual que le pasó a `test_las_tablas_de_sesion_no_se_crean_en_public`
    en `tests/test_sesion_neon.py` antes de `_sin_options`. `_preparar_esquema_de_pruebas()`
    SÍ trae ese `options=` -- lo necesita la conexión síncrona de `asegurar_conversacion` y
    `leer_configuracion`, que no pasan por SQLAlchemy --, así que la prueba existe para que
    nadie reemplace `config.database_url` por esa URL en la llamada a `sesion_de_agente`.

    Lo que esto comprueba es el CONTRATO de esa llamada -- qué argumentos recibe
    `sesion_de_agente` --, no el mecanismo de `schema_translate_map` en sí: con
    `sesion_de_agente` doblada, ese mecanismo nunca se ejercita aquí, y la prueba pasaría
    igual si `schema_translate_map` desapareciera de `persistencia._engine_de`. Ese nivel
    de aserción es el correcto para este diff -- ver `test_sesion_neon.py` para la prueba
    que sí ejercita el mecanismo contra Neon.

    Las dos URLs comparadas son inventadas y distinguibles entre sí a propósito: si esta
    prueba comparara contra `runtime.config.database_url` de verdad, en cualquier máquina
    con el `.env` de la clínica cargado esa aserción fallida volcaría la contraseña real de
    Neon a la salida de pytest -- pasó una vez, durante la verificación por mutación de esta
    misma prueba en la Tarea 6. `dataclasses.replace` sustituye `runtime.config` por uno con
    una URL de mentira antes de comparar, así que ninguna caída puede ya imprimir la cadena
    real."""
    pedidas: list[dict] = []

    def fabrica(id_conversacion, *, database_url, esquema=None, limite=None):
        pedidas.append({"database_url": database_url, "esquema": esquema})
        return conversacion.SesionEnMemoria(id_conversacion)

    config_de_mentira = dataclasses.replace(
        runtime.config, database_url="postgresql://u:c@prod-falsa/db"
    )
    monkeypatch.setattr(runtime, "config", config_de_mentira)
    monkeypatch.setattr(runtime.persistencia, "sesion_de_agente", fabrica)
    # La URL "de pruebas" lleva el `options` a propósito, para poder distinguirla de
    # `config.database_url` en la aserción de abajo. Ninguna de las dos es una URL real.
    monkeypatch.setattr(
        runtime,
        "_preparar_esquema_de_pruebas",
        lambda: "postgresql://x/y?options=-csearch_path%3Dpruebas_web",
    )
    monkeypatch.setattr(
        runtime.persistencia, "asegurar_conversacion", lambda *a, **k: "conv-web-2"
    )
    monkeypatch.setattr(
        runtime.persistencia, "buscar_paciente_por_telefono", lambda conn, tel: None
    )
    monkeypatch.setattr(runtime.persistencia, "conectar", _conexion_de_mentira)
    monkeypatch.setattr(runtime.persistencia, "leer_configuracion", lambda conn: {
        "capacidad_por_hora": 2, "duracion_cita_minutos": 60, "cierre_relevo_minutos": 180
    })
    runtime._conversaciones_de_prueba.clear()

    try:
        runtime._contexto_de_prueba({"usuario": "ana"}, None)

        assert len(pedidas) == 1
        assert pedidas[0]["database_url"] == config_de_mentira.database_url
        assert "options" not in pedidas[0]["database_url"], (
            "la sesion del chat web no puede llevar search_path: el aislamiento tiene que "
            "quedar solo a cargo de schema_translate_map, vía esquema="
        )
        assert pedidas[0]["esquema"] == runtime.ESQUEMA_PRUEBAS_WEB
    finally:
        runtime._conversaciones_de_prueba.clear()


def test_recuperar_una_conversacion_viva_reconstruye_la_sesion(monkeypatch):
    """El contexto se queda en memoria (barato, se reconstruye si el proceso se reinicia),
    pero la sesión NO: se le vuelve a pedir a `persistencia.sesion_de_agente` en cada turno,
    igual que en WhatsApp -- ahí no hay caché de sesión desde la Tarea 5. Si alguien
    reintrodujera una tupla `(ctx, sesion)` cacheada, esta prueba deja de ver una segunda
    llamada a la fábrica."""
    pedidas: list[dict] = []

    def fabrica(id_conversacion, *, database_url, esquema=None, limite=None):
        pedidas.append({"id": id_conversacion, "esquema": esquema})
        return conversacion.SesionEnMemoria(id_conversacion)

    monkeypatch.setattr(runtime.persistencia, "sesion_de_agente", fabrica)
    runtime._conversaciones_de_prueba.clear()
    ctx_previo = contratos.ContextoDaniela(
        id_conversacion="conv-web-viva",
        telefono_completo="web-ana",
        database_url="postgresql://x/y",
        calendario=runtime.CalendarioDoble(),
    )
    runtime._conversaciones_de_prueba["conv-web-viva"] = ctx_previo

    try:
        ctx, _sesion = runtime._contexto_de_prueba({"usuario": "ana"}, "conv-web-viva")

        assert ctx is ctx_previo, "el contexto vivo no se reconstruye si ya existe"
        assert pedidas == [{"id": "conv-web-viva", "esquema": runtime.ESQUEMA_PRUEBAS_WEB}]
    finally:
        runtime._conversaciones_de_prueba.clear()


def test_el_esquema_del_chat_no_es_el_de_los_scripts_de_prueba():
    """`probar_tools.py` y `probar_agentes.py` BORRAN el esquema `pruebas` al terminar.

    Si el chat web usara ese mismo nombre, correr una prueba desde la terminal le vaciaría la
    conversación a quien estuviera usando el chat en ese momento."""
    assert runtime.ESQUEMA_PRUEBAS_WEB != "pruebas"


# ==========================================================================================
# Los archivos del frontend
# ==========================================================================================


def test_una_ruta_de_api_inexistente_devuelve_json_y_no_html(cliente):
    """La ruta comodín sirve `index.html` para que la navegación del panel funcione. Si se
    tragara también lo que cuelga de `/api`, una ruta mal escrita produciría el peor error de
    depurar que hay: un `SyntaxError` de JSON en la consola del navegador."""
    # Con GET, la petición llega hasta la ruta comodín: es ahí donde el `if` que mira si la
    # ruta empieza por `api/` hace su trabajo. Con POST ni siquiera llega -- la aplicación
    # responde 405 antes--, que es igual de correcto y también en JSON.
    inexistente = cliente.get("/api/lo-que-sea")
    assert inexistente.status_code == 404
    assert inexistente.headers["content-type"].startswith("application/json")

    metodo_equivocado = cliente.post("/api/lo-que-sea", json={})
    assert metodo_equivocado.status_code == 405
    assert metodo_equivocado.headers["content-type"].startswith("application/json")

    # Y el contraste: una ruta que NO es de la API devuelve la página del panel. Solo se
    # comprueba si el frontend está compilado -- `web/dist` no se versiona, y una suite que
    # exige `npm run build` para pasar dejaría de correr en cuanto alguien clone el repo.
    panel = cliente.get("/agenda")
    if panel.status_code == 200:
        assert panel.headers["content-type"].startswith("text/html")
    else:
        assert panel.status_code == 503, "sin compilar debe decirlo, no dar un 404 confuso"


def test_sin_compilar_el_frontend_lo_dice_con_el_comando(cliente, monkeypatch, tmp_path):
    """`web/dist` no se versiona. Quien clone el repositorio y arranque el servidor tiene que
    leer qué le falta, no una página en blanco."""
    monkeypatch.setattr(runtime, "RUTA_WEB", tmp_path / "no-existe")

    r = cliente.get("/")

    assert r.status_code == 503
    assert "npm run build" in r.json()["detalle"]


def test_salud_cuenta_los_mensajes_sin_responder(monkeypatch):
    """`/salud` es lo que mira la clínica para saber si algo va mal, y el 13/09/2026 aprendió
    a contar los mensajes que entraron y nadie contestó.

    Esta prueba existe por un despiste que llegó a producción: la consulta nueva se escribió
    un nivel a la izquierda, o sea FUERA del `with persistencia.conectar(...)`, y corría con
    la conexión ya cerrada. `/salud` contestaba `base_de_datos: "FALLA: the connection is
    closed"` -- el indicador de salud mintiendo sobre la salud, y encima estrenándose así.
    Ninguna prueba lo cazó porque el `except` de `/salud` se traga cualquier error de base y
    el endpoint sigue devolviendo 200.

    El doble cierra la conexión al salir del `with` y revienta si alguien la usa después: es
    la reproducción exacta del fallo, no una aproximación.
    """
    class ConexionQueSeCierra:
        def __init__(self) -> None:
            self.cerrada = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.cerrada = True
            return False

        def cursor(self):
            if self.cerrada:
                raise RuntimeError("the connection is closed")
            return self

        def __iter__(self):
            return iter(())

        def execute(self, *_a, **_k):
            if self.cerrada:
                raise RuntimeError("the connection is closed")

        def fetchone(self):
            return (0,)

    conexiones: list[ConexionQueSeCierra] = []

    def conectar_falso(_url):
        conexion = ConexionQueSeCierra()
        conexiones.append(conexion)
        return conexion

    def contar_falso(conn, **_kw):
        if conn.cerrada:
            raise RuntimeError("the connection is closed")
        return 3

    monkeypatch.setattr(runtime, "_secreto_sesion", SECRETO)
    monkeypatch.setattr(persistencia, "conectar", conectar_falso)
    monkeypatch.setattr(persistencia, "contar_sin_responder", contar_falso)

    cuerpo = TestClient(runtime.app).get("/salud").json()

    assert cuerpo["base_de_datos"] == "ok", (
        "la cuenta nueva corrió con la conexión cerrada: mira su indentación"
    )
    assert cuerpo["sin_responder"] == 3

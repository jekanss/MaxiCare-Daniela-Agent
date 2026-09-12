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

from maxicare_daniela import autenticacion, conversacion, persistencia, runtime  # noqa: E402

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
    assert "MAXICARE_SECRETO_SESION" in panel.json()["detail"]

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
    monkeypatch.setattr(persistencia, "leer_configuracion", lambda conn: persistencia.CONFIGURACION_POR_DEFECTO)
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

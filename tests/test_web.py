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


@pytest.fixture(autouse=True)
def sin_intentos_arrastrados():
    """El contador de intentos fallidos vive en un dict de módulo, así que se arrastra.

    `runtime._intentos_de_ingreso` no es estado de una petición: es estado del PROCESO, y la
    ventana dura cinco minutos -- mucho más que una suite entera. Varias pruebas de este
    archivo fallan el ingreso a propósito y todas comparten la misma clave (`TestClient` se
    presenta siempre como el mismo cliente), así que sin esto la número nueve empezaría a
    recibir 429 y la prueba que se rompiera no sería la que metió los intentos.

    Es el mismo problema que el `limpiar_estado()` de `test_atencion.py` y la misma regla de
    `.claude/rules/pruebas.md`: una prueba no le cambia el entorno a las demás. Se limpia
    antes Y después, para que tampoco se escape hacia otro archivo.
    """
    runtime._intentos_de_ingreso.clear()
    yield
    runtime._intentos_de_ingreso.clear()


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


# ==========================================================================================
# Que probar contraseñas cueste algo
#
# `/api/entrar` era el único endpoint público con efecto real y no tenía ningún freno: ni
# contador, ni bloqueo, ni retardo. Contra un panel con datos clínicos eso son intentos
# ilimitados a la velocidad de la red -- y además un amplificador, porque cada intento con
# usuario existente cuesta un scrypt de 16 MiB en el único worker que también atiende los
# webhooks de WhatsApp: bastante concurrencia aquí deja a los pacientes sin respuesta sin
# haber adivinado ninguna clave.
#
# Las tres pruebas de abajo sustituyen `verificar_contrasena` por una comparación directa. No
# es pereza: lo que miden es el CONTADOR, y pagar ocho scrypt de verdad (~60 ms cada uno) por
# una cuenta de enteros es la clase de lentitud que hace que alguien acabe saltándose la
# suite. Que el scrypt sea de verdad lo sostiene la última prueba del bloque, que es la única
# a la que eso le importa.
# ==========================================================================================


def _verificacion_barata(monkeypatch) -> list[tuple[str, str]]:
    """Sustituye el scrypt por una comparación, y anota cada llamada con sus argumentos."""
    llamadas: list[tuple[str, str]] = []

    def verificar(contrasena: str, hash_guardado: str) -> bool:
        llamadas.append((contrasena, hash_guardado))
        return contrasena == CLAVE

    monkeypatch.setattr(autenticacion, "verificar_contrasena", verificar)
    return llamadas


def test_pasados_los_intentos_la_puerta_responde_429_y_no_401(cliente, monkeypatch):
    """429 y no 401, y la diferencia es deliberada.

    Quien se pasó de intentos tiene que poder distinguir «me estás frenando» de «la
    contraseña está mal», o seguirá probando contra un muro creyendo que el muro es la
    contraseña. A quien ataca no le revela nada que no sepa ya --lo está midiendo con el
    reloj-- y a un doctor que olvidó su clave le ahorra media hora de intentos inútiles.
    """
    _verificacion_barata(monkeypatch)

    for numero in range(runtime.MAX_INTENTOS_LOGIN):
        r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "mal"})
        assert r.status_code == 401, f"el intento {numero + 1} ya frenaba: el contador cuenta de más"

    r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "mal"})

    assert r.status_code == 429
    assert "intentos" in r.json()["detalle"].lower()


def test_la_puerta_cerrada_tampoco_se_abre_con_la_contrasena_BUENA(cliente, monkeypatch):
    """El freno va ANTES de mirar la base, así que ni siquiera la clave correcta pasa.

    Es lo que lo convierte en un freno de verdad y no en un adorno: si acertar lo levantara,
    un ataque por diccionario seguiría llegando a su objetivo -- solo que sin recibir la
    confirmación hasta el final. Y de paso es lo que impide que el intento número nueve pague
    el scrypt, que era la otra mitad del problema.
    """
    _verificacion_barata(monkeypatch)
    for _ in range(runtime.MAX_INTENTOS_LOGIN):
        cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "mal"})

    r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": CLAVE})

    assert r.status_code == 429
    assert runtime.COOKIE not in r.cookies


def test_un_ingreso_correcto_borra_la_cuenta_de_intentos(cliente, monkeypatch):
    """Quien acertó no arrastra sus equivocaciones.

    Un doctor que tecleó mal tres veces y entró a la cuarta se quedaría a tres intentos del
    bloqueo durante los cinco minutos siguientes -- y el bloqueo le llegaría el día siguiente,
    sin relación visible con nada. Cinco minutos fuera del panel en mitad de una consulta es
    un coste real, y cero beneficio: el que acertó ya demostró que sabe la clave.

    No basta con mirar el diccionario: se comprueba que después del acierto vuelve a caber la
    tanda ENTERA, que es lo que significa que la cuenta se puso a cero y no que se restó uno.
    """
    _verificacion_barata(monkeypatch)
    for _ in range(3):
        cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "mal"})
    assert runtime._intentos_de_ingreso, "la prueba no prueba nada: no se anotó ni un intento"

    assert cliente.post(
        "/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": CLAVE}
    ).status_code == 200

    assert runtime._intentos_de_ingreso == {}
    for numero in range(runtime.MAX_INTENTOS_LOGIN):
        r = cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "mal"})
        assert r.status_code == 401, f"quedaban intentos arrastrados: frenó en el {numero + 1}"


def test_un_usuario_INEXISTENTE_paga_el_mismo_scrypt_que_uno_real(cliente, monkeypatch):
    """El canal lateral que anulaba la defensa del mensaje genérico.

    Los tres fallos de ingreso dicen exactamente lo mismo --hay una prueba justo arriba que lo
    sostiene-- pero eso era cierto en el CUERPO de la respuesta y falso en el RELOJ: con el
    `and` cortocircuitando, un usuario inexistente no llegaba a `verificar_contrasena` y
    respondía en ~1 ms, mientras uno real pagaba los ~60 ms del scrypt. Tres órdenes de
    magnitud, medibles con cualquier cliente HTTP. Quien probara nombres sabría cuáles existen
    en la clínica sin acertar una sola contraseña.

    **No se miden tiempos de reloj**: una prueba que compara milisegundos en una máquina
    compartida es una prueba que falla sola un martes cualquiera, y entonces alguien la borra.
    Lo que se comprueba es la causa -- que la verificación SE LLAMA en los dos caminos-- y
    contra qué hash se llama, que es lo que demuestra que el señuelo se usó de verdad y no que
    la llamada vino de otra parte.
    """
    llamadas = _verificacion_barata(monkeypatch)
    monkeypatch.setattr(runtime, "_senuelo", "scrypt$el-senuelo")

    cliente.post("/api/entrar", json={"usuario": "ana.rodriguez", "contrasena": "mal"})
    con_usuario = list(llamadas)

    monkeypatch.setattr(persistencia, "buscar_usuario", lambda conn, usuario: None)
    llamadas.clear()
    cliente.post("/api/entrar", json={"usuario": "no.existe", "contrasena": "mal"})

    assert len(con_usuario) == 1, "el usuario existente no llegó a verificar: la prueba no compara nada"
    assert len(llamadas) == 1, (
        "el usuario inexistente no pagó el scrypt: el `and` volvió a cortocircuitar y el "
        "reloj vuelve a delatar qué usuarios existen"
    )
    assert llamadas[0][1] == "scrypt$el-senuelo", "se verificó contra otra cosa, no el señuelo"


def test_el_senuelo_es_un_scrypt_de_VERDAD_con_los_mismos_parametros():
    """Y aquí sí se paga el scrypt entero, porque es lo único que esta prueba comprueba.

    Un señuelo que fuera una cadena cualquiera haría que `verificar_contrasena` se rindiera al
    parsear el formato, o que corriera con otros parámetros: en los dos casos el tiempo no
    coincidiría y el canal lateral seguiría abierto. La defensa no es «llamar a la función»,
    es «pagar exactamente lo mismo». Se paga el mismo coste para no decir nada.
    """
    senuelo = runtime._scrypt_senuelo()

    assert autenticacion.verificar_contrasena("cualquier cosa", senuelo) is False, (
        "no es un hash verificable: `verificar_contrasena` ni siquiera lo procesa"
    )
    # `scrypt$n$r$p$sal$hash`: los cuatro primeros campos son el coste, y tienen que ser los
    # mismos que los de un usuario de la tabla o el tiempo no coincide.
    assert senuelo.split("$")[:4] == USUARIO["hash_contrasena"].split("$")[:4]


def test_el_senuelo_se_calcula_una_sola_vez(monkeypatch):
    """Perezoso y cacheado. Recalcularlo en cada intento regalaría al atacante justo lo que
    este freno le quita: un scrypt de 16 MiB por petición, gratis y sin contador, en el mismo
    worker que atiende los webhooks de WhatsApp. Y pagarlo al importar el módulo retrasaría el
    arranque del servidor por una defensa que quizá no se use nunca."""
    monkeypatch.setattr(runtime, "_senuelo", None)
    veces = []
    original = autenticacion.hash_contrasena
    monkeypatch.setattr(
        autenticacion, "hash_contrasena", lambda c: (veces.append(c), original(c))[1]
    )

    primero = runtime._scrypt_senuelo()
    segundo = runtime._scrypt_senuelo()

    assert primero == segundo
    assert len(veces) == 1


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
# Cambiar la propia contraseña
# ==========================================================================================
#
# Hasta el 22/09/2026 no había forma: la única manera de poner o restablecer una clave era
# `scripts/crear_usuario.py`, con SSH y el `.env` delante. La clave con la que se creaba una
# cuenta era la clave de por vida.


@pytest.fixture
def con_sesion(cliente, monkeypatch):
    """`cliente` ya dentro, y espiando lo que se escribe en la base."""
    cliente.cookies.set(
        runtime.COOKIE, autenticacion.firmar_token("ana.rodriguez", secreto=SECRETO)
    )
    escritos: list[tuple[str, str]] = []
    monkeypatch.setattr(
        persistencia,
        "cambiar_contrasena",
        lambda conn, usuario, *, hash_contrasena: bool(
            escritos.append((usuario, hash_contrasena))
        )
        or True,
    )
    return cliente, escritos


def test_cambiar_la_contrasena_guarda_un_hash_que_sirve(con_sesion):
    """Y se comprueba VERIFICANDO, no mirando que la cadena cambió.

    Un hash guardado con la clave vieja, o con un formato que `verificar_contrasena` no sabe
    leer, pasaría cualquier comprobación de «se escribió algo» y dejaría a la persona fuera
    de su propia cuenta en el siguiente ingreso -- sin un error en ningún log y sin nadie que
    pueda arreglarlo desde el panel.
    """
    cliente, escritos = con_sesion

    r = cliente.post(
        "/api/cambiar-contrasena",
        json={"actual": CLAVE, "nueva": "otra frase bastante larga"},
    )

    assert r.status_code == 200
    assert len(escritos) == 1
    usuario, hash_nuevo = escritos[0]
    assert usuario == "ana.rodriguez"
    assert autenticacion.verificar_contrasena("otra frase bastante larga", hash_nuevo)
    assert not autenticacion.verificar_contrasena(CLAVE, hash_nuevo), "guardó la vieja"


def test_sin_la_contrasena_actual_no_se_cambia_nada(con_sesion):
    """LA PRUEBA QUE NO SE PUEDE RELAJAR de este endpoint.

    Tener la cookie prueba que alguien entró, no que sea el dueño de la cuenta: un portátil
    sin bloquear en la recepción es exactamente ese caso. Sin esta comprobación, ese portátil
    basta para dejar fuera al dueño y quedarse dentro, y una sesión de 8 h se convierte en
    acceso permanente.
    """
    cliente, escritos = con_sesion

    r = cliente.post(
        "/api/cambiar-contrasena",
        json={"actual": "la que me invente", "nueva": "otra frase bastante larga"},
    )

    assert r.status_code == 400
    assert escritos == [], "se escribió una contraseña sin saber la anterior"


def test_una_contrasena_nueva_demasiado_corta_se_rechaza_con_el_motivo(con_sesion):
    """400 con el texto del módulo dentro, no un genérico.

    Es lo único de este endpoint que el usuario puede corregir escribiendo; un «no se pudo»
    lo deja probando largos a ciegas."""
    cliente, escritos = con_sesion

    r = cliente.post("/api/cambiar-contrasena", json={"actual": CLAVE, "nueva": "corta"})

    assert r.status_code == 400
    assert str(autenticacion.MINIMO_CONTRASENA) in r.json()["detalle"]
    assert escritos == []


def test_la_contrasena_corta_se_rechaza_ANTES_de_mirar_la_actual(con_sesion, monkeypatch):
    """Y no al revés, que es lo que saldría de escribirlo en el orden natural.

    `hash_contrasena` va primero a propósito: validar el largo no necesita la base, y
    ponerlo después haría que cada intento con una clave corta pagara los ~60 ms de scrypt de
    `verificar_contrasena` para acabar rechazándolo por algo que se sabía sin consultar nada.

    Se espía la VERIFICACIÓN y no `buscar_usuario`, que es lo primero que uno escribe y no
    aísla nada: `usuario_actual` --la dependencia que protege la ruta-- ya consulta esa misma
    fila para comprobar que el usuario siga activo, así que el doble saltaría antes de entrar
    al endpoint y la prueba pasaría verde sin mirar lo que dice mirar.
    """
    cliente, _ = con_sesion
    monkeypatch.setattr(
        autenticacion,
        "verificar_contrasena",
        lambda *a, **kw: pytest.fail("pagó scrypt por una contraseña que ya se sabía corta"),
    )

    assert cliente.post(
        "/api/cambiar-contrasena", json={"actual": CLAVE, "nueva": "corta"}
    ).status_code == 400


def test_cambiar_la_contrasena_no_toca_el_rol_ni_el_nombre(con_sesion, monkeypatch):
    """`crear_usuario` haría el mismo trabajo y traería tres columnas de regalo.

    La ruta no manda el rol --quien cambia su clave no lo sabe ni tiene por qué--, así que
    reusar aquel upsert significaría reenviar el rol actual desde el frontend, y un descuido
    ahí degrada a un admin a recepción sin un error en ningún sitio.
    """
    cliente, _ = con_sesion
    monkeypatch.setattr(
        persistencia,
        "crear_usuario",
        lambda *a, **kw: pytest.fail("el cambio de clave pasó por el upsert de crear_usuario"),
    )

    assert cliente.post(
        "/api/cambiar-contrasena",
        json={"actual": CLAVE, "nueva": "otra frase bastante larga"},
    ).status_code == 200


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


def test_el_chat_web_comparte_UN_calendario_para_todo_el_proceso():
    """Una clinica tiene un calendario, no uno por conversacion.

    Construir un `CalendarioDoble` nuevo en cada `_contexto_de_prueba` era inofensivo hasta
    que `consultar_citas` empezo a contrastar las citas contra el calendario (no negociable
    20): un doble recien nacido no tiene NINGUN evento, asi que toda cita creada en el chat
    del panel se leeria como «borrada de Calendar» y se cancelaria sola en cuanto alguien
    preguntara por ella. Lo cazo `scripts/probar_tools.py`, que reproducia el mismo patron.
    """
    assert isinstance(runtime._CALENDARIO_WEB, runtime.CalendarioDoble)
    # De mentira, que es la otra mitad: probar «agendame el martes» no puede crear un evento
    # en el calendario donde los doctores miran su dia.
    assert runtime._CALENDARIO_WEB is not runtime._calendario


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


# ==========================================================================================
# `/salud` es PÚBLICO, y eso decide qué puede decir
#
# Lo consultan el `HEALTHCHECK` del contenedor y Traefik, así que no puede pedir sesión --hay
# una prueba arriba que lo fija-- y de ahí sale todo lo demás: cualquier cosa que este
# endpoint imprima la puede leer quien sepa la URL, que es una cadena adivinable.
#
# La señal que la clínica necesita se conserva entera: que la base falla y que falta algo por
# configurar se siguen viendo desde fuera. Lo que se va es el DETALLE, que no le sirve a quien
# mira desde fuera y sí a quien está probando la puerta. Quien puede arreglarlo entra al
# contenedor y lee el log, que es donde está.
# ==========================================================================================


SECRETOS_DEL_ENTORNO = (
    "MAXICARE_WHATSAPP_TOKEN",
    "MAXICARE_WHATSAPP_PHONE_NUMBER_ID",
    "MAXICARE_WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "MAXICARE_TELEGRAM_BOT_TOKEN",
    "MAXICARE_TELEGRAM_CHAT_DOCTORES",
)


def _config_con(**cambios):
    """El `Config` real del proceso con unos campos cambiados. Frozen, así que `replace`."""
    return dataclasses.replace(runtime.config, **cambios)


def test_salud_no_dice_QUE_secreto_le_falta(cliente, monkeypatch):
    """Un `{"faltan": ["WHATSAPP_APP_SECRET", ...]}` público es un mapa de reconocimiento
    gratuito.

    Le dice a un desconocido exactamente qué credencial no está puesta y, con ella, qué
    defensa está apagada: sin `WHATSAPP_APP_SECRET` el webhook no verifica la firma de Meta,
    y sin `MAXICARE_TELEGRAM_BOT_TOKEN` el relevo no existe. Es decirle a quien está probando
    las puertas cuál está sin llave, y en el momento exacto en que lo está.

    La señal que la clínica necesita --«falta algo por configurar»-- no se pierde: se queda en
    una palabra. Qué falta lo ve quien entra al contenedor, que es quien puede ponerlo.
    """
    monkeypatch.setattr(
        runtime,
        "config",
        _config_con(
            whatsapp_token="",
            whatsapp_phone_number_id="",
            whatsapp_verify_token="",
            whatsapp_app_secret="",
            telegram_bot_token="",
            telegram_chat_doctores="",
        ),
    )

    r = cliente.get("/salud")

    assert r.json()["configuracion"] == "incompleta"
    assert isinstance(r.json()["configuracion"], str), "volvió a ser un dict con la lista"
    for variable in SECRETOS_DEL_ENTORNO:
        assert variable not in r.text, f"`/salud` publica que falta {variable}"


def test_salud_con_todo_puesto_dice_ok(cliente, monkeypatch):
    """La otra mitad, y hace falta: una prueba que solo mire el caso incompleto pasaría igual
    con un `configuracion` cableado a `"incompleta"`, y la clínica no se enteraría nunca de
    que ya está todo configurado."""
    monkeypatch.setattr(
        runtime,
        "config",
        _config_con(
            whatsapp_token="t",
            whatsapp_phone_number_id="1",
            whatsapp_verify_token="v",
            whatsapp_app_secret="s",
            telegram_bot_token="tg",
            telegram_chat_doctores="-100",
        ),
    )

    assert cliente.get("/salud").json()["configuracion"] == "ok"


def test_salud_no_publica_el_host_de_neon_cuando_la_base_falla(cliente, monkeypatch):
    """`FALLA` a secas, sin el texto de la excepción. Es la mitad de un par de credenciales.

    psycopg mete en el mensaje el host, el puerto y el usuario: «connection to server at
    "ep-….aws.neon.tech", port 5432 failed: FATAL: password authentication failed for user
    "…"». Eso, servido a cualquiera que pida la URL, y encima justo cuando el sistema está
    caído y nadie lo está mirando.

    Se comprueba contra el TEXTO CRUDO de la respuesta y no contra el campo: el día que
    alguien añada un `detalle` o un `error` al lado, el campo seguiría diciendo `FALLA` y la
    fuga estaría igual de abierta un renglón más abajo.
    """
    def explotar(_url):
        raise RuntimeError(
            'connection to server at "ep-secreto-12345.us-east-2.aws.neon.tech", port 5432 '
            'failed: FATAL: password authentication failed for user "maxicare_admin"'
        )

    monkeypatch.setattr(persistencia, "conectar", explotar)

    r = cliente.get("/salud")

    assert r.status_code == 200, "un healthcheck que devuelve 500 mata el contenedor"
    assert r.json()["base_de_datos"] == "FALLA"
    for filtrado in ("neon.tech", "maxicare_admin", "5432", "password"):
        assert filtrado not in r.text, f"`/salud` publica «{filtrado}» cuando la base falla"


def test_el_tema_general_sale_como_booleano_y_no_como_su_id(cliente, monkeypatch):
    """El id de un tema de Telegram no abre nada por sí solo --hace falta el token del bot--
    pero es una pieza más del mismo mapa, y aquí no la necesita nadie.

    Lo que se mira desde fuera es si el General quedó resuelto o no; quien necesita el número
    lo tiene en la configuración de la clínica.
    """
    monkeypatch.setattr(runtime, "_tema_general", 4242)

    r = cliente.get("/salud")

    assert r.json()["tema_general"] is True
    assert "4242" not in r.text, "`/salud` publica el id del tema General"


def test_sin_tema_general_se_ve_desde_fuera(cliente, monkeypatch):
    """Y el caso contrario, que es el que de verdad se consulta: un `tema_general` en falso
    significa que el relevo y los avisos no tienen dónde caer."""
    monkeypatch.setattr(runtime, "_tema_general", None)

    assert cliente.get("/salud").json()["tema_general"] is False


def test_el_tema_general_CERO_es_el_valor_normal_y_sale_como_verdadero(cliente, monkeypatch):
    """Esta prueba nació roja, en producción y no aquí.

    `0` no es «no hay tema»: es EL General. La migración 005 lo fijó así porque `sendMessage`
    con `message_thread_id=1` responde `message thread not found`, y al General se escribe
    omitiendo el campo --`canales.py` usa `if tema_id:` justamente para que el 0 lo omita--.

    O sea que `0` es el valor que tiene una clínica bien configurada, y las dos pruebas de
    arriba lo dejaban pasar: una usa `4242` y la otra `None`, así que un `bool(_tema_general)`
    quedaba verde en la suite mientras `/salud` decía `tema_general: false` sobre el
    despliegue sano del 21/09/2026. Y el daño no era el susto, sino que esa misma respuesta
    es la del `None`: el único caso que esta señal existe para delatar.
    """
    monkeypatch.setattr(runtime, "_tema_general", 0)

    r = cliente.get("/salud")

    assert r.json()["tema_general"] is True

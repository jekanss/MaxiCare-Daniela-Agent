"""Contraseñas y sesiones. Offline, sin base de datos y sin servidor.

Que esto se pueda probar así es el motivo de que `autenticacion.py` no importe FastAPI: un
token vencido se comprueba pasándole una hora, no esperando ocho horas.
"""

from __future__ import annotations

import time

import pytest

from maxicare_daniela.autenticacion import (
    DURACION_SESION_SEGUNDOS,
    MINIMO_CONTRASENA,
    MINIMO_SECRETO,
    ContrasenaInvalida,
    SecretoDebil,
    exigir_secreto,
    firmar_token,
    hash_contrasena,
    leer_token,
    verificar_contrasena,
)

SECRETO = "x" * MINIMO_SECRETO
OTRO_SECRETO = "y" * MINIMO_SECRETO
CLAVE = "una frase larga que si recuerdo"


# ==========================================================================================
# Contraseñas
# ==========================================================================================


def test_la_contrasena_correcta_se_reconoce():
    assert verificar_contrasena(CLAVE, hash_contrasena(CLAVE)) is True


def test_la_contrasena_equivocada_no():
    assert verificar_contrasena("otra cosa cualquiera", hash_contrasena(CLAVE)) is False


def test_el_hash_no_contiene_la_contrasena():
    """Lo que se guarda en Neon no debe permitir recuperar la contraseña ni por casualidad.

    Un `hash` que dejara pasar la cadena original convertiría un volcado de la base en una
    lista de contraseñas de la clínica."""
    guardado = hash_contrasena(CLAVE)
    assert CLAVE not in guardado
    for palabra in CLAVE.split():
        assert palabra not in guardado


def test_dos_hashes_de_la_misma_contrasena_son_distintos():
    """La sal, comprobada por su efecto y no por su existencia.

    Sin sal, dos personas con la misma contraseña tendrían el mismo hash, y quien viera la
    tabla sabría cuáles repetir. Además haría rentable una tabla precalculada."""
    assert hash_contrasena(CLAVE) != hash_contrasena(CLAVE)


def test_una_contrasena_corta_se_rechaza_al_crear():
    with pytest.raises(ContrasenaInvalida):
        hash_contrasena("a" * (MINIMO_CONTRASENA - 1))


def test_un_hash_corrupto_devuelve_false_y_no_revienta():
    """En la pantalla de ingreso, una excepción y un `False` se ven igual para quien entra,
    pero la excepción deja una traza y puede tumbar la petición. Una fila estropeada en la
    base es un ingreso fallido, no una caída."""
    for basura in ("", "no-es-un-hash", "scrypt$mal", "bcrypt$1$2$3$4$5", "scrypt$a$b$c$d$e"):
        assert verificar_contrasena(CLAVE, basura) is False


def test_los_parametros_viajan_dentro_del_hash():
    """Es lo que permite subir el costo mañana sin invalidar las contraseñas de ayer."""
    etiqueta, n, r, p, _sal, _clave = hash_contrasena(CLAVE).split("$")
    assert etiqueta == "scrypt"
    assert int(n) >= 2**14 and int(r) >= 8 and int(p) >= 1


# ==========================================================================================
# Sesiones
# ==========================================================================================


def test_un_token_recien_firmado_se_lee():
    assert leer_token(firmar_token("ana.rodriguez", secreto=SECRETO), secreto=SECRETO) == (
        "ana.rodriguez"
    )


def test_un_token_firmado_con_otro_secreto_se_rechaza():
    """Es la propiedad que sostiene todo el esquema: sin el secreto, nadie se firma una
    sesión. Y es lo que hace que rotarlo cierre todas las sesiones abiertas de golpe."""
    token = firmar_token("ana.rodriguez", secreto=OTRO_SECRETO)
    assert leer_token(token, secreto=SECRETO) is None


def test_un_token_manipulado_se_rechaza():
    """Cambiarle el usuario al payload sin poder recalcular la firma no sirve de nada."""
    token = firmar_token("recepcion", secreto=SECRETO)
    payload, firma = token.split(".")
    falso = f"{payload[:-2]}XY.{firma}"
    assert leer_token(falso, secreto=SECRETO) is None


def test_un_token_vencido_se_rechaza():
    token = firmar_token("ana.rodriguez", secreto=SECRETO, duracion=60)
    # Un segundo después de vencer. `ahora` existe justo para esto: la alternativa sería
    # esperar, y una prueba que espera es una prueba que alguien acaba saltándose.
    assert leer_token(token, secreto=SECRETO, ahora=time.time() + 61) is None
    assert leer_token(token, secreto=SECRETO, ahora=time.time() + 30) == "ana.rodriguez"


def test_los_tres_modos_de_fallo_devuelven_lo_mismo():
    """Firma mala, token vencido y basura: el mismo `None`.

    Si devolvieran valores distintos, alguien acabaría enseñando esa diferencia en la
    pantalla de ingreso, y «la firma no coincide» le confirma a quien lo intenta que el
    formato sí era el correcto."""
    vencido = firmar_token("ana", secreto=SECRETO, duracion=-1)
    otra_firma = firmar_token("ana", secreto=OTRO_SECRETO)
    for token in ("", "basura", "sin.punto.de.mas", vencido, otra_firma):
        assert leer_token(token, secreto=SECRETO) is None


def test_el_payload_es_legible_y_eso_es_deliberado():
    """Va firmado, NO cifrado. Quien tenga el token puede leer el usuario y el vencimiento.

    Esta prueba existe para que nadie meta ahí un dato sensible creyendo que va protegido."""
    import base64
    import json

    payload = firmar_token("ana.rodriguez", secreto=SECRETO).split(".")[0]
    datos = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert set(datos) == {"u", "exp"}, "no metas nada más en el token: se lee sin el secreto"
    assert datos["u"] == "ana.rodriguez"


def test_un_secreto_corto_se_rechaza_al_arrancar():
    """Falla al arrancar y no en el primer intento de ingreso, por la misma razón que
    `CalendarioGoogle` falla al construirse: un servidor que arranca «bien» y revienta cuando
    alguien intenta entrar parece sano en el monitor."""
    with pytest.raises(SecretoDebil):
        exigir_secreto("corto")
    assert exigir_secreto(SECRETO) == SECRETO


def test_la_duracion_por_defecto_es_un_turno_de_trabajo():
    """Ocho horas. Más larga alarga la ventana de un token robado; más corta echa a alguien
    a media mañana, y una herramienta que estorba se deja de usar."""
    assert DURACION_SESION_SEGUNDOS == 8 * 60 * 60


# ==========================================================================================
# El límite conocido, documentado por una prueba y no escondido en un comentario
# ==========================================================================================


def test_el_limite_conocido_un_token_no_se_puede_revocar():
    """**Cerrar sesión NO invalida el token.** Solo borra la cookie del navegador.

    Se acepta a conciencia: una tabla de sesiones costaría una consulta a Neon en cada
    petición. Las dos salidas de emergencia existen y son las que de verdad importan:

      1. `usuarios.activo` -- se comprueba en CADA petición, así que quitarle el acceso a
         alguien que se fue de la clínica es inmediato pese a que su token siga firmado.
      2. Rotar `MAXICARE_SECRETO_SESION` -- cierra todas las sesiones de golpe.

    Si algún día esto deja de ser aceptable, esta prueba es la que hay que cambiar, y su
    nombre dice exactamente qué se estaba asumiendo.
    """
    token = firmar_token("alguien.que.se.fue", secreto=SECRETO)
    # No hay ninguna función `revocar(token)`, y este `assert` es su ausencia hecha prueba.
    assert leer_token(token, secreto=SECRETO) == "alguien.que.se.fue"
    # La salida real: cambiar el secreto.
    assert leer_token(token, secreto=OTRO_SECRETO) is None

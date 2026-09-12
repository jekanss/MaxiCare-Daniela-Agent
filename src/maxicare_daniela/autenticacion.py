"""Quién entra a la interfaz web: contraseñas y sesiones. Funciones puras, sin framework.

Este módulo no importa FastAPI ni conoce una cookie: recibe y devuelve cadenas. `runtime.py`
es quien decide que esa cadena viaja en una cookie `HttpOnly`. La separación no es estética
--es lo que permite probar el vencimiento de una sesión y el rechazo de una firma falsa sin
levantar un servidor, que es donde estas dos cosas se prueban de verdad.

------------------------------------------------------------------------------------------
Cero dependencias nuevas, y es una decisión
------------------------------------------------------------------------------------------

`hashlib.scrypt` y `hmac` son biblioteca estándar de Python. Se descartaron:

* **`passlib` / `bcrypt`** -- una dependencia con rueda nativa (se compila distinto en
  Windows y en la imagen Debian del VPS) para hacer lo que `scrypt` ya hace. `scrypt` es
  además duro en memoria: un atacante con GPU no gana tanto como contra bcrypt.
* **`SessionMiddleware` de Starlette** -- arrastra `itsdangerous` para firmar exactamente
  el mismo HMAC-SHA256 que hay aquí abajo, y además mete el transporte dentro de la firma.

------------------------------------------------------------------------------------------
El límite que este diseño tiene, dicho aquí y probado en `tests/test_autenticacion.py`
------------------------------------------------------------------------------------------

Un token firmado **no se puede revocar desde el servidor** antes de que venza: la firma se
valida con matemática, no consultando una tabla. Cerrar sesión borra la cookie del navegador
del usuario, y nada más. Quien tuviera una copia del token podría seguir usándola hasta que
expire.

Se acepta a conciencia porque la alternativa --una tabla de sesiones-- cuesta una consulta a
Neon en CADA petición, y porque `DURACION_SESION_SEGUNDOS` es corta. La salida de emergencia
existe y está documentada: **rotar `MAXICARE_SECRETO_SESION` invalida todas las sesiones de
golpe**, que es justo lo que hace falta el día que alguien pierde un portátil.

La otra mitad de esa política vive en la base: `usuarios.activo`. `runtime.py` comprueba en
cada petición que el usuario del token siga activo, así que quitarle el acceso a alguien que
se fue de la clínica SÍ tiene efecto inmediato --que es el caso que de verdad importa-- pese
a que su token siga siendo criptográficamente válido.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

# ==========================================================================================
# Contraseñas
# ==========================================================================================

#: Parámetros de `scrypt`. `n` es el costo: subirlo encarece cada verificación y cada intento
#: de un atacante por igual. 2**14 con r=8 pide 16 MiB por hash, tarda unos 60 ms y cabe de
#: sobra en el límite de memoria por defecto de OpenSSL (32 MiB).
#:
#: Van escritos DENTRO de cada hash, no solo aquí: así subir el costo mañana no invalida las
#: contraseñas de ayer, porque cada una se verifica con los parámetros con los que se creó.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_LONGITUD_CLAVE = 32
_LONGITUD_SAL = 16

#: Longitud mínima. No hay reglas de «una mayúscula y un símbolo»: obligan a `Clinica2026!`,
#: que es corta y adivinable, en vez de a una frase larga, que no lo es.
MINIMO_CONTRASENA = 10


class ContrasenaInvalida(ValueError):
    """La contraseña no cumple el mínimo. Se lanza al CREAR, nunca al verificar."""


def _b64(crudo: bytes) -> str:
    return base64.urlsafe_b64encode(crudo).decode("ascii").rstrip("=")


def _de_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def hash_contrasena(contrasena: str) -> str:
    """Devuelve `scrypt$n$r$p$sal$hash`, todo en base64 sin relleno.

    El formato lleva sus propios parámetros a propósito -- ver el comentario de `_SCRYPT_N`.
    """
    if len(contrasena) < MINIMO_CONTRASENA:
        raise ContrasenaInvalida(
            f"la contraseña debe tener al menos {MINIMO_CONTRASENA} caracteres; "
            "una frase que recuerdes vale más que ocho caracteres con un símbolo"
        )
    sal = secrets.token_bytes(_LONGITUD_SAL)
    clave = hashlib.scrypt(
        contrasena.encode("utf-8"),
        salt=sal,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_LONGITUD_CLAVE,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(sal)}${_b64(clave)}"


def verificar_contrasena(contrasena: str, guardado: str) -> bool:
    """True si la contraseña corresponde al hash guardado.

    Nunca lanza: un hash corrupto en la base es un `False`, no una traza de error en el log
    de una pantalla de ingreso. Y compara con `compare_digest`, que tarda lo mismo acierte o
    falle: comparar con `==` filtra, por el tiempo de respuesta, cuántos bytes iniciales
    coincidían.
    """
    try:
        etiqueta, n, r, p, sal_b64, clave_b64 = guardado.split("$")
        if etiqueta != "scrypt":
            return False
        calculado = hashlib.scrypt(
            contrasena.encode("utf-8"),
            salt=_de_b64(sal_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_de_b64(clave_b64)),
        )
    except Exception:  # noqa: BLE001 -- ver docstring
        return False
    return hmac.compare_digest(calculado, _de_b64(clave_b64))


# ==========================================================================================
# Sesiones
# ==========================================================================================

#: Ocho horas: un turno de trabajo. Más largo alarga la ventana de un token robado; más
#: corto obliga a volver a entrar a media mañana, y una herramienta que estorba se deja de
#: usar.
DURACION_SESION_SEGUNDOS = 8 * 60 * 60

#: Longitud mínima del secreto de firma. Un secreto corto se puede romper por fuerza bruta
#: sobre un token capturado, y entonces cualquiera se firma la sesión que quiera.
MINIMO_SECRETO = 32


class SecretoDebil(ValueError):
    """El secreto de firma no llega al mínimo. Se comprueba al arrancar, no al primer login."""


def exigir_secreto(secreto: str) -> str:
    """Valida el secreto y lo devuelve. `runtime.py` la llama al arrancar.

    Falla al arrancar y no en el primer intento de ingreso por la misma razón que
    `CalendarioGoogle` falla al construirse: un servidor que arranca «bien» y revienta
    cuando alguien intenta entrar es un servidor que parece sano en el monitor.
    """
    if len(secreto) < MINIMO_SECRETO:
        raise SecretoDebil(
            f"MAXICARE_SECRETO_SESION debe tener al menos {MINIMO_SECRETO} caracteres "
            f"(tiene {len(secreto)}). Genera uno con: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return secreto


def firmar_token(usuario: str, *, secreto: str, duracion: int = DURACION_SESION_SEGUNDOS) -> str:
    """`payload.firma`, las dos partes en base64 sin relleno.

    El payload va firmado pero NO cifrado: cualquiera que tenga el token puede leer el
    nombre de usuario y la hora de vencimiento. Es deliberado -- no hay nada secreto ahí, y
    lo que la firma garantiza es que nadie pueda *cambiarlos*.
    """
    cuerpo = json.dumps(
        {"u": usuario, "exp": int(time.time()) + duracion},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload = _b64(cuerpo)
    firma = hmac.new(secreto.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(firma)}"


def leer_token(token: str, *, secreto: str, ahora: float | None = None) -> str | None:
    """El nombre de usuario si el token es válido y no ha vencido; `None` en cualquier otro caso.

    Un solo `None` para todos los fallos -- firma mala, token vencido, basura, cadena vacía --
    y es a propósito: distinguirlos en el valor de retorno acaba distinguiéndolos en el
    mensaje que ve quien intenta entrar, y «la firma no coincide» le confirma a un atacante
    que el formato sí era el correcto.

    La firma se verifica ANTES de mirar el vencimiento. Al revés, el vencimiento se leería de
    un payload que todavía no se sabe si alguien manipuló.
    """
    try:
        payload, firma_b64 = token.split(".")
        esperada = hmac.new(
            secreto.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(esperada, _de_b64(firma_b64)):
            return None
        datos = json.loads(_de_b64(payload))
        usuario = datos["u"]
        vence = float(datos["exp"])
    except Exception:  # noqa: BLE001 -- ver docstring
        return None

    if (time.time() if ahora is None else ahora) >= vence:
        return None
    return usuario if isinstance(usuario, str) and usuario else None

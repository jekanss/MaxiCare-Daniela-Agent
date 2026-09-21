"""El techo de lo que un solo número puede hacer gastar.

Offline y sin base: `persistencia.conectar` y las tres consultas que usa `cuotas` van
sustituidas con `monkeypatch`, que pytest deshace solo. Aquí no se abre ninguna conexión ni
se llama a ningún modelo.

Lo que estas pruebas sostienen son las tres reglas del módulo, y ninguna de las tres es
evidente leyendo el código:

* **El fallo es ABIERTO.** Si Postgres tiene un mal minuto, `revisar` deja pasar. Un freno
  que tumba turnos cuando la base no contesta convierte una defensa en la caída que
  pretendía evitar -- es la misma decisión que `guardrails._preguntar`, y por la misma razón.
* **El estado de `cuotas_avisadas` es el mismo se corte o no.** En modo observación el
  mensaje se permite, pero la marca se consulta igual. Si no se consultara, al apagar el modo
  observación el primer mensaje avisaría de más o de menos según el orden en que llegara.
* **El aviso al doctor tiene que decir si se cortó.** Los dos textos son distintos a
  propósito: un doctor que lee «se pasó» y da por hecho que el sistema ya lo frenó deja sin
  contestar a un paciente de verdad. Un aviso que se malinterpreta es peor que no avisar.

Ninguna prueba de aquí depende del valor por defecto de `CUOTA_MENSAJES_HORA`: todas
construyen su `Config` con un número propio, que es además lo que demuestra que el módulo lee
la configuración y no la constante.
"""

from __future__ import annotations

import asyncio

import pytest

from maxicare_daniela import cuotas, persistencia
from maxicare_daniela.config import Config


# ==========================================================================================
# Dobles
# ==========================================================================================


def config_falso(**cambios) -> Config:
    """Un `Config` de verdad, no un `SimpleNamespace`.

    Mismo criterio que `test_atencion.config_falso`: si mañana alguien le añade un campo
    obligatorio, estas pruebas lo dicen en vez de pasar con un objeto que ya no se parece al
    que corre en producción.
    """
    campos = dict(
        database_url="postgresql://no-se-usa/na",
        whatsapp_token="token-wa",
        whatsapp_phone_number_id="123",
        whatsapp_verify_token="verify",
        whatsapp_app_secret="secreto",
        telegram_bot_token="token-telegram",
        telegram_chat_doctores="-100999",
        google_sa_b64="",
        google_calendar_id="",
        modelo_daniela="modelo-de-prueba",
        modelo_lector="modelo-de-prueba",
        modelo_evaluador="modelo-de-prueba",
        secreto_sesion="",
        permitir_cookie_insegura=False,
        daniela_responde=True,
        politica_datos_url="",
    )
    campos.update(cambios)
    return Config(**campos)


class ConexionFalsa:
    """Sirve para el `with persistencia.conectar(...)` de `cuotas` y nada más."""

    def __init__(self) -> None:
        self.cerrada = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cerrada = True
        return False


class BaseFalsa:
    """Las tres consultas que `cuotas` le hace a `persistencia`, con memoria de lo pedido.

    Guarda las llamadas a `toca_avisar_cuota` porque varias pruebas de aquí afirman una
    AUSENCIA --«no se avisó»-- y una ausencia no distingue «se decidió no avisar» de «el
    camino reventó antes de llegar». Lo que se comprueba es qué se consultó, no solo qué
    salió.
    """

    def __init__(
        self, *, mensajes: int = 0, archivos: int = 0, toca: bool = True
    ) -> None:
        self.mensajes = mensajes
        self.archivos = archivos
        self.toca = toca
        self.conexiones: list[ConexionFalsa] = []
        self.marcas_consultadas: list[tuple] = []
        self.archivos_consultados: list[tuple] = []

    def instalar(self, monkeypatch) -> BaseFalsa:
        monkeypatch.setattr(persistencia, "conectar", self._conectar)
        monkeypatch.setattr(
            persistencia, "mensajes_en_la_ultima_hora", lambda conn, tel: self.mensajes
        )
        monkeypatch.setattr(persistencia, "toca_avisar_cuota", self._toca_avisar)
        monkeypatch.setattr(persistencia, "archivos_del_dia", self._archivos_del_dia)
        return self

    def _conectar(self, url):
        conexion = ConexionFalsa()
        self.conexiones.append(conexion)
        return conexion

    def _toca_avisar(self, conn, telefono, clase, *, ventana_horas=1):
        assert not conn.cerrada, "se consultó la marca con la conexión ya cerrada"
        self.marcas_consultadas.append((telefono, clase, ventana_horas))
        return self.toca

    def _archivos_del_dia(self, conn, telefono, tipos):
        self.archivos_consultados.append((telefono, tuple(tipos)))
        return self.archivos


class BaseCaida:
    """Postgres no contesta. Revienta en `conectar`, que es donde revienta de verdad."""

    def instalar(self, monkeypatch) -> BaseCaida:
        def explotar(_url):
            raise RuntimeError("connection to server failed")

        monkeypatch.setattr(persistencia, "conectar", explotar)
        return self


TELEFONO = "573001112233"
TIPOS = ("image", "document")


def revisar(telefono: str = TELEFONO, **cambios):
    """`cuotas.revisar` sin el `asyncio.run` a cuestas en cada prueba.

    Síncrona y con `asyncio.run` dentro, como el resto de la suite: el proyecto no tiene
    `pytest-asyncio` y añadirlo por unas pruebas sería una dependencia nueva para lo que una
    línea resuelve.
    """
    return asyncio.run(cuotas.revisar(telefono, config=config_falso(**cambios)))


# ==========================================================================================
# El caso normal: quien no se pasó ni se entera
# ==========================================================================================


@pytest.mark.parametrize("cuantos", [0, 1, 39, 40], ids=["ninguno", "uno", "casi", "justo"])
def test_por_debajo_del_limite_se_deja_pasar(monkeypatch, cuantos):
    """40 mensajes con la cuota en 40 TODAVÍA pasan: la comparación es `<=`.

    El borde se prueba a propósito. Un freno que muerde a un paciente real es peor que el
    ataque que evita, y el número del `.env` se lee como «hasta aquí puedes», no como «aquí
    te corto»: quien manda exactamente los mensajes que le dijeron que podía mandar no puede
    quedarse sin respuesta.
    """
    BaseFalsa(mensajes=cuantos).instalar(monkeypatch)

    veredicto = revisar(cuota_mensajes_hora=40)

    assert veredicto.permitido is True
    assert veredicto.clase is None
    assert veredicto.avisar is False


def test_a_quien_no_se_paso_ni_se_le_consulta_la_marca(monkeypatch):
    """La marca es un `INSERT ... ON CONFLICT`, o sea una ESCRITURA, y corre en el camino de
    cada mensaje que entra.

    Consultarla para quien no se pasó sería pagar una escritura por cada mensaje de la
    clínica para no usarla nunca. El `return` temprano de `_revisar` es lo que lo evita, y
    esto es lo que impide que alguien lo mueva sin darse cuenta.
    """
    base = BaseFalsa(mensajes=10).instalar(monkeypatch)

    revisar(cuota_mensajes_hora=40)

    assert base.marcas_consultadas == []


# ==========================================================================================
# Por encima: qué se corta, qué se dice y con qué número
# ==========================================================================================


def test_por_encima_del_limite_se_corta_y_el_veredicto_trae_el_numero_real(monkeypatch):
    """`cuantos` no es decorativo: es lo que hace accionable el aviso.

    «Lleva 47 mensajes en una hora» le dice al doctor si esto es una persona angustiada o un
    script; «se pasó de la cuota» no le dice nada. Por eso el número viaja en el `Veredicto`
    en vez de recalcularse donde se escribe el texto.
    """
    BaseFalsa(mensajes=47).instalar(monkeypatch)

    veredicto = revisar(cuota_mensajes_hora=40)

    assert veredicto.permitido is False
    assert veredicto.clase == "mensajes"
    assert veredicto.cuantos == 47
    assert veredicto.avisar is True


def test_la_marca_se_consulta_con_la_clase_y_la_ventana_de_una_hora(monkeypatch):
    """La clave de `cuotas_avisadas` es `(telefono, clase)`, y la ventana tiene que ser la
    misma que la de la cuenta.

    Si la cuenta mira una hora y la marca dos, un número que se pasa a las 10:00 y sigue
    pasándose a las 11:30 no genera ningún aviso durante la segunda hora: el doctor ve el
    primer episodio y ninguno más. Es el mismo agujero que el no negociable 26 ya pagó con
    los escalamientos, por el otro lado.
    """
    base = BaseFalsa(mensajes=99).instalar(monkeypatch)

    revisar(cuota_mensajes_hora=40)

    assert base.marcas_consultadas == [(TELEFONO, "mensajes", 1)]


def test_el_aviso_no_se_repite_dentro_de_la_ventana_pero_el_corte_sigue(monkeypatch):
    """Avisar una vez y cortar siempre son dos cosas distintas, y por eso son dos campos.

    Sin esto, un número que se pasa recibe la frase fija por CADA mensaje y el doctor un
    Telegram por cada uno: exactamente la inundación que la cuota existe para evitar, servida
    por la propia defensa. Lo que NO puede pasar es que callarse el aviso también levante el
    freno -- el mensaje 300 sigue sin abrir turno.
    """
    base = BaseFalsa(mensajes=200, toca=False).instalar(monkeypatch)

    veredicto = revisar(cuota_mensajes_hora=40)

    assert veredicto.avisar is False, "ya se avisó dentro de la ventana"
    assert veredicto.permitido is False, "callarse el aviso no es levantar el freno"
    assert base.marcas_consultadas, "no llegó a preguntar: la ausencia de aviso es un fallo"


# ==========================================================================================
# El modo observación
# ==========================================================================================


def test_en_modo_observacion_se_deja_pasar_pero_se_avisa_igual(monkeypatch):
    """Cuenta y avisa, pero no corta a nadie.

    Existe porque el 21/09/2026 nadie sabe todavía cuál es el uso normal de esta clínica: con
    4 conversaciones al día, cualquier umbral es una corazonada. Esto permite encender la
    medición hoy y el corte cuando haya datos para elegir el número.
    """
    BaseFalsa(mensajes=47).instalar(monkeypatch)

    veredicto = revisar(cuota_mensajes_hora=40, cuota_modo_observacion=True)

    assert veredicto.permitido is True, "el modo observación no corta a nadie"
    assert veredicto.clase == "mensajes", "pero sí registra que se pasó"
    assert veredicto.cuantos == 47
    assert veredicto.avisar is True


def test_el_estado_de_la_marca_es_EL_MISMO_se_corte_o_no(monkeypatch):
    """La prueba que justifica que la consulta de la marca esté FUERA del `if` del corte.

    Si en modo observación no se consultara, `cuotas_avisadas` quedaría en un estado distinto
    del que tendría con el corte encendido. Y entonces, el día que se apague el modo
    observación --que es todo el propósito del interruptor-- el primer mensaje que llegue
    avisaría de más o de menos según el orden: con la marca fresca se callaría un corte que el
    doctor tiene que ver, y sin marca ninguna avisaría de algo que ya se avisó hace un minuto.

    Calibrar un umbral solo sirve si al encender el corte no cambia nada más, y esto es lo
    que lo sostiene.
    """
    cortando = BaseFalsa(mensajes=47)
    cortando.instalar(monkeypatch)
    revisar(cuota_mensajes_hora=40, cuota_modo_observacion=False)

    observando = BaseFalsa(mensajes=47)
    observando.instalar(monkeypatch)
    revisar(cuota_mensajes_hora=40, cuota_modo_observacion=True)

    assert observando.marcas_consultadas == cortando.marcas_consultadas != []


# ==========================================================================================
# El fallo abierto
# ==========================================================================================


def test_si_la_base_revienta_se_deja_pasar(monkeypatch):
    """Regla 2 del módulo, y es la que más importa de las tres.

    Un freno que tumba turnos cuando Postgres tiene un mal minuto convierte una defensa en la
    caída que pretendía evitar: la clínica se queda sin contestarle a nadie por proteger el
    saldo de OpenAI. El fallo abierto está elegido, igual que en `guardrails._preguntar`.

    Y no propaga: `runtime` llama a esto sin `try`, así que una excepción que saliera de aquí
    se llevaría por delante el turno entero del paciente.
    """
    BaseCaida().instalar(monkeypatch)

    veredicto = revisar(cuota_mensajes_hora=40)

    assert veredicto.permitido is True
    assert veredicto.clase is None
    assert veredicto.avisar is False, "sin poder consultar la marca no se puede avisar una vez"


def test_si_la_base_revienta_los_archivos_se_leen_igual(monkeypatch):
    """Mismo criterio para la otra cuota, y aquí se nota más: el archivo ya le llegó al
    doctor, y lo único que esto decide es si además se lee con el modelo.

    Dejar de leer por un fallo de base sería degradar el servicio por una consulta de
    contabilidad.
    """
    BaseCaida().instalar(monkeypatch)

    assert asyncio.run(
        cuotas.puede_leer_archivos(TELEFONO, config=config_falso(), tipos=TIPOS)
    ) is True


# ==========================================================================================
# La cuota de archivos
# ==========================================================================================


@pytest.mark.parametrize(
    "cuantos,puede",
    [(0, True), (11, True), (12, False), (40, False)],
    ids=["ninguno", "casi", "justo", "muchos"],
)
def test_puede_leer_archivos_respeta_la_cuota_del_dia(monkeypatch, cuantos, puede):
    """Aquí la comparación es `<` y no `<=`, al revés que la de mensajes.

    No es un descuido: la cuota de archivos se lee como «se leen 12 al día», así que el
    número 12 es el primero que ya no entra. La de mensajes se lee como «hasta 40 por hora».
    La asimetría queda fijada aquí para que nadie la «arregle» en el sentido equivocado y
    mueva un límite de producción sin querer.
    """
    BaseFalsa(archivos=cuantos).instalar(monkeypatch)

    resultado = asyncio.run(
        cuotas.puede_leer_archivos(
            TELEFONO, config=config_falso(cuota_archivos_dia=12), tipos=TIPOS
        )
    )

    assert resultado is puede


def test_los_tipos_que_cuentan_los_pone_QUIEN_LLAMA_y_no_la_cuota(monkeypatch):
    """La lista de lo que el lector sabe leer vive en `lectura.TIPOS_QUE_SE_LEEN` y es suya.

    Duplicarla dentro de la cuota significaría que el día que el lector aprenda un tipo nuevo,
    la cuota siga contando los de antes -- y nadie lo notaría, porque el fallo es que un freno
    deja de frenar.
    """
    base = BaseFalsa(archivos=0).instalar(monkeypatch)

    asyncio.run(
        cuotas.puede_leer_archivos(TELEFONO, config=config_falso(), tipos=("sticker",))
    )

    assert base.archivos_consultados == [(TELEFONO, ("sticker",))]


# ==========================================================================================
# El texto que lee el doctor
# ==========================================================================================


def test_el_aviso_en_modo_observacion_dice_que_NO_se_corto_nada():
    """El doctor lee «el número 57300... lleva 47 mensajes en una hora» y, si el texto no
    sigue, concluye lo razonable: que el sistema ya lo frenó.

    En modo observación eso es falso -- Daniela le está contestando con normalidad-- y la
    consecuencia es que nadie llama a un paciente que sí está esperando. El aviso tiene que
    decir explícitamente cuál de los dos mundos es.
    """
    veredicto = cuotas.Veredicto(permitido=True, clase="mensajes", avisar=True, cuantos=47)

    texto = cuotas.aviso_para_el_doctor(TELEFONO, veredicto, corto=True)

    assert "47" in texto and TELEFONO in texto
    assert "sigue respondiendo" in texto
    assert "dejó de responder" not in texto


def test_el_aviso_con_corte_dice_que_daniela_dejo_de_responder():
    """Y el otro lado del mismo texto: cuando sí se cortó, hay un paciente esperando.

    La frase que recibió es «ya le avisé al doctor: te escribe en un momento», así que el
    aviso tiene que dejar claro que esa promesa está hecha en nombre de alguien. Un aviso que
    se malinterpreta es peor que no avisar.
    """
    veredicto = cuotas.Veredicto(permitido=False, clase="mensajes", avisar=True, cuantos=47)

    texto = cuotas.aviso_para_el_doctor(TELEFONO, veredicto, corto=False)

    assert "47" in texto and TELEFONO in texto
    assert "dejó de responder" in texto
    assert "hay que contestarle" in texto


def test_la_frase_al_paciente_no_le_habla_de_cuotas_ni_de_limites():
    """Quien está del otro lado casi siempre es una persona con prisa, no un atacante.

    «Superaste el límite de mensajes» es la clase de frase que hace que alguien con dolor se
    vaya a otra clínica, y encima delata el mecanismo a quien sí esté probando. La frase dice
    la verdad --el doctor recibe el aviso en el mismo turno-- sin nombrar el freno.
    """
    frase = cuotas.FRASE_DE_CUOTA.lower()

    for palabra in ("cuota", "límite", "limite", "bloque", "spam"):
        assert palabra not in frase, f"la frase al paciente nombra el mecanismo: {palabra!r}"
    assert "doctor" in frase, "promete que alguien le escribe, que es lo que de verdad pasa"

"""El ciclo completo del tema, de punta a punta y en orden.

    Daniela escala -> se crea el tema -> el doctor lo toma -> el doctor lo CIERRA
    -> llegan mensajes nuevos SIN notificaciones -> Daniela vuelve a escalar
    -> se rehace el tema y sus notificaciones, sin duplicados y sin nada en el General.

Es la validacion que pidio MaxiCare el 22/09/2026, escrita como una sola prueba larga en vez
de como diez cortas, y eso es deliberado: **lo que fallaba no era ningun paso, era la
transicion entre dos de ellos**. Las pruebas de `test_ingesta.py` y `test_relevo.py` cubren
cada pieza por separado y todas estaban en verde mientras el sistema hacia esto:

    18:21  el doctor pierde el hilo de un paciente
    19:13  la nota de voz de ese paciente cae en el General, sonando

Aqui hay UN doble de Telegram para todo el ciclo, que anota cada envio con su tema y con si
sono. Esa lista es la prueba: al final se lee entera y tiene que decir exactamente lo que el
doctor deberia haber visto en su celular.

Sin red y sin base: los tres modulos que cruzan el ciclo --`ingesta`, `lectura` y `relevo`--
comparten un `Grupo` y una `Base` de mentira.
"""

from __future__ import annotations

import asyncio

import pytest

from maxicare_daniela import ingesta, lectura, relevo
from maxicare_daniela.canales import ArchivoDescargado

TEL = "573001112233"
TEMA_GENERAL = 0


class Grupo:
    """El supergrupo de los doctores. Anota (que, tema, sono) de cada envio."""

    def __init__(self) -> None:
        self.envios: list[tuple[str, int | None, bool]] = []
        self.temas_creados: list[str] = []
        self.cerrados: list[int] = []
        self.desanclados: list[int] = []
        # Arranca lejos del 901 que la prueba siembra a mano: un tema nuevo con el mismo
        # id que el viejo haria pasar por bueno justo el duplicado que se vigila.
        self._siguiente = 950

    # --- lo que mira la prueba -----------------------------------------------------------
    @property
    def al_general(self) -> list[str]:
        return [que for que, tema, _ in self.envios if not tema]

    @property
    def sonaron(self) -> list[tuple[str, int | None]]:
        return [(que, tema) for que, tema, sono in self.envios if sono]

    def en_el_tema(self, tema: int) -> list[str]:
        return [que for que, t, _ in self.envios if t == tema]

    # --- la interfaz de `canales.Telegram` que tocan los tres modulos ---------------------
    async def crear_tema(self, nombre: str) -> int:
        self.temas_creados.append(nombre)
        self._siguiente += 1
        return self._siguiente

    async def cerrar_tema(self, tema_id: int) -> None:
        self.cerrados.append(tema_id)

    async def reabrir_tema(self, tema_id: int) -> None:
        pass

    async def desanclar_todo_del_tema(self, tema_id: int) -> None:
        self.desanclados.append(tema_id)

    async def enviar_mensaje(self, texto, *, tema_id=None, teclado=None, silencioso=False):
        self.envios.append((texto, tema_id, not silencioso))
        return 5000 + len(self.envios)

    async def enviar_archivo(self, archivo, *, tipo_whatsapp, pie, tema_id=None,
                             silencioso=False):
        self.envios.append((f"[archivo {archivo.nombre}]", tema_id, not silencioso))
        return 6000 + len(self.envios)


class Base:
    """Las cuatro cosas que el ciclo guarda de verdad. Todo lo demas se dobla a `None`."""

    def __init__(self) -> None:
        #: `temas_telegram`: teléfono -> (topic_id, perdido). La LAPIDA de la 030 es el
        #: segundo campo, y este doble la modela en vez de borrar la entrada -- que es
        #: exactamente el cambio que la migracion hizo en Postgres.
        self.temas: dict[str, tuple[int, bool]] = {}
        self.en_relevo: set[str] = set()
        self.mensajes: list[dict] = []

    # --- `temas_telegram` ---------------------------------------------------------------
    def tema_vivo(self, telefono: str) -> int | None:
        fila = self.temas.get(telefono)
        return None if fila is None or fila[1] else fila[0]

    def perdido(self, telefono: str) -> bool:
        fila = self.temas.get(telefono)
        return bool(fila and fila[1])

    def guardar(self, telefono: str, topic_id: int) -> None:
        self.temas[telefono] = (topic_id, False)

    def olvidar(self, telefono: str) -> bool:
        fila = self.temas.get(telefono)
        if fila is None or fila[1]:
            return False
        self.temas[telefono] = (fila[0], True)
        return True

    # --- `mensajes_entrantes` -----------------------------------------------------------
    def sin_archivar(self, telefono: str) -> list[tuple]:
        return [
            (m["wamid"], m["texto"], m["transcripcion"], m["tipo"], None)
            for m in self.mensajes
            if m["telefono"] == telefono and m["telegram_message_id"] is None
        ]

    def marcar_archivados(self, wamids: list[str], message_id: int) -> None:
        for m in self.mensajes:
            if m["wamid"] in wamids:
                m["telegram_message_id"] = message_id


@pytest.fixture
def mundo(monkeypatch):
    base = Base()
    grupo = Grupo()

    # --- `lectura`: el hilo, y el volcado del rescate ------------------------------------
    monkeypatch.setattr(
        lectura, "_estado_del_hilo",
        lambda url, tel: (base.tema_vivo(tel), base.perdido(tel)),
    )
    monkeypatch.setattr(
        lectura, "_guardar_tema",
        lambda url, tel, tema, abierto=False: base.guardar(tel, tema),
    )
    monkeypatch.setattr(lectura, "_textos_sin_archivar", lambda url, tel: base.sin_archivar(tel))
    monkeypatch.setattr(
        lectura, "_marcar_archivados",
        lambda url, wamids, mid: base.marcar_archivados(wamids, mid),
    )
    # El lector y el transcriptor no son de esta prueba: lo que se mide es DONDE cae cada
    # cosa, no que el modelo acierte.
    monkeypatch.setattr(lectura, "leer_y_repartir", _nada)
    monkeypatch.setattr(ingesta.transcripcion_mod, "transcribir_y_repartir", _nada)

    # --- `ingesta`: el registro y el destino ---------------------------------------------
    monkeypatch.setattr(ingesta, "_registrar", lambda url, m: base.mensajes.append(_fila(m)) or True)
    monkeypatch.setattr(ingesta, "_marcar_reenviado", _marcar(base))
    monkeypatch.setattr(ingesta, "_marcar_fallo", lambda url, w, e: None)
    monkeypatch.setattr(ingesta, "_conversacion_viva", lambda url, tel: None)
    monkeypatch.setattr(ingesta, "_tema_existente", lambda url, tel: base.tema_vivo(tel))
    monkeypatch.setattr(ingesta, "_en_relevo", lambda url, tel: tel in base.en_relevo)
    monkeypatch.setattr(ingesta, "_olvidar_tema", lambda url, tel: base.olvidar(tel))
    monkeypatch.setattr(ingesta, "_guardar_transcripcion", lambda url, w, t: None)

    # --- `relevo`: el cierre ---------------------------------------------------------------
    monkeypatch.setattr(relevo, "_telefono", lambda url, conv: TEL)
    monkeypatch.setattr(relevo, "_tema_de", lambda url, tel: base.tema_vivo(tel))
    monkeypatch.setattr(
        relevo, "_marcar_abierto", lambda url, tel, abierto: None
    )
    monkeypatch.setattr(relevo, "_olvidar_tema", lambda url, tel: base.olvidar(tel))
    monkeypatch.setattr(relevo, "_marcar_cierre", lambda url, conv, estado: None)
    monkeypatch.setattr(relevo, "_marcar_escalamientos_respondidos", lambda url, conv: 1)
    monkeypatch.setattr(
        relevo, "_cerrar_en_base",
        lambda url, conv, motivo: bool(base.en_relevo.discard(TEL) or True),
    )
    monkeypatch.setattr(
        relevo, "_relevo_de_tema",
        lambda url, tema: (
            {"id_conversacion": "conv-1", "telefono": TEL, "doctor": "Dra. Ruiz"}
            if TEL in base.en_relevo and base.tema_vivo(TEL) == tema
            else None
        ),
    )
    monkeypatch.setattr(relevo, "_avisar_a_daniela", _nada_async)

    return base, grupo


async def _nada(*a, **kw):
    return None


async def _nada_async(*a, **kw):
    return None


def _fila(m) -> dict:
    return {
        "wamid": m.wamid, "telefono": m.telefono, "tipo": m.tipo, "texto": m.texto,
        "transcripcion": None, "telegram_message_id": None,
    }


def _marcar(base: Base):
    def _fn(url, wamid, telegram_message_id, tamano):
        for m in base.mensajes:
            if m["wamid"] == wamid:
                m["telegram_message_id"] = telegram_message_id
    return _fn


class WhatsApp:
    async def descargar_media(self, media_id, *, nombre_original=None):
        return ArchivoDescargado(contenido=b"x" * 2048, mime="image/jpeg", nombre="radio.jpg")


def _mensaje(wamid, *, tipo="text", texto=None, media_id=None):
    return ingesta.MensajeEntrante(
        wamid=wamid, telefono=TEL, nombre_perfil="Ana Perez",
        tipo=tipo, texto=texto, media_id=media_id, mime="image/jpeg",
    )


def _entra(mensaje, grupo) -> None:
    asyncio.run(
        ingesta.procesar_mensaje(
            mensaje, whatsapp=WhatsApp(), telegram=grupo, database_url="postgresql://x"
        )
    )


# ==========================================================================================


def test_el_ciclo_completo_del_tema(mundo):
    base, grupo = mundo

    # ── 1. Ana ya tiene su hilo y un doctor la tomo ─────────────────────────────────────
    base.guardar(TEL, 901)
    base.en_relevo.add(TEL)

    _entra(_mensaje("w-1", texto="gracias, ahi voy"), grupo)
    assert grupo.sonaron == [("[mensaje]", 901)] or grupo.en_el_tema(901), (
        "durante el relevo el hilo esta en vivo: lo del paciente tiene que sonar ahi"
    )
    assert grupo.al_general == [], "ni en relevo llega nada al General"
    durante_el_relevo = [sono for _, tema, sono in grupo.envios if tema == 901]
    assert durante_el_relevo == [True], "en relevo el mensaje del paciente SUENA en su hilo"

    # ── 2. El doctor CIERRA el tema ──────────────────────────────────────────────────────
    asyncio.run(
        relevo.cerrar_por_tema_cerrado(
            {"message_thread_id": 901},
            telegram=grupo, database_url="postgresql://x", tema_general=TEMA_GENERAL,
        )
    )
    assert TEL not in base.en_relevo, "el relevo tiene que quedar cerrado"
    assert base.tema_vivo(TEL) == 901, (
        "cerrar el tema NO puede perder el hilo: sigue siendo el expediente de Ana"
    )
    assert grupo.al_general == [], "cerrar no avisa al General"

    # ── 3. Ana sigue escribiendo. NADA suena ─────────────────────────────────────────────
    antes = len(grupo.envios)
    _entra(_mensaje("w-2", texto="una ultima cosa"), grupo)
    _entra(_mensaje("w-3", tipo="image", media_id="media-1"), grupo)
    _entra(_mensaje("w-4", tipo="audio", media_id="media-2"), grupo)

    posteriores = grupo.envios[antes:]
    assert posteriores, "los mensajes tienen que seguir archivandose en su hilo"
    assert all(tema == 901 for _, tema, _ in posteriores), (
        f"algo se fue del hilo de Ana: {posteriores}"
    )
    assert not any(sono for _, _, sono in posteriores), (
        f"algo sono despues de cerrar el tema: {[q for q, _, s in posteriores if s]}"
    )
    assert grupo.al_general == [], f"llego algo al General: {grupo.al_general}"

    # ── 4. Y ahora el doctor BORRA el tema ───────────────────────────────────────────────
    base.olvidar(TEL)
    assert base.tema_vivo(TEL) is None and base.perdido(TEL) is True

    antes = len(grupo.envios)
    temas_antes = len(grupo.temas_creados)
    _entra(_mensaje("w-5", texto="hola?"), grupo)
    _entra(_mensaje("w-6", tipo="image", media_id="media-3"), grupo)

    assert grupo.envios[antes:] == [], (
        f"con el tema borrado no puede salir nada a Telegram: {grupo.envios[antes:]}"
    )
    assert grupo.al_general == [], "y menos al General"
    assert len(grupo.temas_creados) == temas_antes, (
        "un mensaje del paciente rehizo el tema que el doctor borro: el gesto no sirvio "
        "de nada y ahora hay dos temas para la misma persona"
    )

    # ── 5. Daniela vuelve a escalar: el hilo se rehace y se vuelca lo acumulado ──────────
    tema = asyncio.run(
        lectura.rescatar_hilo(
            telefono=TEL, nombre_perfil="Ana Perez",
            database_url="postgresql://x", telegram=grupo,
        )
    )
    assert tema is not None, "el escalamiento TIENE que poder rehacer el hilo"
    assert len(grupo.temas_creados) == temas_antes + 1, "y exactamente uno, sin duplicados"
    assert base.tema_vivo(TEL) == tema and base.perdido(TEL) is False, (
        "guardar el hilo nuevo tiene que levantar la lapida"
    )

    volcado = grupo.en_el_tema(tema)
    assert len(volcado) == 1, f"un solo mensaje con todo, no uno por frase: {volcado}"
    assert "hola?" in volcado[0], "lo que escribio mientras no habia hilo"
    assert "una imagen" in volcado[0], (
        f"la radiografia no dejo constancia en el expediente: {volcado[0]!r}"
    )

    # ── 6. Y la puerta, que es lo unico que vuelve a sonar ──────────────────────────────
    asyncio.run(
        relevo.ofrecer_la_puerta_en_el_hilo(
            telegram=grupo, tema=tema, telefono=TEL, motivo="clinico"
        )
    )
    puerta = grupo.envios[-1]
    assert "pidió ayuda" in puerta[0] and puerta[1] == tema
    assert puerta[2] is True, "la puerta del escalamiento SUENA: es para lo que existe"
    assert grupo.al_general == [], (
        "el General no recibio ni una linea en todo el ciclo. Lo unico que le toca son las "
        "alertas de escalamiento, y esas las manda `runtime._avisar_a_doctores`"
    )

    # ── 7. Y no se repite ────────────────────────────────────────────────────────────────
    cuantos = len(grupo.envios)
    asyncio.run(
        lectura.rescatar_hilo(
            telefono=TEL, nombre_perfil="Ana Perez",
            database_url="postgresql://x", telegram=grupo,
        )
    )
    assert len(grupo.envios) == cuantos, (
        "el segundo escalamiento repitio el volcado: lo ya archivado no vuelve a bajar"
    )
    assert len(grupo.temas_creados) == temas_antes + 1, "ni creo otro tema"

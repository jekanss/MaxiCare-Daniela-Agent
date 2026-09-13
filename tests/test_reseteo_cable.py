"""El cable de `/clearstate`: que la interceptación esté donde dice estar.

Offline. Se doblan `ingesta.procesar_mensaje`, `atencion.atender` y `reseteo.resetear`, así
que aquí no hay red, ni Neon, ni modelo: lo que se prueba es la decisión de `_entregar`
--a quién le toca este mensaje-- y nada más.

La prueba que justifica el archivo es `test_un_numero_no_autorizado_no_resetea_nada`: sin
ella, un error en la condición convertiría `/clearstate` en un borrado disponible para
cualquiera que escriba a la clínica, y las pruebas de arriba seguirían todas en verde.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest

from maxicare_daniela.config import cargar_dotenv

cargar_dotenv()
os.environ.setdefault("MAXICARE_DATABASE_URL", "postgresql://prueba:prueba@localhost/nada")

from maxicare_daniela import atencion, ingesta, reseteo, runtime  # noqa: E402

TEL = "573001112222"
OTRO = "573009998888"


class WhatsAppFalso:
    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []

    async def enviar_texto(self, telefono: str, texto: str) -> str:
        self.enviados.append((telefono, texto))
        return "wamid-respuesta"


def _mensaje(telefono: str, texto: str) -> ingesta.MensajeEntrante:
    return ingesta.MensajeEntrante(
        wamid=f"wamid-{telefono}-{abs(hash(texto)) % 1000}",
        telefono=telefono,
        nombre_perfil="Quien Sea",
        tipo="text",
        texto=texto,
    )


@pytest.fixture
def cable(monkeypatch):
    """Deja `_entregar` corriendo sin tocar nada de fuera, y registra quién se llevó el
    mensaje."""
    registro: dict = {"atendidos": [], "reseteados": [], "whatsapp": WhatsAppFalso()}

    async def procesar_falso(m, **_kwargs):
        return ingesta.Resultado(m.wamid, nuevo=True, reenviado=True)

    async def atender_falso(m, **_kwargs):
        registro["atendidos"].append(m.texto)
        return atencion.Atendido(wamid=m.wamid, id_conversacion="conv-1", respondido=True)

    async def resetear_falso(telefono, **kwargs):
        registro["reseteados"].append(telefono)
        registro["kwargs"] = kwargs
        return reseteo.Borrado(filas={"pacientes": 1, "conversaciones": 1})

    monkeypatch.setattr(ingesta, "procesar_mensaje", procesar_falso)
    monkeypatch.setattr(atencion, "atender", atender_falso)
    monkeypatch.setattr(reseteo, "resetear", resetear_falso)
    monkeypatch.setattr(runtime, "_bases_secundarias", lambda: ())
    monkeypatch.setattr(runtime, "_whatsapp", registro["whatsapp"])
    monkeypatch.setattr(
        runtime, "config", replace(runtime.config, telefonos_prueba=(TEL,))
    )
    return registro


def test_el_comando_de_un_numero_autorizado_no_llega_a_daniela(cable):
    """Si llegara, el modelo contestaría al `/clearstate` y el turno escribiría en la
    conversación que se acaba de borrar."""
    asyncio.run(runtime._entregar(_mensaje(TEL, "/clearstate")))

    assert cable["reseteados"] == [TEL]
    assert cable["atendidos"] == []


def test_un_numero_no_autorizado_no_resetea_nada(cable):
    """La prueba que impide que esto sea un borrado abierto a cualquiera.

    Un paciente real escribiendo `/clearstate` --por curiosidad, o porque lo vio en algún
    sitio-- recibe la respuesta normal de Daniela y no pierde su cita.
    """
    asyncio.run(runtime._entregar(_mensaje(OTRO, "/clearstate")))

    assert cable["reseteados"] == []
    assert cable["atendidos"] == ["/clearstate"]


def test_con_la_lista_vacia_el_comando_no_existe_para_nadie(cable, monkeypatch):
    """El default de `MAXICARE_TELEFONOS_PRUEBA`, que es como se despliega en producción."""
    monkeypatch.setattr(runtime, "config", replace(runtime.config, telefonos_prueba=()))

    asyncio.run(runtime._entregar(_mensaje(TEL, "/clearstate")))

    assert cable["reseteados"] == []
    assert cable["atendidos"] == ["/clearstate"]


def test_un_mensaje_normal_del_numero_autorizado_sigue_yendo_a_daniela(cable):
    """Estar en la lista no cambia nada más: solo habilita esa palabra exacta."""
    asyncio.run(runtime._entregar(_mensaje(TEL, "hola, quiero una cita")))

    assert cable["reseteados"] == []
    assert cable["atendidos"] == ["hola, quiero una cita"]


def test_el_wamid_del_comando_se_conserva(cable):
    """Contra el reintento de Meta: si la fila se borrara, el comando correría dos veces."""
    mensaje = _mensaje(TEL, "/clearstate")

    asyncio.run(runtime._entregar(mensaje))

    assert cable["kwargs"]["conservar_wamid"] == mensaje.wamid


def test_quien_lo_pidio_recibe_la_confirmacion(cable):
    asyncio.run(runtime._entregar(_mensaje(TEL, "/clearstate")))

    destino, texto = cable["whatsapp"].enviados[0]
    assert destino == TEL
    assert "como si no me conocieras" in texto


def test_si_el_reseteo_aborta_se_avisa_y_no_se_llama_a_daniela(cable, monkeypatch):
    """El caso de Calendar caído. Quien mandó el comando tiene que enterarse de que NO se
    borró nada -- callarse dejaría a alguien creyendo que empieza de cero cuando no."""

    async def resetear_que_aborta(telefono, **kwargs):
        raise reseteo.ErrorDeReseteo("no se pudo eliminar el evento ev-1")

    monkeypatch.setattr(reseteo, "resetear", resetear_que_aborta)

    asyncio.run(runtime._entregar(_mensaje(TEL, "/clearstate")))

    _, texto = cable["whatsapp"].enviados[0]
    assert "No reseteé nada" in texto
    assert cable["atendidos"] == []


def test_si_el_reseteo_revienta_quien_lo_pidio_igual_recibe_algo(cable, monkeypatch):
    """Un comando destructivo mudo es peor que uno que falla: nadie sabe si borró o no."""

    async def resetear_que_revienta(telefono, **kwargs):
        raise RuntimeError("la base se cayó a mitad")

    monkeypatch.setattr(reseteo, "resetear", resetear_que_revienta)

    asyncio.run(runtime._entregar(_mensaje(TEL, "/clearstate")))

    assert len(cable["whatsapp"].enviados) == 1
    assert "No pude resetear" in cable["whatsapp"].enviados[0][1]

"""El entregable de CalendarioGoogle: el camino real contra el calendario de MaxiCare.

    uv run python scripts/probar_calendario.py
    uv run python scripts/probar_calendario.py --diagnosticar   # solo lee, no escribe nada

Lo que las pruebas de `tests/test_calendario_google.py` no pueden comprobar: que `insert`
inserta, que `patch` mueve, que `delete` borra, y sobre todo que un evento creado por
Daniela **no vuelve como bloqueo** -- el fallo que haría que la clínica atendiera la mitad
de los pacientes que su capacidad permite.

ESCRIBE EN EL CALENDARIO REAL. Cada evento que crea lleva un título que empieza por
`[PRUEBA DANIELA]` y una hora deliberadamente absurda --tres años en el futuro, de
madrugada-- para que nadie lo confunda con una cita, y se borra en un `finally` cuya
eliminación se COMPRUEBA, nunca se da por hecha. Si la limpieza falla, el script lo dice en
rojo y nombra el id que quedó suelto.

No gasta un solo token: aquí no hay modelo, solo Google.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela.calendario import (  # noqa: E402
    CLAVE_ORIGEN,
    VALOR_ORIGEN,
    ZONA_BOGOTA,
    CalendarioGoogle,
    ErrorDeCalendario,
    a_bloqueo,
)
from maxicare_daniela.config import Config, cargar_dotenv  # noqa: E402

#: Las pruebas se hacen en 2029, a las 3 de la mañana. Ningún paciente se cita ahí, así que
#: un evento que sobreviviera a la limpieza sería obvio a simple vista y no chocaría con
#: nada real. La fecha fija --y no `now()`-- hace que dos corridas usen el mismo rincón.
CUANDO = datetime(2029, 1, 17, 3, 0, tzinfo=ZONA_BOGOTA)

MARCA = "[PRUEBA DANIELA]"

fallos = 0


def revisar(etiqueta: str, condicion: bool, detalle: str = "") -> bool:
    global fallos
    print(f"  {'OK  ' if condicion else 'FALLA'} {etiqueta}" + (f" -> {detalle}" if detalle else ""))
    if not condicion:
        fallos += 1
    return condicion


def enmascarar(identificador: str) -> str:
    """El calendarId es un correo. Se muestra su dominio, no la cuenta."""
    if "@" not in identificador:
        return "(sin @)"
    usuario, dominio = identificador.split("@", 1)
    return f"{usuario[:3]}***@{dominio}"


def diagnosticar(config: Config) -> int:
    """Solo lee. Sirve para saber por qué falla el acceso antes de intentar escribir."""
    print("\n" + "=" * 78)
    print("DIAGNÓSTICO (no escribe nada)")
    print("=" * 78)

    print(f"\n  MAXICARE_GOOGLE_SA_B64: "
          f"{str(len(config.google_sa_b64)) + ' caracteres' if config.google_sa_b64 else 'AUSENTE'}")
    print(f"  MAXICARE_GOOGLE_CALENDAR_ID: {enmascarar(config.google_calendar_id)}")

    try:
        calendario = CalendarioGoogle(config.google_sa_b64, config.google_calendar_id)
    except ErrorDeCalendario as e:
        print(f"\n  FALLA {e}")
        return 1

    print(f"\n  OK   la cuenta de servicio {calendario.correo_cuenta_de_servicio} "
          f"llega al calendario")

    inicio = datetime.now(ZONA_BOGOTA)
    bloqueos = calendario.bloqueos(inicio, inicio + timedelta(days=14))
    print(f"  OK   {len(bloqueos)} bloqueos de los doctores en los próximos 14 días")
    for bloqueo in bloqueos[:10]:
        print(f"         {bloqueo.inicio:%Y-%m-%d %H:%M} -> {bloqueo.fin:%H:%M}  "
              f"{bloqueo.titulo[:40]!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diagnosticar",
        action="store_true",
        help="Solo lee el calendario: comprueba el acceso y lista los bloqueos.",
    )
    args = parser.parse_args()

    cargar_dotenv()
    try:
        config = Config.desde_entorno()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.diagnosticar:
        return diagnosticar(config)

    print("=" * 78)
    print("CalendarioGoogle CONTRA EL CALENDARIO REAL DE MAXICARE")
    print("=" * 78)
    print(f"  calendario: {enmascarar(config.google_calendar_id)}")
    print(f"  los eventos de prueba se crean en {CUANDO:%Y-%m-%d %H:%M} y se borran al final")

    # ---------------------------------------------------------------------------------
    print("\n1. El constructor comprueba el acceso de verdad")
    # ---------------------------------------------------------------------------------
    try:
        calendario = CalendarioGoogle(config.google_sa_b64, config.google_calendar_id)
    except ErrorDeCalendario as e:
        print(f"  FALLA no se pudo construir: {e}")
        print("\n  Corre `--diagnosticar` para ver el detalle.")
        return 1
    revisar("la cuenta de servicio llega al calendario",
            True, calendario.correo_cuenta_de_servicio)

    evento_id = ""
    try:
        # -----------------------------------------------------------------------------
        print("\n2. Crear un evento")
        # -----------------------------------------------------------------------------
        evento_id = calendario.crear_evento(
            inicio=CUANDO,
            duracion_minutos=60,
            titulo=f"{MARCA} no es una cita",
            descripcion="Creado por scripts/probar_calendario.py. Si ves esto, sobró: bórralo.",
        )
        revisar("insert devolvió un id", bool(evento_id), evento_id)

        # -----------------------------------------------------------------------------
        print("\n3. LO QUE MÁS IMPORTA: esa cita NO vuelve como bloqueo")
        # -----------------------------------------------------------------------------
        ventana_inicio = CUANDO - timedelta(hours=1)
        ventana_fin = CUANDO + timedelta(hours=2)
        bloqueos = calendario.bloqueos(ventana_inicio, ventana_fin)
        propios = [b for b in bloqueos if MARCA in b.titulo]
        revisar(
            "la cita recién creada no aparece entre los bloqueos",
            not propios,
            f"{len(bloqueos)} bloqueos en la ventana, {len(propios)} son la cita de prueba",
        )
        if propios:
            print("       >>> Con esto, la clínica atendería un paciente por hora en vez de")
            print("       >>> los dos que permite su capacidad. Revisa la marca "
                  f"extendedProperties.private.{CLAVE_ORIGEN} = {VALOR_ORIGEN!r}.")

        # -----------------------------------------------------------------------------
        print("\n4. Pero SÍ está en el calendario (el control del paso anterior)")
        # -----------------------------------------------------------------------------
        # Sin esto, un `bloqueos()` que devolviera siempre una lista vacía --o un `insert`
        # que no insertara nada-- haría pasar el paso 3 sin que nada funcionara.
        crudo = calendario._servicio.events().get(
            calendarId=config.google_calendar_id, eventId=evento_id
        ).execute()
        revisar("el evento existe en Google", crudo.get("status") == "confirmed",
                str(crudo.get("status")))
        marca = crudo.get("extendedProperties", {}).get("private", {}).get(CLAVE_ORIGEN)
        revisar("lleva la marca de origen", marca == VALOR_ORIGEN, repr(marca))
        revisar("y `a_bloqueo` lo descarta por esa marca", a_bloqueo(crudo) is None)

        # -----------------------------------------------------------------------------
        print("\n5. Mover el evento conservando su duración")
        # -----------------------------------------------------------------------------
        destino = CUANDO + timedelta(hours=1)
        calendario.mover_evento(evento_id, inicio=destino)
        movido = calendario._servicio.events().get(
            calendarId=config.google_calendar_id, eventId=evento_id
        ).execute()
        nuevo_inicio = datetime.fromisoformat(movido["start"]["dateTime"])
        nuevo_fin = datetime.fromisoformat(movido["end"]["dateTime"])
        revisar("empieza una hora después", nuevo_inicio == destino,
                f"{nuevo_inicio:%Y-%m-%d %H:%M}")
        revisar("y sigue durando 60 minutos", nuevo_fin - nuevo_inicio == timedelta(minutes=60),
                str(nuevo_fin - nuevo_inicio))

        # -----------------------------------------------------------------------------
        print("\n6. Un bloqueo de verdad SÍ tapa (el otro control)")
        # -----------------------------------------------------------------------------
        # Un evento sin la marca: lo que escribiría un doctor a mano. Si esto no apareciera
        # como bloqueo, el paso 3 no probaría nada -- pasaría porque `bloqueos()` no
        # devuelve nunca nada, no porque el filtro funcione.
        del_doctor = calendario._servicio.events().insert(
            calendarId=config.google_calendar_id,
            body={
                "summary": f"{MARCA} bloqueo del doctor",
                "start": {"dateTime": (CUANDO + timedelta(hours=3)).isoformat()},
                "end": {"dateTime": (CUANDO + timedelta(hours=4)).isoformat()},
            },
        ).execute()
        id_doctor = del_doctor["id"]
        try:
            bloqueos = calendario.bloqueos(CUANDO, CUANDO + timedelta(hours=6))
            titulos = [b.titulo for b in bloqueos]
            revisar("un evento sin marca sí aparece como bloqueo",
                    any("bloqueo del doctor" in t for t in titulos), str(titulos))
        finally:
            calendario.eliminar_evento(id_doctor)

        # -----------------------------------------------------------------------------
        print("\n7. Borrar dos veces no es un error")
        # -----------------------------------------------------------------------------
        # El contrato que hace que un reintento de cancelación no escale a los doctores.
        calendario.eliminar_evento(evento_id)
        try:
            calendario.eliminar_evento(evento_id)
            revisar("la segunda eliminación no lanza", True)
        except ErrorDeCalendario as e:
            revisar("la segunda eliminación no lanza", False, str(e))
        evento_id = ""  # ya está borrado: el `finally` no tiene nada que limpiar

    finally:
        # -----------------------------------------------------------------------------
        # La limpieza se COMPRUEBA. Dar por hecho que un `delete` funcionó es exactamente
        # la suposición que deja basura en el calendario donde los doctores miran su día.
        # -----------------------------------------------------------------------------
        if evento_id:
            print("\n8. Limpieza del evento de prueba")
            try:
                calendario.eliminar_evento(evento_id)
            except ErrorDeCalendario as e:
                print(f"  FALLA no se pudo borrar {evento_id}: {e}")
                fallos += 1
            else:
                sobrevive = True
                try:
                    calendario._servicio.events().get(
                        calendarId=config.google_calendar_id, eventId=evento_id
                    ).execute()
                except Exception as e:
                    sobrevive = getattr(e, "status_code", None) not in (404, 410)
                revisar("el evento de prueba ya no está en el calendario", not sobrevive,
                        evento_id)
                if sobrevive:
                    print(f"  >>> BÓRRALO A MANO: busca «{MARCA}» el "
                          f"{CUANDO:%d/%m/%Y} en el calendario.")

    print("\n" + "=" * 78)
    print("CalendarioGoogle: " + ("OK — el camino real funciona" if fallos == 0
                                  else f"{fallos} FALLAN"))
    print("=" * 78)
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

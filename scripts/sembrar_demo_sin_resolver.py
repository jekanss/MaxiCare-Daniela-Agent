"""Siembra casos de demostracion en `public.casos_sin_resolver`. USAR Y TIRAR.

No vive en el repositorio a proposito: es para mirar la pantalla una vez.
Se deshace entero con `sembrar_demo.py --borrar`.

Escribe usando las funciones de produccion (`registrar_caso`, `guardar_informe`) para que
la forma de los datos sea EXACTAMENTE la que dejaria un turno de verdad.
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "src")

from maxicare_daniela import config, persistencia, sin_resolver  # noqa: E402

TEL = "+573001234567"

#: (huella, tipo, veces, escalos, [frases], informe|None, dias_desde_la_primera)
CASOS = [
    (
        sin_resolver.huella_falta_dato("endodoncia", "precio"),
        "FALTA_DATO", 12, 4,
        [
            "buenas, cuanto cuesta una endodoncia?",
            "hola, necesito saber el valor del tratamiento de conducto",
            "cuanto me sale sacar el nervio de una muela",
            "precio endodoncia molar por favor",
            "me dijeron que necesito conducto, cuanto vale eso ahi?",
        ],
        {
            "que_paso": "Doce personas preguntaron por el costo de una endodoncia y Daniela no tuvo ninguna ficha que citar, asi que no pudo dar una respuesta concreta.",
            "por_que": "No hay ninguna entrada de endodoncia en la base de conocimiento. El tratamiento existe en el vocabulario de la clinica, pero su ficha nunca se cargo.",
            "recomiendo": "Cargar la ficha de endodoncia con los conceptos que la gente esta pidiendo. Cuatro de las doce terminaron interrumpiendo a un doctor por algo que una ficha resolveria sola.",
        },
        23,
    ),
    (
        sin_resolver.huella_falta_dato("ortodoncia", "cuota mensual"),
        "FALTA_DATO", 7, 2,
        [
            "cuanto me sale la ortodoncia en cuotas?",
            "hay financiacion para brackets? de cuanto seria la mensualidad",
            "y si pago mes a mes cuanto queda",
            "buenas tardes, manejan cuotas para ortodoncia",
            "la cuota mensual de los brackets cual es",
        ],
        {
            "que_paso": "Siete personas preguntaron por la cuota mensual de ortodoncia. Daniela tiene la ficha del tratamiento y pudo hablar de el, pero no tenia nada documentado sobre pago mes a mes.",
            "por_que": "La ficha de ortodoncia cubre el tratamiento y su duracion, pero no tiene el concepto de financiacion ni de cuota mensual.",
            "recomiendo": "Anadir a la ficha de ortodoncia el concepto de financiacion. Las siete preguntas son sobre lo mismo: como se paga, no cuanto cuesta.",
        },
        18,
    ),
    (
        sin_resolver.huella_guardrail("sin_cifra_no_documentada", "blanqueamiento"),
        "GUARDRAIL", 5, 0,
        [
            "cuanto vale el blanqueamiento dental?",
            "el blanqueamiento cuanto sale mas o menos",
            "hola, precio de blanqueamiento por favor",
            "quisiera saber cuanto cuesta blanquearme los dientes",
            "valor blanqueamiento laser",
        ],
        {
            "que_paso": "Cinco veces un guardrail freno a Daniela cuando iba a dar una cifra de blanqueamiento que la base de conocimiento no habia autorizado en ese turno. La respuesta se regenero y el paciente quedo atendido.",
            "por_que": "El guardrail sin_cifra_no_documentada exige que toda cifra de dinero venga de la tool de conocimiento. La ficha de blanqueamiento no trae el concepto que estas cinco personas pedian, y el modelo intento completarlo por su cuenta.",
            "recomiendo": "Revisar que concepto de blanqueamiento falta en la ficha. El guardrail esta haciendo bien su trabajo: lo que hay que cerrar es el hueco que lo obliga a saltar.",
        },
        11,
    ),
    (
        sin_resolver.huella_humano("excepcion_comercial"),
        "HUMANO", 6, 6,
        [
            "me pueden hacer un descuento si pago todo de una?",
            "hay promocion si vamos dos personas?",
            "mi hermana ya es paciente de ustedes, aplica algun beneficio",
            "tienen convenio con alguna eps o seguro",
            "si soy estudiante hay tarifa especial?",
        ],
        {
            "que_paso": "Seis pacientes pidieron una excepcion comercial --descuentos, promociones, convenios-- y Daniela escalo cada una a un doctor, porque no tiene autoridad para conceder ninguna.",
            "por_que": "No hay ninguna politica documentada sobre descuentos, convenios o tarifas especiales, asi que toda pregunta de este tipo termina interrumpiendo a alguien.",
            "recomiendo": "Decidir con la clinica que se puede responder sin consultar y documentarlo. Seis interrupciones al doctor en tres semanas por preguntas que se repiten es el caso mas caro de esta pantalla.",
        },
        21,
    ),
    (
        sin_resolver.huella_guardrail("sin_lectura_clinica", "no_identificado"),
        "GUARDRAIL", 3, 0,
        [
            "le mando la radiografia y me dice que tengo?",
            "adjunto la foto de mi muela, esta muy mal?",
            "mire esta imagen, necesito cirugia?",
        ],
        None,  # sin informe todavia: asi se ve el caso recien caido
        4,
    ),
    (
        sin_resolver.huella_roto("ModelBehaviorError: invalid tool call"),
        "ROTO", 2, 0,
        [
            "quiero cambiar mi cita del jueves para el otro jueves a la misma hora",
            "puedo mover la cita de mi mama y la mia al mismo dia?",
        ],
        {
            "que_paso": "Dos turnos reventaron con un error del SDK a mitad de la conversacion. El paciente recibio el mensaje seguro y no se quedo sin respuesta, pero el turno no se completo.",
            "por_que": "El modelo intento una llamada a una herramienta con una forma que el SDK rechazo. Las dos frases de los pacientes piden mover dos cosas a la vez, que es el patron que lo dispara.",
            "recomiendo": "Revisar como se comporta el agente cuando el paciente pide dos cambios de cita en un solo mensaje. Son solo dos casos, pero los dos tienen la misma forma.",
        },
        6,
    ),
]


def conectar():
    config.cargar_dotenv()
    url = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not url:
        print("FALLA -> falta MAXICARE_DATABASE_URL")
        raise SystemExit(1)
    return persistencia.conectar(url)


def borrar() -> int:
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.casos_sin_resolver")
            cuantas = cur.rowcount
        conn.commit()
    return cuantas


def sembrar() -> None:
    with conectar() as conn:
        for huella, tipo, veces, escalos, frases, informe, dias in CASOS:
            for i in range(veces):
                persistencia.registrar_caso(
                    conn,
                    huella=huella,
                    tipo=tipo,
                    escalo=1 if i < escalos else 0,
                    ejemplo=frases[i] if i < len(frases) else None,
                    telefono=TEL,
                )
            # La primera vez, hacia atras, para que las fechas de la pantalla se lean reales.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.casos_sin_resolver "
                    "   SET primera_vez = now() - make_interval(days => %s) "
                    " WHERE huella = %s",
                    (dias, huella),
                )
            conn.commit()

            if informe is not None:
                persistencia.guardar_informe(
                    conn, huella=huella, informe=informe, sobre=veces
                )
            print(f"OK  {tipo:<11} {huella:<45} contador={veces} escalo={escalos}")


def main() -> int:
    if "--borrar" in sys.argv:
        print(f"OK  borradas {borrar()} filas de public.casos_sin_resolver")
        return 0

    print("-> sembrando en public.casos_sin_resolver")
    borrar()
    sembrar()
    print("\nOK  listo. Abre http://localhost:5173 -> «Sin resolver»")
    print("    Para deshacerlo: uv run python <este archivo> --borrar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

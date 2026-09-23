"""La tabla de casos sin resolver contra Neon de verdad.

    MAXICARE_PRUEBAS_NEON=1 uv run pytest -q -m neon

Escribe en el esquema `pruebas_sin_resolver`, que se crea y se borra aqui. NUNCA en
`public`: alli hay pacientes reales de una clinica.

La conexion es DIRECTA (sin `-pooler.`) porque el pooler de Neon rechaza `options` como
parametro de arranque, y porque comparte sesiones: una prueba de concurrencia sobre el
pooler mide otra cosa.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
import threading
import time

import psycopg
import pytest

from maxicare_daniela import persistencia
from maxicare_daniela.config import cargar_dotenv

pytestmark = pytest.mark.neon

ESQUEMA = "pruebas_sin_resolver"


class _UrlOculta(str):
    """Para que la contrasena no salga en el encabezado de pytest con -vv."""

    def __repr__(self) -> str:  # pragma: no cover
        return "'***@neon (oculta: ver _UrlOculta)'"


def _url_cruda() -> str:
    cargar_dotenv()
    url = os.environ.get("MAXICARE_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("falta MAXICARE_DATABASE_URL")
    return url


@pytest.fixture(scope="module")
def url() -> str:
    if os.environ.get("MAXICARE_PRUEBAS_NEON") != "1":
        pytest.skip("pruebas contra Neon desactivadas (MAXICARE_PRUEBAS_NEON != 1)")
    return _UrlOculta(_url_cruda().replace("-pooler.", "."))


@pytest.fixture(scope="module")
def esquema(url: str):
    """Crea el esquema, aplica TODAS las migraciones dentro, y lo borra pase lo que pase."""
    separador = "&" if "?" in url else "?"
    con_esquema = f"{url}{separador}options=-csearch_path%3D{ESQUEMA}"

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {ESQUEMA}")
        conn.commit()

    with persistencia.conectar(con_esquema) as conn:
        persistencia.aplicar_esquema(conn)

    yield _UrlOculta(con_esquema)

    with persistencia.conectar(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {ESQUEMA} CASCADE")
        conn.commit()


def _fila(conn, huella: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT contador, escalo, ejemplos, tipo FROM casos_sin_resolver WHERE huella = %s",
            (huella,),
        )
        contador, escalo, ejemplos, tipo = cur.fetchone()
    return {"contador": contador, "escalo": escalo, "ejemplos": json.loads(ejemplos), "tipo": tipo}


def test_la_migracion_crea_la_tabla(esquema):
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'casos_sin_resolver'",
                (ESQUEMA,),
            )
            columnas = {f[0] for f in cur.fetchall()}

    assert {"huella", "tipo", "contador", "escalo", "ejemplos", "informe"} <= columnas


def test_un_tipo_invalido_lo_rechaza_la_base(esquema):
    """El CHECK tiene que vivir en el esquema de pruebas, no solo en `public`.

    `CheckViolation` y no `Exception` a secas: un `Exception` ancho pasaria igual si
    `registrar_caso` reventara con un `TypeError` por un cambio de firma, sin que el CHECK
    tuviera nada que ver.
    """
    with persistencia.conectar(esquema) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            persistencia.registrar_caso(conn, huella="x:y", tipo="INVENTADO")


def test_un_registro_que_falla_no_envenena_la_conexion_del_llamador(esquema):
    """La regresion que importa de verdad.

    `registrar_caso` es instrumentacion: se llama desde una conexion que ya trae escrituras
    clinicas encima (`atencion._anotar_resultado`, `relevo.activar`, tarea 2). Si el fallo de
    un caso deja la transaccion de esa conexion abortada, toda sentencia posterior sobre ELLA
    --incluida una escritura clinica que no tiene nada que ver con este caso-- muere con
    «current transaction is aborted», aunque el `INSERT` fallido no importara para nada.

    Sin el `rollback` dentro de `registrar_caso`, este intento de escribir un caso VALIDO en
    la misma conexion, justo despues del invalido, revienta igual que el primero.
    """
    with persistencia.conectar(esquema) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            persistencia.registrar_caso(conn, huella="x:y", tipo="INVENTADO")

        # La MISMA conexion, sin abrir otra, tiene que poder seguir escribiendo.
        persistencia.registrar_caso(
            conn, huella="falta_dato:sobrevive:precio", tipo="FALTA_DATO"
        )
        fila = _fila(conn, "falta_dato:sobrevive:precio")

    assert fila["contador"] == 1


def test_siete_turnos_iguales_dejan_una_fila_con_contador_siete(esquema):
    huella = "falta_dato:ortodoncia:precio"
    with persistencia.conectar(esquema) as conn:
        for i in range(7):
            persistencia.registrar_caso(
                conn, huella=huella, tipo="FALTA_DATO", escalo=1,
                ejemplo=f"pregunta numero {i}", telefono=f"+5730000000{i}",
            )
        fila = _fila(conn, huella)

    assert fila["contador"] == 7, "siete turnos tienen que ser UNA fila, no siete"
    assert fila["escalo"] == 7
    assert len(fila["ejemplos"]) == 5, "los ejemplos se recortan a cinco"
    assert fila["ejemplos"][-1]["texto"] == "pregunta numero 6"
    assert fila["ejemplos"][0]["texto"] == "pregunta numero 2", "se va el mas viejo"


def test_dos_escrituras_simultaneas_no_crean_dos_filas(esquema):
    """La carrera real: dos turnos de dos pacientes distintos, a la vez, misma huella.

    Sin el `ON CONFLICT`, una de las dos revienta con violacion de UNIQUE o -- peor -- si
    alguien "arreglara" eso con un SELECT-then-INSERT quedarian dos filas, y el informe
    diria seis y seis en vez de doce.
    """
    huella = "guardrail:sin_cifra_no_documentada:_general"
    barrera = threading.Barrier(2)
    errores: list[Exception] = []

    def escribir(n: int) -> None:
        try:
            with persistencia.conectar(esquema) as conn:
                barrera.wait(timeout=10)
                time.sleep(0.05)
                persistencia.registrar_caso(
                    conn, huella=huella, tipo="GUARDRAIL", ejemplo=f"hilo {n}", telefono="+57300"
                )
        except Exception as e:  # noqa: BLE001
            errores.append(e)

    hilos = [threading.Thread(target=escribir, args=(n,)) for n in (1, 2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=20)

    assert errores == [], f"la escritura concurrente fallo: {errores}"
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM casos_sin_resolver WHERE huella = %s", (huella,))
            cuantas = cur.fetchone()[0]
        fila = _fila(conn, huella)

    assert cuantas == 1, "dos escrituras simultaneas crearon dos filas"
    assert fila["contador"] == 2


def test_la_ventana_deja_fuera_lo_viejo_y_ordena_por_frecuencia(esquema):
    with persistencia.conectar(esquema) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM casos_sin_resolver")
        conn.commit()

        for _ in range(3):
            persistencia.registrar_caso(conn, huella="falta_dato:a:precio", tipo="FALTA_DATO")
        for _ in range(9):
            persistencia.registrar_caso(conn, huella="falta_dato:b:precio", tipo="FALTA_DATO")
        persistencia.registrar_caso(conn, huella="falta_dato:viejo:precio", tipo="FALTA_DATO")

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE casos_sin_resolver SET ultima_vez = now() - interval '90 days' "
                "WHERE huella = 'falta_dato:viejo:precio'"
            )
        conn.commit()

        casos = persistencia.casos_recientes(conn, dias=30)

    huellas = [c["huella"] for c in casos]
    assert "falta_dato:viejo:precio" not in huellas, "lo que dejo de pasar se hunde solo"
    assert huellas[0] == "falta_dato:b:precio", "primero lo que mas esta pasando"
    assert casos[0]["contador"] == 9


def test_la_frase_y_el_telefono_salen_SEPARADOS(esquema):
    """Esta prueba decia «el telefono no puede viajar al navegador» y dejo de ser cierta el
    22/09/2026, a proposito.

    La cautela vieja no protegia nada: la pantalla de Conversaciones le ensena a esa MISMA
    persona la lista entera de numeros con su nombre al lado. Y costaba lo unico que
    convierte un caso en algo accionable: poder abrir la conversacion donde paso. MaxiCare
    pidio ese enlace.

    Lo que SI se sostiene, y es lo que esta prueba vigila ahora: los dos salen por campos
    distintos, y `ejemplos` sigue siendo una lista de CADENAS. Mientras lo sea, no hay forma
    de saber quien dijo que -- que es lo que mantiene el caso siendo un agregado. El dia que
    alguien devuelva `[{"texto": ..., "telefono": ...}]` «para que sea mas completo», esto se
    pone rojo.
    """
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(
            conn, huella="falta_dato:secreto:precio", tipo="FALTA_DATO",
            ejemplo="cuanto vale", telefono="+573001112233",
        )
        casos = persistencia.casos_recientes(conn, dias=30)

    uno = next(c for c in casos if c["huella"] == "falta_dato:secreto:precio")
    assert uno["ejemplos"] == ["cuanto vale"]
    assert all(isinstance(e, str) for e in uno["ejemplos"]), (
        "las frases salen sueltas: emparejarlas con su numero convertiria el agregado en un "
        "listado de quien dijo que"
    )
    assert uno["telefonos"] == ["+573001112233"], "el enlace a la conversacion sale aparte"


def test_clearstate_borra_la_frase_y_el_contador_no_baja(esquema):
    """No negociable 9, aplicado aqui: el caso vive, la frase se va.

    El conteo es historia de la clinica, no dato del paciente: «doce personas preguntaron
    por ortodoncia» sigue siendo cierto aunque se borre una de esas conversaciones.
    """
    huella = "falta_dato:borrable:precio"
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(
            conn, huella=huella, tipo="FALTA_DATO", ejemplo="la mia", telefono="+573009998877"
        )
        persistencia.registrar_caso(
            conn, huella=huella, tipo="FALTA_DATO", ejemplo="la de otro", telefono="+573001112233"
        )

        tocadas = persistencia.olvidar_ejemplos_de(conn, "+573009998877")
        fila = _fila(conn, huella)

    assert tocadas == 1
    assert fila["contador"] == 2, "el contador NO baja"
    assert [e["texto"] for e in fila["ejemplos"]] == ["la de otro"]


def test_un_borrado_que_deja_VARIOS_ejemplos_conserva_su_orden(esquema):
    """El de arriba deja UNO solo, y con uno el orden es indistinguible: ese test pasaria
    igual con un reagrupado que baraje. Este deja tres, que es donde se ve.

    El `WITH ORDINALITY` de `_OLVIDAR_EJEMPLOS_DEL_TELEFONO` --y el de `registrar_caso`--
    existe justo para esto: `row_number() OVER ()` sin `ORDER BY` no garantiza nada por
    contrato, y el orden cronologico es lo que hace legible la lista de la pantalla.
    """
    huella = "falta_dato:ordenado:precio"
    with persistencia.conectar(esquema) as conn:
        for texto, tel in [
            ("la primera", "+573001110001"),
            ("la del que se borra", "+573009998877"),
            ("la segunda", "+573001110002"),
            ("la tercera", "+573001110003"),
        ]:
            persistencia.registrar_caso(
                conn, huella=huella, tipo="FALTA_DATO", ejemplo=texto, telefono=tel
            )

        persistencia.olvidar_ejemplos_de(conn, "+573009998877")
        fila = _fila(conn, huella)

    assert [e["texto"] for e in fila["ejemplos"]] == [
        "la primera", "la segunda", "la tercera",
    ], "el reagrupado despues del filtro tiene que conservar el orden cronologico"


def test_un_caso_viejo_sin_informe_no_se_analiza(esquema):
    """`casos_sin_informe` comparte la ventana con `casos_recientes`. Un caso de hace noventa
    dias pagaba su llamada al modelo para un informe que la pantalla no va a mostrar nunca.
    """
    huella = "falta_dato:antiguo:precio"
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(conn, huella=huella, tipo="FALTA_DATO")
        assert any(c["huella"] == huella for c in persistencia.casos_sin_informe(conn, limite=50))

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE casos_sin_resolver SET ultima_vez = now() - interval '90 days' "
                " WHERE huella = %s",
                (huella,),
            )
        conn.commit()

        pendientes = [c["huella"] for c in persistencia.casos_sin_informe(conn, limite=50)]

    assert huella not in pendientes, "lo que la ventana ya hundio no se analiza"


def test_guardar_informe_y_dejar_de_estar_pendiente(esquema):
    huella = "falta_dato:coninforme:precio"
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(conn, huella=huella, tipo="FALTA_DATO")
        assert any(c["huella"] == huella for c in persistencia.casos_sin_informe(conn, limite=50))

        persistencia.guardar_informe(
            conn, huella=huella,
            informe={"que_paso": "paso una vez", "por_que": "no hay ficha", "recomiendo": "crearla"},
            sobre=1,
        )

        assert not any(
            c["huella"] == huella for c in persistencia.casos_sin_informe(conn, limite=50)
        )
        leido = next(c for c in persistencia.casos_recientes(conn) if c["huella"] == huella)

    assert leido["informe"]["recomiendo"] == "crearla"


# ==========================================================================================
# La 029: la medicion empieza de cero, y el DELETE no puede volver a correr
# ==========================================================================================


def test_la_029_deja_la_marca_de_cuando_empezo_a_contarse(esquema):
    """`aplicar_esquema` ya corrio en el fixture, asi que la marca tiene que estar puesta."""
    with persistencia.conectar(esquema) as conn:
        desde = persistencia.medicion_sin_resolver_desde(conn)

    assert desde is not None, "la 029 no escribio la marca"
    assert desde.endswith("Z") and "T" in desde, f"tiene que ser ISO 8601 UTC, llego {desde!r}"
    # Lo que va a hacer el navegador con ella. Si esto falla, la pantalla calla y nadie sabe
    # desde cuando cuentan los numeros.
    datetime.fromisoformat(desde.replace("Z", "+00:00"))


def test_reaplicar_las_migraciones_NO_borra_los_casos_que_ya_se_midieron(esquema):
    """La prueba que impide un desastre silencioso.

    `aplicar_esquema` corre TODAS las migraciones en CADA arranque del contenedor. Un
    `DELETE FROM casos_sin_resolver` sin guarda dentro de la 029 vaciaria la medicion entera
    en el siguiente despliegue, sin un error en ningun log y sin que nadie lo notara hasta
    que alguien abriera la pantalla y la viera vacia. La guarda es la marca, y esto lo fija.
    """
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(
            conn, huella="falta_dato:sobrevive:precio", tipo="FALTA_DATO",
            ejemplo="cuanto vale", telefono="+573001112233",
        )
        marca_antes = persistencia.medicion_sin_resolver_desde(conn)

        # El arranque siguiente, entero.
        persistencia.aplicar_esquema(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM casos_sin_resolver WHERE huella = %s",
                ("falta_dato:sobrevive:precio",),
            )
            assert cur.fetchone()[0] == 1, "la 029 volvio a borrar: el DELETE perdio su guarda"
        assert persistencia.medicion_sin_resolver_desde(conn) == marca_antes, (
            "la marca se reescribio; los contadores dejarian de significar lo que dicen"
        )


def test_un_caso_trae_los_telefonos_de_donde_salio_y_no_quien_dijo_que(esquema):
    """Lo que hace accionable un caso: poder abrir la conversacion. Y lo que sigue sin salir:
    la correspondencia entre cada frase y su numero."""
    with persistencia.conectar(esquema) as conn:
        persistencia.registrar_caso(
            conn, huella="falta_dato:enlace:precio", tipo="FALTA_DATO",
            ejemplo="cuanto vale", telefono="+573001112233",
        )
        persistencia.registrar_caso(
            conn, huella="falta_dato:enlace:precio", tipo="FALTA_DATO",
            ejemplo="y en cuotas", telefono="+573004445566",
        )
        persistencia.registrar_caso(
            conn, huella="falta_dato:enlace:precio", tipo="FALTA_DATO",
            ejemplo="sigues ahi", telefono="+573001112233",
        )

        caso = next(
            c for c in persistencia.casos_recientes(conn)
            if c["huella"] == "falta_dato:enlace:precio"
        )

    assert caso["telefonos"] == ["+573001112233", "+573004445566"], "sin repetir y en orden"
    assert caso["ejemplos"] == ["cuanto vale", "y en cuotas", "sigues ahi"]
    assert all(isinstance(e, str) for e in caso["ejemplos"]), (
        "las frases salen sueltas: emparejarlas con su numero convertiria el agregado en otra cosa"
    )


# ==========================================================================================
# Con que se escribe el caso de un relevo (23/09/2026)
#
# `relevo._registrar_relevo` escribia `humano:relevo` a pelo, sin frase y sin causa, y el
# informe que salia de ahi no podia decir mas que «un doctor tomo la conversacion». Lo que
# faltaba estaba en la base desde siempre; lo que no habia era quien lo leyera.
# ==========================================================================================


_TEL_RELEVO = "573009998877"


def _sembrar_relevo(conn, *, mensaje_id, motivo="dato_faltante", sufijo=""):
    """Una conversacion con dos frases y un aviso en medio. Devuelve su id.

    La segunda frase llega DESPUES del aviso a proposito: es lo que distingue coger «la
    ultima frase» de coger «la del turno que escalo», y solo una de las dos explica el caso.
    """
    # El id lo pone el codigo, no la base: el orquestador lo necesita para armar claves de
    # idempotencia antes de haber escrito nada (`persistencia.asegurar_conversacion`).
    conv = persistencia.asegurar_conversacion(conn, telefono=_TEL_RELEVO)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, conversacion_id, "
            "recibido_en) VALUES (%s, %s, 'text', %s, %s, now() - interval '10 minutes')",
            (f"w1{sufijo}", _TEL_RELEVO, "cuanto vale sacarme las cordales", conv),
        )
        cur.execute(
            "INSERT INTO escalamientos (conversacion_id, motivo, resumen, pregunta, "
            "clave_idempotencia, telegram_message_id, creado_en) "
            "VALUES (%s, %s, 'r', 'p', %s, %s, now() - interval '5 minutes')",
            (conv, motivo, f"k{mensaje_id}{sufijo}", mensaje_id),
        )
        cur.execute(
            "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, conversacion_id, "
            "recibido_en) VALUES (%s, %s, 'text', 'muy amable', %s, now())",
            (f"w2{sufijo}", _TEL_RELEVO, conv),
        )
    conn.commit()
    return conv


def test_el_aviso_detras_del_relevo_trae_el_motivo_y_la_frase_QUE_LO_CAUSO(esquema):
    """Las dos mitades de «por que se derivo». La frase se ata al aviso por su hora: entre
    que el aviso sale y el doctor lo pulsa el paciente sigue escribiendo, y la ultima frase
    seria la de despues del problema."""
    with persistencia.conectar(esquema) as conn:
        _sembrar_relevo(conn, mensaje_id=7001)

        assert persistencia.aviso_detras_del_relevo(conn, 7001) == (
            "dato_faltante",
            "cuanto vale sacarme las cordales",
        )


def test_sin_mensaje_id_no_hay_aviso_y_eso_NO_es_un_fallo(esquema):
    """Es como llama el panel: `activar` sin `mensaje_id` porque no cuelga de ningun aviso.
    Ese `None` significa «nadie escalo, el doctor entro por su cuenta», que es justo lo que
    el informe tiene que poder distinguir -- y no se puede resolver con una consulta."""
    with persistencia.conectar(esquema) as conn:
        assert persistencia.aviso_detras_del_relevo(conn, None) is None


def test_un_mensaje_id_que_no_es_de_ningun_aviso_nuestro_tampoco_inventa_nada(esquema):
    """Un id de Telegram que no esta en `escalamientos` -- el doctor pulso un boton de otro
    mensaje, o la fila se fue con la conversacion. Se devuelve `None`, nunca un motivo
    parecido: una causa inventada es peor que ninguna."""
    with persistencia.conectar(esquema) as conn:
        assert persistencia.aviso_detras_del_relevo(conn, 999999) is None


def test_una_nota_de_voz_llega_al_informe_por_su_TRANSCRIPCION(esquema):
    """En un audio `texto` es NULL y lo que dijo el paciente vive en `transcripcion` (no
    negociable 29). Sin el `coalesce`, los turnos de audio --de los que mas escalan-- caerian
    mudos justo en el informe que existe para leerlos."""
    with persistencia.conectar(esquema) as conn:
        conv = persistencia.asegurar_conversacion(conn, telefono=_TEL_RELEVO)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mensajes_entrantes (wamid, telefono, tipo, texto, transcripcion, "
                "conversacion_id, recibido_en) VALUES ('wv', %s, 'audio', NULL, %s, %s, "
                "now() - interval '10 minutes')",
                (_TEL_RELEVO, "me duele mucho la muela", conv),
            )
            cur.execute(
                "INSERT INTO escalamientos (conversacion_id, motivo, resumen, pregunta, "
                "clave_idempotencia, telegram_message_id, creado_en) "
                "VALUES (%s, 'clinico', 'r', 'p', 'kv', 7002, now())",
                (conv,),
            )
        conn.commit()

        assert persistencia.aviso_detras_del_relevo(conn, 7002) == (
            "clinico",
            "me duele mucho la muela",
        )


def test_registrar_relevo_deja_la_fila_ENTERA_sin_doblar_nada(esquema):
    """El camino real de punta a punta: `relevo._registrar_relevo` sobre Neon.

    Las pruebas de `test_relevo.py` doblan esta función --miran que `activar` la llame con lo
    que toca-- así que ninguna comprueba lo único que importa al final: que la fila que llega
    a la tabla lleve la huella con su causa, la frase del turno que escaló y el teléfono.
    """
    from maxicare_daniela import relevo

    with persistencia.conectar(esquema) as conn:
        _sembrar_relevo(conn, mensaje_id=7003, sufijo="r")

    relevo._registrar_relevo(esquema, telefono=_TEL_RELEVO, mensaje_id=7003)

    with persistencia.conectar(esquema) as conn:
        caso = next(
            c for c in persistencia.casos_recientes(conn)
            if c["huella"].startswith("humano:relevo")
        )

    assert caso["huella"] == "humano:relevo:dato_faltante", "la causa va dentro de la huella"
    assert caso["ejemplos"] == ["cuanto vale sacarme las cordales"], (
        "sin la frase, el analista recibe una etiqueta y un numero -- y eso fue el bug"
    )
    assert caso["telefonos"] == [_TEL_RELEVO], "sin el, no se puede abrir la conversacion"
    assert caso["escalo"] == 0, (
        "un relevo no es una interrupcion nueva: la del aviso ya la conto el escalamiento"
    )


def test_un_relevo_por_iniciativa_se_cuenta_APARTE_y_sin_frase(esquema):
    """La otra mitad, y la que MaxiCare vio mal. Sin `mensaje_id` no hubo aviso: nadie escaló
    y el doctor entró por su cuenta. Va a su propia fila --mezclada con la de arriba, «5
    relevos» no se podría repartir después-- y sin frase, porque no hay ningún mensaje del
    que conste que provocara nada."""
    from maxicare_daniela import relevo

    relevo._registrar_relevo(esquema, telefono="573001234567", mensaje_id=None)

    with persistencia.conectar(esquema) as conn:
        caso = next(
            c for c in persistencia.casos_recientes(conn)
            if c["huella"] == "humano:relevo:_sin_aviso"
        )

    assert caso["ejemplos"] == [], "inventarle una frase seria atribuirle una causa"
    assert caso["contador"] == 1
    # Y SIN telefono, aunque `_registrar_relevo` se lo pase: `registrar_caso` guarda el
    # numero DENTRO de la entrada de la frase, asi que sin frase no hay donde ponerlo.
    #
    # Se deja asi a proposito. Colarlo en una entrada con `texto` NULL daria el enlace a la
    # conversacion --que aqui seria util, porque es el unico caso donde el analisis no puede
    # decir la causa-- al precio de romper `_CONTAR_EJEMPLOS_DEL_TELEFONO`, que es el numero
    # que `/clearstate` le dice al paciente como «N frases tuyas». Mentir en la confirmacion
    # de un borrado cuesta mas que un enlace.
    assert caso["telefonos"] == []

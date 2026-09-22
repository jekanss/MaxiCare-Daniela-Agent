"""Aplica el esquema en Neon y carga la base de conocimiento. Idempotente: se puede correr
todas las veces que haga falta.

    uv run python scripts/inicializar_base.py           # aplica esquema + carga + verifica
    uv run python scripts/inicializar_base.py --solo-verificar

Lee `MAXICARE_DATABASE_URL` de `.env` (que no se versiona) o del entorno del proceso. No
imprime la cadena de conexión ni ningún secreto.

------------------------------------------------------------------------------------------
Pone al día DOS esquemas, no uno
------------------------------------------------------------------------------------------
`public` --la base de la clínica-- y `pruebas_web` --el carril del chat del panel--. El
segundo no es un capricho de simetría: lo creaba solo `runtime._preparar_esquema_de_pruebas`,
que es PEREZOSO --corre la primera vez que alguien abre el chat web-- y nadie lo había
abierto desde que existen las migraciones 009 y 010. Medido el 13/09/2026 contra la base
real: `public` tenía 16 tablas y `pruebas_web` 14 --le faltaban `agent_sessions` y
`agent_messages`--, y a su `mensajes_entrantes` le faltaban cuatro columnas
(`conversacion_id`, `respondido_en`, `wamid_respuesta`, `fallo_respuesta`).

Lo que eso rompía no era el chat: era `/clearstate`, cuyo borrado secundario sobre
`pruebas_web` reventaba con `UndefinedColumn` mientras tres documentos afirmaban que
funcionaba. Que el carril de pruebas esté al día no puede depender de que alguien se acuerde
de abrir una pantalla: depende del despliegue, que es esto.

`--solo-verificar` NO escribe en ninguno de los dos: se limita a decir cuál está atrasado.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# La consola de Windows es cp1252 y revienta al imprimir cualquier cosa fuera de ese
# rango. Este script corre tanto aquí como en el VPS: se fuerza UTF-8 en los dos.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from maxicare_daniela import persistencia  # noqa: E402
from maxicare_daniela.config import Config, cargar_dotenv  # noqa: E402


#: Las dos tablas que trajo la fase 7 (`migraciones/010_sesiones_agente.sql`). Se verifican
#: por nombre en los dos esquemas: sin ellas, el historial del diálogo no se persiste y el
#: turno de cada paciente muere con el proceso -- que es exactamente lo que la fase arregla.
TABLAS_DEL_HISTORIAL = ("agent_sessions", "agent_messages")

#: Las dos que trajo la 019. Se verifican por la misma razón y con el mismo alcance: sin
#: `contactos`, el primer mensaje de cada número revienta al leer el estado y el paciente
#: recibe el mensaje seguro; sin `consentimientos`, el aviso de la política sale y no queda
#: constancia de que salió, que es justo lo que hay que poder acreditar. Y van en los DOS
#: esquemas porque `/clearstate` toca los dos.
TABLAS_DEL_CONSENTIMIENTO = ("contactos", "consentimientos")

#: El valor que la 021 le añade al CHECK de `cambios_configuracion.tabla`. No es una tabla
#: nueva que se pueda buscar por nombre: es una restricción que se AMPLÍA, y por eso los dos
#: bloques de arriba no sirven de molde tal cual.
#:
#: La 007 dejó ese CHECK cerrado en tres valores porque entonces el panel solo escribía
#: configuración. La fase 8 añadió la cuarta escritura --`citas.asistio`, la marca de
#: asistencia-- y `panel.marcar_asistencia` mete el UPDATE y su fila de bitácora en la MISMA
#: transacción: con el CHECK viejo, el INSERT revienta con `CheckViolation` y arrastra al
#: UPDATE. La columna queda inescribible desde el panel y el fallo no se ve hasta que alguien
#: del mostrador intenta marcar a un paciente.
#:
#: Sin este bloque, `--solo-verificar` decía OK sobre una base en ese estado exacto.
VALOR_DE_LA_021 = "citas"

#: Lo mismo, una migración después: la 028 abre el CHECK a `conversaciones` para que borrar
#: el hilo de un paciente deje constancia de quién lo hizo. Mismo modo de fallo exacto que la
#: 021 --`panel.borrar_conversacion` mete el borrado y su bitácora en la misma transacción,
#: así que un CHECK viejo revienta el INSERT y arrastra al borrado-- con una diferencia que lo
#: empeora: aquí el `CheckViolation` no rompe una marca que se puede volver a poner, sino la
#: única fila que iba a quedar de algo que no se puede deshacer.
#:
#: Los dos se comprueban en el mismo bloque, con la misma función, porque son la misma
#: pregunta hecha dos veces: la lista del CHECK es cerrada a propósito, así que la SÉPTIMA
#: escritura del panel añadirá aquí su propia línea.
VALOR_DE_LA_028 = "conversaciones"

#: Las tres tablas de la 022, el perímetro contra el abuso de coste.
#:
#: `consumo_modelo` es la contabilidad --sin ella el sistema vuelve a ser ciego a su propia
#: factura-- y las otras dos son lo que impide que la defensa se convierta en el ruido: sin
#: `cuotas_avisadas`, un número que se pasa recibe una frase por CADA mensaje y el doctor un
#: Telegram por cada uno; sin `alertas_gasto`, el día que se cruce el umbral el vigilante
#: manda 288 avisos idénticos.
#:
#: Si faltan, nada revienta: `cuotas.revisar` falla abierto a propósito y el perímetro queda
#: apagado en silencio. Por eso se verifican.
TABLAS_DEL_PERIMETRO = ("consumo_modelo", "cuotas_avisadas", "alertas_gasto")

#: Las dos migraciones que completan el hilo de la pantalla de Conversaciones, y que hasta hoy
#: no verificaba nadie. Van juntas porque su modo de fallo es el mismo y es de los peores: el
#: hilo se pinta igual, sin error y sin log, con un hueco donde debería estar lo que dijo
#: alguien.
#:
#: Sin la 026 (`mensajes_del_doctor`) el GET del hilo revienta con `UndefinedTable` y la
#: pantalla se queda cargando --`panel.hilo` la consulta fuera de todo `try`-- mientras la
#: ESCRITURA se pierde en silencio, porque esa sí la traga un `except`. Sin la 027
#: (`mensajes_entrantes.transcripcion`) pasa lo contrario y es más sutil: no revienta nada,
#: simplemente toda nota de voz vuelve a decir «(nota de voz)» y el volcado que recibe el
#: doctor al tomar la conversación se las come enteras.
#:
#: Una es una TABLA y la otra una COLUMNA, así que el molde de `_tablas_de` no sirve para las
#: dos: es la misma lección de la 021 --lo que no se puede buscar por nombre de tabla necesita
#: su propia comprobación-- por una tercera puerta.
COLUMNA_DE_LA_027 = ("mensajes_entrantes", "transcripcion")

#: Las claves de `tratamientos` que NO tienen ni una ficha de conocimiento a propósito, y por
#: las que el bloque 11 no pregunta.
#:
#: `endodoncia` y `protesis`: la sección 2.12 del documento maestro dice que no hay precio,
#: especialista, inclusiones ni garantías documentadas, y que no debe publicarse ninguna
#: cifra. La forma de cumplirlo NO es una regla en el prompt: es que la fila no exista, para
#: que salga `SIN DATO DOCUMENTADO`. Ver `.claude/rules/base-conocimiento.md`.
#:
#: `no_identificado`: no es un tratamiento. Es lo que usa el SISTEMA cuando no sabe de qué va
#: un archivo. Que no se pueda hablar de él es lo correcto.
MUDOS_A_PROPOSITO = ("endodoncia", "protesis", "no_identificado")


def _enmascarar(url: str) -> str:
    """Deja ver a qué host se conectó, nunca las credenciales."""
    try:
        resto = url.split("@", 1)[1]
        return "***@" + resto.split("?", 1)[0]
    except IndexError:
        return "..."


def _tablas_de(conn, esquema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (esquema,),
        )
        return {fila[0] for fila in cur.fetchall()}


def _tiene_columna(conn, esquema: str, tabla: str, columna: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (esquema, tabla, columna),
        )
        return cur.fetchone() is not None


def _bitacora_admite(conn, esquema: str, valor: str) -> bool:
    """¿El CHECK de `cambios_configuracion.tabla` de ese esquema deja pasar `valor`?

    Se lee la definición del constraint en vez de intentar el INSERT: probarlo escribiendo
    dejaría una fila falsa en la bitácora de la clínica --o exigiría un ROLLBACK a mano
    dentro de un script que también hace commits-- y `--solo-verificar` no escribe nada.

    Se busca por TABLA y ESQUEMA, no por nombre de constraint: la 021 tiene que borrar dos
    nombres posibles (el que Postgres le puso solo en la 007 y el explícito que ella deja),
    así que el nombre no es una llave fiable. Y se recorren todos los CHECK de la tabla
    porque puede haber más de uno.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(c.oid)
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
             WHERE n.nspname = %s
               AND t.relname = 'cambios_configuracion'
               AND c.contype = 'c'
            """,
            (esquema,),
        )
        definiciones = [fila[0] for fila in cur.fetchall()]
    return any(f"'{valor}'" in definicion for definicion in definiciones)


def _existe_esquema(conn, esquema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (esquema,)
        )
        return cur.fetchone() is not None


def _poner_al_dia_pruebas_web(database_url: str) -> int:
    """Crea `pruebas_web` si falta, le aplica las migraciones y le carga la semilla.

    Hace lo mismo que `runtime._preparar_esquema_de_pruebas`, y a propósito: ese camino es
    perezoso --espera a que alguien abra el chat-- y este es el del despliegue. El que llegue
    primero deja al otro sin trabajo, porque las diez migraciones son idempotentes
    (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) y la carga es un UPSERT.

    No se importa `runtime` para reusar su función: importarlo levanta FastAPI y construye la
    app entera, y esto es un script de base de datos. Lo que sí se comparte es lo que puede
    derivar -- el nombre del esquema y la forma de la URL -- que viven en `persistencia`.
    """
    directa = persistencia.url_directa(database_url)
    esquema = persistencia.ESQUEMA_PRUEBAS_WEB
    with persistencia.conectar(directa) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {esquema}")
        conn.commit()

    url = persistencia.url_con_search_path(database_url, esquema)
    with persistencia.conectar(url) as conn:
        aplicadas = persistencia.aplicar_esquema(conn)
        total = persistencia.cargar_base_conocimiento(conn, persistencia.cargar_semilla())
    print(f"  esquema '{esquema}': {len(aplicadas)} migraciones aplicadas, {total} filas "
          f"de conocimiento")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--solo-verificar",
        action="store_true",
        help="No escribe nada: solo comprueba el estado actual de la base.",
    )
    args = parser.parse_args()

    cargar_dotenv()
    try:
        config = Config.desde_entorno()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Conectando a {_enmascarar(config.database_url)}")
    with persistencia.conectar(config.database_url) as conn:
        if not args.solo_verificar:
            aplicadas = persistencia.aplicar_esquema(conn)
            print(f"Esquema aplicado: {', '.join(aplicadas)}")

            semilla = persistencia.cargar_semilla()
            total = persistencia.cargar_base_conocimiento(conn, semilla)
            print(f"Base de conocimiento cargada: {total} filas")

            # El carril de pruebas del panel, que hasta hoy solo se ponia al dia si alguien
            # abria el chat web. Ver el docstring del modulo.
            print()
            print("Carril de pruebas del panel:")
            _poner_al_dia_pruebas_web(config.database_url)

        print()
        print("=" * 78)
        print("VERIFICACIÓN DEL ENTREGABLE DE LA FASE 1")
        print("=" * 78)

        # 1. La ausencia de dato es un dato explícito.
        for tratamiento in ("endodoncia", "protesis"):
            texto = persistencia.consultar_conocimiento(conn, tratamiento, "precio")
            ok = "SIN DATO DOCUMENTADO" in texto
            print(f"\n  consultar_base_conocimiento('{tratamiento}', 'precio')")
            print(f"  -> {'OK  ' if ok else 'FALLA'} {texto[:150]}...")
            if not ok:
                return 1

        # 2. Un dato aprobado sale limpio.
        texto = persistencia.consultar_conocimiento(conn, "cordales", "precio")
        ok = "$300.000" in texto and "SIN DATO" not in texto
        print("\n  consultar_base_conocimiento('cordales', 'precio')")
        print(f"  -> {'OK  ' if ok else 'FALLA'} {texto[:150]}")
        if not ok:
            return 1

        # 3. Un dato que existe pero MaxiCare no ha aprobado sale advertido.
        texto = persistencia.consultar_conocimiento(conn, "implantes", "garantia")
        ok = "PENDIENTE DE APROBACIÓN" in texto
        print("\n  consultar_base_conocimiento('implantes', 'garantia')")
        print(f"  -> {'OK  ' if ok else 'FALLA'} {texto[:150]}...")
        if not ok:
            return 1

        # 4. La configuración operativa quedó en la base, no en el código.
        cfg = persistencia.leer_configuracion(conn)
        print(f"\n  configuración operativa: {cfg}")

        # 5. El control de capacidad es real: el UNIQUE existe de verdad.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM pg_constraint
                WHERE conname = 'uq_reservas_cupo'
                  AND contype = 'u'
                  AND connamespace = 'public'::regnamespace
                """
            )
            # Filtrado por esquema, y no `COUNT(*) == 1` a secas: los esquemas `pruebas` y
            # `pruebas_web` tienen su propia copia de la restricción, así que sin el filtro
            # esto contaba 3 y declaraba FALLA con la base perfectamente sana. Un
            # verificador que grita en rojo cuando todo está bien es uno al que nadie le va
            # a creer el día que tenga razón -- y este vigila el invariante que impide que
            # dos pacientes ocupen el mismo cupo.
            tiene_unique = cur.fetchone()[0] == 1
        print(f"\n  UNIQUE (inicio, cupo_num) en reservas → {'OK' if tiene_unique else 'FALLA'}")
        if not tiene_unique:
            return 1

        # ---------------------------------------------------------------------------
        # 6. Las tablas de la FASE 7, en los DOS esquemas.
        #
        # La verificación del lunes tiene que incluir lo que se despliega el lunes: sin
        # esto, `--solo-verificar` decía OK sobre una base sin la 010 aplicada, que es
        # justo el estado en que el historial no se persiste y el diálogo de cada
        # paciente muere con el proceso.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DEL ENTREGABLE DE LA FASE 7 (el historial persistido)")
        print("=" * 78)

        faltan_en_public = set(TABLAS_DEL_HISTORIAL) - _tablas_de(conn, "public")
        print(f"\n  public: {'OK  ' if not faltan_en_public else 'FALLA'} "
              + (", ".join(TABLAS_DEL_HISTORIAL) if not faltan_en_public
                 else f"faltan {sorted(faltan_en_public)} -- corre este script sin "
                      "--solo-verificar"))
        if faltan_en_public:
            return 1

        esquema_pruebas = persistencia.ESQUEMA_PRUEBAS_WEB
        if not _existe_esquema(conn, esquema_pruebas):
            # Legítimo en una base recién creada: el esquema lo crea este mismo script o
            # la primera apertura del chat. No es un fallo, es que no hay nada que mirar.
            print(f"\n  {esquema_pruebas}: no existe todavía (nada que verificar)")
        else:
            faltan_en_pruebas = set(TABLAS_DEL_HISTORIAL) - _tablas_de(conn, esquema_pruebas)
            al_dia = not faltan_en_pruebas
            print(f"\n  {esquema_pruebas}: {'OK  ' if al_dia else 'FALLA'} "
                  + (", ".join(TABLAS_DEL_HISTORIAL) if al_dia
                     else f"faltan {sorted(faltan_en_pruebas)} -- el carril del chat del "
                          "panel está atrasado y `/clearstate` fallará ahí; corre este "
                          "script sin --solo-verificar"))
            if not al_dia:
                return 1

        # ---------------------------------------------------------------------------
        # 7. Las tablas de la MIGRACIÓN 019 (contacto y consentimiento), en los dos
        #    esquemas. Mismo argumento que el bloque de arriba: la verificación del día
        #    del despliegue tiene que incluir lo que se despliega ese día.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DE LA 019 (contacto y consentimiento)")
        print("=" * 78)

        for esquema in ("public", esquema_pruebas):
            if esquema != "public" and not _existe_esquema(conn, esquema):
                print(f"\n  {esquema}: no existe todavía (nada que verificar)")
                continue
            faltan = set(TABLAS_DEL_CONSENTIMIENTO) - _tablas_de(conn, esquema)
            print(f"\n  {esquema}: {'OK  ' if not faltan else 'FALLA'} "
                  + (", ".join(TABLAS_DEL_CONSENTIMIENTO) if not faltan
                     else f"faltan {sorted(faltan)} -- corre este script sin "
                          "--solo-verificar"))
            if faltan:
                return 1

        # ---------------------------------------------------------------------------
        # 8. La MIGRACIÓN 021 (la bitácora admite `citas`), en los dos esquemas.
        #
        #    No se comprueba una tabla nueva sino un CHECK ampliado, así que no vale el
        #    molde de los dos bloques de arriba: una base con la 021 sin aplicar tiene
        #    todas las tablas en su sitio y pasaría por sana. Y el fallo que esconde es
        #    silencioso -- la marca de asistencia revienta entera, con su UPDATE, la
        #    primera vez que alguien del mostrador la usa.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DE LA 021 Y LA 028 (lo que la bitácora admite)")
        print("=" * 78)

        que_rompe = {
            VALOR_DE_LA_021: "la marca de asistencia del panel está rota",
            VALOR_DE_LA_028: "borrar una conversación desde el panel está roto",
        }
        for esquema in ("public", esquema_pruebas):
            if esquema != "public" and not _existe_esquema(conn, esquema):
                print(f"\n  {esquema}: no existe todavía (nada que verificar)")
                continue
            print()
            for valor, rotura in que_rompe.items():
                admite = _bitacora_admite(conn, esquema, valor)
                print(f"  {esquema}: {'OK  ' if admite else 'FALLA'} "
                      + (f"cambios_configuracion.tabla admite '{valor}'" if admite
                         else f"cambios_configuracion.tabla NO admite '{valor}' -- {rotura} "
                              "(el cambio y su bitácora van en la misma transacción); corre "
                              "este script sin --solo-verificar"))
                if not admite:
                    return 1

        # ---------------------------------------------------------------------------
        # 9. La MIGRACIÓN 022 (las cuotas y el consumo), en los dos esquemas.
        #
        #    Vuelve al molde de los bloques de tablas --la 022 añade tres y un índice--
        #    pero se verifica igual, y por la lección de la 021: una migración sin
        #    bloque de verificación pasa por sana mirando a otro lado.
        #
        #    Lo que esconde si falta: `cuotas.revisar` atrapa toda excepción y devuelve
        #    «permitido» (fallo abierto deliberado), así que sin estas tablas el
        #    perímetro NO frena a nadie y no hay un solo error visible. El freno
        #    silenciosamente apagado es justo el modo de fallo que este proyecto ya
        #    conoce de la degradación de temas al General.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DE LA 022 (las cuotas y el consumo)")
        print("=" * 78)

        for esquema in ("public", esquema_pruebas):
            if esquema != "public" and not _existe_esquema(conn, esquema):
                print(f"\n  {esquema}: no existe todavía (nada que verificar)")
                continue
            faltan = set(TABLAS_DEL_PERIMETRO) - _tablas_de(conn, esquema)
            print(f"\n  {esquema}: {'OK  ' if not faltan else 'FALLA'} "
                  + (", ".join(TABLAS_DEL_PERIMETRO) if not faltan
                     else f"faltan {sorted(faltan)} -- el perímetro NO está frenando a "
                          "nadie y no hay error visible; corre este script sin "
                          "--solo-verificar"))
            if faltan:
                return 1

        # ---------------------------------------------------------------------------
        # 10. Las migraciones 026 y 027 (el hilo completo de Conversaciones).
        #
        #     Una tabla y una columna, verificadas juntas porque comparten modo de
        #     fallo: el hilo se pinta igual y le falta una voz. Ver el comentario de
        #     `COLUMNA_DE_LA_027` para qué esconde cada una.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DE LA 026 Y LA 027 (el hilo de Conversaciones)")
        print("=" * 78)

        tabla_027, columna_027 = COLUMNA_DE_LA_027
        for esquema in ("public", esquema_pruebas):
            if esquema != "public" and not _existe_esquema(conn, esquema):
                print(f"\n  {esquema}: no existe todavía (nada que verificar)")
                continue

            hay_tabla = "mensajes_del_doctor" in _tablas_de(conn, esquema)
            print(f"\n  {esquema}: {'OK  ' if hay_tabla else 'FALLA'} "
                  + ("mensajes_del_doctor (026)" if hay_tabla
                     else "falta mensajes_del_doctor -- el hilo del panel se queda "
                          "«Cargando…» para siempre y lo que escriba un doctor NO se "
                          "guarda; corre este script sin --solo-verificar"))
            if not hay_tabla:
                return 1

            hay_columna = _tiene_columna(conn, esquema, tabla_027, columna_027)
            print(f"  {esquema}: {'OK  ' if hay_columna else 'FALLA'} "
                  + (f"{tabla_027}.{columna_027} (027)" if hay_columna
                     else f"falta {tabla_027}.{columna_027} -- toda nota de voz vuelve a "
                          "decir «(nota de voz)» y el volcado del relevo se las come; corre "
                          "este script sin --solo-verificar"))
            if not hay_columna:
                return 1

        # ---------------------------------------------------------------------------
        # 11. Ninguna clave de tratamiento se queda MUDA.
        #
        #     Una clave sobre la que Daniela no puede decir absolutamente nada no rompe
        #     nada: contesta «lo estoy confirmando con el equipo» y escala. Por eso no
        #     se ve. El 22/09/2026 le costó DOS interrupciones al doctor en cuatro horas
        #     --escalamientos `dato_faltante` de las 17:16 y las 21:04-- por el valor de
        #     la valoración, que MaxiCare tenía escrito y aprobado desde siempre.
        #
        #     `valoracion` era clave de tratamiento desde la 020 y no tenía ni una ficha.
        #     Nadie lo vio en seis días, porque no hay nada que mirar: ni un error, ni un
        #     log, ni una prueba en rojo. Solo un doctor contestando algo que la clínica
        #     ya había contestado.
        #
        #     ADVERTENCIA y no FALLA, a diferencia de los diez bloques de arriba: aquellos
        #     vigilan el esquema --si faltan, el sistema está roto-- y esto es contenido
        #     que la clínica edita desde el panel. Un tratamiento nuevo creado un martes,
        #     con su ficha pendiente para el miércoles, no puede tumbar un despliegue.
        # ---------------------------------------------------------------------------
        print()
        print("=" * 78)
        print("VERIFICACIÓN DEL VOCABULARIO (que ninguna clave se quede muda)")
        print("=" * 78)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.clave
                  FROM tratamientos t
                 WHERE t.activo
                   AND NOT EXISTS (SELECT 1 FROM base_conocimiento b
                                    WHERE b.tratamiento = t.clave)
                   AND NOT EXISTS (SELECT 1 FROM base_conocimiento g
                                    WHERE g.tratamiento = %s AND g.concepto = t.clave)
                 ORDER BY t.clave
                """,
                (persistencia.TRATAMIENTO_GENERAL,),
            )
            # El respaldo 3 de `herramientas._consultar_base_conocimiento` alcanza
            # `_general`/<clave>, así que una clave con eso puesto NO está muda: es
            # exactamente lo que salva hoy a `valoracion`. La consulta pregunta por las dos
            # puertas porque el código entra por las dos.
            mudas = [f[0] for f in cur.fetchall() if f[0] not in MUDOS_A_PROPOSITO]

        if mudas:
            print(f"\n  ADVERTENCIA: {len(mudas)} clave(s) sin una sola ficha de "
                  "conocimiento, ni propia ni en _general:")
            for clave in mudas:
                print(f"    - {clave}: Daniela no puede decir NADA sobre esto. Cada "
                      "pregunta acaba en «lo confirmo con el equipo» y un escalamiento.")
            print("  Se arregla en la pantalla de Tratamientos del panel, o en "
                  "datos/base_conocimiento.json + este script.")
        else:
            print("\n  OK   toda clave activa tiene con qué contestar "
                  f"(salvo {', '.join(MUDOS_A_PROPOSITO)}, mudas a propósito)")

    print("\n" + "=" * 78)
    print("FASE 1 — segunda mitad: OK  ·  FASE 7 — el historial: OK  ·  019 — contacto y "
          "consentimiento: OK  ·  021/028 — la bitácora: OK  ·  022 — el perímetro: "
          "OK  ·  026/027 — el hilo completo: OK  ·  vocabulario: revisado arriba")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

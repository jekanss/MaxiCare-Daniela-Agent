import { useCallback, useEffect, useRef, useState } from 'react'
import {
  borrarConversacion,
  cerrarRelevo,
  descargarExport,
  escribirAlPaciente,
  leerConversaciones,
  leerHilo,
  listarTratamientos,
  loQueSeVaAlBorrar,
  SesionCaducada,
  tomarConversacion,
  type AntesDeBorrar,
  type EstadoConversacion,
  type HiloDeConversacion,
  type MensajeDelHilo,
  type ResumenConversacion,
  type TratamientoFila,
} from '@/api'
import { CargandoPantalla, Fallo } from '@/componentes/Estado'
import {
  Buscador,
  CabeceraDePanel,
  Chip,
  MarcoDeDosPaneles,
  MONO,
  Panel,
  Pastilla,
  Rotulo,
  SG,
  telefonoLegible,
  TituloDePanel,
  Vacio,
  Volver,
} from '@/componentes/Panel'
import { ANCHO_DOS_PANELES, usarEsAngosto } from '@/medidas'

/* La pantalla de Conversaciones: la lista de personas a la izquierda y su hilo completo a la
 * derecha, refrescándose sola.
 *
 * Spec: `docs/superpowers/specs/2026-09-22-pantalla-conversaciones-design.md`.
 *
 * Dos cosas que deciden todo lo demás:
 *
 * 1. La unidad es el TELÉFONO, no la conversación. Lo resuelve el servidor, pero condiciona
 *    esta pantalla entera: la selección es un número, no un id.
 * 2. El estado de cada conversación sale de la LISTA y no del hilo, aunque el hilo se pida
 *    en la misma vuelta. Con las dos rutas devolviéndolo, un desfase entre ellas pintaría
 *    una pantalla que se contradice a sí misma. */

/* `SG`, `MONO` y las piezas de los dos paneles viven en `componentes/Panel.tsx` desde que
 * `SinResolver` adoptó este mismo layout: dos copias de un borde o de un gris se separan
 * en silencio, y la aplicación acaba con dos maneras de decir lo mismo. */

/** Diez segundos, y solo cuando la pestaña está a la vista.
 *
 * El cómputo de Neon ya está encendido las 24 h --dos tareas de fondo lo consultan cada
 * minuto--, así que esto no enciende nada nuevo: añade unos SELECT por índice a una base que
 * ya estaba despierta. Lo que sí evita es una pestaña olvidada consultando toda la noche. */
const REFRESCO_MS = 10_000

type Props = {
  alCaducarSesion: () => void
  /** Una conversacion que otra pantalla pide abrir --hoy «Sin resolver», desde el caso--.
   *  `null` en el caso normal, que es entrar por el menu. */
  seleccionInicial?: string | null
  /** Se llama en cuanto la peticion se atiende, para que `App` la olvide. Sin esto, volver
   *  aqui desde el menu media hora despues reabriria aquella conversacion sola. */
  alConsumirSeleccion?: () => void
}

type Filtro = 'todas' | 'activas' | 'esperando' | 'relevo'

const CHIPS: { id: Filtro; etiqueta: string }[] = [
  { id: 'todas', etiqueta: 'Todas' },
  { id: 'activas', etiqueta: 'Activas' },
  { id: 'esperando', etiqueta: 'Esperando' },
  { id: 'relevo', etiqueta: 'En relevo' },
]

/** El rótulo y los colores de cada estado.
 *
 * `esperando` va en rojo y no en violeta: es el mismo hecho que la tarjeta «Sin contestar»
 * de la portada, y las dos pantallas tienen que señalarlo igual o la clínica aprende dos
 * códigos de color para una sola cosa. */
const TONOS: Record<EstadoConversacion, { texto: string; fondo: string; tinta: string }> = {
  relevo: { texto: 'En relevo', fondo: '#EDE9FE', tinta: '#4C1D95' },
  esperando: { texto: 'Esperando', fondo: '#FEF2F2', tinta: '#B91C1C' },
  activa: { texto: 'Activa', fondo: '#F4F1F9', tinta: '#4A4458' },
  cerrada: { texto: 'Cerrada', fondo: '#F7F6FA', tinta: '#6E6880' },
}

/* Las horas se pintan en la zona de la CLÍNICA, no en la del navegador. Un doctor mirando el
 * panel desde otro huso vería «09:42» donde el paciente escribió a las 16:42, y no habría
 * forma de notarlo. Es la misma decisión que tomó la portada. */
const FMT_HORA = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'America/Bogota',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const FMT_DIA_CORTO = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'America/Bogota',
  day: 'numeric',
  month: 'short',
})

const FMT_DIA_LARGO = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'America/Bogota',
  weekday: 'long',
  day: 'numeric',
  month: 'long',
})

/** La clave del día en Bogotá, para agrupar. `toDateString()` no vale: usa la zona del
 *  navegador, y entonces dos mensajes del mismo día colombiano caerían en días distintos. */
function diaEnBogota(iso: string): string {
  return FMT_DIA_CORTO.format(new Date(iso))
}

/** La hora de la lista: si fue hoy, la hora; si no, el día. Nadie necesita la hora exacta de
 *  un mensaje de hace tres semanas, y ocuparía el sitio del nombre. */
function cuandoCorto(iso: string, hoy: string): string {
  const dia = diaEnBogota(iso)
  return dia === hoy ? FMT_HORA.format(new Date(iso)) : dia
}

/** El teléfono en trozos, como lo escribiría una persona. Los dígitos se quedan tal cual:
 *  esto es presentación, y lo que viaja al servidor es siempre el número crudo. */
function coincide(c: ResumenConversacion, busqueda: string): boolean {
  const q = busqueda.trim().toLowerCase()
  if (!q) return true
  const soloDigitos = q.replace(/\D/g, '')
  if (soloDigitos && c.telefono.includes(soloDigitos)) return true
  return (c.nombre ?? '').toLowerCase().includes(q)
}

function pasaElFiltro(c: ResumenConversacion, filtro: Filtro): boolean {
  if (filtro === 'todas') return true
  if (filtro === 'relevo') return c.estado === 'relevo'
  if (filtro === 'esperando') return c.estado === 'esperando'
  // «Activas» incluye lo que está vivo por cualquier motivo: lo contrario de «cerrada». Un
  // filtro que dejara fuera las tomadas y las que esperan escondería justo lo que pasa hoy.
  return c.estado !== 'cerrada'
}

// ==========================================================================================
// Piezas
// ==========================================================================================

function Insignia({ estado }: { estado: EstadoConversacion }) {
  const t = TONOS[estado]
  return <Pastilla texto={t.texto} fondo={t.fondo} tinta={t.tinta} />
}

function FilaDeLaLista({
  c,
  activa,
  hoy,
  alAbrir,
}: {
  c: ResumenConversacion
  activa: boolean
  hoy: string
  alAbrir: () => void
}) {
  return (
    <li style={{ borderBottom: '1px solid #ECE8F4' }}>
      <button
        type="button"
        onClick={alAbrir}
        className="flex w-full flex-col gap-2 text-left"
        style={{
          borderLeft: `3px solid ${activa ? '#6D28D9' : 'transparent'}`,
          // Sin fondo en línea cuando NO está activa: un `transparent` inline le gana a la
          // clase de hover y el hover dejaría de verse sin que nada falle.
          backgroundColor: activa ? '#F4F1F9' : undefined,
          padding: '15px 16px',
          minHeight: '44px',
          cursor: 'pointer',
        }}
      >
        <span className="flex w-full items-baseline justify-between gap-2">
          <span
            className="truncate"
            style={{
              fontSize: '14.5px',
              fontWeight: 500,
              letterSpacing: '-0.01em',
              color: c.nombre ? '#16111F' : '#4A4458',
            }}
          >
            {/* Sin nombre se pinta el teléfono, nunca un «(sin nombre)»: el número ES su
                identidad mientras no tenga ficha, y es lo que se busca y se marca. */}
            {c.nombre ?? telefonoLegible(c.telefono)}
          </span>
          <span style={{ fontFamily: MONO, fontSize: '10.5px', color: '#6E6880', flex: 'none' }}>
            {cuandoCorto(c.ultimo_en, hoy)}
          </span>
        </span>

        {c.nombre ? (
          <span style={{ fontFamily: MONO, fontSize: '11.5px', color: '#4C1D95' }}>
            {telefonoLegible(c.telefono)}
          </span>
        ) : null}

        <span
          style={{
            fontSize: '13px',
            fontWeight: 300,
            lineHeight: 1.45,
            color: '#4A4458',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {c.vista_previa}
        </span>

        <span className="flex w-full items-center gap-2" style={{ paddingTop: '2px' }}>
          <Insignia estado={c.estado} />
          {c.estado === 'relevo' && c.tomada_por ? (
            <span className="truncate" style={{ fontSize: '11.5px', color: '#6E6880' }}>
              {c.tomada_por}
            </span>
          ) : null}
          {c.sin_contestar > 0 ? (
            <span
              style={{
                marginLeft: 'auto',
                fontFamily: MONO,
                fontSize: '10.5px',
                backgroundColor: '#B91C1C',
                color: '#FFFFFF',
                minWidth: '20px',
                textAlign: 'center',
                padding: '3px 6px',
              }}
              title="mensajes sin responder"
            >
              {c.sin_contestar}
            </span>
          ) : null}
        </span>
      </button>
    </li>
  )
}

/** Una burbuja. El paciente a la izquierda; Daniela y el doctor a la derecha, porque los dos
 *  son «la clínica» desde el punto de vista de quien lee. Alinear al doctor en un tercer sitio
 *  rompería esa lectura, así que lo que cambia es todo lo demás.
 *
 *  Y tuvo que cambiar. Hasta el 22/09/2026 el doctor se distinguía de Daniela por dos lilas
 *  --`#EDE9FE` contra `#F4F1F9`-- y por el nombre en el pie a 10 px: en la pantalla de un
 *  consultorio eso no es una diferencia, es un matiz. Quien lee el hilo necesita saber en un
 *  vistazo qué dijo la máquina y qué dijo una persona, porque de eso depende si hay que
 *  responder. Ahora el mensaje del doctor lleva su nombre ARRIBA, en violeta, con una barra
 *  del mismo color al costado: el contraste es de forma y no de tono, que es lo único que
 *  sobrevive a una pantalla con brillo bajo y a quien distingue mal los colores. */
function Burbuja({ m }: { m: MensajeDelHilo }) {
  const delPaciente = m.quien === 'paciente'
  const delDoctor = m.quien === 'doctor'
  const fallido = Boolean(m.fallo)

  const fondo = fallido ? '#FEF2F2' : delPaciente ? '#FFFFFF' : delDoctor ? '#F5F3FF' : '#F4F1F9'
  const borde = fallido ? '#FECACA' : delPaciente ? '#DCD8E6' : delDoctor ? '#C4B5FD' : '#D6D0E4'

  const quienDice =
    m.quien === 'paciente' ? 'Paciente' : m.quien === 'daniela' ? 'Daniela' : (m.autor ?? 'Doctor')

  return (
    <div className="flex" style={{ justifyContent: delPaciente ? 'flex-start' : 'flex-end' }}>
      <div className="flex flex-col gap-1" style={{ maxWidth: 'min(76%, 540px)' }}>
        {delDoctor && (
          <span
            style={{
              fontFamily: MONO,
              fontSize: '10px',
              fontWeight: 700,
              letterSpacing: '0.12em',
              textTransform: 'uppercase',
              color: fallido ? '#B91C1C' : '#6D28D9',
              textAlign: 'right',
            }}
          >
            Doctor · {quienDice}
          </span>
        )}
        {/* Esto NO lo escribió el paciente: lo dijo, y lo pasó a texto una máquina. Decirlo
            es clínico y no decorativo -- «el 46» y «el 40» suenan casi igual, y quien lee
            una frase sobre un síntoma tiene derecho a saber de quién se está fiando. */}
        {m.voz && (
          <span
            style={{
              fontFamily: MONO,
              fontSize: '10px',
              fontWeight: 700,
              letterSpacing: '0.12em',
              textTransform: 'uppercase',
              color: '#6B7280',
              textAlign: 'left',
            }}
          >
            Nota de voz · transcrita
          </span>
        )}
        <div
          style={{
            backgroundColor: fondo,
            border: `1px solid ${borde}`,
            // La barra va en el costado interior --el que mira al centro del hilo-- para que
            // se lea como el margen de una nota escrita a mano y no como un borde más. En la
            // transcripción va a PUNTOS y del lado del paciente, porque dice otra cosa: que
            // lo de dentro es aproximado.
            borderLeft: delDoctor && !fallido ? '3px solid #7C3AED' : undefined,
            borderRight: m.voz ? '3px dashed #9CA3AF' : undefined,
            padding: '12px 14px',
            fontSize: '14.5px',
            fontWeight: 300,
            lineHeight: 1.6,
            color: '#16111F',
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
          }}
        >
          {m.texto}
        </div>
        <span
          style={{
            fontFamily: MONO,
            fontSize: '10px',
            letterSpacing: '0.1em',
            color: fallido ? '#B91C1C' : '#6E6880',
            textAlign: delPaciente ? 'left' : 'right',
          }}
        >
          {/* Un mensaje que no salió lo DICE en su pie. Sin esto, el hilo mostraría el mismo
              silencio para «el doctor no escribió» y para «escribió y Meta lo rechazó». */}
          {fallido ? 'NO SALIÓ · ' : ''}
          {/* El del doctor ya lleva su nombre arriba; repetirlo aquí solo gastaría el renglón
              que hace legible la hora. */}
          {delDoctor ? '' : `${quienDice} · `}
          {FMT_HORA.format(new Date(m.cuando))}
        </span>
      </div>
    </div>
  )
}

function SeparadorDeDia({ iso }: { iso: string }) {
  return (
    <div className="flex items-center gap-3" style={{ padding: '8px 0 4px' }}>
      <span aria-hidden style={{ flex: 1, height: '1px', backgroundColor: '#DCD8E6' }} />
      <span
        style={{
          fontFamily: MONO,
          fontSize: '10px',
          letterSpacing: '0.16em',
          textTransform: 'uppercase',
          color: '#6E6880',
        }}
      >
        {FMT_DIA_LARGO.format(new Date(iso))}
      </span>
      <span aria-hidden style={{ flex: 1, height: '1px', backgroundColor: '#DCD8E6' }} />
    </div>
  )
}

// ==========================================================================================
// El pie: tomar, escribir y devolver
// ==========================================================================================

/* La única parte de esta pantalla que ESCRIBE, y por eso está apartada del resto.
 *
 * Lo que decide qué se pinta es el estado de la conversación, que viene de la lista: en
 * relevo hay caja de escribir y puerta de salida; fuera de relevo, solo la puerta de
 * entrada. Los botones no se le pintan a `recepcion` --por comodidad, no por seguridad: el
 * control de verdad vive en `exigir_rol`, en el servidor-- y la caja se apaga fuera de la
 * ventana de 24 h de Meta, que también se comprueba allí. */

type PropsDelPie = {
  conversacion: ResumenConversacion
  ventana: HiloDeConversacion['ventana'] | null
  puedeEscribir: boolean
  alCambiar: () => void
  alCaducar: () => void
}

const BOTON = 'px-4 text-sm font-bold transition-all disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95'
const CAMPO = 'w-full px-3 py-2 text-sm outline-none transition-all focus:ring-3 disabled:opacity-60'
const ESTILO_CAMPO = {
  fontFamily: SG,
  backgroundColor: '#FBFAFD',
  border: '1px solid #DCD8E6',
  color: '#16111F',
}

function PieDelHilo({
  conversacion,
  ventana,
  puedeEscribir,
  alCambiar,
  alCaducar,
}: PropsDelPie) {
  const [texto, setTexto] = useState('')
  const [ocupado, setOcupado] = useState(false)
  const [error, setError] = useState('')
  const [cerrando, setCerrando] = useState(false)
  const [hubocita, setHuboCita] = useState(false)
  const [cuando, setCuando] = useState('')
  const [tratamiento, setTratamiento] = useState('')
  const [nombre, setNombre] = useState('')
  const [tratamientos, setTratamientos] = useState<TratamientoFila[]>([])

  const enRelevo = conversacion.estado === 'relevo'
  const puedeMandar = ventana?.puede === true

  // El formulario se cierra solo cuando el relevo deja de estar vivo: si no, se quedaría
  // abierto sobre una conversación que Daniela ya retomó.
  useEffect(() => {
    if (!enRelevo) setCerrando(false)
  }, [enRelevo])

  // El catálogo se pide al abrir el formulario y no al montar la pantalla: la inmensa
  // mayoría de las veces esta pantalla solo se mira.
  useEffect(() => {
    if (!cerrando || tratamientos.length > 0) return
    listarTratamientos()
      .then((filas) => setTratamientos(filas.filter((t) => t.activo)))
      .catch(() => setError('No se pudo cargar la lista de tratamientos.'))
  }, [cerrando, tratamientos.length])

  const hacer = async (accion: () => Promise<unknown>, alTerminar?: () => void) => {
    setOcupado(true)
    setError('')
    try {
      await accion()
      alTerminar?.()
      alCambiar()
    } catch (e) {
      // Sin esto, un `void hacer(...)` dejaría la excepción como una promesa rechazada sin
      // dueño: la sesión caducada se perdería en la consola y el doctor se quedaría mirando
      // un botón que no hace nada.
      if (e instanceof SesionCaducada) alCaducar()
      else setError(e instanceof Error ? e.message : 'No se pudo completar la operación.')
    } finally {
      setOcupado(false)
    }
  }

  if (!puedeEscribir) {
    return (
      <div
        style={{
          borderTop: '1px solid #ECE8F4',
          padding: '12px 20px',
          fontFamily: MONO,
          fontSize: '11px',
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          color: '#9A93AC',
        }}
      >
        Solo lectura
      </div>
    )
  }

  return (
    <div style={{ borderTop: '1px solid #ECE8F4', backgroundColor: '#FFFFFF' }}>
      {error ? (
        <div
          role="alert"
          style={{
            margin: '12px 20px 0',
            padding: '10px 12px',
            backgroundColor: '#FEF2F2',
            border: '1px solid #FECACA',
            fontFamily: SG,
            fontSize: '13px',
            color: '#B91C1C',
          }}
        >
          {error}
        </div>
      ) : null}

      {enRelevo ? (
        <>
          <form
            className="flex gap-2"
            style={{ padding: '14px 20px 10px' }}
            onSubmit={(e) => {
              e.preventDefault()
              const limpio = texto.trim()
              if (!limpio || ocupado) return
              void hacer(() => escribirAlPaciente(conversacion.telefono, limpio), () =>
                setTexto(''),
              )
            }}
          >
            <input
              className={CAMPO}
              style={{ ...ESTILO_CAMPO, flex: 1, minHeight: '44px' }}
              value={texto}
              onChange={(e) => setTexto(e.target.value)}
              maxLength={4000}
              disabled={!puedeMandar || ocupado}
              placeholder={
                ventana === null
                  ? 'Cargando…'
                  : puedeMandar
                    ? 'Escríbele al paciente por WhatsApp…'
                    : 'WhatsApp no deja escribirle ahora mismo'
              }
              aria-label="Mensaje para el paciente"
            />
            <button
              type="submit"
              className={BOTON}
              style={{ backgroundColor: '#6D28D9', color: '#FFFFFF', minHeight: '44px' }}
              disabled={!puedeMandar || ocupado || texto.trim() === ''}
            >
              {ocupado ? 'Enviando…' : 'Enviar'}
            </button>
          </form>

          {ventana !== null && !puedeMandar ? (
            /* La regla es de Meta, no nuestra, y por eso se explica en vez de esconder la
             * caja: un doctor que no sepa por qué no puede escribir da por hecho que el
             * panel está roto. No se ofrece una plantilla -- eso es otra decisión y otro
             * coste. */
            <p
              style={{
                padding: '0 20px 12px',
                fontFamily: SG,
                fontSize: '12.5px',
                lineHeight: 1.5,
                color: '#6E6880',
              }}
            >
              {ventana?.horas == null
                ? 'Este número nunca ha escrito, así que WhatsApp no deja mandarle un mensaje.'
                : `Pasaron ${Math.round(ventana.horas)} horas desde su último mensaje. WhatsApp
                   solo deja escribirle dentro de las 24 h siguientes: hay que esperar a que
                   él vuelva a escribir.`}
            </p>
          ) : null}

          <div
            className="flex flex-wrap items-center gap-3"
            style={{ padding: '10px 20px 14px', borderTop: '1px solid #F4F1F9' }}
          >
            <span className="min-w-0 flex-1" style={{ fontFamily: SG, fontSize: '12.5px', color: '#6E6880' }}>
              {conversacion.tomada_por
                ? `La tiene ${conversacion.tomada_por}. Daniela está callada.`
                : 'Daniela está callada mientras dure el relevo.'}
            </span>
            {!cerrando ? (
              <button
                type="button"
                className={BOTON}
                style={{ border: '1px solid #DCD8E6', color: '#4A4458', minHeight: '38px' }}
                onClick={() => setCerrando(true)}
                disabled={ocupado}
              >
                Devolvérsela a Daniela
              </button>
            ) : null}
          </div>

          {cerrando ? (
            <FormularioDeCierre
              conversacion={conversacion}
              tratamientos={tratamientos}
              hubocita={hubocita}
              setHuboCita={setHuboCita}
              cuando={cuando}
              setCuando={setCuando}
              tratamiento={tratamiento}
              setTratamiento={setTratamiento}
              nombre={nombre}
              setNombre={setNombre}
              ocupado={ocupado}
              alCancelar={() => setCerrando(false)}
              alDevolver={() =>
                void hacer(
                  () =>
                    cerrarRelevo(conversacion.telefono, {
                      hubo_cita: hubocita,
                      cuando: hubocita ? cuando : null,
                      tratamiento: hubocita ? tratamiento : null,
                      nombre: hubocita && nombre.trim() ? nombre.trim() : null,
                    }),
                  () => {
                    // Solo si el cierre salió bien. Un 409 --la hora se llenó-- deja el
                    // formulario tal cual para escribir otra, que es lo mismo que hace el
                    // diálogo del hilo de Telegram.
                    setCerrando(false)
                    setHuboCita(false)
                    setCuando('')
                    setTratamiento('')
                    setNombre('')
                  },
                )
              }
            />
          ) : null}
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-3" style={{ padding: '14px 16px' }}>
          {/* `basis-full` hasta `xl`: con el botón al lado la frase se quedaba con 120 px y
              salía en cuatro líneas de tres palabras, y no solo en un móvil -- también en el
              panel de detalle de una tableta en horizontal. Mismo criterio que la cabecera. */}
          <span className="min-w-0 basis-full xl:flex-1 xl:basis-0" style={{ fontFamily: SG, fontSize: '12.5px', color: '#6E6880' }}>
            {/* Tomarla fuera de la ventana de 24 h deja el estado peligroso de la regla del
                relevo: Daniela callada y nadie pudiendo hablarle. No se prohíbe --el
                paciente puede escribir en cualquier momento y entonces sí se le contesta--
                pero el doctor tiene que saberlo ANTES de pulsar. */}
            {ventana !== null && !puedeMandar
              ? 'Daniela está atendiendo. Ojo: ahora mismo WhatsApp no deja escribirle, así que si la tomas ella dejará de contestarle hasta que el paciente escriba o se la devuelvas.'
              : 'Daniela está atendiendo esta conversación.'}
          </span>
          <button
            type="button"
            className={BOTON}
            style={{ backgroundColor: '#6D28D9', color: '#FFFFFF', minHeight: '38px' }}
            disabled={ocupado}
            onClick={() => void hacer(() => tomarConversacion(conversacion.telefono))}
          >
            {ocupado ? 'Un momento…' : 'Hablar yo con el paciente'}
          </button>
        </div>
      )}
    </div>
  )
}

type PropsDelCierre = {
  conversacion: ResumenConversacion
  tratamientos: TratamientoFila[]
  hubocita: boolean
  setHuboCita: (v: boolean) => void
  cuando: string
  setCuando: (v: string) => void
  tratamiento: string
  setTratamiento: (v: string) => void
  nombre: string
  setNombre: (v: string) => void
  ocupado: boolean
  alCancelar: () => void
  alDevolver: () => void
}

/** Las cuatro preguntas que en Telegram van encadenadas, aquí de una vez.
 *
 *  El nombre solo se pide cuando falta, como allí: preguntar de más es como se consigue que
 *  el doctor deje el formulario a medias, y un formulario a medias es una cita sin registrar.
 *
 *  Marcar «sí hubo cita» la CREA de verdad --cupo, Google Calendar y fila--. Si no se puede,
 *  no se cierra nada y esto sigue puesto. */
function FormularioDeCierre(p: PropsDelCierre) {
  const faltaElNombre = !p.conversacion.nombre
  const listo = !p.hubocita || (p.cuando !== '' && p.tratamiento !== '')

  return (
    <div style={{ padding: '4px 20px 18px', borderTop: '1px solid #F4F1F9' }}>
      <Rotulo>Antes de devolverla</Rotulo>

      <div className="mt-2 flex flex-wrap gap-2">
        {[
          { valor: false, etiqueta: 'No hubo cita' },
          { valor: true, etiqueta: 'Sí, quedó agendada' },
        ].map((o) => (
          <button
            key={String(o.valor)}
            type="button"
            onClick={() => p.setHuboCita(o.valor)}
            className="px-3 py-2 text-sm transition-colors"
            style={{
              fontFamily: SG,
              fontWeight: p.hubocita === o.valor ? 700 : 500,
              backgroundColor: p.hubocita === o.valor ? '#EDE9FE' : '#FFFFFF',
              border: `1px solid ${p.hubocita === o.valor ? '#6D28D9' : '#DCD8E6'}`,
              color: p.hubocita === o.valor ? '#4C1D95' : '#4A4458',
            }}
          >
            {o.etiqueta}
          </button>
        ))}
      </div>

      {p.hubocita ? (
        <div className="mt-3 flex flex-wrap gap-3">
          {faltaElNombre ? (
            <label className="flex flex-col gap-1" style={{ flex: '1 1 14rem' }}>
              <span style={{ fontFamily: SG, fontSize: '12px', fontWeight: 600, color: '#4A4458' }}>
                Nombre del paciente
              </span>
              <input
                className={CAMPO}
                style={ESTILO_CAMPO}
                value={p.nombre}
                maxLength={80}
                onChange={(e) => p.setNombre(e.target.value)}
                placeholder="Para que la agenda no diga «PENDIENTE»"
              />
            </label>
          ) : null}

          <label className="flex flex-col gap-1" style={{ flex: '1 1 12rem' }}>
            <span style={{ fontFamily: SG, fontSize: '12px', fontWeight: 600, color: '#4A4458' }}>
              De qué es
            </span>
            <select
              className={CAMPO}
              style={ESTILO_CAMPO}
              value={p.tratamiento}
              onChange={(e) => p.setTratamiento(e.target.value)}
            >
              <option value="">Elige uno…</option>
              {p.tratamientos.map((t) => (
                <option key={t.clave} value={t.clave}>
                  {t.etiqueta}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1" style={{ flex: '1 1 12rem' }}>
            <span style={{ fontFamily: SG, fontSize: '12px', fontWeight: 600, color: '#4A4458' }}>
              Cuándo
            </span>
            <input
              type="datetime-local"
              className={CAMPO}
              style={ESTILO_CAMPO}
              value={p.cuando}
              onChange={(e) => p.setCuando(e.target.value)}
            />
          </label>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={BOTON}
          style={{ backgroundColor: '#6D28D9', color: '#FFFFFF', minHeight: '38px' }}
          disabled={p.ocupado || !listo}
          onClick={p.alDevolver}
        >
          {p.ocupado ? 'Devolviendo…' : 'Devolver a Daniela'}
        </button>
        <button
          type="button"
          className={BOTON}
          style={{ border: '1px solid #DCD8E6', color: '#4A4458', minHeight: '38px' }}
          disabled={p.ocupado}
          onClick={p.alCancelar}
        >
          Cancelar
        </button>
        {p.hubocita ? (
          <span style={{ fontFamily: SG, fontSize: '12px', color: '#6E6880' }}>
            La cita se crea de verdad: toma el cupo y entra en el calendario de la clínica.
          </span>
        ) : null}
      </div>
    </div>
  )
}

// ==========================================================================================
// La pantalla
// ==========================================================================================

/* ---------------------------------------------------------------- Exportar y borrar */

/** Un botón pequeño de cabecera. `peligro` lo pinta en rojo y no es decoración: borrar es la
 *  única acción de esta pantalla que destruye algo, y tiene que verse distinta de las otras
 *  tres antes de pulsarla, no después. */
function AccionDeCabecera({
  children, alPulsar, ocupado = false, peligro = false, activo = false,
}: {
  children: React.ReactNode
  alPulsar: () => void
  ocupado?: boolean
  peligro?: boolean
  activo?: boolean
}) {
  const tinta = peligro ? '#B91C1C' : '#4C1D95'
  const borde = peligro ? '#FECACA' : '#DCD8E6'
  return (
    <button
      type="button"
      onClick={alPulsar}
      disabled={ocupado}
      className="transition-all disabled:cursor-not-allowed disabled:opacity-40 hover:brightness-95"
      style={{
        backgroundColor: activo ? (peligro ? '#FEF2F2' : '#EDE9FE') : '#FFFFFF',
        color: tinta,
        border: `1px solid ${borde}`,
        fontFamily: MONO,
        fontSize: '10.5px',
        letterSpacing: '0.1em',
        textTransform: 'uppercase',
        fontWeight: 700,
        padding: '7px 11px',
        minHeight: '32px',
      }}
    >
      {children}
    </button>
  )
}

/** El formulario de export por rango. Vive plegado dentro de la cabecera de la lista.
 *
 *  Las dos fechas son opcionales y se combinan: sin ninguna sale todo, con una sola sale
 *  desde o hasta ahí. El servidor INCLUYE el día de «hasta» entero -- pedir del 1 al 30 y que
 *  falte el 30 es la clase de recorte que no se nota hasta que falta el mensaje que se
 *  buscaba. */
function PanelDeExport({
  alExportar, ocupado,
}: {
  alExportar: (desde: string, hasta: string) => void
  ocupado: boolean
}) {
  const [desde, setDesde] = useState('')
  const [hasta, setHasta] = useState('')
  const invertido = Boolean(desde && hasta && desde > hasta)

  return (
    <div
      className="flex flex-wrap items-end gap-2"
      style={{ backgroundColor: '#FAF9FC', border: '1px solid #ECE8F4', padding: '12px' }}
    >
      {([['Desde', desde, setDesde], ['Hasta', hasta, setHasta]] as const).map(
        ([etiqueta, valor, poner]) => (
          <label key={etiqueta} className="flex min-w-0 flex-col gap-1">
            <span style={{ fontFamily: MONO, fontSize: '9.5px', letterSpacing: '0.12em', color: '#6E6880' }}>
              {etiqueta.toUpperCase()}
            </span>
            <input
              type="date"
              value={valor}
              onChange={(e) => poner(e.target.value)}
              className={CAMPO}
              style={{ ...ESTILO_CAMPO, minHeight: '34px', width: '9.5rem' }}
            />
          </label>
        ),
      )}
      <button
        type="button"
        onClick={() => alExportar(desde, hasta)}
        disabled={ocupado || invertido}
        className={`${BOTON} py-2`}
        style={{ backgroundColor: '#6D28D9', color: '#FFFFFF', minHeight: '34px' }}
      >
        {ocupado ? 'Preparando…' : 'Descargar CSV'}
      </button>
      <p style={{ margin: 0, flexBasis: '100%', fontSize: '11.5px', color: invertido ? '#B91C1C' : '#6E6880' }}>
        {invertido
          ? 'La fecha «desde» es posterior a «hasta».'
          : 'Sin fechas se descargan todas las conversaciones. El día de «hasta» se incluye.'}
      </p>
    </div>
  )
}

/** La confirmación de borrado. Pide los datos al servidor en vez de suponerlos.
 *
 *  Lo que de verdad justifica esta ventana es la línea de las citas: sobreviven al borrado
 *  --es lo que eligió MaxiCare-- pero **su recordatorio no**, porque los seguimientos cuelgan
 *  de la conversación con borrado en cascada. Un paciente que se queda sin el aviso de su
 *  cita es el precio de esta acción, y tiene que estar escrito donde alguien lo paga. */
function ConfirmarBorrado({
  telefono, nombre, alCerrar, alBorrado, alCaducar,
}: {
  telefono: string
  nombre: string
  alCerrar: () => void
  alBorrado: () => void
  alCaducar: () => void
}) {
  const [datos, setDatos] = useState<AntesDeBorrar | null>(null)
  const [error, setError] = useState('')
  const [borrando, setBorrando] = useState(false)

  useEffect(() => {
    let vivo = true
    loQueSeVaAlBorrar(telefono)
      .then((d) => { if (vivo) setDatos(d) })
      .catch((e) => {
        if (e instanceof SesionCaducada) alCaducar()
        else if (vivo) setError('No se pudo leer qué contiene esta conversación.')
      })
    return () => { vivo = false }
    // `alCaducar` se omite a propósito: llega distinta en cada render y meterla aquí
    // repetiría la petición en bucle. Es la misma trampa de `alCaducarSesion`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [telefono])

  // Escape cierra. Una ventana destructiva que solo se puede cerrar acertándole a un botón
  // es una ventana que se acaba confirmando por inercia.
  useEffect(() => {
    const alTeclear = (e: KeyboardEvent) => { if (e.key === 'Escape') alCerrar() }
    document.addEventListener('keydown', alTeclear)
    return () => document.removeEventListener('keydown', alTeclear)
  }, [alCerrar])

  const borrar = async () => {
    setBorrando(true)
    setError('')
    try {
      await borrarConversacion(telefono)
      alBorrado()
    } catch (e) {
      if (e instanceof SesionCaducada) alCaducar()
      else setError(e instanceof Error ? e.message : 'No se pudo borrar.')
      setBorrando(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(22, 17, 31, 0.55)' }}
      onClick={(e) => { if (e.target === e.currentTarget) alCerrar() }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="titulo-borrado"
        className="flex w-full flex-col gap-4 overflow-y-auto"
        style={{
          maxWidth: '30rem', maxHeight: '90vh', backgroundColor: '#FFFFFF',
          border: '1px solid #DCD8E6', padding: '22px',
        }}
      >
        <h2
          id="titulo-borrado"
          style={{ margin: 0, fontFamily: SG, fontWeight: 600, fontSize: '18px', letterSpacing: '-0.022em', color: '#16111F' }}
        >
          ¿Borrar la conversación de {nombre}?
        </h2>

        {datos === null && !error ? (
          <CargandoPantalla
            que="Comprobando qué contiene…"
            detalle="Cuántos mensajes se van y si hay citas futuras."
            fondo="#FFFFFF"
          />
        ) : null}

        {datos !== null ? (
          <>
            <ul className="m-0 flex list-none flex-col gap-2 p-0" style={{ fontSize: '13.5px', color: '#2C2439' }}>
              <li>
                <strong>Se borran {datos.mensajes} mensajes</strong> --lo que escribió el
                paciente, lo que contestó Daniela y lo que le escribió la clínica-- y el
                historial del agente. Daniela empieza de cero con este número.
              </li>
              <li>
                <strong>Se conservan sus citas</strong> y sus eventos en Google Calendar, su
                ficha de paciente y su hilo en el grupo de Telegram.
              </li>
            </ul>

            {datos.citas_futuras.length > 0 ? (
              <div role="alert" style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A', padding: '12px' }}>
                <p style={{ margin: '0 0 6px', fontFamily: MONO, fontSize: '10.5px', letterSpacing: '0.1em', color: '#92400E' }}>
                  OJO · {datos.citas_futuras.length === 1 ? 'UNA CITA FUTURA' : `${datos.citas_futuras.length} CITAS FUTURAS`}
                </p>
                <ul className="m-0 flex list-none flex-col gap-1 p-0" style={{ fontSize: '13px', color: '#78350F' }}>
                  {datos.citas_futuras.map((c) => (
                    <li key={c.inicio}>
                      {FMT_DIA_LARGO.format(new Date(c.inicio))} · {FMT_HORA.format(new Date(c.inicio))} · {c.tratamiento}
                    </li>
                  ))}
                </ul>
                <p style={{ margin: '8px 0 0', fontSize: '13px', color: '#78350F' }}>
                  La cita sigue en pie, pero <strong>su recordatorio se borra con la
                  conversación</strong>: el paciente no recibirá el aviso de las 24 horas ni
                  el de las 2 horas.
                </p>
              </div>
            ) : null}

            <p style={{ margin: 0, fontSize: '13px', color: '#6E6880' }}>
              Esto no se puede deshacer. Queda registrado quién lo hizo y cuándo.
            </p>
          </>
        ) : null}

        {error ? (
          <p role="alert" style={{ margin: 0, fontSize: '13px', color: '#B91C1C' }}>{error}</p>
        ) : null}

        <div className="flex flex-wrap justify-end gap-2">
          <button
            type="button"
            onClick={alCerrar}
            disabled={borrando}
            className={`${BOTON} py-2`}
            style={{ backgroundColor: '#FFFFFF', color: '#4A4458', border: '1px solid #DCD8E6' }}
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={() => void borrar()}
            disabled={borrando || datos === null}
            className={`${BOTON} py-2`}
            style={{ backgroundColor: '#B91C1C', color: '#FFFFFF' }}
          >
            {borrando ? 'Borrando…' : 'Borrar la conversación'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Conversaciones({
  alCaducarSesion,
  seleccionInicial = null,
  alConsumirSeleccion,
}: Props) {
  const [lista, setLista] = useState<ResumenConversacion[]>([])
  const [hilo, setHilo] = useState<HiloDeConversacion | null>(null)
  const [seleccion, setSeleccion] = useState<string | null>(null)
  /* Por debajo de `ANCHO_DOS_PANELES` la pantalla ensena UN panel: la lista, o el
     detalle con su boton de volver. Es la unica decision responsive que no puede ser
     una clase de CSS, porque depende de si hay algo seleccionado. */
  const unaColumna = usarEsAngosto(ANCHO_DOS_PANELES)
  const [busqueda, setBusqueda] = useState('')
  const [filtro, setFiltro] = useState<Filtro>('todas')
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')
  /* El fallo del HILO va aparte del de la lista: son dos peticiones distintas y fallan por
     separado. Y se distingue de `hilo === null` a propósito --uno es «todavía no llega» y el
     otro «no va a llegar»--, que es justo lo que esta pantalla no sabía decir. */
  const [errorHilo, setErrorHilo] = useState('')
  // El rol, ya resuelto por el servidor. No es el control de acceso --ese vive en
  // `exigir_rol`-- sino lo que evita pintarle a recepción botones que van a devolver 403.
  const [puedoEscribir, setPuedoEscribir] = useState(false)
  const [soyAdmin, setSoyAdmin] = useState(false)

  // Exportar y borrar. `porBorrar` guarda el teléfono cuya confirmación está abierta, y no un
  // booleano: la lista se refresca cada diez segundos por debajo de la ventana, y con un
  // booleano bastaría un cambio de selección para que la confirmación acabara apuntando a
  // otra persona sin cambiar de texto.
  const [porBorrar, setPorBorrar] = useState<string | null>(null)
  const [exportAbierto, setExportAbierto] = useState(false)
  const [exportando, setExportando] = useState(false)
  const [avisoExport, setAvisoExport] = useState('')

  // Las dos vueltas de refresco, guardadas para poder pedirlas a mano después de escribir.
  // Por `ref` y no por una dependencia más: meter un contador en las deps de los `useEffect`
  // de abajo reiniciaría los dos intervalos en cada mensaje enviado.
  const refrescarLista = useRef<() => void>(() => {})
  const refrescarHilo = useRef<() => void>(() => {})

  const trasEscribir = useCallback(() => {
    refrescarLista.current()
    refrescarHilo.current()
  }, [])

  // Por `ref` y NO en las deps: `App.tsx` pasa una flecha nueva en cada render, y meterla en
  // deps dejaría la pantalla releyendo Neon en bucle. La trampa está en `web/CLAUDE.md`.
  const caducar = useRef(alCaducarSesion)
  caducar.current = alCaducarSesion

  // La peticion de otra pantalla, atendida UNA vez. `alConsumirSeleccion` va por `ref` por lo
  // mismo que `alCaducarSesion`: en las deps, `App` la recrea en cada render y esto correria
  // en bucle. Es la trampa de `web/CLAUDE.md`.
  const consumir = useRef(alConsumirSeleccion)
  consumir.current = alConsumirSeleccion
  useEffect(() => {
    if (!seleccionInicial) return
    setSeleccion(seleccionInicial)
    consumir.current?.()
  }, [seleccionInicial])

  const hoy = diaEnBogota(new Date().toISOString())

  // --- La lista: se refresca siempre que la pestaña esté a la vista --------------------
  useEffect(() => {
    let vivo = true

    const tic = async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const datos = await leerConversaciones()
        if (!vivo) return
        setLista(datos.conversaciones)
        setPuedoEscribir(datos.puede_escribir)
        setSoyAdmin(datos.es_admin)
        setError('')
      } catch (e) {
        if (e instanceof SesionCaducada) caducar.current()
        else if (vivo) setError('No se pudo cargar la lista de conversaciones.')
      } finally {
        if (vivo) setCargando(false)
      }
    }

    refrescarLista.current = () => void tic()
    void tic()
    const reloj = window.setInterval(() => void tic(), REFRESCO_MS)
    // Al volver a la pestaña se pide de inmediato: esperar hasta diez segundos para ver algo
    // vivo es justo lo que esta pantalla promete no hacer.
    const alVolver = () => {
      if (document.visibilityState === 'visible') void tic()
    }
    document.addEventListener('visibilitychange', alVolver)
    return () => {
      vivo = false
      window.clearInterval(reloj)
      document.removeEventListener('visibilitychange', alVolver)
    }
  }, [])

  // --- El hilo: solo el seleccionado --------------------------------------------------
  useEffect(() => {
    if (!seleccion) {
      setHilo(null)
      return
    }
    let vivo = true
    // Se limpia al cambiar de persona. Sin esto, durante la décima de segundo que tarda la
    // petición se verían los mensajes del paciente ANTERIOR bajo el nombre del nuevo.
    setHilo(null)
    setErrorHilo('')

    const tic = async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const datos = await leerHilo(seleccion)
        if (vivo) {
          setHilo(datos)
          setErrorHilo('')
        }
      } catch (e) {
        if (e instanceof SesionCaducada) caducar.current()
        // Hasta el 22/09/2026 aquí no había nada: cualquier fallo que no fuera la sesión se
        // tragaba en silencio, `hilo` se quedaba en `null` y la caja mostraba «Cargando…»
        // PARA SIEMPRE. Una avería que se lee como lentitud es la peor clase de avería,
        // porque nadie la reporta: se espera.
        else if (vivo) setErrorHilo('No se pudo cargar esta conversación.')
      }
    }

    refrescarHilo.current = () => void tic()
    void tic()
    const reloj = window.setInterval(() => void tic(), REFRESCO_MS)
    const alVolver = () => {
      if (document.visibilityState === 'visible') void tic()
    }
    document.addEventListener('visibilitychange', alVolver)
    return () => {
      vivo = false
      window.clearInterval(reloj)
      document.removeEventListener('visibilitychange', alVolver)
    }
  }, [seleccion])

  // --- El scroll del hilo -------------------------------------------------------------
  const caja = useRef<HTMLDivElement | null>(null)
  const pegadoAbajo = useRef(true)

  const alDesplazar = useCallback(() => {
    const el = caja.current
    if (!el) return
    // 40 px de margen: quien está leyendo el final sigue contando como «abajo» aunque haya
    // movido la rueda un poco.
    pegadoAbajo.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }, [])

  useEffect(() => {
    const el = caja.current
    // Solo si ya estaba abajo. Arrastrar el scroll de alguien que subió a leer lo de ayer,
    // cada diez segundos, haría la pantalla inservible justo cuando más se usa.
    if (el && pegadoAbajo.current) el.scrollTop = el.scrollHeight
  }, [hilo])

  const visibles = lista.filter((c) => pasaElFiltro(c, filtro) && coincide(c, busqueda))
  const elegida = seleccion ? lista.find((c) => c.telefono === seleccion) : undefined

  // Una sola función para las dos formas de exportar: con teléfono baja una conversación, sin
  // él bajan todas o el rango. El `finally` es obligatorio -- si la descarga falla y el botón
  // se queda en «Preparando…», la pantalla se cierra sobre sí misma.
  const exportar = async (filtros: { telefono?: string; desde?: string; hasta?: string }) => {
    setExportando(true)
    setAvisoExport('')
    try {
      await descargarExport(filtros)
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else setAvisoExport(e instanceof Error ? e.message : 'No se pudo exportar.')
    } finally {
      setExportando(false)
    }
  }

  // La pantalla entera, no media: `cargando` solo es cierto en la PRIMERA carga --se pone a
  // `false` y nunca vuelve a `true`--, así que el refresco de cada diez segundos no la tapa.
  if (cargando) return <CargandoPantalla que="Cargando las conversaciones…" />

  return (
    <>
      <MarcoDeDosPaneles etiqueta="Conversaciones">
        {/* ------------------------------------------------------------------ La lista */}
        {unaColumna && seleccion ? null : (
        <Panel etiqueta="Listado de conversaciones" peso="1 1 320px">
          <CabeceraDePanel>
            <div className="flex items-baseline justify-between gap-3">
              <TituloDePanel>Conversaciones</TituloDePanel>
              <div className="flex shrink-0 items-center gap-2">
                <Rotulo>
                  {visibles.length === lista.length
                    ? `${lista.length} ${lista.length === 1 ? 'hilo' : 'hilos'}`
                    : `${visibles.length} de ${lista.length}`}
                </Rotulo>
                {soyAdmin ? (
                  <AccionDeCabecera
                    alPulsar={() => setExportAbierto((a) => !a)}
                    activo={exportAbierto}
                  >
                    Exportar
                  </AccionDeCabecera>
                ) : null}
              </div>
            </div>

            {soyAdmin && exportAbierto ? (
              <PanelDeExport
                ocupado={exportando}
                alExportar={(desde, hasta) => void exportar({ desde, hasta })}
              />
            ) : null}

            {avisoExport ? (
              <p role="alert" style={{ margin: 0, fontSize: '12.5px', color: '#B91C1C' }}>
                {avisoExport}
              </p>
            ) : null}

            <Buscador
              valor={busqueda}
              alCambiar={setBusqueda}
              marcador="Buscar por nombre o teléfono"
              etiqueta="Buscar conversaciones"
            />

            <div className="flex flex-wrap gap-2">
              {CHIPS.map((chip) => (
                <Chip
                  key={chip.id}
                  puesto={filtro === chip.id}
                  alPulsar={() => setFiltro(chip.id)}
                >
                  {chip.etiqueta}
                </Chip>
              ))}
            </div>
          </CabeceraDePanel>

          {error ? (
            // Si la consulta falló, la pantalla lo DICE. Nunca una lista vieja sin avisar:
            // una pantalla «en vivo» congelada es peor que una que reconoce que no sabe.
            // Y con salida: antes solo quedaba recargar la página entera.
            <div className="px-4 py-6">
              <Fallo mensaje={error} alReintentar={() => refrescarLista.current()} />
            </div>
          ) : lista.length === 0 ? (
            <Vacio>Todavía no ha escrito nadie.</Vacio>
          ) : visibles.length === 0 ? (
            <Vacio>
              {busqueda.trim()
                ? 'Nadie con ese nombre ni ese número.'
                : filtro === 'esperando'
                  ? 'Ninguna conversación está esperando respuesta.'
                  : filtro === 'relevo'
                    ? 'Nadie tiene ninguna conversación tomada ahora mismo.'
                    : 'Ninguna conversación activa.'}
            </Vacio>
          ) : (
            <ul className="m-0 flex min-h-0 flex-1 list-none flex-col overflow-y-auto p-0">
              {visibles.map((c) => (
                <FilaDeLaLista
                  key={c.telefono}
                  c={c}
                  hoy={hoy}
                  activa={c.telefono === seleccion}
                  alAbrir={() => setSeleccion(c.telefono)}
                />
              ))}
            </ul>
          )}
        </Panel>
        )}

        {/* -------------------------------------------------------------------- El hilo */}
        {/* En una columna, el hueco «selecciona una conversación» no se pinta: ya está la
            lista entera ocupando la pantalla, y un panel que dice «elige algo» debajo de la
            cosa que hay que elegir es ruido. */}
        {unaColumna && !seleccion ? null : (
        <Panel etiqueta="Detalle de la conversación" peso="2 1 380px">
          {!seleccion || !elegida ? (
            <Vacio>Selecciona una conversación para ver el historial.</Vacio>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">
              <div
                className="flex flex-wrap items-center gap-2 sm:gap-3"
                style={{ padding: '14px 16px', borderBottom: '1px solid #ECE8F4' }}
              >
                {/* La salida. Sin esto, abrir una conversación en un móvil es un camino sin
                    retorno: el hilo ocupa la pantalla entera y la lista no está en ninguna
                    parte. Se esconde solo en escritorio, donde la lista sigue al lado. */}
                <Volver alPulsar={() => setSeleccion(null)} que="Lista" />
                <span
                  aria-hidden
                  className="hidden items-center justify-center sm:flex"
                  style={{ width: '40px', height: '40px', flex: 'none', backgroundColor: '#EDE9FE', color: '#4C1D95' }}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="square">
                    <circle cx="12" cy="8.5" r="3.6" />
                    <path d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6" />
                  </svg>
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-1">
                  <span
                    className="truncate"
                    style={{ fontFamily: SG, fontWeight: 600, fontSize: '17px', letterSpacing: '-0.022em', color: '#16111F' }}
                  >
                    {elegida.nombre ?? telefonoLegible(elegida.telefono)}
                  </span>
                  {/* `whitespace-nowrap`: en un móvil de 360 px el número se partía en TRES
                      líneas («+57 321 / 497 / 3105») y empujaba el nombre a «cano…». Un
                      teléfono partido no se lee ni se dicta, que es justo para lo que está. */}
                  <span className="whitespace-nowrap" style={{ fontFamily: MONO, fontSize: '11.5px', color: '#4C1D95' }}>
                    {telefonoLegible(elegida.telefono)}
                  </span>
                </span>
                {/* Los tres controles en su propio bloque, ocupando la fila entera hasta que
                    de verdad quepan al lado del nombre. Sueltos entre los hermanos competían
                    con él por el ancho y la cabecera salía en cuatro filas con todo estrujado.
                    El corte es `xl` y no `sm`, y la diferencia se vio en una captura: lo que
                    manda es el ancho del PANEL, que es dos tercios de lo que sobra tras el
                    menú, así que a 1024 px de ventana este panel mide ~400 y el nombre salía
                    como «canom…». Desde 1280 hay sitio y vuelven a la derecha, como siempre. */}
                <span className="flex w-full flex-wrap items-center gap-2 xl:w-auto">
                  <Insignia estado={elegida.estado} />
                  {/* Exportar UNA conversación lo puede hacer cualquiera que ya la esté
                      viendo: el archivo no enseña nada que no estuviera en pantalla. Borrar
                      es de admin, y es lo único de aquí que destruye algo. */}
                  <AccionDeCabecera
                    alPulsar={() => void exportar({ telefono: elegida.telefono })}
                    ocupado={exportando}
                  >
                    {exportando ? 'Preparando…' : 'Exportar'}
                  </AccionDeCabecera>
                  {soyAdmin ? (
                    <AccionDeCabecera peligro alPulsar={() => setPorBorrar(elegida.telefono)}>
                      Borrar
                    </AccionDeCabecera>
                  ) : null}
                </span>
              </div>

              <div
                ref={caja}
                onScroll={alDesplazar}
                className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
                style={{ backgroundColor: '#F7F6FA', padding: '20px' }}
              >
                {/* Un refresco que falla con la conversación YA en pantalla no la borra: se
                    avisa y se deja leer lo que hay. Vaciar el hilo por un tropiezo de diez
                    segundos le quitaría al doctor lo que estaba leyendo. */}
                {hilo !== null && errorHilo ? (
                  <div
                    role="alert"
                    className="shrink-0 rounded-lg px-3 py-2"
                    style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}
                  >
                    <span style={{ fontFamily: MONO, fontSize: '10.5px', color: '#B91C1C' }}>
                      NO SE PUDO ACTUALIZAR · lo de abajo puede estar desactualizado
                    </span>
                  </div>
                ) : null}

                {hilo === null && errorHilo ? (
                  <div className="flex flex-1 items-center justify-center px-2">
                    <Fallo
                      mensaje={errorHilo}
                      alReintentar={() => refrescarHilo.current()}
                      className="w-full"
                      estilo={{ maxWidth: 440 }}
                    />
                  </div>
                ) : hilo === null ? (
                  /* El nombre ya está arriba en la cabecera, así que el loader dice lo que la
                     cabecera no dice: que los mensajes vienen en camino. `hilo` vuelve a
                     `null` SOLO al cambiar de persona --el refresco de diez segundos no lo
                     toca-- así que esto no parpadea mientras se lee. */
                  <CargandoPantalla
                    que="Cargando la conversación…"
                    detalle="Trayendo los mensajes de este paciente."
                  />
                ) : hilo.mensajes.length === 0 ? (
                  <Vacio>
                    No hay nada escrito en este hilo. Puede que esta persona solo haya mandado
                    archivos, o que su conversación sea anterior al registro.
                  </Vacio>
                ) : (
                  hilo.mensajes.map((m, i) => {
                    const anterior = i > 0 ? hilo.mensajes[i - 1] : null
                    const nuevoDia =
                      !anterior || diaEnBogota(anterior.cuando) !== diaEnBogota(m.cuando)
                    return (
                      <div key={`${m.cuando}-${i}`} className="flex flex-col gap-3">
                        {nuevoDia ? <SeparadorDeDia iso={m.cuando} /> : null}
                        <Burbuja m={m} />
                      </div>
                    )
                  })
                )}
              </div>

              {/* El pie NO lleva `flex-1`: la caja de arriba se queda con todo el alto que
                  sobra y esto se ancla abajo, así que el scroll sigue siendo solo del
                  historial. */}
              <PieDelHilo
                conversacion={elegida}
                ventana={hilo?.ventana ?? null}
                puedeEscribir={puedoEscribir}
                alCambiar={trasEscribir}
                alCaducar={alCaducarSesion}
              />
            </div>
          )}
        </Panel>
        )}
      </MarcoDeDosPaneles>

      {porBorrar ? (
        <ConfirmarBorrado
          telefono={porBorrar}
          nombre={
            lista.find((c) => c.telefono === porBorrar)?.nombre ?? telefonoLegible(porBorrar)
          }
          alCerrar={() => setPorBorrar(null)}
          alBorrado={() => {
            setPorBorrar(null)
            // La selección se suelta ANTES de refrescar: el hilo que estaba a la vista ya no
            // existe, y dejarlo seleccionado pediría un hilo vacío y pintaría una cabecera
            // con el nombre de alguien que ya no está en la lista.
            setSeleccion(null)
            refrescarLista.current()
          }}
          alCaducar={() => caducar.current()}
        />
      ) : null}
    </>
  )
}

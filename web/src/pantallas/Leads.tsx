import { useCallback, useEffect, useRef, useState } from 'react'
import { leerLeads, SesionCaducada, type BarreraLead, type EstadoLead, type Lead } from '@/api'
import { CargandoPantalla, Fallo } from '@/componentes/Estado'
import {
  AccionDeCabecera,
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

/* «Leads»: quién ha escrito, qué quiere y qué lo frena.
 *
 * Hasta el 23/09/2026 esta sección era un cartel de «todavía no construida». Su motivo para
 * esperar --«conversaciones reales acumuladas; con la base vacía, la pantalla más útil del
 * producto se ve idéntica a una rota»-- dejó de valer: hay dieciséis personas con su estado
 * comercial registrado, y ese dato, que Daniela reescribe en CADA turno, no se veía en
 * ninguna pantalla del panel. Existía y solo se leía a sí mismo.
 *
 * Dos paneles, como Conversaciones, «Sin resolver» y Tratamientos: el layout lo pone
 * `componentes/Panel.tsx` y no se decide aquí.
 *
 * ------------------------------------------------------------------------------------------
 * Las cuatro decisiones que tomó esta pantalla, y por qué
 * ------------------------------------------------------------------------------------------
 *
 * 1. ES DE SOLO LECTURA, y con UNA salida: abrir la conversación. No es una limitación
 *    temporal. El estado lo escribe Daniela --«no es una temperatura de lead: es dónde está
 *    esta persona en su proceso»--, así que un desplegable para cambiarlo a mano prometería
 *    algo que el siguiente turno del modelo pisaría sin avisar. Y un botón de «reactivar a
 *    esta persona» reintroduciría por la puerta del panel el disparo manual que la decisión
 *    D1 de la reactivación descartó a propósito. Lo que esta pantalla hace es DESCUBRIR a
 *    quién hay que atender; atender se hace en Conversaciones, que ya sabe.
 *
 * 2. LA UNIDAD ES LA PERSONA, NO LA CONVERSACIÓN. Una conversación caduca a las 24 h, así que
 *    alguien que lleva una semana preguntando son varias filas en `conversaciones` y UNA
 *    aquí. Sin agrupar, la lista enseñaba a la misma persona cuatro veces.
 *
 * 3. «CON BARRERA» Y LA BARRERA SE LEEN JUNTAS, NUNCA POR SEPARADO. `estado='con_barrera'`
 *    con `barrera='ninguna'` no es una objeción comercial: significa que la conversación se
 *    detuvo por algo que no es del paciente --una avería--, y existe precisamente para que un
 *    fallo técnico no se cuele en las métricas como si fuera un cliente dudando. Esta pantalla
 *    lo pinta distinto y con otro color, porque son dos trabajos de dos personas distintas.
 *
 * 4. NO SE PINTA DE QUÉ ANUNCIO LLEGÓ NADIE. El cartel que esta pantalla sustituye lo
 *    prometía, y el dato no existe: el bloque `referral` de los click-to-WhatsApp de Meta no
 *    se lee en la ingesta, así que llega al servidor y se tira. El precedente del proyecto es
 *    explícito --la fase 8 se topó con lo mismo con el `origen` del paciente-- y dice que en
 *    ese caso la columna sale, no se rellena con `PENDIENTE` en una pantalla de cara al
 *    usuario. Construirla exige antes una migración y leer el `referral`; mientras eso no
 *    pase, una columna vacía solo enseñaría que falta.
 *
 * No se refresca sola, al revés que Conversaciones: la cartera de treinta días no cambia
 * mientras se lee, y un refresco movería bajo el cursor la persona que estás mirando. */

type Props = {
  alCaducarSesion: () => void
  /** Abre esa conversación en la pantalla de Conversaciones. Lo resuelve `App`, que es quien
   *  sabe cambiar de sección; aquí solo se sabe QUÉ persona interesa. Es el mismo camino que
   *  usa «Sin resolver». */
  alAbrirConversacion: (telefono: string) => void
}

// ==========================================================================================
// El vocabulario, con el nombre que ve la clínica
// ==========================================================================================

/* Los seis estados del vocabulario cerrado de `contratos.py`, traducidos.
 *
 * Los nombres crudos NO se pintan en ninguna parte: `listo_para_agendar` es una etiqueta de
 * base de datos y quien mira esta pantalla no tiene por qué leer guiones bajos. El `titulo`
 * es la frase larga detrás de una palabra en versalitas -- sin él, «Frenado» y «Detenido»
 * serían dos sinónimos en vez de dos cosas distintas. */
const ESTADOS: Record<EstadoLead, { etiqueta: string; fondo: string; tinta: string; titulo: string }> = {
  explorando: {
    etiqueta: 'Explorando',
    fondo: '#F4F1F9',
    tinta: '#4A4458',
    titulo: 'Escribió y todavía está mirando. Aún no se sabe qué quiere.',
  },
  comparando: {
    etiqueta: 'Comparando',
    fondo: '#FEF3C7',
    tinta: '#92400E',
    titulo: 'Está midiendo opciones. Puede estar hablando con otra clínica.',
  },
  con_barrera: {
    etiqueta: 'Frenado',
    fondo: '#FFEDD5',
    tinta: '#9A3412',
    titulo: 'Quiere, pero hay algo concreto que lo detiene.',
  },
  listo_para_agendar: {
    etiqueta: 'Listo',
    fondo: '#DCFCE7',
    tinta: '#166534',
    // La frase dice el TRABAJO, no una idea de marketing. Aquí decía «es el grupo más barato
    // de convertir que existe», que es cierto y viene de la especificación de la
    // reactivación, pero en la pantalla de una clínica suena a jerga: quien la lee necesita
    // saber qué hacer, no por qué conviene.
    titulo: 'Pidió hora. Si no tiene cita, es a quien hay que llamar primero.',
  },
  agendado: {
    etiqueta: 'Agendado',
    fondo: '#EDE9FE',
    tinta: '#4C1D95',
    titulo: 'Daniela le dio una hora.',
  },
  post_atencion: {
    etiqueta: 'Ya vino',
    fondo: '#F4F1F9',
    tinta: '#6E6880',
    titulo: 'Ya pasó por la clínica.',
  },
}

/* El séptimo caso, que NO es un estado: esa persona escribió y nadie llegó a registrar nada
 * sobre ella. No se colapsa con «Explorando» --que es una fase real-- porque «no sabemos
 * nada» y «está mirando» llevan a trabajos distintos, y este es justo el grupo que el filtro
 * principal de la pantalla existe para sacar a la luz. */
const SIN_REGISTRAR = {
  etiqueta: 'Sin registrar',
  fondo: '#FFFFFF',
  tinta: '#6E6880',
  titulo: 'Escribió, pero no quedó registrado en qué punto está.',
}

/* `con_barrera` + `barrera: 'ninguna'`. Va en rojo y no en ámbar a propósito: esto no lo
 * arregla quien vende, lo arregla quien programa. Mismo criterio que «Falla técnica» en la
 * pantalla de «Sin resolver». */
const DETENIDO = {
  etiqueta: 'Detenido',
  fondo: '#FEF2F2',
  tinta: '#B91C1C',
  titulo:
    'La conversación se detuvo por algo que NO es una objeción del paciente. Suele ser una ' +
    'avería: mira esta conversación antes de llamar.',
}

/** Cómo se nombra cada barrera dentro de una frase: «Lo frena el precio». */
const BARRERAS: Record<BarreraLead, string> = {
  precio: 'el precio',
  miedo: 'el miedo',
  tiempo: 'no tener tiempo',
  desplazamiento: 'llegar hasta la sede',
  confianza: 'la confianza',
  comparacion: 'estar comparando',
  ninguna: '',
}

type Filtro = 'todos' | 'listos' | 'barrera' | 'averiguar' | 'baja'

/* Los rótulos repiten la palabra de la PASTILLA, no una sinónima: el chip que aísla a los
 * frenados dice «Frenados» porque su pastilla dice «Frenado». Con dos palabras para una idea,
 * la clínica aprende dos códigos para lo mismo -- que es el motivo por el que esta pantalla
 * tampoco se inventó una insignia propia y usa la `Pastilla` de todas las demás. */
const CHIPS: { id: Filtro; etiqueta: string }[] = [
  { id: 'todos', etiqueta: 'Todos' },
  { id: 'listos', etiqueta: 'Listos' },
  { id: 'barrera', etiqueta: 'Frenados' },
  { id: 'averiguar', etiqueta: 'Por averiguar' },
  { id: 'baja', etiqueta: 'Baja' },
]

// ==========================================================================================
// Lo que cada lead significa
// ==========================================================================================

/* Las cinco preguntas que la pantalla le hace a un lead, en un solo sitio.
 *
 * Están juntas porque los chips, las filas y el detalle tienen que responderlas IGUAL: con la
 * condición escrita tres veces, un día el chip dice «2 listos» y la lista enseña tres, y no
 * hay forma de saber cuál de las dos miente. */

/** Detenido por algo que no es del paciente. Ver `DETENIDO`. */
function esAveria(l: Lead): boolean {
  return l.estado === 'con_barrera' && l.barrera === 'ninguna'
}

/** Una barrera de verdad: hay una y la puso el paciente. */
function frenoReal(l: Lead): BarreraLead | null {
  return l.barrera && l.barrera !== 'ninguna' ? l.barrera : null
}

/** Quiere hora y no la tiene. El trabajo más urgente de esta pantalla: alguien que pidió cita
 *  y sigue sin ella. */
function listoSinCita(l: Lead): boolean {
  return l.estado === 'listo_para_agendar' && !l.cita
}

/** Algo va a pasar con esta persona sin que nadie mueva un dedo: tiene cita por delante, o
 *  hay un seguimiento en cola. */
function tieneAlgoEnMarcha(l: Lead): boolean {
  return Boolean(l.cita) || Boolean(l.programado_en)
}

/** EL FILTRO PRINCIPAL, el que la especificación llamaba «sin estado ni actividad
 *  programada» -- una frase que aparecía una sola vez en todo el proyecto y que nadie había
 *  definido nunca.
 *
 *  Aquí significa: no se sabe qué quiere Y no va a pasar nada solo. Las dos mitades hacen
 *  falta. Sin la primera, el filtro deja pasar a quien ya se sabe que quiere ortodoncia, que
 *  es un trabajo distinto --llamarlo-- y no el de esta lista. Sin la segunda, mete a quien ya
 *  tiene cita el martes, que no necesita nada de nadie.
 *
 *  Lo que queda es el grupo donde la clínica aporta lo único que el sistema no puede:
 *  averiguar qué necesita alguien que escribió y se quedó a medias. */
function porAveriguar(l: Lead): boolean {
  return !l.tratamiento && !tieneAlgoEnMarcha(l)
}

function pasaElFiltro(l: Lead, filtro: Filtro): boolean {
  if (filtro === 'todos') return true
  if (filtro === 'listos') return listoSinCita(l)
  if (filtro === 'barrera') return frenoReal(l) !== null
  if (filtro === 'averiguar') return porAveriguar(l)
  return l.baja
}

function coincide(l: Lead, busqueda: string): boolean {
  const q = busqueda.trim().toLowerCase()
  if (!q) return true
  // El teléfono se busca por dígitos: quien teclea «319 684» está buscando un número y no
  // debería tener que saber cómo lo guarda la base.
  const soloDigitos = q.replace(/\D/g, '')
  if (soloDigitos && l.telefono.includes(soloDigitos)) return true
  return (
    (l.nombre ?? '').toLowerCase().includes(q) ||
    (l.tratamiento ?? '').toLowerCase().includes(q) ||
    // Las notas entran a propósito: es donde Daniela escribió «le recomendaron retirar cuatro
    // cordales», y buscar «cordales» tiene que encontrarlo aunque el tratamiento esté vacío.
    (l.notas ?? '').toLowerCase().includes(q)
  )
}

// ==========================================================================================
// Fechas
// ==========================================================================================

/* En la zona de la CLÍNICA, no en la del navegador. Un doctor mirando desde otro huso vería
 * «09:42» donde el paciente escribió a las 16:42 y no habría forma de notarlo. Es la misma
 * decisión que tomaron la portada y Conversaciones. */
const FMT_DIA = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'America/Bogota',
  day: 'numeric',
  month: 'short',
})

const FMT_DIA_Y_HORA = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'America/Bogota',
  weekday: 'long',
  day: 'numeric',
  month: 'long',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function dia(iso: string): string {
  return FMT_DIA.format(new Date(iso))
}

function diaYHora(iso: string): string {
  return FMT_DIA_Y_HORA.format(new Date(iso))
}

/** «hoy», «ayer», «hace 6 días». La antigüedad es el dato que decide si hay que correr, y una
 *  fecha suelta obliga a hacer la resta mentalmente cada vez. */
function hace(iso: string): string {
  const dias = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
  if (dias <= 0) return 'hoy'
  if (dias === 1) return 'ayer'
  return `hace ${dias} días`
}

/** El primer día que entra en la lista, en largo. La pantalla lo dice porque «16 personas» no
 *  significa nada sin saber de cuánto tiempo. */
function desdeCuando(iso: string): string | null {
  const d = new Date(`${iso}T12:00:00`)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleDateString('es-CO', { day: 'numeric', month: 'long' })
}

// ==========================================================================================
// Piezas
// ==========================================================================================

/** La pastilla de estado. Resuelve los tres casos --sin registrar, avería y estado normal--
 *  en un solo sitio, que es lo que impide que la lista y el detalle discrepen. */
function Insignia({ lead }: { lead: Lead }) {
  const t = !lead.estado ? SIN_REGISTRAR : esAveria(lead) ? DETENIDO : ESTADOS[lead.estado]
  return <Pastilla texto={t.etiqueta} fondo={t.fondo} tinta={t.tinta} titulo={t.titulo} />
}

/** Una fila de la lista.
 *
 *  Tres renglones y ni uno más: quién es, en qué punto está, y qué se sabe de lo que quiere.
 *  Lo que NO cabe aquí --las notas de Daniela, la cita, los seguimientos-- es justo lo que
 *  hace falta para decidir, y por eso hay un panel de detalle al lado. */
function FilaDelLead({
  lead,
  activa,
  alAbrir,
}: {
  lead: Lead
  activa: boolean
  alAbrir: () => void
}) {
  const freno = frenoReal(lead)
  return (
    <li style={{ borderBottom: '1px solid #F1EEF7' }}>
      <button
        type="button"
        onClick={alAbrir}
        className="w-full text-left transition-colors hover:brightness-[0.98]"
        style={{
          backgroundColor: activa ? '#F4F1F9' : 'transparent',
          // La franja de la izquierda es lo que hace que la selección se vea de reojo sin
          // tener que comparar dos grises casi iguales.
          borderLeft: `3px solid ${activa ? '#6D28D9' : 'transparent'}`,
          padding: '13px 15px',
          cursor: 'pointer',
        }}
      >
        <div className="flex items-baseline justify-between gap-2">
          <span
            className="min-w-0 truncate"
            style={{ fontFamily: SG, fontWeight: 600, fontSize: '14px', color: '#16111F' }}
          >
            {lead.nombre ?? telefonoLegible(lead.telefono)}
          </span>
          <span
            className="shrink-0"
            style={{ fontFamily: MONO, fontSize: '10px', color: '#8B849C' }}
          >
            {hace(lead.ultimo_en)}
          </span>
        </div>

        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Insignia lead={lead} />
          {/* Estas tres marcas responden «¿hay que hacer algo?» sin abrir a nadie. Solo se
              pintan cuando son ciertas: una fila con cinco pastillas apagadas no se lee. */}
          {lead.cita ? (
            <Pastilla
              texto="Con cita"
              fondo="#EDE9FE"
              tinta="#4C1D95"
              titulo={`Tiene cita el ${diaYHora(lead.cita.inicio)}.`}
            />
          ) : null}
          {lead.programado_en ? (
            <Pastilla
              texto="En cola"
              fondo="#F4F1F9"
              tinta="#4A4458"
              titulo="Hay un mensaje de seguimiento programado para esta persona."
            />
          ) : null}
          {lead.baja ? (
            <Pastilla
              texto="Baja"
              fondo="#FEF2F2"
              tinta="#B91C1C"
              titulo="Pidió no recibir mensajes comerciales. No se le escribe para vender."
            />
          ) : null}
        </div>

        <p
          className="mt-1.5 truncate"
          style={{ margin: 0, fontSize: '12.5px', lineHeight: 1.4, color: '#6E6880' }}
        >
          {lead.tratamiento ? (
            <>
              Quiere <strong style={{ fontWeight: 600, color: '#4A4458' }}>{lead.tratamiento}</strong>
              {freno ? ` · lo frena ${BARRERAS[freno]}` : ''}
            </>
          ) : (
            'Todavía no se sabe qué quiere'
          )}
        </p>
      </button>
    </li>
  )
}

/** Un bloque del detalle, con su rótulo en versalitas. */
function Apartado({ rotulo, children }: { rotulo: string; children: React.ReactNode }) {
  return (
    <section style={{ padding: '16px 18px', borderBottom: '1px solid #F1EEF7' }}>
      <Rotulo>{rotulo}</Rotulo>
      <div className="mt-2">{children}</div>
    </section>
  )
}

/** Una frase del detalle. */
function Frase({ children, apagado = false }: { children: React.ReactNode; apagado?: boolean }) {
  return (
    <p
      style={{
        margin: 0,
        fontSize: '13.5px',
        lineHeight: 1.55,
        color: apagado ? '#8B849C' : '#4A4458',
      }}
    >
      {children}
    </p>
  )
}

/** La persona entera. */
function Detalle({
  lead,
  alAbrirConversacion,
}: {
  lead: Lead
  alAbrirConversacion: (telefono: string) => void
}) {
  const freno = frenoReal(lead)
  const t = !lead.estado ? SIN_REGISTRAR : esAveria(lead) ? DETENIDO : ESTADOS[lead.estado]

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      {/* --------------------------------------------------------------------- La cabecera */}
      {/* `xl:` y no `sm:`: el corte lo decide el ancho del PANEL, no el de la ventana. Este
          panel son dos tercios de lo que sobra tras el menú --a 1024 px de ventana mide unos
          400--, y con el botón en la misma fila un nombre largo salía cortado. Es la misma
          decisión que tomó la cabecera del hilo en Conversaciones. */}
      <div
        className="flex shrink-0 flex-col gap-3 xl:flex-row xl:items-start xl:justify-between"
        style={{ padding: '18px', borderBottom: '1px solid #ECE8F4' }}
      >
        <div className="min-w-0">
          <TituloDePanel>{lead.nombre ?? telefonoLegible(lead.telefono)}</TituloDePanel>
          <p
            className="mt-1"
            style={{ margin: 0, fontFamily: MONO, fontSize: '11.5px', color: '#6E6880' }}
          >
            {telefonoLegible(lead.telefono)}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          {/* La ÚNICA acción de la pantalla, y es de navegación: escribir se hace en
              Conversaciones, que sabe si la ventana de 24 h de Meta sigue abierta y si el rol
              puede. Duplicar aquí esa caja obligaría a duplicar también las dos comprobaciones
              que la protegen. */}
          <AccionDeCabecera alPulsar={() => alAbrirConversacion(lead.telefono)}>
            Abrir conversación
          </AccionDeCabecera>
        </div>
      </div>

      {/* ------------------------------------------------------------------ En qué punto está */}
      <Apartado rotulo="En qué punto está">
        <div className="flex flex-wrap items-center gap-2">
          <Insignia lead={lead} />
          {lead.estado_en ? (
            <span style={{ fontFamily: MONO, fontSize: '10.5px', color: '#8B849C' }}>
              DESDE {dia(lead.estado_en).toUpperCase()}
            </span>
          ) : null}
        </div>
        <div className="mt-2">
          <Frase>{t.titulo}</Frase>
        </div>
        {freno ? (
          <div className="mt-2">
            <Frase>
              Lo frena <strong style={{ fontWeight: 600, color: '#16111F' }}>{BARRERAS[freno]}</strong>.
            </Frase>
          </div>
        ) : null}
        {lead.fuera_de_alcance ? (
          <div className="mt-2">
            <Frase>Pidió algo que la clínica no hace.</Frase>
          </div>
        ) : null}
      </Apartado>

      {/* ------------------------------------------------------------------------ Qué quiere */}
      <Apartado rotulo="Qué quiere">
        {lead.tratamiento ? (
          <Frase>
            <strong style={{ fontWeight: 600, color: '#16111F' }}>{lead.tratamiento}</strong>
          </Frase>
        ) : (
          <Frase apagado>
            Todavía no se sabe. Es lo que esta persona necesita que alguien averigüe.
          </Frase>
        )}
      </Apartado>

      {/* ------------------------------------------------------------ Lo que anotó Daniela */}
      {/* Es lo único de esta pantalla redactado para que lo lea una persona, y lo que hace que
          abrir una conversación sea una decisión y no una lotería. Va antes que la actividad
          porque es lo que de verdad se lee. */}
      <Apartado rotulo="Lo que anotó Daniela">
        {lead.notas ? (
          <Frase>{lead.notas}</Frase>
        ) : (
          <Frase apagado>Sin notas.</Frase>
        )}
      </Apartado>

      {/* -------------------------------------------------------------------- Qué va a pasar */}
      <Apartado rotulo="Qué va a pasar">
        {lead.cita ? (
          <Frase>
            Tiene cita de{' '}
            <strong style={{ fontWeight: 600, color: '#16111F' }}>{lead.cita.tratamiento}</strong>{' '}
            el {diaYHora(lead.cita.inicio)}.
          </Frase>
        ) : lead.programado_en ? (
          <Frase>
            Hay un mensaje de seguimiento en cola para el {diaYHora(lead.programado_en)}.{' '}
            <span style={{ color: '#8B849C' }}>
              Que esté en cola no garantiza que salga: antes pasa por las guardas de horario,
              de contacto reciente y de calidad del número.
            </span>
          </Frase>
        ) : (
          <Frase apagado>
            Nada. No tiene cita por delante ni hay ningún mensaje programado: si nadie lo
            toca, aquí se acaba.
          </Frase>
        )}
      </Apartado>

      {/* ------------------------------------------------------------------------- Contacto */}
      <Apartado rotulo="Contacto">
        <Frase>
          Escribió por última vez el {diaYHora(lead.ultimo_en)} ({hace(lead.ultimo_en)}).
        </Frase>
        <div className="mt-2">
          <Frase apagado>
            {lead.seguimientos_enviados === 0
              ? 'No se le ha mandado ningún seguimiento.'
              : `Se le han mandado ${lead.seguimientos_enviados} ${
                  lead.seguimientos_enviados === 1 ? 'seguimiento' : 'seguimientos'
                }.`}
          </Frase>
        </div>
        {lead.baja ? (
          <div className="mt-2">
            <Frase>
              <strong style={{ fontWeight: 600, color: '#B91C1C' }}>
                Pidió no recibir mensajes comerciales
              </strong>
              {lead.baja_origen === 'clinica' ? ', y lo registró la clínica' : ''}. Se respeta
              para siempre. No apaga el recordatorio de una cita suya: eso no es comercial.
            </Frase>
          </div>
        ) : null}
      </Apartado>

      {/* Lo que esta pantalla NO puede decir, dicho una vez y al final. Callarlo haría que la
          ausencia se leyera como un olvido; ponerlo arriba le daría un peso que no tiene. */}
      <div style={{ padding: '14px 18px 20px' }}>
        <p
          style={{
            margin: 0,
            fontSize: '11.5px',
            lineHeight: 1.5,
            color: '#8B849C',
          }}
        >
          De qué anuncio llegó esta persona no se sabe: WhatsApp lo manda y el sistema
          todavía no lo guarda.
        </p>
      </div>
    </div>
  )
}

// ==========================================================================================
// La pantalla
// ==========================================================================================

export default function Leads({ alCaducarSesion, alAbrirConversacion }: Props) {
  const [leads, setLeads] = useState<Lead[]>([])
  const [desde, setDesde] = useState<string | null>(null)
  const [dias, setDias] = useState(30)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')
  const [seleccion, setSeleccion] = useState<string | null>(null)
  const [busqueda, setBusqueda] = useState('')
  const [filtro, setFiltro] = useState<Filtro>('todos')
  /* Por debajo de `ANCHO_DOS_PANELES` la pantalla enseña UN panel: la lista, o el detalle con
     su botón de volver. Es la única decisión responsive que no puede ser una clase de CSS,
     porque depende de si hay algo seleccionado. */
  const unaColumna = usarEsAngosto(ANCHO_DOS_PANELES)

  // Por `ref` y NO en las deps del `useCallback`: `App` pasa esta prop como una flecha nueva
  // en cada render, así que meterla en las dependencias deja la pantalla releyendo Neon en
  // bucle. Es la trampa documentada en `web/CLAUDE.md`.
  const caducar = useRef(alCaducarSesion)
  caducar.current = alCaducarSesion

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      const datos = await leerLeads()
      setLeads(datos.leads)
      setDesde(datos.desde)
      setDias(datos.dias)
      setError('')
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else setError('No se pudo cargar la cartera.')
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  const visibles = leads.filter((l) => pasaElFiltro(l, filtro) && coincide(l, busqueda))
  const elegido = seleccion ? leads.find((l) => l.telefono === seleccion) : undefined

  /* La cuenta va DENTRO del chip, no en una frase de la cabecera.
   *
   * Es lo que aprendió Tratamientos: allí la cabecera decía «2 tratamientos sin precio» y ahí
   * se acababa -- para saber CUÁLES había que ir abriendo uno por uno. Un número que además
   * es el botón que aísla a esos, convierte un dato muerto en el trabajo del día. */
  const cuentas: Record<Filtro, number> = {
    todos: leads.length,
    listos: leads.filter(listoSinCita).length,
    barrera: leads.filter((l) => frenoReal(l) !== null).length,
    averiguar: leads.filter(porAveriguar).length,
    baja: leads.filter((l) => l.baja).length,
  }

  // La pantalla entera, no media. `cargando` solo es cierto en la primera carga.
  if (cargando) return <CargandoPantalla que="Cargando la cartera…" />

  return (
    <MarcoDeDosPaneles etiqueta="Leads">
      {/* --------------------------------------------------------------------------- La lista */}
      {unaColumna && elegido ? null : (
        <Panel etiqueta="Listado de leads" peso="1 1 320px">
          <CabeceraDePanel>
            <div className="flex items-baseline justify-between gap-3">
              <TituloDePanel>Leads</TituloDePanel>
              <Rotulo>
                {visibles.length === leads.length
                  ? `${leads.length} ${leads.length === 1 ? 'persona' : 'personas'}`
                  : `${visibles.length} de ${leads.length}`}
              </Rotulo>
            </div>

            <p style={{ margin: 0, fontSize: '12.5px', lineHeight: 1.45, color: '#6E6880' }}>
              Quién ha escrito, qué quiere y qué lo frena. Esta pantalla solo informa: para
              escribirle a alguien, ábrelo.
            </p>

            {desdeCuando(desde ?? '') ? (
              <p
                style={{
                  margin: 0,
                  fontFamily: MONO,
                  fontSize: '10.5px',
                  letterSpacing: '0.06em',
                  lineHeight: 1.5,
                  color: '#4C1D95',
                }}
              >
                ÚLTIMOS {dias} DÍAS · DESDE EL {desdeCuando(desde ?? '')?.toUpperCase()}
              </p>
            ) : null}

            <Buscador
              valor={busqueda}
              alCambiar={setBusqueda}
              marcador="Buscar por nombre, teléfono o tratamiento"
              etiqueta="Buscar leads"
            />

            <div className="flex flex-wrap gap-2">
              {CHIPS.map((chip) => (
                <Chip
                  key={chip.id}
                  puesto={filtro === chip.id}
                  alPulsar={() => setFiltro(chip.id)}
                >
                  {chip.etiqueta} {cuentas[chip.id]}
                </Chip>
              ))}
            </div>
          </CabeceraDePanel>

          {error ? (
            // Con salida. `alReintentar` aquí es honesto porque esto es una LECTURA: sobre una
            // escritura, ese botón prometería un reintento que no hace.
            <div className="px-4 py-6">
              <Fallo mensaje={error} alReintentar={() => void recargar()} />
            </div>
          ) : leads.length === 0 ? (
            <Vacio>
              Nadie ha escrito en los últimos {dias} días. Cuando alguien lo haga, aparecerá
              aquí con lo que Daniela averigüe de él.
            </Vacio>
          ) : visibles.length === 0 ? (
            <Vacio>
              {busqueda.trim()
                ? 'Nadie coincide con esa búsqueda.'
                : filtro === 'listos'
                  ? 'Nadie está esperando hora ahora mismo.'
                  : filtro === 'baja'
                    ? 'Nadie ha pedido dejar de recibir mensajes.'
                    : 'Nadie en ese grupo.'}
            </Vacio>
          ) : (
            <ul className="m-0 flex min-h-0 flex-1 list-none flex-col overflow-y-auto p-0">
              {visibles.map((lead) => (
                <FilaDelLead
                  key={lead.telefono}
                  lead={lead}
                  activa={lead.telefono === seleccion}
                  alAbrir={() => setSeleccion(lead.telefono)}
                />
              ))}
            </ul>
          )}
        </Panel>
      )}

      {/* -------------------------------------------------------------------------- El detalle */}
      {/* En una columna no se pinta el hueco de «selecciona a alguien»: la lista ya ocupa la
          pantalla, y un panel que pide elegir debajo de lo que hay que elegir es ruido. */}
      {unaColumna && !elegido ? null : (
        <Panel etiqueta="Detalle del lead" peso="2 1 380px">
          {!elegido ? (
            <Vacio>Selecciona a alguien para ver qué quiere y qué lo frena.</Vacio>
          ) : (
            <>
              {/* La salida del detalle cuando solo cabe un panel. Se esconde sola en
                  escritorio, donde la lista sigue estando al lado. */}
              <div
                className="flex shrink-0 lg:hidden"
                style={{ padding: '12px 14px', borderBottom: '1px solid #ECE8F4' }}
              >
                <Volver alPulsar={() => setSeleccion(null)} que="Leads" />
              </div>
              <Detalle lead={elegido} alAbrirConversacion={alAbrirConversacion} />
            </>
          )}
        </Panel>
      )}
    </MarcoDeDosPaneles>
  )
}

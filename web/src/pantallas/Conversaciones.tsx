import { useCallback, useEffect, useRef, useState } from 'react'
import {
  leerConversaciones,
  leerHilo,
  SesionCaducada,
  type EstadoConversacion,
  type HiloDeConversacion,
  type MensajeDelHilo,
  type ResumenConversacion,
} from '@/api'

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

const SG = "'Space Grotesk', sans-serif"
const MONO = "'JetBrains Mono', monospace"

/** Diez segundos, y solo cuando la pestaña está a la vista.
 *
 * El cómputo de Neon ya está encendido las 24 h --dos tareas de fondo lo consultan cada
 * minuto--, así que esto no enciende nada nuevo: añade unos SELECT por índice a una base que
 * ya estaba despierta. Lo que sí evita es una pestaña olvidada consultando toda la noche. */
const REFRESCO_MS = 10_000

type Props = { alCaducarSesion: () => void }

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
function telefonoLegible(tel: string): string {
  const m = /^57(\d{3})(\d{3})(\d{4})$/.exec(tel)
  return m ? `+57 ${m[1]} ${m[2]} ${m[3]}` : `+${tel}`
}

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
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: '9.5px',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        backgroundColor: t.fondo,
        color: t.tinta,
        padding: '4px 8px',
        whiteSpace: 'nowrap',
      }}
    >
      {t.texto}
    </span>
  )
}

function Rotulo({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: '10.5px',
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: '#6E6880',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  )
}

function Vacio({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center px-6 py-10 text-center">
      <span
        className="text-sm"
        style={{ fontWeight: 300, lineHeight: 1.6, color: '#6E6880', maxWidth: '32ch' }}
      >
        {children}
      </span>
    </div>
  )
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
 *  son «la clínica» desde el punto de vista de quien lee. Lo que los distingue es el pie y el
 *  color del borde, no el lado: alinear al doctor en un tercer sitio rompería la lectura. */
function Burbuja({ m }: { m: MensajeDelHilo }) {
  const delPaciente = m.quien === 'paciente'
  const fallido = Boolean(m.fallo)

  const fondo = fallido
    ? '#FEF2F2'
    : delPaciente
      ? '#FFFFFF'
      : m.quien === 'doctor'
        ? '#EDE9FE'
        : '#F4F1F9'
  const borde = fallido ? '#FECACA' : delPaciente ? '#DCD8E6' : '#D6D0E4'

  const quienDice =
    m.quien === 'paciente' ? 'Paciente' : m.quien === 'daniela' ? 'Daniela' : (m.autor ?? 'Doctor')

  return (
    <div className="flex" style={{ justifyContent: delPaciente ? 'flex-start' : 'flex-end' }}>
      <div className="flex flex-col gap-1" style={{ maxWidth: 'min(76%, 540px)' }}>
        <div
          style={{
            backgroundColor: fondo,
            border: `1px solid ${borde}`,
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
          {quienDice} · {FMT_HORA.format(new Date(m.cuando))}
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
// La pantalla
// ==========================================================================================

export default function Conversaciones({ alCaducarSesion }: Props) {
  const [lista, setLista] = useState<ResumenConversacion[]>([])
  const [hilo, setHilo] = useState<HiloDeConversacion | null>(null)
  const [seleccion, setSeleccion] = useState<string | null>(null)
  const [busqueda, setBusqueda] = useState('')
  const [filtro, setFiltro] = useState<Filtro>('todas')
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')

  // Por `ref` y NO en las deps: `App.tsx` pasa una flecha nueva en cada render, y meterla en
  // deps dejaría la pantalla releyendo Neon en bucle. La trampa está en `web/CLAUDE.md`.
  const caducar = useRef(alCaducarSesion)
  caducar.current = alCaducarSesion

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
        setError('')
      } catch (e) {
        if (e instanceof SesionCaducada) caducar.current()
        else if (vivo) setError('No se pudo cargar la lista de conversaciones.')
      } finally {
        if (vivo) setCargando(false)
      }
    }

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

    const tic = async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const datos = await leerHilo(seleccion)
        if (vivo) setHilo(datos)
      } catch (e) {
        if (e instanceof SesionCaducada) caducar.current()
      }
    }

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

  return (
    <main className="min-w-0 flex-1 overflow-hidden" style={{ backgroundColor: '#F7F6FA' }}>
      <div className="flex h-full flex-wrap items-stretch gap-4 p-4 md:gap-5 md:p-6">
        {/* ---------------------------------------------------------------- La lista */}
        <section
          aria-label="Listado de conversaciones"
          className="flex min-h-0 min-w-0 flex-col overflow-hidden"
          style={{
            flex: '1 1 320px',
            maxWidth: '100%',
            maxHeight: '100%',
            backgroundColor: '#FFFFFF',
            border: '1px solid #DCD8E6',
          }}
        >
          <div
            className="flex flex-col gap-3"
            style={{ padding: '18px 18px 14px', borderBottom: '1px solid #ECE8F4' }}
          >
            <div className="flex items-baseline justify-between gap-3">
              <h1
                style={{
                  margin: 0,
                  fontFamily: SG,
                  fontWeight: 600,
                  fontSize: 'clamp(17px, 1.6vw, 21px)',
                  letterSpacing: '-0.024em',
                  color: '#16111F',
                }}
              >
                Conversaciones
              </h1>
              <Rotulo>
                {visibles.length === lista.length
                  ? `${lista.length} ${lista.length === 1 ? 'hilo' : 'hilos'}`
                  : `${visibles.length} de ${lista.length}`}
              </Rotulo>
            </div>

            <label
              className="flex items-center gap-2"
              style={{ border: '1px solid #DCD8E6', padding: '0 12px', minHeight: '44px' }}
            >
              <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#6E6880" strokeWidth="1.8" strokeLinecap="square">
                <circle cx="11" cy="11" r="6.5" />
                <path d="M16 16l4.5 4.5" />
              </svg>
              <input
                type="search"
                value={busqueda}
                onChange={(e) => setBusqueda(e.target.value)}
                placeholder="Buscar por nombre o teléfono"
                aria-label="Buscar conversaciones"
                className="min-w-0 flex-1 border-none bg-transparent outline-none"
                style={{ fontSize: '14px', color: '#16111F', padding: '10px 0' }}
              />
            </label>

            <div className="flex flex-wrap gap-2">
              {CHIPS.map((chip) => {
                const puesto = filtro === chip.id
                return (
                  <button
                    key={chip.id}
                    type="button"
                    onClick={() => setFiltro(chip.id)}
                    style={{
                      backgroundColor: puesto ? '#6D28D9' : '#FFFFFF',
                      color: puesto ? '#FFFFFF' : '#4A4458',
                      border: `1px solid ${puesto ? '#6D28D9' : '#DCD8E6'}`,
                      fontFamily: MONO,
                      fontSize: '10.5px',
                      letterSpacing: '0.12em',
                      textTransform: 'uppercase',
                      padding: '9px 13px',
                      minHeight: '36px',
                      cursor: 'pointer',
                    }}
                  >
                    {chip.etiqueta}
                  </button>
                )
              })}
            </div>
          </div>

          {cargando ? (
            <Vacio>Cargando…</Vacio>
          ) : error ? (
            // Si la consulta falló, la pantalla lo DICE. Nunca una lista vieja sin avisar:
            // una pantalla «en vivo» congelada es peor que una que reconoce que no sabe.
            <Vacio>{error}</Vacio>
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
        </section>

        {/* ----------------------------------------------------------------- El hilo */}
        <section
          aria-label="Detalle de la conversación"
          className="flex min-h-0 min-w-0 flex-col overflow-hidden"
          style={{
            flex: '2 1 380px',
            maxWidth: '100%',
            maxHeight: '100%',
            backgroundColor: '#FFFFFF',
            border: '1px solid #DCD8E6',
          }}
        >
          {!seleccion || !elegida ? (
            <Vacio>Selecciona una conversación para ver el historial.</Vacio>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">
              <div
                className="flex flex-wrap items-center gap-3"
                style={{ padding: '16px 20px', borderBottom: '1px solid #ECE8F4' }}
              >
                <span
                  aria-hidden
                  className="flex items-center justify-center"
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
                  <span style={{ fontFamily: MONO, fontSize: '11.5px', color: '#4C1D95' }}>
                    {telefonoLegible(elegida.telefono)}
                  </span>
                </span>
                <Insignia estado={elegida.estado} />
              </div>

              <div
                ref={caja}
                onScroll={alDesplazar}
                className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
                style={{ backgroundColor: '#F7F6FA', padding: '20px' }}
              >
                {hilo === null ? (
                  <Vacio>Cargando…</Vacio>
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
            </div>
          )}
        </section>
      </div>
    </main>
  )
}

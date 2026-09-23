import { useCallback, useEffect, useRef, useState } from 'react'
import { listarSinResolver, SesionCaducada, type CasoSinResolver } from '@/api'
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
} from '@/componentes/Panel'

/* «Sin resolver»: lo que Daniela no pudo resolver en los últimos 30 días, agrupado por huella.
 *
 * Dos paneles, como Conversaciones: la lista de casos a la izquierda y el caso entero a la
 * derecha. Antes era una columna de tarjetas desplegadas, todas abiertas a la vez, cada una
 * con su «qué pasó / por qué / recomiendo» y dos `<details>`: con seis casos había que bajar
 * media pantalla para saber cuántos había, y comparar dos era imposible. MaxiCare pidió el
 * 22/09/2026 que siguiera el patrón de Conversaciones, y el layout lo pone
 * `componentes/Panel.tsx`, compartido con ella.
 *
 * Sigue siendo de SOLO LECTURA. No hay ni un botón que escriba, y por eso tampoco se refresca
 * sola: un informe de 30 días no cambia mientras lo lees, y un refresco cada diez segundos
 * movería el caso que estás leyendo bajo el cursor. */

type Props = {
  alCaducarSesion: () => void
  /** Abre esa conversacion en la pantalla de Conversaciones. Lo resuelve `App`, que es quien
   *  sabe cambiar de seccion; aqui solo se sabe QUE conversacion interesa. */
  alAbrirConversacion: (telefono: string) => void
}

/** Los cuatro tipos, con el nombre que ve la clínica y su color.
 *
 * `ROTO` va en rojo y `FALTA_DATO` en ámbar a propósito: una falla técnica la arregla quien
 * programa y un dato que falta lo arregla la clínica desde la pantalla de Tratamientos. Son
 * dos trabajos de dos personas distintas, y el color es lo primero que se lee. */
const TIPOS: Record<CasoSinResolver['tipo'], { etiqueta: string; fondo: string; tinta: string }> = {
  FALTA_DATO: { etiqueta: 'Falta un dato', fondo: '#FEF3C7', tinta: '#92400E' },
  GUARDRAIL: { etiqueta: 'Se frenó sola', fondo: '#EDE9FE', tinta: '#4C1D95' },
  ROTO: { etiqueta: 'Falla técnica', fondo: '#FEF2F2', tinta: '#B91C1C' },
  HUMANO: { etiqueta: 'Al doctor', fondo: '#F4F1F9', tinta: '#4A4458' },
}

type Filtro = 'todos' | CasoSinResolver['tipo']

const CHIPS: { id: Filtro; etiqueta: string }[] = [
  { id: 'todos', etiqueta: 'Todos' },
  { id: 'FALTA_DATO', etiqueta: 'Falta un dato' },
  { id: 'GUARDRAIL', etiqueta: 'Se frenó' },
  { id: 'ROTO', etiqueta: 'Fallas' },
  { id: 'HUMANO', etiqueta: 'Al doctor' },
]

/** `falta_dato:ortodoncia:precio` -> «Falta el dato «precio» de ORTODONCIA».
 *
 * La huella cruda no se le ensena a la clinica: es un identificador, no una frase. El
 * detalle tecnico vive detras del `<details>` de admin.
 *
 * Se exporta porque la portada pinta los tres casos mas frecuentes y necesita los mismos
 * titulos. Duplicarla dejaria dos traducciones de la misma huella que se separan en
 * silencio: la clinica leeria dos nombres distintos para el mismo problema. */
export function titulo(caso: CasoSinResolver): string {
  const [, uno = '', dos = ''] = caso.huella.split(':')
  switch (caso.tipo) {
    case 'FALTA_DATO':
      return `Falta ${dos === '_general' ? 'un dato' : `el dato «${dos}»`} de ${uno.toUpperCase()}`
    case 'GUARDRAIL':
      return `Daniela iba a decir algo que no debía${dos !== '_general' ? ` (${dos})` : ''}`
    case 'ROTO':
      return 'El sistema tuvo una falla técnica'
    case 'HUMANO':
      return 'Hubo que pasarle la conversación al doctor'
    default:
      return caso.huella
  }
}

function fecha(iso: string): string {
  return new Date(iso).toLocaleDateString('es-CO', { day: 'numeric', month: 'short' })
}

/** «22 de septiembre de 2026, 8:15 p. m.» -- la marca desde la que cuentan los contadores.
 *
 *  Va con hora y no solo con dia porque la medicion arranco a media tarde: un caso de esa
 *  misma manana no esta contado, y decir solo «desde el 22 de septiembre» haria creer lo
 *  contrario. Si la fecha no llega o no se puede leer, se devuelve `null` y la pantalla
 *  calla: una fecha de inicio inventada es peor que ninguna. */
function desdeCuando(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString('es-CO', {
    timeZone: 'America/Bogota',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

/** «3 veces · 4 sep al 19 sep», o «1 vez · 4 sep» cuando las dos fechas caen el mismo día:
 *  «4 sep al 4 sep» se lee como un error de la pantalla. */
function cuantasYCuando(caso: CasoSinResolver): string {
  const veces = `${caso.contador} ${caso.contador === 1 ? 'vez' : 'veces'}`
  const desde = fecha(caso.primera_vez)
  const hasta = fecha(caso.ultima_vez)
  return `${veces} · ${desde === hasta ? desde : `${desde} al ${hasta}`}`
}

function coincide(caso: CasoSinResolver, busqueda: string): boolean {
  const q = busqueda.trim().toLowerCase()
  if (!q) return true
  // Se busca sobre lo que el usuario VE --el título-- y también sobre los ejemplos, que es
  // donde está la pregunta tal como la escribió el paciente. La huella queda fuera a
  // propósito: es un identificador, y buscar por él daría resultados que la clínica no
  // puede explicarse.
  return (
    titulo(caso).toLowerCase().includes(q) ||
    caso.ejemplos.some((e) => e.toLowerCase().includes(q))
  )
}

// ==========================================================================================
// Piezas
// ==========================================================================================

function FilaDelCaso({
  caso,
  activa,
  alAbrir,
}: {
  caso: CasoSinResolver
  activa: boolean
  alAbrir: () => void
}) {
  const t = TIPOS[caso.tipo]
  return (
    <li style={{ borderBottom: '1px solid #ECE8F4' }}>
      <button
        type="button"
        onClick={alAbrir}
        className="flex w-full flex-col gap-2 text-left"
        style={{
          borderLeft: `3px solid ${activa ? '#6D28D9' : 'transparent'}`,
          // Sin fondo en línea cuando NO está activa: un `transparent` inline le gana a la
          // clase de hover y el hover dejaría de verse sin que nada falle. Igual que en
          // `Conversaciones.FilaDeLaLista`.
          backgroundColor: activa ? '#F4F1F9' : undefined,
          padding: '15px 16px',
          minHeight: '44px',
          cursor: 'pointer',
        }}
      >
        <span className="flex w-full items-baseline justify-between gap-2">
          <span
            style={{
              fontSize: '14.5px',
              fontWeight: 500,
              letterSpacing: '-0.01em',
              lineHeight: 1.35,
              color: '#16111F',
            }}
          >
            {titulo(caso)}
          </span>
          {/* El contador es el dato que ordena la lista, así que va donde la vista se
              detiene: a la derecha del título, en versalitas y sin partirse. */}
          <span
            style={{
              fontFamily: MONO,
              fontSize: '13px',
              fontWeight: 600,
              color: caso.contador > 1 ? '#6D28D9' : '#6E6880',
              flex: 'none',
            }}
          >
            {caso.contador}
          </span>
        </span>

        {caso.ejemplos.length > 0 ? (
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
            «{caso.ejemplos[0]}»
          </span>
        ) : null}

        <span className="flex w-full items-center gap-2" style={{ paddingTop: '2px' }}>
          <Pastilla texto={t.etiqueta} fondo={t.fondo} tinta={t.tinta} />
          <span style={{ fontFamily: MONO, fontSize: '10.5px', color: '#6E6880' }}>
            {fecha(caso.ultima_vez)}
          </span>
          {caso.escalo > 0 ? (
            <span
              style={{
                marginLeft: 'auto',
                fontFamily: MONO,
                fontSize: '10.5px',
                backgroundColor: '#FEF2F2',
                color: '#B91C1C',
                padding: '3px 6px',
              }}
              title="veces que se interrumpió al doctor"
            >
              {caso.escalo} AL DOCTOR
            </span>
          ) : null}
        </span>
      </button>
    </li>
  )
}

/** Uno de los tres apartados del análisis. Van en tarjetas blancas sobre el gris del panel,
 *  como los mensajes del hilo de Conversaciones: se leen de arriba abajo sin buscar dónde
 *  empieza cada uno. */
function Apartado({ rotulo, children }: { rotulo: string; children: React.ReactNode }) {
  return (
    <div style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6', padding: '14px 16px' }}>
      <p
        style={{
          margin: '0 0 6px',
          fontFamily: MONO,
          fontSize: '10px',
          letterSpacing: '0.14em',
          textTransform: 'uppercase',
          color: '#6E6880',
        }}
      >
        {rotulo}
      </p>
      <p style={{ margin: 0, fontSize: '14px', lineHeight: 1.55, color: '#2C2439' }}>{children}</p>
    </div>
  )
}

function Detalle({
  caso,
  esAdmin,
  alAbrirConversacion,
}: {
  caso: CasoSinResolver
  esAdmin: boolean
  alAbrirConversacion: (telefono: string) => void
}) {
  const t = TIPOS[caso.tipo]
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* La cabecera del detalle, con la misma altura y el mismo borde que la del hilo. */}
      <div
        className="flex flex-wrap items-center gap-3"
        style={{ padding: '16px 20px', borderBottom: '1px solid #ECE8F4' }}
      >
        <span
          aria-hidden
          className="flex items-center justify-center"
          style={{ width: '40px', height: '40px', flex: 'none', backgroundColor: t.fondo, color: t.tinta }}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="square">
            <path d="M12 3.5L21.5 20H2.5L12 3.5z" />
            <path d="M12 10v4.5" />
            <path d="M12 17.2v.1" />
          </svg>
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span
            style={{ fontFamily: SG, fontWeight: 600, fontSize: '17px', letterSpacing: '-0.022em', lineHeight: 1.3, color: '#16111F' }}
          >
            {titulo(caso)}
          </span>
          <span style={{ fontFamily: MONO, fontSize: '11.5px', color: '#4C1D95' }}>
            {cuantasYCuando(caso)}
          </span>
        </span>
        <Pastilla texto={t.etiqueta} fondo={t.fondo} tinta={t.tinta} />
      </div>

      <div
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
        style={{ backgroundColor: '#F7F6FA', padding: '20px' }}
      >
        {caso.informe ? (
          <>
            <Apartado rotulo="Qué pasó">{caso.informe.que_paso}</Apartado>
            <Apartado rotulo="Por qué">{caso.informe.por_que}</Apartado>
            <Apartado rotulo="Recomiendo">{caso.informe.recomiendo}</Apartado>
          </>
        ) : (
          // Un analista caído no puede dejar el panel vacío: el caso se muestra igual, con su
          // contador y sus ejemplos, que ya dicen bastante.
          <Apartado rotulo="Análisis">El análisis todavía no está listo.</Apartado>
        )}

        {caso.escalo > 0 ? (
          <div
            role="note"
            style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA', padding: '12px 16px' }}
          >
            <p style={{ margin: 0, fontSize: '13.5px', lineHeight: 1.5, color: '#78350F' }}>
              Se interrumpió al doctor <strong>{caso.escalo}</strong> de {caso.contador}{' '}
              {caso.contador === 1 ? 'vez' : 'veces'}.
            </p>
          </div>
        ) : null}

        {caso.ejemplos.length > 0 ? (
          <div style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6', padding: '14px 16px' }}>
            <p
              style={{
                margin: '0 0 10px',
                fontFamily: MONO,
                fontSize: '10px',
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                color: '#6E6880',
              }}
            >
              Las preguntas tal como llegaron ({caso.ejemplos.length})
            </p>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {caso.ejemplos.map((texto, i) => (
                <li
                  key={i}
                  style={{
                    borderLeft: '2px solid #DCD8E6',
                    paddingLeft: '12px',
                    fontSize: '13.5px',
                    fontWeight: 300,
                    lineHeight: 1.5,
                    color: '#4A4458',
                  }}
                >
                  «{texto}»
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {caso.telefonos.length > 0 ? (
          <div style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6', padding: '14px 16px' }}>
            <p
              style={{
                margin: '0 0 10px',
                fontFamily: MONO,
                fontSize: '10px',
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                color: '#6E6880',
              }}
            >
              De {caso.telefonos.length === 1 ? 'esta conversacion' : 'estas conversaciones'}
            </p>
            {/* El enlace es la diferencia entre un informe y algo accionable: el caso dice
                que falto un dato, y esto lleva a ver como se pidio. No dice quien escribio
                cada frase --un caso es un agregado-- solo de donde salio. */}
            <div className="flex flex-wrap gap-2">
              {caso.telefonos.map((tel) => (
                <button
                  key={tel}
                  type="button"
                  onClick={() => alAbrirConversacion(tel)}
                  style={{
                    fontFamily: MONO,
                    fontSize: '11.5px',
                    color: '#4C1D95',
                    backgroundColor: '#F4F1F9',
                    border: '1px solid #DCD8E6',
                    padding: '7px 10px',
                    minHeight: '36px',
                    cursor: 'pointer',
                  }}
                  title="Abrir esta conversacion"
                >
                  {telefonoLegible(tel)} &rarr;
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {esAdmin ? (
          <details style={{ marginTop: '2px' }}>
            <summary
              style={{
                cursor: 'pointer',
                fontFamily: MONO,
                fontSize: '10.5px',
                letterSpacing: '0.12em',
                textTransform: 'uppercase',
                color: '#6E6880',
              }}
            >
              Detalle técnico
            </summary>
            <code
              className="mt-2 block break-all"
              style={{ fontFamily: MONO, fontSize: '11.5px', color: '#6E6880' }}
            >
              {caso.huella}
            </code>
          </details>
        ) : null}
      </div>
    </div>
  )
}

// ==========================================================================================

export default function SinResolver({ alCaducarSesion, alAbrirConversacion }: Props) {
  const [casos, setCasos] = useState<CasoSinResolver[]>([])
  const [esAdmin, setEsAdmin] = useState(false)
  const [midiendoDesde, setMidiendoDesde] = useState<string | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')
  const [seleccion, setSeleccion] = useState<string | null>(null)
  const [busqueda, setBusqueda] = useState('')
  const [filtro, setFiltro] = useState<Filtro>('todos')

  // Por `ref` y NO en las deps del useCallback: meter la prop en deps deja la pantalla
  // releyendo Neon en bucle. Es la trampa documentada en `web/CLAUDE.md`.
  const caducar = useRef(alCaducarSesion)
  caducar.current = alCaducarSesion

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      const datos = await listarSinResolver()
      setCasos(datos.casos)
      setEsAdmin(datos.es_admin)
      setMidiendoDesde(datos.midiendo_desde)
      setError('')
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else setError('No se pudo cargar el informe.')
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  const visibles = casos.filter(
    (c) => (filtro === 'todos' || c.tipo === filtro) && coincide(c, busqueda),
  )
  const elegido = seleccion ? casos.find((c) => c.huella === seleccion) : undefined

  // La pantalla entera, no media. `cargando` solo es cierto en la primera carga.
  if (cargando) return <CargandoPantalla que="Cargando el informe…" />

  return (
    <MarcoDeDosPaneles etiqueta="Sin resolver">
      {/* ------------------------------------------------------------------- La lista */}
      <Panel etiqueta="Listado de casos sin resolver" peso="1 1 320px">
        <CabeceraDePanel>
          <div className="flex items-baseline justify-between gap-3">
            <TituloDePanel>Sin resolver</TituloDePanel>
            <Rotulo>
              {visibles.length === casos.length
                ? `${casos.length} ${casos.length === 1 ? 'caso' : 'casos'}`
                : `${visibles.length} de ${casos.length}`}
            </Rotulo>
          </div>

          <p style={{ margin: 0, fontSize: '12.5px', lineHeight: 1.45, color: '#6E6880' }}>
            Últimos 30 días, lo que más está pasando primero. Esta pantalla solo informa.
          </p>

          {desdeCuando(midiendoDesde) ? (
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
              MIDIENDO DESDE EL {desdeCuando(midiendoDesde)}
            </p>
          ) : null}

          <Buscador
            valor={busqueda}
            alCambiar={setBusqueda}
            marcador="Buscar por caso o por lo que preguntaron"
            etiqueta="Buscar casos sin resolver"
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
          // Si la consulta falló, la pantalla lo DICE, y con salida: antes solo quedaba
          // recargar la página entera. Mismo criterio que Conversaciones.
          <div className="px-4 py-6">
            <Fallo mensaje={error} alReintentar={() => void recargar()} />
          </div>
        ) : casos.length === 0 ? (
          <Vacio>Nada sin resolver en los últimos 30 días. Buena señal.</Vacio>
        ) : visibles.length === 0 ? (
          <Vacio>
            {busqueda.trim()
              ? 'Ningún caso coincide con esa búsqueda.'
              : 'Ningún caso de ese tipo en los últimos 30 días.'}
          </Vacio>
        ) : (
          <ul className="m-0 flex min-h-0 flex-1 list-none flex-col overflow-y-auto p-0">
            {visibles.map((caso) => (
              <FilaDelCaso
                key={caso.huella}
                caso={caso}
                activa={caso.huella === seleccion}
                alAbrir={() => setSeleccion(caso.huella)}
              />
            ))}
          </ul>
        )}
      </Panel>

      {/* ------------------------------------------------------------------ El detalle */}
      <Panel etiqueta="Detalle del caso" peso="2 1 380px">
        {!elegido ? (
          <Vacio>Selecciona un caso para ver qué pasó y qué se recomienda.</Vacio>
        ) : (
          <Detalle caso={elegido} esAdmin={esAdmin} alAbrirConversacion={alAbrirConversacion} />
        )}
      </Panel>
    </MarcoDeDosPaneles>
  )
}

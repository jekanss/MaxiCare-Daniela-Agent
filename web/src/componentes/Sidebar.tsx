import type { ReactNode } from 'react'
import type { Sesion } from '@/api'
import { PalabraSinPiloto, SimboloSinPiloto } from '@/componentes/Marca'

/* El menú, en oscuro, siguiendo `docs/diseno/panel-2026-09-22.dc.html` (líneas 40-135).
 *
 * Los colores van en hexadecimales literales y no en tokens `--color-sp-*`: esos vienen del
 * archivo de Figma de la marca y renombrarlos o ampliarlos deja de corresponder con el
 * diseño. Es el mismo dialecto que usan las demás pantallas del panel. */

const SG = "'Space Grotesk', sans-serif"
const SORA = "'Sora', sans-serif"
const MONO = "'JetBrains Mono', monospace"

export type SeccionId =
  | 'inicio'
  | 'bandeja'
  | 'agenda'
  | 'leads'
  | 'tratamientos'
  | 'metricas'
  | 'estado'
  | 'configuracion'
  | 'pruebas'

export type Seccion = {
  id: SeccionId
  etiqueta: string
  icono: ReactNode
  /** La fase del plan que la construye. `null` = ya está construida. */
  fase: number | null
}

/* Los iconos son trazos, no emojis: un emoji lo dibuja el sistema operativo y cambia de
 * forma y de color en cada máquina. Estos vienen del archivo de diseño con su `stroke-width`
 * original y heredan el color del botón por `currentColor`, que es lo que hace que el activo
 * se encienda con el texto sin escribir el color dos veces. */
function Icono({ children }: { children: ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      width="17"
      height="17"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="square"
      className="shrink-0"
    >
      {children}
    </svg>
  )
}

/* Las nueve secciones de la especificación del producto, en el orden en que las puso el
 * diseño. Las que todavía no existen NO se esconden del menú: se muestran con su estado
 * vacío diciendo qué fase las construye.
 *
 * Esconderlas daría una navegación «limpia» que miente sobre el tamaño del producto; un
 * enlace que no lleva a ninguna parte sería peor. Un destino que explica qué falta es lo
 * único honesto de los tres -- y la especificación lo pide literalmente: «cada pantalla
 * tiene un estado vacío que explica qué falta». */
export const SECCIONES: Seccion[] = [
  {
    id: 'inicio',
    etiqueta: 'Inicio',
    icono: (
      <Icono>
        <path d="M4 11 12 4l8 7" />
        <path d="M6.5 10v10h11V10" />
      </Icono>
    ),
    fase: null,
  },
  {
    id: 'bandeja',
    etiqueta: 'Sin resolver',
    icono: (
      <Icono>
        <path d="M4 5h16v11H9l-5 4z" />
      </Icono>
    ),
    fase: null,
  },
  {
    id: 'agenda',
    etiqueta: 'Agenda',
    icono: (
      <Icono>
        <rect x="4" y="5" width="16" height="15" />
        <path d="M4 10h16M9 3v4M15 3v4" />
      </Icono>
    ),
    fase: null,
  },
  {
    id: 'leads',
    etiqueta: 'Leads',
    icono: (
      <Icono>
        <circle cx="9" cy="8" r="3.4" />
        <path d="M3.6 20c0-3.2 2.4-5.4 5.4-5.4s5.4 2.2 5.4 5.4" />
        <path d="M16 7.2a3.2 3.2 0 0 1 0 6M18 20c0-2.6-1-4.4-2.6-5.2" />
      </Icono>
    ),
    fase: 8,
  },
  {
    id: 'tratamientos',
    etiqueta: 'Tratamientos',
    icono: (
      <Icono>
        <path d="M12 20V9" />
        <path d="M7 20c-2 0-3-2.6-3-6.5S6 4 9 5.4L12 7l3-1.6C18 4 20 9.6 20 13.5S19 20 17 20" />
      </Icono>
    ),
    fase: null,
  },
  {
    id: 'metricas',
    etiqueta: 'Métricas',
    icono: (
      <Icono>
        <path d="M4 20h16" />
        <rect x="5" y="12" width="3.6" height="5" />
        <rect x="10.2" y="8" width="3.6" height="9" />
        <rect x="15.4" y="4.6" width="3.6" height="12.4" />
      </Icono>
    ),
    fase: 9,
  },
  {
    id: 'estado',
    etiqueta: 'Estado del sistema',
    icono: (
      <Icono>
        <path d="M3 12h4l2.5-6 4 13L16 12h5" />
      </Icono>
    ),
    fase: 7,
  },
]

export const CONFIGURACION: Seccion = {
  id: 'configuracion',
  etiqueta: 'Configuración',
  icono: (
    <Icono>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
    </Icono>
  ),
  fase: 8,
}

export const PRUEBAS: Seccion = {
  id: 'pruebas',
  etiqueta: 'Pruebas',
  icono: (
    <Icono>
      <path d="M10 3v6.2L4.8 18c-.8 1.4.2 3 1.7 3h11c1.5 0 2.5-1.6 1.7-3L14 9.2V3" />
      <path d="M8.6 3h6.8" />
    </Icono>
  ),
  fase: null,
}

function iniciales(nombre: string): string {
  const partes = nombre.replace(/^(Dra?\.|Sra?\.)\s*/i, '').trim().split(/\s+/)
  return ((partes[0]?.[0] ?? '') + (partes[1]?.[0] ?? '')).toUpperCase() || '··'
}

const ROLES: Record<Sesion['rol'], string> = {
  admin: 'Administrador',
  doctor: 'Doctor',
  recepcion: 'Recepción',
}

function Rotulo({ children }: { children: ReactNode }) {
  return (
    <span
      className="px-2.5 pt-1.5 pb-2.5"
      style={{
        fontFamily: MONO,
        fontSize: 9.5,
        letterSpacing: '0.2em',
        textTransform: 'uppercase',
        color: '#6E6880',
      }}
    >
      {children}
    </span>
  )
}

function Boton({
  seccion,
  activa,
  ir,
  insignia,
}: {
  seccion: Seccion
  activa: boolean
  ir: (id: SeccionId) => void
  insignia?: ReactNode
}) {
  return (
    <button
      onClick={() => ir(seccion.id)}
      aria-current={activa ? 'page' : undefined}
      className="relative w-full flex items-center gap-3 text-left px-3 py-2.5 transition-colors duration-150 hover:bg-white/10 hover:text-white"
      style={{
        minHeight: 44,
        fontFamily: SORA,
        fontSize: 14.5,
        fontWeight: 400,
        letterSpacing: '-0.008em',
        // Sin fondo inline cuando NO está activa: un `backgroundColor: 'transparent'` gana a
        // la clase de hover --el estilo en línea siempre le gana a la hoja-- y el hover
        // dejaría de verse sin que nada falle.
        backgroundColor: activa ? 'rgba(255,255,255,0.07)' : undefined,
        color: activa ? '#FFFFFF' : '#B8B2C8',
      }}
    >
      <span
        aria-hidden="true"
        className="absolute left-0 top-1/2 -translate-y-1/2 block"
        style={{
          width: 3,
          height: activa ? '58%' : 0,
          backgroundColor: '#A78BFA',
          transition: 'height 220ms cubic-bezier(.22,1,.36,1)',
        }}
      />
      {seccion.icono}
      <span className="flex-1 truncate">{seccion.etiqueta}</span>
      {insignia}
      {seccion.fase !== null && (
        <span
          style={{ fontFamily: MONO, fontSize: 10, color: '#6E6880' }}
          title={`La construye la fase ${seccion.fase}`}
        >
          F{seccion.fase}
        </span>
      )}
    </button>
  )
}

export default function Sidebar({
  activa,
  ir,
  sesion,
  alSalir,
}: {
  activa: SeccionId
  ir: (id: SeccionId) => void
  sesion: Sesion
  alSalir: () => void
}) {
  return (
    <aside
      className="flex flex-col h-screen sticky top-0 shrink-0 overflow-hidden"
      style={{
        width: 'clamp(230px, 18vw, 272px)',
        backgroundColor: '#0B0912',
        color: '#EDEAF4',
        fontFamily: SG,
      }}
    >
      <div
        className="flex items-center gap-3 px-5 py-6"
        style={{ borderBottom: '1px solid rgba(255,255,255,0.1)' }}
      >
        <span
          aria-hidden="true"
          className="relative flex items-center justify-center shrink-0"
          style={{ width: 38, height: 38, backgroundColor: '#7C3AED' }}
        >
          <span
            className="block rounded-full"
            style={{ width: 14, height: 14, border: '2.5px solid #FFFFFF' }}
          />
          <span
            className="absolute block"
            style={{ right: 6, bottom: 6, width: 8, height: 8, backgroundColor: '#0B0912' }}
          />
        </span>
        <span className="flex flex-col gap-0.5 min-w-0">
          <span style={{ fontWeight: 600, fontSize: 17, letterSpacing: '-0.025em', color: '#FFFFFF' }}>
            MaxiCare
          </span>
          <span
            style={{
              fontFamily: MONO,
              fontSize: 9.5,
              letterSpacing: '0.18em',
              textTransform: 'uppercase',
              color: '#8B84A0',
            }}
          >
            Clínica dental
          </span>
        </span>
      </div>

      <nav aria-label="Secciones" className="flex-1 flex flex-col gap-0.5 px-3.5 py-4 overflow-y-auto">
        <Rotulo>Operación</Rotulo>

        {SECCIONES.map((s) => (
          <Boton key={s.id} seccion={s} activa={s.id === activa} ir={ir} />
        ))}

        <div className="mt-4">
          <Rotulo>Ajustes</Rotulo>
        </div>

        <Boton seccion={CONFIGURACION} activa={activa === 'configuracion'} ir={ir} />

        {/* El distintivo permanente del carril de pruebas. La especificación lo pide con
            esas palabras -- «nunca un interruptor escondido en un menú» -- y aquí se cumple
            en los dos sitios donde se puede confundir: el menú y la propia pantalla. */}
        <Boton
          seccion={PRUEBAS}
          activa={activa === 'pruebas'}
          ir={ir}
          insignia={
            <span
              style={{
                fontFamily: MONO,
                fontSize: 9.5,
                letterSpacing: '0.08em',
                backgroundColor: 'rgba(253,224,71,0.16)',
                color: '#FDE047',
                padding: '3px 7px',
              }}
            >
              TEST
            </span>
          }
        />
      </nav>

      <div
        className="flex flex-col gap-3.5 px-4 pt-4 pb-5"
        style={{ borderTop: '1px solid rgba(255,255,255,0.1)' }}
      >
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="flex items-center justify-center shrink-0"
            style={{
              width: 34,
              height: 34,
              backgroundColor: '#EDE9FE',
              color: '#4C1D95',
              fontWeight: 600,
              fontSize: 14,
            }}
          >
            {iniciales(sesion.nombre)}
          </span>
          <span className="flex flex-col gap-0.5 min-w-0 flex-1">
            <span className="truncate" style={{ fontSize: 13.5, fontWeight: 500, color: '#FFFFFF' }}>
              {sesion.nombre}
            </span>
            <span
              className="truncate"
              style={{
                fontFamily: MONO,
                fontSize: 9.5,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                color: '#8B84A0',
              }}
            >
              {ROLES[sesion.rol]}
            </span>
          </span>
          <button
            onClick={alSalir}
            title="Cerrar sesión"
            aria-label="Cerrar sesión"
            className="shrink-0 flex items-center justify-center transition-colors hover:bg-white/10"
            style={{
              width: 34,
              height: 34,
              border: '1px solid rgba(255,255,255,0.16)',
              color: '#C4B5FD',
            }}
          >
            <svg
              aria-hidden="true"
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="square"
            >
              <path d="M14 4h6v16h-6" />
              <path d="M10 8l-4 4 4 4M6 12h9" />
            </svg>
          </button>
        </div>

        {/* El logo sale de `Marca.tsx`, cuyos trazados vienen de Figma con la geometría
            original. No se redibuja aquí ni se sustituye por un SVG parecido. */}
        <a
          href="https://sinpiloto.co"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-2 transition-colors hover:text-[#C4B5FD]"
          style={{
            fontFamily: MONO,
            fontSize: 9.5,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: '#6E6880',
          }}
        >
          Operado por
          <SimboloSinPiloto size={0.62} />
          <PalabraSinPiloto size={0.62} />
        </a>
      </div>
    </aside>
  )
}

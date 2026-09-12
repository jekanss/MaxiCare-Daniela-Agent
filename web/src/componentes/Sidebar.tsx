import type { Sesion } from '@/api'

const SP = "'Space Grotesk', sans-serif"

export type SeccionId =
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
  icono: string
  /** La fase del plan que la construye. `null` = ya está construida. */
  fase: number | null
}

/* Las ocho secciones de la especificación del producto, en el orden en que las puso el
 * diseño. Las seis que todavía no existen NO se esconden del menú: se muestran con su
 * estado vacío diciendo qué fase las construye.
 *
 * Esconderlas daría una navegación «limpia» que miente sobre el tamaño del producto; un
 * enlace que no lleva a ninguna parte sería peor. Un destino que explica qué falta es lo
 * único honesto de los tres -- y la especificación lo pide literalmente: «cada pantalla
 * tiene un estado vacío que explica qué falta». */
export const SECCIONES: Seccion[] = [
  { id: 'bandeja', etiqueta: 'Bandeja', icono: '💬', fase: 6 },
  { id: 'agenda', etiqueta: 'Agenda', icono: '📅', fase: null },
  { id: 'leads', etiqueta: 'Leads', icono: '👥', fase: 8 },
  { id: 'tratamientos', etiqueta: 'Tratamientos', icono: '🦷', fase: 8 },
  { id: 'metricas', etiqueta: 'Métricas', icono: '📊', fase: 9 },
  { id: 'estado', etiqueta: 'Estado del sistema', icono: '🔌', fase: 7 },
]

export const CONFIGURACION: Seccion = {
  id: 'configuracion',
  etiqueta: 'Configuración',
  icono: '⚙️',
  fase: 8,
}

export const PRUEBAS: Seccion = { id: 'pruebas', etiqueta: 'Pruebas', icono: '🧪', fase: null }

function iniciales(nombre: string): string {
  const partes = nombre.replace(/^(Dra?\.|Sra?\.)\s*/i, '').trim().split(/\s+/)
  return ((partes[0]?.[0] ?? '') + (partes[1]?.[0] ?? '')).toUpperCase() || '··'
}

const ROLES: Record<Sesion['rol'], string> = {
  admin: 'Administrador',
  doctor: 'Doctor',
  recepcion: 'Recepción',
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
  insignia?: React.ReactNode
}) {
  return (
    <button
      onClick={() => ir(seccion.id)}
      aria-current={activa ? 'page' : undefined}
      className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-left text-sm transition-colors duration-100 hover:bg-gray-50"
      style={{
        backgroundColor: activa ? '#F5F3FF' : 'transparent',
        color: activa ? '#7C3AED' : '#374151',
        fontWeight: activa ? 600 : 400,
      }}
    >
      <span className="text-base leading-none">{seccion.icono}</span>
      <span className="flex-1">{seccion.etiqueta}</span>
      {insignia}
      {seccion.fase !== null && (
        <span
          className="text-[9px] font-semibold px-1.5 py-0.5 rounded"
          style={{ backgroundColor: '#F3F4F6', color: '#9CA3AF' }}
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
      className="flex flex-col h-screen sticky top-0 shrink-0"
      style={{ width: 220, borderRight: '1px solid #E5E7EB', backgroundColor: '#FFFFFF', fontFamily: SP }}
    >
      <div className="px-5 py-5 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div
            className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs font-bold"
            style={{ backgroundColor: '#7C3AED' }}
          >
            M
          </div>
          <div>
            <p className="text-sm font-semibold" style={{ color: '#111827' }}>MaxiCare</p>
            <p className="text-[10px]" style={{ color: '#9CA3AF' }}>Clínica dental</p>
          </div>
        </div>
      </div>

      <nav className="flex-1 px-3 py-4 flex flex-col gap-0.5 overflow-y-auto">
        {SECCIONES.map((s) => (
          <Boton key={s.id} seccion={s} activa={s.id === activa} ir={ir} />
        ))}

        <div className="my-3 border-t border-gray-100" />

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
              className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wide"
              style={{ backgroundColor: '#FEF3C7', color: '#92400E' }}
            >
              TEST
            </span>
          }
        />
      </nav>

      <div className="px-4 py-4 border-t border-gray-100 flex items-center gap-2.5">
        <div
          className="w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-bold shrink-0"
          style={{ backgroundColor: '#6366F1' }}
        >
          {iniciales(sesion.nombre)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-semibold truncate" style={{ color: '#111827' }}>{sesion.nombre}</p>
          <p className="text-[10px] truncate" style={{ color: '#9CA3AF' }}>{ROLES[sesion.rol]}</p>
        </div>
        <button
          onClick={alSalir}
          title="Cerrar sesión"
          aria-label="Cerrar sesión"
          className="shrink-0 p-1.5 rounded-md hover:bg-gray-100 transition-colors"
          style={{ color: '#9CA3AF' }}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
          </svg>
        </button>
      </div>

      <div className="px-4 pb-4">
        <p className="text-[9px]" style={{ color: '#D1D5DB' }}>Operado por sinpiloto.co</p>
      </div>
    </aside>
  )
}

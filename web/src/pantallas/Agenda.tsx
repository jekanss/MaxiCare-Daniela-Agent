import { useState } from 'react'

const SP = "'Space Grotesk', sans-serif"

/* La Agenda tal como la diseñó Figma Make, con la jerarquía visual intacta: la acción de
 * asistencia es la más grande y la más fácil de encontrar de toda la aplicación, que es
 * exactamente lo que la especificación exige ("es el dato más importante de todo el
 * producto y hoy no existe en ningún sistema, vive en un cuaderno").
 *
 * ------------------------------------------------------------------------------------
 * Lo que esta pantalla NO hace todavía, y por qué se dice en pantalla y no solo aquí
 * ------------------------------------------------------------------------------------
 *
 * No lee de Neon y no escribe nada. Los datos son de ejemplo y marcar una asistencia solo
 * cambia el estado de React: al recargar vuelve como estaba.
 *
 * Escribir la asistencia de verdad es el entregable de la fase 8 --y necesita una columna
 * que la tabla `citas` todavía no tiene--, así que fingirlo aquí sería la peor versión
 * posible: alguien de la clínica marcaría un día entero creyendo que quedó guardado. Por
 * eso el aviso de arriba no se puede cerrar. */

type Asistencia = 'asistio' | 'no_asistio' | null

type Cita = {
  id: string
  tipo: 'cita'
  inicio: number // minutos desde las 8:00
  duracion: number
  paciente: string
  telefono: string
  tratamiento: string
  origen: 'whatsapp' | 'google_ads' | 'instagram' | 'referido'
  asistencia: Asistencia
}

type Bloqueo = {
  id: string
  tipo: 'bloqueo'
  inicio: number
  duracion: number
  titulo: string
  doctor: string
}

type Franja = Cita | Bloqueo

const MINUTO_ACTUAL = 6 * 60 + 35 // 14:35

const FRANJAS: Franja[] = [
  { id: 'a1', tipo: 'cita', inicio: 0, duracion: 45, paciente: 'Carlos Mendoza', telefono: '311 820 4471', tratamiento: 'Valoración ortodoncia', origen: 'instagram', asistencia: 'asistio' },
  { id: 'b1', tipo: 'bloqueo', inicio: 60, duracion: 30, titulo: 'Reunión médica', doctor: 'Dr. Ramírez' },
  { id: 'a2', tipo: 'cita', inicio: 90, duracion: 45, paciente: 'María Castillo', telefono: '300 541 9382', tratamiento: 'Blanqueamiento dental', origen: 'whatsapp', asistencia: null },
  { id: 'a3', tipo: 'cita', inicio: 120, duracion: 45, paciente: 'Santiago Vargas', telefono: '315 762 0093', tratamiento: 'Implante dental', origen: 'google_ads', asistencia: 'no_asistio' },
  { id: 'a4', tipo: 'cita', inicio: 180, duracion: 45, paciente: 'Laura Jiménez', telefono: '312 488 5521', tratamiento: 'Diseño de sonrisa', origen: 'referido', asistencia: 'asistio' },
  { id: 'b2', tipo: 'bloqueo', inicio: 240, duracion: 60, titulo: 'Almuerzo', doctor: 'Dr. Salcedo' },
  { id: 'a5', tipo: 'cita', inicio: 300, duracion: 45, paciente: 'Andrés Restrepo', telefono: '317 293 6610', tratamiento: 'Endodoncia', origen: 'whatsapp', asistencia: null },
  { id: 'a6', tipo: 'cita', inicio: 360, duracion: 45, paciente: 'Valentina Torres', telefono: '301 654 8823', tratamiento: 'Prótesis dental', origen: 'google_ads', asistencia: null },
  { id: 'a7', tipo: 'cita', inicio: 420, duracion: 45, paciente: 'Juan Pablo Ríos', telefono: '314 711 2259', tratamiento: 'Valoración ortodoncia', origen: 'instagram', asistencia: null },
  { id: 'a8', tipo: 'cita', inicio: 480, duracion: 45, paciente: 'Camila Herrera', telefono: '310 382 7741', tratamiento: 'Blanqueamiento dental', origen: 'whatsapp', asistencia: null },
]

const AYER_SIN_MARCAR = [
  { paciente: 'Sofía Mora', tratamiento: 'Diseño de sonrisa', hora: '10:00' },
  { paciente: 'Ricardo Patiño', tratamiento: 'Implante dental', hora: '14:00' },
  { paciente: 'Natalia Guerrero', tratamiento: 'Ortodoncia — ajuste', hora: '16:00' },
]

const ORIGEN_ETIQUETA: Record<string, string> = {
  whatsapp: 'Daniela · WhatsApp',
  google_ads: 'Google Ads',
  instagram: 'Instagram',
  referido: 'Referido',
}

const ORIGEN_COLOR: Record<string, string> = {
  whatsapp: '#059669',
  google_ads: '#7C3AED',
  instagram: '#7C3AED',
  referido: '#D97706',
}

function aHora(m: number): string {
  const h = Math.floor(m / 60) + 8
  return `${String(h).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

function TarjetaCita({
  cita,
  pasada,
  marcar,
}: {
  cita: Cita
  pasada: boolean
  marcar: (id: string, v: Asistencia) => void
}) {
  const sinMarcar = pasada && cita.asistencia === null
  const alto = Math.max(cita.duracion * 1.6, 72)

  return (
    <div
      className="rounded-xl px-4 py-3 flex flex-col gap-2 relative overflow-hidden"
      style={{
        minHeight: alto,
        backgroundColor: sinMarcar ? '#FFFBEB' : '#FFFFFF',
        border: `1.5px solid ${sinMarcar ? '#FCD34D' : '#E5E7EB'}`,
        boxShadow: sinMarcar ? '0 0 0 3px rgba(251,191,36,0.15)' : '0 1px 3px rgba(0,0,0,0.06)',
      }}
    >
      <div
        className="absolute left-0 top-0 bottom-0 w-1 rounded-l-xl"
        style={{
          backgroundColor:
            cita.asistencia === 'asistio' ? '#10B981'
            : cita.asistencia === 'no_asistio' ? '#EF4444'
            : sinMarcar ? '#F59E0B'
            : '#7C3AED',
        }}
      />

      <div className="pl-1 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-0.5">
            <p className="text-sm font-semibold truncate" style={{ color: '#111827' }}>{cita.paciente}</p>
            {cita.asistencia === 'asistio' && (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full shrink-0" style={{ backgroundColor: '#D1FAE5', color: '#065F46' }}>✓ Asistió</span>
            )}
            {cita.asistencia === 'no_asistio' && (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full shrink-0" style={{ backgroundColor: '#FEE2E2', color: '#991B1B' }}>✗ No asistió</span>
            )}
          </div>
          <p className="text-xs truncate" style={{ color: '#6B7280' }}>{cita.tratamiento}</p>
          <div className="flex items-center gap-1.5 mt-1">
            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full" style={{ backgroundColor: '#F3F4F6', color: ORIGEN_COLOR[cita.origen] }}>
              {ORIGEN_ETIQUETA[cita.origen]}
            </span>
            <span className="text-[10px]" style={{ color: '#9CA3AF' }}>{cita.telefono}</span>
          </div>
        </div>
        <p className="text-xs font-medium shrink-0" style={{ color: '#9CA3AF' }}>{aHora(cita.inicio)}</p>
      </div>

      {pasada && cita.asistencia === null && (
        <div className="pl-1 flex gap-2 mt-1">
          <button
            onClick={() => marcar(cita.id, 'asistio')}
            className="flex-1 py-2.5 rounded-lg text-sm font-bold transition-colors flex items-center justify-center gap-1.5 hover:brightness-95"
            style={{ backgroundColor: '#10B981', color: '#FFFFFF', boxShadow: '0 2px 8px rgba(16,185,129,0.35)' }}
          >
            ✓ Asistió
          </button>
          <button
            onClick={() => marcar(cita.id, 'no_asistio')}
            className="flex-1 py-2.5 rounded-lg text-sm font-bold transition-colors flex items-center justify-center gap-1.5 hover:brightness-95"
            style={{ backgroundColor: '#FEF2F2', color: '#DC2626', border: '1.5px solid #FECACA' }}
          >
            ✗ No asistió
          </button>
        </div>
      )}

      {pasada && cita.asistencia !== null && (
        <div className="pl-1">
          <button
            onClick={() => marcar(cita.id, null)}
            className="text-[10px] hover:underline"
            style={{ color: '#9CA3AF' }}
          >
            Corregir marcación
          </button>
        </div>
      )}
    </div>
  )
}

function TarjetaBloqueo({ bloqueo }: { bloqueo: Bloqueo }) {
  return (
    <div
      className="rounded-xl px-4 py-3 flex items-center gap-3"
      style={{
        minHeight: Math.max(bloqueo.duracion * 1.6, 52),
        backgroundColor: '#F3F4F6',
        border: '1.5px dashed #D1D5DB',
      }}
    >
      <div className="w-1 self-stretch rounded-full" style={{ backgroundColor: '#9CA3AF' }} />
      <div>
        <p className="text-sm font-medium" style={{ color: '#6B7280' }}>{bloqueo.titulo}</p>
        <p className="text-[11px]" style={{ color: '#9CA3AF' }}>
          {bloqueo.doctor} · {aHora(bloqueo.inicio)}–{aHora(bloqueo.inicio + bloqueo.duracion)}
        </p>
      </div>
      <span className="ml-auto text-[10px] font-semibold px-2 py-0.5 rounded-full" style={{ backgroundColor: '#E5E7EB', color: '#6B7280' }}>
        Google Calendar
      </span>
    </div>
  )
}

export default function Agenda() {
  const [franjas, setFranjas] = useState<Franja[]>(FRANJAS)
  const [verBanner, setVerBanner] = useState(true)
  const fecha = 'Jueves 11 de septiembre, 2026'

  function marcar(id: string, valor: Asistencia) {
    setFranjas((prev) =>
      prev.map((f) => (f.id === id && f.tipo === 'cita' ? { ...f, asistencia: valor } : f)),
    )
  }

  const horas = Array.from({ length: 10 }, (_, i) => i * 60)
  const citas = franjas.filter((f): f is Cita => f.tipo === 'cita')
  const sinMarcar = citas.filter((c) => c.inicio < MINUTO_ACTUAL && c.asistencia === null).length

  const resumen = [
    { etiqueta: 'Agendadas', valor: String(citas.length), color: '#111827' },
    { etiqueta: 'Asistieron', valor: String(citas.filter((c) => c.asistencia === 'asistio').length), color: '#10B981' },
    { etiqueta: 'No asistieron', valor: String(citas.filter((c) => c.asistencia === 'no_asistio').length), color: '#EF4444' },
    { etiqueta: 'Sin marcar', valor: String(sinMarcar), color: sinMarcar > 0 ? '#D97706' : '#10B981' },
  ]

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
      {/* Sin botón de cerrar, y es deliberado: mientras la pantalla no lea de Neon, quien la
          mire tiene que saberlo todo el tiempo, no hasta que lo descarte una vez. */}
      <div
        className="shrink-0 flex items-center gap-3 px-8 py-2.5 border-b"
        style={{ backgroundColor: '#EEF2FF', borderColor: '#C7D2FE' }}
      >
        <span
          className="text-[10px] font-bold px-2 py-1 rounded uppercase tracking-wide shrink-0"
          style={{ backgroundColor: '#4F46E5', color: '#FFFFFF' }}
        >
          Datos de ejemplo
        </span>
        <p className="text-xs" style={{ color: '#3730A3' }}>
          Esta pantalla todavía no lee de la base ni guarda nada. Marcar una asistencia aquí
          no la registra — al recargar vuelve como estaba. <strong>Fase 8.</strong>
        </p>
      </div>

      <header className="shrink-0 flex items-center justify-between px-8 py-4 border-b" style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}>
        <div>
          <h1 className="text-lg font-bold" style={{ color: '#111827' }}>Agenda</h1>
          <p className="text-sm" style={{ color: '#6B7280' }}>{fecha}</p>
        </div>

        <div className="flex items-center gap-2">
          <button className="px-3 py-1.5 rounded-lg text-sm border hover:bg-gray-50 transition-colors" style={{ borderColor: '#E5E7EB', color: '#374151', backgroundColor: '#FFFFFF' }}>← Ayer</button>
          <button className="px-3 py-1.5 rounded-lg text-sm font-semibold border" style={{ borderColor: '#7C3AED', color: '#7C3AED', backgroundColor: '#F5F3FF' }}>Hoy</button>
          <button className="px-3 py-1.5 rounded-lg text-sm border hover:bg-gray-50 transition-colors" style={{ borderColor: '#E5E7EB', color: '#374151', backgroundColor: '#FFFFFF' }}>Mañana →</button>
        </div>

        <div className="flex items-center gap-5">
          {resumen.map((r) => (
            <div key={r.etiqueta} className="text-center">
              <p className="text-xl font-bold" style={{ color: r.color }}>{r.valor}</p>
              <p className="text-[10px]" style={{ color: '#9CA3AF' }}>{r.etiqueta}</p>
            </div>
          ))}
          <button className="ml-4 px-4 py-2 rounded-lg text-sm font-semibold hover:brightness-95 transition-all" style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}>
            + Nueva cita
          </button>
        </div>
      </header>

      {verBanner && AYER_SIN_MARCAR.length > 0 && (
        <div className="shrink-0 flex items-center gap-4 px-8 py-3 border-b" style={{ backgroundColor: '#FFFBEB', borderColor: '#FCD34D' }}>
          <span className="text-lg">⚠️</span>
          <p className="text-sm font-semibold flex-1" style={{ color: '#92400E' }}>
            {AYER_SIN_MARCAR.length} citas de ayer sin marcar —{' '}
            <span className="font-normal">
              {AYER_SIN_MARCAR.map((c) => `${c.paciente} (${c.hora})`).join(' · ')}
            </span>
          </p>
          <button className="text-xs font-semibold underline shrink-0" style={{ color: '#D97706' }}>Ver ayer</button>
          <button className="text-sm shrink-0" style={{ color: '#9CA3AF' }} onClick={() => setVerBanner(false)} aria-label="Ocultar aviso">✕</button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <div className="flex">
          <div className="shrink-0 w-16 relative" style={{ paddingTop: 8 }}>
            {horas.map((h) => (
              <div key={h} className="flex items-start justify-end pr-3" style={{ height: 96 }}>
                <span className="text-xs font-medium" style={{ color: h < MINUTO_ACTUAL ? '#D1D5DB' : '#9CA3AF', marginTop: -8 }}>
                  {aHora(h)}
                </span>
              </div>
            ))}
          </div>

          <div className="flex-1 pr-8 py-2 relative">
            {MINUTO_ACTUAL >= 0 && MINUTO_ACTUAL <= 540 && (
              <div className="absolute left-0 right-8 flex items-center gap-2 z-10 pointer-events-none" style={{ top: 8 + (MINUTO_ACTUAL / 60) * 96 - 1 }}>
                <div className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: '#7C3AED', marginLeft: -5 }} />
                <div className="flex-1 h-0.5 rounded-full" style={{ backgroundColor: '#7C3AED', opacity: 0.5 }} />
                <span className="text-[10px] font-bold shrink-0 px-1.5 py-0.5 rounded" style={{ backgroundColor: '#F5F3FF', color: '#7C3AED' }}>
                  {aHora(MINUTO_ACTUAL)}
                </span>
              </div>
            )}

            {horas.map((h) => {
              const deLaHora = franjas.filter((f) => f.inicio >= h && f.inicio < h + 60)
              const horaPasada = h + 60 <= MINUTO_ACTUAL

              return (
                <div
                  key={h}
                  className="relative flex gap-3 pl-3"
                  style={{ height: 96, borderTop: '1px solid #F3F4F6', backgroundColor: horaPasada ? 'rgba(0,0,0,0.01)' : 'transparent' }}
                >
                  {deLaHora.length === 0 ? (
                    <div className="flex items-center" style={{ paddingTop: 12 }}>
                      {!horaPasada && (
                        <button className="text-xs px-2 py-1 rounded transition-colors" style={{ color: '#E5E7EB' }}>
                          + Agendar
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="flex gap-3 w-full pt-3 pb-2">
                      {deLaHora.map((f) => (
                        <div key={f.id} className="flex-1">
                          {f.tipo === 'bloqueo' ? (
                            <TarjetaBloqueo bloqueo={f} />
                          ) : (
                            <TarjetaCita cita={f} pasada={f.inicio + f.duracion <= MINUTO_ACTUAL} marcar={marcar} />
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

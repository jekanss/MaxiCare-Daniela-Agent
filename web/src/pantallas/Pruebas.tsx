import { useEffect, useRef, useState, type FormEvent } from 'react'
import { hablarConDaniela, reiniciarChat, type RespuestaChat } from '@/api'

const SP = "'Space Grotesk', sans-serif"

/* El chat de pruebas. Es el entregable de la fase 5: alguien conversa con Daniela desde el
 * navegador, sobre el mismo agente que va a correr en producción.
 *
 * ------------------------------------------------------------------------------------
 * Dos cosas que esta pantalla hace y una terminal no
 * ------------------------------------------------------------------------------------
 *
 * 1. Enseña los seis campos de `RespuestaDaniela` al lado de cada respuesta. El texto que
 *    lee el paciente es uno de los seis; los otros cinco son los que hacen que el
 *    orquestador ramifique con un `if` y que salgan las métricas. Ver que el mensaje suena
 *    bien pero el estado quedó en `explorando` cuando debía ser `listo_para_agendar` es
 *    justo el fallo que un chat de terminal esconde.
 *
 * 2. Enseña si saltó un guardrail y si hubo que regenerar. Una respuesta buena que costó
 *    dos llamadas al modelo no es lo mismo que una respuesta buena a la primera.
 *
 * ------------------------------------------------------------------------------------
 * El carril separado
 * ------------------------------------------------------------------------------------
 *
 * Todo lo que pasa aquí vive en el esquema `pruebas` de Neon, nunca en `public`. Sin esa
 * separación, ensayar «quiero cita el martes a las 10» ocuparía un cupo real y la clínica
 * rechazaría a un paciente de verdad por una prueba.
 *
 * La especificación lo exige con estas palabras: «un distintivo permanente y visible, nunca
 * un interruptor escondido en un menú». Por eso la franja de arriba no se puede cerrar. */

type Turno =
  | { de: 'paciente'; texto: string }
  | { de: 'daniela'; texto: string; detalle: RespuestaChat }
  | { de: 'error'; texto: string }

const ETIQUETA_ESTADO: Record<string, string> = {
  explorando: 'Explorando',
  comparando: 'Comparando',
  con_barrera: 'Con barrera',
  listo_para_agendar: 'Listo para agendar',
  agendado: 'Agendado',
  post_atencion: 'Post atención',
}

function Etiqueta({ texto, fondo, color }: { texto: string; fondo: string; color: string }) {
  return (
    <span
      className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full whitespace-nowrap"
      style={{ backgroundColor: fondo, color }}
    >
      {texto}
    </span>
  )
}

function Detalle({ d }: { d: RespuestaChat }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 mt-2">
      <Etiqueta texto={ETIQUETA_ESTADO[d.estado_oportunidad] ?? d.estado_oportunidad} fondo="#EDE9FE" color="#5B21B6" />
      {d.barrera !== 'ninguna' && <Etiqueta texto={`barrera: ${d.barrera}`} fondo="#FEF3C7" color="#92400E" />}
      {d.requiere_escalamiento && <Etiqueta texto={`escala: ${d.motivo_escalamiento}`} fondo="#FEE2E2" color="#991B1B" />}
      {d.fuera_de_alcance && <Etiqueta texto="fuera de alcance" fondo="#F3F4F6" color="#4B5563" />}
      {d.regenerado && <Etiqueta texto="regenerado" fondo="#DBEAFE" color="#1E40AF" />}
      {d.tripwires.map((t) => (
        <Etiqueta key={t} texto={`⛔ ${t}`} fondo="#FEE2E2" color="#991B1B" />
      ))}
      <span className="text-[10px] ml-auto" style={{ color: '#D1D5DB' }}>turno {d.turno}</span>
    </div>
  )
}

export default function Pruebas() {
  const [turnos, setTurnos] = useState<Turno[]>([])
  const [borrador, setBorrador] = useState('')
  const [esperando, setEsperando] = useState(false)
  const [conversacion, setConversacion] = useState<string | null>(null)
  const finRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    finRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turnos, esperando])

  async function enviar(e: FormEvent) {
    e.preventDefault()
    const texto = borrador.trim()
    if (!texto || esperando) return

    setTurnos((t) => [...t, { de: 'paciente', texto }])
    setBorrador('')
    setEsperando(true)
    try {
      const r = await hablarConDaniela(texto, conversacion)
      setConversacion(r.conversacion)
      setTurnos((t) => [...t, { de: 'daniela', texto: r.mensaje, detalle: r }])
    } catch (err) {
      setTurnos((t) => [
        ...t,
        { de: 'error', texto: err instanceof Error ? err.message : 'Falló la llamada.' },
      ])
    } finally {
      setEsperando(false)
    }
  }

  async function reiniciar() {
    await reiniciarChat(conversacion).catch(() => undefined)
    setConversacion(null)
    setTurnos([])
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
      {/* El distintivo permanente. No se puede cerrar. */}
      <div className="shrink-0 flex items-center gap-3 px-8 py-2.5" style={{ backgroundColor: '#92400E' }}>
        <span className="text-[10px] font-bold px-2 py-1 rounded uppercase tracking-widest" style={{ backgroundColor: '#FEF3C7', color: '#92400E' }}>
          🧪 Carril de pruebas
        </span>
        <p className="text-xs" style={{ color: '#FDE68A' }}>
          Nada de lo que pase aquí toca datos reales: se escribe en el esquema
          <code className="mx-1 px-1 rounded" style={{ backgroundColor: 'rgba(0,0,0,0.25)' }}>pruebas</code>
          de Neon, nunca en <code className="mx-1 px-1 rounded" style={{ backgroundColor: 'rgba(0,0,0,0.25)' }}>public</code>.
          Ninguna cita de aquí ocupa un cupo de la clínica.
        </p>
      </div>

      <header className="shrink-0 flex items-center justify-between px-8 py-4 border-b" style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}>
        <div>
          <h1 className="text-lg font-bold" style={{ color: '#111827' }}>Pruebas</h1>
          <p className="text-sm" style={{ color: '#6B7280' }}>
            El mismo agente que responde en WhatsApp, con sus nueve tools y sus seis frenos.
          </p>
        </div>
        <button
          onClick={reiniciar}
          disabled={turnos.length === 0 || esperando}
          className="px-4 py-2 rounded-lg text-sm font-semibold border hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          style={{ borderColor: '#E5E7EB', color: '#374151' }}
        >
          Empezar de nuevo
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-8 py-6">
        <div className="max-w-3xl mx-auto flex flex-col gap-4">
          {turnos.length === 0 && !esperando && (
            <div className="rounded-2xl px-7 py-9 text-center" style={{ backgroundColor: '#FFFFFF', border: '1px dashed #E5E7EB' }}>
              <p className="text-sm font-semibold mb-1.5" style={{ color: '#111827' }}>
                Escríbele como si fueras un paciente
              </p>
              <p className="text-xs leading-relaxed max-w-md mx-auto" style={{ color: '#6B7280' }}>
                Daniela no sabe quién eres hasta que te identifiques, igual que en WhatsApp.
                Al lado de cada respuesta vas a ver el estado de la oportunidad, la barrera
                que detectó y si algún freno tuvo que saltar.
              </p>
            </div>
          )}

          {turnos.map((t, i) =>
            t.de === 'paciente' ? (
              <div key={i} className="self-end max-w-[75%]">
                <div className="rounded-2xl rounded-br-md px-4 py-2.5" style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}>
                  <p className="text-sm whitespace-pre-wrap">{t.texto}</p>
                </div>
              </div>
            ) : t.de === 'daniela' ? (
              <div key={i} className="self-start max-w-[80%]">
                <div className="rounded-2xl rounded-bl-md px-4 py-3" style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}>
                  <p className="text-sm whitespace-pre-wrap" style={{ color: '#111827' }}>{t.texto}</p>
                  <Detalle d={t.detalle} />
                </div>
              </div>
            ) : (
              <div key={i} role="alert" className="self-start max-w-[80%]">
                <div className="rounded-2xl px-4 py-3" style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}>
                  <p className="text-xs font-semibold mb-0.5" style={{ color: '#991B1B' }}>No hubo respuesta</p>
                  <p className="text-xs" style={{ color: '#B91C1C' }}>{t.texto}</p>
                </div>
              </div>
            ),
          )}

          {esperando && (
            <div className="self-start">
              <div className="rounded-2xl rounded-bl-md px-5 py-4 flex items-center gap-2" style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}>
                {[0, 1, 2].map((n) => (
                  <span
                    key={n}
                    className="w-1.5 h-1.5 rounded-full animate-bounce"
                    style={{ backgroundColor: '#C4B5FD', animationDelay: `${n * 120}ms` }}
                  />
                ))}
              </div>
            </div>
          )}

          <div ref={finRef} />
        </div>
      </div>

      <form onSubmit={enviar} className="shrink-0 px-8 py-4 border-t" style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}>
        <div className="max-w-3xl mx-auto flex gap-2">
          <input
            value={borrador}
            onChange={(e) => setBorrador(e.target.value)}
            placeholder="Hola, quisiera saber cuánto cuesta un implante…"
            disabled={esperando}
            aria-label="Mensaje del paciente"
            className="flex-1 px-4 py-3 text-sm rounded-xl outline-none transition-all focus:ring-3"
            style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
          />
          <button
            type="submit"
            disabled={esperando || !borrador.trim()}
            className="px-5 rounded-xl text-sm font-bold transition-all disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95"
            style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}
          >
            Enviar
          </button>
        </div>
        <p className="max-w-3xl mx-auto text-[10px] mt-2" style={{ color: '#9CA3AF' }}>
          Cada mensaje gasta tokens de verdad. Una conversación de ocho mensajes cuesta
          alrededor de 500 pesos.
        </p>
      </form>
    </div>
  )
}

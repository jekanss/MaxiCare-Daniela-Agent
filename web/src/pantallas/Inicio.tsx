import { useCallback, useEffect, useRef, useState } from 'react'
import { leerInicio, SesionCaducada, type ResumenInicio } from '@/api'

/* La portada del panel: cinco números que contestan en cinco segundos si Daniela está
 * funcionando. Sigue `docs/diseno/panel-2026-09-22.dc.html` (líneas 178-330).
 *
 * De SOLO LECTURA, y eso se decide en el servidor: `/api/inicio` no reconcilia contra
 * Google Calendar, al revés que `/api/agenda`. Es la primera pantalla de cada sesión. */

const SG = "'Space Grotesk', sans-serif"
const MONO = "'JetBrains Mono', monospace"

type Props = { alCaducarSesion: () => void }

type Tarjeta = { rotulo: string; valor: string; pie: string; barra: number | null }

/** Las cinco tarjetas, con sus estados vacíos resueltos ANTES que los llenos.
 *
 * La clínica lleva menos de una semana funcionando y tiene cero citas: ese es el estado en
 * que MaxiCare va a ver esta pantalla las próximas semanas, así que es el que tiene que
 * quedar bien. Un cero no es un mal resultado y la pantalla no puede sugerir que lo sea --
 * por eso «llegaron» con cero citas cumplidas dice «sin citas cumplidas todavía» y no
 * `0 %`, que se lee como un fracaso.
 *
 * `barra` es null cuando no hay denominador: una barra de progreso sobre cero es una barra
 * vacía, y una barra vacía dice «fallaste», no «todavía no». */
function tarjetas(r: ResumenInicio): Tarjeta[] {
  const { llegaron, marcadas, cumplibles } = r.asistencia
  return [
    {
      rotulo: 'Personas que escribieron',
      valor: String(r.escribieron),
      pie: `antes: ${r.linea_base.conversaciones_mes} conversaciones al mes`,
      barra: null,
    },
    {
      rotulo: 'Quedaron con cita',
      valor: String(r.con_cita),
      pie: r.escribieron
        ? `${r.con_cita} de ${r.escribieron} · antes: ${r.linea_base.citas_mes} al mes`
        : `antes: ${r.linea_base.citas_mes} al mes`,
      barra: r.escribieron ? r.con_cita / r.escribieron : null,
    },
    {
      rotulo: 'Llegaron a la cita',
      valor: cumplibles === 0 || marcadas === 0 ? '—' : String(llegaron),
      pie:
        cumplibles === 0
          ? 'sin citas cumplidas todavía'
          : marcadas === 0
            ? `ninguna de ${cumplibles} marcada todavía`
            : `${marcadas} de ${cumplibles} marcadas`,
      barra: marcadas ? llegaron / marcadas : null,
    },
    {
      rotulo: 'Sin contestar',
      valor: String(r.sin_contestar),
      pie:
        r.sin_contestar === 0
          ? `ninguno · antes quedaba sin respuesta el ${r.linea_base.sin_responder_pct} %`
          : 'hay mensajes esperando respuesta',
      barra: null,
    },
    {
      rotulo: 'Tu tiempo',
      valor: `${r.relevo.minutos} min`,
      pie:
        r.relevo.conversaciones === 0
          ? 'no tuviste que entrar en ninguna conversación'
          : `en ${r.relevo.conversaciones} conversaciones`,
      barra: null,
    },
  ]
}

const DIA_LARGO: Intl.DateTimeFormatOptions = { weekday: 'long', day: 'numeric', month: 'long' }

function saludo(hora: number): string {
  if (hora < 12) return 'Buenos días'
  if (hora < 19) return 'Buenas tardes'
  return 'Buenas noches'
}

function Cabecera({ resumen }: { resumen: ResumenInicio | null }) {
  const ahora = new Date()
  return (
    <header
      className="relative overflow-hidden"
      style={{ backgroundColor: '#FFFFFF', borderBottom: '1px solid #DCD8E6' }}
    >
      <div
        aria-hidden="true"
        className="absolute inset-0"
        style={{
          backgroundImage: 'radial-gradient(#D6D0E4 1px, transparent 1px)',
          backgroundSize: '96px 84px',
          opacity: 0.55,
        }}
      />
      <div
        className="relative w-full mx-auto flex flex-wrap items-end"
        style={{
          maxWidth: 1240,
          padding: 'clamp(24px,3.4vw,44px) clamp(18px,3.4vw,44px) clamp(22px,2.6vw,34px)',
          gap: 'clamp(18px,3vw,44px)',
        }}
      >
        <div className="flex flex-col gap-3 min-w-0" style={{ flex: '1 1 380px' }}>
          <div className="flex items-center gap-2.5">
            <span
              aria-hidden="true"
              className="relative block shrink-0"
              style={{ width: 9, height: 9, backgroundColor: '#16A34A' }}
            >
              <span
                className="absolute inset-0 block"
                style={{ backgroundColor: '#16A34A', animation: 'mcPulse 2.4s ease-out infinite' }}
              />
            </span>
            <span
              style={{
                fontFamily: MONO,
                fontSize: 11,
                letterSpacing: '0.16em',
                textTransform: 'uppercase',
                color: '#6E6880',
              }}
            >
              Asistente activa · {ahora.toLocaleDateString('es-CO', DIA_LARGO)}
            </span>
          </div>
          <h1
            className="m-0"
            style={{
              fontFamily: SG,
              fontWeight: 600,
              fontSize: 'clamp(27px,3.4vw,44px)',
              lineHeight: 1.06,
              letterSpacing: '-0.032em',
              color: '#16111F',
            }}
          >
            {saludo(ahora.getHours())}
            {resumen ? `, ${resumen.usuario}.` : '.'}
          </h1>
          {/* La ventana es de 30 días y las cinco cifras la comparten. El mockup decía «las
              últimas 24 horas»; copiarlo habría puesto un rótulo que miente sobre lo que
              hay debajo. */}
          <p
            className="m-0"
            style={{
              fontSize: 'clamp(14.5px,1.2vw,16.5px)',
              fontWeight: 300,
              lineHeight: 1.6,
              color: '#4A4458',
              maxWidth: '56ch',
            }}
          >
            Los últimos {resumen?.dias ?? 30} días, comparados con cómo iba la clínica antes
            de Daniela.
          </p>
        </div>
      </div>
    </header>
  )
}

/** El armazón compartido por los tres estados. Repetir la cabecera en los tres es lo que
 *  impide que la pantalla pierda su identidad mientras carga: nunca un mensaje suelto en
 *  lugar de la página. Es el mismo patrón de `SinResolver.tsx`. */
function Marco({ resumen, children }: { resumen: ResumenInicio | null; children: React.ReactNode }) {
  return (
    <div className="flex-1 overflow-y-auto" style={{ fontFamily: SG, backgroundColor: '#F7F6FA' }}>
      <Cabecera resumen={resumen} />
      <div
        className="w-full mx-auto flex flex-col"
        style={{
          maxWidth: 1240,
          padding: 'clamp(20px,2.6vw,34px) clamp(18px,3.4vw,44px) clamp(40px,5vw,72px)',
          gap: 'clamp(18px,2.4vw,30px)',
        }}
      >
        {children}
      </div>
    </div>
  )
}

function Cifra({ tarjeta, retraso }: { tarjeta: Tarjeta; retraso: number }) {
  return (
    <article
      className="flex flex-col gap-3"
      style={{
        backgroundColor: '#FFFFFF',
        border: '1px solid #DCD8E6',
        padding: '18px 18px 16px',
        animation: `mcRise 440ms ease both ${retraso}ms`,
      }}
    >
      <span
        style={{
          fontFamily: MONO,
          fontSize: 10.5,
          letterSpacing: '0.15em',
          textTransform: 'uppercase',
          color: '#6E6880',
        }}
      >
        {tarjeta.rotulo}
      </span>
      <span
        style={{
          fontFamily: SG,
          fontWeight: 600,
          fontSize: 'clamp(30px,3.2vw,40px)',
          lineHeight: 1,
          letterSpacing: '-0.03em',
          color: '#16111F',
          animation: `mcTick 520ms ease both ${retraso + 100}ms`,
        }}
      >
        {tarjeta.valor}
      </span>
      {tarjeta.barra !== null && (
        <span
          aria-hidden="true"
          className="relative block overflow-hidden"
          style={{ height: 3, backgroundColor: '#EDE9FE' }}
        >
          <span
            className="absolute inset-y-0 left-0 block"
            style={{
              width: `${Math.min(100, Math.round(tarjeta.barra * 100))}%`,
              backgroundColor: '#7C3AED',
              transformOrigin: 'left',
              animation: `mcGrow 900ms cubic-bezier(.22,1,.36,1) both ${retraso + 200}ms`,
            }}
          />
        </span>
      )}
      <span style={{ fontSize: 13, fontWeight: 300, color: '#4A4458' }}>{tarjeta.pie}</span>
    </article>
  )
}

export default function Inicio({ alCaducarSesion }: Props) {
  const [resumen, setResumen] = useState<ResumenInicio | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')

  // Por `ref` y NO en las deps del useCallback: meter la prop en deps deja la pantalla
  // releyendo Neon en bucle, porque `App.tsx` la pasa como una flecha nueva en cada render.
  // Es la trampa documentada en `web/CLAUDE.md`.
  const caducar = useRef(alCaducarSesion)
  caducar.current = alCaducarSesion

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      setResumen(await leerInicio())
      setError('')
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else setError('No se pudo cargar el resumen.')
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  if (cargando) {
    return (
      <Marco resumen={null}>
        <p className="text-sm" style={{ color: '#6E6880' }}>
          Cargando…
        </p>
      </Marco>
    )
  }

  // Si la consulta falló, la pantalla lo DICE. Nunca una cifra de respaldo: un número
  // inventado en una portada es peor que una portada vacía, porque nadie sabe que lo es.
  if (error || !resumen) {
    return (
      <Marco resumen={null}>
        <div
          role="alert"
          style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA', padding: '12px 16px' }}
        >
          <p className="text-sm m-0" style={{ color: '#B91C1C' }}>
            {error || 'No se pudo cargar el resumen.'}
          </p>
        </div>
      </Marco>
    )
  }

  return (
    <Marco resumen={resumen}>
      <section
        aria-label="Los cinco números"
        className="grid"
        style={{
          gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%,208px), 1fr))',
          gap: 'clamp(12px,1.4vw,18px)',
        }}
      >
        {tarjetas(resumen).map((t, i) => (
          <Cifra key={t.rotulo} tarjeta={t} retraso={60 + i * 60} />
        ))}
      </section>
    </Marco>
  )
}

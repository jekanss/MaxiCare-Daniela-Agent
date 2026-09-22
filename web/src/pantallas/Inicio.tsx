import { useCallback, useEffect, useRef, useState } from 'react'
import { leerInicio, SesionCaducada, type ResumenInicio } from '@/api'
import { hhmm } from '@/pantallas/Agenda'
import { titulo } from '@/pantallas/SinResolver'

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

/* La clínica está en Bogotá y la pantalla se puede abrir desde cualquier parte. Todo lo que
   se lee como «hoy» va clavado a esa zona: sin `timeZone`, un portátil en otro huso saluda
   «buenas noches» a media mañana y pone la fecha del día anterior. Es el mismo criterio que
   ya aplica `Agenda.tsx` a las horas de las citas. */
const ZONA = 'America/Bogota'
const FMT_HOY = new Intl.DateTimeFormat('es-CO', {
  timeZone: ZONA,
  weekday: 'long',
  day: 'numeric',
  month: 'long',
})
const FMT_HORA_BOGOTA = new Intl.DateTimeFormat('en-GB', {
  timeZone: ZONA,
  hour: '2-digit',
  hour12: false,
})

/* `volumen[].dia` es una fecha pelada («2026-09-16») que Postgres ya cortó en hora de
   Bogotá. `new Date` la lee como medianoche UTC, así que se formatea EN UTC: con la zona
   del navegador, un portátil al oeste de Greenwich pintaría el día anterior en cada rótulo. */
const FMT_DIA_CORTO = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'UTC',
  day: 'numeric',
})
const FMT_DIA_MES = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'UTC',
  day: 'numeric',
  month: 'long',
})

function saludo(ahora: Date): string {
  const hora = Number(FMT_HORA_BOGOTA.format(ahora))
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
              Asistente activa · {FMT_HOY.format(ahora)}
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
            {saludo(ahora)}
            {resumen ? `, ${resumen.usuario}.` : '.'}
          </h1>
          {/* La ventana la dice el SERVIDOR, y por eso esta frase no aparece hasta que hay
              respuesta: escribir «30 días» mientras carga sería una cifra del frontend que
              podría no ser la que se consultó. El mockup decía «las últimas 24 horas»;
              copiarlo habría puesto un rótulo que miente sobre lo que hay debajo. */}
          {resumen && (
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
              Los últimos {resumen.dias} días, comparados con cómo iba la clínica antes de
              Daniela.
            </p>
          )}
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

/** El armazón de los bloques de abajo: franja de color arriba, título, y a la derecha o un
 *  recuento o un enlace. Uno solo para los tres, porque los tres son la misma caja. */
function Bloque({
  id,
  franja,
  encabezado,
  derecha,
  retraso,
  children,
}: {
  id: string
  franja: string
  encabezado: string
  derecha: React.ReactNode
  retraso: number
  children: React.ReactNode
}) {
  return (
    <section
      aria-labelledby={id}
      className="flex flex-col"
      style={{
        backgroundColor: '#FFFFFF',
        border: '1px solid #DCD8E6',
        animation: `mcRise 460ms ease both ${retraso}ms`,
      }}
    >
      <div aria-hidden="true" style={{ height: 4, backgroundColor: franja }} />
      <div
        className="flex items-baseline justify-between gap-3"
        style={{ padding: '20px clamp(16px,2vw,24px) 6px' }}
      >
        <h2
          id={id}
          className="m-0"
          style={{
            fontFamily: SG,
            fontWeight: 600,
            fontSize: 'clamp(18px,1.7vw,22px)',
            letterSpacing: '-0.022em',
            color: '#16111F',
          }}
        >
          {encabezado}
        </h2>
        {derecha}
      </div>
      <div style={{ padding: '12px clamp(16px,2vw,24px) 22px' }}>{children}</div>
    </section>
  )
}

function Rotulo({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="whitespace-nowrap"
      style={{
        fontFamily: MONO,
        fontSize: 10.5,
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: '#6E6880',
      }}
    >
      {children}
    </span>
  )
}

function Enlace({ a, children }: { a: string; children: React.ReactNode }) {
  return (
    <a
      href={a}
      className="whitespace-nowrap transition-colors hover:text-[#4C1D95]"
      style={{
        fontFamily: MONO,
        fontSize: 10.5,
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: '#6D28D9',
      }}
    >
      {children}
    </a>
  )
}

function Vacio({ children }: { children: React.ReactNode }) {
  return (
    <p className="m-0" style={{ fontSize: 13.5, fontWeight: 300, color: '#6E6880' }}>
      {children}
    </p>
  )
}

/** La agenda de hoy. El enlace lleva a `#/agenda` y no reconstruye el día aquí: es ALLÍ
 *  donde el sistema contrasta las citas contra Google Calendar, y esa promesa vive en una
 *  sola pantalla. */
function AgendaDeHoy({ citas }: { citas: ResumenInicio['agenda_hoy'] }) {
  return (
    <Bloque
      id="mc-hoy"
      franja="#7C3AED"
      encabezado="La agenda de hoy"
      derecha={
        citas.length ? (
          <Rotulo>
            {citas.length} {citas.length === 1 ? 'cita' : 'citas'}
          </Rotulo>
        ) : (
          <Enlace a="#/agenda">Ver agenda</Enlace>
        )
      }
      retraso={300}
    >
      {citas.length === 0 ? (
        <Vacio>No hay citas para hoy.</Vacio>
      ) : (
        <ul className="m-0 p-0 list-none flex flex-col">
          {citas.map((c) => (
            <li
              key={c.id}
              className="flex items-center gap-3.5"
              style={{ padding: '13px 0', borderBottom: '1px solid #ECE8F4' }}
            >
              <span
                className="shrink-0"
                style={{ fontFamily: MONO, fontSize: 12.5, color: '#4C1D95', width: 52 }}
              >
                {hhmm(c.inicio)}
              </span>
              <span className="flex-1 min-w-0 flex flex-col gap-0.5">
                <span className="truncate" style={{ fontSize: 14.5, fontWeight: 500, color: '#16111F' }}>
                  {c.nombre_completo} · {c.tratamiento}
                </span>
                {/* `asistio` tiene TRES valores y el `null` no se colapsa con el `false`:
                    decir «no vino» cuando lo único cierto es que nadie lo ha marcado le
                    apunta al paciente una falta que no cometió. */}
                <span style={{ fontSize: 12.5, fontWeight: 300, color: '#6E6880' }}>
                  {c.asistio === true
                    ? 'Llegó'
                    : c.asistio === false
                      ? 'No llegó'
                      : 'Sin marcar'}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Bloque>
  )
}

/** Los tres casos más frecuentes de «sin resolver». El título lo compone `titulo()` de esa
 *  misma pantalla: la huella cruda es un identificador, no una frase para la clínica. */
function NecesitaAtencion({ casos }: { casos: ResumenInicio['atencion'] }) {
  return (
    <Bloque
      id="mc-atencion"
      franja="#16111F"
      encabezado="Qué necesita su atención"
      derecha={<Enlace a="#/bandeja">Ver todo</Enlace>}
      retraso={360}
    >
      {casos.length === 0 ? (
        <Vacio>Nada sin resolver. Buena señal.</Vacio>
      ) : (
        <ul className="m-0 p-0 list-none flex flex-col">
          {casos.map((caso) => (
            <li
              key={caso.huella}
              className="flex gap-3.5"
              style={{ padding: '14px 0', borderBottom: '1px solid #ECE8F4' }}
            >
              <span
                className="shrink-0"
                style={{
                  fontFamily: SG,
                  fontWeight: 600,
                  fontSize: 22,
                  letterSpacing: '-0.02em',
                  color: '#6D28D9',
                  width: 36,
                }}
              >
                {caso.contador}
              </span>
              <span className="flex-1 min-w-0 flex flex-col gap-1">
                <span style={{ fontSize: 14.5, fontWeight: 500, color: '#16111F' }}>
                  {titulo(caso)}
                </span>
                {caso.informe && (
                  <span style={{ fontSize: 12.5, fontWeight: 300, lineHeight: 1.5, color: '#4A4458' }}>
                    {caso.informe.que_paso}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Bloque>
  )
}

/** El volumen por día, con divs y no con una librería de gráficos: no hay ninguna en
 *  `package.json` y una dependencia nueva por ocho barras no se paga.
 *
 *  El rótulo dice el primer día CON datos, nunca «30 días»: un eje de treinta días con
 *  veintiséis vacíos dibuja una caída que no ocurrió. Y si no hay ni un día, el bloque no se
 *  pinta: un gráfico vacío no informa de nada. */
function Volumen({ datos }: { datos: ResumenInicio['volumen'] }) {
  if (datos.length === 0) return null

  const tope = Math.max(...datos.map((d) => d.conversaciones))
  const todosLosRotulos = datos.length <= 12

  return (
    <section
      aria-labelledby="mc-volumen"
      className="flex flex-col gap-4"
      style={{
        backgroundColor: '#FFFFFF',
        border: '1px solid #DCD8E6',
        padding: 'clamp(18px,2.2vw,26px)',
        animation: 'mcRise 460ms ease both 420ms',
      }}
    >
      <h2
        id="mc-volumen"
        className="m-0"
        style={{
          fontFamily: SG,
          fontWeight: 600,
          fontSize: 'clamp(17px,1.6vw,21px)',
          letterSpacing: '-0.022em',
          color: '#16111F',
        }}
      >
        Conversaciones por día · desde el {FMT_DIA_MES.format(new Date(datos[0].dia))}
      </h2>

      <div className="flex items-end gap-1.5" style={{ height: 96 }}>
        {datos.map((d, i) => (
          <div key={d.dia} className="flex-1 flex flex-col items-center gap-1.5 min-w-0">
            <span
              title={`${d.conversaciones} el ${FMT_DIA_MES.format(new Date(d.dia))}`}
              className="w-full block"
              style={{
                height: `${Math.max(3, Math.round((d.conversaciones / tope) * 74))}px`,
                backgroundColor: '#7C3AED',
                transformOrigin: 'bottom',
                animation: `mcGrowY 700ms cubic-bezier(.22,1,.36,1) both ${440 + i * 40}ms`,
              }}
            />
            <span
              style={{
                fontFamily: MONO,
                fontSize: 9.5,
                color: '#6E6880',
                visibility:
                  todosLosRotulos || i === 0 || i === datos.length - 1 ? 'visible' : 'hidden',
              }}
            >
              {FMT_DIA_CORTO.format(new Date(d.dia))}
            </span>
          </div>
        ))}
      </div>
    </section>
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

      <div
        className="grid items-start"
        style={{
          gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%,330px), 1fr))',
          gap: 'clamp(12px,1.4vw,18px)',
        }}
      >
        <AgendaDeHoy citas={resumen.agenda_hoy} />
        <NecesitaAtencion casos={resumen.atencion} />
      </div>

      <Volumen datos={resumen.volumen} />
    </Marco>
  )
}

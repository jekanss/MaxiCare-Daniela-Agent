import { useCallback, useEffect, useRef, useState } from 'react'
import {
  leerAgenda,
  marcarAsistencia,
  SesionCaducada,
  type AgendaDelDia,
  type BloqueoDeAgenda,
  type CitaDeAgenda,
  type CorreccionDeAgenda,
} from '@/api'

const SP = "'Space Grotesk', sans-serif"

/* La Agenda, con la jerarquía visual que trajo de Figma intacta: la acción de asistencia es la
 * más grande y la más fácil de encontrar de toda la aplicación. Es deliberado y no es estética:
 * lo pulsa alguien de pie, con prisa, entre un paciente y el siguiente, y es el dato más
 * importante del producto --hoy vive en un cuaderno--.
 *
 * Lo que la maqueta inventaba y aquí no está: el `origen` del paciente («Instagram», «Google
 * Ads», «referido»). Ese dato NO EXISTE en la base. No se sustituye por una suposición
 * plausible ni se pinta un `PENDIENTE`: en una pantalla de cara al usuario, lo que no existe no
 * se muestra.
 *
 * Abrir un día ESCRIBE: el servidor reconcilia contra Google Calendar y puede mover o cancelar
 * una cita. Lo que corrija baja en `correcciones` y se pinta arriba, con la hora vieja dentro,
 * porque una cita que se fue a otro día deja un hueco que nadie sabría explicar. */

const ZONA = 'America/Bogota'

const FMT_HORA = new Intl.DateTimeFormat('es-CO', {
  timeZone: ZONA,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})
const FMT_DIA = new Intl.DateTimeFormat('en-CA', {
  timeZone: ZONA,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})
const FMT_PALABRAS = new Intl.DateTimeFormat('es-CO', {
  timeZone: 'UTC',
  weekday: 'long',
  day: 'numeric',
  month: 'long',
  year: 'numeric',
})
const FMT_CORTA = new Intl.DateTimeFormat('es-CO', {
  timeZone: ZONA,
  day: 'numeric',
  month: 'short',
})

/* Un ISO sin zona es hora de pared de Bogotá --el servidor arma los rangos del día con
   `ZONA_BOGOTA`--, y `new Date` lo leería con la zona del navegador: un portátil en otro huso
   pintaría toda la agenda corrida. Colombia no tiene horario de verano, así que el desfase fijo
   de -05:00 es exacto todo el año. Si el ISO ya trae zona, se respeta la suya. */
function instante(iso: string): number {
  const conZona = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso)
  return new Date(conZona ? iso : `${iso}-05:00`).getTime()
}

function hhmm(iso: string): string {
  return FMT_HORA.format(instante(iso))
}

function minutosDelDia(iso: string): number {
  const [h, m] = hhmm(iso).split(':').map(Number)
  return h * 60 + m
}

function fechaCorta(iso: string): string {
  return FMT_CORTA.format(instante(iso))
}

/** El día de un ISO en Bogotá, como `YYYY-MM-DD`. */
function diaDe(iso: string): string {
  return FMT_DIA.format(instante(iso))
}

function hoyEnBogota(): string {
  return FMT_DIA.format(new Date())
}

/** Aritmética de días sobre `YYYY-MM-DD`, en UTC para que ningún cambio de huso del navegador
 *  convierta «ayer» en «hoy». */
function sumarDias(dia: string, n: number): string {
  const [a, m, d] = dia.split('-').map(Number)
  return new Date(Date.UTC(a, m - 1, d + n)).toISOString().slice(0, 10)
}

function enPalabras(dia: string): string {
  const [a, m, d] = dia.split('-').map(Number)
  const texto = FMT_PALABRAS.format(new Date(Date.UTC(a, m - 1, d)))
  return texto.charAt(0).toUpperCase() + texto.slice(1)
}

function mensajeDe(err: unknown): string {
  return err instanceof Error && err.message ? err.message : 'Algo salió mal.'
}

const ALTO_HORA = 96

// ------------------------------------------------------------------------------------------

function Cabecera({
  dia,
  irA,
  resumen,
}: {
  dia: string
  irA: (d: string) => void
  resumen: { etiqueta: string; valor: string; color: string }[] | null
}) {
  const hoy = hoyEnBogota()
  return (
    <header
      className="shrink-0 flex items-center justify-between gap-6 px-8 py-4 border-b flex-wrap"
      style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}
    >
      <div className="min-w-0">
        <h1 className="text-lg font-bold" style={{ color: '#111827' }}>Agenda</h1>
        <p className="text-sm truncate" style={{ color: '#6B7280' }}>{enPalabras(dia)}</p>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => irA(sumarDias(dia, -1))}
          className="px-3 py-1.5 rounded-lg text-sm border hover:bg-gray-50 transition-colors"
          style={{ borderColor: '#E5E7EB', color: '#374151', backgroundColor: '#FFFFFF' }}
        >
          ← Ayer
        </button>
        <button
          onClick={() => irA(hoy)}
          className="px-3 py-1.5 rounded-lg text-sm font-semibold border"
          style={
            dia === hoy
              ? { borderColor: '#7C3AED', color: '#7C3AED', backgroundColor: '#F5F3FF' }
              : { borderColor: '#E5E7EB', color: '#374151', backgroundColor: '#FFFFFF' }
          }
        >
          Hoy
        </button>
        <button
          onClick={() => irA(sumarDias(dia, 1))}
          className="px-3 py-1.5 rounded-lg text-sm border hover:bg-gray-50 transition-colors"
          style={{ borderColor: '#E5E7EB', color: '#374151', backgroundColor: '#FFFFFF' }}
        >
          Mañana →
        </button>
        <input
          type="date"
          value={dia}
          onChange={(e) => e.target.value && irA(e.target.value)}
          aria-label="Ver otro día"
          className="px-3 py-1.5 rounded-lg text-sm border"
          style={{ borderColor: '#E5E7EB', color: '#374151', backgroundColor: '#FFFFFF' }}
        />
      </div>

      <div className="flex items-center gap-5">
        {resumen?.map((r) => (
          <div key={r.etiqueta} className="text-center">
            <p className="text-xl font-bold" style={{ color: r.color }}>{r.valor}</p>
            <p className="text-[10px]" style={{ color: '#9CA3AF' }}>{r.etiqueta}</p>
          </div>
        ))}
      </div>
    </header>
  )
}

function TarjetaCita({
  cita,
  pasada,
  ocupada,
  marcar,
}: {
  cita: CitaDeAgenda
  pasada: boolean
  ocupada: boolean
  marcar: (id: string, v: boolean | null) => void
}) {
  const sinMarcar = pasada && cita.asistio === null
  /* Un MÍNIMO, no una altura: lo que de verdad mide la tarjeta lo decide su contenido, y con
     los dos botones de marcar son 137 px. Por eso el desborde no lo arregla este número --se
     intentó, acotándolo a lo que cabe en una fila de 96 px, y la tarjeta siguió midiendo 137--
     sino la rejilla, que ahora deja crecer la fila de cada hora. El detalle está abajo, donde
     se pintan las horas.

     Lo que este número sí hace es que una cita corta no quede raquítica y que una larga se vea
     más alta que una de media hora. Lo que la altura deja de contar --que ya no es una
     proporción exacta-- lo dice el rango horario de la esquina, que es un dato. */
  const alto = Math.min(Math.max(cita.duracion_minutos * 1.6, 72), ALTO_HORA - 20)

  return (
    <div
      className="rounded-xl px-4 py-3 flex flex-col gap-2 relative overflow-hidden"
      style={{
        minHeight: alto,
        backgroundColor: sinMarcar ? '#FFFBEB' : '#FFFFFF',
        border: `1.5px solid ${sinMarcar ? '#FCD34D' : '#E5E7EB'}`,
        boxShadow: sinMarcar ? '0 0 0 3px rgba(251,191,36,0.15)' : '0 1px 3px rgba(0,0,0,0.06)',
        opacity: ocupada ? 0.6 : 1,
      }}
    >
      <div
        className="absolute left-0 top-0 bottom-0 w-1 rounded-l-xl"
        style={{
          backgroundColor:
            cita.asistio === true ? '#10B981'
            : cita.asistio === false ? '#EF4444'
            : sinMarcar ? '#F59E0B'
            : '#7C3AED',
        }}
      />

      <div className="pl-1 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-0.5">
            <p className="text-sm font-semibold truncate" style={{ color: '#111827' }}>{cita.nombre_completo}</p>
            {cita.asistio === true && (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full shrink-0" style={{ backgroundColor: '#D1FAE5', color: '#065F46' }}>✓ Asistió</span>
            )}
            {cita.asistio === false && (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full shrink-0" style={{ backgroundColor: '#FEE2E2', color: '#991B1B' }}>✗ No asistió</span>
            )}
          </div>
          <p className="text-xs truncate" style={{ color: '#6B7280' }}>{cita.tratamiento}</p>
          <div className="flex items-center gap-1.5 mt-1">
            <span className="text-[10px]" style={{ color: '#9CA3AF' }}>{cita.telefono}</span>
            {cita.estado === 'reprogramada' && (
              <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full" style={{ backgroundColor: '#F3F4F6', color: '#6B7280' }}>
                Reprogramada
              </span>
            )}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <p className="text-xs font-medium" style={{ color: '#9CA3AF' }}>{hhmm(cita.inicio)}</p>
          <p className="text-[10px]" style={{ color: '#D1D5DB' }}>
            a {FMT_HORA.format(instante(cita.inicio) + cita.duracion_minutos * 60_000)}
          </p>
        </div>
      </div>

      {/* El botón solo existe si la hora de inicio ya pasó. Un «no asistió» puesto a las 8:00
          sobre una cita de las 16:00 sería una marca falsa sobre alguien que todavía va a
          venir --y el servidor la rechaza igual--. */}
      {pasada && cita.asistio === null && (
        <div className="pl-1 flex gap-2 mt-1">
          <button
            onClick={() => marcar(cita.id, true)}
            disabled={ocupada}
            className="flex-1 py-2.5 rounded-lg text-sm font-bold transition-colors flex items-center justify-center gap-1.5 hover:brightness-95 disabled:cursor-wait"
            style={{ backgroundColor: '#10B981', color: '#FFFFFF', boxShadow: '0 2px 8px rgba(16,185,129,0.35)' }}
          >
            ✓ Asistió
          </button>
          <button
            onClick={() => marcar(cita.id, false)}
            disabled={ocupada}
            className="flex-1 py-2.5 rounded-lg text-sm font-bold transition-colors flex items-center justify-center gap-1.5 hover:brightness-95 disabled:cursor-wait"
            style={{ backgroundColor: '#FEF2F2', color: '#DC2626', border: '1.5px solid #FECACA' }}
          >
            ✗ No asistió
          </button>
        </div>
      )}

      {pasada && cita.asistio !== null && (
        <div className="pl-1">
          <button
            onClick={() => marcar(cita.id, null)}
            disabled={ocupada}
            className="text-[10px] hover:underline disabled:cursor-wait"
            style={{ color: '#9CA3AF' }}
          >
            Corregir marcación
          </button>
        </div>
      )}
    </div>
  )
}

function TarjetaBloqueo({ bloqueo }: { bloqueo: BloqueoDeAgenda }) {
  const inicio = minutosDelDia(bloqueo.inicio)
  const fin = minutosDelDia(bloqueo.fin)
  const dura = fin > inicio ? fin - inicio : 24 * 60 - inicio

  return (
    <div
      className="rounded-xl px-4 py-3 flex items-center gap-3"
      style={{
        minHeight: Math.max(dura * 1.6, 52),
        backgroundColor: '#F3F4F6',
        border: '1.5px dashed #D1D5DB',
      }}
    >
      <div className="w-1 self-stretch rounded-full" style={{ backgroundColor: '#9CA3AF' }} />
      <div className="min-w-0">
        <p className="text-sm font-medium truncate" style={{ color: '#6B7280' }}>{bloqueo.titulo}</p>
        <p className="text-[11px]" style={{ color: '#9CA3AF' }}>
          {hhmm(bloqueo.inicio)}–{hhmm(bloqueo.fin)}
        </p>
      </div>
      <span className="ml-auto text-[10px] font-semibold px-2 py-0.5 rounded-full shrink-0" style={{ backgroundColor: '#E5E7EB', color: '#6B7280' }}>
        Google Calendar
      </span>
    </div>
  )
}

/** Una corrección de la reconciliación, en la frase que la recepcionista necesita: la hora
 *  vieja dentro, porque el hueco que dejó es lo que ella está mirando. */
function frase(c: CorreccionDeAgenda): string {
  const vieja = `${fechaCorta(c.hora_vieja)} a las ${hhmm(c.hora_vieja)}`
  if (c.que_paso === 'cancelada' || !c.hora_nueva) {
    return `estaba el ${vieja} y ya no está en Google Calendar: se canceló`
  }
  return `estaba el ${vieja} y ahora es el ${fechaCorta(c.hora_nueva)} a las ${hhmm(c.hora_nueva)}`
}

// ------------------------------------------------------------------------------------------

export default function Agenda({ alCaducarSesion }: { alCaducarSesion: () => void }) {
  const [dia, setDia] = useState<string>(hoyEnBogota)
  const [datos, setDatos] = useState<AgendaDelDia | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')
  const [aviso, setAviso] = useState('')
  const [ocupadas, setOcupadas] = useState<string[]>([])
  const [verSinMarcar, setVerSinMarcar] = useState(false)
  const [ahora, setAhora] = useState(() => Date.now())

  /* `alCaducarSesion` entra por una `ref` y NO por las dependencias de `recargar`. No es
     estilo: `App.tsx` lo pasa como una flecha nueva en cada render, así que meterlo en las
     deps cambiaría la identidad de `recargar` en cada render y el efecto que depende de ella
     volvería a leer Neon sin parar --y aquí cada lectura además llama a Google Calendar--.
     No hay prueba ni regla de lint que lo atrape: se vería como lentitud y una factura rara.
     Es la trampa documentada en `web/CLAUDE.md`. */
  const caducar = useRef(alCaducarSesion)
  useEffect(() => {
    caducar.current = alCaducarSesion
  })

  /* El día que se está pidiendo ahora mismo. Va en una `ref` y no en el estado porque lo leen
     los `await` de abajo, no el render, y porque tiene que estar puesto ANTES de la petición.
     Es lo que descarta una respuesta que ya no corresponde a lo que la pantalla muestra. */
  const pedido = useRef(dia)

  /* El día viaja como ARGUMENTO, no como dependencia: así `recargar` conserva las deps vacías
     que exige la regla 3 de `web/CLAUDE.md` y el efecto de abajo decide cuándo se pide.

     Dos lecturas en vuelo es el caso normal, no el raro: cada una reconcilia contra Google y
     tarda de 3 a 5 segundos, y llegar al lunes son dos clics en «Ayer» seguidos. Si la de D-1
     resolviera después de la de D-2, la cabecera se quedaría en D-2 y la rejilla en D-1 --y
     no por cinco segundos, sino para siempre--: alguien marcaría la asistencia de un paciente
     mirando el nombre de otro día. Por eso cada respuesta comprueba que su día siga siendo el
     pedido, y si no, se tira entera: ni datos, ni error, ni apagar el «cargando» que la
     petición buena todavía necesita encendido. */
  const recargar = useCallback(async (queDia: string) => {
    pedido.current = queDia
    setCargando(true)
    try {
      const datosDelDia = await leerAgenda(queDia)
      if (pedido.current !== queDia) return
      setDatos(datosDelDia)
      setError('')
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else if (pedido.current === queDia) setError(mensajeDe(e))
    } finally {
      if (pedido.current === queDia) setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar(dia)
  }, [dia, recargar])

  // El reloj de la pantalla. Sin esto, una cita que empieza mientras alguien tiene la agenda
  // abierta no estrenaría su botón hasta que alguien recargara la página.
  useEffect(() => {
    const t = setInterval(() => setAhora(Date.now()), 60_000)
    return () => clearInterval(t)
  }, [])

  async function marcar(id: string, valor: boolean | null) {
    setOcupadas((prev) => [...prev, id])
    setAviso('')
    try {
      const actualizada = await marcarAsistencia(id, valor)
      setDatos((prev) =>
        prev === null
          ? prev
          : {
              ...prev,
              citas: prev.citas.map((c) => (c.id === id ? { ...c, ...actualizada } : c)),
              // Una cita que acaba de quedar marcada deja de estar pendiente; si se desmarcó,
              // se queda donde está.
              sin_marcar:
                actualizada.asistio === null
                  ? prev.sin_marcar
                  : prev.sin_marcar.filter((c) => c.id !== id),
            },
      )
    } catch (e) {
      if (e instanceof SesionCaducada) caducar.current()
      else setAviso(mensajeDe(e))
    } finally {
      setOcupadas((prev) => prev.filter((x) => x !== id))
    }
  }

  const citas = datos?.citas ?? []
  const bloqueos = datos?.bloqueos ?? []
  const pasada = (iso: string) => instante(iso) <= ahora

  const sinMarcarHoy = citas.filter((c) => pasada(c.inicio) && c.asistio === null).length
  const resumen = datos
    ? [
        { etiqueta: 'Agendadas', valor: String(citas.length), color: '#111827' },
        { etiqueta: 'Asistieron', valor: String(citas.filter((c) => c.asistio === true).length), color: '#10B981' },
        { etiqueta: 'No asistieron', valor: String(citas.filter((c) => c.asistio === false).length), color: '#EF4444' },
        { etiqueta: 'Sin marcar', valor: String(sinMarcarHoy), color: sinMarcarHoy > 0 ? '#D97706' : '#10B981' },
      ]
    : null

  // La rejilla cubre la jornada, y se estira si hay algo fuera de ella: una cita a las 7:00 o
  // un bloqueo a las 20:00 tienen que verse, no esconderse.
  const arranques = [...citas.map((c) => minutosDelDia(c.inicio)), ...bloqueos.map((b) => minutosDelDia(b.inicio))]
  const finales = [
    ...citas.map((c) => minutosDelDia(c.inicio) + c.duracion_minutos),
    ...bloqueos.map((b) => {
      const i = minutosDelDia(b.inicio)
      const f = minutosDelDia(b.fin)
      return f > i ? f : 24 * 60
    }),
  ]
  const desde = Math.floor(Math.min(8 * 60, ...arranques) / 60) * 60
  const hasta = Math.ceil(Math.max(19 * 60, ...finales) / 60) * 60
  const horas = Array.from({ length: Math.max(1, (hasta - desde) / 60) }, (_, i) => desde + i * 60)

  // La línea del ahora y el sombreado de las horas pasadas solo tienen sentido en el día que
  // de verdad está corriendo: en ayer o en mañana, `-1` las apaga las dos.
  const isoAhora = new Date(ahora).toISOString()
  const minutoAhora = dia === diaDe(isoAhora) ? minutosDelDia(isoAhora) : -1

  const marco = { fontFamily: SP, backgroundColor: '#F9FAFB' }

  /* Mientras carga NO se enseña el día anterior, ni siquiera atenuado, y no es una cuestión de
     pulcritud: la cabecera ya dice el día nuevo, la reconciliación contra Google tarda de 3 a
     5 segundos, y en esos segundos alguien puede pulsar el botón verde de una cita de HOY
     creyendo que está cerrando lo de AYER. Un contenido viejo bajo un rótulo nuevo es lo único
     que esta pantalla no se puede permitir. Se reemplaza entero, como hacen `Tratamientos` y
     `SinResolver`; la cabecera se queda para que se pueda seguir navegando. */
  if (cargando) {
    return (
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={marco}>
        <Cabecera dia={dia} irA={setDia} resumen={null} />
        <p className="px-8 py-8 text-sm" style={{ color: '#9CA3AF' }}>
          Cargando la agenda y contrastándola con Google Calendar…
        </p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={marco}>
        <Cabecera dia={dia} irA={setDia} resumen={null} />
        <div className="px-8 py-8">
          <div role="alert" className="rounded-xl px-4 py-3 max-w-2xl" style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}>
            <p className="text-sm" style={{ color: '#B91C1C' }}>{error}</p>
            <button
              onClick={() => void recargar(dia)}
              className="mt-2 text-xs font-semibold underline"
              style={{ color: '#B91C1C' }}
            >
              Reintentar
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={marco}>
      <Cabecera dia={dia} irA={setDia} resumen={resumen} />

      {datos?.calendario_disponible === false && (
        <div className="shrink-0 flex items-center gap-3 px-8 py-2.5 border-b" style={{ backgroundColor: '#FEF2F2', borderColor: '#FECACA' }}>
          <span className="text-[10px] font-bold px-2 py-1 rounded uppercase tracking-wide shrink-0" style={{ backgroundColor: '#DC2626', color: '#FFFFFF' }}>
            Sin calendario
          </span>
          <p className="text-xs" style={{ color: '#991B1B' }}>
            No se pudo consultar Google Calendar. Esto es lo que hay en la base: si el doctor
            movió algo en su calendario, aquí todavía no se ve. Marcar asistencia sigue
            funcionando.
          </p>
        </div>
      )}

      {aviso && (
        <div role="alert" className="shrink-0 flex items-center gap-3 px-8 py-2.5 border-b" style={{ backgroundColor: '#FEF2F2', borderColor: '#FECACA' }}>
          <p className="text-xs flex-1" style={{ color: '#991B1B' }}>{aviso}</p>
          <button className="text-sm shrink-0" style={{ color: '#9CA3AF' }} onClick={() => setAviso('')} aria-label="Ocultar aviso">✕</button>
        </div>
      )}

      {datos && datos.correcciones.length > 0 && (
        <div className="shrink-0 px-8 py-3 border-b" style={{ backgroundColor: '#F5F3FF', borderColor: '#DDD6FE' }}>
          <p className="text-xs font-bold uppercase tracking-wide mb-1" style={{ color: '#5B21B6' }}>
            Cambios que venían de Google Calendar
          </p>
          <ul className="flex flex-col gap-0.5">
            {datos.correcciones.map((c) => (
              <li key={`${c.cita_id}-${c.hora_vieja}`} className="text-sm" style={{ color: '#4C1D95' }}>
                <strong>{c.nombre_completo}</strong> ({c.tratamiento}) {frase(c)}.
              </li>
            ))}
          </ul>
        </div>
      )}

      {datos && datos.sin_marcar.length > 0 && (
        <div className="shrink-0 px-8 py-3 border-b" style={{ backgroundColor: '#FFFBEB', borderColor: '#FCD34D' }}>
          <div className="flex items-center gap-4">
            <span className="text-lg">⚠️</span>
            <p className="text-sm font-semibold flex-1" style={{ color: '#92400E' }}>
              {datos.sin_marcar.length}{' '}
              {datos.sin_marcar.length === 1 ? 'cita de días anteriores sin marcar' : 'citas de días anteriores sin marcar'}
            </p>
            <button
              onClick={() => setVerSinMarcar((v) => !v)}
              className="text-xs font-semibold underline shrink-0"
              style={{ color: '#D97706' }}
            >
              {verSinMarcar ? 'Ocultar' : 'Marcarlas ahora'}
            </button>
          </div>

          {verSinMarcar && (
            <ul className="mt-3 flex flex-col gap-2">
              {datos.sin_marcar.map((c) => (
                <li
                  key={c.id}
                  className="flex items-center gap-3 rounded-lg px-3 py-2"
                  style={{ backgroundColor: '#FFFFFF', border: '1px solid #FDE68A', opacity: ocupadas.includes(c.id) ? 0.6 : 1 }}
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-semibold truncate" style={{ color: '#111827' }}>{c.nombre_completo}</p>
                    <p className="text-xs truncate" style={{ color: '#6B7280' }}>
                      {c.tratamiento} · {fechaCorta(c.inicio)} a las {hhmm(c.inicio)}
                    </p>
                  </div>
                  <button
                    onClick={() => setDia(diaDe(c.inicio))}
                    className="text-xs shrink-0 hover:underline"
                    style={{ color: '#9CA3AF' }}
                  >
                    Ver ese día
                  </button>
                  <button
                    onClick={() => marcar(c.id, true)}
                    disabled={ocupadas.includes(c.id)}
                    className="px-4 py-2 rounded-lg text-sm font-bold shrink-0 hover:brightness-95 disabled:cursor-wait"
                    style={{ backgroundColor: '#10B981', color: '#FFFFFF' }}
                  >
                    ✓ Asistió
                  </button>
                  <button
                    onClick={() => marcar(c.id, false)}
                    disabled={ocupadas.includes(c.id)}
                    className="px-4 py-2 rounded-lg text-sm font-bold shrink-0 hover:brightness-95 disabled:cursor-wait"
                    style={{ backgroundColor: '#FEF2F2', color: '#DC2626', border: '1.5px solid #FECACA' }}
                  >
                    ✗ No asistió
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {citas.length === 0 && bloqueos.length === 0 && (
          <p className="px-8 pt-6 text-sm" style={{ color: '#9CA3AF' }}>
            Nada agendado este día.
          </p>
        )}

        {/* La hora y sus tarjetas van en la MISMA fila, no en dos columnas paralelas, y la fila
            crece si lo que lleva dentro no cabe (`minHeight`, nunca `height`).

            Las dos cosas son la misma corrección, verificada en pantalla el 20/09/2026 y no
            deducida del código. Con dos columnas de altura fija de 96 px, una tarjeta de una
            cita de 60 minutos --la duración POR DEFECTO de esta clínica-- mide 137 px con sus
            dos botones, se sale de su hora y **la tarjeta de la hora siguiente se pinta
            encima**. No es un defecto cosmético: los botones «Asistió» y «No asistió» de toda
            cita seguida de otra quedan FÍSICAMENTE tapados y no se pueden pulsar. Se midió con
            un navegador de verdad: el clic se reintentó treinta segundos contra
            «subtree intercepts pointer events» y nunca llegó. Con citas consecutivas --la
            agenda normal de una clínica-- solo la última de la tanda era marcable, que es
            justo la acción que esta fase entera existe para permitir.

            Encogerlos no era opción: el tamaño de esos dos botones es deliberado (lo pulsa
            alguien de pie, con prisa), así que lo que cede es la rejilla. Lo que se pierde es
            la proporcionalidad de la columna del tiempo, que esta pantalla ya había renunciado
            a tener --lo dice el rango horario de cada tarjeta, que es un dato y no una
            proporción--. Lo que se gana es que la fila y su rótulo no se puedan desalinear:
            ahora son el mismo elemento. */}
        <div className="py-2">
          {horas.map((h) => {
            const citasDeLaHora = citas.filter((c) => {
              const m = minutosDelDia(c.inicio)
              return m >= h && m < h + 60
            })
            const bloqueosDeLaHora = bloqueos.filter((b) => {
              const m = minutosDelDia(b.inicio)
              return m >= h && m < h + 60
            })
            const vacia = citasDeLaHora.length === 0 && bloqueosDeLaHora.length === 0
            const horaPasada = minutoAhora >= 0 && h + 60 <= minutoAhora
            const esLaHoraDeAhora = minutoAhora >= h && minutoAhora < h + 60

            return (
              <div
                key={h}
                className="relative flex"
                style={{ minHeight: ALTO_HORA, borderTop: '1px solid #F3F4F6', backgroundColor: horaPasada ? 'rgba(0,0,0,0.01)' : 'transparent' }}
              >
                {/* La línea del ahora vive DENTRO de su hora y se sitúa en un porcentaje de
                    ella. Antes se calculaba desde arriba de la rejilla multiplicando por
                    ALTO_HORA, lo que daba por hecho que todas las filas miden lo mismo: en
                    cuanto una crece, esa cuenta apunta a la hora equivocada. */}
                {esLaHoraDeAhora && (
                  <div
                    className="absolute left-16 right-8 flex items-center gap-2 z-10 pointer-events-none"
                    style={{ top: `${((minutoAhora - h) / 60) * 100}%` }}
                  >
                    <div className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: '#7C3AED', marginLeft: -5 }} />
                    <div className="flex-1 h-0.5 rounded-full" style={{ backgroundColor: '#7C3AED', opacity: 0.5 }} />
                    <span className="text-[10px] font-bold shrink-0 px-1.5 py-0.5 rounded" style={{ backgroundColor: '#F5F3FF', color: '#7C3AED' }}>
                      {String(Math.floor(minutoAhora / 60)).padStart(2, '0')}:{String(minutoAhora % 60).padStart(2, '0')}
                    </span>
                  </div>
                )}

                <div className="shrink-0 w-16 flex items-start justify-end pr-3">
                  <span className="text-xs font-medium" style={{ color: minutoAhora >= 0 && h < minutoAhora ? '#D1D5DB' : '#9CA3AF', marginTop: -8 }}>
                    {String(Math.floor(h / 60)).padStart(2, '0')}:{String(h % 60).padStart(2, '0')}
                  </span>
                </div>

                <div className="flex-1 min-w-0 pl-3 pr-8">
                  {!vacia && (
                    <div className="flex gap-3 w-full pt-3 pb-2">
                      {bloqueosDeLaHora.map((b) => (
                        <div key={`b-${b.inicio}-${b.titulo}`} className="flex-1 min-w-0">
                          <TarjetaBloqueo bloqueo={b} />
                        </div>
                      ))}
                      {citasDeLaHora.map((c) => (
                        <div key={c.id} className="flex-1 min-w-0">
                          <TarjetaCita
                            cita={c}
                            pasada={pasada(c.inicio)}
                            ocupada={ocupadas.includes(c.id)}
                            marcar={marcar}
                          />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

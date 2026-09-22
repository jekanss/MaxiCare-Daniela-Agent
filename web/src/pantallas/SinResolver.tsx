import { useCallback, useEffect, useRef, useState } from 'react'
import { listarSinResolver, SesionCaducada, type CasoSinResolver } from '@/api'

const SP = "'Space Grotesk', sans-serif"

type Props = { alCaducarSesion: () => void }

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

/* La misma cabecera con fondo blanco separado que usan `Tratamientos.tsx` y `Pendiente.tsx`,
 * repetida en los tres estados (cargando, error, contenido) para que la pantalla no pierda su
 * identidad mientras carga -- nunca un remplazo de la página entera por un mensaje suelto. */
function Cabecera() {
  return (
    <header className="px-8 py-4 border-b" style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}>
      <h1 className="text-lg font-bold" style={{ color: '#111827' }}>
        Sin resolver
      </h1>
      <p className="text-sm" style={{ color: '#6B7280' }}>
        Últimos 30 días, lo que más está pasando primero. Esta pantalla solo informa.
      </p>
    </header>
  )
}

export default function SinResolver({ alCaducarSesion }: Props) {
  const [casos, setCasos] = useState<CasoSinResolver[]>([])
  const [esAdmin, setEsAdmin] = useState(false)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState('')

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

  if (cargando) {
    return (
      <div className="flex-1 overflow-y-auto" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
        <Cabecera />
        <p className="px-8 py-8 text-sm" style={{ color: '#9CA3AF' }}>
          Cargando…
        </p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex-1 overflow-y-auto" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
        <Cabecera />
        <div className="max-w-3xl mx-auto px-8 py-8">
          <div role="alert" className="rounded-xl px-4 py-3" style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}>
            <p className="text-sm" style={{ color: '#B91C1C' }}>
              {error}
            </p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
      <Cabecera />

      <div className="max-w-3xl mx-auto px-8 py-8">
        {casos.length === 0 && (
          <p
            className="rounded-2xl p-8 text-center text-sm"
            style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB', color: '#6B7280' }}
          >
            Nada sin resolver en los últimos 30 días. Buena señal.
          </p>
        )}

        <ul className="flex flex-col gap-4">
          {casos.map((caso) => (
            <li
              key={caso.huella}
              className="rounded-2xl overflow-hidden"
              style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}
            >
              <header
                className="px-5 py-3 flex items-baseline justify-between gap-4 flex-wrap"
                style={{ borderBottom: '1px solid #F3F4F6' }}
              >
                <h2 className="text-sm font-semibold" style={{ color: '#111827' }}>
                  {titulo(caso)}
                </h2>
                <span className="text-xs whitespace-nowrap" style={{ color: '#9CA3AF' }}>
                  {caso.contador} {caso.contador === 1 ? 'vez' : 'veces'} ·{' '}
                  {fecha(caso.primera_vez)} al {fecha(caso.ultima_vez)}
                </span>
              </header>

              <div className="px-5 py-4 flex flex-col gap-3 text-sm" style={{ color: '#374151' }}>
                {caso.informe ? (
                  <>
                    <p>
                      <strong
                        className="block text-[10px] font-semibold uppercase tracking-wide"
                        style={{ color: '#9CA3AF' }}
                      >
                        Qué pasó
                      </strong>
                      {caso.informe.que_paso}
                    </p>
                    <p>
                      <strong
                        className="block text-[10px] font-semibold uppercase tracking-wide"
                        style={{ color: '#9CA3AF' }}
                      >
                        Por qué
                      </strong>
                      {caso.informe.por_que}
                    </p>
                    <p>
                      <strong
                        className="block text-[10px] font-semibold uppercase tracking-wide"
                        style={{ color: '#9CA3AF' }}
                      >
                        Recomiendo
                      </strong>
                      {caso.informe.recomiendo}
                    </p>
                  </>
                ) : (
                  // Un analista caído no puede dejar la pantalla vacía: el caso se muestra
                  // igual, con su contador y sus ejemplos, que ya dicen bastante.
                  <p className="text-sm" style={{ color: '#6B7280' }}>
                    El análisis todavía no está listo.
                  </p>
                )}

                {caso.escalo > 0 && (
                  <p className="text-xs" style={{ color: '#6B7280' }}>
                    Se interrumpió al doctor {caso.escalo} de {caso.contador} veces.
                  </p>
                )}

                {caso.ejemplos.length > 0 && (
                  <details>
                    <summary className="text-xs cursor-pointer" style={{ color: '#374151' }}>
                      Las preguntas tal como llegaron ({caso.ejemplos.length})
                    </summary>
                    <ul className="mt-2 flex flex-col gap-1 pl-4 list-disc text-xs" style={{ color: '#6B7280' }}>
                      {caso.ejemplos.map((texto, i) => (
                        <li key={i}>«{texto}»</li>
                      ))}
                    </ul>
                  </details>
                )}

                {esAdmin && (
                  <details>
                    <summary className="text-xs cursor-pointer" style={{ color: '#9CA3AF' }}>
                      Detalle técnico
                    </summary>
                    <code className="block mt-2 text-[11px] break-all" style={{ color: '#9CA3AF' }}>
                      {caso.huella}
                    </code>
                  </details>
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

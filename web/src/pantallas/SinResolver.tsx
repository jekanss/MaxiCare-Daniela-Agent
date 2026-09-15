import { useCallback, useEffect, useRef, useState } from 'react'
import { listarSinResolver, SesionCaducada, type CasoSinResolver } from '../api'

type Props = { alCaducarSesion: () => void }

/** `falta_dato:ortodoncia:precio` -> «Falta el dato «precio» de ORTODONCIA».
 *
 * La huella cruda no se le ensena a la clinica: es un identificador, no una frase. El
 * detalle tecnico vive detras del `<details>` de admin. */
function titulo(caso: CasoSinResolver): string {
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

  if (cargando) return <p className="p-6">Cargando…</p>
  if (error) return <p className="p-6 text-red-700">{error}</p>

  return (
    <section className="p-6 max-w-3xl mx-auto">
      <h1 className="text-2xl font-semibold">Sin resolver</h1>
      <p className="text-sm opacity-70 mt-1">
        Últimos 30 días, lo que más está pasando primero. Esta pantalla solo informa.
      </p>

      {casos.length === 0 && (
        <p className="mt-8 p-6 rounded-lg border text-center">
          Nada sin resolver en los últimos 30 días. Buena señal.
        </p>
      )}

      <ul className="mt-6 space-y-4">
        {casos.map((caso) => (
          <li key={caso.huella} className="rounded-lg border overflow-hidden">
            <header className="p-4 border-b flex items-baseline justify-between gap-4 flex-wrap">
              <h2 className="font-medium">{titulo(caso)}</h2>
              <span className="text-sm whitespace-nowrap opacity-70">
                {caso.contador} {caso.contador === 1 ? 'vez' : 'veces'} ·{' '}
                {fecha(caso.primera_vez)} al {fecha(caso.ultima_vez)}
              </span>
            </header>

            <div className="p-4 space-y-3 text-sm">
              {caso.informe ? (
                <>
                  <p>
                    <strong className="block text-xs uppercase opacity-60">Qué pasó</strong>
                    {caso.informe.que_paso}
                  </p>
                  <p>
                    <strong className="block text-xs uppercase opacity-60">Por qué</strong>
                    {caso.informe.por_que}
                  </p>
                  <p>
                    <strong className="block text-xs uppercase opacity-60">Recomiendo</strong>
                    {caso.informe.recomiendo}
                  </p>
                </>
              ) : (
                // Un analista caído no puede dejar la pantalla vacía: el caso se muestra
                // igual, con su contador y sus ejemplos, que ya dicen bastante.
                <p className="opacity-70">El análisis todavía no está listo.</p>
              )}

              {caso.escalo > 0 && (
                <p className="opacity-70">
                  Se interrumpió al doctor {caso.escalo} de {caso.contador} veces.
                </p>
              )}

              {caso.ejemplos.length > 0 && (
                <details>
                  <summary className="cursor-pointer">
                    Las preguntas tal como llegaron ({caso.ejemplos.length})
                  </summary>
                  <ul className="mt-2 space-y-1 pl-4 list-disc opacity-80">
                    {caso.ejemplos.map((texto, i) => (
                      <li key={i}>«{texto}»</li>
                    ))}
                  </ul>
                </details>
              )}

              {esAdmin && (
                <details>
                  <summary className="cursor-pointer opacity-60">Detalle técnico</summary>
                  <code className="block mt-2 text-xs break-all opacity-80">{caso.huella}</code>
                </details>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

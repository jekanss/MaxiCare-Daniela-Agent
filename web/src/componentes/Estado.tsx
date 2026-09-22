/* Lo que el panel pinta mientras los datos llegan, y lo que pinta cuando no llegan.
 *
 * Existía siete veces, una por pantalla: cinco copias del literal «Cargando…», cinco del
 * bloque rojo de error, y el botón de reintentar en una sola de ellas. La consecuencia no
 * era estética: quien abría Sin Resolver y perdía la conexión se quedaba mirando un texto
 * gris sin nada que pulsar, y su única salida era recargar la página a mano.
 *
 * Lo que este archivo NO toca, a propósito: cómo cada pantalla pide sus datos. Esa parte
 * tiene una trampa documentada en `web/CLAUDE.md` --`alCaducarSesion` va por `ref` y el
 * `useCallback` con deps vacías-- y en Agenda una relectura de más cuesta una llamada a
 * Google Calendar que además escribe. Aquí solo vive lo que se dibuja.
 *
 * Los colores van en hexadecimales literales, como en el resto del panel: vienen del
 * archivo de marca y un token renombrado deja de corresponder con el diseño. */

const SG = "'Space Grotesk', sans-serif"

/** El gris de un hueco que se está llenando. Más claro que el borde (`#DCD8E6`) para que
 *  un esqueleto de varias piezas no se lea como una tabla de verdad ya cargada. */
const HUECO = '#E9E7EF'

/** Un rectángulo gris con la forma de lo que va a aparecer ahí.
 *
 *  Es un esqueleto y no un spinner porque los dos responden preguntas distintas: el spinner
 *  dice «espera» y el esqueleto dice «esto es lo que va a haber», que además evita el salto
 *  de la página cuando los datos por fin llegan.
 *
 *  El latido lo apaga entero el `prefers-reduced-motion` global de `index.css`, y eso está
 *  bien: un rectángulo gris quieto sigue diciendo lo mismo. */
export function Bloque({
  alto = 12,
  ancho = '100%',
  radio = 6,
  retraso = 0,
  estilo,
}: {
  alto?: number | string
  ancho?: number | string
  radio?: number
  /** Milisegundos de desfase, para que varias piezas no latan a la vez como un bloque. */
  retraso?: number
  estilo?: React.CSSProperties
}) {
  return (
    <div
      aria-hidden
      style={{
        height: alto,
        width: ancho,
        borderRadius: radio,
        backgroundColor: HUECO,
        animation: `mcLatido 1.4s ease-in-out ${retraso}ms infinite`,
        flexShrink: 0,
        ...estilo,
      }}
    />
  )
}

/** Varias líneas de texto fingido, con la última más corta como la última línea de un
 *  párrafo de verdad. */
export function Lineas({
  cuantas = 3,
  alto = 12,
  separacion = 8,
  retraso = 0,
}: {
  cuantas?: number
  alto?: number
  separacion?: number
  retraso?: number
}) {
  return (
    <div className="flex flex-col w-full" style={{ gap: separacion }}>
      {Array.from({ length: cuantas }, (_, i) => (
        <Bloque
          key={i}
          alto={alto}
          ancho={i === cuantas - 1 ? '62%' : '100%'}
          retraso={retraso + i * 90}
        />
      ))}
    </div>
  )
}

/** El envoltorio que anuncia a un lector de pantalla que algo está cargando.
 *
 *  Los `Bloque` van con `aria-hidden` porque un rectángulo gris no tiene nada que leerle a
 *  nadie; lo que se anuncia es esta región, una vez y con palabras. */
export function Cargando({
  que = 'Cargando…',
  children,
  className = '',
  estilo,
}: {
  /** Qué se está cargando, para quien no ve la pantalla: «Cargando la agenda…». */
  que?: string
  children: React.ReactNode
  className?: string
  estilo?: React.CSSProperties
}) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className={className} style={estilo}>
      <span className="sr-only">{que}</span>
      {children}
    </div>
  )
}

/** Lo que se pinta cuando la petición no llegó.
 *
 *  El botón no es un adorno: sin él la única salida es recargar la página entera, que en
 *  Conversaciones significa perder el hilo que estabas leyendo. Se omite solo cuando quien
 *  llama no tiene nada que reintentar. */
export function Fallo({
  mensaje,
  alReintentar,
  className = '',
  estilo,
}: {
  mensaje: string
  alReintentar?: () => void
  className?: string
  estilo?: React.CSSProperties
}) {
  return (
    <div
      role="alert"
      className={`rounded-xl px-4 py-3 flex items-center justify-between gap-4 ${className}`}
      style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA', fontFamily: SG, ...estilo }}
    >
      <p className="text-sm" style={{ color: '#B91C1C' }}>
        {mensaje}
      </p>
      {alReintentar && (
        <button
          type="button"
          onClick={alReintentar}
          className="px-3 py-1.5 text-sm font-bold rounded-lg transition-all hover:brightness-95 shrink-0"
          style={{ backgroundColor: '#B91C1C', color: '#FFFFFF', fontFamily: SG }}
        >
          Reintentar
        </button>
      )}
    </div>
  )
}

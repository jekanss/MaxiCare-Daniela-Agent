/* Lo que el panel pinta mientras los datos llegan, y lo que pinta cuando no llegan.
 *
 * Existía siete veces, una por pantalla: cinco copias del literal «Cargando…», cinco del
 * bloque rojo de error, y el botón de reintentar en una sola de ellas. La consecuencia no
 * era estética: quien abría Sin Resolver y perdía la conexión se quedaba mirando un texto
 * gris sin nada que pulsar, y su única salida era recargar la página a mano.
 *
 * AQUÍ HUBO ESQUELETOS Y YA NO LOS HAY. `Bloque`, `Lineas` y `Cargando` pintaban la forma
 * de lo que estaba por llegar --rectángulos grises con la silueta de cada pantalla-- y la
 * frase que los acompañaba iba en un `sr-only`, o sea invisible. La teoría era buena: un
 * esqueleto evita el salto de la página cuando los datos aterrizan. La práctica la contestó
 * MaxiCare el 22/09/2026 --«voy a inicio y no me sale nada»-- y tenía razón: `/api/inicio`
 * tarda 1,34 s medidos, y más de un segundo de rectángulos grises sin una palabra no se lee
 * como «esto viene» sino como «esto está roto». Lo pedido, y lo que hay ahora, es un loader
 * que TAPA la pantalla hasta que los datos están. El salto se paga a cambio, y se paga una
 * vez por carga; la duda de si el panel funciona se pagaba cada vez.
 *
 * Lo que este archivo NO toca, a propósito: cómo cada pantalla pide sus datos. Esa parte
 * tiene una trampa documentada en `web/CLAUDE.md` --`alCaducarSesion` va por `ref` y el
 * `useCallback` con deps vacías-- y en Agenda una relectura de más cuesta una llamada a
 * Google Calendar que además escribe. Aquí solo vive lo que se dibuja.
 *
 * Los colores van en hexadecimales literales, como en el resto del panel: vienen del
 * archivo de marca y un token renombrado deja de corresponder con el diseño. */

const SG = "'Space Grotesk', sans-serif"

/* ------------------------------------------------------------------ El loader de pantalla */

/** Las dos pieles del loader. Son los mismos colores del panel y del ingreso, no una paleta
 *  nueva: el loader oscuro sale sobre el fondo del ingreso, y el claro sobre el del panel. */
const PIEL = {
  claro: {
    fondo: '#F7F6FA',
    titulo: '#16111F',
    detalle: '#6E6880',
    pista: '#E4DFF0',
    acento: '#7C3AED',
  },
  oscuro: {
    fondo: '#07060B',
    titulo: '#FFFFFF',
    detalle: '#9A93AD',
    pista: '#241C33',
    acento: '#C4B5FD',
  },
} as const

/** El anillo que gira, con su halo y su punto. Tres piezas y no una porque una sola gira o
 *  late, y aquí hacen falta las dos cosas: el giro dice «sigue trabajando» y el latido le da
 *  el pulso que tiene el resto del panel. */
function Anillo({ piel }: { piel: (typeof PIEL)[keyof typeof PIEL] }) {
  return (
    <div
      aria-hidden
      style={{ position: 'relative', width: 54, height: 54, display: 'grid', placeItems: 'center' }}
    >
      <div
        style={{
          position: 'absolute',
          inset: 0,
          borderRadius: '50%',
          backgroundColor: piel.acento,
          opacity: 0.12,
          filter: 'blur(13px)',
          animation: 'mcLatido 1.8s ease-in-out infinite',
        }}
      />
      <div
        style={{
          position: 'absolute',
          inset: 3,
          borderRadius: '50%',
          border: `2.5px solid ${piel.pista}`,
          borderTopColor: piel.acento,
          animation: 'mcGiro 0.9s linear infinite',
        }}
      />
      <div
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          backgroundColor: piel.acento,
          animation: 'mcLatido 1.4s ease-in-out infinite',
        }}
      />
    </div>
  )
}

/** El loader que TAPA lo que hay detrás hasta que los datos llegan.
 *
 *  Es lo que MaxiCare pidió el 22/09/2026 después de ver los esqueletos: «un loader que
 *  tapara la pantalla, bien elegante, en la mitad, y hasta que no tiene los datos no muestra
 *  la pantalla». Sustituye al esqueleto en las cinco pantallas y en el hilo de una
 *  conversación, y es deliberado renunciar a lo que el esqueleto daba --la forma de lo que
 *  viene, y por tanto ningún salto al llegar--: media pantalla de rectángulos grises se lee
 *  como una página vacía, y eso costaba más que el salto.
 *
 *  Ocupa lo que le den: en una pantalla es el hermano `flex-1` de la barra lateral y llena
 *  el alto entero; dentro del hilo de Conversaciones llena solo ese panel. Por eso NO se
 *  posiciona `fixed` --taparía la barra lateral, y quien espera la agenda tiene que poder
 *  irse a otra sección-- sino que rellena a su contenedor.
 *
 *  `detalle` es para lo que explica una espera larga: la agenda tarda hasta cinco segundos
 *  porque está hablando con Google, y decirlo evita que parezca colgado. */
export function CargandoPantalla({
  que = 'Cargando…',
  detalle,
  oscuro = false,
  fondo,
  className = '',
}: {
  que?: string
  detalle?: string
  /** Sobre el fondo del ingreso, no el del panel. */
  oscuro?: boolean
  /** El panel no tiene UN blanco roto sino dos --`#F7F6FA` en la portada, `#F9FAFB` en las
   *  demás-- y un loader que trae el que no es deja una costura visible contra la cabecera
   *  que se queda encima. Cada pantalla pasa el suyo. */
  fondo?: string
  className?: string
}) {
  const piel = oscuro ? PIEL.oscuro : PIEL.claro
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className={`flex flex-1 min-h-0 min-w-0 flex-col items-center justify-center ${className}`}
      style={{ backgroundColor: fondo ?? piel.fondo, fontFamily: SG, gap: 20, padding: 28 }}
    >
      <Anillo piel={piel} />
      <div className="flex flex-col items-center" style={{ gap: 7, textAlign: 'center' }}>
        <p
          style={{
            margin: 0,
            fontWeight: 600,
            fontSize: '15.5px',
            letterSpacing: '-0.022em',
            color: piel.titulo,
          }}
        >
          {que}
        </p>
        {detalle ? (
          <p style={{ margin: 0, fontSize: '12.5px', lineHeight: 1.5, color: piel.detalle }}>
            {detalle}
          </p>
        ) : null}
      </div>
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

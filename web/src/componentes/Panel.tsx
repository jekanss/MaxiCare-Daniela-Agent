/* Las piezas del layout de dos paneles: lista a la izquierda, detalle a la derecha.
 *
 * Nacieron dentro de `pantallas/Conversaciones.tsx`, que fue la primera pantalla con esta
 * forma. Salieron de ahí el 22/09/2026, cuando MaxiCare pidió que «Sin resolver» siguiera el
 * mismo patrón: con las dos pantallas pintándolo por su cuenta, los dos paneles se habrían
 * separado en silencio --un borde, un `flex`, un gris-- y la aplicación habría acabado con
 * dos maneras de decir lo mismo. Es el mismo criterio por el que `SinResolver.titulo()` se
 * exporta en vez de duplicarse en la portada.
 *
 * Lo que este archivo NO decide: qué va dentro de cada panel. Solo la caja, el espaciado y
 * las tres piezas pequeñas que las dos pantallas repetían igual.
 *
 * Los colores van en hexadecimales literales, como en el resto del panel: vienen del archivo
 * de marca y un token renombrado deja de corresponder con el diseño. */

export const SG = "'Space Grotesk', sans-serif"
export const MONO = "'JetBrains Mono', monospace"

/** El marco de la pantalla entera: el fondo y la separación entre los dos paneles.
 *
 *  ------------------------------------------------------------------------------------
 *  Por qué NO hay `flex-wrap`, que es lo que había
 *  ------------------------------------------------------------------------------------
 *
 *  Lo tuvo hasta el 23/09/2026, con este comentario: «por debajo de unos 700 px los dos
 *  paneles se apilan solos, sin un media query que haya que mantener en dos sitios». Se
 *  apilaban, sí, **y el segundo desaparecía**: este contenedor es `h-full overflow-hidden` y
 *  cada `Panel` lleva `maxHeight: 100%`, así que dos filas piden el 200 % de una caja que no
 *  hace scroll. En un móvil, «Conversaciones» enseñaba la lista y ni rastro del hilo -- no
 *  cortado por abajo, invisible y sin barra que bajara.
 *
 *  Envolver eso en un `overflow-y-auto` lo habría hecho visible y seguiría siendo malo: una
 *  lista de 320 px de alto encima de un hilo de chat, en una pantalla de 667 px. Lo que se
 *  hace es lo que hace cualquier aplicación de mensajería -- lista O detalle, y un botón para
 *  volver--, y eso no lo puede decidir este archivo porque no sabe qué hay seleccionado. Lo
 *  deciden las dos pantallas con `usarEsAngosto(ANCHO_DOS_PANELES)`, y aquí solo queda que
 *  UN hijo ocupe el ancho entero sin que nada se envuelva. */
export function MarcoDeDosPaneles({ children, etiqueta }: {
  children: React.ReactNode
  /** Para el lector de pantalla: «Conversaciones», «Sin resolver». */
  etiqueta: string
}) {
  return (
    <main
      aria-label={etiqueta}
      className="min-w-0 flex-1 overflow-hidden"
      style={{ backgroundColor: '#F7F6FA' }}
    >
      <div className="flex h-full items-stretch gap-4 p-3 sm:p-4 lg:gap-5 lg:p-6">
        {children}
      </div>
    </main>
  )
}

/** «← Volver», la salida del detalle cuando la pantalla enseña un panel a la vez.
 *
 *  Sin esto, abrir una conversación en un móvil es un camino sin retorno: el detalle ocupa
 *  todo y la lista no está en ninguna parte. El botón «atrás» del navegador tampoco sirve --
 *  la selección no vive en el hash, y ponerla ahí obligaría a convertir el enrutado en un
 *  router de verdad por un solo salto (la decisión está en `App.tsx`). */
export function Volver({ alPulsar, que }: { alPulsar: () => void; que: string }) {
  return (
    <button
      type="button"
      onClick={alPulsar}
      className="flex shrink-0 items-center gap-1.5 lg:hidden"
      style={{
        backgroundColor: '#FFFFFF',
        border: '1px solid #DCD8E6',
        color: '#4A4458',
        fontFamily: MONO,
        fontSize: '10.5px',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        padding: '0 12px',
        // 40 px: se pulsa con el pulgar, como los `Chip` de la cabecera.
        minHeight: '40px',
        cursor: 'pointer',
      }}
    >
      <svg aria-hidden width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square">
        <path d="M14 6l-6 6 6 6" />
      </svg>
      {que}
    </button>
  )
}

/** Uno de los dos paneles.
 *
 *  `peso` es la base del `flex`: la lista va con `'1 1 320px'` y el detalle con `'2 1 380px'`,
 *  así que el detalle se lleva el doble del espacio sobrante y los dos se apilan cuando ya no
 *  caben. Esos dos números son los de Conversaciones y no se tocan por pantalla: dos listas
 *  con anchos distintos se leerían como dos aplicaciones. */
export function Panel({
  etiqueta,
  peso,
  children,
}: {
  etiqueta: string
  peso: '1 1 320px' | '2 1 380px'
  children: React.ReactNode
}) {
  return (
    <section
      aria-label={etiqueta}
      className="flex min-h-0 min-w-0 flex-col overflow-hidden"
      style={{
        flex: peso,
        maxWidth: '100%',
        maxHeight: '100%',
        backgroundColor: '#FFFFFF',
        border: '1px solid #DCD8E6',
      }}
    >
      {children}
    </section>
  )
}

/** La cabecera de un panel: el bloque con borde inferior donde van el título y los filtros. */
export function CabeceraDePanel({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex flex-col gap-3"
      style={{ padding: '18px 18px 14px', borderBottom: '1px solid #ECE8F4' }}
    >
      {children}
    </div>
  )
}

/** El título de un panel. */
export function TituloDePanel({ children }: { children: React.ReactNode }) {
  return (
    <h1
      style={{
        margin: 0,
        fontFamily: SG,
        fontWeight: 600,
        fontSize: 'clamp(17px, 1.6vw, 21px)',
        letterSpacing: '-0.024em',
        color: '#16111F',
      }}
    >
      {children}
    </h1>
  )
}

/** `573196842471` -> `+57 319 684 2471`. Un numero de diez digitos pegado no se lee ni se
 *  dicta por telefono, y el panel lo ensena en tres sitios. Lo que no tenga forma de movil
 *  colombiano sale con un `+` delante y sin tocar: inventarle una agrupacion a un numero
 *  internacional lo dejaria peor que crudo. */
export function telefonoLegible(tel: string): string {
  const m = /^57(\d{3})(\d{3})(\d{4})$/.exec(tel)
  return m ? `+57 ${m[1]} ${m[2]} ${m[3]}` : `+${tel}`
}

/** El contador en versalitas de la esquina: «14 HILOS», «6 DE 23». */
export function Rotulo({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: '10.5px',
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: '#6E6880',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  )
}

/** Una etiqueta de estado o de categoría. Los colores los pone quien la usa, porque el
 *  significado es suyo: «Esperando» es rojo en Conversaciones por la misma razón por la que
 *  «Falla técnica» lo es aquí, y eso no lo puede saber este archivo. */
export function Pastilla({
  texto,
  fondo,
  tinta,
}: {
  texto: string
  fondo: string
  tinta: string
}) {
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: '9.5px',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        backgroundColor: fondo,
        color: tinta,
        padding: '4px 8px',
        whiteSpace: 'nowrap',
      }}
    >
      {texto}
    </span>
  )
}

/** Uno de los botones de filtro de la cabecera. `minHeight` de 36 px y no menos: se pulsa
 *  con el dedo en la tablet de recepción. */
export function Chip({
  puesto,
  alPulsar,
  children,
}: {
  puesto: boolean
  alPulsar: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={alPulsar}
      aria-pressed={puesto}
      style={{
        backgroundColor: puesto ? '#6D28D9' : '#FFFFFF',
        color: puesto ? '#FFFFFF' : '#4A4458',
        border: `1px solid ${puesto ? '#6D28D9' : '#DCD8E6'}`,
        fontFamily: MONO,
        fontSize: '10.5px',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        padding: '9px 13px',
        minHeight: '36px',
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  )
}

/** Lo que ocupa un panel cuando no hay nada que enseñar: ni una lista vacía ni un hueco, una
 *  frase centrada que dice qué hacer. */
export function Vacio({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center px-6 py-10 text-center">
      <span
        className="text-sm"
        style={{ fontWeight: 300, lineHeight: 1.6, color: '#6E6880', maxWidth: '32ch' }}
      >
        {children}
      </span>
    </div>
  )
}

/** El buscador de la cabecera de una lista. */
export function Buscador({
  valor,
  alCambiar,
  marcador,
  etiqueta,
}: {
  valor: string
  alCambiar: (v: string) => void
  marcador: string
  etiqueta: string
}) {
  return (
    <label
      className="flex items-center gap-2"
      style={{ border: '1px solid #DCD8E6', padding: '0 12px', minHeight: '44px' }}
    >
      <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#6E6880" strokeWidth="1.8" strokeLinecap="square">
        <circle cx="11" cy="11" r="6.5" />
        <path d="M16 16l4.5 4.5" />
      </svg>
      <input
        type="search"
        value={valor}
        onChange={(e) => alCambiar(e.target.value)}
        placeholder={marcador}
        aria-label={etiqueta}
        className="min-w-0 flex-1 border-none bg-transparent outline-none"
        style={{ fontSize: '14px', color: '#16111F', padding: '10px 0' }}
      />
    </label>
  )
}

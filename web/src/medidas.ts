import { useEffect, useState } from 'react'

/* El único sitio del frontend que sabe a qué ancho cambia la forma del panel.
 *
 * Casi todo lo responsive se resuelve con clases (`max-md:`, `md:`) y no pasa por aquí: una
 * media query en CSS no re-renderiza nada y no puede desincronizarse. Esto existe solo para
 * los dos casos en los que hay que decidir QUÉ SE PINTA y no cómo se ve -- el cajón del menú
 * y la elección entre lista y detalle--, que son decisiones de React y no de CSS.
 *
 * Los dos números están aquí y NO repartidos por las pantallas: el día que uno cambie, la
 * clase de Tailwind y el hook tienen que moverse juntos, y con el número escrito en seis
 * sitios se mueve uno solo y la pantalla queda con el menú de móvil y el contenido de
 * escritorio a la vez. */

/** Por debajo de esto el menú es un cajón. Es `md` de Tailwind (48rem), así que las clases
 *  `max-md:` y `md:` del `Sidebar` y de `App` hablan exactamente de este mismo punto. */
export const ANCHO_MENU = 768

/** Por debajo de esto, las pantallas de dos paneles enseñan UNO solo.
 *
 *  No es `md` y la diferencia importa: los dos paneles miden `320px` y `380px` de base más
 *  el hueco y el margen, o sea unos 768 px justos. A 800 px «caben» --de ahí que hasta hoy
 *  nadie lo viera roto en el inspector-- y no se pueden usar: una lista de 320 px al lado de
 *  un hilo de conversación de 380 px son dos columnas estrechas en vez de una legible. Y una
 *  tableta en vertical (768 px) cae del lado bueno.
 *
 *  Es `lg` de Tailwind (64rem). */
export const ANCHO_DOS_PANELES = 1024

/** `true` mientras la ventana sea más angosta que `limite`. Se actualiza al girar el móvil.
 *
 *  El `- 0.02` imita a Tailwind: su `max-lg:` es `width < 64rem`, no `<= 64rem`. Sin eso, a
 *  exactamente 1024 px el hook y las clases dirían cosas distintas, que es el ancho de una
 *  tableta en horizontal -- justo uno de los que hay que probar. */
export function usarEsAngosto(limite: number): boolean {
  const consulta = `(max-width: ${limite - 0.02}px)`
  const [angosto, setAngosto] = useState(() => window.matchMedia(consulta).matches)

  useEffect(() => {
    const mq = window.matchMedia(consulta)
    const alCambiar = (e: MediaQueryListEvent) => setAngosto(e.matches)
    // Se vuelve a leer al suscribirse: entre el primer render y este efecto la ventana pudo
    // cambiar de tamaño, y el estado inicial se calculó en el primero.
    setAngosto(mq.matches)
    mq.addEventListener('change', alCambiar)
    return () => mq.removeEventListener('change', alCambiar)
  }, [consulta])

  return angosto
}

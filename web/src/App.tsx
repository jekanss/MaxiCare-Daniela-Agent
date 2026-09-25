import { useEffect, useState } from 'react'
import Sidebar, { CONFIGURACION, PRUEBAS, SECCIONES, type Seccion, type SeccionId } from '@/componentes/Sidebar'
import Ingreso from '@/pantallas/Ingreso'
import Conversaciones from '@/pantallas/Conversaciones'
import Inicio from '@/pantallas/Inicio'
import Agenda from '@/pantallas/Agenda'
import Pruebas from '@/pantallas/Pruebas'
import Tratamientos from '@/pantallas/Tratamientos'
import SinResolver from '@/pantallas/SinResolver'
import Leads from '@/pantallas/Leads'
import Configuracion from '@/pantallas/Configuracion'
import CambiarContrasena from '@/componentes/CambiarContrasena'
import { CargandoPantalla } from '@/componentes/Estado'
import { salir, sesionActual, type Sesion } from '@/api'

const TODAS: Seccion[] = [...SECCIONES, CONFIGURACION, PRUEBAS]

/* La navegación va por el hash de la URL, no solo por un `useState`.
 *
 * Es cuatro líneas más y compra tres cosas que en una herramienta que se usa varias horas al
 * día se notan: el botón «atrás» del navegador funciona, recargar no te devuelve al inicio, y
 * una pantalla se puede pasar por chat como enlace. No se trajo `react-router` porque son
 * nueve destinos planos sin parámetros: la dependencia costaría más de lo que resuelve. */
function seccionDelHash(): SeccionId {
  const id = window.location.hash.replace(/^#\/?/, '') as SeccionId
  return TODAS.some((s) => s.id === id) ? id : 'inicio'
}

export default function App() {
  const [sesion, setSesion] = useState<Sesion | null>(null)
  const [comprobando, setComprobando] = useState(true)
  const [activa, setActiva] = useState<SeccionId>(seccionDelHash)
  /* Un salto de «Sin resolver» a la conversacion donde paso el caso.
   *
   * Vive aqui y NO en el hash, a proposito: el hash lleva nueve destinos planos sin
   * parametros --la decision esta tres lineas mas arriba-- y meterle uno obligaria a
   * cambiar `seccionDelHash` y a volver el enrutado un router de verdad por un solo salto.
   * Lo que se pierde: este salto no se puede pegar como enlace. Lo que no se pierde: el
   * boton «atras» sigue funcionando, porque `ir` sigue escribiendo el hash.
   *
   * Se consume UNA vez. Sin limpiarlo, volver a Conversaciones desde el menu media hora
   * despues reabriria aquella conversacion sola, sin que nadie lo hubiera pedido. */
  const [saltarA, setSaltarA] = useState<string | null>(null)
  /* El cajón del menú, que solo existe por debajo de `ANCHO_MENU`. Arriba de ese ancho el
   * menú está siempre puesto y este estado no se mira. */
  const [menuAbierto, setMenuAbierto] = useState(false)
  /* El modal de cambiar la propia contraseña. Vive aquí y no dentro del `Sidebar` porque se
   * dibuja sobre TODO el panel --es `fixed inset-0`-- y porque puede toparse con un 401, que
   * en este archivo es una línea (`setSesion(null)`) y allí sería un callback más. */
  const [cambiandoClave, setCambiandoClave] = useState(false)

  // Al cargar se le pregunta al servidor si la cookie sigue valiendo. Sin esto, recargar la
  // página devolvería al login aunque la sesión estuviera viva -- la cookie es `HttpOnly` y
  // el JavaScript no puede leerla, que es justo lo que la protege de un XSS.
  useEffect(() => {
    sesionActual()
      .then(setSesion)
      .finally(() => setComprobando(false))
  }, [])

  useEffect(() => {
    const alCambiar = () => setActiva(seccionDelHash())
    window.addEventListener('hashchange', alCambiar)
    return () => window.removeEventListener('hashchange', alCambiar)
  }, [])

  function ir(id: SeccionId) {
    window.location.hash = `#/${id}`
    setActiva(id)
    // Navegar cierra el cajón. En escritorio no hay cajón y esto no hace nada; en un móvil,
    // no cerrarlo deja el menú tapando la pantalla a la que se acaba de ir.
    setMenuAbierto(false)
  }

  async function cerrarSesion() {
    await salir()
    setSesion(null)
  }

  if (comprobando) {
    // Esto decía «un instante en blanco, no un spinner: en una red local dura menos de lo
    // que tarda en verse». Era cierto en una red local y falso donde vive el panel: la
    // comprobación de sesión cruza internet hasta el VPS y de ahí a Neon, que está en otro
    // continente. Lo que MaxiCare veía al abrir el panel el 22/09/2026 era un rectángulo
    // negro sin una palabra, y después una pantalla de rectángulos grises también sin una
    // palabra (ver `componentes/Estado.tsx`). Para quien mira, eso no es «cargando»: es
    // «no funciona».
    return (
      <div className="flex" style={{ minHeight: '100dvh' }}>
        <CargandoPantalla oscuro que="MaxiCare · Daniela" detalle="Comprobando tu sesión…" />
      </div>
    )
  }

  if (!sesion) return <Ingreso alEntrar={setSesion} />

  const seccion = TODAS.find((s) => s.id === activa) ?? TODAS[0]

  return (
    // `h-dvh` y no `h-screen`: `100vh` en un movil es la altura CON la barra del navegador
    // escondida, asi que mientras se ve la barra el panel mide mas que el hueco visible y lo
    // que queda debajo --el campo para escribirle al paciente, que esta anclado abajo-- cae
    // fuera de la pantalla. `100dvh` sigue al hueco de verdad. No cambia nada en escritorio.
    <div className="flex h-dvh overflow-hidden" style={{ backgroundColor: '#F9FAFB' }}>
      <Sidebar
        activa={activa}
        ir={ir}
        sesion={sesion}
        alSalir={cerrarSesion}
        alCambiarClave={() => setCambiandoClave(true)}
        abierto={menuAbierto}
        alCerrar={() => setMenuAbierto(false)}
      />
      {/* El velo del cajón. `md:hidden` y solo cuando está abierto: en escritorio no existe.
          Tocar fuera cierra, que es lo que todo el mundo intenta primero. */}
      {menuAbierto ? (
        <div
          aria-hidden="true"
          onClick={() => setMenuAbierto(false)}
          className="fixed inset-0 z-40 md:hidden"
          style={{ backgroundColor: 'rgba(11, 9, 18, 0.5)' }}
        />
      ) : null}
      {cambiandoClave ? (
        <CambiarContrasena
          nombre={sesion.nombre}
          alCerrar={() => setCambiandoClave(false)}
          alCaducarSesion={() => {
            setCambiandoClave(false)
            setSesion(null)
          }}
        />
      ) : null}
      {/* La columna del contenido. Existe por el cajón: en móvil hace falta una barra encima
          de la pantalla con el botón del menú, y ponerla dentro de cada una de las siete
          pantallas sería la misma barra escrita siete veces. En escritorio la barra no se
          pinta y esta columna es transparente: su hijo sigue siendo `flex-1`, como antes. */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <header
        className="flex shrink-0 items-center gap-3 px-3 md:hidden"
        style={{ backgroundColor: '#0B0912', color: '#EDEAF4', minHeight: 52 }}
      >
        <button
          type="button"
          onClick={() => setMenuAbierto(true)}
          aria-label="Abrir el menú"
          aria-expanded={menuAbierto}
          className="flex shrink-0 items-center justify-center"
          style={{ width: 38, height: 38, border: '1px solid rgba(255,255,255,0.16)', color: '#C4B5FD' }}
        >
          <svg aria-hidden width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="square">
            <path d="M4 7h16M4 12h16M4 17h16" />
          </svg>
        </button>
        {/* El nombre de la sección donde estás. En escritorio lo dice el menú, que siempre se
            ve; con el cajón cerrado no lo diría nadie. */}
        <span className="truncate" style={{ fontWeight: 600, fontSize: 15, letterSpacing: '-0.02em' }}>
          {seccion.etiqueta}
        </span>
      </header>
      {activa === 'inicio' ? (
        // La portada. De solo lectura, como SinResolver: vuelve al ingreso por el mismo
        // camino si la sesión caduca a mitad de la mañana.
        <Inicio alCaducarSesion={() => setSesion(null)} />
      ) : activa === 'conversaciones' ? (
        // Mira y, si el rol lo permite, escribe. El 401 se maneja como en las demas.
        <Conversaciones
          alCaducarSesion={() => setSesion(null)}
          seleccionInicial={saltarA}
          alConsumirSeleccion={() => setSaltarA(null)}
        />
      ) : activa === 'agenda' ? (
        // Escribe --marca asistencias--, así que puede toparse con un 401 a mitad de la tarde
        // igual que Tratamientos y SinResolver, y vuelve al ingreso por el mismo camino.
        <Agenda alCaducarSesion={() => setSesion(null)} />
      ) : activa === 'pruebas' ? (
        <Pruebas />
      ) : activa === 'tratamientos' ? (
        // La única pantalla que necesita la sesión ENTERA y no solo el 401: el formulario de
        // crear tratamiento solo lo ve `admin`, y el de editar fichas también deja fuera a
        // recepción.
        //
        // Como Agenda, escribe, así que puede toparse con un 401 a mitad de la tarde.
        // Devolverla al ingreso se hace desde aquí y no allí: `sesion` vive en este estado, y
        // con `null` la rama de arriba ya renderiza `<Ingreso />` sola.
        <Tratamientos sesion={sesion} alCaducarSesion={() => setSesion(null)} />
      ) : activa === 'bandeja' ? (
        // Pantalla puramente informativa: sin sesion no hay nada que mostrar mas que el
        // ingreso, y con sesion caducada a mitad de lectura vuelve a el desde aqui, igual
        // que Tratamientos.
        <SinResolver
          alCaducarSesion={() => setSesion(null)}
          alAbrirConversacion={(telefono) => {
            setSaltarA(telefono)
            ir('conversaciones')
          }}
        />
      ) : activa === 'leads' ? (
        // La cartera. De solo lectura, como SinResolver, y con el mismo salto: su única
        // acción es abrir a esa persona en Conversaciones, que es la pantalla que sabe
        // escribir --y la que comprueba la ventana de 24 h y el rol antes de dejar hacerlo--.
        <Leads
          alCaducarSesion={() => setSesion(null)}
          alAbrirConversacion={(telefono) => {
            setSaltarA(telefono)
            ir('conversaciones')
          }}
        />
      ) : activa === 'configuracion' ? (
        // Escribe, y solo `admin` --lo comprueba `exigir_rol` en el PATCH, no el botón que
        // esta pantalla esconde--. Como Tratamientos y Agenda, puede toparse con un 401 a
        // mitad de la tarde y vuelve al ingreso por el mismo camino.
        <Configuracion alCaducarSesion={() => setSesion(null)} />
      ) : null}
      </div>
    </div>
  )
}

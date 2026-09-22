import { useEffect, useState } from 'react'
import Sidebar, { CONFIGURACION, PRUEBAS, SECCIONES, type Seccion, type SeccionId } from '@/componentes/Sidebar'
import Ingreso from '@/pantallas/Ingreso'
import Conversaciones from '@/pantallas/Conversaciones'
import Inicio from '@/pantallas/Inicio'
import Agenda from '@/pantallas/Agenda'
import Pruebas from '@/pantallas/Pruebas'
import Tratamientos from '@/pantallas/Tratamientos'
import SinResolver from '@/pantallas/SinResolver'
import PantallaPendiente from '@/pantallas/Pendiente'
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
  }

  async function cerrarSesion() {
    await salir()
    setSesion(null)
  }

  if (comprobando) {
    // Un instante en blanco, no un spinner: en una red local esto dura menos de lo que tarda
    // en verse, y un spinner que parpadea se lee como si algo hubiera fallado.
    return <div style={{ minHeight: '100vh', backgroundColor: '#07060B' }} />
  }

  if (!sesion) return <Ingreso alEntrar={setSesion} />

  const seccion = TODAS.find((s) => s.id === activa) ?? TODAS[0]

  return (
    <div className="flex h-screen overflow-hidden" style={{ backgroundColor: '#F9FAFB' }}>
      <Sidebar activa={activa} ir={ir} sesion={sesion} alSalir={cerrarSesion} />
      {activa === 'inicio' ? (
        // La portada. De solo lectura, como SinResolver: vuelve al ingreso por el mismo
        // camino si la sesión caduca a mitad de la mañana.
        <Inicio alCaducarSesion={() => setSesion(null)} />
      ) : activa === 'conversaciones' ? (
        // Mira y, si el rol lo permite, escribe. El 401 se maneja como en las demas.
        <Conversaciones alCaducarSesion={() => setSesion(null)} />
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
        <SinResolver alCaducarSesion={() => setSesion(null)} />
      ) : (
        <PantallaPendiente seccion={seccion} />
      )}
    </div>
  )
}

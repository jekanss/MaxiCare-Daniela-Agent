/* El único sitio del frontend que sabe qué rutas existen en el servidor.
 *
 * Todo pasa por `pedir`, y eso compra dos cosas que si no habría que recordar en cada
 * pantalla: `credentials: 'same-origin'` (sin eso la cookie de sesión no viaja y todo
 * responde 401 sin explicar por qué) y el 401 tratado como «tu sesión se acabó», que es la
 * única forma de que caducar a mitad de la tarde no se vea como un error roto. */

export type Sesion = {
  usuario: string
  nombre: string
  rol: 'admin' | 'doctor' | 'recepcion'
}

export type RespuestaChat = {
  mensaje: string
  turno: number
  estado_oportunidad: string
  barrera: string
  requiere_escalamiento: boolean
  motivo_escalamiento: string
  fuera_de_alcance: boolean
  tripwires: string[]
  regenerado: boolean
  conversacion: string
}

export class SesionCaducada extends Error {}

async function pedir<T>(ruta: string, opciones: RequestInit = {}): Promise<T> {
  const r = await fetch(ruta, {
    ...opciones,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(opciones.headers ?? {}) },
  })
  if (r.status === 401) throw new SesionCaducada('Tu sesión se cerró. Vuelve a ingresar.')
  if (!r.ok) {
    // El servidor manda `{detalle: "..."}` en el vocabulario de quien lo va a leer. Si no
    // vino nada legible, un texto genérico antes que volcar el HTML de un error 500.
    const cuerpo = await r.json().catch(() => null)
    throw new Error(cuerpo?.detalle ?? `No se pudo completar la operación (${r.status}).`)
  }
  return r.json() as Promise<T>
}

/** La sesión actual, o `null` si no hay. Nunca lanza por falta de sesión: que no la haya es
 *  el estado normal de quien todavía no ha entrado, no un error. */
export async function sesionActual(): Promise<Sesion | null> {
  try {
    return await pedir<Sesion>('/api/sesion')
  } catch {
    return null
  }
}

export async function entrar(usuario: string, contrasena: string): Promise<Sesion> {
  return pedir<Sesion>('/api/entrar', {
    method: 'POST',
    body: JSON.stringify({ usuario, contrasena }),
  })
}

export async function salir(): Promise<void> {
  await fetch('/api/salir', { method: 'POST', credentials: 'same-origin' })
}

export async function hablarConDaniela(
  mensaje: string,
  conversacion: string | null,
): Promise<RespuestaChat> {
  return pedir<RespuestaChat>('/api/pruebas/chat', {
    method: 'POST',
    body: JSON.stringify({ mensaje, conversacion }),
  })
}

export async function reiniciarChat(conversacion: string | null): Promise<void> {
  await pedir('/api/pruebas/reiniciar', {
    method: 'POST',
    body: JSON.stringify({ conversacion }),
  })
}

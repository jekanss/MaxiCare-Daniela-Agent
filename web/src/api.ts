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
  // `null` cuando el turno fue un `/clearstate`: la conversación se borró y la siguiente
  // petición tiene que pedir una nueva, no reusar un id que ya no existe.
  conversacion: string | null
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

// ------------------------------------------------------------------------------------------
// Tratamientos y base de conocimiento
// ------------------------------------------------------------------------------------------

export type TratamientoFila = {
  clave: string
  etiqueta: string
  activo: boolean
  /** Si la clave está en el `Literal` que clasifica radiografías. Se calcula en el servidor
   *  a partir del código, no de una columna, así que no puede desincronizarse. */
  en_el_muro: boolean
  fichas: number
  /** Los conceptos mínimos que este tratamiento todavía no tiene. */
  faltan: string[]
}

export type FichaFila = {
  tratamiento: string
  concepto: string
  contenido: string
  aprobado: boolean
  nota_pendiente: string | null
  actualizado_en: string
}

export type CambioFila = {
  tabla: string
  clave: string
  valor_anterior: string | null
  valor_nuevo: string
  usuario: string
  cambiado_en: string
}

export async function listarTratamientos(): Promise<TratamientoFila[]> {
  const r = await pedir<{ tratamientos: TratamientoFila[] }>('/api/tratamientos')
  return r.tratamientos
}

/** La clave viaja tal cual se escribió. No se pasa a minúsculas aquí a propósito: el
 *  servidor la valida y devuelve un 400 legible, y silenciar 'Carillas' convirtiéndola en
 *  'carillas' escondería justo el descuido que esa validación existe para atrapar. */
export async function crearTratamiento(clave: string, etiqueta: string): Promise<TratamientoFila> {
  return pedir<TratamientoFila>('/api/tratamientos', {
    method: 'POST',
    body: JSON.stringify({ clave, etiqueta }),
  })
}

export async function cambiarTratamiento(
  clave: string,
  cambio: { etiqueta?: string; activo?: boolean },
): Promise<TratamientoFila> {
  return pedir<TratamientoFila>(`/api/tratamientos/${encodeURIComponent(clave)}`, {
    method: 'PATCH',
    body: JSON.stringify(cambio),
  })
}

export async function listarFichas(): Promise<FichaFila[]> {
  const r = await pedir<{ fichas: FichaFila[] }>('/api/conocimiento')
  return r.fichas
}

/** El PUT devuelve la ficha SIN `actualizado_en` -- `panel.guardar_ficha` no lo incluye en
 *  su diccionario de vuelta--. Se declara así en vez de prometer una `FichaFila` completa:
 *  un tipo que afirma un campo que el servidor no manda es una mentira que el compilador
 *  no puede atrapar. La pantalla recarga la lista después de guardar, que además es lo
 *  único que actualiza el `faltan` del tratamiento. */
export async function guardarFicha(f: {
  tratamiento: string
  concepto: string
  contenido: string
  aprobado: boolean
  nota_pendiente: string | null
}): Promise<Omit<FichaFila, 'actualizado_en'>> {
  return pedir<Omit<FichaFila, 'actualizado_en'>>('/api/conocimiento', {
    method: 'PUT',
    body: JSON.stringify(f),
  })
}

/** La bitácora. `panel.py` la escribe en la misma transacción que cada cambio desde el
 *  primer día; sin esta función nadie podía leerla, y una bitácora que solo se escribe no
 *  sirve para reconstruir quién cambió un precio. */
export async function listarHistorial(): Promise<CambioFila[]> {
  const r = await pedir<{ cambios: CambioFila[] }>('/api/historial')
  return r.cambios
}

export type CasoSinResolver = {
  huella: string
  tipo: 'FALTA_DATO' | 'GUARDRAIL' | 'ROTO' | 'HUMANO'
  contador: number
  escalo: number
  primera_vez: string
  ultima_vez: string
  ejemplos: string[]
  informe: { que_paso: string; por_que: string; recomiendo: string } | null
}

/** La ventana de «sin resolver»: lo que Daniela no pudo resolver en los últimos 30 días,
 *  agrupado por huella. La `huella` llega SIEMPRE, con cualquier rol: `titulo()` la parsea
 *  para componer el título legible, y sin ella la clínica se quedaría sin títulos. Lo que
 *  `es_admin` decide es si la pantalla la enseña CRUDA -- el detalle técnico no es para la
 *  clínica. Pantalla de solo lectura, sin ninguna escritura que doble esta función. */
export async function listarSinResolver(): Promise<{
  casos: CasoSinResolver[]
  es_admin: boolean
}> {
  return pedir<{ casos: CasoSinResolver[]; es_admin: boolean }>('/api/sin-resolver')
}

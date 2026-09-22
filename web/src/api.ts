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

// ------------------------------------------------------------------------------------------
// Agenda y marca de asistencia
// ------------------------------------------------------------------------------------------

/** Una cita del día, tal como la devuelve el endpoint.
 *
 *  No hay `origen` del paciente y no es un olvido: ese dato NO EXISTE en la base --`citas` no
 *  tiene la columna y `conversaciones.canal` solo distingue `whatsapp` de `web`--. La maqueta
 *  de la pantalla se lo inventaba; declararlo aquí volvería a invitar a pintarlo.
 *
 *  `asistio` tiene TRES valores: `true`, `false` y `null` (sin marcar todavía). Colapsar el
 *  `null` con el `false` diría que el paciente no vino cuando lo único cierto es que nadie lo
 *  ha marcado. La respuesta puede traer más claves --`evento_calendar_id`, `reserva_id`...--;
 *  aquí se declaran solo las que la pantalla pinta. */
export type CitaDeAgenda = {
  id: string
  telefono: string
  nombre_completo: string
  tratamiento: string
  /** ISO. Si viene sin zona es hora de pared de Bogotá: la pantalla lo resuelve en un sitio. */
  inicio: string
  duracion_minutos: number
  estado: string
  asistio: boolean | null
}

/** Una franja que el doctor bloqueó en Google Calendar. No es una cita de Daniela: no se
 *  marca, no tiene paciente y no cuenta en el resumen del día. */
export type BloqueoDeAgenda = { inicio: string; fin: string; titulo: string }

/** Lo que la reconciliación contra Google Calendar corrigió al abrir el día.
 *
 *  `hora_nueva` en `null` significa CANCELADA, no movida: es lo único que distingue los dos
 *  casos, y por eso `que_paso` viaja aparte en vez de deducirse. */
export type CorreccionDeAgenda = {
  cita_id: string
  que_paso: 'movida' | 'cancelada'
  hora_vieja: string
  hora_nueva: string | null
  tratamiento: string
  nombre_completo: string
}

/** Por qué el servidor pintó un día SIN contrastarlo contra Google Calendar.
 *
 *  Los dos literales los decide Python --`runtime.MOTIVO_FUERA_DE_VENTANA` y
 *  `runtime.MOTIVO_CALENDARIO_NO_DISPONIBLE`--, y este archivo es el ÚNICO sitio de
 *  TypeScript donde se escriben: las pantallas comparan contra las constantes de abajo, nunca
 *  contra la cadena. Que las dos copias no se separen en silencio lo ata una prueba de Python
 *  (`tests/test_agenda_pantalla.py`), porque cruzar el borde de lenguaje sin nada que ate los
 *  dos lados es exactamente cómo un aviso vuelve a mentir sin que nadie se entere.
 *
 *  Y los dos casos NO son el mismo: `fuera_de_ventana` es rutina --el día es viejo y por eso
 *  ya no se contrasta-- y `no_disponible` es una avería. Colapsarlos deja la pantalla dando
 *  la alarma roja a diario por nada, y una alarma que suena por nada deja de leerse el día
 *  que significa algo. */
export type MotivoSinCalendario = 'fuera_de_ventana' | 'no_disponible'

/** El día es más viejo que `herramientas.DIAS_HACIA_ATRAS_AL_SINCRONIZAR`. No falló nada. */
export const MOTIVO_FUERA_DE_VENTANA: MotivoSinCalendario = 'fuera_de_ventana'
/** Google no contestó, o no hay un calendario en el que se pueda confiar. Eso sí es avería. */
export const MOTIVO_CALENDARIO_NO_DISPONIBLE: MotivoSinCalendario = 'no_disponible'

export type AgendaDelDia = {
  dia: string
  citas: CitaDeAgenda[]
  bloqueos: BloqueoDeAgenda[]
  correcciones: CorreccionDeAgenda[]
  /** Las de días anteriores cuya hora pasó y siguen sin marcar. */
  sin_marcar: CitaDeAgenda[]
  /** `false` cuando ese día NO se contrastó contra Google Calendar, y eso pasa por dos
   *  motivos distintos: que Google no contestara, o que el día sea demasiado viejo para que
   *  se contraste. `motivo_sin_calendario` dice cuál. La agenda se pinta igual, con su aviso,
   *  y la marca de asistencia funciona en los dos casos. */
  calendario_disponible: boolean
  /** Por qué no se contrastó, o `null` cuando sí se contrastó. Es `null` si y solo si
   *  `calendario_disponible` es `true`: el servidor sostiene esa invariante. */
  motivo_sin_calendario: MotivoSinCalendario | null
}

/** El día reconciliado. Con `dia` en `null` el servidor decide: hoy en hora de Bogotá.
 *
 *  Esta lectura ESCRIBE en la base --puede mover o cancelar una cita que en Google ya cambió--.
 *  Queda dicho aquí porque un `GET` que escribe sorprende a cualquiera que lo lea después. */
export async function leerAgenda(dia: string | null): Promise<AgendaDelDia> {
  const ruta = dia ? `/api/agenda?dia=${encodeURIComponent(dia)}` : '/api/agenda'
  return pedir<AgendaDelDia>(ruta)
}

/** Marca la asistencia, o la desmarca con `null`.
 *
 *  El `null` viaja explícito en el cuerpo --`{"asistio": null}`-- y no omitiendo el campo:
 *  «desmarcar» tiene que distinguirse de «no lo mandé», o corregir una marca equivocada sería
 *  imposible. Devuelve la cita ya actualizada, que es lo que la pantalla pinta: releer el día
 *  entero para ver un booleano costaría la reconciliación completa contra Google. */
export async function marcarAsistencia(
  citaId: string,
  valor: boolean | null,
): Promise<CitaDeAgenda> {
  return pedir<CitaDeAgenda>(`/api/agenda/citas/${encodeURIComponent(citaId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ asistio: valor }),
  })
}

// ------------------------------------------------------------------------------------------
// La portada
// ------------------------------------------------------------------------------------------

/** Lo que pinta la portada.
 *
 *  Las cifras llegan CRUDAS y los porcentajes se calculan aquí: así `llegaron` viaja con
 *  `marcadas` y `cumplibles` al lado, que es lo que permite escribir «8 de 12 marcadas» en
 *  vez de un 67 % que no dice sobre cuántas citas se calculó.
 *
 *  La unidad de `escribieron` y `con_cita` es el TELÉFONO, no la conversación: una
 *  conversación caduca a las 24 h, así que contar filas inflaría el denominador.
 *
 *  La respuesta trae más claves de las que se declaran aquí (`desde`, por ejemplo); esta es
 *  la convención del archivo (ver `CitaDeAgenda`): solo lo que la pantalla pinta. */
export type ResumenInicio = {
  usuario: string
  dias: number
  escribieron: number
  con_cita: number
  asistencia: { llegaron: number; marcadas: number; cumplibles: number }
  sin_contestar: number
  relevo: { minutos: number; conversaciones: number }
  volumen: { dia: string; conversaciones: number }[]
  agenda_hoy: CitaDeAgenda[]
  /** Los tres casos más frecuentes, el mismo tipo que pinta la pantalla de Sin resolver. */
  atencion: CasoSinResolver[]
}

/** La portada. De SOLO LECTURA, y ahí está la diferencia con `leerAgenda`: aquella
 *  reconcilia contra Google Calendar al abrirse. Esta es la primera pantalla de cada sesión,
 *  así que reconciliar aquí serían llamadas a Google en cada ingreso al panel. */
export async function leerInicio(): Promise<ResumenInicio> {
  return pedir<ResumenInicio>('/api/inicio')
}

// ------------------------------------------------------------------------------------------
// Conversaciones
// ------------------------------------------------------------------------------------------

/** Los cuatro estados de la lista, en orden de precedencia. `relevo` gana a `esperando`:
 *  una conversación tomada en la que entran mensajes cumple las dos, y lo que hay que decir
 *  es que ya hay alguien encima, no que nadie contesta. */
export type EstadoConversacion = 'relevo' | 'esperando' | 'activa' | 'cerrada'

export type ResumenConversacion = {
  telefono: string
  /** `null` cuando no hay ficha ni nombre de perfil -- o cuando la ficha dice `PENDIENTE`,
   *  que el servidor ya traduce a `null`. La pantalla cae al teléfono. */
  nombre: string | null
  vista_previa: string
  ultimo_en: string
  sin_contestar: number
  tomada_por: string | null
  estado: EstadoConversacion
}

export type MensajeDelHilo = {
  quien: 'paciente' | 'daniela' | 'doctor'
  /** El nombre de quien escribió. Solo lo llevan los del doctor. */
  autor: string | null
  texto: string
  cuando: string
  /** Solo los del doctor, y solo si el envío a WhatsApp falló. Lo que hace que el hilo
   *  distinga «no lo escribió» de «lo escribió y no salió». */
  fallo: string | null
}

export type HiloDeConversacion = {
  telefono: string
  mensajes: MensajeDelHilo[]
  /** La ventana de 24 h de Meta. `horas` es `null` si esa persona nunca escribió. */
  ventana: { puede: boolean; horas: number | null }
  puede_escribir: boolean
}

export type ListaDeConversaciones = {
  conversaciones: ResumenConversacion[]
  /** Si el ROL puede tomar y escribir. No es el control de acceso --ese vive en el
   *  servidor-- sino lo que evita pintar botones que van a devolver 403. */
  puede_escribir: boolean
  usuario: string
}

/** La lista. De SOLO LECTURA, y es la ruta que más veces se pide del panel: la pantalla se
 *  refresca sola cada diez segundos mientras esté a la vista. */
export async function leerConversaciones(): Promise<ListaDeConversaciones> {
  return pedir<ListaDeConversaciones>('/api/conversaciones')
}

/** El hilo de un paciente: las tres voces en orden. No trae el estado de la conversación --
 *  eso lo trae la lista, en la misma vuelta-- para que las dos rutas no puedan contradecirse. */
export async function leerHilo(telefono: string): Promise<HiloDeConversacion> {
  return pedir<HiloDeConversacion>(`/api/conversaciones/${encodeURIComponent(telefono)}`)
}

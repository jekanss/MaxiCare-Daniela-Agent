import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import {
  cambiarTratamiento,
  crearTratamiento,
  guardarFicha,
  listarFichas,
  listarHistorial,
  listarTratamientos,
  SesionCaducada,
  type CambioFila,
  type FichaFila,
  type Sesion,
  type TratamientoFila,
} from '@/api'

const SP = "'Space Grotesk', sans-serif"

/* La primera pantalla del panel que escribe en la base de verdad.
 *
 * ------------------------------------------------------------------------------------
 * Lo que se edita aquí sale por WhatsApp en el siguiente mensaje
 * ------------------------------------------------------------------------------------
 *
 * No hay despliegue de por medio ni una cola que revise nada: `persistencia.consultar_
 * conocimiento` lee la tabla en cada turno. Cambiar «$1.800.000» por «$1.500.000» aquí es
 * cambiar lo que Daniela le cotiza al siguiente paciente que pregunte. Por eso la pantalla
 * gasta tanto espacio en decir qué significa cada control en vez de asumirlo.
 *
 * ------------------------------------------------------------------------------------
 * Las tres cosas que esta pantalla existe para no dejar que se malentiendan
 * ------------------------------------------------------------------------------------
 *
 * 1. **El interruptor de aprobación no es «visible / invisible».** Una ficha sin aprobar le
 *    llega igual al modelo, precedida de la advertencia de `formatear_conocimiento`. Quien
 *    lo lea como «esto deja de decirse» va a desaprobar un precio creyendo que lo calla, y
 *    el precio va a seguir saliendo. De ahí las etiquetas literales del interruptor y la
 *    frase gris que va debajo.
 *
 * 2. **Una ficha que falta se tiene que ver que falta.** `faltan` viene calculado del
 *    servidor con los tres conceptos mínimos. Un tratamiento sin precio se ve distinto de
 *    uno completo desde la lista, sin desplegar nada: es el trabajo principal de la
 *    pantalla, porque hoy endodoncia y prótesis no tienen ni una ficha y nadie en la
 *    clínica lo sabe.
 *
 * 3. **`_general` no es un tratamiento.** Son los hechos de la clínica -- horario, sede,
 *    EPS, medios de pago, urgencias--. Mostrarlo en la lista con su guion bajo delante
 *    invitaría a agendarle una cita. Va en su propia pestaña, «La clínica».
 *
 * ------------------------------------------------------------------------------------
 * Los permisos
 * ------------------------------------------------------------------------------------
 *
 * Los botones se deshabilitan según el rol, con el porqué en el `title`. Eso es comodidad,
 * no control: `exigir_rol` en el servidor devuelve 403 igual, y ese 403 se enseña tal cual
 * viene. Un botón escondido nunca ha protegido una tabla. */

const GENERAL = '_general'

/* `no_identificado` es una fila de `tratamientos` como las otras trece, pero no es un
 * servicio: es donde cae lo que Daniela no pudo clasificar, y lo que queda escrito en
 * `citas.tratamiento` cuando la clasificación falla. Nadie le va a poner precio.
 *
 * Por eso se muestra sin el marco rojo y fuera del contador de «sin precio». Contarlo daría
 * «3 tratamientos sin precio» cuando los que de verdad le faltan a la clínica son dos
 * --endodoncia y prótesis--, y el tercero mandaría a alguien a inventarle una tarifa a una
 * categoría de error. Sigue en la lista porque la clave existe y se puede desactivar. */
const CLASIFICACION = 'no_identificado'

/** Los tres conceptos que `panel.CONCEPTOS_MINIMOS` exige, con el nombre que usa la
 *  clínica. Si el servidor empieza a exigir uno más, el que no esté aquí sale con su clave
 *  cruda -- feo, pero nunca invisible. */
const NOMBRE_CONCEPTO: Record<string, string> = {
  precio: 'precio',
  duracion: 'duración',
  profesional: 'profesional',
}

function mensajeDe(err: unknown): string {
  return err instanceof Error ? err.message : 'No se pudo completar la operación.'
}

/** Sugiere la clave a partir del nombre visible. Solo sugiere: lo que se envía es lo que
 *  quede escrito en el campo, aunque lleve mayúsculas. El servidor rechaza y explica. */
function sugerirClave(etiqueta: string): string {
  return etiqueta
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 24)
}

function cuando(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('es-CO', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function Insignia({
  texto,
  fondo,
  color,
  titulo,
}: {
  texto: string
  fondo: string
  color: string
  titulo?: string
}) {
  return (
    <span
      title={titulo}
      className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full whitespace-nowrap"
      style={{ backgroundColor: fondo, color }}
    >
      {texto}
    </span>
  )
}

function Aviso({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs leading-relaxed" style={{ color: '#92400E' }}>
      {children}
    </p>
  )
}

// ------------------------------------------------------------------------------------------
// El interruptor de aprobación
// ------------------------------------------------------------------------------------------

/* Dos botones etiquetados con la frase entera, no un `switch` con la palabra «Aprobado» al
 * lado. Un switch obliga a adivinar qué significa apagado, y la respuesta que todo el mundo
 * adivina -- «no se ve»-- es justo la equivocada. */
function Aprobacion({
  aprobado,
  cambiar,
  puedeEditar,
  porQueNo,
}: {
  aprobado: boolean
  cambiar: (v: boolean) => void
  puedeEditar: boolean
  porQueNo: string
}) {
  const opciones: { valor: boolean; texto: string; fondo: string; color: string }[] = [
    { valor: true, texto: 'Es un compromiso comercial', fondo: '#D1FAE5', color: '#065F46' },
    { valor: false, texto: 'Todavía no lo es', fondo: '#FEF3C7', color: '#92400E' },
  ]

  return (
    <div>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Estado de aprobación de la ficha">
        {opciones.map((o) => {
          const puesta = o.valor === aprobado
          return (
            <button
              key={String(o.valor)}
              type="button"
              aria-pressed={puesta}
              disabled={!puedeEditar}
              title={puedeEditar ? undefined : porQueNo}
              onClick={() => cambiar(o.valor)}
              className="text-xs font-semibold px-3 py-1.5 rounded-lg transition-all disabled:cursor-not-allowed disabled:opacity-50"
              style={{
                backgroundColor: puesta ? o.fondo : '#FFFFFF',
                color: puesta ? o.color : '#6B7280',
                border: `1.5px solid ${puesta ? o.color : '#E5E7EB'}`,
              }}
            >
              {o.texto}
            </button>
          )
        })}
      </div>
      <p className="text-[11px] mt-1.5 leading-relaxed" style={{ color: '#9CA3AF' }}>
        Sin aprobar no la esconde: Daniela igual la usa, avisando que falta por definir.
      </p>
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// Una ficha
// ------------------------------------------------------------------------------------------

function EditorFicha({
  ficha,
  puedeEditar,
  porQueNo,
  alGuardar,
}: {
  ficha: FichaFila
  puedeEditar: boolean
  porQueNo: string
  alGuardar: (f: {
    tratamiento: string
    concepto: string
    contenido: string
    aprobado: boolean
    nota_pendiente: string | null
  }) => Promise<boolean>
}) {
  const campo = useId()
  const [contenido, setContenido] = useState(ficha.contenido)
  const [aprobado, setAprobado] = useState(ficha.aprobado)
  const [nota, setNota] = useState(ficha.nota_pendiente ?? '')
  const [guardando, setGuardando] = useState(false)

  /* Sin resincronizar con la prop cuando la lista se recarga: después de guardar, `ficha`
     vuelve con lo que se acaba de escribir y `sucio` se apaga solo. Resincronizar borraría
     lo que alguien tenga a medias en OTRA ficha de la misma pantalla. */
  const sucio =
    contenido !== ficha.contenido ||
    aprobado !== ficha.aprobado ||
    nota.trim() !== (ficha.nota_pendiente ?? '')

  const vacio = contenido.trim() === ''

  async function guardar() {
    setGuardando(true)
    try {
      await alGuardar({
        tratamiento: ficha.tratamiento,
        concepto: ficha.concepto,
        contenido,
        aprobado,
        nota_pendiente: nota.trim() === '' ? null : nota.trim(),
      })
    } finally {
      setGuardando(false)
    }
  }

  return (
    <div
      className="rounded-xl p-4"
      style={{
        backgroundColor: '#FFFFFF',
        border: `1px solid ${aprobado ? '#E5E7EB' : '#FDE68A'}`,
      }}
    >
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <p className="text-sm font-semibold" style={{ color: '#111827' }}>
          {ficha.concepto.replace(/_/g, ' ')}
        </p>
        {!aprobado && <Insignia texto="sin aprobar" fondo="#FEF3C7" color="#92400E" />}
        {sucio && <Insignia texto="sin guardar" fondo="#EDE9FE" color="#5B21B6" />}
        <span className="text-[10px] ml-auto" style={{ color: '#D1D5DB' }}>
          editada {cuando(ficha.actualizado_en)}
        </span>
      </div>

      <textarea
        value={contenido}
        onChange={(e) => setContenido(e.target.value)}
        disabled={!puedeEditar}
        rows={Math.min(10, Math.max(3, contenido.split('\n').length + 1))}
        aria-label={`Contenido de ${ficha.concepto}`}
        className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3 disabled:opacity-70"
        style={{
          backgroundColor: '#F9FAFB',
          border: '1px solid #E5E7EB',
          color: '#111827',
          fontFamily: SP,
        }}
      />

      <div className="flex flex-wrap items-start justify-between gap-4 mt-3">
        <Aprobacion aprobado={aprobado} cambiar={setAprobado} puedeEditar={puedeEditar} porQueNo={porQueNo} />

        <button
          type="button"
          onClick={guardar}
          disabled={!puedeEditar || !sucio || guardando || vacio}
          title={
            !puedeEditar ? porQueNo
            : vacio ? 'Una ficha vacía no es lo mismo que una ficha sin datos. Si MaxiCare no tiene el dato, déjala sin crear: Daniela dirá «SIN DATO DOCUMENTADO» y escalará.'
            : undefined
          }
          className="px-4 py-2 rounded-lg text-sm font-bold transition-all disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95"
          style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}
        >
          {guardando ? 'Guardando…' : 'Guardar'}
        </button>
      </div>

      {!aprobado && (
        <div className="mt-3">
          <label htmlFor={`${campo}-nota`} className="block text-xs font-semibold mb-1" style={{ color: '#374151' }}>
            Qué falta por definir
          </label>
          <input
            id={`${campo}-nota`}
            value={nota}
            onChange={(e) => setNota(e.target.value)}
            disabled={!puedeEditar}
            placeholder="Falta confirmarlo con la doctora"
            className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3 disabled:opacity-70"
            style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A', color: '#111827', fontFamily: SP }}
          />
          <p className="text-[11px] mt-1" style={{ color: '#9CA3AF' }}>
            Esta nota se la lee Daniela, no el paciente: va dentro de la advertencia, como
            «Falta por definir: …».
          </p>
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// Crear una ficha
// ------------------------------------------------------------------------------------------

const OTRO = '__otro__'

function NuevaFicha({
  tratamiento,
  conceptos,
  existentes,
  puedeEditar,
  porQueNo,
  alGuardar,
  alCerrar,
}: {
  tratamiento: string
  conceptos: string[]
  /** Las fichas que este tratamiento ya tiene. Enteras, no solo sus conceptos: hace falta
   *  el contenido para poder enseñar qué se estaría reemplazando. */
  existentes: FichaFila[]
  puedeEditar: boolean
  porQueNo: string
  alGuardar: (f: {
    tratamiento: string
    concepto: string
    contenido: string
    aprobado: boolean
    nota_pendiente: string | null
  }) => Promise<boolean>
  alCerrar: () => void
}) {
  const campo = useId()
  const yaTiene = existentes.map((f) => f.concepto)
  const disponibles = conceptos.filter((c) => !yaTiene.includes(c))
  const [elegido, setElegido] = useState(disponibles[0] ?? OTRO)
  const [otro, setOtro] = useState('')
  const [contenido, setContenido] = useState('')
  const [aprobado, setAprobado] = useState(true)
  const [nota, setNota] = useState('')
  const [guardando, setGuardando] = useState(false)

  /* Si mientras este formulario está abierto se guarda otra ficha del mismo tratamiento,
     `recargar()` encoge `disponibles` y el `<option>` que casaba con `elegido` desaparece.
     El navegador enseña entonces el primero de la lista, pero el estado sigue apuntando al
     viejo: se enviaría un concepto que la pantalla ya no ofrece. Se resincroniza por el
     contenido de la lista, no por su identidad, que cambia en cada render. */
  const listaDisponibles = disponibles.join('|')
  useEffect(() => {
    setElegido((e) => (e === OTRO || disponibles.includes(e) ? e : (disponibles[0] ?? OTRO)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listaDisponibles])

  /* El servidor recorta y pasa a minúsculas antes de escribir (`panel.guardar_ficha`), así
     que «Precio » y «precio» son la MISMA fila. La comprobación de choque tiene que usar la
     misma regla o avisaría de menos justo en el caso que más duele. */
  const concepto = elegido === OTRO ? otro : elegido
  const conceptoReal = concepto.trim().toLowerCase()

  /* El `PUT` es un upsert: `ON CONFLICT (tratamiento, concepto) DO UPDATE SET contenido =
     EXCLUDED.contenido`. Escribir a mano un concepto que ya existe NO crea una segunda
     ficha: reemplaza la que hay, y si esa era el precio, el precio anterior desaparece sin
     que nadie lo haya pedido. El `<select>` ya filtra los tomados; «otro concepto…» es
     texto libre y se le escapaban. */
  const chocaCon = existentes.find((f) => f.concepto === conceptoReal) ?? null
  const listo = conceptoReal !== '' && contenido.trim() !== ''

  async function guardar() {
    setGuardando(true)
    try {
      // Solo se cierra si el servidor aceptó. Cerrar pase lo que pase borraría lo escrito
      // justo cuando hace falta: al volver un 403 o un 400, con el texto todavía a medias.
      const ok = await alGuardar({
        tratamiento,
        concepto,
        contenido,
        aprobado,
        nota_pendiente: nota.trim() === '' ? null : nota.trim(),
      })
      if (!ok) return
      setContenido('')
      setOtro('')
      setNota('')
      alCerrar()
    } finally {
      setGuardando(false)
    }
  }

  return (
    <div className="rounded-xl p-4" style={{ backgroundColor: '#FFFFFF', border: '1px dashed #C4B5FD' }}>
      <p className="text-sm font-semibold mb-3" style={{ color: '#111827' }}>
        Nueva ficha
      </p>

      <label htmlFor={`${campo}-concepto`} className="block text-xs font-semibold mb-1" style={{ color: '#374151' }}>
        Concepto
      </label>
      <select
        id={`${campo}-concepto`}
        value={elegido}
        onChange={(e) => setElegido(e.target.value)}
        disabled={!puedeEditar}
        className="w-full px-3 py-2 text-sm rounded-lg outline-none disabled:opacity-70"
        style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
      >
        {disponibles.map((c) => (
          <option key={c} value={c}>
            {c.replace(/_/g, ' ')}
          </option>
        ))}
        <option value={OTRO}>otro concepto…</option>
      </select>

      {/* La lista sale de lo que ya existe en la base, no de una constante escrita aquí: si
          la clínica inventa un concepto, la siguiente persona lo encuentra en el desplegable
          en vez de volver a inventarlo con otro nombre. */}
      {elegido === OTRO && (
        <div className="mt-2">
          <input
            value={otro}
            onChange={(e) => setOtro(e.target.value)}
            disabled={!puedeEditar}
            placeholder="garantia_extendida"
            aria-label="Concepto nuevo"
            className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3 disabled:opacity-70"
            style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
          />
          <p className="text-[11px] mt-1 leading-relaxed" style={{ color: '#9CA3AF' }}>
            Un concepto nuevo no se pierde: si Daniela pregunta por uno que no existe, la tool
            le devuelve todos los del tratamiento. Pero un «precios» junto a un «precio» deja
            dos precios conviviendo.
          </p>
        </div>
      )}

      {/* El aviso vecino de arriba es el de duplicar. Este es el de destruir, y por eso va
          en ámbar, con el texto que se perdería delante. */}
      {chocaCon !== null && (
        <div role="alert" className="mt-2 rounded-lg px-3 py-2.5" style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A' }}>
          <p className="text-xs font-semibold" style={{ color: '#92400E' }}>
            «{conceptoReal.replace(/_/g, ' ')}» ya existe en este tratamiento. Guardar no crea
            una segunda ficha: reemplaza la que hay.
          </p>
          <p className="text-xs mt-1.5 whitespace-pre-wrap" style={{ color: '#92400E' }}>
            Hoy dice: <span style={{ color: '#78350F' }}>{chocaCon.contenido}</span>
          </p>
          <p className="text-[11px] mt-1.5" style={{ color: '#B45309' }}>
            Si lo que querías era corregirla, ciérrala y edítala arriba: así ves lo que
            cambias. La bitácora guarda el texto anterior de todas formas.
          </p>
        </div>
      )}

      <label htmlFor={`${campo}-contenido`} className="block text-xs font-semibold mb-1 mt-3" style={{ color: '#374151' }}>
        Contenido
      </label>
      <textarea
        id={`${campo}-contenido`}
        value={contenido}
        onChange={(e) => setContenido(e.target.value)}
        disabled={!puedeEditar}
        rows={3}
        placeholder="Desde $1.200.000 por unidad. Incluye la valoración inicial."
        className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3 disabled:opacity-70"
        style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
      />

      <div className="mt-3">
        <Aprobacion aprobado={aprobado} cambiar={setAprobado} puedeEditar={puedeEditar} porQueNo={porQueNo} />
      </div>

      {!aprobado && (
        <input
          value={nota}
          onChange={(e) => setNota(e.target.value)}
          disabled={!puedeEditar}
          placeholder="Qué falta por definir"
          aria-label="Qué falta por definir"
          className="w-full mt-3 px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3 disabled:opacity-70"
          style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A', color: '#111827', fontFamily: SP }}
        />
      )}

      <div className="flex gap-2 mt-4">
        <button
          type="button"
          onClick={guardar}
          disabled={!puedeEditar || !listo || guardando}
          title={puedeEditar ? undefined : porQueNo}
          className="px-4 py-2 rounded-lg text-sm font-bold transition-all disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95"
          style={{ backgroundColor: chocaCon !== null ? '#D97706' : '#7C3AED', color: '#FFFFFF' }}
        >
          {guardando ? 'Guardando…' : chocaCon !== null ? 'Reemplazar la ficha existente' : 'Crear ficha'}
        </button>
        <button
          type="button"
          onClick={alCerrar}
          className="px-4 py-2 rounded-lg text-sm font-semibold border hover:bg-gray-50 transition-colors"
          style={{ borderColor: '#E5E7EB', color: '#374151' }}
        >
          Cancelar
        </button>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// Un tratamiento de la lista
// ------------------------------------------------------------------------------------------

function FilaTratamiento({
  t,
  fichas,
  conceptos,
  abierta,
  alternar,
  puedeFichas,
  puedeTratamientos,
  porQueNoFichas,
  porQueNoTratamientos,
  forzarNueva,
  alCerrarNueva,
  alGuardarFicha,
  alCambiar,
}: {
  t: TratamientoFila
  fichas: FichaFila[]
  conceptos: string[]
  abierta: boolean
  alternar: () => void
  puedeFichas: boolean
  puedeTratamientos: boolean
  porQueNoFichas: string
  porQueNoTratamientos: string
  forzarNueva: boolean
  alCerrarNueva: () => void
  alGuardarFicha: (f: {
    tratamiento: string
    concepto: string
    contenido: string
    aprobado: boolean
    nota_pendiente: string | null
  }) => Promise<boolean>
  alCambiar: (clave: string, cambio: { etiqueta?: string; activo?: boolean }) => Promise<boolean>
}) {
  const [agregando, setAgregando] = useState(false)
  const [renombrando, setRenombrando] = useState(false)
  const [nombre, setNombre] = useState(t.etiqueta)

  const mostrarNueva = agregando || forzarNueva
  const esClasificacion = t.clave === CLASIFICACION
  const incompleto = t.faltan.length > 0 && !esClasificacion
  const sinFichas = t.fichas === 0 && !esClasificacion

  function cerrarNueva() {
    setAgregando(false)
    alCerrarNueva()
  }

  return (
    <div
      className="rounded-2xl overflow-hidden"
      style={{
        backgroundColor: '#FFFFFF',
        border: `1px solid ${incompleto ? '#FECACA' : '#E5E7EB'}`,
        opacity: t.activo ? 1 : 0.65,
      }}
    >
      <button
        onClick={alternar}
        aria-expanded={abierta}
        className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-gray-50 transition-colors"
      >
        <span className="text-xs shrink-0" style={{ color: '#9CA3AF' }}>
          {abierta ? '▾' : '▸'}
        </span>

        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-sm font-semibold" style={{ color: '#111827' }}>
              {t.etiqueta}
            </p>
            <code className="text-[11px]" style={{ color: '#9CA3AF' }}>
              {t.clave}
            </code>
            {!t.activo && (
              <Insignia
                texto="inactivo"
                fondo="#F3F4F6"
                color="#6B7280"
                titulo="Daniela no lo ofrece ni lo agenda. Las citas que ya existen siguen legibles."
              />
            )}
            {esClasificacion && (
              <Insignia
                texto="no es un servicio"
                fondo="#F3F4F6"
                color="#6B7280"
                titulo="Aquí cae lo que Daniela no pudo clasificar. No se cotiza ni se agenda, y no necesita precio."
              />
            )}
            {!t.en_el_muro && (
              <Insignia
                texto="fuera del muro"
                fondo="#EEF2FF"
                color="#3730A3"
                titulo="Se puede cotizar y agendar, pero una radiografía sobre él se clasifica como «no identificado» hasta que se incorpore al muro con un cambio de código."
              />
            )}
          </div>

          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <span className="text-xs" style={{ color: '#6B7280' }}>
              {t.fichas === 0 ? 'sin fichas' : t.fichas === 1 ? '1 ficha' : `${t.fichas} fichas`}
            </span>
            {esClasificacion ? (
              <span className="text-xs" style={{ color: '#9CA3AF' }}>
                donde cae lo que no se pudo clasificar
              </span>
            ) : incompleto ? (
              <span className="text-xs font-medium" style={{ color: '#DC2626' }}>
                falta {t.faltan.map((c) => NOMBRE_CONCEPTO[c] ?? c).join(', ')}
              </span>
            ) : (
              <span className="text-xs" style={{ color: '#059669' }}>
                ✓ precio, duración y profesional
              </span>
            )}
          </div>
        </div>

        <span className="ml-auto shrink-0 text-xs" style={{ color: '#9CA3AF' }}>
          {abierta ? 'Cerrar' : 'Abrir'}
        </span>
      </button>

      {abierta && (
        <div className="px-5 pb-5 flex flex-col gap-3" style={{ borderTop: '1px solid #F3F4F6' }}>
          {sinFichas && (
            <div className="rounded-xl px-4 py-3 mt-4" style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}>
              <p className="text-xs leading-relaxed" style={{ color: '#991B1B' }}>
                Este tratamiento no tiene ni una ficha. Daniela dirá «SIN DATO DOCUMENTADO» y
                escalará cada vez que alguien pregunte por él.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-3 mt-4">
            {fichas.map((f) => (
              <EditorFicha
                key={`${f.tratamiento}/${f.concepto}`}
                ficha={f}
                puedeEditar={puedeFichas}
                porQueNo={porQueNoFichas}
                alGuardar={alGuardarFicha}
              />
            ))}
          </div>

          {mostrarNueva ? (
            <NuevaFicha
              tratamiento={t.clave}
              conceptos={conceptos}
              existentes={fichas}
              puedeEditar={puedeFichas}
              porQueNo={porQueNoFichas}
              alGuardar={alGuardarFicha}
              alCerrar={cerrarNueva}
            />
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setAgregando(true)}
                disabled={!puedeFichas}
                title={puedeFichas ? undefined : porQueNoFichas}
                className="px-3 py-1.5 rounded-lg text-xs font-semibold border hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                style={{ borderColor: '#C4B5FD', color: '#7C3AED' }}
              >
                + Añadir ficha
              </button>

              <button
                type="button"
                onClick={() => {
                  setNombre(t.etiqueta)
                  setRenombrando(true)
                }}
                disabled={!puedeTratamientos}
                title={puedeTratamientos ? undefined : porQueNoTratamientos}
                className="px-3 py-1.5 rounded-lg text-xs font-semibold border hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                style={{ borderColor: '#E5E7EB', color: '#374151' }}
              >
                Renombrar
              </button>

              {/* Desactivar, nunca borrar: borrar dejaría las citas históricas apuntando a
                  una clave que ya no existe. */}
              <button
                type="button"
                onClick={() => void alCambiar(t.clave, { activo: !t.activo })}
                disabled={!puedeTratamientos}
                title={
                  puedeTratamientos
                    ? t.activo
                      ? 'Daniela deja de ofrecerlo y de agendarlo. No se borra nada: las citas que ya existen siguen legibles.'
                      : 'Vuelve al vocabulario que Daniela puede ofrecer y agendar.'
                    : porQueNoTratamientos
                }
                className="px-3 py-1.5 rounded-lg text-xs font-semibold border hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed ml-auto"
                style={{ borderColor: t.activo ? '#FECACA' : '#A7F3D0', color: t.activo ? '#B91C1C' : '#047857' }}
              >
                {t.activo ? 'Desactivar' : 'Reactivar'}
              </button>
            </div>
          )}

          {renombrando && (
            <div className="flex flex-wrap gap-2 items-center">
              <input
                value={nombre}
                onChange={(e) => setNombre(e.target.value)}
                aria-label="Nombre visible del tratamiento"
                className="flex-1 min-w-[12rem] px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3"
                style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
              />
              <button
                type="button"
                onClick={async () => {
                  if (await alCambiar(t.clave, { etiqueta: nombre })) setRenombrando(false)
                }}
                disabled={nombre.trim() === '' || nombre.trim() === t.etiqueta}
                className="px-4 py-2 rounded-lg text-sm font-bold disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95"
                style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}
              >
                Guardar nombre
              </button>
              <button
                type="button"
                onClick={() => setRenombrando(false)}
                className="px-4 py-2 rounded-lg text-sm font-semibold border hover:bg-gray-50 transition-colors"
                style={{ borderColor: '#E5E7EB', color: '#374151' }}
              >
                Cancelar
              </button>
              <p className="w-full text-[11px]" style={{ color: '#9CA3AF' }}>
                Cambia el nombre visible, nunca la clave <code>{t.clave}</code>: esa es la que
                está escrita dentro de las citas que ya existen.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// Crear un tratamiento
// ------------------------------------------------------------------------------------------

function NuevoTratamiento({
  alCrear,
}: {
  alCrear: (clave: string, etiqueta: string) => Promise<boolean>
}) {
  const campo = useId()
  const [abierto, setAbierto] = useState(false)
  const [etiqueta, setEtiqueta] = useState('')
  const [clave, setClave] = useState('')
  const [tocada, setTocada] = useState(false)
  const [creando, setCreando] = useState(false)

  function escribirEtiqueta(v: string) {
    setEtiqueta(v)
    if (!tocada) setClave(sugerirClave(v))
  }

  async function crear() {
    setCreando(true)
    try {
      // Si el servidor rechazó la clave, el formulario se queda abierto con lo escrito: es
      // justo lo que hay que corregir, y el 400 explica cómo.
      if (!(await alCrear(clave, etiqueta))) return
      setEtiqueta('')
      setClave('')
      setTocada(false)
      setAbierto(false)
    } finally {
      setCreando(false)
    }
  }

  if (!abierto) {
    return (
      <button
        type="button"
        onClick={() => setAbierto(true)}
        className="px-4 py-2 rounded-lg text-sm font-semibold hover:brightness-95 transition-all"
        style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}
      >
        + Nuevo tratamiento
      </button>
    )
  }

  return (
    <div className="rounded-2xl p-5 w-full" style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}>
      <p className="text-sm font-semibold mb-3" style={{ color: '#111827' }}>
        Nuevo tratamiento
      </p>

      <div className="flex flex-wrap gap-3">
        <div className="flex-1 min-w-[14rem]">
          <label htmlFor={`${campo}-etiqueta`} className="block text-xs font-semibold mb-1" style={{ color: '#374151' }}>
            Nombre visible
          </label>
          <input
            id={`${campo}-etiqueta`}
            value={etiqueta}
            onChange={(e) => escribirEtiqueta(e.target.value)}
            placeholder="Carillas estéticas"
            className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3"
            style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
          />
        </div>

        <div className="flex-1 min-w-[14rem]">
          <label htmlFor={`${campo}-clave`} className="block text-xs font-semibold mb-1" style={{ color: '#374151' }}>
            Clave
          </label>
          {/* Se sugiere, no se impone: lo que se envía es lo que quede escrito. Si alguien
              lo sobrescribe con mayúsculas, el servidor devuelve su 400 y esa clave nunca
              entra a `citas.tratamiento` disfrazada de minúscula. */}
          <input
            id={`${campo}-clave`}
            value={clave}
            onChange={(e) => {
              setTocada(true)
              setClave(e.target.value)
            }}
            placeholder="carillas_esteticas"
            className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all focus:ring-3"
            style={{ backgroundColor: '#F9FAFB', border: '1px solid #E5E7EB', color: '#111827', fontFamily: SP }}
          />
          <p className="text-[11px] mt-1" style={{ color: '#9CA3AF' }}>
            Minúsculas, números y guion bajo, de 3 a 24. Es el texto que queda dentro de cada
            cita y en los argumentos que ve el modelo.
          </p>
        </div>
      </div>

      {/* Los dos avisos. Los dos son ciertos y callarlos sería mentir sobre lo que se acaba
          de hacer: el primero limita lo que el tratamiento nuevo puede hacer, el segundo
          dice qué va a contestar Daniela mañana si nadie llena las fichas. */}
      <div className="mt-4 rounded-xl px-4 py-3 flex flex-col gap-2" style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A' }}>
        <Aviso>
          Daniela ya podrá cotizarlo y agendarlo. Una radiografía sobre este tratamiento se
          seguirá clasificando como «no identificado» hasta que lo incorporemos al muro.
        </Aviso>
        <Aviso>
          Todavía no tiene fichas: Daniela dirá «SIN DATO DOCUMENTADO» y escalará. Llena
          precio, duración y profesional.
        </Aviso>
      </div>

      <div className="flex gap-2 mt-4">
        <button
          type="button"
          onClick={crear}
          disabled={creando || etiqueta.trim() === '' || clave.trim() === ''}
          className="px-4 py-2 rounded-lg text-sm font-bold disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95"
          style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}
        >
          {creando ? 'Creando…' : 'Crear y llenar sus fichas'}
        </button>
        <button
          type="button"
          onClick={() => setAbierto(false)}
          className="px-4 py-2 rounded-lg text-sm font-semibold border hover:bg-gray-50 transition-colors"
          style={{ borderColor: '#E5E7EB', color: '#374151' }}
        >
          Cancelar
        </button>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// La bitácora
// ------------------------------------------------------------------------------------------

/* `cambios === null` NO es «no hay cambios»: es «todavía no se ha podido leer». Colapsar
 * los dos estados hacía que un fallo de red se leyera como «aquí nunca ha pasado nada»
 * sobre la única estructura del sistema que existe para saber qué pasó de verdad. */
function Bitacora({ cambios, fallo }: { cambios: CambioFila[] | null; fallo: string | null }) {
  return (
    <div className="rounded-2xl overflow-hidden" style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}>
      <div className="px-5 py-3" style={{ borderBottom: '1px solid #F3F4F6' }}>
        <p className="text-sm font-semibold" style={{ color: '#111827' }}>
          Últimos cambios
        </p>
        <p className="text-xs" style={{ color: '#6B7280' }}>
          Cada edición se anota en la misma transacción que la escribe. Es lo único que
          permite reconstruir qué decía un precio antes y quién lo cambió.
        </p>
      </div>

      {fallo !== null ? (
        <div role="alert" className="px-5 py-4">
          <p className="text-xs font-semibold" style={{ color: '#991B1B' }}>
            No se pudo leer la bitácora. Esto no quiere decir que no haya cambios: quiere
            decir que no sabemos cuáles son.
          </p>
          <p className="text-xs mt-1" style={{ color: '#B91C1C' }}>
            {fallo}
          </p>
        </div>
      ) : cambios === null ? (
        <p className="px-5 py-4 text-xs" style={{ color: '#9CA3AF' }}>
          Leyendo la bitácora…
        </p>
      ) : cambios.length === 0 ? (
        <p className="px-5 py-4 text-xs" style={{ color: '#9CA3AF' }}>
          Todavía no hay cambios registrados.
        </p>
      ) : (
        <ul className="divide-y" style={{ borderColor: '#F3F4F6' }}>
          {cambios.map((c, i) => (
            <li key={i} className="px-5 py-3">
              <div className="flex items-center gap-2 flex-wrap">
                <code className="text-xs font-semibold" style={{ color: '#5B21B6' }}>
                  {c.clave}
                </code>
                <span className="text-[10px]" style={{ color: '#9CA3AF' }}>
                  {c.tabla}
                </span>
                <span className="text-[10px] ml-auto" style={{ color: '#9CA3AF' }}>
                  {c.usuario} · {cuando(c.cambiado_en)}
                </span>
              </div>
              <p className="text-xs mt-1 break-words" style={{ color: '#6B7280' }}>
                {c.valor_anterior === null ? (
                  <span style={{ color: '#059669' }}>nuevo: </span>
                ) : (
                  <>
                    <span className="line-through" style={{ color: '#D1D5DB' }}>
                      {c.valor_anterior}
                    </span>
                    {' → '}
                  </>
                )}
                {c.valor_nuevo}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// La pantalla
// ------------------------------------------------------------------------------------------

export default function Tratamientos({
  sesion,
  alCaducarSesion,
}: {
  sesion: Sesion
  /** Qué hacer cuando el servidor contesta 401 a una escritura. Es una llamada y no una
   *  excepción a propósito: estas funciones se invocan desde un `onClick` asíncrono, y una
   *  excepción lanzada ahí no la atrapa nadie -- React no tiene un `catch` río arriba para
   *  una promesa rechazada--. Acabaría en la consola del navegador, que es el único sitio
   *  donde la persona que está guardando un precio no va a mirar. */
  alCaducarSesion: () => void
}) {
  const [tratamientos, setTratamientos] = useState<TratamientoFila[]>([])
  const [fichas, setFichas] = useState<FichaFila[]>([])
  const [cambios, setCambios] = useState<CambioFila[] | null>(null)
  const [falloBitacora, setFalloBitacora] = useState<string | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pestana, setPestana] = useState<'tratamientos' | 'clinica'>('tratamientos')
  const [abierta, setAbierta] = useState<string | null>(null)
  const [nuevaEn, setNuevaEn] = useState<string | null>(null)
  const [verBitacora, setVerBitacora] = useState(false)

  const puedeFichas = sesion.rol === 'admin' || sesion.rol === 'doctor'
  const puedeTratamientos = sesion.rol === 'admin'
  const porQueNoFichas = 'Hace falta permiso de admin o doctor para editar fichas.'
  const porQueNoTratamientos = 'Hace falta permiso de admin para crear o cambiar tratamientos.'

  const recargar = useCallback(async () => {
    try {
      const [t, f] = await Promise.all([listarTratamientos(), listarFichas()])
      setTratamientos(t)
      setFichas(f)
      setError(null)
    } catch (err) {
      setError(mensajeDe(err))
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  // La bitácora se pide solo cuando alguien la abre: son cien filas que nadie mira la mayor
  // parte del tiempo, y se recarga cada vez para que refleje lo que se acaba de escribir.
  useEffect(() => {
    if (!verBitacora) return
    listarHistorial()
      .then((c) => {
        setCambios(c)
        setFalloBitacora(null)
      })
      .catch((err) => {
        // Se olvida lo que hubiera: enseñar una lista vieja junto a un fallo diría que eso
        // es todo lo que ha pasado, y puede no serlo.
        setCambios(null)
        setFalloBitacora(mensajeDe(err))
        setError(mensajeDe(err))
      })
  }, [verBitacora, fichas, tratamientos])

  /* Los conceptos que ya existen, sacados de la base y no de una lista escrita aquí. Los
     de `_general` entran también: «horario» o «medios_pago» son conceptos legítimos que
     alguien puede querer repetir en un tratamiento. */
  const conceptos = useMemo(
    () => Array.from(new Set(fichas.map((f) => f.concepto))).sort((a, b) => a.localeCompare(b, 'es')),
    [fichas],
  )

  const deLaClinica = useMemo(() => fichas.filter((f) => f.tratamiento === GENERAL), [fichas])

  const sinAprobar = fichas.filter((f) => !f.aprobado).length
  const sinPrecio = tratamientos.filter(
    (t) => t.clave !== CLASIFICACION && t.faltan.includes('precio'),
  ).length

  async function guardarUnaFicha(f: {
    tratamiento: string
    concepto: string
    contenido: string
    aprobado: boolean
    nota_pendiente: string | null
  }): Promise<boolean> {
    try {
      await guardarFicha(f)
      setError(null)
      await recargar()
      return true
    } catch (err) {
      if (err instanceof SesionCaducada) {
        alCaducarSesion()
        return false
      }
      setError(mensajeDe(err))
      return false
    }
  }

  /* Las tres escrituras devuelven si el servidor aceptó, en vez de propagar la excepción.
     Quien las llama necesita esa respuesta para decidir si cierra su formulario, y el
     mensaje ya está donde tiene que estar: en la región `role="alert"` de arriba.

     `SesionCaducada` aparte: un 401 no es «el servidor dijo que no» sino «ya no hay con
     quién hablar». Enseñarlo en la franja roja dejaría a alguien pulsando «Guardar» contra
     un servidor que ya no le contesta, así que avisa a `App.tsx` y la aplicación vuelve al
     ingreso. Se pierde lo que hubiera escrito en el formulario, y es inevitable: la sesión
     ya no existe y no hay dónde guardarlo. */
  async function cambiarUno(
    clave: string,
    cambio: { etiqueta?: string; activo?: boolean },
  ): Promise<boolean> {
    try {
      await cambiarTratamiento(clave, cambio)
      setError(null)
      await recargar()
      return true
    } catch (err) {
      if (err instanceof SesionCaducada) {
        alCaducarSesion()
        return false
      }
      setError(mensajeDe(err))
      return false
    }
  }

  async function crearUno(clave: string, etiqueta: string): Promise<boolean> {
    try {
      const creado = await crearTratamiento(clave, etiqueta)
      setError(null)
      await recargar()
      // Directo a llenar las fichas: es lo único que evita que el aviso de «SIN DATO
      // DOCUMENTADO» se cumpla mañana en una conversación real.
      setPestana('tratamientos')
      setAbierta(creado.clave)
      setNuevaEn(creado.clave)
      return true
    } catch (err) {
      if (err instanceof SesionCaducada) {
        alCaducarSesion()
        return false
      }
      setError(mensajeDe(err))
      return false
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-hidden" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
      <header className="shrink-0 px-8 py-4 border-b" style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-lg font-bold" style={{ color: '#111827' }}>
              Tratamientos
            </h1>
            <p className="text-sm" style={{ color: '#6B7280' }}>
              {cargando
                ? 'Leyendo la base…'
                : `${fichas.length} fichas · ${sinAprobar} sin aprobar · ${sinPrecio} tratamientos sin precio`}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setVerBitacora((v) => !v)}
              className="px-3 py-2 rounded-lg text-sm font-semibold border hover:bg-gray-50 transition-colors"
              style={{ borderColor: '#E5E7EB', color: '#374151' }}
            >
              {verBitacora ? 'Ocultar bitácora' : 'Ver bitácora'}
            </button>
            {puedeTratamientos && pestana === 'tratamientos' && <NuevoTratamiento alCrear={crearUno} />}
          </div>
        </div>

        <p className="text-xs mt-2" style={{ color: '#9CA3AF' }}>
          Lo que se guarda aquí sale por WhatsApp en el siguiente mensaje: Daniela lee esta
          tabla en cada turno, sin despliegue de por medio.
        </p>

        <div className="flex gap-1 mt-3">
          {([
            ['tratamientos', `Tratamientos (${tratamientos.length})`],
            ['clinica', `La clínica (${deLaClinica.length})`],
          ] as const).map(([id, texto]) => (
            <button
              key={id}
              type="button"
              onClick={() => setPestana(id)}
              aria-current={pestana === id ? 'true' : undefined}
              className="px-3 py-1.5 rounded-lg text-sm transition-colors"
              style={{
                backgroundColor: pestana === id ? '#F5F3FF' : 'transparent',
                color: pestana === id ? '#7C3AED' : '#6B7280',
                fontWeight: pestana === id ? 600 : 400,
              }}
            >
              {texto}
            </button>
          ))}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-8 py-6">
        <div className="max-w-4xl mx-auto flex flex-col gap-4">
          {error !== null && (
            <div role="alert" className="rounded-xl px-4 py-3" style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}>
              <p className="text-xs font-semibold mb-0.5" style={{ color: '#991B1B' }}>
                No se pudo
              </p>
              <p className="text-xs" style={{ color: '#B91C1C' }}>
                {error}
              </p>
            </div>
          )}

          {verBitacora && <Bitacora cambios={cambios} fallo={falloBitacora} />}

          {cargando ? (
            <p className="text-sm" style={{ color: '#9CA3AF' }}>
              Leyendo la base…
            </p>
          ) : pestana === 'tratamientos' ? (
            tratamientos.map((t) => (
              <FilaTratamiento
                key={t.clave}
                t={t}
                fichas={fichas.filter((f) => f.tratamiento === t.clave)}
                conceptos={conceptos}
                abierta={abierta === t.clave}
                alternar={() => setAbierta((a) => (a === t.clave ? null : t.clave))}
                puedeFichas={puedeFichas}
                puedeTratamientos={puedeTratamientos}
                porQueNoFichas={porQueNoFichas}
                porQueNoTratamientos={porQueNoTratamientos}
                forzarNueva={nuevaEn === t.clave}
                alCerrarNueva={() => setNuevaEn(null)}
                alGuardarFicha={guardarUnaFicha}
                alCambiar={cambiarUno}
              />
            ))
          ) : (
            <>
              {/* La pestaña se llama «La clínica» y en ninguna parte se escribe `_general`
                  como si fuera un tratamiento. Es la clave técnica de la fila, no un
                  servicio que alguien pueda agendar. */}
              <div className="rounded-2xl px-5 py-4" style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}>
                <p className="text-sm font-semibold mb-1" style={{ color: '#111827' }}>
                  La clínica
                </p>
                <p className="text-xs leading-relaxed" style={{ color: '#6B7280' }}>
                  Horario, sede, EPS, medios de pago, urgencias y la política de precios: lo
                  que Daniela responde cuando la pregunta no es sobre un tratamiento. No es un
                  servicio y nadie le agenda una cita; por eso vive aparte de la lista.
                </p>
              </div>

              {deLaClinica.map((f) => (
                <EditorFicha
                  key={f.concepto}
                  ficha={f}
                  puedeEditar={puedeFichas}
                  porQueNo={porQueNoFichas}
                  alGuardar={guardarUnaFicha}
                />
              ))}

              {nuevaEn === GENERAL ? (
                <NuevaFicha
                  tratamiento={GENERAL}
                  conceptos={conceptos}
                  existentes={deLaClinica}
                  puedeEditar={puedeFichas}
                  porQueNo={porQueNoFichas}
                  alGuardar={guardarUnaFicha}
                  alCerrar={() => setNuevaEn(null)}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => setNuevaEn(GENERAL)}
                  disabled={!puedeFichas}
                  title={puedeFichas ? undefined : porQueNoFichas}
                  className="self-start px-3 py-1.5 rounded-lg text-xs font-semibold border hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ borderColor: '#C4B5FD', color: '#7C3AED' }}
                >
                  + Añadir un hecho de la clínica
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

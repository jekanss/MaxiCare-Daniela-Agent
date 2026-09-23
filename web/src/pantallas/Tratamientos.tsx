import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
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
import { CargandoPantalla, Fallo } from '@/componentes/Estado'
import {
  AccionDeCabecera,
  BOTON,
  Buscador,
  CabeceraDePanel,
  CAMPO,
  Chip,
  ESTILO_CAMPO,
  MarcoDeDosPaneles,
  MONO,
  Panel,
  Pastilla,
  Rotulo,
  SG,
  TituloDePanel,
  Vacio,
  Volver,
} from '@/componentes/Panel'
import { ANCHO_DOS_PANELES, usarEsAngosto } from '@/medidas'

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
 * La forma: lista a la izquierda, trabajo a la derecha (23/09/2026)
 * ------------------------------------------------------------------------------------
 *
 * Hasta esa fecha era UNA columna con quince acordeones, y las tres cosas que esta pantalla
 * enseña se empujaban entre sí: abrir «ortodoncia» metía sus cinco editores de ficha DENTRO
 * de la lista y mandaba el resto de los tratamientos fuera de la ventana; «Ver bitácora»
 * insertaba cien filas entre la cabecera y la lista; «Nuevo tratamiento» crecía dentro de la
 * propia cabecera. Cada acción movía de sitio a las otras dos.
 *
 * Ahora es el mismo layout de `Conversaciones` y `SinResolver`, con sus piezas reutilizadas
 * de `componentes/Panel.tsx` -- no copiadas: dos copias de un borde se separan en silencio, y
 * ese es el motivo por el que ese archivo existe--. La lista es el índice y no se mueve; el
 * panel de la derecha es donde se trabaja, y las CUATRO cosas que se pueden estar haciendo
 * --un tratamiento, los hechos de la clínica, la bitácora, crear uno nuevo-- son
 * SELECCIONES de esa lista. Por eso `seleccion` es una sola cadena y no cuatro banderas:
 * abrir una cierra la anterior por construcción, que es justo lo que antes no pasaba.
 *
 * Y la lista trae lo que no tenía: un buscador y cuatro filtros. El contador de la cabecera
 * vieja decía «2 tratamientos sin precio» y ahí se acababa -- para saber CUÁLES había que ir
 * abriendo acordeones--. Ese número es ahora el `Chip` que los deja solos en la lista, que es
 * el trabajo principal de la pantalla (ver el punto 2 de aquí abajo).
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
 *    invitaría a agendarle una cita.
 *
 *    Hasta el 23/09/2026 eso se resolvía con una pestaña aparte, «La clínica». Sigue sin
 *    ser un tratamiento y sigue sin enseñar nunca su clave, pero ya no está escondido
 *    detrás de una pestaña que hay que saber que existe: es una entrada con nombre propio
 *    en su PROPIO GRUPO de la lista, encima del grupo «Tratamientos» y separado por un
 *    rótulo. Lo que la decisión prohibía era que se leyera como un servicio agendable, no
 *    que se pudiera alcanzar; y doce fichas con el horario, la sede y los medios de pago
 *    son de lo más consultado que hay aquí. Por eso los filtros y el buscador recortan el
 *    grupo de tratamientos y NUNCA este: filtrar «sin precio» no puede hacer desaparecer
 *    algo a lo que nadie le pone precio.
 *
 * ------------------------------------------------------------------------------------
 * Los permisos
 * ------------------------------------------------------------------------------------
 *
 * Los botones se deshabilitan según el rol, con el porqué en el `title`. Eso es comodidad,
 * no control: `exigir_rol` en el servidor devuelve 403 igual, y ese 403 se enseña tal cual
 * viene. Un botón escondido nunca ha protegido una tabla. */

const GENERAL = '_general'

/* `no_identificado` es una fila de `tratamientos` como las otras catorce, pero no es un
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

/* Las dos entradas de la lista que no son un tratamiento ni `_general`, y el formulario de
 * crear uno. Van como valores de `seleccion` --y no como banderas sueltas-- porque son lo que
 * ocupa el panel de la derecha, igual que un tratamiento: con banderas, abrir la bitácora
 * mientras hay una ficha a medias dejaría las dos cosas pintadas a la vez.
 *
 * El doble guion bajo no puede chocar con ninguna clave real: `panel.crear_tratamiento` las
 * valida contra `[a-z0-9_]{3,24}` empezando por letra. Mismo truco que el `OTRO` de
 * `NuevaFicha`, que lleva aquí desde la fase 8. */
const BITACORA = '__bitacora__'
const NUEVO = '__nuevo__'

type Filtro = 'todos' | 'sin_precio' | 'sin_aprobar' | 'inactivos'

/** Los cuatro filtros de la lista, con lo que cada uno responde.
 *
 *  No son categorías simétricas y no tienen por qué serlo: son las cuatro preguntas que
 *  alguien trae cuando abre esta pantalla. «Sin precio» es la que más duele --Daniela dice
 *  «SIN DATO DOCUMENTADO» y escala-- y por eso va primera. */
const CHIPS: { id: Filtro; etiqueta: string }[] = [
  { id: 'todos', etiqueta: 'Todos' },
  { id: 'sin_precio', etiqueta: 'Sin precio' },
  { id: 'sin_aprobar', etiqueta: 'Sin aprobar' },
  { id: 'inactivos', etiqueta: 'Inactivos' },
]

/** Lo que le falta a un tratamiento, resuelto en un sitio y no en cuatro.
 *
 *  `no_identificado` queda fuera de todo lo que sea «le falta algo»: es donde cae lo que
 *  Daniela no pudo clasificar, no un servicio, y nadie le va a poner precio. Contarlo daría
 *  «3 sin precio» cuando los que de verdad le faltan a la clínica son dos, y mandaría a
 *  alguien a inventarle una tarifa a una categoría de error. */
function estado(t: TratamientoFila, sinAprobar: number) {
  const esClasificacion = t.clave === CLASIFICACION
  return {
    esClasificacion,
    incompleto: t.faltan.length > 0 && !esClasificacion,
    sinPrecio: t.activo && !esClasificacion && t.faltan.includes('precio'),
    sinAprobar,
  }
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

/** Le da a un `textarea` el alto de lo que tiene dentro.
 *
 *  Aquí había `rows={Math.min(10, Math.max(3, contenido.split('\n').length + 1))}`, que
 *  cuenta SALTOS DE LÍNEA y por tanto no sabe nada del ancho. Las fichas de esta clínica no
 *  llevan saltos --son una frase larga-- así que todas salían con tres renglones: en un móvil
 *  de 360 px el precio de la ortodoncia ocupa seis, y lo que se veía era el texto cortado por
 *  la mitad dentro de una caja con su propio scroll. Se comprobó con una captura a 360 px; el
 *  ancho es justo la variable que el cálculo viejo no podía ver, y por eso también se
 *  recalcula al cambiar el tamaño de la ventana. */
function usarAltoDelContenido(texto: string) {
  const caja = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => {
    const el = caja.current
    if (!el) return
    const ajustar = () => {
      el.style.height = 'auto'
      // 340 px de tope: pasado eso, un scroll dentro del campo es mejor que una ficha que se
      // come la pantalla y esconde las otras cuatro del tratamiento.
      el.style.height = `${Math.min(340, el.scrollHeight + 2)}px`
    }
    ajustar()
    window.addEventListener('resize', ajustar)
    return () => window.removeEventListener('resize', ajustar)
  }, [texto])
  return caja
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

/* Aquí había una `Insignia` propia --una píldora redonda de 10 px-- y se fue el 23/09/2026 a
 * favor de la `Pastilla` de `componentes/Panel.tsx`, que es la que usan Conversaciones y Sin
 * resolver. No era solo estética: «Esperando» allí y «sin aprobar» aquí son lo mismo para
 * quien mira --un estado que hay que atender-- y con dos formas distintas la clínica aprende
 * dos códigos para una sola idea. */

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
              className="text-xs font-semibold px-3 transition-all disabled:cursor-not-allowed disabled:opacity-50"
              style={{
                backgroundColor: puesta ? o.fondo : '#FFFFFF',
                color: puesta ? o.color : '#6E6880',
                border: `1.5px solid ${puesta ? o.color : '#DCD8E6'}`,
                // 36 px, como los `Chip` de la cabecera: se pulsa con el dedo en la tablet
                // de recepción, y esto decide si un precio sale con advertencia o sin ella.
                minHeight: '36px',
              }}
            >
              {o.texto}
            </button>
          )
        })}
      </div>
      <p className="text-[11px] mt-1.5 leading-relaxed" style={{ color: '#6E6880' }}>
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
  const alto = usarAltoDelContenido(contenido)

  /* Sin resincronizar con la prop cuando la lista se recarga: después de guardar, `ficha`
     vuelve con lo que se acaba de escribir y `sucio` se apaga solo. Resincronizar borraría
     lo que alguien tenga a medias en OTRA ficha de la misma pantalla. */
  const sucio =
    contenido !== ficha.contenido ||
    aprobado !== ficha.aprobado ||
    nota.trim() !== (ficha.nota_pendiente ?? '')

  const vacio = contenido.trim() === ''

  /* Aprobar vacía la nota. El campo se esconde al aprobar, pero escondido no es vaciado: sin
     esto se enviaba lo que hubiera dentro y quedaba una ficha aprobada con un «falta
     confirmarlo con la doctora» invisible, que reaparece el día que alguien la desapruebe.
     `panel.guardar_ficha` hace lo mismo en el servidor, que es donde no se puede esquivar;
     aquí sirve para que `sucio` y lo que se ve concuerden con lo que se va a guardar. */
  function aprobar(v: boolean) {
    setAprobado(v)
    if (v) setNota('')
  }

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
      className="p-4"
      style={{
        backgroundColor: '#FFFFFF',
        border: `1px solid ${aprobado ? '#DCD8E6' : '#FDE68A'}`,
      }}
    >
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <p
          className="text-sm"
          style={{ fontFamily: SG, fontWeight: 600, letterSpacing: '-0.018em', color: '#16111F' }}
        >
          {ficha.concepto.replace(/_/g, ' ')}
        </p>
        {!aprobado && <Pastilla texto="sin aprobar" fondo="#FEF3C7" tinta="#92400E" />}
        {sucio && <Pastilla texto="sin guardar" fondo="#EDE9FE" tinta="#5B21B6" />}
        <span
          className="ml-auto"
          title="Última edición"
          style={{ fontFamily: MONO, fontSize: '10px', color: '#9C95AD', whiteSpace: 'nowrap' }}
        >
          {cuando(ficha.actualizado_en)}
        </span>
      </div>

      <textarea
        ref={alto}
        value={contenido}
        onChange={(e) => setContenido(e.target.value)}
        disabled={!puedeEditar}
        rows={3}
        aria-label={`Contenido de ${ficha.concepto}`}
        className={CAMPO}
        style={ESTILO_CAMPO}
      />

      <div className="flex flex-wrap items-start justify-between gap-4 mt-3">
        <Aprobacion aprobado={aprobado} cambiar={aprobar} puedeEditar={puedeEditar} porQueNo={porQueNo} />

        <button
          type="button"
          onClick={guardar}
          disabled={!puedeEditar || !sucio || guardando || vacio}
          title={
            !puedeEditar ? porQueNo
            : vacio ? 'Una ficha vacía no es lo mismo que una ficha sin datos. Si MaxiCare no tiene el dato, déjala sin crear: Daniela dirá «SIN DATO DOCUMENTADO» y escalará.'
            : undefined
          }
          className={`py-2 ${BOTON}`}
          style={{ backgroundColor: '#7C3AED', color: '#FFFFFF' }}
        >
          {guardando ? 'Guardando…' : 'Guardar'}
        </button>
      </div>

      {!aprobado && (
        <div className="mt-3">
          <label htmlFor={`${campo}-nota`} className="block text-xs font-semibold mb-1" style={{ color: '#4A4458' }}>
            Qué falta por definir
          </label>
          <input
            id={`${campo}-nota`}
            value={nota}
            onChange={(e) => setNota(e.target.value)}
            disabled={!puedeEditar}
            placeholder="Falta confirmarlo con la doctora"
            className={CAMPO}
            style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A', color: '#16111F', fontFamily: SG }}
          />
          <p className="text-[11px] mt-1" style={{ color: '#6E6880' }}>
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
  const alto = usarAltoDelContenido(contenido)

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

  /* Igual que en `EditorFicha`: el campo de la nota se esconde al aprobar, así que también
     se vacía. Lo escondido que igual se envía es la peor clase de dato. */
  function aprobar(v: boolean) {
    setAprobado(v)
    if (v) setNota('')
  }

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
    <div className="p-4" style={{ backgroundColor: '#FFFFFF', border: '1px dashed #C4B5FD' }}>
      <p
        className="text-sm mb-3"
        style={{ fontFamily: SG, fontWeight: 600, letterSpacing: '-0.018em', color: '#16111F' }}
      >
        Nueva ficha
      </p>

      <label htmlFor={`${campo}-concepto`} className="block text-xs font-semibold mb-1" style={{ color: '#4A4458' }}>
        Concepto
      </label>
      <select
        id={`${campo}-concepto`}
        value={elegido}
        onChange={(e) => setElegido(e.target.value)}
        disabled={!puedeEditar}
        className={CAMPO}
        style={ESTILO_CAMPO}
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
            className={CAMPO}
            style={ESTILO_CAMPO}
          />
          <p className="text-[11px] mt-1 leading-relaxed" style={{ color: '#6E6880' }}>
            Un concepto nuevo no se pierde: si Daniela pregunta por uno que no existe, la tool
            le devuelve todos los del tratamiento. Pero un «precios» junto a un «precio» deja
            dos precios conviviendo.
          </p>
        </div>
      )}

      {/* El aviso vecino de arriba es el de duplicar. Este es el de destruir, y por eso va
          en ámbar, con el texto que se perdería delante. */}
      {chocaCon !== null && (
        <div role="alert" className="mt-2 px-3 py-2.5" style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A' }}>
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

      <label htmlFor={`${campo}-contenido`} className="block text-xs font-semibold mb-1 mt-3" style={{ color: '#4A4458' }}>
        Contenido
      </label>
      <textarea
        id={`${campo}-contenido`}
        ref={alto}
        value={contenido}
        onChange={(e) => setContenido(e.target.value)}
        disabled={!puedeEditar}
        rows={3}
        placeholder="Desde $1.200.000 por unidad. Incluye la valoración inicial."
        className={CAMPO}
        style={ESTILO_CAMPO}
      />

      <div className="mt-3">
        <Aprobacion aprobado={aprobado} cambiar={aprobar} puedeEditar={puedeEditar} porQueNo={porQueNo} />
      </div>

      {!aprobado && (
        <input
          value={nota}
          onChange={(e) => setNota(e.target.value)}
          disabled={!puedeEditar}
          placeholder="Qué falta por definir"
          aria-label="Qué falta por definir"
          className={`mt-3 ${CAMPO}`}
          style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A', color: '#16111F', fontFamily: SG }}
        />
      )}

      <div className="flex gap-2 mt-4">
        <button
          type="button"
          onClick={guardar}
          disabled={!puedeEditar || !listo || guardando}
          title={puedeEditar ? undefined : porQueNo}
          className={`py-2 ${BOTON}`}
          style={{ backgroundColor: chocaCon !== null ? '#D97706' : '#7C3AED', color: '#FFFFFF' }}
        >
          {guardando ? 'Guardando…' : chocaCon !== null ? 'Reemplazar la ficha existente' : 'Crear ficha'}
        </button>
        <button
          type="button"
          onClick={alCerrar}
          className="px-4 py-2 text-sm font-semibold border transition-colors hover:brightness-95"
          style={{ borderColor: '#DCD8E6', backgroundColor: '#FFFFFF', color: '#4A4458' }}
        >
          Cancelar
        </button>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------------------------------
// La lista de la izquierda
// ------------------------------------------------------------------------------------------

/** El rótulo que separa los tres grupos de la lista: «La clínica», «Tratamientos»,
 *  «Registro».
 *
 *  Es lo que sostiene la decisión 3 de la cabecera de este archivo. `_general` deja de estar
 *  escondido detrás de una pestaña sin convertirse en un tratamiento más: está en OTRO grupo,
 *  con otro rótulo, y su clave no se pinta en ninguna parte. Sin estos rótulos sería una fila
 *  al lado de las otras quince, que es exactamente lo que la decisión prohíbe. */
function RotuloDeGrupo({ children }: { children: React.ReactNode }) {
  return (
    <li
      style={{
        backgroundColor: '#FBFAFD',
        borderBottom: '1px solid #ECE8F4',
        padding: '8px 16px',
        fontFamily: MONO,
        fontSize: '10px',
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: '#6E6880',
      }}
    >
      {children}
    </li>
  )
}

/** El esqueleto de una fila de la lista: el borde izquierdo violeta cuando está elegida y la
 *  misma altura mínima de 44 px que en Conversaciones. Lo comparten los tratamientos y las
 *  dos entradas fijas, y no se escribe dos veces por lo de siempre: dos copias de un borde
 *  se separan en silencio. */
function FilaBase({
  activa,
  alAbrir,
  children,
}: {
  activa: boolean
  alAbrir: () => void
  children: React.ReactNode
}) {
  return (
    <li style={{ borderBottom: '1px solid #ECE8F4' }}>
      <button
        type="button"
        onClick={alAbrir}
        className="flex w-full flex-col gap-1.5 text-left"
        style={{
          borderLeft: `3px solid ${activa ? '#6D28D9' : 'transparent'}`,
          // Sin fondo en línea cuando NO está activa: un `transparent` inline le gana a la
          // clase de hover y el hover dejaría de verse sin que nada falle. Mismo motivo que
          // en `Conversaciones.FilaDeLaLista`.
          backgroundColor: activa ? '#F4F1F9' : undefined,
          padding: '13px 16px',
          minHeight: '44px',
          cursor: 'pointer',
        }}
      >
        {children}
      </button>
    </li>
  )
}

/** Una de las dos entradas que no son un tratamiento: «La clínica» y «Últimos cambios».
 *
 *  Llevan una frase debajo del nombre y ninguna pastilla, y eso las distingue de un
 *  tratamiento de un vistazo: lo que tiene estado es un servicio que la clínica cotiza. */
function FilaFija({
  titulo,
  detalle,
  activa,
  alAbrir,
}: {
  titulo: string
  detalle: string
  activa: boolean
  alAbrir: () => void
}) {
  return (
    <FilaBase activa={activa} alAbrir={alAbrir}>
      <span
        className="truncate"
        style={{ fontFamily: SG, fontSize: '14.5px', fontWeight: 500, letterSpacing: '-0.01em', color: '#16111F' }}
      >
        {titulo}
      </span>
      <span style={{ fontSize: '12.5px', fontWeight: 300, lineHeight: 1.4, color: '#6E6880' }}>
        {detalle}
      </span>
    </FilaBase>
  )
}

/** Un tratamiento en la lista.
 *
 *  Lo que se ve sin abrir nada es lo que esta pantalla existe para que se vea (decisión 2 de
 *  la cabecera): cuántas fichas tiene y qué le falta. La pastilla de «falta precio» va con
 *  las palabras enteras y no con un punto de color: «falta precio, duración» se entiende sin
 *  haber aprendido antes qué significa el rojo.
 *
 *  `no_identificado` sale sin marca de falta y con su propia pastilla, por lo que dice la
 *  constante `CLASIFICACION`: no es un servicio y nadie le va a poner precio. */
function FilaDeLaLista({
  t,
  sinAprobar,
  activa,
  alAbrir,
}: {
  t: TratamientoFila
  /** Cuántas de sus fichas están sin aprobar. Se calcula en la pantalla, que es quien tiene
   *  las fichas: la fila recibe el número ya hecho para no filtrar la lista entera por cada
   *  una de las quince. */
  sinAprobar: number
  activa: boolean
  alAbrir: () => void
}) {
  const e = estado(t, sinAprobar)
  return (
    <FilaBase activa={activa} alAbrir={alAbrir}>
      <span className="flex w-full items-baseline justify-between gap-2">
        <span
          className="truncate"
          style={{
            fontFamily: SG,
            fontSize: '14.5px',
            fontWeight: 500,
            letterSpacing: '-0.01em',
            color: t.activo ? '#16111F' : '#6E6880',
          }}
        >
          {t.etiqueta}
        </span>
        <span style={{ fontFamily: MONO, fontSize: '10.5px', color: '#6E6880', flex: 'none' }}>
          {t.fichas === 0 ? 'SIN FICHAS' : t.fichas === 1 ? '1 FICHA' : `${t.fichas} FICHAS`}
        </span>
      </span>

      <span className="truncate" style={{ fontFamily: MONO, fontSize: '11px', color: '#4C1D95' }}>
        {t.clave}
      </span>

      <span className="flex w-full flex-wrap items-center gap-1.5" style={{ paddingTop: '2px' }}>
        {e.esClasificacion ? (
          <Pastilla
            texto="no es un servicio"
            fondo="#F7F6FA"
            tinta="#6E6880"
            titulo="Aquí cae lo que Daniela no pudo clasificar. No se cotiza ni se agenda, y no necesita precio."
          />
        ) : e.incompleto ? (
          <Pastilla
            texto={`falta ${t.faltan.map((c) => NOMBRE_CONCEPTO[c] ?? c).join(', ')}`}
            fondo="#FEF2F2"
            tinta="#B91C1C"
            titulo="Daniela dirá «SIN DATO DOCUMENTADO» y escalará cada vez que alguien pregunte por esto."
          />
        ) : (
          <Pastilla texto="completo" fondo="#ECFDF5" tinta="#065F46" titulo="Tiene precio, duración y profesional." />
        )}
        {!t.activo && (
          <Pastilla
            texto="inactivo"
            fondo="#F7F6FA"
            tinta="#6E6880"
            titulo="Daniela no lo ofrece ni lo agenda. Las citas que ya existen siguen legibles."
          />
        )}
        {sinAprobar > 0 && (
          <Pastilla
            texto={`${sinAprobar} sin aprobar`}
            fondo="#FEF3C7"
            tinta="#92400E"
            titulo="Daniela las usa igual, avisando que faltan por definir."
          />
        )}
      </span>
    </FilaBase>
  )
}

// ------------------------------------------------------------------------------------------
// El detalle de la derecha
// ------------------------------------------------------------------------------------------

/** La cabecera del panel de detalle: quién es, y qué se le puede hacer.
 *
 *  Las acciones van aquí arriba y no al final de la lista de fichas, que es donde estaban
 *  cuando esto era un acordeón: con cinco fichas de por medio, «Renombrar» y «Desactivar»
 *  quedaban a una pantalla de scroll del nombre al que se refieren. */
function CabeceraDeDetalle({
  titulo,
  clave,
  alVolver,
  pastillas,
  acciones,
}: {
  titulo: string
  /** La clave técnica, en versalitas debajo del nombre. `null` en «La clínica» y en la
   *  bitácora: `_general` no se enseña nunca, y las otras dos no tienen ninguna. */
  clave: string | null
  alVolver: () => void
  pastillas?: React.ReactNode
  acciones?: React.ReactNode
}) {
  return (
    <div
      className="flex shrink-0 flex-wrap items-center gap-2 sm:gap-3"
      style={{ padding: '14px 16px', borderBottom: '1px solid #ECE8F4' }}
    >
      {/* La salida cuando solo cabe un panel. Se esconde sola en escritorio, donde la lista
          sigue estando al lado. */}
      <Volver alPulsar={alVolver} que="Lista" />
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span
          className="truncate"
          style={{ fontFamily: SG, fontWeight: 600, fontSize: '17px', letterSpacing: '-0.022em', color: '#16111F' }}
        >
          {titulo}
        </span>
        {clave !== null && (
          <span className="truncate" style={{ fontFamily: MONO, fontSize: '11.5px', color: '#4C1D95' }}>
            {clave}
          </span>
        )}
      </span>
      {/* Las acciones ocupan la fila entera hasta que de verdad quepan al lado del nombre. El
          corte es `xl` y no `sm` por lo mismo que en el hilo de Conversaciones: lo que manda
          es el ancho del PANEL --dos tercios de lo que sobra tras el menú--, así que a 1024 px
          de ventana esto mide unos 400 y el nombre saldría cortado. */}
      {(pastillas || acciones) && (
        <span className="flex w-full flex-wrap items-center gap-2 xl:w-auto">
          {pastillas}
          {acciones}
        </span>
      )}
    </div>
  )
}

/** La caja con scroll propio donde va el contenido de cualquiera de los cuatro detalles.
 *
 *  El scroll es de ESTA caja y no del panel entero: la cabecera con el nombre y las acciones
 *  se queda puesta mientras se baja por las fichas. Cuando esto era un acordeón, bajar a la
 *  quinta ficha dejaba fuera de la pantalla el nombre del tratamiento que se estaba
 *  editando. */
function CuerpoDeDetalle({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
      style={{ backgroundColor: '#F7F6FA', padding: '16px' }}
    >
      {children}
    </div>
  )
}

/** Lo que se puede hacer con UN tratamiento: leer y editar sus fichas, añadir una, cambiarle
 *  el nombre visible y activarlo o desactivarlo. */
function DetalleTratamiento({
  t,
  fichas,
  conceptos,
  puedeFichas,
  puedeTratamientos,
  porQueNoFichas,
  porQueNoTratamientos,
  forzarNueva,
  alCerrarNueva,
  alGuardarFicha,
  alCambiar,
  alVolver,
}: {
  t: TratamientoFila
  fichas: FichaFila[]
  conceptos: string[]
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
  alVolver: () => void
}) {
  const [agregando, setAgregando] = useState(false)
  const [renombrando, setRenombrando] = useState(false)
  const [nombre, setNombre] = useState(t.etiqueta)

  /* Los dos formularios se cierran al cambiar de tratamiento. Sin esto, abrir «ortodoncia»
     con el renombrador puesto en «implantes» dejaría un campo con el nombre del anterior
     encima del nuevo: el error que eso produce --renombrar al que no era-- no lo deshace
     ningún botón, solo otro renombrado. */
  useEffect(() => {
    setAgregando(false)
    setRenombrando(false)
    setNombre(t.etiqueta)
  }, [t.clave, t.etiqueta])

  const e = estado(t, 0)
  const mostrarNueva = agregando || forzarNueva
  const sinFichas = t.fichas === 0 && !e.esClasificacion

  return (
    <>
      <CabeceraDeDetalle
        titulo={t.etiqueta}
        clave={t.clave}
        alVolver={alVolver}
        pastillas={
          <>
            {!t.activo && <Pastilla texto="inactivo" fondo="#F7F6FA" tinta="#6E6880" />}
            {!t.en_el_muro && (
              <Pastilla
                texto="fuera del muro"
                fondo="#EEF2FF"
                tinta="#3730A3"
                titulo="Se puede cotizar y agendar, pero una radiografía sobre él se clasifica como «no identificado» hasta que se incorpore al muro con un cambio de código."
              />
            )}
          </>
        }
        acciones={
          <>
            <AccionDeCabecera
              alPulsar={() => setAgregando(true)}
              ocupado={!puedeFichas || mostrarNueva}
              titulo={puedeFichas ? undefined : porQueNoFichas}
            >
              + Ficha
            </AccionDeCabecera>
            <AccionDeCabecera
              alPulsar={() => {
                setNombre(t.etiqueta)
                setRenombrando(true)
              }}
              ocupado={!puedeTratamientos || renombrando}
              titulo={puedeTratamientos ? undefined : porQueNoTratamientos}
            >
              Renombrar
            </AccionDeCabecera>
            {/* Desactivar, nunca borrar: borrar dejaría las citas históricas apuntando a una
                clave que ya no existe. En rojo solo al apagar, que es el lado que quita algo. */}
            <AccionDeCabecera
              peligro={t.activo}
              alPulsar={() => void alCambiar(t.clave, { activo: !t.activo })}
              ocupado={!puedeTratamientos}
              titulo={
                puedeTratamientos
                  ? t.activo
                    ? 'Daniela deja de ofrecerlo y de agendarlo. No se borra nada: las citas que ya existen siguen legibles.'
                    : 'Vuelve al vocabulario que Daniela puede ofrecer y agendar.'
                  : porQueNoTratamientos
              }
            >
              {t.activo ? 'Desactivar' : 'Reactivar'}
            </AccionDeCabecera>
          </>
        }
      />

      <CuerpoDeDetalle>
        {renombrando && (
          <div
            className="flex flex-wrap items-center gap-2 p-4"
            style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6' }}
          >
            <input
              value={nombre}
              onChange={(ev) => setNombre(ev.target.value)}
              aria-label="Nombre visible del tratamiento"
              className={`flex-1 min-w-[12rem] ${CAMPO}`}
              style={ESTILO_CAMPO}
            />
            <button
              type="button"
              onClick={async () => {
                if (await alCambiar(t.clave, { etiqueta: nombre })) setRenombrando(false)
              }}
              disabled={nombre.trim() === '' || nombre.trim() === t.etiqueta}
              className={`py-2 ${BOTON}`}
              style={{ backgroundColor: '#6D28D9', color: '#FFFFFF' }}
            >
              Guardar nombre
            </button>
            <button
              type="button"
              onClick={() => setRenombrando(false)}
              className="px-4 py-2 text-sm font-semibold border transition-colors hover:brightness-95"
              style={{ borderColor: '#DCD8E6', backgroundColor: '#FFFFFF', color: '#4A4458' }}
            >
              Cancelar
            </button>
            <p className="w-full text-[11px]" style={{ color: '#6E6880' }}>
              Cambia el nombre visible, nunca la clave <code>{t.clave}</code>: esa es la que
              está escrita dentro de las citas que ya existen.
            </p>
          </div>
        )}

        {sinFichas && (
          <div
            className="px-4 py-3"
            style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}
          >
            <p className="text-xs leading-relaxed" style={{ color: '#991B1B' }}>
              Este tratamiento no tiene ni una ficha. Daniela dirá «SIN DATO DOCUMENTADO» y
              escalará cada vez que alguien pregunte por él.
            </p>
          </div>
        )}

        {/* Lo que falta se dice también aquí, y no solo en la pastilla de la lista: quien
            llega a este panel desde el filtro «Sin precio» ya no tiene la lista delante en un
            móvil, y «qué le falta exactamente» es justo lo que viene a hacer. */}
        {e.incompleto && !sinFichas && (
          <div
            className="px-4 py-3"
            style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A' }}
          >
            <Aviso>
              Le falta {t.faltan.map((c) => NOMBRE_CONCEPTO[c] ?? c).join(', ')}. Mientras no
              esté, Daniela contesta «SIN DATO DOCUMENTADO» a esa pregunta y escala.
            </Aviso>
          </div>
        )}

        {/* El formulario va ARRIBA de las fichas que ya existen, y no al final como estaría
            en una lista de «añadir». El botón que lo abre está en la cabecera: puesto abajo,
            pulsar «+ Ficha» en un tratamiento con cinco fichas no cambiaba nada de lo que se
            veía --el formulario nacía a dos pantallas de scroll-- y se lee como un botón
            roto. Se vio en una captura a 320 px, no razonándolo. */}
        {mostrarNueva && (
          <NuevaFicha
            tratamiento={t.clave}
            conceptos={conceptos}
            existentes={fichas}
            puedeEditar={puedeFichas}
            porQueNo={porQueNoFichas}
            alGuardar={alGuardarFicha}
            alCerrar={() => {
              setAgregando(false)
              alCerrarNueva()
            }}
          />
        )}

        {fichas.map((f) => (
          <EditorFicha
            key={`${f.tratamiento}/${f.concepto}`}
            ficha={f}
            puedeEditar={puedeFichas}
            porQueNo={porQueNoFichas}
            alGuardar={alGuardarFicha}
          />
        ))}
      </CuerpoDeDetalle>
    </>
  )
}


// ------------------------------------------------------------------------------------------
// Crear un tratamiento
// ------------------------------------------------------------------------------------------

/** El formulario de crear, que ocupa el panel de detalle como uno más.
 *
 *  Antes era un botón de la cabecera que desplegaba el formulario DENTRO de la cabecera: la
 *  barra de arriba crecía cuatro centímetros y empujaba la lista hacia abajo cada vez que
 *  alguien pulsaba «+ Nuevo». Ahora es una selección como las otras tres, así que abrirlo no
 *  mueve nada de sitio y cerrarlo es volver a lo que estuviera elegido antes.
 *
 *  Quién puede verlo lo decide la pantalla, que es la que tiene el rol. */
function NuevoTratamiento({
  alCrear,
  alVolver,
  alCancelar,
}: {
  alCrear: (clave: string, etiqueta: string) => Promise<boolean>
  alVolver: () => void
  alCancelar: () => void
}) {
  const campo = useId()
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
    } finally {
      setCreando(false)
    }
  }

  return (
    <>
      <CabeceraDeDetalle titulo="Nuevo tratamiento" clave={null} alVolver={alVolver} />

      <CuerpoDeDetalle>
        <div className="p-5" style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6' }}>
          <div className="flex flex-wrap gap-3">
            <div className="flex-1 min-w-[14rem]">
              <label htmlFor={`${campo}-etiqueta`} className="block text-xs font-semibold mb-1" style={{ color: '#4A4458' }}>
                Nombre visible
              </label>
              <input
                id={`${campo}-etiqueta`}
                value={etiqueta}
                onChange={(e) => escribirEtiqueta(e.target.value)}
                placeholder="Carillas estéticas"
                className={CAMPO}
                style={ESTILO_CAMPO}
              />
            </div>

            <div className="flex-1 min-w-[14rem]">
              <label htmlFor={`${campo}-clave`} className="block text-xs font-semibold mb-1" style={{ color: '#4A4458' }}>
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
                className={CAMPO}
                style={ESTILO_CAMPO}
              />
              <p className="text-[11px] mt-1" style={{ color: '#6E6880' }}>
                Minúsculas, números y guion bajo, de 3 a 24. Es el texto que queda dentro de
                cada cita y en los argumentos que ve el modelo.
              </p>
            </div>
          </div>

          {/* Los dos avisos. Los dos son ciertos y callarlos sería mentir sobre lo que se
              acaba de hacer: el primero limita lo que el tratamiento nuevo puede hacer, el
              segundo dice qué va a contestar Daniela mañana si nadie llena las fichas. */}
          <div className="mt-4 px-4 py-3 flex flex-col gap-2" style={{ backgroundColor: '#FFFBEB', border: '1px solid #FDE68A' }}>
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
              className={`py-2 ${BOTON}`}
              style={{ backgroundColor: '#6D28D9', color: '#FFFFFF' }}
            >
              {creando ? 'Creando…' : 'Crear y llenar sus fichas'}
            </button>
            <button
              type="button"
              onClick={alCancelar}
              className="px-4 py-2 text-sm font-semibold border transition-colors hover:brightness-95"
              style={{ borderColor: '#DCD8E6', backgroundColor: '#FFFFFF', color: '#4A4458' }}
            >
              Cancelar
            </button>
          </div>
        </div>
      </CuerpoDeDetalle>
    </>
  )
}

// ------------------------------------------------------------------------------------------
// La bitácora
// ------------------------------------------------------------------------------------------

/* `cambios === null` NO es «no hay cambios»: es «todavía no se ha podido leer». Colapsar
 * los dos estados hacía que un fallo de red se leyera como «aquí nunca ha pasado nada»
 * sobre la única estructura del sistema que existe para saber qué pasó de verdad. */
function Bitacora({
  cambios,
  fallo,
  alVolver,
}: {
  cambios: CambioFila[] | null
  fallo: string | null
  alVolver: () => void
}) {
  return (
    <>
      <CabeceraDeDetalle titulo="Últimos cambios" clave={null} alVolver={alVolver} />

      <div
        className="shrink-0"
        style={{ backgroundColor: '#FFFFFF', padding: '12px 16px', borderBottom: '1px solid #ECE8F4' }}
      >
        <p className="text-xs leading-relaxed" style={{ color: '#6E6880' }}>
          Cada edición se anota en la misma transacción que la escribe. Es lo único que
          permite reconstruir qué decía un precio antes y quién lo cambió. Las marcas de
          asistencia no entran aquí: son quince al día y taparían el cambio de precio de la
          semana pasada.
        </p>
      </div>

      {fallo !== null ? (
        <div role="alert" className="px-4 py-4">
          <p className="text-xs font-semibold" style={{ color: '#991B1B' }}>
            No se pudo leer la bitácora. Esto no quiere decir que no haya cambios: quiere
            decir que no sabemos cuáles son.
          </p>
          <p className="text-xs mt-1" style={{ color: '#B91C1C' }}>
            {fallo}
          </p>
        </div>
      ) : cambios === null ? (
        <CargandoPantalla que="Leyendo la bitácora…" fondo="#F7F6FA" />
      ) : cambios.length === 0 ? (
        <Vacio>Todavía no hay cambios registrados.</Vacio>
      ) : (
        <ul
          className="m-0 flex min-h-0 flex-1 list-none flex-col overflow-y-auto p-0"
          style={{ backgroundColor: '#FFFFFF' }}
        >
          {cambios.map((c, i) => (
            <li key={i} className="px-4 py-3" style={{ borderBottom: '1px solid #ECE8F4' }}>
              <div className="flex items-center gap-2 flex-wrap">
                <code style={{ fontFamily: MONO, fontSize: '11.5px', fontWeight: 700, color: '#4C1D95' }}>
                  {c.clave}
                </code>
                <span style={{ fontFamily: MONO, fontSize: '10px', color: '#9C95AD' }}>
                  {c.tabla}
                </span>
                <span
                  className="ml-auto"
                  style={{ fontFamily: MONO, fontSize: '10px', color: '#9C95AD', whiteSpace: 'nowrap' }}
                >
                  {c.usuario} · {cuando(c.cambiado_en)}
                </span>
              </div>
              <p className="text-xs mt-1 break-words" style={{ color: '#4A4458' }}>
                {c.valor_anterior === null ? (
                  <span style={{ color: '#059669' }}>nuevo: </span>
                ) : (
                  <>
                    <span className="line-through" style={{ color: '#9C95AD' }}>
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
    </>
  )
}

// ------------------------------------------------------------------------------------------
// Los hechos de la clínica
// ------------------------------------------------------------------------------------------

/** El detalle de `_general`. Es el mismo editor de fichas y ninguna otra cosa: lo único que
 *  cambia respecto a un tratamiento es que aquí no hay nada que renombrar ni que desactivar
 *  --no es un servicio-- y que la explicación de qué es esto va arriba del todo, porque
 *  «La clínica» no se explica sola como se explica «Ortodoncia». */
function DetalleClinica({
  fichas,
  conceptos,
  puedeEditar,
  porQueNo,
  forzarNueva,
  alCerrarNueva,
  alGuardarFicha,
  alPedirNueva,
  alVolver,
}: {
  fichas: FichaFila[]
  conceptos: string[]
  puedeEditar: boolean
  porQueNo: string
  forzarNueva: boolean
  alCerrarNueva: () => void
  alGuardarFicha: (f: {
    tratamiento: string
    concepto: string
    contenido: string
    aprobado: boolean
    nota_pendiente: string | null
  }) => Promise<boolean>
  alPedirNueva: () => void
  alVolver: () => void
}) {
  return (
    <>
      <CabeceraDeDetalle
        titulo="La clínica"
        clave={null}
        alVolver={alVolver}
        acciones={
          <AccionDeCabecera
            alPulsar={alPedirNueva}
            ocupado={!puedeEditar || forzarNueva}
            titulo={puedeEditar ? undefined : porQueNo}
          >
            + Hecho
          </AccionDeCabecera>
        }
      />

      <CuerpoDeDetalle>
        <div className="px-4 py-3" style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6' }}>
          <p className="text-xs leading-relaxed" style={{ color: '#4A4458' }}>
            Horario, sede, EPS, medios de pago, urgencias y la política de precios: lo que
            Daniela responde cuando la pregunta no es sobre un tratamiento. No es un servicio y
            nadie le agenda una cita; por eso vive aparte de la lista.
          </p>
        </div>

        {/* Arriba de las fichas, por lo mismo que en `DetalleTratamiento`: el botón que lo
            abre está en la cabecera. */}
        {forzarNueva && (
          <NuevaFicha
            tratamiento={GENERAL}
            conceptos={conceptos}
            existentes={fichas}
            puedeEditar={puedeEditar}
            porQueNo={porQueNo}
            alGuardar={alGuardarFicha}
            alCerrar={alCerrarNueva}
          />
        )}

        {fichas.map((f) => (
          <EditorFicha
            key={f.concepto}
            ficha={f}
            puedeEditar={puedeEditar}
            porQueNo={porQueNo}
            alGuardar={alGuardarFicha}
          />
        ))}
      </CuerpoDeDetalle>
    </>
  )
}

// ------------------------------------------------------------------------------------------
// La pantalla
// ------------------------------------------------------------------------------------------

/** `true` si el tratamiento pasa el filtro elegido. `sinAprobar` llega ya contado. */
function pasaElFiltro(t: TratamientoFila, filtro: Filtro, sinAprobar: number): boolean {
  const e = estado(t, sinAprobar)
  if (filtro === 'sin_precio') return e.sinPrecio
  if (filtro === 'sin_aprobar') return sinAprobar > 0
  if (filtro === 'inactivos') return !t.activo
  return true
}

/** La búsqueda mira el nombre visible y la clave. Las dos, y no solo la primera: quien viene
 *  de un log o de una cita lee `no_identificado`, no «Sin clasificar». */
function coincide(t: TratamientoFila, busqueda: string): boolean {
  const q = busqueda.trim().toLowerCase()
  if (q === '') return true
  return t.etiqueta.toLowerCase().includes(q) || t.clave.toLowerCase().includes(q)
}

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
  /* El fallo de LEER va aparte del de escribir, y no es orden por el orden: el botón
     «Reintentar» solo tiene sentido sobre una lectura. Puesto sobre el mismo estado, aparecía
     también cuando lo que falló fue guardar un precio, y ahí «reintentar» promete repetir la
     escritura --que es justo lo que ese botón NO hace--. */
  const [falloDeCarga, setFalloDeCarga] = useState<string | null>(null)

  /* Lo que ocupa el panel de la derecha, en una sola cadena: la clave de un tratamiento,
     `GENERAL`, `BITACORA` o `NUEVO`. Eran cuatro estados sueltos --`abierta`, `pestana`,
     `verBitacora` y el `abierto` interno de `NuevoTratamiento`-- y podían ser ciertos a la
     vez: la bitácora salía ENCIMA del tratamiento abierto, empujándolo. Con una sola cadena
     eso deja de poder ocurrir por construcción, no por cuidado. */
  const [seleccion, setSeleccion] = useState<string | null>(null)
  const [nuevaEn, setNuevaEn] = useState<string | null>(null)
  const [busqueda, setBusqueda] = useState('')
  const [filtro, setFiltro] = useState<Filtro>('todos')
  /* Por debajo de `ANCHO_DOS_PANELES` la pantalla ensena UN panel: la lista, o el detalle con
     su boton de volver. Es la unica decision responsive que no puede ser una clase de CSS,
     porque depende de si hay algo seleccionado. Igual que en Conversaciones y Sin resolver. */
  const unaColumna = usarEsAngosto(ANCHO_DOS_PANELES)

  const puedeFichas = sesion.rol === 'admin' || sesion.rol === 'doctor'
  const puedeTratamientos = sesion.rol === 'admin'
  const porQueNoFichas = 'Hace falta permiso de admin o doctor para editar fichas.'
  const porQueNoTratamientos = 'Hace falta permiso de admin para crear o cambiar tratamientos.'

  /* `alCaducarSesion` llega por una `ref` a las dos funciones memoizadas de abajo, y directo
     a las tres escrituras, que se recrean en cada render y no lo necesitan.
     La razón no es estilo: `App.tsx` lo pasa como una flecha nueva en cada render, así que
     meterlo en las dependencias de `recargar` cambiaría su identidad en cada render, y el
     efecto que depende de ella volvería a leer la base sin parar. La `ref` deja `recargar`
     estable y aun así llama siempre a la última versión del callback. */
  const caducar = useRef(alCaducarSesion)
  useEffect(() => {
    caducar.current = alCaducarSesion
  })

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      const [t, f] = await Promise.all([listarTratamientos(), listarFichas()])
      setTratamientos(t)
      setFichas(f)
      setError(null)
      setFalloDeCarga(null)
    } catch (err) {
      /* El momento más común para descubrir que una sesión caducó es este --volver a la
         pestaña después de comer y que la pantalla cargue--, no pulsar «Guardar». Dejarlo
         solo en la franja roja era el caso principal, no una incoherencia cosmética con las
         escrituras. */
      if (err instanceof SesionCaducada) {
        caducar.current()
        return
      }
      setFalloDeCarga(mensajeDe(err))
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  const verBitacora = seleccion === BITACORA

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
        /* El 401 sale al ingreso como en todas partes, y esto NO deshace lo que arregló B-3.
           La comprobación es `instanceof SesionCaducada`, que es exactamente el 401 y nada
           más: una caída de red, un 500 o un error del pooler siguen cayendo abajo, que es
           la clase de fallo por la que la bitácora tiene que distinguir «no hay cambios» de
           «no se pudo leer». Lo que se evita es lo contrario: decir «no se pudo leer la
           bitácora» cuando lo cierto y más útil es «ya no tienes sesión», y dejar a alguien
           en una pantalla cuyo botón de guardar ya no sirve. */
        if (err instanceof SesionCaducada) {
          caducar.current()
          return
        }
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

  /* Cuántas fichas sin aprobar tiene cada tratamiento, contadas UNA vez y no quince.
     Alimenta la pastilla de la fila y el filtro «Sin aprobar», que tienen que decir lo
     mismo: un chip que deja fuera a alguien con la pastilla puesta es peor que no tenerlo. */
  const sinAprobarPorClave = useMemo(() => {
    const cuenta = new Map<string, number>()
    for (const f of fichas) {
      if (f.aprobado) continue
      cuenta.set(f.tratamiento, (cuenta.get(f.tratamiento) ?? 0) + 1)
    }
    return cuenta
  }, [fichas])

  /* La cabecera cuenta lo que la pantalla enseña, y nada más.
   *
   * `sinPrecio` excluye los desactivados: un tratamiento que la clínica decidió dejar de
   * ofrecer no le falta un precio, le sobra la fila. Contarlo mantenía la alarma encendida
   * sobre algo que ya nadie cotiza, que es la manera más rápida de enseñarle a la clínica a
   * ignorar la alarma. */
  const cuentas = useMemo(() => {
    let sinPrecio = 0
    let sinAprobar = 0
    let inactivos = 0
    for (const t of tratamientos) {
      const n = sinAprobarPorClave.get(t.clave) ?? 0
      if (estado(t, n).sinPrecio) sinPrecio += 1
      if (n > 0) sinAprobar += 1
      if (!t.activo) inactivos += 1
    }
    return { todos: tratamientos.length, sin_precio: sinPrecio, sin_aprobar: sinAprobar, inactivos }
  }, [tratamientos, sinAprobarPorClave])

  const visibles = useMemo(
    () =>
      tratamientos.filter(
        (t) => pasaElFiltro(t, filtro, sinAprobarPorClave.get(t.clave) ?? 0) && coincide(t, busqueda),
      ),
    [tratamientos, filtro, busqueda, sinAprobarPorClave],
  )

  const elegido = seleccion ? tratamientos.find((t) => t.clave === seleccion) : undefined

  /* El fallo de una escritura se suelta al cambiar de panel. Sin esto, la franja roja de «no
     se pudo guardar el precio de implantes» se quedaba puesta encima de ortodoncia, diciendo
     de la ficha que se está mirando algo que le pasó a otra. */
  useEffect(() => {
    setError(null)
  }, [seleccion])

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
      setSeleccion(creado.clave)
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

  // Sin cabecera ni filtros: la pantalla no se enseña a medias. Ver `CargandoPantalla`.
  if (cargando) return <CargandoPantalla que="Leyendo la base…" />

  /* El contenido del panel de la derecha, que es una de cuatro cosas o ninguna. Se arma aquí
     y no dentro del JSX para que el `Panel` quede legible y para que la lista de casos se
     lea de un vistazo, que es lo que esta pantalla tenía repartido en cuatro banderas. */
  const detalle =
    seleccion === NUEVO ? (
      <NuevoTratamiento
        alCrear={crearUno}
        alVolver={() => setSeleccion(null)}
        alCancelar={() => setSeleccion(null)}
      />
    ) : seleccion === BITACORA ? (
      <Bitacora cambios={cambios} fallo={falloBitacora} alVolver={() => setSeleccion(null)} />
    ) : seleccion === GENERAL ? (
      <DetalleClinica
        fichas={deLaClinica}
        conceptos={conceptos}
        puedeEditar={puedeFichas}
        porQueNo={porQueNoFichas}
        forzarNueva={nuevaEn === GENERAL}
        alCerrarNueva={() => setNuevaEn(null)}
        alGuardarFicha={guardarUnaFicha}
        alPedirNueva={() => setNuevaEn(GENERAL)}
        alVolver={() => setSeleccion(null)}
      />
    ) : elegido ? (
      <DetalleTratamiento
        // Al cambiar de tratamiento se monta uno nuevo: el `key` es lo que garantiza que el
        // texto a medias de una ficha no viaje al siguiente. Sin él, React reutilizaría el
        // mismo `EditorFicha` con otra prop y `sucio` compararía contra la ficha equivocada.
        key={elegido.clave}
        t={elegido}
        fichas={fichas.filter((f) => f.tratamiento === elegido.clave)}
        conceptos={conceptos}
        puedeFichas={puedeFichas}
        puedeTratamientos={puedeTratamientos}
        porQueNoFichas={porQueNoFichas}
        porQueNoTratamientos={porQueNoTratamientos}
        forzarNueva={nuevaEn === elegido.clave}
        alCerrarNueva={() => setNuevaEn(null)}
        alGuardarFicha={guardarUnaFicha}
        alCambiar={cambiarUno}
        alVolver={() => setSeleccion(null)}
      />
    ) : null

  return (
    <MarcoDeDosPaneles etiqueta="Tratamientos">
      {/* ------------------------------------------------------------------- La lista */}
      {unaColumna && detalle ? null : (
      <Panel etiqueta="Listado de tratamientos" peso="1 1 320px">
        <CabeceraDePanel>
          <div className="flex items-baseline justify-between gap-3">
            <TituloDePanel>Tratamientos</TituloDePanel>
            <div className="flex shrink-0 items-center gap-2">
              <Rotulo>
                {visibles.length === tratamientos.length
                  ? `${tratamientos.length} ${tratamientos.length === 1 ? 'clave' : 'claves'}`
                  : `${visibles.length} de ${tratamientos.length}`}
              </Rotulo>
              {puedeTratamientos ? (
                <AccionDeCabecera
                  alPulsar={() => setSeleccion(NUEVO)}
                  activo={seleccion === NUEVO}
                >
                  + Nuevo
                </AccionDeCabecera>
              ) : null}
            </div>
          </div>

          {/* La frase que justifica que esta pantalla exista y que vaya despacio. No es un
              subtítulo decorativo: lo que se guarda aquí no pasa por ningún despliegue. */}
          <p style={{ margin: 0, fontSize: '12.5px', lineHeight: 1.45, color: '#6E6880' }}>
            Lo que se guarda aquí sale por WhatsApp en el siguiente mensaje: Daniela lee esta
            tabla en cada turno, sin despliegue de por medio.
          </p>

          <Buscador
            valor={busqueda}
            alCambiar={setBusqueda}
            marcador="Buscar por nombre o clave"
            etiqueta="Buscar tratamientos"
          />

          {/* Los contadores viven DENTRO de los filtros y no en una línea de texto aparte.
              Antes la cabecera decía «2 tratamientos sin precio» y ahí se acababa: para saber
              cuáles había que ir abriendo acordeones de uno en uno. */}
          <div className="flex flex-wrap gap-2">
            {CHIPS.map((chip) => (
              <Chip key={chip.id} puesto={filtro === chip.id} alPulsar={() => setFiltro(chip.id)}>
                {chip.id === 'todos' ? chip.etiqueta : `${chip.etiqueta} · ${cuentas[chip.id]}`}
              </Chip>
            ))}
          </div>
        </CabeceraDePanel>

        {falloDeCarga !== null ? (
          // Si la consulta falló, la pantalla lo DICE, y con salida: antes solo quedaba
          // recargar la página entera. Mismo criterio que Conversaciones.
          <div className="px-4 py-6">
            <Fallo mensaje={falloDeCarga} alReintentar={() => void recargar()} />
          </div>
        ) : (
          <ul className="m-0 flex min-h-0 flex-1 list-none flex-col overflow-y-auto p-0">
            {/* Los hechos de la clínica, arriba y fuera del filtro. El porqué de que ya no
                sean una pestaña está en la decisión 3 de la cabecera de este archivo. */}
            <RotuloDeGrupo>La clínica</RotuloDeGrupo>
            <FilaFija
              titulo="Hechos de la clínica"
              detalle={
                deLaClinica.length === 0
                  ? 'Horario, sede, EPS, medios de pago. Todavía sin ninguna ficha.'
                  : `Horario, sede, EPS, medios de pago. ${deLaClinica.length} fichas.`
              }
              activa={seleccion === GENERAL}
              alAbrir={() => setSeleccion(GENERAL)}
            />

            <RotuloDeGrupo>
              Tratamientos
              {visibles.length !== tratamientos.length
                ? ` · ${visibles.length} de ${tratamientos.length}`
                : ''}
            </RotuloDeGrupo>
            {visibles.length === 0 ? (
              <li>
                <Vacio>
                  {busqueda.trim()
                    ? 'Ningún tratamiento con ese nombre ni esa clave.'
                    : filtro === 'sin_precio'
                      ? 'Todos los tratamientos activos tienen precio. Buena señal.'
                      : filtro === 'sin_aprobar'
                        ? 'Ninguna ficha está pendiente de aprobar.'
                        : 'Ninguno está desactivado.'}
                </Vacio>
              </li>
            ) : (
              visibles.map((t) => (
                <FilaDeLaLista
                  key={t.clave}
                  t={t}
                  sinAprobar={sinAprobarPorClave.get(t.clave) ?? 0}
                  activa={t.clave === seleccion}
                  alAbrir={() => setSeleccion(t.clave)}
                />
              ))
            )}

            <RotuloDeGrupo>Registro</RotuloDeGrupo>
            <FilaFija
              titulo="Últimos cambios"
              detalle="Quién cambió qué precio y qué decía antes."
              activa={seleccion === BITACORA}
              alAbrir={() => setSeleccion(BITACORA)}
            />
          </ul>
        )}
      </Panel>
      )}

      {/* ------------------------------------------------------------------ El detalle */}
      {/* En una columna no se pinta el hueco de «selecciona algo»: la lista ya ocupa la
          pantalla, y un panel que pide elegir debajo de lo que hay que elegir es ruido. */}
      {unaColumna && !detalle ? null : (
      <Panel etiqueta="Detalle del tratamiento" peso="2 1 380px">
        {/* El fallo de una ESCRITURA, arriba del todo y sin botón de reintentar: ese botón
            prometería repetir un guardado que no repite. Va aquí y no en la lista porque lo
            que falló se pulsó en este panel. */}
        {error !== null && (
          <div
            role="alert"
            className="shrink-0 px-4 py-3"
            style={{ backgroundColor: '#FEF2F2', borderBottom: '1px solid #FECACA' }}
          >
            <p style={{ fontFamily: MONO, fontSize: '10.5px', letterSpacing: '0.1em', color: '#B91C1C' }}>
              NO SE PUDO GUARDAR
            </p>
            <p className="text-xs mt-1" style={{ color: '#B91C1C' }}>
              {error}
            </p>
          </div>
        )}
        {detalle ?? (
          <Vacio>Elige un tratamiento para ver sus fichas y corregir lo que falte.</Vacio>
        )}
      </Panel>
      )}
    </MarcoDeDosPaneles>
  )
}

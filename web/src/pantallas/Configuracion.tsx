import { useCallback, useEffect, useRef, useState } from 'react'
import {
  guardarConfiguracion,
  leerConfiguracion,
  SesionCaducada,
  type Configuracion as Datos,
} from '@/api'
import { CargandoPantalla, Fallo } from '@/componentes/Estado'
import { BOTON, CAMPO, ESTILO_CAMPO, MONO, Rotulo, SG } from '@/componentes/Panel'

/* «Configuración»: las trece perillas operativas.
 *
 * Hasta el 25/09/2026 esta sección era un cartel de «todavía no construida», y su motivo para
 * esperar --«las tres perillas de la agenda ya viven en la base y funcionan; lo que falta es
 * la pantalla»-- era cierto a medias. Lo que faltaba de verdad era la otra mitad: no existía
 * ni una línea de Python que ESCRIBIERA esa tabla. Se cambiaban entrando a Neon por SQL.
 *
 * ------------------------------------------------------------------------------------------
 * Las cuatro decisiones de esta pantalla
 * ------------------------------------------------------------------------------------------
 *
 * 1. SOLO SE MANDA LO QUE CAMBIÓ. El PATCH lleva las perillas cuyo valor difiere de lo
 *    guardado, no las trece. El servidor tampoco escribiría las iguales --ni fila de bitácora
 *    ni UPDATE-- pero mandarlas todas haría que un fallo de validación en una perilla que
 *    nadie tocó bloqueara el cambio de la que sí.
 *
 * 2. `atiende_domingo` SON DOS BOTONES, NUNCA UNA CASILLA. El valor es `0` o `1` en una
 *    columna de TEXTO, y el `checked` de una casilla viaja como booleano JSON: `true` se
 *    guardaría como la cadena 'True', que `leer_configuracion` descarta EN SILENCIO en su
 *    `int(valor)`. La clínica se quedaría sin domingos sin un error en ningún log. El
 *    servidor también lo rechaza (`panel._entero_de_configuracion`), pero el sitio donde ese
 *    booleano NACE es este, y aquí es donde no tiene que existir.
 *
 * 3. LOS RANGOS VIENEN DEL SERVIDOR, no escritos aquí. Con dos copias, el `min` del campo y
 *    el 422 del servidor discrepan el día que uno de los dos cambie, y el usuario ve un campo
 *    que acepta un valor que después se rechaza.
 *
 * 4. EL AVISO DEL HORARIO ES LA MITAD QUE NO ES OBVIA. Estas perillas mandan sobre los cupos
 *    que Daniela OFRECE. Lo que Daniela DICE cuando le preguntan «¿a qué hora abren?» sale de
 *    la ficha `_general` / `horario` de la base de conocimiento, que es texto que edita un
 *    humano en Tratamientos. Son dos sitios y hay que moverlos juntos: sin el aviso, se
 *    cambia el cierre aquí y Daniela sigue diciendo «cerramos a las 5» mientras ofrece un
 *    cupo a las 6.
 *
 * El `useCallback` de `recargar` lleva dependencias vacías y `alCaducarSesion` se consume por
 * una `ref`, igual que en Tratamientos y por el mismo motivo: `App.tsx` pasa esa prop como
 * una flecha nueva en cada render, así que meterla en las dependencias deja la pantalla
 * releyendo la base en bucle. No hay regla de hooks ni prueba de frontend que lo atrape. */

type Props = { alCaducarSesion: () => void }

const ETIQUETAS: Record<string, string> = {
  capacidad_por_hora: 'Pacientes por bloque',
  duracion_cita_minutos: 'Duración de una cita (minutos)',
  hora_apertura: 'Hora de apertura',
  hora_cierre: 'Hora de cierre (lunes a viernes)',
  hora_cierre_sabado: 'Hora de cierre (sábado)',
  atiende_domingo: '¿Se atiende el domingo?',
  aviso_relevo_minutos: 'Aviso al doctor (minutos sin escribir)',
  cierre_relevo_minutos: 'Devolver a Daniela (minutos sin escribir)',
  hora_recordatorio_vispera: 'Hora del recordatorio de la víspera',
  horas_minimas_para_recordar: 'Antelación mínima para recordar (horas)',
  tope_diario_reactivacion: 'Tope de reactivaciones al día',
  max_reactivaciones_12m: 'Tope por persona en 12 meses',
  max_seguimientos_fallidos: 'Envíos sin respuesta antes de parar',
}

const GRUPOS: { titulo: string; nota: string; claves: string[] }[] = [
  {
    titulo: 'Agenda',
    nota: 'Manda sobre los cupos que Daniela ofrece. El turno siguiente ya los usa.',
    claves: [
      'capacidad_por_hora',
      'duracion_cita_minutos',
      'hora_apertura',
      'hora_cierre',
      'hora_cierre_sabado',
      'atiende_domingo',
    ],
  },
  {
    titulo: 'Relevo',
    nota: 'Cuándo se le recuerda al doctor que tiene una conversación tomada, y cuándo vuelve sola a Daniela.',
    claves: ['aviso_relevo_minutos', 'cierre_relevo_minutos'],
  },
  {
    titulo: 'Recordatorios',
    nota: 'La cola que avisa a un paciente de su cita del día siguiente.',
    claves: ['hora_recordatorio_vispera', 'horas_minimas_para_recordar'],
  },
  {
    titulo: 'Reactivación',
    nota: 'El barrido que vuelve a escribirle a quien no agendó. Un tope en 0 lo apaga.',
    claves: ['tope_diario_reactivacion', 'max_reactivaciones_12m', 'max_seguimientos_fallidos'],
  },
]

const CLAVES_DE_HORARIO = ['hora_apertura', 'hora_cierre', 'hora_cierre_sabado']

export default function Configuracion({ alCaducarSesion }: Props) {
  const [datos, setDatos] = useState<Datos | null>(null)
  const [borrador, setBorrador] = useState<Record<string, number>>({})
  const [cargando, setCargando] = useState(true)
  const [guardando, setGuardando] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [guardado, setGuardado] = useState(false)

  const caduco = useRef(alCaducarSesion)
  caduco.current = alCaducarSesion

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      const d = await leerConfiguracion()
      setDatos(d)
      setBorrador({ ...d.valores })
      setError(null)
    } catch (e) {
      if (e instanceof SesionCaducada) return caduco.current()
      setError(e instanceof Error ? e.message : 'No se pudo leer la configuración.')
    } finally {
      setCargando(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ver la cabecera del archivo
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  if (cargando && !datos) {
    return (
      <CargandoPantalla que="Configuración" detalle="Leyendo lo que está guardado…" fondo="#F9FAFB" />
    )
  }
  if (!datos) {
    return (
      <div className="flex-1 p-8" style={{ fontFamily: SG, backgroundColor: '#F9FAFB' }}>
        <Fallo mensaje={error ?? 'No se pudo leer la configuración.'} alReintentar={() => void recargar()} />
      </div>
    )
  }

  const cambios = Object.fromEntries(
    Object.entries(borrador).filter(([clave, valor]) => valor !== datos.valores[clave]),
  )
  const hayCambios = Object.keys(cambios).length > 0
  const tocaElHorario = CLAVES_DE_HORARIO.some((c) => c in cambios)

  function poner(clave: string, valor: number) {
    setGuardado(false)
    setBorrador((b) => ({ ...b, [clave]: valor }))
  }

  async function guardar() {
    if (!hayCambios || guardando) return
    setGuardando(true)
    setError(null)
    try {
      const d = await guardarConfiguracion(cambios)
      setDatos(d)
      setBorrador({ ...d.valores })
      setGuardado(true)
    } catch (e) {
      if (e instanceof SesionCaducada) return caduco.current()
      setError(e instanceof Error ? e.message : 'No se pudo guardar.')
    } finally {
      setGuardando(false)
    }
  }

  const soloLectura = !datos.es_admin

  return (
    <div
      className="flex min-w-0 flex-1 flex-col overflow-hidden"
      style={{ fontFamily: SG, backgroundColor: '#F9FAFB' }}
    >
      <header
        className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b px-4 py-4 sm:px-8"
        style={{ backgroundColor: '#FFFFFF', borderColor: '#DCD8E6' }}
      >
        <div className="min-w-0">
          <h1 className="text-lg font-bold" style={{ color: '#16111F' }}>
            Configuración
          </h1>
          <p className="text-sm" style={{ color: '#6B6480' }}>
            {soloLectura
              ? 'Así está configurado el sistema. Cambiarlo es cosa de un administrador.'
              : 'Lo que cambies aquí queda registrado con tu nombre y entra en el turno siguiente.'}
          </p>
        </div>
        {!soloLectura && (
          <div className="flex items-center gap-3">
            {guardado && !hayCambios && (
              <span style={{ fontFamily: MONO, fontSize: 11, color: '#166534' }}>GUARDADO</span>
            )}
            <button
              type="button"
              onClick={() => void guardar()}
              disabled={!hayCambios || guardando}
              className={BOTON}
              style={{
                minHeight: 38,
                backgroundColor: hayCambios ? '#7C3AED' : '#EDE9FE',
                color: hayCambios ? '#FFFFFF' : '#6B6480',
              }}
            >
              {guardando ? 'Guardando…' : hayCambios ? `Guardar ${Object.keys(cambios).length}` : 'Guardar'}
            </button>
          </div>
        )}
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-8">
        <div className="mx-auto flex max-w-3xl flex-col gap-7">
          {error && <Fallo mensaje={error} />}

          {tocaElHorario && (
            <div
              className="px-4 py-3"
              style={{ backgroundColor: '#FEF3C7', border: '1px solid #FDE68A' }}
            >
              <p className="text-xs leading-relaxed" style={{ color: '#92400E' }}>
                Esto cambia los horarios que Daniela <strong>ofrece</strong>. Lo que{' '}
                <strong>dice</strong> cuando le preguntan «¿a qué hora abren?» sale de la ficha{' '}
                <code style={{ fontFamily: MONO }}>General → horario</code> de Tratamientos, y hay
                que moverla a mano: si no, dirá una hora y ofrecerá otra.
              </p>
            </div>
          )}

          {GRUPOS.map((grupo) => (
            <section key={grupo.titulo} className="flex flex-col">
              <div className="flex flex-col gap-0.5 pb-2">
                <Rotulo>{grupo.titulo}</Rotulo>
                <p className="px-2.5 text-xs" style={{ color: '#6B6480' }}>
                  {grupo.nota}
                </p>
              </div>
              <div style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6' }}>
                {grupo.claves.map((clave, i) => (
                  <Perilla
                    key={clave}
                    clave={clave}
                    valor={borrador[clave]}
                    guardado={datos.valores[clave]}
                    rango={datos.rangos[clave]}
                    descripcion={datos.descripciones[clave]}
                    soloLectura={soloLectura || guardando}
                    primera={i === 0}
                    poner={poner}
                  />
                ))}
              </div>
            </section>
          ))}

          {datos.fijas.length > 0 && (
            <section className="flex flex-col">
              <div className="flex flex-col gap-0.5 pb-2">
                <Rotulo>No se editan</Rotulo>
                <p className="px-2.5 text-xs" style={{ color: '#6B6480' }}>
                  Están aquí para que no se busquen en vano. Se cambian con una migración.
                </p>
              </div>
              <div style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6' }}>
                {datos.fijas.map((f, i) => (
                  <div
                    key={f.clave}
                    className="px-4 py-3"
                    style={{ borderTop: i === 0 ? undefined : '1px solid #F0EEF5' }}
                  >
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span style={{ fontFamily: MONO, fontSize: 12, color: '#16111F' }}>
                        {f.clave}
                      </span>
                      <span style={{ fontFamily: MONO, fontSize: 12, color: '#7C3AED' }}>
                        = {f.valor}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed" style={{ color: '#6B6480' }}>
                      {f.por_que}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}

function Perilla({
  clave,
  valor,
  guardado,
  rango,
  descripcion,
  soloLectura,
  primera,
  poner,
}: {
  clave: string
  valor: number
  guardado: number
  rango: [number, number] | undefined
  descripcion: string | undefined
  soloLectura: boolean
  primera: boolean
  poner: (clave: string, valor: number) => void
}) {
  const cambiada = valor !== guardado
  const [minimo, maximo] = rango ?? [0, 9999]

  return (
    <div
      className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:gap-4"
      style={{ borderTop: primera ? undefined : '1px solid #F0EEF5' }}
    >
      <div className="min-w-0 flex-1">
        <label
          htmlFor={`perilla-${clave}`}
          className="block text-sm"
          style={{ color: '#16111F', fontWeight: cambiada ? 600 : 400 }}
        >
          {ETIQUETAS[clave] ?? clave}
        </label>
        {descripcion && (
          <p className="mt-0.5 text-xs leading-relaxed" style={{ color: '#6B6480' }}>
            {descripcion}
          </p>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {cambiada && (
          <span style={{ fontFamily: MONO, fontSize: 10.5, color: '#92400E' }}>
            antes {guardado}
          </span>
        )}
        {/* `atiende_domingo` va con botones y NUNCA con una casilla: el `checked` de una
            casilla viaja como booleano JSON, y este valor es un 0 o un 1 en una columna de
            texto. Ver la decisión 2 en la cabecera del archivo. */}
        {clave === 'atiende_domingo' ? (
          <div className="flex" style={{ border: '1px solid #DCD8E6' }}>
            {[
              { texto: 'Sí', v: 1 },
              { texto: 'No', v: 0 },
            ].map((o) => (
              <button
                key={o.v}
                type="button"
                disabled={soloLectura}
                onClick={() => poner(clave, o.v)}
                className="px-4 text-sm transition-colors disabled:opacity-60"
                style={{
                  minHeight: 36,
                  backgroundColor: valor === o.v ? '#7C3AED' : '#FBFAFD',
                  color: valor === o.v ? '#FFFFFF' : '#6B6480',
                  fontWeight: valor === o.v ? 600 : 400,
                }}
              >
                {o.texto}
              </button>
            ))}
          </div>
        ) : (
          <input
            id={`perilla-${clave}`}
            type="number"
            inputMode="numeric"
            value={Number.isFinite(valor) ? valor : ''}
            min={minimo}
            max={maximo}
            disabled={soloLectura}
            onChange={(e) => {
              const n = e.target.valueAsNumber
              if (Number.isFinite(n)) poner(clave, Math.trunc(n))
            }}
            className={CAMPO}
            style={{
              ...ESTILO_CAMPO,
              width: 104,
              minHeight: 36,
              textAlign: 'right',
              fontFamily: MONO,
              borderColor: cambiada ? '#A78BFA' : '#DCD8E6',
            }}
          />
        )}
        <span
          className="hidden sm:block"
          style={{ fontFamily: MONO, fontSize: 10, color: '#9A93AD', width: 62 }}
        >
          {minimo}–{maximo}
        </span>
      </div>
    </div>
  )
}

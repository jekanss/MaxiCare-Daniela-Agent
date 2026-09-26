import { useCallback, useEffect, useRef, useState } from 'react'
import {
  leerEstado,
  SesionCaducada,
  sondearEstado,
  type EstadoDelSistema as Estado,
  type SondasDelSistema,
} from '@/api'
import { CargandoPantalla, Fallo } from '@/componentes/Estado'
import { BOTON, MONO, Rotulo, SG } from '@/componentes/Panel'

/* «Estado del sistema»: la salud operativa, con detalle.
 *
 * El archivo se llama `EstadoDelSistema.tsx` y no `Estado.tsx` a propósito: ya existe
 * `componentes/Estado.tsx`, que es el loader genérico del panel, y dos módulos con el mismo
 * nombre bajo el alias `@/` son una tarde perdida.
 *
 * ------------------------------------------------------------------------------------------
 * Las tres decisiones de esta pantalla
 * ------------------------------------------------------------------------------------------
 *
 * 1. LAS SONDAS EXTERNAS VAN DETRÁS DE UN BOTÓN. `GET /api/estado` no sale a internet: lee el
 *    proceso y Neon. Preguntarle a Meta y a Telegram al abrir convertiría una caída de Meta
 *    en «la pantalla de diagnóstico no carga», que es el fallo más tonto posible en la
 *    herramienta que existe para saber qué está roto.
 *
 * 2. LO QUE NO SE SABE NO SE PINTA DE VERDE. Un bloque vacío --Neon caído, una sonda que no
 *    contestó-- sale en gris con su texto, nunca como un cero tranquilizador. «0 plantillas»
 *    sobre un token caducado diría que Meta las rechazó todas.
 *
 * 3. DOS HECHOS DEL WEBHOOK, NUNCA MEZCLADOS. «La URL está puesta» lo dice Telegram; «el
 *    secreto está configurado» lo dice este proceso. `getWebhookInfo` NO reporta el secreto,
 *    así que juntarlos en un solo semáforo sería inventarse la mitad. */

type Props = { alCaducarSesion: () => void }

type Tono = 'bien' | 'ojo' | 'mal' | 'mudo'

const COLORES: Record<Tono, { fondo: string; borde: string; tinta: string; punto: string }> = {
  bien: { fondo: '#FFFFFF', borde: '#DCD8E6', tinta: '#166534', punto: '#16A34A' },
  ojo: { fondo: '#FFFBEB', borde: '#FDE68A', tinta: '#92400E', punto: '#D97706' },
  mal: { fondo: '#FEF2F2', borde: '#FECACA', tinta: '#991B1B', punto: '#DC2626' },
  mudo: { fondo: '#FFFFFF', borde: '#DCD8E6', tinta: '#6B6480', punto: '#C9C3D6' },
}

function Tarjeta({
  titulo,
  valor,
  tono,
  nota,
}: {
  titulo: string
  valor: string
  tono: Tono
  nota?: string
}) {
  const c = COLORES[tono]
  return (
    <div
      className="flex flex-col gap-1 px-4 py-3"
      style={{ backgroundColor: c.fondo, border: `1px solid ${c.borde}` }}
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="shrink-0 rounded-full"
          style={{ width: 8, height: 8, backgroundColor: c.punto }}
        />
        <span
          className="truncate"
          style={{ fontFamily: MONO, fontSize: 9.5, letterSpacing: '0.12em', textTransform: 'uppercase', color: '#6B6480' }}
        >
          {titulo}
        </span>
      </div>
      <span style={{ fontSize: 15, fontWeight: 600, color: c.tinta }}>{valor}</span>
      {nota && (
        <span className="text-xs leading-relaxed" style={{ color: '#6B6480' }}>
          {nota}
        </span>
      )}
    </div>
  )
}

function Rejilla({ children }: { children: React.ReactNode }) {
  return <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(232px, 1fr))' }}>{children}</div>
}

/** Un número que puede no haber llegado. `undefined` es «no se sabe», no cero. */
function cifra(n: number | null | undefined): string {
  return n === null || n === undefined ? 'sin dato' : String(n)
}

export default function EstadoDelSistema({ alCaducarSesion }: Props) {
  const [estado, setEstado] = useState<Estado | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sondas, setSondas] = useState<SondasDelSistema | null>(null)
  const [sondeando, setSondeando] = useState(false)
  const [errorSonda, setErrorSonda] = useState<string | null>(null)

  const caduco = useRef(alCaducarSesion)
  caduco.current = alCaducarSesion

  const recargar = useCallback(async () => {
    setCargando(true)
    try {
      setEstado(await leerEstado())
      setError(null)
    } catch (e) {
      if (e instanceof SesionCaducada) return caduco.current()
      setError(e instanceof Error ? e.message : 'No se pudo leer el estado.')
    } finally {
      setCargando(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- misma razón que en Tratamientos
  }, [])

  useEffect(() => {
    void recargar()
  }, [recargar])

  async function sondear() {
    if (sondeando) return
    setSondeando(true)
    setErrorSonda(null)
    try {
      setSondas(await sondearEstado())
    } catch (e) {
      if (e instanceof SesionCaducada) return caduco.current()
      setErrorSonda(e instanceof Error ? e.message : 'No se pudo preguntar.')
    } finally {
      setSondeando(false)
    }
  }

  if (cargando && !estado) {
    return <CargandoPantalla que="Estado del sistema" detalle="Leyendo el proceso y la base…" fondo="#F9FAFB" />
  }
  if (!estado) {
    return (
      <div className="flex-1 p-8" style={{ fontFamily: SG, backgroundColor: '#F9FAFB' }}>
        <Fallo mensaje={error ?? 'No se pudo leer el estado.'} alReintentar={() => void recargar()} />
      </div>
    )
  }

  const { base, calendario, frenos, atencion, cola, relevo, evaluador, gasto, entorno } = estado
  const esAdmin = gasto !== undefined
  const huerfanas = base.citas_sin_calendar

  // `CalendarioDoble` no debería poder existir (no negociable 1): dice que sí a todo y le
  // confirma al paciente una cita que no está en ninguna agenda. Si algún día aparece aquí,
  // la pantalla lo dice con esas palabras en vez de pintarlo como un estado más.
  const calTono: Tono =
    calendario.clase === 'CalendarioGoogle' ? 'bien' : calendario.clase === 'CalendarioDoble' ? 'mal' : 'mal'
  const calNota =
    calendario.clase === 'CalendarioGoogle'
      ? undefined
      : calendario.clase === 'CalendarioDoble'
        ? 'Esto no debería poder pasar. Un calendario doble confirma citas que no existen.'
        : 'Google no arrancó: Daniela no puede agendar aunque todo lo demás funcione.'

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden" style={{ fontFamily: SG, backgroundColor: '#F9FAFB' }}>
      <header
        className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b px-4 py-4 sm:px-8"
        style={{ backgroundColor: '#FFFFFF', borderColor: '#DCD8E6' }}
      >
        <div className="min-w-0">
          <h1 className="text-lg font-bold" style={{ color: '#16111F' }}>
            Estado del sistema
          </h1>
          <p className="text-sm" style={{ color: '#6B6480' }}>
            Lo que el proceso y la base saben ahora mismo. No sale a internet.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void recargar()}
          disabled={cargando}
          className={BOTON}
          style={{ minHeight: 38, backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6', color: '#4C1D95' }}
        >
          {cargando ? 'Mirando…' : 'Actualizar'}
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-8">
        <div className="mx-auto flex max-w-5xl flex-col gap-7">
          {error && <Fallo mensaje={error} alReintentar={() => void recargar()} />}

          <section className="flex flex-col gap-2">
            <Rotulo>Lo esencial</Rotulo>
            <Rejilla>
              <Tarjeta
                titulo="Base de datos"
                valor={base.ok ? 'Responde' : 'No responde'}
                tono={base.ok ? 'bien' : 'mal'}
                nota={base.ok ? undefined : 'Lo de abajo que salga «sin dato» es por esto.'}
              />
              <Tarjeta titulo="Calendario" valor={calendario.clase} tono={calTono} nota={calNota} />
              <Tarjeta
                titulo="Citas en Google"
                valor={huerfanas === null ? 'sin dato' : huerfanas === 0 ? 'Todas puestas' : `${huerfanas} sin llegar`}
                tono={huerfanas === null ? 'mudo' : huerfanas === 0 ? 'bien' : 'ojo'}
                nota={
                  huerfanas
                    ? 'Confirmadas al paciente y que la clínica no ve en su agenda.'
                    : undefined
                }
              />
              <Tarjeta
                titulo="Relevo por Telegram"
                valor={relevo.secreto_del_webhook ? 'Con secreto' : 'Sin secreto'}
                tono={relevo.secreto_del_webhook ? 'bien' : 'mal'}
                nota={
                  relevo.secreto_del_webhook
                    ? undefined
                    : 'El webhook responde 403 a todo: «Hablar yo con el paciente» no hace nada, y no queda error en ningún log.'
                }
              />
            </Rejilla>
          </section>

          <section className="flex flex-col gap-2">
            <div className="flex flex-col gap-0.5">
              <Rotulo>Frenos de mano</Rotulo>
              <p className="px-2.5 text-xs" style={{ color: '#6B6480' }}>
                Son tres e independientes: apagar uno no apaga los otros. Se mueven en el
                <code style={{ fontFamily: MONO }}> .env</code> del servidor, no desde aquí.
              </p>
            </div>
            <Rejilla>
              <Tarjeta
                titulo="Daniela responde"
                valor={frenos.daniela_responde ? 'Encendida' : 'APAGADA'}
                tono={frenos.daniela_responde ? 'bien' : 'ojo'}
                nota={frenos.daniela_responde ? undefined : 'Los archivos siguen llegando al doctor.'}
              />
              <Tarjeta
                titulo="Lector de archivos"
                valor={frenos.leer_archivos ? 'Encendido' : 'APAGADO'}
                tono={frenos.leer_archivos ? 'bien' : 'ojo'}
                nota={frenos.leer_archivos ? undefined : 'El archivo llega igual; lo que falta es la lectura.'}
              />
              <Tarjeta
                titulo="Transcripción de audios"
                valor={frenos.transcribir_audio ? 'Encendida' : 'APAGADA'}
                tono={frenos.transcribir_audio ? 'bien' : 'ojo'}
                nota={frenos.transcribir_audio ? undefined : 'Se le pide al paciente que lo escriba.'}
              />
            </Rejilla>
          </section>

          <section className="flex flex-col gap-2">
            <Rotulo>Atención y colas</Rotulo>
            <Rejilla>
              <Tarjeta
                titulo="Sin responder"
                valor={cifra(atencion.sin_responder)}
                tono={atencion.sin_responder ? 'ojo' : atencion.sin_responder === 0 ? 'bien' : 'mudo'}
                nota="Últimas 24 h. Vuelve a cero solo."
              />
              {/* La señal de alarma del no negociable 14, y la que más caro sale perder: un
                  archivo clínico que entró y no llegó al doctor. `/salud` la publica desde
                  siempre, pero eso lo mira Docker, no una persona. */}
              <Tarjeta
                titulo="Sin entregar al doctor"
                valor={cifra(atencion.sin_entregar)}
                tono={atencion.sin_entregar ? 'mal' : atencion.sin_entregar === 0 ? 'bien' : 'mudo'}
                nota={
                  atencion.sin_entregar
                    ? 'Entraron y el reenvío a Telegram falló. Puede haber una radiografía ahí.'
                    : undefined
                }
              />
              <Tarjeta
                titulo="Relevos abiertos"
                valor={cifra(atencion.relevos_abiertos)}
                tono="mudo"
                nota="Conversaciones que tiene tomadas un doctor."
              />
              <Tarjeta
                titulo="Recordatorios en cola"
                valor={cifra(cola.recordatorios_por_despachar)}
                tono="mudo"
                nota="Por despachar ahora mismo."
              />
              <Tarjeta
                titulo="Reactivaciones hoy"
                valor={cifra(cola.reactivaciones_hoy)}
                tono="mudo"
                nota="Contra el tope diario de Configuración."
              />
              <Tarjeta
                titulo="Fallos del evaluador"
                valor={String(evaluador.fallos_en_la_ventana)}
                tono={evaluador.fallos_en_la_ventana ? 'ojo' : 'bien'}
                // El contador vive en memoria del proceso, no en la base: un despliegue lo
                // pone a cero. Decir «hoy» sería mentir la mitad de las veces.
                nota="Desde el último arranque. Un pico es la firma de alguien probando el guardrail."
              />
              {esAdmin && gasto && (
                <Tarjeta
                  titulo="Gasto de hoy"
                  valor={gasto.usd_hoy === null ? 'sin dato' : `$${gasto.usd_hoy.toFixed(2)} USD`}
                  tono={
                    gasto.usd_hoy === null
                      ? 'mudo'
                      : gasto.usd_hoy >= gasto.umbral_usd
                        ? 'ojo'
                        : 'bien'
                  }
                  nota={`Avisa al pasar de $${gasto.umbral_usd} USD.`}
                />
              )}
            </Rejilla>
          </section>

          {esAdmin && entorno && (
            <section className="flex flex-col gap-2">
              <Rotulo>Credenciales</Rotulo>
              {entorno.faltan.length === 0 ? (
                <Rejilla>
                  <Tarjeta titulo="Variables de entorno" valor="Todas puestas" tono="bien" />
                </Rejilla>
              ) : (
                <div className="px-4 py-3" style={{ backgroundColor: '#FEF2F2', border: '1px solid #FECACA' }}>
                  <p className="text-sm font-semibold" style={{ color: '#991B1B' }}>
                    Faltan {entorno.faltan.length} variables en el servidor
                  </p>
                  <ul className="mt-2 flex flex-col gap-1">
                    {entorno.faltan.map((v) => (
                      <li key={v} style={{ fontFamily: MONO, fontSize: 12, color: '#B91C1C' }}>
                        {v}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          )}

          {esAdmin && (
            <section className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-col gap-0.5">
                  <Rotulo>Meta y Telegram</Rotulo>
                  <p className="px-2.5 text-xs" style={{ color: '#6B6480' }}>
                    Estas dos salen a internet, así que no se preguntan solas: si Meta está
                    caída, la pantalla que sirve para saber qué falla no debe dejar de cargar.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void sondear()}
                  disabled={sondeando}
                  className={BOTON}
                  style={{ minHeight: 38, backgroundColor: '#7C3AED', color: '#FFFFFF' }}
                >
                  {sondeando ? 'Preguntando…' : 'Comprobar ahora'}
                </button>
              </div>

              {errorSonda && <Fallo mensaje={errorSonda} alReintentar={() => void sondear()} />}

              {sondas && (
                <div className="flex flex-col gap-3">
                  <Bloque titulo="Plantillas de WhatsApp">
                    {'error' in sondas.whatsapp ? (
                      <Malo texto={sondas.whatsapp.error} />
                    ) : sondas.whatsapp.plantillas.length === 0 ? (
                      <p className="text-xs" style={{ color: '#6B6480' }}>
                        Meta no devolvió ninguna. Puede ser que el token no alcance la cuenta de
                        negocio: «ninguna» aquí no significa «no hay».
                      </p>
                    ) : (
                      <ul className="flex flex-col gap-1.5">
                        {sondas.whatsapp.plantillas.map((p) => (
                          <li key={`${p.nombre}-${p.idioma}`} className="flex flex-wrap items-center gap-2">
                            <span style={{ fontFamily: MONO, fontSize: 12, color: '#16111F' }}>{p.nombre}</span>
                            <span style={{ fontFamily: MONO, fontSize: 10, color: '#9A93AD' }}>{p.idioma}</span>
                            <span
                              style={{
                                fontFamily: MONO,
                                fontSize: 9.5,
                                letterSpacing: '0.1em',
                                padding: '3px 7px',
                                backgroundColor: p.estado === 'APPROVED' ? '#DCFCE7' : '#FEE2E2',
                                color: p.estado === 'APPROVED' ? '#166534' : '#991B1B',
                              }}
                            >
                              {p.estado}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {!('error' in sondas.whatsapp) && sondas.whatsapp.calidad?.quality_rating && (
                      <p className="mt-2 text-xs" style={{ color: '#6B6480' }}>
                        Calidad del número: <strong>{sondas.whatsapp.calidad.quality_rating}</strong>
                        {sondas.whatsapp.calidad.messaging_limit_tier
                          ? ` · límite ${sondas.whatsapp.calidad.messaging_limit_tier}`
                          : ''}
                      </p>
                    )}
                  </Bloque>

                  <Bloque titulo="Webhook de Telegram">
                    {'error' in sondas.telegram ? (
                      <Malo texto={sondas.telegram.error} />
                    ) : (
                      <div className="flex flex-col gap-1.5 text-xs" style={{ color: '#4B4458' }}>
                        {/* Los DOS hechos, separados. `getWebhookInfo` no reporta el secreto:
                            juntarlos en un solo semáforo sería inventarse la mitad. */}
                        <p>
                          URL registrada en Telegram:{' '}
                          <span style={{ fontFamily: MONO }}>{sondas.telegram.url || '— NO HAY —'}</span>
                        </p>
                        <p>
                          Secreto configurado en este proceso:{' '}
                          <strong>{sondas.secreto_del_webhook ? 'sí' : 'no'}</strong>{' '}
                          <span style={{ color: '#9A93AD' }}>
                            (Telegram no lo reporta; la única prueba de que coincide es que no
                            haya un 403 aquí abajo)
                          </span>
                        </p>
                        <p>Actualizaciones encoladas: {sondas.telegram.pendientes}</p>
                        {sondas.telegram.ultimo_error && (
                          <p style={{ color: '#B91C1C' }}>
                            Último error: {sondas.telegram.ultimo_error}
                          </p>
                        )}
                      </div>
                    )}
                  </Bloque>
                </div>
              )}
            </section>
          )}

          <p className="text-xs leading-relaxed" style={{ color: '#9A93AD' }}>
            No hay una marca de «última sincronización» con Google: la reconciliación corre
            cuando alguien abre la Agenda o cuando un paciente pregunta por su cita, y no deja
            hora. Lo que se mide arriba son las citas que no llegaron.
          </p>
        </div>
      </div>
    </div>
  )
}

function Bloque({ titulo, children }: { titulo: string; children: React.ReactNode }) {
  return (
    <div className="px-4 py-3" style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6' }}>
      <p
        className="mb-2"
        style={{ fontFamily: MONO, fontSize: 9.5, letterSpacing: '0.12em', textTransform: 'uppercase', color: '#6B6480' }}
      >
        {titulo}
      </p>
      {children}
    </div>
  )
}

function Malo({ texto }: { texto: string }) {
  return (
    <p className="text-xs" style={{ color: '#B91C1C' }}>
      No contestó: {texto}
    </p>
  )
}

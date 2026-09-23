import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { SesionCaducada, cambiarContrasena } from '@/api'

/* El modal de «cambiar mi contraseña», desde el pie del menú.
 *
 * Hasta el 22/09/2026 el panel no tenía ninguno: la única forma de poner o restablecer una
 * clave era `scripts/crear_usuario.py`, o sea alguien con SSH y el `.env` delante. Eso hacía
 * que la clave con la que se crea una cuenta fuera la clave de por vida, y convertía «se me
 * filtró» en una llamada a un desarrollador.
 *
 * Mismo dialecto que el modal de borrar una conversación (`pantallas/Conversaciones.tsx`):
 * hexadecimales literales del archivo de diseño, Escape cierra, y clic en el fondo cierra. */

const SG = "'Space Grotesk', sans-serif"
const MONO = "'JetBrains Mono', monospace"
const BOTON =
  'px-4 text-sm font-bold transition-all disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-95'
const CAMPO = 'w-full px-3 py-2 text-sm outline-none transition-all disabled:opacity-60'

/** Lo mismo que exige `autenticacion.MINIMO_CONTRASENA` en el servidor.
 *
 * Está duplicado a sabiendas y la duplicación va en una sola dirección: esto solo sirve para
 * no hacer ir y volver una petición que se sabe rechazada, y **el servidor vuelve a
 * comprobarlo**. Si algún día allí sube a 16, aquí seguiría diciendo 12 y el único efecto
 * sería que el aviso llega del servidor en vez de antes de enviarlo -- nunca que pase una
 * contraseña corta. */
const MINIMO = 12

type Props = {
  /** Para el título: «la contraseña de Dra. Ruiz» dice más que «tu contraseña». */
  nombre: string
  alCerrar: () => void
  alCaducarSesion: () => void
}

export default function CambiarContrasena({ nombre, alCerrar, alCaducarSesion }: Props) {
  const [actual, setActual] = useState('')
  const [nueva, setNueva] = useState('')
  const [repetida, setRepetida] = useState('')
  const [ver, setVer] = useState(false)
  const [guardando, setGuardando] = useState(false)
  const [error, setError] = useState('')
  const [listo, setListo] = useState(false)

  const cerrar = useCallback(() => {
    if (!guardando) alCerrar()
  }, [guardando, alCerrar])

  // Escape cierra, salvo mientras se guarda: cerrar a mitad del envío dejaría a la persona
  // sin saber si su contraseña cambió o no, que es el peor estado posible con una clave.
  useEffect(() => {
    const alTeclear = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cerrar()
    }
    document.addEventListener('keydown', alTeclear)
    return () => document.removeEventListener('keydown', alTeclear)
  }, [cerrar])

  /* Las dos comprobaciones que se pueden hacer sin preguntarle al servidor. La de «no
   * coinciden» NO existe en el servidor y no tiene por qué: allí solo llega una contraseña,
   * y repetirla es una defensa contra el dedo de quien escribe, no contra nadie más. */
  const corta = nueva.length > 0 && nueva.length < MINIMO
  const distintas = repetida.length > 0 && nueva !== repetida
  const puede = actual.length > 0 && nueva.length >= MINIMO && nueva === repetida

  async function enviar(e: FormEvent) {
    e.preventDefault()
    if (!puede) return
    setGuardando(true)
    setError('')
    try {
      await cambiarContrasena(actual, nueva)
      setListo(true)
    } catch (err) {
      if (err instanceof SesionCaducada) alCaducarSesion()
      else setError(err instanceof Error ? err.message : 'No se pudo cambiar la contraseña.')
      setGuardando(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(22, 17, 31, 0.55)' }}
      onClick={(e) => {
        if (e.target === e.currentTarget) cerrar()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="titulo-clave"
        className="flex w-full flex-col gap-4 overflow-y-auto"
        style={{
          maxWidth: '26rem',
          maxHeight: '90vh',
          backgroundColor: '#FFFFFF',
          border: '1px solid #DCD8E6',
          padding: '22px',
        }}
      >
        <h2
          id="titulo-clave"
          style={{
            margin: 0,
            fontFamily: SG,
            fontWeight: 600,
            fontSize: '18px',
            letterSpacing: '-0.022em',
            color: '#16111F',
          }}
        >
          {listo ? 'Contraseña cambiada' : `Cambiar la contraseña de ${nombre}`}
        </h2>

        {listo ? (
          <>
            <p style={{ margin: 0, fontSize: '13.5px', color: '#2C2439' }}>
              La próxima vez que entres, usa la nueva.
            </p>
            {/* El límite del diseño, dicho donde importa y no en un docstring que nadie
                abre: el token de sesión se valida con matemática, no consultando una tabla,
                así que cambiar la clave no expulsa a nadie que ya estuviera dentro. Quien
                cambia su contraseña porque cree que se la robaron tiene que saberlo. */}
            <p
              style={{
                margin: 0,
                fontSize: '13px',
                color: '#6E6880',
                backgroundColor: '#F9FAFB',
                border: '1px solid #EDEAF3',
                padding: '11px',
              }}
            >
              Si crees que alguien más tenía tu contraseña: las sesiones que ya estuvieran
              abiertas <strong>siguen abiertas</strong> hasta 8 horas. Avisa para cerrarlas
              todas de golpe.
            </p>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={alCerrar}
                className={`${BOTON} py-2`}
                style={{ backgroundColor: '#4C1D95', color: '#FFFFFF' }}
              >
                Listo
              </button>
            </div>
          </>
        ) : (
          <form className="flex flex-col gap-4" onSubmit={(e) => void enviar(e)}>
            <Campo
              etiqueta="Tu contraseña actual"
              valor={actual}
              alCambiar={setActual}
              ver={ver}
              deshabilitado={guardando}
              autoFocus
              autoComplete="current-password"
            />
            <div className="flex flex-col gap-1.5">
              <Campo
                etiqueta="La nueva"
                valor={nueva}
                alCambiar={setNueva}
                ver={ver}
                deshabilitado={guardando}
                autoComplete="new-password"
              />
              <p style={{ margin: 0, fontSize: '12px', color: corta ? '#B91C1C' : '#6E6880' }}>
                Mínimo {MINIMO} caracteres. Una frase que recuerdes vale más que ocho
                caracteres con un símbolo.
              </p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Campo
                etiqueta="La nueva, otra vez"
                valor={repetida}
                alCambiar={setRepetida}
                ver={ver}
                deshabilitado={guardando}
                autoComplete="new-password"
              />
              {distintas ? (
                <p style={{ margin: 0, fontSize: '12px', color: '#B91C1C' }}>
                  Las dos no coinciden.
                </p>
              ) : null}
            </div>

            <label className="flex items-center gap-2" style={{ fontSize: '13px', color: '#4A4458' }}>
              <input
                type="checkbox"
                checked={ver}
                onChange={(e) => setVer(e.target.checked)}
                disabled={guardando}
              />
              Ver lo que escribo
            </label>

            {error ? (
              <p role="alert" style={{ margin: 0, fontSize: '13px', color: '#B91C1C' }}>
                {error}
              </p>
            ) : null}

            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                onClick={cerrar}
                disabled={guardando}
                className={`${BOTON} py-2`}
                style={{ backgroundColor: '#FFFFFF', color: '#4A4458', border: '1px solid #DCD8E6' }}
              >
                Cancelar
              </button>
              <button
                type="submit"
                disabled={!puede || guardando}
                className={`${BOTON} py-2`}
                style={{ backgroundColor: '#4C1D95', color: '#FFFFFF' }}
              >
                {guardando ? 'Guardando…' : 'Cambiar la contraseña'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

function Campo({
  etiqueta,
  valor,
  alCambiar,
  ver,
  deshabilitado,
  autoFocus = false,
  autoComplete,
}: {
  etiqueta: string
  valor: string
  alCambiar: (v: string) => void
  ver: boolean
  deshabilitado: boolean
  autoFocus?: boolean
  autoComplete: string
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span
        style={{
          fontFamily: MONO,
          fontSize: '10.5px',
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: '#6E6880',
        }}
      >
        {etiqueta}
      </span>
      <input
        // `type` cambia con la casilla de «ver lo que escribo». El navegador se queda con el
        // `autoComplete`, que es lo que le dice al gestor de contraseñas cuál es cuál.
        type={ver ? 'text' : 'password'}
        value={valor}
        onChange={(e) => alCambiar(e.target.value)}
        disabled={deshabilitado}
        autoFocus={autoFocus}
        autoComplete={autoComplete}
        className={CAMPO}
        style={{ backgroundColor: '#FFFFFF', border: '1px solid #DCD8E6', color: '#16111F' }}
      />
    </label>
  )
}

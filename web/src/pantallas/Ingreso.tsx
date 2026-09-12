import { useState, type FormEvent } from 'react'
import FondoEspacial from '@/componentes/FondoEspacial'
import { LogoSinPiloto } from '@/componentes/Marca'
import { entrar, type Sesion } from '@/api'

const SP = "'Space Grotesk', sans-serif"

/* La pantalla de ingreso de Figma Make, con una diferencia que no es cosmética: allí el
 * botón esperaba 1.200 ms con un `setTimeout` y daba por bueno cualquier usuario. Aquí
 * pregunta al servidor.
 *
 * Y trae el estado que el diseño pedía y el prototipo no tenía: contraseña equivocada. Con
 * un texto que NO distingue «ese usuario no existe» de «la contraseña no es esa» -- decirlo
 * le confirmaría a quien prueba nombres cuáles existen. */

function IconoOjo({ abierto }: { abierto: boolean }) {
  return abierto ? (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ) : (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  )
}

export default function Ingreso({ alEntrar }: { alEntrar: (s: Sesion) => void }) {
  const [usuario, setUsuario] = useState('')
  const [contrasena, setContrasena] = useState('')
  const [verClave, setVerClave] = useState(false)
  const [cargando, setCargando] = useState(false)
  const [error, setError] = useState('')
  const [enfocado, setEnfocado] = useState<string | null>(null)

  async function enviar(e: FormEvent) {
    e.preventDefault()
    setCargando(true)
    setError('')
    try {
      alEntrar(await entrar(usuario, contrasena))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo ingresar.')
      setContrasena('')
    } finally {
      setCargando(false)
    }
  }

  const deshabilitado = cargando || !usuario || !contrasena
  const borde = (campo: string) =>
    enfocado === campo ? '#7C3AED' : error ? 'rgba(248,113,113,0.45)' : 'rgba(196,181,253,0.12)'

  return (
    <div className="relative min-h-screen flex flex-col" style={{ fontFamily: SP }}>
      <FondoEspacial />
      <header className="relative z-10 px-8 pt-7">
        <LogoSinPiloto scale={1.3} />
      </header>

      <main className="relative z-10 flex-1 flex items-center justify-center px-6 py-12">
        <div
          className="w-full max-w-[380px] rounded-3xl p-9 relative"
          style={{
            background: 'linear-gradient(145deg, rgba(22,14,40,0.75) 0%, rgba(10,7,20,0.82) 100%)',
            border: '1px solid rgba(196,181,253,0.14)',
            backdropFilter: 'blur(40px) saturate(1.4)',
            WebkitBackdropFilter: 'blur(40px) saturate(1.4)',
            boxShadow: [
              '0 0 0 1px rgba(196,181,253,0.06)',
              'inset 0 1px 0 rgba(196,181,253,0.1)',
              'inset 0 -1px 0 rgba(0,0,0,0.3)',
              '0 40px 100px rgba(7,6,11,0.75)',
              '0 0 80px rgba(124,58,237,0.12)',
            ].join(', '),
          }}
        >
          <div
            className="absolute top-0 left-8 right-8 h-px rounded-full"
            style={{ background: 'linear-gradient(90deg, transparent, rgba(196,181,253,0.3), transparent)' }}
          />

          <div className="mb-8">
            <p className="text-xs font-semibold tracking-widest uppercase mb-3" style={{ color: '#7C3AED' }}>
              Portal de acceso
            </p>
            <h1 className="text-2xl font-bold leading-tight mb-1" style={{ color: '#FFFFFF', letterSpacing: '-0.02em' }}>
              Bienvenido equipo
              <br />
              <span style={{ color: '#C4B5FD' }}>MaxiCare</span>
            </h1>
            <p className="text-sm mt-2" style={{ color: 'rgba(161,158,171,0.8)' }}>
              Ingresa tus credenciales para continuar
            </p>
          </div>

          <div className="mb-7 h-px" style={{ background: 'rgba(196,181,253,0.1)' }} />

          <form onSubmit={enviar} className="flex flex-col gap-5">
            <div>
              <label
                htmlFor="usuario"
                className="block text-xs font-semibold tracking-widest uppercase mb-2"
                style={{ color: 'rgba(196,181,253,0.6)' }}
              >
                Usuario
              </label>
              <input
                id="usuario"
                type="text"
                value={usuario}
                onChange={(e) => setUsuario(e.target.value)}
                placeholder="tu.usuario"
                required
                autoComplete="username"
                autoFocus
                onFocus={() => setEnfocado('usuario')}
                onBlur={() => setEnfocado(null)}
                className="w-full px-4 py-3 text-sm rounded-xl outline-none transition-all duration-200"
                style={{
                  backgroundColor: 'rgba(196,181,253,0.05)',
                  border: `1px solid ${borde('usuario')}`,
                  boxShadow: enfocado === 'usuario' ? '0 0 0 3px rgba(124,58,237,0.15)' : 'none',
                  color: '#FFFFFF',
                  fontFamily: SP,
                }}
              />
            </div>

            <div>
              <label
                htmlFor="contrasena"
                className="block text-xs font-semibold tracking-widest uppercase mb-2"
                style={{ color: 'rgba(196,181,253,0.6)' }}
              >
                Contraseña
              </label>
              <div className="relative">
                <input
                  id="contrasena"
                  type={verClave ? 'text' : 'password'}
                  value={contrasena}
                  onChange={(e) => setContrasena(e.target.value)}
                  placeholder="••••••••••••"
                  required
                  autoComplete="current-password"
                  onFocus={() => setEnfocado('contrasena')}
                  onBlur={() => setEnfocado(null)}
                  className="w-full px-4 py-3 pr-11 text-sm rounded-xl outline-none transition-all duration-200"
                  style={{
                    backgroundColor: 'rgba(196,181,253,0.05)',
                    border: `1px solid ${borde('contrasena')}`,
                    boxShadow: enfocado === 'contrasena' ? '0 0 0 3px rgba(124,58,237,0.15)' : 'none',
                    color: '#FFFFFF',
                    fontFamily: SP,
                  }}
                />
                <button
                  type="button"
                  onClick={() => setVerClave(!verClave)}
                  aria-label={verClave ? 'Ocultar contraseña' : 'Mostrar contraseña'}
                  className="absolute right-3.5 top-1/2 -translate-y-1/2"
                  style={{ color: 'rgba(161,158,171,0.6)' }}
                >
                  <IconoOjo abierto={verClave} />
                </button>
              </div>
            </div>

            {/* El estado que el prototipo no tenía. `role="alert"` para que un lector de
                pantalla lo anuncie: sin eso, quien no ve la pantalla vuelve a enviar el
                formulario sin enterarse de que falló. */}
            {error && (
              <div
                role="alert"
                className="rounded-xl px-4 py-3 text-sm"
                style={{
                  backgroundColor: 'rgba(127,29,29,0.35)',
                  border: '1px solid rgba(248,113,113,0.35)',
                  color: '#FCA5A5',
                }}
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={deshabilitado}
              className="w-full py-3 rounded-xl text-sm font-bold flex items-center justify-center gap-2 transition-all duration-200 mt-1"
              style={{
                background: deshabilitado ? 'rgba(124,58,237,0.35)' : 'linear-gradient(135deg, #7C3AED 0%, #6d28d9 100%)',
                color: deshabilitado ? 'rgba(255,255,255,0.4)' : '#FFFFFF',
                cursor: deshabilitado ? 'not-allowed' : 'pointer',
                boxShadow: deshabilitado ? 'none' : '0 4px 24px rgba(124,58,237,0.45)',
                fontFamily: SP,
              }}
            >
              {cargando ? (
                <>
                  <svg className="animate-spin" width="16" height="16" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="10" stroke="rgba(255,255,255,0.3)" strokeWidth="2.5" />
                    <path d="M12 2a10 10 0 0 1 10 10" stroke="white" strokeWidth="2.5" strokeLinecap="round" />
                  </svg>
                  Verificando…
                </>
              ) : (
                <>
                  Ingresar al sistema
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </>
              )}
            </button>
          </form>

          <p className="text-center text-[11px] mt-7" style={{ color: 'rgba(161,158,171,0.4)', fontFamily: SP }}>
            sinpiloto.co · Agentes de Inteligencia Artificial que empiezan por el negocio
          </p>
        </div>
      </main>

      <footer className="relative z-10 text-center px-8 pb-7">
        <p className="text-[11px]" style={{ color: 'rgba(161,158,171,0.3)', fontFamily: SP }}>
          © 2026 SinPiloto Technologies · Todos los derechos reservados
        </p>
      </footer>
    </div>
  )
}

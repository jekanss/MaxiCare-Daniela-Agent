import { useEffect, useRef } from 'react'

/* El fondo animado de la pantalla de ingreso, tal como lo trajo Figma Make.
 *
 * Solo se le añadió `prefers-reduced-motion`: quien pidió a su sistema operativo que no le
 * animen la pantalla -- por vértigo, por migraña, o por una tableta vieja -- recibe el mismo
 * fondo, quieto. Se dibuja un fotograma y se para; no se deja en negro, que sería otra
 * pantalla distinta. */

type Estrella = {
  x: number
  y: number
  z: number
  r: number
  parpadeo: number
  velocidad: number
  violeta: boolean
}

export default function FondoEspacial() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const quieto = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

    let animId = 0
    let t = 0
    const estrellas: Estrella[] = []

    function medir() {
      if (!canvas) return
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
    }

    function sembrar() {
      estrellas.length = 0
      const cuantas = Math.floor((window.innerWidth * window.innerHeight) / 9000)
      for (let i = 0; i < cuantas; i++) {
        estrellas.push({
          x: Math.random() * window.innerWidth,
          y: Math.random() * window.innerHeight,
          z: Math.random(),
          r: Math.random() * 0.55 + 0.1,
          parpadeo: Math.random() * Math.PI * 2,
          velocidad: Math.random() * 0.004 + 0.001,
          violeta: Math.random() > 0.88,
        })
      }
    }

    function nebulosa() {
      if (!canvas || !ctx) return
      const W = canvas.width
      const H = canvas.height

      const nc1x = W * 0.58 + Math.sin(t * 0.0006) * 110 + Math.cos(t * 0.00038) * 50
      const nc1y = H * 0.38 + Math.cos(t * 0.00048) * 80 + Math.sin(t * 0.00032) * 35
      const g1 = ctx.createRadialGradient(nc1x, nc1y, 0, nc1x, nc1y, W * 0.54)
      g1.addColorStop(0, 'rgba(100,32,210,0.30)')
      g1.addColorStop(0.28, 'rgba(124,58,237,0.16)')
      g1.addColorStop(0.6, 'rgba(76,29,149,0.08)')
      g1.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = g1
      ctx.fillRect(0, 0, W, H)

      const pulso = 0.82 + 0.18 * Math.sin(t * 0.0022)
      const gcx = nc1x + Math.cos(t * 0.0009) * 60
      const gcy = nc1y + Math.sin(t * 0.0011) * 40
      const gc = ctx.createRadialGradient(gcx, gcy, 0, gcx, gcy, W * 0.2 * pulso)
      gc.addColorStop(0, 'rgba(196,181,253,0.24)')
      gc.addColorStop(0.4, 'rgba(139,92,246,0.14)')
      gc.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = gc
      ctx.fillRect(0, 0, W, H)

      const nc2x = W * 0.28 + Math.sin(t * 0.00042 + 1.2) * 130
      const nc2y = H * 0.62 + Math.cos(t * 0.00035 + 0.8) * 90
      const g2 = ctx.createRadialGradient(nc2x, nc2y, 0, nc2x, nc2y, W * 0.38)
      g2.addColorStop(0, 'rgba(88,28,180,0.22)')
      g2.addColorStop(0.5, 'rgba(59,7,100,0.10)')
      g2.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = g2
      ctx.fillRect(0, 0, W, H)

      const nc3x = W * 0.75 + Math.cos(t * 0.00028) * 90
      const nc3y = H * 0.2 + Math.sin(t * 0.00033) * 55
      const g3 = ctx.createRadialGradient(nc3x, nc3y, 0, nc3x, nc3y, W * 0.26)
      g3.addColorStop(0, 'rgba(109,40,217,0.17)')
      g3.addColorStop(0.6, 'rgba(76,29,149,0.07)')
      g3.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = g3
      ctx.fillRect(0, 0, W, H)
    }

    function puntos() {
      if (!ctx) return
      for (const e of estrellas) {
        e.parpadeo += e.velocidad
        const titileo = quieto ? 1 : 0.5 + 0.5 * Math.sin(e.parpadeo)
        const alfa = (0.1 + e.z * 0.35) * titileo
        const radio = e.r * (0.4 + e.z * 0.6)
        ctx.beginPath()
        ctx.arc(e.x, e.y, radio, 0, Math.PI * 2)
        ctx.fillStyle = e.violeta ? `rgba(196,181,253,${alfa})` : `rgba(255,255,255,${alfa})`
        ctx.fill()
      }
    }

    function pintar() {
      if (!canvas || !ctx) return
      t++
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      ctx.fillStyle = '#07060B'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      nebulosa()
      puntos()
      if (!quieto) animId = requestAnimationFrame(pintar)
    }

    medir()
    sembrar()
    pintar()

    const alRedimensionar = () => {
      medir()
      sembrar()
      if (quieto) pintar()
    }
    window.addEventListener('resize', alRedimensionar)
    return () => {
      cancelAnimationFrame(animId)
      window.removeEventListener('resize', alRedimensionar)
    }
  }, [])

  return <canvas ref={canvasRef} className="fixed inset-0 w-full h-full" style={{ zIndex: 0 }} />
}

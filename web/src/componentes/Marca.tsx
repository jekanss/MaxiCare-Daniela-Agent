import svgPaths from '@/marca/logo-sinpiloto'

/* El logo tal como vino de Figma: los trazados se pintan desde el archivo exportado, con la
 * geometría original (18.0633 x 15.7032 el símbolo, 97.511 x 19.9016 la palabra) escalada
 * por un factor. No se sustituyó por un icono parecido ni se redibujó a mano. */

export function SimboloSinPiloto({ size = 1 }: { size?: number }) {
  return (
    <svg fill="none" height={15.7032 * size} viewBox="0 0 18.0633 15.7032" width={18.0633 * size}>
      <path d={svgPaths.p3bec4540} stroke="#C4B5FD" strokeDasharray="0.41 2.6" strokeLinecap="round" strokeOpacity="0.8" strokeWidth="0.932258" />
      <path d={svgPaths.p5bdf400} fill="#C4B5FD" />
      <path d={svgPaths.pee9a440} fill="url(#lgrad)" />
      <path d={svgPaths.p1aff3100} fill="white" fillOpacity="0.95" />
      <defs>
        <linearGradient gradientUnits="userSpaceOnUse" id="lgrad" x1="7.04262" x2="16.6546" y1="1.30158" y2="5.5179">
          <stop stopColor="#D6C9FF" />
          <stop offset="1" stopColor="#7C3AED" />
        </linearGradient>
      </defs>
    </svg>
  )
}

export function PalabraSinPiloto({ size = 1 }: { size?: number }) {
  return (
    <svg fill="none" height={19.9016 * size} viewBox="0 0 97.511 19.9016" width={97.511 * size}>
      <path d={svgPaths.p3d5e2780} fill="white" />
      <path d={svgPaths.p12a220f0} fill="#A78BFA" />
    </svg>
  )
}

export function LogoSinPiloto({ scale = 1 }: { scale?: number }) {
  return (
    <div className="flex items-center gap-2.5">
      <SimboloSinPiloto size={scale} />
      <PalabraSinPiloto size={scale} />
    </div>
  )
}

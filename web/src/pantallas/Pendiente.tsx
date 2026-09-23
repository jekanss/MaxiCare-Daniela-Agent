import type { Seccion } from '@/componentes/Sidebar'

const SP = "'Space Grotesk', sans-serif"

/* El estado vacío de una pantalla que todavía no existe.
 *
 * Dice tres cosas, y las tres hacen falta: qué va a hacer (sacado de la especificación del
 * producto, no inventado aquí), qué fase la construye, y qué le falta al sistema antes de
 * poder construirla. Sin lo tercero, «fase 8» es una fecha sin causa. */

const QUE_HARA: Record<string, { hace: string; espera: string }> = {
  bandeja: {
    hace:
      'Una sola cola con las conversaciones que Daniela escaló, lo urgente primero y ' +
      'distinguible sin leer. El doctor toma el control, responde como humano y lo devuelve ' +
      'a Daniela — dos acciones explícitas, nunca automáticas.',
    espera:
      'El relevo por temas de Telegram, que es donde hoy ocurre de verdad. Mientras esta ' +
      'pantalla no exista, los doctores toman conversaciones desde Telegram, no desde aquí.',
  },
  leads: {
    hace:
      'Todas las personas que han escrito, con su tratamiento de interés, su estado y de qué ' +
      'anuncio llegaron. El filtro que más importa: «sin estado ni actividad programada».',
    espera:
      'Conversaciones reales acumuladas. Con la base vacía, la pantalla más útil del ' +
      'producto se ve idéntica a una rota.',
  },
  tratamientos: {
    hace:
      'Las fichas que los doctores editan —precio, qué incluye, garantías, objeciones— y de ' +
      'las que Daniela lee en vivo. Una ficha incompleta se ve incompleta, y mientras lo ' +
      'esté, Daniela no responde esa pregunta: escala.',
    espera:
      'Nada técnico. Lo que falta son datos de MaxiCare: el precio y el especialista de ' +
      'endodoncia y de prótesis, y los nombres de las especialistas de ortodoncia y ' +
      'periodoncia. Hoy están marcados PENDIENTE en la base de conocimiento.',
  },
  metricas: {
    hace:
      'Ocho indicadores, ni uno más, y ningún nombre de paciente. Desde «de conversación a ' +
      'valoración asistida» hasta el costo por valoración asistida — no por conversación.',
    espera:
      'Un mes de operación real. Dos de los ocho indicadores saldrán marcados como ' +
      'provisionales porque todavía no hay una medición anterior contra la cual compararlos.',
  },
  estado: {
    hace:
      'La salud operativa: citas que aún no llegaron a Google Calendar, última sincronización, ' +
      'estado de las plantillas de WhatsApp. Si Google se cae, las citas se siguen agendando ' +
      '—así está diseñado— pero alguien tiene que enterarse por aquí y no porque un doctor reclame.',
    espera:
      'La conexión con Google Calendar. Hoy `CalendarioGoogle` falla al construirse a ' +
      'propósito: faltan las credenciales y el identificador del calendario de la clínica.',
  },
  configuracion: {
    hace:
      'Dos mitades con riesgo muy distinto. La agenda —cuántas valoraciones caben en una ' +
      'franja, cuánto dura, horario— cambia en vivo. El comportamiento de Daniela se edita ' +
      'como borrador, con historial de versiones y un paso obligatorio de probarlo antes de ' +
      'publicarlo.',
    espera:
      'Las tres perillas de la agenda ya viven en la base y funcionan; lo que falta es la ' +
      'pantalla. El borrador de prompts con historial es alcance nuevo: no está en el plan ' +
      'de diseño todavía.',
  },
}

export default function Pendiente({ seccion }: { seccion: Seccion }) {
  const info = QUE_HARA[seccion.id]

  return (
    <div className="flex-1 overflow-y-auto" style={{ fontFamily: SP, backgroundColor: '#F9FAFB' }}>
      <header className="px-4 sm:px-8 py-4 border-b" style={{ backgroundColor: '#FFFFFF', borderColor: '#E5E7EB' }}>
        {/* El icono es un SVG de trazo desde que el menú pasó a oscuro, no un emoji: por
            eso va en una fila flexible y no pegado al texto, que lo dejaba fuera de la
            línea base del título. */}
        <h1 className="text-lg font-bold flex items-center gap-2" style={{ color: '#111827' }}>
          {seccion.icono} {seccion.etiqueta}
        </h1>
        <p className="text-sm" style={{ color: '#6B7280' }}>Todavía no construida</p>
      </header>

      <div className="max-w-2xl mx-auto px-4 sm:px-8 py-14">
        <div
          className="rounded-2xl p-8"
          style={{ backgroundColor: '#FFFFFF', border: '1px solid #E5E7EB' }}
        >
          <span
            className="inline-block text-[10px] font-bold px-2 py-1 rounded uppercase tracking-wide mb-5"
            style={{ backgroundColor: '#EDE9FE', color: '#5B21B6' }}
          >
            Fase {seccion.fase}
          </span>

          <h2 className="text-base font-semibold mb-2" style={{ color: '#111827' }}>
            Qué va a hacer esta pantalla
          </h2>
          <p className="text-sm leading-relaxed mb-7" style={{ color: '#4B5563' }}>
            {info?.hace ?? 'Está descrita en la especificación del producto.'}
          </p>

          <h2 className="text-base font-semibold mb-2" style={{ color: '#111827' }}>
            Qué falta antes
          </h2>
          <p className="text-sm leading-relaxed" style={{ color: '#4B5563' }}>
            {info?.espera ?? 'PENDIENTE.'}
          </p>

          <div className="mt-8 pt-6 border-t" style={{ borderColor: '#F3F4F6' }}>
            <p className="text-xs" style={{ color: '#9CA3AF' }}>
              Esta sección aparece en el menú a propósito, aunque no funcione. Esconderla
              daría una navegación limpia que miente sobre el tamaño del producto.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

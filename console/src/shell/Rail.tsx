import type { ModuleDefinition, ModuleId } from '../modules/types'

export interface ModuleNavProps {
  modules: readonly ModuleDefinition[]
  active: ModuleId
  onSelect: (id: ModuleId) => void
}

const RAIL_NOTE =
  'Control availability follows the relay capability profile and selected device classes. Requests show the relay outcome.'

export function Rail({ modules, active, onSelect }: ModuleNavProps) {
  return (
    <nav className="sh-rail" data-rail="1" aria-label="Modules">
      {modules.map((module) => {
        const current = module.id === active
        const classes = ['sh-rail-item']
        if (current) classes.push('is-current')
        return (
          <button
            key={module.id}
            type="button"
            className={classes.join(' ')}
            aria-current={current ? 'page' : undefined}
            onClick={() => onSelect(module.id)}
          >
            {module.label}
          </button>
        )
      })}
      <p className="sh-rail-note">{RAIL_NOTE}</p>
    </nav>
  )
}

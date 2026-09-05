import type { ModuleDefinition, ModuleId } from '../modules/types'

export interface ModuleNavProps {
  modules: readonly ModuleDefinition[]
  active: ModuleId
  onSelect: (id: ModuleId) => void
}

const RAIL_NOTE =
  'M2.0 — the console sends select, hold, capture_room and estop. Other controls are listed as unsupported until the relay accepts them.'

export function Rail({ modules, active, onSelect }: ModuleNavProps) {
  return (
    <nav className="sh-rail" data-rail="1" aria-label="Modules">
      {modules.map((module) => {
        const current = module.id === active
        const classes = ['sh-rail-item']
        if (current) classes.push('is-current')
        if (module.id === 'reference') classes.push('is-reference')
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

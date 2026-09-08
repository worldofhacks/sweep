import type { ModuleDefinition, ModuleId } from '../modules/types'
import { Icon, type IconName } from '../atlas/Icon'

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
      <div className="sh-rail-brand"><Icon name="spaces" size={24} /><span>sweep<span>FIELD WORKSPACE</span></span></div>
      <p className="sh-rail-section">WORKSPACE</p>
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
            <Icon name={(({ captures: 'camera', worlds: 'cube', map: 'pin' } as Record<string, string>)[module.id] ?? module.id) as IconName} size={18} />
            <span>{module.label}</span>
          </button>
        )
      })}
      <p className="sh-rail-note">{RAIL_NOTE}</p>
      <div className="sh-rail-footer"><span className="sh-rail-avatar">S</span><div><strong>Your workspace</strong><span>A shared perspective</span></div></div>
    </nav>
  )
}

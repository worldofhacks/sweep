import type { ModuleDefinition, ModuleId } from '../modules/types'
import { Icon, type IconName } from '../atlas/Icon'

export interface ModuleNavProps {
  modules: readonly ModuleDefinition[]
  active: ModuleId
  onSelect: (id: ModuleId) => void
}

const RAIL_NOTE = 'A little perspective can make a big difference. Start with a place you care about.'

export function Rail({ modules, active, onSelect }: ModuleNavProps) {
  return (
    <nav className="sh-rail" data-rail="1" aria-label="Modules">
      <div className="sh-rail-welcome"><span className="sh-rail-welcome-icon"><Icon name="people" size={24} /></span><strong>Better, together.</strong><p>Your places. Our shared picture.</p></div>
      <p className="sh-rail-section">EXPLORE & CONTRIBUTE</p>
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
      <div className="sh-rail-footer"><Icon name="pin" size={18} /><div><strong>Built around your world</strong><span>One perspective at a time</span></div></div>
    </nav>
  )
}

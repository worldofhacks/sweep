import type { ModuleNavProps } from './Rail'
import { Icon, type IconName } from '../atlas/Icon'

export function TabBar({ modules, active, onSelect }: ModuleNavProps) {
  return (
    <nav data-tabbar="1" aria-label="Primary">
      {modules.map((module) => {
        const current = module.id === active
        const classes = ['sh-tabbar-item']
        if (current) classes.push('is-current')
        return (
          <button
            key={module.id}
            type="button"
            className={classes.join(' ')}
            aria-current={current ? 'page' : undefined}
            onClick={() => onSelect(module.id)}
          >
            <Icon name={(({ captures: 'camera', worlds: 'cube', map: 'pin' } as Record<string, string>)[module.id] ?? module.id) as IconName} size={19} />
            <span>{module.label}</span>
          </button>
        )
      })}
    </nav>
  )
}

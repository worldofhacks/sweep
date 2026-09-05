import type { ModuleNavProps } from './Rail'

export function TabBar({ modules, active, onSelect }: ModuleNavProps) {
  return (
    <nav data-tabbar="1" aria-label="Primary">
      {modules.map((module) => {
        const current = module.id === active
        const classes = ['sh-tabbar-item']
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
    </nav>
  )
}

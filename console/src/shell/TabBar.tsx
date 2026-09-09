import { useEffect, useRef } from 'react'
import type { ModuleNavProps } from './Rail'
import { Icon, type IconName } from '../atlas/Icon'

export function TabBar({ modules, active, onSelect }: ModuleNavProps) {
  const nav = useRef<HTMLElement>(null)
  useEffect(() => {
    // A cross-workspace link can select a tab outside the current phone scroll position.
    nav.current?.querySelector('[aria-current="page"]')?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [active])
  return (
    <nav ref={nav} data-tabbar="1" aria-label="Primary">
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

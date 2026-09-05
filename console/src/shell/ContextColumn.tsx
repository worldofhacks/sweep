import type { ReactNode } from 'react'

export function ContextColumn({ rosterVersion, children }: { rosterVersion: number; children: ReactNode }) {
  return (
    <aside className="sh-context" data-context="1" aria-label="Fleet">
      <p className="sh-context-head">Fleet · roster v{rosterVersion}</p>
      <div className="sh-context-scroll" data-scroll="1">
        {children}
      </div>
    </aside>
  )
}

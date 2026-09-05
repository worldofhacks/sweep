import type { ReactNode } from 'react'
import { SkipLink } from './SkipLink'

export interface FrameProps {
  header: ReactNode
  rail: ReactNode
  pane: ReactNode
  context: ReactNode
  dock: ReactNode
  tabBar: ReactNode
}

/** The persistent 100dvh grid: header, body (rail, pane, context), footer (dock, tab bar). */
export function Frame({ header, rail, pane, context, dock, tabBar }: FrameProps) {
  return (
    <div data-frame="1">
      <SkipLink />
      {header}
      <div data-body="1">
        {rail}
        {pane}
        {context}
      </div>
      <footer className="sh-footer">
        {dock}
        {tabBar}
      </footer>
    </div>
  )
}

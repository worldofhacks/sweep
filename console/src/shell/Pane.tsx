import type { ReactNode } from 'react'

export interface PaneTab {
  id: string
  label: string
}

export interface PaneTabsProps {
  tabs: PaneTab[]
  active: string
  onChange: (id: string) => void
  /** Accessible name of the button group. */
  label: string
  variant?: 'panes' | 'reference'
}

/** The sub-tab strip: an aria-pressed button group in a rounded ground-coloured well. */
export function PaneTabs({ tabs, active, onChange, label, variant = 'panes' }: PaneTabsProps) {
  return (
    <span
      className={variant === 'reference' ? 'sh-tabs is-reference' : 'sh-tabs'}
      data-tabs="1"
      role="group"
      aria-label={label}
    >
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className={tab.id === active ? 'sh-tab is-active' : 'sh-tab'}
          aria-pressed={tab.id === active}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </span>
  )
}

export interface PaneProps {
  title: string
  note: string
  tabs?: PaneTab[]
  activeTab?: string
  onTabChange?: (id: string) => void
  tabsLabel?: string
  tabsVariant?: 'panes' | 'reference'
  children: ReactNode
}

export function Pane({
  title,
  note,
  tabs,
  activeTab,
  onTabChange,
  tabsLabel,
  tabsVariant,
  children,
}: PaneProps) {
  const hasTabs = Boolean(tabs && tabs.length > 1 && activeTab !== undefined && onTabChange)
  return (
    <section id="pane" className="sh-pane" data-pane="1" aria-label="Working pane">
      <div className="sh-pane-head">
        <div className="sh-pane-title-block">
          <h1 className="sh-pane-h1">{title}</h1>
          <p className="sh-pane-note">{note}</p>
        </div>
        {hasTabs && tabs && activeTab !== undefined && onTabChange && (
          <div className="sh-pane-tabs-wrap">
            <PaneTabs
              tabs={tabs}
              active={activeTab}
              onChange={onTabChange}
              label={tabsLabel ?? `${title} panes`}
              variant={tabsVariant}
            />
          </div>
        )}
      </div>
      <div className="sh-pane-scroll" data-scroll="1">
        <div className="sh-swap" key={activeTab ?? 'single'}>
          {children}
        </div>
      </div>
    </section>
  )
}

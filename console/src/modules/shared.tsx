export function PanelHeading({ eyebrow, title, meta, id }: { eyebrow: string; title: string; meta: string; id: string }) {
  return (
    <header className="panel-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2 id={id}>{title}</h2>
      </div>
      <span className="panel-meta mono">{meta}</span>
    </header>
  )
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  )
}

/** Honest placeholder for a module or pane the relay does not feed yet. */
export function EmptyModule({ what, detail }: { what: string; detail?: string }) {
  return (
    <div className="sh-empty" role="status">
      <strong>Nothing to show</strong>
      <p>
        {detail ??
          `The relay does not report ${what} on this console yet. Nothing is rendered rather than a fixture.`}
      </p>
    </div>
  )
}

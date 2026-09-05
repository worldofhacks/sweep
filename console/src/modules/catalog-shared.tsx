import type { CatalogLink } from '../catalog/derive'
import type { CatalogNoteState } from './catalog-notes'
import './catalog.css'

/** Outcome of the last catalog action: a polite live region that stays mounted while empty. */
export function CatalogNote({ label, note }: { label: string; note: CatalogNoteState | null }) {
  const classes = ['cat-note']
  if (note === null) classes.push('is-empty')
  else if (note.tone === 'danger' || note.tone === 'warn' || note.tone === 'ink') {
    classes.push(`is-${note.tone}`)
  }
  return (
    <p className={classes.join(' ')} role="status" aria-live="polite" aria-label={label}>
      {note?.text}
    </p>
  )
}

/** The console link is not connected: the last snapshot stays and actions are refused. */
export function LinkNotice({ link, label }: { link: CatalogLink; label: string }) {
  if (link.notice === null) return null
  return (
    <p className={link.up ? 'cat-note is-warn' : 'cat-note is-danger'} role="status" aria-label={label}>
      {link.notice}
    </p>
  )
}

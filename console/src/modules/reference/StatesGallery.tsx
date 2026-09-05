import { VOCAB, vocabNote, vocabTone } from '../../catalog/derive'
import { MEMBERSHIP_REASON, READINESS, REASONS } from '../../shell/sentences'
import '../catalog.css'

/**
 * Every vocabulary value the console renders, in the design's thirteen
 * domains, followed by the refusal, readiness and membership reason tables.
 * This is the console's own vocabulary, so it needs no relay data.
 */
export function StatesGallery() {
  return (
    <div className="cat-swap">
      {VOCAB.map((domain) => (
        <section key={domain.domain} className="gal-domain" aria-label={`${domain.domain} vocabulary`}>
          <p className="cat-eyebrow gal-domain-name">{domain.domain}</p>
          <ul className="gal-chips">
            {domain.values.map((value) => {
              const note = vocabNote(value)
              return (
                <li key={value} className={`gal-chip tone-${vocabTone(value)}`}>
                  <span aria-hidden="true">■</span>
                  {value}
                  {note && <span className="gal-chip-note">{note}</span>}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
      <ReasonTable title="Refusal and failure reasons" rows={REASONS} danger />
      <ReasonTable title="Readiness reasons" rows={READINESS} />
      <ReasonTable title="Membership reasons" rows={MEMBERSHIP_REASON} />
    </div>
  )
}

function ReasonTable({
  title,
  rows,
  danger = false,
}: {
  title: string
  rows: Record<string, string>
  danger?: boolean
}) {
  return (
    <section className="gal-table" aria-label={title}>
      <p className="cat-eyebrow gal-table-name">{title}</p>
      <ul className="gal-grid">
        {Object.entries(rows).map(([code, sentence]) => (
          <li key={code} className={danger ? 'gal-row is-danger' : 'gal-row'}>
            <code>{code}</code>
            <span>{sentence}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

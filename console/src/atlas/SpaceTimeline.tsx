import { useEffect, useId, useMemo, useState } from 'react'
import type { AtlasClient } from './client'
import type { Capture } from './types'
import { MediaCard } from './SpacesModule'
import { AtlasDialog } from './AtlasDialog'
import { calendarLabel, chapter, dateLabel, dateSource, utcLabel, type DatePrecision, type DateRevision, type TimelineEntry } from './timeline'
import './timeline.css'

export default function SpaceTimeline({ client, spaceId, onVisible, onOpenMemory }: { client: AtlasClient; spaceId: string; onVisible: (ids: string[]) => void; onOpenMemory?: (capture: Capture) => void }) {
  const [entries, setEntries] = useState<TimelineEntry[] | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [selected, setSelected] = useState('all')
  const [limit, setLimit] = useState(24)
  const [dateOpen, setDateOpen] = useState<TimelineEntry | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function update() {
      try {
        const result = await client.timeline(spaceId, controller.signal)
        if (!controller.signal.aborted) {
          setEntries(result.entries); setError('')
          setDateOpen(current => {
            if (!current) return null
            const latest = result.entries.find(entry => entry.capture.id === current.capture.id)
            return latest ? { ...current, can_edit: latest.can_edit } : null
          })
        }
      } catch (error) {
        if (!controller.signal.aborted) { setEntries(null); setDateOpen(null); setError(error instanceof Error ? error.message : 'Timeline unavailable.') }
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void update(), 10_000)
    }
    void update()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [client, spaceId, revision])
  const visible = useMemo(() => (entries ?? []).filter(entry => selected === 'all' || chapter(entry).key === selected), [entries, selected])
  const page = useMemo(() => visible.slice(0, limit), [visible, limit])
  useEffect(() => { onVisible(page.map(entry => entry.capture.id)) }, [page, onVisible])
  const chapters = [...new Map((entries ?? []).map(entry => [chapter(entry).key, chapter(entry)])).values()]
  const groups = [...new Map(page.map(entry => [chapter(entry).key, chapter(entry)])).values()]
  const dated = (entries ?? []).filter(entry => entry.time.precision !== 'unknown').length
  const withoutLocation = page.filter(entry => !entry.capture.position).length
  return <section className="space-timeline" aria-label="Space timeline">
    <div className="atlas-section-heading"><h3>A place, through time.</h3><p>Revisit the days and little chapters that brought you here.</p></div>
    <p className="atlas-fine">{entries ? `${entries.length} ${entries.length === 1 ? 'memory' : 'memories'} · ${dated} ${dated === 1 ? 'with a date' : 'with dates'}` : 'Finding your chapters…'} · Upload time is never treated as the event date.</p>
    {error && <p role="alert">{error} <button className="atlas-text-button" onClick={() => setRevision(value => value + 1)}>Retry timeline</button></p>}
    {!entries && !error && <p role="status">Loading the timeline…</p>}
    {entries && <>
      {entries.length > 0 && <details className="timeline-chapter-picker"><summary>Jump to a chapter · {selected === 'all' ? 'All memories' : chapters.find(c => c.key === selected)?.label ?? 'Chapter changed'}</summary>
        <div role="group" aria-label="Timeline chapters"><button className="atlas-secondary" aria-pressed={selected === 'all'} onClick={() => { setSelected('all'); setLimit(24) }}>All memories</button>
          {chapters.map(item => <button key={item.key} className="atlas-secondary" aria-pressed={selected === item.key} onClick={() => { setSelected(item.key); setLimit(24) }}>{item.label}</button>)}</div>
      </details>}
      <p className="atlas-fine">The map shows capture-time GPS for the {page.length} {page.length === 1 ? 'memory' : 'memories'} shown here; {withoutLocation} {withoutLocation === 1 ? 'has' : 'have'} no recorded location. Live people and current requests are hidden in this view.</p>
      {!visible.length && <div className="timeline-empty"><h4>{entries.length ? 'No memories in this chapter now.' : 'Your first chapter is waiting.'}</h4><p>{entries.length ? 'A corrected date may have moved a memory. Choose All memories to find it.' : 'Add a photo or video to begin. An unknown date is okay—you can fill it in later.'}</p></div>}
      {groups.map(group => <section key={group.key} className="timeline-chapter" aria-label={group.label}><h4>{group.label}</h4>
        {page.filter(entry => chapter(entry).key === group.key).map(entry => <div key={entry.capture.id} className="timeline-memory">
          <p className="timeline-date">{dateLabel(entry.time)}</p><p className="atlas-fine">{dateSource(entry)}{entry.time.note ? ` · ${entry.time.note}` : ''}</p>
          {entry.warnings.map(warning => <p key={warning} className="atlas-fine">{warning}</p>)}
          <MediaCard capture={entry.capture} client={client} spaceId={spaceId} onOpenMemory={onOpenMemory} timeCaption={`Uploaded ${calendarLabel(utcLabel(entry.capture.uploaded_at).slice(0, 10))} (UTC)`} />
          <button className="atlas-text-button" onClick={() => setDateOpen(entry)}>{entry.can_edit ? 'Date details & correction' : 'Date details'}</button>
        </div>)}
      </section>)}
      {visible.length > limit && <button className="atlas-secondary atlas-full" onClick={() => setLimit(value => value + 24)}>Show {Math.min(24, visible.length - limit)} more memories</button>}
      <details className="timeline-provenance"><summary>How dates become chapters</summary><p>Calendar dates keep their stated precision. Exact times keep their supplied UTC offset; device timestamps use UTC when the local zone is unknown. Approximate ranges sort by their start, without claiming a particular day. Embedded metadata is a clue, not verified truth. Corrections change this timeline, not original files, hardware clocks, or weather requests.</p></details>
      {dateOpen && <AtlasDialog title="Memory date & history" onClose={() => setDateOpen(null)}><DateDetails key={dateOpen.capture.id} client={client} spaceId={spaceId} entry={dateOpen} onClose={() => setDateOpen(null)} onSaved={() => { setDateOpen(null); setRevision(value => value + 1) }} /></AtlasDialog>}
    </>}
  </section>
}

function DateDetails({ client, spaceId, entry, onClose, onSaved }: { client: AtlasClient; spaceId: string; entry: TimelineEntry; onClose: () => void; onSaved: () => void }) {
  const dateId = useId()
  const [original] = useState(entry)
  const [precision, setPrecision] = useState<DatePrecision>(entry.time.precision)
  const [value, setValue] = useState(entry.time.value ?? '')
  const [end, setEnd] = useState(entry.time.end ?? '')
  const [note, setNote] = useState(entry.time.source === 'declared' ? entry.time.note : '')
  const [history, setHistory] = useState<DateRevision[] | null>(null)
  const [error, setError] = useState('')
  const [historyError, setHistoryError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    void client.dateHistory(spaceId, entry.capture.id, controller.signal).then(value => {
      if (!controller.signal.aborted) setHistory(value)
    }).catch(error => { if (!controller.signal.aborted) setHistoryError(error instanceof Error ? error.message : 'History unavailable.') })
    return () => controller.abort()
  }, [client, spaceId, entry.capture.id])
  const hints = { instant: 'YYYY-MM-DDTHH:MM:SS-05:00 (include the actual UTC offset)', day: 'YYYY-MM-DD', month: 'YYYY-MM', year: 'YYYY', range: 'Start date · YYYY-MM-DD', unknown: '' }
  return <div className="timeline-date-details" aria-label="Capture date details">
    <h5>Keep the story honest.</h5><p>Original capture timestamps and earlier corrections stay available. Choosing “Unknown” changes the chapter, not the original evidence or its audience.</p>
    {entry.evidence.length > 0 && <ul>{entry.evidence.map((source, index) => <li key={index}>{source.source.replaceAll('_', ' ')}: {typeof source.value === 'number' ? utcLabel(source.value) : source.value}</li>)}</ul>}
    {entry.can_edit && <form onSubmit={event => { event.preventDefault(); if (busy) return; setBusy(true); setError('')
      void client.correctDate(spaceId, entry.capture.id, original.revision, { precision, value: precision === 'unknown' ? null : value.trim(), end: precision === 'range' ? end.trim() : null, note: note.trim() })
        .then(onSaved).catch(error => setError(error instanceof Error ? error.message : 'The date could not be saved.')).finally(() => setBusy(false))
    }}>
      <fieldset className="atlas-fieldset" disabled={busy}><legend>How precisely do you remember?</legend>
        <div className="timeline-precision">{([['day', 'Day'], ['month', 'Month'], ['year', 'Year'], ['range', 'Date range'], ['instant', 'Exact time'], ['unknown', 'Unknown']] as const).map(([kind, label]) => <button key={kind} type="button" className="atlas-secondary" aria-pressed={precision === kind} onClick={() => { setPrecision(kind); setValue(''); setEnd('') }}>{label}</button>)}</div>
        {precision !== 'unknown' && <div className="atlas-field"><label htmlFor={dateId}>Memory date</label><input id={dateId} required maxLength={40} value={value} placeholder={hints[precision]} aria-describedby={dateId + '-format'} onChange={event => setValue(event.target.value)} /><span id={dateId + '-format'} className="atlas-fine">{hints[precision]}</span></div>}
        {precision === 'range' && <label className="atlas-field">End date<input required maxLength={10} value={end} placeholder="YYYY-MM-DD" onChange={event => setEnd(event.target.value)} /></label>}
        <label className="atlas-field">What helped you place this memory? <span className="atlas-muted">Optional</span><input maxLength={240} value={note} onChange={event => setNote(event.target.value)} /></label>
      </fieldset>
      {error && <p role="alert">{error} Your input is still here. Close and reopen date details to reload the latest revision.</p>}
      <div className="timeline-date-actions"><button type="button" className="atlas-secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="atlas-primary" disabled={busy}>{busy ? 'Saving…' : 'Save date correction'}</button></div>
    </form>}
    <details><summary>Earlier date corrections · {history?.length ?? '…'}</summary>{historyError ? <p role="alert">{historyError}</p> : history === null ? <p role="status">Loading history…</p> : !history.length ? <p>No corrections yet.</p> : <ol>{history.map(item => <li key={item.revision}><strong>{dateLabel(item.assertion)}</strong><p>{item.assertion.note}</p><small>Revision {item.revision} · {item.actor === 'workspace-operator' ? 'Workspace operator' : `Account ${item.actor.slice(-8)}`} · {utcLabel(item.changed_at)}</small></li>)}</ol>}</details>
  </div>
}

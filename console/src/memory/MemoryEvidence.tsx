import { useEffect, useState } from 'react'
import type { AtlasClient } from '../atlas/client'
import { memoryActor, type MemoryContext, type MemoryEdit } from './types'

const WEATHER_LABELS: Record<string, string> = {
  temperature_2m: 'Temperature',
  relative_humidity_2m: 'Humidity',
  precipitation: 'Precipitation',
  cloud_cover: 'Cloud cover',
  wind_speed_10m: 'Wind at 10 m',
  wind_direction_10m: 'Wind direction',
  wind_gusts_10m: 'Wind gusts at 10 m',
}
export function MemoryEvidence({
  data,
  inspectionStatus,
  onInspect,
  client,
  spaceId,
}: {
  data: MemoryContext
  inspectionStatus: string
  onInspect: () => void
  client: AtlasClient
  spaceId: string
}) {
  const inspection = data.inspection ?? data.analysis?.inspection
  const analysis = data.analysis
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState<MemoryEdit[] | null>(null)
  const [historyError, setHistoryError] = useState('')
  const [before, setBefore] = useState<number>()
  useEffect(() => {
    if (!showHistory) return
    const controller = new AbortController()
    void client
      .memoryHistory(spaceId, data.capture.id, controller.signal, before)
      .then((value) => {
        if (!controller.signal.aborted) {
          setHistory(value)
          setHistoryError('')
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setHistory(null)
          setHistoryError('History is unavailable. Your saved memory is unchanged.')
        }
      })
    return () => controller.abort()
  }, [client, spaceId, data.capture.id, data.last_edit?.sequence, showHistory, before])
  const exportContext = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify({ version: 1, ...data }, null, 2)], {
        type: 'application/json',
      }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = `memory-${data.capture.id}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return (
    <div className="memory-evidence-list">
      <p className="atlas-fine">
        Your words, source metadata, and generated suggestions stay separate. Nothing changes the
        original capture or its map coverage.
      </p>
      <section className="memory-evidence">
        <strong>Original · unchanged</strong>
        <p>
          {data.capture.source === 'camera' ? 'Camera capture' : 'Imported capture'} ·{' '}
          {data.capture.mime} · {(data.capture.bytes / 1024 / 1024).toFixed(1)} MB
        </p>
        <details>
          <summary>Original checksum</summary>
          <code>{data.capture.sha256}</code>
        </details>
      </section>
      {inspection ? (
        <section className="memory-evidence">
          <strong>Embedded metadata · not confirmed context</strong>
          <p>
            {inspection.width && inspection.height
              ? `${inspection.width} × ${inspection.height} · `
              : ''}
            {inspection.has_audio ? 'Audio track present' : 'No audio track'}
            {inspection.duration_seconds !== undefined
              ? ` · ${Math.round(inspection.duration_seconds)} seconds`
              : ''}
          </p>
          <p>
            {inspection.timestamp || inspection.local_timestamp || 'No embedded capture time'}
            {!inspection.timestamp && inspection.local_timestamp ? ' · timezone unknown' : ''}
          </p>
          <p>
            {inspection.location
              ? `${inspection.location.latitude}, ${inspection.location.longitude}`
              : 'No embedded location'}
          </p>
          {inspection.warnings.map((value) => (
            <p className="atlas-fine" key={value}>
              {value}
            </p>
          ))}
        </section>
      ) : (
        <div>
          <p role="status">{inspectionStatus || 'No embedded metadata available.'}</p>
          {data.can_edit && (
            <button className="atlas-secondary" onClick={onInspect}>
              Read image / video metadata
            </button>
          )}
        </div>
      )}
      {analysis && (
        <>
          <p>Context analysis: {analysis.status}</p>
          {analysis.weather && (
            <section className="memory-evidence">
              <strong>Weather · estimated</strong>
              <p>
                {analysis.weather.dataset} ·{' '}
                {new Date(analysis.weather.sampled_at).toLocaleString()}
              </p>
              <dl>
                {Object.entries(analysis.weather.fields).map(([key, item]) => (
                  <div key={key}>
                    <dt>{WEATHER_LABELS[key] ?? key}</dt>
                    <dd>
                      {item.value} {item.unit}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="atlas-fine">{analysis.weather.note}</p>
              <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">
                {analysis.weather.attribution}
              </a>
            </section>
          )}
          {analysis.audio && (
            <section className="memory-evidence">
              <strong>Audio · measured from the file</strong>
              <p>
                {analysis.audio.analyzed_seconds.toFixed(1)} seconds analyzed ·{' '}
                {analysis.audio.rms_dbfs === null
                  ? 'No measurable digital signal'
                  : `${analysis.audio.rms_dbfs} dBFS RMS`}
              </p>
              <p className="atlas-fine">{analysis.audio.note}</p>
            </section>
          )}
          {analysis.transcript && (
            <section className="memory-evidence">
              <strong>Speech transcript · AI draft</strong>
              <p>{analysis.transcript.text}</p>
              <small>{analysis.transcript.model}</small>
            </section>
          )}
          {analysis.suggestion && (
            <section className="memory-evidence">
              <strong>AI draft · not a verified account</strong>
              <p>{analysis.suggestion.summary}</p>
              {(
                [
                  ['Visible in the sample', analysis.suggestion.visual_observations],
                  ['Optional atmosphere suggestions', analysis.suggestion.atmosphere_suggestions],
                  ['What remains uncertain', analysis.suggestion.uncertainties],
                ] as const
              ).map(([label, items]) => (
                <div key={label}>
                  <h5>{label}</h5>
                  <ul>
                    {items.map((item, index) => (
                      <li key={index}>{item}</li>
                    ))}
                  </ul>
                </div>
              ))}
              <small>{analysis.suggestion.model}</small>
            </section>
          )}
          {analysis.warnings?.map((value) => (
            <p className="memory-callout" key={value}>
              {value}
            </p>
          ))}
        </>
      )}
      {data.review && (
        <p className="atlas-fine">
          Reviewed · {memoryActor(data.review.actor)} ·{' '}
          {new Date(data.review.reviewed_at).toLocaleString()}. Generated details remain
          suggestions, not verified facts.
        </p>
      )}
      <section className="memory-evidence memory-history">
        <strong>Made together</strong>
        {data.last_edit && (
          <p>
            Last changed by {memoryActor(data.last_edit.actor)} ·{' '}
            {new Date(data.last_edit.changed_at).toLocaleString()}
          </p>
        )}
        <button
          className="atlas-text-button"
          aria-expanded={showHistory}
          onClick={() => {
            setShowHistory(!showHistory)
            setHistory(null)
            setHistoryError('')
            setBefore(undefined)
          }}
        >
          {showHistory ? 'Hide edit history' : 'Show edit history'}
        </button>
        {showHistory && (
          <div aria-label="Memory edit history">
            <p className="atlas-fine">
              Saved edits are attributed to accounts, not verified personal names. History begins
              when this feature was enabled; earlier edits may not be recorded.
            </p>
            {historyError ? (
              <p role="alert">{historyError}</p>
            ) : history === null ? (
              <p role="status">Opening history…</p>
            ) : history.length === 0 ? (
              <p>No recorded edits yet.</p>
            ) : (
              history.map((entry) => (
                <details key={entry.sequence}>
                  <summary>
                    {entry.kind === 'notes'
                      ? 'Story updated'
                      : entry.kind === 'recording'
                        ? 'Recording added'
                        : 'Memory kept'}{' '}
                    · {memoryActor(entry.actor)} · {new Date(entry.changed_at).toLocaleString()}
                  </summary>
                  <p className="atlas-fine">
                    Edit {entry.sequence} · memory revision {entry.revision}
                  </p>
                  {entry.notes && (
                    <>
                      <strong>Saved story</strong>
                      <p>{entry.notes.description || 'No description'}</p>
                      <p>{entry.notes.feeling || 'No feeling added'}</p>
                      <details>
                        <summary>All saved fields and previous values</summary>
                        <pre>
                          {JSON.stringify(
                            { previous: entry.previous, saved: entry.notes },
                            null,
                            2,
                          )}
                        </pre>
                      </details>
                    </>
                  )}
                  {entry.asset && (
                    <p>
                      {entry.asset.title} · {entry.asset.role}. Original recording kept separately.
                    </p>
                  )}
                  {entry.review && (
                    <p>
                      {entry.review.analysis_id
                        ? 'Generated context reviewed; it remains a suggestion.'
                        : 'Story kept without adopting generated context.'}
                    </p>
                  )}
                </details>
              ))
            )}
            {history && history.length === 20 && history[history.length - 1].sequence > 1 && (
              <button
                onClick={() => {
                  setBefore(history[history.length - 1].sequence)
                  setHistory(null)
                }}
              >
                Earlier edits
              </button>
            )}
            {before !== undefined && (
              <button
                onClick={() => {
                  setBefore(undefined)
                  setHistory(null)
                  setHistoryError('')
                }}
              >
                Latest edits
              </button>
            )}
          </div>
        )}
      </section>
      <button className="atlas-secondary" onClick={exportContext}>
        Export saved memory context
      </button>
      <p className="atlas-fine">
        Shared with everyone who has access to this Space. This export includes current saved
        coordinates and transcripts, not the edit history or original media. No public post is created.
      </p>
    </div>
  )
}

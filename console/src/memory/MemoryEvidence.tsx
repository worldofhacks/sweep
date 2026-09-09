import type { MemoryContext } from './types'

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
}: {
  data: MemoryContext
  inspectionStatus: string
  onInspect: () => void
}) {
  const inspection = data.inspection ?? data.analysis?.inspection
  const analysis = data.analysis
  const exportContext = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify({ version: 1, ...data }, null, 2)], { type: 'application/json' }),
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
          Reviewed by the owner · {new Date(data.review.reviewed_at).toLocaleString()}. Generated
          details remain suggestions, not verified facts.
        </p>
      )}
      <button className="atlas-secondary" onClick={exportContext}>
        Export saved memory context
      </button>
      <p className="atlas-fine">
        Shared with this space’s invited viewers. Exports include saved coordinates and transcripts.
        No public post is created.
      </p>
    </div>
  )
}

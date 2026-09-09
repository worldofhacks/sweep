import { useEffect, useRef, useState } from 'react'
import { AtlasClient, locate } from '../atlas/client'
import { Icon } from '../atlas/Icon'
import type { Capture } from '../atlas/types'
import { EMPTY_NOTES, type MemoryAsset, type MemoryContext, type MemoryNotes } from './types'
import './memory.css'

const explain = (error: unknown) =>
  error instanceof Error ? error.message : 'This step could not finish. Your original is safe.'
const WEATHER_LABELS: Record<string, string> = {
  temperature_2m: 'Temperature',
  relative_humidity_2m: 'Humidity',
  precipitation: 'Precipitation',
  cloud_cover: 'Cloud cover',
  wind_speed_10m: 'Wind at 10 m',
  wind_direction_10m: 'Wind direction',
  wind_gusts_10m: 'Wind gusts at 10 m',
}

export function MemoryPanel({
  client,
  spaceId,
  capture,
  onDirtyChange,
}: {
  client: AtlasClient
  spaceId: string
  capture: Capture
  onDirtyChange?: (dirty: boolean) => void
}) {
  const [data, setData] = useState<MemoryContext | null>(null)
  const [notes, setNotes] = useState<MemoryNotes>(EMPTY_NOTES)
  const [coordinates, setCoordinates] = useState(['', ''])
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [weather, setWeather] = useState(false)
  const [ai, setAI] = useState(false)
  const [audioId, setAudioId] = useState('')
  const [role, setRole] = useState<MemoryAsset['role']>('ambient')
  const [rights, setRights] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [reload, setReload] = useState(0)
  const mounted = useRef(false)
  const upload = useRef<AbortController | null>(null)
  useEffect(() => {
    onDirtyChange?.(dirty || Boolean(busy))
  }, [dirty, busy, onDirtyChange])
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    void client
      .memory(spaceId, capture.id, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return
        setData(value)
        setNotes(value.notes)
        setDirty(false)
        setError('')
        setCoordinates(
          value.notes.location
            ? [String(value.notes.location.latitude), String(value.notes.location.longitude)]
            : ['', ''],
        )
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(explain(error))
      })
    return () => {
      mounted.current = false
      controller.abort()
      upload.current?.abort()
    }
  }, [client, spaceId, capture.id, reload])
  const running = data?.analysis?.status === 'running'
  useEffect(() => {
    if (!running) return
    const controller = new AbortController()
    const timer = setInterval(() => {
      void client
        .memory(spaceId, capture.id, controller.signal)
        .then((value) => {
          if (!controller.signal.aborted) setData(value)
        })
        .catch((error) => {
          if (!controller.signal.aborted) setError(explain(error))
        })
    }, 2000)
    return () => {
      clearInterval(timer)
      controller.abort()
    }
  }, [client, spaceId, capture.id, running])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const act = async (label: string, operation: () => Promise<void>) => {
    setBusy(label)
    setError('')
    try {
      await operation()
    } catch (error) {
      if (mounted.current) setError(explain(error))
    } finally {
      if (mounted.current) setBusy('')
    }
  }
  const edit = (change: Partial<MemoryNotes>) => {
    setNotes((previous) => ({ ...previous, ...change }))
    setDirty(true)
  }
  const save = async () => {
    if (!data) return
    let location = null
    if (coordinates.some((value) => value.trim())) {
      if (coordinates.some((value) => !value.trim() || !Number.isFinite(Number(value))))
        throw new Error('Enter both latitude and longitude, or leave both empty.')
      location = { latitude: Number(coordinates[0]), longitude: Number(coordinates[1]) }
    }
    const result = await client.saveMemory(spaceId, capture.id, data.revision, {
      ...notes,
      location,
      occurred_at: notes.occurred_at?.trim() || null,
    })
    if (mounted.current) {
      setData(result)
      setNotes(result.notes)
      setDirty(false)
    }
  }
  const attach = async () => {
    if (!file || !rights || !title.trim())
      throw new Error('Choose a recording, give it a name, and confirm sharing rights.')
    upload.current = new AbortController()
    await client.uploadMemoryAsset(
      spaceId,
      capture.id,
      file,
      { title: title.trim(), role, rights_confirmed: true },
      upload.current.signal,
    )
    const result = await client.memory(spaceId, capture.id)
    if (mounted.current) {
      setData(result)
      setFile(null)
      setTitle('')
      setRights(false)
    }
  }
  const exportContext = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify({ version: 1, ...data }, null, 2)], { type: 'application/json' }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = `memory-${capture.id}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const disabled = Boolean(busy || running || !data?.can_edit)
  const inspection = data?.inspection ?? data?.analysis?.inspection
  return (
    <section className="memory-panel" aria-label="Memory context">
      <div className="memory-intro">
        <Icon name="spark" size={24} />
        <div>
          <span className="atlas-eyebrow">MORE THAN A PICTURE</span>
          <h3>Bring this memory to life.</h3>
          <p>The sounds, the setting, and how it felt—kept beside the original.</p>
        </div>
      </div>
      {error && (
        <div className="memory-error" role="alert">
          {error}
          <button
            onClick={() => {
              if (!dirty || window.confirm('Reload and discard unsaved memory notes?'))
                setReload((value) => value + 1)
            }}
          >
            Reload saved context
          </button>
        </div>
      )}
      {!data ? (
        <p role="status">
          {error ? 'Memory context is unavailable on this server.' : 'Opening memory context…'}
        </p>
      ) : (
        <>
          {!data.can_edit && (
            <p className="memory-callout">
              Your invitation can view this memory. A workspace owner adds context, recordings, and
              optional provider analysis.
            </p>
          )}
          <fieldset disabled={disabled} className="memory-section">
            <legend>1. Your memory, in your words</legend>
            <label>
              What was happening?
              <textarea
                rows={3}
                maxLength={2000}
                value={notes.description}
                onChange={(event) => edit({ description: event.target.value })}
                placeholder="The little details you want someone else to experience…"
              />
            </label>
            <label>
              How did it feel to you?
              <textarea
                rows={2}
                maxLength={500}
                value={notes.feeling}
                onChange={(event) => edit({ feeling: event.target.value })}
                placeholder="A peaceful evening, a joyful reunion, a moment of wonder…"
              />
            </label>
            <p className="atlas-fine">
              Only you can tell us how it felt. AI will not infer emotions from faces or voices.
            </p>
            <label>
              When did this happen?
              <input
                value={notes.occurred_at ?? ''}
                onChange={(event) => edit({ occurred_at: event.target.value })}
                placeholder="2026-09-08T18:30:00-05:00"
                maxLength={40}
              />
            </label>
            <p className="atlas-fine">
              Date, time, and UTC offset. Example: 2026-09-08T18:30:00-05:00 in Austin during
              daylight time. Leave unknown details empty.
            </p>
            <div className="memory-coordinates">
              {['Latitude', 'Longitude'].map((label, index) => (
                <label key={label}>
                  {label}
                  <input
                    inputMode="decimal"
                    value={coordinates[index]}
                    onChange={(event) => {
                      setCoordinates((previous) =>
                        previous.map((value, i) => (i === index ? event.target.value : value)),
                      )
                      setDirty(true)
                    }}
                  />
                </label>
              ))}
            </div>
            <div className="memory-actions">
              {capture.captured_at != null && (
                <button
                  onClick={() =>
                    edit({ occurred_at: new Date(capture.captured_at!).toISOString() })
                  }
                >
                  Use capture timestamp
                </button>
              )}
              {capture.position && (
                <button
                  onClick={() => {
                    setCoordinates([
                      String(capture.position!.latitude),
                      String(capture.position!.longitude),
                    ])
                    setDirty(true)
                  }}
                >
                  Use capture location
                </button>
              )}
              <button
                onClick={() =>
                  void act('Checking location…', async () => {
                    const position = await locate()
                    if (mounted.current) {
                      setCoordinates([String(position.latitude), String(position.longitude)])
                      setDirty(true)
                    }
                  })
                }
              >
                Use my location—I’m at this place
              </button>
            </div>
            <p className="atlas-fine">
              This confirms context for the memory, not a surveyed camera pose. It never changes map
              coverage or the original capture’s provenance.
            </p>
            <button
              className="atlas-secondary"
              onClick={() =>
                void act('Reading embedded metadata…', async () => {
                  const result = await client.inspectMemory(spaceId, capture.id)
                  if (mounted.current) setData(result)
                })
              }
            >
              Read image / video metadata
            </button>
            {inspection && (
              <div className="memory-evidence">
                <strong>Embedded metadata · review before using</strong>
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
                <div className="memory-actions">
                  {inspection.timestamp && (
                    <button onClick={() => edit({ occurred_at: inspection.timestamp })}>
                      Use embedded time
                    </button>
                  )}
                  {inspection.location && (
                    <button
                      onClick={() => {
                        setCoordinates([
                          String(inspection.location!.latitude),
                          String(inspection.location!.longitude),
                        ])
                        setDirty(true)
                      }}
                    >
                      Use embedded location
                    </button>
                  )}
                </div>
                {inspection.warnings.map((value) => (
                  <p className="atlas-fine" key={value}>
                    {value}
                  </p>
                ))}
              </div>
            )}
            <label>
              A song connected to this memory
              <input
                maxLength={200}
                value={notes.music_title}
                onChange={(event) => edit({ music_title: event.target.value })}
                placeholder="Song and artist (optional)"
              />
            </label>
            <label>
              Music link
              <input
                type="url"
                maxLength={1000}
                value={notes.music_url}
                onChange={(event) => edit({ music_url: event.target.value })}
                placeholder="Spotify, Apple Music, or YouTube HTTPS link"
              />
            </label>
            <p className="atlas-fine">
              A reference link, not a music download or playback license. A soundtrack is a creative
              addition unless you describe that it actually played there.
            </p>
            <button
              className="atlas-primary"
              disabled={!dirty}
              onClick={() => void act('Saving your memory…', save)}
            >
              {dirty ? 'Save memory details' : 'Memory details saved'}
            </button>
            {dirty && (
              <p role="status" className="atlas-fine">
                Unsaved changes. Save before analysis or leaving this view.
              </p>
            )}
          </fieldset>
          <section className="memory-section">
            <h4>2. The sound of being there</h4>
            <p>
              Keep ambience, a spoken recollection, or a soundtrack beside the scan. Nothing plays
              automatically.
            </p>
            {data.assets.map((asset) => (
              <MemoryTrack
                key={asset.id}
                asset={asset}
                client={client}
                spaceId={spaceId}
                captureId={capture.id}
              />
            ))}
            {data.notes.music_url && (
              <a href={data.notes.music_url} target="_blank" rel="noreferrer noopener">
                Open music reference ↗
              </a>
            )}
            {data.can_edit &&
              (client.memoryUploadsSupported ? (
                <fieldset disabled={disabled || dirty}>
                  <div className="memory-choice" role="group" aria-label="Recording role">
                    {(['ambient', 'narration', 'soundtrack'] as const).map((value) => (
                      <button
                        key={value}
                        aria-pressed={role === value}
                        onClick={() => setRole(value)}
                      >
                        {value === 'ambient'
                          ? 'Ambient recording'
                          : value === 'narration'
                            ? 'Spoken memory'
                            : 'Soundtrack'}
                      </button>
                    ))}
                  </div>
                  <label>
                    Recording name
                    <input
                      maxLength={120}
                      value={title}
                      onChange={(event) => setTitle(event.target.value)}
                      placeholder="Creekside birds and passing footsteps"
                    />
                  </label>
                  <label className="memory-file">
                    Choose audio or video
                    <input
                      type="file"
                      accept="audio/mpeg,audio/mp4,audio/wav,audio/x-wav,audio/x-m4a,audio/webm,audio/ogg,audio/flac,video/mp4,video/webm"
                      onChange={(event) => {
                        const selected = event.target.files?.[0]
                        if (selected) {
                          setFile(selected)
                          setTitle((previous) => previous || selected.name.slice(0, 120))
                        }
                        event.target.value = ''
                      }}
                    />
                  </label>
                  {file && (
                    <p>
                      {file.name} · {(file.size / 1024 / 1024).toFixed(1)} MB · ready to attach
                    </p>
                  )}
                  <label className="memory-check">
                    <input
                      type="checkbox"
                      checked={rights}
                      onChange={(event) => setRights(event.target.checked)}
                    />
                    I recorded this or have permission to share it, including any music and people’s
                    voices.
                  </label>
                  <button
                    className="atlas-secondary"
                    disabled={!file || !rights || !title.trim()}
                    onClick={() => void act('Saving your recording…', attach)}
                  >
                    Attach recording
                  </button>
                  <p className="atlas-fine">
                    Originals stay unchanged. Up to 64 MB per file, eight tracks per capture.
                    Soundtracks are never sent for speech transcription.
                  </p>
                </fieldset>
              ) : (
                <p className="memory-callout">
                  Attach recordings from the web console for now. Your Android camera and original
                  upload flow are unchanged.
                </p>
              ))}
          </section>
          <fieldset disabled={disabled || dirty} className="memory-section">
            <legend>3. Find the context</legend>
            <label className="memory-check">
              <input
                type="checkbox"
                checked={weather}
                disabled={!data.capabilities.weather}
                onChange={(event) => setWeather(event.target.checked)}
              />
              Look up weather for the confirmed time and place
            </label>
            <p className="atlas-fine">
              Sends the saved coordinates and date to Open-Meteo. Conditions are hourly model
              estimates, not exact measurements at the scene.
              {!data.capabilities.weather && ' Weather provider setup is needed on this server.'}
            </p>
            <label className="memory-check">
              <input
                type="checkbox"
                checked={ai}
                disabled={!data.capabilities.ai}
                onChange={(event) => setAI(event.target.checked)}
              />
              Ask AI for a draft description and speech transcript
            </label>
            <p className="atlas-fine">
              Sends one visual preview, your saved notes/context, and up to 60 seconds of the chosen
              recording to OpenAI. Review any transcript and suggestions. Sound-event recognition is
              not included.{!data.capabilities.ai && ' A server-side OpenAI API key is needed.'}
            </p>
            <div className="memory-choice" role="group" aria-label="Audio for analysis">
              <button aria-pressed={!audioId} onClick={() => setAudioId('')}>
                Original video audio, if present
              </button>
              {data.assets
                .filter((asset) => asset.role !== 'soundtrack')
                .map((asset) => (
                  <button
                    key={asset.id}
                    aria-pressed={audioId === asset.id}
                    onClick={() => setAudioId(asset.id)}
                  >
                    {asset.title}
                  </button>
                ))}
            </div>
            <button
              className="atlas-primary"
              onClick={() =>
                void act('Starting context analysis…', async () => {
                  const result = await client.analyzeMemory(spaceId, capture.id, {
                    revision: data.revision,
                    weather,
                    ai,
                    audio_asset_id: audioId || null,
                  })
                  if (mounted.current) setData(result)
                })
              }
            >
              <Icon name="spark" size={16} />
              {ai || weather ? 'Gather memory context' : 'Inspect local context'}
            </button>
          </fieldset>
          {(busy || running) && (
            <p className="memory-progress" role="status">
              {busy ||
                'Gathering context… Your original is already safe. You can return to this memory later.'}
            </p>
          )}
          {data.analysis && (
            <section className="memory-results" aria-label="Memory evidence and suggestions">
              <h4>What the evidence tells us</h4>
              {['outdated', 'interrupted', 'failed'].includes(data.analysis.status) && (
                <p role="status">
                  Analysis is {data.analysis.status}. Review the saved details and gather context
                  again.
                </p>
              )}
              {data.analysis.weather && (
                <div className="memory-evidence">
                  <strong>Weather · estimated</strong>
                  <p>
                    {data.analysis.weather.dataset} ·{' '}
                    {new Date(data.analysis.weather.sampled_at).toLocaleString()}
                  </p>
                  <dl>
                    {Object.entries(data.analysis.weather.fields).map(([key, item]) => (
                      <div key={key}>
                        <dt>{WEATHER_LABELS[key] ?? key}</dt>
                        <dd>
                          {item.value} {item.unit}
                        </dd>
                      </div>
                    ))}
                  </dl>
                  <p className="atlas-fine">{data.analysis.weather.note}</p>
                  <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">
                    {data.analysis.weather.attribution}
                  </a>
                </div>
              )}
              {data.analysis.audio && (
                <div className="memory-evidence">
                  <strong>Audio · measured from the file</strong>
                  <p>
                    {data.analysis.audio.analyzed_seconds.toFixed(1)} seconds analyzed ·{' '}
                    {data.analysis.audio.rms_dbfs === null
                      ? 'No measurable digital signal'
                      : `${data.analysis.audio.rms_dbfs} dBFS RMS`}
                  </p>
                  <p className="atlas-fine">{data.analysis.audio.note}</p>
                </div>
              )}
              {data.analysis.transcript && (
                <div className="memory-evidence">
                  <strong>Speech transcript · AI, review required</strong>
                  <p>{data.analysis.transcript.text}</p>
                  <small>{data.analysis.transcript.model}</small>
                </div>
              )}
              {data.analysis.suggestion && (
                <div className="memory-evidence memory-suggestion">
                  <strong>AI draft · not a verified account</strong>
                  <p>{data.analysis.suggestion.summary}</p>
                  {[
                    ['Visible in the sample', data.analysis.suggestion.visual_observations],
                    [
                      'Optional atmosphere suggestions',
                      data.analysis.suggestion.atmosphere_suggestions,
                    ],
                    ['What remains uncertain', data.analysis.suggestion.uncertainties],
                  ].map(([label, items]) => (
                    <div key={label as string}>
                      <h5>{label}</h5>
                      <ul>
                        {(items as string[]).map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                  <small>{data.analysis.suggestion.model}</small>
                </div>
              )}
              {data.analysis.warnings?.map((value) => (
                <p role="status" className="memory-callout" key={value}>
                  {value}
                </p>
              ))}
            </section>
          )}
          <div className="memory-footer">
            <button className="atlas-secondary" onClick={exportContext}>
              Export saved memory context
            </button>
            <p className="atlas-fine">
              Context is shared with this space’s invited viewers. Exports include saved coordinates
              and transcripts. Original checksums and source labels stay attached; no public post is
              created.
            </p>
          </div>
        </>
      )}
    </section>
  )
}

function MemoryTrack({
  asset,
  client,
  spaceId,
  captureId,
}: {
  asset: MemoryAsset
  client: AtlasClient
  spaceId: string
  captureId: string
}) {
  const [requested, setRequested] = useState(false),
    [url, setUrl] = useState(''),
    [error, setError] = useState('')
  useEffect(() => {
    if (!requested) return
    const controller = new AbortController()
    let objectUrl = ''
    void client
      .memoryAssetMedia(spaceId, captureId, asset.id, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(explain(error))
          setRequested(false)
        }
      })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [asset.id, client, spaceId, captureId, requested])
  return (
    <article className="memory-track">
      <span className="atlas-eyebrow">
        {asset.role === 'soundtrack'
          ? 'SOUNDTRACK · CREATIVE ADDITION'
          : asset.role === 'narration'
            ? 'SPOKEN RECOLLECTION'
            : 'AMBIENT RECORDING · CONTRIBUTOR-SUPPLIED'}
      </span>
      <strong>{asset.title}</strong>
      {!requested ? (
        <button
          className="atlas-text-button"
          onClick={() => {
            setError('')
            setRequested(true)
          }}
        >
          Load recording · {(asset.bytes / 1024 / 1024).toFixed(1)} MB
        </button>
      ) : url ? (
        asset.mime.startsWith('video/') ? (
          <video controls playsInline preload="metadata" src={url} />
        ) : (
          <audio controls preload="metadata" src={url} />
        )
      ) : (
        <p role="status">{error || 'Loading recording…'}</p>
      )}
      {error && !requested && <p role="status">{error} You can retry loading the original.</p>}
    </article>
  )
}

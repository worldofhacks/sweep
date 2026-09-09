import { useEffect, useRef, useState } from 'react'
import { AtlasClient } from '../atlas/client'
import { Icon, type IconName } from '../atlas/Icon'
import type { Capture } from '../atlas/types'
import type { MemoryAnalysisRequest, MemoryAsset } from './types'
import { useMemory } from './useMemory'
import { useAnalysisRecovery } from './useAnalysisRecovery'
import { MemoryPreview, MemoryTrack, VoiceNote } from './MemoryMedia'
import { AssistEditor, MusicEditor, PlaceEditor } from './MemoryEditors'
import { MemoryEvidence } from './MemoryEvidence'
import './memory.css'

const EDITORS = {
  sound: 'The sound of being there',
  music: 'Add your soundtrack',
  place: 'Place & time',
  assist: 'Let the details come to you',
  evidence: 'Details & sources',
}
type View = 'memory' | keyof typeof EDITORS
const MOODS = ['Peaceful', 'Joyful', 'Bittersweet', 'Full of wonder']

export function MemoryPanel({
  client,
  spaceId,
  capture,
  onDirtyChange,
  onDone,
  onRemove,
}: {
  client: AtlasClient
  spaceId: string
  capture: Capture
  onDirtyChange?: (dirty: boolean) => void
  onDone?: () => void
  onRemove?: () => void
}) {
  const memory = useMemory(client, spaceId, capture)
  const {
    data,
    notes,
    coordinates,
    dirty,
    busy,
    error,
    running,
    act,
    save,
    edit,
    coordinate,
    mounted,
    setData,
  } = memory
  const [view, setView] = useState<View>('memory')
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [role, setRole] = useState<MemoryAsset['role']>('ambient')
  const [rights, setRights] = useState(false)
  const [recording, setRecording] = useState(false)
  const [notice, setNotice] = useState('')
  const recovery = useAnalysisRecovery(client, spaceId, capture.id,
    !!data?.analysis_idempotency && !!(data.can_analyze ?? data.can_edit))
  const pendingAnalysis = recovery.pending
  const focusTarget = useRef<HTMLHeadingElement>(null)
  const panel = useRef<HTMLElement>(null)
  const recoveryNotice = useRef<HTMLDivElement>(null)
  const pendingId = pendingAnalysis?.request_id
  const returnView = useRef<View>('memory')
  const lastView = useRef<View>('memory')
  const unsaved = dirty || !!file || recording || !!busy
  useEffect(() => {
    if (!pendingId || busy) return
    recoveryNotice.current?.focus({ preventScroll: true })
    panel.current?.closest('.memory-dialog')?.scrollTo?.({ top: 0 })
  }, [pendingId, busy])
  useEffect(() => {
    onDirtyChange?.(unsaved)
  }, [onDirtyChange, unsaved])
  useEffect(() => {
    if (!unsaved) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [unsaved])
  useEffect(() => {
    if (lastView.current === view) return
    if (view === 'memory')
      panel.current
        ?.querySelector<HTMLButtonElement>(`[data-memory-view="${returnView.current}"]`)
        ?.focus()
    else {
      focusTarget.current?.focus({ preventScroll: true })
      panel.current?.closest('.memory-dialog')?.scrollTo?.({ top: 0 })
    }
    lastView.current = view
  }, [view])
  const kept =
    !!data?.review && data.review.revision === data.revision && !dirty && !file && !running
  const disabled = !!(busy || running || pendingAnalysis || !data?.can_edit)
  const canAnalyze = data?.can_analyze ?? data?.can_edit
  const open = (next: View, button?: HTMLButtonElement) => {
    if (button) returnView.current = next
    setView(next)
  }
  const chooseFile = (selected: File, nextRole = role) => {
    if (selected.size === 0 || selected.size > 64 * 1024 * 1024) {
      memory.setError('Choose a non-empty recording smaller than 64 MB.')
      return
    }
    setFile(selected)
    setTitle(selected.name.slice(0, 120))
    setRole(nextRole)
    setRights(false)
    memory.setError('')
  }
  const attach = () =>
    void act('Saving your recording…', async () => {
      if (!file || !rights || !title.trim())
        throw new Error('Choose a recording and confirm you have permission to share it.')
      const saved = await save()
      memory.upload.current = new AbortController()
      await client.uploadMemoryAsset(
        spaceId,
        capture.id,
        file,
        { title: title.trim(), role, rights_confirmed: true },
        memory.upload.current.signal,
      )
      const result = await client.memory(spaceId, capture.id)
      if (mounted.current) {
        // A concurrent writer may have changed notes during upload. Do not silently adopt their revision.
        if (JSON.stringify(result.notes) !== JSON.stringify(saved.notes))
          throw new Error('This memory changed in another window. Reload before editing further.')
        setData(result)
        setFile(null)
        setRights(false)
        setTitle('')
        setNotice('Recording added.')
        setView('memory')
      }
    })
  const keep = () =>
    void act('Keeping your memory…', async () => {
      const result = await save()
      const analysis = result.analysis
      const id = analysis && ['complete', 'partial'].includes(analysis.status) ? analysis.id : null
      const reviewed = await client.reviewMemory(spaceId, capture.id, result.revision, id)
      if (mounted.current) {
        setData(reviewed)
        setNotice('Memory kept. Ready to revisit with your space.')
        setView('memory')
      }
    })
  const submitAnalysis = async (request: MemoryAnalysisRequest) => {
    const current = memory.currentScope()
    let result
    try {
      result = await client.analyzeMemory(spaceId, capture.id, request)
    } catch (error) {
      if (!current()) return
      throw error
    }
    const receipt = result.analysis_request
    if (request.request_id && (
      result.capture?.id !== capture.id || result.analysis_idempotency !== true ||
      receipt?.id !== request.request_id ||
      !((typeof receipt.analysis_id === 'string' && !!receipt.analysis_id && receipt.rejection === null) ||
        (receipt.analysis_id === null && typeof receipt.rejection === 'string' && !!receipt.rejection && !receipt.current)) ||
      typeof receipt.replayed !== 'boolean' || typeof receipt.current !== 'boolean' ||
      (receipt.current && result.analysis?.id !== receipt.analysis_id)
    )) {
      if (!current()) return
      throw new Error('The relay did not confirm this request. Recover the same request before starting another.')
    }
    let remaining = null
    if (request.request_id) {
      try {
        remaining = await client.analysisRecovery.acknowledge(spaceId, capture.id, request.request_id)
      } catch {
        if (!current()) return
        throw new Error('The relay replied, but its recovery reference could not be cleared on this browser. Recover the same request again; do not start another.')
      }
    }
    if (current()) {
      recovery.setPending(remaining)
      memory.adopt(result)
      setNotice(receipt?.rejection
        ? `Analysis was not started. ${receipt.rejection} Your latest saved memory is shown; review it before making a new request.`
        : receipt?.replayed
        ? receipt.current
          ? 'Your earlier request was recovered. No new analysis was started.'
          : 'Your earlier request was recovered. Showing the latest saved context; no new analysis was started.'
        : '')
      setView('memory')
    }
  }
  const analyze = (options: { weather: boolean; ai: boolean; audio_asset_id: string | null }) =>
    void act('Finding the details…', async () => {
      if (pendingAnalysis || !recovery.ready) return
      const current = memory.currentScope()
      const saved = await save()
      if (!current()) return
      const request: MemoryAnalysisRequest = {
        ...options,
        revision: saved.revision,
        ...(saved.analysis_idempotency ? { request_id: crypto.randomUUID() } : {}),
      }
      if (request.request_id) {
        const reserved = await client.analysisRecovery.reserve(spaceId, capture.id, request)
        if (!current()) return
        recovery.setPending(reserved.request)
        setView('memory')
        if (reserved.existing) return // Another panel already froze an intent. Ask before replaying it.
      }
      await submitAnalysis(request)
    })
  const recoverAnalysis = () => {
    if (dirty && !window.confirm('Recover the saved analysis and replace your unsaved text and place/time edits with the latest saved version?')) return
    void act('Recovering your request…', async () => {
    if (!pendingAnalysis || !recovery.ready) return
    const current = memory.currentScope()
    const reserved = await client.analysisRecovery.reserve(spaceId, capture.id, pendingAnalysis)
    if (!current()) return
    recovery.setPending(reserved.request)
    if (reserved.request.request_id !== pendingAnalysis.request_id) return
    await submitAnalysis(reserved.request)
    })
  }
  const analysis = data?.analysis
  const currentAnalysis = analysis && ['complete', 'partial'].includes(analysis.status) && !dirty
  return (
    <section ref={panel} className="memory-panel" aria-label="Memory context" aria-busy={!!busy}>
      {error && (
        <div className="memory-error" role="alert">
          {error}
          {!pendingAnalysis && <button
            disabled={recording || !!busy}
            onClick={() => {
              if (!unsaved || window.confirm('Reload and discard unsaved memory changes?')) {
                setFile(null)
                memory.reload()
              }
            }}
          >
            Reload saved context
          </button>}
        </div>
      )}
      {pendingAnalysis && !busy && (
        <div ref={recoveryNotice} tabIndex={-1} className="memory-callout memory-recovery" role="status">
          <p>This request hasn’t been confirmed. Recover it with the same settings. We’ll reuse an
            existing job—or submit it once if it never arrived. Selected providers may charge.</p>
          <button
            className="atlas-secondary"
            disabled={recording}
            onClick={recoverAnalysis}
          >Recover this request</button>
          <p className="atlas-fine">Saved settings: AI {pendingAnalysis.ai ? 'on' : 'off'} · weather {pendingAnalysis.weather ? 'on' : 'off'} · {pendingAnalysis.audio_asset_id ? 'selected recording' : 'original media'}.</p>
          <p className="atlas-fine">This recovery reference is saved on this browser for the same workspace connection.
            You can close and return. Nothing resumes automatically. Clearing browser data or changing
            credentials can make it unavailable.</p>
        </div>
      )}
      {recovery.error && <div className="memory-error" role="alert">
        <p>{recovery.error}</p>
        <button className="atlas-secondary" onClick={recovery.retry}>Check recovery storage</button>
      </div>}
      {!data ? (
        <p role="status">
          {error ? 'Memory context is unavailable on this server.' : 'Opening your memory…'}
        </p>
      ) : (
        <>
          {!data.can_edit && (
            <p className="memory-callout">
              You can revisit this memory. Its contributor or the Space owner can add context and
              recordings.
            </p>
          )}
          {data.can_edit && (
            <p className="atlas-fine">
              Your story and recordings are shared with everyone who has access to this Space.
              Earlier versions stay in its edit history. Nothing is posted publicly.
            </p>
          )}
          <div hidden={view !== 'memory'}>
            <div className="memory-compose">
              <MemoryPreview
                client={client}
                spaceId={spaceId}
                capture={capture}
                visible={view === 'memory'}
              />
              <div className="memory-story">
                <fieldset disabled={disabled}>
                  <label className="memory-caption">
                    What made this moment memorable?
                    <textarea
                      rows={3}
                      maxLength={2000}
                      value={notes.description}
                      onChange={(event) => edit({ description: event.target.value })}
                      placeholder="Add a thought, or let the moment speak for itself…"
                    />
                  </label>
                  <div className="memory-feeling-label">
                    How did it feel? <span>Optional</span>
                  </div>
                  <div
                    className="memory-choice memory-moods"
                    role="group"
                    aria-label="How did it feel?"
                  >
                    {MOODS.map((mood) => (
                      <button
                        key={mood}
                        aria-pressed={notes.feeling === mood}
                        onClick={() => edit({ feeling: notes.feeling === mood ? '' : mood })}
                      >
                        {mood}
                      </button>
                    ))}
                  </div>
                  {notes.feeling && !MOODS.includes(notes.feeling) && (
                    <p className="memory-personal-feeling">{notes.feeling}</p>
                  )}
                  <details className="memory-own-words">
                    <summary>In your own words</summary>
                    <label>
                      Your feeling
                      <input
                        maxLength={500}
                        value={notes.feeling}
                        onChange={(event) => edit({ feeling: event.target.value })}
                        placeholder="Only you know how it felt"
                      />
                    </label>
                  </details>
                </fieldset>
                <div className="memory-tools" aria-label="Add to this memory">
                  {(
                    [
                      [
                        'sound',
                        'speech',
                        data.assets.length
                          ? `${data.assets.length} recording${data.assets.length === 1 ? '' : 's'}`
                          : 'Add sound',
                      ],
                      ['music', 'heart', notes.music_title || 'Music'],
                      ['place', 'pin', 'Place & time'],
                    ] as [View, IconName, string][]
                  ).map(([next, icon, label]) => (
                    <button
                      key={next}
                      data-memory-view={next}
                      onClick={(event) => open(next, event.currentTarget)}
                    >
                      <Icon name={icon} size={18} />
                      <span>{label}</span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
            {file && (
              <button
                className="memory-pending"
                onClick={(event) => open('sound', event.currentTarget)}
              >
                Finish adding {file.name} →
              </button>
            )}
            {running ? (
              <div>
                <p className="memory-progress" role="status">
                  <Icon name="spark" size={18} />
                  {analysis?.status === 'cancelling'
                    ? 'Stop requested. Waiting for the current work to finish; no new generated context will be kept.'
                    : analysis?.status === 'interrupted'
                      ? 'Analysis is taking longer than expected. We are still checking; its worker has not confirmed completion.'
                      : 'Finding the details… Your original is saved. You can come back later.'}
                </p>
                {data.can_cancel && analysis && analysis.status !== 'cancelling' && (
                  <button
                    className="atlas-secondary"
                    disabled={!!busy}
                    onClick={() => void act('Requesting a stop…', async () => {
                      const result = await client.cancelMemory(spaceId, capture.id, analysis.id)
                      if (mounted.current) setData(result)
                    })}
                  >
                    Stop analysis
                  </button>
                )}
                {(data.can_cancel || analysis?.status === 'cancelling') && (
                  <p className="atlas-fine">
                    Already-sent provider work may finish and incur charges. A stop request is not
                    a refund or a recall of sent data. If this stays pending, the workspace operator
                    must verify the worker stopped.
                  </p>
                )}
              </div>
            ) : currentAnalysis ? (
              <div className="memory-draft">
                <span className="atlas-eyebrow">
                  {analysis.suggestion ? 'AI DRAFT · YOUR REVIEW' : 'CONTEXT · YOUR REVIEW'}
                </span>
                {analysis.suggestion && <p>{analysis.suggestion.summary}</p>}
                {analysis.weather && (
                  <div className="memory-weather">
                    <span>Weather · estimated</span>
                    {['temperature_2m', 'wind_speed_10m'].map((key) => {
                      const field = analysis.weather?.fields[key]
                      return (
                        field && (
                          <strong key={key}>
                            {key === 'wind_speed_10m' ? 'Wind ' : ''}
                            {field.value} {field.unit}
                          </strong>
                        )
                      )
                    })}
                    <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">
                      Open-Meteo
                    </a>
                  </div>
                )}
                {analysis.transcript && (
                  <details>
                    <summary>Read the speech transcript · AI draft</summary>
                    <p>{analysis.transcript.text}</p>
                  </details>
                )}
                {analysis.status === 'partial' && (
                  <p className="atlas-fine">
                    Some details couldn’t be gathered. Check sources for what’s missing.
                  </p>
                )}
                {!analysis.suggestion && !analysis.weather && !analysis.transcript && (
                  <p>Local context inspected. Details and sources are ready to review.</p>
                )}
                <div className="memory-actions">
                  <button
                    data-memory-view="evidence"
                    onClick={(event) => open('evidence', event.currentTarget)}
                  >
                    Details & sources
                  </button>
                  {canAnalyze && (
                    <button
                      data-memory-view="assist"
                      disabled={disabled || !!file}
                      onClick={(event) => open('assist', event.currentTarget)}
                    >
                      Adjust assistance
                    </button>
                  )}
                </div>
              </div>
            ) : (
              canAnalyze && (
                <button
                  className="memory-assist"
                  data-memory-view="assist"
                  disabled={disabled || !!file}
                  onClick={(event) => open('assist', event.currentTarget)}
                >
                  <span className="memory-assist-icon">
                    <Icon name="spark" size={22} />
                  </span>
                  <span>
                    <strong>Let the details come to you</strong>
                    <small>
                      {analysis
                        ? dirty
                          ? 'Your edits need fresh context. Find the details again.'
                          : `Previous context is ${analysis.status}. Review or try again.`
                        : 'Optional AI & weather, with your permission.'}
                    </small>
                  </span>
                  <Icon name="arrow" size={18} />
                </button>
              )
            )}
            {!currentAnalysis && (
              <button
                className="atlas-text-button memory-source-link"
                data-memory-view="evidence"
                onClick={(event) => open('evidence', event.currentTarget)}
              >
                Details & sources
              </button>
            )}
            {analysis?.status === 'cancelled' && <p role="status" className="atlas-fine">Analysis stopped. No new generated context was kept. Your original and story are unchanged.</p>}
            {data.can_edit && !canAnalyze && (
              <p className="atlas-fine">
                You can keep your story and sounds now. AI and weather assistance is currently
                available only through the connected workspace.
              </p>
            )}
          </div>
          {view !== 'memory' && (
            <div className="memory-focused">
              <div className="memory-editor-heading">
                <button
                  className="atlas-icon-button"
                  aria-label="Back to memory"
                  disabled={recording}
                  onClick={() => setView('memory')}
                >
                  <Icon name="back" />
                </button>
                <h3 ref={focusTarget} tabIndex={-1}>
                  {EDITORS[view]}
                </h3>
              </div>
              {view === 'evidence' ? (
                <>
                <MemoryEvidence
                  data={data}
                  client={client}
                  spaceId={spaceId}
                  inspectionStatus={memory.inspectionStatus}
                  onInspect={() =>
                    void act('Reading metadata…', async () => {
                      const result = await client.inspectMemory(spaceId, capture.id)
                      if (mounted.current)
                        setData(
                          (current) =>
                            current && {
                              ...current,
                              inspection: result.inspection,
                            },
                        )
                    })
                  }
                />
                {data.can_remove && onRemove && client.memoryRemovalSupported && <div className="memory-removal-entry">
                  <button className="atlas-text-button" disabled={unsaved || !!pendingAnalysis} onClick={onRemove}>Review removal from this Space</button>
                  {unsaved && <p className="atlas-fine">Save or finish your pending edits and recordings before reviewing removal.</p>}
                </div>}
                </>
              ) : (
                <fieldset disabled={view === 'sound' ? !!busy : disabled}>
                  {view === 'place' && (
                    <PlaceEditor
                      capture={capture}
                      data={data}
                      notes={notes}
                      coordinates={coordinates}
                      edit={edit}
                      coordinate={coordinate}
                      onLocate={(operation) => void act('Checking location…', operation)}
                    />
                  )}
                  {view === 'music' && <MusicEditor notes={notes} edit={edit} />}
                  {view === 'assist' && (
                    <AssistEditor
                      data={data}
                      notes={notes}
                      coordinates={coordinates}
                      disabled={disabled || !!file || !canAnalyze || !recovery.ready}
                      onPlace={() => setView('place')}
                      onAnalyze={analyze}
                    />
                  )}
                  {view === 'sound' && (
                    <>
                      <p>Keep the sounds around you, or tell the story in your own voice.</p>
                      {data.assets.map((asset) => (
                        <MemoryTrack
                          key={asset.id}
                          asset={asset}
                          client={client}
                          spaceId={spaceId}
                          captureId={capture.id}
                        />
                      ))}
                      {notes.music_url && (
                        <a href={notes.music_url} target="_blank" rel="noreferrer noopener">
                          Open music reference ↗
                        </a>
                      )}
                      {data.can_edit &&
                        (client.memoryUploadsSupported ? (
                          <fieldset disabled={disabled}>
                            {!file && (
                              <>
                                <VoiceNote
                                  disabled={disabled || data.assets.length >= 8}
                                  onActive={setRecording}
                                  onFile={(value) => chooseFile(value, 'narration')}
                                />
                                <label className="memory-file">
                                  Choose audio or video
                                  <input
                                    type="file"
                                    disabled={recording || data.assets.length >= 8}
                                    accept="audio/mpeg,audio/mp4,audio/wav,audio/x-wav,audio/x-m4a,audio/webm,audio/ogg,audio/flac,video/mp4,video/webm"
                                    onChange={(event) => {
                                      const selected = event.target.files?.[0]
                                      if (selected) chooseFile(selected)
                                      event.target.value = ''
                                    }}
                                  />
                                </label>
                              </>
                            )}
                            {file && (
                              <div className="memory-attachment">
                                <strong>{file.name}</strong>
                                <small>
                                  {(file.size / 1024 / 1024).toFixed(1)} MB · not uploaded yet
                                </small>
                                <div
                                  className="memory-choice"
                                  role="group"
                                  aria-label="Recording role"
                                >
                                  {(['ambient', 'narration', 'soundtrack'] as const).map(
                                    (value) => (
                                      <button
                                        key={value}
                                        aria-pressed={role === value}
                                        onClick={() => setRole(value)}
                                      >
                                        {value === 'ambient'
                                          ? 'Around me'
                                          : value === 'narration'
                                            ? 'My story'
                                            : 'Soundtrack'}
                                      </button>
                                    ),
                                  )}
                                </div>
                                <details>
                                  <summary>Rename recording</summary>
                                  <label>
                                    Recording name
                                    <input
                                      maxLength={120}
                                      value={title}
                                      onChange={(event) => setTitle(event.target.value)}
                                    />
                                  </label>
                                </details>
                                <label className="memory-check">
                                  <input
                                    type="checkbox"
                                    checked={rights}
                                    onChange={(event) => setRights(event.target.checked)}
                                  />
                                  <span>
                                    I recorded this or have permission to share it, including music
                                    and people’s voices.
                                  </span>
                                </label>
                                <div className="memory-actions">
                                  <button
                                    className="atlas-primary"
                                    disabled={!rights || !title.trim()}
                                    onClick={attach}
                                  >
                                    Add recording
                                  </button>
                                  <button
                                    onClick={() => {
                                      setFile(null)
                                      setRights(false)
                                    }}
                                  >
                                    Discard recording
                                  </button>
                                </div>
                              </div>
                            )}
                            <p className="atlas-fine">
                              Up to 64 MB per file, eight tracks per capture. Nothing plays
                              automatically. Recordings are shared with this space’s invited
                              viewers.
                            </p>
                          </fieldset>
                        ) : (
                          <p className="memory-callout">
                            Attach recordings from the web console for now. Your Android camera and
                            original upload flow are unchanged.
                          </p>
                        ))}
                    </>
                  )}
                </fieldset>
              )}
              {view !== 'assist' && (
                <button
                  className="atlas-secondary atlas-full"
                  disabled={recording || !!busy}
                  onClick={() => setView('memory')}
                >
                  Back to memory
                </button>
              )}
            </div>
          )}
          {view !== 'assist' && (
            <footer className="memory-footer">
              <div role="status" aria-live="polite">
                {busy ||
                  (kept
                    ? notice || 'Memory kept. Shared with your space.'
                    : dirty
                      ? 'Your changes are ready to keep.'
                      : notice || 'Shared with your space. No public post.')}
              </div>
              {data.can_edit && !running && !kept && !pendingAnalysis ? (
                <button
                  className="atlas-primary"
                  disabled={!!busy || !!file || recording || !!pendingAnalysis}
                  onClick={keep}
                >
                  <Icon name="check" size={18} />
                  Keep this memory
                </button>
              ) : (
                onDone && (
                  <button className="atlas-primary" disabled={unsaved} onClick={onDone}>
                    {running || pendingAnalysis ? 'Done for now' : 'Done'}
                  </button>
                )
              )}
            </footer>
          )}
        </>
      )}
    </section>
  )
}

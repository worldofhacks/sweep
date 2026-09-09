import { useState } from 'react'
import { locate } from '../atlas/client'
import type { Capture } from '../atlas/types'
import type { MemoryContext, MemoryNotes } from './types'

export function PlaceEditor({
  capture,
  data,
  notes,
  coordinates,
  edit,
  coordinate,
  onLocate,
}: {
  capture: Capture
  data: MemoryContext
  notes: MemoryNotes
  coordinates: string[]
  edit: (value: Partial<MemoryNotes>) => void
  coordinate: (value: string[]) => void
  onLocate: (operation: () => Promise<void>) => void
}) {
  const inspection = data.inspection ?? data.analysis?.inspection
  const candidates = [
    {
      label: 'Capture',
      timestamp: capture.captured_at != null ? new Date(capture.captured_at).toISOString() : null,
      location: capture.position,
    },
    { label: 'Embedded', timestamp: inspection?.timestamp, location: inspection?.location },
  ].filter((value) => value.timestamp || value.location)
  return (
    <>
      <p>Check the details found in your capture. Unknown is okay—you can skip this.</p>
      {candidates.map((value) => (
        <div className="memory-evidence" key={value.label}>
          <strong>{value.label} details · please confirm</strong>
          {value.timestamp && (
            <div className="memory-found">
              <span>{new Date(value.timestamp).toLocaleString()}</span>
              <button onClick={() => edit({ occurred_at: value.timestamp })}>
                Use {value.label.toLowerCase()} time
              </button>
            </div>
          )}
          {value.location && (
            <div className="memory-found">
              <span>
                {value.location.latitude.toFixed(4)}, {value.location.longitude.toFixed(4)}
              </span>
              <button
                onClick={() =>
                  coordinate([String(value.location!.latitude), String(value.location!.longitude)])
                }
              >
                Use {value.label.toLowerCase()} location
              </button>
            </div>
          )}
        </div>
      ))}
      {!candidates.length && (
        <p className="memory-callout">
          No reliable time or location was found in this file. We won’t substitute the upload time
          or guess where you were.
        </p>
      )}
      <div className="memory-evidence">
        <strong>Your confirmed context</strong>
        <p>
          {notes.occurred_at && Number.isFinite(Date.parse(notes.occurred_at))
            ? new Date(notes.occurred_at).toLocaleString()
            : 'Time not set'}{' '}
          ·{' '}
          {coordinates.every((value) => value.trim())
            ? `${coordinates[0]}, ${coordinates[1]}`
            : 'Place not set'}
        </p>
        {(notes.occurred_at || coordinates.some(Boolean)) && (
          <button
            className="atlas-text-button"
            onClick={() => {
              edit({ occurred_at: null })
              coordinate(['', ''])
            }}
          >
            Clear time and place
          </button>
        )}
      </div>
      <button
        className="atlas-secondary"
        onClick={() =>
          onLocate(async () => {
            const value = await locate()
            coordinate([String(value.latitude), String(value.longitude)])
          })
        }
      >
        I’m here now · use my location
      </button>
      <details className="memory-manual">
        <summary>Enter or adjust details manually</summary>
        <label>
          When did this happen?
          <input
            value={notes.occurred_at ?? ''}
            onChange={(event) => edit({ occurred_at: event.target.value })}
            maxLength={40}
            placeholder="2026-09-08T18:30:00-05:00"
          />
        </label>
        <p className="atlas-fine">
          Include the UTC offset: −05:00 in Austin during daylight time, −06:00 in winter. Leave
          unknown details empty.
        </p>
        <div className="memory-coordinates">
          {['Latitude', 'Longitude'].map((label, index) => (
            <label key={label}>
              {label}
              <input
                inputMode="decimal"
                value={coordinates[index]}
                onChange={(event) =>
                  coordinate(
                    coordinates.map((value, i) => (i === index ? event.target.value : value)),
                  )
                }
              />
            </label>
          ))}
        </div>
      </details>
    </>
  )
}

export function MusicEditor({
  notes,
  edit,
}: {
  notes: MemoryNotes
  edit: (value: Partial<MemoryNotes>) => void
}) {
  return (
    <>
      <p>That song that takes you right back.</p>
      <label>
        Song and artist
        <input
          maxLength={200}
          value={notes.music_title}
          onChange={(event) => edit({ music_title: event.target.value })}
          placeholder="What was playing?"
        />
      </label>
      <label>
        Music link · optional
        <input
          type="url"
          inputMode="url"
          maxLength={1000}
          value={notes.music_url}
          onChange={(event) => edit({ music_url: event.target.value })}
          placeholder="Spotify, Apple Music, or YouTube link"
        />
      </label>
      <p className="atlas-fine">
        A reference link, not a download or playback license. A soundtrack is a creative addition
        unless you say it actually played there.
      </p>
    </>
  )
}

export function AssistEditor({
  data,
  notes,
  coordinates,
  disabled,
  onPlace,
  onAnalyze,
}: {
  data: MemoryContext
  notes: MemoryNotes
  coordinates: string[]
  disabled: boolean
  onPlace: () => void
  onAnalyze: (options: { weather: boolean; ai: boolean; audio_asset_id: string | null }) => void
}) {
  const [weather, setWeather] = useState(false),
    [ai, setAI] = useState(false),
    [audioId, setAudioId] = useState(() => {
      const recordings = data.assets.filter((asset) => asset.role !== 'soundtrack')
      return !(data.inspection ?? data.analysis?.inspection)?.has_audio && recordings.length === 1
        ? recordings[0].id
        : ''
    })
  const hasContext =
    !!notes.occurred_at &&
    Number.isFinite(Date.parse(notes.occurred_at)) &&
    coordinates.every((value) => value.trim() && Number.isFinite(Number(value)))
  const allowance = data.analysis_allowance
  const providerReady = !allowance || (allowance.remaining > 0 && !allowance.error)
  return (
    <>
      <p>A little help with the details. Choose what to use, then review what comes back.</p>
      {allowance && (
        <div className="memory-callout" role="status" aria-label="External assistance allowance">
          <strong>{providerReady
            ? `${allowance.remaining} provider requests available today`
            : 'External assistance is paused'}</strong>
          <p>{allowance.error
            ? 'The workspace’s request allowance needs attention from its operator.'
            : !allowance.limits.space || !allowance.limits.relay
              ? 'Ask the workspace operator to set daily request allowances before using external assistance.'
              : `Shared across this Space and its workspace server. Resets ${new Date(allowance.resets_at).toLocaleString()}.`}</p>
          <small>
            AI uses up to two requests; weather uses one. This is not a price or billing balance.
            Failed or interrupted requests count. If the allowance runs out, later stages won’t run.
            Local context and originals stay available.
          </small>
        </div>
      )}
      <label className="memory-check memory-provider">
        <input
          type="checkbox"
          checked={ai && providerReady}
          disabled={!data.capabilities.ai || !providerReady}
          onChange={(event) => setAI(event.target.checked)}
        />
        <span>
          <strong>Draft my memory with AI</strong>
          <small>
            One visual preview, your notes/context, and up to 60 seconds of selected audio go to
            OpenAI. Suggestions and transcripts need your review.
          </small>
          {!data.capabilities.ai && <small>AI isn’t connected yet. Server setup is needed.</small>}
        </span>
      </label>
      {ai && providerReady && (
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
      )}
      <label className="memory-check memory-provider">
        <input
          type="checkbox"
          checked={weather && providerReady}
          disabled={!data.capabilities.weather || !hasContext || !providerReady}
          onChange={(event) => setWeather(event.target.checked)}
        />
        <span>
          <strong>Bring back the weather</strong>
          <small>
            Sends the confirmed place and date to Open-Meteo. Hourly model estimates—not
            measurements at the scene.
          </small>
          {!data.capabilities.weather && <small>Weather isn’t connected on this server.</small>}
        </span>
      </label>
      {data.capabilities.weather && !hasContext && (
        <button className="atlas-text-button" onClick={onPlace}>
          Confirm place & time for weather →
        </button>
      )}
      <p className="atlas-fine">
        Nothing is sent to these providers until you continue. Your feelings are yours to describe;
        AI won’t infer them from faces or voices. Soundtracks are never transcribed.
      </p>
      <details className="memory-manual">
        <summary>Local analysis & setup details</summary>
        <p className="atlas-fine">
          Metadata is read on your workspace server automatically. Local audio analysis measures
          file level, not real-world loudness or sound events. AI needs a server-side OpenAI key;
          weather needs an enabled Open-Meteo plan.
          {!allowance && ' This server does not report a daily request allowance; provider charges may apply.'}
        </p>
        <button
          className="atlas-secondary"
          disabled={disabled}
          onClick={() => onAnalyze({ weather: false, ai: false, audio_asset_id: null })}
        >
          Inspect local context
        </button>
      </details>
      <footer className="memory-footer">
        <div>{data.analysis
          ? 'This is a new request. Selected providers may charge again.'
          : 'Only the providers you select will be used.'}</div>
        <button
          className="atlas-primary"
          disabled={disabled || !providerReady || (!ai && !weather)}
          onClick={() => onAnalyze({ weather, ai, audio_asset_id: audioId || null })}
        >
          {data.analysis ? 'Run another analysis' : 'Find the details'}
        </button>
      </footer>
    </>
  )
}

import { useState } from 'react'
import type { CatalogClient } from '../../catalog/client'
import {
  captureFilters,
  deriveCatalogLink,
  filesLabel,
  formatPose,
  groupCapturesByProject,
  type CatalogLink,
} from '../../catalog/derive'
import { closedRelayCaptures, openRelayCaptures, type OpenCapture } from '../../catalog/relay-captures'
import type { CaptureRecord } from '../../catalog/types'
import { formatDroneId } from '../../control/state'
import { Pane } from '../../shell/Pane'
import { formatTime } from '../../shell/format'
import { noteFromError, type CatalogNoteState } from '../catalog-notes'
import { CatalogNote, LinkNotice } from '../catalog-shared'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'

/**
 * Capture library: the relay's retained captures (closed sets as records, open sets as
 * progress lines) ahead of the catalog, grouped by project, filtered by room, aircraft
 * and retake flag, newest first. Downloads and exports go through the catalog client
 * and report their outcome sentence; nothing is re-encoded here.
 */
export function CapturesModule({ controller, catalog }: ModuleProps) {
  const [filterId, setFilterId] = useState('all')
  const [note, setNote] = useState<CatalogNoteState | null>(null)
  const relayCaptures = controller.state.captures
  const closed = closedRelayCaptures(relayCaptures, controller.state.sessionId)
  const open = openRelayCaptures(relayCaptures)
  const catalogCaptures = catalog.snapshot.captures
  const captures =
    catalogCaptures === null && closed.length === 0 && open.length === 0
      ? null
      : [...closed, ...(catalogCaptures ?? [])]
  const link = deriveCatalogLink(
    controller.state,
    'The capture library',
    'downloads and exports are refused',
  )

  const run = async (action: () => Promise<string>) => {
    try {
      setNote({ text: await action(), tone: 'muted' })
    } catch (error) {
      setNote(noteFromError(error))
    }
  }

  return (
    <Pane title="Capture library" note="Captured media by room, capture, aircraft and time.">
      <LinkNotice link={link} label="Capture library connection" />
      <CatalogNote label="Capture library notice" note={note} />
      {open.length > 0 && <OpenCaptures captures={open} />}
      {captures === null ? (
        <EmptyModule what="captured media sets" />
      ) : (
        <CaptureCatalog
          captures={captures}
          filterId={filterId}
          onFilter={setFilterId}
          link={link}
          client={catalog.client}
          onRun={run}
        />
      )}
    </Pane>
  )
}

/** Captures the relay holds files for but no bundle has closed yet: progress, not records. */
function OpenCaptures({ captures }: { captures: OpenCapture[] }) {
  return (
    <section aria-label="Captures in progress">
      <p className="cat-eyebrow cap-project">In progress</p>
      {captures.map((capture) => (
        <p key={`${capture.drone_id}-${capture.connection_epoch}-${capture.capture_id}`} className="cap-open mono">
          {capture.capture_id} · {formatDroneId(capture.drone_id)} · {filesLabel(capture.files)} captured,{' '}
          {capture.retrieved} retrieved · {capture.phase} · {formatTime(capture.updated_at)}
        </p>
      ))}
    </section>
  )
}

function CaptureCatalog({
  captures,
  filterId,
  onFilter,
  link,
  client,
  onRun,
}: {
  captures: CaptureRecord[]
  filterId: string
  onFilter: (id: string) => void
  link: CatalogLink
  client: CatalogClient
  onRun: (action: () => Promise<string>) => void
}) {
  const filters = captureFilters(captures)
  const active = filters.find((filter) => filter.id === filterId) ?? filters[0]
  const visible = captures.filter(active.test)
  const projects = groupCapturesByProject(visible)
  return (
    <div className="cat-swap">
      <div className="cat-filters" role="group" aria-label="Capture filters">
        {filters.map((filter) => (
          <button
            key={filter.id}
            type="button"
            className={filter.id === active.id ? 'cat-filter is-active' : 'cat-filter'}
            aria-pressed={filter.id === active.id}
            onClick={() => onFilter(filter.id)}
          >
            {filter.label}
          </button>
        ))}
      </div>
      {visible.length === 0 ? (
        <p className="cat-empty">
          No captures match this filter. A project with no captures shows the same notice.
        </p>
      ) : (
        projects.map((project) => (
          <section key={project.project} aria-label={`Project ${project.project}`}>
            <p className="cat-eyebrow cap-project">Project {project.project}</p>
            <div data-two="1">
              {project.captures.map((capture) => (
                <CaptureItem
                  key={capture.capture_id}
                  capture={capture}
                  link={link}
                  client={client}
                  onRun={onRun}
                />
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  )
}

function CaptureItem({
  capture,
  link,
  client,
  onRun,
}: {
  capture: CaptureRecord
  link: CatalogLink
  client: CatalogClient
  onRun: (action: () => Promise<string>) => void
}) {
  const blocked = link.up ? undefined : `The console connection is ${link.status}. Nothing can be sent.`
  return (
    <article className="cap-item" aria-label={`Capture ${capture.capture_id}`}>
      <div className="cap-item-row">
        <div className="cap-thumb" aria-hidden="true" />
        <div className="cap-item-copy">
          <p className="cap-id">{capture.capture_id}</p>
          <p className="cap-meta">
            {capture.room_id} · {formatDroneId(capture.drone_id)} · {formatTime(capture.captured_at)}
          </p>
          <p className="cap-pattern">
            <span className="mono">{capture.pattern}</span>
          </p>
          <p className="cap-coverage">{capture.coverage}</p>
          <p className="cap-quality">
            {filesLabel(capture.files)} · quality{' '}
            <strong className={capture.quality === 'pass' ? 'tone-ok' : 'tone-danger'}>
              {capture.quality}
            </strong>
          </p>
          {capture.needs_retake && <p className="cap-retake">needs retake</p>}
        </div>
      </div>
      <p className="cap-checksum">{capture.checksum ?? 'checksum unreported'}</p>
      <p className="cap-pose">{formatPose(capture.pose)}</p>
      <div className="cap-actions">
        <button
          type="button"
          className="cat-button"
          disabled={!link.up}
          title={blocked}
          onClick={() => onRun(() => client.stageCaptureSet(capture.capture_id))}
        >
          Download set
        </button>
        <button
          type="button"
          className="cat-button"
          disabled={!link.up}
          title={blocked}
          onClick={() => onRun(() => client.exportCaptureMetadata(capture.capture_id))}
        >
          Export metadata
        </button>
      </div>
    </article>
  )
}

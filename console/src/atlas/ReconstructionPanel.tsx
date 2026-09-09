import { useState } from 'react'
import type { AtlasClient } from './client'
import { Icon } from './Icon'
import type { Reconstruction } from './types'

export function ReconstructionPanel({
  job,
  client,
  spaceId,
  canBuild,
  onChange,
}: {
  job: Reconstruction
  client: AtlasClient
  spaceId: string
  canBuild: boolean
  onChange: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const processing = !['not_started', 'ready', 'failed'].includes(job.status)
  const textured = job.representation === 'textured_mesh'
  const start = async () => {
    setBusy(true)
    setError('')
    try {
      await client.reconstruct(spaceId)
      onChange()
    } catch (value) {
      setError(value instanceof Error ? value.message : 'The build could not be started.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="atlas-world-state" data-state={job.status}>
      <div className="atlas-world-emblem">
        <Icon name="cube" size={64} />
      </div>
      <span className="atlas-eyebrow">FROM PERSPECTIVES TO PLACE</span>
      <h3>
        {job.status === 'ready'
          ? 'Your space, in 3D.'
          : processing
            ? 'Connecting the perspectives.'
            : 'Build the next dimension.'}
      </h3>
      <p role="status">{job.detail}</p>
      {processing && (
        <progress aria-label="Reconstruction progress" value={job.progress ?? 0} max={100} />
      )}
      {job.status === 'ready' && (
        <dl className="atlas-reconstruction-stats">
          <div>
            <dt>Reconstructed views</dt>
            <dd>
              {job.registered_views} / {job.prepared_views}
            </dd>
          </div>
          <div>
            <dt>{textured ? 'Surface triangles' : 'Observed 3D points'}</dt>
            <dd>{(textured ? job.faces : job.points)?.toLocaleString()}</dd>
          </div>
          <div>
            <dt>Camera fit · reprojection</dt>
            <dd>{job.mean_reprojection_error_px?.toFixed(2)} px</dd>
          </div>
          <div>
            <dt>Scale</dt>
            <dd>Relative · not metric</dd>
          </div>
        </dl>
      )}
      {job.experimental && <p className="atlas-fine">Experimental local reconstruction · not production-qualified.</p>}
      {(job.components ?? 0) > 1 && (
        <p className="atlas-fine">
          The views formed {job.components} separate components. The largest is shown; add
          overlapping views between the disconnected areas.
        </p>
      )}
      {!!job.new_source_count && (
        <p>{job.new_source_count} new captures are available for the next build.</p>
      )}
      {canBuild && (
        <button
          className="atlas-primary atlas-full"
          disabled={busy || processing || job.source_count === 0}
          onClick={() => void start()}
        >
          <Icon name="cube" />
          {busy
            ? 'Starting…'
            : processing
              ? 'Build in progress'
              : job.status === 'ready'
                ? 'Rebuild with current captures'
                : 'Build 3D atlas'}
        </button>
      )}
      {!canBuild && !processing && job.status !== 'ready' && (
        <p className="atlas-fine">
          The space owner can start a reconstruction once overlapping views are ready.
        </p>
      )}
      {error && (
        <p role="alert" className="atlas-inline-notice">
          {error}
        </p>
      )}
      <p className="atlas-fine">
        {job.source_count} originals preserved. {textured
          ? 'Photo-textured surfaces are estimates from overlapping views. Gaps remain; camera fit is not a surface-accuracy score.'
          : 'Camera poses and sparse points come from matching image features. Detailed surfaces require a configured dense worker.'}
        {' '}Relative scale only; this model does not establish geographic measurements or prove complete coverage.
      </p>
    </div>
  )
}

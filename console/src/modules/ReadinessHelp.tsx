import type { RelayAircraftState } from '../relay/contract'
import { readinessNotes } from '../shell/readiness'

export function ReadinessHelp({
  drone,
  className,
  capabilityProfile = null,
}: {
  drone: RelayAircraftState
  className: string
  capabilityProfile?: string | null
}) {
  const notes = readinessNotes(drone, capabilityProfile)
  if (notes.length === 0) return null
  return (
    <div className={className}>
      {notes.map(({ code, text }) => (
        <p key={code ?? 'position-quality-warning'}>
          {code !== null ? <><code>{code}</code> — {text}</> : <span className="tone-warn">{text}</span>}
        </p>
      ))}
    </div>
  )
}

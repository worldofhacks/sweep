import type { OperatorNotice } from '../control/state'

export function DangerBanner({ notice }: { notice: OperatorNotice | null }) {
  if (!notice) return null
  return (
    <p className="sh-danger" role="alert">
      <strong>Danger</strong> — <span>{notice.title}:</span> <span>{notice.detail}</span>
    </p>
  )
}

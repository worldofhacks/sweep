import type { OperatorNotice } from '../control/state'

/**
 * One notice under the header row, mounted at all times as a polite live region
 * so assistive technology hears each warning or info notice as it arrives,
 * whether or not the session sheet is open. The shell hands it the newest
 * warning or info notice; danger notices go to the DangerBanner alert instead.
 */
export function NoticeLine({ notice }: { notice: OperatorNotice | null }) {
  return (
    <p
      className={notice ? `sh-notice-line is-${notice.level}` : 'sh-notice-line is-empty'}
      role="status"
      aria-live="polite"
      aria-label="Latest notice"
    >
      {notice && (
        <>
          <strong>{severityWord(notice.level)}</strong> — <span>{notice.title}:</span>{' '}
          <span>{notice.detail}</span>
        </>
      )}
    </p>
  )
}

function severityWord(level: OperatorNotice['level']): string {
  if (level === 'danger') return 'Danger'
  if (level === 'warning') return 'Warning'
  return 'Info'
}

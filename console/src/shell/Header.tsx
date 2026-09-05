import type { ReactNode } from 'react'
import type { ControlState, OperatorNotice } from '../control/state'
import {
  deriveLinks,
  deriveRcLine,
  deriveReadyCount,
  deriveSelectionLabel,
  deriveStateTags,
  deriveStop,
  isLinkUp,
  noticeSummary,
  type StopTimes,
} from './derive'
import { formatTime } from './format'

const STOP_REASON_ID = 'network-stop-reason'
const SHEET_NOTICE_CAP = 5

export interface HeaderProps {
  state: ControlState
  stopTimes: StopTimes
  /** Wall-clock at render, injected so the cleared-stop notice window is testable. */
  now: number
  onStop: () => void
  detailOpen: boolean
  onToggleDetail: () => void
  isFixture: boolean
  webcam?: ControlState['connection']['status']
  /** Banners rendered inside the header, under the session sheet. */
  children?: ReactNode
}

export function Header({
  state,
  stopTimes,
  now,
  onStop,
  detailOpen,
  onToggleDetail,
  isFixture,
  webcam,
  children,
}: HeaderProps) {
  const stop = deriveStop(state, stopTimes, now)
  const tags = deriveStateTags(state)
  const rc = deriveRcLine(state)
  const links = deriveLinks(state, webcam)
  const up = isLinkUp(state.connection.status)

  return (
    <header className="sh-header">
      <div className="sh-header-row">
        <button
          type="button"
          className={stop.active ? 'sh-stop is-active' : 'sh-stop'}
          aria-label="Network stop"
          aria-describedby={STOP_REASON_ID}
          disabled={stop.disabled}
          title={stop.reason}
          onClick={onStop}
        >
          <span className="sh-stop-title">{stop.title}</span>
          <span className="sh-stop-sub">{stop.sub}</span>
        </button>
        <span id={STOP_REASON_ID} className="visually-hidden">
          {stop.reason}
        </span>

        <div className="sh-header-middle">
          <ul className="sh-tags" aria-label="Session state">
            {tags.map((tag) => (
              <li key={tag.id} className={`sh-tag is-${tag.variant}`}>
                <span className="sh-tag-dot" aria-hidden="true" />
                {tag.label}
              </li>
            ))}
            <li className="sh-selection">
              <span className="sh-selection-label">{deriveSelectionLabel(state.selection)}</span>
              <span className="sh-ready">{deriveReadyCount(state.aircraft)}</span>
            </li>
          </ul>
          <p className={rc.danger ? 'sh-rc is-danger' : 'sh-rc'} aria-label="Control authority">
            {rc.text}
          </p>
        </div>

        <div className="sh-header-right">
          <ul className="sh-links" aria-label="Connections">
            {links.map((link) => (
              <li key={link.id} className="sh-pill" title={link.label}>
                <span className={`sh-pill-dot tone-${link.tone}`} aria-hidden="true" />
                <span className="sh-pill-short">{link.short}</span>
                <span className={`sh-pill-value tone-${link.tone}`}>{link.value}</span>
              </li>
            ))}
            <li>
              <span
                className={up ? 'sh-live' : 'sh-live is-down'}
                aria-hidden="true"
                title={up ? 'receiving state frames' : 'no state frames'}
              />
            </li>
          </ul>
          <button
            type="button"
            className="sh-detail-toggle"
            aria-expanded={detailOpen}
            onClick={onToggleDetail}
          >
            {detailOpen ? 'Hide detail' : 'Session detail'}
          </button>
        </div>
      </div>

      {detailOpen && (
        <SessionSheet state={state} stopReason={stop.reason} isFixture={isFixture} />
      )}
      {children}
    </header>
  )
}

function SessionSheet({
  state,
  stopReason,
  isFixture,
}: {
  state: ControlState
  stopReason: string
  isFixture: boolean
}) {
  const shown = state.notices.slice(0, SHEET_NOTICE_CAP)
  return (
    <div className="sh-sheet" data-two="1" aria-label="Session detail">
      <div className="sh-sheet-column">
        <p className="sh-sheet-reason">{stopReason}</p>
        <p className="sh-sheet-line">
          <span className="is-unreported">operating_state unreported</span>
          <span className="is-unreported">operator presence unreported</span>
        </p>
        <p className="sh-sheet-meta">
          <span>mode indoor</span>
          <span>roster v{state.rosterVersion}</span>
          <span>session {state.sessionId}</span>
          <span>console {state.connection.transport}</span>
          <span>keyboard {state.keyboardConnection.transport}</span>
        </p>
        {isFixture && (
          <p className="sh-sheet-fixture">
            Fixture data — the roster is a development fixture. No aircraft are connected.
          </p>
        )}
      </div>
      <div className="sh-notices" data-scroll="1">
        <p className="sh-notices-head">Notices — {noticeSummary(state.notices)}</p>
        <ul className="sh-notice-list">
          {shown.map((notice) => (
            <NoticeRow key={notice.id} notice={notice} />
          ))}
        </ul>
        <p className="sh-notices-cap">
          {state.notices.length === 0
            ? 'No notices in this session.'
            : `Newest first, capped at ${SHEET_NOTICE_CAP}. ${state.notices.length} kept.`}
        </p>
      </div>
    </div>
  )
}

function NoticeRow({ notice }: { notice: OperatorNotice }) {
  const isDanger = notice.level === 'danger'
  return (
    <li
      className="sh-notice"
      role={isDanger ? 'alert' : 'status'}
      aria-live={isDanger ? 'assertive' : 'polite'}
    >
      <span className={`sh-notice-severity tone-${severityTone(notice.level)}`}>{notice.level}</span>
      <span className="sh-notice-text">
        {notice.title}: {notice.detail}
      </span>
      <span className="sh-notice-time">{formatTime(notice.t)}</span>
    </li>
  )
}

function severityTone(level: OperatorNotice['level']): 'danger' | 'warn' | 'muted' {
  if (level === 'danger') return 'danger'
  if (level === 'warning') return 'warn'
  return 'muted'
}

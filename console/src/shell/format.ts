import type { DroneId } from '../relay/contract'
import { formatDroneId } from '../control/state'

export function formatTime(value: number): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(value)
}

export function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}

export function humanizeCode(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase())
}

export function formatSelection(selection: DroneId[]): string {
  return selection.map(formatDroneId).join(', ') || 'None selected'
}

export function formatPercent(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`
}

/** "unreported", "just now", or "N s ago" from the design's ago(); never negative. */
export function formatAgo(now: number, at: number | null): string {
  if (at === null) return 'unreported'
  const seconds = Math.max(0, Math.round((now - at) / 1000))
  return seconds < 1 ? 'just now' : `${seconds} s ago`
}

/** m:ss from a millisecond span, floored at zero. */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/** Age of a reported timestamp; null means the relay never reported one. */
export function formatAge(ageMs: number | null): string {
  if (ageMs === null) return 'unreported'
  const seconds = Math.round(Math.max(0, ageMs) / 1000)
  return seconds < 1 ? 'just now' : `${seconds} s ago`
}

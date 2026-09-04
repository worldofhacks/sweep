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

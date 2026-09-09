import type { Capture } from './types'

export type DatePrecision = 'instant' | 'day' | 'month' | 'year' | 'range' | 'unknown'
export interface DateAssertion { precision: DatePrecision; value: string | null; end: string | null; note: string }
export interface TimelineTime extends DateAssertion {
  source: 'declared' | 'memory_note' | 'capture' | 'metadata' | 'metadata_local' | 'unknown'
  start_date: string | null; end_date: string | null
  utc_offset?: string | null
}
export interface TimeEvidence { source: string; value: string | number }
export interface TimelineEntry { capture: Capture; time: TimelineTime; evidence: TimeEvidence[]; warnings: string[]; revision: number; can_edit: boolean }
export interface Timeline { entries: TimelineEntry[]; total: number; ordering: string }
export interface DateRevision { revision: number; assertion: DateAssertion; previous: TimelineTime; evidence: TimeEvidence[]; actor: string; changed_at: number }

export function utcLabel(value: number): string {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toISOString() : 'Unrecognized source timestamp'
}

export function calendarLabel(value: string, options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', year: 'numeric' }): string {
  const time = new Date(`${value}T12:00:00Z`)
  return Number.isFinite(time.getTime()) ? new Intl.DateTimeFormat(undefined, { ...options, timeZone: 'UTC' }).format(time) : value
}
export function dateLabel(time: DateAssertion): string {
  if (!time.value || time.precision === 'unknown') return 'Date unknown'
  if (time.precision === 'year') return `${time.value} · year only`
  if (time.precision === 'month') return `${calendarLabel(time.value + '-01', { month: 'long', year: 'numeric' })} · month only`
  if (time.precision === 'range') return `${calendarLabel(time.value)} – ${calendarLabel(time.end ?? time.value)}`
  if (time.precision === 'instant') return `${calendarLabel(time.value.slice(0, 10))} · ${time.value.slice(11, 19)} UTC${time.value.endsWith('Z') ? '+00:00' : time.value.slice(-6)}`
  return `${calendarLabel(time.value)} · day only`
}
export function chapter(entry: TimelineEntry): { key: string; label: string } {
  const time = entry.time
  if (!time.start_date) return { key: 'unknown', label: 'Still finding its date' }
  if (time.precision === 'range') return { key: `range:${time.value}:${time.end}`, label: dateLabel(time) }
  if (time.precision === 'year') return { key: `year:${time.value}`, label: dateLabel(time) }
  const key = time.start_date.slice(0, 7)
  return { key, label: calendarLabel(key + '-01', { month: 'long', year: 'numeric' }) }
}
export function dateSource(entry: TimelineEntry): string {
  return { declared: 'Date supplied by a member or workspace operator', memory_note: 'Date from a memory note',
    capture: entry.capture.source === 'camera' ? 'Submitted device clock · shown in UTC' : 'Submitted import timestamp · unverified',
    metadata: 'Embedded media timestamp · unverified', metadata_local: 'Embedded local date · time zone unknown', unknown: 'No event date supplied' }[entry.time.source]
}

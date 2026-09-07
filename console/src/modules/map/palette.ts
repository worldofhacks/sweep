/**
 * Canvas has no cascade, so the map reads its tokens once per draw and falls
 * back to the values in tokens.css when a stylesheet has not been applied
 * (a test environment, or a canvas outside the document).
 */
export interface MapPalette {
  ground: string
  frame: string
  ink: string
  muted: string
  geofence: string
  /** One hue per lidar-fitted device, by unit. */
  scans: readonly string[]
}

export const FALLBACK_PALETTE: MapPalette = {
  ground: '#F8F7F4',
  frame: '#C9C4B6',
  ink: '#16150F',
  muted: '#6E6B62',
  geofence: '#8A5A00',
  scans: ['#2F7F9E', '#B4622B', '#6B5AA6', '#1B6A3F'],
}

const TOKENS: Record<Exclude<keyof MapPalette, 'scans'>, string> = {
  ground: '--color-map-grid',
  frame: '--color-map-frame',
  ink: '--color-ink',
  muted: '--color-eyebrow',
  geofence: '--color-map-geofence',
}

const SCAN_TOKENS = ['--color-scan-1', '--color-scan-2', '--color-scan-3', '--color-scan-4']

/** The custom property a unit's hue comes from, for elements the cascade can style. */
export function scanColorToken(unit: number): string {
  return SCAN_TOKENS[scanIndex(unit, SCAN_TOKENS.length)]
}

export function readMapPalette(element: Element | null): MapPalette {
  if (!element || typeof getComputedStyle !== 'function') return FALLBACK_PALETTE
  let styles: CSSStyleDeclaration
  try {
    styles = getComputedStyle(element)
  } catch {
    return FALLBACK_PALETTE
  }
  const read = (token: string, fallback: string) => styles.getPropertyValue(token).trim() || fallback
  return {
    ground: read(TOKENS.ground, FALLBACK_PALETTE.ground),
    frame: read(TOKENS.frame, FALLBACK_PALETTE.frame),
    ink: read(TOKENS.ink, FALLBACK_PALETTE.ink),
    muted: read(TOKENS.muted, FALLBACK_PALETTE.muted),
    geofence: read(TOKENS.geofence, FALLBACK_PALETTE.geofence),
    scans: SCAN_TOKENS.map((token, index) => read(token, FALLBACK_PALETTE.scans[index])),
  }
}

/** Units are 1-based and stable across reconnects, so a device keeps its hue. */
export function scanColor(palette: MapPalette, unit: number): string {
  return palette.scans[scanIndex(unit, palette.scans.length)]
}

function scanIndex(unit: number, length: number): number {
  return Number.isInteger(unit) && unit > 0 ? (unit - 1) % length : 0
}

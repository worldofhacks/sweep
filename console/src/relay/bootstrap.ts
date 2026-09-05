import { RELAY_BOOTSTRAP_ENDPOINT } from './bootstrap-endpoint'
import { createConsoleRuntime, type ConsoleRuntime, type SweepRelayRuntimeConfig } from './runtime'

/**
 * Resolves the relay bootstrap before the runtime exists. A host that set
 * `window.__SWEEP_RELAY_CONFIG__` is trusted exactly as before and nothing is
 * fetched; otherwise the same-origin endpoint is read once. Only then is the
 * runtime created, so the console mounts with its real clients or, without
 * either source, exactly as before: visibly disconnected, network controls
 * unavailable, and no retry.
 */
export async function bootstrapConsoleRuntime(
  loader: () => Promise<SweepRelayRuntimeConfig | undefined> = loadRelayBootstrap,
  hostConfig: SweepRelayRuntimeConfig | undefined = window.__SWEEP_RELAY_CONFIG__,
): Promise<ConsoleRuntime> {
  return createConsoleRuntime(hostConfig ?? (await loader()))
}

export async function loadRelayBootstrap(
  fetcher: typeof fetch = fetch,
  report: (message: string) => void = console.warn,
): Promise<SweepRelayRuntimeConfig | undefined> {
  try {
    const response = await fetcher(RELAY_BOOTSTRAP_ENDPOINT, {
      cache: 'no-store',
      credentials: 'same-origin',
    })
    if (!response.ok) return unavailable(report)
    const payload: unknown = await response.json()
    if (!isObject(payload)) return unavailable(report)
    return normalizeRelayBootstrap(payload.relay) ?? unavailable(report)
  } catch {
    return unavailable(report)
  }
}

/** Says only that the bootstrap is missing; the payload is never echoed. */
function unavailable(report: (message: string) => void): undefined {
  report('Relay bootstrap unavailable: network controls stay disconnected.')
}

/** Accepts only a complete bootstrap whose relay URL parses as ws or wss; nothing is defaulted. */
export function normalizeRelayBootstrap(value: unknown): SweepRelayRuntimeConfig | undefined {
  if (!isObject(value)) return undefined
  const baseUrl = nonemptyString(value.baseUrl)
  const sessionId = nonemptyString(value.sessionId)
  const token = nonemptyString(value.token)
  if (!baseUrl || !sessionId || !token || !isWebSocketUrl(baseUrl)) return undefined
  return { baseUrl, sessionId, token }
}

function isWebSocketUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'ws:' || url.protocol === 'wss:'
  } catch {
    return false
  }
}

function nonemptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

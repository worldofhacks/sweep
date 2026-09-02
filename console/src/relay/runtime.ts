import { UnavailableRelayClient, WebSocketRelayClient, type RelayClient } from './client'

export interface SweepRelayRuntimeConfig {
  baseUrl: string
  sessionId: string
  token: string
}

export interface ConsoleRuntime {
  client: RelayClient
  keyboardClient: RelayClient
  sessionId: string
}

declare global {
  interface Window {
    __SWEEP_RELAY_CONFIG__?: SweepRelayRuntimeConfig
  }
}

export function createConsoleRuntime(config = window.__SWEEP_RELAY_CONFIG__): ConsoleRuntime {
  if (!config?.baseUrl || !config.sessionId || !config.token) {
    return {
      sessionId: config?.sessionId || 'session-unconfigured',
      client: new UnavailableRelayClient(
        'Relay bootstrap is not configured. Network controls are unavailable; use the physical RC safety path.',
      ),
      keyboardClient: new UnavailableRelayClient(
        'Relay bootstrap is not configured. Keyboard network stop is unavailable; use the physical RC safety path.',
      ),
    }
  }

  return {
    sessionId: config.sessionId,
    client: new WebSocketRelayClient({
      baseUrl: config.baseUrl,
      sessionId: config.sessionId,
      source: 'console',
      token: config.token,
    }),
    keyboardClient: new WebSocketRelayClient({
      baseUrl: config.baseUrl,
      sessionId: config.sessionId,
      source: 'keyboard',
      token: config.token,
    }),
  }
}

import { HttpNavigationClient, type NavigationClient } from '../navigation/client'
import { HttpSearchClient, type SearchClient } from '../search/client'
import { UnavailableRelayClient, WebSocketRelayClient, type RelayClient } from './client'
import { HttpTranscriptClient, type TranscriptClient } from '../voice/client'

export interface SweepRelayRuntimeConfig {
  baseUrl: string
  sessionId: string
  token: string
}

export interface ConsoleRuntime {
  navigationClient: NavigationClient | null
  searchClient: SearchClient | null
  client: RelayClient
  keyboardClient: RelayClient
  webcamClient: RelayClient
  transcriptClient: TranscriptClient | null
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
      webcamClient: new UnavailableRelayClient(
        'Relay bootstrap is not configured. Webcam gesture source is unavailable; use the console controls and the physical RC safety path.',
      ),
      transcriptClient: null,
      navigationClient: null,
      searchClient: null,
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
    webcamClient: new WebSocketRelayClient({
      baseUrl: config.baseUrl,
      sessionId: config.sessionId,
      source: 'webcam',
      token: config.token,
    }),
    navigationClient: new HttpNavigationClient(config),
    searchClient: new HttpSearchClient(config),
    transcriptClient: new HttpTranscriptClient({ baseUrl: config.baseUrl, token: config.token }),
  }
}

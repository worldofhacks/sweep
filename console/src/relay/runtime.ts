import { HttpNavigationClient, type NavigationClient } from '../navigation/client'
import { HttpSearchClient, type SearchClient } from '../search/client'
import { UnavailableRelayClient, WebSocketRelayClient, type RelayClient } from './client'
import {
  relayMediaConfigurationSource,
  type MediaConfigurationSource,
} from '../media/runtime-config'
import { HttpTranscriptClient, type TranscriptClient } from '../voice/client'

export interface SweepRelayRuntimeConfig {
  baseUrl: string
  sessionId: string
  token: string
}

export interface ConsoleRuntime {
  client: RelayClient
  keyboardClient: RelayClient
  webcamClient: RelayClient
  /** Null when no relay bootstrap exists: the Speech module renders language disabled. */
  transcriptClient: TranscriptClient | null
  navigationClient: NavigationClient | null
  searchClient: SearchClient | null
  /**
   * The relay's copy of the media bootstrap, read after the same-origin endpoint so a built
   * console can play; null when no relay bootstrap exists.
   */
  mediaConfigurationSource: MediaConfigurationSource | null
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
      mediaConfigurationSource: null,
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
    transcriptClient: new HttpTranscriptClient({ baseUrl: config.baseUrl, token: config.token }),
    navigationClient: new HttpNavigationClient(config),
    searchClient: new HttpSearchClient(config),
    mediaConfigurationSource: relayMediaConfigurationSource(config.baseUrl, config.token),
  }
}

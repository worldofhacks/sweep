import type { IntentSource, IntentV1, RelayAuthFrame, RelayServerEvent } from './contract'
import { parseRelayServerEvent } from './contract'
import type { RelayConnection } from '../control/state'

export type RelayClientEvent =
  | { kind: 'connection'; connection: RelayConnection }
  | { kind: 'server_event'; event: RelayServerEvent }

export type RelayClientListener = (event: RelayClientEvent) => void

export interface RelayClient {
  readonly transport: RelayConnection['transport']
  start(): void
  stop(): void
  subscribe(listener: RelayClientListener): () => void
  sendIntent(intent: IntentV1): Promise<void>
}

export interface WebSocketRelayConfig {
  baseUrl: string
  sessionId: string
  source: IntentSource
  token: string
}

interface WebSocketDependencies {
  now: () => number
  createSocket: (url: string) => WebSocket
}

const browserDependencies: WebSocketDependencies = {
  now: () => Date.now(),
  createSocket: (url) => new WebSocket(url),
}

export class WebSocketRelayClient implements RelayClient {
  readonly transport = 'websocket' as const
  private readonly listeners = new Set<RelayClientListener>()
  private socket: WebSocket | null = null
  private authenticated = false
  private presenceTimer: ReturnType<typeof setInterval> | null = null
  private readonly config: WebSocketRelayConfig
  private readonly dependencies: WebSocketDependencies

  constructor(
    config: WebSocketRelayConfig,
    dependencies: WebSocketDependencies = browserDependencies,
  ) {
    this.config = config
    this.dependencies = dependencies
  }

  start(): void {
    if (this.socket) return
    this.emitConnection('connecting', 'Opening the authenticated relay session.')

    let socket: WebSocket
    try {
      socket = this.dependencies.createSocket(buildSessionWebSocketUrl(this.config))
    } catch {
      this.emitConnection('disconnected', 'Relay URL is invalid; no connection was opened.')
      return
    }
    this.socket = socket

    socket.addEventListener('open', () => {
      if (this.socket !== socket) return
      const authFrame: RelayAuthFrame = {
        v: 1,
        type: 'auth',
        source: this.config.source,
        token: this.config.token,
      }
      socket.send(JSON.stringify(authFrame))
      this.emitConnection('connecting', 'Relay socket opened; awaiting authenticated state.')
    })

    socket.addEventListener('message', (message) => {
      if (this.socket !== socket) return
      let raw: unknown
      try {
        raw = JSON.parse(String(message.data))
      } catch {
        this.emitConnection('degraded', 'Relay sent a non-JSON frame; it was ignored.')
        return
      }

      const event = parseRelayServerEvent(raw)
      if (!event) {
        this.emitConnection('degraded', 'Relay event did not match the console contract; it was ignored.')
        return
      }

      if (event.type === 'auth.accepted') {
        if (event.session !== this.config.sessionId) {
          this.emitConnection('degraded', 'Relay authenticated a different session; the event was ignored.')
          return
        }
        if (event.source !== this.config.source) {
          this.emitConnection('degraded', 'Relay authenticated a different input source; the event was ignored.')
          return
        }
        if (event.drone_id !== null) {
          this.emitConnection('degraded', 'Relay returned adapter identity on an operator connection; the event was ignored.')
          return
        }
        this.authenticated = true
        if (this.config.source === 'console' && this.presenceTimer === null) {
          this.presenceTimer = setInterval(() => {
            if (this.socket !== socket || !this.authenticated || socket.readyState !== 1) return
            if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
            socket.send(JSON.stringify({ v: 1, type: 'operator_presence' }))
          }, 1_000)
        }
        this.emitConnection('connected', 'Relay authenticated this console source.')
      } else if (!this.authenticated) {
        if (event.type === 'auth.refused') {
          this.emit({ kind: 'server_event', event })
          this.emitConnection('disconnected', `Relay refused authentication: ${event.reason}.`)
        } else {
          this.emitConnection('degraded', 'Relay data arrived before authentication and was ignored.')
        }
        return
      }
      this.emit({ kind: 'server_event', event })
    })

    socket.addEventListener('error', () => {
      if (this.socket !== socket) return
      this.emitConnection('degraded', 'Relay socket reported a connection error.')
    })

    socket.addEventListener('close', (event) => {
      if (this.socket !== socket) return
      this.socket = null
      this.authenticated = false
      this.stopPresenceTimer()
      this.emitConnection('disconnected', `Relay socket closed (code ${event.code}). No retry was attempted.`)
    })
  }

  stop(): void {
    const socket = this.socket
    this.socket = null
    this.authenticated = false
    this.stopPresenceTimer()
    socket?.close(1000, 'console_unmounted')
  }

  subscribe(listener: RelayClientListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async sendIntent(intent: IntentV1): Promise<void> {
    if (!this.socket || this.socket.readyState !== 1 || !this.authenticated) {
      throw new Error('Relay is not authenticated; the intent was not sent.')
    }
    this.socket.send(JSON.stringify(intent))
  }

  private stopPresenceTimer(): void {
    if (this.presenceTimer !== null) clearInterval(this.presenceTimer)
    this.presenceTimer = null
  }

  private emitConnection(status: RelayConnection['status'], reason?: string): void {
    this.emit({
      kind: 'connection',
      connection: {
        status,
        transport: this.transport,
        changedAt: this.dependencies.now(),
        reason,
      },
    })
  }

  private emit(event: RelayClientEvent): void {
    this.listeners.forEach((listener) => listener(event))
  }
}

export class UnavailableRelayClient implements RelayClient {
  readonly transport = 'unavailable' as const
  private readonly listeners = new Set<RelayClientListener>()
  private readonly reason: string
  private readonly now: () => number

  constructor(reason: string, now: () => number = () => Date.now()) {
    this.reason = reason
    this.now = now
  }

  start(): void {
    this.emit({
      kind: 'connection',
      connection: {
        status: 'disconnected',
        transport: this.transport,
        changedAt: this.now(),
        reason: this.reason,
      },
    })
  }

  stop(): void {}

  subscribe(listener: RelayClientListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async sendIntent(): Promise<void> {
    throw new Error(this.reason)
  }

  private emit(event: RelayClientEvent): void {
    this.listeners.forEach((listener) => listener(event))
  }
}

export function buildSessionWebSocketUrl(config: Pick<WebSocketRelayConfig, 'baseUrl' | 'sessionId'>): string {
  const url = new URL(config.baseUrl)
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') {
    throw new Error('Relay URL must use ws or wss.')
  }
  url.username = ''
  url.password = ''
  url.search = ''
  url.hash = ''
  const basePath = url.pathname.replace(/\/$/, '')
  url.pathname = `${basePath}/ws/${encodeURIComponent(config.sessionId)}`
  return url.toString()
}

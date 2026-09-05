import type { BundleRef, CatalogSnapshot } from './types'

export type CatalogListener = (snapshot: CatalogSnapshot) => void

/**
 * The catalog seam. Production has no implementation because the relay
 * exposes no capture, world, node, or configuration endpoint yet; the
 * development fixture implements it as scenario data.
 */
export interface CatalogClient {
  subscribe(listener: CatalogListener): () => void
  start(): void
  stop(): void
  submitGeneration(roomId: string, bundle: BundleRef): Promise<void>
  retryGeneration(roomId: string): Promise<void>
  /** Stages a capture's file set for download; resolves with the outcome sentence. */
  stageCaptureSet(captureId: string): Promise<string>
  exportCaptureMetadata(captureId: string): Promise<string>
  applyConfig(groupId: string, values: Record<string, string>): Promise<void>
  stageConfig(groupId: string, values: Record<string, string>): Promise<void>
}

export const UNREPORTED_CATALOG: CatalogSnapshot = {
  captures: null,
  building: null,
  jobs: null,
  nodes: null,
  services: null,
  metrics: null,
  config: null,
}

export const CATALOG_UNREPORTED_REASON =
  'The relay reports no catalog endpoint on this console; nothing was sent.'

/** Production: every catalog surface is unreported and every action refuses. */
export class UnreportedCatalogClient implements CatalogClient {
  private readonly listeners = new Set<CatalogListener>()

  subscribe(listener: CatalogListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  start(): void {
    this.listeners.forEach((listener) => listener(UNREPORTED_CATALOG))
  }

  stop(): void {}

  async submitGeneration(): Promise<void> {
    throw new Error(CATALOG_UNREPORTED_REASON)
  }

  async retryGeneration(): Promise<void> {
    throw new Error(CATALOG_UNREPORTED_REASON)
  }

  async stageCaptureSet(): Promise<string> {
    throw new Error(CATALOG_UNREPORTED_REASON)
  }

  async exportCaptureMetadata(): Promise<string> {
    throw new Error(CATALOG_UNREPORTED_REASON)
  }

  async applyConfig(): Promise<void> {
    throw new Error(CATALOG_UNREPORTED_REASON)
  }

  async stageConfig(): Promise<void> {
    throw new Error(CATALOG_UNREPORTED_REASON)
  }
}

import type { NavigationPreview, NavigationPreviewRequest, NavigationSnapshot } from './types'

export const NAVIGATION_CONFIRMATION_UNAVAILABLE = 'Navigation execution unavailable: this relay has no qualified route-execution capability.'
export const NAVIGATION_UNAVAILABLE = 'Navigation is unavailable: no accepted-map catalog or frozen-preview backend is connected.'

/** Catalog and frozen-review service, independent of motion capability release. */
export interface NavigationClient {
  /** Stable immutable snapshot until notification; deadlines use the console clock. */
  getSnapshot(): NavigationSnapshot
  subscribe(listener: (snapshot: NavigationSnapshot) => void): () => void
  requestPreview(request: NavigationPreviewRequest): Promise<NavigationPreview>
  confirmPreview?(preview: NavigationPreview): Promise<unknown>
}

export class UnavailableNavigationClient implements NavigationClient {
  private readonly snapshot: NavigationSnapshot

  constructor(reason = NAVIGATION_UNAVAILABLE) {
    this.snapshot = Object.freeze({ status: 'unavailable', reason, catalog: null, preview: null })
  }

  getSnapshot(): NavigationSnapshot { return this.snapshot }

  subscribe(listener: (snapshot: NavigationSnapshot) => void): () => void {
    listener(this.snapshot)
    return () => {}
  }

  async requestPreview(): Promise<NavigationPreview> {
    throw new Error(this.snapshot.reason ?? NAVIGATION_UNAVAILABLE)
  }
}

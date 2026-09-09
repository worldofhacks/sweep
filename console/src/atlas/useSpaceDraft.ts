import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { EMPTY_SPACE, type SpaceDraft, type SpaceDraftStore } from './drafts'
import type { NewSpace } from './types'

interface Snapshot { loaded: boolean; draft: SpaceDraft | null; saving: boolean; error: string }
/** Keeps a write alive across page navigation and coalesces edits without reordering them. */
export class SpaceDraftSession {
  private snapshot: Snapshot = { loaded: false, draft: null, saving: false, error: '' }
  private durable: SpaceDraft | null = null
  private running: Promise<void> | null = null
  private loading: Promise<void> | null = null
  private listeners = new Set<() => void>()
  private readonly store: SpaceDraftStore | null
  constructor(store: SpaceDraftStore | null) { this.store = store }
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private set(change: Partial<Snapshot>) { this.snapshot = { ...this.snapshot, ...change }; this.listeners.forEach(fn => fn()) }
  load(): Promise<void> {
    if (this.snapshot.loaded) return Promise.resolve()
    if (this.loading) return this.loading
    this.loading = this.read().finally(() => { this.loading = null })
    return this.loading
  }
  private async read() {
    try {
      this.durable = await this.store?.read() ?? null
      this.set({ loaded: true, draft: this.durable })
    } catch (error) { this.set({ error: error instanceof Error ? error.message : 'Local storage is unavailable.' }) }
  }
  start() {
    if (!this.snapshot.loaded || !this.store) throw new Error('Wait for the saved draft to load first.')
    if (this.snapshot.draft) return
    this.set({ draft: { version: 1, id: crypto.randomUUID(), revision: 1, updatedAt: Date.now(),
      space: { ...EMPTY_SPACE }, coordinates: ['', ''], submitted: null } })
    void this.flush().catch(() => {})
  }
  edit(space: NewSpace, coordinates?: [string, string]) {
    const draft = this.snapshot.draft
    if (!draft || draft.submitted) return
    this.set({ draft: { ...draft, space, coordinates: coordinates ?? draft.coordinates, updatedAt: Date.now() } })
    void this.flush().catch(() => {})
  }
  flush(): Promise<void> {
    if (this.running) return this.running
    this.running = this.persist().finally(() => { this.running = null })
    return this.running
  }
  private async persist() {
    if (!this.store || !this.snapshot.draft || this.snapshot.draft === this.durable) return
    this.set({ saving: true, error: '' })
    try {
      while (this.snapshot.draft && this.snapshot.draft !== this.durable) {
        const desired: SpaceDraft = this.snapshot.draft
        const next = { ...desired, revision: (this.durable?.revision ?? 0) + 1 }
        await this.store.write(next, this.durable)
        this.durable = next
        if (this.snapshot.draft === desired) this.set({ draft: next })
      }
    } catch (error) {
      this.set({ error: error instanceof Error ? error.message : 'This draft could not be saved.' })
      throw error
    } finally { this.set({ saving: false }) }
  }
  async submission() {
    await this.flush()
    const draft = this.snapshot.draft
    if (!draft) throw new Error('Open your draft before publishing.')
    if (!draft.submitted) {
      this.set({ draft: { ...draft, submitted: { ...draft.space }, updatedAt: Date.now() } })
      await this.flush()
    }
    return this.snapshot.draft!
  }
  async discard() {
    // Finish existing writes, but do not require saving unsaved text just to discard it.
    await this.running?.catch(() => {})
    if (this.durable && this.store) await this.store.remove(this.durable)
    this.durable = null
    this.set({ draft: null, error: '' })
  }
}

export function useSpaceDraft(store: SpaceDraftStore | null) {
  const session = useMemo(() => new SpaceDraftSession(store), [store])
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot)
  useEffect(() => { void session.load() }, [session])
  return { ...state, session }
}

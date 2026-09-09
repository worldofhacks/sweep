import { useEffect, useState } from 'react'
import type { AtlasClient } from '../atlas/client'
import type { MemoryAnalysisRequest } from './types'
import { RECOVERY_CHANGED } from './analysisRecovery'

export function useAnalysisRecovery(client: AtlasClient, space: string, capture: string, enabled: boolean) {
  const [state, setState] = useState<{
    client: AtlasClient; space: string; capture: string;
    request: MemoryAnalysisRequest | null; error: string
  } | null>(null)
  useEffect(() => {
    if (!enabled) return
    let active = true, sequence = 0
    const refresh = async () => {
      const attempt = ++sequence
      try {
        const request = await client.analysisRecovery.read(space, capture)
        if (active && attempt === sequence) setState({ client, space, capture, request, error: '' })
      } catch {
        if (active && attempt === sequence) setState({ client, space, capture, request: null,
          error: 'Analysis recovery is unavailable on this browser. No new analysis will be sent. Your saved memory is still available.' })
      }
    }
    const changed = (event: Event) => {
      if (event instanceof StorageEvent && event.key && !event.key.startsWith('sweep.atlas.analysis.v1.')) return
      void refresh()
    }
    void refresh()
    window.addEventListener('storage', changed)
    window.addEventListener(RECOVERY_CHANGED, changed)
    return () => { active = false; window.removeEventListener('storage', changed); window.removeEventListener(RECOVERY_CHANGED, changed) }
  }, [client, space, capture, enabled])
  const current = state?.client === client && state.space === space && state.capture === capture ? state : null
  return {
    pending: enabled ? current?.request ?? null : null,
    ready: !enabled || !!current && !current.error,
    error: enabled ? current?.error ?? '' : '',
    retry: () => window.dispatchEvent(new Event(RECOVERY_CHANGED)),
    setPending: (request: MemoryAnalysisRequest | null) => setState({ client, space, capture, request, error: '' }),
  }
}

import { useEffect, useState } from 'react'
import type { SearchClient, SearchStatus } from '../../search/client'
import { SearchStatusView } from '../search/SearchModule'

/** The voice workspace follows the same mission and findings as Visual search. */
export function VoiceSearchMission({ client, session, intentId }: {
  client: SearchClient; session: string; intentId: string
}) {
  const [status, setStatus] = useState<SearchStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [acknowledging, setAcknowledging] = useState<string | null>(null)
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const next = await client.status(session, intentId)
        if (active) { setStatus(next); setError(null) }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : 'Mission status unavailable.')
      } finally {
        if (active) timer = setTimeout(() => { void load() }, 2000)
      }
    }
    void load()
    return () => { active = false; clearTimeout(timer) }
  }, [client, session, intentId])
  const acknowledge = async (id: string) => {
    setAcknowledging(id)
    try { setStatus(await client.acknowledge(session, intentId, id)); setError(null) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not acknowledge finding.') }
    finally { setAcknowledging(null) }
  }
  return <section aria-label="Voice mission progress">
    {error && <p role="alert">{error}</p>}
    {status && <SearchStatusView status={status} acknowledging={acknowledging} onAcknowledge={(id) => { void acknowledge(id) }} />}
  </section>
}

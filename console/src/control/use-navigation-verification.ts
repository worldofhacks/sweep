import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import {
  equalNavigationEvidence, navigationPreviewValidity, parseNavigationConfirmation,
  type NavigationClient, type NavigationConfirmationOutcome, type NavigationPreview,
  type NavigationSnapshot, type NavigationVerification,
} from '../navigation'
import { navigationBlockedReason, navigationTargets } from './navigation'
import { observedControlState } from './observation'
import type { ControlState } from './state'

const IDLE: NavigationVerification = Object.freeze({ status: 'idle', reason: null, outcome: null })
const CHANGED: NavigationVerification = Object.freeze({ status: 'invalidated',
  reason: 'The frozen review changed. Request a new destination review before verifying it.', outcome: null })

type Attempt = {
  token: number
  client: NavigationClient
  context: string
  generation: number
  view: NavigationVerification
}

/** One-shot server evidence checks, kept separate from all motion-send paths. */
export function useNavigationVerification({ state, snapshot, client, now, generationRef, onOutcome }: {
  state: ControlState
  snapshot: NavigationSnapshot
  client?: NavigationClient
  now: () => number
  generationRef: RefObject<number>
  onOutcome: (outcome: NavigationConfirmationOutcome) => void
}) {
  const current = observedControlState(state, now())
  const blocked = navigationBlockedReason(current, snapshot.reviewSupported === true)
  const context = JSON.stringify({ session: current.sessionId, roster: current.rosterVersion,
    selected: navigationTargets(current), catalog: snapshot.catalog, preview: snapshot.preview,
    status: snapshot.status, blocked, armed: current.armed, estop: current.estop,
    capabilityProfile: current.capabilityProfile, enabled: current.enabledIntentNames })
  const latest = useRef({ state, snapshot, client, context, now, onOutcome })
  useLayoutEffect(() => { latest.current = { state, snapshot, client, context, now, onOutcome } },
    [state, snapshot, client, context, now, onOutcome])
  const mounted = useRef(true)
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const sequence = useRef(0)
  const attempted = useRef(new WeakMap<NavigationClient, Map<string, number>>())
  const [attempt, setAttempt] = useState<Attempt | null>(null)
  const [spent, setSpent] = useState<readonly { client: NavigationClient; previewId: string; expiresAt: number }[]>([])

  function currentPreview(preview: NavigationPreview | null, provider: NavigationClient | undefined): boolean {
    const live = latest.current
    if (!preview || !provider?.confirmPreview || provider !== live.client || live.snapshot.status !== 'ready') return false
    const at = live.now()
    const fresh = observedControlState(live.state, at)
    if (navigationBlockedReason(fresh, live.snapshot.reviewSupported === true)) return false
    const source = provider.getSnapshot()
    if (source.status !== 'ready' || !equalNavigationEvidence(source.catalog, live.snapshot.catalog)
      || !equalNavigationEvidence(source.preview, preview)) return false
    const validity = navigationPreviewValidity(preview, live.snapshot.catalog, { session: fresh.sessionId,
      rosterVersion: fresh.rosterVersion, selected: navigationTargets(fresh), destinationZoneId: preview.destination.zoneId,
      intentId: preview.intentId, frozenPreview: live.snapshot.preview ?? undefined, now: at,
      reviewOnly: live.snapshot.reviewSupported === true })
    return validity.valid || validity.code === 'node_refused'
  }

  const verify = useCallback(async (): Promise<NavigationConfirmationOutcome | null> => {
    const live = latest.current
    const preview = live.snapshot.preview, provider = live.client
    if (!preview || !provider?.confirmPreview || !currentPreview(preview, provider)) return null
    const used = attempted.current.get(provider) ?? new Map<string, number>()
    for (const [id, expiresAt] of used) if (expiresAt <= live.now()) used.delete(id)
    if (used.has(preview.previewId)) return null
    // The server retains at most 256 outstanding previews; keep the same local bound.
    if (used.size >= 256) return null
    used.set(preview.previewId, preview.expiresAt)
    attempted.current.set(provider, used)
    setSpent((previous) => [...previous.filter((entry) => entry.expiresAt > live.now()).slice(-255),
      { client: provider, previewId: preview.previewId, expiresAt: preview.expiresAt }])
    const captured: Attempt = { token: ++sequence.current, client: provider, context: live.context,
      generation: generationRef.current, view: { status: 'verifying', reason: null, outcome: null } }
    setAttempt(captured)
    const owns = () => mounted.current && sequence.current === captured.token
      && generationRef.current === captured.generation && latest.current.context === captured.context
      && latest.current.client === provider && currentPreview(preview, provider)
    try {
      const raw = await provider.confirmPreview(preview)
      if (!owns()) {
        if (mounted.current && sequence.current === captured.token) setAttempt({ ...captured, view: CHANGED })
        return null
      }
      const outcome = parseNavigationConfirmation(raw)
      if (!outcome || outcome.previewId !== preview.previewId || outcome.intentId !== preview.intentId) {
        throw new Error('The relay returned verification evidence for another frozen review.')
      }
      setAttempt({ ...captured, view: { status: 'complete', reason: null, outcome } })
      latest.current.onOutcome(outcome)
      return outcome
    } catch (error) {
      if (mounted.current && sequence.current === captured.token) setAttempt({ ...captured, view: owns()
        ? { status: 'error', reason: `${error instanceof Error ? error.message : 'The relay could not verify this review.'} Request a new destination review before trying again.`, outcome: null }
        : CHANGED })
      return null
    }
  }, [generationRef])

  const verification = attempt && (attempt.client !== client || attempt.context !== context)
    ? CHANGED : attempt?.view ?? IDLE
  const preview = snapshot.preview
  const available = client?.confirmPreview !== undefined && snapshot.status === 'ready' && blocked === null
    && preview !== null && preview.expiresAt > now()
  return { verification, verify, canVerify: available && Boolean(client && preview
    && !spent.some((entry) => entry.client === client && entry.previewId === preview.previewId)) }
}

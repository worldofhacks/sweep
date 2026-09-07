import { useCallback, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type RefObject } from 'react'
import {
  UnavailableNavigationClient, navigationCatalogValidity, navigationPreviewValidity,
  parseNavigationCatalog, parseNavigationPreview, resolveNavigationZoneId,
  type NavigationClient, type NavigationPreview, type NavigationSnapshot,
} from '../navigation'
import { createIntent, type IntentFactoryDependencies } from './intent'
import { navigationBlockedReason, navigationTargets } from './navigation'
import { observedControlState } from './observation'
import type { ControlState } from './state'
import type { IntentV1 } from '../relay/contract'

const UNAVAILABLE = new UnavailableNavigationClient()
type Review = { client: NavigationClient; contextKey: string; snapshot: NavigationSnapshot }

/** Review evidence only. This port cannot execute, take off, capture, or survey. */
export function useNavigationReview({ state, client = UNAVAILABLE, dependencies, generationRef, reset, onPreview }: {
  state: ControlState
  client?: NavigationClient
  dependencies: IntentFactoryDependencies
  generationRef: RefObject<number>
  reset: number
  onPreview: (intent: IntentV1, preview: NavigationPreview) => void
}) {
  const subscribe = useCallback((notify: () => void) => client.subscribe(() => notify()), [client])
  const getSnapshot = useCallback(() => client.getSnapshot(), [client])
  const source = useSyncExternalStore(subscribe, getSnapshot)
  const catalog = useMemo(() => parseNavigationCatalog(source.catalog), [source.catalog])
  const current = observedControlState(state, dependencies.now())
  const catalogValidity = navigationCatalogValidity(catalog, current.sessionId, dependencies.now())
  const blocked = navigationBlockedReason(current, source.reviewSupported === true)
  // Includes all bounded catalog evidence, not just version labels. A provider
  // must not silently replace geometry/configuration under an unchanged label.
  const contextKey = JSON.stringify({ reset, session: current.sessionId, roster: current.rosterVersion,
    selected: navigationTargets(current), catalog, blocked, catalogValid: catalogValidity.valid,
    status: source.status, armed: current.armed, estop: current.estop })
  const [review, setReview] = useState<Review | null>(null)
  const latest = useRef({ state, contextKey, onPreview, sourcePreview: source.preview })
  useLayoutEffect(() => { latest.current = { state, contextKey, onPreview, sourcePreview: source.preview } }, [state, contextKey, onPreview, source.preview])
  useLayoutEffect(() => {
    generationRef.current += 1
    return () => { generationRef.current += 1 }
  }, [client, contextKey, generationRef])

  const invalidate = useCallback(() => {
    generationRef.current += 1
    setReview(null)
  }, [generationRef, setReview])

  const prepare = useCallback(async (zoneId: string): Promise<IntentV1 | null> => {
    const requestNumber = ++generationRef.current
    const at = dependencies.now()
    const before = observedControlState(latest.current.state, at)
    if (source.status !== 'ready' || !navigationCatalogValidity(catalog, before.sessionId, at).valid ||
      !catalog || navigationBlockedReason(before, source.reviewSupported === true)) return null
    const targets = navigationTargets(before)
    const destination = resolveNavigationZoneId(catalog, zoneId, at, targets.map((target) => target.deviceClass), source.reviewSupported === true)
    if (destination.kind !== 'resolved' || destination.destination.zoneId !== zoneId) return null
    const intent = createIntent({ name: 'navigate', args: { zone_id: zoneId }, selection: before.selection,
      session: before.sessionId, source: 'console' }, dependencies)
    const ownsResult = () => generationRef.current === requestNumber && latest.current.contextKey === contextKey
    setReview({ client, contextKey, snapshot: { status: 'loading', reason: 'Requesting an authoritative destination review…', catalog, preview: null } })
    try {
      const result = await client.requestPreview({ session: before.sessionId, intentId: intent.intent_id,
        zoneId, rosterVersion: before.rosterVersion, selected: targets, catalogVersion: catalog.catalogVersion,
        map: catalog.map, configVersion: catalog.configVersion, motionConfig: catalog.motionConfig })
      if (!ownsResult()) return null
      const preview = parseNavigationPreview(result)
      const after = observedControlState(latest.current.state, dependencies.now())
      const validity = navigationPreviewValidity(preview, catalog, { session: after.sessionId,
        rosterVersion: after.rosterVersion, selected: navigationTargets(after), destinationZoneId: zoneId,
        intentId: intent.intent_id, now: dependencies.now(), reviewOnly: source.reviewSupported === true })
      const eligibility = navigationBlockedReason(after, source.reviewSupported === true)
      if (!preview || (!validity.valid && validity.code !== 'node_refused') || eligibility) {
        throw new Error(eligibility ?? validity.reason)
      }
      if (latest.current.sourcePreview !== null) {
        const published = navigationPreviewValidity(parseNavigationPreview(latest.current.sourcePreview), catalog, {
          session: after.sessionId, rosterVersion: after.rosterVersion, selected: navigationTargets(after),
          destinationZoneId: zoneId, intentId: intent.intent_id, now: dependencies.now(), frozenPreview: preview, reviewOnly: source.reviewSupported === true,
        })
        if (!published.valid && published.code !== 'node_refused') throw new Error(published.reason)
      }
      setReview({ client, contextKey, snapshot: { status: 'ready', reason: null, catalog, preview } })
      latest.current.onPreview(intent, preview)
      return intent
    } catch (error) {
      if (ownsResult()) setReview({ client, contextKey, snapshot: { status: 'ready',
        reason: error instanceof Error ? error.message : 'The destination review failed.', catalog, preview: null } })
      return null
    }
  }, [catalog, client, contextKey, dependencies, generationRef, source.status, source.reviewSupported])

  // Retire a review permanently when its context changes; restoring old
  // values must never resurrect a cancelled or invalidated route.
  if (review && (review.client !== client || review.contextKey !== contextKey)) setReview(null)
  const active = review?.client === client && review.contextKey === contextKey ? review.snapshot : null
  const publishedValidity = active?.preview && source.preview !== null
    ? navigationPreviewValidity(parseNavigationPreview(source.preview), catalog, { session: current.sessionId,
      rosterVersion: current.rosterVersion, selected: navigationTargets(current),
      destinationZoneId: active.preview.destination.zoneId, intentId: active.preview.intentId,
      now: dependencies.now(), frozenPreview: active.preview, reviewOnly: source.reviewSupported === true }) : null
  if (active?.preview && publishedValidity && !publishedValidity.valid && publishedValidity.code !== 'node_refused') {
    setReview({ client, contextKey, snapshot: { status: 'ready', reason: publishedValidity.reason, catalog, preview: null } })
  }
  const snapshot: NavigationSnapshot = publishedValidity && !publishedValidity.valid && publishedValidity.code !== 'node_refused'
    ? { status: 'ready', reason: publishedValidity.reason, catalog, preview: null } : active ?? {
    status: source.status === 'ready' && !catalogValidity.valid ? 'error' : source.status,
    reason: source.status === 'ready' && !catalogValidity.valid ? catalogValidity.reason : source.reason,
    catalog, preview: null,
  }
  return { snapshot: { ...snapshot, reviewSupported: source.reviewSupported === true }, prepare, invalidate }
}

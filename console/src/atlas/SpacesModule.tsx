import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react'
import type { ModuleProps } from '../modules/types'
import type { PlatformConnection } from '../platform/http'
import { AtlasClient, locate, phonePosition } from './client'
import { CaptureComposer } from './CaptureComposer'
import { Icon } from './Icon'
import { ReconstructionPanel } from './ReconstructionPanel'
import { SurfaceReviewPanel } from './SurfaceReviewPanel'
import { CaptureRequestsPanel } from './CaptureRequestsPanel'
import { isActionableRequest, isCurrentSurfaceRequest, linkedCaptureIds, spaceCaptureRequests } from './captureRequests'
import { EMPTY_SPACE } from './drafts'
import { useSpaceDraft } from './useSpaceDraft'
import { AtlasDialog } from './AtlasDialog'
import { CommunityGuide, ExampleArt, ExampleList, JourneyPanel } from '../community/CommunityPanels'
import { contributingNeighbors, journeyScore } from '../community/journey'
import { useJourney } from '../community/useJourney'
import { COMMUNITY_EXAMPLES, type CommunityExample } from '../community/examples'
import '../community/community.css'
import { useAccountSession } from '../community/accountSession'
import { pendingInvitation } from '../community/accountClient'
export { AtlasDialog } from './AtlasDialog'
import {
  CATEGORY_LABEL,
  type Capture,
  type CaptureRequestContext,
  type GeoPosition,
  type NewSpace,
  type Space,
  type SpaceCategory,
  type SpaceDetail,
  type SurfaceFocus,
  type SpaceRequest,
  type SurfaceRequest,
} from './types'
import './atlas.css'

const SpaceMap = lazy(() => import('./SpaceMap'))
const WorldViewer = lazy(() => import('./WorldViewer'))
const MemoryDialog = lazy(() => import('../memory/MemoryDialog'))
const AccountSpaces = lazy(() => import('../community/AccountSpaces'))
const SpaceSharing = lazy(() => import('../community/SpaceSharing'))
const SpaceTimeline = lazy(() => import('./SpaceTimeline'))
const SpaceRemovals = lazy(() => import('../memory/SpaceRemovals'))
const DEFAULT_CENTER: [number, number] = [-97.7431, 30.2672]
const EXAMPLE_MAP_SPACES: Space[] = COMMUNITY_EXAMPLES.map(example => ({ ...example.space, id: example.id,
  title: `Example · ${example.space.title}`, created_at: 0, updated_at: 0, status: 'active', verification: 'unverified',
  capture_count: 0, coverage_percent: 0, contributors: 0 }))
type Filter = 'all' | 'incident' | 'needs-captures' | 'resolved' | 'saved' | 'examples'
type DetailTab = 'overview' | 'captures' | 'timeline' | 'coverage' | 'requests' | 'world'
function errorText(error: unknown) {
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.'
}
function age(timestamp: number) {
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000))
  return minutes < 1
    ? 'Just now'
    : minutes < 60
      ? `${minutes}m ago`
      : minutes < 1440
        ? `${Math.floor(minutes / 60)}h ago`
        : `${Math.floor(minutes / 1440)}d ago`
}
function identity() {
  try {
    const value = localStorage.getItem('sweep.atlas.contributor') || crypto.randomUUID()
    localStorage.setItem('sweep.atlas.contributor', value)
    return value
  } catch {
    return crypto.randomUUID()
  }
}

export interface NativeCaptureRequest { spaceId: string; title: string; contributor: string; name: string; request?: CaptureRequestContext }
interface SpacesProps extends Pick<ModuleProps, 'services'> {
  initialSpace?: string | null
  captureNative?: (value: NativeCaptureRequest) => Promise<void>
  connectNative?: () => void
  accountRole?: 'viewer' | 'contributor' | 'owner'
  accountKey?: string
  onExitAccount?: () => void
  onOpenAccountSpaces?: () => void
}
export function SpacesModule(props: SpacesProps) {
  const session = useAccountSession()
  const [pending, setPending] = useState(() => props.captureNative || props.connectNative ? '' : pendingInvitation())
  const [accountOpen, setAccountOpen] = useState(Boolean(pending))
  if (accountOpen) return <Suspense fallback={<p role="status">Opening your shared spaces…</p>}>
    <AccountSpaces session={session} pending={pending} onHandled={() => setPending('')} onClose={() => { setPending(''); setAccountOpen(false) }} />
  </Suspense>
  return <WorkspaceSpaces {...props} onOpenAccountSpaces={props.captureNative || props.connectNative ? undefined : () => { setPending(pendingInvitation()); setAccountOpen(true) }} />
}

export function WorkspaceSpaces({ services, initialSpace, captureNative, connectNative, accountRole, accountKey, onExitAccount, onOpenAccountSpaces }: SpacesProps) {
  const canContribute = accountRole !== 'viewer'
  const [manualClient, setManualClient] = useState<AtlasClient | null>(null)
  const [invitedSpace, setInvitedSpace] = useState<string | null>(initialSpace ?? null)
  const client = manualClient ?? services.atlas ?? null
  const [spaces, setSpaces] = useState<Space[]>([])
  const [selected, setSelected] = useState<string | null>(initialSpace ?? null)
  const [detail, setDetail] = useState<SpaceDetail | null>(null)
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(Boolean(client))
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState<DetailTab>('overview')
  const [timelineIds, setTimelineIds] = useState<string[]>([])
  const [creating, setCreating] = useState(false)
  const localDraft = useSpaceDraft(invitedSpace ? null : client?.drafts ?? null)
  const draft = localDraft.draft?.space ?? EMPTY_SPACE
  const coordinateInput = localDraft.draft?.coordinates ?? ['', '']
  const submitted = localDraft.draft?.submitted ?? null
  const setDraft = (value: NewSpace | ((previous: NewSpace) => NewSpace), coordinates?: [string, string]) => {
    const current = localDraft.session.getSnapshot().draft?.space ?? EMPTY_SPACE
    localDraft.session.edit(typeof value === 'function' ? value(current) : value, coordinates)
  }
  const hasCoordinate = coordinateInput.every(value => value.trim() !== '' && Number.isFinite(Number(value))) &&
    Math.abs(Number(coordinateInput[0])) <= 85 && Math.abs(Number(coordinateInput[1])) <= 180
  const [position, setPosition] = useState<GeoPosition | null>(null)
  const [center, setCenter] = useState<[number, number]>(DEFAULT_CENTER)
  const [selectedCell, setSelectedCell] = useState<string | null>(null)
  const [surfaceFocus, setSurfaceFocus] = useState<SurfaceFocus | null>(null)
  const [surfaceRequest, setSurfaceRequest] = useState<SurfaceRequest | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [modal, setModal] = useState<'capture' | 'connect' | 'share' | 'discard' | 'guide' | 'journey' | 'example' | null>(null)
  const [example, setExample] = useState<CommunityExample | null>(null)
  const [captureRequest, setCaptureRequest] = useState<CaptureRequestContext | undefined>()
  const [captureFilter, setCaptureFilter] = useState<CaptureRequestContext | null>(null)
  const [captureLimit, setCaptureLimit] = useState(24)
  const [inviteTokens, setInviteTokens] = useState<Record<string, string>>({})
  const [contributor] = useState(identity)
  const { journey, error: journeyError, toggleSaved, remember } = useJourney(
    client ? `${client.connection.baseUrl}/${client.connection.sessionId}${accountKey ? `/account/${accountKey}` : ''}` : 'disconnected', contributor)
  const score = journeyScore(journey)
  const [name, setName] = useState('Contributor')
  const [sharing, setSharing] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [removalsOpen, setRemovalsOpen] = useState(false)
  const [selectedMemory, setSelectedMemory] = useState<Capture | null>(null)
  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])
  const memoryRemoved = useCallback(() => {
    // Drop all local projections, including the map and derived world, immediately.
    setDetail(null); setTimelineIds([]); setSurfaceFocus(null); setSurfaceRequest(null)
    setSelectedMemory(null)
    setSelectedCell(null); setCaptureFilter(null); setTab('captures'); setRemovalsOpen(true)
    refresh()
  }, [refresh])
  useEffect(() => {
    const stop = () => setSharing(false)
    window.addEventListener('atlas-background', stop)
    return () => window.removeEventListener('atlas-background', stop)
  }, [])
  useEffect(() => {
    const back = () => {
      if (modal) setModal(null)
      else if (creating) void localDraft.session.flush().then(() => setCreating(false)).catch(error => setNotice(errorText(error)))
      else if (selected && !invitedSpace) { setSelected(null); setSharing(false) }
      else window.dispatchEvent(new Event('atlas-exit'))
    }
    window.addEventListener('atlas-back', back)
    return () => window.removeEventListener('atlas-back', back)
  }, [modal, creating, selected, invitedSpace, localDraft.session])
  useEffect(() => {
    if (!localDraft.draft || (!localDraft.saving && !localDraft.error)) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [localDraft.draft, localDraft.saving, localDraft.error])
  const openConnect = () => { if (connectNative) connectNative(); else setModal('connect') }
  const startDraft = () => {
    if (!client) { openConnect(); return }
    try {
      localDraft.session.start()
      setCreating(true); setSelected(null); setSharing(false); setNotice('')
    } catch (error) { setNotice(errorText(error)) }
  }
  const openCapture = (request?: CaptureRequestContext) => {
    if (!canContribute) { setNotice('Your account has view-only access to this space.'); return }
    setNotice('')
    // Freeze the selected request before handing control to a camera or file picker.
    const snapshot = request ? structuredClone(request) : undefined
    if (captureNative && selected) {
      void captureNative({ spaceId: selected, title: detail?.space.title ?? 'Space capture', contributor,
        name: name.trim() || 'Contributor', ...(snapshot ? { request: snapshot } : {}) }).catch(error => setNotice(errorText(error)))
    } else { setCaptureRequest(snapshot); setModal('capture') }
  }
  const viewLinkedCaptures = (request: CaptureRequestContext) => {
    setNotice(''); setCaptureFilter(request); setCaptureLimit(24); setTab('captures')
  }
  const openSpace = useCallback((id: string, view: DetailTab = 'overview') => {
    setRemovalsOpen(false)
    setSelectedMemory(null)
    setSelected(id)
    setDetailError('')
    setTab(view)
    setSelectedCell(null)
    setSurfaceFocus(null)
    setSurfaceRequest(null)
    setCaptureFilter(null)
    setCaptureLimit(24)
    setCreating(false)
    setSharing(false)
  }, [])

  useEffect(() => {
    if (!client || invitedSpace) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const update = async () => {
      try {
        const result = await client.list(controller.signal)
        if (!controller.signal.aborted) {
          setSpaces(result)
          setLoading(false)
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setNotice(errorText(error))
          setLoading(false)
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void update(), 10_000)
    }
    void update()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [client, refreshKey, invitedSpace])

  useEffect(() => {
    if (!client || !selected) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const update = async () => {
      try {
        const result = await client.detail(selected, controller.signal)
        if (!controller.signal.aborted) {
          setDetail(result)
          setDetailError('')
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          // Do not leave stale live locations or a refused space looking current.
          // Android's explicitly labeled offline cache is handled by its client instead.
          setDetail(null)
          setSelectedMemory(null)
          setDetailError(errorText(error))
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void update(), 5000)
    }
    void update()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [client, selected, refreshKey])

  // Sharing is opt-in, limited to the open space, and stopped when the screen is left.
  useEffect(() => {
    if (!sharing || !client || !selected || !navigator.geolocation) return
    let lastSent = 0
    const watch = navigator.geolocation.watchPosition(
      (value) => {
        const fix = phonePosition(value)
        setPosition(fix)
        if (Date.now() - lastSent < 10_000) return
        lastSent = Date.now()
        void client
          .presence(selected, contributor, name, fix)
          .catch((error) => setNotice(errorText(error)))
      },
      () => setNotice('Location sharing paused. Check location permission.'),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 15_000 },
    )
    return () => {
      navigator.geolocation.clearWatch(watch)
      void client.leave(selected, contributor).catch(() => {})
    }
  }, [sharing, client, selected, contributor, name])

  const activeDetail = detail?.space.id === selected ? detail : null
  useEffect(() => {
    if (activeDetail) remember(activeDetail.captures)
  }, [activeDetail, remember])
  const requestCount = activeDetail ? spaceCaptureRequests(activeDetail).filter(item => isActionableRequest(item, activeDetail)).length : 0
  const inspectRequest = (request: SpaceRequest | SurfaceRequest) => {
    setNotice(''); setSurfaceFocus(null)
    if ('cell_id' in request) { setSelectedCell(request.cell_id); setTab('coverage') }
    else { setSurfaceRequest(request); setTab('world') }
  }
  const linkedIds = activeDetail && captureFilter ? linkedCaptureIds(activeDetail, captureFilter.target) : null
  const visibleCaptures = (activeDetail?.captures ?? []).filter(capture => !linkedIds || linkedIds.includes(capture.id))
  const visible = useMemo(
    () =>
      spaces.filter((space) => {
        const matches = `${space.title} ${space.place} ${space.description}`
          .toLowerCase()
          .includes(query.toLowerCase())
        return (
          matches &&
          (filter === 'saved' ? journey.saved.includes(space.id)
            : filter === 'examples' ? false
            : filter === 'all'
            ? space.status === 'active'
            : filter === 'resolved'
              ? space.status === 'resolved'
              : filter === 'incident'
                ? space.status === 'active' &&
                  (space.category === 'incident' || space.category === 'hazard')
                : space.status === 'active' && (space.open_request_count ?? 0) > 0)
        )
      }),
    [spaces, query, filter, journey.saved],
  )
  const mapCenter: [number, number] =
    creating && hasCoordinate
      ? [draft.longitude, draft.latitude]
      : activeDetail
        ? [activeDetail.space.longitude, activeDetail.space.latitude]
        : position
          ? [position.longitude, position.latitude]
          : spaces[0]
            ? [spaces[0].longitude, spaces[0].latitude]
            : center
  const focusedCell = tab === 'coverage' ? activeDetail?.coverage.cells.find(cell => cell.id === selectedCell) : undefined
  const exampleView = !selected && !creating && (filter === 'examples' || (filter === 'all' && !loading && spaces.length === 0))
  const openExample = (value: CommunityExample) => { setExample(value); setModal('example') }

  const run = async (operation: () => Promise<void>) => {
    setBusy(true)
    setNotice('')
    try {
      await operation()
    } catch (error) {
      setNotice(errorText(error))
    } finally {
      setBusy(false)
    }
  }
  const findLocation = () =>
    run(async () => {
      const fix = await locate()
      setPosition(fix)
      setCenter([fix.longitude, fix.latitude])
      if (creating) {
        setDraft((value) => ({
          ...value,
          latitude: fix.latitude,
          longitude: fix.longitude,
        }), [String(fix.latitude), String(fix.longitude)])
      }
    })
  const create = () =>
    run(async () => {
      if (!client || !hasCoordinate) return
      const publication = await localDraft.session.submission()
      const result = await client.publishDraft(publication.id, publication.submitted!)
      if (result.contributor_token) setInviteTokens((tokens) => ({
        ...tokens, [result.space.id]: result.contributor_token!,
      }))
      await localDraft.session.discard()
      setCreating(false)
      openSpace(result.space.id)
      refresh()
      setNotice('Your space is ready. Add the first perspective.')
    })

  const legacyInvitation = async (replace = false) => {
      if (!client || !selected) throw new Error('Choose a space first.')
      const token = !replace && inviteTokens[selected] || (await client.invitation(selected))
      setInviteTokens((tokens) => ({ ...tokens, [selected]: token }))
      const invite = JSON.stringify({
        relay: client.connection.baseUrl,
        workspace: client.connection.sessionId,
        space: selected,
        token,
      })
      return invite
  }

  const useExample = () => {
    if (!client) { openConnect(); return }
    if (!example) return
    // Never overwrite an existing draft merely by browsing an example.
    if (localDraft.draft) {
      setModal(null); startDraft(); setNotice('Your existing draft is safe. Finish or discard it before using a starter story.'); return
    }
    try {
      localDraft.session.start()
      setDraft({ ...example.space }, [String(example.space.latitude), String(example.space.longitude)])
      setCreating(true); setSelected(null); setSharing(false); setModal(null)
      setNotice('This is your private draft. Replace the example text with your own observations before publishing.')
    } catch (error) { setNotice(errorText(error)) }
  }

  return (
    <main
      id="pane"
      tabIndex={-1}
      className={`atlas-workspace ${selected || creating ? 'has-detail' : ''}`}
      data-pane="1"
    >
      <div className="atlas-topbar">
        <div className="atlas-title-group">
          <span className="atlas-eyebrow">YOUR COMMUNITY ATLAS</span>
          <h1>
            Spaces<span className="atlas-title-dot">.</span>
          </h1>
        </div>
        <div className="atlas-topbar-actions">
          <button className="community-help" aria-label="How Sweep works" onClick={() => setModal('guide')}>How it works</button>
          <button className="community-points" aria-label={`Your journey: ${score.points} perspective points`} onClick={() => setModal('journey')}><Icon name="spark" size={16} />{score.points}<span> pts</span></button>
          <span className={`atlas-workspace-status ${client ? 'is-connected' : ''}`}>
            <i />
            {client ? 'Shared workspace' : 'Workspace offline'}
          </span>
          <button
            className="atlas-primary"
            disabled={Boolean(invitedSpace) || Boolean(client && !localDraft.loaded)}
            onClick={startDraft}
          >
            <Icon name="plus" size={18} />
            <span>{localDraft.draft ? 'Continue draft' : 'Create a space'}</span>
          </button>
          {onOpenAccountSpaces && <button className="atlas-secondary" disabled={busy} onClick={() => void run(async () => { await localDraft.session.flush(); onOpenAccountSpaces() })}><Icon name="people" size={17} />My spaces</button>}
        </div>
      </div>
      {journeyError && <p className="community-storage-notice" role="status">{journeyError}</p>}
      {!selected && !creating && <section className="community-hero" aria-label="Welcome to your community">
        <div><span className="atlas-eyebrow"><Icon name="heart" size={13} /> SMALL CONTRIBUTIONS. SHARED POSSIBILITIES.</span><h2>Your perspective belongs here.</h2>
          <p>Discover a place. Share what you see. Help your neighbors build the bigger picture.</p></div>
        <button className="community-hero-action" onClick={() => setFilter('examples')}><span className="community-orbit"><Icon name="people" size={28} /></span><span><strong>A good place to start</strong><small>Explore stories from Austin <Icon name="arrow" size={14} /></small></span></button>
      </section>}
      <div className="atlas-stage" data-scroll="1" data-detail={selected || creating ? '1' : undefined}>
        <Suspense fallback={<div className="atlas-map-loading">Loading your atlas…</div>}>
          {tab === 'world' && activeDetail?.reconstruction.status === 'ready' && client ? (
            <WorldViewer
              key={activeDetail.reconstruction.id}
              client={client}
              spaceId={activeDetail.space.id}
              jobId={activeDetail.reconstruction.id!}
              checksum={activeDetail.reconstruction.artifact_sha256!}
              focus={surfaceFocus}
            />
          ) : (
            <SpaceMap
              spaces={exampleView ? EXAMPLE_MAP_SPACES.filter(space => `${space.title} ${space.description} ${space.place}`.toLowerCase().includes(query.toLowerCase())) : visible}
              detail={tab === 'timeline' && activeDetail ? { ...activeDetail,
                captures: activeDetail.captures.filter(capture => timelineIds.includes(capture.id)),
                people: [], requests: [], surface_requests: [], coverage: { ...activeDetail.coverage, cells: [] },
              } : activeDetail}
              center={exampleView ? [-97.728, 30.28] : focusedCell ? [focusedCell.longitude, focusedCell.latitude] : mapCenter}
              overviewZoom={exampleView ? 12 : undefined}
              picking={creating && !submitted && !busy}
              coverageVisible={tab === 'coverage'}
              selectedCell={selectedCell}
              position={tab === 'timeline' ? null : position}
              onSelect={id => { const starter = exampleView && COMMUNITY_EXAMPLES.find(value => value.id === id); if (starter) openExample(starter); else openSpace(id) }}
              onCell={setSelectedCell}
              onPick={(longitude, latitude) => {
                setDraft((value) => ({ ...value, latitude, longitude }), [String(latitude), String(longitude)])
              }}
            />
          )}
        </Suspense>

        {!selected && !creating && (
          <section className="atlas-feed" aria-label="Space directory">
            <div className="atlas-feed-intro">
              <span className="atlas-eyebrow">FIND YOUR NEXT CONNECTION</span>
              <h2>Places worth caring about.</h2>
              <p>A familiar corner. A shared question. A place to help.</p>
            </div>
            <label className="atlas-search">
              <Icon name="search" size={18} />
              <input
                aria-label="Search spaces"
                placeholder="Search a place or a space"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
              <kbd>/</kbd>
            </label>
            <div className="atlas-filter-row" role="group" aria-label="Filter spaces">
              {(
                [
                  ['all', 'Explore'],
                  ['incident', 'Reports'],
                  ['needs-captures', 'Needs views'],
                  ['saved', 'Saved'],
                  ['examples', 'Examples'],
                  ['resolved', 'Resolved'],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  aria-pressed={filter === id}
                  className={filter === id ? 'is-active' : ''}
                  onClick={() => setFilter(id)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="atlas-list-heading">
              <span>
                {exampleView ? 'Austin starter stories' :
                  <>
                {loading
                  ? 'Loading spaces'
                  : `${visible.length} ${visible.length === 1 ? 'space' : 'spaces'}`}
                  </>}
              </span>
              <span>
                {exampleView ? 'Examples, not live reports' : <>Latest activity <Icon name="clock" size={13} /></>}
              </span>
            </div>
            <div className="atlas-space-list">
              {localDraft.error && <p className="atlas-draft-error" role="alert">{localDraft.error}
                <button className="atlas-text-button" onClick={() => void localDraft.session.load()}>Retry local storage</button></p>}
              {localDraft.draft && <div className="atlas-draft-card">
                <span className="atlas-eyebrow">ONLY ON THIS DEVICE</span>
                <h3>{localDraft.draft.space.title || 'Your next space'}</h3>
                <p>{submitted ? 'Publication needs confirmation. Retry to find the same space.' : 'A private draft, ready when you are. Nothing has been shared.'}</p>
                <button className="atlas-text-button" onClick={startDraft}>Continue draft <Icon name="arrow" size={16} /></button>
              </div>}
              {visible.map((space) => (
                <div className="community-space-row" key={space.id}><SpaceCard space={space} onOpen={() => openSpace(space.id, filter === 'needs-captures' ? 'requests' : 'overview')} />
                  <button className="community-save" aria-label={`${journey.saved.includes(space.id) ? 'Unsave' : 'Save'} ${space.title}`} aria-pressed={journey.saved.includes(space.id)} onClick={() => toggleSaved(space.id)}><Icon name="bookmark" size={17} /></button>
                </div>
              ))}
              {filter === 'saved' && !visible.length && <div className="atlas-empty"><Icon name="bookmark" size={32} /><h3>A place to come back to.</h3><p>Save a space using its bookmark. Your saved list stays on this device; it doesn’t enable notifications.</p><button className="atlas-text-button" onClick={() => { setFilter('all'); setQuery('') }}>Explore all spaces <Icon name="arrow" /></button></div>}
              {exampleView && <ExampleList query={query} onOpen={openExample} />}
              {!loading && visible.length === 0 && !exampleView && !['examples', 'saved'].includes(filter) && (
                <div className="atlas-empty">
                  <div className="atlas-empty-symbol">
                    <Icon name="spaces" size={32} />
                  </div>
                  <h3>{query ? 'No matching spaces' : filter === 'needs-captures' ? 'No open requests right now.' : 'A new perspective starts here.'}</h3>
                  <p>
                    {query
                      ? 'Try another place, title, or category.'
                      : client && filter === 'needs-captures'
                        ? 'Requests appear when someone asks for a map location or a current 3D region. Unmapped areas alone are not requests.'
                      : client
                        ? 'Create the first space in this workspace. A location and a few photos are all it takes to begin.'
                        : 'Connect your workspace to discover shared spaces and contribute your view.'}
                  </p>
                  <button
                    className="atlas-text-button"
                    disabled={Boolean(client && filter !== 'needs-captures' && !localDraft.loaded)}
                    onClick={client && filter === 'needs-captures' ? () => setFilter('all') : startDraft}
                  >
                    {client ? filter === 'needs-captures' ? 'Explore all spaces' : 'Create the first space' : 'Connect workspace'}
                    <Icon name="arrow" size={16} />
                  </button>
                </div>
              )}
            </div>
            <div className="atlas-field-note">
              <Icon name="panorama" size={22} />
              <div>
                <strong>Every angle adds understanding.</strong>
                <p>Photos, video, and 360 scans come together here.</p>
              </div>
            </div>
          </section>
        )}

        {creating && (
          <section className="atlas-detail atlas-create" aria-label="Create a space">
            <button className="atlas-back" disabled={busy} onClick={() => void run(async () => { await localDraft.session.flush(); setCreating(false) })}>
              <Icon name="back" size={17} />
              Back to spaces
            </button>
            <span className="atlas-eyebrow">START SOMETHING SHARED</span>
            <h2>
              A place. A purpose.
              <br />A new perspective.
            </h2>
            <p className="atlas-muted">Give people a place to contribute what they see.</p>
            <div className="atlas-draft-status" role="status">
              <strong>{localDraft.error ? 'Draft not saved' : localDraft.saving ? 'Saving on this device…' : 'Draft saved on this device'}</strong>
              <p>{submitted ? 'A publication was attempted. Details are locked so a retry cannot create another report. Check publication when connected.' : 'Private to this browser or device and workspace connection. Nothing is shared until you publish.'}</p>
              {localDraft.error && <><p className="atlas-draft-error">{localDraft.error}</p><button className="atlas-text-button" onClick={() => void run(() => localDraft.session.flush())}>Retry saving</button></>}
            </div>
            {notice && <div className="atlas-draft-notice" role="status"><span>{notice}</span>
              <button type="button" aria-label="Dismiss notice" onClick={() => setNotice('')}><Icon name="close" size={16} /></button></div>}
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void create()
              }}
            >
              <fieldset className="atlas-compose-fields" disabled={busy || Boolean(submitted)}>
              <label className="atlas-field">
                Space name
                <input
                  required
                  minLength={3}
                  maxLength={100}
                  value={draft.title}
                  onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                  placeholder="What’s happening here?"
                />
              </label>
              <fieldset className="atlas-fieldset">
                <legend>What kind of space?</legend>
                <div className="atlas-category-options">
                  {(Object.keys(CATEGORY_LABEL) as SpaceCategory[]).map((category) => (
                    <button
                      type="button"
                      key={category}
                      aria-pressed={draft.category === category}
                      className={draft.category === category ? 'is-active' : ''}
                      onClick={() => setDraft({ ...draft, category })}
                    >
                      {CATEGORY_LABEL[category]}
                    </button>
                  ))}
                </div>
              </fieldset>
              <label className="atlas-field">
                The story so far
                <textarea
                  maxLength={2000}
                  rows={3}
                  value={draft.description}
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                  placeholder="Share what you observed and what would help."
                />
              </label>
              <label className="atlas-field">
                Place name
                <input
                  maxLength={120}
                  value={draft.place}
                  onChange={(e) => setDraft({ ...draft, place: e.target.value })}
                  placeholder="A landmark, street, or neighborhood"
                />
              </label>
              <div className="atlas-location-picker">
                <Icon name="pin" />
                <div>
                  <strong>
                    {hasCoordinate
                      ? `${draft.latitude.toFixed(5)}, ${draft.longitude.toFixed(5)}`
                      : 'Choose a place on the map'}
                  </strong>
                  <p>Tap the map or use your phone’s location.</p>
                </div>
                <button
                  type="button"
                  aria-label="Use my location for this space"
                  disabled={busy}
                  onClick={() => void findLocation()}
                >
                  <Icon name="target" />
                </button>
              </div>
              <div className="atlas-coordinates">
                <label className="atlas-field">
                  Latitude
                  <input
                    type="number"
                    step="any"
                    min={-85}
                    max={85}
                    value={coordinateInput[0]}
                    onChange={(e) => {
                      setDraft({ ...draft, latitude: Number(e.target.value) }, [e.target.value, coordinateInput[1]])
                    }}
                    required
                  />
                </label>
                <label className="atlas-field">
                  Longitude
                  <input
                    type="number"
                    step="any"
                    min={-180}
                    max={180}
                    value={coordinateInput[1]}
                    onChange={(e) => {
                      setDraft({ ...draft, longitude: Number(e.target.value) }, [coordinateInput[0], e.target.value])
                    }}
                    required
                  />
                </label>
              </div>
              <label className="atlas-field">
                Area radius <span className="atlas-range-value">{draft.radius} m</span>
                <input
                  type="range"
                  min={20}
                  max={500}
                  step={10}
                  value={draft.radius}
                  onChange={(e) => setDraft({ ...draft, radius: Number(e.target.value) })}
                />
              </label>
              </fieldset>
              <button
                className="atlas-primary atlas-full"
                disabled={!client || !hasCoordinate || busy}
              >
                {busy ? 'Confirming publication…' : submitted ? 'Check publication' : 'Publish space'}
                <Icon name="arrow" size={18} />
              </button>
              <div className="atlas-draft-actions">
                <button type="button" disabled={busy} onClick={() => void run(async () => { await localDraft.session.flush(); setCreating(false) })}>Keep for later</button>
                <button type="button" disabled={busy} onClick={() => setModal('discard')}>Discard draft</button>
              </div>
            </form>
          </section>
        )}

        {selected && (
          <section className="atlas-detail" aria-label="Space details">
            <div className="atlas-detail-toolbar">
              <button
                className="atlas-back"
                onClick={() => {
                  if (invitedSpace) {
                    if (onExitAccount) { onExitAccount(); return }
                    openConnect()
                    return
                  }
                  setSelected(null)
                  setDetail(null)
                  setSharing(false)
                }}
              >
                <Icon name="back" size={17} />
                {onExitAccount ? 'My spaces' : invitedSpace ? 'Open another space' : 'All spaces'}
              </button>
              {(!invitedSpace || accountRole === 'owner') && (
                <button className="atlas-icon-button" aria-label="Share space" onClick={() => setModal('share')}>
                  <Icon name="share" size={18} />
                </button>
              )}
              {activeDetail && <button className="atlas-icon-button" aria-label={journey.saved.includes(activeDetail.space.id) ? 'Unsave this space' : 'Save this space'} aria-pressed={journey.saved.includes(activeDetail.space.id)} onClick={() => toggleSaved(activeDetail.space.id)}><Icon name="bookmark" size={18} /></button>}
            </div>
            {!activeDetail ? (
              <div className="atlas-empty" role="status">
                {detailError ? <><h3>Space unavailable</h3><p>{detailError}</p>
                  <button className="atlas-secondary" onClick={() => { setDetailError(''); refresh() }}>Try again</button>
                </> : 'Loading this space…'}
              </div>
            ) : (
              <>
                <span className={`atlas-category ${activeDetail.space.category}`}>
                  <span />
                  {CATEGORY_LABEL[activeDetail.space.category]}
                </span>
                <h2>{activeDetail.space.title}</h2>
                {accountRole && <p className="atlas-fine">{accountRole === 'owner' ? 'This is your Space. Invite people, add captures, and manage who can participate.' : accountRole === 'viewer' ? 'You’re here to explore. This account has view-only access.' : 'You’re a contributor. Your captures are attributed to your signed-in account.'}</p>}
                <p className="atlas-place">
                  <Icon name="pin" size={15} />
                  {activeDetail.space.place ||
                    `${activeDetail.space.latitude.toFixed(4)}, ${activeDetail.space.longitude.toFixed(4)}`}
                </p>
                <div className="atlas-detail-meta">
                  <span className="atlas-report-state">
                    {activeDetail.space.status === 'resolved' ? 'Resolved' : 'Open space'}
                  </span>
                  <span>{age(activeDetail.space.created_at)}</span>
                  {activeDetail.space.category === 'incident' && (
                    <span>Community report · unverified</span>
                  )}
                </div>
                <div className="atlas-detail-tabs" role="group" aria-label="Space views">
                  {(
                    [
                      ['overview', 'Overview'],
                      ['captures', 'Captures'],
                      ['timeline', 'Timeline'],
                      ['coverage', 'Coverage'],
                      ['requests', 'Requests'],
                      ['world', '3D atlas'],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      className={tab === value ? 'is-active' : ''}
                      aria-pressed={tab === value}
                      onClick={() => { if (tab !== value) { setTab(value); setSurfaceFocus(null); setSurfaceRequest(null) } }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {tab === 'overview' && (
                  <>
                    <p className="atlas-story">
                      {activeDetail.space.description ||
                        'A shared space for a better view of this area. Add the first photo or video to begin.'}
                    </p>
                    <div className="community-together"><Icon name="people" size={24} /><div><strong>There’s room for your perspective.</strong><p>Add a view, answer a request, or invite someone who knows this place.</p></div></div>
                    {contributingNeighbors(activeDetail).length > 0 && <section className="community-neighbors" aria-label="Contributing neighbors"><h3>Built with a little help from…</h3><div>{contributingNeighbors(activeDetail).slice(0, 6).map(person => <span key={person.id}><i>{person.name.slice(0, 1).toUpperCase()}</i><strong>{person.name}</strong><small>{person.captures} {person.captures === 1 ? 'view' : 'views'}</small></span>)}</div><p>Names supplied with contributions · not live locations or verified identities.</p></section>}
                    {requestCount > 0 && <button className="atlas-request-summary" onClick={() => setTab('requests')}>
                      <Icon name="target" size={24} /><span><strong>{requestCount} {requestCount === 1 ? 'view requested' : 'views requested'}</strong>
                        <small>See where your next perspective can help.</small></span><Icon name="arrow" size={18} />
                    </button>}
                    <div className="atlas-space-metrics">
                      <div>
                        <strong>{activeDetail.captures.length}</strong>
                        <span>captures</span>
                      </div>
                      <div>
                        <strong>{activeDetail.space.contributors}</strong>
                        <span>contributors</span>
                      </div>
                      <div>
                        <strong>
                          {activeDetail.coverage.percent}
                          <small>%</small>
                        </strong>
                        <span>capture coverage</span>
                      </div>
                    </div>
                    <button className="atlas-coverage-preview" onClick={() => setTab('coverage')}>
                      <CoverageMini detail={activeDetail} />
                      <div>
                        <span className="atlas-eyebrow">THE PICTURE IS GROWING</span>
                        <strong>
                          {activeDetail.coverage.observed
                            ? 'Help connect the missing pieces.'
                            : 'Be the first to put this place on the map.'}
                        </strong>
                        <span>
                          Explore coverage <Icon name="arrow" size={15} />
                        </span>
                      </div>
                    </button>
                    <h3 className="atlas-section-title">
                      People in this space
                      <span>{activeDetail.people.length} sharing</span>
                    </h3>
                    <div className="atlas-people">
                      {activeDetail.people.length ? (
                        activeDetail.people.map((person) => (
                          <div className="atlas-person" key={person.contributor_id}>
                            <span className="atlas-avatar">
                              {person.name.slice(0, 1).toUpperCase()}
                            </span>
                            <div>
                              <strong>{person.name}</strong>
                              <span>
                                GPS ±{Math.round(person.position.accuracy)} m ·{' '}
                                {age(person.position.timestamp)}
                              </span>
                            </div>
                            <i />
                          </div>
                        ))
                      ) : (
                        <p className="atlas-muted">
                          No one is sharing their live location right now.
                        </p>
                      )}
                    </div>
                    <label className="atlas-field atlas-name">
                      Your contributor name
                      <input
                        maxLength={40}
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                      />
                    </label>
                    <button
                      className={`atlas-secondary atlas-full ${sharing ? 'is-active' : ''}`}
                      disabled={!canContribute || !name.trim()}
                      onClick={() => setSharing((value) => !value)}
                    >
                      <Icon name="target" size={17} />
                      {sharing ? 'Stop sharing my location' : 'Share my location in this space'}
                    </button>
                    <p className="atlas-fine">
                      Visible only while you choose to share. Stale locations disappear after 90
                      seconds.
                    </p>
                  </>
                )}
                {tab === 'captures' && (
                  <>
                    <div className="atlas-section-heading">
                      <h3>Every perspective, together.</h3>
                      <p>Original media stays connected to its source and capture time.</p>
                    </div>
                    {canContribute && client?.memoryRemovalSupported && <button className="atlas-text-button" onClick={() => setRemovalsOpen(true)}>Removal status</button>}
                    {captureFilter && <div className="atlas-request-context" role="status">
                      <strong>{captureFilter.label} · {visibleCaptures.length} linked views</strong>
                      <p>These originals were submitted for this request. Review them before assessing coverage.</p>
                      <button className="atlas-text-button" onClick={() => { setCaptureFilter(null); setCaptureLimit(24) }}>Show all captures</button>
                    </div>}
                    {visibleCaptures.length === 0 ? (
                      <div className="atlas-empty">
                        <Icon name="camera" size={32} />
                        <h3>The first view is yours.</h3>
                        <p>Add a photo, record a short video, or walk through a 360 scan.</p>
                      </div>
                    ) : (
                      <div className="atlas-capture-grid">
                        {visibleCaptures.slice(0, captureLimit).map((capture) => (
                          <MediaCard
                            key={capture.id}
                            capture={capture}
                            client={client!}
                            spaceId={selected}
                            onOpenMemory={setSelectedMemory}
                          />
                        ))}
                      </div>
                    )}
                    {visibleCaptures.length > captureLimit && (
                      <button className="atlas-secondary atlas-full" onClick={() => setCaptureLimit(limit => limit + 24)}>
                        Show {Math.min(24, visibleCaptures.length - captureLimit)} more captures
                      </button>
                    )}
                  </>
                )}
                {tab === 'requests' && <CaptureRequestsPanel key={selected} detail={activeDetail} canContribute={canContribute}
                  onInspect={inspectRequest} onContribute={openCapture} onViewCaptures={viewLinkedCaptures} />}
                {tab === 'timeline' && <Suspense fallback={<p role="status">Opening the timeline…</p>}><SpaceTimeline key={selected} client={client!} spaceId={selected} onVisible={setTimelineIds} onOpenMemory={setSelectedMemory} /></Suspense>}
                {tab === 'coverage' && (
                  <>
                    <div className="atlas-section-heading">
                      <h3>A little more context goes a long way.</h3>
                      <p>Select a gray cell to request another perspective.</p>
                    </div>
                    <div className="atlas-coverage-number">
                      <strong>{activeDetail.coverage.percent}%</strong>
                      <span>
                        {activeDetail.coverage.observed} of {activeDetail.coverage.total} capture
                        areas
                      </span>
                    </div>
                    <CoverageMini
                      detail={activeDetail}
                      selected={selectedCell}
                      onSelect={setSelectedCell}
                    />
                    <div className="atlas-map-key">
                      <span>
                        <i className="is-covered" />
                        Captured location
                      </span>
                      <span>
                        <i />
                        Needs a viewpoint
                      </span>
                    </div>
                    <p className="atlas-fine">
                      Coverage uses capture-time GPS with sufficient accuracy. Camera positions do
                      not establish reconstructed surface completeness.
                    </p>
                    <button
                      className="atlas-primary atlas-full"
                      disabled={
                        !canContribute || !selectedCell ||
                        activeDetail.space.status !== 'active' ||
                        busy ||
                        Boolean(
                          activeDetail.coverage.cells.find((cell) => cell.id === selectedCell)
                            ?.captures,
                        )
                      }
                      onClick={() =>
                        void run(async () => {
                          await client!.request(selected, selectedCell!)
                          refresh()
                          setTab('requests')
                          setNotice(
                            'Viewpoint requested. Contributors can see this area in the space.',
                          )
                        })
                      }
                    >
                      <Icon name="plus" size={17} />
                      {selectedCell ? 'Request a view here' : 'Select a missing area'}
                    </button>
                    <button className="atlas-secondary atlas-full" onClick={() => setTab('requests')}>View all requests</button>
                  </>
                )}
                {tab === 'world' && (
                  <>
                    <ReconstructionPanel
                      job={activeDetail.reconstruction}
                      client={client!}
                      spaceId={selected}
                      canBuild={!invitedSpace}
                      onChange={refresh}
                    />
                    <SurfaceReviewPanel
                      key={`${selected}:${activeDetail.reconstruction.id}`}
                      job={activeDetail.reconstruction}
                      requests={activeDetail.surface_requests ?? []}
                      client={client!} spaceId={selected} canManage={!invitedSpace}
                      active={activeDetail.space.status === 'active'}
                      onFocus={setSurfaceFocus} onChange={refresh}
                      initialRegionId={surfaceRequest && isCurrentSurfaceRequest(surfaceRequest, activeDetail.reconstruction) ? surfaceRequest.region_id : undefined}
                      onContribute={canContribute ? openCapture : undefined} onViewCaptures={viewLinkedCaptures}
                    />
                  </>
                )}
                <div className="atlas-detail-footer">
                  <button
                    className="atlas-primary atlas-full"
                    disabled={!canContribute || activeDetail.space.status !== 'active'}
                    onClick={() => openCapture()}
                  >
                    <Icon name="camera" />
                    Add a capture
                    <Icon name="plus" size={17} />
                  </button>
                  {(!invitedSpace || accountRole === 'owner') && (
                    <button
                      className="atlas-text-button atlas-resolve"
                      disabled={busy}
                      onClick={() =>
                        void run(async () => {
                          await client!.status(
                            selected,
                            activeDetail.space.status === 'active' ? 'resolved' : 'active',
                          )
                          refresh()
                        })
                      }
                    >
                      {activeDetail.space.status === 'active'
                        ? 'Mark this space resolved'
                        : 'Reopen this space'}
                    </button>
                  )}
                </div>
              </>
            )}
          </section>
        )}

        {!(tab === 'world' && activeDetail?.reconstruction.status === 'ready') && (
          <div className="atlas-map-toolbar">
            <span className="atlas-map-label">
              <Icon name="spaces" size={16} />
              {creating
                ? 'Pick a location'
                : tab === 'coverage' && selected
                  ? 'Capture coverage'
                  : 'Geographic atlas'}
            </span>
            <button
              className="atlas-icon-button"
              aria-label="Find my location"
              disabled={busy}
              onClick={() => void findLocation()}
            >
              <Icon name="target" />
            </button>
          </div>
        )}
        {notice && !creating && (
          <div className="atlas-toast" role="status">
            <Icon name="spaces" size={18} />
            <span>{notice}</span>
            <button aria-label="Dismiss notice" onClick={() => setNotice('')}>
              <Icon name="close" size={16} />
            </button>
          </div>
        )}
      </div>
      {removalsOpen && client && selected && <Suspense fallback={<p role="status">Opening removal receipts…</p>}><SpaceRemovals key={selected} client={client} spaceId={selected} onClose={() => setRemovalsOpen(false)} /></Suspense>}
      {selectedMemory && client && selected && <Suspense fallback={<p role="status">Opening memory tools…</p>}><MemoryDialog key={`${selected}/${selectedMemory.id}`} client={client} spaceId={selected} capture={selectedMemory} onClose={() => setSelectedMemory(null)} onRemoved={memoryRemoved} /></Suspense>}
      {modal && (
        <AtlasDialog
          title={
            modal === 'capture'
              ? 'Add your perspective'
              : modal === 'connect'
                ? 'Connect your workspace'
                : modal === 'guide' ? 'A small start. A shared story.'
                : modal === 'journey' ? 'Every perspective counts.'
                : modal === 'example' ? 'Imagine what we could see together.'
                : modal === 'discard' ? 'Discard this local draft?' : 'Invite a contributor'
          }
          onClose={() => setModal(null)}
        >
          {modal === 'guide' && <CommunityGuide />}
          {modal === 'journey' && <>{journeyError && <p role="status">{journeyError}</p>}<JourneyPanel journey={journey} /></>}
          {modal === 'example' && example && <div className="community-example-detail"><ExampleArt theme={example.theme} /><span className="atlas-eyebrow">AUSTIN STARTER STORY · NOT A LIVE REPORT</span><h3>{example.space.title}</h3><p className="atlas-place"><Icon name="pin" size={16} />{example.space.place}</p><p>{example.space.description}</p><h4>A little invitation</h4><p>{example.invitation}</p><h4>Three ways to add your perspective</h4><ol>{example.views.map(view => <li key={view}>{view}</li>)}</ol><p className="atlas-fine">Illustration, not a photograph or reconstruction. These coordinates are a starting place, not a verified incident location.</p>
            {!invitedSpace && <button className="atlas-primary atlas-full" disabled={Boolean(client && !localDraft.loaded)} onClick={useExample}>{client ? localDraft.draft ? 'Continue your existing draft' : 'Start a space like this' : 'Connect a workspace to start'}<Icon name="arrow" size={16} /></button>}</div>}
          {modal === 'discard' && <>
            <p>This removes the draft’s text and selected location from this device. It cannot be undone.
              {submitted ? ' If publication already succeeded, the shared space remains available.' : ' No shared space or capture will be removed.'}</p>
            <div className="atlas-draft-actions"><button disabled={busy} onClick={() => setModal(null)}>Keep draft</button>
              <button disabled={busy} onClick={() => void run(async () => { await localDraft.session.discard(); setModal(null); setCreating(false); setNotice('The local draft was removed. Shared spaces and captures were not changed.') })}>Discard local draft</button></div>
          </>}
          {modal === 'capture' && client && selected && (
            <CaptureComposer
              client={client}
              spaceId={selected}
              contributor={contributor}
              name={name.trim() || 'Contributor'}
              onSaved={refresh}
              captureRequest={captureRequest}
            />
          )}
          {modal === 'connect' && (
            <ConnectForm
              onConnect={(value, space) => {
                setManualClient(value)
                setModal(null)
                setLoading(!space)
                setInvitedSpace(space ?? null)
                setSpaces([])
                setDetail(null)
                setSelected(null)
                setSharing(false)
                setCreating(false)
                if (space) openSpace(space)
                refresh()
              }}
            />
          )}
          {modal === 'share' && (
            <Suspense fallback={<p role="status">Opening sharing settings…</p>}>
              <SpaceSharing client={client!} spaceId={selected!} allowAccounts={!captureNative && !connectNative} allowLegacy={!accountRole} legacyInvitation={() => legacyInvitation()} replaceLegacy={() => legacyInvitation(true)} />
            </Suspense>
          )}
        </AtlasDialog>
      )}
    </main>
  )
}

function SpaceCard({ space, onOpen }: { space: Space; onOpen: () => void }) {
  return (
    <button className="atlas-space-card" aria-label={`Open ${space.title}`} onClick={onOpen}>
      <div className="atlas-card-heading">
        <span className={`atlas-category ${space.category}`}>
          <span />
          {CATEGORY_LABEL[space.category]}
        </span>
        <span className="atlas-time">{age(space.updated_at)}</span>
      </div>
      <h3>{space.title}</h3>
      <p>{space.description || 'A shared view of this place, built one contribution at a time.'}</p>
      <span className="atlas-card-place">
        <Icon name="pin" size={13} />
        {space.place || `${space.latitude.toFixed(4)}, ${space.longitude.toFixed(4)}`}
      </span>
      {(space.open_request_count ?? 0) > 0 && <span className="atlas-card-request-count"><Icon name="target" size={14} />
        {space.open_request_count} {space.open_request_count === 1 ? 'view requested' : 'views requested'}
      </span>}
      <div className="atlas-card-bottom">
        <span>
          <Icon name="camera" size={14} />
          {space.capture_count} {space.capture_count === 1 ? 'capture' : 'captures'}
        </span>
        <span>
          <Icon name="people" size={14} />
          {space.contributors}
        </span>
        <span className="atlas-card-progress">
          <i
            style={
              {
                '--coverage': `${space.coverage_percent}%`,
              } as React.CSSProperties
            }
          />
          {space.coverage_percent}%
        </span>
        <Icon name="arrow" size={16} />
      </div>
    </button>
  )
}

function CoverageMini({
  detail,
  selected,
  onSelect,
}: {
  detail: SpaceDetail
  selected?: string | null
  onSelect?: (id: string) => void
}) {
  const byId = new Map(detail.coverage.cells.map((cell) => [cell.id, cell]))
  return (
    <div
      className={`atlas-coverage-grid ${onSelect ? 'is-interactive' : ''}`}
      aria-label={onSelect ? 'Select a coverage area' : 'Capture coverage preview'}
    >
      {Array.from({ length: 100 }, (_, i) => {
        const id = `${i % 10}:${9 - Math.floor(i / 10)}`,
          cell = byId.get(id)
        const className = !cell
          ? 'is-outside'
          : cell.id === selected
            ? 'is-selected'
            : cell.captures
              ? 'is-observed'
              : ''
        return onSelect ? (
          <button
            key={id}
            className={className}
            disabled={!cell}
            title={`Area ${id}: ${cell?.captures ?? 0} captures`}
            aria-label={`Area ${id}, ${cell?.captures ?? 0} captures`}
            aria-pressed={selected === id}
            onClick={() => onSelect(id)}
          />
        ) : (
          <span key={id} className={className} />
        )
      })}
    </div>
  )
}

export function MediaCard({
  capture,
  client,
  spaceId,
  timeCaption,
  onOpenMemory,
}: {
  capture: Capture
  client: AtlasClient
  spaceId: string
  timeCaption?: string
  onOpenMemory?: (capture: Capture) => void
}) {
  const [url, setUrl] = useState('')
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [requested, setRequested] = useState(
    capture.kind !== 'video' && capture.bytes <= 8 * 1024 * 1024,
  )
  const [error, setError] = useState(false)
  const captureId = capture.id
  useEffect(() => {
    if (!requested) return
    const controller = new AbortController()
    let objectUrl = ''
    void client
      .media(spaceId, captureId, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true)
      })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [client, spaceId, captureId, requested])
  return (
    <article className="atlas-media-card">
      <div className="atlas-media-image">
        {url ? (
          capture.kind === 'video' ? (
            <video src={url} controls playsInline preload="metadata" />
          ) : (
            <img src={url} alt={`Capture by ${capture.name}`} loading="lazy" />
          )
        ) : !requested ? (
          <button onClick={() => setRequested(true)}>
            <Icon name={capture.kind === 'video' ? 'video' : 'camera'} />
            {`Load ${capture.kind === 'video' ? 'video' : 'image'} · ${Math.ceil(capture.bytes / 1024 / 1024)} MB`}
          </button>
        ) : (
          <span>{error ? 'Could not load' : 'Loading…'}</span>
        )}
        <span className="atlas-media-type">
          <Icon name={capture.kind === 'photo' ? 'camera' : capture.kind} size={13} />
        </span>
      </div>
      <strong>{capture.name}</strong>
      <span>
        {timeCaption ?? (capture.source === 'camera' && capture.captured_at !== null
          ? `${age(capture.captured_at)} · Phone capture`
          : `Imported ${age(capture.uploaded_at)} · Capture time ${capture.captured_at === null ? 'unknown' : 'unverified'}`)}
      </span>
      <span>
        {capture.position
          ? `GPS ±${Math.round(capture.position.accuracy)} m`
          : 'No capture location'}
      </span>
      <button className="atlas-text-button" onClick={() => onOpenMemory ? onOpenMemory(capture) : setMemoryOpen(true)}><Icon name="spark" size={15} />Memory & sounds</button>
      {memoryOpen && <Suspense fallback={<p role="status">Opening memory tools…</p>}><MemoryDialog client={client} spaceId={spaceId} capture={capture} onClose={() => setMemoryOpen(false)} /></Suspense>}
    </article>
  )
}


export function ConnectForm({ onConnect, createClient }: {
  onConnect: (client: AtlasClient, space?: string) => void
  createClient?: (connection: PlatformConnection, space?: string) => Promise<AtlasClient>
}) {
  const [relay, setRelay] = useState('http://127.0.0.1:8000')
  const [workspace, setWorkspace] = useState('atlas')
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [invitation, setInvitation] = useState('')
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        setBusy(true)
        setError('')
        void (async () => {
          try {
            if (invitation.trim()) {
              const invite = JSON.parse(invitation) as Record<string, string>
              if (
                ['relay', 'workspace', 'space', 'token'].some(
                  (key) => typeof invite[key] !== 'string' || !invite[key],
                )
              )
                throw new Error('Paste the complete space invitation.')
              const configuration = {
                baseUrl: invite.relay,
                sessionId: invite.workspace,
                token: invite.token,
              }
              const invited = createClient ? await createClient(configuration, invite.space) : new AtlasClient(configuration)
              await invited.detail(invite.space)
              onConnect(invited, invite.space)
            } else {
              const configuration = {
                baseUrl: relay.replace(/^http/, 'ws'),
                sessionId: workspace,
                token,
              }
              const connection = createClient ? await createClient(configuration) : new AtlasClient(configuration)
              await connection.list()
              onConnect(connection)
            }
          } catch (error) {
            setError(errorText(error))
          } finally {
            setBusy(false)
          }
        })()
      }}
    >
      <p className="atlas-muted">
        Use your workspace connection or paste a space invitation. {createClient
          ? 'Access is saved in encrypted storage on this device, separately from fleet controls.'
          : 'Credentials stay in memory for this visit.'}
      </p>
      <label className="atlas-field">
        Space invitation
        <textarea
          rows={3}
          value={invitation}
          onChange={(e) => setInvitation(e.target.value)}
          placeholder="Paste an invitation to contribute to one space"
        />
      </label>
      {!invitation && (
        <>
          <label className="atlas-field">
            Workspace address
            <input
              required
              value={relay}
              onChange={(e) => setRelay(e.target.value)}
              placeholder="https://your-workspace.example"
            />
          </label>
          <label className="atlas-field">
            Workspace name
            <input
              required
              maxLength={128}
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
            />
          </label>
          <label className="atlas-field">
            Access key
            <input
              required
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
        </>
      )}
      {error && (
        <p role="alert" className="atlas-inline-notice">
          {error}
        </p>
      )}
      <button className="atlas-primary atlas-full" disabled={busy}>
        {busy ? 'Connecting…' : 'Open workspace'}
        <Icon name="arrow" />
      </button>
    </form>
  )
}

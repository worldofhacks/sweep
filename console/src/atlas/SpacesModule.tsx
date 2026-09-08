import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import type { ModuleProps } from '../modules/types'
import { AtlasClient, locate, phonePosition } from './client'
import { CaptureComposer } from './CaptureComposer'
import { Icon } from './Icon'
import { ReconstructionPanel } from './ReconstructionPanel'
import {
  CATEGORY_LABEL,
  type Capture,
  type GeoPosition,
  type NewSpace,
  type Space,
  type SpaceCategory,
  type SpaceDetail,
} from './types'
import './atlas.css'

const SpaceMap = lazy(() => import('./SpaceMap'))
const WorldViewer = lazy(() => import('./WorldViewer'))
const DEFAULT_CENTER: [number, number] = [-98.5, 39.5]
const emptyDraft: NewSpace = {
  title: '',
  description: '',
  category: 'community',
  latitude: 0,
  longitude: 0,
  radius: 80,
  place: '',
}
type Filter = 'all' | 'incident' | 'needs-captures' | 'resolved'
type DetailTab = 'overview' | 'captures' | 'coverage' | 'world'
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

export function SpacesModule({ services }: ModuleProps) {
  const [manualClient, setManualClient] = useState<AtlasClient | null>(null)
  const [invitedSpace, setInvitedSpace] = useState<string | null>(null)
  const client = manualClient ?? services.atlas ?? null
  const [spaces, setSpaces] = useState<Space[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<SpaceDetail | null>(null)
  const [loading, setLoading] = useState(Boolean(client))
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState<DetailTab>('overview')
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState<NewSpace>(emptyDraft)
  const [coordinateInput, setCoordinateInput] = useState<[string, string]>(['', ''])
  const hasCoordinate = coordinateInput.every(value => value.trim() !== '' && Number.isFinite(Number(value)))
  const [position, setPosition] = useState<GeoPosition | null>(null)
  const [center, setCenter] = useState<[number, number]>(DEFAULT_CENTER)
  const [selectedCell, setSelectedCell] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [modal, setModal] = useState<'capture' | 'connect' | 'share' | null>(null)
  const [invitation, setInvitation] = useState('')
  const [inviteTokens, setInviteTokens] = useState<Record<string, string>>({})
  const [contributor] = useState(identity)
  const [name, setName] = useState('Contributor')
  const [sharing, setSharing] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])
  const openSpace = useCallback((id: string) => {
    setSelected(id)
    setTab('overview')
    setSelectedCell(null)
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
        }
      } catch (error) {
        if (!controller.signal.aborted) setNotice(errorText(error))
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
  const visible = useMemo(
    () =>
      spaces.filter((space) => {
        const matches = `${space.title} ${space.place} ${space.description}`
          .toLowerCase()
          .includes(query.toLowerCase())
        return (
          matches &&
          (filter === 'all'
            ? space.status === 'active'
            : filter === 'resolved'
              ? space.status === 'resolved'
              : filter === 'incident'
                ? space.status === 'active' &&
                  (space.category === 'incident' || space.category === 'hazard')
                : space.status === 'active' && space.coverage_percent < 100)
        )
      }),
    [spaces, query, filter],
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
        }))
        setCoordinateInput([String(fix.latitude), String(fix.longitude)])
      }
    })
  const create = () =>
    run(async () => {
      if (!client || !hasCoordinate) return
      const result = await client.create(draft)
      setInviteTokens((tokens) => ({
        ...tokens,
        [result.space.id]: result.contributor_token,
      }))
      setCreating(false)
      openSpace(result.space.id)
      refresh()
      setNotice('Your space is ready. Add the first perspective.')
    })

  const share = () =>
    void run(async () => {
      if (!client || !selected) return
      const token = inviteTokens[selected] ?? (await client.invitation(selected))
      setInviteTokens((tokens) => ({ ...tokens, [selected]: token }))
      const invite = JSON.stringify({
        relay: client.connection.baseUrl,
        workspace: client.connection.sessionId,
        space: selected,
        token,
      })
      setInvitation(invite)
      setModal('share')
    })

  return (
    <main
      id="pane"
      tabIndex={-1}
      className={`atlas-workspace ${selected || creating ? 'has-detail' : ''}`}
      data-pane="1"
    >
      <div className="atlas-topbar">
        <div className="atlas-title-group">
          <span className="atlas-eyebrow">THE COLLABORATIVE WORLD</span>
          <h1>
            Spaces<span className="atlas-title-dot">.</span>
          </h1>
        </div>
        <div className="atlas-topbar-actions">
          <span className={`atlas-workspace-status ${client ? 'is-connected' : ''}`}>
            <i />
            {client ? 'Shared workspace' : 'Workspace offline'}
          </span>
          <button
            className="atlas-primary"
            disabled={Boolean(invitedSpace)}
            onClick={() => {
              if (!client) {
                setModal('connect')
                return
              }
              setDraft(emptyDraft)
              setCoordinateInput(['', ''])
              setCreating(true)
              setSelected(null)
              setSharing(false)
            }}
          >
            <Icon name="plus" size={18} />
            <span>Create a space</span>
          </button>
        </div>
      </div>
      <div className="atlas-stage" data-scroll="1">
        <Suspense fallback={<div className="atlas-map-loading">Loading your atlas…</div>}>
          {tab === 'world' && activeDetail?.reconstruction.status === 'ready' && client ? (
            <WorldViewer
              key={activeDetail.reconstruction.id}
              client={client}
              spaceId={activeDetail.space.id}
              jobId={activeDetail.reconstruction.id!}
              checksum={activeDetail.reconstruction.artifact_sha256!}
            />
          ) : (
            <SpaceMap
              spaces={visible}
              detail={activeDetail}
              center={mapCenter}
              picking={creating}
              coverageVisible={tab === 'coverage'}
              selectedCell={selectedCell}
              position={position}
              onSelect={openSpace}
              onCell={setSelectedCell}
              onPick={(longitude, latitude) => {
                setDraft((value) => ({ ...value, latitude, longitude }))
                setCoordinateInput([String(latitude), String(longitude)])
              }}
            />
          )}
        </Suspense>

        {!selected && !creating && (
          <section className="atlas-feed" aria-label="Space directory">
            <div className="atlas-feed-intro">
              <span className="atlas-eyebrow">A SHARED PERSPECTIVE</span>
              <h2>
                See what’s happening.
                <br />
                Build the whole picture.
              </h2>
              <p>Local stories. Real perspectives. One evolving atlas.</p>
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
                {loading
                  ? 'Loading spaces'
                  : `${visible.length} ${visible.length === 1 ? 'space' : 'spaces'}`}
              </span>
              <span>
                Latest activity <Icon name="clock" size={13} />
              </span>
            </div>
            <div className="atlas-space-list">
              {visible.map((space) => (
                <SpaceCard key={space.id} space={space} onOpen={() => openSpace(space.id)} />
              ))}
              {!loading && visible.length === 0 && (
                <div className="atlas-empty">
                  <div className="atlas-empty-symbol">
                    <Icon name="spaces" size={32} />
                  </div>
                  <h3>{query ? 'No matching spaces' : 'A new perspective starts here.'}</h3>
                  <p>
                    {query
                      ? 'Try another place, title, or category.'
                      : client
                        ? 'Create the first space in this workspace. A location and a few photos are all it takes to begin.'
                        : 'Connect your workspace to discover shared spaces and contribute your view.'}
                  </p>
                  <button
                    className="atlas-text-button"
                    onClick={() => {
                      if (client) {
                        setCreating(true)
                        setSelected(null)
                      } else setModal('connect')
                    }}
                  >
                    {client ? 'Create the first space' : 'Connect workspace'}
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
            <button className="atlas-back" onClick={() => setCreating(false)}>
              <Icon name="back" size={17} />
              Back to spaces
            </button>
            <span className="atlas-eyebrow">START SOMETHING SHARED</span>
            <h2>
              A place. A purpose.
              <br />A new perspective.
            </h2>
            <p className="atlas-muted">Give people a place to contribute what they see.</p>
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void create()
              }}
            >
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
                      setDraft({ ...draft, latitude: Number(e.target.value) })
                      setCoordinateInput([e.target.value, coordinateInput[1]])
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
                      setDraft({ ...draft, longitude: Number(e.target.value) })
                      setCoordinateInput([coordinateInput[0], e.target.value])
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
              <button
                className="atlas-primary atlas-full"
                disabled={!client || !hasCoordinate || busy}
              >
                {busy ? 'Creating…' : 'Create space'}
                <Icon name="arrow" size={18} />
              </button>
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
                    setModal('connect')
                    return
                  }
                  setSelected(null)
                  setDetail(null)
                  setSharing(false)
                }}
              >
                <Icon name="back" size={17} />
                {invitedSpace ? 'Open another space' : 'All spaces'}
              </button>
              {!invitedSpace && (
                <button className="atlas-icon-button" aria-label="Share space" onClick={share}>
                  <Icon name="share" size={18} />
                </button>
              )}
            </div>
            {!activeDetail ? (
              <div className="atlas-empty">Loading this space…</div>
            ) : (
              <>
                <span className={`atlas-category ${activeDetail.space.category}`}>
                  <span />
                  {CATEGORY_LABEL[activeDetail.space.category]}
                </span>
                <h2>{activeDetail.space.title}</h2>
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
                      ['coverage', 'Coverage'],
                      ['world', '3D atlas'],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      className={tab === value ? 'is-active' : ''}
                      aria-pressed={tab === value}
                      onClick={() => setTab(value)}
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
                      disabled={!name.trim()}
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
                    {activeDetail.captures.length === 0 ? (
                      <div className="atlas-empty">
                        <Icon name="camera" size={32} />
                        <h3>The first view is yours.</h3>
                        <p>Add a photo, record a short video, or walk through a 360 scan.</p>
                      </div>
                    ) : (
                      <div className="atlas-capture-grid">
                        {activeDetail.captures.slice(0, 24).map((capture) => (
                          <MediaCard
                            key={capture.id}
                            capture={capture}
                            client={client!}
                            spaceId={selected}
                          />
                        ))}
                      </div>
                    )}
                    {activeDetail.captures.length > 24 && (
                      <p className="atlas-fine">Showing the latest 24 captures.</p>
                    )}
                  </>
                )}
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
                        !selectedCell ||
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
                          setNotice(
                            'Viewpoint requested. Contributors can see this area in the space.',
                          )
                        })
                      }
                    >
                      <Icon name="plus" size={17} />
                      {selectedCell ? 'Request a view here' : 'Select a missing area'}
                    </button>
                    <h3 className="atlas-section-title">Viewpoint requests</h3>
                    {activeDetail.requests.length ? (
                      activeDetail.requests.map((item) => (
                        <button
                          className="atlas-request"
                          key={item.cell_id}
                          onClick={() => setSelectedCell(item.cell_id)}
                        >
                          <Icon name={item.status === 'captured' ? 'check' : 'target'} />
                          <div>
                            <strong>
                              {item.status === 'captured'
                                ? 'New view received'
                                : 'Another perspective needed'}
                            </strong>
                            <span>{item.note}</span>
                          </div>
                        </button>
                      ))
                    ) : (
                      <p className="atlas-muted">
                        No requests yet. Choose a gap to invite a contribution.
                      </p>
                    )}
                  </>
                )}
                {tab === 'world' && (
                  <ReconstructionPanel
                    job={activeDetail.reconstruction}
                    client={client!}
                    spaceId={selected}
                    canBuild={!invitedSpace}
                    onChange={refresh}
                  />
                )}
                <div className="atlas-detail-footer">
                  <button
                    className="atlas-primary atlas-full"
                    disabled={activeDetail.space.status !== 'active'}
                    onClick={() => setModal('capture')}
                  >
                    <Icon name="camera" />
                    Add a capture
                    <Icon name="plus" size={17} />
                  </button>
                  {!invitedSpace && (
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
        {!selected && !creating && (
          <div className="atlas-map-caption">
            <span className="atlas-eyebrow">A WORLD BUILT TOGETHER</span>
            <p>One place. Every perspective.</p>
          </div>
        )}
        {notice && (
          <div className="atlas-toast" role="status">
            <Icon name="spaces" size={18} />
            <span>{notice}</span>
            <button aria-label="Dismiss notice" onClick={() => setNotice('')}>
              <Icon name="close" size={16} />
            </button>
          </div>
        )}
      </div>
      {modal && (
        <AtlasDialog
          title={
            modal === 'capture'
              ? 'Add your perspective'
              : modal === 'connect'
                ? 'Connect your workspace'
                : 'Invite a contributor'
          }
          onClose={() => setModal(null)}
        >
          {modal === 'capture' && client && selected && (
            <CaptureComposer
              client={client}
              spaceId={selected}
              contributor={contributor}
              name={name.trim() || 'Contributor'}
              onSaved={refresh}
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
                if (space) openSpace(space)
                refresh()
              }}
            />
          )}
          {modal === 'share' && (
            <>
              <p className="atlas-muted">
                This invitation grants access to this space’s captures and contributions. It does
                not grant fleet controls.
              </p>
              <textarea
                className="atlas-invite"
                aria-label="Space invitation"
                readOnly
                value={invitation}
                rows={6}
              />
              <button
                className="atlas-primary atlas-full"
                onClick={() =>
                  void navigator.clipboard
                    .writeText(invitation)
                    .then(() => setNotice('Invitation copied.'))
                    .catch(() => setNotice('Select and copy the invitation text.'))
                }
              >
                <Icon name="share" />
                Copy invitation
              </button>
            </>
          )}
        </AtlasDialog>
      )}
    </main>
  )
}

function SpaceCard({ space, onOpen }: { space: Space; onOpen: () => void }) {
  return (
    <button className="atlas-space-card" onClick={onOpen}>
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
      <div className="atlas-card-bottom">
        <span>
          <Icon name="camera" size={14} />
          {space.capture_count} captures
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

function MediaCard({
  capture,
  client,
  spaceId,
}: {
  capture: Capture
  client: AtlasClient
  spaceId: string
}) {
  const [url, setUrl] = useState('')
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
        {age(capture.captured_at)} · {capture.source === 'camera' ? 'Phone capture' : 'Imported'}
      </span>
      <span>
        {capture.position
          ? `GPS ±${Math.round(capture.position.accuracy)} m`
          : 'No capture location'}
      </span>
    </article>
  )
}

function AtlasDialog({
  title,
  children,
  onClose,
}: {
  title: string
  children: ReactNode
  onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    dialog.current?.showModal()
  }, [])
  return (
    <dialog
      ref={dialog}
      className="atlas-dialog"
      aria-labelledby="atlas-dialog-title"
      onCancel={onClose}
    >
      <div className="atlas-dialog-header">
        <div>
          <span className="atlas-eyebrow">SWEEP ATLAS</span>
          <h2 id="atlas-dialog-title">{title}</h2>
        </div>
        <button className="atlas-icon-button" aria-label="Close dialog" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      {children}
    </dialog>
  )
}

function ConnectForm({ onConnect }: { onConnect: (client: AtlasClient, space?: string) => void }) {
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
              const invited = new AtlasClient({
                baseUrl: invite.relay,
                sessionId: invite.workspace,
                token: invite.token,
              })
              await invited.detail(invite.space)
              onConnect(invited, invite.space)
            } else {
              const connection = new AtlasClient({
                baseUrl: relay.replace(/^http/, 'ws'),
                sessionId: workspace,
                token,
              })
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
        Use your workspace connection or paste a space invitation. Credentials stay in memory for
        this visit.
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

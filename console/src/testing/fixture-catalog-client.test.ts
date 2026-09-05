import { describe, expect, test } from 'vitest'
import {
  CATALOG_UNREPORTED_REASON,
  UNREPORTED_CATALOG,
  UnreportedCatalogClient,
  type CatalogClient,
} from '../catalog/client'
import type { CatalogSnapshot } from '../catalog/types'
import { FIXTURE_JOB_CHAIN, FixtureCatalogClient, manualScheduler } from './fixture-relay-client'

const clock = () => 1_756_700_000_000
const DISCONNECTED = 'Fixture relay is disconnected; nothing was sent.'

function record(client: CatalogClient): CatalogSnapshot[] {
  const snapshots: CatalogSnapshot[] = []
  client.subscribe((snapshot) => snapshots.push(snapshot))
  return snapshots
}

function field(client: FixtureCatalogClient, groupId: string, key: string): string | undefined {
  return client.current.config?.groups
    .find((group) => group.group_id === groupId)
    ?.fields.find((item) => item.key === key)?.value
}

describe('fixture catalog client', () => {
  test('the control scenario reports every surface present but empty', () => {
    const client = new FixtureCatalogClient(4, clock)
    const snapshots = record(client)
    client.start()

    expect(snapshots).toHaveLength(1)
    expect(snapshots[0]).toMatchObject({ captures: [], jobs: [], services: [], metrics: [], nodes: {} })
    expect(snapshots[0].building?.rooms).toEqual([])
    expect(snapshots[0].building?.floor_plan).toBeNull()
    expect(snapshots[0].config).toEqual({ groups: [], staged_changes: [], modes: [] })
  })

  test('the design scenarios carry the design tables with public false on every job', () => {
    const client = new FixtureCatalogClient('six6', clock)
    const snapshot = client.current

    expect(snapshot.captures?.map((capture) => capture.capture_id)).toEqual([
      'cap-0147',
      'cap-0146',
      'cap-0142',
      'cap-0139',
    ])
    expect(snapshot.captures?.filter((capture) => capture.needs_retake).map((c) => c.capture_id)).toEqual([
      'cap-0146',
    ])
    expect(snapshot.building?.rooms.map((room) => room.room_id)).toEqual([
      'kitchen-01',
      'hall-02',
      'studio-03',
      'stair-04',
    ])
    expect(snapshot.jobs?.map((job) => job.state)).toEqual([
      'succeeded',
      'running',
      'failed',
      'timed_out',
      'queued',
      'uploading',
      'draft',
    ])
    expect(snapshot.jobs?.every((job) => job.public === false)).toBe(true)
    expect(Object.keys(snapshot.nodes ?? {})).toHaveLength(6)
    expect(snapshot.nodes?.[5]).toMatchObject({ rtt_ms: null, telemetry_rate_hz: 0 })
    expect(snapshot.metrics).toHaveLength(9)
    expect(snapshot.services?.map((service) => service.service_id)).toEqual([
      'media_server',
      'world_api',
      'storage',
    ])
    expect(snapshot.config?.groups).toHaveLength(7)
    expect(snapshot.config?.groups.filter((group) => group.staged).map((g) => g.group_id)).toEqual([
      'thresholds',
      'connection',
    ])
    expect(snapshot.config?.modes.map((mode) => mode.status)).toEqual([
      'accepted',
      'unsupported',
      'unsupported',
    ])
  })

  test('submit runs the job chain on the injected scheduler with a fresh operation id', async () => {
    const scheduler = manualScheduler()
    const client = new FixtureCatalogClient('pending4', clock, scheduler.schedule)
    const snapshots = record(client)
    client.start()
    const bundle = { kind: 'reconstruct_8' as const, capture_id: 'cap-0139' }

    await client.submitGeneration('studio-03', bundle)
    const job = () => client.current.jobs?.find((item) => item.room_id === 'studio-03')
    expect(job()).toMatchObject({
      state: 'uploading',
      operation_id: 'op_8f3395',
      world_id: null,
      model: 'world-gen-1',
      updated_at: clock(),
      assets: '8 frames, 0 of 8 sent',
      public: false,
      bundle,
    })
    expect(scheduler.pending()).toBe(FIXTURE_JOB_CHAIN.length)

    scheduler.advance(1_400)
    expect(job()).toMatchObject({ state: 'queued', assets: '8 frames uploaded' })
    scheduler.advance(1_400)
    expect(job()?.state).toBe('running')
    scheduler.advance(3_600)
    expect(job()).toMatchObject({
      state: 'succeeded',
      world_id: 'wld_7803',
      assets: '8 frames, 1 mesh, 1 preview',
    })
    expect(scheduler.pending()).toBe(0)
    expect(snapshots).toHaveLength(5)
  })

  test('a second submit for the same room supersedes the first chain', async () => {
    const scheduler = manualScheduler()
    const client = new FixtureCatalogClient('pending4', clock, scheduler.schedule)
    const manual = { kind: 'manual_phone' as const, capture_id: null }

    await client.submitGeneration('corridor-07', manual)
    await client.submitGeneration('corridor-07', manual)
    scheduler.advance(6_400)
    const job = client.current.jobs?.find((item) => item.room_id === 'corridor-07')
    expect(job).toMatchObject({ state: 'succeeded', operation_id: 'op_8f3568', world_id: 'wld_7864' })
    expect(client.current.jobs?.filter((item) => item.room_id === 'corridor-07')).toHaveLength(1)
  })

  test('stop cancels a running chain so nothing advances after unmount', async () => {
    const scheduler = manualScheduler()
    const client = new FixtureCatalogClient('pending4', clock, scheduler.schedule)
    await client.submitGeneration('kitchen-01', { kind: 'pano_360', capture_id: 'cap-0147' })
    client.stop()
    scheduler.advance(10_000)
    expect(client.current.jobs?.find((item) => item.room_id === 'kitchen-01')?.state).toBe('uploading')
  })

  test('retry reuses the recorded bundle and refuses jobs that are not failed or timed out', async () => {
    const scheduler = manualScheduler()
    const client = new FixtureCatalogClient('pending4', clock, scheduler.schedule)

    await client.retryGeneration('stair-04')
    expect(client.current.jobs?.find((item) => item.room_id === 'stair-04')).toMatchObject({
      state: 'uploading',
      bundle: { kind: 'pano_360', capture_id: null },
      assets: '1 pano, 0 of 1 sent',
    })
    await expect(client.retryGeneration('kitchen-01')).rejects.toThrow(
      'No failed or timed-out job exists for kitchen-01; nothing was submitted.',
    )
    await expect(client.retryGeneration('corridor-07')).rejects.toThrow(
      'No failed or timed-out job exists for corridor-07; nothing was submitted.',
    )
  })

  test('download and export answer with the design sentences and refuse unknown captures', async () => {
    const client = new FixtureCatalogClient('pending4', clock)
    expect(await client.stageCaptureSet('cap-0146')).toBe(
      "cap-0146 — 8 files staged to session/captures, checksum sha256:1ba07c39…44e2 verified against the aircraft's manifest.",
    )
    expect(await client.exportCaptureMetadata('cap-0146')).toBe(
      'cap-0146.json exported: pattern, coverage label, pose, camera intrinsics, quality results and checksums. Media files are not re-encoded.',
    )
    await expect(client.stageCaptureSet('cap-9999')).rejects.toThrow(
      'No capture cap-9999 is catalogued; nothing was staged.',
    )
    await expect(client.exportCaptureMetadata('cap-9999')).rejects.toThrow(
      'No capture cap-9999 is catalogued; nothing was exported.',
    )
  })

  test('apply changes the live value; stage records it beside an unchanged value', async () => {
    const client = new FixtureCatalogClient('pending4', clock)
    const snapshots = record(client)

    await client.applyConfig('camera', { exposure: 'manual' })
    expect(field(client, 'camera', 'exposure')).toBe('manual')
    expect(field(client, 'camera', 'frame_format')).toBe('jpeg')

    await client.stageConfig('thresholds', { battery_reserve: '30%' })
    expect(field(client, 'thresholds', 'battery_reserve')).toBe('28%')
    expect(client.current.config?.staged_changes).toEqual([
      { group_id: 'thresholds', key: 'battery_reserve', value: '30%' },
    ])
    await client.stageConfig('thresholds', { battery_reserve: '32%', ceiling: '2.2 m' })
    expect(client.current.config?.staged_changes).toEqual([
      { group_id: 'thresholds', key: 'battery_reserve', value: '32%' },
      { group_id: 'thresholds', key: 'ceiling', value: '2.2 m' },
    ])
    expect(snapshots).toHaveLength(3)

    await expect(client.applyConfig('missing', {})).rejects.toThrow(
      'No configuration group missing is reported; nothing was changed.',
    )
  })

  test('manual phone photos accumulate per room and refuse unknown rooms', async () => {
    const client = new FixtureCatalogClient('pending4', clock)
    const photos = (roomId: string) =>
      client.current.building?.rooms.find((room) => room.room_id === roomId)?.manual_photos

    await client.addManualPhotos('kitchen-01', 2)
    expect(photos('kitchen-01')).toBe(2)
    await client.addManualPhotos('kitchen-01', 1)
    expect(photos('kitchen-01')).toBe(3)
    expect(photos('stair-04')).toBe(3)
    await expect(client.addManualPhotos('vault-09', 1)).rejects.toThrow(
      'No room vault-09 is catalogued; no photos were added.',
    )
  })

  test('the down scenario keeps its snapshot and refuses every action', async () => {
    const client = new FixtureCatalogClient('down', clock)
    expect(client.current.captures).toHaveLength(4)
    expect(client.current.jobs).toHaveLength(7)

    await expect(client.stageCaptureSet('cap-0147')).rejects.toThrow(DISCONNECTED)
    await expect(client.exportCaptureMetadata('cap-0147')).rejects.toThrow(DISCONNECTED)
    await expect(
      client.submitGeneration('kitchen-01', { kind: 'pano_360', capture_id: 'cap-0147' }),
    ).rejects.toThrow(DISCONNECTED)
    await expect(client.retryGeneration('studio-03')).rejects.toThrow(DISCONNECTED)
    await expect(client.addManualPhotos('kitchen-01', 1)).rejects.toThrow(DISCONNECTED)
    await expect(client.applyConfig('camera', { exposure: 'manual' })).rejects.toThrow(DISCONNECTED)
    await expect(client.stageConfig('thresholds', { ceiling: '2 m' })).rejects.toThrow(DISCONNECTED)
    expect(client.current.jobs?.find((item) => item.room_id === 'studio-03')?.state).toBe('failed')
  })
})

describe('unreported catalog client', () => {
  test('reports every surface null and refuses every action with the reason', async () => {
    const client: CatalogClient = new UnreportedCatalogClient()
    const snapshots = record(client)
    client.start()

    expect(snapshots).toEqual([UNREPORTED_CATALOG])
    expect(Object.values(UNREPORTED_CATALOG).every((surface) => surface === null)).toBe(true)
    await expect(
      client.submitGeneration('kitchen-01', { kind: 'pano_360', capture_id: 'cap-0147' }),
    ).rejects.toThrow(CATALOG_UNREPORTED_REASON)
    await expect(client.retryGeneration('kitchen-01')).rejects.toThrow(CATALOG_UNREPORTED_REASON)
    await expect(client.addManualPhotos('kitchen-01', 1)).rejects.toThrow(CATALOG_UNREPORTED_REASON)
    await expect(client.stageCaptureSet('cap-0147')).rejects.toThrow(CATALOG_UNREPORTED_REASON)
    await expect(client.exportCaptureMetadata('cap-0147')).rejects.toThrow(CATALOG_UNREPORTED_REASON)
    await expect(client.applyConfig('camera', {})).rejects.toThrow(CATALOG_UNREPORTED_REASON)
    await expect(client.stageConfig('thresholds', {})).rejects.toThrow(CATALOG_UNREPORTED_REASON)
  })
})

import { describe, expect, test } from 'vitest'
import { createInitialControlState, type ControlState } from '../control/state'
import type { RelayAircraftState } from '../relay/contract'
import { fixtureAircraft } from '../testing/fixture-relay-client'
import {
  VOCAB,
  bundleImages,
  bundleLabel,
  bundleOptions,
  captureFilters,
  configSemantics,
  deriveCatalogLink,
  formatPose,
  formatSigned,
  groupCapturesByProject,
  ladderRungs,
  ladderSentence,
  liveServices,
  nodeCells,
  nodeError,
  submitBlockedReason,
  vocabNote,
  vocabTone,
} from './derive'
import type { CaptureRecord, GenerationJob, NodeRecord } from './types'

const now = 1_756_700_000_000

function capture(overrides: Partial<CaptureRecord>): CaptureRecord {
  return {
    capture_id: 'cap-0001',
    project: 'ground-floor',
    room_id: 'kitchen-01',
    drone_id: 1,
    pattern: 'pano_360',
    coverage: 'full_equirectangular',
    files: 1,
    captured_at: now,
    quality: 'pass',
    needs_retake: false,
    checksum: null,
    pose: null,
    ...overrides,
  }
}

function state(overrides: Partial<ControlState> = {}): ControlState {
  const base = createInitialControlState('derive-session', now)
  return {
    ...base,
    connection: { status: 'connected', transport: 'fixture', changedAt: now },
    keyboardConnection: { status: 'connected', transport: 'fixture', changedAt: now },
    ...overrides,
  }
}

function drone(overrides: Partial<RelayAircraftState>): RelayAircraftState {
  return { ...fixtureAircraft(now)[0], ...overrides }
}

const node: NodeRecord = {
  drone_id: 1,
  rc_firmware: '2.4.1',
  aircraft_firmware: '0.9.7',
  phone_model: 'Pixel 7a',
  sdk_release: '1.3.0',
  rtt_ms: 18,
  telemetry_rate_hz: 29.4,
  storage_free_gb: 15,
}

describe('catalog link', () => {
  test('connected is silent, degraded warns, anything else blocks', () => {
    expect(deriveCatalogLink(state(), 'X', 'y')).toEqual({ up: true, status: 'connected', notice: null })
    expect(
      deriveCatalogLink(
        state({ connection: { status: 'degraded', transport: 'fixture', changedAt: now } }),
        'The library',
        'exports are refused',
      ),
    ).toEqual({
      up: true,
      status: 'degraded',
      notice: 'The console connection is degraded. The library may lag the relay.',
    })
    expect(
      deriveCatalogLink(
        state({ connection: { status: 'connecting', transport: 'websocket', changedAt: now } }),
        'The library',
        'exports are refused',
      ),
    ).toEqual({
      up: false,
      status: 'connecting',
      notice:
        'The console connection is connecting. The library shows the last snapshot received; exports are refused until the relay reports connected.',
    })
  })
})

describe('captures', () => {
  test('formats the pose line with a typographic minus and two decimals', () => {
    expect(formatPose(null)).toBe('pose unreported')
    expect(
      formatPose({ x: 0.38, y: 1.11, z: 1.4, yaw_deg: 44.9, gimbal_pitch_deg: -10.5, focal_mm: 3.2 }),
    ).toBe('x 0.38 y 1.11 z 1.40 · yaw 44.9° · gimbal −10.5° · f 3.2 mm')
    expect(formatSigned(-12)).toBe('−12.0')
    expect(formatSigned(132.44)).toBe('132.4')
  })

  test('filters cover projects only when more than one, then rooms, aircraft and retake', () => {
    const single = captureFilters([
      capture({ capture_id: 'a', room_id: 'hall-02', drone_id: 2 }),
      capture({ capture_id: 'b', room_id: 'kitchen-01', drone_id: 1, needs_retake: true }),
    ])
    expect(single.map((filter) => filter.label)).toEqual([
      'All captures',
      'hall-02',
      'kitchen-01',
      'D-01',
      'D-02',
      'Needs retake',
    ])
    expect(single.find((f) => f.id === 'retake')?.test(capture({ needs_retake: true }))).toBe(true)

    const multi = captureFilters([
      capture({ capture_id: 'a', project: 'annex' }),
      capture({ capture_id: 'b', project: 'ground-floor' }),
    ])
    expect(multi.slice(1, 3).map((filter) => filter.label)).toEqual(['project annex', 'project ground-floor'])
  })

  test('groups by project in first-seen order and sorts newest first inside each', () => {
    const groups = groupCapturesByProject([
      capture({ capture_id: 'old', project: 'annex', captured_at: now - 2 }),
      capture({ capture_id: 'gf', project: 'ground-floor', captured_at: now - 1 }),
      capture({ capture_id: 'new', project: 'annex', captured_at: now }),
    ])
    expect(groups.map((group) => group.project)).toEqual(['annex', 'ground-floor'])
    expect(groups[0].captures.map((item) => item.capture_id)).toEqual(['new', 'old'])
  })
})

describe('worlds', () => {
  test('bundle options list the room captures in catalog order, then the manual fallback', () => {
    const options = bundleOptions(
      [
        capture({ capture_id: 'cap-1', room_id: 'kitchen-01', pattern: 'pano_360' }),
        capture({ capture_id: 'cap-2', room_id: 'hall-02' }),
        capture({ capture_id: 'cap-3', room_id: 'kitchen-01', pattern: 'reconstruct_8' }),
      ],
      'kitchen-01',
    )
    expect(options).toEqual([
      { kind: 'pano_360', capture_id: 'cap-1' },
      { kind: 'reconstruct_8', capture_id: 'cap-3' },
      { kind: 'manual_phone', capture_id: null },
    ])
    expect(options.map((option) => bundleLabel(option, 2))).toEqual([
      'cap-1 · pano_360',
      'cap-3 · reconstruct_8',
      'manual phone fallback · 2 photos',
    ])
    expect(options.map((option) => bundleImages(option, 2))).toEqual([1, 8, 2])
  })

  test('submit is blocked in order: link, manual photo count, active job', () => {
    const link = deriveCatalogLink(state(), 'World Builder', 'submissions are refused')
    const down = deriveCatalogLink(
      state({ connection: { status: 'disconnected', transport: 'fixture', changedAt: now } }),
      'World Builder',
      'submissions are refused',
    )
    const manual = { kind: 'manual_phone' as const, capture_id: null }
    const pano = { kind: 'pano_360' as const, capture_id: 'cap-1' }
    const running: GenerationJob = {
      room_id: 'kitchen-01',
      state: 'running',
      operation_id: 'op_1',
      world_id: null,
      model: 'world-gen-1',
      updated_at: now,
      assets: '',
      public: false,
      bundle: pano,
    }
    expect(submitBlockedReason(down, manual, 0, running)).toBe(
      'The console connection is disconnected. Nothing can be submitted.',
    )
    expect(submitBlockedReason(link, manual, 2, undefined)).toBe(
      'Manual fallback needs exactly 3 overlapping phone photos; 2 added.',
    )
    expect(submitBlockedReason(link, manual, 3, running)).toBe(
      'A job for this room is already running. Wait for it to finish or fail.',
    )
    expect(submitBlockedReason(link, pano, 0, { ...running, state: 'failed' })).toBeNull()
    expect(submitBlockedReason(link, manual, 3, undefined)).toBeNull()
  })
})

describe('connectivity', () => {
  test('node cells read relay facts and unreported catalog fields honestly', () => {
    const cells = nodeCells(drone({ camera_patterns: ['pano_360'] }), null, now)
    expect(cells.map((cell) => cell.key)).toEqual([
      'RC controller',
      'Android bridge',
      'LAN',
      'Relay',
      'Telemetry',
      'Camera',
      'Video',
      'Storage',
      'Firmware',
    ])
    expect(cells.map((cell) => cell.value)).toEqual([
      'standby · fw unreported',
      'unreported',
      'unreported',
      'connected',
      'unreported',
      'ready · 1 pattern',
      'live · just now',
      'unreported',
      'unreported',
    ])
    expect(cells.map((cell) => cell.tone)).toEqual([
      'ink',
      'muted',
      'muted',
      'ok',
      'muted',
      'ink',
      'ok',
      'muted',
      'muted',
    ])
  })

  test('node cells with a record: versions, rtt, rate, storage, and the stale and down variants', () => {
    const healthy = nodeCells(drone({}), node, now)
    expect(healthy.map((cell) => cell.value)).toEqual([
      'standby · fw 2.4.1',
      'Pixel 7a · sdk 1.3.0',
      '18 ms',
      'connected',
      '29.4 Hz',
      'ready · 2 patterns',
      'live · just now',
      '15 GB free',
      'aircraft 0.9.7',
    ])
    const slow = nodeCells(drone({}), { ...node, rtt_ms: 90 }, now)
    expect(slow[2]).toEqual({ key: 'LAN', value: '90 ms', tone: 'warn' })

    const stale = nodeCells(
      drone({ readiness_reasons: ['telemetry_stale'], last_seen_at: now - 9_400, video: undefined }),
      node,
      now,
    )
    expect(stale[4]).toEqual({ key: 'Telemetry', value: 'stale 9 s ago', tone: 'warn' })
    expect(stale[6]).toEqual({ key: 'Video', value: 'unreported', tone: 'muted' })

    const down = nodeCells(
      drone({ membership: 'disconnected', control_authority: false, camera_patterns: [] }),
      { ...node, rtt_ms: null },
      now,
    )
    expect(down.map((cell) => cell.value)).toEqual([
      'in control · fw 2.4.1',
      'down',
      'no route',
      'disconnected',
      '29.4 Hz',
      'not ready',
      'live · just now',
      'unknown',
      'aircraft 0.9.7',
    ])
    expect(down.map((cell) => cell.tone)).toEqual([
      'danger',
      'danger',
      'danger',
      'danger',
      'ink',
      'danger',
      'ok',
      'ink',
      'ink',
    ])
  })

  test('the error line names what is wrong, disconnected first', () => {
    expect(nodeError(drone({}))).toBeNull()
    expect(nodeError(drone({ membership: 'disconnected', control_authority: false }))).toMatch(
      /^Adapter connection lost/,
    )
    expect(nodeError(drone({ readiness_reasons: ['telemetry_stale'], control_authority: false }))).toMatch(
      /^Telemetry stopped/,
    )
    expect(nodeError(drone({ control_authority: false }))).toMatch(/^The RC pilot holds authority/)
  })

  test('the ladder marks the rung the sockets and video can prove', () => {
    const fleet = Object.fromEntries(fixtureAircraft(now).map((item) => [item.drone_id, item]))
    const live = state({ aircraft: fleet })
    expect(ladderRungs(live).find((rung) => rung.current)?.label).toBe('full')
    expect(ladderSentence(live, ladderRungs(live))).toBe('Current rung: full.')

    const noVideo = state({
      aircraft: Object.fromEntries(
        Object.values(fleet).map((item) => [item.drone_id, { ...item, video: undefined }]),
      ),
    })
    expect(ladderRungs(noVideo).find((rung) => rung.current)?.label).toBe('no video')

    const keyboardOnly = state({
      aircraft: fleet,
      connection: { status: 'disconnected', transport: 'fixture', changedAt: now },
    })
    expect(ladderRungs(keyboardOnly).find((rung) => rung.current)?.label).toBe('keyboard stop only')

    const none = state({
      connection: { status: 'disconnected', transport: 'fixture', changedAt: now },
      keyboardConnection: { status: 'disconnected', transport: 'fixture', changedAt: now },
    })
    expect(ladderRungs(none).some((rung) => rung.current)).toBe(false)
    expect(ladderSentence(none, ladderRungs(none))).toBe(
      'Both sockets are disconnected; no rung is held. The physical RC remains primary.',
    )
  })

  test('the relay and keyboard stop rows come from live connection state', () => {
    expect(liveServices(state()).map((service) => [service.status, service.tone, service.note])).toEqual([
      ['connected', 'ok', 'Two sockets authenticated: console and keyboard.'],
      ['connected', 'ok', 'Carries Shift+Escape only.'],
    ])
    const half = state({
      keyboardConnection: {
        status: 'disconnected',
        transport: 'unavailable',
        changedAt: now,
        reason: 'Keyboard socket closed.',
      },
    })
    expect(liveServices(half).map((service) => [service.status, service.tone, service.note])).toEqual([
      ['connected', 'ok', 'Console socket authenticated; the keyboard socket is disconnected.'],
      ['disconnected', 'danger', 'Keyboard socket closed.'],
    ])
    const degraded = state({
      connection: { status: 'degraded', transport: 'websocket', changedAt: now, reason: 'Heartbeat late.' },
    })
    expect(liveServices(degraded)[0]).toMatchObject({ status: 'degraded', tone: 'warn', note: 'Heartbeat late.' })
  })
})

describe('configuration and gallery', () => {
  test('apply-now versus staged semantics', () => {
    const live = configSemantics({ group_id: 'camera', title: 'Camera', staged: false, fields: [] })
    expect(live).toEqual({ word: 'live', sentence: 'Applies now.', action: 'Save', tone: 'ok' })
    const staged = configSemantics({ group_id: 'thresholds', title: 'Thresholds', staged: true, fields: [] })
    expect(staged).toEqual({
      word: 'pending until the next run',
      sentence: 'Safety-sensitive. Staged and applied between runs.',
      action: 'Stage for the next run',
      tone: 'warn',
    })
  })

  test('the vocabulary has thirteen domains and the design colour key', () => {
    expect(VOCAB).toHaveLength(13)
    expect(VOCAB.reduce((count, domain) => count + domain.values.length, 0)).toBe(62)
    expect(['live', 'completed', 'succeeded', 'ready', 'pass', 'connected'].map(vocabTone)).toEqual(
      Array(6).fill('ok'),
    )
    expect(['offline', 'degraded', 'leaving', 'queued', 'uploading', 'running'].map(vocabTone)).toEqual(
      Array(6).fill('warn'),
    )
    expect(['unreported', 'draft'].map(vocabTone)).toEqual(['muted', 'muted'])
    expect(['disconnected', 'failed', 'timed_out', 'refused'].map(vocabTone)).toEqual(Array(4).fill('danger'))
    expect(vocabTone('hovering')).toBe('ink')
    expect(vocabNote('outdoorC')).toBe('unsupported')
    expect(vocabNote('indoor')).toBe('')
  })
})

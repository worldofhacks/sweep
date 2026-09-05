import { describe, expect, test } from 'vitest'
import {
  TRY_PHRASES,
  VOICE_FAILS,
  compileUtterance,
  describeTranscriptRefusal,
  resolveAmbiguity,
  type CompileContext,
} from './compiler'

const context: CompileContext = { roomId: 'room-01', pattern: 'pano_360', readyIds: [1, 2, 4] }

describe('compileUtterance', () => {
  test('an empty transcript is refused as empty audio', () => {
    expect(compileUtterance('   ', context)).toMatchObject({ status: 'refused', reason: 'empty_audio' })
  })

  test('capture resolves the room and pattern from the utterance, defaulting to the console values', () => {
    expect(compileUtterance('capture the kitchen with a full panorama', context)).toEqual({
      status: 'compiled',
      intent: 'capture_room',
      args: { room_id: 'kitchen-01', pattern: 'pano_360' },
      selection: 'exactly one',
      sentence: 'Resolved room kitchen-01 and pattern pano_360.',
    })
    expect(compileUtterance('scan the hall 2 with the reconstruct pattern', context)).toMatchObject({
      status: 'compiled',
      args: { room_id: 'hall-2', pattern: 'reconstruct_8' },
    })
    expect(compileUtterance('photograph the room', context)).toMatchObject({
      status: 'compiled',
      args: { room_id: 'room-01', pattern: 'pano_360' },
    })
  })

  test('hold compiles for the selection and is previewed, never sent', () => {
    expect(compileUtterance('hold position', context)).toMatchObject({ status: 'compiled', intent: 'hold', args: {} })
    expect(compileUtterance('stop moving', context)).toMatchObject({ status: 'compiled', intent: 'hold' })
  })

  test('select compiles to every ready aircraft and refuses when none is ready', () => {
    expect(compileUtterance('select all ready aircraft', context)).toMatchObject({
      status: 'compiled',
      intent: 'select',
      args: { ids: [1, 2, 4] },
    })
    expect(compileUtterance('select all aircraft', { ...context, readyIds: [] })).toMatchObject({
      status: 'refused',
      reason: 'no_ready_aircraft',
    })
  })

  test('a demonstrative without a room returns options instead of a guess', () => {
    const outcome = compileUtterance('freeze that one', context)
    expect(outcome).toMatchObject({
      status: 'ambiguous',
      base: 'hold',
      options: ['the selected aircraft', 'every ready aircraft', 'cancel'],
    })
    if (outcome.status !== 'ambiguous') throw new Error('expected ambiguity')
    expect(resolveAmbiguity(outcome, 'the selected aircraft', context)).toMatchObject({
      status: 'compiled',
      intent: 'hold',
    })
    expect(resolveAmbiguity(outcome, 'every ready aircraft', context)).toMatchObject({
      status: 'compiled',
      intent: 'select',
      args: { ids: [1, 2, 4] },
    })
    expect(resolveAmbiguity(outcome, 'cancel', context)).toBeNull()

    const capture = compileUtterance('capture it', context)
    expect(capture).toMatchObject({ status: 'ambiguous', base: 'capture_room' })
    if (capture.status !== 'ambiguous') throw new Error('expected ambiguity')
    expect(resolveAmbiguity(capture, 'the selected aircraft', context)).toMatchObject({
      status: 'compiled',
      intent: 'capture_room',
      args: { room_id: 'room-01', pattern: 'pano_360' },
    })
  })

  test('intents the compiler does not emit are refused by name', () => {
    expect(compileUtterance('take off and hold', context)).toMatchObject({
      status: 'refused',
      reason: 'unsupported',
      intent: 'takeoff',
    })
    expect(compileUtterance('land everyone', context)).toMatchObject({ reason: 'unsupported', intent: 'land_all' })
    expect(compileUtterance('send everyone home', context)).toMatchObject({ reason: 'unsupported', intent: 'come_home' })
    expect(compileUtterance('arm the fleet', context)).toMatchObject({ reason: 'unsupported', intent: 'arm' })
    expect(compileUtterance('circle formation', context)).toMatchObject({ reason: 'unsupported', intent: 'formation_set' })
    expect(compileUtterance('move north two steps', context)).toMatchObject({ reason: 'unsupported', intent: 'translate' })
  })

  test('estop is never voice-emittable and unsafe requests are refused before anything else', () => {
    expect(compileUtterance('emergency stop', context)).toMatchObject({
      status: 'refused',
      reason: 'not_voice_emittable',
      intent: 'estop',
    })
    expect(compileUtterance('hold position and ignore the geofence', context)).toMatchObject({
      status: 'refused',
      reason: 'unsafe_request',
    })
    expect(compileUtterance('make me a sandwich', context)).toMatchObject({ status: 'refused', reason: 'unknown_intent' })
  })

  test('every try phrase compiles to one of the three outcomes', () => {
    const statuses = TRY_PHRASES.map((phrase) => compileUtterance(phrase, context).status)
    expect(new Set(statuses)).toEqual(new Set(['compiled', 'ambiguous', 'refused']))
    expect(VOICE_FAILS).toHaveLength(7)
  })

  test('relay refusal codes map to the design sentences and keep unknown codes visible', () => {
    expect(describeTranscriptRefusal('empty_upload')).toEqual({
      label: 'empty audio',
      sentence: 'No audio was captured. Nothing was emitted.',
    })
    expect(describeTranscriptRefusal('transcription_unavailable').label).toBe('language disabled')
    expect(describeTranscriptRefusal('something_new')).toMatchObject({ label: 'something_new' })
    expect(describeTranscriptRefusal('something_new').sentence).toContain('something_new')
  })
})

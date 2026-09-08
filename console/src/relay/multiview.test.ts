import { expect, test, vi } from 'vitest'
import { PlatformHttp } from '../platform/http'
import { HttpMultiviewClient, type MultiviewPreviewRequest } from './multiview'

const request: MultiviewPreviewRequest = { intentId: 'photos-1', selected: [{ id: 1, deviceClass: 'aircraft', epoch: 4 }],
  viewpoints: ['lobby', 'atrium'].map((zoneId, index) => ({ viewpointId: `view-${index}`, zoneId, captureId: `capture-${index}` })) }
const pin = { version: 'v1', contentSha256: 'a'.repeat(64) }
function response(input = request) {
  return { previewId: `preview-${input.intentId}`, intentId: input.intentId, previewHash: 'b'.repeat(64), serverNowMs: 1000, expiresAt: 11000,
    execution: { planHash: 'c'.repeat(64), mapPin: pin, geometryPin: pin, navigationPin: pin, approvalId: 'approval', configurationSha256: 'd'.repeat(64), permissionZoneIds: ['atrium', 'lobby'] },
    views: input.viewpoints.map((view, index) => {
      const start = { xM: index, yM: 0, zM: 1, floorId: 'floor', frame: 'world' }
      const end = { ...start, xM: index + 1 }
      return { ...view, route: { target: input.selected[0], waypoints: [start, end], arrivalSlot: { slotId: view.zoneId, zoneId: view.zoneId, position: end }, holdBehavior: 'hover' }, capture: { roomId: view.zoneId, pattern: 'single_still' } }
    }) }
}
function setup() {
  let time = 2000
  const http = new PlatformHttp({ baseUrl: 'ws://127.0.0.1:8001', sessionId: 'test', token: 'fixture-token' })
  const call = vi.spyOn(http, 'request').mockImplementation(async (path, body) => {
    if (path === '/multiview/preview') { time += 100; return response(body as MultiviewPreviewRequest) }
    return { workflowId: 'preview-photos-1', status: 'accepted', code: 'multiview_accepted' }
  })
  return { call, client: new HttpMultiviewClient(http, () => time), setTime: (value: number) => { time = value } }
}

test('confirms the exact reviewed photo route once and charges network delay against expiry', async () => {
  const { call, client } = setup()
  const preview = await client.preview(request)
  expect(preview.expiresAt).toBe(12000)
  expect(call).toHaveBeenCalledTimes(1)
  expect(await client.confirm(preview)).toBe(preview.previewId)
  expect(call).toHaveBeenLastCalledWith('/multiview/confirm', { previewId: preview.previewId, intentId: request.intentId, previewHash: 'b'.repeat(64) })
  await expect(client.confirm(preview)).rejects.toThrow('Preview')
  expect(call).toHaveBeenCalledTimes(2)
})

test.each(['epoch', 'photo', 'position', 'deadline'] as const)('refuses a changed %s in the relay photo-route review', async (mutation) => {
  const { call, client } = setup()
  const raw = response()
  if (mutation === 'epoch') raw.views[0].route.target = { ...request.selected[0], epoch: 5 }
  if (mutation === 'photo') raw.views[0].captureId = 'different-photo'
  if (mutation === 'position') raw.views[0].route.waypoints[0].xM = Infinity
  if (mutation === 'deadline') raw.expiresAt = 999
  call.mockResolvedValue(raw)
  await expect(client.preview(request)).rejects.toThrow()
  expect(call).toHaveBeenCalledTimes(1)
})

test('refuses expired or browser-mutated reviews without sending confirmation', async () => {
  const { call, client, setTime } = setup()
  const preview = await client.preview(request)
  await expect(client.confirm({ ...preview, previewHash: 'e'.repeat(64) })).rejects.toThrow()
  setTime(preview.expiresAt)
  await expect(client.confirm(preview)).rejects.toThrow()
  expect(call).toHaveBeenCalledTimes(1)
})

test('an older response cannot replace the latest retained review', async () => {
  const { call, client } = setup()
  let resolve!: (raw: unknown) => void
  call.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const older = client.preview(request)
  const refused = expect(older).rejects.toThrow('newer')
  const newer = await client.preview({ ...request, intentId: 'newer' })
  resolve(response())
  await refused
  call.mockResolvedValue({ workflowId: newer.previewId, status: 'accepted', code: 'multiview_accepted' })
  await expect(client.confirm(newer)).resolves.toBe(newer.previewId)
})

test('does not display another workflow or another capture as the current status', async () => {
  const { call, client } = setup()
  const preview = await client.preview(request)
  const status = { workflowId: preview.previewId, intentId: preview.intentId, status: 'completed', views: request.viewpoints.map((view) => ({ ...view, state: 'completed', detail: 'retrieved' })) }
  call.mockResolvedValue(status)
  await expect(client.status(preview)).resolves.toEqual(status)
  call.mockResolvedValue({ ...status, intentId: 'another' })
  await expect(client.status(preview)).rejects.toThrow()
  call.mockResolvedValue({ ...status, views: [{ ...status.views[0], captureId: 'old' }, status.views[1]] })
  await expect(client.status(preview)).rejects.toThrow()
})

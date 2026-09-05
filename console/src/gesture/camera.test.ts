import { describe, expect, test, vi } from 'vitest'
import { createCameraController, type CameraDependencies, type CameraState } from './camera'

interface FakeTrack {
  kind: string
  readyState: string
  stop: ReturnType<typeof vi.fn>
  getSettings: () => { deviceId: string }
  addEventListener: (type: string, listener: () => void) => void
  removeEventListener: (type: string, listener: () => void) => void
  end: () => void
}

function fakeTrack(deviceId: string): FakeTrack {
  const listeners = new Set<() => void>()
  return {
    kind: 'video',
    readyState: 'live',
    stop: vi.fn(),
    getSettings: () => ({ deviceId }),
    addEventListener: (_type, listener) => listeners.add(listener),
    removeEventListener: (_type, listener) => listeners.delete(listener),
    end: () => listeners.forEach((listener) => listener()),
  }
}

function fakeStream(track: FakeTrack): MediaStream {
  return {
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream
}

function videoInput(deviceId: string, label = ''): MediaDeviceInfo {
  return { deviceId, label, kind: 'videoinput', groupId: 'g', toJSON: () => ({}) }
}

function harness(overrides: Partial<CameraDependencies> = {}) {
  const deviceChangeListeners = new Set<() => void>()
  let devices: MediaDeviceInfo[] = [
    videoInput('cam-1', 'Built-in camera'),
    videoInput('cam-2'),
    { deviceId: 'mic', label: 'Mic', kind: 'audioinput', groupId: 'g', toJSON: () => ({}) },
  ]
  const track = fakeTrack('cam-1')
  const dependencies: CameraDependencies = {
    getUserMedia: vi.fn(async () => fakeStream(track)),
    enumerateDevices: vi.fn(async () => devices),
    onDeviceChange: (listener) => {
      deviceChangeListeners.add(listener)
      return () => deviceChangeListeners.delete(listener)
    },
    ...overrides,
  }
  const controller = createCameraController(dependencies)
  const states: CameraState[] = []
  controller.subscribe((state) => states.push(state))
  return {
    controller,
    dependencies,
    states,
    track,
    setDevices: (next: MediaDeviceInfo[]) => {
      devices = next
    },
    fireDeviceChange: async () => {
      deviceChangeListeners.forEach((listener) => listener())
      await Promise.resolve()
      await Promise.resolve()
    },
    listenerCount: () => deviceChangeListeners.size,
  }
}

describe('camera controller', () => {
  test('lists only video inputs and labels unnamed devices', async () => {
    const { controller } = harness()
    await expect(controller.refreshDevices()).resolves.toEqual([
      { deviceId: 'cam-1', label: 'Built-in camera' },
      { deviceId: 'cam-2', label: 'Camera 2' },
    ])
    expect(controller.state.devices).toHaveLength(2)
    expect(controller.state.status).toBe('idle')
  })

  test('starts the default device, then an exact device, and reports streaming with its id', async () => {
    const { controller, dependencies, states } = harness()
    const stream = await controller.start(null)
    expect(stream).not.toBeNull()
    expect(dependencies.getUserMedia).toHaveBeenLastCalledWith({ video: true, audio: false })
    expect(states.map((state) => state.status)).toEqual(['starting', 'streaming', 'streaming'])
    expect(controller.state).toMatchObject({ status: 'streaming', deviceId: 'cam-1', detail: null })
    expect(controller.state.devices).toHaveLength(2)

    await controller.start('cam-2')
    expect(dependencies.getUserMedia).toHaveBeenLastCalledWith({
      video: { deviceId: { exact: 'cam-2' } },
      audio: false,
    })
  })

  test('permission denial is a state that leaves no stream behind', async () => {
    const { controller } = harness({
      getUserMedia: async () => {
        throw new DOMException('Permission denied', 'NotAllowedError')
      },
    })
    await expect(controller.start(null)).resolves.toBeNull()
    expect(controller.state).toMatchObject({
      status: 'permission_denied',
      deviceId: null,
      detail: expect.stringContaining('Camera permission was denied'),
    })
  })

  test('a missing device and an unexpected failure are unavailable with the error shown', async () => {
    const missing = harness({
      getUserMedia: async () => {
        throw new DOMException('Requested device not found', 'NotFoundError')
      },
    })
    await missing.controller.start('ghost')
    expect(missing.controller.state).toMatchObject({ status: 'unavailable', detail: 'No camera matched the selection.' })

    const busy = harness({
      getUserMedia: async () => {
        throw new DOMException('Could not start video source', 'NotReadableError')
      },
    })
    await busy.controller.start(null)
    expect(busy.controller.state).toMatchObject({ status: 'unavailable', detail: 'Could not start video source' })
  })

  test('an ended track reports webcam_dropped and stops the stream', async () => {
    const { controller, track } = harness()
    await controller.start(null)
    track.end()
    expect(controller.state).toMatchObject({
      status: 'webcam_dropped',
      deviceId: null,
      detail: expect.stringContaining('unplugged'),
    })
    expect(track.stop).toHaveBeenCalledTimes(1)
  })

  test('the active device disappearing on devicechange reports webcam_dropped', async () => {
    const { controller, track, setDevices, fireDeviceChange } = harness()
    await controller.start(null)
    setDevices([videoInput('cam-2', 'Other')])
    await fireDeviceChange()
    expect(controller.state).toMatchObject({ status: 'webcam_dropped', detail: 'The selected camera is no longer present.' })
    expect(controller.state.devices).toEqual([{ deviceId: 'cam-2', label: 'Other' }])
    expect(track.stop).toHaveBeenCalledTimes(1)
  })

  test('a devicechange that keeps the active device only refreshes the list', async () => {
    const { controller, setDevices, fireDeviceChange } = harness()
    await controller.start(null)
    setDevices([videoInput('cam-1', 'Built-in camera'), videoInput('cam-3', 'USB')])
    await fireDeviceChange()
    expect(controller.state.status).toBe('streaming')
    expect(controller.state.devices.map((device) => device.deviceId)).toEqual(['cam-1', 'cam-3'])
  })

  test('stop releases the track and detaches listeners', async () => {
    const { controller, track, listenerCount } = harness()
    await controller.start(null)
    expect(listenerCount()).toBe(1)
    controller.stop()
    expect(controller.state).toMatchObject({ status: 'idle', deviceId: null })
    expect(track.stop).toHaveBeenCalledTimes(1)
    expect(listenerCount()).toBe(0)
    track.end()
    expect(controller.state.status).toBe('idle')
  })
})

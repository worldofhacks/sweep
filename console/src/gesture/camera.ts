/**
 * Webcam device selection and stream lifecycle. Reports `permission_denied`
 * and `webcam_dropped` (the active track ended or the device disappeared) as
 * states rather than exceptions so the panel can show them and the hook can
 * disable emission. Browser media APIs are injected for deterministic tests.
 */

export type CameraStatus =
  | 'idle'
  | 'starting'
  | 'streaming'
  | 'permission_denied'
  | 'webcam_dropped'
  | 'unavailable'

export interface CameraDevice {
  deviceId: string
  label: string
}

export interface CameraState {
  status: CameraStatus
  detail: string | null
  /** Device id of the live stream, or null when nothing is streaming. */
  deviceId: string | null
  devices: CameraDevice[]
}

export type CameraListener = (state: CameraState) => void

export interface CameraController {
  readonly state: CameraState
  refreshDevices(): Promise<CameraDevice[]>
  /** Starts the given device (or the browser default). Resolves null when the start failed. */
  start(deviceId: string | null): Promise<MediaStream | null>
  stop(): void
  subscribe(listener: CameraListener): () => void
}

export interface CameraDependencies {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>
  enumerateDevices(): Promise<MediaDeviceInfo[]>
  /** Subscribes to device changes; returns the unsubscribe. */
  onDeviceChange(listener: () => void): () => void
}

export const browserCameraDependencies: CameraDependencies = {
  getUserMedia: (constraints) => {
    const devices = navigator.mediaDevices
    if (!devices?.getUserMedia) {
      return Promise.reject(new DOMException('Camera access is not available in this browser.', 'NotSupportedError'))
    }
    return devices.getUserMedia(constraints)
  },
  enumerateDevices: () => {
    const devices = navigator.mediaDevices
    if (!devices?.enumerateDevices) return Promise.resolve([])
    return devices.enumerateDevices()
  },
  onDeviceChange: (listener) => {
    const devices = navigator.mediaDevices
    if (!devices?.addEventListener) return () => {}
    devices.addEventListener('devicechange', listener)
    return () => devices.removeEventListener('devicechange', listener)
  },
}

class BrowserCameraController implements CameraController {
  state: CameraState = { status: 'idle', detail: null, deviceId: null, devices: [] }
  private readonly listeners = new Set<CameraListener>()
  private readonly dependencies: CameraDependencies
  private stream: MediaStream | null = null
  private detachTrack: (() => void) | null = null
  private unsubscribeDeviceChange: (() => void) | null = null
  private startSequence = 0

  constructor(dependencies: CameraDependencies) {
    this.dependencies = dependencies
  }

  async refreshDevices(): Promise<CameraDevice[]> {
    const devices = await this.dependencies.enumerateDevices().then(
      (raw) =>
        raw
          .filter((device) => device.kind === 'videoinput')
          .map((device, index) => ({
            deviceId: device.deviceId,
            label: device.label || `Camera ${index + 1}`,
          })),
      () => [] as CameraDevice[],
    )
    this.update({ devices })
    return devices
  }

  async start(deviceId: string | null): Promise<MediaStream | null> {
    this.releaseStream()
    const sequence = ++this.startSequence
    this.update({ status: 'starting', detail: null, deviceId: null })
    let stream: MediaStream
    try {
      stream = await this.dependencies.getUserMedia({
        video: deviceId ? { deviceId: { exact: deviceId } } : true,
        audio: false,
      })
    } catch (error) {
      if (sequence === this.startSequence) this.update(failureState(error))
      return null
    }
    if (sequence !== this.startSequence) {
      stream.getTracks().forEach((track) => track.stop())
      return null
    }

    const track = stream.getVideoTracks()[0]
    if (!track) {
      stream.getTracks().forEach((item) => item.stop())
      this.update({ status: 'unavailable', detail: 'The camera stream carried no video track.', deviceId: null })
      return null
    }
    const settings = typeof track.getSettings === 'function' ? track.getSettings() : {}
    const liveDeviceId = settings.deviceId ?? deviceId
    this.stream = stream

    const handleEnded = () => this.drop('The camera track ended; the device was unplugged or stopped.')
    track.addEventListener('ended', handleEnded)
    this.detachTrack = () => track.removeEventListener('ended', handleEnded)
    this.unsubscribeDeviceChange = this.dependencies.onDeviceChange(() => {
      void this.refreshDevices().then((devices) => {
        if (this.stream !== stream) return
        if (liveDeviceId && !devices.some((device) => device.deviceId === liveDeviceId)) {
          this.drop('The selected camera is no longer present.')
        }
      })
    })

    this.update({ status: 'streaming', detail: null, deviceId: liveDeviceId ?? null })
    await this.refreshDevices()
    return stream
  }

  stop(): void {
    this.startSequence += 1
    this.releaseStream()
    this.update({ status: 'idle', detail: null, deviceId: null })
  }

  subscribe(listener: CameraListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private drop(detail: string): void {
    this.releaseStream()
    this.update({ status: 'webcam_dropped', detail, deviceId: null })
  }

  private releaseStream(): void {
    this.detachTrack?.()
    this.detachTrack = null
    this.unsubscribeDeviceChange?.()
    this.unsubscribeDeviceChange = null
    this.stream?.getTracks().forEach((track) => track.stop())
    this.stream = null
  }

  private update(patch: Partial<CameraState>): void {
    this.state = { ...this.state, ...patch }
    this.listeners.forEach((listener) => listener(this.state))
  }
}

function failureState(error: unknown): Partial<CameraState> {
  // DOMException is not an Error subclass in every runtime; read the fields structurally.
  const record = typeof error === 'object' && error !== null ? (error as Record<string, unknown>) : {}
  const name = typeof record.name === 'string' ? record.name : ''
  const message =
    typeof record.message === 'string' && record.message ? record.message : 'The camera could not be started.'
  if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') {
    return {
      status: 'permission_denied',
      detail: 'Camera permission was denied. Allow camera access in the browser and enable tracking again.',
      deviceId: null,
    }
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return { status: 'unavailable', detail: 'No camera matched the selection.', deviceId: null }
  }
  return { status: 'unavailable', detail: message, deviceId: null }
}

export function createCameraController(
  dependencies: CameraDependencies = browserCameraDependencies,
): CameraController {
  return new BrowserCameraController(dependencies)
}

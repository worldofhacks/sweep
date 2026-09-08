import { useState } from 'react'
import type { DeviceCameraState, RelayAircraftState } from '../relay/contract'
import { streamName } from './playback'

/** A viewing target is one camera on one connection, never just a display label. */
export interface CameraTarget {
  deviceId: number
  connectionEpoch: number
  cameraId: string
  stream: string
}

export function cameraTarget(device: RelayAircraftState, camera: DeviceCameraState): CameraTarget {
  return { deviceId: device.drone_id, connectionEpoch: device.connection_epoch, cameraId: camera.camera_id, stream: camera.stream }
}

export function cameraTargetKey(target: CameraTarget): string {
  return `${target.deviceId}:${target.connectionEpoch}:${target.cameraId}:${target.stream}`
}

export function resolveCameraTarget(device: RelayAircraftState | null, target: CameraTarget): DeviceCameraState | null {
  if (device?.drone_id !== target.deviceId || device.connection_epoch !== target.connectionEpoch) return null
  return device.cameras?.find((camera) => camera.camera_id === target.cameraId && camera.stream === target.stream) ?? null
}

/** Once removed/remapped, an inspection needs a new explicit choice, including A→B→A. */
export function useCameraInspection(device: RelayAircraftState | null, target: CameraTarget | null | undefined, unavailableStreams?: ReadonlySet<string>): DeviceCameraState | null {
  const resolved = target && !unavailableStreams?.has(target.stream) ? resolveCameraTarget(device, target) : null
  const [inspection, setInspection] = useState({ target, retired: resolved === null })
  if (inspection.target !== target) {
    setInspection({ target, retired: resolved === null })
    return resolved
  }
  if (!inspection.retired && resolved === null) setInspection({ target, retired: true })
  return inspection.retired ? null : resolved
}

/** Explicit camera configuration is authoritative, including an empty list. */
export function deviceCameras(device: RelayAircraftState): DeviceCameraState[] {
  if (device.cameras !== undefined) return device.cameras
  return [{
    camera_id: 'primary', label: 'Primary camera', stream: streamName(device),
    status: device.video?.status ?? 'unreported', last_frame_at: device.video?.last_frame_at ?? null,
  }]
}

/** Camera choice is local viewing state, scoped to the current device connection. */
export function useCameraChoice(device: RelayAircraftState | null, unavailableStreams?: ReadonlySet<string>) {
  const scope = device ? `${device.drone_id}:${device.connection_epoch}` : ''
  const [choice, setChoice] = useState<{ scope: string; cameraId: string } | null>(null)
  const cameras = device ? deviceCameras(device) : []
  const camera = (choice?.scope === scope
    ? cameras.find((item) => item.camera_id === choice.cameraId)
    : undefined) ?? cameras[0] ?? null
  const identity = camera ? `${scope}:${camera.camera_id}:${camera.stream}` : scope
  const unavailable = camera !== null && unavailableStreams?.has(camera.stream) === true
  const [retirement, setRetirement] = useState({ identity, choice, retired: unavailable })
  let retired = retirement.retired
  if (retirement.identity !== identity || retirement.choice !== choice) {
    retired = unavailable
    setRetirement({ identity, choice, retired })
  } else if (unavailable && !retired) {
    retired = true
    setRetirement({ identity, choice, retired })
  }
  return {
    cameras, camera: retired ? null : camera,
    choose: (cameraId: string) => setChoice({ scope, cameraId }),
  }
}


/** A configured stream must identify one camera across the current whole roster. */
export function ambiguousCameraStreams(devices: readonly RelayAircraftState[]): ReadonlySet<string> {
  const counts = new Map<string, number>()
  for (const device of devices) for (const camera of device.cameras ?? []) {
    counts.set(camera.stream, (counts.get(camera.stream) ?? 0) + 1)
  }
  return new Set([...counts].filter(([, count]) => count > 1).map(([stream]) => stream))
}

import { useState } from 'react'
import type { DeviceCameraState, RelayAircraftState } from '../relay/contract'
import { streamName } from './playback'

/** Explicit camera configuration is authoritative, including an empty list. */
export function deviceCameras(device: RelayAircraftState): DeviceCameraState[] {
  if (device.cameras !== undefined) return device.cameras
  return [{
    camera_id: 'primary', label: 'Primary camera', stream: streamName(device),
    status: device.video?.status ?? 'unreported', last_frame_at: device.video?.last_frame_at ?? null,
  }]
}

/** Camera choice is local viewing state, scoped to the current device connection. */
export function useCameraChoice(device: RelayAircraftState | null) {
  const scope = device ? `${device.drone_id}:${device.connection_epoch}` : ''
  const [choice, setChoice] = useState<{ scope: string; cameraId: string } | null>(null)
  const cameras = device ? deviceCameras(device) : []
  const camera = (choice?.scope === scope
    ? cameras.find((item) => item.camera_id === choice.cameraId)
    : undefined) ?? cameras[0] ?? null
  return {
    cameras, camera,
    choose: (cameraId: string) => setChoice({ scope, cameraId }),
  }
}

import { formatDeviceId } from '../../control/state'
import type { DeviceCameraState, RelayAircraftState } from '../../relay/contract'
import { deriveStream } from './derive-live'

export function CameraChoice({ device, cameras, camera, now, onChoose }: {
  device: RelayAircraftState
  cameras: DeviceCameraState[]
  camera: DeviceCameraState | null
  now: number
  onChoose: (cameraId: string) => void
}) {
  if (device.cameras === undefined) return null
  if (cameras.length === 0) return <p className="lv-note">No cameras configured for this device.</p>
  return (
    <label className="lv-camera-choice">
      Camera
      <select aria-label={`${formatDeviceId(device)} camera`} value={camera?.camera_id ?? ''}
        onChange={(event) => onChoose(event.target.value)}>
        {cameras.map((item) => <option key={item.camera_id} value={item.camera_id}>
          {item.label} · {deriveStream(device, now, item).status}
        </option>)}
      </select>
    </label>
  )
}

import type { ControlState, OutcomeSummary, RequestRecord } from '../../control/state'
import type { DroneId, RelayAircraftState } from '../../relay/contract'
import { sensorStatus } from '../../sensor/status'

/** The newest refusal that named this device: a request it was selected for, or an adapter refusal. */
export function lastRefusal(
  state: ControlState,
  droneId: DroneId,
): { t: number; reasonCode: string; detail: string } | null {
  const candidates: Array<{ t: number; reasonCode: string; detail: string }> = []
  const outcome: OutcomeSummary | null = state.lastOutcome
  if (outcome && outcome.kind === 'refusal' && outcome.droneId === droneId) {
    candidates.push({ t: outcome.t, reasonCode: outcome.reasonCode ?? 'refused', detail: outcome.detail })
  }
  state.requests
    .filter(
      (request: RequestRecord) =>
        request.status === 'refused' && request.intent.selection.includes(droneId),
    )
    .forEach((request) => {
      candidates.push({
        t: request.timestamps.refused ?? request.timestamps.draft ?? 0,
        reasonCode: request.reasonCode ?? 'refused',
        detail: request.detail ?? '',
      })
    })
  if (candidates.length === 0) return null
  return candidates.sort((left, right) => right.t - left.t)[0]
}

/** The sensor line: no lidar advertised, a scan age, or a kit that has not reported. */
export function sensorWord(device: RelayAircraftState, now: number): { text: string; tone: string } {
  return sensorStatus(device, now)
}

/** The lines a node's environment needs; the key line is a placeholder, never a value. */
export function nodeConfigurationText(
  relayBaseUrl: string | undefined,
  sessionId: string,
  deviceId: number | null,
): string {
  return [
    `SWEEP_RELAY_URL=${relayBaseUrl ?? '<relay URL: this console was not given a relay bootstrap>'}`,
    `SWEEP_SESSION_ID=${sessionId}`,
    `SWEEP_DEVICE_ID=${deviceId ?? '<device id>'}`,
    'SWEEP_NODE_KEY=<entered on the device by a person; never shown on this console>',
  ].join('\n')
}

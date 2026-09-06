import { useState } from 'react'
import './devices.css'
import type { DepartureRecord } from '../../control/state'
import { deviceNoun, formatDeviceId, pluralNoun, rosterNoun } from '../../control/state'
import type { DroneId, RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import { useSensorStore } from '../../sensor/store'
import { Pane } from '../../shell/Pane'
import { authorityWords, membershipTone, sortedAircraft } from '../../shell/derive'
import { formatAgo, formatPercent, formatTime, humanizeCode } from '../../shell/format'
import { membershipReasonSentence, readinessSentence, reasonSentence } from '../../shell/sentences'
import { motionStateWord } from '../control/controls'
import { useSecondTick } from '../live/use-second-tick'
import { deviceScan } from '../map/derive-map'
import { LidarPolar } from '../map/LidarPolar'
import type { ModuleProps } from '../types'
import { lastRefusal, nodeConfigurationText, sensorWord } from './derive-devices'

const CLASS_WORD: Record<RelayAircraftState['device_class'], string> = {
  aircraft: 'aircraft',
  ground_vehicle: 'ground vehicle',
}

/**
 * Every device the relay reports, connected or departed, with its class,
 * unit, capabilities, link, readiness, video and sensor state, and the last
 * refusal that named it; then the configuration a person enters on a node.
 * The device key is never shown here: the relay never sends it, and a
 * person types it on the device.
 */
export function DevicesModule({ controller, now, relayBaseUrl }: ModuleProps) {
  const { state, sensors } = controller
  const snapshot = useSensorStore(sensors)
  const fleet = sortedAircraft(state.aircraft)
  useSecondTick(fleet.some((device) => device.video?.last_frame_at != null || device.sensor?.last_scan_at != null))
  const at = now()
  return (
    <Pane
      title="Devices"
      note="Every device the relay reports, its class and feeds, and the configuration a node needs to join."
    >
      <div data-two="1" className="dv-two">
        <div className="dv-column">
          <p className="dv-eyebrow">Connected · roster v{state.rosterVersion}</p>
          {fleet.length === 0 ? (
            <p className="dv-empty">No devices have joined this session. The relay reports an empty roster.</p>
          ) : (
            fleet.map((device) => (
              <DeviceCard
                key={device.drone_id}
                device={device}
                now={at}
                scan={deviceScan(device, snapshot)}
                refusal={lastRefusal(state, device.drone_id)}
              />
            ))
          )}
          <p className="dv-eyebrow">Departed this session</p>
          {state.departed.length === 0 ? (
            <p className="dv-empty">No {pluralNoun(rosterNoun(fleet))} have left.</p>
          ) : (
            state.departed.map((record, index) => (
              <DepartedCard
                key={`${record.drone.drone_id}-${record.t}-${index}`}
                record={record}
                current={state.aircraft[record.drone.drone_id]}
              />
            ))
          )}
        </div>
        <div className="dv-column">
          <NodeConfiguration
            relayBaseUrl={relayBaseUrl}
            sessionId={state.sessionId}
            knownIds={fleet.map((device) => device.drone_id)}
          />
        </div>
      </div>
    </Pane>
  )
}

function DeviceCard({
  device,
  now,
  scan,
  refusal,
}: {
  device: RelayAircraftState
  now: number
  scan: RelaySensorEvent | null
  refusal: { t: number; reasonCode: string; detail: string } | null
}) {
  const id = formatDeviceId(device)
  const noun = deviceNoun(device.device_class)
  const words = authorityWords(device)
  const video = device.video
  const sensor = sensorWord(device, now)
  return (
    <article className="dv-card" aria-label={`${id} device card`}>
      <div className="dv-card-head">
        <span className="dv-id">{id}</span>
        <span className="dv-class">{CLASS_WORD[device.device_class]}</span>
        <span className={`dv-membership tone-${membershipTone(device.membership)}`}>{device.membership}</span>
        <span className="dv-state">{motionStateWord(device)}</span>
      </div>
      <dl className="dv-rows">
        <Row k="unit" v={`${device.unit} · device id ${device.drone_id} · epoch ${device.connection_epoch}`} />
        <Row k="adapter" v={device.adapter_id} />
        <Row
          k="capabilities"
          v={device.adapter_capabilities.length ? device.adapter_capabilities.join(', ') : 'none advertised'}
        />
        <Row
          k="link"
          v={`${formatPercent(device.link)} · battery ${formatPercent(device.battery)} · position ${formatPercent(device.pos_quality)}`}
        />
        <Row k="authority" v={`${words.authority} · ${words.operator.toLowerCase()} ${device.rc_safety_operator_present ? 'present' : 'absent'}`} />
        <Row
          k="video"
          v={video ? `${video.status}${video.last_frame_at === null ? '' : ` · ${formatAgo(now, video.last_frame_at)}`}` : 'unreported'}
          tone={video ? (video.status === 'live' ? 'ok' : 'warn') : 'muted'}
        />
        <Row k="sensor" v={sensor.text} tone={sensor.tone} />
        <Row k="last seen" v={device.last_seen_at === null ? 'unreported' : formatAgo(now, device.last_seen_at)} />
      </dl>
      {device.adapter_capabilities.includes('lidar') && (
        <LidarPolar device={device} scan={scan} size={104} />
      )}
      {device.readiness_reasons.length > 0 ? (
        <div className="dv-reasons">
          {device.readiness_reasons.map((code) => (
            <p key={code}>
              <code>{code}</code> — {readinessSentence(code, noun) ?? humanizeCode(code)}
            </p>
          ))}
        </div>
      ) : (
        <p className="dv-ready tone-ok">ready</p>
      )}
      <p className="dv-refusal">
        {refusal ? (
          <>
            last refusal <code>{refusal.reasonCode}</code> at {formatTime(refusal.t)} —{' '}
            {reasonSentence(refusal.reasonCode, noun) || refusal.detail}
          </>
        ) : (
          'no refusal has named this device'
        )}
      </p>
    </article>
  )
}

function Row({ k, v, tone = 'ink' }: { k: string; v: string; tone?: string }) {
  return (
    <div className="dv-row">
      <dt>{k}</dt>
      <dd className={`tone-${tone}`}>{v}</dd>
    </div>
  )
}

function DepartedCard({
  record,
  current,
}: {
  record: DepartureRecord
  current: RelayAircraftState | undefined
}) {
  const id = formatDeviceId(record.drone)
  const noun = deviceNoun(record.drone.device_class)
  const rejoined = current !== undefined && current.connection_epoch > record.drone.connection_epoch
  return (
    <div className="dv-departed" aria-label={`${id} departed`}>
      <p className="dv-departed-head">
        <span className="dv-id">{id}</span>
        <span className="dv-class">{CLASS_WORD[record.drone.device_class]}</span>
        <span>epoch {record.drone.connection_epoch}</span>
        <span>{formatTime(record.t)}</span>
      </p>
      <p>
        <code>{record.reasonCode}</code> — {membershipReasonSentence(record.reasonCode, noun) ?? record.detail}
      </p>
      <p className="dv-departed-rejoin">
        {rejoined ? `Rejoined with connection epoch ${current.connection_epoch}.` : 'Has not rejoined.'}
      </p>
    </div>
  )
}

function NodeConfiguration({
  relayBaseUrl,
  sessionId,
  knownIds,
}: {
  relayBaseUrl: string | undefined
  sessionId: string
  knownIds: DroneId[]
}) {
  // Follows the first reported id until the operator types one.
  const [edited, setEdited] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const deviceId = edited ?? String(knownIds[0] ?? '')
  const parsed = Number(deviceId)
  const validId = Number.isInteger(parsed) && parsed > 0 ? parsed : null
  const text = nodeConfigurationText(relayBaseUrl, sessionId, validId)
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error('unavailable')
      await navigator.clipboard.writeText(text)
      setCopied('Copied to the clipboard.')
    } catch {
      setCopied('Copy is not available in this browser; select the text instead.')
    }
  }
  return (
    <section className="dv-config" aria-label="Node configuration">
      <p className="dv-eyebrow">Node configuration</p>
      <p className="dv-config-copy">
        A node joins with the relay URL, the session, its device id, and its key. The first three are
        below; the key is generated with the relay configuration and typed on the device by a person.
        The console never receives it and never shows it.
      </p>
      <label className="dv-config-label">
        Device id
        <input
          className="dv-config-input"
          type="number"
          min={1}
          step={1}
          value={deviceId}
          aria-invalid={validId === null}
          onChange={(event) => setEdited(event.target.value)}
        />
      </label>
      {knownIds.length > 0 && (
        <p className="dv-config-known">
          Ids in this session: {knownIds.join(', ')}. A new device needs an id the relay configuration
          lists with a key.
        </p>
      )}
      <pre className="dv-config-block" data-scroll="1" aria-label="Node configuration block">
        {text}
      </pre>
      <div className="dv-config-actions">
        <button type="button" className="dv-config-copy-btn" onClick={() => void copy()}>
          Copy configuration
        </button>
        <span className="dv-config-status" role="status">
          {copied ?? ''}
        </span>
      </div>
    </section>
  )
}

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import { controlReducer, createInitialControlState, createRequestRecord } from '../../control/state'
import { createIntent } from '../../control/intent'
import { C1_BASIC_CONTROL_INTENTS } from '../../relay/contract'
import { FixtureRelayClient, fixtureScenario } from '../../testing/fixture-relay-client'
import { lastRefusal, nodeConfigurationText, sensorWord } from './derive-devices'

const session = 'devices-module-session'
const clock = () => 1_756_700_000_000

function renderDevices(relayBaseUrl?: string) {
  const clients = {
    console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
    keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
  }
  render(
    <App
      sessionId={session}
      clients={clients}
      intentDependencies={{ now: clock, nextId: () => 'devices-intent' }}
      initialModule="devices"
      relayBaseUrl={relayBaseUrl}
    />,
  )
  return clients
}

const card = (id: string) => within(screen.getByRole('article', { name: `${id} device card` }))
const row = (scope: ReturnType<typeof within>, key: string) =>
  scope.getByText(key, { selector: 'dt' }).nextElementSibling

describe('Devices module', () => {
  test('lists every device with its class, unit, capabilities, feeds and readiness', async () => {
    renderDevices('wss://relay.test')
    await screen.findByText(/Development fixture active/i)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Devices')
    expect(screen.getAllByRole('article', { name: /device card$/ }).map((item) => item.getAttribute('aria-label'))).toEqual([
      'D-01 device card',
      'D-02 device card',
      'G-01 device card',
      'G-02 device card',
      'G-03 device card',
    ])

    const one = card('G-01')
    expect(one.getByText('ground vehicle')).toBeInTheDocument()
    expect(one.getByText('idle')).toBeInTheDocument()
    expect(row(one, 'unit')).toHaveTextContent('1 · device id 11 · epoch 1')
    expect(row(one, 'adapter')).toHaveTextContent('ohmni-01')
    expect(row(one, 'capabilities')).toHaveTextContent('class:ground_vehicle, ground_drive, camera, neck, speech, lights, screen, lidar')
    expect(row(one, 'link')).toHaveTextContent('87% · battery 71% · position 60%')
    expect(row(one, 'authority')).toHaveTextContent('Sweep · spotter present')
    expect(row(one, 'video')).toHaveTextContent('live · just now')
    expect(row(one, 'sensor')).toHaveTextContent('Lidar live · single scan plane only')
    expect(one.getByText('ready', { selector: '.dv-ready' })).toBeInTheDocument()
    expect(one.getByText('no refusal has named this device')).toBeInTheDocument()

    const three = card('G-03')
    expect(three.getByText('docked')).toBeInTheDocument()
    expect(row(three, 'sensor')).toHaveTextContent('No lidar fitted / advertised · obstacle coverage unavailable')
    expect(row(three, 'video')).toHaveTextContent('unreported')
    expect(row(three, 'authority')).toHaveTextContent('Sweep · spotter absent')
    expect(three.getByText('rc_safety_operator_missing')).toBeInTheDocument()
    expect(three.getByText(/No spotter is reported present beside the robot/)).toBeInTheDocument()

    const aircraft = card('D-02')
    expect(aircraft.getByText('aircraft', { selector: '.dv-class' })).toBeInTheDocument()
    expect(row(aircraft, 'authority')).toHaveTextContent('Sweep · rc safety operator present')
    expect(aircraft.queryByText('sensor', { selector: 'dt' })).not.toBeInTheDocument()
    expect(aircraft.queryByText(/lidar/i)).not.toBeInTheDocument()
    expect(row(aircraft, 'video')).toHaveTextContent('offline · 38 s ago')
    const pane = within(screen.getByRole('region', { name: 'Working pane' }))
    expect(pane.getByText('No devices have left.')).toBeInTheDocument()
  })

  test('the node configuration block names the relay, session and device id and never a key', async () => {
    const user = userEvent.setup()
    renderDevices('wss://relay.test')
    await screen.findByText(/Development fixture active/i)
    const config = within(screen.getByRole('region', { name: 'Node configuration' }))
    const block = config.getByLabelText('Node configuration block')
    expect(block).toHaveTextContent('SWEEP_RELAY_URL=wss://relay.test')
    expect(block).toHaveTextContent(`SWEEP_SESSION_ID=${session}`)
    expect(block).toHaveTextContent('SWEEP_DEVICE_ID=1')
    expect(block).toHaveTextContent('SWEEP_NODE_KEY=<entered on the device by a person; never shown on this console>')
    expect(block.textContent).not.toMatch(/SWEEP_NODE_KEY=[A-Za-z0-9]/)
    expect(config.getByText(/Ids in this session: 1, 2, 11, 12, 13/)).toBeInTheDocument()

    const input = config.getByLabelText('Device id')
    await user.clear(input)
    await user.type(input, '14')
    expect(block).toHaveTextContent('SWEEP_DEVICE_ID=14')

    await user.click(config.getByRole('button', { name: 'Copy configuration' }))
    expect(config.getByRole('status')).toHaveTextContent(/Copied to the clipboard|Copy is not available/)
  })

  test('without a relay bootstrap the block says so instead of inventing a URL', async () => {
    renderDevices()
    await screen.findByText(/Development fixture active/i)
    expect(screen.getByLabelText('Node configuration block')).toHaveTextContent(
      'SWEEP_RELAY_URL=<relay URL: this console was not given a relay bootstrap>',
    )
    expect(nodeConfigurationText(undefined, 's', null)).toContain('SWEEP_DEVICE_ID=<device id>')
  })

  test('the last refusal that named a device comes from its refused requests or the adapter outcome', () => {
    const t = clock()
    const drones = fixtureScenario('mixed').fleet(t)
    let state = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: {
        v: 1, t, type: 'state', event_id: 'state-1', session, roster_version: 14, armed: true, estop: false,
        selection: [11], formation: 'line', spacing: 1.2, mode: 'indoor', capability_profile: 'c1_basic_control',
        enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS], pending: null, accepted_plan: null, drones,
      },
    })
    expect(lastRefusal(state, 11)).toBeNull()
    const intent = createIntent(
      { name: 'takeoff', args: {}, selection: [11], source: 'console', session },
      { now: () => t + 1, nextId: () => 'takeoff-robot' },
    )
    state = controlReducer(state, { type: 'request_created', request: createRequestRecord(intent, t + 1) })
    state = controlReducer(state, { type: 'request_sent', intentId: 'takeoff-robot', t: t + 2 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1, t: t + 3, type: 'refusal', event_id: 'refusal-1', session, intent_id: 'takeoff-robot',
        command_id: null, status: 'refused', source: 'relay', reason: 'unsupported_for_device_class',
        detail: 'takeoff is not supported for ground_vehicle', roster_version: 14, drone_id: 11, connection_epoch: 1,
      },
    })
    expect(lastRefusal(state, 11)).toMatchObject({ t: t + 3, reasonCode: 'unsupported_for_device_class' })
    expect(lastRefusal(state, 12)).toBeNull()

    expect(sensorWord(drones[2], t)).toEqual({ text: 'Lidar live · single scan plane only', tone: 'ink' })
    expect(sensorWord(drones[4], t)).toEqual({ text: 'No lidar fitted / advertised · obstacle coverage unavailable', tone: 'warn' })
    expect(sensorWord({ ...drones[2], sensor: undefined }, t)).toEqual({
      text: 'Lidar advertised · no scan reported · coverage unknown',
      tone: 'warn',
    })
    expect(sensorWord({ ...drones[2], sensor: { kind: 'lidar_scan', last_scan_at: null } }, t)).toEqual({
      text: 'Lidar advertised · no scan reported · coverage unknown',
      tone: 'warn',
    })
  })
})

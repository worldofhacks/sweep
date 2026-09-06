import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import App from '../../App'
import type { ControlClients } from '../../control/use-control-console'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1 } from '../../relay/contract'
import { createGestureTestRig, hand } from '../../testing/gesture-fixtures'

const session = 'gesture-module-session'

interface FixtureClients extends ControlClients {
  console: FixtureRelayClient
  keyboard: FixtureRelayClient
  webcam?: FixtureRelayClient
}

function mount(options: { loadError?: Error; withWebcam?: boolean } = {}) {
  const rig = createGestureTestRig({ loadError: options.loadError })
  const wall = () => rig.dependencies.clock.wall()
  const clients: FixtureClients = {
    console: new FixtureRelayClient(session, wall, 'console'),
    keyboard: new FixtureRelayClient(session, wall, 'keyboard'),
    ...(options.withWebcam === false ? {} : { webcam: new FixtureRelayClient(session, wall, 'webcam') }),
  }
  render(
    <App
      sessionId={session}
      clients={clients}
      intentDependencies={{ now: wall, nextId: () => 'panel-intent-0123456789abcdef' }}
      initialModule="gesture"
      services={{ gesture: rig.dependencies }}
    />,
  )
  const hold = (category: Parameters<typeof hand>[0], durationMs: number, score = 0.95) => {
    for (let elapsed = 0; elapsed < durationMs; elapsed += 50) {
      act(() => rig.frame(category === null ? [] : [hand(category, score)], 50))
    }
  }
  return { rig, clients, hold }
}

function createContext() {
  return {
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
  }
}

let context = createContext()

beforeEach(() => {
  context = createContext()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  )
})

const trackingState = () => screen.getByRole('status', { name: 'Tracking state' })
const enableButton = () => screen.getByRole('button', { name: 'Enable tracking' })

describe('Gesture module', () => {
  test('Flight buttons work with the camera off and no webcam connection, but only send the selected pulse after dock confirmation', async () => {
    const { clients, rig } = mount({ withWebcam: false })
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await user.click(screen.getByRole('radio', { name: 'Flight (opt in)' }))
    const flight = within(screen.getByRole('region', { name: 'Selected flight actions' }))
    expect(flight.getByRole('button', { name: 'Forward 0.5 seconds' })).toBeDisabled()
    const t = rig.dependencies.clock.wall()
    act(() => clients.console.emitServer({
      v: 1, t, event_id: 'flight-ui-state', state_sequence: 1, type: 'state', session,
      roster_version: 7, armed: true, estop: false, selection: [1, 2], formation: 'none', spacing: 0.8,
      mode: 'indoor', capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'body_pulse'], pending: null, accepted_plan: null,
      drones: fixtureAircraft(t, 4).slice(0, 2).map((drone) => ({ ...drone, flight_state: 'hovering', adapter_capabilities: ['flight', 'body_pulse_v1'] })),
    }))
    expect(flight.getByText('Flight · D-01, D-02')).toBeInTheDocument()
    expect(flight.getByRole('button', { name: 'Forward 0.5 seconds' })).toBeEnabled()
    await user.click(flight.getByRole('button', { name: 'Forward 0.5 seconds' }))
    const dock = within(screen.getByRole('region', { name: 'Pending confirmation' }))
    expect(dock.getByText('Forward 0.5 seconds', { selector: '.sh-dock-title' })).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
    expect(rig.camera.startCalls).toBe(0)
    expect(rig.source.loadCalls).toBe(0)
    await user.click(dock.getByRole('button', { name: 'Confirm and send' }))
    expect(clients.console.sent).toHaveLength(1)
    expect(clients.console.sent[0]).toMatchObject({ name: 'body_pulse', source: 'console', selection: [1, 2], args: { forward_mm_s: 250, duration_ms: 500 }, confirm: true })
    expect(isConsoleIntentV1(clients.console.sent[0])).toBe(true)
    const quick = within(screen.getByRole('group', { name: 'Quick commands' }))
    expect(quick.getByRole('button', { name: 'Hold' })).toBeEnabled()
    expect(quick.getByRole('button', { name: 'Land all' })).toBeEnabled()
  })

  test('opting into Flight stops tracking and cancels the old profile preview', async () => {
    const { rig, clients, hold } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await user.click(enableButton())
    hold('Open_Palm', 650)
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: 'Flight (opt in)' }))
    expect(enableButton()).toHaveAttribute('aria-pressed', 'false')
    expect(rig.scheduler.pending).toBe(false)
    expect(rig.source.closed).toBe(true)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Open palm pair')).toHaveTextContent('previews Arm session')
    expect(screen.getByLabelText('I love you pair')).toHaveTextContent('previews Land selected')
    expect(clients.console.sent).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
  })

  test('opens off by default with the camera list, the pairs, the never-emittable note and the webcam source', async () => {
    const { rig } = mount()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Gesture recognition')
    const tabs = within(screen.getByRole('group', { name: 'Gesture panes' }))
    expect(tabs.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Camera and readout',
      'Gesture vocabulary',
    ])
    expect(enableButton()).toHaveAttribute('aria-pressed', 'false')
    expect(rig.camera.startCalls).toBe(0)
    expect(rig.source.loadCalls).toBe(0)

    const select = screen.getByRole('combobox', { name: 'Camera' })
    expect(within(select).getByRole('option', { name: 'Fixture camera' })).toBeInTheDocument()
    expect(screen.getByLabelText('Gesture camera preview')).toBeInTheDocument()
    expect(screen.getByText('tracking disabled (default)')).toBeInTheDocument()
    expect(trackingState()).toHaveTextContent('Tracking off')
    expect(trackingState()).toHaveTextContent('Emission disabled. The network stop and physical RC remain available.')
    expect(trackingState()).toHaveTextContent('webcam source connected')

    expect(screen.getByLabelText('Open palm pair')).toHaveTextContent(
      'Open palmemits capture_room as a preview600 ms dwell · score 0.80',
    )
    expect(screen.getByLabelText('Closed fist pair')).toHaveTextContent('emits hold as a preview600 ms dwell')
    expect(screen.getByLabelText('Thumb up pair')).toHaveTextContent('confirms the pending preview400 ms dwell')
    expect(screen.getByLabelText('Thumb down pair')).toHaveTextContent('cancels the pending preview400 ms dwell')
    const never = screen.getByText(/Never gesture-emittable/)
    expect(never).toHaveTextContent('estop')
    expect(never).not.toHaveTextContent('takeoff')
    expect(screen.getByRole('radio', { name: 'Capture / HOLD (default)' })).toBeChecked()
    expect(screen.queryByRole('region', { name: 'Selected flight actions' })).not.toBeInTheDocument()
    expect(never).toHaveTextContent('translate')
    expect(never).toHaveTextContent('stay on the console controls and the physical RC')
    expect(screen.getByText(/Nothing recognised yet/)).toBeInTheDocument()

    const target = within(screen.getByRole('group', { name: 'Target' }))
    expect(target.getByText('1 of 4 selected')).toBeInTheDocument()
    expect(target.getByRole('button', { name: 'Deselect D-01' })).toHaveAttribute('aria-pressed', 'true')
    expect(target.getByRole('button', { name: 'Select D-03' })).toBeDisabled()
    expect(target.getByText(/D-03 telemetry stale — these cannot be selected or commanded/)).toBeInTheDocument()

    const links = within(screen.getByRole('list', { name: 'Connections' }))
    expect(links.getByTitle('Webcam')).toHaveTextContent(/^webcam\s*connected$/)
  })

  test('shows the candidate preview, overlay, dwell feedback, and confirmation through gestures', async () => {
    const { rig, clients, hold } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    await user.click(enableButton())
    await act(async () => {})
    expect(trackingState()).toHaveTextContent('Tracking')
    expect(screen.getByRole('button', { name: 'Disable tracking' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('tracking enabled')).toBeInTheDocument()

    hold('Open_Palm', 300)
    expect(context.lineTo).toHaveBeenCalled()
    expect(screen.getByText(/250 \/ 600 ms/)).toBeInTheDocument()
    expect(screen.getByText('Candidate')).toBeInTheDocument()
    expect(screen.getByLabelText('Open palm pair')).toHaveClass('is-dwelling')

    hold('Open_Palm', 350)
    const previewId = screen.getByText('panel-in…cdef', { selector: '.gs-preview code' })
    expect(previewId).toHaveAttribute('title', 'panel-intent-0123456789abcdef')
    expect(previewId.closest('strong')).toHaveTextContent('Capture room ·')
    expect(screen.getByText(/Thumb up confirms and sends through the webcam source/)).toBeInTheDocument()
    expect(screen.getAllByText(/Duplicate suppressed/).length).toBeGreaterThan(0)
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('Capture room')
    expect(dock).toHaveTextContent('source webcam')
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
    const readout = within(screen.getByRole('list', { name: 'Gesture readout' }))
    expect(readout.getByText('drafted')).toBeInTheDocument()

    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({
      intent_id: 'panel-intent-0123456789abcdef',
      name: 'capture_room',
      source: 'webcam',
      confirm: true,
    })
    expect(clients.console.sent).toHaveLength(0)
    expect(screen.getByText(/Last gesture action: Confirm/)).toBeInTheDocument()
    expect(screen.getByText('No gesture-drafted preview')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(readout.getByText('confirmed')).toBeInTheDocument()
    expect(rig.downloads).toHaveLength(0)
  })

  test('renders model failure as a distinct non-emitting state and downloads the session', async () => {
    const { rig } = mount({ loadError: new Error('wasm fetch failed') })
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    await user.click(enableButton())
    await act(async () => {})
    expect(trackingState()).toHaveTextContent('Model failed to load')
    expect(trackingState()).toHaveTextContent('wasm fetch failed')
    expect(trackingState()).toHaveTextContent('Emission disabled. The network stop and physical RC remain available.')
    expect(screen.getByText(/wasm fetch failed/, { selector: '[role="alert"]' })).toBeInTheDocument()
    expect(screen.getByText('tracking failed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Disable tracking' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Download session (JSONL)' }))
    expect(rig.downloads).toHaveLength(1)
    const lines = rig.downloads[0].contents.trimEnd().split('\n').map((line) => JSON.parse(line))
    expect(lines[0]).toMatchObject({ kind: 'header', session, source: 'webcam' })
    expect(lines.at(-1)).toMatchObject({ kind: 'status', recognizer: 'model_failed_to_load', detail: 'wasm fetch failed' })
  })

  test('renders low confidence, a dwell timeout and a dropped webcam as non-emitting states', async () => {
    const { rig, clients, hold } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})
    await user.click(enableButton())
    await act(async () => {})

    act(() => rig.frame([hand('Open_Palm', 0.4)], 50))
    expect(screen.getByText(/Low confidence · Open palm at 40% \(needs 80%\)/)).toBeInTheDocument()
    const readout = within(screen.getByRole('list', { name: 'Gesture readout' }))
    expect(readout.getByText('low confidence')).toHaveClass('is-blocked')
    expect(readout.getByText(/Open palm scored 40%; the threshold is 80%\. Nothing was emitted\./)).toBeInTheDocument()

    hold(null, 100)
    hold('Open_Palm', 200)
    hold(null, 100)
    expect(readout.getByText('dwell timeout')).toBeInTheDocument()
    expect(readout.getByText(/Open palm held \d+ of 600 ms \(released\)\. Nothing was emitted\./)).toBeInTheDocument()

    hold('Open_Palm', 200)
    act(() => rig.camera.dropWebcam())
    expect(trackingState()).toHaveTextContent('Webcam dropped')
    expect(trackingState()).toHaveTextContent('unplugged')
    expect(trackingState()).toHaveTextContent('Emission disabled')
    expect(screen.getByText(/unplugged/, { selector: '[role="alert"]' })).toBeInTheDocument()

    hold('Open_Palm', 1000)
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
  })

  test('the vocabulary pane lists the states that emit nothing and a pane switch keeps tracking alive', async () => {
    const { hold } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})
    await user.click(enableButton())
    await act(async () => {})

    const tabs = within(screen.getByRole('group', { name: 'Gesture panes' }))
    await user.click(tabs.getByRole('button', { name: 'Gesture vocabulary' }))
    expect(screen.getByText('States that emit nothing')).toBeInTheDocument()
    for (const key of ['model failed to load', 'webcam dropped', 'low confidence', 'dwell timeout', 'duplicate suppressed']) {
      expect(screen.getByText(key)).toBeInTheDocument()
    }
    expect(screen.getByText(/Adversarial target from the PRD/)).toBeInTheDocument()
    expect(screen.getByText('Open_Palm')).toBeInTheDocument()
    expect(screen.queryByLabelText('Gesture camera preview')).not.toBeInTheDocument()

    await user.click(tabs.getByRole('button', { name: 'Camera and readout' }))
    expect(trackingState()).toHaveTextContent('Tracking')
    hold('Open_Palm', 650)
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toHaveTextContent('source webcam')
  })

  test('without a webcam relay source every accepted gesture is blocked and the header shows no pill', async () => {
    const { clients, hold } = mount({ withWebcam: false })
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    expect(trackingState()).toHaveTextContent('webcam source disconnected — Webcam relay source is unavailable.')
    const links = within(screen.getByRole('list', { name: 'Connections' }))
    expect(links.queryByTitle('Webcam')).not.toBeInTheDocument()

    await user.click(enableButton())
    await act(async () => {})
    expect(
      screen.getByText('Drafting blocked: The webcam relay source is not connected; no gesture intent can be sent.'),
    ).toBeInTheDocument()
    hold('Open_Palm', 650)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(screen.getByText(/Last gesture action: Blocked/)).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('a dropped console connection blocks drafting while the webcam source stays connected', async () => {
    const { clients, hold } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})
    await user.click(enableButton())
    await act(async () => {})

    act(() => clients.console.emitConnection('disconnected', 'Relay socket closed.'))
    const links = within(screen.getByRole('list', { name: 'Connections' }))
    expect(links.getByTitle('Relay (console)')).toHaveTextContent(/disconnected$/)
    expect(links.getByTitle('Webcam')).toHaveTextContent(/^webcam\s*connected$/)
    expect(trackingState()).toHaveTextContent('Tracking')
    expect(trackingState()).toHaveTextContent('webcam source connected')
    expect(screen.getByRole('group', { name: 'Target' })).toHaveTextContent('1 of 4 selected')
    expect(
      screen.getByText('Drafting blocked: The console connection is disconnected; no gesture intent can be drafted.'),
    ).toBeInTheDocument()

    hold('Open_Palm', 650)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(screen.getByText(/Last gesture action: Blocked/)).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Gesture readout' })).getByText('blocked')).toBeInTheDocument()
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
  })
})

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { useControlConsole, type ControlClients } from '../control/use-control-console'
import { FixtureRelayClient } from '../testing/fixture-relay-client'
import { createGestureTestRig, hand, type GestureTestRig } from '../testing/gesture-fixtures'
import GesturePanel from './GesturePanel'

const session = 'gesture-panel-session'

function Harness({ clients, rig }: { clients: ControlClients; rig: GestureTestRig }) {
  const control = useControlConsole({
    sessionId: session,
    clients,
    intentDependencies: {
      now: () => rig.dependencies.clock.wall(),
      nextId: () => 'panel-intent-0123456789abcdef',
    },
  })
  return <GesturePanel control={control} roomId="room-01" dependencies={rig.dependencies} />
}

interface FixtureClients extends ControlClients {
  console: FixtureRelayClient
  keyboard: FixtureRelayClient
  webcam: FixtureRelayClient
}

function mount(options: { loadError?: Error } = {}) {
  const rig = createGestureTestRig(options)
  const wall = () => rig.dependencies.clock.wall()
  const clients: FixtureClients = {
    console: new FixtureRelayClient(session, wall, 'console'),
    keyboard: new FixtureRelayClient(session, wall, 'keyboard'),
    webcam: new FixtureRelayClient(session, wall, 'webcam'),
  }
  render(<Harness clients={clients} rig={rig} />)
  const hold = (category: Parameters<typeof hand>[0], durationMs: number) => {
    for (let elapsed = 0; elapsed < durationMs; elapsed += 50) {
      act(() => rig.frame(category === null ? [] : [hand(category)], 50))
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

describe('GesturePanel', () => {
  test('mounts collapsed, off by default, with the camera list, pairs, and the never-emittable note', async () => {
    const { rig } = mount()
    await act(async () => {})

    const panel = screen.getByRole('heading', { name: 'Gesture readout' }).closest('details')
    expect(panel).not.toBeNull()
    expect(panel).not.toHaveAttribute('open')
    const enable = screen.getByRole('button', { name: 'Enable tracking' })
    expect(enable).toHaveAttribute('aria-pressed', 'false')
    expect(rig.camera.startCalls).toBe(0)

    const select = screen.getByLabelText('Camera')
    expect(within(select).getByRole('option', { name: 'Fixture camera' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Tracking off')
    expect(screen.getByRole('status')).toHaveTextContent(
      'Emission disabled. The network stop and physical RC remain available.',
    )

    const rows = screen.getAllByRole('row').slice(1)
    expect(rows.map((row) => row.textContent)).toEqual([
      'Open_Palmdraft capture_room600 ms0.80',
      'Closed_Fistdraft hold600 ms0.80',
      'Thumb_Upconfirm pending preview400 ms0.80',
      'Thumb_Downcancel pending preview400 ms0.80',
    ])
    const never = screen.getByText(/Never gesture-emittable/)
    expect(never).toHaveTextContent('estop')
    expect(never).toHaveTextContent('arm')
    expect(never).toHaveTextContent('takeoff')
    expect(never).toHaveTextContent('translate')
    expect(never).toHaveTextContent('stay on the console controls and the physical RC')
  })

  test('shows the candidate preview, overlay, dwell feedback, and confirmation through gestures', async () => {
    const { rig, clients, hold } = mount()
    const user = userEvent.setup()
    await act(async () => {})

    await user.click(screen.getByRole('button', { name: 'Enable tracking' }))
    await act(async () => {})
    expect(screen.getByRole('status')).toHaveTextContent('Tracking')
    expect(screen.getByRole('button', { name: 'Disable tracking' })).toHaveAttribute('aria-pressed', 'true')

    hold('Open_Palm', 300)
    expect(context.lineTo).toHaveBeenCalled()
    expect(screen.getByText(/250 \/ 600 ms/)).toBeInTheDocument()
    expect(screen.getByText('Candidate')).toBeInTheDocument()

    hold('Open_Palm', 350)
    const previewId = screen.getByText('panel-in…cdef')
    expect(previewId).toHaveAttribute('title', 'panel-intent-0123456789abcdef')
    expect(previewId.closest('strong')).toHaveTextContent('Capture room ·')
    expect(screen.getByText(/Thumb up confirms and sends through the webcam source/)).toBeInTheDocument()
    expect(screen.getByText(/Duplicate suppressed/)).toBeInTheDocument()
    expect(clients.webcam.sent).toHaveLength(0)

    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam.sent).toHaveLength(1)
    expect(screen.getByText(/Last gesture action: Confirm/)).toBeInTheDocument()
    expect(screen.getByText('No gesture-drafted preview')).toBeInTheDocument()
    expect(rig.downloads).toHaveLength(0)
  })

  test('renders model failure as a distinct non-emitting state and downloads the session', async () => {
    const { rig } = mount({ loadError: new Error('wasm fetch failed') })
    const user = userEvent.setup()
    await act(async () => {})

    await user.click(screen.getByRole('button', { name: 'Enable tracking' }))
    await act(async () => {})
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Model failed to load')
    expect(status).toHaveTextContent('wasm fetch failed')
    expect(status).toHaveTextContent('Emission disabled. The network stop and physical RC remain available.')
    expect(screen.getByRole('button', { name: 'Disable tracking' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Download session (JSONL)' }))
    expect(rig.downloads).toHaveLength(1)
    const lines = rig.downloads[0].contents.trimEnd().split('\n').map((line) => JSON.parse(line))
    expect(lines[0].kind).toBe('header')
    expect(lines.at(-1)).toMatchObject({ kind: 'status', recognizer: 'model_failed_to_load', detail: 'wasm fetch failed' })
  })

  test('renders a dropped webcam and low confidence as non-emitting states', async () => {
    const { rig, hold } = mount()
    const user = userEvent.setup()
    await act(async () => {})
    await user.click(screen.getByRole('button', { name: 'Enable tracking' }))
    await act(async () => {})

    act(() => rig.frame([hand('Open_Palm', 0.4)], 50))
    expect(screen.getByText(/Low confidence · Open_Palm at 40% \(needs 80%\)/)).toBeInTheDocument()

    hold('Open_Palm', 200)
    act(() => rig.camera.dropWebcam())
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Webcam dropped')
    expect(status).toHaveTextContent('unplugged')
    expect(status).toHaveTextContent('Emission disabled')
  })
})

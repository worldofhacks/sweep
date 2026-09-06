import { render, screen, within } from '@testing-library/react'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import type { RelaySensorEvent } from '../../relay/contract'
import { callsNamed, canvasCalls } from '../../testing/canvas-context'
import { FixtureRelayClient } from '../../testing/fixture-relay-client'
import { LidarPolar } from './LidarPolar'

const session = 'lidar-polar-session'
const clock = () => 1_756_700_000_000

const ranges = Array.from({ length: 360 }, () => 0)
ranges[0] = 200
ranges[90] = 100

const scan: RelaySensorEvent = {
  v: 1,
  t: 1_756_700_000_000,
  type: 'sensor',
  event_id: 'polar-1',
  session,
  drone_id: 11,
  connection_epoch: 1,
  kind: 'lidar_scan',
  pose: { x: 0, y: 0, yaw_deg: 0 },
  angle_min_deg: 0,
  angle_increment_deg: 1,
  range_min_m: 0.15,
  range_max_m: 12,
  ranges_cm: ranges,
}

const device = { device_class: 'ground_vehicle', unit: 1 } as const

function renderConsole(initialModule: 'devices' | 'control') {
  render(
    <App
      sessionId={session}
      clients={{
        console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
        keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
      }}
      intentDependencies={{ now: clock, nextId: () => 'polar-intent' }}
      initialModule={initialModule}
    />,
  )
}

describe('LidarPolar', () => {
  test('plots the returns of the newest scan with the forward axis up', () => {
    render(<LidarPolar device={device} scan={scan} size={100} now={scan.t} />)
    expect(screen.getByText('Live · 2 of 360 returns · 2.0 m')).toBeInTheDocument()
    const canvas = screen.getByRole('img', { name: 'G-01 polar lidar plot' }) as HTMLCanvasElement
    const dots = callsNamed(canvasCalls(canvas), 'fillRect').filter((args) => args[2] === 2)
    // Two metres forward fills the plot; one metre to its left is half of it.
    expect(dots[0]).toEqual([49, 5, 2, 2])
    expect(dots[1][0]).toBeCloseTo(27, 6)
    expect(dots[1][1]).toBeCloseTo(49, 6)
  })

  test('says so rather than drawing an empty plot when no scan has arrived', () => {
    render(<LidarPolar device={device} scan={null} />)
    expect(screen.getByLabelText('G-01 lidar scan')).toHaveTextContent('No scan reported.')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  test('rides on the device cards of ground vehicles that advertise lidar, and nowhere else', async () => {
    renderConsole('devices')
    await screen.findByRole('article', { name: 'G-01 device card' })
    const fitted = within(screen.getByRole('article', { name: 'G-01 device card' }))
    expect(fitted.getByRole('img', { name: 'G-01 polar lidar plot' })).toBeInTheDocument()
    const unfitted = within(screen.getByRole('article', { name: 'G-03 device card' }))
    expect(unfitted.queryByRole('img')).not.toBeInTheDocument()
    const aircraft = within(screen.getByRole('article', { name: 'D-01 device card' }))
    expect(aircraft.queryByRole('img')).not.toBeInTheDocument()
  })

  test('rides on the registry cards of ground vehicles in the fleet context', async () => {
    renderConsole('control')
    await screen.findAllByRole('article', { name: 'G-01 registry card' })
    const [card] = screen.getAllByRole('article', { name: 'G-01 registry card' })
    expect(within(card).getByRole('img', { name: 'G-01 polar lidar plot' })).toBeInTheDocument()
    const [aircraft] = screen.getAllByRole('article', { name: 'D-01 registry card' })
    expect(within(aircraft).queryByRole('img')).not.toBeInTheDocument()
  })
})

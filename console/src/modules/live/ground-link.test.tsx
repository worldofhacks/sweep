import { render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { fixtureScenario } from '../../testing/fixture-relay-client'
import { formatDeviceLink } from '../../shell/format'
import { Mosaic } from './Mosaic'
import { FocusFeed } from './FocusFeed'

const now = 1_756_700_000_000
const ground = { ...fixtureScenario('mixed').fleet(now).find((device) => device.device_class === 'ground_vehicle')!, link: 1 }

// Real ground adapters use a legacy numeric transport field, never an RF measurement.
test.each([1, 0.87, 0, null])('ground link %s never becomes a measured percentage', (link) => {
  const text = formatDeviceLink({ ...ground, link })
  expect(text).toContain('radio quality unreported')
  expect(text).not.toContain('%')
  expect(text).toContain(link === null ? 'transport unreported' : 'transport receipt reported')
})

test('aircraft link readings retain their percentage contract', () => {
  expect(formatDeviceLink({ device_class: 'aircraft', link: 0.96 })).toBe('96%')
  expect(formatDeviceLink({ device_class: 'aircraft', link: null })).toBe('—')
})

test.each(['ready', 'disconnected'] as const)('wall and focus do not label %s ground transport as radio quality', (membership) => {
  const device = { ...ground, membership }
  const wall = render(<Mosaic devices={[device]} now={now} focusedId={null} selection={[]} onFocus={() => {}} onToggleSelection={() => {}} />)
  const tile = within(screen.getByRole('article', { name: 'G-01 camera tile' }))
  expect(tile.getByText('link transport receipt reported · radio quality unreported')).toBeInTheDocument()
  expect(tile.queryByText('link 100%')).not.toBeInTheDocument()
  wall.unmount()
  render(<FocusFeed focused={device} now={now} requests={[]} />)
  expect(screen.getByText('link', { selector: 'dt' }).nextElementSibling).toHaveTextContent('transport receipt reported · radio quality unreported')
  expect(screen.getByText('link', { selector: 'dt' }).nextElementSibling).not.toHaveTextContent('100%')
})

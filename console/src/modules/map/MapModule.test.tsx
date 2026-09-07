import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import App from '../../App'
import { UnavailableRelayClient } from '../../relay/client'

test('existing Map module offers honest authoring and retains edits between its tabs', () => {
  const clients = { console: new UnavailableRelayClient('No live relay.'), keyboard: new UnavailableRelayClient('No live relay.') }
  render(<App sessionId="map-module-test" clients={clients} initialModule="map" />)
  const tabs = within(screen.getByRole('group', { name: 'Map panes' }))
  fireEvent.click(tabs.getByRole('button', { name: 'Map authoring' }))
  expect(screen.getByText('No authored objects.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save to relay' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Review approval' })).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: 'Map version' }), { target: { value: 'operator-entered-version' } })
  fireEvent.click(tabs.getByRole('button', { name: 'Live observations' }))
  expect(screen.queryByRole('region', { name: 'Map and zone authoring' })).not.toBeInTheDocument()
  fireEvent.click(tabs.getByRole('button', { name: 'Map authoring' }))
  expect(screen.getByRole('textbox', { name: 'Map version' })).toHaveValue('operator-entered-version')
  expect(screen.getByText('No authored objects.')).toBeInTheDocument()
})

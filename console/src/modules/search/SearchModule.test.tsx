import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import App from '../../App'
import { C1_BASIC_CONTROL_INTENTS, type IntentV1 } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import type { SearchClient } from '../../search/client'
import { searchPreview, searchStatus } from '../../search/client.test'

const session = 'search-module-session'
const T0 = 1_756_700_000_000

function mount(search: SearchClient) {
  let sequence = 0
  const clients = {
    console: new FixtureRelayClient(session, () => T0, 'console'),
    keyboard: new FixtureRelayClient(session, () => T0, 'keyboard'),
  }
  render(<App sessionId={session} clients={clients} initialModule="search"
    intentDependencies={{ now: () => T0, nextId: () => `search-intent-${++sequence}` }} services={{ search }} />)
  act(() => clients.console.emitServer({
    v: 1, t: T0, type: 'state', event_id: 'search-enabled', session, roster_version: 7,
    armed: true, estop: false, selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor',
    pending: null, accepted_plan: null, capability_profile: 'c1_basic_control.search',
    enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'search'], drones: fixtureAircraft(T0),
  }))
  return clients
}

describe('Search module', () => {
  test('previews the frozen search route, confirms its same intent, and acknowledges findings without motion', async () => {
    const catalog = vi.fn(async () => ({ target_classes: ['backpack'], zones: ['lobby'] }))
    const preview = vi.fn(async (intent: IntentV1) => ({
      ...searchPreview(),
      session: intent.session,
      intent_id: intent.intent_id,
    }))
    const status = vi.fn(async (_session: string, intentId: string) => ({
      ...searchStatus(),
      intent_id: intentId,
    }))
    const acknowledge = vi.fn(async (_session: string, intentId: string) => ({
      ...searchStatus(true),
      intent_id: intentId,
    }))
    const clients = mount({ catalog, preview, status, acknowledge })
    await screen.findByText(/Development fixture active/i)
    const u = userEvent.setup()

    await u.click(await screen.findByRole('button', { name: 'Preview search' }))
    const dock = await screen.findByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('Search for an object')
    expect(clients.console.sent).toHaveLength(0)
    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0].intent_id).toBe(preview.mock.calls[0][0].intent_id)
    expect(clients.console.sent[0]).toMatchObject({ name: 'search', confirm: true, args: { zone_id: 'lobby', target_class: 'backpack' } })

    await screen.findByText(/camera-1 \/ frame frame-1 · box 1, 2, 3, 4/i)
    expect(screen.getByText(/lobby, floor-1 at 1\.0, 1\.0 m/i)).toBeVisible()
    await u.click(await screen.findByRole('button', { name: 'Acknowledge finding' }))
    await waitFor(() => expect(acknowledge).toHaveBeenCalledWith(session, clients.console.sent[0].intent_id, 'sighting/1'))
    expect(clients.console.sent).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Acknowledged' })).toBeDisabled()
  })

  test('tracks and acknowledges only the newest search mission', async () => {
    const catalog = vi.fn(async () => ({ target_classes: ['backpack'], zones: ['lobby'] }))
    const preview = vi.fn(async (intent: IntentV1) => ({
      ...searchPreview(),
      session: intent.session,
      intent_id: intent.intent_id,
    }))
    const status = vi.fn(async (_session: string, intentId: string) => ({
      ...searchStatus(),
      intent_id: intentId,
      candidates: [{ ...searchStatus().candidates[0], sighting_id: `sighting-${intentId}` }],
    }))
    const acknowledge = vi.fn(async (_session: string, intentId: string, sightingId: string) => ({
      ...searchStatus(true),
      intent_id: intentId,
      candidates: [{ ...searchStatus(true).candidates[0], sighting_id: sightingId }],
    }))
    const clients = mount({ catalog, preview, status, acknowledge })
    const u = userEvent.setup()

    await u.click(await screen.findByRole('button', { name: 'Preview search' }))
    await u.click(within(await screen.findByRole('region', { name: 'Pending confirmation' }))
      .getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    const firstIntentId = clients.console.sent[0].intent_id
    await screen.findByRole('button', { name: 'Acknowledge finding' })
    await u.click(screen.getByRole('button', { name: 'Acknowledge finding' }))
    await waitFor(() => expect(acknowledge).toHaveBeenCalledWith(
      session, firstIntentId, `sighting-${firstIntentId}`,
    ))
    expect(screen.getByRole('button', { name: 'Acknowledged' })).toBeDisabled()

    await u.click(screen.getByRole('button', { name: 'Preview search' }))
    await u.click(within(await screen.findByRole('region', { name: 'Pending confirmation' }))
      .getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(2))
    const secondIntentId = clients.console.sent[1].intent_id
    await waitFor(() => expect(status).toHaveBeenCalledWith(session, secondIntentId))
    await screen.findByRole('button', { name: 'Acknowledge finding' })
    expect(screen.queryByRole('button', { name: 'Acknowledged' })).toBeNull()
    await u.click(screen.getByRole('button', { name: 'Acknowledge finding' }))
    await waitFor(() => expect(acknowledge).toHaveBeenLastCalledWith(
      session, secondIntentId, `sighting-${secondIntentId}`,
    ))
  })
})

test('previews a typed target-free survey without an object class', async () => {
  const catalog = vi.fn(async () => ({ target_classes: ['backpack'], zones: ['lobby'] }))
  const resolve = vi.fn(async () => ({
    session,
    correlation_id: 'survey-query',
    source: 'template' as const,
    status: 'resolved' as const,
    detail: 'The room is configured. Preview the coverage route before confirming.',
    zone_id: 'lobby',
    target_class: null,
    mode: 'survey' as const,
  }))
  const preview = vi.fn(async (intent: IntentV1) => ({
    ...searchPreview(),
    session: intent.session,
    intent_id: intent.intent_id,
    preview: { ...searchPreview().preview, target_class: null, mode: 'survey' as const },
  }))
  const clients = mount({
    catalog,
    resolve,
    preview,
    status: vi.fn(async () => ({ ...searchStatus(), mode: 'survey' as const, candidates: [] })),
    acknowledge: vi.fn(),
  })
  const u = userEvent.setup()

  await u.type(await screen.findByLabelText('Describe the search'), 'survey the lobby grid')
  await u.click(screen.getByRole('button', { name: 'Preview request' }))

  await waitFor(() => expect(preview).toHaveBeenCalledTimes(1))
  expect(preview.mock.calls[0][0]).toMatchObject({
    name: 'search',
    args: { zone_id: 'lobby', mode: 'survey' },
  })
  const dock = await screen.findByRole('region', { name: 'Pending confirmation' })
  expect(dock).toHaveTextContent('Survey camera coverage')
  await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
  await waitFor(() => expect(clients.console.sent).toHaveLength(1))
  expect(clients.console.sent[0]).toMatchObject({ args: { zone_id: 'lobby', mode: 'survey' } })
  expect(clients.console.sent[0].args).not.toHaveProperty('target_class')
})

import { render, screen, within } from '@testing-library/react'
import type userEvent from '@testing-library/user-event'
import App from '../App'
import type { CatalogClient } from '../catalog/client'
import {
  FixtureCatalogClient,
  FixtureRelayClient,
  manualScheduler,
  type FixtureFleetSize,
  type FixtureScenarioName,
} from './fixture-relay-client'

export const CATALOG_CLOCK = 1_756_700_000_000
export const CATALOG_SESSION = 'catalog-test-session'

type User = ReturnType<typeof userEvent.setup>

export interface RenderCatalogOptions {
  /** Relay scenario for both sockets; a number is the plain contract fixture. */
  scenario?: FixtureFleetSize | FixtureScenarioName
  /** `unreported` renders App without a catalog client, as production does. */
  catalog?: 'fixture' | 'unreported' | CatalogClient
  now?: () => number
  nextId?: () => string
}

/**
 * Renders the whole console on fixture relay clients with a fixture catalog
 * client driven by a manual scheduler, so job chains advance only in tests.
 */
export function renderCatalogConsole({
  scenario = 'pending4',
  catalog = 'fixture',
  now = () => CATALOG_CLOCK,
  nextId,
}: RenderCatalogOptions = {}) {
  const clients = {
    console: new FixtureRelayClient(CATALOG_SESSION, now, 'console', scenario),
    keyboard: new FixtureRelayClient(CATALOG_SESSION, now, 'keyboard', scenario),
  }
  const scheduler = manualScheduler()
  const catalogClient =
    catalog === 'fixture'
      ? new FixtureCatalogClient(scenario, now, scheduler.schedule)
      : catalog === 'unreported'
        ? undefined
        : catalog
  render(
    <App
      sessionId={CATALOG_SESSION}
      clients={clients}
      catalog={catalogClient}
      intentDependencies={nextId ? { now, nextId } : undefined}
    />,
  )
  return { clients, scheduler, catalog: catalogClient }
}

export async function openModule(user: User, label: string) {
  const rail = within(screen.getByRole('navigation', { name: 'Modules' }))
  await user.click(rail.getByRole('button', { name: label }))
}

export async function openReferenceTab(user: User, label: string) {
  await openModule(user, 'Reference')
  const tabs = within(screen.getByRole('group', { name: 'Reference sections' }))
  await user.click(tabs.getByRole('button', { name: label }))
}

export async function openPaneTab(user: User, group: string, label: string) {
  const tabs = within(screen.getByRole('group', { name: group }))
  await user.click(tabs.getByRole('button', { name: label }))
}

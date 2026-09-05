import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { UnreportedCatalogClient } from './catalog/client.ts'
import { createMediaRuntime } from './media/runtime.ts'
import { bootstrapMediaConfiguration } from './media/runtime-config.ts'
import { createConsoleRuntime } from './relay/runtime.ts'
import {
  FixtureCatalogClient,
  FixtureRelayClient,
  isFixtureScenarioName,
} from './testing/fixture-relay-client.ts'

const requestedFixture = new URLSearchParams(window.location.search).get('fixture')
const fixtureScenario =
  import.meta.env.DEV && requestedFixture !== null && isFixtureScenarioName(requestedFixture)
    ? requestedFixture
    : null
const fixtureSessionId = 'fixture-control-session'
const runtime = fixtureScenario
  ? {
      sessionId: fixtureSessionId,
      client: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'console', fixtureScenario),
      keyboardClient: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'keyboard', fixtureScenario),
      catalogClient: new FixtureCatalogClient(fixtureScenario, () => Date.now()),
    }
  : { ...createConsoleRuntime(), catalogClient: new UnreportedCatalogClient() }
const clients = { console: runtime.client, keyboard: runtime.keyboardClient }
const root = createRoot(document.getElementById('root')!)

// The console renders at once without media; a valid runtime configuration
// re-renders the same tree with playback enabled. Relay state is unaffected.
bootstrapMediaConfiguration((configuration) => {
  root.render(
    <StrictMode>
      <App
        sessionId={runtime.sessionId}
        clients={clients}
        catalog={runtime.catalogClient}
        media={configuration ? createMediaRuntime(configuration) : undefined}
      />
    </StrictMode>,
  )
})

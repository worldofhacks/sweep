import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { UnreportedCatalogClient } from './catalog/client.ts'
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

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App
      sessionId={runtime.sessionId}
      clients={{ console: runtime.client, keyboard: runtime.keyboardClient }}
      catalog={runtime.catalogClient}
    />
  </StrictMode>,
)

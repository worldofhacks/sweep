import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { UnreportedCatalogClient } from './catalog/client.ts'
import { createMediaRuntime } from './media/runtime.ts'
import {
  SAME_ORIGIN_MEDIA_SOURCE,
  bootstrapMediaConfiguration,
  loadMediaRuntimeConfiguration,
} from './media/runtime-config.ts'
import { bootstrapConsoleRuntime } from './relay/bootstrap.ts'
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
const root = createRoot(document.getElementById('root')!)

// The relay bootstrap is resolved once before the runtime exists: the host
// global, else one same-origin read of the bootstrap endpoint. The fixture
// path never reads it. Without either source the console mounts as before,
// visibly disconnected with network controls unavailable.
async function resolveRuntime() {
  if (fixtureScenario) {
    return {
      sessionId: fixtureSessionId,
      client: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'console', fixtureScenario),
      keyboardClient: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'keyboard', fixtureScenario),
      webcamClient: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'webcam', fixtureScenario),
      languageClient: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'language', fixtureScenario),
      // The fixture has no transcription endpoint; the Speech module says so and accepts typed text.
      transcriptClient: null,
      // The fixture has no relay, so only a same-origin media endpoint can enable playback.
      mediaConfigurationSource: null,
      navigationClient: null,
      searchClient: null,
      catalogClient: new FixtureCatalogClient(fixtureScenario, () => Date.now()),
    }
  }
  return { ...(await bootstrapConsoleRuntime()), catalogClient: new UnreportedCatalogClient() }
}

void resolveRuntime().then((runtime) => {
  const clients = {
    console: runtime.client,
    keyboard: runtime.keyboardClient,
    webcam: runtime.webcamClient,
    language: runtime.languageClient,
  }
  const services = {
    transcript: runtime.transcriptClient ?? undefined,
    navigation: runtime.navigationClient ?? undefined,
    search: runtime.searchClient ?? undefined,
  }

  // The console renders at once without media; a valid runtime configuration
  // re-renders the same tree with playback enabled. Relay state is unaffected.
  // The same-origin endpoint (pnpm dev, or a host that serves it) is read first;
  // a built console without one falls back to the relay's copy behind the bearer.
  const mediaSources = runtime.mediaConfigurationSource
    ? [SAME_ORIGIN_MEDIA_SOURCE, runtime.mediaConfigurationSource]
    : [SAME_ORIGIN_MEDIA_SOURCE]
  const loadMedia = () =>
    loadMediaRuntimeConfiguration((input, init) => fetch(input, init), console.warn, mediaSources)
  bootstrapMediaConfiguration((configuration) => {
    root.render(
      <StrictMode>
        <App
          sessionId={runtime.sessionId}
          clients={clients}
          catalog={runtime.catalogClient}
          services={services}
          media={configuration ? createMediaRuntime(configuration) : undefined}
        />
      </StrictMode>,
    )
  }, loadMedia)
})

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { createConsoleRuntime } from './relay/runtime.ts'
import { FixtureRelayClient } from './testing/fixture-relay-client.ts'
import { UnavailableTranscriptClient } from './voice/client.ts'

const useFixture =
  import.meta.env.DEV && new URLSearchParams(window.location.search).get('fixture') === 'control'
const fixtureSessionId = 'fixture-control-session'
const runtime = useFixture
  ? {
      sessionId: fixtureSessionId,
      client: new FixtureRelayClient(fixtureSessionId),
      keyboardClient: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'keyboard'),
      transcriptClient: new UnavailableTranscriptClient(
        'Voice input is unavailable in the development fixture. No audio was sent.',
      ),
    }
  : createConsoleRuntime()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App
      sessionId={runtime.sessionId}
      clients={{ console: runtime.client, keyboard: runtime.keyboardClient }}
      transcriptClient={runtime.transcriptClient}
    />
  </StrictMode>,
)

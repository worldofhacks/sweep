import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import type { MediaRuntimeConfiguration } from './media/playback.ts'
import { createConsoleRuntime } from './relay/runtime.ts'
import { FixtureRelayClient } from './testing/fixture-relay-client.ts'
import { bootstrapMediaConfiguration } from './runtime-config.ts'

const useFixture =
  import.meta.env.DEV && new URLSearchParams(window.location.search).get('fixture') === 'control'
const fixtureSessionId = 'fixture-control-session'
const runtime = useFixture
  ? {
      sessionId: fixtureSessionId,
      client: new FixtureRelayClient(fixtureSessionId),
      keyboardClient: new FixtureRelayClient(fixtureSessionId, () => Date.now(), 'keyboard'),
    }
  : createConsoleRuntime()
const root = createRoot(document.getElementById('root')!)

function render(mediaConfiguration?: MediaRuntimeConfiguration) {
  root.render(
    <StrictMode>
      <App
        sessionId={runtime.sessionId}
        clients={{ console: runtime.client, keyboard: runtime.keyboardClient }}
        mediaConfiguration={mediaConfiguration}
      />
    </StrictMode>,
  )
}

bootstrapMediaConfiguration(render)

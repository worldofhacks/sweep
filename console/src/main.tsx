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
const root = createRoot(document.getElementById('root')!)

// The relay bootstrap is resolved once before the runtime exists: the host
// global, else one same-origin read of the bootstrap endpoint. Every build uses
// real relay data. Without configuration the console is visibly disconnected,
// with unavailable controls and an empty roster. URL parameters cannot create data.
async function resolveRuntime() {
  return { ...(await bootstrapConsoleRuntime()), catalogClient: new UnreportedCatalogClient() }
}

void resolveRuntime().then((runtime) => {
  const clients = {
    console: runtime.client,
    keyboard: runtime.keyboardClient,
    webcam: runtime.webcamClient,
    language: runtime.languageClient,
  }
  const sharedServices = {
    liveDetection: runtime.liveDetectionClient ?? undefined,
    atlas: runtime.atlas ?? undefined,
    transcript: runtime.transcriptClient ?? undefined,
    search: runtime.searchClient ?? undefined,
    multiview: runtime.multiviewClient ?? undefined,
  }
  let services = { ...runtime.platform?.getSnapshot(), ...sharedServices }
  let media: ReturnType<typeof createMediaRuntime> | undefined
  const render = () => root.render(
    <StrictMode>
      <App initialModule="speech" sessionId={runtime.sessionId} clients={clients} catalog={runtime.catalogClient}
        services={services} media={media} relayBaseUrl={runtime.baseUrl ?? undefined}
        mapEndpoint={runtime.mapEndpoint ?? undefined} />
    </StrictMode>,
  )
  runtime.platform?.subscribe((platform) => {
    services = { ...platform, ...sharedServices }
    render()
  })

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
    media = configuration ? createMediaRuntime(configuration) : undefined
    render()
  }, loadMedia)
})

import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'
import { RELAY_BOOTSTRAP_ENDPOINT, relayFromEnvironment } from './src/relay/bootstrap-endpoint.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), runtimeConfiguration(), relayBootstrap()],
})

/**
 * Development-only relay bootstrap, the same pattern as the media endpoint
 * below: built from the relay's own variables at request time so the token
 * never enters the bundle or a URL. Without SWEEP_RELAY_TOKEN it answers 503
 * and the console runs visibly disconnected. A production host serves the same
 * JSON at this path or sets window.__SWEEP_RELAY_CONFIG__ itself.
 */
function relayBootstrap(): Plugin {
  return {
    name: 'sweep-relay-bootstrap',
    configureServer(server) {
      server.middlewares.use(RELAY_BOOTSTRAP_ENDPOINT, (_request, response) => {
        const relay = relayFromEnvironment(process.env)
        response.setHeader('Cache-Control', 'no-store')
        response.setHeader('Content-Type', 'application/json')
        if (!relay) {
          response.statusCode = 503
          response.end(JSON.stringify({ relay: null }))
          return
        }
        response.end(JSON.stringify({ relay }))
      })
    },
  }
}

/**
 * Development-only runtime endpoint, ported from PR #68 (feat/m31-media-ingest,
 * console/vite.config.ts). Serves the media configuration from the environment
 * so credentials never enter the bundle; without a complete set of variables it
 * answers 503 and the console runs with playback disabled. Reconcile when #68
 * merges.
 */
function runtimeConfiguration(): Plugin {
  return {
    name: 'sweep-runtime-configuration',
    configureServer(server) {
      server.middlewares.use('/runtime-config.json', (_request, response) => {
        const media = mediaFromEnvironment(process.env)
        response.setHeader('Cache-Control', 'no-store')
        response.setHeader('Content-Type', 'application/json')
        if (!media) {
          response.statusCode = 503
          response.end(JSON.stringify({ media: null }))
          return
        }
        response.end(JSON.stringify({ media }))
      })
    },
  }
}

function mediaFromEnvironment(env: NodeJS.ProcessEnv) {
  const webrtcOrigin = env.SWEEP_MEDIA_WEBRTC_ORIGIN
  const readerUsername = env.SWEEP_MEDIA_READ_USERNAME
  const readerPassword = env.SWEEP_MEDIA_READ_PASSWORD
  if (!webrtcOrigin || !readerUsername || !readerPassword) return null
  return { webrtcOrigin, readerUsername, readerPassword }
}

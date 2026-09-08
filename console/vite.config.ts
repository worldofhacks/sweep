import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin, type ServerOptions } from 'vite'
import { RELAY_BOOTSTRAP_ENDPOINT, relayFromEnvironment } from './src/relay/bootstrap-endpoint.ts'

const CANONICAL_CONSOLE_PORT = 5173
const M14_BROWSER_PORT = 14173

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), fixedConsolePort(), runtimeConfiguration(), relayBootstrap()],
  // A second process must not silently become a competing operator console.
  server: { host: '127.0.0.1', port: CANONICAL_CONSOLE_PORT, strictPort: true },
  preview: { host: '127.0.0.1', port: CANONICAL_CONSOLE_PORT, strictPort: true },
})

function fixedConsolePort(): Plugin {
  return {
    name: 'sweep-single-console-port',
    configResolved(config) {
      if (config.command !== 'serve') return
      assertConsoleEndpoint(config.server, expectedServerPort(process.env))
      assertConsoleEndpoint(config.preview, CANONICAL_CONSOLE_PORT)
    },
  }
}

export function expectedServerPort(env: NodeJS.ProcessEnv): number {
  return env.SWEEP_CONSOLE_TEST_MODE === 'm14-browser' ? M14_BROWSER_PORT : CANONICAL_CONSOLE_PORT
}

export function assertConsoleEndpoint(
  endpoint: Pick<ServerOptions, 'host' | 'port' | 'strictPort'>,
  expectedPort: number,
) {
  if (endpoint.port !== expectedPort || !endpoint.strictPort || endpoint.host !== '127.0.0.1') {
    throw new Error('Sweep uses one laptop console at http://127.0.0.1:5173/. Use python3 tools/console.py start from the canonical checkout.')
  }
}

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

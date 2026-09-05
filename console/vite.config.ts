import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), runtimeConfiguration()],
})

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

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import type { Plugin } from 'vite'

export default defineConfig({
  plugins: [react(), runtimeConfiguration()],
})

function runtimeConfiguration(): Plugin {
  return {
    name: 'sweep-runtime-configuration',
    configureServer(server) {
      server.middlewares.use('/runtime-config.json', (_request, response) => {
        response.setHeader('Cache-Control', 'no-store')
        response.setHeader('Content-Type', 'application/json')
        response.end(JSON.stringify({
          media: {
            webrtcOrigin: required('SWEEP_MEDIA_WEBRTC_ORIGIN'),
            hlsOrigin: required('SWEEP_MEDIA_HLS_ORIGIN'),
            readerUsername: required('SWEEP_MEDIA_READ_USERNAME'),
            readerPassword: required('SWEEP_MEDIA_READ_PASSWORD'),
          },
        }))
      })
    },
  }
}

function required(name: string): string {
  const value = process.env[name]
  if (!value) throw new Error(`${name} is required`)
  return value
}

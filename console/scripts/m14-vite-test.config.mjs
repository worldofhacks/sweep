import { resolve } from 'node:path'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Only the automated smoke harness uses this server. The production Vite
// configuration continues enforcing the single operator console on port 5173.
const port = Number(process.env.SWEEP_M14_TEST_PORT)
const directory = process.env.SWEEP_M14_TEST_DIRECTORY
const runId = process.env.SWEEP_M14_TEST_RUN_ID
if (process.env.SWEEP_M14_BROWSER_TEST !== '1' || !directory || !runId ||
    !Number.isInteger(port) || port < 1024 || port > 65535 || [5173, 8010].includes(port)) {
  throw new Error('This configuration requires the isolated M14 browser test harness.')
}

export default defineConfig(({ command }) => {
  if (command !== 'serve') throw new Error('The browser fixture configuration cannot build assets.')
  return {
    root: resolve(import.meta.dirname, '..'),
    envDir: false,
    cacheDir: resolve(directory, 'vite-cache'),
    plugins: [react(), {
      name: 'm14-test-transport',
      enforce: 'pre',
      transform(source, id) {
        if (id.split('?')[0] !== resolve(import.meta.dirname, '../src/relay/client.ts')) return
        const guard = 'if (containsSyntheticAdapter(event)) {'
        if (source.split(guard).length !== 2) {
          throw new Error('The isolated test transport no longer matches its guarded source.')
        }
        // This historical mission deliberately exercises simulated device I/O.
        // Only its ephemeral server compiles the test transport; production source
        // and builds continue refusing synthetic frames, covered by client tests.
        return source.replace(guard, 'if (false) {')
      },
    }, {
      name: 'm14-test-identity',
      configureServer(server) {
        server.middlewares.use(`/__m14_test__/${runId}`, (_request, response) => {
          response.setHeader('Cache-Control', 'no-store')
          response.end('isolated-m14-browser-test')
        })
      },
    }],
    server: { host: '127.0.0.1', port, strictPort: true, open: false },
  }
})

import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { chromium } from 'playwright'

const consoleRoot = resolve(import.meta.dirname, '..')
const repositoryRoot = resolve(consoleRoot, '..')
const relayPort = 18765
const consolePort = 14173
const sessionId = 'm14-browser-smoke'
const relayToken = 'm14-browser-console-key-32-bytes'
const logDirectory = await mkdtemp(join(tmpdir(), 'sweep-m14-browser-'))
const processes = []
let browser

try {
  processes.push(
    spawn(resolve(repositoryRoot, '.venv/bin/python'), ['-m', 'uvicorn', 'adapters.sim.app:app', '--host', '127.0.0.1', '--port', String(relayPort)], {
      cwd: repositoryRoot,
      env: {
        ...process.env,
        SWEEP_RELAY_TOKEN: relayToken,
        SWEEP_ADAPTER_KEYS_JSON: JSON.stringify({
          1: 'm14-browser-adapter-one-key-32-bytes',
          2: 'm14-browser-adapter-two-key-32-bytes',
        }),
        SWEEP_SESSION_LOG_DIR: logDirectory,
      },
      stdio: 'inherit',
    }),
  )
  processes.push(
    spawn(resolve(consoleRoot, 'node_modules/.bin/vite'), ['--host', '127.0.0.1', '--port', String(consolePort), '--strictPort'], {
      cwd: consoleRoot,
      env: process.env,
      stdio: 'inherit',
    }),
  )

  await Promise.all([
    waitForHttp(`http://127.0.0.1:${relayPort}/docs`),
    waitForHttp(`http://127.0.0.1:${consolePort}/`),
  ])
  browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()
  await page.addInitScript(
    ({ baseUrl, sessionId, token }) => {
      window.__SWEEP_RELAY_CONFIG__ = { baseUrl, sessionId, token }
    },
    { baseUrl: `ws://127.0.0.1:${relayPort}`, sessionId, token: relayToken },
  )
  await page.goto(`http://127.0.0.1:${consolePort}/`)
  await page.getByText('Console connected').waitFor()
  await page.getByRole('button', { name: /D-01 Ready epoch 1 Select/i }).click()
  await page.getByRole('button', { name: /D-02 Ready epoch 1 Select/i }).click()
  await page.getByRole('button', { name: /^Arm session/ }).click()
  await page.getByRole('button', { name: /^Take off selected/ }).click()
  await page.getByRole('button', { name: 'Confirm and send' }).click()

  for (const droneId of ['D-01', 'D-02']) {
    await page.locator('.drone-state').filter({ hasText: droneId }).getByText('Hovering').waitFor()
  }

  await page.getByRole('button', { name: /^Translate forward/ }).click()
  const translate = page.locator('.request-item').filter({ hasText: 'Translate' }).first()
  await translate.locator('.status-completed').waitFor()
  if (!(await translate.getByText('D-01, D-02 · console').isVisible())) {
    throw new Error('browser translation did not target both production simulator nodes')
  }
} finally {
  await browser?.close()
  for (const child of processes.reverse()) await stop(child)
  await rm(logDirectory, { recursive: true, force: true })
}

async function waitForHttp(url) {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    let ready
    try {
      const response = await fetch(url)
      ready = response.ok
    } catch {
      ready = false
    }
    if (ready) return
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
  }
  throw new Error(`service did not become ready: ${url}`)
}

async function stop(child) {
  if (child.exitCode !== null) return
  child.kill('SIGTERM')
  await Promise.race([
    new Promise((resolvePromise) => child.once('exit', resolvePromise)),
    new Promise((resolvePromise) => setTimeout(resolvePromise, 2_000)),
  ])
  if (child.exitCode === null) child.kill('SIGKILL')
}

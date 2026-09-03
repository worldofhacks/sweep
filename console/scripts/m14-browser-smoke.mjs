import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
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
  await waitForRequest(page, 'Select', 'completed')
  await page.getByRole('button', { name: /D-02 Ready epoch 1 Select/i }).click()
  await waitForRequest(page, 'Select', 'completed')
  await page.getByRole('button', { name: /^Arm session/ }).click()
  await waitForRequest(page, 'Arm', 'completed')
  await page.getByRole('button', { name: /^Take off selected/ }).click()
  await page.getByRole('button', { name: 'Confirm and send' }).click()
  await waitForRequest(page, 'Takeoff', 'completed')

  for (const droneId of ['D-01', 'D-02']) {
    await page.locator('.drone-state').filter({ hasText: droneId }).getByText('Hovering').waitFor()
  }

  await page.getByRole('button', { name: /^Translate forward/ }).click()
  const translate = await waitForRequest(page, 'Translate', 'completed')
  if (!(await translate.getByText('D-01, D-02 · console').isVisible())) {
    throw new Error('browser translation did not target both production simulator nodes')
  }

  await page.getByRole('button', { name: /^Hold selected/ }).click()
  await waitForRequest(page, 'Hold', 'completed')
  await page.getByRole('button', { name: /^Come home/ }).click()
  await waitForRequest(page, 'Come home', 'completed')

  let geofenceRefused = false
  for (let step = 0; step < 24; step += 1) {
    await page.getByRole('button', { name: /^Translate forward/ }).click()
    const request = page.locator('.request-item').filter({ hasText: 'Translate' }).first()
    await request.locator('.status-completed, .status-refused').waitFor()
    if (await request.locator('.status-refused').isVisible()) {
      if (!(await request.locator('code').filter({ hasText: /^geofence$/ }).isVisible())) {
        throw new Error('browser geofence refusal did not expose its safety reason')
      }
      geofenceRefused = true
      break
    }
  }
  if (!geofenceRefused) throw new Error('browser workflow did not reach the geofence refusal')

  const silence = await fetch(
    `http://127.0.0.1:${relayPort}/sim/${sessionId}/nodes/1/silence`,
    { method: 'POST', headers: { Authorization: `Bearer ${relayToken}` } },
  )
  if (!silence.ok) throw new Error(`simulator silence control failed: ${silence.status}`)
  await waitForRelayEvent(
    (event) => event.type === 'safety_action' && event.drone_id === 1 && event.action === 'hold',
    5_000,
  )
  await page.getByText('Aircraft hold').waitFor({ timeout: 3_000 })
  await waitForRelayEvent(
    (event) => event.type === 'safety_action' && event.drone_id === 1 && event.action === 'failsafe',
    15_000,
  )
  await page.getByText('Aircraft failsafe').waitFor({ timeout: 3_000 })

  await page.getByRole('button', { name: 'Network E-stop' }).click()
  await waitForRequest(page, 'Estop', 'completed')
  await page.getByText('Network stop active').waitFor()
  await page.getByRole('button', { name: /^Land all/ }).click()
  await page.getByRole('button', { name: 'Confirm and send' }).click()
  await waitForRequest(page, 'Land all', 'completed')
  const resume = await fetch(
    `http://127.0.0.1:${relayPort}/sim/${sessionId}/nodes/1/resume`,
    { method: 'POST', headers: { Authorization: `Bearer ${relayToken}` } },
  )
  if (!resume.ok) throw new Error(`simulator recovery control failed: ${resume.status}`)
  for (const droneId of ['D-01', 'D-02']) {
    await page.locator('.drone-state').filter({ hasText: droneId }).getByText('Landed').waitFor()
  }

  const logFiles = (await readdir(logDirectory)).filter((name) => name.endsWith('.jsonl'))
  if (logFiles.length !== 1) throw new Error(`expected one session log, found ${logFiles.length}`)
  const records = (await readFile(join(logDirectory, logFiles[0]), 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line))
  assertRunEvidence(records)
} finally {
  await browser?.close()
  for (const child of processes.reverse()) await stop(child)
  await rm(logDirectory, { recursive: true, force: true })
}

async function waitForRequest(page, name, status) {
  const request = page.locator('.request-item').filter({ hasText: name }).first()
  await request.locator(`.status-${status}`).waitFor()
  return request
}

function assertRunEvidence(records) {
  const events = records.map((record) => record.event)
  const names = events
    .filter((event) => event.type === 'intent_record')
    .map((event) => event.intent.name)
  for (const required of ['select', 'arm', 'takeoff', 'translate', 'hold', 'come_home', 'estop', 'land_all']) {
    if (!names.includes(required)) throw new Error(`session log is missing ${required}`)
  }
  if (!events.some((event) => event.type === 'refusal' && event.reason === 'geofence')) {
    throw new Error('session log is missing the geofence refusal')
  }
  for (const action of ['hold', 'failsafe']) {
    if (!events.some((event) => event.type === 'safety_action' && event.action === action)) {
      throw new Error(`session log is missing the ${action} link-loss action`)
    }
  }
  if (containsKey(records, 'signature') || JSON.stringify(records).includes(relayToken)) {
    throw new Error('session log contains authentication material')
  }
}

function containsKey(value, target) {
  if (Array.isArray(value)) return value.some((item) => containsKey(item, target))
  if (value !== null && typeof value === 'object') {
    return Object.entries(value).some(
      ([key, item]) => key === target || containsKey(item, target),
    )
  }
  return false
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

async function waitForRelayEvent(predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const response = await fetch(`http://127.0.0.1:${relayPort}/session/${sessionId}`, {
      headers: { Authorization: `Bearer ${relayToken}` },
    })
    if (response.ok) {
      const replay = await response.json()
      if (replay.events.some((record) => predicate(record.event))) return
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
  }
  throw new Error('relay evidence did not reach the expected state before timeout')
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

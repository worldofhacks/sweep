/** Scripted Flight buttons against exactly two in-process simulated aircraft.
 * This proves the production console/relay/planner/arbiter/adapter path, not
 * human gesture recognition, Android command transport, or physical flight.
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { mkdir, open, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { resolve, join } from 'node:path'
import { StringDecoder } from 'node:string_decoder'
import { chromium } from 'playwright'

const consoleRoot = resolve(import.meta.dirname, '..')
const repositoryRoot = resolve(consoleRoot, '..')
const relayPort = 18876
const consolePort = 14186
const relayUrl = `http://127.0.0.1:${relayPort}`
const consoleUrl = `http://127.0.0.1:${consolePort}`
const runId = `${Date.now()}-${randomBytes(4).toString('hex')}`
const session = `two-drone-flight-${runId}`
const token = randomBytes(32).toString('hex')
const adapterKeys = { 1: randomBytes(32).toString('hex'), 2: randomBytes(32).toString('hex') }
const secrets = [token, ...Object.values(adapterKeys)]
const output = resolve(repositoryRoot, 'output/playwright', session)
const auditDirectory = join(output, 'audit')
const auditPath = join(auditDirectory, `${createHash('sha256').update(session).digest('hex')}.jsonl`)
const children = []
const serviceLogs = []
const checks = []
const records = []
const decoder = new StringDecoder('utf8')
let auditOffset = 0
let partialLine = ''
let browser
let page
let failure

// Deliberate environment allowlist: no inherited SWEEP/VITE/provider settings,
// .env loading, credential file, remote backend or hardware relay is used.
const cleanEnv = Object.fromEntries(['PATH', 'HOME', 'LANG', 'TMPDIR', 'SYSTEMROOT'].filter((key) => process.env[key]).map((key) => [key, process.env[key]]))

try {
  await mkdir(auditDirectory, { recursive: true })
  await Promise.all([unusedPort(relayPort), unusedPort(consolePort)])
  launch('simulator', resolve(repositoryRoot, '.venv/bin/python'), ['-m', 'uvicorn', 'adapters.sim.app:app', '--host', '127.0.0.1', '--port', String(relayPort)], repositoryRoot, {
    ...cleanEnv,
    SWEEP_RELAY_TOKEN: token,
    SWEEP_ADAPTER_KEYS_JSON: JSON.stringify(adapterKeys),
    SWEEP_ADAPTER_BACKEND: 'sim',
    SWEEP_SESSION_LOG_DIR: auditDirectory,
    SWEEP_CONSOLE_ORIGINS: consoleUrl,
  })
  // Run the built production bundle. Build explicitly before invoking this script.
  launch('console', resolve(consoleRoot, 'node_modules/.bin/vite'), ['preview', '--host', '127.0.0.1', '--port', String(consolePort), '--strictPort'], consoleRoot, cleanEnv)
  await Promise.all([ready(relayUrl + '/docs'), ready(consoleUrl + '/')])
  browser = await chromium.launch({ headless: true })
  page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  page.setDefaultTimeout(10_000)
  await page.route('**/*', (route) => {
    const origin = new URL(route.request().url()).origin
    return origin === relayUrl || origin === consoleUrl ? route.continue() : route.abort()
  })
  await page.addInitScript(({ baseUrl, sessionId, token }) => {
    window.__SWEEP_RELAY_CONFIG__ = { baseUrl, sessionId, token }
  }, { baseUrl: `ws://127.0.0.1:${relayPort}`, sessionId: session, token })
  await page.goto(consoleUrl)
  await page.getByRole('navigation', { name: 'Modules' }).getByRole('button', { name: 'Gesture', exact: true }).click()
  await page.getByRole('radio', { name: 'Flight (opt in)' }).check()
  await until(async () => {
    const states = (await events()).filter((event) => event.type === 'state')
    return states.some((event) => event.drones.length === 2 && event.drones.every((drone) => drone.membership === 'ready' && drone.adapter_capabilities.includes('body_pulse_v1')))
  }, 'two ready simulated nodes with pulse support')
  const target = page.getByRole('group', { name: 'Target' })
  await target.getByRole('button', { name: 'Select D-01', exact: true }).click()
  await until(async () => selected(await events(), [1]), 'D-01 selection')
  assert.equal(await page.getByRole('button', { name: 'Enable tracking', exact: true }).getAttribute('aria-pressed'), 'false')
  await action('Arm session', 'arm', [], {})
  for (const ids of [[1], [1, 2]]) {
    if (ids.length === 2) {
      await target.getByRole('button', { name: 'Select D-02', exact: true }).click()
      await until(async () => selected(await events(), ids), 'both nodes selected')
    }
    await action('Takeoff selected', 'takeoff', ids, {})
    await pulse('Forward 0.5 seconds', ids, 250)
    await pulse('Backward 0.5 seconds', ids, -250)
    await action('Land selected', 'land', ids, {})
    await page.screenshot({ path: join(output, `landed-${ids.length}-selected.png`), fullPage: true })
  }
  const all = await events()
  const joined = [...new Set(all.filter((event) => event.type === 'membership').map((event) => event.drone_id))].sort()
  assert.deepEqual(joined, [1, 2], 'only two simulator nodes participated')
  assert.equal(all.some((event) => event.type === 'refusal' || event.type === 'safety_action'), false, 'unexpected refusal or watchdog action')
  assert.equal(all.filter((event) => event.type === 'intent_record').some((event) => event.intent?.source !== 'console'), false)
  // One final authenticated replay proves the HTTP route and its redaction.
  // Acceptance polling above reads appended local records instead of replaying HTTP history.
  const response = await fetch(`${relayUrl}/session/${session}`, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5000) })
  assert.equal(response.ok, true, 'authenticated replay')
  const replay = await response.json()
  assert.ok(replay.events.length > 0)
  for (const check of checks) assert.ok(replay.events.some((record) => record.event.type === 'autonomy_result' && record.event.intent_id === check.intent_id && record.event.status === 'completed'))
  assert.equal(containsKey(replay, 'signature'), false)
  assert.equal(secrets.some((secret) => JSON.stringify(replay).includes(secret)), false)
  assert.equal(containsKey(records, 'signature'), false)
  assert.equal(secrets.some((secret) => JSON.stringify(records).includes(secret)), false)
} catch (error) {
  failure = error
  if (page) await Promise.allSettled([
    page.screenshot({ path: join(output, 'failure.png'), fullPage: true }),
    page.locator('body').innerText().then((text) => writeFile(join(output, 'failure.txt'), redact(text))),
  ])
} finally {
  const cleanup = await Promise.allSettled([browser?.close(), ...children.map(stop)])
  const rejected = cleanup.filter((result) => result.status === 'rejected')
  if (rejected.length && !failure) failure = new Error('Browser/process cleanup failed')
  await writeFile(join(output, 'services.log'), redact(serviceLogs.join('')))
  await writeFile(join(output, 'summary.json'), JSON.stringify({
    passed: !failure, session, input: 'Scripted Flight button clicks; camera tracking remains off',
    execution: 'Production console and authenticated relay, planner, arbiter and in-process SimFlightAdapter; signed simulated membership/telemetry. No Android transport or physical flight.',
    relayOrigin: relayUrl, consoleOrigin: consoleUrl, nodes: [1, 2], checks,
    cleanup: rejected.length === 0, ...(failure ? { error: redact(failure.message) } : {}),
  }, null, 2) + '\n')
}
if (failure) throw failure
console.log(`Two-drone Flight browser smoke passed: ${output}`)

async function action(label, name, ids, args) {
  const flight = page.getByRole('region', { name: 'Selected flight actions' })
  await flight.getByRole('button', { name: label, exact: true }).click()
  const dock = page.getByRole('region', { name: 'Pending confirmation' })
  await dock.waitFor()
  const intent = JSON.parse(await dock.locator('pre').innerText())
  assert.equal(intent.name, name)
  assert.equal(intent.source, 'console')
  assert.equal(intent.confirm, false)
  assert.deepEqual(intent.selection, ids)
  assert.deepEqual(intent.args, args)
  assert.equal((await events()).some((event) => event.intent_id === intent.intent_id || event.intent?.intent_id === intent.intent_id), false, 'draft emitted before confirmation')
  await dock.getByRole('button', { name: 'Confirm and send', exact: true }).click()
  const terminal = await until(async () => {
    const related = (await events()).filter((event) => event.intent_id === intent.intent_id)
    const rejected = related.find((event) => ['refused', 'failed', 'invalidated'].includes(event.status))
    if (rejected) throw new Error(`${label} failed: ${JSON.stringify(rejected)}`)
    return related.find((event) => event.type === 'autonomy_result' && event.status === 'completed')
  }, `${label} completed`)
  const all = await events()
  const accepted = all.filter((event) => event.type === 'intent_record' && event.intent?.intent_id === intent.intent_id)
  assert.ok(accepted.length > 0)
  for (const event of accepted) assert.deepEqual(event.intent, { ...intent, t: event.intent.t, confirm: true })
  assert.equal(terminal.result.status, 'completed')
  if (name === 'arm') assert.equal(terminal.result.plan.armed_update, true)
  else assert.deepEqual(terminal.result.plan.selection, ids)
  const commands = terminal.result.plan.commands
  assert.deepEqual(commands.map((command) => command.drone_id).sort(), ids, 'exact per-node command scope')
  for (const command of commands) {
    assert.equal(command.operation, name)
    assert.equal(command.intent_id, intent.intent_id)
    if (name === 'body_pulse') assert.deepEqual(command.parameters, args)
    assert.ok(terminal.result.acknowledgements.some((ack) => ack.command_id === command.command_id && ack.drone_id === command.drone_id && ack.status === 'completed'), 'per-node adapter completion')
  }
  if (name === 'takeoff' || name === 'land') {
    await until(async () => {
      const all = await events()
      return ids.every((id) => newestTelemetry(all, id)?.state === (name === 'takeoff' ? 'hovering' : 'landed'))
    }, `${label} telemetry`)
  }
  checks.push({ intent_id: intent.intent_id, name, selection: ids, args, status: terminal.status, command_ids: commands.map((command) => command.command_id) })
  return terminal
}

async function pulse(label, ids, speed) {
  const before = Object.fromEntries([1, 2].map((id) => [id, newestTelemetry(records.map((record) => record.event), id)]))
  const terminal = await action(label, 'body_pulse', ids, { forward_mm_s: speed, duration_ms: 500 })
  const after = await until(async () => {
    const all = await events()
    const positions = Object.fromEntries([1, 2].map((id) => [id, newestTelemetry(all, id)]))
    return [1, 2].every((id) => positions[id]?.t > terminal.t && positions[id].t > before[id].t && Math.abs(positions[id].y - (before[id].y + (ids.includes(id) ? speed * 500 / 1_000_000 : 0))) < 1e-6) && positions
  }, `${label} fresh displacement telemetry`)
  for (const id of [1, 2]) {
    assert.equal(after[id].x, before[id].x)
    assert.equal(after[id].z, before[id].z)
  }
  checks.at(-1).telemetry = { before, after, result_t: terminal.t }
}

function newestTelemetry(all, id) { return all.filter((event) => event.type === 'telemetry' && event.drone === id).at(-1) }
function selected(all, ids) { return JSON.stringify(all.filter((event) => event.type === 'state').at(-1)?.selection) === JSON.stringify(ids) }

async function events() {
  let file
  try { file = await open(auditPath, 'r') } catch (error) { if (error.code === 'ENOENT') return []; throw error }
  try {
    const stat = await file.stat()
    assert.ok(stat.size >= auditOffset, 'append-only audit unexpectedly shrank')
    if (stat.size > auditOffset) {
      const buffer = Buffer.alloc(stat.size - auditOffset)
      const { bytesRead } = await file.read(buffer, 0, buffer.length, auditOffset)
      auditOffset += bytesRead
      partialLine += decoder.write(buffer.subarray(0, bytesRead))
      const lines = partialLine.split('\n')
      partialLine = lines.pop()
      for (const line of lines) if (line) records.push(JSON.parse(line))
    }
  } finally { await file.close() }
  return records.map((record) => record.event)
}

function launch(label, command, args, cwd, env) {
  const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] })
  children.push(child)
  child.on('error', (error) => { child.spawnFailure = error })
  for (const stream of [child.stdout, child.stderr]) stream.on('data', (data) => {
    serviceLogs.push(`[${label}] ${data}`)
    if (serviceLogs.length > 1000) serviceLogs.shift()
  })
}

async function until(predicate, label, timeout = 15_000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    for (const child of children) {
      if (child.spawnFailure) throw child.spawnFailure
      if (child.exitCode !== null || child.signalCode !== null) throw new Error(`Owned service exited while waiting for ${label}`)
    }
    const value = await predicate()
    if (value) return value
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
  }
  throw new Error(`Timed out waiting for ${label}`)
}

async function ready(url) {
  return until(async () => { try { return (await fetch(url, { signal: AbortSignal.timeout(1000) })).ok } catch { return false } }, url)
}
async function unusedPort(port) {
  const server = createServer()
  await new Promise((resolvePromise, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolvePromise) })
  await new Promise((resolvePromise, reject) => server.close((error) => error ? reject(error) : resolvePromise()))
}
async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null || child.spawnFailure) return
  const exited = new Promise((resolvePromise) => child.once('exit', resolvePromise))
  child.kill('SIGTERM')
  const timer = setTimeout(() => child.kill('SIGKILL'), 2000)
  try { await exited } finally { clearTimeout(timer) }
}
function redact(text) { return secrets.reduce((value, secret) => value.replaceAll(secret, '[redacted]'), text) }
function containsKey(value, key) {
  if (Array.isArray(value)) return value.some((item) => containsKey(item, key))
  return value !== null && typeof value === 'object' && Object.entries(value).some(([name, item]) => name === key || containsKey(item, key))
}

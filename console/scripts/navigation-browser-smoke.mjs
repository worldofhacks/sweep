import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { chromium } from 'playwright'

const root = resolve(import.meta.dirname, '../..')
const session = `navigation-browser-${Date.now()}`
const directory = join(root, 'output/playwright', session)
await mkdir(directory, { recursive: true })
const service = spawn(join(root, '.venv/bin/python'), ['-m', 'adapters.sim.demo', '--navigation-demo',
  '--count', '4', '--session', session, '--console-dist', join(root, 'console/dist'),
  '--log-dir', join(directory, 'audit')], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] })
let serviceLog = '', browser, page, baseUrl, token, sequence = 0
const events = [], errors = [], evidence = { session, aircraft: 'four signed simulated nodes', destinations: [] }
service.stderr.on('data', chunk => { serviceLog += chunk.toString() })

try {
  const ready = await new Promise((resolve, reject) => {
    let buffer = ''
    const timer = setTimeout(() => reject(new Error('Navigation demo startup timed out')), 20000)
    service.once('exit', code => { clearTimeout(timer); reject(new Error(`Demo exited ${code}`)) })
    service.once('error', reject)
    service.stdout.on('data', chunk => {
      buffer += chunk.toString()
      const lines = buffer.split('\n'); buffer = lines.pop()
      for (const line of lines) {
        try { const value = JSON.parse(line); if (value.type === 'demo.ready') { clearTimeout(timer); resolve(value) } }
        catch { /* Startup logging may precede the readiness envelope. */ }
      }
    })
  })
  baseUrl = ready.console_url
  token = (await (await fetch(`${baseUrl}/relay-bootstrap.json`)).json()).relay.token
  browser = await chromium.launch()
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(baseUrl)
  await pane('Swarm')
  await waitUntil(async () => (await status()).drones.every(drone => drone.membership === 'ready'))
  await command('Arm', 'arm')
  await page.locator('.ct-select-all').click()
  await outcome('select')
  await command(/^Takeoff/, 'takeoff', true)
  await pane('Fleet')
  for (const id of [2, 3, 4]) {
    const after = (await readEvents()).length
    await page.getByRole('region', { name: 'Registry', exact: true }).getByRole('button', { name: `Deselect D-0${id}`, exact: true }).click()
    await outcome('select', undefined, after)
  }
  const stationary = (await status()).drones.filter(drone => drone.drone_id !== 1).map(drone => drone.telemetry)
  await pane('Routes')
  await page.getByLabel('Destination').selectOption('lobby')
  await page.getByRole('button', { name: 'Preview route', exact: true }).click()
  await dock().waitFor()
  await dock().getByRole('button', { name: 'Cancel', exact: true }).click()
  assert.equal((await readEvents()).filter(event => event.type === 'intent_record' && event.intent.name === 'navigate').length, 0)

  for (const zone of ['kitchen', 'lobby', 'formation-one', 'formation-two', 'atrium']) {
    await page.getByLabel('Destination').selectOption(zone)
    const response = page.waitForResponse(value => value.url().endsWith('/navigation/preview'))
    await page.getByRole('button', { name: 'Preview route', exact: true }).click()
    const previewResponse = await response
    const preview = await previewResponse.json()
    assert.equal(previewResponse.status(), 200, JSON.stringify(preview))
    assert.deepEqual(preview.plan.selection, [1])
    const route = preview.plan.navigation.route.routes[0]
    await dock().getByRole('img', { name: `Planned routes to ${zone}` }).waitFor()
    await page.screenshot({ path: join(directory, `${zone}-preview.png`), fullPage: true, animations: 'disabled' })
    await dock().getByRole('button', { name: 'Confirm and send', exact: true }).click()
    await outcome('navigate', preview.intent_id)
    const end = route.waypoints.at(-1)
    await waitUntil(async () => {
      const aircraft = (await status()).drones.find(drone => drone.drone_id === 1)
      return aircraft.telemetry.state === 'hovering' && ['x', 'y', 'z'].every(axis => Math.abs(aircraft.telemetry[axis] - end[`${axis}_m`]) < .06)
    })
    const actual = (await readEvents()).filter(event => event.type === 'command' && event.intent_id === preview.intent_id)
    assert.ok(actual.length > 1)
    assert.ok(actual.every(event => event.drone_id === 1))
    evidence.destinations.push({ zone, intent_id: preview.intent_id, waypoints: route.waypoints, commands: actual.length })
  }
  const after = (await status()).drones.filter(drone => drone.drone_id !== 1).map(drone => drone.telemetry)
  assert.deepEqual(after.map(pose => [pose.x, pose.y, pose.z]), stationary.map(pose => [pose.x, pose.y, pose.z]))
  await pane('Swarm')
  await command(/^Land all/, 'land_all', true)
  assert.deepEqual(errors, [])
  await writeFile(join(directory, 'evidence.json'), JSON.stringify(evidence, null, 2))
  console.log(`PASS: ${directory}`)
} catch (error) {
  await page?.screenshot({ path: join(directory, 'failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
  throw error
} finally {
  await writeFile(join(directory, 'service.log'), serviceLog)
  await browser?.close()
  service.kill('SIGTERM')
}

function dock() { return page.getByRole('region', { name: 'Pending confirmation' }) }
async function pane(name) { await page.getByRole('group', { name: 'Control panes' }).getByRole('button', { name, exact: true }).click() }
async function status() { return (await fetch(`${baseUrl}/demo/status`)).json() }
async function readEvents() {
  const response = await fetch(`${baseUrl}/session/${session}?after_sequence=${sequence}`, { headers: { Authorization: `Bearer ${token}` } })
  assert.ok(response.ok)
  const replay = await response.json()
  events.push(...replay.events.map(record => record.event))
  sequence = replay.last_sequence
  return events
}
async function outcome(name, id, after = 0) {
  await waitUntil(async () => {
    const all = await readEvents()
    id ??= all.slice(after).findLast(event => event.type === 'intent_record' && event.intent.name === name)?.intent.intent_id
    const terminal = all.find(event => event.intent_id === id && (event.type === 'refusal' ||
      event.source === 'autonomy' && ['completed', 'refused', 'failed', 'invalidated'].includes(event.status)))
    if (!terminal) return false
    assert.equal(terminal.status, 'completed', JSON.stringify(terminal))
    return true
  })
}
async function command(label, name, confirm = false) {
  const after = (await readEvents()).length
  await page.getByRole('button', { name: label, exact: typeof label === 'string' }).click()
  if (confirm) await dock().getByRole('button', { name: 'Confirm and send', exact: true }).click()
  await outcome(name, undefined, after)
}
async function waitUntil(predicate) {
  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error('Navigation browser condition timed out')
}

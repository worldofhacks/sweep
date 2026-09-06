/** Four-node production-wire acceptance. Scripted gestures are not recognition-accuracy evidence. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { chromium } from 'playwright'

const consoleRoot = resolve(import.meta.dirname, '..')
const repositoryRoot = resolve(consoleRoot, '..')
const liveLanguage = process.argv.includes('--live-language')
const language = liveLanguage || process.argv.includes('--language')
const providerEnvIndex = process.argv.indexOf('--provider-env')
const providerEnv = providerEnvIndex < 0 ? null : process.argv[providerEnvIndex + 1]
if (providerEnvIndex >= 0) assert.ok(liveLanguage && providerEnv, '--provider-env requires --live-language and a file path')
if (liveLanguage) assert.equal(process.platform, 'darwin', 'Live provider rehearsal currently uses macOS say/afconvert to generate speech audio')
const session = `fleet-browser-${Date.now()}`
const directory = resolve(repositoryRoot, 'output/playwright', session)
const logs = join(directory, 'audit')
await mkdir(logs, { recursive: true })
const evidence = { session, nodes: 4, aircraft: 'synthetic signed bridge nodes', gestures: 'scripted MediaPipe results', language: liveLanguage ? 'real Whisper and Anthropic providers with computer-generated speech; not a human microphone accuracy test' : language ? 'synthetic provider responses through real compiler and audio upload' : 'typed local fallback', checks: [], utterances: [] }
const child = spawn(join(repositoryRoot, '.venv/bin/python'), [
  '-m', language ? 'adapters.sim.language_demo' : 'adapters.sim.demo',
  '--count', '4', '--session', session, '--console-dist', join(consoleRoot, 'dist'), '--log-dir', logs,
  ...(language && !liveLanguage ? ['--synthetic-inputs'] : []),
  ...(providerEnv ? ['--provider-env', resolve(providerEnv)] : []),
], { cwd: repositoryRoot, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] })
let browser, context, page, baseUrl, token
let serviceLog = ''
child.stderr.on('data', (chunk) => { serviceLog += chunk.toString() })
const pageErrors = []

try {
  const ready = await readyMessage(child)
  baseUrl = ready.console_url
  assert.equal(ready.session, session)
  const bootstrap = await (await fetch(`${baseUrl}/relay-bootstrap.json`)).json()
  token = bootstrap.relay.token
  browser = await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] })
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, recordVideo: { dir: directory }, permissions: ['camera', 'microphone'] })
  page = await context.newPage()
  page.on('pageerror', (error) => pageErrors.push(error.message))
  if (liveLanguage) await page.addInitScript(() => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      if (!constraints.audio || !window.__fleetSpeechAudio) return original(constraints)
      await window.__fleetSpeechContext?.close()
      const audio = new AudioContext()
      window.__fleetSpeechContext = audio
      const data = Uint8Array.from(atob(window.__fleetSpeechAudio), (value) => value.charCodeAt(0))
      const buffer = await audio.decodeAudioData(data.buffer)
      window.__fleetSpeechDuration = buffer.duration * 1000 + 400
      const destination = audio.createMediaStreamDestination()
      const source = audio.createBufferSource()
      source.buffer = buffer
      source.connect(destination)
      await audio.resume()
      source.start(audio.currentTime + .2)
      return destination.stream
    }
  })
  // Only recognition is scripted. Real browser capture, dwell/confirmation policy,
  // WebSockets, relay, arbiter, signed commands, node ACKs and telemetry still run.
  await page.route(/\/assets\/vision_bundle[^/]*\.js(?:\?.*)?$/, (route) => route.fulfill({
    contentType: 'text/javascript',
    body: `export const FilesetResolver = { forVisionTasks: async () => ({}) };
      export const GestureRecognizer = { createFromOptions: async () => ({
        recognizeForVideo() {
          const f = window.__fleetGesture;
          if (!f || !f.category) return { landmarks: [], handedness: [], gestures: [] };
          return { landmarks: [Array.from({length: 21}, (_, i) => ({x: .3+i*.01, y: .4+(i%5)*.02, z: 0}))],
            handedness: [[{categoryName: 'Right', score: 1}]],
            gestures: [[{categoryName: f.category, score: f.score ?? .95}]] };
        }, close() {} }) };`,
  }))
  await page.goto(baseUrl)
  await page.getByRole('button', { name: 'Network stop', exact: true }).waitFor()
  await module('Control')
  await controlPane('Fleet')
  for (const id of [1, 2, 3, 4]) await registry(id).locator('.ct-registry-membership').filter({ hasText: /^ready$/ }).waitFor()
  evidence.checks.push('four ready aircraft visible in console')

  if (language) {
    for (const [text, name, targets] of [
      ['arm', 'arm', []], ['select all drones', 'select', [1, 2, 3, 4]],
      ['take off', 'takeoff', [1, 2, 3, 4]], ['move forward 0.5 meters', 'translate', [1, 2, 3, 4]],
    ]) await voiceCommand(text, name, targets)
  } else {
    await controlCommand('Arm', 'arm')
    await module('Gesture')
    await page.getByRole('button', { name: 'All ready', exact: true }).click()
    await confirmAndWait('select')
    await controlCommand(/^Takeoff/, 'takeoff', true)
    await controlCommand('Translate east', 'translate')
  }
  await waitUntil(async () => (await status()).drones.every((drone) => drone.telemetry.state === 'hovering'), 'four hovering nodes')

  await module('Gesture')
  for (const id of [2, 4]) {
    const before = (await events()).length
    await page.getByRole('group', { name: 'Target', exact: true }).getByRole('button', { name: `Deselect ${droneName(id)}`, exact: true }).click()
    await completed('select', before)
  }
  await page.getByRole('button', { name: 'Enable tracking', exact: true }).click()
  await page.getByLabel('Closed fist pair').getByText('Ready to draft hold for D-01, D-03.', { exact: true }).waitFor()
  await page.getByLabel('Open palm pair').getByText(/Select exactly one ready aircraft/).waitFor()
  evidence.checks.push('fleet HOLD ready while capture reports its one-aircraft requirement')
  await gesture('Closed_Fist', 900, .3)
  assert.equal(await dock().count(), 0)
  evidence.checks.push('low-confidence gesture emits no preview')
  await neutral()

  const holdBaseline = (await events()).length
  await gesture('Closed_Fist', 900)
  const cancelled = await pendingIntent()
  assert.equal(cancelled.name, 'hold')
  assert.deepEqual(cancelled.selection, [1, 3])
  assert.equal(cancelled.source, 'webcam')
  assert.equal(intentRecords(await events(), cancelled.intent_id).length, 0)
  await page.screenshot({ path: join(directory, 'fleet-gesture-preview.png'), fullPage: true })
  await neutral()
  await gesture('Thumb_Down', 700)
  await dock().waitFor({ state: 'hidden' })
  assert.equal(intentRecords(await events(), cancelled.intent_id).length, 0)
  evidence.checks.push('gesture cancellation sends nothing')

  await neutral()
  await gesture('Closed_Fist', 900)
  const held = await pendingIntent()
  assert.deepEqual(held.selection, [1, 3])
  await neutral()
  await gesture('Thumb_Up', 1200)
  const heldId = await completed('hold', holdBaseline, 'webcam')
  assert.equal(heldId, held.intent_id)
  await neutral()
  assert.equal(intentRecords(await events(), heldId).length, 1)
  await assertCommands(heldId, [1, 3], 'hover')
  evidence.checks.push('one confirmed gesture HOLD reaches exactly D-01 and D-03 with adapter completion')

  await gesture('Closed_Fist', 900)
  const invalidated = await pendingIntent()
  await api('/demo/nodes/3/disconnect', {})
  await dock().waitFor({ state: 'hidden' })
  await neutral()
  await gesture('Thumb_Up', 700)
  assert.equal(intentRecords(await events(), invalidated.intent_id).length, 0)
  await api('/demo/nodes/3/rejoin', {})
  assert.equal((await status()).drones.find((drone) => drone.drone_id === 3).connection_epoch, 2)
  const gestureDownload = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download session (JSONL)', exact: true }).click()
  await (await gestureDownload).saveAs(join(directory, 'gesture.jsonl'))
  evidence.checks.push('membership change invalidates preview; rejoin gets a new epoch')

  if (language) {
    for (const [text, name, targets] of [
      ['select drones 1 and 2', 'select', [1, 2]], ['hold', 'hold', [1, 2]],
      ['come home', 'come_home', [1, 2]], ['land all', 'land_all', []],
    ]) await voiceCommand(text, name, targets)
  } else {
    await typedCommand('select all ready aircraft', 'select')
    await typedCommand('hold', 'hold')
    await controlCommand(/^Land all/, 'land_all', true)
  }
  await waitUntil(async () => (await status()).drones.every((drone) => drone.telemetry.state === 'landed'), 'all nodes landed')
  await module('Control')
  await controlPane('Fleet')
  await page.screenshot({ path: join(directory, 'fleet-landed.png'), fullPage: true })
  evidence.checks.push('fleet landing reflected in authoritative telemetry and console')
  assert.deepEqual(pageErrors, [])
  const files = (await readdir(logs)).filter((file) => file.endsWith('.jsonl'))
  assert.equal(files.length, 1)
  const audit = await readFile(join(logs, files[0]), 'utf8')
  assert.ok(!audit.includes(token), 'audit must not contain the console credential')
  assert.ok(!audit.includes('"signature"'), 'audit must redact signatures')
  evidence.result = 'passed'
} catch (error) {
  evidence.result = 'failed'
  evidence.error = error instanceof Error ? error.message : String(error)
  await page?.screenshot({ path: join(directory, 'failure.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  evidence.pageErrors = pageErrors
  const cleanupErrors = []
  try { await context?.close() } catch (error) { cleanupErrors.push(String(error)) }
  try { await browser?.close() } catch (error) { cleanupErrors.push(String(error)) }
  try { await stop(child) } catch (error) { cleanupErrors.push(String(error)) }
  if (cleanupErrors.length) {
    evidence.result = 'failed'
    evidence.cleanupErrors = cleanupErrors
  }
  await writeFile(join(directory, 'evidence.json'), JSON.stringify(evidence, null, 2) + '\n')
  await writeFile(join(directory, 'service.log'), token ? serviceLog.replaceAll(token, '[redacted]') : serviceLog)
  console.log(`Fleet evidence: ${directory}`)
  if (cleanupErrors.length) process.exitCode = 1
}

function droneName(id) { return `D-${String(id).padStart(2, '0')}` }
function registry(id) { return page.getByRole('article', { name: `${droneName(id)} registry card` }) }
function dock() { return page.getByRole('region', { name: 'Pending confirmation', exact: true }) }
async function pendingIntent() { await dock().waitFor(); return JSON.parse(await dock().locator('pre').textContent()) }
async function module(name) { await page.getByRole('navigation', { name: 'Modules' }).getByRole('button', { name, exact: true }).click() }
async function controlPane(name) { await page.getByRole('group', { name: 'Control panes' }).getByRole('button', { name, exact: true }).click() }
async function status() { return (await fetch(`${baseUrl}/demo/status`)).json() }
async function events() {
  const result = await fetch(`${baseUrl}/session/${session}`, { headers: { Authorization: `Bearer ${token}` } })
  assert.ok(result.ok)
  return (await result.json()).events.map((record) => record.event)
}
async function api(path, body) {
  const response = await fetch(`${baseUrl}${path}`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  assert.ok(response.ok, `${path}: ${response.status}`)
  return response.json()
}
function intentRecords(all, id) { return all.filter((event) => event.type === 'intent_record' && event.intent.intent_id === id) }
async function completed(name, after, source) {
  let id
  await waitUntil(async () => {
    const all = await events()
    const intent = all.slice(after).find((event) => event.type === 'intent_record' && event.intent.name === name && (!source || event.intent.source === source))
    if (!intent) return false
    id = intent.intent.intent_id
    const terminal = all.find((event) => event.intent_id === id && (event.type === 'refusal' || (event.source === 'autonomy' && ['completed', 'refused', 'failed', 'invalidated'].includes(event.status))))
    if (!terminal) return false
    assert.equal(terminal.status, 'completed', JSON.stringify(terminal))
    return true
  }, `completed ${name}`)
  return id
}
async function confirmAndWait(name) {
  const before = (await events()).length
  await dock().getByRole('button', { name: 'Confirm and send', exact: true }).click()
  return completed(name, before)
}
async function controlCommand(label, name, confirm = false) {
  await module('Control'); await controlPane('Swarm')
  const before = (await events()).length
  await page.getByRole('button', { name: label, exact: typeof label === 'string' }).click()
  if (confirm) await dock().getByRole('button', { name: 'Confirm and send', exact: true }).click()
  const id = await completed(name, before)
  evidence.checks.push(`button ${name} completed`)
  return id
}
async function typedCommand(text, name) {
  await module('Speech')
  await page.getByRole('textbox', { name: /Utterance/ }).fill(text)
  await page.getByRole('button', { name: 'Compile to intents', exact: true }).click()
  await page.getByRole('button', { name: 'Draft for confirmation', exact: true }).click()
  await confirmAndWait(name)
  evidence.checks.push(`typed local ${name} completed`)
}
async function voiceCommand(text, name, targets) {
  await module('Speech')
  if (liveLanguage) {
    const number = evidence.utterances.length
    const aiff = join(directory, `speech-${number}.aiff`)
    const wav = join(directory, `speech-${number}.wav`)
    await command('/usr/bin/say', ['-r', '145', '-o', aiff, text])
    await command('/usr/bin/afconvert', ['-f', 'WAVE', '-d', 'LEI16@24000', '-c', '1', aiff, wav])
    await page.evaluate((audio) => { window.__fleetSpeechAudio = audio }, (await readFile(wav)).toString('base64'))
  } else await api('/demo/language/next', { text })
  const upload = page.waitForResponse((response) => response.url().endsWith(`/api/sessions/${session}/transcripts`) && response.request().method() === 'POST', { timeout: 60000 })
  const talk = page.locator('.sp-listen')
  await talk.dispatchEvent('pointerdown', { buttons: 1 })
  await page.getByRole('button', { name: /Listening — release to transcribe/ }).waitFor()
  await page.waitForTimeout(liveLanguage ? await page.evaluate(() => window.__fleetSpeechDuration) : 500)
  await talk.dispatchEvent('pointerup', { buttons: 0 })
  const outcome = await (await upload).json()
  evidence.utterances.push({ requested: text, transcript: outcome.transcript, kind: outcome.compilation?.kind, reason: outcome.compilation?.reason, source: outcome.compilation?.source })
  if (!liveLanguage) assert.equal(outcome.transcript, text)
  assert.equal(outcome.compilation?.kind, 'plan', JSON.stringify(outcome))
  await page.getByRole('button', { name: 'Stage step 1 of 1', exact: true }).click()
  const pending = await pendingIntent()
  assert.equal(pending.name, name)
  assert.deepEqual(pending.selection, targets)
  await confirmAndWait(name)
  evidence.checks.push(`audio upload → compiler → confirmed ${name} completed`)
}
async function command(executable, args) {
  const process = spawn(executable, args, { stdio: 'ignore' })
  await new Promise((done, reject) => {
    process.once('error', reject)
    process.once('exit', (code) => code === 0 ? done() : reject(new Error(`${executable} exited ${code}`)))
  })
}
async function gesture(category, duration, score = .95) {
  await page.evaluate((frame) => { window.__fleetGesture = frame }, { category, score })
  await page.waitForTimeout(duration)
}
async function neutral() { await gesture(null, 350) }
async function assertCommands(id, targets, operation) {
  const all = await events()
  const commands = all.filter((event) => event.type === 'command' && event.intent_id === id)
  assert.deepEqual(commands.map((event) => event.drone_id).sort(), targets)
  assert.ok(commands.every((event) => event.operation === operation))
  for (const droneId of targets) assert.ok(all.some((event) => event.type === 'acknowledgement' && event.source === 'adapter' && event.drone_id === droneId && event.intent_id === id && event.status === 'completed'))
}
async function waitUntil(predicate, description) {
  const until = Date.now() + 15000
  while (Date.now() < until) { if (await predicate()) return; await new Promise((done) => setTimeout(done, 100)) }
  throw new Error(`Timed out: ${description}`)
}
function readyMessage(process) {
  return new Promise((done, reject) => {
    let buffer = ''
    const timer = setTimeout(() => reject(new Error('Fleet demo startup timed out')), 20000)
    process.once('error', (error) => { clearTimeout(timer); reject(error) })
    process.once('exit', (code) => { clearTimeout(timer); reject(new Error(`Fleet demo exited during startup (${code}): ${serviceLog}`)) })
    process.stdout.on('data', (chunk) => {
      buffer += chunk.toString()
      const lines = buffer.split('\n'); buffer = lines.pop()
      for (const line of lines) {
        try { const message = JSON.parse(line); if (message.type === 'demo.ready') { clearTimeout(timer); done(message) } } catch { /* Other startup messages are not readiness. */ }
      }
    })
  })
}
async function stop(process) {
  if (process.exitCode !== null || process.signalCode !== null) return
  const exited = new Promise((done) => process.once('exit', done))
  process.kill('SIGTERM')
  await Promise.race([exited, new Promise((done) => setTimeout(done, 5000))])
  if (process.exitCode === null && process.signalCode === null) {
    process.kill('SIGKILL')
    await exited
  }
}

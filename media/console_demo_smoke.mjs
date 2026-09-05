import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createWriteStream } from 'node:fs'
import { appendFile, mkdir, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
const require = createRequire(new URL('../console/package.json', import.meta.url))
const { chromium } = require('playwright')
const artifacts = process.env.SWEEP_MEDIA_DEMO_ARTIFACTS
  ? resolve(process.env.SWEEP_MEDIA_DEMO_ARTIFACTS)
  : join(root, '.sweep/media-demo')
await mkdir(artifacts, { recursive: true })
await writeFile(join(artifacts, 'evidence.jsonl'), '')
const consoleUrl = 'http://127.0.0.1:14175'
const mediaUrl = 'http://127.0.0.1:18889'
const children = []
let browser
let source
let consolePage
let publisherLocation

try {
  start(process.env.MEDIAMTX_BINARY ?? 'mediamtx', ['media/demo.yml'], 'mediamtx.log', {
    ...Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('MTX_'))),
    MTX_WEBRTCADDRESS: '127.0.0.1:18889',
    MTX_WEBRTCLOCALUDPADDRESS: '127.0.0.1:18189',
    MTX_WEBRTCALLOWORIGINS: consoleUrl,
  })
  start(join(root, 'console/node_modules/.bin/vite'), [
    '--host', '127.0.0.1', '--port', '14175', '--strictPort',
  ], 'vite.log', { ...process.env, VITE_SWEEP_WHEP_BASE_URL: mediaUrl }, join(root, 'console'))
  await Promise.all([ready(consoleUrl), ready(`${mediaUrl}/drone1/publish`)])
  browser = await chromium.launch({ headless: true })
  source = await browser.newPage()
  await source.goto(`${consoleUrl}/?fixture=control`)
  const offer = await source.evaluate(createPublisher)
  const endpoint = `${mediaUrl}/drone1/whip`
  const response = await fetch(endpoint, {
    method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: offer,
    signal: AbortSignal.timeout(10_000), redirect: 'error',
  })
  assert.equal(response.status, 201, 'WHIP publisher offer rejected')
  const location = response.headers.get('location')
  assert(location, 'WHIP session omitted Location')
  publisherLocation = new URL(location, endpoint)
  assert.equal(publisherLocation.origin, mediaUrl)
  await source.evaluate(async (sdp) => {
    await window.publisher.pc.setRemoteDescription({ type: 'answer', sdp })
  }, await response.text())
  await source.waitForFunction(() => window.publisher.pc.connectionState === 'connected')
  await evidence('canvas_whip_connected', { codec: 'H264' })

  consolePage = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const whepResponses = []
  const deletedSessions = []
  consolePage.on('response', (response) => {
    if (response.request().method() === 'POST' && response.url() === `${mediaUrl}/drone1/whep`) {
      whepResponses.push(response.status())
    }
    if (response.request().method() === 'DELETE' && response.url().startsWith(mediaUrl)) {
      deletedSessions.push(response.status())
    }
  })
  await consolePage.goto(`${consoleUrl}/?fixture=control`)
  await consolePage.getByText(/Development fixture active/).waitFor()
  const navigation = consolePage.getByRole('navigation', { name: 'Modules' })
  await navigation.getByRole('button', { name: 'Live', exact: true }).click()
  await consolePage.getByRole('button', { name: 'Focus D-01', exact: true }).click()
  await assertLiveVideo()
  assert(whepResponses.includes(201), 'actual console did not negotiate WHEP')
  await consolePage.screenshot({ path: join(artifacts, 'live-console.png'), fullPage: true })

  const stop = consolePage.getByRole('button', { name: 'Network stop', exact: true })
  assert(await stop.isEnabled(), 'shell network stop disabled during video playback')
  await stop.click()
  await assertLiveVideo()
  await navigation.getByRole('button', { name: 'Control', exact: true }).click()
  await consolePage.getByRole('group', { name: 'Control panes' })
    .getByRole('button', { name: 'Requests', exact: true }).click()
  await consolePage.locator('.request-item').filter({ hasText: 'Estop' })
    .locator('.status-accepted').waitFor()
  assert(await stop.isEnabled(), 'shell network stop disabled after navigation')
  await poll(() => deletedSessions.some((status) => [200, 204, 404].includes(status)), 'WHEP teardown')
  await evidence('shell_controls_preserved', { fixtureStopRequestAccepted: true, whepSessionDeleted: true })

  await navigation.getByRole('button', { name: 'Live', exact: true }).click()
  await assertLiveVideo()
  await evidence('live_module_reopened', { successfulOffers: whepResponses.filter((s) => s === 201).length })
  await rm(join(artifacts, 'failure.png'), { force: true })
  await evidence('passed', { artifacts })
} catch (error) {
  await consolePage?.screenshot({ path: join(artifacts, 'failure.png'), fullPage: true }).catch(() => {})
  await evidence('failed', { message: error instanceof Error ? error.message : String(error), artifacts })
  process.exitCode = 1
} finally {
  await consolePage?.close().catch(() => {})
  if (publisherLocation) {
    await fetch(publisherLocation, { method: 'DELETE', signal: AbortSignal.timeout(3_000) }).catch(() => {})
  }
  await browser?.close().catch(() => {})
  for (const child of children.reverse()) {
    if (child.exitCode !== null || child.signalCode !== null) continue
    child.kill('SIGTERM')
    await Promise.race([
      new Promise((resolve) => child.once('exit', resolve)),
      delay(2_000),
    ])
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
  }
}

function start(command, args, name, env, cwd = root) {
  const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] })
  const log = createWriteStream(join(artifacts, name))
  child.stdout.pipe(log, { end: false })
  child.stderr.pipe(log, { end: false })
  child.on('error', (error) => log.write(`${error.message}\n`))
  child.once('close', () => log.end())
  children.push(child)
}

async function evidence(event, fields = {}) {
  const line = `${JSON.stringify({ event, t: Date.now(), ...fields })}\n`
  await appendFile(join(artifacts, 'evidence.jsonl'), line)
  process.stdout.write(line)
}

async function poll(predicate, label) {
  const deadline = Date.now() + 15_000
  while (!await predicate()) {
    if (Date.now() >= deadline) throw new Error(`timed out: ${label}`)
    await delay(100)
  }
}

async function ready(url) {
  await poll(async () => {
    try {
      return (await fetch(url, { signal: AbortSignal.timeout(1_000) })).ok
    } catch {
      return false
    }
  }, url)
}

async function assertLiveVideo() {
  const video = consolePage.getByLabel('Live camera drone1', { exact: true })
  await video.waitFor({ state: 'visible' })
  await consolePage.locator('.whep-player[data-playback-state="live"]').waitFor()
  const result = await video.evaluate(async (video) => {
    const initialTime = video.currentTime
    const initialFrames = video.getVideoPlaybackQuality().totalVideoFrames
    const deadline = performance.now() + 10_000
    while (performance.now() < deadline) {
      const frames = video.getVideoPlaybackQuality().totalVideoFrames
      if (video.currentTime > initialTime && frames >= initialFrames + 5 && video.videoWidth > 0) {
        return { currentTime: video.currentTime, frames, width: video.videoWidth, height: video.videoHeight }
      }
      await new Promise((resolve) => setTimeout(resolve, 50))
    }
    throw new Error('console video did not render advancing frames')
  })
  await evidence('console_video_advancing', result)
}

async function createPublisher() {
  const canvas = document.createElement('canvas')
  canvas.width = 640
  canvas.height = 360
  const context = canvas.getContext('2d')
  let frame = 0
  const draw = () => {
    context.fillStyle = `hsl(${frame % 360} 80% 40%)`
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.fillStyle = 'white'
    context.font = '32px monospace'
    context.fillText(`Sweep demo frame ${frame++}`, 20, 70)
    context.fillRect((frame * 5) % 600, 180, 40, 40)
  }
  draw()
  const timer = setInterval(draw, 1000 / 30)
  const stream = canvas.captureStream(30)
  const pc = new RTCPeerConnection({ iceServers: [] })
  window.publisher = { pc, timer, stream }
  const transceiver = pc.addTransceiver(stream.getVideoTracks()[0], {
    direction: 'sendonly', streams: [stream],
  })
  const codecs = RTCRtpSender.getCapabilities('video').codecs.filter(
    (codec) => codec.mimeType.toLowerCase() === 'video/h264',
  )
  if (!codecs.length) throw new Error('browser has no H264 encoder')
  transceiver.setCodecPreferences(codecs)
  await pc.setLocalDescription(await pc.createOffer())
  const deadline = performance.now() + 10_000
  while (pc.iceGatheringState !== 'complete') {
    if (performance.now() > deadline) throw new Error('publisher ICE gathering timed out')
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  return pc.localDescription.sdp
}

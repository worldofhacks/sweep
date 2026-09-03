import { chromium } from 'playwright'

const readerPassword = process.env.SWEEP_MEDIA_READ_PASSWORD
if (!readerPassword) throw new Error('SWEEP_MEDIA_READ_PASSWORD is required')

const browser = await chromium.launch({
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required'],
})

try {
  const page = await browser.newPage()
  const diagnostics = []
  const whepStatuses = []
  page.on('pageerror', (error) => diagnostics.push(`pageerror:${error.message}`))
  page.on('requestfailed', (request) => diagnostics.push(`requestfailed:${request.url()}:${request.failure()?.errorText}`))
  page.on('response', (response) => {
    if (response.url().includes('/whep')) {
      diagnostics.push(`whep:${response.status()}`)
      whepStatuses.push(response.status())
    }
  })
  await page.goto('http://localhost:5173/?fixture=control')
  await page.getByRole('button', { name: 'View feed' }).first().click()
  const media = page.locator('[data-playback-state="playing_whep"] video')
  try {
    await media.waitFor({ state: 'visible', timeout: 15_000 })
  } catch (error) {
    const states = await page.locator('[data-playback-state]').evaluateAll((elements) =>
      elements.map((element) => ({
        state: element.getAttribute('data-playback-state'),
        detail: element.getAttribute('data-playback-detail'),
      })),
    )
    throw new Error(
      `${error.message}; states=${JSON.stringify(states)}; diagnostics=${JSON.stringify(diagnostics)}`,
      { cause: error },
    )
  }
  await page.waitForFunction(() => {
    const video = document.querySelector('[data-playback-state="playing_whep"] video')
    return video instanceof HTMLVideoElement && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
  })

  const video = await media.evaluate((element) => ({
    ready_state: element.readyState,
    width: element.videoWidth,
    height: element.videoHeight,
    current_time: element.currentTime,
  }))
  const refusal = await page.evaluate(async () => {
    const request = async (authorization) => {
      const headers = { 'Content-Type': 'application/sdp' }
      if (authorization) headers.Authorization = authorization
      const response = await fetch('http://localhost:8889/drone1/whep', {
        method: 'POST',
        headers,
        body: 'v=0\r\n',
      })
      return response.status
    }
    return {
      anonymous_status: await request(null),
      wrong_reader_status: await request(`Basic ${btoa('sweep-reader:wrong-password')}`),
    }
  })
  if (video.width <= 0 || video.height <= 0 || video.current_time <= 0) {
    throw new Error(`WHEP video did not render a progressing frame: ${JSON.stringify(video)}`)
  }
  if (whepStatuses[0] !== 201) {
    throw new Error(`WHEP offer was not accepted: ${JSON.stringify(whepStatuses)}`)
  }
  if (refusal.anonymous_status !== 401 || refusal.wrong_reader_status !== 401) {
    throw new Error(`WHEP authentication did not fail closed: ${JSON.stringify(refusal)}`)
  }

  const fallbackPage = await browser.newPage()
  await fallbackPage.route('http://localhost:8889/**/whep', (route) =>
    route.fulfill({ status: 503, contentType: 'text/plain', body: 'forced smoke failure' }),
  )
  await fallbackPage.goto('http://localhost:5173/?fixture=control')
  await fallbackPage.getByRole('button', { name: 'View feed' }).first().click()
  const fallbackMedia = fallbackPage.locator('[data-playback-state="playing_hls"] video')
  await fallbackMedia.waitFor({ state: 'visible', timeout: 15_000 })
  await fallbackPage.waitForFunction(() => {
    const fallback = document.querySelector('[data-playback-state="playing_hls"] video')
    return fallback instanceof HTMLVideoElement
      && fallback.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      && fallback.currentTime > 0
  })
  const hlsVideo = await fallbackMedia.evaluate((element) => ({
    hls_ready_state: element.readyState,
    hls_width: element.videoWidth,
    hls_height: element.videoHeight,
    hls_current_time: element.currentTime,
  }))
  if (hlsVideo.hls_width <= 0 || hlsVideo.hls_height <= 0 || hlsVideo.hls_current_time <= 0) {
    throw new Error(`HLS fallback did not render a progressing frame: ${JSON.stringify(hlsVideo)}`)
  }
  console.log(JSON.stringify({
    event: 'browser_playback_rendered',
    whep_status: whepStatuses[0],
    ...video,
    ...refusal,
    forced_whep_status: 503,
    ...hlsVideo,
  }))
} finally {
  await browser.close()
}

export function readConsoleServerConfiguration(environment) {
  const bindHost = environment.SWEEP_CONSOLE_BIND_HOST ?? 'localhost'
  const bindPort = readPort(environment.SWEEP_CONSOLE_PORT ?? '5173')
  if (!environment.SWEEP_CONSOLE_ORIGIN && bindPort !== 5173) {
    throw new Error(
      'SWEEP_CONSOLE_ORIGIN is required when SWEEP_CONSOLE_PORT differs from 5173',
    )
  }
  const publicOrigin = environment.SWEEP_CONSOLE_ORIGIN
    ? canonicalPublicOrigin(environment.SWEEP_CONSOLE_ORIGIN)
    : 'http://localhost:5173'
  return {
    bindHost,
    bindPort,
    publicOrigin,
    media: readMediaConfiguration(environment),
  }
}

function canonicalPublicOrigin(value) {
  const origin = normalizeOrigin(value)
  if (value !== origin) {
    throw new Error(
      `SWEEP_CONSOLE_ORIGIN must be a canonical origin such as ${origin}`,
    )
  }
  return origin
}

function readMediaConfiguration(environment) {
  const values = [
    environment.SWEEP_MEDIA_WEBRTC_ORIGIN,
    environment.SWEEP_MEDIA_HLS_ORIGIN,
    environment.SWEEP_MEDIA_READ_USERNAME,
    environment.SWEEP_MEDIA_READ_PASSWORD,
  ]
  if (values.some((value) => typeof value !== 'string' || value.length === 0)) {
    return undefined
  }
  try {
    return {
      webrtcOrigin: normalizeOrigin(values[0]),
      hlsOrigin: normalizeOrigin(values[1]),
      readerUsername: values[2],
      readerPassword: values[3],
    }
  } catch {
    return undefined
  }
}

function normalizeOrigin(value) {
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('origin must use HTTP or HTTPS without credentials')
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new Error('origin must not contain a path, query, or fragment')
  }
  return url.origin
}

function readPort(value) {
  const port = Number(value)
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('SWEEP_CONSOLE_PORT must be an integer from 1 through 65535')
  }
  return port
}

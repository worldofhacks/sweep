import { createReadStream } from 'node:fs'
import { stat } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname, resolve, sep } from 'node:path'
import { hostHeaderIsAllowed } from './host-validation.mjs'
import { readConsoleServerConfiguration } from './server-config.mjs'

const configuration = readConsoleServerConfiguration(process.env)
const dist = resolve(import.meta.dirname, '..', 'dist')

const server = createServer(async (request, response) => {
  if (!hostHeaderIsAllowed(request.headers.host, configuration.publicOrigin)) {
    response.writeHead(421).end()
    return
  }
  const url = new URL(request.url ?? '/', configuration.publicOrigin)
  if (url.pathname === '/runtime-config.json') {
    const status = configuration.media ? 200 : 503
    response.writeHead(status, {
      'Cache-Control': 'no-store',
      'Content-Type': 'application/json',
    })
    response.end(JSON.stringify(configuration.media
      ? { media: configuration.media }
      : { error: 'media_not_configured' }))
    return
  }

  const relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1)
  const requested = resolve(dist, relative)
  if (!requested.startsWith(`${dist}${sep}`)) {
    response.writeHead(404).end()
    return
  }
  const file = await existingFile(requested) ?? resolve(dist, 'index.html')
  response.writeHead(200, { 'Content-Type': contentType(file) })
  createReadStream(file).pipe(response)
})

server.listen(configuration.bindPort, configuration.bindHost)

async function existingFile(path) {
  try {
    return (await stat(path)).isFile() ? path : null
  } catch {
    return null
  }
}

function contentType(path) {
  return {
    '.css': 'text/css',
    '.html': 'text/html',
    '.js': 'text/javascript',
    '.svg': 'image/svg+xml',
  }[extname(path)] ?? 'application/octet-stream'
}

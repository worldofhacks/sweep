import { useEffect, useRef, useState } from 'react'

export function WhepVideo({ droneId, baseUrl = import.meta.env.VITE_SWEEP_WHEP_BASE_URL }: {
  droneId: number
  baseUrl?: string
}) {
  const [attempt, setAttempt] = useState(0)
  let endpoint: string | null = null
  try {
    const base = new URL(baseUrl || '')
    if (['http:', 'https:'].includes(base.protocol) && !base.username && !base.password &&
      !base.search && !base.hash && Number.isInteger(droneId) && droneId >= 1 && droneId <= 6) {
      endpoint = `${base.href.replace(/\/$/, '')}/drone${droneId}/whep`
    }
  } catch { endpoint = null }
  if (!endpoint) return <p role="status">Camera playback is not configured.</p>
  return <WhepSession key={`${endpoint}:${attempt}`} endpoint={endpoint} droneId={droneId}
    retry={() => setAttempt((value) => value + 1)} />
}

function WhepSession({ endpoint, droneId, retry }: {
  endpoint: string
  droneId: number
  retry: () => void
}) {
  const video = useRef<HTMLVideoElement>(null)
  const playing = useRef<() => void>(() => undefined)
  const [status, setStatus] = useState<'connecting' | 'live' | 'error'>('connecting')
  useEffect(() => {
    const element = video.current!
    const abort = new AbortController()
    let peer: RTCPeerConnection | undefined
    let resource: string | undefined
    let disposed = false
    const timer = window.setTimeout(() => fail(), 15_000)
    function releaseResource() {
      if (resource) {
        const url = resource
        resource = undefined
        void fetch(url, { method: 'DELETE', credentials: 'omit', redirect: 'error' }).catch(() => undefined)
      }
    }
    function close() {
      if (disposed) return
      disposed = true
      window.clearTimeout(timer)
      abort.abort()
      if (peer) {
        peer.ontrack = null
        peer.onconnectionstatechange = null
        peer.getReceivers().forEach((receiver) => receiver.track?.stop())
        peer.close()
      }
      element.srcObject = null
      releaseResource()
    }
    function fail() {
      if (disposed) return
      close()
      setStatus('error')
    }
    playing.current = () => {
      if (!disposed) {
        window.clearTimeout(timer)
        setStatus('live')
      }
    }
    async function connect() {
      try {
        peer = new RTCPeerConnection()
        peer.addTransceiver('video', { direction: 'recvonly' })
        peer.ontrack = (event) => {
          if (!disposed) element.srcObject = event.streams[0] ?? new MediaStream([event.track])
        }
        peer.onconnectionstatechange = () => {
          if (peer?.connectionState === 'failed' || peer?.connectionState === 'disconnected') fail()
        }
        await peer.setLocalDescription(await peer.createOffer())
        if (disposed) return
        if (peer.iceGatheringState !== 'complete') {
          await new Promise<void>((resolve, reject) => {
            const cleanup = () => {
              peer?.removeEventListener('icegatheringstatechange', changed)
              abort.signal.removeEventListener('abort', cancelled)
            }
            const changed = () => {
              if (peer?.iceGatheringState === 'complete') { cleanup(); resolve() }
            }
            const cancelled = () => { cleanup(); reject(new Error('Cancelled')) }
            peer!.addEventListener('icegatheringstatechange', changed)
            abort.signal.addEventListener('abort', cancelled, { once: true })
            changed()
          })
        }
        if (disposed) return
        const response = await fetch(endpoint, {
          method: 'POST', headers: { 'Content-Type': 'application/sdp', Accept: 'application/sdp' },
          body: peer.localDescription?.sdp, signal: abort.signal, credentials: 'omit', redirect: 'error',
        })
        const location = response.headers.get('Location')
        if (location) {
          const url = new URL(location, endpoint)
          if (url.origin === new URL(endpoint).origin) resource = url.href
        }
        if (disposed) { releaseResource(); return }
        if (response.status !== 201 || !resource) throw new Error('Playback unavailable')
        await peer.setRemoteDescription({ type: 'answer', sdp: await response.text() })
      } catch { fail() }
    }
    void connect()
    return close
  }, [endpoint])
  return <div className="whep-player" data-playback-state={status}>
    <video ref={video} autoPlay muted playsInline aria-label={`Live camera drone${droneId}`}
      onPlaying={() => playing.current()} />
    <p role={status === 'error' ? 'alert' : 'status'}>
      {status === 'live' ? 'Live video' : status === 'connecting' ? 'Connecting to camera…' : 'Camera connection lost or unavailable.'}
    </p>
    {status === 'error' && <button type="button" className="text-button" onClick={retry}>Retry camera</button>}
  </div>
}

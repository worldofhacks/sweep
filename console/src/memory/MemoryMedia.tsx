import { useEffect, useRef, useState } from 'react'
import { AtlasClient } from '../atlas/client'
import { Icon } from '../atlas/Icon'
import type { Capture } from '../atlas/types'
import type { MemoryAsset } from './types'
import { explain } from './useMemory'

export function MemoryPreview({
  client,
  spaceId,
  capture,
  visible = true,
}: {
  client: AtlasClient
  spaceId: string
  capture: Capture
  visible?: boolean
}) {
  const video = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    if (!visible) video.current?.pause()
  }, [visible])
  const [requested, setRequested] = useState(
    capture.kind !== 'video' && capture.bytes <= 8 * 1024 * 1024,
  )
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    if (!requested) return
    const controller = new AbortController()
    let objectUrl = ''
    void client
      .media(spaceId, capture.id, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(explain(error))
          setRequested(false)
        }
      })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [client, spaceId, capture.id, requested])
  return (
    <div className="memory-preview">
      {url ? (
        capture.kind === 'video' ? (
          <video
            ref={video}
            controls
            playsInline
            preload="metadata"
            src={url}
            aria-label="Original capture"
          />
        ) : (
          <img src={url} alt={capture.note || `Original capture by ${capture.name}`} />
        )
      ) : (
        <div className="memory-preview-empty">
          <Icon name={capture.kind === 'video' ? 'video' : 'camera'} size={32} />
          {requested ? (
            <span role="status">Opening your capture…</span>
          ) : (
            <button
              onClick={() => {
                setError('')
                setRequested(true)
              }}
            >
              Load preview · {(capture.bytes / 1024 / 1024).toFixed(1)} MB
            </button>
          )}
          {error && <small role="status">Preview unavailable. Your original is saved.</small>}
        </div>
      )}
      <span className="memory-original">
        <Icon name="check" size={14} /> Original saved
      </span>
    </div>
  )
}

export function MemoryTrack({
  asset,
  client,
  spaceId,
  captureId,
}: {
  asset: MemoryAsset
  client: AtlasClient
  spaceId: string
  captureId: string
}) {
  const [requested, setRequested] = useState(false),
    [url, setUrl] = useState(''),
    [error, setError] = useState('')
  useEffect(() => {
    if (!requested) return
    const controller = new AbortController()
    let objectUrl = ''
    void client
      .memoryAssetMedia(spaceId, captureId, asset.id, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(explain(error))
          setRequested(false)
        }
      })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [asset.id, client, spaceId, captureId, requested])
  return (
    <article className="memory-track">
      <span className="atlas-eyebrow">
        {asset.role === 'soundtrack'
          ? 'SOUNDTRACK · CREATIVE ADDITION'
          : asset.role === 'narration'
            ? 'SPOKEN RECOLLECTION'
            : 'AMBIENT RECORDING'}
      </span>
      <strong>{asset.title}</strong>
      {!requested ? (
        <button
          className="atlas-text-button"
          onClick={() => {
            setError('')
            setRequested(true)
          }}
        >
          Load recording · {(asset.bytes / 1024 / 1024).toFixed(1)} MB
        </button>
      ) : url ? (
        asset.mime.startsWith('video/') ? (
          <video controls playsInline preload="metadata" src={url} />
        ) : (
          <audio controls preload="metadata" src={url} />
        )
      ) : (
        <p role="status">Loading recording…</p>
      )}
      {error && <p role="status">{error} You can retry.</p>}
    </article>
  )
}

const RECORDER_TYPES = [
  'audio/mp4',
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
]
/** One explicit, bounded voice note. Never connects to the fleet's speech controls. */
export function VoiceNote({
  disabled,
  onFile,
  onActive,
}: {
  disabled: boolean
  onFile: (file: File) => void
  onActive: (active: boolean) => void
}) {
  const [phase, setPhase] = useState<'idle' | 'asking' | 'recording'>('idle')
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState('')
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const mounted = useRef(false)
  const sequence = useRef(0)
  const stop = useRef(() => {})
  const supported = typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
  useEffect(() => {
    mounted.current = true
    const hidden = () => {
      if (document.visibilityState === 'hidden') stop.current()
    }
    document.addEventListener('visibilitychange', hidden)
    return () => {
      mounted.current = false
      // Invalidate the latest permission request, including one started after mount.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      sequence.current++
      document.removeEventListener('visibilitychange', hidden)
      stop.current()
      onActive(false)
    }
  }, [onActive])
  const start = async () => {
    if (phase !== 'idle') return
    const request = ++sequence.current
    setError('')
    setPhase('asking')
    onActive(true)
    try {
      const mimeType = RECORDER_TYPES.find((type) => MediaRecorder.isTypeSupported(type))
      if (!mimeType)
        throw new Error('Recording is not supported here. You can upload a voice note instead.')
      const source = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      if (
        !mounted.current ||
        request !== sequence.current ||
        document.visibilityState === 'hidden'
      ) {
        source.getTracks().forEach((track) => track.stop())
        if (mounted.current && request === sequence.current) {
          setPhase('idle')
          onActive(false)
        }
        return
      }
      stream.current = source
      const active = new MediaRecorder(source, { mimeType, audioBitsPerSecond: 128000 })
      recorder.current = active
      const chunks: Blob[] = []
      let bytes = 0
      let failed = false
      const began = Date.now()
      const timer = window.setInterval(() => {
        const elapsed = Math.floor((Date.now() - began) / 1000)
        if (mounted.current) setSeconds(Math.min(elapsed, 60))
        if (elapsed >= 60) stop.current()
      }, 250)
      stop.current = () => {
        window.clearInterval(timer)
        if (active.state !== 'inactive') active.stop()
        source.getTracks().forEach((track) => track.stop())
      }
      active.ondataavailable = (event) => {
        if (event.data.size) {
          chunks.push(event.data)
          bytes += event.data.size
        }
        if (bytes > 8 * 1024 * 1024) {
          failed = true
          stop.current()
        }
      }
      active.onerror = () => {
        failed = true
        stop.current()
      }
      active.onstop = () => {
        window.clearInterval(timer)
        source.getTracks().forEach((track) => track.stop())
        if (!mounted.current) return
        setPhase('idle')
        onActive(false)
        if (failed || !bytes) {
          setError('The recording could not be saved. Try again or choose a file.')
          return
        }
        const extension = mimeType.includes('mp4')
          ? 'm4a'
          : mimeType.includes('ogg')
            ? 'ogg'
            : 'webm'
        onFile(new File(chunks, `Voice memory.${extension}`, { type: active.mimeType || mimeType }))
      }
      active.start(1000)
      setSeconds(0)
      setPhase('recording')
    } catch (error) {
      if (request !== sequence.current || !mounted.current) return
      stop.current()
      stream.current?.getTracks().forEach((track) => track.stop())
      setError(`${explain(error)} You can still choose an audio file.`)
      setPhase('idle')
      onActive(false)
    }
  }
  return (
    <div className="memory-recorder">
      {supported ? (
        phase === 'recording' ? (
          <button className="atlas-secondary" onClick={() => stop.current()}>
            <Icon name="speech" size={18} />
            Stop recording · {seconds}s / 60s
          </button>
        ) : phase === 'asking' ? (
          <button
            className="atlas-secondary"
            onClick={() => {
              sequence.current++
              setPhase('idle')
              onActive(false)
            }}
          >
            Cancel microphone request
          </button>
        ) : (
          <button className="atlas-secondary" disabled={disabled} onClick={() => void start()}>
            <Icon name="speech" size={18} />
            Record a voice memory
          </button>
        )
      ) : (
        <p className="atlas-fine">
          Voice recording isn’t available in this browser. Choose an audio or video file instead.
        </p>
      )}
      {phase === 'recording' && (
        <span role="status">Microphone on. Only this voice note is recording.</span>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}

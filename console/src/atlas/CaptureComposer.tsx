import { useEffect, useRef, useState } from 'react'
import { AtlasClient, locate } from './client'
import { Icon } from './Icon'
import type { CaptureMetadata, GeoPosition } from './types'

interface Props {
  client: AtlasClient
  spaceId: string
  contributor: string
  name: string
  onSaved: () => void
}
export function CaptureComposer({ client, spaceId, contributor, name, onSaved }: Props) {
  const video = useRef<HTMLVideoElement>(null)
  const stream = useRef<MediaStream | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const recordTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const request = useRef<AbortController | null>(null)
  const heading = useRef<number | null>(null)
  const mounted = useRef(false)
  const pending = useRef<{ file: File; metadata: CaptureMetadata } | null>(null)
  const [needsRetry, setNeedsRetry] = useState(false)
  const [mode, setMode] = useState<'photo' | 'video' | 'panorama'>('photo')
  const [active, setActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [recording, setRecording] = useState(false)
  const [count, setCount] = useState(0)
  const [message, setMessage] = useState('')
  const [position, setPosition] = useState<GeoPosition | null>(null)

  useEffect(() => {
    mounted.current = true
    const orientation = (event: DeviceOrientationEvent) => {
      // Relative alpha is not a geographic heading. Only absolute orientation is recorded.
      if (event.absolute && event.alpha !== null) heading.current = (360 - event.alpha) % 360
    }
    window.addEventListener('deviceorientationabsolute', orientation)
    return () => {
      mounted.current = false
      window.removeEventListener('deviceorientationabsolute', orientation)
      clearTimeout(recordTimer.current)
      if (recorder.current?.state === 'recording') {
        recorder.current.onstop = null
        recorder.current.stop()
      }
      stream.current?.getTracks().forEach((track) => track.stop())
      request.current?.abort()
    }
  }, [])

  const uploadPending = async () => {
    const item = pending.current
    if (!item || !mounted.current) return
    setBusy(true)
    setNeedsRetry(false)
    setMessage('Saving your capture…')
    const controller = new AbortController()
    request.current = controller
    try {
      await client.upload(spaceId, item.file, item.metadata, controller.signal)
      if (!mounted.current) return
      pending.current = null
      setCount((n) => n + 1)
      setMessage('Capture saved to this space.')
      onSaved()
    } catch (error) {
      if (!mounted.current) return
      setNeedsRetry(true)
      setMessage(
        `${error instanceof Error ? error.message : 'Upload failed.'} Your capture is retained while this dialog is open. Retry or download it before leaving.`,
      )
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const save = async (
    file: File,
    source: 'camera' | 'import',
    capturedAt: number,
    fix: GeoPosition | null,
    kind = mode,
  ) => {
    if (!mounted.current) return
    pending.current = {
      file,
      metadata: {
        contributor_id: contributor,
        name,
        kind,
        source,
        captured_at: capturedAt,
        position: source === 'camera' ? fix : null,
        note: '',
      },
    }
    await uploadPending()
  }

  const startCamera = async () => {
    setBusy(true)
    setMessage('Opening your camera…')
    try {
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error(
          'Camera access needs HTTPS or localhost. You can still upload from your library.',
        )
      const opened = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
        audio: false,
      })
      if (!mounted.current) {
        opened.getTracks().forEach((track) => track.stop())
        return
      }
      stream.current?.getTracks().forEach((track) => track.stop())
      stream.current = opened
      if (video.current) {
        video.current.srcObject = opened
        await video.current.play()
      }
      setActive(true)
      try {
        setPosition(await locate())
        setMessage('Camera ready. Keep your subject in view.')
      } catch {
        setMessage(
          'Camera ready. Location is unavailable; the capture will be saved without map coverage.',
        )
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'The camera could not be opened.')
    } finally {
      setBusy(false)
    }
  }

  const shutter = async () => {
    if (!video.current || busy || needsRetry || recorder.current?.state === 'recording') return
    let fix = position
    if (!fix || Date.now() - fix.timestamp > 10_000) {
      setBusy(true)
      try {
        fix = await locate()
        setPosition(fix)
      } catch {
        fix = null
      }
      setBusy(false)
    }
    if (!mounted.current || !video.current) return
    const capturedAt = Date.now()
    if (fix) fix = { ...fix, heading: heading.current }
    if (mode === 'video') {
      if (!stream.current || typeof MediaRecorder === 'undefined') {
        setMessage('Video recording is unavailable here. Upload an existing video.')
        return
      }
      const mime = ['video/webm;codecs=vp8', 'video/mp4'].find((type) =>
        MediaRecorder.isTypeSupported(type),
      )
      if (!mime) {
        setMessage('This browser does not support a compatible recording format.')
        return
      }
      const capture = new MediaRecorder(stream.current, {
        mimeType: mime,
        videoBitsPerSecond: 3_000_000,
      })
      const parts: Blob[] = []
      let bytes = 0
      capture.ondataavailable = (event) => {
        parts.push(event.data)
        bytes += event.data.size
        if (bytes > 60 * 1024 * 1024 && capture.state === 'recording') capture.stop()
      }
      capture.onstop = () => {
        clearTimeout(recordTimer.current)
        setRecording(false)
        const type = mime.split(';')[0]
        void save(
          new File(parts, `capture-${capturedAt}.${type.endsWith('mp4') ? 'mp4' : 'webm'}`, {
            type,
          }),
          'camera',
          capturedAt,
          fix,
          'video',
        )
      }
      capture.onerror = () => {
        setMessage('Recording failed. Please try again.')
        setRecording(false)
      }
      recorder.current = capture
      capture.start(1000)
      setRecording(true)
      setMessage('Recording · stops automatically after 60 seconds.')
      recordTimer.current = setTimeout(() => {
        if (capture.state === 'recording') capture.stop()
      }, 60_000)
    } else {
      const canvas = document.createElement('canvas')
      canvas.width = video.current.videoWidth
      canvas.height = video.current.videoHeight
      if (!canvas.width || !canvas.height) {
        setMessage('Wait for the camera to finish opening.')
        return
      }
      canvas.getContext('2d')?.drawImage(video.current, 0, 0)
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', 0.92),
      )
      if (blob)
        await save(
          new File([blob], `capture-${capturedAt}.jpg`, { type: 'image/jpeg' }),
          'camera',
          capturedAt,
          fix,
        )
    }
  }

  return (
    <div className="atlas-composer">
      <div className="atlas-capture-modes" role="group" aria-label="Capture type">
        {(['photo', 'video', 'panorama'] as const).map((type) => (
          <button
            key={type}
            className={mode === type ? 'is-active' : ''}
            aria-pressed={mode === type}
            disabled={recording || busy || needsRetry}
            onClick={() => {
              setMode(type)
              setCount(0)
            }}
          >
            <Icon name={type === 'photo' ? 'camera' : type} />
            {type === 'panorama' ? '360 scan' : type === 'photo' ? 'Photo' : 'Video'}
          </button>
        ))}
      </div>
      <div className={`atlas-camera-preview ${active ? 'is-active' : ''}`}>
        <video ref={video} autoPlay playsInline muted aria-label="Phone camera preview" />
        {!active && (
          <div className="atlas-camera-prompt">
            <Icon name="camera" size={40} />
            <h3>Your perspective matters.</h3>
            <p>Capture a clear view of the space around you.</p>
            <button className="atlas-primary" disabled={busy} onClick={() => void startCamera()}>
              Enable camera
            </button>
          </div>
        )}
        {active && <div className="atlas-camera-reticle" aria-hidden="true" />}
        {active && (
          <span className="atlas-camera-badge">
            {position ? `GPS ±${Math.round(position.accuracy)} m` : 'No location fix'}
          </span>
        )}
      </div>
      {mode === 'panorama' && (
        <div className="atlas-scan-guide">
          <div className="atlas-scan-segments">
            {Array.from({ length: 8 }, (_, i) => (
              <span key={i} className={i < count ? 'is-filled' : ''} />
            ))}
          </div>
          <strong>{Math.min(count, 8)} of 8 viewpoints</strong>
          <p>
            Capture overlapping views all around the space. Then move a few steps and repeat:
            changing position, not only rotating, gives the 3D atlas depth.
          </p>
        </div>
      )}
      {active && (
        <button
          className={`atlas-primary atlas-full ${recording ? 'is-recording' : ''}`}
          disabled={busy || needsRetry}
          onClick={() => (recording ? recorder.current?.stop() : void shutter())}
        >
          <Icon name={recording ? 'check' : 'camera'} />
          {recording
            ? 'Finish video'
            : busy
              ? 'Saving…'
              : mode === 'video'
                ? 'Start video'
                : mode === 'panorama'
                  ? 'Capture next viewpoint'
                  : 'Take photo'}
        </button>
      )}
      <label className={`atlas-upload-label ${busy || recording ? 'is-disabled' : ''}`}>
        <Icon name="upload" />
        Or choose from your library
        <input
          type="file"
          accept="image/jpeg,image/png,image/webp,video/mp4,video/webm"
          disabled={busy || recording || needsRetry}
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file)
              void save(
                file,
                'import',
                Math.min(file.lastModified || Date.now(), Date.now()),
                null,
                file.type.startsWith('video/') ? 'video' : 'photo',
              )
            event.target.value = ''
          }}
        />
      </label>
      <p className="atlas-fine">
        Imported media keeps its source history. Your current location is never assigned to an older
        photo.
      </p>
      {message && (
        <p className="atlas-inline-notice" role="status">
          {message}
        </p>
      )}
      {needsRetry && (
        <div className="atlas-retry-actions">
          <button className="atlas-primary" onClick={() => void uploadPending()}>
            Retry upload
          </button>
          <button
            className="atlas-secondary"
            onClick={() => {
              const item = pending.current
              if (!item) return
              const url = URL.createObjectURL(item.file)
              const link = document.createElement('a')
              link.href = url
              link.download = item.file.name
              link.click()
              setTimeout(() => URL.revokeObjectURL(url), 1000)
            }}
          >
            Download capture
          </button>
        </div>
      )}
    </div>
  )
}

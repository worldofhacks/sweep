import { useEffect, useState } from 'react'
import type { DetectionFrame, DetectionSource, LiveDetectionClient } from '../../media/detections'

/** Image and rectangles are one analyzed frame; they never float over unrelated WHEP video. */
export function DetectionFeed({ client, source }: { client: LiveDetectionClient; source: DetectionSource }) {
  return <CameraDetectionFeed key={`${source.drone_id}:${source.connection_epoch}:${source.camera_id}:${source.stream}`} client={client} source={source} />
}

function CameraDetectionFeed({ client, source }: { client: LiveDetectionClient; source: DetectionSource }) {
  const [frame, setFrame] = useState<DetectionFrame | null>(null)
  const [status, setStatus] = useState('Starting object detection…')
  const { drone_id, connection_epoch, camera_id, stream } = source
  useEffect(() => {
    let active = true
    let poll: ReturnType<typeof setTimeout>
    let expiry: ReturnType<typeof setTimeout>
    const abort = new AbortController()
    const load = async () => {
      const started = Date.now()
      try {
        const result = await client.snapshot({ drone_id, connection_epoch, camera_id, stream }, abort.signal)
        if (!active) return
        clearTimeout(expiry)
        const lifetime = result.frame ? 1500 - result.frame.age_ms - (Date.now() - started) : 0
        if (result.frame && lifetime > 0) {
          setFrame(result.frame)
          setStatus(`Live detection · ${result.frame.detections.length} objects · ${result.frame.age_ms} ms since relay decode`)
          expiry = setTimeout(() => { setFrame(null); setStatus('Detection frame stale. Waiting for a new frame…') }, lifetime)
        } else {
          setFrame(null)
          setStatus(result.state === 'unconfigured' ? 'Object detection is not configured for this camera.' :
            result.state === 'failed' ? 'Object detection failed. Check the configured model and camera source.' :
            `Object detection: ${result.state.replaceAll('_', ' ')}.`)
        }
      } catch (reason) {
        if (active) { setFrame(null); setStatus(reason instanceof Error ? reason.message : 'Object detection unavailable.') }
      } finally {
        if (active) poll = setTimeout(() => { void load() }, 500)
      }
    }
    void load()
    return () => { active = false; abort.abort(); clearTimeout(poll); clearTimeout(expiry) }
  }, [client, drone_id, connection_epoch, camera_id, stream])
  return <div className="lv-detection-feed">
    {frame && <svg key={`${connection_epoch}:${camera_id}:${frame.sequence}`} viewBox={`0 0 ${frame.width} ${frame.height}`} role="img" aria-label="Live object detection overlay" preserveAspectRatio="xMidYMid meet">
      <image href={`data:image/jpeg;base64,${frame.jpeg_base64}`} width={frame.width} height={frame.height} />
      {frame.detections.map((box, index) => <g key={index}>
        <rect x={box.bbox_xyxy[0]} y={box.bbox_xyxy[1]} width={box.bbox_xyxy[2] - box.bbox_xyxy[0]} height={box.bbox_xyxy[3] - box.bbox_xyxy[1]} fill="none" stroke="#70f5b0" strokeWidth="2" />
        <text x={box.bbox_xyxy[0] + 4} y={Math.max(18, box.bbox_xyxy[1] + 18)} fill="#70f5b0" stroke="#111" strokeWidth="3" paintOrder="stroke" fontSize="18">{box.label} {Math.round(box.confidence * 100)}%</text>
      </g>)}
    </svg>}
    <p className="lv-detection-status" role="status">{status}</p>
  </div>
}

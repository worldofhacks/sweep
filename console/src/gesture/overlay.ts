import { HAND_CONNECTIONS, type GestureFrame } from './recognizer'

/** Draws the 21-point hand landmarks and their connections over the video frame. */
export function drawLandmarkOverlay(
  canvas: HTMLCanvasElement | null,
  video: HTMLVideoElement | null,
  frame: GestureFrame | null,
): void {
  if (!canvas) return
  // Nothing to clear until something has been drawn; avoids touching the 2D context needlessly.
  if (!frame && !canvas.dataset.overlayDrawn) return
  const width = video?.videoWidth || 640
  const height = video?.videoHeight || 480
  if (canvas.width !== width) canvas.width = width
  if (canvas.height !== height) canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) return
  context.clearRect(0, 0, width, height)
  if (!frame) return
  canvas.dataset.overlayDrawn = '1'
  frame.hands.forEach((hand) => {
    context.strokeStyle = hand.category ? '#63c69b' : '#aab5c0'
    context.lineWidth = 2
    HAND_CONNECTIONS.forEach(([start, end]) => {
      const from = hand.landmarks[start]
      const to = hand.landmarks[end]
      if (!from || !to) return
      context.beginPath()
      context.moveTo(from.x * width, from.y * height)
      context.lineTo(to.x * width, to.y * height)
      context.stroke()
    })
    context.fillStyle = '#e5eaf0'
    hand.landmarks.forEach((point) => {
      context.beginPath()
      context.arc(point.x * width, point.y * height, 3, 0, Math.PI * 2)
      context.fill()
    })
  })
}

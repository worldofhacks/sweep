/**
 * Sizes a canvas for the display and returns a context whose units are CSS
 * pixels, so every draw pass can work in the units the layout uses. Null when
 * the browser gives no 2D context; the caller then draws nothing.
 */
export function prepareCanvas(
  canvas: HTMLCanvasElement,
  width: number,
  height: number,
): CanvasRenderingContext2D | null {
  const ratio = typeof devicePixelRatio === 'number' && devicePixelRatio > 0 ? devicePixelRatio : 1
  const pixelWidth = Math.max(1, Math.round(width * ratio))
  const pixelHeight = Math.max(1, Math.round(height * ratio))
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight
  const context = canvas.getContext('2d')
  if (!context) return null
  context.setTransform(ratio, 0, 0, ratio, 0, 0)
  return context
}

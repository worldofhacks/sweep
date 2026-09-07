/**
 * jsdom has no 2D canvas, so tests get a context that records what was drawn.
 * Method calls and property assignments land in `calls` in order, which is
 * what a draw pass is: an ordered list of operations.
 */
export interface RecordedCall {
  name: string
  args: unknown[]
}

export interface RecordingContext extends CanvasRenderingContext2D {
  calls: RecordedCall[]
}

const contexts = new WeakMap<HTMLCanvasElement, RecordingContext>()

export function recordingContext(): RecordingContext {
  const calls: RecordedCall[] = []
  const values = new Map<string, unknown>()
  const methods = new Map<string, (...args: unknown[]) => unknown>()
  const target = {} as Record<string, unknown>
  return new Proxy(target, {
    get(_target, property) {
      const name = String(property)
      if (name === 'calls') return calls
      if (values.has(name)) return values.get(name)
      let method = methods.get(name)
      if (!method) {
        method = (...args: unknown[]) => {
          calls.push({ name, args })
          return name === 'measureText' ? { width: 0 } : undefined
        }
        methods.set(name, method)
      }
      return method
    },
    set(_target, property, value) {
      const name = String(property)
      values.set(name, value)
      calls.push({ name: `set ${name}`, args: [value] })
      return true
    },
  }) as unknown as RecordingContext
}

/**
 * Replaces `getContext` for the whole run, one stable context per canvas, so
 * a test can read back exactly what a component drew. Assigned rather than
 * spied so `restoreMocks` between tests leaves it in place.
 */
export function installRecordingCanvas(): void {
  HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, kind: string) {
    if (kind !== '2d') return null
    let context = contexts.get(this)
    if (!context) {
      context = recordingContext()
      contexts.set(this, context)
    }
    return context
  } as HTMLCanvasElement['getContext']
}

/** The recording context a rendered canvas drew into. */
export function canvasCalls(canvas: HTMLCanvasElement): RecordedCall[] {
  return (canvas.getContext('2d') as RecordingContext | null)?.calls ?? []
}

/** The arguments of every call with this name, in order. */
export function callsNamed(calls: readonly RecordedCall[], name: string): unknown[][] {
  return calls.filter((call) => call.name === name).map((call) => call.args)
}

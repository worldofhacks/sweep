import { useEffect, useReducer } from 'react'

const TICK_MS = 1_000

/** Re-renders once a second so frame ages keep counting between relay frames. */
export function useSecondTick(active: boolean): void {
  const [, tick] = useReducer((count: number) => count + 1, 0)
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => tick(), TICK_MS)
    return () => clearInterval(id)
  }, [active])
}

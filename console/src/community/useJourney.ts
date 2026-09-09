import { useCallback, useEffect, useMemo, useState } from 'react'
import { EMPTY_JOURNEY, readJourney, rememberContributions, type Journey } from './journey'
import type { Capture } from '../atlas/types'

export function useJourney(scope: string, contributor: string) {
  const key = `sweep.atlas.journey.v1:${encodeURIComponent(scope)}:${encodeURIComponent(contributor)}`
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState({ key, message: '' })
  const loaded = useMemo(() => {
    void revision
    try { return { journey: readJourney(key), error: '' } } catch { return { journey: EMPTY_JOURNEY, error: 'Saved spaces and progress could not be read on this device. Your uploaded originals are unaffected.' } }
  }, [key, revision])
  useEffect(() => {
    const update = (event: StorageEvent) => { if (event.key === key || event.key === null) setRevision(value => value + 1) }
    window.addEventListener('storage', update)
    return () => window.removeEventListener('storage', update)
  }, [key])
  const update = useCallback((change: (value: Journey) => Journey) => {
    try {
      const previous = readJourney(key)
      const next = change(previous)
      if (JSON.stringify(previous) !== JSON.stringify(next)) {
        localStorage.setItem(key, JSON.stringify(next))
        setRevision(value => value + 1)
      }
      setError({ key, message: '' })
    } catch { setError({ key, message: 'Your progress could not be saved on this device. Check browser storage; your uploaded originals are unaffected.' }) }
  }, [key])
  const remember = useCallback((captures: Capture[]) => update(previous => rememberContributions(previous, captures, contributor)), [contributor, update])
  const toggleSaved = (id: string) => update(previous => ({ ...previous,
    saved: previous.saved.includes(id) ? previous.saved.filter(value => value !== id) : [...previous.saved, id].slice(-500) }))
  return { journey: loaded.journey, error: (error.key === key ? error.message : '') || loaded.error, toggleSaved, remember }
}

import { useEffect, useRef, useState } from 'react'
import { AtlasClient } from '../atlas/client'
import type { Capture } from '../atlas/types'
import { analysisActive, EMPTY_NOTES, type MemoryContext, type MemoryNotes } from './types'

export const explain = (error: unknown) =>
  error instanceof Error ? error.message : 'This step could not finish. Your original is safe.'

/** Local inspection is automatic; external enrichment is always an explicit action. */
export function useMemory(client: AtlasClient, spaceId: string, capture: Capture) {
  const [data, setData] = useState<MemoryContext | null>(null)
  const [notes, setNotes] = useState<MemoryNotes>(EMPTY_NOTES)
  const [coordinates, setCoordinates] = useState(['', ''])
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [inspectionStatus, setInspectionStatus] = useState('')
  const [reload, setReload] = useState(0)
  const mounted = useRef(false)
  const generation = useRef(0)
  const locked = useRef(false)
  const upload = useRef<AbortController | null>(null)
  useEffect(() => {
    generation.current += 1
    mounted.current = true
    const controller = new AbortController()
    void client
      .memory(spaceId, capture.id, controller.signal)
      .then(async (value) => {
        if (controller.signal.aborted) return
        setData(value)
        setNotes(value.notes)
        setCoordinates(
          value.notes.location
            ? [String(value.notes.location.latitude), String(value.notes.location.longitude)]
            : ['', ''],
        )
        setDirty(false)
        setError('')
        setInspectionStatus('')
        if (
          !value.can_edit ||
          value.inspection ||
          analysisActive(value.analysis) ||
          !value.capabilities.metadata
        )
          return
        setInspectionStatus('Reading details from your original…')
        try {
          const inspected = await client.inspectMemory(spaceId, capture.id, controller.signal)
          if (controller.signal.aborted) return
          // Inspection may finish after a save. Never replace newer notes or revisions.
          setData((current) => current && { ...current, inspection: inspected.inspection })
          setInspectionStatus('')
        } catch (error) {
          if (!controller.signal.aborted)
            setInspectionStatus(`Metadata unavailable. You can keep going. ${explain(error)}`)
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(explain(error))
      })
    return () => {
      generation.current += 1
      mounted.current = false
      controller.abort()
      // Event handlers create uploads after mount; cancel the current one.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      upload.current?.abort()
    }
  }, [client, spaceId, capture.id, reload])
  const running = analysisActive(data?.analysis)
  useEffect(() => {
    if (!running) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const value = await client.memory(spaceId, capture.id, controller.signal)
        if (controller.signal.aborted) return
        setData(value)
        setNotes(value.notes)
        setCoordinates(
          value.notes.location
            ? [String(value.notes.location.latitude), String(value.notes.location.longitude)]
            : ['', ''],
        )
        setError('')
        if (!analysisActive(value.analysis)) return
      } catch (error) {
        if (!controller.signal.aborted) setError(explain(error))
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 2000)
    }
    timer = setTimeout(() => void poll(), 2000)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [client, spaceId, capture.id, running])

  const act = async (label: string, operation: () => Promise<void>) => {
    if (locked.current) return
    locked.current = true
    setBusy(label)
    setError('')
    try {
      await operation()
    } catch (error) {
      if (mounted.current) setError(explain(error))
    } finally {
      locked.current = false
      if (mounted.current) setBusy('')
    }
  }
  const edit = (change: Partial<MemoryNotes>) => {
    setNotes((previous) => ({ ...previous, ...change }))
    setDirty(true)
  }
  const coordinate = (values: string[]) => {
    setCoordinates(values)
    setDirty(true)
  }
  const save = async (): Promise<MemoryContext> => {
    if (!data) throw new Error('Your memory is still loading.')
    if (!dirty) return data
    let location = null
    if (coordinates.some((value) => value.trim())) {
      if (coordinates.some((value) => !value.trim() || !Number.isFinite(Number(value))))
        throw new Error('Enter both latitude and longitude, or leave both empty.')
      location = { latitude: Number(coordinates[0]), longitude: Number(coordinates[1]) }
      if (Math.abs(location.latitude) > 85 || Math.abs(location.longitude) > 180)
        throw new Error('Latitude must be between −85 and 85; longitude between −180 and 180.')
    }
    const occurred_at = notes.occurred_at?.trim() || null
    if (
      occurred_at &&
      (!/(Z|[+-]\d{2}:\d{2})$/i.test(occurred_at) ||
        !Number.isFinite(Date.parse(occurred_at)) ||
        Date.parse(occurred_at) > Date.now() ||
        Date.parse(occurred_at) < Date.UTC(1940, 0))
    )
      throw new Error(
        'Use a past date and time with its UTC offset, from 1940 onward, or leave it empty.',
      )
    const result = await client.saveMemory(spaceId, capture.id, data.revision, {
      ...notes,
      location,
      occurred_at,
    })
    if (mounted.current) {
      setData(result)
      setNotes(result.notes)
      setDirty(false)
    }
    return result
  }
  return {
    data,
    notes,
    coordinates,
    dirty,
    busy,
    error,
    running,
    inspectionStatus,
    setData,
    adopt: (value: MemoryContext) => {
      setData(value)
      setNotes(value.notes)
      setCoordinates(value.notes.location
        ? [String(value.notes.location.latitude), String(value.notes.location.longitude)]
        : ['', ''])
      setDirty(false)
    },
    setError,
    mounted,
    currentScope: () => {
      const started = generation.current
      return () => mounted.current && started === generation.current
    },
    upload,
    edit,
    coordinate,
    save,
    act,
    reload: () => {
      setData(null)
      setReload((value) => value + 1)
    },
  }
}

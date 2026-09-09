/** Display-only phrases consume no motion plan, even if a provider proposes one. */
export function voiceObservationView(transcript: string): 'camera' | 'detections' | 'lidar' | null {
  const text = transcript.trim().toLowerCase().replace(/[.!?]+$/, '')
  const match = /^(?:please )?(?:show(?: me)?|open|display)(?: the)? (?:live |raw )?(lidar(?: scan)?|object detections?|detections?|camera|video|live feed)(?: view| overlay)?(?: please)?$/.exec(text)
  if (!match) return null
  return match[1].startsWith('lidar') ? 'lidar' : match[1].includes('detection') ? 'detections' : 'camera'
}

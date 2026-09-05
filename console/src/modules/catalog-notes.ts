import type { Tone } from '../shell/derive'

export interface CatalogNoteState {
  text: string
  /** `muted` is the design's plain grey status box; other tones add the left accent. */
  tone: Tone
}

export function noteFromError(error: unknown): CatalogNoteState {
  return { text: error instanceof Error ? error.message : String(error), tone: 'danger' }
}

const COUNT_WORDS = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten']

/** "Four rooms", as the design writes small counts. */
export function countWord(count: number, noun: string): string {
  const word = COUNT_WORDS[count] ?? String(count)
  return `${word} ${count === 1 ? noun : `${noun}s`}`
}

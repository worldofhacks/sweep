import { useEffect } from 'react'

export interface PushToTalkKeyOptions {
  enabled: boolean
  start: () => void
  stop: () => void
  target?: Pick<Window, 'addEventListener' | 'removeEventListener'>
}

// Space is the push-to-talk key anywhere in the console, except while the operator is typing.
export function usePushToTalkKey({ enabled, start, stop, target }: PushToTalkKeyOptions) {
  useEffect(() => {
    const listenTo = target ?? window
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== ' ' || event.repeat || hasModifier(event) || event.isComposing) return
      if (isTypingTarget(event.target)) return
      event.preventDefault()
      if (enabled) start()
    }
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key !== ' ') return
      if (isTypingTarget(event.target)) return
      event.preventDefault()
      stop()
    }
    const onBlur = () => stop()
    listenTo.addEventListener('keydown', onKeyDown)
    listenTo.addEventListener('keyup', onKeyUp)
    listenTo.addEventListener('blur', onBlur)
    return () => {
      listenTo.removeEventListener('keydown', onKeyDown)
      listenTo.removeEventListener('keyup', onKeyUp)
      listenTo.removeEventListener('blur', onBlur)
    }
  }, [enabled, start, stop, target])
}

function hasModifier(event: KeyboardEvent): boolean {
  return event.ctrlKey || event.altKey || event.metaKey
}

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag === 'INPUT') {
    const type = (target as HTMLInputElement).type
    return !['button', 'submit', 'reset', 'checkbox', 'radio', 'range', 'file', 'color', 'image'].includes(type)
  }
  return false
}

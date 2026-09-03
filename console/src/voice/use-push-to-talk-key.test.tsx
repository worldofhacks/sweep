import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import { usePushToTalkKey } from './use-push-to-talk-key'

function Harness({ enabled, start, stop }: { enabled: boolean; start: () => void; stop: () => void }) {
  usePushToTalkKey({ enabled, start, stop })
  return (
    <div>
      <input aria-label="Room identifier" defaultValue="room-01" />
      <button type="button">View feed</button>
    </div>
  )
}

describe('global push-to-talk key', () => {
  test('Space starts on press and stops on release when nothing has focus', () => {
    const start = vi.fn()
    const stop = vi.fn()
    render(<Harness enabled start={start} stop={stop} />)

    fireEvent.keyDown(document.body, { key: ' ' })
    fireEvent.keyDown(document.body, { key: ' ', repeat: true })
    expect(start).toHaveBeenCalledTimes(1)
    fireEvent.keyUp(document.body, { key: ' ' })
    expect(stop).toHaveBeenCalledTimes(1)
  })

  test('Space still works when a non-text control such as a button has focus', () => {
    const start = vi.fn()
    const stop = vi.fn()
    render(<Harness enabled start={start} stop={stop} />)
    const button = screen.getByRole('button', { name: 'View feed' })
    button.focus()

    const pressed = fireEvent.keyDown(button, { key: ' ' })
    expect(pressed).toBe(false)
    expect(start).toHaveBeenCalledTimes(1)
  })

  test('Space types normally inside a text field and never records', () => {
    const start = vi.fn()
    const stop = vi.fn()
    render(<Harness enabled start={start} stop={stop} />)
    const field = screen.getByRole('textbox', { name: 'Room identifier' })
    field.focus()

    const notPrevented = fireEvent.keyDown(field, { key: ' ' })
    fireEvent.keyUp(field, { key: ' ' })
    expect(notPrevented).toBe(true)
    expect(start).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
  })

  test('does not start while voice is disabled but still releases and stops on blur', () => {
    const start = vi.fn()
    const stop = vi.fn()
    render(<Harness enabled={false} start={start} stop={stop} />)

    fireEvent.keyDown(document.body, { key: ' ' })
    expect(start).not.toHaveBeenCalled()
    fireEvent.blur(window)
    expect(stop).toHaveBeenCalledTimes(1)
  })

  test('ignores Space with a modifier held', () => {
    const start = vi.fn()
    render(<Harness enabled start={start} stop={vi.fn()} />)
    fireEvent.keyDown(document.body, { key: ' ', ctrlKey: true })
    expect(start).not.toHaveBeenCalled()
  })
})

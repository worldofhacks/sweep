import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Icon } from './Icon'

export function AtlasDialog({
  title,
  children,
  onClose,
  className = '',
}: {
  title: string
  children: ReactNode
  onClose: () => void
  className?: string
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const trigger = document.activeElement
    dialog.current?.showModal()
    return () => {
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true })
    }
  }, [])
  return (
    <dialog
      ref={dialog}
      className={`atlas-dialog ${className}`}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
    >
      <div className="atlas-dialog-header">
        <div>
          <span className="atlas-eyebrow">SWEEP ATLAS</span>
          <h2 id={titleId}>{title}</h2>
        </div>
        <button className="atlas-icon-button" aria-label="Close dialog" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      {children}
    </dialog>
  )
}

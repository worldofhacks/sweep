import { useState } from 'react'
import { AtlasDialog } from '../atlas/AtlasDialog'
import { Icon } from '../atlas/Icon'
import './community.css'

/** Shared setup state; the native bundle does not import the web authentication SDK. */
export function AccountSetup({ native = false }: { native?: boolean }) {
  const [open, setOpen] = useState(false)
  return <>
    <button className="community-account" onClick={() => setOpen(true)}><Icon name="people" size={16} /><span>Join in</span></button>
    {open && <AtlasDialog title="Good neighbors. More perspectives." onClose={() => setOpen(false)}>
      <div className="community-welcome-mark"><Icon name="people" size={32} /></div>
      <p className="community-lead">A familiar sign-in, a shared sense of place.</p>
      <div className="community-providers" aria-label="Planned sign-in providers"><span>Apple</span><span>Google</span><span>X / Twitter</span></div>
      <p>{native ? 'Social sign-in for the Android app still needs native browser setup. You can keep contributing with a workspace invitation.' : 'Social sign-in is not configured on this deployment yet. You can still explore examples and use a workspace invitation.'}</p>
      <p className="atlas-fine">Signing in will not give someone access to private spaces or fleet controls. Saved spaces and contribution points currently stay on this device.</p>
      <button className="atlas-primary atlas-full" onClick={() => setOpen(false)}>Keep exploring <Icon name="arrow" size={16} /></button>
    </AtlasDialog>}
  </>
}

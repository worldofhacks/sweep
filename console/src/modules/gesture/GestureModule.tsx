import { Pane } from '../../shell/Pane'
import { EmptyModule } from '../shared'

export function GestureModule() {
  return (
    <Pane
      title="Gesture recognition"
      note="Live camera in, canonical intents out. Tracking is off until you enable it."
    >
      <EmptyModule
        what="gesture input"
        detail="No camera pipeline is wired to this console yet. Nothing is tracked and nothing is emitted."
      />
    </Pane>
  )
}

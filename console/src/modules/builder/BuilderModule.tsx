import { Pane } from '../../shell/Pane'
import { EmptyModule } from '../shared'

export function BuilderModule() {
  return (
    <Pane
      title="World Builder"
      note="Rooms, bundles, and generation jobs. A generated world is never a safety record."
    >
      <EmptyModule what="rooms, bundles, or generation jobs" />
    </Pane>
  )
}

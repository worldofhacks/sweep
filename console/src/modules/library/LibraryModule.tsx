import { Pane } from '../../shell/Pane'
import { EmptyModule } from '../shared'

export function LibraryModule() {
  return (
    <Pane title="Capture library" note="Captured media by room, capture, aircraft and time.">
      <EmptyModule what="captured media sets" />
    </Pane>
  )
}

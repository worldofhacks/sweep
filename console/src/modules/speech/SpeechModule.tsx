import { Pane } from '../../shell/Pane'
import { EmptyModule } from '../shared'

export function SpeechModule() {
  return (
    <Pane
      title="Speech to intents"
      note="An utterance compiles to intents, the arbiter validates, you confirm. Never a command straight to an aircraft."
    >
      <EmptyModule
        what="speech input"
        detail="No utterance compiler is wired to this console yet. Nothing is captured and nothing is emitted."
      />
    </Pane>
  )
}

import './App.css'
import type { IntentFactoryDependencies } from './control/intent'
import type { ControlClients } from './control/use-control-console'
import { useControlConsole } from './control/use-control-console'
import type { MediaRuntime } from './media/runtime'
import type { ModuleId } from './modules/types'
import { Shell } from './shell/Shell'

interface AppProps {
  sessionId: string
  clients: ControlClients
  intentDependencies?: IntentFactoryDependencies
  initialModule?: ModuleId
  /** Playback runtime from the media bootstrap; absent means playback is not configured. */
  media?: MediaRuntime
}

/** Runtime clients in, the control hook, and the persistent shell around every module. */
export default function App({ sessionId, clients, intentDependencies, initialModule, media }: AppProps) {
  const controller = useControlConsole({ sessionId, clients, intentDependencies })
  return (
    <Shell
      controller={controller}
      now={intentDependencies?.now}
      initialModule={initialModule}
      media={media}
    />
  )
}

import './App.css'
import type { IntentFactoryDependencies } from './control/intent'
import type { ControlClients } from './control/use-control-console'
import { useControlConsole } from './control/use-control-console'
import type { ModuleId, ModuleServices } from './modules/types'
import { Shell } from './shell/Shell'

interface AppProps {
  sessionId: string
  clients: ControlClients
  intentDependencies?: IntentFactoryDependencies
  initialModule?: ModuleId
  /** Input services for the Gesture and Speech modules; absent members render as unavailable. */
  services?: ModuleServices
}

/** Runtime clients in, the control hook, and the persistent shell around every module. */
export default function App({ sessionId, clients, intentDependencies, initialModule, services }: AppProps) {
  const controller = useControlConsole({ sessionId, clients, intentDependencies })
  return (
    <Shell
      controller={controller}
      now={intentDependencies?.now}
      initialModule={initialModule}
      services={services}
    />
  )
}

import { useState } from 'react'
import './App.css'
import { UnreportedCatalogClient, type CatalogClient } from './catalog/client'
import { useCatalog } from './catalog/use-catalog'
import type { IntentFactoryDependencies } from './control/intent'
import type { ControlClients } from './control/use-control-console'
import { useControlConsole } from './control/use-control-console'
import type { ModuleId } from './modules/types'
import { Shell } from './shell/Shell'

interface AppProps {
  sessionId: string
  clients: ControlClients
  /** Absent in production: the relay exposes no catalog endpoint yet. */
  catalog?: CatalogClient
  intentDependencies?: IntentFactoryDependencies
  initialModule?: ModuleId
}

/** Runtime clients in, the control hook, and the persistent shell around every module. */
export default function App({ sessionId, clients, catalog, intentDependencies, initialModule }: AppProps) {
  const controller = useControlConsole({ sessionId, clients, intentDependencies })
  const [fallbackCatalog] = useState(() => new UnreportedCatalogClient())
  const catalogController = useCatalog(catalog ?? fallbackCatalog)
  return (
    <Shell
      controller={controller}
      catalog={catalogController}
      now={intentDependencies?.now}
      initialModule={initialModule}
    />
  )
}

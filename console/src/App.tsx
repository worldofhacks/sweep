import { useState } from 'react'
import './App.css'
import { UnreportedCatalogClient, type CatalogClient } from './catalog/client'
import { useCatalog } from './catalog/use-catalog'
import type { IntentFactoryDependencies } from './control/intent'
import type { ControlClients } from './control/use-control-console'
import { useControlConsole } from './control/use-control-console'
import type { MediaRuntime } from './media/runtime'
import type { ModuleId, ModuleServices } from './modules/types'
import { Shell } from './shell/Shell'

interface AppProps {
  sessionId: string
  clients: ControlClients
  /** Absent in production: the relay exposes no catalog endpoint yet. */
  catalog?: CatalogClient
  intentDependencies?: IntentFactoryDependencies
  initialModule?: ModuleId
  /** Input services for the Gesture and Speech modules; absent members render as unavailable. */
  services?: ModuleServices
  /** Playback runtime from the media bootstrap; absent means playback is not configured. */
  media?: MediaRuntime
}

/** Runtime clients in, the control hook, and the persistent shell around every module. */
export default function App({
  sessionId,
  clients,
  catalog,
  intentDependencies,
  initialModule,
  services,
  media,
}: AppProps) {
  const controller = useControlConsole({ sessionId, clients, intentDependencies, navigation: services?.navigation })
  const [fallbackCatalog] = useState(() => new UnreportedCatalogClient())
  const catalogController = useCatalog(catalog ?? fallbackCatalog)
  return (
    <Shell
      controller={controller}
      catalog={catalogController}
      now={intentDependencies?.now}
      initialModule={initialModule}
      services={services}
      media={media}
    />
  )
}

import { useEffect, useMemo, useState } from 'react'
import { UNREPORTED_CATALOG, type CatalogClient } from './client'
import type { CatalogSnapshot } from './types'

export interface CatalogController {
  snapshot: CatalogSnapshot
  client: CatalogClient
}

/** Subscribes to the catalog client for the life of the console. */
export function useCatalog(client: CatalogClient): CatalogController {
  const [snapshot, setSnapshot] = useState<CatalogSnapshot>(UNREPORTED_CATALOG)

  useEffect(() => {
    const unsubscribe = client.subscribe(setSnapshot)
    client.start()
    return () => {
      unsubscribe()
      client.stop()
    }
  }, [client])

  return useMemo(() => ({ snapshot, client }), [snapshot, client])
}

import { useEffect, useReducer, useState } from 'react'
import './shell.css'
import type { CatalogController } from '../catalog/use-catalog'
import type { ConnectionStatus } from '../control/state'
import { MODULES, getModule } from '../modules/registry'
import type { ConsoleController, ModuleId } from '../modules/types'
import { ContextColumn } from './ContextColumn'
import { DangerBanner } from './DangerBanner'
import { Dock } from './Dock'
import { Frame } from './Frame'
import { Header } from './Header'
import { NoticeLine } from './NoticeLine'
import { Rail } from './Rail'
import { TabBar } from './TabBar'
import {
  STOP_CLEARED_NOTICE_MS,
  deriveInvalidation,
  newestAdvisory,
  newestDanger,
  type StopTimes,
} from './derive'

export interface ShellProps {
  controller: ConsoleController
  catalog: CatalogController
  now?: () => number
  initialModule?: ModuleId
  /** Present only when a webcam pipeline reports a status; absent today. */
  webcam?: ConnectionStatus
}

const TICK_MS = 1_000

export function Shell({
  controller,
  catalog,
  now = Date.now,
  initialModule = 'control',
  webcam,
}: ShellProps) {
  const { state, pendingRequest, confirmRequest, cancelRequest, issueNetworkStop } = controller
  const [activeId, setActiveId] = useState<ModuleId>(initialModule)
  const [detailOpen, setDetailOpen] = useState(false)
  const stopTimes = useStopTimes(state.estop, now)
  const currentNow = now()
  const clearedRecently =
    stopTimes.seenClearedAt !== null && currentNow - stopTimes.seenClearedAt < STOP_CLEARED_NOTICE_MS
  const countdownActive = pendingRequest?.plan?.expiresAt !== undefined
  useTicker(clearedRecently || countdownActive)

  const module = getModule(activeId)
  const ModuleComponent = module.component
  const ModuleContext = module.context
  const isFixture =
    state.connection.transport === 'fixture' || state.keyboardConnection.transport === 'fixture'
  const invalidation = deriveInvalidation(state.requests, pendingRequest)

  return (
    <Frame
      header={
        <Header
          state={state}
          stopTimes={stopTimes}
          now={currentNow}
          onStop={() => issueNetworkStop('console')}
          detailOpen={detailOpen}
          onToggleDetail={() => setDetailOpen((open) => !open)}
          isFixture={isFixture}
          webcam={webcam}
        >
          {isFixture && (
            <p className="sh-fixture-line" role="status">
              Development fixture active — no aircraft commands leave this browser.
            </p>
          )}
          <DangerBanner notice={newestDanger(state.notices)} />
          <NoticeLine notice={newestAdvisory(state.notices)} />
        </Header>
      }
      rail={<Rail modules={MODULES} active={activeId} onSelect={setActiveId} />}
      pane={<ModuleComponent controller={controller} catalog={catalog} now={now} />}
      context={
        <ContextColumn rosterVersion={state.rosterVersion}>
          <ModuleContext controller={controller} catalog={catalog} now={now} />
        </ContextColumn>
      }
      dock={
        <Dock
          pending={pendingRequest}
          invalidation={invalidation}
          now={currentNow}
          onConfirm={confirmRequest}
          onCancel={cancelRequest}
        />
      }
      tabBar={<TabBar modules={MODULES} active={activeId} onSelect={setActiveId} />}
    />
  )
}

/**
 * The relay reports estop as a boolean only. Record when this console saw it
 * change so the stop button and the session sheet can say so honestly.
 */
function useStopTimes(estop: boolean, now: () => number): StopTimes {
  const [tracked, setTracked] = useState<StopTimes & { estop: boolean }>({
    estop,
    seenActiveAt: null,
    seenClearedAt: null,
  })
  if (tracked.estop !== estop) {
    const t = now()
    setTracked(
      estop
        ? { estop, seenActiveAt: t, seenClearedAt: null }
        : { estop, seenActiveAt: null, seenClearedAt: t },
    )
  }
  return tracked
}

/** Re-renders once a second only while something on screen counts down. */
function useTicker(active: boolean): void {
  const [, tick] = useReducer((count: number) => count + 1, 0)
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => tick(), TICK_MS)
    return () => clearInterval(id)
  }, [active])
}

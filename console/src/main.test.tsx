import type { ReactElement } from 'react'
import { expect, test, vi } from 'vitest'
import type { ModuleServices } from './modules/types'

const harness = vi.hoisted(() => ({
  render: vi.fn(),
  subscribe: vi.fn(),
  initial: { navigation: { label: 'initial platform provider' } },
  atlas: { label: 'Atlas client' },
  transcript: { label: 'Semantic speech client' },
  search: { label: 'Search client' },
  multiview: { label: 'Ordered photo client' },
}))

vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: harness.render }) }))
vi.mock('./App.tsx', () => ({ default: () => null }))
vi.mock('./relay/bootstrap.ts', () => ({
  bootstrapConsoleRuntime: async () => ({
    sessionId: 'integration-test',
    atlas: harness.atlas,
    transcriptClient: harness.transcript,
    searchClient: harness.search,
    multiviewClient: harness.multiview,
    platform: { getSnapshot: () => harness.initial, subscribe: harness.subscribe },
  }),
}))
vi.mock('./media/runtime-config.ts', () => ({
  SAME_ORIGIN_MEDIA_SOURCE: {},
  bootstrapMediaConfiguration: (render: (configuration: null) => void) => render(null),
  loadMediaRuntimeConfiguration: vi.fn(),
}))

test('platform refreshes retain Atlas and all hardware workflow services in the same Spaces shell', async () => {
  await import('./main')
  await vi.waitFor(() => expect(harness.render).toHaveBeenCalledOnce())
  const app = () => (harness.render.mock.lastCall![0] as ReactElement<{
    children: ReactElement<{ services: ModuleServices; initialModule: string }>
  }>).props.children.props
  const expected = { atlas: harness.atlas, transcript: harness.transcript, search: harness.search, multiview: harness.multiview }
  expect(app().initialModule).toBe('spaces')
  expect(app().services).toEqual({ ...harness.initial, ...expected })
  const replacement = { navigation: { label: 'refreshed platform provider' } }
  harness.subscribe.mock.calls[0][0](replacement)
  expect(app().services).toEqual({ ...replacement, ...expected })
  for (const key of ['atlas', 'transcript', 'search', 'multiview'] as const) {
    expect(app().services[key]).toBe(expected[key])
  }
})

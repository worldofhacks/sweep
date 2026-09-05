import { act, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import { FIXTURE_JOB_CHAIN } from '../../testing/fixture-relay-client'
import { openModule, openPaneTab, renderCatalogConsole } from '../../testing/catalog-console'

type User = ReturnType<typeof userEvent.setup>

async function openWorlds(user: User) {
  await openModule(user, 'Worlds')
  return screen.getByRole('heading', { level: 1, name: 'World Builder' })
}

async function openJobs(user: User) {
  await openPaneTab(user, 'World Builder panes', 'Jobs')
  return within(screen.getByRole('region', { name: 'Generation jobs' }))
}

describe('Worlds module', () => {
  test('populated rooms: building, doorway adjacency, floor-plan reference, bundles and the public false badge', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openWorlds(user)

    const rooms = within(screen.getByRole('region', { name: 'Rooms' }))
    expect(rooms.getByText('Building — ground floor')).toBeInTheDocument()
    expect(rooms.getByText(/Four rooms with explicit doorway adjacency/)).toHaveTextContent(
      'Floor-plan reference floorplan-gf.svg is a reference only, never a generation input.',
    )
    expect(rooms.getAllByRole('article').map((room) => room.getAttribute('aria-label'))).toEqual([
      'Room kitchen-01',
      'Room hall-02',
      'Room studio-03',
      'Room stair-04',
    ])

    const hall = within(rooms.getByRole('article', { name: 'Room hall-02' }))
    expect(hall.getByText('capturing')).toBeInTheDocument()
    expect(hall.getByText(/Doorways to/)).toHaveTextContent(
      'Doorways to kitchen-01, studio-03, stair-04. Both sides of each doorway are kept as composition references, separate from generation inputs.',
    )
    expect(hall.getByText(/Accepted bundle/)).toHaveTextContent('Accepted bundle cap-0142 · pano_360')
    const hallBundles = within(hall.getByRole('radiogroup', { name: 'Bundle for hall-02' }))
    expect(hallBundles.getAllByRole('radio').map((radio) => radio.textContent)).toEqual([
      'cap-0142 · pano_360',
      'manual phone fallback · 0 photos',
    ])
    expect(hallBundles.getByRole('radio', { name: 'cap-0142 · pano_360' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(hall.getByText('Upload set 1 image, model world-gen-1')).toBeInTheDocument()
    expect(hall.getByText('public: false')).toBeInTheDocument()
    expect(hall.getByRole('button', { name: 'Submit for generation' })).toBeDisabled()
    expect(hall.getByText('A job for this room is already running. Wait for it to finish or fail.')).toBeInTheDocument()

    const studio = within(rooms.getByRole('article', { name: 'Room studio-03' }))
    expect(studio.getByText('needs retake')).toHaveClass('tone-warn')
    expect(studio.getByText('Upload set 8 images, model world-gen-1')).toBeInTheDocument()
    expect(studio.getByRole('button', { name: 'Submit for generation' })).toBeEnabled()

    const stair = within(rooms.getByRole('article', { name: 'Room stair-04' }))
    expect(stair.getByText('not captured')).toBeInTheDocument()
    expect(stair.getByRole('radio', { name: 'manual phone fallback · 3 photos' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(
      stair.getByText(
        'Manual fallback — exactly three overlapping phone photos, added on this page, used when the drone path is unavailable.',
      ),
    ).toBeInTheDocument()
    expect(stair.getByText('Upload set 3 images, model world-gen-1')).toBeInTheDocument()
  })

  test('manual fallback: exactly three phone photos added on this page unblock the submit', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openWorlds(user)

    const kitchen = within(screen.getByRole('article', { name: 'Room kitchen-01' }))
    await user.click(kitchen.getByRole('radio', { name: 'manual phone fallback · 0 photos' }))
    expect(kitchen.getByText('Upload set 0 images, model world-gen-1')).toBeInTheDocument()
    expect(kitchen.getByRole('button', { name: 'Submit for generation' })).toBeDisabled()
    expect(
      kitchen.getByText('Manual fallback needs exactly 3 overlapping phone photos; 0 added.'),
    ).toBeInTheDocument()

    const photos = [1, 2, 3].map((n) => new File(['photo'], `phone-${n}.jpg`, { type: 'image/jpeg' }))
    await user.upload(kitchen.getByLabelText('Add phone photos'), photos)
    expect(screen.getByRole('status', { name: 'World Builder notice' })).toHaveTextContent(
      'Added 3 phone photos to kitchen-01. They are used only when the drone path is unavailable.',
    )
    expect(kitchen.getByRole('radio', { name: 'manual phone fallback · 3 photos' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(kitchen.getByText('Upload set 3 images, model world-gen-1')).toBeInTheDocument()
    expect(kitchen.getByRole('button', { name: 'Submit for generation' })).toBeEnabled()
  })

  test('submit runs the job chain uploading → queued → running → succeeded, then the world opens labelled generated with its sources', async () => {
    const user = userEvent.setup()
    const { scheduler } = renderCatalogConsole({ scenario: 'pending4' })
    await openWorlds(user)

    const studio = within(screen.getByRole('article', { name: 'Room studio-03' }))
    await user.click(studio.getByRole('button', { name: 'Submit for generation' }))
    // The pane strip remounts its children on a tab switch, so query the note each time.
    const note = () => screen.getByRole('status', { name: 'World Builder notice' })
    expect(note()).toHaveTextContent(
      'Submitted studio-03 with cap-0139 · reconstruct_8 and public false. Uploading now; you can keep working on the next room.',
    )
    expect(studio.getByRole('button', { name: 'Submit for generation' })).toBeDisabled()

    const jobs = await openJobs(user)
    const job = () => within(jobs.getByRole('article', { name: 'Job studio-03' }))
    expect(job().getByText('uploading')).toHaveClass('tone-warn')
    expect(job().getByText('Sending the accepted bundle to the World API.')).toBeInTheDocument()
    expect(job().getByText(/^op /)).toHaveTextContent(
      'op op_8f3395 · world — · model world-gen-1 · 8 frames, 0 of 8 sent · source cap-0139 · reconstruct_8',
    )
    expect(job().getByText('public: false')).toBeInTheDocument()
    expect(job().queryByRole('button', { name: 'Retry with the same capture' })).not.toBeInTheDocument()

    for (const step of FIXTURE_JOB_CHAIN) {
      const previous = FIXTURE_JOB_CHAIN[FIXTURE_JOB_CHAIN.indexOf(step) - 1]?.afterMs ?? 0
      act(() => scheduler.advance(step.afterMs - previous))
      expect(job().getByText(step.state)).toBeInTheDocument()
    }
    expect(job().getByText(/^op /)).toHaveTextContent('world wld_7803')
    expect(
      job().getByText('The room world is generated and can be opened. Its source photos stay beside it.'),
    ).toBeInTheDocument()

    await user.click(job().getByRole('button', { name: 'Open room world' }))
    expect(note()).toHaveTextContent(
      'Opened wld_7803 for studio-03, generated by world-gen-1. Labelled generated; its source photos stay beside it. It is not a factual or safety record.',
    )
    const world = within(job().getByRole('region', { name: 'Open world wld_7803' }))
    expect(world.getByText('generated')).toBeInTheDocument()
    expect(world.getByText('by world-gen-1 for studio-03')).toBeInTheDocument()
    expect(world.getByRole('listitem')).toHaveTextContent(
      'cap-0139 · reconstruct_8 · 8 files · sha256:70d1e5f8…2c31',
    )
  })

  test('populated jobs: all seven states with retry on failed and timed out, provenance on every card', async () => {
    const user = userEvent.setup()
    const { scheduler } = renderCatalogConsole({ scenario: 'pending4' })
    await openWorlds(user)
    const jobs = await openJobs(user)

    const cards = jobs.getAllByRole('article')
    expect(cards.map((card) => card.getAttribute('aria-label'))).toEqual([
      'Job kitchen-01',
      'Job hall-02',
      'Job studio-03',
      'Job stair-04',
      'Job store-05',
      'Job lobby-06',
      'Job corridor-07',
    ])
    const stateOf = (room: string) =>
      within(jobs.getByRole('article', { name: `Job ${room}` })).getByText(
        /^(draft|uploading|queued|running|succeeded|failed|timed_out)$/,
      )
    expect(stateOf('kitchen-01')).toHaveTextContent('succeeded')
    expect(stateOf('kitchen-01')).toHaveClass('tone-ok')
    expect(stateOf('hall-02')).toHaveTextContent('running')
    expect(stateOf('studio-03')).toHaveTextContent('failed')
    expect(stateOf('studio-03')).toHaveClass('tone-danger')
    expect(stateOf('stair-04')).toHaveTextContent('timed_out')
    expect(stateOf('store-05')).toHaveTextContent('queued')
    expect(stateOf('lobby-06')).toHaveTextContent('uploading')
    expect(stateOf('corridor-07')).toHaveTextContent('draft')
    expect(stateOf('corridor-07')).toHaveClass('tone-muted')

    const failed = within(jobs.getByRole('article', { name: 'Job studio-03' }))
    expect(
      failed.getByText(
        'The generation service returned an error. The capture is preserved; retry uses the same bundle.',
      ),
    ).toBeInTheDocument()
    expect(failed.getByText(/^op /)).toHaveTextContent(
      'op op_2c8811 · world — · model world-gen-1 · 8 frames uploaded · source cap-0139 · reconstruct_8',
    )
    expect(
      within(jobs.getByRole('article', { name: 'Job stair-04' })).getByRole('button', {
        name: 'Retry with the same capture',
      }),
    ).toBeInTheDocument()
    expect(
      within(jobs.getByRole('article', { name: 'Job corridor-07' })).getByText(/^op /),
    ).toHaveTextContent('op — · world — · model world-gen-1 · no bundle accepted yet · source none')
    expect(jobs.getAllByRole('button', { name: 'Retry with the same capture' })).toHaveLength(2)
    expect(jobs.getAllByRole('button', { name: 'Open room world' })).toHaveLength(1)
    expect(
      jobs.getByText(
        'A generated world is labelled generated. Its source photos stay visible beside it. It is not a factual or safety record.',
      ),
    ).toBeInTheDocument()

    await user.click(failed.getByRole('button', { name: 'Retry with the same capture' }))
    expect(screen.getByRole('status', { name: 'World Builder notice' })).toHaveTextContent(
      'Retrying studio-03 with the same bundle cap-0139 · reconstruct_8 and public false. Uploading now.',
    )
    expect(stateOf('studio-03')).toHaveTextContent('uploading')
    act(() => scheduler.advance(1_400))
    expect(stateOf('studio-03')).toHaveTextContent('queued')
  })

  test('empty: a building with no rooms and no jobs says so on both panes', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4 })
    await openWorlds(user)

    const rooms = within(screen.getByRole('region', { name: 'Rooms' }))
    expect(rooms.getByText('Building — ground floor')).toBeInTheDocument()
    expect(rooms.getByText(/No rooms with explicit doorway adjacency/)).toHaveTextContent(
      'No floor-plan reference is recorded',
    )
    expect(rooms.getByText(/No rooms are catalogued for this building/)).toBeInTheDocument()

    const jobs = await openJobs(user)
    expect(jobs.getByText('No generation jobs exist for this session. Submit a room to start one.')).toBeInTheDocument()
    expect(jobs.queryByRole('article')).not.toBeInTheDocument()
  })

  test('degraded: the console link is down, so submit and retry are refused while the snapshot stays', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'down' })
    await openWorlds(user)

    expect(screen.getByRole('status', { name: 'World Builder connection' })).toHaveTextContent(
      'The console connection is disconnected. World Builder shows the last snapshot received; submissions and retries are refused until the relay reports connected.',
    )
    const studio = within(screen.getByRole('article', { name: 'Room studio-03' }))
    expect(studio.getByRole('button', { name: 'Submit for generation' })).toBeDisabled()
    expect(studio.getByText('The console connection is disconnected. Nothing can be submitted.')).toBeInTheDocument()

    const jobs = await openJobs(user)
    expect(jobs.getAllByRole('article')).toHaveLength(7)
    jobs.getAllByRole('button', { name: 'Retry with the same capture' }).forEach((button) => {
      expect(button).toBeDisabled()
    })
  })

  test('unreported: production has no world endpoint, so rooms and jobs say so', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4, catalog: 'unreported' })
    await openWorlds(user)

    expect(screen.getByText(/does not report rooms, bundles, or generation jobs/)).toBeInTheDocument()
    await openPaneTab(user, 'World Builder panes', 'Jobs')
    expect(screen.getByText(/does not report generation jobs/)).toBeInTheDocument()
  })
})

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { MapAuthoring } from './MapAuthoring'
import { clientFixture, draftFixture, revision } from './test-fixtures'
import type { MapDraft } from './types'

vi.mock('./files', async (load) => ({
  ...await load<typeof import('./files')>(),
  verifyDraftImage: vi.fn(async (draft: MapDraft) => draft),
}))

test.each([Number.MAX_SAFE_INTEGER, -Number.MAX_SAFE_INTEGER])(
  'an editable relay draft with an out-of-range creation timestamp stays reviewable (%s)',
  async (createdAt) => {
    const user = userEvent.setup()
    const client = clientFixture()
    const draft = draftFixture()
    draft.metadata.createdAt = createdAt
    client.load.mockResolvedValue({ reference: revision, draft })
    render(<MapAuthoring client={client} />)

    await user.click(screen.getByRole('button', { name: 'Load revision list' }))
    await user.click(await screen.findByRole('button', { name: 'Load revision-1' }))
    await waitFor(() => expect(client.load).toHaveBeenCalled())
    await screen.findByText('Relay revision loaded. Approval requires fresh server validation of this exact revision.')

    expect(screen.getByRole('region', { name: 'Map and zone authoring' })).toBeInTheDocument()
    expect(screen.getByLabelText('Map created at · UTC')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Validate saved revision' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review approval' })).toBeDisabled()
  },
)

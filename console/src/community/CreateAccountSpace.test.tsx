import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import CreateAccountSpace from './CreateAccountSpace'
import { AccountClient } from './accountClient'
import { publishAccountSession } from './accountSession'

vi.mock('../atlas/SpaceMap', () => ({ default: ({ onPick, picking }: { onPick: (lng: number, lat: number) => void; picking: boolean }) =>
  <button type="button" disabled={!picking} onClick={() => onPick(-97.7431, 30.2672)}>Choose Austin point</button> }))
afterEach(() => { publishAccountSession(null); vi.unstubAllEnvs(); vi.restoreAllMocks() })
function view() {
  vi.stubEnv('VITE_ATLAS_API_ORIGIN', 'https://atlas.example')
  const session = { key: 'user:session', userId: 'user', getToken: async () => 'signed-session' }
  publishAccountSession(session)
  const api = new AccountClient(session)
  const create = vi.spyOn(api, 'create').mockResolvedValue({ space_id: 'created', session: 'private', role: 'owner' })
  const onCreated = vi.fn(), onClose = vi.fn()
  render(<CreateAccountSpace api={api} onCreated={onCreated} onClose={onClose} />)
  return { create, onCreated, onClose }
}
function nameSpace() {
  fireEvent.change(screen.getByLabelText('Space name'), { target: { value: 'Our lake weekends' } })
  fireEvent.change(screen.getByLabelText('Place name'), { target: { value: 'Lady Bird Lake, Austin, Texas' } })
}

it('does not infer location or publish anything until the creator explicitly chooses and submits', async () => {
  const { create, onCreated } = view()
  nameSpace()
  expect(screen.getByRole('button', { name: 'Create my space' })).toBeDisabled()
  fireEvent.click(await screen.findByRole('button', { name: 'Choose Austin point' }))
  expect(create).not.toHaveBeenCalled()
  expect(screen.getByText(/Chosen location/)).toHaveTextContent('30.26720, -97.74310')
  fireEvent.click(screen.getByRole('button', { name: 'Create my space' }))
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith({ space_id: 'created', session: 'private', role: 'owner' }))
  expect(create).toHaveBeenCalledWith(expect.stringMatching(/^[0-9a-f-]{36}$/), expect.objectContaining({ title: 'Our lake weekends', latitude: 30.2672, longitude: -97.7431 }))
})

it('freezes the payload and reuses its draft ID when creation could have succeeded before a timeout', async () => {
  const { create, onCreated } = view()
  create.mockRejectedValueOnce(new Error('Response timed out.'))
  nameSpace()
  fireEvent.click(await screen.findByRole('button', { name: 'Choose Austin point' }))
  fireEvent.click(screen.getByRole('button', { name: 'Create my space' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('draft is held unchanged')
  expect(screen.getByLabelText('Space name')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Choose Austin point' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry this draft' }))
  await waitFor(() => expect(onCreated).toHaveBeenCalledOnce())
  expect(create.mock.calls[1]).toEqual(create.mock.calls[0])
})

it('offers explicit coordinate entry when the map or location service cannot be used', async () => {
  const { create } = view()
  nameSpace()
  fireEvent.change(screen.getByLabelText('Latitude'), { target: { value: '99' } })
  fireEvent.change(screen.getByLabelText('Longitude'), { target: { value: '-97.74' } })
  expect(screen.getByRole('button', { name: 'Create my space' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Latitude'), { target: { value: '30.27' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create my space' }))
  await waitFor(() => expect(create).toHaveBeenCalledOnce())
  expect(create.mock.calls[0][1]).toMatchObject({ latitude: 30.27, longitude: -97.74 })
})

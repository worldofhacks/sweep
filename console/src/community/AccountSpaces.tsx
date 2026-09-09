import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { WorkspaceSpaces } from '../atlas/SpacesModule'
import { Icon } from '../atlas/Icon'
import { AccountClient, clearPendingInvitation, invitationToken, type AccountGrant, type InvitePreview, type JoinedSpace } from './accountClient'
import type { AccountSession } from './accountSession'
const CreateAccountSpace = lazy(() => import('./CreateAccountSpace'))

export default function AccountSpaces({ session, pending, onClose, onHandled }: {
  session: AccountSession | null; pending: string; onClose: () => void; onHandled: () => void
}) {
  if (!session) return <main className="atlas-workspace account-spaces"><button className="atlas-back" onClick={onClose}><Icon name="back" />Back to Spaces</button>
    <span className="atlas-eyebrow">A PLACE FOR YOUR PEOPLE</span><h1>Better, together.</h1>
    <p>Sign in using the account button above to create your own Space, review an invitation, and find your shared places.</p>
    {pending && <p className="account-hint">Your invitation is waiting in this tab. Sign-in does not accept it—you’ll review its permissions first.</p>}
    {!import.meta.env.VITE_CLERK_PUBLISHABLE_KEY && <p className="atlas-muted">Account sign-in hasn’t been configured yet. You can still explore examples and use existing workspace invitations.</p>}
    {pending && <button className="atlas-text-button" onClick={() => { clearPendingInvitation(); onClose() }}>Discard this invitation</button>}
  </main>
  return <SignedAccountSpaces key={session.key} session={session} pending={pending} onClose={onClose} onHandled={onHandled} />
}

function SignedAccountSpaces({ session, pending, onClose, onHandled }: { session: AccountSession; pending: string; onClose: () => void; onHandled: () => void }) {
  const connection = useMemo(() => {
    try { return { api: new AccountClient(session), error: '' } }
    catch (error) { return { api: null, error: error instanceof Error ? error.message : 'Account sharing is unavailable.' } }
  }, [session])
  const api = connection.api
  const [spaces, setSpaces] = useState<JoinedSpace[]>([])
  const [selected, setSelected] = useState<AccountGrant | null>(null)
  const [input, setInput] = useState(pending)
  const [review, setReview] = useState<{ token: string; value: InvitePreview } | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [directoryError, setDirectoryError] = useState('')
  const [notice, setNotice] = useState('')
  const [revision, setRevision] = useState(0)
  const [leaving, setLeaving] = useState<JoinedSpace | null>(null)
  const [creating, setCreating] = useState(false)
  const selectedClient = useMemo(() => api && selected ? api.space(selected) : null, [api, selected])
  useEffect(() => {
    if (!api) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function update() {
      try {
        const values = await api!.list(controller.signal)
        if (!controller.signal.aborted) { setSpaces(values); setLoading(false); setDirectoryError('') }
      } catch (error) {
        if (!controller.signal.aborted) { setSpaces([]); setLoading(false); setDirectoryError(error instanceof Error ? error.message : 'Your shared spaces could not be loaded.') }
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void update(), 10_000)
    }
    void update()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [api, revision])
  async function run(operation: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await operation() } catch (error) { setError(error instanceof Error ? error.message : 'Please try again.') }
    finally { setBusy(false) }
  }
  if (selected && selectedClient) return <WorkspaceSpaces key={selected.space_id} services={{ atlas: selectedClient }}
    initialSpace={selected.space_id} accountRole={selected.role} accountKey={session.userId}
    onExitAccount={() => { setSelected(null); setRevision(value => value + 1) }} />
  if (creating && api) return <main className="atlas-workspace account-spaces"><Suspense fallback={<p role="status">Preparing your new Space…</p>}>
    <CreateAccountSpace api={api} onClose={() => { setCreating(false); setRevision(value => value + 1) }} onCreated={grant => { setCreating(false); setSelected(grant); setRevision(value => value + 1) }} />
  </Suspense></main>
  return <main className="atlas-workspace account-spaces">
    <button className="atlas-back" onClick={onClose}><Icon name="back" />Back to Spaces</button>
    <div className="account-page-heading"><div><span className="atlas-eyebrow">YOUR SHARED CORNERS OF THE WORLD</span><h1>My spaces.</h1><p>The places you make. The stories you share. A little more connected.</p></div><button className="atlas-primary" disabled={!api || loading || Boolean(directoryError)} onClick={() => setCreating(true)}><Icon name="plus" size={18} />Create a space</button></div>
    {(connection.error || directoryError || error) && <p className="account-hint" role="alert">{connection.error || error || directoryError} <button className="atlas-text-button" onClick={() => setRevision(value => value + 1)}>Retry directory</button></p>}
    {notice && <p role="status">{notice}</p>}
    <section className="account-join" aria-label="Join a shared space"><h2>Have an invitation?</h2>
      <form onSubmit={event => { event.preventDefault(); if (api) void run(async () => {
        const token = invitationToken(input)
        const value = await api.preview(token)
        setReview({ token, value })
      }) }}><label className="atlas-field">Account invitation<input value={input} disabled={busy} onChange={event => { setInput(event.target.value); setReview(null) }} maxLength={4096} placeholder="Paste an invitation link or code" /></label>
        <button className="atlas-secondary" disabled={!api || !input.trim() || busy}>{busy ? 'Working…' : 'Review invitation'}</button>
      </form>
      {review && <div className="account-invitation" aria-label="Invitation permissions"><span className="atlas-eyebrow">YOU’RE INVITED</span><h3>{review.value.title}</h3><p>{review.value.place}</p>
        <p>{review.value.role === 'contributor' ? 'You can view this Space and add captures.' : 'You can view this Space, but cannot add or change anything.'} This includes current and future captures, memory recordings, location details, and any live location explicitly shared here. It does not grant fleet controls or paid AI access.</p>
        <p className="atlas-fine">Accept before {new Date(review.value.expires_at).toLocaleString()}. You can leave at any time; leaving does not delete your earlier contributions.</p>
        <button className="atlas-primary" disabled={busy} onClick={() => api && void run(async () => {
          const grant = await api.accept(review.token)
          clearPendingInvitation(); onHandled(); setInput(''); setReview(null); setSelected(grant); setRevision(value => value + 1)
        })}>Join this space</button> <button className="atlas-text-button" disabled={busy} onClick={() => { clearPendingInvitation(); onHandled(); setReview(null); setInput('') }}>Not now</button>
      </div>}
    </section>
    <section aria-label="Your joined spaces"><h2>Places you belong.</h2>
      {loading && api ? <p role="status">Finding your shared spaces…</p> : !spaces.length ? <div className="account-hint"><h3>A place for your first shared story.</h3><p>Create your own Space or accept an invitation. Neither action publishes your media automatically.</p></div>
        : <div className="account-space-grid">{spaces.map(item => <article key={item.space.id}>
          <Icon name="spaces" size={26} /><span className="atlas-eyebrow">{item.role === 'owner' ? 'YOUR SPACE' : item.role === 'viewer' ? 'VIEWER' : 'CONTRIBUTOR'}</span><h3>{item.space.title}</h3><p>{item.space.place}</p><p>{item.space.description || 'A place to see more, together.'}</p>
          <button className="atlas-primary" onClick={() => setSelected({ space_id: item.space.id, session: item.session, role: item.role })}>Open space <Icon name="arrow" size={16} /></button>
          {item.role !== 'owner' && <button className="atlas-text-button" disabled={busy} onClick={() => setLeaving(item)}>Leave space</button>}
        </article>)}</div>}
    </section>
    {leaving && <div className="account-hint" role="group" aria-label="Confirm leaving space"><h3>Leave {leaving.space.title}?</h3><p>Your contributions remain with the Space. You’ll need a new account invitation to return. Any separate contribution link still needs its own revocation.</p>
      <button className="atlas-secondary" disabled={busy} onClick={() => setLeaving(null)}>Stay</button> <button className="atlas-primary" disabled={busy} onClick={() => api && void run(async () => {
        await api.leave(leaving.space.id); setLeaving(null); setRevision(value => value + 1); setNotice('You’ve left the space.')
      })}>Confirm leaving</button></div>}
  </main>
}

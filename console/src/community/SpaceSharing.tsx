import { useEffect, useState } from 'react'
import type { AccountInvitation, AtlasClient, SpaceMember } from '../atlas/client'
import { invitationLink, accountApiOrigin } from './accountClient'
import { relayHttpOrigin } from '../relay/origin'

export default function SpaceSharing({ client, spaceId, legacyInvitation, replaceLegacy, allowAccounts = true, allowLegacy = true }: {
  client: AtlasClient; spaceId: string; legacyInvitation: () => Promise<string>; replaceLegacy: () => Promise<string>; allowAccounts?: boolean; allowLegacy?: boolean
}) {
  const [state, setState] = useState<{ enabled: boolean; invitations: AccountInvitation[]; members: SpaceMember[] } | null>(null)
  const [role, setRole] = useState<'viewer' | 'contributor'>('contributor')
  const [hours, setHours] = useState(24)
  const [link, setLink] = useState('')
  const [createdId, setCreatedId] = useState('')
  const [legacy, setLegacy] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [revision, setRevision] = useState(0)
  const [confirm, setConfirm] = useState<SpaceMember | null>(null)
  useEffect(() => {
    if (!allowAccounts) return
    const controller = new AbortController()
    void Promise.all([client.accountInvitations(spaceId, controller.signal), client.members(spaceId, controller.signal)])
      .then(([invitations, members]) => {
        if (!controller.signal.aborted) setState({ ...invitations, ...members,
          enabled: invitations.enabled && relayHttpOrigin(client.connection.baseUrl) === accountApiOrigin() })
      }).catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Sharing settings are unavailable.') })
    return () => controller.abort()
  }, [client, spaceId, revision, allowAccounts])
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (error) { setError(error instanceof Error ? error.message : 'Please try again.') }
    finally { setBusy(false) }
  }
  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); setNotice('Copied. Send it to someone you trust.') }
    catch { setNotice('Select the invitation text and copy it manually.') }
  }
  return <div className="account-sharing">
    <p className="atlas-muted">Bring someone into this place. Account invitations are for one person and never grant fleet controls.</p>
    {error && <p role="alert">{error} <button className="atlas-text-button" disabled={busy} onClick={() => { setError(''); setRevision(value => value + 1) }}>Retry settings</button></p>}
    {allowAccounts && !state && !error && <p role="status">Loading sharing settings…</p>}
    {!allowAccounts && <p className="atlas-muted">Account invitations are managed in the web console. This device keeps using its existing contribution invitation.</p>}
    {state && !state.enabled && <p className="account-hint">Account invitations need sign-in configured on this application’s API. Existing contribution links are available below.</p>}
    {state?.enabled && <>
      <fieldset className="atlas-fieldset" disabled={busy}>
        <legend>What can they do?</legend>
        <div className="account-choices">{(['contributor', 'viewer'] as const).map(value => <button type="button" key={value} aria-pressed={role === value} onClick={() => setRole(value)}>
          <strong>{value === 'contributor' ? 'Contribute together' : 'Just explore'}</strong>
          <span>{value === 'contributor' ? 'View everything here and add captures.' : 'View captures, memories, and worlds. No changes.'}</span>
        </button>)}</div>
      </fieldset>
      <fieldset className="atlas-fieldset" disabled={busy}><legend>Invitation expires in</legend><div className="account-expiry">
        {[24, 168].map(value => <button key={value} type="button" aria-pressed={hours === value} onClick={() => setHours(value)}>{value === 24 ? '24 hours' : '7 days'}</button>)}
      </div></fieldset>
      <p className="atlas-fine">They can access all current and future captures and memory recordings in this Space until membership is removed, including location details and any live location explicitly shared here. Editing memories and running AI remain workspace-owner actions.</p>
      <button className="atlas-primary atlas-full" disabled={busy} onClick={() => void run(async () => {
        const invitation = await client.inviteAccount(spaceId, role, hours)
        setLink(invitationLink(invitation.token)); setCreatedId(invitation.id); setRevision(value => value + 1)
      })}>{busy ? 'Working…' : 'Create account invitation'}</button>
      {link && <div className="account-invitation"><label className="atlas-field">Account invitation link<textarea readOnly value={link} rows={3} /></label>
        <button className="atlas-secondary atlas-full" onClick={() => void copy(link)}>Copy account invitation</button></div>}
      {state.invitations.length > 0 && <section aria-label="Pending invitations"><h3>Waiting to join</h3><ul className="account-list">{state.invitations.map(invite => <li key={invite.id}>
        <div><strong>{invite.role === 'viewer' ? 'Viewer' : 'Contributor'} invitation</strong><small>Expires {new Date(invite.expires_at).toLocaleString()}</small></div>
        <button className="atlas-text-button" disabled={busy} onClick={() => void run(async () => {
          await client.revokeAccountInvitation(spaceId, invite.id)
          if (createdId === invite.id) setLink('')
          setRevision(value => value + 1); setNotice('Invitation revoked. It can no longer be accepted.')
        })}>Revoke</button></li>)}</ul></section>}
      <section aria-label="Space members"><h3>People with account access</h3>{state.members.length === 0 ? <p className="atlas-muted">No account members yet. An accepted invitation will appear here.</p>
        : <ul className="account-list">{state.members.map(member => <li key={member.account_id}><div><strong>{member.role === 'owner' ? 'Owner' : member.role === 'viewer' ? 'Viewer' : 'Contributor'}</strong>
          <small>Account {member.account_id.slice(-8)} · joined {new Date(member.joined_at).toLocaleDateString()}</small></div>
          {member.role !== 'owner' && <button className="atlas-text-button" disabled={busy} onClick={() => setConfirm(member)}>Remove access</button>}</li>)}</ul>}</section>
      {confirm && <div className="account-hint" role="group" aria-label="Confirm membership removal"><p>Remove account {confirm.account_id.slice(-8)}? Their contributions remain. Existing independent contribution links and downloaded copies are unaffected.</p>
        <button className="atlas-secondary" disabled={busy} onClick={() => setConfirm(null)}>Keep access</button> <button className="atlas-primary" disabled={busy} onClick={() => void run(async () => {
          await client.removeMember(spaceId, confirm.account_id); setConfirm(null); setRevision(value => value + 1); setNotice('Account membership removed.')
        })}>Confirm removal</button></div>}
    </>}
    {allowLegacy && <details className="account-legacy"><summary>Existing contribution links</summary><p className="atlas-fine">Anyone holding one of these links can view and contribute without signing in. Removing an account does not revoke these links. Replacing a link invalidates earlier links, but cannot erase downloaded copies.</p>
      <button className="atlas-secondary" disabled={busy} onClick={() => void run(async () => { setLegacy(await legacyInvitation()) })}>Show contribution invitation</button>
      {legacy && <><label className="atlas-field">Space invitation<textarea readOnly rows={5} value={legacy} /></label><button className="atlas-secondary" onClick={() => void copy(legacy)}>Copy contribution invitation</button>
        <button className="atlas-text-button" disabled={busy} onClick={() => void run(async () => { setLegacy(await replaceLegacy()); setNotice('Earlier contribution links are now invalid. Account memberships are unchanged.') })}>Replace and revoke earlier links</button></>}
    </details>}
    {notice && <p role="status">{notice}</p>}
  </div>
}

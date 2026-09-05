import { useState } from 'react'
import {
  configSemantics,
  configurationChangedDetail,
  deriveCatalogLink,
  type CatalogLink,
} from '../../catalog/derive'
import type { ConfigGroup, ModeRecord, StagedChange } from '../../catalog/types'
import { noteFromError, type CatalogNoteState } from '../catalog-notes'
import { CatalogNote, LinkNotice } from '../catalog-shared'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'

/**
 * Configuration, rendered under Reference › Config. Ordinary groups apply now
 * and, while a plan is pending, warn before invalidating it; safety-sensitive
 * groups are staged and applied between runs, so they never touch a plan.
 */
export function ConfigModule({ controller, catalog }: ModuleProps) {
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [confirming, setConfirming] = useState<string | null>(null)
  const [note, setNote] = useState<CatalogNoteState | null>(null)
  const { snapshot, client } = catalog
  const config = snapshot.config
  const link = deriveCatalogLink(controller.state, 'Configuration', 'saves and staging are refused')
  const pending = controller.pendingRequest

  if (config === null) {
    return (
      <div className="cat-swap">
        <LinkNotice link={link} label="Configuration connection" />
        <EmptyModule what="editable configuration" />
      </div>
    )
  }

  const editKey = (group: ConfigGroup, key: string) => `${group.group_id}.${key}`
  const changedValues = (group: ConfigGroup): Record<string, string> =>
    Object.fromEntries(
      group.fields
        .map((field) => [field.key, edits[editKey(group, field.key)]] as const)
        .filter(([, value]) => value !== undefined)
        .filter(([key, value]) => value !== group.fields.find((f) => f.key === key)?.value)
        .map(([key, value]) => [key, value as string]),
    )
  const clearEdits = (group: ConfigGroup) =>
    setEdits((prev) =>
      Object.fromEntries(
        Object.entries(prev).filter(([key]) => !key.startsWith(`${group.group_id}.`)),
      ),
    )

  const save = async (group: ConfigGroup) => {
    const values = changedValues(group)
    const count = Object.keys(values).length
    try {
      await client.applyConfig(group.group_id, values)
      clearEdits(group)
      setNote({
        text: `Saved ${group.title} — ${count} ${count === 1 ? 'value' : 'values'} applied now.`,
        tone: 'muted',
      })
    } catch (error) {
      setNote(noteFromError(error))
    }
  }

  const stage = async (group: ConfigGroup) => {
    const values = changedValues(group)
    const count = Object.keys(values).length
    try {
      await client.stageConfig(group.group_id, values)
      clearEdits(group)
      setNote({
        text: `Staged ${group.title} — ${count} ${count === 1 ? 'value' : 'values'} pending until the next run. Nothing changes until then.`,
        tone: 'muted',
      })
    } catch (error) {
      setNote(noteFromError(error))
    }
  }

  const act = (group: ConfigGroup) => {
    if (group.staged) {
      void stage(group)
    } else if (pending) {
      setConfirming(group.group_id)
    } else {
      void save(group)
    }
  }

  const confirmSave = (group: ConfigGroup) => {
    controller.invalidatePending(
      'configuration_changed',
      configurationChangedDetail(group.title),
    )
    setConfirming(null)
    void save(group)
  }

  return (
    <div className="cat-swap">
      <LinkNotice link={link} label="Configuration connection" />
      <p className="cfg-warning" role="status">
        A configuration change while a plan is active invalidates that plan. The form warns before
        the change, and the shell states the invalidation.
      </p>
      <CatalogNote label="Configuration notice" note={note} />
      {config.groups.length === 0 ? (
        <p className="cat-empty">No configuration groups are reported for this session.</p>
      ) : (
        <div data-two="1">
          {config.groups.map((group) => (
            <GroupForm
              key={group.group_id}
              group={group}
              edits={edits}
              staged={config.staged_changes.filter((change) => change.group_id === group.group_id)}
              link={link}
              changed={Object.keys(changedValues(group)).length}
              confirming={confirming === group.group_id}
              pendingTitle={pending?.plan?.title ?? pending?.intent.name ?? null}
              onEdit={(key, value) => setEdits((prev) => ({ ...prev, [editKey(group, key)]: value }))}
              onAct={() => act(group)}
              onConfirm={() => confirmSave(group)}
              onKeep={() => setConfirming(null)}
            />
          ))}
        </div>
      )}
      <section className="cat-section" aria-labelledby="cfg-modes-title">
        <h3 className="cat-h3" id="cfg-modes-title">
          Modes
        </h3>
        <p className="cfg-modes-intro">
          Mode is relay-owned. v1 execution is indoor only; the outdoor modes are designed and
          return unsupported.
        </p>
        {config.modes.length === 0 ? (
          <p className="cat-line">No modes are reported.</p>
        ) : (
          config.modes.map((mode) => <ModeRow key={mode.mode} mode={mode} />)
        )}
      </section>
    </div>
  )
}

function GroupForm({
  group,
  edits,
  staged,
  link,
  changed,
  confirming,
  pendingTitle,
  onEdit,
  onAct,
  onConfirm,
  onKeep,
}: {
  group: ConfigGroup
  edits: Record<string, string>
  staged: StagedChange[]
  link: CatalogLink
  changed: number
  confirming: boolean
  pendingTitle: string | null
  onEdit: (key: string, value: string) => void
  onAct: () => void
  onConfirm: () => void
  onKeep: () => void
}) {
  const semantics = configSemantics(group)
  const blocked = !link.up
    ? `The console connection is ${link.status}. Nothing can be sent.`
    : changed === 0
      ? 'No values changed.'
      : undefined
  return (
    <section className="cfg-group" aria-labelledby={`cfg-${group.group_id}`}>
      <div className="cfg-head">
        <h3 className="cfg-title" id={`cfg-${group.group_id}`}>
          {group.title}
        </h3>
        <span className={`cfg-word tone-${semantics.tone}`}>{semantics.word}</span>
      </div>
      <p className={`cfg-semantics tone-${semantics.tone}`}>{semantics.sentence}</p>
      {group.fields.map((field) => {
        const stagedChange = staged.find((change) => change.key === field.key)
        return (
          <label className="cfg-field" key={field.key}>
            <span>{field.label}</span>
            <span className="cfg-field-side">
              {stagedChange && <span className="cfg-staged">staged {stagedChange.value}</span>}
              <input
                className="cfg-input"
                value={edits[`${group.group_id}.${field.key}`] ?? field.value}
                disabled={!link.up}
                onChange={(event) => onEdit(field.key, event.target.value)}
              />
            </span>
          </label>
        )
      })}
      {confirming ? (
        <div className="cfg-confirm" role="group" aria-label={`Confirm ${group.title} change`}>
          <p>
            Saving {group.title} invalidates the pending plan{' '}
            {pendingTitle ? <span className="mono">{pendingTitle}</span> : null}. Nothing has been
            sent.
          </p>
          <div className="cfg-confirm-actions">
            <button type="button" className="cat-button is-primary" onClick={onConfirm}>
              Save and invalidate the plan
            </button>
            <button type="button" className="cat-button" onClick={onKeep}>
              Keep the plan
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className="cat-button is-config"
          disabled={blocked !== undefined}
          title={blocked}
          onClick={onAct}
        >
          {semantics.action}
        </button>
      )}
    </section>
  )
}

function ModeRow({ mode }: { mode: ModeRecord }) {
  return (
    <div className="cfg-mode">
      <p className="cfg-mode-head">
        <span className="cfg-mode-id">{mode.mode}</span>
        <span className={`cfg-mode-status tone-${mode.status === 'accepted' ? 'ok' : 'warn'}`}>
          {mode.status}
        </span>
        <span className="cfg-mode-note">{mode.note}</span>
      </p>
      <p className="cfg-mode-facts">
        <span>positioning {mode.positioning}</span>
        <span>box {mode.box}</span>
        <span>spacing {mode.spacing}</span>
        <span>speed {mode.speed}</span>
      </p>
    </div>
  )
}

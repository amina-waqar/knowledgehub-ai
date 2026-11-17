import { useState } from 'react'

import type { Workspace } from '../types/Workspace'

type WorkspaceSelectorProps = {
  workspaces: Workspace[]
  currentWorkspaceId: number | null
  onSelect: (workspaceId: number) => void
  onCreate: (name: string) => Promise<void>
}

function WorkspaceSelector({
  workspaces,
  currentWorkspaceId,
  onSelect,
  onCreate,
}: WorkspaceSelectorProps) {
  const [isCreating, setIsCreating] = useState(false)
  const [name, setName] = useState('')
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState('')

  const currentWorkspace = workspaces.find(
    (workspace) => workspace.id === currentWorkspaceId
  )

  async function createWorkspace(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()

    if (!name.trim()) {
      return
    }

    setIsSaving(true)
    setError('')

    try {
      await onCreate(name.trim())
      setName('')
      setIsCreating(false)
    } catch (error) {
      setError(
        error instanceof Error ? error.message : 'Could not create workspace'
      )
    } finally {
      setIsSaving(false)
    }
  }

  function cancelCreate() {
    setIsCreating(false)
    setName('')
    setError('')
  }

  return (
    <div className="workspace-selector">
      <label htmlFor="workspace-select">Workspace</label>

      <select
        id="workspace-select"
        value={currentWorkspaceId ?? ''}
        onChange={(event) => onSelect(Number(event.target.value))}
        disabled={workspaces.length === 0}
      >
        {workspaces.map((workspace) => (
          <option key={workspace.id} value={workspace.id}>
            {workspace.name}
          </option>
        ))}
      </select>

      {currentWorkspace && (
        <p className="workspace-role">Your role: {currentWorkspace.role}</p>
      )}

      {isCreating ? (
        <form className="workspace-create-form" onSubmit={createWorkspace}>
          <label htmlFor="new-workspace-name">New workspace name</label>
          <input
            id="new-workspace-name"
            type="text"
            maxLength={100}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />

          <div className="workspace-create-actions">
            <button type="submit" disabled={isSaving || !name.trim()}>
              {isSaving ? 'Creating...' : 'Create'}
            </button>
            <button type="button" onClick={cancelCreate}>
              Cancel
            </button>
          </div>

          {error && <p className="workspace-error">{error}</p>}
        </form>
      ) : (
        <button
          type="button"
          className="workspace-new-button"
          onClick={() => setIsCreating(true)}
        >
          + New workspace
        </button>
      )}
    </div>
  )
}

export default WorkspaceSelector

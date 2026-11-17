import { useCallback, useEffect, useState } from 'react'

import { API_URL, authHeaders, errorMessage } from '../api'
import { hasRole } from '../workspaceRoles'
import type {
  Workspace,
  WorkspaceMember,
  WorkspaceMemberListResponse,
  WorkspaceRole,
} from '../types/Workspace'

type WorkspaceMembersProps = {
  token: string
  workspace: Workspace
  currentUserId: number | null
}

// The parent renders this with key={workspace.id}, so switching workspaces
// remounts it and the loading state starts fresh.
function WorkspaceMembers({
  token,
  workspace,
  currentUserId,
}: WorkspaceMembersProps) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState('')

  const [username, setUsername] = useState('')
  const [role, setRole] = useState<WorkspaceRole>('member')
  const [isSaving, setIsSaving] = useState(false)
  const [actionError, setActionError] = useState('')

  const canManage = hasRole(workspace.role, 'admin')
  const isOwner = workspace.role === 'owner'

  const loadMembers = useCallback(() => {
    return fetch(`${API_URL}/workspaces/${workspace.id}/members`, {
      headers: authHeaders(token, workspace.id),
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(await errorMessage(response, 'Could not load members'))
        }
        return response.json()
      })
      .then((data: WorkspaceMemberListResponse) => {
        setMembers(data.members)
        setLoadError('')
      })
      .catch((error) => {
        setLoadError(error.message)
      })
      .finally(() => {
        setIsLoading(false)
      })
  }, [token, workspace.id])

  useEffect(() => {
    loadMembers()
  }, [loadMembers])

  async function addMember(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()

    if (!username.trim()) {
      return
    }

    setIsSaving(true)
    setActionError('')

    try {
      const response = await fetch(
        `${API_URL}/workspaces/${workspace.id}/members`,
        {
          method: 'POST',
          headers: {
            ...authHeaders(token, workspace.id),
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ username: username.trim(), role }),
        }
      )

      if (!response.ok) {
        throw new Error(await errorMessage(response, 'Could not add member'))
      }

      setUsername('')
      setRole('member')
      await loadMembers()
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : 'Could not add member'
      )
    } finally {
      setIsSaving(false)
    }
  }

  async function removeMember(member: WorkspaceMember) {
    setActionError('')

    const response = await fetch(
      `${API_URL}/workspaces/${workspace.id}/members/${member.user_id}`,
      {
        method: 'DELETE',
        headers: authHeaders(token, workspace.id),
      }
    )

    if (!response.ok) {
      setActionError(await errorMessage(response, 'Could not remove member'))
      return
    }

    await loadMembers()
  }

  function canRemove(member: WorkspaceMember): boolean {
    if (!canManage || member.role === 'owner') {
      return false
    }
    if (member.user_id === currentUserId) {
      return false
    }
    return member.role !== 'admin' || isOwner
  }

  return (
    <section className="workspace-members">
      {canManage && (
        <form className="upload-form" onSubmit={addMember}>
          <h2>Add Member</h2>

          <label htmlFor="member-username">Username</label>
          <input
            id="member-username"
            type="text"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="Example: jsmith"
          />

          <label htmlFor="member-role">Role</label>
          <select
            id="member-role"
            value={role}
            onChange={(event) => setRole(event.target.value as WorkspaceRole)}
          >
            <option value="member">Member: can create incidents and ask</option>
            <option value="viewer">Viewer: read only</option>
            {isOwner && (
              <option value="admin">Admin: can also manage members</option>
            )}
          </select>

          <button type="submit" disabled={isSaving || !username.trim()}>
            {isSaving ? 'Adding...' : 'Add Member'}
          </button>
        </form>
      )}

      {actionError && <p className="error-message">{actionError}</p>}

      <h2>Members</h2>

      {isLoading ? (
        <p>Loading members...</p>
      ) : loadError ? (
        <p className="error-message">{loadError}</p>
      ) : members.length === 0 ? (
        <p>No members found.</p>
      ) : (
        <ul className="member-list">
          {members.map((member) => (
            <li key={member.user_id} className="document-card member-card">
              <h3>{member.username}</h3>
              <p>
                <strong>Role:</strong> {member.role}
              </p>

              {canRemove(member) && (
                <button
                  type="button"
                  className="member-remove-button"
                  onClick={() => removeMember(member)}
                  aria-label={`Remove ${member.username}`}
                >
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export default WorkspaceMembers

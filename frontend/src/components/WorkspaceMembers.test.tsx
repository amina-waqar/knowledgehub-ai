import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import WorkspaceMembers from './WorkspaceMembers'
import { header, method, mockFetch } from '../test/mockFetch'
import type { Workspace, WorkspaceMember } from '../types/Workspace'

const ALICE = 1

const team: WorkspaceMember[] = [
  { user_id: ALICE, username: 'alice', role: 'owner' },
  { user_id: 2, username: 'bob', role: 'member' },
  { user_id: 3, username: 'carol', role: 'admin' },
]

function workspaceAs(role: Workspace['role']): Workspace {
  return { id: 5, name: 'Payments team', role }
}

function renderMembers(role: Workspace['role'], currentUserId = ALICE) {
  return render(
    <WorkspaceMembers token="tok" workspace={workspaceAs(role)} currentUserId={currentUserId} />
  )
}

function memberRow(username: string) {
  return screen.getByRole('heading', { name: username }).closest('li') as HTMLElement
}

describe('WorkspaceMembers', () => {
  it('loads members for the workspace with the workspace header', async () => {
    const fetchMock = mockFetch(() => ({ body: { members: team } }))

    renderMembers('owner')

    expect(await screen.findByRole('heading', { name: 'bob' })).toBeInTheDocument()
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/workspaces/5/members')
    expect(header(init, 'Authorization')).toBe('Bearer tok')
    expect(header(init, 'X-Workspace-Id')).toBe('5')
  })

  it('lets the owner add members and remove anyone except the owner', async () => {
    mockFetch(() => ({ body: { members: team } }))

    renderMembers('owner')
    await screen.findByRole('heading', { name: 'bob' })

    expect(screen.getByRole('heading', { name: 'Add Member' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Admin/ })).toBeInTheDocument()
    expect(within(memberRow('alice')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(memberRow('bob')).getByRole('button', { name: 'Remove bob' })).toBeInTheDocument()
    expect(within(memberRow('carol')).getByRole('button', { name: 'Remove carol' })).toBeInTheDocument()
  })

  it('does not let an admin grant admin or remove another admin', async () => {
    mockFetch(() => ({ body: { members: team } }))

    renderMembers('admin', 3)
    await screen.findByRole('heading', { name: 'bob' })

    expect(screen.queryByRole('option', { name: /Admin/ })).not.toBeInTheDocument()
    expect(within(memberRow('bob')).getByRole('button', { name: 'Remove bob' })).toBeInTheDocument()
    expect(within(memberRow('carol')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows members and viewers the list without management controls', async () => {
    mockFetch(() => ({ body: { members: team } }))

    renderMembers('member', 2)
    await screen.findByRole('heading', { name: 'bob' })

    expect(screen.queryByRole('heading', { name: 'Add Member' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument()
  })

  it('adds a member and refreshes the list', async () => {
    let members = team.slice(0, 1)
    const fetchMock = mockFetch((_url, init) => {
      if (method(init) === 'POST') {
        members = [...members, { user_id: 4, username: 'dave', role: 'viewer' }]
        return { status: 201, body: members[1] }
      }
      return { body: { members } }
    })

    renderMembers('owner')
    await screen.findByRole('heading', { name: 'alice' })
    await userEvent.type(screen.getByLabelText('Username'), ' dave ')
    await userEvent.selectOptions(screen.getByLabelText('Role'), 'viewer')
    await userEvent.click(screen.getByRole('button', { name: 'Add Member' }))

    expect(await screen.findByRole('heading', { name: 'dave' })).toBeInTheDocument()
    const post = fetchMock.mock.calls.find(([, init]) => method(init) === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ username: 'dave', role: 'viewer' })
    expect(screen.getByLabelText('Username')).toHaveValue('')
  })

  it('shows the API error when adding fails', async () => {
    mockFetch((_url, init) =>
      method(init) === 'POST'
        ? { status: 404, body: { detail: 'User not found.' } }
        : { body: { members: team } }
    )

    renderMembers('owner')
    await screen.findByRole('heading', { name: 'bob' })
    await userEvent.type(screen.getByLabelText('Username'), 'ghost')
    await userEvent.click(screen.getByRole('button', { name: 'Add Member' }))

    expect(await screen.findByText('User not found.')).toBeInTheDocument()
  })

  it('removes a member and refreshes the list', async () => {
    let members = team
    const fetchMock = mockFetch((_url, init) => {
      if (method(init) === 'DELETE') {
        members = members.filter((m) => m.username !== 'bob')
        return { status: 204 }
      }
      return { body: { members } }
    })

    renderMembers('owner')
    await userEvent.click(await screen.findByRole('button', { name: 'Remove bob' }))

    await screen.findByRole('heading', { name: 'carol' })
    expect(screen.queryByRole('heading', { name: 'bob' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([url, init]) => method(init) === 'DELETE' && url === '/workspaces/5/members/2')).toBe(true)
  })

  it('shows an error when members cannot be loaded', async () => {
    mockFetch(() => ({ status: 404, body: { detail: 'Workspace not found.' } }))

    renderMembers('owner')

    expect(await screen.findByText('Workspace not found.')).toBeInTheDocument()
  })
})

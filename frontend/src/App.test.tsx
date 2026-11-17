import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App from './App'
import { header, mockFetch } from './test/mockFetch'
import type { Workspace } from './types/Workspace'

const workspaces: Workspace[] = [
  { id: 1, name: "alice's workspace", role: 'owner' },
  { id: 2, name: 'Payments team', role: 'viewer' },
]

const incidentsByWorkspace: Record<string, { id: number; title: string }[]> = {
  '1': [{ id: 10, title: 'Checkout latency' }],
  '2': [{ id: 20, title: 'Card declines' }],
}

function mockApi() {
  return mockFetch((url, init) => {
    if (url === '/me') {
      return { body: { id: 1, username: 'alice', email: 'a@example.com', is_admin: false } }
    }
    if (url === '/workspaces') {
      return { body: { workspaces } }
    }
    if (url === '/incidents') {
      const workspaceId = header(init, 'X-Workspace-Id') ?? ''
      const incidents = (incidentsByWorkspace[workspaceId] ?? []).map((incident) => ({
        ...incident,
        description: null,
        status: 'open',
        severity: null,
        created_at: '2026-11-16T10:00:00Z',
        creator_id: 1,
      }))
      return { body: { count: incidents.length, incidents } }
    }
    return undefined
  })
}

function signIn(workspaceId?: number) {
  localStorage.setItem('token', 'tok')
  if (workspaceId !== undefined) {
    localStorage.setItem('workspaceId', String(workspaceId))
  }
}

describe('App workspaces', () => {
  it('loads incidents for the first workspace by default', async () => {
    const fetchMock = mockApi()
    signIn()

    render(<App />)

    expect(await screen.findByText('Checkout latency')).toBeInTheDocument()
    const incidentCalls = fetchMock.mock.calls.filter(([url]) => url === '/incidents')
    expect(incidentCalls.every(([, init]) => header(init, 'X-Workspace-Id') === '1')).toBe(true)
    expect(screen.getByText("alice's workspace", { selector: 'strong' })).toBeInTheDocument()
  })

  it('switching workspace reloads incidents and remembers the choice', async () => {
    mockApi()
    signIn()
    render(<App />)
    await screen.findByText('Checkout latency')

    await userEvent.selectOptions(screen.getByLabelText('Workspace'), 'Payments team')

    expect(await screen.findByText('Card declines')).toBeInTheDocument()
    expect(screen.queryByText('Checkout latency')).not.toBeInTheDocument()
    expect(localStorage.getItem('workspaceId')).toBe('2')
  })

  it('restores the remembered workspace and hides editing for viewers', async () => {
    mockApi()
    signIn(2)

    render(<App />)

    expect(await screen.findByText('Card declines')).toBeInTheDocument()
    expect(screen.getByText('You have view-only access to this workspace.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create Incident' })).not.toBeInTheDocument()
  })

  it('falls back to the first workspace if the remembered one is gone', async () => {
    mockApi()
    signIn(99)

    render(<App />)

    expect(await screen.findByText('Checkout latency')).toBeInTheDocument()
    expect(localStorage.getItem('workspaceId')).toBe('1')
  })
})

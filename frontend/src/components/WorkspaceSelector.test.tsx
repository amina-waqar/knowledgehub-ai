import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import WorkspaceSelector from './WorkspaceSelector'
import type { Workspace } from '../types/Workspace'

const workspaces: Workspace[] = [
  { id: 1, name: "alice's workspace", role: 'owner' },
  { id: 2, name: 'Payments team', role: 'viewer' },
]

function renderSelector(onCreate = vi.fn().mockResolvedValue(undefined)) {
  const onSelect = vi.fn()
  render(
    <WorkspaceSelector
      workspaces={workspaces}
      currentWorkspaceId={1}
      onSelect={onSelect}
      onCreate={onCreate}
    />
  )
  return { onSelect, onCreate }
}

describe('WorkspaceSelector', () => {
  it('lists workspaces and shows the role in the current one', () => {
    renderSelector()

    expect(screen.getByLabelText('Workspace')).toHaveValue('1')
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      "alice's workspace",
      'Payments team',
    ])
    expect(screen.getByText('Your role: owner')).toBeInTheDocument()
  })

  it('reports the chosen workspace id', async () => {
    const { onSelect } = renderSelector()

    await userEvent.selectOptions(screen.getByLabelText('Workspace'), 'Payments team')

    expect(onSelect).toHaveBeenCalledWith(2)
  })

  it('creates a workspace with a trimmed name and closes the form', async () => {
    const { onCreate } = renderSelector()

    await userEvent.click(screen.getByRole('button', { name: '+ New workspace' }))
    await userEvent.type(screen.getByLabelText('New workspace name'), '  Search team  ')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(onCreate).toHaveBeenCalledWith('Search team')
    expect(screen.queryByLabelText('New workspace name')).not.toBeInTheDocument()
  })

  it('does not allow a blank name', async () => {
    const { onCreate } = renderSelector()

    await userEvent.click(screen.getByRole('button', { name: '+ New workspace' }))
    await userEvent.type(screen.getByLabelText('New workspace name'), '   ')

    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    expect(onCreate).not.toHaveBeenCalled()
  })

  it('keeps the form open and shows the error when creation fails', async () => {
    renderSelector(vi.fn().mockRejectedValue(new Error('Name cannot be blank.')))

    await userEvent.click(screen.getByRole('button', { name: '+ New workspace' }))
    await userEvent.type(screen.getByLabelText('New workspace name'), 'Team')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('Name cannot be blank.')).toBeInTheDocument()
    expect(screen.getByLabelText('New workspace name')).toHaveValue('Team')
  })
})

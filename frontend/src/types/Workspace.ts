export type WorkspaceRole = 'owner' | 'admin' | 'member' | 'viewer'

export type Workspace = {
  id: number
  name: string
  role: WorkspaceRole
}

export type WorkspaceListResponse = {
  workspaces: Workspace[]
}

export type WorkspaceMember = {
  user_id: number
  username: string
  role: WorkspaceRole
}

export type WorkspaceMemberListResponse = {
  members: WorkspaceMember[]
}

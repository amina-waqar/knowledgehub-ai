import type { WorkspaceRole } from './types/Workspace'

const ROLE_RANK: Record<WorkspaceRole, number> = {
  viewer: 0,
  member: 1,
  admin: 2,
  owner: 3,
}

// Mirrors the backend's require_role(). The backend still enforces every
// check; this only decides which controls to show.
export function hasRole(
  role: WorkspaceRole | undefined,
  minimum: WorkspaceRole
): boolean {
  return role !== undefined && ROLE_RANK[role] >= ROLE_RANK[minimum]
}

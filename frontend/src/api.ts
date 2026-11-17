export const API_URL = import.meta.env.VITE_API_URL || ''

export function authHeaders(
  token: string,
  workspaceId: number | null
): Record<string, string> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  }

  if (workspaceId !== null) {
    headers['X-Workspace-Id'] = String(workspaceId)
  }

  return headers
}

export async function errorMessage(
  response: Response,
  fallback: string
): Promise<string> {
  try {
    const body = await response.json()
    return typeof body.detail === 'string' ? body.detail : fallback
  } catch {
    return fallback
  }
}

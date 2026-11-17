import { describe, expect, it } from 'vitest'

import { authHeaders, errorMessage } from './api'
import { hasRole } from './workspaceRoles'

describe('authHeaders', () => {
  it('sends only the token when no workspace is selected', () => {
    expect(authHeaders('abc', null)).toEqual({ Authorization: 'Bearer abc' })
  })

  it('adds the workspace header when a workspace is selected', () => {
    expect(authHeaders('abc', 7)).toEqual({
      Authorization: 'Bearer abc',
      'X-Workspace-Id': '7',
    })
  })
})

describe('errorMessage', () => {
  it('uses the API detail message', async () => {
    const response = new Response(JSON.stringify({ detail: 'User not found.' }), {
      status: 404,
    })
    expect(await errorMessage(response, 'fallback')).toBe('User not found.')
  })

  it('falls back when the body is not a detail message', async () => {
    expect(await errorMessage(new Response('oops', { status: 500 }), 'fallback')).toBe(
      'fallback'
    )
    const validation = new Response(JSON.stringify({ detail: [{ msg: 'bad' }] }), {
      status: 422,
    })
    expect(await errorMessage(validation, 'fallback')).toBe('fallback')
  })
})

describe('hasRole', () => {
  it('ranks owner > admin > member > viewer', () => {
    expect(hasRole('owner', 'admin')).toBe(true)
    expect(hasRole('admin', 'admin')).toBe(true)
    expect(hasRole('member', 'admin')).toBe(false)
    expect(hasRole('viewer', 'member')).toBe(false)
  })

  it('treats a missing role as no access', () => {
    expect(hasRole(undefined, 'viewer')).toBe(false)
  })
})

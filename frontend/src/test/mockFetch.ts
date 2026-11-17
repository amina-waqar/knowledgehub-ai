import { vi } from 'vitest'

type MockReply = { status?: number; body?: unknown }
type Handler = (url: string, init: RequestInit | undefined) => MockReply | undefined

export function mockFetch(handler: Handler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const reply = handler(url, init)

    if (!reply) {
      throw new Error(`Unexpected request: ${init?.method ?? 'GET'} ${url}`)
    }

    const status = reply.status ?? 200
    return new Response(status === 204 ? null : JSON.stringify(reply.body ?? {}), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  })

  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

export function header(init: RequestInit | undefined, name: string): string | null {
  return new Headers(init?.headers).get(name)
}

export function method(init: RequestInit | undefined): string {
  return init?.method ?? 'GET'
}

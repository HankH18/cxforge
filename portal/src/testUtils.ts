// Shared mocked-`fetch` plumbing for component tests. The portal must be
// tested against a mocked API, never a real backend — this is what makes
// that mocking installable per test while still recording exactly what was
// sent, so a test can assert on the real request payload rather than just
// on a handler having fired.
import { vi } from 'vitest'

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export interface FetchCall {
  url: string
  method: string
  body: unknown
}

type Handler = (call: FetchCall) => Response | Promise<Response>

/** Installs a `global.fetch` mock keyed by `"<METHOD> <pathname>"` (query
 * strings ignored for routing, but preserved on the recorded call). Every
 * call is pushed onto the returned `calls` array with its parsed JSON body,
 * so tests can assert on precisely what api.ts sent. Throws loudly for any
 * request without a matching handler instead of silently hitting the
 * network. */
export function installFetchMock(handlers: Record<string, Handler>): { calls: FetchCall[] } {
  const calls: FetchCall[] = []
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString()
    const method = (init?.method ?? 'GET').toUpperCase()
    const pathname = url.split('?')[0]
    let body: unknown
    if (init?.body) {
      try {
        body = JSON.parse(init.body as string)
      } catch {
        body = init.body
      }
    }
    const call: FetchCall = { url, method, body }
    calls.push(call)
    const key = `${method} ${pathname}`
    const handler = handlers[key]
    if (!handler) {
      throw new Error(`installFetchMock: no handler registered for ${key}`)
    }
    return handler(call)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls }
}

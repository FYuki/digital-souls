import { afterEach, describe, expect, test, vi } from 'vitest'

import { fetchScreenRouting } from './client'

const routing = {
  protocol_version: '1.0',
  type: 'screen_routing_disclosed',
  event_id: '10000000-0000-4000-8000-000000000001',
  client_session_id: '20000000-0000-4000-8000-000000000001',
  routing_revision:
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  vision_destination: 'local',
  chat_destination: 'local',
  limits: {
    allowed_mime_types: ['image/png', 'image/jpeg'],
    max_width: 2560,
    max_height: 2560,
    max_pixels: 4194304,
    max_bytes: 5242880,
    snapshot_max_age_ms: 5000,
    capture_timeout_ms: 5000,
    vision_timeout_ms: 30000,
    request_timeout_ms: 45000,
    max_concurrency: 1,
  },
}

const heartbeatAccepted = {
  protocol_version: '1.0',
  type: 'screen_session_heartbeat_accepted',
  event_id: '10000000-0000-4000-8000-000000000005',
  screen_session_id: '40000000-0000-4000-8000-000000000001',
  generation: 1,
  lease_expires_at: '2026-09-06T03:00:20Z',
}

const stubFetch = (
  handler: (url: string, init?: RequestInit) => Promise<Response> | Response,
) => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (
    handler(String(input), init)
  ))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('screen perception client request boundary', () => {
  test('returns the disclosed routing for a valid response', async () => {
    const fetchMock = stubFetch(() => new Response(JSON.stringify(routing)))

    await expect(fetchScreenRouting()).resolves.toEqual(routing)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/perception/screen/routing',
      { credentials: 'same-origin' },
    )
  })

  test('rejects a failed HTTP response with the request status', async () => {
    stubFetch(() => new Response('{}', { status: 503 }))

    await expect(fetchScreenRouting()).rejects.toThrow('status 503')
  })

  test('rejects a valid event of a different type at the domain boundary', async () => {
    stubFetch(() => new Response(JSON.stringify(heartbeatAccepted)))

    await expect(fetchScreenRouting()).rejects.toThrow('routing response')
  })

  test('rejects a malformed response body', async () => {
    stubFetch(() => new Response(JSON.stringify({})))

    await expect(fetchScreenRouting()).rejects.toThrow('protocol 1.0')
  })
})

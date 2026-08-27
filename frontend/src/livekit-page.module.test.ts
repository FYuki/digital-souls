import { cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

const fetchMock = vi.fn<typeof fetch>()

const tokenResponse = {
  session_id: '20000000-0000-4000-8000-000000000001',
  participant_id: '40000000-0000-4000-8000-000000000001',
  room: 'voice-20000000-0000-4000-8000-000000000001',
  token: 'browser-token',
  livekit_url: 'ws://127.0.0.1:7880',
  expires_at: '2026-08-27T00:01:30Z',
  reconnect_grace_ms: 60_000,
}
const conversationResponse = {
  character_id: 'miori',
  conversation_id: '20000000-0000-4000-8000-000000000001',
}

const mountLiveKitEntrypoint = async () => {
  window.history.replaceState({}, '', '/voice/livekit')
  document.body.innerHTML = '<div id="app"></div>'
  await import('./main')
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: 'LiveKit 音声transport実験' })).toBeTruthy()
  })
}

describe('LiveKit experimental page client flow', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    cleanup()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  test('token取得は固定graceとclient UUIDを公開APIへ送る', async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(conversationResponse), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(tokenResponse), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))
    await mountLiveKitEntrypoint()

    await fireEvent.click(screen.getByRole('button', { name: 'token取得' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/characters/miori/conversations')
    const [url, init] = fetchMock.mock.calls[1] ?? []
    expect(url).toBe('/voice/livekit/token')
    expect(init?.method).toBe('POST')
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>
    expect(body.protocol_version).toBe('1.0')
    expect(body.requested_reconnect_grace_ms).toBe(60_000)
    expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(body.conversation_id).toBe(conversationResponse.conversation_id)
    expect(body).not.toHaveProperty('session_id')
  })

  test('再接続用tokenは同じsession IDでendpointを再呼び出す', async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(conversationResponse), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(tokenResponse), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(tokenResponse), { status: 200 }))
    await mountLiveKitEntrypoint()
    await fireEvent.click(screen.getByRole('button', { name: 'token取得' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))

    await fireEvent.click(screen.getByRole('button', { name: '再接続token取得' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    const [, reconnectInit] = fetchMock.mock.calls[2] ?? []
    const reconnectBody = JSON.parse(String(reconnectInit?.body)) as Record<string, unknown>
    expect(reconnectBody.session_id).toBe(tokenResponse.session_id)
    expect(reconnectBody.requested_reconnect_grace_ms).toBe(60_000)
  })

  test('不完全なtoken応答を接続情報として受理しない', async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(conversationResponse), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: 'incomplete' }), { status: 200 }))
    await mountLiveKitEntrypoint()

    await fireEvent.click(screen.getByRole('button', { name: 'token取得' }))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toContain(
        'LiveKit response field reconnect_grace_ms',
      )
    })
    expect(screen.getByRole('button', { name: 'Room接続' }).hasAttribute('disabled')).toBe(true)
  })
})

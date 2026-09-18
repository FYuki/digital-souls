import {afterEach, describe, expect, test, vi} from 'vitest'
import {requestLiveKitToken, VoicePreparationError} from './client'

const token = {
  session_id: 'session', participant_id: 'participant', room: 'room',
  token: 'token', livekit_url: 'ws://127.0.0.1:7880',
  expires_at: '2026-09-19T00:00:00Z', reconnect_grace_ms: 60000,
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('音声開始準備の状態確認', () => {
  test('長い準備でも同じ要求IDを確認し、完了まで開始を返さない', async () => {
    vi.useFakeTimers()
    let ready = false
    const requests: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
      const body = JSON.parse(options.body as string) as Record<string, unknown>
      requests.push(body)
      return new Response(JSON.stringify(ready ? token : {status: 'preparing', request_id: body.request_id}),
        {status: ready ? 200 : 202})
    }))
    let completed = false
    const request = requestLiveKitToken('miori', 'conversation').then(value => { completed = true; return value })
    await vi.advanceTimersByTimeAsync(120_000)
    expect(completed).toBe(false)
    expect(requests.length).toBeGreaterThan(100)
    expect(new Set(requests.map(body => body.request_id)).size).toBe(1)
    expect(requests.every(body => body.wait_for_ready === false)).toBe(true)
    ready = true
    await vi.advanceTimersByTimeAsync(500)
    expect(await request).toEqual(token)
  })

  test('失敗した処理を表示し、準備を自動で再実行しない', async () => {
    vi.useFakeTimers()
    const fetch = vi.fn(async (url: string) => url.endsWith('/cancel')
      ? new Response(null, {status: 204})
      : new Response(JSON.stringify({detail: {code: 'tts_voice_missing', stage: 'tts',
        message: 'PRIVATE_SECRET_SENTINEL'}}), {status: 503}))
    vi.stubGlobal('fetch', fetch)
    const failure = await requestLiveKitToken('miori', 'conversation').catch(error => error)
    expect(failure).toBeInstanceOf(VoicePreparationError)
    expect(failure.message).toContain('音声合成の準備')
    expect(failure.message).toContain('選択した音声が登録されていません')
    expect(failure.message).not.toContain('PRIVATE_SECRET_SENTINEL')
    await vi.advanceTimersByTimeAsync(120_000)
    expect(fetch.mock.calls.filter(([url]) => url.endsWith('/token'))).toHaveLength(1)
    expect(fetch.mock.calls.filter(([url]) => url.endsWith('/cancel'))).toHaveLength(1)
  })

  test.each([
    ['stt', 'stt_inference_timeout', '音声認識', 'タイムアウト'],
    ['inference', 'inference_model_not_found', '応答モデル', '見つかりません'],
    ['inference', 'inference_provider_error', '応答モデル', 'サービスでエラー'],
  ])('%s準備の%sは安全な理由を返し自動再試行しない', async (stage, code, label, reason) => {
    const fetch = vi.fn(async (url: string) => url.endsWith('/cancel')
      ? new Response(null, {status: 204})
      : new Response(JSON.stringify({detail: {stage, code, message: 'PRIVATE_SENTINEL'}}), {status: 503}))
    vi.stubGlobal('fetch', fetch)
    const failure = await requestLiveKitToken('miori', 'conversation').catch(error => error)
    expect(failure).toBeInstanceOf(VoicePreparationError)
    expect(failure.message).toContain(label)
    expect(failure.message).toContain(reason)
    expect(failure.message).not.toContain('PRIVATE_SENTINEL')
    expect(fetch.mock.calls.filter(([url]) => url.endsWith('/token'))).toHaveLength(1)
    expect(fetch.mock.calls.filter(([url]) => url.endsWith('/cancel'))).toHaveLength(1)
  })

  test('準備中の取消は状態確認を止め、同じ要求の回収を依頼する', async () => {
    vi.useFakeTimers()
    const calls: Array<{url: string; body: Record<string, unknown>}> = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit) => {
      const body = JSON.parse(options.body as string) as Record<string, unknown>
      calls.push({url, body})
      return url.endsWith('/cancel') ? new Response(null, {status: 204})
        : new Response(JSON.stringify({status: 'preparing', request_id: body.request_id}), {status: 202})
    }))
    const abort = new AbortController()
    const request = requestLiveKitToken('miori', 'conversation', undefined, undefined, abort.signal)
    const rejected = expect(request).rejects.toMatchObject({name: 'AbortError'})
    await vi.advanceTimersByTimeAsync(1)
    abort.abort()
    await rejected
    await vi.advanceTimersByTimeAsync(120_000)
    expect(calls).toHaveLength(2)
    expect(calls[1].url).toContain('/preparation/cancel')
    expect(calls[1].body.request_id).toBe(calls[0].body.request_id)
  })

  test('既存Sessionの再接続は新しい準備を開始しない', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(token), {status: 200}))
    vi.stubGlobal('fetch', fetch)
    await expect(requestLiveKitToken('miori', 'conversation', 'session')).resolves.toEqual(token)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body as string))
      .toMatchObject({session_id: 'session', wait_for_ready: true})
  })
})

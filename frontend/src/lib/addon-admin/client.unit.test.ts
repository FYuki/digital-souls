import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  deleteConnection,
  listAddons,
  ManagementError,
  SettingsDurabilityError,
  type AddonStatus,
} from './client'

const item = (id: string): AddonStatus => ({
  connection_instance_id: id,
  display_name: id,
  source_type: 'external',
  desired_enabled: true,
  availability: 'available',
  effective_state: 'available',
  error_code: null,
  last_checked_at: null,
})

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

describe('addon admin client request boundary', () => {
  test('sends the caller signal with a no-store cache to a single fetch', async () => {
    const controller = new AbortController()
    const fetchMock = stubFetch(() => new Response(JSON.stringify([item('one')])))

    const result = await listAddons(controller.signal)

    expect(result).toEqual([item('one')])
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.signal).toBe(controller.signal)
    expect(init?.cache).toBe('no-store')
  })

  test('rejects the in-flight request with the original abort reason', async () => {
    const controller = new AbortController()
    let observed: AbortSignal | undefined
    const fetchMock = stubFetch((_url, init) => {
      observed = init?.signal as AbortSignal | undefined
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          'abort', () => reject(init.signal?.reason), { once: true },
        )
      })
    })

    const request = listAddons(controller.signal)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(observed).toBe(controller.signal)
    expect(observed?.aborted).toBe(false)

    const reason = new Error('cancelled by controller')
    controller.abort(reason)

    await expect(request).rejects.toBe(reason)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  test('maps a 503 durability response to SettingsDurabilityError', async () => {
    stubFetch(() => new Response(
      JSON.stringify({ detail: 'settings_durability_uncertain', private: 'raw-secret' }),
      { status: 503 },
    ))

    await expect(
      listAddons(new AbortController().signal),
    ).rejects.toBeInstanceOf(SettingsDurabilityError)
  })

  test('maps an error detail to ManagementError and falls back for opaque bodies', async () => {
    stubFetch(() => new Response(
      JSON.stringify({ detail: 'connection_busy' }), { status: 400 },
    ))
    const detailFailure = await listAddons(new AbortController().signal)
      .catch((error: unknown) => error)
    expect(detailFailure).toBeInstanceOf(ManagementError)
    expect((detailFailure as Error).message).toBe('connection_busy')

    stubFetch(() => new Response('proxy error', { status: 500 }))
    const opaqueFailure = await listAddons(new AbortController().signal)
      .catch((error: unknown) => error)
    expect(opaqueFailure).toBeInstanceOf(ManagementError)
    expect((opaqueFailure as Error).message).toBe('management_request_failed')
  })

  test('deleteConnection resolves on 204 without reading a body', async () => {
    const response = new Response(null, { status: 204 })
    const readJson = vi.spyOn(response, 'json')
    stubFetch(() => response)

    await expect(
      deleteConnection('one', new AbortController().signal),
    ).resolves.toBeUndefined()
    expect(readJson).not.toHaveBeenCalled()
  })
})

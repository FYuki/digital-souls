import { afterEach, describe, expect, test, vi } from 'vitest'

import { requestHttp } from './http-client'

const failure = (response: Response): Error => (
  new Error(`Request failed with status ${response.status}`)
)

const jsonResponse = (body: unknown, status = 200): Response => (
  new Response(JSON.stringify(body), { status })
)

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

describe('http client request boundary', () => {
  test('passes the url and init to a single fetch and returns the parsed body', async () => {
    const init: RequestInit = { method: 'POST', headers: { 'x-test': '1' } }
    const fetchMock = stubFetch(() => jsonResponse({ accepted: true }))

    const result = await requestHttp('/api/example', init, failure, 'json')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/api/example', init)
    expect(result).toEqual({ accepted: true })
  })

  test('maps a 204 success to null and other successes to the parsed body', async () => {
    const fetchMock = stubFetch(() => new Response(null, { status: 204 }))
    await expect(
      requestHttp('/api/example', undefined, failure, 'json-or-null'),
    ).resolves.toBeNull()

    fetchMock.mockImplementation(async () => jsonResponse({ value: 1 }))
    await expect(
      requestHttp('/api/example', undefined, failure, 'json-or-null'),
    ).resolves.toEqual({ value: 1 })
  })

  test('does not read the body when the caller does not need it', async () => {
    const response = new Response(null, { status: 204 })
    const readJson = vi.spyOn(response, 'json')
    stubFetch(() => response)

    await expect(
      requestHttp('/api/example', { method: 'DELETE' }, failure, 'none'),
    ).resolves.toBeUndefined()
    expect(readJson).not.toHaveBeenCalled()
  })

  test('still reads the body in json mode when the success response is empty', async () => {
    stubFetch(() => new Response(null, { status: 204 }))

    await expect(
      requestHttp('/api/example', undefined, failure, 'json'),
    ).rejects.toThrow(SyntaxError)
  })

  test('throws the error produced by the client-owned factory', async () => {
    const produced = new Error('domain failure')
    stubFetch(() => jsonResponse({ detail: 'x' }, 409))

    await expect(
      requestHttp('/api/example', undefined, () => produced, 'json'),
    ).rejects.toBe(produced)
  })

  test('lets the error factory inspect the failed response body', async () => {
    stubFetch(() => jsonResponse({ detail: 'connection_busy' }, 400))

    await expect(
      requestHttp('/api/example', undefined, async (response) => {
        const body = await response.json() as { detail?: unknown }
        return new Error(`mapped:${String(body.detail)}`)
      }, 'json'),
    ).rejects.toThrow('mapped:connection_busy')
  })

  test('propagates a network rejection without calling the error factory', async () => {
    const network = new TypeError('network down')
    const errorFactory = vi.fn(failure)
    stubFetch(() => Promise.reject(network))

    await expect(
      requestHttp('/api/example', undefined, errorFactory, 'json'),
    ).rejects.toBe(network)
    expect(errorFactory).not.toHaveBeenCalled()
  })

  test('rejects the in-flight request with the original abort reason', async () => {
    const controller = new AbortController()
    const errorFactory = vi.fn(failure)
    const fetchMock = stubFetch((_url, init) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true })
    }))

    const request = requestHttp(
      '/api/example', { signal: controller.signal }, errorFactory, 'json',
    )
    const reason = new Error('cancelled by caller')
    controller.abort(reason)

    await expect(request).rejects.toBe(reason)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(errorFactory).not.toHaveBeenCalled()
  })
})

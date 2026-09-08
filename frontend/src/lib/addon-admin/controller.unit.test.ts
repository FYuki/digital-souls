import { get } from 'svelte/store'
import { describe, test, expect, vi, afterEach } from 'vitest'
import { createAddonController, sortedAddons, aggregateBadge } from './controller'
import { parseStatus, stateMessage, setAddonEnabled, type AddonStatus } from './client'

const row = (id = 'one', overrides: Partial<AddonStatus> = {}): AddonStatus => ({
  connection_instance_id: id, display_name: id, source_type: 'external', desired_enabled: true,
  availability: 'available', effective_state: 'available', error_code: null, last_checked_at: null, ...overrides,
})
const deferred = <T>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('連携管理', () => {
  test('希望OFFを最下部へ並べ、障害badgeから除外する', () => {
    const items = [row('z'), row('off', { desired_enabled: false, availability: 'unavailable', effective_state: 'disabled' }),
      row('err', { availability: 'unavailable', effective_state: 'unavailable' }), row('a', { availability: 'unknown', effective_state: 'unknown' })]
    expect(sortedAddons(items).map((item) => item.connection_instance_id)).toEqual(['err', 'a', 'z', 'off'])
    expect(aggregateBadge(items)).toBe('error')
    expect(aggregateBadge(items.filter((item) => item.connection_instance_id !== 'err'))).toBeNull()
    const partial = row('self', { source_type: 'self_owned', availability: 'degraded', effective_state: 'degraded', error_code: 'partial_failure' })
    expect(aggregateBadge([partial])).toBe('warning')
    expect(aggregateBadge([partial, ...items])).toBe('error')
  })
  test('外部MCPのdegradedを拒否し、余分な接続情報を取り込まない', () => {
    expect(() => parseStatus(row('one', { availability: 'degraded', effective_state: 'degraded' }))).toThrow()
    expect(parseStatus({ ...row(), endpoint: 'private', secret_ref: 'SECRET', raw_error: 'private' })).toEqual(row())
    expect(() => parseStatus(row('one', { error_code: 'raw-error' as never }))).toThrow()
    expect(stateMessage(row('one', { availability: 'unavailable', effective_state: 'unavailable', error_code: 'authentication_failed' }))).toContain('認証に失敗しました')
  })
  test('更新前に開始したpollが成功したPATCHを上書きしない', async () => {
    const stale = deferred<AddonStatus[]>()
    const gateway = { list: vi.fn().mockResolvedValueOnce([row()]).mockReturnValueOnce(stale.promise),
      setEnabled: vi.fn().mockResolvedValue(row('one', { desired_enabled: false, effective_state: 'disabled' })) }
    const controller = createAddonController(gateway)
    await controller.refresh()
    const reading = controller.refresh()
    await controller.toggle('one', false)
    stale.resolve([row()])
    await reading
    expect(get(controller).items[0].desired_enabled).toBe(false)
    controller.destroy()
  })
  test('行の多重送信を止め、失敗時は確定値と固定エラーを保持する', async () => {
    const pending = deferred<AddonStatus>()
    const gateway = { list: vi.fn().mockResolvedValue([row()]), setEnabled: vi.fn().mockReturnValue(pending.promise) }
    const controller = createAddonController(gateway)
    await controller.refresh()
    const first = controller.toggle('one', false)
    await controller.toggle('one', false)
    expect(gateway.setEnabled).toHaveBeenCalledTimes(1)
    pending.resolve(row('one', { desired_enabled: false, effective_state: 'disabled' }))
    await first
    gateway.setEnabled.mockRejectedValue(new Error('raw-secret'))
    await controller.toggle('one', true)
    expect(get(controller).items[0].desired_enabled).toBe(false)
    expect(get(controller).rowErrors.one).not.toContain('raw-secret')
    expect(get(controller).pending.size).toBe(0)
    controller.destroy()
  })
  test('poll、終了時abortと遅延responseの破棄', async () => {
    vi.useFakeTimers()
    const pending = deferred<AddonStatus[]>()
    const list = vi.fn().mockResolvedValueOnce([row()]).mockReturnValue(pending.promise)
    const controller = createAddonController({ list, setEnabled: vi.fn() })
    controller.start()
    await vi.advanceTimersByTimeAsync(5000)
    expect(list).toHaveBeenCalledTimes(2)
    const signal = list.mock.calls[1][0] as AbortSignal
    controller.destroy()
    expect(signal.aborted).toBe(true)
    pending.resolve([row('late')])
    await vi.advanceTimersByTimeAsync(15000)
    expect(list).toHaveBeenCalledTimes(2)
    expect(get(controller).items[0].connection_instance_id).toBe('one')
  })
  test('PATCHは希望booleanだけを送る', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(row()))))
    await setAddonEnabled('one', true, new AbortController().signal)
    expect(fetch).toHaveBeenCalledWith('/api/addon-admin/connections/one', expect.objectContaining({
      method: 'PATCH', body: '{"desired_enabled":true}', cache: 'no-store',
    }))
  })
})

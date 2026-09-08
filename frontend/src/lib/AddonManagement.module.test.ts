import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import AddonManagement from './AddonManagement.svelte'
import { createAddonController } from './addon-admin/controller'
import type { AddonStatus } from './addon-admin/client'

afterEach(cleanup)
const item: AddonStatus = { connection_instance_id: 'one', display_name: '資料', source_type: 'external', desired_enabled: true,
  availability: 'unavailable', effective_state: 'unavailable', error_code: 'authentication_failed', last_checked_at: null }

test('未接続でもtoggleでき、再配置後もkeyboard focusを保持する', async () => {
  const gateway = { list: vi.fn().mockResolvedValue([item]), setEnabled: vi.fn().mockResolvedValue({ ...item, desired_enabled: false, effective_state: 'disabled' }) }
  const controller = createAddonController(gateway)
  const onClose = vi.fn()
  render(AddonManagement, { controller, onClose })
  await screen.findByText('External')
  expect(screen.getByText('使用不可 — 認証に失敗しました')).toBeTruthy()
  const toggle = screen.getByRole('switch', { name: '資料を利用する' })
  toggle.focus()
  await fireEvent.click(toggle)
  await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('false'))
  expect(document.activeElement).toBe(toggle)
  await fireEvent.click(screen.getByText('チャットへ戻る'))
  expect(onClose).toHaveBeenCalledOnce()
  controller.destroy()
})

test('loading、empty、API errorを区別してraw errorを表示しない', async () => {
  let reject!: (error: Error) => void
  const list = vi.fn().mockReturnValueOnce(new Promise((_, fail) => { reject = fail })).mockResolvedValue([])
  const controller = createAddonController({ list, setEnabled: vi.fn() })
  render(AddonManagement, { controller, onClose: vi.fn() })
  expect(screen.getByText('連携を読み込んでいます')).toBeTruthy()
  reject(new Error('secret-endpoint'))
  await screen.findByRole('alert')
  expect(screen.queryByText('secret-endpoint')).toBeNull()
  await fireEvent.click(screen.getByText('状態を再取得'))
  await screen.findByText('登録済みの連携はありません')
  controller.destroy()
})

test.each([
  ['available', '使用可能'], ['unknown', '状態を確認しています'],
  ['unavailable', '使用不可 — 認証に失敗しました'], ['degraded', '一部機能に問題があります'], ['disabled', '無効'],
] as const)('状態 %s を固定文言で表示する', async (state, message) => {
  const data: AddonStatus = { ...item,
    source_type: state === 'degraded' ? 'self_owned' : 'external',
    desired_enabled: state !== 'disabled', availability: state === 'disabled' ? 'unavailable' : state,
    effective_state: state, error_code: state === 'degraded' ? 'partial_failure' : state === 'unavailable' || state === 'disabled' ? 'authentication_failed' : null,
  }
  const controller = createAddonController({ list: vi.fn().mockResolvedValue([data]), setEnabled: vi.fn() })
  render(AddonManagement, { controller, onClose: vi.fn() })
  await screen.findByText(message)
  expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe(String(data.desired_enabled))
  controller.destroy()
})

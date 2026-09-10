import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import ExternalConnectionEditor from './ExternalConnectionEditor.svelte'
import type { ConnectionDetail } from './addon-admin/client'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const detail: ConnectionDetail = {
  connection_instance_id: 'external-one', display_name: '資料接続', source_type: 'external',
  desired_enabled: false, availability: 'available', effective_state: 'disabled', error_code: null,
  last_checked_at: '2026-09-10T00:00:00Z', settings_revision: 1,
  last_success_at: '2026-09-10T00:00:00Z', last_attempt_at: '2026-09-10T00:00:00Z', last_check_error: null,
  settings: { transport: 'streamable_http', endpoint: 'https://example.com/mcp', auth: 'bearer' },
  credential_set: false, capabilities: { counts: { tools: 0, resources: 0, prompts: 0 }, tools: [] },
}
const reply = (value: unknown, status = 200) => new Response(status === 204 ? null : JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

test('Bearer登録はcredentialを専用境界で送り、保存後は入力値を消去する', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(reply(detail, 201)).mockResolvedValueOnce(reply({ ...detail, credential_set: true }))
  vi.stubGlobal('fetch', fetch)
  render(ExternalConnectionEditor, { onClose: vi.fn(), onChanged: vi.fn() })
  await fireEvent.input(screen.getByLabelText('表示名'), { target: { value: '資料接続' } })
  await fireEvent.input(screen.getByLabelText('接続先URL'), { target: { value: 'https://example.com/mcp' } })
  await fireEvent.change(screen.getByLabelText('認証'), { target: { value: 'bearer' } })
  await fireEvent.input(screen.getByLabelText('APIキー'), { target: { value: 'synthetic-private-token' } })
  await fireEvent.click(screen.getByRole('button', { name: '登録して接続確認' }))
  await screen.findByText('設定を保存しました。')
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(fetch.mock.calls[0][1].body).not.toContain('synthetic-private-token')
  expect(fetch.mock.calls[1][0]).toBe('/api/addon-admin/external-connections/external-one/credential')
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ token: 'synthetic-private-token' })
  expect((screen.getByLabelText('新しいAPIキー（更新する場合のみ）') as HTMLInputElement).value).toBe('')
  expect(screen.getByText('利用意思: OFF / 無効')).toBeTruthy()
  expect(screen.getByText('取得したToolは0件です。')).toBeTruthy()
})

test('stdioのcommandと引数を個別fieldで保存する', async () => {
  const data = { ...detail, settings: { transport: 'stdio', command: 'python', args: ['server.py', '--port', '9100'] } }
  const fetch = vi.fn().mockResolvedValue(reply(data, 201))
  vi.stubGlobal('fetch', fetch)
  render(ExternalConnectionEditor, { onClose: vi.fn(), onChanged: vi.fn() })
  await fireEvent.input(screen.getByLabelText('表示名'), { target: { value: 'ローカル' } })
  await fireEvent.change(screen.getByLabelText('接続方式'), { target: { value: 'stdio' } })
  await fireEvent.input(screen.getByLabelText('実行ファイル'), { target: { value: 'python' } })
  for (const [index, value] of ['server.py', '--port', '9100'].entries()) {
    await fireEvent.click(screen.getByRole('button', { name: '引数を追加' }))
    await fireEvent.input(screen.getByLabelText(`引数 ${index + 1}`), { target: { value } })
  }
  await fireEvent.click(screen.getByRole('button', { name: '登録して接続確認' }))
  await screen.findByText('設定を保存しました。')
  expect(JSON.parse(fetch.mock.calls[0][1].body).settings).toEqual(data.settings)
})

test('削除確認にcredentialの同時削除を明示し、失敗時は画面を維持する', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(reply(detail)).mockResolvedValueOnce(reply({ detail: 'connection_busy' }, 409)).mockResolvedValueOnce(reply(null, 204))
  vi.stubGlobal('fetch', fetch)
  const onClose = vi.fn()
  render(ExternalConnectionEditor, { connectionId: 'external-one', onClose, onChanged: vi.fn() })
  await screen.findByText('取得したToolは0件です。')
  await fireEvent.click(screen.getByRole('button', { name: '接続を削除…' }))
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(screen.getByText('この接続と保存済みcredentialを削除します。外部サービス側のAPIキーは失効しません。')).toBeTruthy()
  await fireEvent.click(screen.getByRole('button', { name: '接続とcredentialを削除' }))
  await screen.findByText('実行完了後に再試行してください。')
  expect(onClose).not.toHaveBeenCalled()
  await fireEvent.click(screen.getByRole('button', { name: '接続とcredentialを削除' }))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
})

test('Tool説明をテキスト表示し、個別無効化操作を追加しない', async () => {
  const description = '<img src=x onerror="alert(1)">'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ ...detail, capabilities: {
    counts: { tools: 1, resources: 0, prompts: 0 }, tools: [{ name: 'tool', description, status: 'active' }],
  } })))
  render(ExternalConnectionEditor, { connectionId: 'external-one', onClose: vi.fn(), onChanged: vi.fn() })
  await screen.findByText(description)
  expect(document.querySelector('img')).toBeNull()
  expect(screen.queryByRole('switch')).toBeNull()
  expect(screen.queryByRole('checkbox')).toBeNull()
})

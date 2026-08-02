import { render, screen } from '@testing-library/svelte'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import HardDeleteDialog from './HardDeleteDialog.svelte'

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'

describe('HardDeleteDialog', () => {
  beforeEach(() => {
    render(HardDeleteDialog, {
      conversationId: CONVERSATION_ID,
      disabled: false,
      onConfirm: vi.fn(),
      onCancel: vi.fn(),
    })
  })

  test('選択したconversationを削除対象として表示する', () => {
    expect(screen.getByRole('dialog').textContent).toContain(`対象スレッド: ${CONVERSATION_ID}`)
  })

  test('短期会話履歴が復元不能になることを表示する', () => {
    expect(screen.getByRole('dialog').textContent).toContain(
      '短期会話履歴は失われ、削除後は復元できません。',
    )
  })

  test('RAG長期記憶は削除対象外であることを表示する', () => {
    expect(screen.getByRole('dialog').textContent).toContain('RAG長期記憶は削除されません。')
  })

  test('backup等の既存複製は消去保証対象外であることを表示する', () => {
    const dialog = screen.getByRole('dialog')

    expect(dialog.textContent).toContain('backup')
    expect(dialog.textContent).toContain('snapshot')
    expect(dialog.textContent).toContain('ファイルシステム上の複製')
    expect(dialog.textContent).toContain('消去を保証しません')
  })
})

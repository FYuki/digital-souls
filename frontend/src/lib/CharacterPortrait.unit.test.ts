import { fireEvent, render, screen } from '@testing-library/svelte'
import { describe, expect, test } from 'vitest'

import CharacterPortrait from './CharacterPortrait.svelte'

const available = {
  character_id: 'miori',
  display_name: '光織',
  standing_image: {
    status: 'available' as const,
    url: '/api/characters/miori/assets/standing/default.png',
  },
}

describe('CharacterPortrait', () => {
  test('catalogの安全なURLから立ち絵を表示する', () => {
    render(CharacterPortrait, { character: available })

    expect(screen.getByRole<HTMLImageElement>('img', { name: '光織の立ち絵' }).src)
      .toContain('/api/characters/miori/assets/standing/default.png')
  })

  test('未配置の場合は共通placeholderを表示する', () => {
    render(CharacterPortrait, {
      character: {
        ...available,
        standing_image: { status: 'missing' as const, url: null },
      },
    })

    expect(screen.getByLabelText('立ち絵未設定')).toBeTruthy()
    expect(screen.getByText('立ち絵を準備中')).toBeTruthy()
  })

  test('画像取得失敗時は会話を遮らずplaceholderへ切り替える', async () => {
    render(CharacterPortrait, { character: available })

    await fireEvent.error(screen.getByRole('img', { name: '光織の立ち絵' }))

    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByLabelText('立ち絵未設定')).toBeTruthy()
  })
})

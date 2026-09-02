import { fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, describe, expect, test, vi } from 'vitest'

import RenameThreadDialog from './RenameThreadDialog.svelte'

const mountedButtons: HTMLButtonElement[] = []

afterEach(() => {
  for (const button of mountedButtons.splice(0)) button.remove()
})

describe('RenameThreadDialog', () => {
  test('現在名を選択した入力へ初期表示しtrim後の名前を送る', async () => {
    const onConfirm = vi.fn()
    render(RenameThreadDialog, {
      currentTitle: ' 以前の名前 ',
      disabled: false,
      onConfirm,
      onCancel: vi.fn(),
    })
    const input = screen.getByRole<HTMLInputElement>('textbox', { name: 'スレッド名' })
    await waitFor(() => expect(document.activeElement).toBe(input))
    await fireEvent.input(input, { target: { value: '  新しい名前  ' } })

    await fireEvent.click(screen.getByRole('button', { name: '保存' }))

    expect(onConfirm).toHaveBeenCalledWith('新しい名前')
  })

  test('Escapeとダイアログ外clickでキャンセルできる', async () => {
    const onCancel = vi.fn()
    const rendered = render(RenameThreadDialog, {
      currentTitle: '名前', disabled: false, onConfirm: vi.fn(), onCancel,
    })

    await fireEvent.keyDown(window, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    await fireEvent.pointerDown(rendered.container.querySelector('.backdrop') as Element)
    expect(onCancel).toHaveBeenCalledTimes(2)
  })

  test('Tab移動をダイアログ内で循環させる', async () => {
    render(RenameThreadDialog, {
      currentTitle: '名前', disabled: false, onConfirm: vi.fn(), onCancel: vi.fn(),
    })
    const input = screen.getByRole('textbox', { name: 'スレッド名' })
    const save = screen.getByRole('button', { name: '保存' })
    await waitFor(() => expect(document.activeElement).toBe(input))

    await fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(save)
    await fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(input)
  })

  test('閉じた後は開始ボタンへfocusを戻す', async () => {
    const trigger = document.createElement('button')
    document.body.append(trigger)
    mountedButtons.push(trigger)
    trigger.focus()
    const rendered = render(RenameThreadDialog, {
      currentTitle: '名前', disabled: false, returnFocus: trigger,
      onConfirm: vi.fn(), onCancel: vi.fn(),
    })
    await waitFor(() => expect(document.activeElement).not.toBe(trigger))

    rendered.unmount()

    expect(document.activeElement).toBe(trigger)
  })
})

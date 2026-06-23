import { fireEvent, render, screen } from '@testing-library/svelte'
import { describe, expect, test, vi } from 'vitest'

import InputBar from './InputBar.svelte'

describe('InputBar', () => {
  test('should submit the current text when the send button is clicked', async () => {
    const handleSend = vi.fn()
    render(InputBar, { props: { onSend: handleSend } })
    const input = screen.getByRole('textbox')
    await fireEvent.input(input, { target: { value: '今日の空は？' } })

    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(handleSend).toHaveBeenCalledTimes(1)
    expect(handleSend).toHaveBeenCalledWith('今日の空は？')
  })

  test('should submit the current text when Enter is pressed in the input', async () => {
    const handleSend = vi.fn()
    render(InputBar, { props: { onSend: handleSend } })
    const input = screen.getByRole('textbox')
    await fireEvent.input(input, { target: { value: '明日の予定は？' } })

    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })

    expect(handleSend).toHaveBeenCalledTimes(1)
    expect(handleSend).toHaveBeenCalledWith('明日の予定は？')
  })

  test('should not submit a whitespace-only message', async () => {
    const handleSend = vi.fn()
    render(InputBar, { props: { onSend: handleSend } })
    const input = screen.getByRole('textbox')
    await fireEvent.input(input, { target: { value: '   ' } })

    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })

    expect(handleSend).not.toHaveBeenCalled()
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '送信' }).disabled).toBe(true)
  })

  test('should not submit while Japanese IME composition is active', async () => {
    const handleSend = vi.fn()
    render(InputBar, { props: { onSend: handleSend } })
    const input = screen.getByRole('textbox')
    await fireEvent.input(input, { target: { value: 'み' } })

    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', isComposing: true })

    expect(handleSend).not.toHaveBeenCalled()
  })
})

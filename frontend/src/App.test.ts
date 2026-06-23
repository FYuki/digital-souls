import { fireEvent, render, screen } from '@testing-library/svelte'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App.svelte'
import { sendMessage } from './lib/api'

vi.mock('./lib/api', () => ({
  sendMessage: vi.fn(),
}))

describe('App chat flow', () => {
  beforeEach(() => {
    vi.mocked(sendMessage).mockReset()
  })

  test('should send user text through the API and render the character response', async () => {
    vi.mocked(sendMessage).mockResolvedValue('夕焼けがきれいですね。')
    const { container } = render(App)

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'こんにちは' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(sendMessage).toHaveBeenCalledWith('こんにちは')
    expect(await screen.findByText('こんにちは')).toBeTruthy()
    expect(await screen.findByText('夕焼けがきれいですね。')).toBeTruthy()
    if (container.textContent === null) {
      throw new Error('Chat text content is required')
    }
    expect(container.textContent.indexOf('こんにちは')).toBeLessThan(
      container.textContent.indexOf('夕焼けがきれいですね。'),
    )
  })

  test('should render an error message when the API call fails', async () => {
    vi.mocked(sendMessage).mockRejectedValue(new Error('LLM request failed'))
    render(App)

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: '応答して' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('応答して')).toBeTruthy()
    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
  })

  test('should keep the input disabled while waiting for the character response', async () => {
    let resolveResponse: (value: string) => void
    const pendingResponse = new Promise<string>((resolve) => {
      resolveResponse = resolve
    })
    vi.mocked(sendMessage).mockReturnValue(pendingResponse)
    render(App)

    const input = screen.getByRole<HTMLInputElement>('textbox')
    await fireEvent.input(input, { target: { value: '少し待って' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(input.disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '送信' }).disabled).toBe(true)

    resolveResponse!('待っていました。')

    expect(await screen.findByText('待っていました。')).toBeTruthy()
    expect(input.disabled).toBe(false)
  })
})

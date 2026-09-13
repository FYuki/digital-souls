import {fireEvent, render, screen, waitFor} from '@testing-library/svelte'
import {expect, test, vi} from 'vitest'
import InputBar from './lib/InputBar.svelte'
import type {TextSubmission} from './livekit/text-input'

const submission = (status: TextSubmission['status']): TextSubmission => ({
  inputId: 'input-a', sessionId: 'session-a', context: {characterId: 'miori', conversationId: 'a'},
  text: '保持する本文', status, responseId: null, errorCode: null,
})

test('失敗時に本文とfocusを保持し、IME変換中Enterは送信しない', async () => {
  const onSend = vi.fn(async () => 'failed' as const)
  render(InputBar, {onSend})
  const input = screen.getByRole<HTMLInputElement>('textbox', {name: 'メッセージ'})
  input.focus()
  await fireEvent.input(input, {target: {value: '保持する本文'}})
  await fireEvent.keyDown(input, {key: 'Enter', isComposing: true})
  expect(onSend).not.toHaveBeenCalled()
  await fireEvent.click(screen.getByRole('button', {name: '送信'}))
  await waitFor(() => expect(onSend).toHaveBeenCalledOnce())
  expect(input.value).toBe('保持する本文')
  expect(document.activeElement).toBe(input)
})

test('受理結果を待ってから本文とfocusを解除する', async () => {
  const view = render(InputBar, {onSend: async () => ({inputId: 'input-a'})})
  const input = screen.getByRole<HTMLInputElement>('textbox', {name: 'メッセージ'})
  input.focus()
  await fireEvent.input(input, {target: {value: '保持する本文'}})
  await fireEvent.keyDown(input, {key: 'Enter'})
  await view.rerender({submission: submission('confirming')})
  expect(input.value).toBe('保持する本文')
  expect(input.readOnly).toBe(true)
  expect(screen.getByText('送信確認中')).toBeTruthy()
  await view.rerender({submission: submission('accepted')})
  await waitFor(() => expect(input.value).toBe(''))
  expect(document.activeElement).not.toBe(input)
})

test('スレッド切り替えで下書きを混ぜず、別スレッドの遅着結果でfocusを奪わない', async () => {
  let release: (value: 'failed') => void = () => undefined
  const view = render(InputBar, {threadKey: 'a', onSend: () => new Promise<'failed'>(resolve => {release = resolve})})
  const input = screen.getByRole<HTMLInputElement>('textbox', {name: 'メッセージ'})
  await fireEvent.input(input, {target: {value: 'Aの下書き'}})
  await fireEvent.click(screen.getByRole('button', {name: '送信'}))
  await view.rerender({threadKey: 'b'})
  expect(input.value).toBe('')
  await fireEvent.input(input, {target: {value: 'Bの下書き'}})
  release('failed')
  await Promise.resolve()
  expect(input.value).toBe('Bの下書き')
  await view.rerender({threadKey: 'a'})
  expect(input.value).toBe('Aの下書き')
})

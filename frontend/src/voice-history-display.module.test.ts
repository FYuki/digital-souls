import {act, render} from '@testing-library/svelte'
import {afterEach, expect, test} from 'vitest'
import ChatWindow from './lib/ChatWindow.svelte'
import type {SettledVoiceTurnDisplay} from './lib/voice-turn-display'
import {installStaleTextProbe} from '../playwright/stale-text-probe'

const live = {responseId: 'response', historyTurnId: 'saved-turn', userContent: '質問', assistantContent: '第一文。第二文。'}
const settled: SettledVoiceTurnDisplay = {...live, terminal: 'cancelled'}
const history = {kind: 'content' as const, turn_id: 'saved-turn', user_content: '質問', assistant_content: '第一文。'}
function receive(type: 'response_started' | 'response_cancelled') {
  window.__voiceStaleTextProbe!.receive({type, sessionId: 'session', responseId: 'response', historyTurnId: 'saved-turn',
    generation: 1, atMs: performance.now(), duplicate: false, textCharacters: 0, textSequence: null})
}
afterEach(() => {window.__voiceStaleTextProbe?.close(); delete window.__voiceStaleTextProbe})

test('cancelから非同期履歴反映まで同じ本文DOMを保持し、既表示prefixを再提示しない', async () => {
  installStaleTextProbe(); receive('response_started')
  const view = render(ChatWindow, {turns: [], liveVoiceTurn: live})
  const original = view.getByText(live.assistantContent)
  receive('response_cancelled')
  await view.rerender({turns: [], liveVoiceTurn: null, settledVoiceTurns: [settled]})
  expect(view.getByText(live.assistantContent)).toBe(original)
  expect(original.isConnected).toBe(true)
  await view.rerender({turns: [history], liveVoiceTurn: null, settledVoiceTurns: []})
  expect(view.getByText(history.assistant_content)).toBe(original)
  const row = window.__voiceStaleTextProbe!.close().rows[0]
  expect(row.domAfterCancelAddedCharacters).toBe(0)
  expect(row.history).toMatchObject({domObserved: true, domAddedCharacters: 0, missingReason: null,
    retainedLiveDomTransitions: 1, retainedLiveDomCharacters: 4})
})

test('履歴反映前の次応答でも旧本文DOMを保持し、履歴更新による新規文字は計測する', async () => {
  installStaleTextProbe(); receive('response_started')
  const view = render(ChatWindow, {turns: [], liveVoiceTurn: live})
  const original = view.getByText(live.assistantContent); receive('response_cancelled')
  const next = {...live, responseId: 'next', historyTurnId: 'next-turn', userContent: '次の質問', assistantContent: '次の回答'}
  await view.rerender({turns: [], settledVoiceTurns: [settled], liveVoiceTurn: next})
  expect(view.getByText(live.assistantContent)).toBe(original)
  await view.rerender({turns: [{...history, assistant_content: '第一文。更新'}], settledVoiceTurns: [], liveVoiceTurn: next})
  expect(view.getByText('第一文。更新')).toBe(original)
  expect(view.getByText('次の回答')).toBeTruthy()
  expect(window.__voiceStaleTextProbe!.close().rows[0].history).toMatchObject({domAddedCharacters: 2, retainedLiveDomCharacters: 4})
})

test('同じnodeでも実際に除去して再挿入した場合は表示済み文字を継承しない', async () => {
  installStaleTextProbe(); receive('response_started')
  const view = render(ChatWindow, {turns: [], liveVoiceTurn: live})
  const original = view.getByText(live.assistantContent); receive('response_cancelled')
  await act(() => {
    original.remove()
    original.removeAttribute('data-live-response-text')
    original.setAttribute('data-history-turn-text', 'saved-turn')
    view.container.append(original)
  })
  expect(window.__voiceStaleTextProbe!.close().rows[0].history).toMatchObject({domAddedCharacters: 8, retainedLiveDomTransitions: 0})
})

test('保存禁止の履歴は未保存の本文を表示せず、失敗表示の既存動作も保つ', async () => {
  const view = render(ChatWindow, {turns: [], liveVoiceTurn: live,
    failedVoiceTurns: [{responseId: 'failed', userContent: '失敗した質問', assistantContent: ''}]})
  expect(view.getByText('応答を完了できませんでした。')).toBeTruthy()
  await view.rerender({turns: [{kind: 'privacy_skipped', turn_id: 'saved-turn', reason_code: 'sensitive', sanitizer_version: '1', policy_version: '1'}],
    liveVoiceTurn: live, settledVoiceTurns: [settled]})
  expect(view.queryByText(live.assistantContent)).toBeNull()
  expect(view.getByText('保存されなかったターン')).toBeTruthy()
})

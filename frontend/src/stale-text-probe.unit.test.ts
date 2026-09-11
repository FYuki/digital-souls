import {afterEach, expect, test, vi} from 'vitest'
import {installStaleTextProbe} from '../playwright/stale-text-probe'
import type {CoreDeliveryObservation} from './livekit/core-delivery-observation'

const event = (type: CoreDeliveryObservation['type'], extra: Partial<CoreDeliveryObservation> = {}): CoreDeliveryObservation => ({
  type, sessionId: 'session', responseId: 'response', generation: 1, atMs: performance.now(),
  duplicate: false, textCharacters: type === 'response_delta' ? 2 : 0, textSequence: type === 'response_delta' ? 1 : null, ...extra,
})
const start = () => {
  installStaleTextProbe()
  const probe = window.__voiceStaleTextProbe!
  probe.receive(event('response_started'))
  const node = document.createElement('p'); node.dataset.liveResponseText = 'response'; document.body.append(node)
  return {probe, node}
}
afterEach(() => {window.__voiceStaleTextProbe?.close(); delete window.__voiceStaleTextProbe; document.body.replaceChildren()})

test('重複を含む旧delta受信と実際のDOM追加を別々に数え、本文を出力しない', async () => {
  const {probe, node} = start()
  probe.receive(event('response_delta')); node.textContent = '秘密'
  // MutationObserver callbackが未実行でも、cancel時に既に存在するDOMを先に確定する。
  probe.receive(event('response_cancelled'))
  probe.receive(event('response_delta', {duplicate: true}))
  await Promise.resolve()
  expect(probe.snapshot().rows[0]).toMatchObject({receivedEvents: 2, duplicateEvents: 1,
    receivedAfterCancelEvents: 1, receivedAfterCancelCharacters: 2, domObserved: true,
    domAddedCharacters: 2, domAfterCancelChanges: 0, domAfterCancelAddedCharacters: 0})
  node.textContent = '秘密追加'
  await Promise.resolve()
  const snapshot = probe.close()
  expect(snapshot).toMatchObject({closed: true, overflow: false, characterUnit: 'utf16_code_units'})
  expect(snapshot.rows[0]).toMatchObject({domAfterCancelChanges: 1, domAfterCancelAddedCharacters: 2})
  expect(JSON.stringify(snapshot)).not.toMatch(/秘密|追加|previousText/)
})

test('cancel後の同じ長さの置換と除去後の再提示も検出する', async () => {
  const {probe, node} = start(); node.textContent = '前文'; probe.receive(event('response_cancelled'))
  node.textContent = '後文'; await Promise.resolve()
  expect(probe.snapshot().rows[0].domAfterCancelAddedCharacters).toBe(2)
  node.remove(); await Promise.resolve()
  expect(probe.snapshot().rows[0].domAfterCancelAddedCharacters).toBe(2)
  document.body.append(node); await Promise.resolve()
  expect(probe.snapshot().rows[0].domAfterCancelAddedCharacters).toBe(4)
})

test('次応答・履歴のDOM変化を旧応答へ混ぜず、観測範囲を明示する', async () => {
  const {probe, node} = start(); node.textContent = '旧文'; probe.receive(event('response_cancelled'))
  node.remove(); probe.receive(event('response_started', {responseId: 'next'}))
  const next = document.createElement('p'); next.dataset.liveResponseText = 'next'; next.textContent = '次の文'
  document.body.append(next)
  const history = document.createElement('p'); history.textContent = '履歴'; document.body.append(history)
  await Promise.resolve()
  expect(probe.snapshot()).toMatchObject({scope: 'live_and_history_response_dom', rows: [
    {responseId: 'response', domAfterCancelAddedCharacters: 0}, {responseId: 'next', domAddedCharacters: 3},
  ]})
})

test('重複DOM・本文上限・応答数上限を欠測として残す', () => {
  const {probe, node} = start()
  const other = node.cloneNode() as HTMLElement; document.body.append(other)
  expect(probe.snapshot().rows[0].missingReason).toBe('dom_response_duplicated')
  other.remove(); node.textContent = 'x'.repeat(1_000_001)
  expect(probe.snapshot().rows[0].missingReason).toBe('text_observation_overflow')
  for (let i = 0; i < 128; i++) probe.receive(event('response_started', {responseId: `response-${i}`}))
  expect(probe.snapshot().overflow).toBe(true); expect(probe.snapshot().rows).toHaveLength(128)
})

test('終了後は観測を更新せず、後着cancelでも最初の境界を保つ', async () => {
  const {probe, node} = start()
  probe.receive(event('response_cancelled', {atMs: 10}))
  probe.receive(event('response_cancelled', {atMs: 20, duplicate: true}))
  expect(probe.snapshot().rows[0].cancelledAtMs).toBe(10)
  const final = probe.close(); node.textContent = '観測終了後'; probe.receive(event('response_delta'))
  await Promise.resolve()
  expect(probe.snapshot().rows).toEqual(final.rows)
})


test('DOM変更は前回観測からcallbackまでの区間を保持し、過去の境界を判定できる', async () => {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(1000)
  try {
    const {probe, node} = start()
    clock.mockReturnValue(1010); probe.receive(event('response_delta'))
    clock.mockReturnValue(1011); node.textContent = '本文'
    clock.mockReturnValue(1015); await Promise.resolve()
    clock.mockReturnValue(1030); probe.receive(event('response_cancelled'))
    const row = probe.snapshot().rows[0]
    expect(row.changes).toEqual([
      {kind: 'received', lowerMs: 1009.8, upperMs: 1010.2, characters: 2, duplicate: false, textSequence: 1},
      {kind: 'dom_added', lowerMs: 1009.8, upperMs: 1015.2, characters: 2},
    ])
    expect(row.domAfterCancelAddedCharacters).toBe(0)
    expect(row.observedFromMs).toBe(1000); expect(row.observedThroughMs).toBe(1030)
    row.changes[0] = {...row.changes[0], characters: 999}
    expect(probe.snapshot().rows[0].changes[0].characters).toBe(2)
    probe.close()
  } finally {clock.mockRestore()}
})


test('保存turn IDで履歴を相関し、cancel後の同じ文章の再提示も独立に数える', async () => {
  const {probe, node} = start()
  probe.receive(event('response_started', {historyTurnId: 'saved-turn', duplicate: true}))
  node.textContent = '同文🙂'; probe.receive(event('response_cancelled')); node.remove()
  const unrelated = document.createElement('p')
  unrelated.dataset.historyTurnText = 'another-turn'; unrelated.textContent = '無関係'; document.body.append(unrelated)
  await Promise.resolve()
  expect(probe.snapshot().rows[0].history).toMatchObject({domObserved: false, domAddedCharacters: 0})
  const history = document.createElement('p')
  history.dataset.historyTurnText = 'saved-turn'; history.textContent = '同文🙂'; document.body.append(history)
  await Promise.resolve()
  const snapshot = probe.snapshot()
  expect(snapshot.rows[0]).toMatchObject({domAfterCancelAddedCharacters: 0,
    history: {turnId: 'saved-turn', domObserved: true, domAddedCharacters: 4,
      domAfterCancelAddedCharacters: 4, missingReason: null}})
  snapshot.rows[0].history!.changes[0] = {...snapshot.rows[0].history!.changes[0], characters: 999}
  expect(probe.snapshot().rows[0].history!.changes[0].characters).toBe(4)
  history.remove(); await Promise.resolve(); document.body.append(history); await Promise.resolve()
  expect(probe.close().rows[0].history!.domAfterCancelAddedCharacters).toBe(8)
  expect(JSON.stringify(probe.snapshot())).not.toMatch(/同文|無関係|previousHistoryText/)
})

test('履歴mappingの欠測・変更・複数応答への再利用を0件の証明へしない', () => {
  const {probe} = start()
  expect(probe.snapshot().rows[0].history).toBeUndefined()
  probe.receive(event('response_started', {historyTurnId: 'saved-turn', duplicate: true}))
  probe.receive(event('response_started', {historyTurnId: 'other-turn', duplicate: true}))
  expect(probe.snapshot().rows[0].history!.missingReason).toBe('history_mapping_conflict')
  probe.receive(event('response_started', {responseId: 'next', historyTurnId: 'saved-turn'}))
  expect(probe.snapshot().rows[1].history!.missingReason).toBe('history_mapping_conflict')
})

test('履歴の重複DOMを欠測にし、ライブ表示の観測と分離する', () => {
  const {probe, node} = start(); node.textContent = 'ライブ'
  probe.receive(event('response_started', {historyTurnId: 'saved-turn', duplicate: true}))
  for (let i = 0; i < 2; i++) {
    const history = document.createElement('p'); history.dataset.historyTurnText = 'saved-turn'; document.body.append(history)
  }
  expect(probe.close().rows[0]).toMatchObject({missingReason: null, domAddedCharacters: 3,
    history: {missingReason: 'dom_response_duplicated'}})
})

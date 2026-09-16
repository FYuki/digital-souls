import {afterEach, beforeEach, describe, expect, test, vi} from 'vitest'
import {installInterruptionProbe} from '../playwright/interruption-probe'

afterEach(() => vi.restoreAllMocks())

beforeEach(() => {
  window.__voiceChatE2E = {
    cycles: [], frameOrder: [], liveKitOrder: [], coreEventDiagnostics: [],
    micStates: [], interruptions: [],
  }
  installInterruptionProbe()
  vi.spyOn(performance, 'now').mockReturnValue(200)
})

describe('BE判断へ移した割り込み観測', () => {
  test.each([
    ['stop', 'decision', 'cancel'],
    ['stop', 'cancel', 'decision'],
    ['decision', 'stop', 'cancel'],
    ['decision', 'cancel', 'stop'],
    ['cancel', 'decision', 'stop'],
    ['cancel', 'stop', 'decision'],
  ])('callback順序 %s/%s/%s でも実停止と取消を対応付ける', (...order) => {
    const probe = window.__voiceInterruptionProbe!
    const callbacks: Record<string, () => void> = {
      stop: () => probe.observeRoom({activeResponseId: 'response', localPlaybackStoppedAtMs: 190}),
      cancel: () => probe.observeRoom({activeResponseId: 'response', cancelConfirmedAtMs: 210}),
      decision: () => probe.receiveCoreEvent({
        type: 'turn_decision', decision: 'take_turn', response_id: 'response', utterance_id: 'utterance',
      }),
    }
    for (const name of order) callbacks[name]()
    expect(window.__voiceChatE2E.interruptions).toEqual([{
      responseId: 'response', utteranceId: 'utterance', speechStartedAtMs: null,
      backendDecisionReceivedAtMs: 200, localPlaybackStoppedAtMs: 190,
      cancelConfirmedAtMs: 210, ambiguousDecision: false, duplicateStop: false,
    }])
  })

  test('取消だけから実停止を捏造せず、別responseの取消も混ぜない', () => {
    const probe = window.__voiceInterruptionProbe!
    probe.observeRoom({activeResponseId: 'first', cancelConfirmedAtMs: 10})
    expect(window.__voiceChatE2E.interruptions).toEqual([])
    probe.observeRoom({activeResponseId: 'second', localPlaybackStoppedAtMs: 30})
    expect(window.__voiceChatE2E.interruptions[0]).toMatchObject({
      responseId: 'second', cancelConfirmedAtMs: null, speechStartedAtMs: null,
      backendDecisionReceivedAtMs: null,
    })
  })

  test('異なる停止時刻・utteranceの重複を成功値へ上書きしない', () => {
    const probe = window.__voiceInterruptionProbe!
    probe.observeRoom({activeResponseId: 'response', localPlaybackStoppedAtMs: 10, speechStartedAtMs: 1})
    probe.observeRoom({activeResponseId: 'response', localPlaybackStoppedAtMs: 11})
    for (const utterance of ['first', 'second']) probe.receiveCoreEvent({
      type: 'turn_decision', decision: 'take_turn', response_id: 'response', utterance_id: utterance,
    })
    expect(window.__voiceChatE2E.interruptions[0]).toMatchObject({
      localPlaybackStoppedAtMs: 10, speechStartedAtMs: 1, duplicateStop: true,
      utteranceId: 'first', ambiguousDecision: true,
    })
  })

  test('収集上限を超えた観測を欠測として明示する', () => {
    const probe = window.__voiceInterruptionProbe!
    for (let i = 0; i < 1025; ++i) probe.observeRoom({
      activeResponseId: String(i), localPlaybackStoppedAtMs: i,
    })
    expect(window.__voiceChatE2E.interruptions).toHaveLength(1024)
    expect(window.__voiceChatE2E.interruptionsOverflow).toBe(true)
  })
})

import {expect, test} from 'vitest'
import {hasOneSecondOfObservedSilence, type FailureOutputRow} from '../playwright/tts-failure-output-probe'

const observed = (): FailureOutputRow => ({
  sampleRate: 48000, outputCount: 500, nonzeroBefore: 12000, nonzeroAfter: 0,
  failureFrameUpper: 1000, zeroStart: 1024, zeroEnd: 50000, outputClockFrame: 50000, missing: null,
})

test('旧音声の無音を実出力時計が通過した場合だけ確認済みにする', () => {
  expect(hasOneSecondOfObservedSilence([observed()])).toBe(true)
  expect(hasOneSecondOfObservedSilence([])).toBe(false)
})

test.each([
  {missing: 'audit_output_gap'}, {zeroEnd: null}, {outputClockFrame: null},
  {outputClockFrame: Number.POSITIVE_INFINITY}, {outputClockFrame: 49000},
  {nonzeroAfter: 1}, {zeroStart: 5000}, {sampleRate: 44100},
])('観測欠落・出力待ち・古い音声を無音の成功にしない: %o', changed => {
  expect(hasOneSecondOfObservedSilence([{...observed(), ...changed}])).toBe(false)
})

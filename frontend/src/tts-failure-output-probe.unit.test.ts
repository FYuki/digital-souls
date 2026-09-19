import {expect, test} from 'vitest'
import {hasConfirmedStoppedOutput, type FailureOutputRow} from '../playwright/tts-failure-output-probe'

const observed = (): FailureOutputRow => ({
  sampleRate: 48000, outputCount: 500, nonzeroBefore: 12000, nonzeroAfter: 0,
  failureFrameUpper: 1000, zeroStart: 1024, zeroEnd: 1280, outputClockFrame: 1280, finishedFrame: 1280, disconnected: true, missing: null,
})

test('旧音声の無音を実出力時計が通過した場合だけ確認済みにする', () => {
  expect(hasConfirmedStoppedOutput([observed()])).toBe(true)
  expect(hasConfirmedStoppedOutput([])).toBe(false)
})

test.each([
  {nonzeroBefore: 0}, {outputCount: 0}, {missing: 'audit_output_gap'}, {zeroEnd: null}, {outputClockFrame: null},
  {outputClockFrame: Number.POSITIVE_INFINITY}, {outputClockFrame: 1279},
  {nonzeroAfter: 1}, {zeroStart: 5000}, {zeroStart: 999}, {zeroEnd: 1100},
  {finishedFrame: null}, {finishedFrame: 1152}, {disconnected: false}, {sampleRate: 44100},
])('観測欠落・出力待ち・古い音声を無音の成功にしない: %o', changed => {
  expect(hasConfirmedStoppedOutput([{...observed(), ...changed}])).toBe(false)
})

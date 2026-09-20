import {expect, test, vi} from 'vitest'
import type {Page} from '@playwright/test'
import {hasConfirmedStoppedOutput, installFailureOutputProbe, type FailureOutputRow} from '../playwright/tts-failure-output-probe'

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


test('観測上限を超えても音声nodeを返し、以降は成功判定できない欠測を残す', async () => {
  class Context {
    currentTime = 1
    sampleRate = 48000
    getOutputTimestamp() {return {contextTime: 1, performanceTime: 1}}
  }
  class Node {
    port = {addEventListener: vi.fn()}
    disconnect = vi.fn()
    constructor(public context: unknown, public name: unknown) {}
  }
  vi.stubGlobal('AudioContext', Context)
  vi.stubGlobal('AudioWorkletNode', Node)
  const page = {addInitScript: async (install: () => void) => install()} as unknown as Page
  try {
    await installFailureOutputProbe(page)
    const context = new AudioContext()
    const nodes = Array.from({length: 8}, () => new AudioWorkletNode(context, 'post-gain-audit'))
    expect(window.__ttsFailureOutput!.missing()).toBeNull()
    const unrelated = new AudioWorkletNode(context, 'other')
    expect(unrelated).toBeInstanceOf(Node)
    expect(window.__ttsFailureOutput!.missing()).toBeNull()
    const overflow = new AudioWorkletNode(context, 'post-gain-audit')
    expect(overflow).toBeInstanceOf(Node)
    expect(overflow.port.addEventListener).not.toHaveBeenCalled()
    expect(window.__ttsFailureOutput!.missing()).toBe('probe_capacity_exceeded')
    expect(window.__ttsFailureOutput!.snapshot()).toHaveLength(8)
    expect(window.__ttsFailureOutput!.snapshot().every(row => row.missing === 'probe_capacity_exceeded')).toBe(true)
    const completeRows = nodes.map(() => ({...observed(), missing: window.__ttsFailureOutput!.missing()}))
    expect(hasConfirmedStoppedOutput(completeRows)).toBe(false)
    window.__ttsFailureOutput!.markFailure()
    expect(window.__ttsFailureOutput!.missing()).toBe('probe_capacity_exceeded')
  } finally {
    delete window.__ttsFailureOutput
    vi.unstubAllGlobals()
  }
})

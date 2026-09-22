import {afterEach, beforeEach, expect, test, vi} from 'vitest'
import {installPreparationProbe} from '../playwright/preparation-probe'

let button: HTMLButtonElement
beforeEach(() => {
  document.body.innerHTML = '<button aria-label="マイクをオンにする" aria-pressed="false">開始</button>'
  button = document.querySelector('button')!
  installPreparationProbe()
})
afterEach(() => {
  window.__voicePreparationProbe?.close()
  delete window.__voicePreparationProbe
  vi.restoreAllMocks()
})

test('クリック前と準備中は完了時間を補完しない', () => {
  expect(window.__voicePreparationProbe?.snapshot()).toMatchObject({
    attempt_count: 0, started_client_ms: null, ready_client_ms: null, duration_ms: null,
  })
  vi.spyOn(performance, 'now').mockReturnValue(100)
  button.click()
  expect(window.__voicePreparationProbe?.snapshot()).toMatchObject({
    attempt_count: 1, started_client_ms: 100, ready_client_ms: null, duration_ms: null,
  })
})

test('準備の長さに上限を設けず同じブラウザ時計で発話受付までを記録する', async () => {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(100)
  button.click()
  clock.mockReturnValue(120_500)
  button.setAttribute('aria-pressed', 'true')
  button.classList.add('mic-standby')
  await Promise.resolve()
  expect(window.__voicePreparationProbe?.snapshot()).toMatchObject({
    started_client_ms: 100, ready_client_ms: 120_500, duration_ms: 120_400,
  })
  clock.mockReturnValue(130_000)
  button.classList.add('mic-active')
  await Promise.resolve()
  expect(window.__voicePreparationProbe?.snapshot().ready_client_ms).toBe(120_500)
})

test('無効な開始操作とミュート操作は準備試行に含めない', () => {
  button.disabled = true
  button.dispatchEvent(new MouseEvent('click', {bubbles: true}))
  button.disabled = false
  button.setAttribute('aria-pressed', 'true')
  button.click()
  expect(window.__voicePreparationProbe?.snapshot().attempt_count).toBe(0)
})

test('複数の開始操作は最初の時刻を保持し回数を残す', () => {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(100)
  button.click()
  clock.mockReturnValue(200)
  button.click()
  expect(window.__voicePreparationProbe?.snapshot()).toMatchObject({
    attempt_count: 2, started_client_ms: 100, ready_client_ms: null, duration_ms: null,
  })
})

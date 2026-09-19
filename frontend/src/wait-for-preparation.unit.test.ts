import {afterEach, expect, test, vi} from 'vitest'
import type {Page, TestInfo} from '@playwright/test'
import {waitForVoicePreparation} from '../playwright/wait-for-preparation'

afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = '' })

test.each([false, true])('準備結果がエラー=%sでも準備中の時間だけを試験deadlineから除く', async failure => {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(100)
  const setTimeout = vi.fn()
  let complete!: (handle: {jsonValue: () => Promise<string>}) => void
  let predicate!: () => unknown
  const waitForFunction = vi.fn((condition: () => unknown) => {
    predicate = condition
    return new Promise(resolve => { complete = resolve })
  })
  const operation = waitForVoicePreparation({waitForFunction} as unknown as Page,
    {timeout: 120000, setTimeout} as unknown as TestInfo)
  const outcome = operation.catch(error => error)
  expect(waitForFunction.mock.calls[0]).toEqual([expect.any(Function), undefined, {timeout: 0}])
  expect(predicate()).toBe(false)
  clock.mockReturnValue(120500)
  document.body.innerHTML = failure
    ? '<p class="application-error" role="alert">PRIVATE_SENTINEL</p>'
    : '<button aria-label="マイクをオフにする" aria-pressed="true" class="mic-standby"></button>'
  expect(predicate()).toBe(failure ? 'error' : 'ready')
  complete({jsonValue: async () => failure ? 'error' : 'ready'})
  const result = await outcome
  if (failure) expect(result.message).toBe('voice_preparation_failed')
  else expect(result).toBeUndefined()
  expect(setTimeout.mock.calls).toEqual([[0], [240400]])
})

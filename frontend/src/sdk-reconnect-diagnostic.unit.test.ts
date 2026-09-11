import {expect, test} from 'vitest'
import {classifySdkReconnectLog} from '../playwright/sdk-reconnect-diagnostic'

test('SDK終了通知は固定stageと数値だけを残す', () => {
  expect(classifySdkReconnectLog('could not recover connection after 3 attempts, 806ms. giving up private-sentinel'))
    .toEqual({stage: 'gave_up', attempts: 3, elapsedMs: 806})
  expect(classifySdkReconnectLog('could not recover connection after 3 attempts, -1000ms. giving up'))
    .toEqual({stage: 'gave_up', attempts: 3, elapsedMs: -1000})
})

test.each(['private-sentinel', 'could not recover connection after NaN attempts, 3ms. giving up',
  'could not recover connection after 9007199254740992 attempts, 3ms. giving up',
  'could not recover connection after 3 attempts, 1ms. giving upSECRET'])('任意本文や不正値を出力しない', value => {
  expect(classifySdkReconnectLog(value)).toBeNull()
})

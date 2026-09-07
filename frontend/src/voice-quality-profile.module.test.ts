import { afterEach, beforeEach, expect, test, vi } from 'vitest'

const keys = ['VOICE_QUALITY_NETWORK_FAULT', 'VOICE_QUALITY_FAULT_BRIDGE', 'VOICE_QUALITY_CONTROL_PROBE', 'VOICE_QUALITY_RUN_ID',
  'DS_PROFILE', 'DS_DATA_DIR', 'DS_ENVIRONMENT_ID', 'DS_ENVIRONMENT_RUN_REPORT', 'DS_PROFILE_REPORT',
  'VOICE_MEASUREMENT_KIND', 'VOICE_CONTROLLED_TRACE_PATH', 'VOICE_QUALITY_MANIFEST_PATH']
let previous: Record<string, string | undefined>
beforeEach(() => {
  previous = Object.fromEntries(keys.map(key => [key, process.env[key]]))
  for (const key of keys) delete process.env[key]
  vi.resetModules()
})
afterEach(() => {
  for (const key of keys) {
    if (previous[key] === undefined) delete process.env[key]
    else process.env[key] = previous[key]
  }
})

test.each([false, true])('runnerとorchestratorが同じ明示Profileを使用する: fault=%s', async fault => {
  process.env.VOICE_QUALITY_RUN_ID = 'isolated-test'
  if (fault) {
    process.env.VOICE_QUALITY_FAULT_BRIDGE = '1'
    process.env.VOICE_QUALITY_CONTROL_PROBE = '1'
  }
  const {default: config} = await import('../playwright.livekit-quality.config')
  const server = config.webServer as {env: Record<string, string>; url: string}
  const expected = fault ? 'integration-voice-fault' : 'integration-voice'
  expect(server.env.DS_PROFILE).toBe(expected)
  expect(process.env.DS_PROFILE).toBe(expected)
  expect(server.env.DS_ENVIRONMENT_ID).toBe('test')
  expect(server.env.DS_DATA_DIR).toContain('/runs/isolated-test/runtime-data')
  expect(server.url).toBe('http://127.0.0.1:4174/ready')
})

test('故障環境だけの選択を通常100試行へ混在させない', async () => {
  process.env.VOICE_QUALITY_FAULT_BRIDGE = '1'
  await expect(import('../playwright.livekit-quality.config')).rejects.toThrow('explicit control probe')
})

test('不正な選択値を通常環境へ暗黙fallbackしない', async () => {
  process.env.VOICE_QUALITY_FAULT_BRIDGE = 'invalid'
  await expect(import('../playwright.livekit-quality.config')).rejects.toThrow('invalid fault bridge')
})


test.each(['1', 'invalid'])('専用bridgeなしでnetwork faultを選択できない: %s', async value => {
  process.env.VOICE_QUALITY_NETWORK_FAULT = value
  await expect(import('../playwright.livekit-quality.config')).rejects.toThrow('dedicated bridge')
})

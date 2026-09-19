import {defineConfig} from '@playwright/test'
import base from './playwright.livekit-quality.config'

// 開始取消の局所診断を正式な音声品質cohortとは別のrunで実行する。
if (!process.argv.includes('--list') && (!process.env.VOICE_QUALITY_RUN_ID
  || process.env.VOICE_QUALITY_PROFILE !== 'integration-irodori-cuda-graph')) {
  throw new Error('microphone cancel diagnostic requires a dedicated candidate run')
}

export default defineConfig({
  ...base,
  testDir: './diagnostics/microphone-cancel',
  testMatch: 'microphone-cancel.spec.ts',
  workers: 1,
  fullyParallel: false,
})

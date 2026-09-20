import {defineConfig,devices} from '@playwright/test'
export default defineConfig({testDir:'.',testMatch:process.env.ROLLBACK_TEST ?? 'roundtrip.spec.ts',workers:1,retries:0,
reporter:[['list'],['json',{outputFile:process.env.ROLLBACK_REPORT}]],
outputDir:process.env.ROLLBACK_ARTIFACTS,
use:{...devices['Desktop Chrome'],baseURL:'http://localhost:5173',trace:'off',screenshot:'off',video:'off'}})

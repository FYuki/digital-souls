import {defineConfig,devices} from '@playwright/test'
export default defineConfig({testDir:'.',testMatch:'preparation.spec.ts',workers:1,retries:0,
reporter:[['list'],['json',{outputFile:process.env.PREPARATION_REPORT}]],outputDir:process.env.PREPARATION_ARTIFACTS,
use:{...devices['Desktop Chrome'],baseURL:'http://localhost:5173',trace:'off',screenshot:'off',video:'off'}})

// 生の試行記録を匿名集計する。任意の例外本文や入力pathをエラーへ転記しない。
import {readFile, stat, writeFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {parseArgs} from 'node:util'
import {dirname, join} from 'node:path'
import {createServer} from 'vite'
import Ajv from 'ajv'

let server
try {
  const {values, positionals} = parseArgs({options: {expected: {type: 'string'}, output: {type: 'string'}, 'require-native-sdk': {type: 'boolean'}}, allowPositionals: true})
  const expected = Number(values.expected)
  if (!values.output || !Number.isInteger(expected) || expected < 1 || expected > 100 || positionals.length !== expected) {
    throw new Error('invalid arguments')
  }
  const schema = JSON.parse(await readFile(new URL('../../docs/schemas/voice-quality-reconnect-report-v1.schema.json', import.meta.url), 'utf8'))
  const validateSdk = new Ajv({strict: true}).compile(schema.properties.native_sdk_build)
  let nativeBuild
  const records = []
  for (const path of positionals) {
    if ((await stat(path)).size > 16000000) throw new Error('manifest too large')
    records.push(JSON.parse(await readFile(path, 'utf8')))
    if (values['require-native-sdk']) {
      const sdkPath = join(dirname(path), 'native-sdk.json')
      if ((await stat(sdkPath)).size > 4096) throw new Error('native SDK record too large')
      const sdk = JSON.parse(await readFile(sdkPath, 'utf8'))
      if (Object.keys(sdk).sort().join(',') !== 'build,status' || sdk.status !== 'verified' || !validateSdk(sdk.build)) {
        throw new Error('native SDK not verified')
      }
      const normalized = Object.fromEntries(Object.keys(sdk.build).sort().map(key => [key, sdk.build[key]]))
      if (nativeBuild && JSON.stringify(nativeBuild) !== JSON.stringify(normalized)) throw new Error('mixed native SDK cohort')
      nativeBuild = normalized
    }
  }
  const root = fileURLToPath(new URL('..', import.meta.url))
  server = await createServer({root, configFile: false, server: {middlewareMode: true}, appType: 'custom', logLevel: 'silent'})
  const {summarizeFaultRecoveryCohort} = await server.ssrLoadModule('/playwright/fault-recovery-cohort.ts')
  const report = summarizeFaultRecoveryCohort(records, expected)
  if (nativeBuild) report.native_sdk_build = nativeBuild
  if (!new Ajv({strict: true}).compile(schema)(report)) throw new Error('report schema mismatch')
  await writeFile(values.output, JSON.stringify(report, null, 2) + '\n', {flag: 'wx'})
  process.stdout.write(JSON.stringify({report_written: true, passed: report.evaluation.passed}) + '\n')
  process.exitCode = report.evaluation.passed ? 0 : 1
} catch {
  process.stderr.write('再接続集計を作成できませんでした。引数・入力形式・出力先を確認してください。\n')
  process.exitCode = 2
} finally {await server?.close()}

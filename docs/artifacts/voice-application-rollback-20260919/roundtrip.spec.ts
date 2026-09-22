
import {test, expect} from '@playwright/test'
import {readFileSync, writeFileSync, existsSync} from 'node:fs'
import {createHash} from 'node:crypto'
import {join} from 'node:path'
import {installScheduledFixture, parseScheduledFixture} from '../../../../playwright/controlled-audio-fixture'
import {createVoiceChatDriver, createVoiceTestUseOptions} from '../../../../playwright/voice-chat-suite'
const root = process.env.ROLLBACK_ROOT!
const phase = process.env.ROLLBACK_PHASE!
const resultDir = process.env.ROLLBACK_RESULT_DIR!
const statePath = join(root, 'history-state.json')
const previous = existsSync(statePath) ? JSON.parse(readFileSync(statePath,'utf8')) : null
const hash = (v: unknown) => createHash('sha256').update(JSON.stringify(v)).digest('hex')
const driver = createVoiceChatDriver()
test.use(createVoiceTestUseOptions())
test.setTimeout(240_000)
test('実アプリ切替で既存履歴を保持し、同じ会話へ追記する', async ({page}) => {
  page.setDefaultTimeout(30_000)
  page.setDefaultNavigationTimeout(30_000)
  const result: Record<string,unknown> = {phase, status:'started', priorRows: previous?.rows.length ?? 0}
  const mark = (step: string) => {result.step=step;writeFileSync(join(resultDir,'progress.json'),JSON.stringify(result,null,2))}
  const ended: number[] = []
  page.on('response', response => {
    if (/\/api\/voice\/livekit\/sessions\/[^/]+$/.test(new URL(response.url()).pathname) && response.request().method() === 'DELETE') ended.push(response.status())
  })
  await installScheduledFixture(page, parseScheduledFixture(
    readFileSync(new URL('../../../../playwright/fixtures/speech.wav',import.meta.url)),
    JSON.parse(readFileSync(new URL('../../../../playwright/fixtures/speech.metadata.json',import.meta.url),'utf8')),
  ))
  let cid: string | null = null
  const history = async () => {
    const r = await page.request.get('/api/characters/miori/conversations/'+cid+'/turns')
    expect(r.status()).toBe(200)
    return await r.json() as Array<Record<string,unknown>>
  }
  const signatures = (rows: Array<Record<string,unknown>>) => rows.map(row => ({id:row.turn_id, hash:hash(row)}))
  try {
    mark('open')
    await driver.openVoiceChat(page)
    if (previous) {
      mark('select-existing-thread')
      const menu=page.locator('[data-thread-menu="miori-'+previous.conversationId+'"]')
      await menu.locator('..').locator('button.thread-select').click()
      await expect(menu.locator('..').locator('button.thread-select')).toHaveAttribute('aria-current','page')
    }
    cid = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
    expect(cid).not.toBeNull()
    if (previous) {
      expect(cid).toBe(previous.conversationId)
      expect(signatures(await history())).toEqual(previous.rows)
      result.priorRowsPreserved = true
    }
    mark('before-microphone')
    const mic = page.getByRole('button',{name:/マイクを(オン|オフ)にする/})
    mark('click-microphone')
    await mic.click()
    await expect(mic).toHaveAttribute('aria-pressed','true',{timeout:90_000})
    await expect(mic).toHaveClass(/mic-standby/,{timeout:90_000})
    mark('start-fixture')
    await page.evaluate(() => window.__voiceFixtureClock!.start())
    mark('wait-voice')
    const cycles = await driver.waitForCompletedVoiceCycles(page,1)
    result.voiceCycles = cycles.length
    const count = previous?.rows.length ?? 0
    await expect.poll(async () => (await history()).length,{timeout:30_000}).toBe(count+1)
    const normalRows = await history()
    expect(normalRows.at(-1)?.kind).toBe('content')
    result.appendedCompleted = true
    if (phase === 'after') {
      mark('privacy-input')
    const input=page.getByLabel('メッセージ')
      await input.fill('この話は履歴に残さないで。')
      await input.press('Enter')
      await expect(input).toHaveValue('',{timeout:30_000})
      await expect.poll(async () => (await history()).length,{timeout:30_000}).toBe(count+2)
      const rows=await history(), skipped=rows.at(-1)!
      expect(skipped.kind).toBe('privacy_skipped')
      expect(skipped.reason_code).toBe('STORAGE_OPT_OUT')
      expect('user_content' in skipped || 'assistant_content' in skipped).toBe(false)
      result.privacyBodyExcluded = true
    }
    await driver.endVoiceSession(page)
    await expect(page.getByRole('button',{name:'音声会話を終了'})).toHaveCount(0)
    mark('save-state')
    const rows=await history()
    writeFileSync(statePath,JSON.stringify({conversationId:cid,rows:signatures(rows)},null,2))
    result.historyRows=rows.length
    result.historyDigest=hash(rows)
    result.status='passed'
  } catch (error) {
    result.status='failed';result.error=error instanceof Error ? error.message : String(error)
    throw error
  } finally {
    await driver.endVoiceSession(page).catch(() => undefined)
    await page.evaluate(() => window.__voiceFixtureClock?.close()).catch(() => undefined)
    result.endResponses=ended
    result.transportFailures=await page.evaluate(() => window.__voiceChatE2E?.transportFailures?.map(e=>({stage:e.stage,reason:e.reason})) ?? []).catch(() => [])
    writeFileSync(join(resultDir,'browser-result.json'),JSON.stringify(result,null,2))
  }
})

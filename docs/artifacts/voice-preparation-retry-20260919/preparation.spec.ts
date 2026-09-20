
import {test,expect} from '@playwright/test'
import {readFileSync,writeFileSync,unlinkSync,existsSync} from 'node:fs'
import {join} from 'node:path'
import {installScheduledFixture,parseScheduledFixture} from '../../../../playwright/controlled-audio-fixture'
import {createVoiceChatDriver,createVoiceTestUseOptions} from '../../../../playwright/voice-chat-suite'
const root=process.env.PREPARATION_ROOT!,driver=createVoiceChatDriver()
test.use(createVoiceTestUseOptions())
test.setTimeout(180_000)
test('準備失敗の理由を表示し利用者の再試行で実会話へ復帰する',async({page})=>{
 page.setDefaultTimeout(30_000)
 const flag=join(root,'fail-readiness'),result:any={status:'started'}
 const requests=new Set<string>(),failures:any[]=[],ends:number[]=[]
 const mark=(step:string)=>{result.step=step;writeFileSync(join(root,'progress.json'),JSON.stringify(result,null,2))}
 page.on('request',r=>{if(r.method()==='POST' && new URL(r.url()).pathname==='/api/voice/livekit/token'){requests.add(r.postDataJSON().request_id)}})
 page.on('response',async r=>{
  const path=new URL(r.url()).pathname
  if(path==='/api/voice/livekit/token' && r.status()>=400){
   const data=await r.json();failures.push({status:r.status(),code:data.detail?.code,stage:data.detail?.stage})
  }
  if(r.request().method()==='DELETE' && /\/voice\/livekit\/sessions\//.test(path))ends.push(r.status())
 })
 await installScheduledFixture(page,parseScheduledFixture(
  readFileSync(new URL('../../../../playwright/fixtures/speech.wav',import.meta.url)),
  JSON.parse(readFileSync(new URL('../../../../playwright/fixtures/speech.metadata.json',import.meta.url),'utf8'))))
 try{
  await driver.openVoiceChat(page)
  const mic=page.getByRole('button',{name:/マイクを(オン|オフ)にする/})
  writeFileSync(flag,'enabled')
  mark('fail-preparation')
  await mic.click()
  await expect(page.getByText(/音声合成.*準備.*再試行/).first()).toBeVisible({timeout:45_000})
  await expect(mic).toHaveAttribute('aria-pressed','false')
  await expect(mic).toBeEnabled()
  await expect.poll(()=>failures.length).toBeGreaterThan(0)
  expect(failures.some(x=>x.code==='tts_not_ready' && x.stage==='tts')).toBe(true)
  result.reasonDisplayed=true;result.microphoneOffAfterFailure=true;result.retryEnabled=true
  const before=requests.size
  unlinkSync(flag)
  await page.waitForTimeout(1000)
  expect(requests.size).toBe(before)
  result.noAutomaticRestart=true
  mark('manual-retry')
  await mic.click()
  await expect(mic).toHaveAttribute('aria-pressed','true',{timeout:90_000})
  await expect(mic).toHaveClass(/mic-standby/,{timeout:90_000})
  expect(requests.size).toBe(before+1)
  mark('actual-voice')
  await page.evaluate(()=>window.__voiceFixtureClock!.start())
  const cycles=await driver.waitForCompletedVoiceCycles(page,1)
  result.completedVoiceCycles=cycles.length
  const cid=await page.evaluate(()=>localStorage.getItem('digital-souls:conversation:miori'))
  const history=await page.request.get('/api/characters/miori/conversations/'+cid+'/turns')
  expect(history.status()).toBe(200)
  const rows=await history.json()
  expect(rows).toHaveLength(1);expect(rows[0].kind).toBe('content')
  result.historyRows=rows.length
  await driver.endVoiceSession(page)
  await expect.poll(()=>ends.length).toBe(1)
  result.status='passed'
 }catch(error){result.status='failed';throw error}
 finally{
  if(existsSync(flag))unlinkSync(flag)
  await driver.endVoiceSession(page).catch(()=>undefined)
  await page.evaluate(()=>window.__voiceFixtureClock?.close()).catch(()=>undefined)
  result.failedTokenResponses=failures;result.distinctRequestCount=requests.size;result.endResponses=ends
  result.transportFailures=await page.evaluate(()=>window.__voiceChatE2E?.transportFailures?.map(x=>({stage:x.stage,reason:x.reason}))??[]).catch(()=>[])
  writeFileSync(join(root,'browser-result.json'),JSON.stringify(result,null,2))
 }
})

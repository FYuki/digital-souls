
import {test,expect} from '@playwright/test'
import {readFileSync,writeFileSync} from 'node:fs'
import {join} from 'node:path'
import {createHash} from 'node:crypto'
import {installScheduledFixture,parseScheduledFixture} from '../../../../playwright/controlled-audio-fixture'
import {createVoiceChatDriver,createVoiceTestUseOptions} from '../../../../playwright/voice-chat-suite'
const root=process.env.ROLLBACK_ROOT!,resultDir=process.env.ROLLBACK_RESULT_DIR!
const statePath=join(root,'history-state.json'), previous=JSON.parse(readFileSync(statePath,'utf8'))
const hash=(v:unknown)=>createHash('sha256').update(JSON.stringify(v)).digest('hex')
const driver=createVoiceChatDriver()
test.use(createVoiceTestUseOptions())
test.setTimeout(150_000)
test('新版の実再生途中で終了し再生済み接頭部分を保持する',async({page})=>{
  page.setDefaultTimeout(30_000)
  const result:any={status:'started'},ends:number[]=[]
  const mark=(step:string)=>{result.step=step;writeFileSync(join(resultDir,'progress.json'),JSON.stringify(result,null,2))}
  page.on('response',r=>{if(r.request().method()==='DELETE' && /\/voice\/livekit\/sessions\//.test(r.url()))ends.push(r.status())})
  const history=async()=>{const r=await page.request.get('/api/characters/miori/conversations/'+previous.conversationId+'/turns');expect(r.status()).toBe(200);return r.json()}
  await installScheduledFixture(page,parseScheduledFixture(
    readFileSync(new URL('../../../../playwright/fixtures/speech.wav',import.meta.url)),
    JSON.parse(readFileSync(new URL('../../../../playwright/fixtures/speech.metadata.json',import.meta.url),'utf8'))))
  try{
    await driver.openVoiceChat(page)
    await page.locator('[data-thread-menu="miori-'+previous.conversationId+'"]').locator('..').locator('button.thread-select').click()
    await page.evaluate(()=>{
      const w=window as any,p=w.__digitalSoulsVoiceSessionTestPort, original=p.observeRoom
      w.__rollbackObservation={acked:-1,rendered:0,played:-1}
      p.observeRoom=(o:any)=>{
        original?.(o)
        if(o.acknowledgedPlaybackPrefix!==undefined)w.__rollbackObservation.acked=o.acknowledgedPlaybackPrefix
        if(o.renderedSamples!==undefined)w.__rollbackObservation.rendered=o.renderedSamples
        if(o.playedPrefix!==undefined)w.__rollbackObservation.played=o.playedPrefix
      }
    })
    mark('prepare')
    const mic=page.getByRole('button',{name:/マイクを(オン|オフ)にする/});await mic.click()
    await expect(mic).toHaveClass(/mic-standby/,{timeout:90_000})
    mark('send-text')
    const input=page.getByLabel('メッセージ')
    await input.fill('日本の四季を、春、夏、秋、冬の順に、それぞれ三文ずつ、少し詳しく紹介してください。')
    await input.press('Enter')
    mark('wait-real-prefix-ack')
    await page.waitForFunction(()=>((window as any).__rollbackObservation.acked>=0),undefined,{timeout:90_000})
    result.beforeStop=await page.evaluate(()=>({
      ...(window as any).__rollbackObservation,
      playbackCompletions:Object.keys(window.__voiceChatE2E.playbackCompletions??{}).length
    }))
    expect(result.beforeStop.rendered).toBeGreaterThan(0)
    expect(result.beforeStop.playbackCompletions).toBe(0)
    mark('end-during-playback')
    await driver.endVoiceSession(page)
    await expect.poll(()=>ends.length).toBe(1)
    await expect.poll(async()=> (await history()).length,{timeout:30_000}).toBe(previous.rows.length+1)
    const rows=await history(),last=rows.at(-1)!
    expect(last.kind).toBe('content');expect(last.assistant_content.length).toBeGreaterThan(0)
    expect(rows.slice(0,-1).map((row:any)=>({id:row.turn_id,hash:hash(row)}))).toEqual(previous.rows)
    result.assistantPrefixLength=last.assistant_content.length
    result.historyRows=rows.length;result.historyDigest=hash(rows)
    result.activeAudioGraphsAfterEnd=await page.evaluate(()=>window.__voiceChatE2E.activeAudioGraphs)
    writeFileSync(statePath,JSON.stringify({conversationId:previous.conversationId,rows:rows.map((row:any)=>({id:row.turn_id,hash:hash(row)}))},null,2))
    result.status='passed'
  }catch(error){result.status='failed';throw error}
  finally{
    await driver.endVoiceSession(page).catch(()=>undefined)
    await page.evaluate(()=>window.__voiceFixtureClock?.close()).catch(()=>undefined)
    result.endResponses=ends
    writeFileSync(join(resultDir,'browser-result.json'),JSON.stringify(result,null,2))
  }
})

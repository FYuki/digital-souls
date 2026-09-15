const fs = require('fs');
const { chromium, expect } = require('@playwright/test');
const path = require('path');
const root = fs.realpathSync(process.argv[2]);
if(path.dirname(root)!=='/tmp'||!path.basename(root).startsWith('ds-memory-341-')) throw Error('owned root required');
const manifest=JSON.parse(fs.readFileSync(root+'/runtime-manifest.json','utf8'));
const prepared=JSON.parse(fs.readFileSync(root+'/boundaries-prepare.json','utf8')).derived;
const phase=process.argv[3]||'inspect';
if(!['inspect','delete'].includes(phase)) throw Error('unknown phase');
if(manifest.status!=='ready'||manifest.environmentId!=='test') throw Error('owned test runtime required');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:1000}});
 const errors=[],responses=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('response',r=>{if(r.url().includes('/semantic-memories')) responses.push({method:r.request().method(),url:r.url(),status:r.status()});});
 try {
   await page.goto(manifest.frontend,{waitUntil:'networkidle'});
   await page.getByRole('button',{name:/記憶管理/}).click();
   const section=page.getByRole('region',{name:'意味記憶',exact:true});
   await section.getByRole('heading',{name:'意味記憶',exact:true}).waitFor();
   const card=page.locator('#semantic-'+prepared.delete_id);
   await card.waitFor();
   await expect(card.getByRole('button',{name:'自己申告を訂正',exact:true})).toHaveCount(0);
   await card.locator('summary').click();
   const link=card.getByRole('link',{name:'根拠の経験を開く'}).first();
   await link.click();
   await expect(page.locator('#episodic-'+prepared.sources[0].source_id)).toBeInViewport();
   const correction=await page.request.patch(manifest.backend+'/characters/miori/semantic-memories/'+prepared.delete_id,
      {data:{expected_version:1,value:'紅茶',idempotency_key:require('crypto').randomUUID()}});
   if(correction.status()!==422) throw Error('derived correction status '+correction.status()+' '+await correction.text());
   const foreignList=await page.request.get(manifest.backend+'/characters/acceptance-other/semantic-memories');
   if(foreignList.status()!==200 || (await foreignList.json()).length) throw Error('foreign list leaked');
   const foreign=await page.request.get(manifest.backend+'/characters/acceptance-other/semantic-memories/'+prepared.delete_id);
   if(foreign.status()!==404) throw Error('foreign record read');
   const foreignDelete=await page.request.delete(manifest.backend+'/characters/acceptance-other/semantic-memories/'+prepared.delete_id,{data:{expected_version:1}});
   if(foreignDelete.status()!==404) throw Error('foreign deletion status '+foreignDelete.status());
   if(phase==='delete') {
      await card.getByRole('button',{name:'削除',exact:true}).click();
      await page.screenshot({path:root+'/ui-derived-delete-review.png',fullPage:true});
      const response=page.waitForResponse(r=>r.request().method()==='DELETE'&&r.url().includes('/miori/semantic-memories/'));
      await card.getByRole('button',{name:'完全に削除',exact:true}).click();
      if((await response).status()!==204) throw Error('delete failed');
      await card.getByText(/削除済み・第/).waitFor();
      await expect(card.locator('.content')).toHaveCount(0);
      await expect(card).toBeFocused();
   }
   await page.screenshot({path:root+'/ui-derived-'+phase+'.png',fullPage:true});
   const evidence={phase,run_id:manifest.runId,record_id:prepared.delete_id,errors,responses,
      derived_correction_denied:true,episode_link_visible:true,foreign_read_and_delete_denied:true,visible_text:await page.locator('body').innerText()};
   fs.writeFileSync(root+'/ui-derived-'+phase+'.json',JSON.stringify(evidence,null,2));
   console.log(JSON.stringify({...evidence,visible_text:undefined}));
   if(errors.length) throw Error('browser errors');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
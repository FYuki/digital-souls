import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
const require = createRequire(import.meta.url);

test('assertion uses Python normalization and rejects malformed output', () => {
  const assertion = require('../evals/semantic/assertions.cjs');
  const vars = { case_id: 'cross-language-08' };
  const output = JSON.stringify({id: vars.case_id, answer:'ＴＵＥＳＤＡＹ', dispatch_valid:true,
    dispatch_memory_ids:['target'], dispatched:true, discarded:false, context_empty:false});
  assert.equal(assertion(output, {vars}).pass, true);
  assert.equal(assertion('{"error":"failed"}', {vars}).pass, false);
});

test('report gate rejects empty, malformed and nonfinite promptfoo exports', async () => {
  const { inspectReport } = await import('../evals/semantic/report_gate.mjs');
  for (const raw of [{}, {results:{version:3, results:[]}}, {score:NaN}]) {
    assert.throws(() => inspectReport(raw, {mode:'fixture'}));
  }
});

test('configuration contains all input IDs and no gold', () => {
  const { configuration } = require('../evals/semantic/config.cjs');
  const c = configuration('fixture');
  assert.equal(c.tests.length, 62);
  assert(c.tests.every(t => Object.keys(t.vars).join() === 'case_id'));
  assert.equal(c.providers.length, 1);
  assert.equal(c.sharing, false);
});

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const directory = path.join(root,'evals/semantic');
const { python } = require('../evals/semantic/bridge.cjs');
const cases = JSON.parse(fs.readFileSync(path.join(directory,'cases.json'),'utf8')).cases;
const dispatches = new Map(JSON.parse(fs.readFileSync(path.join(directory,'expectations.json'),'utf8')).cases.map(c => [c.id,c.dispatch.memory_ids]));
const fixtures = JSON.parse(fs.readFileSync(path.join(directory,'answer-fixtures.json'),'utf8')).responses;
const invalidFixtures = JSON.parse(fs.readFileSync(path.join(directory,'invalid-answer-fixtures.json'),'utf8')).overrides;
const manifest = python({operation:'manifest',mode:'fixture'});
const identity = Object.fromEntries(Object.entries(manifest).filter(([k]) => !['schema_version','commit','case_version','promptfoo_version'].includes(k)));
function observation(c, responses = fixtures) {
  const blocked = c.mutations.some(m => m.phase === 'after_search');
  const discarded = c.mutations.some(m => m.phase === 'after_answer');
  const empty = c.id.startsWith('unrelated-') || ['source-epoch-changed','below-threshold'].includes(c.id);
  return {id:c.id,answer:blocked || discarded ? null : responses[c.id].answer,
    dispatch_valid:!blocked,dispatch_memory_ids:dispatches.get(c.id),dispatched:!blocked,discarded,context_empty:empty};
}
function exported(responses = fixtures, adjust = o => o) {
  const { configuration } = require('../evals/semantic/config.cjs');
  const observations = cases.map(c => adjust(observation(c,responses)));
  const run = python({operation:'batch',observations});
  const config = configuration('fixture');
  return {config, results:{version:3, stats:{successes:run.cases.filter(r => r.passed).length,
    failures:run.cases.filter(r => !r.passed).length,errors:0},results:run.cases.map((checked,i) => {
      const id = checked.id;
      const spec = config.defaultTest.assert[0];
      const grade = {pass:checked.passed,score:Number(checked.passed),assertion:spec,metadata:checked};
      return {vars:{case_id:id},testCase:{vars:{case_id:id},assert:[spec]},prompt:{raw:id},
        provider:{id:config.providers[0].id,label:'core-answer'},
        success:checked.passed,score:Number(checked.passed),failureReason:checked.passed ? 0 : 1,
        response:{cached:false,output:JSON.stringify({observation:observations[i],identity})},
        gradingResult:{pass:grade.pass,score:grade.score,componentResults:[grade]}};
    })}};
}
const baseline = exported();
test('full export is independently scored, body-free, with inclusive 90% boundary', async () => {
  const {inspectReport,finalizeReport} = await import('../evals/semantic/report_gate.mjs');
  const run = inspectReport(baseline,{mode:'fixture',identity});
  assert.equal(run.passed,true);
  const missing = exported({...fixtures,'synonym-02':{answer:'わかりません。'}});
  const ninety = inspectReport(missing,{mode:'fixture',identity});
  assert.equal(ninety.categories.synonym.rate,0.9);
  assert.equal(ninety.passed,true);
  const report = finalizeReport(manifest,[run,run,run],3);
  assert.equal(report.quality_evidence,false);
  const serialized = JSON.stringify(report);
  assert(cases.every(c => !serialized.includes(c.query)));
  assert(cases.every(c => !serialized.includes(fixtures[c.id].answer)));
  assert.throws(() => finalizeReport(manifest,[],3));
});

for (const variant of ['forbidden','missing']) {
  test(`invalid answer input fixture makes gate FAIL: ${variant}`, async () => {
    const {inspectReport} = await import('../evals/semantic/report_gate.mjs');
    const bad = exported({...fixtures,...invalidFixtures[variant]});
    const run = inspectReport(bad,{mode:'fixture',identity});
    assert.equal(run.passed,false);
    if (variant === 'forbidden') assert.equal(run.gates_passed,false);
    else assert.equal(run.categories.synonym.rate,0.8);
  });
}
for (const fault of ['empty','missing','duplicate','unknown','error','response-error','nan','inf','cached','grading','assertion','stats','identity','prompt','skipped','version']) {
  test(`strict export rejects ${fault}`, async () => {
    const {inspectReport} = await import('../evals/semantic/report_gate.mjs');
    const bad = structuredClone(baseline);
    const rows = bad.results.results;
    const r = rows[0];
    if (fault === 'empty') bad.results.results = [];
    if (fault === 'missing') rows.pop();
    if (fault === 'duplicate') rows[1] = rows[0];
    if (fault === 'unknown') r.vars.case_id = 'unknown';
    if (fault === 'error') r.error = 'provider-failed';
    if (fault === 'response-error') r.response.error = 'failed';
    if (fault === 'nan') r.score = NaN;
    if (fault === 'inf') r.gradingResult.score = Infinity;
    if (fault === 'cached') r.response.cached = true;
    if (fault === 'grading') r.gradingResult.componentResults[0].metadata.passed = false;
    if (fault === 'assertion') r.testCase.assert = [];
    if (fault === 'stats') bad.results.stats.successes = 0;
    if (fault === 'identity') { const output=JSON.parse(r.response.output); output.identity.quality_evidence=true; r.response.output=JSON.stringify(output); }
    if (fault === 'prompt') r.prompt.raw = 'gold-injected';
    if (fault === 'skipped') r.skipped = true;
    if (fault === 'version') bad.results.version = 4;
    assert.throws(() => inspectReport(bad,{mode:'fixture',identity}));
  });
}

test('credentials, proxy, dotenv and caller Node options cannot enter child environment', async () => {
  const {cleanEnvironment} = await import('./evaluate-semantic-answer.mjs');
  const env=cleanEnvironment('/private/tmp',{mode:'fixture'},{PATH:'/usr/bin',OPENAI_API_KEY:'secret',HTTPS_PROXY:'proxy',NODE_OPTIONS:'--import evil',DOTENV_CONFIG_PATH:'evil',PGPASSWORD:'secret',DSC_TEST_POSTGRES_SOCKET:'/socket'});
  for (const key of ['OPENAI_API_KEY','HTTPS_PROXY','NODE_OPTIONS','PGPASSWORD']) assert(!Object.hasOwn(env,key));
  assert.equal(env.DOTENV_CONFIG_PATH,'/private/tmp/empty.env');
  assert.equal(env.DSC_TEST_POSTGRES_SOCKET,'/socket');
  assert.equal(env.PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION,'1');
});

test('Node CJS/ESM HTTP, TCP, TLS, DNS and fetch are blocked before connection', () => {
  const script = `const assert=require('node:assert/strict'); const re=/network access is disabled/;
  (async()=>{ await assert.rejects(fetch('http://127.0.0.1:9'),re);
    for(const name of ['node:http','node:https']) assert.throws(()=>require(name).get('http://127.0.0.1:9'),re);
    assert.throws(()=>require('node:net').createConnection({port:9}),re);
    assert.throws(()=>require('node:tls').connect(9),re);
    assert.throws(()=>require('node:dns').lookup('localhost',()=>{}),re);
    await assert.rejects(require('node:dns').promises.lookup('localhost'),re);
    const esm=await import('node:net'); assert.throws(()=>esm.connect(9),re);
  })().catch(()=>process.exit(1));`;
  const child = spawnSync(process.execPath,['--require',path.join(directory,'network_guard.cjs'),'-e',script],{env:{PATH:'/usr/bin:/bin'},encoding:'utf8'});
  assert.equal(child.status,0,child.stderr);
});

test('local_model is disabled by default and requires explicit execution with profile', () => {
  const child = spawnSync(process.execPath,[path.join(root,'tools/evaluate-semantic-answer.mjs'),'--mode','local_model'],{env:{PATH:process.env.PATH},encoding:'utf8'});
  assert.equal(child.status,3);
  assert.equal(JSON.parse(child.stdout).status,'NOT RUN');
});

test('package and lock pin the installed promptfoo 0.117.2', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(directory,'package.json')));
  const lock = JSON.parse(fs.readFileSync(path.join(directory,'package-lock.json')));
  assert.equal(pkg.devDependencies.promptfoo,'0.117.2');
  assert.equal(lock.packages['node_modules/promptfoo'].version,'0.117.2');
  assert.equal(pkg.overrides.promptfoo['better-sqlite3'],'13.0.3');
  assert.equal(lock.packages['node_modules/better-sqlite3'].version,'13.0.3');
});

test('native SQLite statement collection survives GC with the Node 24 evaluation binding', () => {
  const script = `const assert=require('node:assert/strict');
    const Database=require(${JSON.stringify(path.join(directory,'node_modules/better-sqlite3'))});
    const db=new Database(':memory:');
    db.exec('CREATE TABLE synthetic (value INTEGER)');
    for(let batch=0;batch<20;batch++) {
      for(let i=0;i<100;i++) {
        db.prepare('INSERT INTO synthetic VALUES (?)').run(i);
        assert.equal(db.prepare('SELECT count(*) AS n FROM synthetic').get().n,batch*100+i+1);
      }
      global.gc();
    }
    db.close(); global.gc();`;
  const child=spawnSync(process.execPath,['--expose-gc','-e',script],{encoding:'utf8'});
  assert.equal(child.signal,null);
  assert.equal(child.status,0);
});

test('install scripts are disabled and the shipped SQLite binding needs no lifecycle script', () => {
  assert.equal(fs.readFileSync(path.join(directory,'.npmrc'),'utf8').trim(),'ignore-scripts=true');
  const pkg=require('../evals/semantic/node_modules/better-sqlite3/package.json');
  for(const event of ['preinstall','install','postinstall','prepare']) assert.equal(pkg.scripts[event],undefined);
  assert.equal(pkg.gypfile,false);
});

test('diagnostic classifications disclose only fixed structural labels', async () => {
  const {classifyLog}=await import('./evaluate-semantic-answer.mjs');
  assert.deepEqual(classifyLog(''),['empty']);
  assert.deepEqual(classifyLog('private query / answer / stderr'),['unclassified']);
  assert.deepEqual(classifyLog('RemoveEnvironmentCleanupHook\nAssertion failed: (env) != nullptr\nprivate answer'),['native-cleanup-hook-abort']);
  assert.deepEqual(classifyLog('SQLITE_BUSY: private answer'),['sqlite-busy','sqlite-error']);
  assert.deepEqual(classifyLog('ETIMEDOUT private answer'),['assertion-timeout']);
});

test('command awaits stdio closure and keeps failure bodies in private files', async () => {
  const {command,classifyLog}=await import('./evaluate-semantic-answer.mjs');
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'answer-command-test-'));
  fs.chmodSync(temp,0o700);
  try {
    const logs={stdout:path.join(temp,'stdout'),stderr:path.join(temp,'stderr')};
    const result=await command(['-e',"process.stdout.write('private output'); process.stderr.write('private error'); process.exitCode=7"],{},logs);
    assert.deepEqual(result,{code:7,signal:null,spawn_error:null});
    assert.equal(fs.readFileSync(logs.stdout,'utf8'),'private output');
    assert.equal(fs.readFileSync(logs.stderr,'utf8'),'private error');
    assert.equal(fs.statSync(logs.stderr).mode & 0o777,0o600);
    assert.deepEqual(classifyLog(fs.readFileSync(logs.stderr,'utf8')),['unclassified']);
  } finally { fs.rmSync(temp,{recursive:true,force:true}); }
});

test('error answer input fixture cannot become a no_memory success', async () => {
  const {inspectReport} = await import('../evals/semantic/report_gate.mjs');
  const raw = structuredClone(baseline);
  const row = raw.results.results.find(r => r.vars.case_id === 'synonym-02');
  row.response = {error:invalidFixtures.error['synonym-02'].error};
  row.failureReason = 2;
  assert.throws(() => inspectReport(raw,{mode:'fixture',identity}));
});


test('report gate rescores top-five inclusion and accepts reordered extra IDs', async () => {
  const {inspectReport} = await import('../evals/semantic/report_gate.mjs');
  const raw = exported(fixtures, o => {
    if (o.id === 'synonym-02') o.dispatch_memory_ids = ['extra', 'target'];
    if (o.id === 'equivalent-band-order') o.dispatch_memory_ids.reverse();
    return o;
  });
  assert.equal(inspectReport(raw,{mode:'fixture',identity}).passed,true);
  const missing = exported(fixtures, o => {
    if (o.id === 'synonym-02') o.dispatch_memory_ids = ['extra'];
    return o;
  });
  const run = inspectReport(missing,{mode:'fixture',identity});
  assert.equal(run.gates_passed,false);
  assert.equal(run.cases.find(c => c.id === 'synonym-02').gates.dispatch,false);
  const forged = structuredClone(raw);
  forged.results.results.find(r => r.vars.case_id === 'synonym-02')
    .response.output = JSON.stringify({observation:{...observation(cases.find(c => c.id === 'synonym-02')),
      dispatch_memory_ids:['extra']},identity});
  assert.throws(() => inspectReport(forged,{mode:'fixture',identity}));
});

test('external runner forwards only nominated credentials and never in dry-run', async () => {
  const {cleanEnvironment}=await import('./evaluate-semantic-answer.mjs');
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'external-env-'));
  try {
    const profile=path.join(temp,'profile.json');
    fs.writeFileSync(profile,JSON.stringify({api_key_env:'SYNTHETIC_KEY',api_base_env:'SYNTHETIC_BASE'}));
    const inherited={PATH:process.env.PATH,SYNTHETIC_KEY:'key-sentinel',SYNTHETIC_BASE:'https://user:secret@host.invalid/private?secret',OPENAI_API_KEY:'ambient-secret',HTTPS_PROXY:'proxy-secret',PGPASSWORD:'pg-secret',NODE_OPTIONS:'evil'};
    const env=cleanEnvironment(temp,{mode:'external_model',profile},inherited);
    assert.equal(env.SYNTHETIC_KEY,inherited.SYNTHETIC_KEY);
    assert.equal(env.SYNTHETIC_BASE,inherited.SYNTHETIC_BASE);
    assert.equal(env.DSC_EXECUTE_EXTERNAL_MODEL,'1');
    for(const key of ['OPENAI_API_KEY','HTTPS_PROXY','PGPASSWORD','NODE_OPTIONS']) assert(!Object.hasOwn(env,key));
    const dry=cleanEnvironment(temp,{mode:'external_model',profile,dryRun:true},inherited);
    assert(!Object.hasOwn(dry,'SYNTHETIC_KEY') && !Object.hasOwn(dry,'SYNTHETIC_BASE'));
    assert(!Object.hasOwn(dry,'DSC_EXECUTE_EXTERNAL_MODEL'));
    assert.throws(()=>cleanEnvironment(temp,{mode:'external_model',profile},{}),/credentials-unavailable/);
    fs.writeFileSync(profile,JSON.stringify({api_key_env:'HTTPS_PROXY'}));
    assert.throws(()=>cleanEnvironment(temp,{mode:'external_model',profile},inherited),/credential-name/);
  } finally {fs.rmSync(temp,{recursive:true,force:true});}
});

test('external_model requires its own execution flag and profile (exit 3)', () => {
  for(const args of [[],['--execute-local-model'],['--execute-external-model'],['--profile','/absent'],['--dry-run']]) {
    const child=spawnSync(process.execPath,[path.join(root,'tools/evaluate-semantic-answer.mjs'),'--mode','external_model',...args],{env:{PATH:process.env.PATH},encoding:'utf8'});
    assert.equal(child.status,3);
    assert.equal(JSON.parse(child.stdout).status,'NOT RUN');
  }
});

test('external dry-run runs without execution flag and never forwards credentials or execution permission', async () => {
  const bridge=require('../evals/semantic/bridge.cjs');
  const original=bridge.python;
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'external-dry-cli-'));
  const calls=[];
  try {
    bridge.python=(request,env) => {
      calls.push(request.operation);
      if(request.operation === 'manifest') return {...manifest,mode:'external_model',classifier_reason:'baseline-parity-no-classifier-export'};
      assert.equal(request.operation,'dry-run');
      assert.equal(request.profile,'/synthetic/external.json');
      assert(!Object.hasOwn(env,'DSC_EXECUTE_EXTERNAL_MODEL'));
      assert(!Object.hasOwn(env,'OPENAI_API_KEY'));
      return {cases:62,calls_per_run:60,input_tokens_per_run:10000,token_estimator:'utf8-byte-estimate'};
    };
    const {main}=await import('./evaluate-semantic-answer.mjs?dry-run-cli-test');
    const output=path.join(temp,'report.json');
    assert.equal(await main(['--mode','external_model','--profile','/synthetic/external.json','--dry-run','--output',output]),0);
    const report=JSON.parse(fs.readFileSync(output,'utf8'));
    assert.equal(report.status,'DRY RUN');
    assert.equal(report.external_send,false);
    assert.equal(report.quality_evidence,false);
    assert.equal(report.estimate.calls,180);
    assert.equal(report.classifier_reason,'baseline-parity-no-classifier-export');
    assert.deepEqual(calls,['manifest','dry-run']);
  } finally {
    bridge.python=original;
    fs.rmSync(temp,{recursive:true,force:true});
  }
});

test('external dry-run with execution flag fails before profile or credentials are read', () => {
  for(const args of [[],['--profile','/absent']]) {
    const child=spawnSync(process.execPath,[path.join(root,'tools/evaluate-semantic-answer.mjs'),'--mode','external_model','--dry-run','--execute-external-model',...args],{env:{PATH:process.env.PATH},encoding:'utf8'});
    assert.equal(child.status,1);
    assert.equal(child.stdout,'');
    assert.match(child.stderr,/FAIL \(configuration: arguments\)/);
  }
});

test('external dry-run counts runs and uses only explicit price/output assumptions', async () => {
  const {dryRunReport}=await import('./evaluate-semantic-answer.mjs');
  const external={...manifest,mode:'external_model',quality_evidence:true,external_send:true,cache:false};
  const counts={cases:62,calls_per_run:60,input_tokens_per_run:10000,token_estimator:'utf8-byte-estimate'};
  const report=dryRunReport(external,counts,{runs:3,inputPrice:2,outputPrice:4,outputTokens:100});
  assert.equal(report.estimate.calls,180);
  assert.equal(report.estimate.input_tokens,30000);
  assert.equal(report.estimate.output_tokens_assumption,18000);
  assert.equal(report.estimate.total_cost,0.132);
  assert.equal(report.external_send,false);
  assert.equal(report.quality_evidence,false);
  const noPrices=dryRunReport(external,counts,{runs:3});
  assert(!Object.hasOwn(noPrices.estimate,'total_cost'));
  assert.equal(noPrices.estimate.output_tokens_assumption,null);
  assert(cases.every(c=>!JSON.stringify(report).includes(c.query)));
});

test('external reports remain body-free and disclose only approved execution identity', async () => {
  const {inspectReport,finalizeReport}=await import('../evals/semantic/report_gate.mjs');
  const external={...manifest,mode:'external_model',classifier_reason:'baseline-parity-no-classifier-export',quality_evidence:true,provider:'openai',model:'openai/synthetic-model',transport:'sdk',external_send:true,cache:false};
  const externalIdentity=Object.fromEntries(Object.entries(external).filter(([k])=>!['schema_version','commit','case_version','promptfoo_version'].includes(k)));
  const raw=structuredClone(baseline);
  raw.config.providers[0].config.mode='external_model';
  for(const row of raw.results.results) {
    const parsed=JSON.parse(row.response.output);
    parsed.identity=externalIdentity;
    // Private SDK output may contain a synthetic sentinel; the public report cannot.
    if (parsed.observation.answer !== null) parsed.observation.answer += ' key-sentinel https://user:secret@host.invalid/private?secret';
    row.response.output=JSON.stringify(parsed);
  }
  const run=inspectReport(raw,{mode:'external_model',identity:externalIdentity});
  const report=finalizeReport(external,[run],1);
  const encoded=JSON.stringify(report);
  assert.equal(report.quality_evidence,true);
  assert.equal(report.classifier_reason,'baseline-parity-no-classifier-export');
  for(const sentinel of ['key-sentinel','user:secret','/private?secret',...cases.map(c=>c.query)]) assert(!encoded.includes(sentinel));
  assert.deepEqual(Object.keys(manifest).sort(),['answer_model','card_sha256','case_version','cases_sha256','characters_sha256','classifier','commit','fixtures_sha256','mode','promptfoo_version','quality_evidence','retrieval','schema_version','space'].sort());
});

test('external missing credential fails before execution with secret-free stdout stderr report', () => {
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'external-public-test-'));
  try {
    const profile=path.join(temp,'profile.json');
    const output=path.join(temp,'report.json');
    const embedding=JSON.parse(fs.readFileSync(path.join(root,'examples/embedding.example.json'),'utf8'));
    embedding.enabled=true;
    fs.writeFileSync(profile,JSON.stringify({enabled:true,chat:{profile_id:'external-test',model:'openai/synthetic-model',transport:'sdk',external_send_allowed:true},embedding,api_key_env:'SYNTHETIC_KEY',api_base_env:'SYNTHETIC_BASE'}));
    const child=spawnSync(process.execPath,[path.join(root,'tools/evaluate-semantic-answer.mjs'),'--mode','external_model','--execute-external-model','--profile',profile,'--output',output],{env:{PATH:process.env.PATH,SYNTHETIC_BASE:'https://user:url-sentinel@host.invalid/private?url-sentinel',UNRELATED_API_KEY:'key-sentinel'},encoding:'utf8'});
    assert.equal(child.status,1);
    assert(child.stderr.includes('credentials-unavailable'));
    const report=JSON.parse(fs.readFileSync(output,'utf8'));
    assert.equal(report.passed,false);
    assert.deepEqual(report.executions,[]);
    const publicOutput=child.stdout+child.stderr+JSON.stringify(report);
    for(const secret of ['url-sentinel','key-sentinel','user:','/private?',...cases.map(c=>c.query)]) assert(!publicOutput.includes(secret));
  } finally {fs.rmSync(temp,{recursive:true,force:true});}
});

test('codex runner forwards no credentials, proxies, ambient Codex home or MCP configuration', async () => {
  const {cleanEnvironment}=await import('./evaluate-semantic-answer.mjs');
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'codex-env-'));
  try {
    const profile=path.join(temp,'profile.json');
    fs.writeFileSync(profile,JSON.stringify({answer_provider:'codex_subscription',codex_executable:'/synthetic/codex',codex_home:'/synthetic/home'}));
    const inherited={PATH:process.env.PATH,OPENAI_API_KEY:'key-sentinel',HTTPS_PROXY:'proxy-sentinel',CODEX_HOME:'/ambient/home',T3_ACP_MCP_NODE:'/ambient/mcp',SYNTHETIC_KEY:'key-sentinel'};
    const env=cleanEnvironment(temp,{mode:'external_model',profile},inherited);
    assert.equal(env.DSC_EXECUTE_EXTERNAL_MODEL,'1');
    for(const key of ['OPENAI_API_KEY','HTTPS_PROXY','CODEX_HOME','T3_ACP_MCP_NODE','SYNTHETIC_KEY']) assert(!Object.hasOwn(env,key));
    const dry=cleanEnvironment(temp,{mode:'external_model',profile,dryRun:true},inherited);
    assert(!Object.hasOwn(dry,'DSC_EXECUTE_EXTERNAL_MODEL'));
  } finally {fs.rmSync(temp,{recursive:true,force:true});}
});

function codexExport() {
  const external={...manifest,mode:'external_model',classifier_reason:'baseline-parity-no-classifier-export',
    quality_evidence:true,provider:'codex_subscription',model:'gpt-6-luna',transport:'sdk',external_send:true,
    cache:false,codex_cli_version:null,condition_note:'codex-cli-adds-own-instructions-not-pure-api'};
  const expected=Object.fromEntries(Object.entries(external).filter(([k])=>!['schema_version','commit','case_version','promptfoo_version'].includes(k)));
  const raw=structuredClone(baseline);
  raw.config.providers[0].config.mode='external_model';
  for(const row of raw.results.results) {
    const parsed=JSON.parse(row.response.output);
    parsed.identity={...expected,codex_cli_version:parsed.observation.dispatched ? '0.162.1' : null};
    if (parsed.observation.answer !== null) parsed.observation.answer += ' stdout-sentinel stderr-sentinel credential-file-sentinel';
    row.response.output=JSON.stringify(parsed);
  }
  return {external,expected,raw};
}

test('codex report records dispatched CLI version and condition difference without private output', async () => {
  const {inspectReport,finalizeReport}=await import('../evals/semantic/report_gate.mjs');
  const {external,expected,raw}=codexExport();
  const run=inspectReport(raw,{mode:'external_model',identity:expected});
  const report=finalizeReport(external,[run,run,run],3);
  assert.equal(report.codex_cli_version,'0.162.1');
  assert.equal(report.provider,'codex_subscription');
  assert.equal(report.model,'gpt-6-luna');
  assert.equal(report.external_send,true);
  assert.equal(report.cache,false);
  assert.equal(report.condition_note,'codex-cli-adds-own-instructions-not-pure-api');
  assert.equal(report.classifier_reason,'baseline-parity-no-classifier-export');
  assert.equal(report.runs.length,3);
  assert.deepEqual(report.commit,manifest.commit);
  const encoded=JSON.stringify(report);
  for(const secret of ['stdout-sentinel','stderr-sentinel','credential-file-sentinel',...cases.map(c=>c.query)]) assert(!encoded.includes(secret));
  const dry=(await import('./evaluate-semantic-answer.mjs')).dryRunReport(external,
    {cases:62,calls_per_run:60,input_tokens_per_run:10000,token_estimator:'utf8-byte-estimate'},{runs:3});
  assert.equal(dry.codex_cli_version,null);
  assert.equal(dry.external_send,false);
});

for(const fault of ['missing','old','private-text','drift','denied-version','missing-dispatched-version']) {
  test(`codex report rejects invalid or drifting runtime version: ${fault}`, async () => {
    const {inspectReport}=await import('../evals/semantic/report_gate.mjs');
    const {expected,raw}=codexExport();
    let altered=false;
    for(const row of raw.results.results) {
      const parsed=JSON.parse(row.response.output);
      if(fault === 'denied-version' && !parsed.observation.dispatched) {
        parsed.identity.codex_cli_version='0.162.1';
        altered=true;
      } else if(parsed.observation.dispatched) {
        if(fault === 'missing') delete parsed.identity.codex_cli_version;
        if(fault === 'old') parsed.identity.codex_cli_version='0.151.9';
        if(fault === 'private-text') parsed.identity.codex_cli_version='credential-file-sentinel';
        if(fault === 'drift' && !altered) parsed.identity.codex_cli_version='0.162.2';
        if(fault === 'missing-dispatched-version') parsed.identity.codex_cli_version=null;
        altered=true;
      }
      row.response.output=JSON.stringify(parsed);
    }
    assert(altered);
    assert.throws(()=>inspectReport(raw,{mode:'external_model',identity:expected}));
  });
}

test('codex report rejects version changes between runs', async () => {
  const {inspectReport,finalizeReport}=await import('../evals/semantic/report_gate.mjs');
  const {external,expected,raw}=codexExport();
  const run=inspectReport(raw,{mode:'external_model',identity:expected});
  assert.throws(()=>finalizeReport(external,[run,{...run,codex_cli_version:'0.162.2'}],2));
});

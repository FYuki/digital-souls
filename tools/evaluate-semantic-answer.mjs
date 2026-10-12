import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { inspectReport, finalizeReport } from '../evals/semantic/report_gate.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const directory = path.join(root,'evals/semantic');
const require = createRequire(import.meta.url);
const { python } = require('../evals/semantic/bridge.cjs');

export function cleanEnvironment(temp, options, inherited = process.env) {
  const env = {
    PATH: inherited.PATH, HOME: temp, TMPDIR: temp, LANG:'C.UTF-8',
    LITELLM_LOCAL_MODEL_COST_MAP:'True', DOTENV_CONFIG_PATH:path.join(temp,'empty.env'),
    PROMPTFOO_CONFIG_DIR:path.join(temp,'promptfoo'), PROMPTFOO_DISABLE_TELEMETRY:'1',
    PROMPTFOO_DISABLE_UPDATE:'1', PROMPTFOO_DISABLE_SHARING:'1',
    PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION:'1', PROMPTFOO_CACHE_ENABLED:'false',
    DSC_ANSWER_MODE:options.mode,
  };
  for (const name of ['SOCKET','PORT','DATABASE','USER']) {
    const key = `DSC_TEST_POSTGRES_${name}`;
    if (inherited[key]) env[key] = inherited[key];
  }
  if (options.profile) env.DSC_ANSWER_PROFILE = options.profile;
  if (options.variant) env.DSC_ANSWER_VARIANT = options.variant;
  if (options.mode === 'external_model' && !options.dryRun) {
    env.DSC_EXECUTE_EXTERNAL_MODEL = '1';
    const profile = JSON.parse(fs.readFileSync(options.profile,'utf8'));
    if (profile.answer_provider === 'codex_subscription') return env;
    const names = [profile.api_key_env, ...(profile.api_base_env ? [profile.api_base_env] : [])];
    for (const name of names) {
      if (typeof name !== 'string' || !/^[A-Z][A-Z0-9_]{0,63}$/.test(name)
        || /^(PATH|HOME|TMPDIR|LANG|.*PROXY|DOTENV.*|NODE.*|PYTHON.*|DSC_.*|PROMPTFOO_.*|LITELLM_.*|LD_.*|DYLD_.*)$/.test(name)) throw new Error('credential-name');
      if (typeof inherited[name] !== 'string' || !inherited[name].trim()) throw new Error('credentials-unavailable');
      env[name] = inherited[name];
    }
  }
  return env;
}

export function dryRunReport(manifest, counts, options) {
  for (const key of ['cases','calls_per_run','input_tokens_per_run']) {
    if (!Number.isSafeInteger(counts[key]) || counts[key] < 0) throw new Error('dry-run-count');
  }
  if (counts.cases !== 62 || counts.calls_per_run > counts.cases) throw new Error('dry-run-count');
  const calls = counts.calls_per_run * options.runs;
  const input = counts.input_tokens_per_run * options.runs;
  const output = calls * (options.outputTokens ?? 0);
  const estimate = {calls_per_run:counts.calls_per_run, calls, input_tokens:input,
    token_estimator:counts.token_estimator, output_tokens_per_call_assumption:options.outputTokens ?? null,
    output_tokens_assumption:options.outputTokens === undefined ? null : output};
  if (options.inputPrice !== undefined) estimate.input_cost = input / 1e6 * options.inputPrice;
  if (options.outputPrice !== undefined) estimate.output_cost = output / 1e6 * options.outputPrice;
  if (estimate.input_cost !== undefined && estimate.output_cost !== undefined) estimate.total_cost = estimate.input_cost + estimate.output_cost;
  return {...manifest, status:'DRY RUN', dry_run:true, quality_evidence:false, external_send:false,
    runs:options.runs, cases:counts.cases, estimate};
}
export function classifyLog(log) {
  const patterns = {
    'native-cleanup-hook-abort': /RemoveEnvironmentCleanupHook[\s\S]*Assertion failed: \(env\) != nullptr/,
    'native-binding-missing': /Could not locate the bindings file|Cannot find module.*better_sqlite3/,
    'python-timeout': /Command timed out|Python.*timed out/i,
    'assertion-timeout': /Script execution timed out|ETIMEDOUT|Answer scoring bridge failed/i,
    'sqlite-busy': /SQLITE_BUSY|database is locked/i,
    'sqlite-error': /SQLITE_[A-Z_]+/,
    'provider-error': /answer_evaluation_failed|Error running Python script/,
    'network-blocked': /network access is disabled/,
    'output-write': /ENOSPC|EACCES|EROFS/,
    'worker-error': /worker.*(?:error|exit)|ERR_WORKER/i,
  };
  const matches = Object.entries(patterns).filter(([,pattern]) => pattern.test(log)).map(([name]) => name);
  return matches.length ? matches : [log.trim() ? 'unclassified' : 'empty'];
}
export function command(args, options, logs) {
  return new Promise(resolve => {
    const stdout = fs.openSync(logs.stdout,'wx',0o600);
    const stderr = fs.openSync(logs.stderr,'wx',0o600);
    const child = spawn(process.execPath,args,{...options,stdio:['ignore','pipe','pipe']});
    child.stdout.on('data',chunk => fs.writeSync(stdout,chunk));
    child.stderr.on('data',chunk => fs.writeSync(stderr,chunk));
    let spawnError = null;
    child.once('error',error => { spawnError = ['ENOENT','EACCES','EAGAIN'].includes(error.code) ? error.code : 'spawn-error'; });
    // close guarantees the process and its stdio have finished before reading export/logs.
    child.once('close',(code,signal) => {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      resolve({code,signal,spawn_error:spawnError});
    });
  });
}
export async function main(argv = process.argv.slice(2)) {
  let temp;
  let output;
  let stage = "configuration";
  let keepPrivate = false;
  let manifest;
  let toolchain;
  const runs = [];
  const executions = [];
  const options = {mode:'fixture',runs:3};
  try {
    for (let i = 0; i < argv.length; i++) {
      const name = argv[i];
      if (name === '--execute-external-model') options.executeExternal = true;
      else if (name === '--dry-run') options.dryRun = true;
      else if (name === '--execute-local-model') options.execute = true;
      else if (name === '--keep-private-artifacts') keepPrivate = true;
      else {
        if (!['--mode','--runs','--profile','--output','--fixture-variant','--input-price-per-million','--output-price-per-million','--output-tokens-per-call'].includes(name) || !argv[i+1]) throw new Error('arguments');
        const value = argv[++i];
        if (name === '--mode') options.mode = value;
        if (name === '--runs') options.runs = Number(value);
        if (name === '--profile') options.profile = value;
        if (name === '--output') output = path.resolve(value);
        if (name === '--input-price-per-million') options.inputPrice = Number(value);
        if (name === '--output-price-per-million') options.outputPrice = Number(value);
        if (name === '--output-tokens-per-call') options.outputTokens = Number(value);
        if (name === '--fixture-variant') options.variant = value;
      }
    }
    if (!['fixture','local_model','external_model'].includes(options.mode) || !Number.isSafeInteger(options.runs) || options.runs < 1) throw new Error('arguments');
    if (options.dryRun && options.executeExternal) throw new Error('arguments');
    if (options.mode === 'local_model' && (!options.execute || !options.profile)) {
      process.stdout.write('{"status":"NOT RUN","reason":"explicit execution and profile required"}\n');
      return 3;
    }
    if (options.mode === 'external_model' && ((!options.dryRun && !options.executeExternal) || !options.profile)) {
      process.stdout.write('{"status":"NOT RUN","reason":"explicit external execution and profile required"}\n');
      return 3;
    }
    if (options.profile && options.mode === 'external_model' && !path.isAbsolute(options.profile)) throw new Error('profile-path');
    if (options.profile && options.mode === 'local_model') options.profile = path.resolve(options.profile);
    if (options.executeExternal && options.mode !== 'external_model') throw new Error('external-mode');
    if ((options.dryRun || options.inputPrice !== undefined || options.outputPrice !== undefined || options.outputTokens !== undefined)
      && options.mode !== 'external_model') throw new Error('dry-run-mode');
    for (const key of ['inputPrice','outputPrice','outputTokens']) {
      if (options[key] !== undefined && (!Number.isFinite(options[key]) || options[key] < 0)) throw new Error('price-arguments');
    }
    if (options.outputTokens !== undefined && !Number.isSafeInteger(options.outputTokens)) throw new Error('price-arguments');
    if (options.outputPrice !== undefined && options.outputTokens === undefined) throw new Error('output-assumption');
    if (options.mode === 'fixture'  && (options.profile || options.execute)) throw new Error('fixture-profile');
    if (options.variant && (options.mode !== 'fixture' || !['forbidden','missing','error'].includes(options.variant))) throw new Error('fixture-variant');
    const pkg = require('../evals/semantic/node_modules/promptfoo/package.json');
    if (pkg.version !== '0.117.2') throw new Error('promptfoo-version');
    const promptfooRequire = createRequire(require.resolve('../evals/semantic/node_modules/promptfoo/package.json'));
    const sqlite = promptfooRequire('better-sqlite3/package.json');
    if (sqlite.version !== '13.0.3') throw new Error('sqlite-version');
    toolchain = {node:process.versions.node,promptfoo_sqlite:sqlite.version};
    stage = "manifest";
    manifest = python({operation:'manifest',mode:options.mode,profile:options.profile});
    const identity = Object.fromEntries(Object.entries(manifest).filter(([key]) => !['schema_version','commit','case_version','promptfoo_version'].includes(key)));
    temp = fs.mkdtempSync(path.join(os.tmpdir(),'core-answer-'));
    fs.chmodSync(temp,0o700);
    fs.writeFileSync(path.join(temp,'empty.env'),'');
    const env = cleanEnvironment(temp,options);
    if (options.dryRun) {
      stage = 'dry-run';
      const counts = python({operation:'dry-run',profile:options.profile},env);
      const report = dryRunReport(manifest,counts,options);
      const encoded = JSON.stringify(report,null,2)+'\n';
      if (output) fs.writeFileSync(output,encoded,{mode:0o600});
      else process.stdout.write(encoded);
      return 0;
    }
    for (let n = 1; n <= options.runs; n++) {
      const raw = path.join(temp,`run-${n}.json`);
      const logs = {stdout:path.join(temp,`run-${n}.stdout`),stderr:path.join(temp,`run-${n}.stderr`)};
      stage = "promptfoo-export";
      const result = await command([
        '--require',path.join(directory,'network_guard.cjs'),
        path.join(directory,'node_modules/promptfoo/dist/src/main.js'),'eval',
        '--config',path.join(directory,'config.cjs'),'--no-cache','--no-progress-bar',
        '--env-file',path.join(temp,'empty.env'),'--output',raw,
      ],{cwd:temp,env},logs);
      const diagnostic = {run:n,attempts:1,exit_code:result.code,signal:result.signal,spawn_error:result.spawn_error,
        raw_present:fs.existsSync(raw),
        stdout_classes:classifyLog(fs.readFileSync(logs.stdout,'utf8')),
        stderr_classes:classifyLog(fs.readFileSync(logs.stderr,'utf8'))};
      executions.push(diagnostic);
      // Assertion failures can be quality failures; execution errors cannot.
      if (![0,100].includes(result.code) || !diagnostic.raw_present) {
        throw new Error('promptfoo-export');
      }
      stage = "report-gate";
      runs.push(inspectReport(JSON.parse(fs.readFileSync(raw,'utf8')),{mode:options.mode,identity}));
      process.stderr.write(`answer ${options.mode} run ${n}: ${runs.at(-1).passed ? 'PASS' : 'FAIL'} (${runs.at(-1).cases.length} cases)\n`);
    }
    const report = {...finalizeReport(manifest,runs,options.runs),toolchain,executions,...(options.mode === 'external_model' ? {run_count:options.runs} : {})};
    const encoded = JSON.stringify(report,null,2)+'\n';
    if (output) fs.writeFileSync(output,encoded,{mode:0o600});
    else process.stdout.write(encoded);
    return report.passed ? 0 : 1;
  } catch (error) {
    // Reasons are structural identifiers only; never print raw SDK or promptfoo text.
    const safeReasons = new Set(['arguments','fixture-profile','fixture-variant','promptfoo-version','sqlite-version','promptfoo-export','credential-name','credentials-unavailable','profile-path','external-mode','dry-run-mode','price-arguments','output-assumption','dry-run-count','codex-version','export-version','provider-config','vars','assertion-config','components','grading','configured-count','row-count','case-set','prompt','provider','provider-path','provider-drift','row-error','response','critical-count','output','identity-required','observation-id','forged-grading','forged-row','stats','runs','manifest','nonfinite']);
    const reason = safeReasons.has(error?.message) ? error.message : 'inconsistent-data';
    if (executions.length) process.stderr.write(`promptfoo diagnostic: ${JSON.stringify(executions.at(-1))}\n`);
    if (output && manifest) fs.writeFileSync(output,JSON.stringify({...manifest,toolchain,runs,executions,
      ...(options.dryRun ? {dry_run:true,external_send:false,quality_evidence:false} : {}),
      passed:false,failure:{stage,reason}},null,2)+'\n',{mode:0o600});
    process.stderr.write(`回答評価 FAIL (${stage}: ${reason})。\n`);
    return 1;
  } finally {
    if (temp && keepPrivate) process.stderr.write(`private artifacts (0700): ${temp}\n`);
    else if (temp) fs.rmSync(temp,{recursive:true,force:true});
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = await main();

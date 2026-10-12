import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { python } = require('./bridge.cjs');
const directory = path.dirname(fileURLToPath(import.meta.url));
const ids = JSON.parse(fs.readFileSync(path.join(directory,'cases.json'),'utf8')).cases.map(c => c.id);
const providerPath = path.join(directory,'provider.py');
const assertionPath = path.join(directory,'assertions.cjs');
function demand(value, reason) { if (!value) throw new Error(reason); }
function finite(value) {
  if (typeof value === 'number') demand(Number.isFinite(value),'nonfinite');
  else if (value && typeof value === 'object') Object.values(value).forEach(finite);
}
function vars(value) {
  demand(value && Object.keys(value).join() === 'case_id' && typeof value.case_id === 'string','vars');
  return value.case_id;
}
function specification(value) {
  return value?.type === 'javascript' && value.metric === 'core-answer'
    && value.value === `file://${assertionPath}`;
}
function critical(specs) {
  demand(Array.isArray(specs) && specs.length === 1 && specification(specs[0]),'assertion-config');
}
function components(result, found = []) {
  demand(result && typeof result.pass === 'boolean' && Number.isFinite(result.score)
    && result.score >= 0 && result.score <= 1,'grading');
  if (specification(result.assertion)) found.push(result);
  if (result.componentResults !== undefined) {
    demand(Array.isArray(result.componentResults) && result.componentResults.length > 0,'components');
    result.componentResults.forEach(r => components(r, found));
  }
  return found;
}
export function inspectReport(raw, options) {
  finite(raw);
  demand(raw?.results?.version === 3 && Array.isArray(raw.results.results),'export-version');
  const config = raw.config;
  demand(config && config.sharing === false && config.providers?.length === 1,'provider-config');
  const provider = config.providers[0];
  demand(provider.id === `file://${providerPath}` && provider.label === 'core-answer'
    && provider.config?.mode === options.mode,'provider-config');
  assert.deepEqual(config.prompts,['{{case_id}}']);
  critical(config.defaultTest?.assert);
  demand(config.tests?.length === ids.length,'configured-count');
  assert.deepEqual(config.tests.map(t => vars(t.vars)), ids);
  const rows = raw.results.results;
  demand(rows.length === ids.length && rows.length > 0,'row-count');
  const seen = new Set();
  const observations = [];
  const grades = [];
  let actualProvider;
  let codexVersion = null;
  for (const r of rows) {
    const id = vars(r.vars);
    demand(ids.includes(id) && !seen.has(id) && vars(r.testCase?.vars) === id,'case-set');
    seen.add(id);
    demand(r.prompt?.raw === id,'prompt');
    demand(r.provider?.label === 'core-answer','provider');
    const accepted = [`file://${providerPath}`,`python:${providerPath}:default`,`python:provider.py:default`];
    demand(accepted.includes(r.provider.id),'provider-path');
    actualProvider ??= r.provider.id;
    demand(actualProvider === r.provider.id,'provider-drift');
    demand(typeof r.success === 'boolean' && (r.failureReason === 0 || r.failureReason === 1)
      && (r.error === undefined || r.error === null || (r.failureReason === 1
        && r.error === r.gradingResult?.reason && r.error === 'deterministic-answer'))
      && Number.isFinite(r.score)
      && r.skipped !== true && r.skip !== true
      && !['NOT_RUN','SKIP','SKIPPED'].includes(r.status),'row-error');
    demand(r.response && !r.response.error && r.response.cached === false
      && Object.hasOwn(r.response,'output'),'response');
    critical(r.testCase.assert);
    const results = components(r.gradingResult);
    demand(results.length === 1,'critical-count');
    const parsed = typeof r.response.output === 'string' ? JSON.parse(r.response.output) : r.response.output;
    demand(parsed && Object.keys(parsed).sort().join() === 'identity,observation','output');
    demand(options.identity,'identity-required');
    if (options.mode === 'external_model' && options.identity.provider === 'codex_subscription') {
      const version = parsed.identity?.codex_cli_version;
      demand(version === null || (typeof version === 'string' && /^\d+\.\d+\.\d+$/.test(version)), 'codex-version');
      demand(parsed.observation?.dispatched === (version !== null), 'codex-version');
      if (version !== null) {
        const parts = version.split('.').map(Number);
        demand(parts[0] > 0 || parts[1] > 152 || (parts[1] === 152 && parts[2] >= 0), 'codex-version');
        codexVersion ??= version;
        demand(codexVersion === version, 'codex-version');
      }
      assert.deepEqual({...parsed.identity,codex_cli_version:null},options.identity);
    } else assert.deepEqual(parsed.identity,options.identity);
    demand(parsed.observation?.id === id,'observation-id');
    observations.push(parsed.observation);
    grades.push([r, results[0]]);
  }
  const run = python({operation:'batch',observations});
  for (let i = 0; i < rows.length; i++) {
    const [r, grading] = grades[i];
    const checked = run.cases[i];
    assert.deepEqual(grading.metadata, checked);
    for (const result of [r.gradingResult, grading]) {
      demand(result.pass === checked.passed && result.score === Number(checked.passed),'forged-grading');
    }
    demand(r.success === checked.passed && r.score === Number(checked.passed)
      && r.failureReason === (checked.passed ? 0 : 1),'forged-row');
  }
  const stats = raw.results.stats;
  demand(stats && stats.errors === 0 && stats.successes === rows.filter(r => r.success).length
    && stats.failures === rows.filter(r => !r.success).length,'stats');
  if (options.mode === 'external_model' && options.identity.provider === 'codex_subscription') {
    demand(codexVersion !== null, 'codex-version');
    return {...run,codex_cli_version:codexVersion};
  }
  return run;
}

export function finalizeReport(manifest, runs, expectedRuns) {
  demand(Number.isSafeInteger(expectedRuns) && expectedRuns > 0 && runs.length === expectedRuns,'runs');
  demand(manifest.promptfoo_version === '0.117.2'
    && manifest.quality_evidence === (['local_model','external_model'].includes(manifest.mode)),'manifest');
  finite(runs);
  if (manifest.mode === 'external_model' && manifest.provider === 'codex_subscription') {
    const version = runs[0]?.codex_cli_version;
    demand(typeof version === 'string' && /^\d+\.\d+\.\d+$/.test(version)
      && runs.every(r => r.codex_cli_version === version), 'codex-version');
    return {...manifest,codex_cli_version:version,runs,passed:runs.every(r => r.passed)};
  }
  return {...manifest, runs, passed:runs.every(r => r.passed)};
}

// 起動済みdevへの操作試験。模擬マイクを使用し、実マイク受入とは区別する。
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || '@playwright/test');
const fs = require('node:fs');

async function main() {
  const cycles = Number(process.env.VOICE_OPERATION_CYCLES || 8);
  if (!Number.isInteger(cycles) || cycles < 1 || cycles > 32) throw new Error('cycles must be 1..32');
  const browser = await chromium.launch({
    channel: 'chrome', headless: process.env.HEADED !== '1',
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
  });
  const context = await browser.newContext({
    permissions: ['microphone'], baseURL: process.env.DEV_URL || 'http://localhost:5173',
  });
  const page = await context.newPage();
  const sessions = new Set();
  const result = {cycles, passed: false, failures: [], events: [], playbacks: [], actions: [], playing: 0};
  page.on('response', async response => {
    if (response.url().endsWith('/voice/livekit/token') && response.ok()) {
      const value = await response.json();
      if (value.session_id) sessions.add(value.session_id);
    }
  });
  await page.addInitScript(() => {
    window.__operationProbe = {failures: [], events: [], playbacks: [], actions: [], playing: 0};
    window.__digitalSoulsVoiceSessionTestPort = {
      observeRoom: row => {
        if (row.failureStage) window.__operationProbe.failures.push({stage: row.failureStage, reason: row.failureReason});
        if (row.packetPlaybackObservation) window.__operationProbe.playing++;
        if (row.playbackCompletedResponseId) window.__operationProbe.playbacks.push(row.playbackCompletedResponseId);
      },
      receiveCoreEvent: event => window.__operationProbe.events.push({type: event.type, responseId: event.response_id}),
    };
  });
  try {
    await page.goto('/');
    await page.getByRole('button', {name: '新規スレッド（光織）'}).click();
    const mic = page.getByRole('button', {name: /マイクを(オン|オフ)にする/});
    await mic.click();
    await page.waitForFunction(() => document.querySelector('button[aria-label^="マイクを"]')?.getAttribute('aria-pressed') === 'true');
    const input = page.getByLabel('メッセージ', {exact: true});
    await input.fill('夜空を散歩して見つけた星について十文で説明してください。');
    await input.press('Enter');
    for (let index = 0; index < cycles; index++) {
      await page.waitForFunction(n => window.__operationProbe.playing > n || window.__operationProbe.failures.length > 0, index, {timeout: 90000});
      if (await page.evaluate(() => window.__operationProbe.failures.length)) break;
      if (index % 2 === 0) {
        await mic.click();
        await page.waitForTimeout(200);
        await mic.click();
      }
      await input.evaluate(element => element.blur());
      await page.waitForTimeout(300);
      await input.fill(index === cycles - 1
        ? 'おやすみなさい、と一言だけ返してください。'
        : '続けて、星について十文で説明してください。');
      await page.waitForTimeout(1200);
      await input.press('Enter');
      await page.evaluate(() => window.__operationProbe.actions.push('text_submitted_during_playback'));
      await page.waitForTimeout(2500);
    }
    // 最終応答そのものの出力完了を待つ。以前の完了やreadyだけを合格にしない。
    await page.waitForFunction(n => {
      const state = window.__operationProbe;
      const last = state.events.filter(event => event.type === 'response_started').at(-1)?.responseId;
      return state.failures.length > 0 || (state.playing >= n + 1 && state.playbacks.includes(last));
    }, cycles, {timeout: 90000});
    Object.assign(result, await page.evaluate(() => window.__operationProbe));
    const last = result.events.filter(event => event.type === 'response_started').at(-1)?.responseId;
    result.passed = result.failures.length === 0 && result.actions.length === cycles
      && result.playing >= cycles + 1 && result.playbacks.includes(last);
  } catch (error) {
    Object.assign(result, await page.evaluate(() => window.__operationProbe).catch(() => ({})));
    result.error = error.message.split('\n')[0];
  } finally {
    // この試験が作った音声sessionだけ終了する。利用者のsessionや履歴は削除しない。
    try {
      for (const id of sessions) {
        const response = await context.request.delete(`${process.env.DEV_API_URL || 'http://localhost:8000'}/voice/livekit/sessions/${id}`);
        if (!response.ok()) {result.passed = false; result.cleanupFailed = true;}
      }
    } finally {
      await browser.close();
      fs.writeFileSync(process.argv[2] || 'voice-operations.json', JSON.stringify(result, null, 2));
      console.log(JSON.stringify({passed: result.passed, cycles, operations: result.actions.length,
        playbackStarts: result.playing, playbackCompletions: result.playbacks.length, failures: result.failures, error: result.error}));
      if (!result.passed) process.exitCode = 1;
    }
  }
}
main().catch(error => {console.error(error.message); process.exitCode = 1;});

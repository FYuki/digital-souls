import type {Page, TestInfo} from '@playwright/test'

// 準備全体の待機は計測側でも固定時間で打ち切らない。
// 個別処理の失敗は製品のalertから検出し、準備以外のtest deadlineは維持する。
export const waitForVoicePreparation = async (page: Page, testInfo: TestInfo): Promise<void> => {
  const originalTimeout = testInfo.timeout
  const waitStarted = performance.now()
  testInfo.setTimeout(0)
  try {
    const result = await page.waitForFunction(() => {
      if (document.querySelector('.application-error[role="alert"]')) return 'error'
      const button = document.querySelector<HTMLButtonElement>('button[aria-label^="マイクを"]')
      return button?.getAttribute('aria-pressed') === 'true' && button.classList.contains('mic-standby')
        ? 'ready' : false
    }, undefined, {timeout: 0})
    if (await result.jsonValue() !== 'ready') throw new Error('voice_preparation_failed')
  } finally {
    testInfo.setTimeout(originalTimeout === 0 ? 0 : originalTimeout + Math.ceil(performance.now() - waitStarted))
  }
}

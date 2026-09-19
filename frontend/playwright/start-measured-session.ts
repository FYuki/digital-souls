import type {Locator, Page, Response, TestInfo} from '@playwright/test'
import {waitForVoicePreparation} from './wait-for-preparation'

export type IssuedSession = {sessionId: string; reconnectGraceMs: number; httpStatus: 200}

export const isIssuedSessionResponse = (response: Response): boolean =>
  response.request().method() === 'POST'
  && new URL(response.url()).pathname.endsWith('/voice/livekit/token')
  && response.status() === 200

// 202は準備中。200の識別子だけを取り出し、認証token・URL・本文は証跡に渡さない。
// 発話受付前に失敗しても、発行済みSessionを終了確認できるよう先に通知する。
export const startMeasuredSession = async (
  page: Page, microphone: Locator, testInfo: TestInfo,
  onIssued: (session: IssuedSession) => void,
): Promise<void> => {
  const issued = page.waitForResponse(isIssuedSessionResponse, {timeout: 0})
    .then(async response => {
      const {session_id: sessionId, reconnect_grace_ms: reconnectGraceMs} =
        await response.json() as {session_id?: unknown; reconnect_grace_ms?: unknown}
      if (typeof sessionId !== 'string'
        || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(sessionId)
        || typeof reconnectGraceMs !== 'number' || !Number.isInteger(reconnectGraceMs)
        || reconnectGraceMs < 1 || reconnectGraceMs > 120_000) {
        throw new Error('session_creation_identity_unavailable')
      }
      onIssued({sessionId, reconnectGraceMs, httpStatus: 200})
    })
    .catch(() => { throw new Error('session_creation_identity_unavailable') })
  // 待機とclickを同時に監視し、準備エラー時に200を待ち続けない。
  // callerのfinallyでpageを閉じると、残ったresponse待機も終了する。
  await Promise.all([issued, (async () => {
    await microphone.click()
    await waitForVoicePreparation(page, testInfo)
  })()])
}

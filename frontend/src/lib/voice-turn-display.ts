import type {ConversationTurn} from './conversations/types'

export type VoiceTurnDisplay = {
  responseId: string | null; historyTurnId?: string
  userContent: string; assistantContent: string
}
export type SettledVoiceTurnDisplay = VoiceTurnDisplay & {
  historyTurnId: string; responseId: string; terminal: 'completed' | 'cancelled'
}
export type FailedVoiceTurnDisplay = {
  responseId: string; userContent: string; assistantContent: string
}
type DisplayTurn = {
  key: string; kind: 'content'; userContent: string; assistantContent: string
  turnId?: string; liveResponseId?: string; historyTurnId?: string; failedResponseId?: string
  voicePending: boolean; label: string
} | {key: string; kind: 'privacy'; turnId: string; reason: string}

// 同じ保存turnの本文を、生成中・履歴取得待ち・保存済みで同じkeyとDOMに保つ。
export function voiceDisplayTurns(turns: ConversationTurn[], failed: FailedVoiceTurnDisplay[],
  settled: SettledVoiceTurnDisplay[], live: VoiceTurnDisplay | null): DisplayTurn[] {
  const rows: DisplayTurn[] = turns.map(turn => turn.kind === 'content'
    ? {key: `turn:${turn.turn_id}`, kind: 'content', userContent: turn.user_content,
      assistantContent: turn.assistant_content, turnId: turn.turn_id, historyTurnId: turn.turn_id,
      voicePending: false, label: ''}
    : {key: `turn:${turn.turn_id}`, kind: 'privacy', turnId: turn.turn_id, reason: turn.reason_code})
  rows.push(...failed.map(turn => ({key: `failed:${turn.responseId}`, kind: 'content' as const,
    userContent: turn.userContent, assistantContent: turn.assistantContent || '応答を完了できませんでした。',
    failedResponseId: turn.responseId, voicePending: false, label: '応答失敗'})))
  for (const turn of settled) {
    const key = `turn:${turn.historyTurnId}`
    if (!rows.some(row => row.key === key)) rows.push({key, kind: 'content',
      userContent: turn.userContent, assistantContent: turn.assistantContent,
      turnId: turn.historyTurnId, liveResponseId: turn.responseId, voicePending: true,
      label: turn.terminal === 'cancelled' ? '中断' : '応答完了'})
  }
  if (live !== null) {
    const key = live.historyTurnId === undefined ? `live:${live.responseId ?? 'pending'}` : `turn:${live.historyTurnId}`
    const row: DisplayTurn = {key, kind: 'content', userContent: live.userContent, assistantContent: live.assistantContent,
      turnId: live.historyTurnId, liveResponseId: live.responseId ?? undefined, voicePending: true, label: '応答中'}
    const index = rows.findIndex(item => item.key === key)
    // 保存禁止の履歴を、まだ到着しているライブ本文で上書きしない。
    if (index < 0) rows.push(row)
    else if (rows[index].kind !== 'privacy') rows[index] = row
  }
  return rows
}

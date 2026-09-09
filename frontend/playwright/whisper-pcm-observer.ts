// 専用診断のNode側からだけ操作する。アプリへ本文や追加の制御eventを送らない。
const enabled = () => process.env.VOICE_QUALITY_OBSERVE_STT_PCM === '1'
const base = 'http://127.0.0.1:50023'

export async function selectPcmFixture(fixtureSha256: string, trialOrdinal: number,
  phase: 'initial' | 'labeled'): Promise<void> {
  if (!enabled()) return
  const response = await fetch(`${base}/fixture`, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({fixture_sha256: fixtureSha256, trial_ordinal: trialOrdinal, phase}),
    signal: AbortSignal.timeout(10_000)})
  if (!response.ok) throw new Error('PCM observer fixture selection failed')
}

export async function snapshotPcmInputs(trialOrdinal: number): Promise<unknown> {
  if (!enabled()) return undefined
  const response = await fetch(`${base}/observations`, {signal: AbortSignal.timeout(10_000)})
  if (!response.ok) throw new Error('PCM observer snapshot unavailable')
  const result = await response.json() as {scope: string; rows: {trial_ordinal: number | null}[];
    active_requests: number; overflow: boolean}
  return {...result, rows: result.rows.filter(row => row.trial_ordinal === trialOrdinal)}
}

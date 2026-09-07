import type {ReconnectContext, ReconnectPolicy} from 'livekit-client'

export type RetryObservation = Readonly<{retryCount: number; elapsedMs: number; delayMs: number | null}>

// 短い断では復旧直後の再試行を数秒先へ送らない。長い断にはbackoff・回数・総時間上限を残す。
export class VoiceReconnectPolicy implements ReconnectPolicy {
  constructor(private readonly random: () => number = Math.random,
    private readonly observe?: (row: RetryObservation) => void) {}

  nextRetryDelayInMs(context: ReconnectContext): number | null {
    const {retryCount, elapsedMs} = context
    if (!Number.isSafeInteger(retryCount) || retryCount < 0 || !Number.isFinite(elapsedMs) || elapsedMs < 0) return null
    let delayMs: number | null = null
    if (retryCount < 40 && elapsedMs < 60000) {
      const jitter = Math.max(0, Math.min(1, this.random()))
      if (Number.isFinite(jitter)) delayMs = retryCount === 0 ? 0
        : Math.min(60000 - elapsedMs, elapsedMs < 10000 ? 250 + jitter * 250 : 1000 + jitter * 500)
    }
    // SDKのserverUrl・例外本文は診断へ渡さない。
    this.observe?.({retryCount, elapsedMs, delayMs})
    return delayMs
  }
}

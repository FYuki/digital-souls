import type { Plugin } from 'vite'

// 開発環境の障害分類とイベント相関を収集する。本文・音声・token・例外本文は送らない。
export function createVoiceDiagnosticsPlugin(): Plugin {
  return {
    name: 'voice-failure-diagnostics',
    configureServer(server) {
      server.ws.on('voice:lifecycle', (data: unknown) => {
        if (!data || typeof data !== 'object') return
        const source = data as Record<string, unknown>
        if (!['incoming', 'outgoing'].includes(String(source.direction))) return
        const row: Record<string, string> = {}
        for (const key of ['direction', 'type', 'reason', 'decision', 'status']) {
          const value = source[key]
          if (typeof value === 'string' && /^[a-z_]{1,64}$/.test(value)) row[key] = value
        }
        for (const key of ['event_id', 'response_id', 'utterance_id', 'input_event_id']) {
          const value = source[key]
          if (typeof value === 'string' && /^[a-f0-9-]{36}$/.test(value)) row[key] = value
        }
        if (row.type) server.config.logger.info(`[voice-lifecycle] ${JSON.stringify(row)}`)
      })
      server.ws.on('voice:failure', (data: unknown) => {
        if (!data || typeof data !== 'object') return
        const {stage, reason} = data as Record<string, unknown>
        if (typeof stage !== 'string' || !['transport', 'media_decoder', 'audio_graph', 'output_clock', 'renderer', 'rtp_timeline'].includes(stage)
          || typeof reason !== 'string' || !/^[A-Za-z_ ]{1,80}$/.test(reason)) return
        server.config.logger.warn(`[voice-failure] stage=${stage} reason=${reason}`)
      })
    },
  }
}

import type { Plugin } from 'vite'

// 開発環境の障害分類だけを収集する。本文・音声・識別子・例外本文は送らない。
export function createVoiceDiagnosticsPlugin(): Plugin {
  return {
    name: 'voice-failure-diagnostics',
    configureServer(server) {
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

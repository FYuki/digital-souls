// ローカルの2 PeerConnectionで合成toneを送受信し、実APIの観測境界を確認する。
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { transform } from 'esbuild'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const modules = new Map()
for (const name of ['media-observer', 'render-worklet']) {
  const source = await readFile(resolve(root, `src/livekit/${name}.ts`), 'utf8')
  modules.set(`/${name}.js`, (await transform(source, { loader: 'ts', format: 'esm' })).code)
}
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
const trials = []
try {
  for (let index = 0; index < 5; index += 1) {
    const page = await browser.newPage()
    try {
      await page.route('https://voice-boundary.invalid/**', async (route) => {
        const path = new URL(route.request().url()).pathname
        await route.fulfill({
          status: path === '/' || modules.has(path) ? 200 : 404,
          contentType: path === '/' ? 'text/html' : 'text/javascript',
          body: path === '/' ? '<!doctype html><title>音声境界の診断</title>' : modules.get(path) ?? '',
        })
      })
      await page.goto('https://voice-boundary.invalid/')
      const result = await page.evaluate(async () => {
        const { RemoteMediaObserver } = await import('/media-observer.js')
        const { renderWorkletSource, outputFrameTimeMs } = await import('/render-worklet.js')
        const tx = new RTCPeerConnection({ iceServers: [] })
        const rx = new RTCPeerConnection({ iceServers: [] })
        const context = new AudioContext({ sampleRate: 48000 })
        let observer
        let source
        let worklet
        let playbackAt
        let observed = {}
        let originalTrack
        let audioElement
        const oscillator = context.createOscillator()
        const gain = context.createGain()
        const destination = context.createMediaStreamDestination()
        oscillator.frequency.value = 440
        gain.gain.value = .1
        oscillator.connect(gain).connect(destination)
        const moduleUrl = URL.createObjectURL(new Blob([renderWorkletSource], { type: 'text/javascript' }))
        await context.audioWorklet.addModule(moduleUrl)
        URL.revokeObjectURL(moduleUrl)
        rx.ontrack = (event) => {
          originalTrack = event.track
          audioElement = document.createElement('audio')
          audioElement.autoplay = true
          audioElement.muted = true
          audioElement.srcObject = new MediaStream([event.track])
          document.body.append(audioElement)
          observer = new RemoteMediaObserver(event.receiver, event.track, (value) => { observed = value })
          source = context.createMediaStreamSource(new MediaStream([event.track]))
          worklet = new AudioWorkletNode(context, 'render-evidence-processor')
          worklet.port.onmessage = (event) => {
            if (playbackAt === undefined && event.data.firstAudibleFrame !== undefined) {
              playbackAt = outputFrameTimeMs(event.data.firstAudibleFrame, context.sampleRate, context.getOutputTimestamp())
            }
          }
          source.connect(worklet).connect(context.destination)
        }
        const waitUntil = async (condition, timeout = 10000) => {
          const end = performance.now() + timeout
          while (!condition()) {
            if (performance.now() > end) throw new Error('media boundary observation timed out: ' + JSON.stringify({ observed, playbackAt, tx: tx.connectionState, rx: rx.connectionState }))
            await new Promise((resolve) => setTimeout(resolve, 10))
          }
        }
        try {
          await context.resume()
          oscillator.start()
          tx.addTrack(destination.stream.getAudioTracks()[0])
          await tx.setLocalDescription(await tx.createOffer())
          await waitUntil(() => tx.iceGatheringState === 'complete')
          await rx.setRemoteDescription(tx.localDescription)
          await rx.setLocalDescription(await rx.createAnswer())
          await waitUntil(() => rx.iceGatheringState === 'complete')
          await tx.setRemoteDescription(rx.localDescription)
          // 最初のmediaから既知のtoneを送る。Opusのcomfort noiseをtoneの開始と誤認しない。
          await waitUntil(() => observed.firstEncodedFrameAtMs !== undefined
            && observed.firstDecodedSampleAtMs !== undefined && playbackAt !== undefined)
          if (observed.encodedMissingReason || observed.decodedMissingReason) throw new Error('media boundary observer failed')
          const result = { ...observed, firstPlaybackAtMs: playbackAt }
          observer.close()
          if (originalTrack.readyState !== 'live') throw new Error('observer stopped the original track')
          if (result.firstEncodedFrameAtMs > result.firstDecodedSampleAtMs) throw new Error('encoded observation followed decoded audio')
          if (result.firstDecodedSampleAtMs > result.firstPlaybackAtMs) throw new Error('decoded callback arrived after playout')
          return result
        } finally {
          observer?.close()
          if (audioElement) { audioElement.srcObject = null; audioElement.remove() }
          oscillator.disconnect()
          gain.disconnect()
          source?.disconnect()
          worklet?.disconnect()
          for (const track of destination.stream.getTracks()) track.stop()
          tx.close()
          rx.close()
          await context.close()
        }
      })
      trials.push(result)
    } finally { await page.close() }
  }
  const output = resolve(root, 'test-results/media-boundaries/loopback.json')
  await mkdir(dirname(output), { recursive: true })
  await writeFile(output, JSON.stringify({
    scope: 'chromium_webrtc_loopback_diagnostic', browser: browser.version(),
    source: 'synthetic_440hz_tone', trials,
  }, null, 2) + '\n')
  console.log(JSON.stringify({ trials: trials.length, output }))
} finally { await browser.close() }

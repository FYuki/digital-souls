// 無音→toneを送る実WebRTCで、RTP metadataとdecoded frame clockの関係を調べる。
import { chromium } from 'playwright'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
try {
  const page = await browser.newPage()
  await page.route('https://voice-timing.invalid/', route => route.fulfill({ contentType: 'text/html', body: '<title>音声clock診断</title>' }))
  await page.goto('https://voice-timing.invalid/')
  const trial = await page.evaluate(async () => {
    const tx = new RTCPeerConnection({ iceServers: [] })
    const rx = new RTCPeerConnection({ iceServers: [] })
    const context = new AudioContext({ sampleRate: 48000 })
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    const destination = context.createMediaStreamDestination()
    const encoded = [], decoded = [], synchronization = []
    let worker, receiver, clone, reader, element, trackReceivedAt, syncTimer
    let lastRtpTimestamp
    const source = `self.onrtctransform = event => {
      const t = event.transformer; let count = 0;
      t.readable.pipeThrough(new TransformStream({ transform(frame, controller) {
        const row = {atMs: performance.timeOrigin + performance.now() - t.options.origin,
          timestamp: frame.timestamp, bytes: frame.data.byteLength, metadata: frame.getMetadata()};
        if (typeof row.metadata.receiveTime === 'number') row.receiveAtMs = performance.timeOrigin + row.metadata.receiveTime - t.options.origin;
        controller.enqueue(frame);
        if (count++ < 500) self.postMessage(row);
      }})).pipeTo(t.writable).catch(() => {});
    }`
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
    const until = async predicate => {
      const end = performance.now() + 10000
      while (!predicate()) { if (performance.now() > end) throw new Error('peer setup timed out'); await wait(10) }
    }
    rx.ontrack = event => {
      trackReceivedAt = performance.now()
      receiver = event.receiver
      syncTimer = setInterval(() => {
        for (const value of receiver.getSynchronizationSources()) {
          if (value.rtpTimestamp !== lastRtpTimestamp && synchronization.length < 1000) {
            synchronization.push({ sampledAtMs: performance.now(), ...value })
            lastRtpTimestamp = value.rtpTimestamp
          }
        }
      }, 2)
      const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
      worker = new Worker(url)
      URL.revokeObjectURL(url)
      worker.onmessage = event => encoded.push(event.data)
      receiver.transform = new RTCRtpScriptTransform(worker, { origin: performance.timeOrigin })
      element = document.createElement('audio')
      element.autoplay = true; element.muted = true
      element.srcObject = new MediaStream([event.track]); document.body.append(element)
      clone = event.track.clone()
      reader = new MediaStreamTrackProcessor({ track: clone }).readable.getReader()
      void (async () => {
        while (true) {
          const {value: frame, done} = await reader.read()
          if (done) return
          try {
            const samples = new Float32Array(frame.numberOfFrames)
            frame.copyTo(samples, {planeIndex: 0, format: 'f32-planar'})
            if (decoded.length < 1000) decoded.push({atMs: performance.now(), timestamp: frame.timestamp,
              samples: frame.numberOfFrames, sampleRate: frame.sampleRate,
              synchronization: receiver.getSynchronizationSources(),
              peak: samples.reduce((peak, sample) => Math.max(peak, Math.abs(sample)), 0)})
          } finally { frame.close() }
        }
      })().catch(() => {})
    }
    try {
      gain.gain.value = 0
      oscillator.frequency.value = 440
      oscillator.connect(gain).connect(destination)
      oscillator.start()
      await context.resume()
      tx.addTrack(destination.stream.getAudioTracks()[0])
      await tx.setLocalDescription(await tx.createOffer())
      await until(() => tx.iceGatheringState === 'complete')
      await rx.setRemoteDescription(tx.localDescription)
      await rx.setLocalDescription(await rx.createAnswer())
      await until(() => rx.iceGatheringState === 'complete')
      await tx.setRemoteDescription(rx.localDescription)
      await until(() => decoded.length > 10)
      await wait(1000)
      const toneStartRequestedAt = performance.now()
      gain.gain.setValueAtTime(.1, context.currentTime)
      await wait(500)
      const toneStopRequestedAt = performance.now()
      gain.gain.setValueAtTime(0, context.currentTime)
      await wait(500)
      return { timeOrigin: performance.timeOrigin, trackReceivedAt, toneStartRequestedAt, toneStopRequestedAt, encoded, decoded, synchronization }
    } finally {
      clearInterval(syncTimer)
      if (receiver) receiver.transform = null
      worker?.terminate(); await reader?.cancel(); clone?.stop()
      if (element) { element.srcObject = null; element.remove() }
      tx.close(); rx.close()
      for (const track of destination.stream.getTracks()) track.stop()
      await context.close()
    }
  })
  const output = resolve(process.argv[2] ?? resolve(root, 'test-results/media-boundaries/rtp-timing.json'))
  await mkdir(dirname(output), { recursive: true })
  await writeFile(output, JSON.stringify({ scope: 'synthetic_webrtc_clock_diagnostic', browser: browser.version(), trial }, null, 2) + '\n', { flag: 'wx' })
  const firstSignal = trial.encoded.find(row => row.metadata.audioLevel > 0)
  console.log(JSON.stringify({ output, encoded: trial.encoded.length, decoded: trial.decoded.length,
    synchronization: trial.synchronization.length, firstSynchronization: trial.synchronization[0],
    firstEncoded: trial.encoded[0], firstSignal, firstDecoded: trial.decoded[0], toneStartRequestedAt: trial.toneStartRequestedAt }))
} finally { await browser.close() }

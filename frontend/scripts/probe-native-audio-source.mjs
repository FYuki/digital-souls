// Python SDKの実AudioSourceを受信し、入力前packetと独立Opus decodeを観測する。
import { chromium } from 'playwright'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { createInterface } from 'node:readline'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const lines = createInterface({ input: process.stdin })
const input = lines[Symbol.asyncIterator]()
const initial = await input.next()
if (initial.done) throw new Error('probe configuration missing')
const configuration = JSON.parse(initial.value)
const emit = row => process.stdout.write(JSON.stringify(row) + '\n')
const server = createServer((_request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  response.end('<title>音声source診断</title>')
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
try {
  const page = await browser.newPage()
  await page.goto(`http://127.0.0.1:${server.address().port}/`)
  await page.addScriptTag({ content: await readFile(resolve(root, 'node_modules/livekit-client/dist/livekit-client.umd.js'), 'utf8') })
  await page.exposeFunction('sourceProbeEvent', emit)
  await page.evaluate(async config => {
    const { Room, RoomEvent, Track } = window.LivekitClient
    const room = new Room()
    const rows = {}, workers = [], receivers = [], elements = [], nodes = {}
    const context = new AudioContext({sampleRate: 48000})
    const renderSource = `class PacketRenderer extends AudioWorkletProcessor {
      constructor() {
        super(); this.queue = []; this.offset = 0;
        this.port.onmessage = ({data}) => {
          if (this.queue.length >= 200) { this.port.postMessage({kind: 'overflow'}); return; }
          this.queue.push(data);
        };
      }
      process(_inputs, outputs) {
        const out = outputs[0][0]; let target = 0;
        while (target < out.length && this.queue.length) {
          const chunk = this.queue[0];
          if (this.offset === 0) this.port.postMessage({kind: 'rendered', packetIndex: chunk.packetIndex, chunkTimestamp: chunk.chunkTimestamp,
            firstFrame: currentFrame + target, samples: chunk.samples.length});
          const count = Math.min(out.length - target, chunk.samples.length - this.offset);
          out.set(chunk.samples.subarray(this.offset, this.offset + count), target);
          this.offset += count; target += count;
          if (this.offset === chunk.samples.length) {
            this.port.postMessage({kind: 'completed', packetIndex: chunk.packetIndex, chunkTimestamp: chunk.chunkTimestamp,
              endFrame: currentFrame + target});
            this.queue.shift(); this.offset = 0;
          }
        }
        return true;
      }
    }
    registerProcessor('packet-renderer', PacketRenderer);`
    const renderUrl = URL.createObjectURL(new Blob([renderSource], {type: 'text/javascript'}))
    try { await context.audioWorklet.addModule(renderUrl) } finally { URL.revokeObjectURL(renderUrl) }
    await context.resume()
    const source = `self.onmessage = event => {
      if (event.data.kind === 'clock') self.postMessage({kind: 'clock', lower: event.data.lower,
        workerNow: performance.now(), workerOrigin: performance.timeOrigin});
    };
    self.onrtctransform = async event => {
      const t = event.transformer; let index = 0; let decoder; let decodingIndex = null;
      const config = {codec: 'opus', sampleRate: 48000, numberOfChannels: 1};
      try {
        if (typeof AudioDecoder === 'undefined' || !(await AudioDecoder.isConfigSupported(config)).supported) {
          self.postMessage({kind: 'decoder_unavailable'});
        } else {
          decoder = new AudioDecoder({output: frame => {
            try {
              const samples = new Float32Array(frame.numberOfFrames);
              frame.copyTo(samples, {planeIndex: 0, format: 'f32-planar'});
              self.postMessage({kind: 'decoded', packetIndex: decodingIndex, chunkTimestamp: frame.timestamp,
                originBasedAtMs: performance.timeOrigin + performance.now() - t.options.origin, workerNow: performance.now(),
                sampleRate: frame.sampleRate, frames: frame.numberOfFrames, channels: frame.numberOfChannels,
                peak: samples.reduce((p, value) => Math.max(p, Math.abs(value)), 0),
                firstNonzero: samples.findIndex(value => value !== 0), samples}, [samples.buffer]);
            } finally { frame.close(); }
          }, error: error => self.postMessage({kind: 'decoder_error', name: error.name})});
          decoder.configure(config);
        }
        await t.readable.pipeThrough(new TransformStream({async transform(frame, controller) {
          const metadata = frame.getMetadata(), n = index++;
          const row = {kind: 'packet', index: n, rtpTimestamp: metadata.rtpTimestamp,
            sequenceNumber: metadata.sequenceNumber, audioLevel: metadata.audioLevel,
            payloadType: metadata.payloadType, mimeType: metadata.mimeType,
            workerNow: performance.now(), workerOrigin: performance.timeOrigin, rawReceiveTime: metadata.receiveTime,
            bytes: frame.data.byteLength,
            originBasedReceivedAtMs: performance.timeOrigin + metadata.receiveTime - t.options.origin,
            observedAtMs: performance.timeOrigin + performance.now() - t.options.origin};
          if (decoder?.state === 'configured' && n < 400) {
            try {
              let payload = new Uint8Array(frame.data);
              if (metadata.mimeType === 'audio/red') {
                // RFC 2198: 全headerを読み、冗長blockを飛ばしてprimaryだけをdecodeする。
                let header = 0, redundant = 0;
                while (header < payload.length && payload[header] & 128) {
                  if (header + 4 > payload.length) throw new Error('truncated RED header');
                  redundant += ((payload[header + 2] & 3) << 8) | payload[header + 3];
                  header += 4;
                }
                if (header >= payload.length || header + 1 + redundant >= payload.length) throw new Error('truncated RED primary');
                row.primaryPayloadType = payload[header] & 127;
                payload = payload.subarray(header + 1 + redundant);
              } else if (metadata.mimeType !== 'audio/opus') throw new Error('unsupported codec');
              row.primaryBytes = payload.byteLength;
              // 1packetずつflushし、output callbackを現在の入力packetへ対応させる。
              // AudioData.timestampはdecoderが再構成するため識別子に使わない。
              decodingIndex = n;
              decoder.decode(new EncodedAudioChunk({type: 'key', timestamp: n * 20000, data: payload}));
              await decoder.flush();
              decodingIndex = null;
            }
            catch (error) { self.postMessage({kind: 'decode_call_error', name: error.name}); }
          }
          controller.enqueue(frame);
          if (n < 400) self.postMessage(row);
        }})).pipeTo(t.writable);
      } catch { self.postMessage({kind: 'observer_error'}); }
    }`
    room.on(RoomEvent.TrackSubscribed, (track, publication) => {
      if (track.kind !== Track.Kind.Audio || !['buffered', 'direct'].includes(publication.trackName)) return
      const name = publication.trackName
      rows[name] = { packets: [], decoded: [], errors: [], subscribedAtMs: performance.now(), codecs: track.receiver.getParameters().codecs, mainTimeOrigin: performance.timeOrigin, clocks: [], rendered: [], completed: [] }
      const node = new AudioWorkletNode(context, 'packet-renderer', {numberOfInputs: 0,
        numberOfOutputs: 1, outputChannelCount: [1]})
      nodes[name] = node
      node.connect(context.destination)
      node.port.onmessage = ({data}) => {
        if (data.kind === 'rendered') {
          const timestamp = context.getOutputTimestamp()
          rows[name].rendered.push({...data, contextSampleRate: context.sampleRate,
            outputTimestamp: timestamp, observedAtMs: performance.now(),
            outputAtMs: timestamp.performanceTime + (data.firstFrame / context.sampleRate - timestamp.contextTime) * 1000})
        } else if (data.kind === 'completed') rows[name].completed.push(data)
        else rows[name].errors.push({kind: data.kind})
      }
      const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
      const worker = new Worker(url)
      URL.revokeObjectURL(url)
      workers.push(worker); receivers.push(track.receiver)
      worker.onmessage = ({ data }) => {
        if (data.kind === 'clock') {
          rows[name].clocks.push({...data, upper: performance.now()})
          if (rows[name].clocks.length < 10) worker.postMessage({kind: 'clock', lower: performance.now()})
        }
        else if (data.kind === 'packet') rows[name].packets.push({...data, mainObservedAtMs: performance.now()})
        else if (data.kind === 'decoded') {
          const {samples, ...observation} = data
          rows[name].decoded.push(observation)
          node.port.postMessage({packetIndex: data.packetIndex, chunkTimestamp: data.chunkTimestamp, samples}, [samples.buffer])
        }
        else rows[name].errors.push({kind: data.kind, name: data.name})
      }
      track.receiver.transform = new RTCRtpScriptTransform(worker, { origin: performance.timeOrigin })
      worker.postMessage({kind: 'clock', lower: performance.now()})
      const element = document.createElement('audio')
      element.autoplay = true; element.muted = true
      element.srcObject = new MediaStream([track.mediaStreamTrack])
      document.body.append(element); elements.push(element)
      window.sourceProbeEvent({ kind: 'subscribed', mode: name })
    })
    window.sourceProbe = {
      snapshot() {
        const outputTimestamp = context.getOutputTimestamp()
        const observedAtMs = performance.now()
        return Object.fromEntries(Object.entries(rows).map(([name, row]) => [name, {...row,
          outputObservation: {outputTimestamp, observedAtMs, sampleRate: context.sampleRate}}]))
      },
      async close() {
        window.sourceProbeEvent({kind: 'stage', stage: 'browser_room_cleanup'})
        for (const receiver of receivers) receiver.transform = null
        for (const worker of workers) worker.terminate()
        for (const element of elements) { element.srcObject = null; element.remove() }
        for (const node of Object.values(nodes)) { node.port.close(); node.disconnect() }
        await context.close()
        await room.disconnect()
      },
    }
    await room.connect(config.url, config.token)
  }, configuration)
  emit({ kind: 'ready', browser: browser.version() })
  while (true) {
    const line = await input.next()
    if (line.done) break
    const command = JSON.parse(line.value)
    if (command.kind === 'snapshot') emit({ kind: 'snapshot', rows: await page.evaluate(() => window.sourceProbe.snapshot()) })
    else if (command.kind === 'close') break
    else throw new Error('unknown probe command')
  }
  await page.evaluate(() => window.sourceProbe.close())
  emit({kind: 'stage', stage: 'browser_room_closed'})
} catch {
  emit({ kind: 'failed' })
  process.exitCode = 1
} finally {
  await browser.close()
  emit({kind: 'stage', stage: 'browser_closed'})
  lines.close()
  server.closeAllConnections()
  await new Promise(resolve => server.close(resolve))
  emit({kind: 'closed'})
}

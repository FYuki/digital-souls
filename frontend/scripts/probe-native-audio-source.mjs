// Python SDKの実AudioSourceを受信し、入力前packetと独立Opus decodeを観測する。
import { chromium } from 'playwright'
import ts from 'typescript'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { createInterface } from 'node:readline'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const decoderModule = ts.transpileModule(await readFile(resolve(root, 'src/livekit/opus-packet-decoder.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
})
const {opusPacketDecoderSource} = await import('data:text/javascript;base64,' + Buffer.from(decoderModule.outputText).toString('base64'))
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
    const rows = {}, workers = [], receivers = [], elements = [], nodes = {}, comparisons = new Map()
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
    const source = `${config.packetDecoderSource}
self.onmessage = event => {
      if (event.data.kind === 'clock') self.postMessage({kind: 'clock', lower: event.data.lower,
        workerNow: performance.now(), workerOrigin: performance.timeOrigin});
    };
    self.onrtctransform = async event => {
      const t = event.transformer; let index = 0; let decoder;
      const inputs = [], serialOutputs = [];
      // 比較用のPCMはworker内だけに保持し、artifactには差分の数値だけを返す。
      const copySamples = frame => {
        const samples = new Float32Array(frame.numberOfFrames);
        frame.copyTo(samples, {planeIndex: 0, format: 'f32-planar'});
        return samples;
      };
      const decodeReference = async (inputPackets, flushEach) => {
        const outputs = []; let failure = null;
        const reference = new AudioDecoder({output: frame => {
          try { outputs.push(copySamples(frame)); } finally { frame.close(); }
        }, error: error => { failure = error; }});
        try {
          reference.configure(config);
          for (let n = 0; n < inputPackets.length; n++) {
            reference.decode(new EncodedAudioChunk({type: 'key', timestamp: n * 20000, data: inputPackets[n]}));
            if (flushEach) await reference.flush();
          }
          await reference.flush();
          if (failure) throw failure;
          return outputs;
        } finally { if (reference.state !== 'closed') reference.close(); }
      };
      const difference = (actual, reference) => {
        if (actual.length !== reference.length || actual.some((chunk, i) => chunk.length !== reference[i].length)) {
          throw new Error('reference output format mismatch');
        }
        let maximum = 0, squares = 0, samples = 0;
        for (let n = 0; n < actual.length; n++) for (let i = 0; i < actual[n].length; i++) {
          const error = actual[n][i] - reference[n][i];
          if (!Number.isFinite(error)) throw new Error('nonfinite reference sample');
          maximum = Math.max(maximum, Math.abs(error)); squares += error * error; samples++;
        }
        return {packets: actual.length, samples, maxAbsoluteError: maximum, rmsError: Math.sqrt(squares / samples)};
      };
      self.addEventListener('message', async ({data}) => {
        if (data.kind !== 'compare') return;
        try {
          // snapshotまでの同じpacketだけを固定し、終了後の追加frameを混ぜない。
          const count = data.count;
          if (!Number.isInteger(count) || count < 1 || count > serialOutputs.length || count > inputs.length) {
            throw new Error('comparison input count unavailable');
          }
          const inputPackets = inputs.slice(0, count), actual = serialOutputs.slice(0, count);
          const batch = await decodeReference(inputPackets, false);
          const flushed = await decodeReference(inputPackets, true);
          self.postMessage({kind: 'comparison', serialVersusBatch: difference(actual, batch),
            flushedVersusBatch: difference(flushed, batch), perPacketFlush: false});
        } catch (error) { self.postMessage({kind: 'comparison_error', name: error.name}); }
      });
      const config = {codec: 'opus', sampleRate: 48000, numberOfChannels: 1};
      try {
        decoder = await OpusPacketDecoder.create(reason => self.postMessage({kind: 'decoder_error', name: reason}));
        await t.readable.pipeThrough(new TransformStream({async transform(frame, controller) {
          const metadata = frame.getMetadata(), n = index++;
          const row = {kind: 'packet', index: n, rtpTimestamp: metadata.rtpTimestamp,
            sequenceNumber: metadata.sequenceNumber, audioLevel: metadata.audioLevel,
            payloadType: metadata.payloadType, mimeType: metadata.mimeType,
            workerNow: performance.now(), workerOrigin: performance.timeOrigin, rawReceiveTime: metadata.receiveTime,
            bytes: frame.data.byteLength,
            originBasedReceivedAtMs: performance.timeOrigin + metadata.receiveTime - t.options.origin,
            observedAtMs: performance.timeOrigin + performance.now() - t.options.origin};
          if (decoder && !decoder.failed && n < 400) {
            try {
              const payload = OpusPacketDecoder.primaryPayload(frame.data, metadata.mimeType);
              row.primaryBytes = payload.byteLength;
              inputs.push(payload.slice());
              const decoded = await decoder.decode(payload, n);
              const samples = decoded.samples;
              serialOutputs.push(samples.slice());
              self.postMessage({kind: 'decoded', packetIndex: decoded.packetIndex, chunkTimestamp: decoded.timestamp,
                originBasedAtMs: performance.timeOrigin + decoded.decodedAtWorkerMs - t.options.origin,
                workerNow: decoded.decodedAtWorkerMs, sampleRate: 48000, frames: samples.length, channels: 1,
                peak: samples.reduce((p, value) => Math.max(p, Math.abs(value)), 0),
                firstNonzero: samples.findIndex(value => value !== 0), samples}, [samples.buffer]);
            } catch (error) { self.postMessage({kind: 'decode_call_error', name: error.name}); }
          }
          controller.enqueue(frame);
          if (n < 400) self.postMessage(row);
        }})).pipeTo(t.writable);
      } catch { self.postMessage({kind: 'observer_error'}); }
      finally { decoder?.close(); }
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
      workers.push(worker); worker.probeMode = name; receivers.push(track.receiver)
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
        else if (data.kind === 'comparison' || data.kind === 'comparison_error') {
          const pending = comparisons.get(worker)
          if (pending) { comparisons.delete(worker); clearTimeout(pending.timer); pending.resolve(data) }
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
      async compare(mode, count) {
        const worker = workers.find(worker => worker.probeMode === mode)
        if (!worker) throw new Error('comparison worker missing')
        return await new Promise((resolve, reject) => {
          const timer = setTimeout(() => { comparisons.delete(worker); reject(new Error('comparison timeout')) }, 5000)
          comparisons.set(worker, {resolve, timer})
          worker.postMessage({kind: 'compare', count})
        })
      },
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
  }, {...configuration, packetDecoderSource: opusPacketDecoderSource})
  emit({ kind: 'ready', browser: browser.version() })
  while (true) {
    const line = await input.next()
    if (line.done) break
    const command = JSON.parse(line.value)
    if (command.kind === 'snapshot') emit({ kind: 'snapshot', rows: await page.evaluate(() => window.sourceProbe.snapshot()) })
    else if (command.kind === 'compare') emit({kind: 'comparison', mode: command.mode, result: await page.evaluate(({mode, count}) => window.sourceProbe.compare(mode, count), command)})
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

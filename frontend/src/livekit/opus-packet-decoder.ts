// workerへ埋め込む復号器。途中flushせず、1入力の960sample出力を待って次へ進む。
// PCMとpacket番号を対にし、AudioData.timestampを入力識別子として扱わない。
export const opusPacketDecoderSource = `
class OpusPacketDecoder {
  static async create(onFailure) {
    const config = {codec: 'opus', sampleRate: 48000, numberOfChannels: 1};
    if (typeof AudioDecoder === 'undefined' || !(await AudioDecoder.isConfigSupported(config)).supported) {
      throw new Error('opus_decoder_unavailable');
    }
    return new OpusPacketDecoder(config, onFailure);
  }
  constructor(config, onFailure) {
    this.pending = null; this.failed = false; this.onFailure = onFailure;
    this.decoder = new AudioDecoder({output: frame => this.output(frame), error: () => this.fail('opus_decode_failed')});
    this.decoder.configure(config);
  }
  static primaryPayload(data, mimeType) {
    let payload = new Uint8Array(data);
    if (mimeType === 'audio/red') {
      // RFC 2198のheaderを検証して冗長blockを飛ばす。loss回復は別レイヤーで行う。
      let header = 0, redundant = 0;
      while (header < payload.length && (payload[header] & 128)) {
        if (header + 4 > payload.length) throw new Error('invalid_red_payload');
        redundant += ((payload[header + 2] & 3) << 8) | payload[header + 3]; header += 4;
      }
      if (header >= payload.length || header + 1 + redundant >= payload.length) throw new Error('invalid_red_payload');
      payload = payload.subarray(header + 1 + redundant);
    } else if (mimeType !== 'audio/opus') throw new Error('unsupported_audio_codec');
    // RFC 6716 3.1/3.2: TOCからpacket全体の長さを求め、現在の20ms契約を検証する。
    if (!payload.length) throw new Error('empty_opus_payload');
    const config = payload[0] >> 3, code = payload[0] & 3;
    const frameSamples = config >= 16 ? 120 << (config & 3)
      : config >= 12 ? 480 << (config & 1) : (config & 3) === 3 ? 2880 : 480 << (config & 3);
    const frames = code === 0 ? 1 : code === 3 ? (payload[1] ?? 0) & 63 : 2;
    if (frames * frameSamples !== 960) throw new Error('unsupported_opus_packet_duration');
    return payload.slice();
  }
  decode(payload, packetIndex) {
    if (this.failed || this.decoder.state !== 'configured') return Promise.reject(new Error('opus_decoder_closed'));
    if (this.pending !== null) return Promise.reject(new Error('opus_decode_already_pending'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail('opus_decode_timeout'), 1000);
      this.pending = {packetIndex, resolve, reject, timer};
      try { this.decoder.decode(new EncodedAudioChunk({type: 'key', timestamp: packetIndex * 20000, data: payload})); }
      catch { this.fail('opus_decode_failed'); }
    });
  }
  output(frame) {
    try {
      if (this.failed) return;
      const pending = this.pending;
      if (pending === null || frame.numberOfFrames !== 960 || frame.sampleRate !== 48000 || frame.numberOfChannels !== 1) {
        this.fail('opus_output_format_mismatch'); return;
      }
      const samples = new Float32Array(960);
      frame.copyTo(samples, {planeIndex: 0, format: 'f32-planar'});
      if (samples.some(value => !Number.isFinite(value))) { this.fail('opus_output_nonfinite'); return; }
      this.pending = null; clearTimeout(pending.timer);
      pending.resolve({packetIndex: pending.packetIndex, samples, timestamp: frame.timestamp, decodedAtWorkerMs: performance.now()});
    } catch { this.fail('opus_output_failed'); }
    finally { frame.close(); }
  }
  fail(reason) {
    if (this.failed) return;
    this.failed = true;
    if (this.pending !== null) {
      const pending = this.pending; this.pending = null;
      clearTimeout(pending.timer); pending.reject(new Error(reason));
    }
    if (this.decoder.state !== 'closed') this.decoder.close();
    this.onFailure(reason);
  }
  close() {
    if (this.failed) return;
    this.failed = true;
    if (this.pending !== null) {
      clearTimeout(this.pending.timer); this.pending.reject(new Error('opus_decoder_closed')); this.pending = null;
    }
    if (this.decoder.state !== 'closed') this.decoder.close();
  }
}
`

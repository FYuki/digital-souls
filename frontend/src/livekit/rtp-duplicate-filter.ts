// payloadはworker内だけで短期間比較し、本文やhashを観測へ送らない。
export const rtpDuplicateFilterSource = `
class RtpDuplicateFilter {
  constructor() { this.seen = new Map(); this.timestamps = new Map(); this.bytes = 0; }
  accept(packet, payload) {
    if (!Number.isInteger(packet.sequenceNumber) || packet.sequenceNumber < 0 || packet.sequenceNumber > 65535) {
      return 'sequence_unavailable';
    }
    const key = packet.source + ':' + packet.sequenceNumber;
    const timestampKey = packet.source + ':' + packet.rtpTimestamp;
    const previous = this.seen.get(key);
    if (previous) {
      return previous.rtpTimestamp === packet.rtpTimestamp && previous.payload.length === payload.length
        && previous.payload.every((value, index) => value === payload[index]) ? 'duplicate' : 'conflict';
    }
    // 別連番でも同じ20ms区間を二度再生しない。重複再送とは区別し、応答を中断する。
    if (this.timestamps.has(timestampKey)) return 'overlap';
    const copy = payload.slice();
    this.seen.set(key, {payload: copy, rtpTimestamp: packet.rtpTimestamp, timestampKey});
    this.timestamps.set(timestampKey, key); this.bytes += copy.length;
    while (this.seen.size > 1024 || this.bytes > 524288) {
      const oldest = this.seen.keys().next().value;
      const record = this.seen.get(oldest);
      this.bytes -= record.payload.length;
      this.timestamps.delete(record.timestampKey); this.seen.delete(oldest);
    }
    return 'accepted';
  }
}
`

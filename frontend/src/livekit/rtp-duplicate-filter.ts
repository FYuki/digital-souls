// payloadはworker内だけで短期間比較し、本文やhashを観測へ送らない。
export const rtpDuplicateFilterSource = `
class RtpDuplicateFilter {
  constructor() { this.seen = new Map(); this.bytes = 0; }
  accept(packet, payload) {
    const key = packet.source + ':' + packet.rtpTimestamp;
    const previous = this.seen.get(key);
    if (previous) {
      return previous.length === payload.length && previous.every((value, index) => value === payload[index])
        ? 'duplicate' : 'conflict';
    }
    const copy = payload.slice();
    this.seen.set(key, copy); this.bytes += copy.length;
    while (this.seen.size > 1024 || this.bytes > 524288) {
      const oldest = this.seen.keys().next().value;
      this.bytes -= this.seen.get(oldest).length;
      this.seen.delete(oldest);
    }
    return 'accepted';
  }
}
`

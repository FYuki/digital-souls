// 音声RTPの数値だけを取り出す。IP、SSRC、stats ID、本文を観測eventへ出さない。
export type RtpCounterObservation = Readonly<{
  status: 'measured'
  bytes: number
  packets: number
  lostPackets?: number
}> | Readonly<{
  status: 'missing'
  reason: 'stats_api_unavailable' | 'stats_failed' | 'stats_timeout' | 'stats_unavailable'
    | 'ambiguous_audio_stream' | 'invalid_rtp_counters' | 'counter_regressed' | 'playback_packets_not_yet_reported'
}>

export type NetworkObservation = Readonly<{
  method: 'browser_audio_rtp_counters_v1'
  boundary?: 'response_cancelled'
  uplink: RtpCounterObservation
  downlink: RtpCounterObservation
}>

type StatsSource = { getStats: () => Promise<RTCStatsReport> }
type Snapshot = { id: string; bytes: number; packets: number; lostPackets?: number }
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const missing = (reason: Extract<RtpCounterObservation, { status: 'missing' }>['reason']): RtpCounterObservation => ({status: 'missing', reason})

export class RtpNetworkObserver {
  private readonly senders = new WeakMap<StatsSource, Snapshot>()

  async capture(sender: StatsSource | undefined, receiver: StatsSource | undefined, expectedPackets: number): Promise<NetworkObservation> {
    const [uplink, downlink] = await Promise.all([
      this.read(sender, 'outbound-rtp'), this.read(receiver, 'inbound-rtp', expectedPackets),
    ])
    return {method: 'browser_audio_rtp_counters_v1', uplink, downlink}
  }

  private async read(source: StatsSource | undefined, type: 'outbound-rtp' | 'inbound-rtp', expectedPackets = 0): Promise<RtpCounterObservation> {
    if (!source?.getStats) return missing('stats_api_unavailable')
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const report = await Promise.race([
        source.getStats(),
        new Promise<null>(resolve => {timer = setTimeout(() => resolve(null), 2000)}),
      ])
      if (report === null) return missing('stats_timeout')
      const rows: Record<string, unknown>[] = []
      report.forEach(row => {if (row.type === type && (row.kind ?? row.mediaType) === 'audio') rows.push(row)})
      if (!rows.length) return missing('stats_unavailable')
      if (rows.length !== 1) return missing('ambiguous_audio_stream')
      const row = rows[0]
      const bytes = row[type === 'outbound-rtp' ? 'bytesSent' : 'bytesReceived']
      const packets = row[type === 'outbound-rtp' ? 'packetsSent' : 'packetsReceived']
      const lost = row.packetsLost
      if (typeof row.id !== 'string' || !row.id || !count(bytes) || !count(packets)
        || (type === 'inbound-rtp' && (typeof lost !== 'number' || !Number.isSafeInteger(lost)))) return missing('invalid_rtp_counters')
      if (type === 'inbound-rtp') {
        if (packets + (lost as number) < 0) return missing('invalid_rtp_counters')
        if (!count(expectedPackets) || packets < expectedPackets) return missing('playback_packets_not_yet_reported')
        // RFCの累積lossは重複受信等で負数にもなる。ここでは0に書き換えない。
        return {status: 'measured', bytes, packets, lostPackets: lost as number}
      }
      const current: Snapshot = {id: row.id, bytes, packets}
      const previous = this.senders.get(source)
      if (previous?.id === current.id && (bytes < previous.bytes || packets < previous.packets)) return missing('counter_regressed')
      this.senders.set(source, current)
      return {status: 'measured', bytes: bytes - (previous?.id === current.id ? previous.bytes : 0),
        packets: packets - (previous?.id === current.id ? previous.packets : 0)}
    } catch {
      return missing('stats_failed')
    } finally {
      if (timer !== undefined) clearTimeout(timer)
    }
  }
}

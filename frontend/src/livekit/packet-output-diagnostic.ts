import type {DecodedAudioPacket} from './media-observer'
import type {PacketRenderInterval} from './packet-renderer'

type PacketTimes = Omit<DecodedAudioPacket, 'pcm'>
export type PacketOutputEvidence = Readonly<{
  status: 'captured'; responseId: string; trackSid: string; generation: number
  packet: PacketTimes; interval: PacketRenderInterval; outputAtMs: number
  confirmedAtMs: number; firstAudibleAtMs?: number
}> | Readonly<{status: 'missing'; responseId: string; reason: 'packet_metadata_missing' | 'packet_metadata_overflow'}>

// 明示診断だけで有効化する。PCMを保持せず、出力待ちpacketの数値情報だけを上限付きで保存する。
export class PacketOutputDiagnostic {
  private readonly packets = new Map<number, PacketTimes>()
  constructor(private readonly responseId: string, private readonly trackSid: string,
    private readonly generation: number, private readonly report: (row: PacketOutputEvidence) => void) {}

  receive(packet: DecodedAudioPacket): void {
    if (this.packets.size >= 256) {
      this.report({status: 'missing', responseId: this.responseId, reason: 'packet_metadata_overflow'})
      return
    }
    const {pcm: _pcm, ...metadata} = packet
    this.packets.set(packet.packetIndex, metadata)
  }

  confirm(interval: PacketRenderInterval, outputAtMs: number, confirmedAtMs: number): void {
    const packet = this.packets.get(interval.packetIndex)
    if (!packet || packet.rtpTimestamp !== interval.rtpTimestamp || !packet.receivedAtBoundsMs
      || !packet.decodedAtBoundsMs) {
      this.report({status: 'missing', responseId: this.responseId, reason: 'packet_metadata_missing'})
      return
    }
    this.report({status: 'captured', responseId: this.responseId, trackSid: this.trackSid,
      generation: this.generation, packet, interval: {...interval}, outputAtMs, confirmedAtMs,
      ...(interval.firstAudibleFrame === undefined ? {} : {
        firstAudibleAtMs: outputAtMs + (interval.firstAudibleFrame - interval.startFrame) / 48})})
    if (interval.packetSampleOffset + interval.endFrame - interval.startFrame === 960) {
      this.packets.delete(interval.packetIndex)
    }
  }
}

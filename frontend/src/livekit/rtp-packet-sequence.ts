export class RtpPacketSequenceError extends Error {
  readonly context: Readonly<Record<string, number>>
  constructor(values: Record<string, number | undefined>) {
    super('RTP packet sequence invalid')
    this.context = Object.fromEntries(Object.entries(values).filter(([, value]) =>
      typeof value === 'number' && Number.isFinite(value))) as Record<string, number>
  }
}

export type RtpPacketGap = Readonly<{
  expectedTimestamp: number; receivedTimestamp: number; missingPacketCount: number
}>

// 現行経路は48kHz・20msのOpus packetだけを受ける。欠落はPCM出力へ渡す前に検出する。
export class RtpPacketSequence {
  private nextIndex = 0
  private previousTimestamp: number | undefined
  private previousSource: number | undefined

  receive(packet: {packetIndex: number; rtpTimestamp: number; source?: number}): RtpPacketGap | undefined {
    const invalid = () => new RtpPacketSequenceError({packetIndex: packet.packetIndex,
      expectedPacketIndex: this.nextIndex, rtpTimestamp: packet.rtpTimestamp,
      previousRtpTimestamp: this.previousTimestamp, source: packet.source, previousSource: this.previousSource,
      timestampDelta: this.previousTimestamp === undefined ? undefined : (packet.rtpTimestamp - this.previousTimestamp) >>> 0})
    if (!Number.isSafeInteger(packet.packetIndex) || packet.packetIndex !== this.nextIndex
      || !Number.isInteger(packet.rtpTimestamp) || packet.rtpTimestamp < 0 || packet.rtpTimestamp > 0xffffffff) {
      throw invalid()
    }
    if (this.previousTimestamp !== undefined) {
      const delta = (packet.rtpTimestamp - this.previousTimestamp) >>> 0
      if (!delta || delta >= 0x80000000 || delta % 960) throw invalid()
      if (delta > 960) return {expectedTimestamp: (this.previousTimestamp + 960) >>> 0,
        receivedTimestamp: packet.rtpTimestamp, missingPacketCount: delta / 960 - 1}
    }
    this.nextIndex++
    this.previousTimestamp = packet.rtpTimestamp
    this.previousSource = packet.source
    return undefined
  }
}

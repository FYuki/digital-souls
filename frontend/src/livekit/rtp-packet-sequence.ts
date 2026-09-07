export type RtpPacketGap = Readonly<{
  expectedTimestamp: number; receivedTimestamp: number; missingPacketCount: number
}>

// 現行経路は48kHz・20msのOpus packetだけを受ける。欠落はPCM出力へ渡す前に検出する。
export class RtpPacketSequence {
  private nextIndex = 0
  private previousTimestamp: number | undefined

  receive(packet: {packetIndex: number; rtpTimestamp: number}): RtpPacketGap | undefined {
    if (!Number.isSafeInteger(packet.packetIndex) || packet.packetIndex !== this.nextIndex
      || !Number.isInteger(packet.rtpTimestamp) || packet.rtpTimestamp < 0 || packet.rtpTimestamp > 0xffffffff) {
      throw new Error('RTP packet sequence invalid')
    }
    if (this.previousTimestamp !== undefined) {
      const delta = (packet.rtpTimestamp - this.previousTimestamp) >>> 0
      if (!delta || delta >= 0x80000000 || delta % 960) throw new Error('RTP packet sequence invalid')
      if (delta > 960) return {expectedTimestamp: (this.previousTimestamp + 960) >>> 0,
        receivedTimestamp: packet.rtpTimestamp, missingPacketCount: delta / 960 - 1}
    }
    this.nextIndex++
    this.previousTimestamp = packet.rtpTimestamp
    return undefined
  }
}

import { afterEach, describe, expect, test, vi } from 'vitest'
import { RtpNetworkObserver } from './livekit/network-observer'
const source = (...rows: Record<string, unknown>[]) => ({getStats: vi.fn(async () => new Map(rows.map((row, i) => [String(i), row])) as unknown as RTCStatsReport)})
const sent = (bytes = 100, packets = 5, id = 'private-stats-id') => ({id, type: 'outbound-rtp', kind: 'audio', bytesSent: bytes, packetsSent: packets, secret: 'private-payload'})
const received = (lost = 0) => ({id: 'private-receiver', type: 'inbound-rtp', kind: 'audio', bytesReceived: 200, packetsReceived: 10, packetsLost: lost, ssrc: 12345, address: 'private-host'})
afterEach(() => vi.useRealTimers())
describe('RTP統計の数値だけを観測する', () => {
  test('送信・受信・損失を分け、IDや接続先を含めない', async () => {
    const value = await new RtpNetworkObserver().capture(source(sent()), source(received(2)), 10)
    expect(value).toEqual({method: 'browser_audio_rtp_counters_v1', uplink: {status: 'measured', bytes: 100, packets: 5}, downlink: {status: 'measured', bytes: 200, packets: 10, lostPackets: 2}})
    expect(JSON.stringify(value)).not.toContain('private')
  })
  test('同じマイクで連続応答しても送信累積値を二重加算しない', async () => {
    const observer = new RtpNetworkObserver(), sender = source(sent())
    await observer.capture(sender, source(received()), 10)
    sender.getStats.mockImplementation(source(sent(160, 8)).getStats)
    expect((await observer.capture(sender, source(received()), 10)).uplink).toEqual({status: 'measured', bytes: 60, packets: 3})
    sender.getStats.mockImplementation(source(sent(20, 1, 'new-stream')).getStats)
    expect((await observer.capture(sender, source(received()), 10)).uplink).toEqual({status: 'measured', bytes: 20, packets: 1})
  })
  test('同一streamのカウンター巻戻りを新しい0起点にしない', async () => {
    const observer = new RtpNetworkObserver(), sender = source(sent())
    await observer.capture(sender, source(received()), 10)
    sender.getStats.mockImplementation(source(sent(80, 4)).getStats)
    expect((await observer.capture(sender, source(received()), 10)).uplink).toEqual({status: 'missing', reason: 'counter_regressed'})
  })
  test('API欠落・曖昧な複数音声stream・未更新のpacket数を欠測にする', async () => {
    const observer = new RtpNetworkObserver()
    expect((await observer.capture(undefined, source(received()), 10)).uplink.status).toBe('missing')
    expect((await observer.capture(source(sent()), source(received(), received()), 10)).downlink).toEqual({status: 'missing', reason: 'ambiguous_audio_stream'})
    expect((await observer.capture(source(sent()), source(received()), 11)).downlink).toEqual({status: 'missing', reason: 'playback_packets_not_yet_reported'})
  })
  test.each([NaN, Infinity, -1, 1.5, undefined])('不正なbyte数 %s を0にしない', async bytes => {
    expect((await new RtpNetworkObserver().capture(source({...sent(), bytesSent: bytes}), source(received()), 10)).uplink).toEqual({status: 'missing', reason: 'invalid_rtp_counters'})
  })
  test('RFCの負のlossカウンターは観測値のまま残す', async () => {
    expect((await new RtpNetworkObserver().capture(source(sent()), source(received(-2)), 10)).downlink).toEqual({status: 'measured', bytes: 200, packets: 10, lostPackets: -2})
  })
  test('応答しないstats APIを2秒で欠測にし、timerを解放する', async () => {
    vi.useFakeTimers()
    const result = new RtpNetworkObserver().capture({getStats: () => new Promise(() => {})}, source(received()), 10)
    await vi.advanceTimersByTimeAsync(2000)
    expect((await result).uplink).toEqual({status: 'missing', reason: 'stats_timeout'})
    expect(vi.getTimerCount()).toBe(0)
  })
})


test('受信packet数と負のlossから負の分母になる値は欠測にする', async () => {
  expect((await new RtpNetworkObserver().capture(source(sent()), source(received(-11)), 10)).downlink)
    .toEqual({status: 'missing', reason: 'invalid_rtp_counters'})
})

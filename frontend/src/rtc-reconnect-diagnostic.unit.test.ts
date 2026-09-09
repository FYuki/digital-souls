import {afterEach, beforeEach, expect, test, vi} from 'vitest'
import {installRtcReconnectDiagnostic} from '../playwright/rtc-reconnect-diagnostic'

type Result = {status: string; closed: boolean; overflow: boolean;
  samples: Array<{status: string; rows: Record<string, unknown>[]}>, sends: Record<string, unknown>[]}
const port = () => (window as typeof window & {__voiceRtcDiagnostic: {
  start: () => void; finish: () => Promise<Result>}}).__voiceRtcDiagnostic
class FakeChannel {
  label = '_reliable'
  bufferedAmount = 7
  payload: unknown
  failure?: Error
  send(data: unknown) {if (this.failure) throw this.failure; this.payload = data}
}
let reports: Record<string, unknown>[]
let getStats: ReturnType<typeof vi.fn>
class FakePC {
  getStats = () => getStats()
}
beforeEach(() => {
  vi.useFakeTimers()
  reports = [{type: 'data-channel', id: 'private-id', label: 'private-label', protocol: 'private-protocol',
    state: 'open', messagesSent: 7, messagesReceived: 3, bytesSent: 10, bytesReceived: 20},
  {type: 'transport', localCertificateId: 'private-cert', selectedCandidatePairId: 'private-pair',
    dtlsState: 'connected', iceState: 'connected', bytesSent: 40, bytesReceived: 50},
  {type: 'local-candidate', address: 'private-ip', usernameFragment: 'private-credential'}]
  getStats = vi.fn(async () => new Map(reports.map((row, index) => [index, row])))
  vi.stubGlobal('RTCPeerConnection', FakePC)
  vi.stubGlobal('RTCDataChannel', FakeChannel)
})
afterEach(async () => {
  if (port()) await port().finish()
  delete (window as typeof window & {__voiceRtcDiagnostic?: unknown}).__voiceRtcDiagnostic
  vi.useRealTimers(); vi.unstubAllGlobals()
})

test('本物へ渡すthis・payload・例外を保持し、診断には本文を保存しない', async () => {
  const original = FakeChannel.prototype.send
  installRtcReconnectDiagnostic()
  const pc = new window.RTCPeerConnection()
  expect(pc).toBeInstanceOf(FakePC)
  const dc = new FakeChannel(), payload = new Uint8Array([1, 2, 3])
  dc.send(payload)
  port().start()
  dc.send(payload)
  expect(dc.payload).toBe(payload)
  const failure = new Error('private-error'); dc.failure = failure
  expect(() => dc.send(payload)).toThrow(failure)
  await vi.advanceTimersByTimeAsync(1)
  const result = await port().finish()
  expect(result.sends).toEqual([
    expect.objectContaining({channel: 'reliable', status: 'returned', bufferedAmount: 7}),
    expect.objectContaining({channel: 'reliable', status: 'threw', bufferedAmount: 7}),
  ])
  expect(result.samples[0].rows).toEqual([
    {kind: 'data_channel', channel: 'other', state: 'open', messagesSent: 7, messagesReceived: 3, bytesSent: 10, bytesReceived: 20},
    {kind: 'transport', dtlsState: 'connected', iceState: 'connected', packetsSent: null, packetsReceived: null, bytesSent: 40, bytesReceived: 50},
    {kind: 'selected_candidate_pair', status: 'unavailable', state: null, localProtocol: null, remoteProtocol: null,
      localType: null, remoteType: null, bytesSent: null, bytesReceived: null, requestsSent: null, requestsReceived: null,
      responsesSent: null, responsesReceived: null, consentRequestsSent: null},
  ])
  expect(JSON.stringify(result)).not.toContain('private')
  expect(FakeChannel.prototype.send).toBe(original)
  expect(window.RTCPeerConnection).toBe(FakePC)
  expect(vi.getTimerCount()).toBe(0)
})

test('統計待ちのtimeoutを欠測として残し、未完了queryを重ねず終了する', async () => {
  getStats.mockImplementation(() => new Promise(() => undefined))
  installRtcReconnectDiagnostic(); new window.RTCPeerConnection(); port().start()
  await vi.advanceTimersByTimeAsync(300)
  expect(getStats).toHaveBeenCalledTimes(1)
  const finish = port().finish()
  await vi.advanceTimersByTimeAsync(100)
  const result = await finish
  expect(result).toMatchObject({closed: true, samples: [{status: 'timeout', rows: []}]})
  expect(vi.getTimerCount()).toBe(0)
})

test('件数上限と未知state・不正counterを保持し、秘密や巨大statsを展開しない', async () => {
  reports[0].state = 'private-state'; reports[0].messagesSent = -1
  installRtcReconnectDiagnostic(); new window.RTCPeerConnection(); port().start()
  const dc = new FakeChannel()
  for (let index = 0; index < 1030; index++) dc.send('private-payload')
  await vi.advanceTimersByTimeAsync(1)
  const result = await port().finish()
  expect(result.sends).toHaveLength(1024)
  expect(result.overflow).toBe(true)
  expect(result.samples[0].rows[0]).toMatchObject({state: null, messagesSent: null})
  expect(JSON.stringify(result)).not.toContain('private')
})

test('stats上限超過を明示し、任意のエラー本文は残さない', async () => {
  reports = Array.from({length: 17}, () => reports[0])
  installRtcReconnectDiagnostic(); new window.RTCPeerConnection(); port().start()
  await vi.advanceTimersByTimeAsync(1)
  getStats.mockRejectedValue(new Error('private-error'))
  await vi.advanceTimersByTimeAsync(100)
  const result = await port().finish()
  expect(result.samples).toMatchObject([{status: 'invalid', rows: []}, {status: 'failed', rows: []}])
  expect(JSON.stringify(result)).not.toContain('private')
})

test('150回で観測を止め、終了後に計測やprototype変更を残さない', async () => {
  installRtcReconnectDiagnostic(); new window.RTCPeerConnection(); port().start(); port().start()
  await vi.advanceTimersByTimeAsync(16000)
  const result = await port().finish()
  expect(result.samples).toHaveLength(150)
  expect(vi.getTimerCount()).toBe(0)
  port().start()
  await vi.advanceTimersByTimeAsync(1000)
  expect(getStats).toHaveBeenCalledTimes(150)
})


test('ページへ直列化しても外部変数に依存せず、PCがなければ取得成功としない', async () => {
  const standalone = (0, eval)(`(${installRtcReconnectDiagnostic.toString()})`) as () => void
  standalone(); port().start()
  expect(await port().finish()).toMatchObject({status: 'unavailable', closed: true, samples: []})
})

test('PCの観測上限を超えてもnativeの生成を妨げず超過を記録する', async () => {
  installRtcReconnectDiagnostic()
  for (let index = 0; index < 10; index++) expect(new window.RTCPeerConnection()).toBeInstanceOf(FakePC)
  port().start(); await vi.advanceTimersByTimeAsync(1)
  expect(await port().finish()).toMatchObject({overflow: true, samples: Array.from({length: 8}, () => ({status: 'captured'}))})
})


test('選択したICE経路のprotocolと疎通counterを抽出し、IDやアドレスを残さない', async () => {
  const map = new Map([
    ['transport', {type: 'transport', selectedCandidatePairId: 'private-pair'}],
    ['private-pair', {type: 'candidate-pair', state: 'succeeded', localCandidateId: 'private-local', remoteCandidateId: 'private-remote',
      bytesSent: 100, bytesReceived: 200, requestsSent: 4, requestsReceived: 2, responsesSent: 2, responsesReceived: 3, consentRequestsSent: 1}],
    ['private-local', {type: 'local-candidate', protocol: 'udp', candidateType: 'host', address: 'private-address'}],
    ['private-remote', {type: 'remote-candidate', protocol: 'udp', candidateType: 'prflx', usernameFragment: 'private-credential'}],
  ])
  getStats.mockResolvedValue(map)
  installRtcReconnectDiagnostic(); new window.RTCPeerConnection(); port().start()
  await vi.advanceTimersByTimeAsync(1)
  const result = await port().finish()
  expect(result.samples[0].rows[1]).toEqual({kind: 'selected_candidate_pair', status: 'captured', state: 'succeeded',
    localProtocol: 'udp', remoteProtocol: 'udp', localType: 'host', remoteType: 'prflx', bytesSent: 100, bytesReceived: 200,
    requestsSent: 4, requestsReceived: 2, responsesSent: 2, responsesReceived: 3, consentRequestsSent: 1})
  expect(JSON.stringify(result)).not.toContain('private')
})

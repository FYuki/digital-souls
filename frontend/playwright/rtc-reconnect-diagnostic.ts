// 障害注入ページだけで使う受動診断。addInitScriptで直列化するため外部値を参照しない。
// RTCへの送信呼び出しはwire到達の証明ではない。本文、URL、SDP、stats IDは保持しない。
export function installRtcReconnectDiagnostic(): void {
  type Row = Record<string, number | string | null>
  type Sample = {index: number; pc: number; startedAtMs: number; observedAtMs: number;
    status: 'captured' | 'failed' | 'timeout' | 'invalid'; rows: Row[]}
  type Send = {atMs: number; channel: string; status: 'returned' | 'threw'; bufferedAmount: number | null}
  const target = window as typeof window & {__voiceRtcDiagnostic?: {
    start: () => void; finish: () => Promise<unknown>}}
  if (target.__voiceRtcDiagnostic) return
  const NativePC = window.RTCPeerConnection, nativeSend = window.RTCDataChannel.prototype.send
  const peers: RTCPeerConnection[] = [], samples: Sample[] = [], sends: Send[] = []
  let started = false, closed = false, armed = false, overflow = false, index = 0
  let timer: ReturnType<typeof setInterval> | undefined, pending: Promise<void> | undefined
  const counter = (value: unknown): number | null => typeof value === 'number'
    && Number.isSafeInteger(value) && value >= 0 ? value : null
  const channel = (label: string) => label === '_reliable' ? 'reliable' : label === '_lossy' ? 'lossy' : 'other'
  const state = (value: unknown, allowed: readonly string[]) => typeof value === 'string' && allowed.includes(value) ? value : null
  const ObservedPC = new Proxy(NativePC, {
    construct(constructor, args, newTarget) {
      const pc = Reflect.construct(constructor, args, newTarget) as RTCPeerConnection
      if (peers.length < 8) peers.push(pc)
      else overflow = true
      return pc
    },
  })
  const observedSend: typeof nativeSend = function (this: RTCDataChannel, ...args) {
    let status: Send['status'] = 'returned'
    try {return Reflect.apply(nativeSend, this, args)}
    catch (error) {status = 'threw'; throw error}
    finally {
      if (armed) {
        if (sends.length < 1024) sends.push({atMs: performance.now(), channel: channel(this.label),
          status, bufferedAmount: counter(this.bufferedAmount)})
        else overflow = true
      }
    }
  }
  window.RTCPeerConnection = ObservedPC
  window.RTCDataChannel.prototype.send = observedSend

  const capture = async (pc: RTCPeerConnection, ordinal: number, sequence: number) => {
    const startedAtMs = performance.now()
    let timeout: ReturnType<typeof setTimeout> | undefined
    let status: Sample['status'] = 'captured', rows: Row[] = []
    try {
      const report = await Promise.race([pc.getStats(), new Promise<null>(resolve => {
        timeout = setTimeout(() => resolve(null), 400)
      })])
      if (report === null) status = 'timeout'
      else report.forEach((row: Record<string, unknown>) => {
        if (row.type === 'data-channel') rows.push({kind: 'data_channel',
          channel: channel(typeof row.label === 'string' ? row.label : ''),
          state: state(row.state, ['connecting', 'open', 'closing', 'closed']),
          messagesSent: counter(row.messagesSent), messagesReceived: counter(row.messagesReceived),
          bytesSent: counter(row.bytesSent), bytesReceived: counter(row.bytesReceived)})
        else if (row.type === 'transport') {
          rows.push({kind: 'transport',
            dtlsState: state(row.dtlsState, ['new', 'connecting', 'connected', 'closed', 'failed']),
            iceState: state(row.iceState, ['new', 'checking', 'connected', 'completed', 'disconnected', 'failed', 'closed']),
            packetsSent: counter(row.packetsSent), packetsReceived: counter(row.packetsReceived),
            bytesSent: counter(row.bytesSent), bytesReceived: counter(row.bytesReceived)})
          // stats IDはページ内の対応づけだけに使い、選択経路の固定enumとcounterだけを残す。
          const pair = typeof row.selectedCandidatePairId === 'string' ? report.get(row.selectedCandidatePairId) : undefined
          const local = pair && typeof pair.localCandidateId === 'string' ? report.get(pair.localCandidateId) : undefined
          const remote = pair && typeof pair.remoteCandidateId === 'string' ? report.get(pair.remoteCandidateId) : undefined
          rows.push({kind: 'selected_candidate_pair', status: pair?.type === 'candidate-pair' ? 'captured' : 'unavailable',
            state: state(pair?.state, ['frozen', 'waiting', 'in-progress', 'failed', 'succeeded']),
            localProtocol: state(local?.protocol, ['udp', 'tcp']), remoteProtocol: state(remote?.protocol, ['udp', 'tcp']),
            localType: state(local?.candidateType, ['host', 'srflx', 'prflx', 'relay']),
            remoteType: state(remote?.candidateType, ['host', 'srflx', 'prflx', 'relay']),
            bytesSent: counter(pair?.bytesSent), bytesReceived: counter(pair?.bytesReceived),
            requestsSent: counter(pair?.requestsSent), requestsReceived: counter(pair?.requestsReceived),
            responsesSent: counter(pair?.responsesSent), responsesReceived: counter(pair?.responsesReceived),
            consentRequestsSent: counter(pair?.consentRequestsSent)})
        }
      })
      if (rows.length > 16) {status = 'invalid'; rows = []}
    } catch {status = 'failed'; rows = []}
    finally {if (timeout !== undefined) clearTimeout(timeout)}
    samples.push({index: sequence, pc: ordinal, startedAtMs, observedAtMs: performance.now(), status, rows})
  }
  const tick = () => {
    if (pending || closed) return
    if (index >= 150) {clearInterval(timer); armed = false; return}
    const sequence = index++
    pending = Promise.all(peers.map((pc, ordinal) => capture(pc, ordinal, sequence)))
      .then(() => undefined).finally(() => {pending = undefined})
  }
  target.__voiceRtcDiagnostic = {
    start: () => {
      if (started || closed) return
      started = true; armed = true
      tick(); timer = setInterval(tick, 100)
    },
    finish: async () => {
      closed = true; armed = false
      if (timer !== undefined) clearInterval(timer)
      await pending
      if (window.RTCPeerConnection === ObservedPC) window.RTCPeerConnection = NativePC
      if (window.RTCDataChannel.prototype.send === observedSend) window.RTCDataChannel.prototype.send = nativeSend
      return {status: started ? (peers.length ? 'captured' : 'unavailable') : 'not_started',
        peerCount: peers.length, clock_domain: 'browser_monotonic',
        closed: true, overflow, samples, sends}
    },
  }
}

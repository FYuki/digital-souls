import type {Room} from 'livekit-client'

type Row = {stage: string; atMs: number; track: number; response: number; generation: number}
type Snapshot = {rows: Row[]; closed: boolean; overflow: boolean}
declare global {
  interface Window {
    __responseTrackDiagnostic?: {close: () => Snapshot}
  }
}

// 実Roomのイベントと実ready送信を観測する。ID・payload本文を保存しない。
export function installResponseTrackDiagnostic(): void {
  const rows: Row[] = []
  const tracks = new Map<string, number>(), responses = new Map<string, number>()
  let closed = false, overflow = false
  let restore: (() => void) | undefined
  let timer: ReturnType<typeof setInterval> | undefined
  const ordinal = (map: Map<string, number>, key: string) => {
    if (!map.has(key)) map.set(key, map.size + 1)
    return map.get(key)!
  }
  const record = (stage: string, sid: string, response: string, generation: number) => {
    if (closed) return
    if (rows.length >= 128) {overflow = true; return}
    rows.push({stage, atMs: performance.now(), track: ordinal(tracks, sid),
      response: ordinal(responses, response), generation})
  }
  window.__responseTrackDiagnostic = {close() {
    closed = true; clearInterval(timer); restore?.()
    return {rows: rows.map(row => ({...row})), closed, overflow}
  }}
  const target = window as typeof window & {__digitalSoulsVoiceSessionTestPort?: {
    bindRoom?: (client: unknown) => void}}
  target.__digitalSoulsVoiceSessionTestPort ??= {}
  const previous = target.__digitalSoulsVoiceSessionTestPort.bindRoom
  target.__digitalSoulsVoiceSessionTestPort.bindRoom = value => {
    previous?.(value)
    // bindRoomは接続前に呼ばれる。実SDK Roomが作られた後だけlistenerを付ける。
    const client = value as {room: Room | null; generation: number}
    timer = setInterval(() => {
      const room = client.room
      if (closed || !room) return
      clearInterval(timer)
      const published = (publication: {trackSid: string; trackName: string}) => {
        if (publication.trackName.startsWith('ds-response-v1:'))
          record('published', publication.trackSid, publication.trackName.slice(15), client.generation)
      }
      const subscribed = (_track: unknown, publication: {trackSid: string; trackName: string}) => {
        if (publication.trackName.startsWith('ds-response-v1:'))
          record('subscribed', publication.trackSid, publication.trackName.slice(15), client.generation)
      }
      room.on('trackPublished', published)
      room.on('trackSubscribed', subscribed)
      const participant = room.localParticipant
      const native = participant.publishData
      const observed: typeof native = function(this: typeof participant, payload, options) {
        let frame: {type?: string; response_id?: string; track_sid?: string; generation?: number} | undefined
        if (options?.topic === 'digital-souls.livekit-transport.v1') {
          try {frame = JSON.parse(new TextDecoder().decode(payload))} catch { /* 診断で通信を変更しない。 */ }
        }
        const ready = frame?.type === 'response_track_ready' && typeof frame.response_id === 'string'
          && typeof frame.track_sid === 'string' && typeof frame.generation === 'number'
        if (ready) record('ready_send_started', frame!.track_sid!, frame!.response_id!, frame!.generation!)
        const sent = native.call(this, payload, options)
        if (ready) void sent.then(
          () => record('ready_send_completed', frame!.track_sid!, frame!.response_id!, frame!.generation!),
          () => record('ready_send_failed', frame!.track_sid!, frame!.response_id!, frame!.generation!),
        )
        return sent
      }
      participant.publishData = observed
      restore = () => {
        room.off('trackPublished', published); room.off('trackSubscribed', subscribed)
        if (participant.publishData === observed) participant.publishData = native
      }
    }, 5)
  }
}

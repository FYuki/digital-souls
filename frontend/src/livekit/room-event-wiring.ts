/**
 * LiveKit RoomEvent の購読と private frame decode を所有する。
 *
 * Room SDK のevent配線を一箇所に集約し、decode済みframeは
 * sinkの責務別handlerへ振り分ける。
 */

import {
  Room,
  RoomEvent,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
} from 'livekit-client'

import { VoiceReconnectPolicy } from './reconnect-policy'
import { decodePrivateFrame } from './private-contract'
import {
  APPLICATION_TOPIC,
  PRIVATE_TOPIC,
  SCREEN_TOPIC,
  type RoomEventSink,
} from './room-contract'

/** Roomのevent結線をsinkへ接続したRoomを返す。 */
export function createWiredRoom(sink: RoomEventSink): Room {
  const room = new Room({
    adaptiveStream: true,
    dynacast: true,
    reconnectPolicy: new VoiceReconnectPolicy(Math.random, (retry) =>
      sink.observeConnection('retry_scheduled', retry),
    ),
  })
  room.on(RoomEvent.SignalReconnecting, () => {
    sink.handleSignalReconnecting(room)
  })
  room.on(RoomEvent.SignalConnected, () => sink.handleSignalConnected())
  room.on(RoomEvent.Reconnecting, () => {
    sink.handleReconnecting(room)
  })
  room.on(RoomEvent.Reconnected, () => {
    sink.handleReconnected(room)
  })
  room.on(
    RoomEvent.DataReceived,
    (payload, participant, _kind, topic) => {
      if (topic === APPLICATION_TOPIC) {
        sink.handleApplicationFrame(room, payload)
        return
      }
      if (topic === SCREEN_TOPIC) {
        sink.handleScreenFrame(payload)
        return
      }
      if (topic !== PRIVATE_TOPIC) return
      try {
        const frame = decodePrivateFrame(payload)
        if (frame.type === 'output_stop_request') {
          if (!sink.isProbePublisher(participant)) return
          sink.handleOutputStopRequest(room, frame)
          return
        }
        sink.handlePrivateFrame(room, frame, participant)
      } catch {
        sink.failTransport()
      }
    },
  )
  room.on(
    RoomEvent.TrackPublished,
    (publication, participant) => {
      sink.handleTrackPublished(publication, participant)
    },
  )
  room.on(
    RoomEvent.TrackSubscribed,
    (
      track: RemoteTrack,
      publication: RemoteTrackPublication,
      participant: RemoteParticipant,
    ) => {
      sink.handleTrackSubscribed(room, track, publication, participant)
    },
  )
  room.on(RoomEvent.TrackUnsubscribed, (track, publication) => {
    sink.handleTrackUnsubscribed(track, publication)
  })
  room.on(RoomEvent.Disconnected, (reason) => {
    sink.handleDisconnected(reason)
  })
  return room
}

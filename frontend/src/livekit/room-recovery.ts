/**
 * 接続復旧・generation・状態同期を所有する。
 *
 * generationと再同期の進行状態はこの所有点だけが更新する。
 * Room・delivery・音声graphの実資源はfacade経由で参照する。
 */

import type { Room } from 'livekit-client'

import { StateSyncRequest } from './state-sync-request'
import {
  PRIVATE_TOPIC,
  browserRetryTimer,
  type PrivateFrame,
  type RoomClientHost,
} from './room-contract'

export class LiveKitRoomRecovery {
  generation = 0
  recovering = false
  recoverySynchronized = false
  recoveryPending = false
  private recoveryProbe: {
    probeId: string
    generation: number
    request: StateSyncRequest
  } | null = null
  private stateSyncRequest: StateSyncRequest | null = null
  private syncRequestedGeneration: number | null = null

  constructor(private readonly host: RoomClientHost) {}

  get syncRequestedGenerationValue(): number | null {
    return this.syncRequestedGeneration
  }

  /** 接続開始時の復旧状態だけを初期化する。deliveryやgraphは触らない。 */
  beginConnect(): void {
    this.recovering = true
    this.recoverySynchronized = false
    this.clearStateSync()
  }

  /** SDK切断通知で復旧を開始する。再接続要求の記録はfacade側の責務。 */
  beginDisconnect(): void {
    this.recovering = true
    this.recoverySynchronized = false
    this.clearStateSync()
  }

  /** 音声graphの破棄に同期して、進行中の同期・確認を閉じる。 */
  clearRecoveryState(): void {
    this.recoveryPending = false
    this.clearRecoveryProbe()
    this.recoverySynchronized = false
    this.clearStateSync()
  }

  beginStateRecovery(room: Room, awaitingReconnect = true): void {
    this.recovering = awaitingReconnect
    this.recoveryPending = true
    this.clearRecoveryProbe()
    this.host.observe({
      transport: 'unavailable',
      control: 'unavailable',
      audio: 'unavailable',
    })
    this.recoverySynchronized = false
    this.clearStateSync()
    this.host.resetControlProbes()
    void this.requestStateSync(room).catch(() => this.host.failTransport())
  }

  clearRecoveryProbe(): void {
    this.recoveryProbe?.request.close()
    this.recoveryProbe = null
  }

  confirmRecovery(room: Room): void {
    if (
      !this.recoveryPending ||
      this.recovering ||
      !this.recoverySynchronized ||
      this.recoveryProbe !== null ||
      this.host.sessionId === null ||
      this.host.room !== room
    ) {
      return
    }
    const probeId = crypto.randomUUID()
    const generation = this.generation
    const sessionId = this.host.sessionId
    const frame = new TextEncoder().encode(
      JSON.stringify({
        protocol_version: '2.0',
        type: 'control_probe',
        probe_id: probeId,
        generation,
      }),
    )
    // publish完了では閉じず、同じnonceへの返信まで250ms間隔で再送する。
    // 送信中は重ねず、60秒の期限と切断時のcloseで所有timerを終了する。
    const request = new StateSyncRequest(
      async () => {
        if (
          this.host.room !== room ||
          this.host.sessionId !== sessionId ||
          this.generation !== generation
        ) {
          return
        }
        await room.localParticipant.publishData(frame, {
          reliable: true,
          topic: PRIVATE_TOPIC,
        })
      },
      browserRetryTimer,
      () => this.host.failTransport('transport', 'recovery_probe_timeout'),
    )
    this.recoveryProbe = { probeId, generation, request }
    request.start()
  }

  acknowledgeRecovery(probeId: string, generation: number): void {
    const pending = this.recoveryProbe
    if (
      pending === null ||
      pending.probeId !== probeId ||
      pending.generation !== generation ||
      this.generation !== generation ||
      this.recovering ||
      !this.recoverySynchronized
    ) {
      return
    }
    this.clearRecoveryProbe()
    this.recoveryPending = false
    this.host.observe({
      transport: 'available',
      control: 'available',
      audio: this.host.hasAudioGraphs() ? 'available' : 'unavailable',
      generation,
    })
  }

  clearStateSync(): void {
    this.stateSyncRequest?.close()
    this.stateSyncRequest = null
    this.syncRequestedGeneration = null
  }

  async requestStateSync(room: Room): Promise<void> {
    if (this.stateSyncRequest !== null) return
    const generation = this.generation
    const sessionId = this.host.sessionId
    this.syncRequestedGeneration = generation
    const frame = new TextEncoder().encode(
      JSON.stringify({
        protocol_version: '2.0',
        type: 'state_sync_request',
        generation,
      }),
    )
    this.stateSyncRequest = new StateSyncRequest(
      async () => {
        if (this.host.room !== room || this.host.sessionId !== sessionId) {
          return
        }
        this.host.observeConnection('state_sync_requested')
        await room.localParticipant.publishData(frame, {
          reliable: true,
          topic: PRIVATE_TOPIC,
        })
      },
      browserRetryTimer,
      () => this.host.failTransport('transport', 'state_sync_timeout'),
    )
    this.stateSyncRequest.start()
  }

  /** 権威状態frameのgeneration・終端・phaseを同期し、graph側の世代切替えを依頼する。 */
  handleAuthoritativeState(
    room: Room,
    frame: Extract<PrivateFrame, { type: 'authoritative_state' }>,
  ): void {
    if (frame.generation < this.generation) return
    for (const terminal of frame.terminalOutcomes) {
      this.host.markResponseStopped(terminal.responseId)
    }
    const generationChanged = frame.generation !== this.generation
    this.generation = frame.generation
    this.host.observeConnection('authoritative_state')
    if (
      this.syncRequestedGeneration !== null &&
      frame.generation > this.syncRequestedGeneration &&
      frame.sessionPhase === 'available'
    ) {
      this.recoverySynchronized = true
      this.clearStateSync()
    }
    if (generationChanged) {
      this.clearRecoveryProbe()
      this.host.resetControlProbes()
      this.host.handleGenerationChanged(frame.generation)
    }
    if (frame.sessionPhase !== 'available') {
      this.recoverySynchronized = false
      this.clearRecoveryProbe()
      if (this.recoveryPending) {
        void this.requestStateSync(room).catch(() => this.host.failTransport())
      }
      this.host.cancelAudioProbe()
    }
    if (frame.sessionPhase === 'ended') {
      this.host.failTransport()
      return
    }
    this.confirmRecovery(room)
    this.host.observe({
      transport: frame.sessionPhase === 'available' ? 'available' : 'unavailable',
      control: 'available',
      audio: this.host.hasAudioGraphs() ? 'available' : 'unavailable',
      generation: frame.generation,
      ...(generationChanged
        ? {
            renderedSamples: 0,
            playedPrefix: -1,
            activeAudioGraphs: 0,
            renderedEnergy: 0,
            confirmedSegments: 0,
            unassignedRenderedSamples: 0,
            activeResponseId: '',
          }
        : {}),
      terminalResponseId:
        frame.terminalOutcomes.at(-1)?.responseId ?? '',
      terminalConfirmedAudioSequence:
        frame.terminalOutcomes.at(-1)?.confirmedAudioSequence ?? 0,
    })
  }
}

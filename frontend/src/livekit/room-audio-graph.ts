/**
 * 応答音声の受信track・復号・render graph・出力証跡を所有する。
 *
 * AudioContext、応答ごとのworklet graph、購読中track、再生evidence、
 * 出力停止確認を一つの所有点に集約する。
 * Room配線・private制御経路・世代遷移は別moduleの責務。
 */

import {
  Track,
  type RemoteTrack,
  type RemoteTrackPublication,
  type Room,
} from 'livekit-client'

import { DecodedReceiptAudit, type DecodedReceiptSnapshot } from './decoded-receipt-audit'
import { postGainAuditSource } from './post-gain-audit'
import { PostGainAudioMonitor, type StaleAudioObservation } from './post-gain-monitor'
import type { OutputStopRequest } from './private-contract'
import {
  PlaybackEvidenceController,
  type SegmentMetadata,
} from './playback'
import { packetRendererSource, PacketOutputTracker, PacketRenderError, type PacketRenderInterval, type SourceAudioFinished } from './packet-renderer'
import { RemoteMediaObserver, type MediaObservation, type DecodedAudioPacket } from './media-observer'
import { PacketOutputDiagnostic, type PacketOutputEvidence } from './packet-output-diagnostic'
import { RtpPacketSequence, RtpPacketSequenceError, type RtpPacketGap } from './rtp-packet-sequence'
import { RtpNetworkObserver, type NetworkObservation } from './network-observer'
import { parseVoiceSessionEvent } from '../lib/voice-session/validation'
import { PRIVATE_TOPIC, type RoomClientHost, type RoomObservation } from './room-contract'

type AudioGraphEntry = {
  responseId: string
  outputTracker: PacketOutputTracker
  outputTimer: ReturnType<typeof setInterval>
  firstPacket?: DecodedAudioPacket
  audit?: PostGainAudioMonitor
  packetDiagnostic?: PacketOutputDiagnostic
  packetSequence: RtpPacketSequence
  worklet: AudioWorkletNode
  outputGain: GainNode
  playbackElement: HTMLAudioElement
  suspended: boolean
}

export class LiveKitRoomAudioGraph {
  private audioContext: AudioContext | null = null
  private workletReady: Promise<void> | null = null
  private readonly subscriptions = new Set<string>()
  private readonly subscribedTracks = new Map<string, RemoteTrack>()
  private readonly pendingMetadata: SegmentMetadata[] = []
  private readonly pendingFinishes = new Map<string, SourceAudioFinished>()
  private readonly trackResponses = new Map<string, string>()
  private readonly trackMediaEvidence = new Map<string, MediaObservation>()
  private readonly stoppedResponses = new Set<string>()
  private latestResponseId: string | null = null
  private readonly audioGraphs = new Map<string, AudioGraphEntry>()
  private audioGraphResetVersion = 0
  private audioGraphResetTask: Promise<void> = Promise.resolve()
  private readonly audioAuditDisposals = new Set<Promise<void>>()
  private staleAudioObserver: ((row: StaleAudioObservation) => void) | undefined
  private readonly receiptAudits = new Map<string, DecodedReceiptAudit>()
  private receiptObserver: ((row: DecodedReceiptSnapshot) => void) | undefined
  private readonly mediaObservers = new Map<string, RemoteMediaObserver>()
  private duplicateTrackFrames = 0
  private readonly playbackStartedResponses = new Set<string>()
  private readonly firstOutputTimes = new Map<string, number>()
  private readonly outputConnectedResponses = new Set<string>()
  private readonly completedPlaybackResponses = new Set<string>()
  private readonly outputStopConfirmations = new Map<string, {
    requestId: string
    generation: number
    promise: Promise<{
      lastPlayedAudioSequence: number
      outputConfirmation: 'output_clock_passed' | 'never_connected'
    }>
  }>()
  private readonly networkObserver = new RtpNetworkObserver()
  private readonly networkMeasurements = new Set<string>()
  private readonly playback: PlaybackEvidenceController
  private packetOutputObserver: ((row: PacketOutputEvidence) => void) | undefined
  private suppressedResponseId: string | null = null
  private suppressedLastPlayedAudioSequence = 0
  private pendingPlaybackResponseId: string | null = null

  constructor(private readonly host: RoomClientHost) {
    this.playback = new PlaybackEvidenceController(0, (evidence) => {
      const firstPlaybackAtMs = this.firstOutputTimes.get(evidence.responseId)
      this.host.observe({
        transport: 'available',
        control: 'available',
        audio: 'available',
        renderedSamples: evidence.renderedSamples,
        playedPrefix: evidence.continuousPrefix,
        duplicateTrackFrames: this.duplicateTrackFrames,
        activeAudioGraphs: [...this.audioGraphs.values()].filter(
          (graph) => !graph.suspended,
        ).length,
        renderedEnergy: evidence.renderedEnergy,
        confirmedSegments: evidence.confirmedSegments,
        unassignedRenderedSamples: evidence.unassignedRenderedSamples,
        activeResponseId: evidence.responseId,
        ...(firstPlaybackAtMs === undefined ? {} : { firstPlaybackAtMs }),
      })
      void this.host
        .publishPlaybackConfirmation(
          evidence.responseId,
          evidence.continuousPrefix,
        )
        .catch(() => this.host.failTransport())
      if (
        firstPlaybackAtMs !== undefined &&
        evidence.responseId !== '' &&
        !this.playbackStartedResponses.has(evidence.responseId)
      ) {
        this.playbackStartedResponses.add(evidence.responseId)
        void this.host
          .publishPlaybackStarted(evidence.responseId, firstPlaybackAtMs)
          .catch(() => {
            this.host.failTransport()
          })
      }
    })
  }

  hasGraphs(): boolean {
    return this.audioGraphs.size > 0
  }

  isStopped(responseId: string): boolean {
    return this.stoppedResponses.has(responseId)
  }

  markStopped(responseId: string): void {
    this.stoppedResponses.add(responseId)
    for (const graph of this.audioGraphs.values()) {
      if (graph.responseId === responseId) this.suspendAudioGraph(graph)
    }
  }

  /** session識別の切替えで、旧sessionの応答終端・完了・測定だけを破棄する。 */
  onSessionChanged(): void {
    this.stoppedResponses.clear()
    this.completedPlaybackResponses.clear()
    this.networkMeasurements.clear()
    this.latestResponseId = null
  }

  setStaleAudioObserver(
    observer: (row: StaleAudioObservation) => void,
  ): void {
    this.staleAudioObserver = observer
  }

  setDecodedReceiptObserver(
    observer: (row: DecodedReceiptSnapshot) => void,
  ): void {
    this.receiptObserver = observer
  }

  setPacketOutputObserver(
    observer: (row: PacketOutputEvidence) => void,
  ): void {
    this.packetOutputObserver = observer
  }

  stopPlayback(responseId: string, speechStartedAtMs?: number): number {
    if (this.suppressedResponseId === responseId) {
      return this.suppressedLastPlayedAudioSequence
    }
    const lastPlayedAudioSequence = Math.max(
      0,
      this.playback.continuousPrefix(responseId) + 1,
    )
    this.stoppedResponses.add(responseId)

    this.suppressedResponseId = responseId
    this.suppressedLastPlayedAudioSequence = lastPlayedAudioSequence
    this.pendingPlaybackResponseId = null
    this.playback.discardResponse(responseId)
    for (let index = this.pendingMetadata.length - 1; index >= 0; index -= 1) {
      if (this.pendingMetadata[index].responseId === responseId) {
        this.pendingMetadata.splice(index, 1)
      }
    }
    this.audioGraphResetVersion += 1
    for (const graph of this.audioGraphs.values()) {
      if (graph.responseId === responseId) this.suspendAudioGraph(graph)
    }
    const controlAvailable = this.host.controlAvailable
    this.host.observe({
      transport:
        this.host.room === null
          ? 'idle'
          : controlAvailable
            ? 'available'
            : 'unavailable',
      control: controlAvailable ? 'available' : 'unavailable',
      audio: 'unavailable',
      activeAudioGraphs: 0,
      activeResponseId: responseId,
      playedPrefix: lastPlayedAudioSequence - 1,
      ...(speechStartedAtMs === undefined ? {} : { speechStartedAtMs }),
      localPlaybackStoppedAtMs: Math.floor(performance.now()),
    })
    return lastPlayedAudioSequence
  }

  async confirmOutputStop(room: Room, request: OutputStopRequest): Promise<void> {
    if (
      request.sessionId !== this.host.sessionId ||
      request.generation !== this.host.generation ||
      this.host.room !== room
    ) {
      return
    }
    const existing = this.outputStopConfirmations.get(request.responseId)
    if (
      existing !== undefined &&
      (existing.requestId !== request.requestId ||
        existing.generation !== request.generation)
    ) {
      throw new Error('output_stop_request_changed')
    }
    let confirmation = existing?.promise
    if (confirmation === undefined) {
      const lastPlayedAudioSequence = this.stopPlayback(request.responseId)
      const graphs = [...this.audioGraphs.values()].filter(
        (graph) => graph.responseId === request.responseId,
      )
      confirmation = (async () => {
        if (graphs.length === 0) {
          // 停止済みIDは準備中/後着trackも接続しない。既に接続したgraphの欠測は成功にしない。
          if (
            this.outputConnectedResponses.has(request.responseId) ||
            lastPlayedAudioSequence !== 0
          ) {
            throw new Error('output_stop_graph_missing')
          }
          return {
            lastPlayedAudioSequence,
            outputConfirmation: 'never_connected' as const,
          }
        }
        await Promise.all(
          graphs.map((graph) => {
            if (graph.audit === undefined)
              throw new Error('output_stop_monitor_missing')
            return graph.audit.stopAndConfirm(request)
          }),
        )
        return {
          lastPlayedAudioSequence,
          outputConfirmation: 'output_clock_passed' as const,
        }
      })()
      this.outputStopConfirmations.set(request.responseId, {
        requestId: request.requestId,
        generation: request.generation,
        promise: confirmation,
      })
    }
    const result = await confirmation
    if (
      this.host.room !== room ||
      request.sessionId !== this.host.sessionId ||
      request.generation !== this.host.generation
    ) {
      return
    }
    await room.localParticipant.publishData(
      new TextEncoder().encode(
        JSON.stringify({
          protocol_version: '2.0',
          type: 'output_stop_confirmed',
          session_id: request.sessionId,
          response_id: request.responseId,
          request_id: request.requestId,
          generation: request.generation,
          last_played_audio_sequence: result.lastPlayedAudioSequence,
          output_confirmation: result.outputConfirmation,
        }),
      ),
      { reliable: true, topic: PRIVATE_TOPIC },
    )
  }

  /** 応答trackの購読確立を購読・復号・graph接続まで一括で行う。 */
  handleResponseTrackSubscribed(
    room: Room,
    track: RemoteTrack,
    key: string,
    responseId: string,
  ): void {
    if (this.subscriptions.has(key)) {
      this.duplicateTrackFrames += 1
      this.host.observe({
        transport: 'available',
        control: 'available',
        audio: 'available',
        duplicateTrackFrames: this.duplicateTrackFrames,
      })
      return
    }
    this.subscriptions.add(key)
    this.trackResponses.set(key, responseId)
    this.subscribedTracks.set(key, track)
    const sessionId = this.host.sessionId
    const receiptAudit =
      this.receiptObserver && sessionId !== null
        ? new DecodedReceiptAudit(
            responseId,
            sessionId,
            this.host.generation,
            performance.now(),
            this.receiptObserver,
          )
        : undefined
    if (receiptAudit) this.receiptAudits.set(key, receiptAudit)
    const generation = this.host.generation
    // 解除・取消・再購読後に戻った旧処理の失敗を、現在の会話へ適用しない。
    let observer: RemoteMediaObserver | undefined
    const isCurrent = () =>
      this.host.room === room &&
      this.host.sessionId === sessionId &&
      this.subscriptions.has(key) &&
      this.subscribedTracks.get(key) === track &&
      this.trackResponses.get(key) === responseId &&
      !this.stoppedResponses.has(responseId) &&
      (observer === undefined || this.mediaObservers.get(key) === observer)
    observer = new RemoteMediaObserver(
      track.receiver,
      track.mediaStreamTrack,
      (evidence) => this.observeTrackMedia(evidence, responseId, key),
      {
        packet: (packet) => {
          receiptAudit?.received(packet.pcm.length, performance.now())
          const graph = this.audioGraphs.get(key)
          graph?.audit?.received(packet.pcm.length)
          if (
            !this.subscriptions.has(key) ||
            !graph ||
            this.stoppedResponses.has(responseId)
          ) {
            return
          }
          try {
            const gap = graph.packetSequence.receive(packet)
            if (gap) {
              this.interruptResponseAfterPacketLoss(responseId, gap)
              return
            }
          } catch (error) {
            const context =
              error instanceof RtpPacketSequenceError
                ? error.context
                : undefined
            if (
              context &&
              context.packetIndex === context.expectedPacketIndex &&
              Number.isInteger(context.rtpTimestamp) &&
              context.rtpTimestamp >= 0 &&
              context.rtpTimestamp <= 0xffffffff &&
              Number.isInteger(context.previousRtpTimestamp) &&
              context.previousRtpTimestamp >= 0 &&
              context.previousRtpTimestamp <= 0xffffffff &&
              Number.isInteger(context.timestampDelta)
            ) {
              this.interruptResponseAfterMediaDiscontinuity(responseId, {
                mediaTimelineInterruption: {
                  responseId,
                  atMs: performance.now(),
                  reason: 'timestamp_discontinuity',
                },
              })
            } else this.host.failTransport('rtp_timeline', error)
            return
          }
          graph.packetDiagnostic?.receive(packet)
          if (packet.packetIndex === 0)
            graph.firstPacket = { ...packet, pcm: new Float32Array(0) }
          if (this.completedPlaybackResponses.has(responseId)) return
          graph.worklet.port.postMessage(
            {
              kind: 'pcm',
              packetIndex: packet.packetIndex,
              rtpTimestamp: packet.rtpTimestamp,
              samples: packet.pcm,
            },
            [packet.pcm.buffer],
          )
        },
        failed: () => {
          if (isCurrent()) this.host.failTransport('media_decoder')
        },
        interrupted: () => {
          if (
            !this.subscriptions.has(key) ||
            this.trackResponses.get(key) !== responseId
          ) {
            return
          }
          this.interruptResponseAfterMediaDiscontinuity(responseId, {
            mediaTimelineInterruption: {
              responseId,
              atMs: performance.now(),
              reason: 'timestamp_overlap',
            },
          })
        },
      },
    )
    this.mediaObservers.set(key, observer)
    void this.attachRenderEvidence(track, key).catch((error) => {
      if (isCurrent() && this.host.generation === generation)
        this.host.failTransport('audio_graph', error)
    })
  }

  handleTrackUnsubscribed(key: string): void {
    this.subscriptions.delete(key)
    this.subscribedTracks.delete(key)
    this.trackResponses.delete(key)
    this.trackMediaEvidence.delete(key)
    this.mediaObservers.get(key)?.close()
    this.mediaObservers.delete(key)
    this.receiptAudits.get(key)?.close(performance.now())
    this.receiptAudits.delete(key)
    const graph = this.audioGraphs.get(key)
    if (graph !== undefined) this.disconnectAudioGraph(graph)
    this.audioGraphs.delete(key)
  }

  /** 権威状態の世代変更に同期して、graph側の世代境界だけを進める。 */
  handleGenerationChanged(generation: number): void {
    this.playback.setGeneration(generation)
    this.pendingMetadata.length = 0
    this.pendingFinishes.clear()
    this.playbackStartedResponses.clear()
    this.firstOutputTimes.clear()
    const resetVersion = ++this.audioGraphResetVersion
    const resetTask = this.audioGraphResetTask.then(() =>
      this.resetAudioGraphs(resetVersion),
    )
    this.audioGraphResetTask = resetTask.catch(() => undefined)
    void resetTask.catch(() => this.host.failTransport())
  }

  handleResponseStarted(responseId: string): void {
    if (
      this.latestResponseId !== null &&
      this.latestResponseId !== responseId
    ) {
      this.stoppedResponses.add(this.latestResponseId)
      this.playback.discardResponse(this.latestResponseId)
      this.firstOutputTimes.delete(this.latestResponseId)
      for (const graph of this.audioGraphs.values()) {
        if (graph.responseId === this.latestResponseId)
          this.suspendAudioGraph(graph)
      }
    }
    this.latestResponseId = responseId
  }

  noteSuppressedResponse(responseId: string): void {
    if (
      this.suppressedResponseId !== null &&
      responseId !== this.suppressedResponseId
    ) {
      this.pendingPlaybackResponseId = responseId
    }
  }

  handleResponseAudioSegment(responseId: string): void {
    if (responseId === this.pendingPlaybackResponseId) {
      this.suppressedResponseId = null
      this.suppressedLastPlayedAudioSequence = 0
      this.pendingPlaybackResponseId = null
      this.resumePlaybackGraphs(responseId)
    }
  }

  handleResponseTerminal(responseId: string): void {
    this.stoppedResponses.add(responseId)
    this.playback.discardResponse(responseId)
    for (const graph of this.audioGraphs.values()) {
      if (graph.responseId === responseId) this.suspendAudioGraph(graph)
    }
    if (responseId === this.pendingPlaybackResponseId) {
      this.pendingPlaybackResponseId = null
    }
  }

  handleResponseCancelled(responseId: string): void {
    const trackKey = [...this.trackResponses].find(
      ([, id]) => id === responseId,
    )?.[0]
    void this.observeNetwork(
      responseId,
      trackKey,
      this.host.generation,
      0,
      'response_cancelled',
    )
    const confirmedAt = performance.now()
    for (const receipt of this.receiptAudits.values()) {
      if (receipt.responseId === responseId) receipt.cancel(confirmedAt)
    }
    for (const graph of this.audioGraphs.values()) {
      if (graph.responseId === responseId) graph.audit?.cancel(confirmedAt)
    }
    this.host.observe({
      transport: 'available',
      control: 'available',
      audio: 'unavailable',
      activeResponseId: responseId,
      cancelConfirmedAtMs: Math.floor(confirmedAt),
    })
  }

  handleAudioFinished(frame: SourceAudioFinished & { responseId: string; generation: number }): void {
    if (
      frame.generation !== this.host.generation ||
      this.stoppedResponses.has(frame.responseId)
    ) {
      return
    }
    const graph = [...this.audioGraphs.values()].find(
      (graph) => graph.responseId === frame.responseId,
    )
    if (graph) graph.outputTracker.finish(frame)
    else {
      if (this.pendingFinishes.size >= 128)
        throw new Error('pending source completion overflow')
      this.pendingFinishes.set(frame.responseId, frame)
    }
  }

  handleLogicalAudioSegment(
    frame: SegmentMetadata & { generation: number },
  ): void {
    if (
      frame.generation !== this.host.generation ||
      this.stoppedResponses.has(frame.responseId)
    ) {
      return
    }
    const context = this.audioContext
    if (context === null) this.pendingMetadata.push(frame)
    else this.recordMetadataOnContext(frame, context)
  }

  private observeTrackMedia(
    evidence: MediaObservation,
    responseId: string,
    key: string,
  ): void {
    if (
      !this.subscriptions.has(key) ||
      this.trackResponses.get(key) !== responseId
    ) {
      return
    }
    this.trackMediaEvidence.set(key, evidence)
    // track名だけでは相関を確定せず、同じpacketのPCMが出力時計を通過するまで待つ。
    const controlAvailable = this.host.controlAvailable
    const audioAvailable =
      controlAvailable &&
      [...this.audioGraphs.values()].some((graph) => !graph.suspended)
    this.host.observe({
      transport: controlAvailable ? 'available' : 'unavailable',
      control: controlAvailable ? 'available' : 'unavailable',
      audio: audioAvailable ? 'available' : 'unavailable',
      mediaObservation: evidence,
      mediaTrackResponseId: responseId,
      mediaCorrelationMissingReason: 'response_frame_correlation_unavailable',
    })
  }

  async observeNetwork(
    responseId: string,
    key: string | undefined,
    generation: number,
    expectedPackets: number,
    boundary?: 'response_cancelled',
  ): Promise<void> {
    const room = this.host.room
    const sessionId = this.host.sessionId
    if (
      !room ||
      sessionId === null ||
      this.host.generation !== generation ||
      this.networkMeasurements.has(responseId)
    ) {
      return
    }
    if (
      boundary === undefined &&
      (key === undefined || this.trackResponses.get(key) !== responseId)
    ) {
      return
    }
    this.networkMeasurements.add(responseId)
    const sender = room.localParticipant.getTrackPublication(
      Track.Source.Microphone,
    )?.track?.sender
    const receiver =
      key === undefined ? undefined : this.subscribedTracks.get(key)?.receiver
    const captured = await this.networkObserver.capture(
      sender,
      receiver,
      expectedPackets,
    )
    const networkObservation: NetworkObservation = {
      ...captured,
      ...(boundary === undefined ? {} : { boundary }),
    }
    if (
      this.host.room !== room ||
      this.host.sessionId !== sessionId ||
      this.host.generation !== generation
    ) {
      return
    }
    if (
      boundary === undefined &&
      (key === undefined || this.trackResponses.get(key) !== responseId)
    ) {
      return
    }
    let networkMeasurementDelivered = false
    try {
      await this.host.publishControlEvent(
        parseVoiceSessionEvent({
          type: 'observation',
          protocol_version: '2.0',
          event_id: crypto.randomUUID(),
          session_id: sessionId,
          response_id: responseId,
          measurement: 'network_summary',
          network_summary: networkObservation,
          timestamp: Math.floor(performance.now()),
          clock_domain: 'client_monotonic',
          unit: 'millisecond',
        }),
      )
      networkMeasurementDelivered = true
    } catch {
      // 計測の配送失敗で再生済み応答をcancelしない。Backend側の欠測は集計に残す。
    }
    if (
      this.host.room !== room ||
      this.host.generation !== generation ||
      this.host.sessionId !== sessionId
    ) {
      return
    }
    // 非同期の取消統計で、次応答や再接続のaudio状態を上書きしない。
    if (boundary !== undefined) return
    this.host.observe({
      transport: 'available',
      control: 'available',
      audio: 'available',
      networkResponseId: responseId,
      networkObservation,
      networkMeasurementDelivered,
    })
  }

  private interruptResponseAfterPacketLoss(
    responseId: string,
    gap: RtpPacketGap,
  ): void {
    this.interruptResponseAfterMediaDiscontinuity(responseId, {
      mediaPacketLoss: { ...gap, responseId, atMs: performance.now() },
    })
  }

  private interruptResponseAfterMediaDiscontinuity(
    responseId: string,
    evidence: Pick<
      RoomObservation,
      'mediaPacketLoss' | 'mediaTimelineInterruption'
    >,
  ): void {
    const sessionId = this.host.sessionId
    const room = this.host.room
    const generation = this.host.generation
    if (
      sessionId === null ||
      room === null ||
      this.stoppedResponses.has(responseId)
    ) {
      return
    }
    if (this.completedPlaybackResponses.has(responseId)) {
      // 全source PCMの実出力が確認済みなら、旧trackの遅着異常で次の応答を再同期しない。
      // 異常の観測は残す。受信しただけ・Coreが完了しただけの応答には適用しない。
      const controlAvailable = this.host.controlAvailable
      this.host.observe({
        transport: controlAvailable ? 'available' : 'unavailable',
        control: controlAvailable ? 'available' : 'unavailable',
        audio:
          controlAvailable &&
          [...this.audioGraphs.values()].some((graph) => !graph.suspended)
            ? 'available'
            : 'unavailable',
        ...evidence,
      })
      return
    }
    // 欠けたPCMを全出力済みに補完しない。確認済みprefixだけ通知し、現在の応答を止める。
    const lastPlayedAudioSequence = this.host.stopPlayback(responseId)
    this.host.observe({
      transport: 'available',
      control: 'available',
      audio: 'unavailable',
      ...evidence,
    })
    const event = (fields: Record<string, unknown>) =>
      parseVoiceSessionEvent({
        protocol_version: '2.0',
        event_id: crypto.randomUUID(),
        session_id: sessionId,
        response_id: responseId,
        reason: 'disconnect',
        monotonic_timestamp_ms: Math.floor(performance.now()),
        ...fields,
      })
    // reliable制御経路は維持する。Coreは停止位置を確定してから生成をcancelする。
    void (async () => {
      await this.host.publishControlEvent(
        event({
          type: 'playback_stopped',
          last_played_audio_sequence: lastPlayedAudioSequence,
        }),
      )
      if (this.host.room !== room || this.host.sessionId !== sessionId) return
      await this.host.publishControlEvent(
        event({ type: 'response_cancel_requested' }),
      )
      // Backendが再送期限切れでunavailableになった場合も、同じsessionの制御を戻す。
      if (
        this.host.room === room &&
        this.host.sessionId === sessionId &&
        this.host.generation === generation
      ) {
        // 接続が維持されていても再同期は入力readerの世代を交換する。
        // 旧認可を先に無効化し、権威状態と制御往復の確認後に新trackを認可する。
        if (!this.host.recoveryPending)
          this.host.beginStateRecovery(room, false)
        else await this.host.requestStateSync(room)
      }
    })().catch(() => {
      if (this.host.room === room && this.host.sessionId === sessionId)
        this.host.failTransport()
    })
  }

  private async attachRenderEvidence(
    track: RemoteTrack,
    key: string,
  ): Promise<void> {
    if (!this.subscriptions.has(key)) return
    const responseId = this.trackResponses.get(key)
    if (responseId === undefined || this.stoppedResponses.has(responseId))
      return
    if (this.audioContext === null) {
      // 出力待ちの音声を減らす。実際の遅延はブラウザに依存するため診断側で実測する。
      const created = new AudioContext({ sampleRate: 48_000, latencyHint: 0 })
      this.audioContext = created
      const url = URL.createObjectURL(
        new Blob([packetRendererSource, postGainAuditSource], {
          type: 'text/javascript',
        }),
      )
      this.workletReady = created.audioWorklet
        .addModule(url)
        .finally(() => {
          URL.revokeObjectURL(url)
        })
        .catch(async (error: unknown) => {
          if (this.audioContext === created) await this.disposeAudioContext()
          throw error
        })
    }
    const context = this.audioContext
    const workletReady = this.workletReady
    const generation = this.host.generation
    await context.resume()
    await workletReady
    await this.mediaObservers.get(key)?.ready()
    if (
      !this.subscriptions.has(key) ||
      this.audioContext !== context ||
      this.host.generation !== generation ||
      this.stoppedResponses.has(responseId)
    ) {
      return
    }
    const worklet = new AudioWorkletNode(context, 'packet-renderer', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: {
        paused:
          this.stoppedResponses.has(responseId) ||
          this.suppressedResponseId !== null,
      },
    })
    const outputGain = context.createGain()
    outputGain.gain.value = 1
    const outputTracker = new PacketOutputTracker(
      (interval, atMs, clock) => {
        const graph = this.audioGraphs.get(key)
        if (
          !graph ||
          graph.suspended ||
          this.host.generation !== generation ||
          this.audioContext !== context
        ) {
          return
        }
        if (interval.packetIndex === 0 && interval.packetSampleOffset === 0) {
          if (
            !graph.firstPacket ||
            graph.firstPacket.rtpTimestamp !== interval.rtpTimestamp
          ) {
            throw new Error('first output packet mismatch')
          }
          const media = this.trackMediaEvidence.get(key)
          const packet = graph.firstPacket
          if (
            !media ||
            media.packetDecodeMissingReason !== undefined ||
            media.firstPacketDecodedSamples !== 960 ||
            media.firstPacketReceivedAtMs !== packet.receivedAtMs ||
            media.firstPacketDecodedAtMs !== packet.decodedAtMs ||
            !Number.isFinite(media.trackReceivedAtMs) ||
            media.trackReceivedAtMs < 0 ||
            media.trackReceivedAtMs > packet.receivedAtMs ||
            packet.receivedAtMs > packet.decodedAtMs ||
            packet.decodedAtMs > atMs
          ) {
            throw new Error('first output media correlation mismatch')
          }
          this.firstOutputTimes.set(responseId, atMs)
          // 応答専用trackの最初の受信packetを復号し、そのPCMの出力通過を確認した時点で相関する。
          // 元PCMのcodec lookaheadやlogical segmentのplayed prefixとは別の観測境界。
          void Promise.all([
            this.host.publishMediaMeasurement(
              responseId,
              'client_track_received',
              media.trackReceivedAtMs,
            ),
            this.host.publishMediaMeasurement(
              responseId,
              'client_encoded_received',
              packet.receivedAtMs,
            ),
            this.host.publishMediaMeasurement(
              responseId,
              'client_audio_decoded',
              packet.decodedAtMs,
            ),
          ]).catch(() => this.host.failTransport('media_decoder'))
          this.host.observe({
            transport: 'available',
            control: 'available',
            audio: 'available',
            activeResponseId: responseId,
            mediaResponseId: responseId,
            mediaTrackResponseId: responseId,
            mediaObservation: media,
            packetPlaybackObservation: {
              packetIndex: 0,
              receivedAtMs: graph.firstPacket.receivedAtMs,
              decodedAtMs: graph.firstPacket.decodedAtMs,
              firstOutputFrame: interval.startFrame,
              firstOutputEndFrame: interval.endFrame,
              firstOutputAtMs: atMs,
              outputClockContextTime: clock.contextTime,
              outputClockPerformanceTime: clock.performanceTime,
              confirmationObservedAtMs: clock.observedAtMs,
              sampleRate: 48000,
              outputClockPassed: true,
              sourcePcmOffsetVerified: false,
              renderClockMethod: 'quantum_count_reconciled_with_global_frame',
              renderQuantumStartFrame: interval.renderQuantumStartFrame,
              renderClockConfirmationFrame:
                interval.renderClockConfirmationFrame,
            },
          })
        }
        graph.packetDiagnostic?.confirm(interval, atMs, clock.observedAtMs)
        this.playback.recordRenderedInterval({
          ...interval,
          responseId,
          ...(interval.packetIndex === 0 && interval.packetSampleOffset === 0
            ? { firstResponseFrame: interval.startFrame }
            : {}),
        })
      },
      (completion) => {
        const graph = this.audioGraphs.get(key)
        if (
          !graph ||
          graph.suspended ||
          this.host.generation !== generation ||
          this.audioContext !== context
        ) {
          return
        }
        clearInterval(graph.outputTimer)
        graph.worklet.port.postMessage({ kind: 'stop' })
        const prefix = this.playback.metadataPrefixForTotal(
          responseId,
          completion.inputSamples,
        )
        if (prefix < 0)
          throw new Error('full playback metadata does not match source samples')
        this.completedPlaybackResponses.add(responseId)
        void this.host
          .publishPlaybackConfirmation(responseId, prefix, true, completion)
          .catch(() => this.host.failTransport())
        this.host.observe({
          transport: 'available',
          control: 'available',
          audio: 'available',
          activeResponseId: responseId,
          playbackCompletedResponseId: responseId,
          playbackCompletion: completion,
        })
        void this.observeNetwork(
          responseId,
          key,
          generation,
          completion.packetCount,
        )
      },
    )
    const outputTimer = setInterval(() => {
      try {
        outputTracker.poll(context.getOutputTimestamp(), context.sampleRate)
      } catch (error) {
        this.host.failTransport('output_clock', error)
      }
    }, 5)
    worklet.port.onmessage = (
      event: MessageEvent<
        PacketRenderInterval | { kind: 'empty' | 'error'; reason?: string }
      >,
    ) => {
      if (
        !this.subscriptions.has(key) ||
        this.host.generation !== generation ||
        this.audioContext !== context ||
        this.audioGraphs.get(key)?.suspended
      ) {
        return
      }
      try {
        if (event.data.kind === 'rendered') outputTracker.record(event.data)
        else if (event.data.kind === 'error')
          this.host.failTransport('renderer', event.data.reason)
      } catch (error) {
        this.host.failTransport(
          error instanceof Error && error.message === 'RTP timeline discontinuity'
            ? 'rtp_timeline'
            : 'renderer',
          error,
        )
      }
    }
    const playbackElement = document.createElement('audio')
    playbackElement.autoplay = true
    playbackElement.hidden = true
    // 要素はWebRTCのmedia処理を維持する。出音はworklet経路だけにする。
    playbackElement.muted = true
    playbackElement.srcObject = new MediaStream([track.mediaStreamTrack])
    document.body.append(playbackElement)
    const graphSessionId = this.host.sessionId
    if (graphSessionId === null)
      throw new Error('output graph requires session identity')
    const audit = new PostGainAudioMonitor(
      context,
      responseId,
      graphSessionId,
      generation,
      this.staleAudioObserver,
    )
    const graph = {
      responseId,
      audit,
      outputTracker,
      outputTimer,
      firstPacket: undefined as DecodedAudioPacket | undefined,
      packetSequence: new RtpPacketSequence(),
      packetDiagnostic:
        this.packetOutputObserver === undefined
          ? undefined
          : new PacketOutputDiagnostic(responseId, key, generation, (row) =>
              this.packetOutputObserver?.(row),
            ),
      worklet,
      outputGain,
      playbackElement,
      suspended:
        this.stoppedResponses.has(responseId) ||
        this.suppressedResponseId !== null,
    }
    if (!graph.suspended) this.connectAudioEvidence(graph, context)
    this.audioGraphs.set(key, graph)
    const finished = this.pendingFinishes.get(responseId)
    if (finished) {
      this.pendingFinishes.delete(responseId)
      outputTracker.finish(finished)
    }
    for (const metadata of this.pendingMetadata.splice(0)) {
      this.recordMetadataOnContext(metadata, context)
    }
    const currentRoom = this.host.room
    if (
      currentRoom !== null &&
      this.subscriptions.has(key) &&
      this.audioGraphs.get(key) === graph &&
      this.host.generation === generation &&
      !this.stoppedResponses.has(responseId)
    ) {
      await currentRoom.localParticipant.publishData(
        new TextEncoder().encode(
          JSON.stringify({
            protocol_version: '2.0',
            type: 'response_track_ready',
            response_id: responseId,
            track_sid: key,
            generation,
          }),
        ),
        { reliable: true, topic: PRIVATE_TOPIC },
      )
    }
    if (
      !this.subscriptions.has(key) ||
      this.audioContext !== context ||
      this.host.generation !== generation ||
      this.audioGraphs.get(key) !== graph
    ) {
      return
    }
    this.host.observe({
      transport: 'available',
      control: 'available',
      audio: 'available',
      activeAudioGraphs: [...this.audioGraphs.values()].filter(
        (graph) => !graph.suspended,
      ).length,
    })
  }

  private async resetAudioGraphs(resetVersion: number): Promise<void> {
    const tracks = [...this.subscribedTracks.entries()].map(([key, track]) => ({
      key,
      track,
    }))
    await this.disposeAudioContext()
    if (resetVersion !== this.audioGraphResetVersion) return
    for (const { key, track } of tracks) {
      if (resetVersion !== this.audioGraphResetVersion) return
      if (this.subscriptions.has(key))
        await this.attachRenderEvidence(track, key)
    }
  }

  /** graphの購読・監視・停止状態を閉じる。AudioContextの破棄は呼出側が最後に行う。 */
  closeAudioGraphState(): void {
    this.audioGraphResetVersion += 1
    for (const observer of this.mediaObservers.values()) observer.close()
    this.mediaObservers.clear()
    for (const receipt of this.receiptAudits.values())
      receipt.close(performance.now())
    this.receiptAudits.clear()
    this.subscriptions.clear()
    this.subscribedTracks.clear()
    this.trackResponses.clear()
    this.trackMediaEvidence.clear()
    this.pendingMetadata.length = 0
    this.pendingFinishes.clear()
    this.firstOutputTimes.clear()
    this.suppressedResponseId = null
    this.suppressedLastPlayedAudioSequence = 0
    this.pendingPlaybackResponseId = null
  }

  /** Core受信の重複履歴とは別に、出力確認と接続済み応答だけを破棄する。 */
  clearOutputState(): void {
    this.outputConnectedResponses.clear()
    this.outputStopConfirmations.clear()
  }

  async disposeAudioContext(): Promise<void> {
    for (const graph of this.audioGraphs.values()) {
      this.disconnectAudioGraph(graph)
    }
    this.audioGraphs.clear()
    const context = this.audioContext
    this.audioContext = null
    this.workletReady = null
    await Promise.all([...this.audioAuditDisposals])
    if (context !== null) await context.close()
  }

  private disconnectAudioGraph(graph: AudioGraphEntry): void {
    clearInterval(graph.outputTimer)
    graph.outputTracker.stop()
    graph.worklet.port.postMessage({ kind: 'stop' })
    graph.worklet.port.close()
    if (!graph.suspended) {
      graph.worklet.disconnect()
      graph.outputGain.disconnect()
    }
    graph.playbackElement.srcObject = null
    graph.playbackElement.remove()
    if (graph.audit) {
      const task = graph.audit.dispose()
      this.audioAuditDisposals.add(task)
      void task.then(
        () => this.audioAuditDisposals.delete(task),
        () => this.audioAuditDisposals.delete(task),
      )
    }
  }

  private suspendAudioGraph(graph: AudioGraphEntry): void {
    graph.playbackElement.muted = true
    clearInterval(graph.outputTimer)
    graph.outputTracker.stop()
    graph.worklet.port.postMessage({ kind: 'stop' })
    if (graph.suspended) return
    graph.worklet.disconnect()
    graph.outputGain.disconnect()
    graph.suspended = true
  }

  private resumePlaybackGraphs(responseId: string): void {
    const context = this.audioContext
    if (context !== null && this.audioGraphs.size > 0) {
      for (const graph of this.audioGraphs.values()) {
        if (
          graph.responseId === responseId &&
          !this.stoppedResponses.has(responseId) &&
          graph.suspended
        ) {
          this.connectAudioEvidence(graph, context)
        }
      }
      this.host.observe({
        transport: this.host.room === null ? 'idle' : 'available',
        control: this.host.controlAvailable ? 'available' : 'unavailable',
        audio: 'available',
        activeAudioGraphs: [...this.audioGraphs.values()].filter(
          (graph) => !graph.suspended,
        ).length,
      })
      return
    }
    const resetVersion = ++this.audioGraphResetVersion
    const resetTask = this.audioGraphResetTask.then(() =>
      this.resetAudioGraphs(resetVersion),
    )
    this.audioGraphResetTask = resetTask.catch(() => undefined)
    void resetTask.catch(() => this.host.failTransport())
  }

  private connectAudioEvidence(
    graph: AudioGraphEntry,
    context: AudioContext,
  ): void {
    this.outputConnectedResponses.add(graph.responseId)
    graph.worklet.port.postMessage({ kind: 'resume' })
    graph.worklet.connect(graph.outputGain)
    graph.outputGain.connect(graph.audit?.node ?? context.destination)
    graph.suspended = false
  }

  private recordMetadataOnContext(
    metadata: SegmentMetadata,
    context: AudioContext,
  ): void {
    this.playback.recordMetadata(
      metadata,
      Math.floor(context.currentTime * context.sampleRate),
    )
  }
}

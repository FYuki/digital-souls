import type {
  ActualSurface,
  CaptureState,
  RecognitionState,
  ReasonCode,
  SnapshotRequested,
  SnapshotUploadMetadata,
} from './generated'

export const SCREEN_CAPTURE_LIMITS = {
  maxWidth: 2560,
  maxHeight: 2560,
  maxPixels: 4_194_304,
  maxBytes: 5_242_880,
  captureTimeoutMs: 5_000,
} as const

// Chromium系では静止したwindow共有で次frame通知が来ないことがある。
// 現在frameが利用可能なら短時間だけ更新を待ち、その時点の表示内容を取得する。
const FRESH_FRAME_GRACE_MS = 250

type DisplayVideoTrackSettings = MediaTrackSettings & {
  displaySurface?: string
}

type VideoWithFrameCallback = HTMLVideoElement & {
  requestVideoFrameCallback?: (callback: () => void) => number
  cancelVideoFrameCallback?: (handle: number) => void
}

export type ScreenCaptureState = {
  generation: number
  captureState: CaptureState
  recognitionState: RecognitionState
  reasonCode: ReasonCode | null
  requestedSurface: ActualSurface
  actualSurface: ActualSurface | null
  targetLabel: string | null
  trackMuted: boolean
  lastRecognizedCaptureAt: string | null
}

export type AuthorizedSnapshotRequest = {
  request: SnapshotRequested
  clientSessionId: string
}

export type CapturedScreenSnapshot = {
  blob: Blob
  metadata: SnapshotUploadMetadata
}

type CaptureDependencies = {
  isSecureContext: () => boolean
  hasTransientActivation: () => boolean | undefined
  getDisplayMedia: ((options: DisplayMediaStreamOptions) => Promise<MediaStream>) | null
  createCanvas: () => HTMLCanvasElement
  createUuid: () => string
  now: () => Date
  setTimer: (callback: () => void, delay: number) => number
  clearTimer: (handle: number) => void
}

const browserDependencies = (): CaptureDependencies => ({
  isSecureContext: () => window.isSecureContext,
  hasTransientActivation: () => navigator.userActivation?.isActive,
  getDisplayMedia: navigator.mediaDevices?.getDisplayMedia?.bind(navigator.mediaDevices) ?? null,
  createCanvas: () => document.createElement('canvas'),
  createUuid: () => crypto.randomUUID(),
  now: () => new Date(),
  setTimer: (callback, delay) => window.setTimeout(callback, delay),
  clearTimer: (handle) => window.clearTimeout(handle),
})

const initialState = (): ScreenCaptureState => ({
  generation: 0,
  captureState: 'off',
  recognitionState: 'idle',
  reasonCode: null,
  requestedSurface: 'monitor',
  actualSurface: null,
  targetLabel: null,
  trackMuted: false,
  lastRecognizedCaptureAt: null,
})

const mapCaptureError = (error: unknown): ReasonCode => {
  if (!(error instanceof DOMException)) return 'capture_os_error'
  return ({
    InvalidStateError: 'transient_activation_required',
    NotAllowedError: 'capture_not_allowed',
    NotFoundError: 'no_capture_source',
    NotReadableError: 'capture_os_error',
    AbortError: 'capture_os_error',
    TypeError: 'api_unavailable',
  } as Partial<Record<string, ReasonCode>>)[error.name] ?? 'capture_os_error'
}

const surfaceReason = (actual: string | undefined, requested: ActualSurface): ReasonCode => {
  if (actual === undefined) return 'surface_unknown'
  if (actual !== requested) return 'surface_mismatch'
  return 'video_track_invalid'
}

const outputDimensions = (width: number, height: number): { width: number; height: number } => {
  const scale = Math.min(
    1,
    SCREEN_CAPTURE_LIMITS.maxWidth / width,
    SCREEN_CAPTURE_LIMITS.maxHeight / height,
    Math.sqrt(SCREEN_CAPTURE_LIMITS.maxPixels / (width * height)),
  )
  return {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  }
}

const encodeCanvas = (
  canvas: HTMLCanvasElement,
  type: 'image/png' | 'image/jpeg',
  quality?: number,
): Promise<Blob> => new Promise((resolve, reject) => {
  canvas.toBlob((blob) => {
    if (blob === null) reject(new ScreenCaptureFailure('image_decode_failed'))
    else resolve(blob)
  }, type, quality)
})

class ScreenCaptureFailure extends Error {
  constructor(readonly reasonCode: ReasonCode) {
    super(reasonCode)
    this.name = 'ScreenCaptureFailure'
  }
}

export class BrowserScreenCaptureController {
  private state = initialState()
  private stream: MediaStream | null = null
  private track: MediaStreamTrack | null = null
  private removeTrackListeners: (() => void) | null = null
  private cancelPendingFrame: (() => void) | null = null
  private snapshotOperation = 0

  constructor(
    private readonly preview: HTMLVideoElement,
    private readonly notify: (state: ScreenCaptureState) => void,
    private readonly dependencies: CaptureDependencies = browserDependencies(),
  ) {
    this.publish()
  }

  snapshot(): ScreenCaptureState {
    return { ...this.state }
  }

  async selectSurface(requestedSurface: ActualSurface): Promise<void> {
    const generation = this.beginOperation(requestedSurface)
    if (!this.dependencies.isSecureContext()) {
      this.failSelection(generation, 'unsupported', 'insecure_context')
      return
    }
    if (this.dependencies.getDisplayMedia === null) {
      this.failSelection(generation, 'unsupported', 'api_unavailable')
      return
    }
    if (this.dependencies.hasTransientActivation() === false) {
      this.failSelection(generation, 'unavailable', 'transient_activation_required')
      return
    }

    let selected: MediaStream
    try {
      // picker前にawaitせず、clickのtransient activation中に直接呼び出す。
      const selection = this.dependencies.getDisplayMedia(this.displayOptions(requestedSurface))
      selected = await selection
    } catch (error) {
      this.failSelection(generation, 'unavailable', mapCaptureError(error))
      return
    }

    if (generation !== this.state.generation) {
      this.stopTracks(selected)
      return
    }
    const videoTracks = selected.getVideoTracks()
    const audioTracks = selected.getAudioTracks()
    const selectedTrack = videoTracks[0]
    const actual = (selectedTrack?.getSettings() as DisplayVideoTrackSettings | undefined)?.displaySurface
    if (
      videoTracks.length !== 1
      || audioTracks.length !== 0
      || selectedTrack === undefined
      || selectedTrack.readyState !== 'live'
      || actual !== requestedSurface
    ) {
      this.stopTracks(selected)
      this.failSelection(generation, 'unavailable', surfaceReason(actual, requestedSurface))
      return
    }

    this.stream = selected
    this.track = selectedTrack
    this.preview.srcObject = selected
    this.installTrackListeners(selectedTrack, selected, generation)
    try {
      await this.preview.play()
    } catch {
      if (generation === this.state.generation && this.stream === selected) {
        this.releaseCurrent()
        this.failSelection(generation, 'unavailable', 'capture_os_error')
      }
      return
    }
    if (generation !== this.state.generation || this.stream !== selected) {
      this.stopTracks(selected)
      return
    }
    this.update({
      captureState: 'active',
      actualSurface: requestedSurface,
      targetLabel: selectedTrack.label || null,
      trackMuted: selectedTrack.muted,
      reasonCode: null,
    })
  }

  stop(reason: 'user_off' | 'target_change' | 'conversation_change' | 'character_change' | 'consent_revoked' | 'capture_ended' | 'pagehide' | 'backend_disconnect' = 'user_off'): void {
    this.snapshotOperation += 1
    this.state = {
      ...this.state,
      generation: this.state.generation + 1,
      captureState: reason === 'capture_ended' ? 'unavailable' : 'off',
      recognitionState: 'idle',
      reasonCode: reason === 'capture_ended' ? 'frame_unavailable' : null,
      actualSurface: null,
      targetLabel: null,
      trackMuted: false,
    }
    this.releaseCurrent()
    this.publish()
  }

  async captureSnapshot(authorization: AuthorizedSnapshotRequest): Promise<CapturedScreenSnapshot> {
    const { request, clientSessionId } = authorization
    const generation = this.state.generation
    const operation = this.snapshotOperation + 1
    this.snapshotOperation = operation
    this.cancelPendingFrame?.()
    this.cancelPendingFrame = null
    this.update({ recognitionState: 'snapshot_requested', reasonCode: null })
    if (
      this.stream === null
      || this.track === null
      || this.state.captureState !== 'active'
      || this.state.actualSurface === null
    ) throw this.captureFailure('session_revoked')
    if (request.generation !== generation) throw this.captureFailure('generation_mismatch')
    const deadlineMs = Date.parse(request.capture_deadline)
    if (!Number.isFinite(deadlineMs) || deadlineMs <= this.dependencies.now().getTime()) {
      throw this.captureFailure('request_expired', generation, operation)
    }

    this.update({ recognitionState: 'capturing' })
    await this.waitForFreshFrame(generation, operation, this.track, deadlineMs)
    this.assertCurrent(generation, operation, this.track)
    if (deadlineMs <= this.dependencies.now().getTime()) {
      throw this.captureFailure('request_expired', generation, operation)
    }
    const sourceWidth = this.preview.videoWidth
    const sourceHeight = this.preview.videoHeight
    if (sourceWidth < 1 || sourceHeight < 1) throw this.captureFailure('frame_unavailable')
    const dimensions = outputDimensions(sourceWidth, sourceHeight)
    const canvas = this.dependencies.createCanvas()
    canvas.width = dimensions.width
    canvas.height = dimensions.height
    try {
      const context = canvas.getContext('2d')
      if (context === null) throw new ScreenCaptureFailure('image_decode_failed')
      context.drawImage(this.preview, 0, 0, dimensions.width, dimensions.height)
      let blob = await encodeCanvas(canvas, 'image/png')
      let mimeType: 'image/png' | 'image/jpeg' = 'image/png'
      if (blob.size > SCREEN_CAPTURE_LIMITS.maxBytes) {
        mimeType = 'image/jpeg'
        for (const quality of [0.92, 0.8, 0.65, 0.5, 0.35]) {
          blob = await encodeCanvas(canvas, mimeType, quality)
          if (blob.size <= SCREEN_CAPTURE_LIMITS.maxBytes) break
        }
      }
      this.assertCurrent(generation, operation, this.track)
      if (blob.size < 1 || blob.size > SCREEN_CAPTURE_LIMITS.maxBytes) {
        throw new ScreenCaptureFailure('image_too_large')
      }
      const capturedAt = this.dependencies.now().toISOString()
      const metadata: SnapshotUploadMetadata = {
        protocol_version: '1.0',
        type: 'screen_snapshot_upload_metadata',
        event_id: this.dependencies.createUuid(),
        screen_session_id: request.screen_session_id,
        client_session_id: clientSessionId,
        generation,
        request_id: request.request_id,
        turn_id: request.turn_id,
        image_id: this.dependencies.createUuid(),
        actual_surface: this.state.actualSurface,
        captured_at: capturedAt,
        mime_type: mimeType,
        width: dimensions.width,
        height: dimensions.height,
        byte_length: blob.size,
      }
      this.update({ recognitionState: 'uploading', reasonCode: null })
      return { blob, metadata }
    } catch (error) {
      canvas.width = 0
      canvas.height = 0
      if (error instanceof ScreenCaptureFailure) {
        throw this.captureFailure(error.reasonCode, generation, operation)
      }
      throw this.captureFailure('image_decode_failed', generation, operation)
    } finally {
      canvas.width = 0
      canvas.height = 0
    }
  }

  markRecognition(state: Extract<RecognitionState, 'recognizing' | 'composing' | 'succeeded' | 'failed'>, capturedAt?: string): void {
    this.update({
      recognitionState: state,
      reasonCode: state === 'failed' ? this.state.reasonCode : null,
      lastRecognizedCaptureAt: state === 'succeeded' ? capturedAt ?? null : this.state.lastRecognizedCaptureAt,
    })
  }

  failRecognition(reasonCode: ReasonCode): void {
    this.update({ recognitionState: 'failed', reasonCode })
  }

  private beginOperation(requestedSurface: ActualSurface): number {
    const generation = this.state.generation + 1
    this.snapshotOperation += 1
    this.releaseCurrent()
    this.state = {
      ...this.state,
      generation,
      captureState: 'selecting',
      recognitionState: 'idle',
      reasonCode: null,
      requestedSurface,
      actualSurface: null,
      targetLabel: null,
      trackMuted: false,
    }
    this.publish()
    return generation
  }

  private displayOptions(surface: ActualSurface): DisplayMediaStreamOptions {
    return {
      video: { displaySurface: surface },
      audio: false,
      selfBrowserSurface: 'exclude',
      surfaceSwitching: 'exclude',
      monitorTypeSurfaces: surface === 'monitor' ? 'include' : 'exclude',
    } as DisplayMediaStreamOptions
  }

  private installTrackListeners(track: MediaStreamTrack, stream: MediaStream, generation: number): void {
    const ended = () => {
      if (generation === this.state.generation && this.stream === stream) this.stop('capture_ended')
    }
    const muted = () => {
      if (generation === this.state.generation && this.track === track) this.update({ trackMuted: true })
    }
    const unmuted = () => {
      if (generation === this.state.generation && this.track === track) this.update({ trackMuted: false })
    }
    track.addEventListener('ended', ended)
    track.addEventListener('mute', muted)
    track.addEventListener('unmute', unmuted)
    this.removeTrackListeners = () => {
      track.removeEventListener('ended', ended)
      track.removeEventListener('mute', muted)
      track.removeEventListener('unmute', unmuted)
    }
  }

  private async waitForFreshFrame(
    generation: number,
    operation: number,
    track: MediaStreamTrack,
    deadlineMs: number,
  ): Promise<void> {
    if (track.readyState !== 'live' || track.muted) {
      throw this.captureFailure('frame_unavailable', generation, operation)
    }
    const video = this.preview as VideoWithFrameCallback
    if (video.requestVideoFrameCallback === undefined) {
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        throw this.captureFailure('frame_unavailable', generation, operation)
      }
      return
    }
    await new Promise<void>((resolve, reject) => {
      let frameHandle: number | null = null
      let settled = false
      const finish = (operation: () => void) => {
        if (settled) return
        settled = true
        this.dependencies.clearTimer(timeoutHandle)
        this.cancelPendingFrame = null
        operation()
      }
      const remainingMs = Math.max(0, deadlineMs - this.dependencies.now().getTime())
      const currentFrameAvailable = video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      const waitMs = currentFrameAvailable
        ? Math.min(FRESH_FRAME_GRACE_MS, remainingMs)
        : Math.min(SCREEN_CAPTURE_LIMITS.captureTimeoutMs, remainingMs)
      const timeoutHandle = this.dependencies.setTimer(() => {
        if (frameHandle !== null) video.cancelVideoFrameCallback?.(frameHandle)
        if (currentFrameAvailable) finish(resolve)
        else finish(() => reject(new ScreenCaptureFailure(
          remainingMs < SCREEN_CAPTURE_LIMITS.captureTimeoutMs ? 'request_expired' : 'frame_unavailable',
        )))
      }, waitMs)
      this.cancelPendingFrame = () => {
        if (frameHandle !== null) video.cancelVideoFrameCallback?.(frameHandle)
        const reason = generation === this.state.generation && operation !== this.snapshotOperation
          ? 'request_cancelled'
          : 'generation_mismatch'
        finish(() => reject(new ScreenCaptureFailure(reason)))
      }
      frameHandle = video.requestVideoFrameCallback?.(() => {
        finish(resolve)
      }) ?? null
    }).catch((error) => {
      if (error instanceof ScreenCaptureFailure) {
        throw this.captureFailure(error.reasonCode, generation, operation)
      }
      throw error
    })
    this.assertCurrent(generation, operation, track)
  }

  private assertCurrent(generation: number, operation: number, track: MediaStreamTrack): void {
    if (
      generation !== this.state.generation
      || this.track !== track
      || track.readyState !== 'live'
      || track.muted
    ) throw this.captureFailure('generation_mismatch', generation, operation)
    if (operation !== this.snapshotOperation) {
      throw this.captureFailure('request_cancelled', generation, operation)
    }
  }

  private captureFailure(
    reasonCode: ReasonCode,
    generation = this.state.generation,
    operation = this.snapshotOperation,
  ): ScreenCaptureFailure {
    if (generation === this.state.generation && operation === this.snapshotOperation) {
      this.update({ recognitionState: 'failed', reasonCode })
    }
    return new ScreenCaptureFailure(reasonCode)
  }

  private failSelection(generation: number, captureState: CaptureState, reasonCode: ReasonCode): void {
    if (generation !== this.state.generation) return
    this.update({ captureState, recognitionState: 'idle', reasonCode })
  }

  private update(update: Partial<ScreenCaptureState>): void {
    this.state = { ...this.state, ...update }
    this.publish()
  }

  private publish(): void {
    this.notify({ ...this.state })
  }

  private releaseCurrent(): void {
    this.cancelPendingFrame?.()
    this.cancelPendingFrame = null
    this.removeTrackListeners?.()
    this.removeTrackListeners = null
    const current = this.stream
    this.stream = null
    this.track = null
    if (current !== null) this.preview.pause()
    this.preview.srcObject = null
    if (current !== null) this.stopTracks(current)
  }

  private stopTracks(stream: MediaStream): void {
    for (const track of stream.getTracks()) track.stop()
  }
}

export const screenCaptureFailureReason = (error: unknown): ReasonCode => (
  error instanceof ScreenCaptureFailure ? error.reasonCode : 'capture_os_error'
)

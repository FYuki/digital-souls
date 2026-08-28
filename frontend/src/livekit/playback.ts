export interface SegmentMetadata {
  responseId: string
  audioSequence: number
  generation: number
  pcmSampleCount: number
}

export interface RenderedSegment {
  responseId: string
  audioSequence: number
  generation: number
  renderedSampleCount: number
}

type SegmentEvidence = {
  metadataSamples?: number
  renderedSamples?: number
}

export class PlaybackPrefixTracker {
  private generation: number
  private readonly responses = new Map<string, Map<number, SegmentEvidence>>()

  constructor(options: { generation: number }) {
    this.generation = options.generation
  }

  setGeneration(generation: number): void {
    this.generation = generation
    this.responses.clear()
  }

  recordMetadata(metadata: SegmentMetadata): void {
    if (metadata.generation !== this.generation) return
    const evidence = this.evidence(metadata.responseId, metadata.audioSequence)
    evidence.metadataSamples = metadata.pcmSampleCount
  }

  recordRendered(segment: RenderedSegment): void {
    if (segment.generation !== this.generation) return
    const evidence = this.evidence(segment.responseId, segment.audioSequence)
    evidence.renderedSamples = segment.renderedSampleCount
  }

  continuousPrefix(responseId: string): number {
    const segments = this.responses.get(responseId)
    if (segments === undefined) return -1
    let sequence = 0
    while (true) {
      const evidence = segments.get(sequence)
      if (
        evidence?.metadataSamples === undefined
        || evidence.renderedSamples === undefined
        || evidence.metadataSamples !== evidence.renderedSamples
      ) return sequence - 1
      sequence += 1
    }
  }

  private evidence(responseId: string, sequence: number): SegmentEvidence {
    let response = this.responses.get(responseId)
    if (response === undefined) {
      response = new Map<number, SegmentEvidence>()
      this.responses.set(responseId, response)
    }
    let evidence = response.get(sequence)
    if (evidence === undefined) {
      evidence = {}
      response.set(sequence, evidence)
    }
    return evidence
  }
}

export type PlaybackEvidence = Readonly<{
  responseId: string
  continuousPrefix: number
  renderedSamples: number
  renderedEnergy: number
  confirmedSegments: number
  unassignedRenderedSamples: number
}>

type PendingSegment = {
  metadata: SegmentMetadata
  eligibleAfterFrame: number
  renderedSamples: number
  renderedEnergy: number
}

export type RenderInterval = Readonly<{
  startFrame: number
  endFrame: number
  energy: number
}>

export class PlaybackEvidenceController {
  private readonly tracker: PlaybackPrefixTracker
  private readonly pending: PendingSegment[] = []
  private generation: number
  private renderedSamples = 0
  private renderedEnergy = 0
  private confirmedSegments = 0
  private unassignedRenderedSamples = 0

  constructor(
    generation: number,
    private readonly observe: (evidence: PlaybackEvidence) => void,
  ) {
    this.generation = generation
    this.tracker = new PlaybackPrefixTracker({ generation })
  }

  setGeneration(generation: number): void {
    this.generation = generation
    this.tracker.setGeneration(generation)
    this.pending.length = 0
    this.renderedSamples = 0
    this.renderedEnergy = 0
    this.confirmedSegments = 0
    this.unassignedRenderedSamples = 0
  }

  discardResponse(responseId: string): void {
    for (let index = this.pending.length - 1; index >= 0; index -= 1) {
      if (this.pending[index].metadata.responseId === responseId) {
        this.pending.splice(index, 1)
      }
    }
  }

  recordMetadata(metadata: SegmentMetadata, eligibleAfterFrame: number): void {
    if (metadata.generation !== this.generation) return
    if (!Number.isInteger(eligibleAfterFrame) || eligibleAfterFrame < 0) return
    this.tracker.recordMetadata(metadata)
    this.pending.push({
      metadata,
      eligibleAfterFrame,
      renderedSamples: 0,
      renderedEnergy: 0,
    })
  }

  recordRenderedInterval(interval: RenderInterval): void {
    if (
      !Number.isInteger(interval.startFrame)
      || !Number.isInteger(interval.endFrame)
      || interval.startFrame < 0
      || interval.endFrame <= interval.startFrame
      || !Number.isFinite(interval.energy)
      || interval.energy < 0
    ) return
    let cursor = interval.startFrame
    const intervalSamples = interval.endFrame - interval.startFrame
    while (cursor < interval.endFrame) {
      const segment = this.pending[0]
      if (segment === undefined) {
        this.unassignedRenderedSamples += interval.endFrame - cursor
        return
      }
      if (cursor < segment.eligibleAfterFrame) {
        const unassignedEnd = Math.min(interval.endFrame, segment.eligibleAfterFrame)
        this.unassignedRenderedSamples += unassignedEnd - cursor
        cursor = unassignedEnd
        continue
      }
      const required = segment.metadata.pcmSampleCount - segment.renderedSamples
      const consumed = Math.min(required, interval.endFrame - cursor)
      segment.renderedSamples += consumed
      segment.renderedEnergy += interval.energy * (consumed / intervalSamples)
      cursor += consumed
      if (segment.renderedSamples !== segment.metadata.pcmSampleCount) {
        this.observe({
          responseId: segment.metadata.responseId,
          continuousPrefix: this.tracker.continuousPrefix(segment.metadata.responseId),
          renderedSamples: this.renderedSamples + segment.renderedSamples,
          renderedEnergy: this.renderedEnergy + segment.renderedEnergy,
          confirmedSegments: this.confirmedSegments,
          unassignedRenderedSamples: this.unassignedRenderedSamples,
        })
        return
      }
      this.pending.shift()
      this.renderedSamples += segment.metadata.pcmSampleCount
      this.renderedEnergy += segment.renderedEnergy
      this.confirmedSegments += 1
      this.tracker.recordRendered({
        responseId: segment.metadata.responseId,
        audioSequence: segment.metadata.audioSequence,
        generation: segment.metadata.generation,
        renderedSampleCount: segment.metadata.pcmSampleCount,
      })
      this.observe({
        responseId: segment.metadata.responseId,
        continuousPrefix: this.tracker.continuousPrefix(segment.metadata.responseId),
        renderedSamples: this.renderedSamples,
        renderedEnergy: this.renderedEnergy,
        confirmedSegments: this.confirmedSegments,
        unassignedRenderedSamples: this.unassignedRenderedSamples,
      })
    }
  }
}

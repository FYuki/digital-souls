import { describe, expect, test, vi } from 'vitest'

interface SegmentMetadata {
  responseId: string
  audioSequence: number
  generation: number
  pcmSampleCount: number
}

interface RenderedSegment {
  responseId: string
  audioSequence: number
  generation: number
  renderedSampleCount: number
}

interface PlaybackTracker {
  recordMetadata(metadata: SegmentMetadata): void
  recordRendered(segment: RenderedSegment): void
  continuousPrefix(responseId: string): number
}

interface PlaybackModule {
  PlaybackPrefixTracker: new (options: { generation: number }) => PlaybackTracker
  PlaybackEvidenceController: new (
    generation: number,
    observe: (evidence: {
      responseId: string
      continuousPrefix: number
      renderedSamples: number
    }) => void,
  ) => {
    setGeneration(generation: number): void
    recordMetadata(metadata: SegmentMetadata, eligibleAfterFrame: number): void
    recordRenderedInterval(interval: {
      startFrame: number
      endFrame: number
      energy: number
    }): void
  }
}

async function playbackModule(contract: string): Promise<PlaybackModule> {
  try {
    return await vi.importActual<PlaybackModule>('./livekit/playback')
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes('livekit/playback')) {
      throw error
    }
    expect.fail(`livekit/playback must implement ${contract}`)
  }
}

describe('LiveKit rendered playback prefix', () => {
  test('metadataだけまたはrenderだけでは連続prefixを進めない', async () => {
    const { PlaybackPrefixTracker } = await playbackModule('dual-evidence playback confirmation')
    const tracker = new PlaybackPrefixTracker({ generation: 2 })

    tracker.recordMetadata({
      responseId: '30000000-0000-4000-8000-000000000010',
      audioSequence: 0,
      generation: 2,
      pcmSampleCount: 480,
    })
    expect(tracker.continuousPrefix('30000000-0000-4000-8000-000000000010')).toBe(-1)

    tracker.recordRendered({
      responseId: '30000000-0000-4000-8000-000000000011',
      audioSequence: 0,
      generation: 2,
      renderedSampleCount: 480,
    })
    expect(tracker.continuousPrefix('30000000-0000-4000-8000-000000000011')).toBe(-1)
  })

  test('欠番より後がrender済みでも再生済みprefixを進めない', async () => {
    const { PlaybackPrefixTracker } = await playbackModule('continuous-prefix tracking')
    const tracker = new PlaybackPrefixTracker({ generation: 2 })
    const responseId = '30000000-0000-4000-8000-000000000010'
    for (const audioSequence of [0, 2]) {
      tracker.recordMetadata({
        responseId,
        audioSequence,
        generation: 2,
        pcmSampleCount: 480,
      })
      tracker.recordRendered({
        responseId,
        audioSequence,
        generation: 2,
        renderedSampleCount: 480,
      })
    }

    expect(tracker.continuousPrefix(responseId)).toBe(0)
  })

  test('旧generationのmetadataとrenderを受理しない', async () => {
    const { PlaybackPrefixTracker } = await playbackModule('stale-generation rejection')
    const tracker = new PlaybackPrefixTracker({ generation: 2 })
    const responseId = '30000000-0000-4000-8000-000000000010'
    tracker.recordMetadata({
      responseId,
      audioSequence: 0,
      generation: 1,
      pcmSampleCount: 480,
    })
    tracker.recordRendered({
      responseId,
      audioSequence: 0,
      generation: 1,
      renderedSampleCount: 480,
    })

    expect(tracker.continuousPrefix(responseId)).toBe(-1)
  })

  test('metadataより前のrenderを後続segmentへ割り当てない', async () => {
    const { PlaybackEvidenceController } = await playbackModule('metadata-owned render allocation')
    const observations: Array<{ continuousPrefix: number; renderedSamples: number }> = []
    const controller = new PlaybackEvidenceController(2, (evidence) => {
      observations.push(evidence)
    })
    const responseId = '30000000-0000-4000-8000-000000000010'

    controller.recordRenderedInterval({ startFrame: 0, endFrame: 960, energy: 1 })
    expect(observations).toEqual([])
    controller.recordMetadata({
      responseId,
      audioSequence: 0,
      generation: 2,
      pcmSampleCount: 480,
    }, 960)
    controller.recordMetadata({
      responseId,
      audioSequence: 1,
      generation: 2,
      pcmSampleCount: 480,
    }, 1_440)

    expect(observations).toEqual([])
    controller.recordRenderedInterval({ startFrame: 960, endFrame: 1_440, energy: 2 })
    controller.recordRenderedInterval({ startFrame: 1_440, endFrame: 1_920, energy: 3 })

    expect(observations.map(({ continuousPrefix, renderedSamples }) => ({
      continuousPrefix, renderedSamples,
    }))).toEqual([
      { continuousPrefix: 0, renderedSamples: 480 },
      { continuousPrefix: 1, renderedSamples: 960 },
    ])
  })

  test('generation更新時に旧metadataと未割当sampleを破棄する', async () => {
    const { PlaybackEvidenceController } = await playbackModule('generation-owned playback evidence')
    const observations: Array<{ continuousPrefix: number; renderedSamples: number }> = []
    const controller = new PlaybackEvidenceController(1, (evidence) => {
      observations.push(evidence)
    })
    const responseId = '30000000-0000-4000-8000-000000000010'
    controller.recordRenderedInterval({ startFrame: 0, endFrame: 480, energy: 1 })
    controller.recordMetadata({
      responseId,
      audioSequence: 0,
      generation: 0,
      pcmSampleCount: 480,
    }, 0)
    controller.setGeneration(2)
    controller.recordMetadata({
      responseId,
      audioSequence: 0,
      generation: 2,
      pcmSampleCount: 480,
    }, 480)

    expect(observations).toEqual([])
    controller.recordRenderedInterval({ startFrame: 480, endFrame: 960, energy: 2 })
    expect(observations.map(({ continuousPrefix, renderedSamples }) => ({
      continuousPrefix, renderedSamples,
    }))).toEqual([{ continuousPrefix: 0, renderedSamples: 480 }])
  })
})

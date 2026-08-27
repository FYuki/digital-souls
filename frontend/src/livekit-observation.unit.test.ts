import { describe, expect, test, vi } from 'vitest'

type Observation = Readonly<{
  name: string
  value: number
  clockDomain: string
  unit: string
}>

type ObservationModule = Readonly<{
  elapsed: (end: Observation, start: Observation) => Observation
}>

const observationModule = async (contract: string): Promise<ObservationModule> => {
  try {
    return await vi.importActual<ObservationModule>('./livekit/observation')
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes('livekit/observation')) {
      throw error
    }
    expect.fail(`livekit/observation must implement ${contract}`)
  }
}

describe('LiveKit browser observations', () => {
  test('TTFAは同じAudioContext clock上のspeech stoppedからplayback startedまでを測る', async () => {
    const observation = await observationModule('same-clock TTFA measurement')
    const speechStopped = {
      name: 'speech_stopped',
      value: 1_000,
      clockDomain: 'audio_context',
      unit: 'sample',
    }
    const playbackStarted = {
      name: 'playback_started',
      value: 1_480,
      clockDomain: 'audio_context',
      unit: 'sample',
    }

    expect(observation.elapsed(playbackStarted, speechStopped)).toEqual({
      name: 'ttfa',
      value: 480,
      clockDomain: 'audio_context',
      unit: 'sample',
    })
  })

  test('browser clockとserver clockを直接減算しない', async () => {
    const observation = await observationModule('cross-clock subtraction rejection')
    const server = {
      name: 'first_audio_out',
      value: 1_000,
      clockDomain: 'server_monotonic',
      unit: 'millisecond',
    }
    const browser = {
      name: 'playback_started',
      value: 1_010,
      clockDomain: 'audio_context',
      unit: 'millisecond',
    }

    expect(() => observation.elapsed(browser, server)).toThrow()
  })
})

import { render, waitFor } from '@testing-library/svelte'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import AudioPlayer from './AudioPlayer.svelte'

const connect = vi.fn()
const start = vi.fn()
const createBufferSource = vi.fn()
const decodeAudioData = vi.fn()
const close = vi.fn()

class FakeAudioContext {
  destination = {}

  decodeAudioData = decodeAudioData
  createBufferSource = createBufferSource
  close = close
}

describe('AudioPlayer', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    connect.mockReset()
    start.mockReset()
    createBufferSource.mockReset()
    decodeAudioData.mockReset()
    close.mockReset()
    close.mockResolvedValue(undefined)
    createBufferSource.mockReturnValue({ connect, start })
    decodeAudioData.mockResolvedValue({ duration: 1 })
    vi.stubGlobal('AudioContext', FakeAudioContext)
  })

  test('should decode and play received WAV audio data', async () => {
    const audioData = new ArrayBuffer(12)

    render(AudioPlayer, {
      props: {
        request: { audioData, onFirstPlayback: vi.fn() },
        onError: vi.fn(),
      },
    })

    await waitFor(() => expect(decodeAudioData).toHaveBeenCalledWith(audioData.slice(0)))
    expect(createBufferSource).toHaveBeenCalledTimes(1)
    expect(connect).toHaveBeenCalledTimes(1)
    expect(start).toHaveBeenCalledTimes(1)
  })

  test('should report the first playback at the source start boundary', async () => {
    const onFirstPlayback = vi.fn()
    const props = {
      request: { audioData: new ArrayBuffer(12), onFirstPlayback },
      onError: vi.fn(),
    }

    render(AudioPlayer, {
      props,
    })

    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    expect(onFirstPlayback).toHaveBeenCalledTimes(1)
    expect(start.mock.invocationCallOrder[0]).toBeLessThan(
      onFirstPlayback.mock.invocationCallOrder[0],
    )
  })

  test('should not create an AudioContext when there is no audio data', () => {
    render(AudioPlayer, {
      props: {
        request: null,
        onError: vi.fn(),
      },
    })

    expect(decodeAudioData).not.toHaveBeenCalled()
    expect(createBufferSource).not.toHaveBeenCalled()
  })

  test('should report decode failures without starting playback', async () => {
    const audioData = new ArrayBuffer(12)
    const error = new Error('decode failed')
    const onError = vi.fn()
    decodeAudioData.mockRejectedValueOnce(error)

    render(AudioPlayer, {
      props: {
        request: { audioData, onFirstPlayback: vi.fn() },
        onError,
      },
    })

    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(createBufferSource).not.toHaveBeenCalled()
    expect(start).not.toHaveBeenCalled()
  })

  test('should close the owned AudioContext on unmount', async () => {
    const audioData = new ArrayBuffer(12)
    const rendered = render(AudioPlayer, {
      props: {
        request: { audioData, onFirstPlayback: vi.fn() },
        onError: vi.fn(),
      },
    })
    await waitFor(() => expect(decodeAudioData).toHaveBeenCalledWith(audioData.slice(0)))

    rendered.unmount()

    expect(close).toHaveBeenCalledTimes(1)
  })
})

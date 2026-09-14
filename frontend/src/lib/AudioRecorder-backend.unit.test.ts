import {fireEvent, render, screen, waitFor} from '@testing-library/svelte'
import {afterEach, expect, it, vi} from 'vitest'
import AudioRecorder from './AudioRecorder.svelte'

const vadNew = vi.hoisted(() => vi.fn(async () => {throw new Error('VAD model unavailable')}))
vi.mock('@ricky0123/vad-web', () => ({MicVAD: {new: vadNew}}))

afterEach(() => vi.unstubAllGlobals())

it('continuous audio starts without FE VAD and waits for the input owner to enable the track', async () => {
  const track = {enabled: true, stop: vi.fn()}
  const stream = {getTracks: () => [track], getAudioTracks: () => [track]} as unknown as MediaStream
  const getUserMedia = vi.fn(async () => stream)
  vi.stubGlobal('navigator', {mediaDevices: {getUserMedia}})
  let authorize = () => {}
  const onMicrophoneEnabled = vi.fn(async () => {
    expect(track.enabled).toBe(false)
    await new Promise<void>(resolve => {authorize = () => {track.enabled = true; resolve()}})
  })
  const onError = vi.fn()
  const {component} = render(AudioRecorder, {
    continuous: true, disabled: false, forceOff: false, onError,
    onAudioCaptured: vi.fn(), onMicrophoneEnabled,
  })
  await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
  await waitFor(() => expect(onMicrophoneEnabled).toHaveBeenCalledOnce())
  expect(track.enabled).toBe(false)
  expect(vadNew).not.toHaveBeenCalled()
  authorize()
  await waitFor(() => expect(screen.getByRole('button', {name: 'マイクをオフにする'})).toBeTruthy())
  expect(track.enabled).toBe(true)
  expect(onError).not.toHaveBeenCalled()
  await component.$set({suspended: true})
  await waitFor(() => expect(track.enabled).toBe(false))
  await component.$set({suspended: false})
  expect(track.enabled).toBe(false)
  component.$destroy()
})

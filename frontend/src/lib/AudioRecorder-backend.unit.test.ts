import {fireEvent, render, screen, waitFor} from '@testing-library/svelte'
import {afterEach, expect, it, vi} from 'vitest'
import {tick} from 'svelte'
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

it('接続開始の取消が返ったらマイク取得やVADを開始せずOFFを保つ', async () => {
  const getUserMedia = vi.fn()
  vi.stubGlobal('navigator', {mediaDevices: {getUserMedia}})
  let cancel!: (error: Error) => void
  const onBeforeEnable = vi.fn(() => new Promise<void>((_resolve, reject) => {cancel = reject}))
  const onMicrophoneEnabled = vi.fn()
  const onError = vi.fn()
  const {component} = render(AudioRecorder, {
    continuous: true, disabled: false, forceOff: false, onError,
    onAudioCaptured: vi.fn(), onBeforeEnable, onMicrophoneEnabled,
  })
  const button = screen.getByRole('button', {name: 'マイクをオンにする'})
  await fireEvent.click(button)
  await waitFor(() => expect(onBeforeEnable).toHaveBeenCalledOnce())
  expect(getUserMedia).not.toHaveBeenCalled()
  const cancellation = new Error('音声Sessionの開始は取り消されました')
  cancel(cancellation)
  await waitFor(() => expect(onError).toHaveBeenCalledWith(cancellation))
  expect(getUserMedia).not.toHaveBeenCalled()
  expect(onMicrophoneEnabled).not.toHaveBeenCalled()
  expect(vadNew).not.toHaveBeenCalled()
  expect(button.getAttribute('aria-pressed')).toBe('false')
  expect((button as HTMLButtonElement).disabled).toBe(false)
  component.$destroy()
})


function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((done, fail) => {resolve = done; reject = fail})
  return {promise, resolve, reject}
}

async function flushActivation() {
  await tick()
  await new Promise(resolve => setTimeout(resolve, 0))
}

function pendingMicrophone() {
  const track = {enabled: true, stop: vi.fn()}
  const stream = {getTracks: () => [track], getAudioTracks: () => [track]} as unknown as MediaStream
  return {track, stream}
}

it.each(['destroy', 'forceOff'] as const)('準備待ち中の%sは遅れて成功してもdeviceを取得しない', async cancellation => {
  const preparation = deferred<void>()
  const {stream} = pendingMicrophone()
  const getUserMedia = vi.fn(async () => stream)
  vi.stubGlobal('navigator', {mediaDevices: {getUserMedia}})
  const onBeforeEnable = vi.fn(() => preparation.promise)
  const onMicrophoneEnabled = vi.fn()
  const onError = vi.fn()
  const {component} = render(AudioRecorder, {
    continuous: true, disabled: false, forceOff: false,
    onAudioCaptured: vi.fn(), onBeforeEnable, onMicrophoneEnabled, onError,
  })
  await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
  await waitFor(() => expect(onBeforeEnable).toHaveBeenCalledOnce())
  if (cancellation === 'destroy') component.$destroy()
  else {
    await component.$set({forceOff: true})
    await tick()
    // OFFの解除は古い開始操作を復活させない。
    await component.$set({forceOff: false})
    await tick()
  }
  preparation.resolve()
  await flushActivation()
  expect(getUserMedia).not.toHaveBeenCalled()
  expect(onMicrophoneEnabled).not.toHaveBeenCalled()
  expect(onError).not.toHaveBeenCalled()
  if (cancellation !== 'destroy') component.$destroy()
})

it.each(['destroy', 'forceOff'] as const)('マイク許可待ち中の%sは遅着trackを停止して公開しない', async cancellation => {
  const permission = deferred<MediaStream>()
  const {stream, track} = pendingMicrophone()
  const getUserMedia = vi.fn(() => permission.promise)
  vi.stubGlobal('navigator', {mediaDevices: {getUserMedia}})
  const onMicrophoneEnabled = vi.fn()
  const onError = vi.fn()
  const {component} = render(AudioRecorder, {
    continuous: true, disabled: false, forceOff: false,
    onAudioCaptured: vi.fn(), onMicrophoneEnabled, onError,
  })
  await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
  await waitFor(() => expect(getUserMedia).toHaveBeenCalledOnce())
  if (cancellation === 'destroy') component.$destroy()
  else {await component.$set({forceOff: true}); await tick()}
  permission.resolve(stream)
  await flushActivation()
  expect(track.stop).toHaveBeenCalledOnce()
  expect(onMicrophoneEnabled).not.toHaveBeenCalled()
  expect(onError).not.toHaveBeenCalled()
  if (cancellation !== 'destroy') component.$destroy()
})

it('入力ACK待ち中の強制OFFでも直ちにtrackを停止する', async () => {
  const authorization = deferred<void>()
  const {stream, track} = pendingMicrophone()
  vi.stubGlobal('navigator', {mediaDevices: {getUserMedia: vi.fn(async () => stream)}})
  const onMicrophoneEnabled = vi.fn(() => authorization.promise)
  const onError = vi.fn()
  const {component} = render(AudioRecorder, {
    continuous: true, disabled: false, forceOff: false,
    onAudioCaptured: vi.fn(), onMicrophoneEnabled, onError,
  })
  await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
  await waitFor(() => expect(onMicrophoneEnabled).toHaveBeenCalledOnce())
  await component.$set({forceOff: true})
  await tick()
  expect(track.stop).toHaveBeenCalledOnce()
  authorization.resolve()
  await flushActivation()
  expect(screen.getByRole('button', {name: 'マイクをオンにする'}).getAttribute('aria-pressed')).toBe('false')
  expect(onError).not.toHaveBeenCalled()
  component.$destroy()
})

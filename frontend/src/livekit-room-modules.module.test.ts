import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

import { describe, expect, test } from 'vitest'

import { LiveKitRoomClient } from './livekit/room'

// #484: room.tsの責務分割は公開APIを変えない構造整理である。
// ここでは計画されたmoduleの所在とroom.tsのfacade維持だけを観測し、
// 振る舞いはlivekit-room.unit.test.tsが担う。

// 起点版74ef3d4cの`LiveKitRoomClient`公開メンバー集合。
// 内部owner結線のためのgetter・handler・状態公開をこの一覧へ追加しない。
type BaselinePublicApi =
  | 'connect'
  | 'disconnect'
  | 'temporaryDisconnect'
  | 'isAudioProbeReady'
  | 'muteMicrophone'
  | 'probeAudio'
  | 'probeClock'
  | 'probeControl'
  | 'publishControlEvent'
  | 'publishMicrophone'
  | 'setConnectionObserver'
  | 'setCoreDeliveryObserver'
  | 'setDecodedReceiptObserver'
  | 'setPacketOutputObserver'
  | 'setStaleAudioObserver'
  | 'stopPlayback'

type Exactly<Actual, Expected> = [Actual] extends [Expected]
  ? [Expected] extends [Actual]
    ? true
    : never
  : never

// keyof LiveKitRoomClient が起点版の公開面から増減した場合、型検査で失敗する。
const livekitRoomClientPublicApi: Exactly<
  keyof LiveKitRoomClient,
  BaselinePublicApi
> = true
void livekitRoomClientPublicApi

const BASELINE_PUBLIC_METHODS = [
  'connect',
  'disconnect',
  'temporaryDisconnect',
  'isAudioProbeReady',
  'muteMicrophone',
  'probeAudio',
  'probeClock',
  'probeControl',
  'publishControlEvent',
  'publishMicrophone',
  'setConnectionObserver',
  'setCoreDeliveryObserver',
  'setDecodedReceiptObserver',
  'setPacketOutputObserver',
  'setStaleAudioObserver',
  'stopPlayback',
] as const

describe('LiveKit Room module boundary', () => {
  test('room.tsがLiveKitRoomClientの公開facadeを維持する', async () => {
    const room = await import('./livekit/room')
    expect(typeof room.LiveKitRoomClient).toBe('function')
  })

  test('room.tsのruntime exportはLiveKitRoomClientだけを持つ', async () => {
    const room = await import('./livekit/room')
    expect(Object.keys(room).sort()).toEqual(['LiveKitRoomClient'])
  })

  test.each([...BASELINE_PUBLIC_METHODS])(
    '起点版の公開メソッド %s を維持する',
    (name) => {
      expect(
        typeof Reflect.get(LiveKitRoomClient.prototype, name),
      ).toBe('function')
    },
  )

  test.each([
    'room-contract',
    'room-event-wiring',
    'room-recovery',
    'room-control-delivery',
    'room-audio-graph',
  ])('責務module %s が存在する', async (name) => {
    const path = join(import.meta.dirname, 'livekit', `${name}.ts`)
    await expect(readFile(path, 'utf-8')).resolves.toBeTruthy()
  })
})

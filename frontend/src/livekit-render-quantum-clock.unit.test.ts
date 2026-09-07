import {expect, test} from 'vitest'
import {renderQuantumClockSource} from './livekit/render-quantum-clock'
const clock = () => new Function(renderQuantumClockSource + ';return new RenderQuantumClock()')() as {
  read: (frame: number, size: number) => {frame: number; confirmed: boolean} | null
}

test('最初の公開frameが更新されるまでは時計の起点を確定しない', () => {
  const c = clock()
  expect(c.read(0, 128)).toBeNull()
  expect(c.read(0, 128)).toBeNull()
  expect(c.read(256, 128)).toEqual({frame: 256, confirmed: true})
})
test('更新欠落中はquantumを数え、次の公開frameとの一致まで未確認にする', () => {
  const c = clock()
  c.read(0, 128); c.read(128, 128)
  expect(c.read(128, 128)).toEqual({frame: 256, confirmed: false})
  expect(c.read(128, 128)).toEqual({frame: 384, confirmed: false})
  expect(c.read(512, 128)).toEqual({frame: 512, confirmed: true})
})
test.each([384, 640, 0])('計数と一致しない次の公開frameを補正で隠さない（%s）', frame => {
  const c = clock()
  c.read(0, 128); c.read(128, 128); c.read(128, 128); c.read(128, 128)
  expect(() => c.read(frame, 128)).toThrow()
})
test('保留中でない処理の空きは公開frameへ同期する', () => {
  const c = clock()
  c.read(0, 128); c.read(128, 128)
  expect(c.read(1024, 128)).toEqual({frame: 1024, confirmed: true})
})
test('時計の更新が止まり続けても無制限に外挿しない', () => {
  const c = clock()
  c.read(0, 128); c.read(128, 128)
  for (let i = 0; i < 375; i++) expect(c.read(128, 128)?.confirmed).toBe(false)
  expect(() => c.read(128, 128)).toThrow('unreconciled')
})

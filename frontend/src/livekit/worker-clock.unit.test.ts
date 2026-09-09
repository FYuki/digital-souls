import { describe, expect, test } from 'vitest'
import { WorkerClockCalibration } from './worker-clock'

describe('workerとmainの因果的な時計較正', () => {
  test('epoch時計が異なってもmonotonic交換だけでoffsetを求める', () => {
    const clock = new WorkerClockCalibration()
    for (let index = 0; index < 10; index++) clock.add(4700 + index, 10 + index, 4700.1 + index)
    expect(clock.bounds()?.lowerMs).toBeCloseTo(4689.8)
    expect(clock.bounds()?.upperMs).toBeCloseTo(4690.3)
    expect(clock.toMain(1030)?.lowerMs).toBeCloseTo(5719.8)
    expect(clock.toMain(1030)?.upperMs).toBeCloseTo(5720.3)
  })

  test('片道遅延を対称と仮定せず、最も狭い上下限の共通部分を使う', () => {
    const clock = new WorkerClockCalibration()
    for (let index = 0; index < 5; index++) clock.add(100, 10, 110)
    for (let index = 0; index < 5; index++) clock.add(105, 15, 105.1)
    expect(clock.bounds()?.lowerMs).toBeCloseTo(89.8)
    expect(clock.bounds()?.upperMs).toBeCloseTo(90.3)
  })

  test.each(['incomplete', 'wide', 'contradictory', 'nan', 'inverted'])('未確定なclockを返さない: %s', kind => {
    const clock = new WorkerClockCalibration()
    for (let index = 0; index < (kind === 'incomplete' ? 9 : 10); index++) {
      const worker = kind === 'nan' ? NaN : kind === 'contradictory' && index === 9 ? 30 : 10
      clock.add(100, worker, kind === 'wide' ? 110 : kind === 'inverted' ? 99 : 100.1)
    }
    expect(clock.bounds()).toBeUndefined()
    expect(clock.toMain(200)).toBeUndefined()
  })
})

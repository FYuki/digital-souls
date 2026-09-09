// Chromiumはgraph lock競合でglobal currentFrameの更新を見送ることがある。
// quantum数で追跡し、次の更新されたglobal値が一致するまで出力証跡を保留する。
export const renderQuantumClockSource = `
class RenderQuantumClock {
  constructor() {this.lastObserved = null; this.nextFrame = null; this.anchored = false; this.pendingQuanta = 0;}
  read(observed, size) {
    if (!Number.isSafeInteger(observed) || observed < 0 || !Number.isSafeInteger(size) || size < 1
      || (this.lastObserved !== null && observed < this.lastObserved)) throw new Error('render_clock_invalid');
    if (!this.anchored) {
      const advanced = this.lastObserved !== null && observed > this.lastObserved;
      this.lastObserved = observed;
      if (!advanced) {if (++this.pendingQuanta > 375) throw new Error('render_clock_unreconciled'); return null;}
      this.anchored = true; this.pendingQuanta = 0; this.nextFrame = observed + size;
      return {frame: observed, confirmed: true};
    }
    const expected = this.nextFrame;
    if (observed === expected || (observed > expected && this.pendingQuanta === 0)) {
      this.lastObserved = observed; this.nextFrame = observed + size; this.pendingQuanta = 0;
      return {frame: observed, confirmed: true};
    }
    if (observed !== this.lastObserved || observed >= expected || ++this.pendingQuanta > 375) {
      throw new Error('render_clock_unreconciled');
    }
    this.nextFrame = expected + size;
    return {frame: expected, confirmed: false};
  }
}
`

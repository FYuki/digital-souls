// 再生先へ渡したsampleだけを観測する。PCM本体はmain threadへ送らない。
export const renderWorkletSource = `
class RenderEvidenceProcessor extends AudioWorkletProcessor {
  process(inputs, outputs) {
    const input = inputs[0] || []
    const output = outputs[0] || []
    let energy = 0
    let firstAudibleOffset = null
    for (let index = 0; index < output.length; index += 1) {
      const channel = output[index]
      const source = input[index]
      channel.fill(0)
      if (source) channel.set(source.subarray(0, channel.length))
      for (let sample = 0; sample < channel.length; sample += 1) {
        const value = channel[sample]
        energy += value * value
        if (value !== 0 && (firstAudibleOffset === null || sample < firstAudibleOffset)) {
          firstAudibleOffset = sample
        }
      }
    }
    const samples = output[0] ? output[0].length : 0
    if (samples > 0) {
      this.port.postMessage({
        startFrame: currentFrame,
        endFrame: currentFrame + samples,
        energy,
        ...(firstAudibleOffset === null ? {} : {
          firstAudibleFrame: currentFrame + firstAudibleOffset,
        }),
      })
    }
    return true
  }
}
registerProcessor('render-evidence-processor', RenderEvidenceProcessor)
`

// ブラウザが報告する出力時刻をclient monotonic clockへ変換する。
// callback到着時刻を代用しない。出力clockが未確立なら欠測のままにする。
export const outputFrameTimeMs = (
  frame: number,
  sampleRate: number,
  timestamp: AudioTimestamp,
): number | undefined => {
  const { contextTime, performanceTime } = timestamp
  if (
    !Number.isInteger(frame) || frame < 0
    || !Number.isFinite(sampleRate) || sampleRate <= 0
    || contextTime === undefined || !Number.isFinite(contextTime) || contextTime <= 0
    || performanceTime === undefined || !Number.isFinite(performanceTime) || performanceTime <= 0
  ) return undefined
  const value = performanceTime + (frame / sampleRate - contextTime) * 1000
  return Number.isFinite(value) && value >= 0 ? value : undefined
}

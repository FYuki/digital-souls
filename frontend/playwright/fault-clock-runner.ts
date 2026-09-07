import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process'
import {randomUUID} from 'node:crypto'
import {createInterface} from 'node:readline'
import {resolve} from 'node:path'
import type {Page} from '@playwright/test'
import type {ClockSample, FaultClockCalibration} from './fault-clock'

export type FaultEvent = {event: string; timestamp_ns: string; clock_domain: 'fault_runner_monotonic'}

type ClockReply = {event?: unknown; nonce?: unknown; timestamp_ns?: unknown; clock_domain?: unknown}

// 起動時間を較正のRTTへ含めない。ready後のnonce付き要求だけを往復させる。
// pulseは明示された専用bridgeだけへ送る。較正と障害eventを同じ子processから得る。
export class FaultClockRunner {
  private readonly child: ChildProcessWithoutNullStreams
  private readonly lines: AsyncIterator<string>
  private readonly completion: Promise<boolean>
  private failed = false
  private busy = false

  private pulseAttempted = false

  constructor(repositoryRoot: string, private readonly allowFault = false) {
    this.child = spawn(resolve(repositoryRoot, 'backend/.venv/bin/python'),
      [resolve(repositoryRoot, 'scripts/voice_quality/network_fault.py'), '--stdio',
        ...(allowFault ? ['--container', 'ds-voice-quality-fault-livekit-1'] : [])], {stdio: 'pipe'})
    // 子processのstderrは収集・表示しない。診断へ秘密値や例外本文を転記しない。
    this.child.stderr.resume()
    this.lines = createInterface({input: this.child.stdout})[Symbol.asyncIterator]()
    this.completion = new Promise(resolveExit => {
      this.child.once('error', () => {this.failed = true; resolveExit(false)})
      this.child.once('close', code => {this.failed ||= code !== 0; resolveExit(code === 0)})
    })
    this.child.stdin.on('error', () => {this.failed = true})
  }

  private async read(timeoutMs = 3000): Promise<ClockReply> {
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const line = await Promise.race([this.lines.next(), new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error('fault clock response timed out')), timeoutMs)
      })])
      if (this.failed || line.done || line.value.length > 1024) throw new Error('fault clock process unavailable')
      const reply: unknown = JSON.parse(line.value)
      if (!reply || typeof reply !== 'object' || Array.isArray(reply)) throw new Error('invalid fault clock response')
      return reply as ClockReply
    } catch {
      this.failed = true
      throw new Error('fault clock response unavailable')
    } finally {clearTimeout(timer)}
  }

  async ready(): Promise<void> {
    if ((await this.read()).event !== 'fault_runner_ready') throw new Error('fault clock process not ready')
  }

  async sample(): Promise<ClockSample> {
    if (this.busy || this.failed) throw new Error('fault clock request unavailable')
    this.busy = true
    try {
      const nonce = randomUUID(), sentAtMs = performance.now()
      this.child.stdin.write(JSON.stringify({command: 'clock', nonce}) + '\n')
      const reply = await this.read(), receivedAtMs = performance.now()
      if (reply.event !== 'clock_sample' || reply.nonce !== nonce
        || reply.clock_domain !== 'fault_runner_monotonic' || typeof reply.timestamp_ns !== 'string'
        || !/^(0|[1-9][0-9]*)$/.test(reply.timestamp_ns)) throw new Error('fault clock correlation failed')
      const ns = BigInt(reply.timestamp_ns)
      const integerMs = Number(ns / 1000000n)
      if (!Number.isSafeInteger(integerMs)) throw new Error('fault clock timestamp overflow')
      return {sentAtMs, remoteAtMs: integerMs + Number(ns % 1000000n) / 1e6, receivedAtMs}
    } finally {this.busy = false}
  }

  async pulse(report: (event: FaultEvent) => void): Promise<void> {
    if (!this.allowFault || this.busy || this.failed) throw new Error('explicit dedicated fault required')
    this.busy = true
    this.pulseAttempted = true
    try {
      this.child.stdin.write(JSON.stringify({command: 'pulse', duration_ms: 2000}) + '\n')
      let previous = -1n
      for (const name of ['network_link_disconnected', 'network_link_restore_started',
        'network_link_restored', 'signaling_tcp_reachable']) {
        // docker各操作の30秒期限とfinallyの復旧が終わる前に子processをkillしない。
        const event = await this.read(180000)
        if (event.event !== name || event.clock_domain !== 'fault_runner_monotonic'
          || typeof event.timestamp_ns !== 'string' || !/^[0-9]+$/.test(event.timestamp_ns)
          || BigInt(event.timestamp_ns) < previous) throw new Error('fault event sequence invalid')
        previous = BigInt(event.timestamp_ns)
        report(event as FaultEvent)
      }
    } finally {this.busy = false}
  }

  async close(): Promise<boolean> {
    this.child.stdin.end()
    const timer = setTimeout(() => this.child.kill('SIGTERM'), this.pulseAttempted ? 180000 : 3000)
    try {return await this.completion} finally {clearTimeout(timer)}
  }
}

export async function calibrateFaultClock(page: Page, runner: FaultClockRunner): Promise<FaultClockCalibration> {
  const browser: ClockSample[] = [], faultRunner: ClockSample[] = []
  for (let index = 0; index < 5; index++) {
    const sentAtMs = performance.now()
    const remoteAtMs = await page.evaluate(() => performance.now())
    browser.push({sentAtMs, remoteAtMs, receivedAtMs: performance.now()})
    faultRunner.push(await runner.sample())
  }
  return {browser, faultRunner}
}

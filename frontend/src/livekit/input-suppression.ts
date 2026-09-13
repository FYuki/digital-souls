export type InputSuppressionReason = 'manual' | 'text_focus' | 'thread_switch'

/** 音声device操作から独立した入力抑止の理由。明示再開だけが持続muteを解除する。 */
export class InputSuppressionPolicy {
  private readonly reasons = new Set<InputSuppressionReason>(['manual'])

  snapshot(): readonly InputSuppressionReason[] {
    return [...this.reasons]
  }

  get suppressed(): boolean { return this.reasons.size > 0 }

  setFocused(focused: boolean): void {
    if (focused) this.reasons.add('text_focus')
    else this.reasons.delete('text_focus')
  }

  mute(): void { this.reasons.add('manual') }

  switchThread(): void {
    this.reasons.add('thread_switch')
    this.reasons.delete('text_focus')
  }

  resumeExplicitly(): void {
    this.reasons.delete('manual')
    this.reasons.delete('thread_switch')
  }
}

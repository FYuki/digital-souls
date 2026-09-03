import { tick } from 'svelte'

export type ModalDialogOptions = {
  disabled: boolean
  focusableSelector: string
  initialFocusSelector: string
  onCancel: () => void
  returnFocus: HTMLElement | null
  selectInitialText?: boolean
}

export const modalDialog = (node: HTMLElement, initial: ModalDialogOptions) => {
  let options = initial

  const focusableElements = (): HTMLElement[] => [
    ...node.querySelectorAll<HTMLElement>(options.focusableSelector),
  ]

  const focusInitialElement = async (): Promise<void> => {
    await tick()
    const target = node.querySelector<HTMLElement>(options.initialFocusSelector)
    target?.focus()
    if (
      options.selectInitialText === true
      && (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)
    ) target.select()
  }

  const cancelFromOutside = (event: PointerEvent): void => {
    if (!options.disabled && !node.contains(event.target as Node)) options.onCancel()
  }

  const handleKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape' && !options.disabled) {
      event.preventDefault()
      options.onCancel()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = focusableElements()
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (!node.contains(document.activeElement)) {
      event.preventDefault()
      ;(event.shiftKey ? last : first).focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  document.addEventListener('pointerdown', cancelFromOutside)
  window.addEventListener('keydown', handleKeydown)
  void focusInitialElement()

  return {
    update(next: ModalDialogOptions): void {
      options = next
    },
    destroy(): void {
      document.removeEventListener('pointerdown', cancelFromOutside)
      window.removeEventListener('keydown', handleKeydown)
      options.returnFocus?.focus()
    },
  }
}

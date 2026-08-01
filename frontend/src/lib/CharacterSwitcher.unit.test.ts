import { fireEvent, render, screen } from '@testing-library/svelte'
import { describe, expect, test, vi } from 'vitest'

import CharacterSwitcher from './CharacterSwitcher.svelte'

describe('CharacterSwitcher', () => {
  test('should submit a trimmed non-empty character ID', async () => {
    const handleSwitch = vi.fn()
    render(CharacterSwitcher, {
      props: { currentCharacter: 'miori', disabled: false, onSwitch: handleSwitch },
    })
    const input = screen.getByRole<HTMLInputElement>('textbox', { name: 'キャラクターID' })

    expect(input.value).toBe('miori')
    await fireEvent.input(input, { target: { value: '  mock-character-b  ' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))

    expect(handleSwitch).toHaveBeenCalledTimes(1)
    expect(handleSwitch).toHaveBeenCalledWith('mock-character-b')
  })

  test('should reject a whitespace-only character ID', async () => {
    const handleSwitch = vi.fn()
    render(CharacterSwitcher, {
      props: { currentCharacter: 'miori', disabled: false, onSwitch: handleSwitch },
    })
    const input = screen.getByRole('textbox', { name: 'キャラクターID' })

    await fireEvent.input(input, { target: { value: '   ' } })

    expect(screen.getByRole<HTMLButtonElement>('button', { name: '切り替え' }).disabled).toBe(true)
    await fireEvent.submit(input.closest('form') as HTMLFormElement)
    expect(handleSwitch).not.toHaveBeenCalled()
  })

  test('should disable both controls while another request is pending', () => {
    render(CharacterSwitcher, {
      props: { currentCharacter: 'miori', disabled: true, onSwitch: vi.fn() },
    })

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'キャラクターID' }).disabled).toBe(
      true,
    )
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '切り替え' }).disabled).toBe(true)
  })
})

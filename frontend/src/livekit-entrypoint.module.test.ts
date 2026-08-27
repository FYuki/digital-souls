import { cleanup, screen, waitFor } from '@testing-library/svelte'
import { afterEach, describe, expect, test, vi } from 'vitest'

describe('LiveKit experimental entrypoint', () => {
  afterEach(() => {
    cleanup()
    document.body.innerHTML = ''
    window.history.replaceState({}, '', '/')
    vi.resetModules()
  })

  test('/voice/livekit mounts the isolated experimental page', async () => {
    window.history.replaceState({}, '', '/voice/livekit')
    document.body.innerHTML = '<div id="app"></div>'

    await import('./main')

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'LiveKit 音声transport実験' })).toBeTruthy()
    })
    expect(screen.getByRole('button', { name: 'token取得' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Room接続' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '切断' })).toBeTruthy()
    expect(screen.getByText('requested reconnect grace: 60000 ms')).toBeTruthy()
  })
})

import { expect, test } from '@playwright/test'

test.afterEach(async ({ page, context }) => {
  await context.setOffline(false)
  const sessionId = await page.getByTestId('session-id').textContent({ timeout: 500 }).catch(() => null)
  const conversationId = await page.getByTestId('conversation-id').textContent({ timeout: 500 }).catch(() => null)
  if (sessionId?.trim()) {
    await page.evaluate(async (ownedSessionId) => {
      const response = await fetch(`/voice/livekit/sessions/${ownedSessionId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(`session cleanup failed: ${response.status}`)
    }, sessionId)
  }
  if (conversationId?.trim()) {
    await page.evaluate(async (ownedConversationId) => {
      const response = await fetch(`/characters/miori/conversations/${ownedConversationId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(`conversation cleanup failed: ${response.status}`)
    }, conversationId)
  }
})

test('fake microphone and character fixture traverse the real LiveKit room once', async ({ page }) => {
  await page.goto('/voice/livekit')

  await page.getByRole('button', { name: 'token取得' }).click()
  await page.getByRole('button', { name: 'Room接続' }).click()
  await page.getByRole('button', { name: 'microphone開始' }).click()

  await expect(page.getByText('control: available', { exact: true })).toBeVisible()
  await expect(page.getByText('audio: available', { exact: true })).toBeVisible()
  await expect(page.getByText(/microphone frames: [1-9][0-9]*/)).toBeVisible()
  await expect(page.getByText(/microphone samples: [1-9][0-9]*/)).toBeVisible()
  await expect(page.getByText('character rendered samples: 4800', { exact: true })).toBeVisible()
  await expect(page.getByText(/character rendered energy: (?!0(?:\.0+)?$)[0-9.]+/)).toBeVisible()
  await expect(page.getByText('played prefix: 0', { exact: true })).toBeVisible()
  await expect(page.getByText('duplicate track frames: 0', { exact: true })).toBeVisible()
  await expect(page.getByText('active audio graphs: 1', { exact: true })).toBeVisible()
  await expect(page.getByText('confirmed segments: 1', { exact: true })).toBeVisible()
})

test('network interruption resubscribes once and resumes the same session', async ({ page, context }) => {
  await page.goto('/voice/livekit')
  await page.getByRole('button', { name: 'token取得' }).click()
  await page.getByRole('button', { name: 'Room接続' }).click()
  const sessionLocator = page.getByTestId('session-id')
  await expect(sessionLocator).not.toHaveText('')
  const session = await sessionLocator.textContent()
  await expect(page.getByText('acknowledged playback prefix: 0', { exact: true })).toBeVisible()
  const initialResponse = await page.getByText(/active response ID: [0-9a-f-]{36}/).textContent()

  await context.setOffline(true)
  await expect(page.getByText('transport: unavailable', { exact: true })).toBeVisible()
  await context.setOffline(false)

  await expect(page.getByText('transport: available', { exact: true })).toBeVisible()
  await expect(page.getByTestId('session-id')).toHaveText(session as string)
  await expect(page.getByText('generation: 1', { exact: true })).toBeVisible()
  await expect(page.getByText('duplicate track frames: 0', { exact: true })).toBeVisible()
  await expect(page.getByText('active audio graphs: 1', { exact: true })).toBeVisible()
  await expect(page.getByText('confirmed segments: 1', { exact: true })).toBeVisible()
  await expect(page.getByText('character rendered samples: 4800', { exact: true })).toBeVisible()
  await expect(page.getByText('played prefix: 0', { exact: true })).toBeVisible()
  await expect(page.getByText(/terminal response ID: [0-9a-f-]{36}/)).toBeVisible()
  await expect(page.getByText('terminal confirmed audio sequence: 1', { exact: true })).toBeVisible()
  await expect(page.getByText('acknowledged playback prefix: 0', { exact: true })).toBeVisible()
  await expect(page.getByText(`terminal response ID: ${initialResponse?.split(': ')[1]}`, { exact: true })).toBeVisible()
  await expect(page.getByText(/active response ID:/)).not.toHaveText(initialResponse ?? '')
})

import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

import { hardDeleteSelectedConversation } from '../../playwright/conversation-cleanup'
import { attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile } from '../../playwright/resolved-profile'

// 管理用認証はNode側の環境変数だけから使い、ブラウザやartifactへ渡さない。
const readRoom = (room: string): { matching_rooms: number; participants: number } => {
  const python = fileURLToPath(new URL('../../../backend/.venv/bin/python', import.meta.url))
  const source = [
    'import asyncio,json,os,sys',
    'from livekit import api',
    'async def main():',
    ' client=api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("ws://","http://").replace("wss://","https://"),os.environ["LIVEKIT_API_KEY"],os.environ["LIVEKIT_API_SECRET"])',
    ' try:',
    '  result=await client.room.list_rooms(api.ListRoomsRequest(names=[sys.argv[1]]))',
    '  participants=await client.room.list_participants(api.ListParticipantsRequest(room=sys.argv[1])) if result.rooms else None',
    '  print(json.dumps({"matching_rooms":len(result.rooms),"participants":len(participants.participants) if participants else 0}))',
    ' finally: await client.aclose()',
    'try: asyncio.run(main())',
    'except Exception as error: print(json.dumps({"inspection_error":type(error).__name__}))',
  ].join('\n')
  return JSON.parse(execFileSync(python, ['-c', source, room], {
    encoding: 'utf8', timeout: 10_000,
  })) as { matching_rooms: number; participants: number }
}

test('実bootstrap後の接続失敗で作成したSessionとSFU Roomを解放する', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const profile = await readResolvedProfile()
  await attachProfileEvidence(testInfo, profile)
  const reason = getCapabilitySkipReason(profile, 'voice-chat-real')
  if (reason !== null) test.skip(true, reason)
  let sessionId: string | null = null
  let roomName: string | null = null
  let conversationId: string | null = null
  let bootstrapCount = 0
  let bootstrapStatus: number | null = null
  let deliverBinding!: () => void
  const allowFault = new Promise<void>(resolve => { deliverBinding = resolve })
  let clientDeleteCount = 0
  let roomBefore: ReturnType<typeof readRoom> | null = null
  let rejectedConnections = 0
  let faultPort = 0
  // OSに空きportを割り当ててもらい、この試験のsocketだけを接続直後に閉じる。
  const faultServer = createServer(socket => { rejectedConnections += 1; socket.destroy() })
  await page.addInitScript(() => {
    const target = window as typeof window & { __startupMicrophoneCalls: number }
    target.__startupMicrophoneCalls = 0
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    navigator.mediaDevices.getUserMedia = (...args) => {
      target.__startupMicrophoneCalls += 1
      return original(...args)
    }
  })
  page.on('request', request => {
    if (request.method() === 'DELETE' && new URL(request.url()).pathname ===
      `/api/voice/livekit/sessions/${sessionId}`) clientDeleteCount += 1
  })
  await page.route('**/api/voice/livekit/token', async route => {
    bootstrapCount += 1
    const request = route.request().postDataJSON() as { conversation_id: string }
    conversationId = request.conversation_id
    // Session・Room・BE参加者は実BEが作る。障害はFEの接続先だけへ限定する。
    const response = await route.fetch()
    bootstrapStatus = response.status()
    if (bootstrapStatus !== 200) { await route.fulfill({ response }); return }
    const binding = await response.json() as {
      session_id: string; room: string; livekit_url: string
    }
    sessionId = binding.session_id
    roomName = binding.room
    // 検証側で実参加者を確認してから、到達不能な接続先をFEへ返す。
    await allowFault
    await route.fulfill({ response, json: {
      ...binding, livekit_url: 'ws://127.0.0.1:' + faultPort,
    } })
  })
  try {
    await new Promise<void>((resolve, reject) => {
      faultServer.once('error', reject)
      faultServer.listen(0, '127.0.0.1', resolve)
    })
    const address = faultServer.address()
    if (address === null || typeof address === 'string') throw new Error('専用障害portの起動失敗')
    faultPort = address.port
    await page.goto('/')
    await page.getByRole('button', { name: '新規スレッド（光織）' }).click()
    const button = page.getByRole('button', { name: 'マイクをオンにする' })
    await button.click()
    await expect.poll(() => bootstrapStatus, { timeout: 15_000 }).not.toBeNull()
    expect(bootstrapStatus).toBe(200)
    await expect.poll(() => {
      if (roomName === null) return null
      roomBefore = readRoom(roomName)
      return roomBefore
    }, { timeout: 10_000 }).toEqual({ matching_rooms: 1, participants: 1 })
    const ended = page.waitForResponse(response =>
      response.request().method() === 'DELETE'
      && new URL(response.url()).pathname === `/api/voice/livekit/sessions/${sessionId}`,
    { timeout: 60_000 })
    deliverBinding()
    const endedResponse = await ended
    expect(endedResponse.status()).toBe(200)
    expect(await endedResponse.json()).toEqual({ session_id: sessionId, phase: 'ended' })
    await expect(page.getByText('セッション: エラー', { exact: true })).toBeVisible()
    await expect(page.getByText('入力: 停止', { exact: true })).toBeVisible()
    await expect(button).toHaveAttribute('aria-pressed', 'false')
    await expect(button).toBeEnabled()
    if (sessionId === null || roomName === null || conversationId === null) {
      throw new Error('実bootstrapの資源識別子が未取得')
    }
    await expect.poll(() => readRoom(roomName!), { timeout: 10_000 }).toEqual({
      matching_rooms: 0, participants: 0,
    })
    // NodeのAPI clientはpage.routeを経由しない。終了後の実BEの判断を確認する。
    const reconnect = await page.request.post('/api/voice/livekit/token', { data: {
      protocol_version: '2.0', transport_protocol_version: '2.0',
      request_id: randomUUID(), character_id: 'miori', conversation_id: conversationId,
      session_id: sessionId, requested_reconnect_grace_ms: 60000,
    } })
    expect(reconnect.status()).toBe(409)
    expect(await reconnect.json()).toEqual({ detail: { code: 'session_not_reconnectable' } })
    const microphoneCalls = await page.evaluate(() =>
      (window as typeof window & { __startupMicrophoneCalls: number }).__startupMicrophoneCalls)
    const evidence = {
      source: 'real_bootstrap_browser_signaling_reset',
      rejected_connections: rejectedConnections,
      bootstrap_count: bootstrapCount, client_delete_count: clientDeleteCount,
      delete_status: endedResponse.status(), reconnect_status: reconnect.status(),
      room_before: roomBefore, room_after: readRoom(roomName),
      microphone_calls: microphoneCalls,
    }
    await testInfo.attach('startup-cleanup.real.json', {
      body: JSON.stringify(evidence), contentType: 'application/json',
    })
    expect(bootstrapCount).toBe(1)
    expect(clientDeleteCount).toBe(1)
    expect(microphoneCalls).toBe(0)
    expect(rejectedConnections).toBeGreaterThan(0)
  } finally {
    deliverBinding()
    // 故障で本体cleanupが失敗しても、この試験が作ったSessionだけを後始末する。
    // 上の成功判定・件数へこのテスト側のcleanupを加えない。
    if (faultServer.listening) {
      await new Promise<void>((resolve, reject) => faultServer.close(error => error ? reject(error) : resolve()))
    }
    if (sessionId !== null) {
      const cleanup = await page.request.delete(`/api/voice/livekit/sessions/${sessionId}`)
      expect(cleanup.ok()).toBe(true)
    }
    await hardDeleteSelectedConversation(page, 'miori')
  }
})


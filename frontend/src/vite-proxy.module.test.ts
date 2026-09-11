// @vitest-environment node
import { createServer as createHttpServer, type Server } from 'node:http'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createServer as createViteServer, type ViteDevServer } from 'vite'
import { expect, test } from 'vitest'
import { createBackendProxy } from '../vite.proxy'
import { DEPENDENCY_NAMES } from '../resolved-profile'

async function listen(server: Server) {
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('TCP listener required')
  return `http://127.0.0.1:${address.port}`
}

async function close(server: Server) {
  server.closeAllConnections()
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
}

test.each(['requests', 'admin/requests'])('承認後の会話 %s は管理APIの待機上限を超えてもVite経由で回答を受け取れる', async (route) => {
  const directory = await mkdtemp(join(tmpdir(), 'action-proxy-'))
  // 本物のHTTP転送で確認し、待機時間だけ全経路共通で1/100へ短縮する。
  const backend = createHttpServer((request, response) => {
    request.resume()
    const timer = setTimeout(() => response.end(request.url), 800)
    response.on('close', () => clearTimeout(timer))
  })
  let vite: ViteDevServer | undefined
  let frontend: Server | undefined
  try {
    const origin = await listen(backend)
    const report = join(directory, 'profile.json')
    await writeFile(report, JSON.stringify({
      reportSchemaVersion: 1, effectiveProfile: 'test', capabilities: ['text-chat-real'],
      readyGate: { baseUrl: origin, host: '127.0.0.1', port: Number(new URL(origin).port) },
      dependencies: Object.fromEntries(DEPENDENCY_NAMES.map(name => [name,
        name === 'backend' ? { mode: 'real', source: 'managed', baseUrl: origin }
          : { mode: 'disabled', source: null },
      ])),
    }))
    const proxy = createBackendProxy({ DS_PROFILE_REPORT: report, DS_BACKEND_ORIGIN: origin })
    for (const options of Object.values(proxy)) {
      if (options.timeout) options.timeout /= 100
      if (options.proxyTimeout) options.proxyTimeout /= 100
    }
    vite = await createViteServer({
      configFile: false, root: directory, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true, hmr: false, proxy },
    })
    frontend = createHttpServer(vite.middlewares)
    const url = await listen(frontend)
    const path = `/addon-actions/${route}/0f703072-47fa-4fc8-805f-c14779bbb73d`
    for (const query of ['', '?source=chat']) {
      const response = await fetch(`${url}/api${path}/continue${query}`, {
        method: 'POST', body: '{}', signal: AbortSignal.timeout(3000),
      })
      expect(response.ok).toBe(true)
      expect(await response.text()).toBe(`${path}/continue${query}`)
    }
    // 設定保存APIの期限をまとめて延ばしていないことも通信結果で確認する。
    await expect(fetch(`${url}/api${path}/answer`, {
      method: 'POST', body: '{}', signal: AbortSignal.timeout(3000),
    })).rejects.toThrow()
  } finally {
    if (frontend) await close(frontend)
    await vite?.close()
    await close(backend)
    await rm(directory, { recursive: true, force: true })
  }
}, 10_000)

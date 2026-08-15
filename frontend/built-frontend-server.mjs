import { createReadStream, stat } from 'node:fs'
import { createServer, request as createProxyRequest } from 'node:http'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

const { values } = parseArgs({
  options: {
    host: { type: 'string' },
    port: { type: 'string' },
  },
  strict: true,
})

if (values.host === undefined || values.port === undefined) {
  throw new Error('--host and --port are required')
}
if (!/^\d+$/.test(values.port)) {
  throw new Error('--port must be a positive integer')
}
const port = Number(values.port)
if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
  throw new Error('--port must be between 1 and 65535')
}

const backendOriginValue = process.env.DS_BACKEND_ORIGIN
if (backendOriginValue === undefined || backendOriginValue.length === 0) {
  throw new Error('DS_BACKEND_ORIGIN is required')
}
const backendOrigin = new URL(backendOriginValue)
if (backendOrigin.protocol !== 'http:') {
  throw new Error('DS_BACKEND_ORIGIN must use http')
}
const backendProxyTimeoutMs = 30_000

const frontendDirectory = fileURLToPath(new URL('.', import.meta.url))
const distDirectory = resolve(frontendDirectory, 'dist')
const indexPath = resolve(distDirectory, 'index.html')
const contentTypes = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.mjs', 'text/javascript; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.wasm', 'application/wasm'],
  ['.webp', 'image/webp'],
  ['.woff2', 'font/woff2'],
])

const proxyHeaders = (headers) => ({
  ...headers,
  host: backendOrigin.host,
})

const proxyHttp = (incoming, outgoing, targetPath) => {
  let timedOut = false
  const proxy = createProxyRequest(
    backendOrigin,
    {
      method: incoming.method,
      path: targetPath,
      headers: proxyHeaders(incoming.headers),
    },
    (response) => {
      outgoing.writeHead(response.statusCode ?? 502, response.headers)
      response.pipe(outgoing)
    },
  )
  const timeout = setTimeout(() => {
    timedOut = true
    proxy.destroy(new Error('Backend proxy timed out'))
  }, backendProxyTimeoutMs)
  timeout.unref()
  proxy.once('close', () => clearTimeout(timeout))
  proxy.on('error', (error) => {
    if (outgoing.destroyed || outgoing.writableEnded) return
    if (outgoing.headersSent) {
      outgoing.destroy()
      return
    }
    outgoing.writeHead(timedOut ? 504 : 502)
    outgoing.end(`Backend proxy failed: ${error.message}`)
  })
  const abortProxy = () => proxy.destroy()
  incoming.on('aborted', abortProxy)
  incoming.on('error', abortProxy)
  outgoing.on('close', () => {
    if (!outgoing.writableEnded) abortProxy()
  })
  incoming.pipe(proxy)
}

const serveFile = (request, response, path) => {
  stat(path, (error, details) => {
    if (error !== null || !details.isFile()) {
      if (path !== indexPath && (error === null || error.code === 'ENOENT')) {
        serveFile(request, response, indexPath)
        return
      }
      response.writeHead(error?.code === 'EACCES' ? 500 : 404)
      response.end()
      return
    }
    response.writeHead(200, {
      'Content-Length': details.size,
      'Content-Type': contentTypes.get(extname(path)) ?? 'application/octet-stream',
    })
    if (request.method === 'HEAD') {
      response.end()
      return
    }
    const stream = createReadStream(path)
    stream.on('error', () => response.destroy())
    stream.pipe(response)
  })
}

const resolveStaticPath = (pathname) => {
  let decoded
  try {
    decoded = decodeURIComponent(pathname)
  } catch {
    return undefined
  }
  if (decoded.includes('\0')) {
    return undefined
  }
  const candidate = resolve(distDirectory, `.${decoded}`)
  if (candidate !== distDirectory && !candidate.startsWith(`${distDirectory}${sep}`)) {
    return undefined
  }
  return candidate === distDirectory ? indexPath : candidate
}

const server = createServer((request, response) => {
  if (request.url === undefined) {
    response.writeHead(400).end()
    return
  }
  const requestUrl = new URL(request.url, 'http://frontend.invalid')
  if (requestUrl.pathname === '/api' || requestUrl.pathname.startsWith('/api/')) {
    const rewrittenPath = `${requestUrl.pathname.slice(4) || '/'}${requestUrl.search}`
    proxyHttp(request, response, rewrittenPath)
    return
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405, { Allow: 'GET, HEAD' }).end()
    return
  }
  const staticPath = resolveStaticPath(requestUrl.pathname)
  if (staticPath === undefined) {
    response.writeHead(400).end()
    return
  }
  serveFile(request, response, staticPath)
})

server.on('upgrade', (request, socket, head) => {
  socket.on('error', () => socket.destroy())
  if (request.url === undefined) {
    socket.destroy()
    return
  }
  const requestUrl = new URL(request.url, 'http://frontend.invalid')
  if (requestUrl.pathname !== '/ws' && !requestUrl.pathname.startsWith('/ws/')) {
    socket.destroy()
    return
  }
  const proxy = createProxyRequest(backendOrigin, {
    method: request.method,
    path: `${requestUrl.pathname}${requestUrl.search}`,
    headers: proxyHeaders(request.headers),
  })
  proxy.on('upgrade', (response, proxySocket, proxyHead) => {
    proxySocket.on('error', () => {
      proxySocket.destroy()
      socket.destroy()
    })
    socket.on('close', () => proxySocket.destroy())
    proxySocket.on('close', () => socket.destroy())
    const headers = response.rawHeaders.reduce(
      (lines, value, index) => index % 2 === 0
        ? [...lines, `${value}: ${response.rawHeaders[index + 1]}`]
        : lines,
      [],
    )
    socket.write(
      `HTTP/${response.httpVersion} ${response.statusCode} ${response.statusMessage}\r\n${headers.join('\r\n')}\r\n\r\n`,
    )
    if (proxyHead.length > 0) socket.write(proxyHead)
    if (head.length > 0) proxySocket.write(head)
    proxySocket.pipe(socket)
    socket.pipe(proxySocket)
  })
  proxy.on('error', () => socket.destroy())
  proxy.end()
})

server.on('error', (error) => {
  process.stderr.write(`frontend server failed: ${error.message}\n`)
  process.exitCode = 1
})
server.listen(port, values.host)

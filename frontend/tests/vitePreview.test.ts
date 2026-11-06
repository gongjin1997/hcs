import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Connect, Plugin, PreviewServer } from 'vite'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import config from '../vite.config'

let middleware: Connect.NextHandleFunction

beforeEach(() => {
  const plugin = config.plugins?.find((item) => item && typeof item === 'object' && 'name' in item && item.name === 'private-source-maps') as Plugin | undefined
  const hook = plugin?.configurePreviewServer
  if (!hook) throw new Error('preview source-map guard missing')
  const configure = typeof hook === 'function' ? hook : hook.handler
  const use = vi.fn((handler: Connect.NextHandleFunction) => { middleware = handler })
  const result = configure({ middlewares: { use } } as unknown as PreviewServer)
  // 返回 post hook 会在静态文件中间件之后执行，无法保护真实 map 文件。
  expect(result).toBeUndefined()
  expect(use).toHaveBeenCalledOnce()
})

function request(url: string | undefined, method = 'GET') {
  const req = { url, method } as IncomingMessage
  const res = { statusCode: 200, setHeader: vi.fn(), end: vi.fn() }
  const next = vi.fn()
  middleware(req, res as unknown as ServerResponse, next)
  return { req, res, next }
}

describe('preview保留本地source map但拒绝HTTP读取', () => {
  it('构建仅启用hidden map，现有CSP和nosniff配置保留', () => {
    expect(config.build?.sourcemap).toBe('hidden')
    expect(config.preview?.headers).toMatchObject({
      'Content-Security-Policy': expect.stringContaining("script-src 'self'"),
      'X-Content-Type-Options': 'nosniff',
    })
  })
  it.each([
    '/assets/index-A1b2c3d4.js.map',
    '/assets/index.js.map?download=1',
    '/assets/index.js.map#ignored',
    '/assets/index.js%2Emap',
    '/assets/index.js.%6D%61%70?download=1',
    '/assets/index.js.map/',
    '/assets/index.js.map//',
    '/assets/index.js.map/.',
    '/assets/index.js.map%2F',
    '/assets/index.js.map%5C',
    '/assets/index.js.MAP',
    '/index.js.map',
    '/assets/nested%2Findex.js.map',
    '//assets/index.js.map/',
  ])('请求 %s 在静态服务前直接返回404', (url) => {
    const { req, res, next } = request(url)
    expect(res.statusCode).toBe(404)
    expect(res.setHeader).toHaveBeenCalledExactlyOnceWith('Cache-Control', 'no-store')
    expect(res.end).toHaveBeenCalledExactlyOnceWith()
    expect(next).not.toHaveBeenCalled()
    expect(req.url).toBe(url)
  })
  it.each(['HEAD', 'OPTIONS'])('%s请求同样不能读取map', (method) => {
    const { res, next } = request('/assets/index.js.map', method)
    expect(res.statusCode).toBe(404)
    expect(next).not.toHaveBeenCalled()
  })
  it.each([
    undefined,
    '/',
    '/rules',
    '/assets/index-A1b2c3d4.js',
    '/assets/index.js?source=index.js.map',
    '/assets/index.js?query=%E0%A4',
    '/assets/style.css',
    '/guides?type=memory.map',
    '/api/v1/dcs/history?keyword=test.map',
    '/api/v1/diagnostics/client-errors',
    '/assets/map-icon.svg',
    '/assets/index.js.mapping',
  ])('普通页面、API与资源 %s 正常放行', (url) => {
    const { req, res, next } = request(url)
    expect(next).toHaveBeenCalledExactlyOnceWith()
    expect(res.statusCode).toBe(200)
    expect(res.end).not.toHaveBeenCalled()
    expect(res.setHeader).not.toHaveBeenCalled()
    expect(req.url).toBe(url)
  })
  it.each(['/assets/index.js%ZZ.map', '/assets/%E0%A4.map', 'http://[invalid'])('非法URL %s 安全结束，不进入静态服务', (url) => {
    const { res, next } = request(url)
    expect(res.statusCode).toBe(400)
    expect(res.end).toHaveBeenCalledExactlyOnceWith()
    expect(next).not.toHaveBeenCalled()
  })
})

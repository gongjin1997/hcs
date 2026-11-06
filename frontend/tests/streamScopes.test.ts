import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'
import * as Vue from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { useAlarmStream as StreamFactory } from '../src/composables/useAlarmStream'

class Source {
  static instances: Source[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  listeners: Record<string, (event: { data: string }) => void> = {}
  close = vi.fn()
  constructor(public url: string) { Source.instances.push(this) }
  addEventListener(name: string, callback: (event: { data: string }) => void) { this.listeners[name] = callback }
  alarm(tenant: number) { this.listeners.alarm?.({ data: JSON.stringify({ type: 'FIRE', alarm: { tenant_id: tenant } }) }) }
}
const storage = new Map<string, string>()
beforeEach(() => {
  Source.instances = []
  storage.clear(); storage.set('hi_token', 'test-access'); storage.set('hi_tenant', '1')
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) ?? null })
  vi.stubGlobal('EventSource', Source)
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })
const flush = async () => { await Promise.resolve(); await Promise.resolve() }
function deferred() {
  let resolve!: (value: { ticket: string }) => void
  let reject!: (error: Error) => void
  const promise = new Promise<{ ticket: string }>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
function setup(enabled: () => boolean = () => true) {
  const mounted: (() => void)[] = [], unmounted: (() => void)[] = []
  const ticket = vi.fn(async () => ({ ticket: 'test-ticket' }))
  const callback = vi.fn()
  const source = readFileSync(resolve(import.meta.dirname, '../src/composables/useAlarmStream.ts'), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } })
  const output: { useAlarmStream?: typeof StreamFactory } = {}
  const dependencies = (name: string) => {
    if (name === 'vue') return { ...Vue, onMounted: (cb: () => void) => mounted.push(cb), onBeforeUnmount: (cb: () => void) => unmounted.push(cb) }
    if (name === '@/api') return { streamTicket: ticket }
    if (name === '@/api/request') return { TENANT_KEY: 'hi_tenant', TOKEN_KEY: 'hi_token' }
    throw new Error('Unexpected dependency: ' + name)
  }
  new Function('require', 'exports', outputText)(dependencies, output)
  const stream = output.useAlarmStream!(callback, enabled)
  return { stream, ticket, callback, mount: () => mounted.forEach(cb => cb()), unmount: () => unmounted.forEach(cb => cb()) }
}

describe('tenant-scoped alarm streams', () => {
  it('does not request tickets without alarm permission', async () => {
    const { ticket, mount, unmount } = setup(() => false)
    mount(); await flush()
    expect(ticket).not.toHaveBeenCalled()
    expect(Source.instances).toHaveLength(0)
    unmount()
  })
  it('does not open a pending ticket after permission is removed', async () => {
    let allowed = true
    const { ticket, mount, unmount } = setup(() => allowed), pending = deferred()
    ticket.mockReturnValueOnce(pending.promise)
    mount(); allowed = false; pending.resolve({ ticket: 'late' }); await flush()
    expect(Source.instances).toHaveLength(0)
    unmount()
  })
  it('captures the tenant when requesting a ticket', async () => {
    const { ticket, mount, unmount } = setup(), pending = deferred()
    ticket.mockReturnValueOnce(pending.promise)
    mount(); storage.set('hi_tenant', '2'); pending.resolve({ ticket: 'first' }); await flush()
    expect(Source.instances[0].url).toContain('tenant=1')
    unmount()
  })
  it('discards a pending ticket from the previous tenant after reopening', async () => {
    const { stream, ticket, mount, unmount } = setup(), old = deferred(), next = deferred()
    ticket.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    mount(); storage.set('hi_tenant', '2'); stream.reopen()
    old.resolve({ ticket: 'old' }); await flush()
    expect(Source.instances).toHaveLength(0)
    next.resolve({ ticket: 'new' }); await flush()
    expect(Source.instances).toHaveLength(1)
    expect(Source.instances[0].url).toContain('ticket=new')
    expect(Source.instances[0].url).toContain('tenant=2')
    unmount()
  })
  it('ignores queued events from a closed tenant connection', async () => {
    const { stream, callback, mount, unmount } = setup()
    mount(); await flush(); const old = Source.instances[0]
    storage.set('hi_tenant', '2'); stream.reopen(); await flush()
    old.alarm(1)
    expect(callback).not.toHaveBeenCalled()
    Source.instances[1].alarm(2)
    expect(callback).toHaveBeenCalledOnce()
    expect(callback.mock.calls[0][0].alarm.tenant_id).toBe(2)
    unmount()
  })
  it('does not mark a closed connection as connected', async () => {
    const { stream, mount, unmount } = setup()
    mount(); await flush(); const old = Source.instances[0]
    stream.reopen(); old.onopen?.()
    expect(stream.connected.value).toBe(false)
    await flush(); Source.instances[1].onopen?.()
    expect(stream.connected.value).toBe(true)
    unmount()
  })
  it('does not open a pending connection after unmounting', async () => {
    const { ticket, mount, unmount } = setup(), pending = deferred()
    ticket.mockReturnValueOnce(pending.promise)
    mount(); unmount(); pending.resolve({ ticket: 'late' }); await flush()
    expect(Source.instances).toHaveLength(0)
  })
  it('ignores messages after unmounting', async () => {
    const { callback, mount, unmount } = setup()
    mount(); await flush(); const old = Source.instances[0]
    unmount(); old.alarm(1)
    expect(callback).not.toHaveBeenCalled()
  })
  it('reconnects a failed current connection with a fresh ticket', async () => {
    vi.useFakeTimers()
    const { ticket, mount, unmount } = setup()
    mount(); await flush(); Source.instances[0].onerror?.()
    expect(Source.instances[0].close).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(5000)
    expect(ticket).toHaveBeenCalledTimes(2)
    expect(Source.instances).toHaveLength(2)
    unmount()
  })
  it('does not schedule retries for a stale ticket failure', async () => {
    vi.useFakeTimers()
    const { stream, ticket, mount, unmount } = setup(), old = deferred()
    ticket.mockReturnValueOnce(old.promise)
    mount(); stream.reopen(); await flush()
    old.reject(new Error('old connection')); await flush()
    expect(vi.getTimerCount()).toBe(0)
    unmount()
  })
})

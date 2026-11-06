import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type EffectScope } from 'vue'
import { useLatestRequest } from '../src/composables/useLatestRequest'

const scopes: EffectScope[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
function setup() {
  const scope = effectScope()
  scopes.push(scope)
  const request = scope.run(useLatestRequest)!
  const value = ref('')
  const run = () => {
    const pending = deferred<string>()
    const done = request.run(() => pending.promise, result => { value.value = result })
    return { ...pending, done }
  }
  return { scope, request, value, run }
}

describe('latest detail requests', () => {
  it('keeps the selected detail when an older response arrives later', async () => {
    const { value, run } = setup()
    const old = run(), latest = run()
    latest.resolve('resource B')
    expect(await latest.done).toBe(true)
    old.resolve('resource A')
    expect(await old.done).toBe(false)
    expect(value.value).toBe('resource B')
  })
  it('keeps loading until the latest request settles', async () => {
    const { request, run } = setup()
    const old = run(), latest = run()
    old.resolve('old')
    await old.done
    expect(request.loading.value).toBe(true)
    latest.resolve('new')
    await latest.done
    expect(request.loading.value).toBe(false)
  })
  it('ignores stale errors', async () => {
    const { run, value } = setup()
    const old = run(), latest = run()
    old.reject(new Error('old request failed'))
    expect(await old.done).toBe(false)
    latest.resolve('new')
    await latest.done
    expect(value.value).toBe('new')
  })
  it('propagates current failures and releases loading', async () => {
    const { request, run } = setup()
    const pending = run()
    pending.reject(new Error('current request failed'))
    await expect(pending.done).rejects.toThrow('current request failed')
    expect(request.loading.value).toBe(false)
  })
  it('does not repopulate a cleared panel', async () => {
    const { request, run, value } = setup()
    const pending = run()
    request.invalidate()
    pending.resolve('closed detail')
    expect(await pending.done).toBe(false)
    expect(value.value).toBe('')
    expect(request.loading.value).toBe(false)
  })
  it('supports another selection after invalidation', async () => {
    const { request, run, value } = setup()
    request.invalidate()
    const pending = run()
    pending.resolve('selected')
    expect(await pending.done).toBe(true)
    expect(value.value).toBe('selected')
  })
  it('does not update a disposed component', async () => {
    const { scope, run, value, request } = setup()
    const pending = run()
    scope.stop()
    pending.resolve('late')
    expect(await pending.done).toBe(false)
    expect(value.value).toBe('')
    expect(request.loading.value).toBe(false)
  })
  it('does not start requests after disposal', async () => {
    const { scope, request } = setup()
    scope.stop()
    const loader = vi.fn(async () => 'never')
    expect(await request.run(loader, () => {})).toBe(false)
    expect(loader).not.toHaveBeenCalled()
  })
})

import { afterEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, type EffectScope } from 'vue'
import { useTable } from '../src/composables/useTable'

type Result = { items: string[]; total: number; page: number; page_size: number }
const scopes: EffectScope[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })

function setup() {
  const requests: { resolve: (value: Result) => void; reject: (error: Error) => void }[] = []
  const scope = effectScope()
  scopes.push(scope)
  const table = scope.run(() => useTable<string, { keyword: string }>(
    () => new Promise((resolve, reject) => requests.push({ resolve, reject })), { keyword: '' },
  ))!
  const finish = (index: number, item: string) => requests[index].resolve({ items: [item], total: 1, page: 1, page_size: 20 })
  return { table, requests, scope, finish }
}

describe('useTable request ordering', () => {
  it('keeps the latest response when an old request arrives later', async () => {
    const { table, finish } = setup()
    const old = table.load()
    const latest = table.load()
    finish(1, 'new')
    await latest
    finish(0, 'old')
    await old
    expect(table.rows.value).toEqual(['new'])
  })
  it('keeps loading until the latest request settles', async () => {
    const { table, finish } = setup()
    const old = table.load()
    const latest = table.load()
    finish(0, 'old')
    await old
    expect(table.loading.value).toBe(true)
    finish(1, 'new')
    await latest
    expect(table.loading.value).toBe(false)
  })
  it('ignores stale errors but propagates the current error', async () => {
    const { table, requests, finish } = setup()
    const old = table.load()
    const latest = table.load()
    requests[0].reject(new Error('stale'))
    await expect(old).resolves.toBeUndefined()
    finish(1, 'new')
    await latest
    const failed = table.load()
    requests[2].reject(new Error('latest'))
    await expect(failed).rejects.toThrow('latest')
    expect(table.loading.value).toBe(false)
  })
  it('does not update a disposed table', async () => {
    const { table, scope, finish } = setup()
    const loading = table.load()
    scope.stop()
    finish(0, 'late')
    await loading
    expect(table.rows.value).toEqual([])
    expect(table.loading.value).toBe(false)
  })
  it('searching from a later page issues one request', async () => {
    const { table, requests, finish } = setup()
    table.page.value = 2
    await nextTick()
    finish(0, 'page 2')
    await nextTick()
    const search = table.search()
    await nextTick()
    expect(requests).toHaveLength(2)
    finish(1, 'page 1')
    await search
    expect(table.page.value).toBe(1)
  })
})

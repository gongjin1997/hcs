import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, shallowRef } from 'vue'
import { useTaskScopedResource } from '../src/views/system/composables/useTaskScopedResource'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()); vi.useRealTimers() })
const settle = async () => { await Promise.resolve(); await nextTick(); await Promise.resolve() }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function setup<T>(key: string | null, loader: (key: string, signal: AbortSignal) => Promise<T>, interval = 0) {
  const context = shallowRef(key)
  const scope = effectScope()
  dispose.push(() => scope.stop())
  const resource = scope.run(() => useTaskScopedResource(context, loader, interval))!
  return { ...resource, context, stop: () => scope.stop() }
}

describe('任务工作区的查询隔离与轮询', () => {
  it('租户 / 项目变化立即清空旧数据，取消旧请求并丢弃迟到结果', async () => {
    const first = deferred<string[]>(), second = deferred<string[]>()
    const requests: AbortSignal[] = []
    const loader = vi.fn((key: string, signal: AbortSignal) => { requests.push(signal); return key === '7:11' ? first.promise : second.promise })
    const feed = setup('7:11', loader)
    expect(feed.loading.value).toBe(true)
    feed.context.value = '8:12'
    expect(requests[0]!.aborted).toBe(true)
    expect(feed.data.value).toBeNull()
    second.resolve(['项目12执行']); await settle()
    first.resolve(['旧项目执行']); await settle()
    expect(feed.data.value).toEqual(['项目12执行'])
    expect(feed.loading.value).toBe(false)
  })

  it('旧范围的失败不覆盖新范围，重复刷新只接受最新响应', async () => {
    const requests = [deferred<number>(), deferred<number>(), deferred<number>()]
    let index = 0
    const feed = setup('7:11', () => requests[index++]!.promise)
    feed.context.value = '8:12'
    const latest = feed.refresh()
    requests[2]!.resolve(3); await latest
    requests[1]!.resolve(2); requests[0]!.reject(new Error('旧租户失败')); await settle()
    expect(feed.data.value).toBe(3)
    expect(feed.error.value).toBeNull()
  })

  it('查询失败保留已获取的数据和后端状态，刷新成功后清除查询错误', async () => {
    const loader = vi.fn<() => Promise<{ status: string }>>()
      .mockResolvedValueOnce({ status: 'RUNNING' })
      .mockRejectedValueOnce(new Error('网络中断'))
      .mockResolvedValueOnce({ status: 'RUNNING' })
    const feed = setup('7:11', loader)
    await settle()
    await feed.refresh()
    expect(feed.data.value).toEqual({ status: 'RUNNING' })
    expect(feed.error.value).toBeInstanceOf(Error)
    await feed.refresh()
    expect(feed.error.value).toBeNull()
  })

  it('慢请求期间不叠加轮询；停止观察后不再发起查询', async () => {
    vi.useFakeTimers()
    const pending = deferred<string>()
    const loader = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue('服务器状态')
    const feed = setup('7:11', loader, 1000)
    await vi.advanceTimersByTimeAsync(10000)
    expect(loader).toHaveBeenCalledTimes(1)
    pending.resolve('服务器状态'); await settle()
    await vi.advanceTimersByTimeAsync(1000)
    expect(loader).toHaveBeenCalledTimes(2)
    feed.stop()
    await vi.advanceTimersByTimeAsync(10000)
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('关闭详情的空上下文不查询，卸载后的迟到响应不能恢复数据', async () => {
    const pending = deferred<string>()
    let signal: AbortSignal | undefined
    const loader = vi.fn((_key: string, value: AbortSignal) => { signal = value; return pending.promise })
    const feed = setup(null, loader)
    expect(loader).not.toHaveBeenCalled()
    feed.context.value = '7:11:execution'
    feed.stop()
    expect(signal?.aborted).toBe(true)
    pending.resolve('迟到详情'); await settle()
    expect(feed.data.value).toBeNull()
    await feed.refresh()
    expect(loader).toHaveBeenCalledTimes(1)
  })
})

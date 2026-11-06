import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, reactive } from 'vue'
import { useMonitoringCoverage } from '../src/views/monitoring/useMonitoringCoverage'
import type { MonitoringCoverage } from '../src/types/monitoring'

const disposers: (() => void)[] = []
afterEach(() => disposers.splice(0).forEach(dispose => dispose()))
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (cause: Error) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
const result = (time: string): MonitoringCoverage => ({ evaluated_at: time, rows: [] })
function fixture() {
  const scope = effectScope(), selection = reactive({ tenant: null as number | null, permission: 'ledger:view' })
  const calls: { tenantId: number | null; signal: AbortSignal; request: ReturnType<typeof deferred<MonitoringCoverage>> }[] = []
  const fetcher = vi.fn((tenantId: number | null, signal: AbortSignal) => {
    const request = deferred<MonitoringCoverage>(); calls.push({ tenantId, signal, request }); return request.promise
  })
  const model = scope.run(() => useMonitoringCoverage({ tenantId: () => selection.tenant, permissionKey: () => selection.permission, fetcher }))!
  disposers.push(() => scope.stop())
  return { model, selection, calls, scope, fetcher }
}

describe('监控覆盖的范围与请求生命周期', () => {
  it('切换租户取消旧请求，迟到结果不能恢复其他租户数据', async () => {
    const { model, selection, calls } = fixture()
    selection.tenant = 7
    expect(calls[0].signal.aborted).toBe(true)
    expect(calls[1].tenantId).toBe(7)
    calls[1].request.resolve(result('new')); await settle()
    calls[0].request.resolve(result('old')); await settle()
    expect(model.data.value?.evaluated_at).toBe('new')
    expect(model.loading.value).toBe(false)
  })

  it('撤销权限立即清空数据，未获授权时不发送查询', async () => {
    const { model, selection, calls, fetcher } = fixture()
    calls[0].request.resolve(result('before')); await settle()
    selection.permission = ''
    expect(model.data.value).toBeNull()
    expect(calls[0].signal.aborted).toBe(true)
    await model.refresh()
    expect(fetcher).toHaveBeenCalledOnce()
    selection.permission = 'report:view'
    expect(fetcher).toHaveBeenCalledTimes(2)
    calls[1].request.resolve(result('after')); await settle()
    expect(model.data.value?.evaluated_at).toBe('after')
  })

  it('局部撤权重新请求，旧范围响应不覆盖新权限结果', async () => {
    const { model, selection, calls } = fixture()
    selection.permission = 'ledger:view,report:view'
    selection.permission = 'report:view'
    calls[2].request.resolve(result('report')); await settle()
    calls[1].request.resolve(result('all')); calls[0].request.resolve(result('ledger')); await settle()
    expect(model.data.value?.evaluated_at).toBe('report')
    expect(calls[1].signal.aborted).toBe(true)
  })

  it('查询失败保留明确错误，刷新成功后清除错误', async () => {
    const { model, calls } = fixture()
    calls[0].request.reject(new Error('本地查询失败')); await settle()
    expect(model.error.value).toBe('本地查询失败')
    expect(model.data.value).toBeNull()
    const pending = model.refresh()
    calls[1].request.resolve(result('retry')); await pending
    expect(model.error.value).toBe('')
    expect(model.data.value?.evaluated_at).toBe('retry')
  })

  it('卸载页面取消请求且不回填', async () => {
    const { model, calls, scope } = fixture()
    scope.stop()
    expect(calls[0].signal.aborted).toBe(true)
    calls[0].request.resolve(result('late')); await settle()
    expect(model.data.value).toBeNull()
  })
})

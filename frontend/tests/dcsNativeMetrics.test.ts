import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { freshnessState } from '../src/views/dcs/dcs-utils'
import { memoryChart } from '../src/views/dcs/memory-chart'
import type { DcsHistoryOut, DcsInstance } from '../src/types/dcs'

const request = vi.hoisted(() => ({ get: vi.fn(), download: vi.fn() }))
vi.mock('../src/api/request', () => request)
import { exportDcsMetricsHistory, getDcsMetricsHistory } from '../src/api/dcs'

const now = Date.parse('2026-11-02T00:00:00Z')
const instance: DcsInstance = { id: 1, resource_id: 'dcs-one', name: 'cache', tenant_id: 7, tenant_name: 'tenant',
  project_ref: 11, project_name: 'project', region: null, status_raw: 'RUNNING', memory_usage: 0,
  used_memory_mb: 0, max_memory_mb: 1000, used_cpu_seconds: 12000.56, snapshot_at: new Date(now).toISOString(),
  metrics_source: 'DCS', api_version: 'v2', source_endpoint: 'instances/statistic',
  metrics_observed_at: new Date(now).toISOString(), time_basis: 'RESPONSE_RECEIVED', freshness: 'FRESH', stale_after_sec: 600 }
const query = { resource_ids: [1], start_at: '2026-11-01T23:50:00Z', end_at: '2026-11-02T00:00:00Z', interval_minutes: 5 as const }

const data: DcsHistoryOut = { ...query, timezone: 'UTC', generated_at: new Date(now).toISOString(), demo: false,
  metrics: [{ key: 'used_cpu_seconds', label: 'CPU累计时间', unit: 's', aggregation: 'last' },
    { key: 'input_kbps', label: '网络入流量', unit: 'kbit/s', aggregation: 'mean' }],
  series: [{ instance, source: 'LOCAL', status: 'PARTIAL', warning: null, valid_points: 1, total_points: 2,
    summary: { average_usage: 0, peak_usage: 0, latest_usage: 0, latest_at: query.start_at },
    points: [{ at: query.start_at, memory_usage: 0, memory_max: 0, used_memory_mb: 0, max_memory_mb: 1000,
      used_cpu_seconds: 12000.56, input_kbps: 1.25, sample_count: 1 },
    { at: '2026-11-01T23:55:00Z', memory_usage: null, memory_max: null, used_memory_mb: null,
      max_memory_mb: null, sample_count: 0 }] }] }

describe('DCS V2 原生指标', () => {
  it('当前表明确获取时间与源端未知采样时间的区别', () => {
    const source = readFileSync(new URL('../src/views/dcs/DcsCurrentStatistics.vue', import.meta.url), 'utf8')
    expect(source).toContain('新鲜度 / 获取时间')
    expect(source).toContain('源端未提供采样时间；新鲜度按本地获取时间计算')
    expect(source).not.toContain('实际采样时间')
  })

  it('当前值以本地响应获取时间判断新鲜度，资源刷新时间不能续期', () => {
    expect(freshnessState(instance, now)).toBe('FRESH')
    expect(freshnessState(instance, now + 600000)).toBe('FRESH')
    expect(freshnessState(instance, now + 600001)).toBe('STALE')
    expect(freshnessState({ ...instance, freshness: 'LEGACY' }, now)).toBe('LEGACY')
    expect(freshnessState({ ...instance, metrics_source: 'NONE' }, now)).toBe('NO_DATA')
    expect(freshnessState({ ...instance, metrics_observed_at: null }, now)).toBe('NO_DATA')
    expect(freshnessState({ ...instance, metrics_observed_at: new Date(now + 31000).toISOString() }, now)).toBe('NO_DATA')
  })

  it('CPU图表使用累计秒；缺点保留null而非0或连线', () => {
    const chart = memoryChart(data, 'used_cpu_seconds')
    expect(chart.yAxis).toMatchObject({ name: 's', min: 0 })
    expect(chart.grid).toMatchObject({ outerBoundsMode: 'same', outerBoundsContain: 'all' })
    expect(chart.grid).not.toHaveProperty('containLabel')
    const lines = chart.series as { connectNulls: boolean; data: [number, number | null][] }[]
    expect(lines[0].data).toEqual([[Date.parse(query.start_at), 12000.56], [Date.parse('2026-11-01T23:55:00Z'), null]])
    expect(lines[0].connectNulls).toBe(false)
    const tooltip = chart.tooltip as { valueFormatter: (value: number) => string }
    expect(tooltip.valueFormatter(12000.56)).toBe('12000.56 s')
    expect(memoryChart(data, 'input_kbps').yAxis).toMatchObject({ name: 'kbit/s' })
  })

  it('新增历史/Excel合同复用同一筛选、租户头、取消信号', async () => {
    const context = { signal: new AbortController().signal, tenantId: null }
    await getDcsMetricsHistory(query, context)
    expect(request.get).toHaveBeenCalledWith('/dcs/metrics/history', query,
      { timeout: 120000, signal: context.signal, silent: true, headers: { 'X-Tenant-Id': 'all' } })
    request.download.mockResolvedValue('原生指标.xlsx')
    expect(await exportDcsMetricsHistory(query, context)).toBe('原生指标.xlsx')
    expect(request.download).toHaveBeenCalledWith('/dcs/metrics/export', query, 'DCS原生指标.xlsx',
      { timeout: 120000, signal: context.signal, silent: true, headers: { 'X-Tenant-Id': 'all' } })
  })
})

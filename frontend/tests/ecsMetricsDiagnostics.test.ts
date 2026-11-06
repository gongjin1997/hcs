import { afterEach, describe, expect, it } from 'vitest'
import type { TaskRun } from '../src/api'
import { mountBusinessScript } from './businessViewHarness'

const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
function statsText(code: string, stats: Record<string, unknown>, resourceTotal = 201) {
  const view = mountBusinessScript<{ statsText: (code: string, run: TaskRun) => string }>('system/TaskPanel.vue')
  dispose.push(view.unmount)
  return view.state.statsText(code, { stats, resource_total: resourceTotal } as TaskRun)
}

describe('ECS 运行记录指标诊断', () => {
  it('ecs_metrics 处理 201 台但零调用、零采样时仍显示来源、覆盖和缺失原因', () => {
    const text = statsText('ecs_metrics', {
      metrics_source: 'NONE', servers: 201, with_metrics: 0, sample_points: 0,
      metrics_diagnostic: { status: 'no_source', metrics: { cpu_util: { status: 'no_source', reason: 'prometheus_not_configured', point_count: 0, fresh_points: 0 } } },
    })
    expect(text).toContain('指标来源 仅服务原生')
    expect(text).toContain('0/201 台有采样')
    expect(text).toContain('采样点 0')
    expect(text).toContain('CPU 使用率：未配置 Prometheus 数据源')
  })

  it.each(['ecs_status', 'ecs_metrics'])('%s 保留有采样资源和采样点，支持部分覆盖', code => {
    const text = statsText(code, {
      metrics_source: 'PROMETHEUS', servers: 201, with_metrics: 3, sample_points: 7,
      metrics_diagnostic: { status: 'partial', metrics: {
        cpu_util: { status: 'ready', point_count: 3, fresh_points: 3 },
        mem_util: { status: 'no_series', reason: null, point_count: 0, fresh_points: 0 },
        disk_util: { status: 'partial', reason: 'incomplete_fresh_coverage', point_count: 4, fresh_points: 1 },
      } },
    })
    expect(text).toContain('指标来源 Prometheus · 3/201 台有采样')
    expect(text).toContain('采样点 7')
    expect(text).toContain('内存使用率：无匹配时序')
    expect(text).toContain('磁盘使用率：新鲜样本覆盖不完整')
    expect(text).not.toContain('CPU 使用率：')
  })

  it.each([
    ['aom_disabled', 'AOM 已停用'],
    ['legacy_ces_disabled', 'CES 已关闭'],
    ['metrics_source_disabled', '未启用外部指标来源'],
    ['source_not_configured', '未配置指标来源'],
    ['no_unambiguous_ip', '无可唯一匹配的 IP'],
    ['no_matching_series', '未匹配序列'],
    ['stale_samples', '只有过期样本'],
    ['metric_query_missing', '缺少指标查询配置'],
    ['query_failed', '指标查询失败'],
  ])('将缺失原因 %s 转为白名单中文', (reason, label) => {
    expect(statsText('ecs_metrics', { servers: 1, with_metrics: 0, sample_points: 0,
      metrics_diagnostic: { status: 'no_data', metrics: { cpu_util: { reason, status: 'no_data' } } },
    })).toContain(`CPU 使用率：${label}`)
  })

  it.each(['http://diagnostic.invalid/private?query=secret', 'constructor', '__proto__'])('未知字段 %s 不泄漏来源、查询、地址或动态指标名', unknown => {
    const text = statsText('ecs_metrics', {
      metrics_source: unknown, servers: 201, with_metrics: 0, sample_points: 0,
      metrics_diagnostic: { status: unknown, message: unknown, metrics: {
        [unknown]: { reason: unknown, status: unknown }, cpu_util: { reason: unknown, status: unknown, message: unknown },
      } },
    })
    expect(text).toContain('指标来源 未知来源')
    expect(text).toContain('CPU 使用率：原因未知')
    expect(text).not.toContain(unknown)
  })

  it('没有逐指标原因时显示汇总状态，没有新字段的历史记录保持可读', () => {
    expect(statsText('ecs_metrics', { servers: 0, with_metrics: 0, sample_points: 0,
      metrics_diagnostic: { status: 'no_selected_resources' },
    }, 0)).toContain('无选中资源')
    expect(statsText('ecs_status', { metrics_source: 'AOM', servers: 201, with_metrics: 0 }))
      .toBe('指标来源 AOM（已停用，历史） · 0/201 台有采样')
    expect(statsText('ecs_metrics', {})).toBe('')
    expect(statsText('cce_pod_events', { events_created: 2, probes: 4 })).toBe('新增事件 2 · 执行探针 4')
  })

  it('缺少或非法计数不伪造零值或输出任意文本', () => {
    const text = statsText('ecs_metrics', { metrics_source: 'PROMETHEUS', servers: 201,
      with_metrics: 'http://counter.invalid', sample_points: -1,
    })
    expect(text).toContain('—/201 台有采样')
    expect(text).toContain('采样点 —')
    expect(text).not.toContain('http://counter.invalid')
  })

  it('历史过期采样仍计入有采样资源和采样点，不宣称新鲜或有效', () => {
    const text = statsText('ecs_metrics', { metrics_source: 'PROMETHEUS', servers: 201, with_metrics: 201, sample_points: 603,
      metrics_diagnostic: { status: 'partial', metrics: {
        cpu_util: { reason: 'stale_samples', status: 'partial', point_count: 201, fresh_points: 0, stale_points: 201 },
        mem_util: { reason: 'stale_samples', status: 'partial', point_count: 201, fresh_points: 0, stale_points: 201 },
        disk_util: { reason: 'stale_samples', status: 'partial', point_count: 201, fresh_points: 0, stale_points: 201 },
      } },
    })
    expect(text).toContain('201/201 台有采样 · 采样点 603')
    for (const label of ['CPU 使用率', '内存使用率', '磁盘使用率']) expect(text).toContain(`${label}：只有过期样本`)
    expect(text).not.toMatch(/新鲜|有效资源|台有指标/)
  })
})

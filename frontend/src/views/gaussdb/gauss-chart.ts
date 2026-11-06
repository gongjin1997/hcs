import type { EChartsCoreOption } from 'echarts/core'
import type { GaussDiskHistoryOut, GaussMetricSeries } from '../../types/gaussdb'

export const finiteValue = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

function chartBase(unit: string): EChartsCoreOption {
  return {
    animation: false,
    color: ['#2563eb', '#94a3b8'],
    legend: { type: 'scroll', top: 0 },
    tooltip: { trigger: 'axis', renderMode: 'richText', valueFormatter: (value: unknown) => {
      const number = finiteValue(Array.isArray(value) ? value[1] : value)
      return number === null ? '—' : `${number.toLocaleString('zh-CN', { maximumFractionDigits: 3 })}${unit ? ` ${unit}` : ''}`
    } },
    grid: { left: 12, right: 24, top: 44, bottom: 18, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', min: 0, name: unit, splitLine: { lineStyle: { color: '#e8edf4' } } },
  }
}

export function gaussDiskChart(history: GaussDiskHistoryOut, mode: 'usage' | 'capacity'): EChartsCoreOption {
  const metrics = mode === 'usage'
    ? [{ key: 'usage_percent' as const, label: '存储使用率' }]
    : [{ key: 'used_gb' as const, label: '已用容量' }, { key: 'total_gb' as const, label: '总容量' }]
  return {
    ...chartBase(mode === 'usage' ? '%' : 'GB'),
    xAxis: { type: 'time', min: Date.parse(history.start_at), max: Date.parse(history.end_at), axisLabel: { hideOverlap: true } },
    series: metrics.map(metric => ({ id: metric.key, name: metric.label, type: 'line', smooth: false,
      connectNulls: false, showSymbol: true, symbolSize: 4,
      data: history.points.map(point => [Date.parse(point.timestamp), point.observed_at ? finiteValue(point[metric.key]) : null]),
    })),
  }
}

export function gaussReportChart(series: GaussMetricSeries): EChartsCoreOption {
  return {
    ...chartBase(series.unit ?? ''),
    series: [{ id: 'report', name: series.name, type: 'line', smooth: false, connectNulls: false, showSymbol: true, symbolSize: 5,
      data: series.points.filter(point => Number.isFinite(Date.parse(point.observed_at)))
        .map(point => [Date.parse(point.observed_at), finiteValue(point.value)]),
    }],
  }
}

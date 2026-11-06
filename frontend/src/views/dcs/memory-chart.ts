import type { EChartsCoreOption } from 'echarts/core'
import type { DcsChartMetric, DcsHistoryOut } from '../../types/dcs'
import { metricText } from './dcs-utils'

export function memoryChart(history: DcsHistoryOut, metric: DcsChartMetric): EChartsCoreOption {
  const definition = history.metrics?.find(item => item.key === metric)
  const unit = definition?.unit ?? '%'
  return {
    animation: false,
    legend: { type: 'scroll', top: 0, textStyle: { width: 180, overflow: 'truncate' } },
    grid: { top: 44, left: 55, right: 20, bottom: 38, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    tooltip: { trigger: 'axis', renderMode: 'richText', valueFormatter: (value: unknown) => metricText(Array.isArray(value) ? value[1] : value, ` ${unit}`) },
    xAxis: { type: 'time', min: Date.parse(history.start_at), max: Date.parse(history.end_at), axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: unit, min: 0, axisLabel: { formatter: `{value}${unit === '%' ? '%' : ''}` } },
    series: history.series.map(item => {
      const valid = item.points.map(point => typeof point[metric] === 'number' && Number.isFinite(point[metric]))
      const isolated = new Set(valid.flatMap((present, index) => present && !valid[index - 1] && !valid[index + 1] ? [index] : []))
      return {
      id: String(item.instance.id), name: `${item.instance.name} · #${item.instance.id}`,
      type: 'line',
      showSymbol: isolated.size > 0, symbol: 'circle',
      symbolSize: valid.filter(Boolean).length === 1 ? 6 : (_value: unknown, params: { dataIndex: number }) => isolated.has(params.dataIndex) ? 6 : 0,
      connectNulls: false,
      data: item.points.map(point => [Date.parse(point.at), point[metric] ?? null]),
      }
    }),
  }
}

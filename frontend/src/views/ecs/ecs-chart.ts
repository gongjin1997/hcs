import type { EChartsCoreOption } from 'echarts/core'
import type { EcsHistoryOut, EcsMetricKey } from '../../types/ecs'
import { ecsTimestampLabel, percentText } from './ecs-utils'
import { historicalMetricsSourceLabel } from '../../utils/dict'

export function ecsTrendChart(history: EcsHistoryOut, metric: EcsMetricKey, statistic: 'average' | 'maximum' = 'average'): EChartsCoreOption {
  return {
    animation: false,
    legend: { type: 'scroll', top: 0 },
    tooltip: { trigger: 'axis', renderMode: 'richText', valueFormatter: (value: unknown) => percentText(Array.isArray(value) ? value[1] : value) },
    grid: { top: 45, left: 12, right: 20, bottom: 20, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    xAxis: { type: 'time', min: Date.parse(history.start_at), max: Date.parse(history.end_at), axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value}%' } },
    series: history.series.filter(series => series.metric === metric).map((series, index) => ({
      id: `${series.metric}:${series.source}:${index}`, name: `${series.label} · ${historicalMetricsSourceLabel(series.source)} · ${ecsTimestampLabel(series.timestamp_semantics)}`,
      type: 'line', connectNulls: false, showSymbol: true, symbolSize: 4,
      data: series.points.map(point => [Date.parse(point.at), point.sample_count > 0 && typeof point[statistic] === 'number' && Number.isFinite(point[statistic]) ? point[statistic] : null]),
    })),
  }
}

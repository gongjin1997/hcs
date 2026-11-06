import { describe, expect, it, vi } from 'vitest'
import { detailText } from '../src/views/job-reports/report-utils'
import { JOB_STATUSES, METRIC_LABELS, PANEL_CONFIG, SOURCE_FACTS, statusMeta } from '../src/views/job-reports/report-config'
import { exportJobReport, fetchJobReport } from '../src/api/job-reports'
import { download, get } from '../src/api/request'
import type { JobReportQuery } from '../src/types/job-reports'

vi.mock('../src/api/request', () => ({ get: vi.fn(), download: vi.fn() }))

describe('PDF 作业报表边界复核 20261102', () => {
  it('shows the documented paused batch state in filters and labels', () => {
    expect(JOB_STATUSES.PAUSED?.label).toBe('暂停')
    expect(PANEL_CONFIG.batch.statuses).toContain('PAUSED')
    expect(PANEL_CONFIG.dgc.statuses).toContain('PAUSED')
  })

  it.each(['mrs_job', 'dgc_realtime_job'] as const)('keeps unverified numeric dates raw for %s', resourceType => {
    expect(detailText('start_time', 1792886400, resourceType)).toBe('1792886400')
  })

  it('shows the local 24h P90 and its sample provenance', () => {
    expect(METRIC_LABELS.p90_minutes).toContain('24小时')
    expect(SOURCE_FACTS.dgc_batch_job.fields).toContain('batch_baseline')
  })

  it('keeps unknown canonical-looking source states neutral', () => {
    expect(statusMeta('UNRECOGNIZED:SUCCEEDED')).toMatchObject({ label: '未识别：SUCCEEDED', type: 'info' })
  })

  it('sends every supported report filter identically to list and export', async () => {
    const query = { keyword: ' same ', status: ['FAILED'], status_raw: ['fail'], engine: ' Flink ',
      resource_type: 'dgc_batch_job', job_type: 'BATCH', project_ref: 11, lifecycle: 'ALL',
      has_alarms: true, page: 3, page_size: 20 } as JobReportQuery
    const request = { signal: new AbortController().signal, tenantId: 1 }
    await fetchJobReport('batch', query, request)
    await exportJobReport('batch', query, request)
    const list = vi.mocked(get).mock.calls.at(-1)?.[1] as Record<string, unknown>
    const exported = vi.mocked(download).mock.calls.at(-1)?.[1]
    const { page: _page, page_size: _size, ...filters } = list
    expect(exported).toEqual(filters)
    expect(exported).toMatchObject({ project_ref: 11, lifecycle: 'ALL', has_alarms: true })
  })
})

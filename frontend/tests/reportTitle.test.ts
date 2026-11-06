import { describe, expect, it } from 'vitest'
import { reportPeriodName } from '../src/views/reports/report-title'

describe('report calendar labels', () => {
  it('uses the report date instead of the browser timezone date', () => {
    expect(reportPeriodName('HCS 巡检日报（2026-10-26）', '日报')).toBe('日报 · 2026-10-26')
  })
  it('keeps both dates of a weekly report', () => {
    expect(reportPeriodName('HCS 巡检周报（2026-10-19 ~ 2026-10-25）', '周报')).toBe('周报 · 2026-10-19 ~ 2026-10-25')
  })
  it('keeps the reporting month', () => {
    expect(reportPeriodName('HCS 巡检月报（2026 年 10 月）', '月报')).toBe('月报 · 2026 年 10 月')
  })
  it('preserves ledger snapshot time', () => {
    expect(reportPeriodName('资源台账报表（2026-10-27 09:30）', '台账')).toBe('台账 · 2026-10-27 09:30')
  })
  it('does not mistake parentheses in a tenant name for the period', () => {
    expect(reportPeriodName('HCS 巡检日报 · 租户（生产）（2026-10-26）', '日报')).toBe('日报 · 2026-10-26')
  })
  it('preserves a custom title without a date suffix', () => {
    expect(reportPeriodName('手工导入的历史日报', '日报')).toBe('手工导入的历史日报')
  })
})

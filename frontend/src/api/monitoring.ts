import { get } from './request'
import { scopeHeaders } from './scope'
import type { MonitoringCoverage } from '@/types/monitoring'

export const fetchMonitoringCoverage = (tenantId: number | null, signal: AbortSignal) =>
  get<MonitoringCoverage>('/monitoring/coverage', { tenant_id: tenantId ?? undefined }, {
    signal, silent: true, headers: scopeHeaders(tenantId),
  })

import { post } from './request'
import { scopeHeaders } from './scope'
import type { ImportResult } from '../types'
import type { LedgerImportContext } from '../views/ledger/useLedgerImport'

export function importLedgerFile(file: File, dryRun: boolean, skipErrors: boolean, { tenantId, signal }: LedgerImportContext) {
  const body = new FormData()
  body.append('file', file)
  return post<ImportResult>('/resources/import', body, {
    params: { dry_run: dryRun, skip_errors: skipErrors }, timeout: 120000, silent: true, signal,
    headers: scopeHeaders(tenantId),
  })
}

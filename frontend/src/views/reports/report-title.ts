/** The report title already contains dates in the reporting timezone. */
export function reportPeriodName(title: string, kindLabel: string): string {
  const period = /[（(]([^（）()]*)[）)]$/.exec(title)
  return period ? `${kindLabel} · ${period[1]}` : title
}


export function reportRegenerationDate(kind: string, platformPeriodLabel: string): string | undefined {
  if (kind === 'LEDGER') return undefined
  const date = /^(\d{4}-\d{2}-\d{2})(?:\s|$)/.exec(platformPeriodLabel)?.[1]
  if (!date) throw new Error('报表缺少平台周期日期，请重新选择周期生成')
  return date
}

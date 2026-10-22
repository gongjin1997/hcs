import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

export const fmtTime = (v?: string | null, f = 'MM-DD HH:mm:ss') => (v ? dayjs(v).format(f) : '-')
export const fromNow = (v?: string | null) => (v ? dayjs(v).fromNow() : '-')

export function humanize(sec: number): string {
  sec = Math.max(0, Math.round(sec))
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  if (h < 24) return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`
}
export const durationOf = (from?: string | null, to?: string | null) =>
  from ? humanize(dayjs(to || undefined).diff(dayjs(from), 'second')) : '-'

export function downloadCsv(filename: string, header: string[], rows: (string | number | null | undefined)[][]) {
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const csv = '\ufeff' + [header, ...rows].map((r) => r.map(esc).join(',')).join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}

import type { JobPanel } from '@/types/job-reports'

export const JOB_PERMISSION = 'report:view'
export const JOB_MENU_PATH = '/job-reports/dgc'

/** 旧 URL、页内入口及路由元数据共用；具体报表口径仍由原面板配置负责。 */
export const JOB_NAVIGATION = ([
  { panel: 'dgc', label: 'DGC 作业' },
  { panel: 'flink', label: 'Flink 作业' },
  { panel: 'realtime', label: 'DGC 实时作业' },
  { panel: 'batch', label: 'DGC 批作业' },
  { panel: 'mrs', label: 'MRS 作业' },
] satisfies { panel: JobPanel; label: string }[]).map(item => ({
  ...item,
  path: `/job-reports/${item.panel}`,
  meta: { title: `${item.label}报表`, perm: JOB_PERMISSION, activeMenu: JOB_MENU_PATH },
}))

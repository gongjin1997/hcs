import type { GuideField, ResourceGuide } from '@/types/guides'

export const FIELD_GROUPS = [
  { key: 'metrics', label: '监控快照', prefix: 'metrics.', description: '保存在资源上的监控快照；缺失不能当作 0。' },
  { key: 'facts', label: '规则事实', prefix: '', description: '只在对应 check_item 中可用；不是所有快照都能作为规则变量。' },
  { key: 'attributes', label: '扩展属性', prefix: 'attrs.', description: '资源配置与状态补充，包含只展示而未采集的旧字段说明。' },
  { key: 'ledger_fields', label: '台账与容量', prefix: '', description: '顶层配置值；配置容量不等于已用量、可用量或使用率。' },
] as const

export function filterGuideFields(fields: GuideField[], search: string): GuideField[] {
  const terms = search.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return fields
  return fields.filter((field) => {
    const text = [field.key, field.label, field.unit, field.meaning, field.source, field.data_type,
      ...field.check_items, ...field.limitations,
      field.availability === 'not_collected' ? '未采集' : field.availability === 'derived' ? '平台计算' : '',
      field.optional ? '可缺失' : ''].filter(Boolean).join(' ').toLocaleLowerCase()
    return terms.every((term) => text.includes(term))
  })
}

export function guideFieldGroups(guide: ResourceGuide, search = '') {
  return FIELD_GROUPS.map((group) => ({
    ...group,
    total: guide[group.key].length,
    fields: filterGuideFields(guide[group.key], search),
  }))
}

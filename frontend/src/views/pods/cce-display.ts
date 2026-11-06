export function cceState(value?: string | null): string {
  const labels: Record<string, string> = {
    fresh: '有效', stale: '已过期', unknown: '未知', refresh_failed: '刷新失败',
    available: '可用', partial: '部分可用', disabled: '未启用', unsupported: '不支持',
    forbidden: '无权限', error: '查询失败', healthy: '正常', abnormal: '异常', transition: '变更中',
    Active: '运行中', Abnormal: '不可用', Error: '错误', Build: '创建中', Installing: '安装中',
    Upgrading: '升级中', Deleting: '删除中', Available: '可用', Unavailable: '不可用',
  }
  return value ? labels[value] ?? value : '未知'
}

export function cceCondition(type: string | null, status: string): string {
  const name: Record<string, string> = { Scalable: '允许扩容', QuotaInsufficient: '配额不足',
    ResourceInsufficient: '资源不足', UnexpectedError: '扩容失败', Error: '节点池错误' }
  return `${type ? name[type] ?? type : '条件'}：${status === 'True' ? '是' : status === 'False' ? '否' : '未知'}`
}
